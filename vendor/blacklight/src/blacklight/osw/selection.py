"""
jdcore 公共层：osw_selection（**选品CMS：公共商品池 / 认领 / 驳回 / 选品任务 / 待铺货**，osw 采销工作台）。

osw 商品列表页的「公共商品池 / 待铺货」等 tab，后端是独立跨域微应用
`jx-zy-selectioncms-pro.pf.jd.com`，走 `api.m.jd.com` 开放网关、`appid=selectioncms`。
逆向笔记见 osw-mcp/NOTES_selection.md。

★签名可复用：selectioncms 与 markettool 的 wqadmin **同一 secret**（实测 signStr 逐字节吻合），
  直接借 osw_margin._build_body（HMAC-MD5 排序拼接）。body 固定 env/buid/appCode/time/signStr，cookie-only。

选品流程（8 步）：①发起选品(猎物) ②寻源(猎人) ③品拉审核(猎枪) ④三猎确认 ⑤系统审核 ⑥审批中 ⑦待上品(待铺货) ⑧选品完成。
三个列表接口分别对应不同环节：pool_list=待认领，task_list=我的全部任务，bid_item_list=认领成功待铺货。

能力：
  - pool_list：公共商品池（listVenderTask，供应商提报待认领的标的）
  - task_list：选品任务列表（getProjectList，我名下已认领、在流程流转的任务，带三猎确认进度）
  - bid_item_list：待铺货列表（getBidItemList，认领成功待上品，带京东价/毛利率区间）
  - project_detail：单标的详情（getProjectById，认领页数据 + 同规格成本一致性体检 specWarnings）
  - proxy_roles / erp_info：可代理采销角色 / erp 信息
  - 认领(claim)：modifyProject(opt:1)，回填猎人/猎枪/售价(可按 采购价上限×倍率→.99 建议) —— dry-run + confirm_token + @audited
  - 驳回(reject)：refuseVenderTask(vprojId/refuseType/checkMsg) —— dry-run + confirm_token + @audited
  - （待补）铺货写：待铺货 authorityList 的 adopt/copyWare 动作未抓包

只依赖 jd_auth / jd_core / osw_margin(仅借签名)，不反向依赖任何业务 MCP 场域。
"""
from __future__ import annotations

import copy
import json as _json
import math as _math
import re as _re
from typing import Optional

from blacklight.core import auth as jd_auth
from blacklight.osw import margin as osw_margin # 仅借签名：_build_body（同一 secret）
from blacklight.core import BlacklightError, make_client, confirm_token as _confirm_token, audited

# --------------------------------------------------------------------------- #
# 网关 / 常量
# --------------------------------------------------------------------------- #
SEL_API = "https://api.m.jd.com/api"
SEL_ORIGIN = "https://jx-zy-selectioncms-pro.pf.jd.com"
SEL_COMMON = {"appid": "selectioncms", "channel": "jxh5", "clientVersion": "1.2.5",
              "client": "jxh5", "cthr": "1", "loginType": "7"}
BUID = 325
APP_CODE = "msc588d6d5"

# 驳回原因枚举（来自 detail.checkRefuseTypeList，2026-07-22 抓包）
REFUSE_TYPES = {
    20: "采购价没有竞争力，请商家修改价格提交",
    30: "商品信息填写错误，请商家重新修改提交",
    40: "大店已有同款，请确认无同款后提交",
}


# --------------------------------------------------------------------------- #
# 底层请求（签名复用 osw_margin，cookie 复用 jd_auth）
# --------------------------------------------------------------------------- #
def _cookie() -> str:
    ck = jd_auth.get_cookie()
    if not ck:
        raise BlacklightError("无登录态：先 osw_login / yx_login，或设 JD_COOKIE 环境变量")
    return ck


def _visitkey(cookie: str) -> str:
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "visitkey":
            return v
    return ""


def _call(function_id: str, body: dict, method: str = "GET") -> dict:
    """签名 + 发送一次 selectioncms 请求 → 校验 code==0 → 返回 data。
    GET：参数走 query（读接口）；POST：form-urlencoded（写接口 modifyProject/refuseVenderTask）。"""
    ck = _cookie()
    signed = osw_margin._build_body({**(body or {}), "buid": BUID, "appCode": APP_CODE})
    params = dict(SEL_COMMON)
    params.update({"functionId": function_id, "t": str(signed["time"]), "uuid": _visitkey(ck),
                   "body": _json.dumps(signed, separators=(",", ":"), ensure_ascii=False)})
    c = make_client(ck, origin=SEL_ORIGIN, referer=SEL_ORIGIN + "/", content_type=None, timeout=20.0)
    with c:
        if method.upper() == "POST":
            r = c.post(SEL_API, data=params, headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            r = c.get(SEL_API, params=params, headers={"Content-Type": "text/plain"})
    r.raise_for_status()
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"{function_id} 未返回 JSON（HTTP {r.status_code}）——登录态可能失效，请 osw_login") from e
    if j.get("code") not in (0, "0"):
        raise BlacklightError(f"{function_id}: {j.get('msg') or j.get('message') or ('code=' + str(j.get('code')))}")
    return j.get("data")


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _spec_key(sku_name: Optional[str], sale_attr: Optional[str]) -> Optional[str]:
    """从 SKU 名抽「规格键」= **尺寸/规格信号的组合**（号型/容量/斤数/轮数/装数/长宽高维度），用于「同规格异价」检查。
    扫全名收集所有尺寸信号 token 拼成 key（**颜色/图案/描述词不匹配任何尺寸模式，自然被排除**）；
    提不出任何尺寸信号→返回 None(不分组,避免误报)。这样：同号型不同色→同 key(同价则不报)；不同容量/装数→不同 key(不误报)。"""
    s = (sku_name or sale_attr or "").strip()
    if not s:
        return None
    toks = []
    # 1) 尺寸维度括号（含 长/宽/高/直径/cm/尺寸）——真·尺寸，保留
    for m in _re.findall(r"[【（(\[]([^】）)\]]*(?:长|宽|高|直径|cm|CM|mm|尺寸)[^】）)\]]*)[】）)\]]", s):
        toks.append(_re.sub(r"\s+", "", m))
    # 2) 容量/量：NNL / NN升 / NN斤 / NNml / NNkg / NN寸
    toks += [_re.sub(r"\s+", "", x) for x in _re.findall(r"\d+(?:\.\d+)?\s*(?:L|升|斤|ml|ML|kg|KG|寸)", s)]
    # 3) 轮数
    toks += [_re.sub(r"\s+", "", x) for x in _re.findall(r"\d+\s*轮", s)]
    # 4) 装数/数量（不同装数是不同规格，本就不同价）
    toks += [_re.sub(r"\s+", "", x) for x in _re.findall(r"\d+\s*(?:个|双|对|套|件|包|只|条)", s)]
    # 5) 号型档位（长优先匹配，特大号不被吞成大号）
    toks += _re.findall(r"特大号|超大号|加大号|加小号|大号|中号|小号|[SsMmLl]码", s)
    if not toks:
        return None                                  # 无尺寸信号→不分组，避免颜色/图案类误报
    return "+".join(sorted(set(toks)))               # 顺序无关，去重


# 同规格需一致的成本字段（字段名: 中文标签）。材质/包装/单位不在 getProjectById 返回里（采销表单填），暂无法比对。
_SPEC_CMP_FIELDS = [("supplyAll", "采购价上限"), ("skuCost", "商品成本"),
                    ("expressCost", "快递成本"), ("expressPack", "物流打包成本"),
                    ("materialsCost", "耗材成本")]


def _spec_consistency(skus: list) -> list:
    """检测「同规格（尺寸大小）成本字段不一致」。skus=project_detail 的 skus 列表。
    对每个规格组，逐字段（采购价上限/商品成本/快递/打包/耗材）比对，**任一字段有分歧就报**。
    返回 [{spec, diffs:[{field,label,distinct,spread}], skus:[{venderSkuId,skuName,各成本}]}]（只列有分歧的组）。
    ⚠️材质/包装/单位不在数据源（采销表单填），此处不覆盖。"""
    groups: dict = {}
    for s in skus:
        key = _spec_key(s.get("skuName"), s.get("saleAttributes"))
        if key is None:
            continue
        groups.setdefault(key, []).append(s)
    warnings = []
    for spec, items in groups.items():
        if len(items) < 2:
            continue
        diffs = []
        for field, label in _SPEC_CMP_FIELDS:
            vals = [x.get(field) for x in items if x.get(field) is not None]
            distinct = sorted({round(float(v), 2) for v in vals})
            if len(distinct) > 1:
                diffs.append({"field": field, "label": label, "distinct": distinct,
                              "spread": round(distinct[-1] - distinct[0], 2)})
        if diffs:
            warnings.append({
                "spec": spec, "diffFields": [d["label"] for d in diffs], "diffs": diffs,
                "skus": [{"venderSkuId": x.get("venderSkuId"), "skuName": x.get("skuName"),
                          "采购价上限": x.get("supplyAll"), "商品成本": x.get("skuCost"),
                          "快递成本": x.get("expressCost"), "物流打包成本": x.get("expressPack"),
                          "耗材成本": x.get("materialsCost")} for x in items],
            })
    return warnings


def _trunc6(x: float) -> str:
    """截断到 6 位小数的字符串（匹配平台 grossRate 观测口径，非四舍五入）。"""
    neg = x < 0
    x = abs(x)
    s = f"{int(x * 1_000_000) / 1_000_000:.6f}"
    return ("-" + s) if neg else s


PRICE_MULTIPLIER_DEFAULT = 1.8   # 默认建议售价倍率（对采购价上限 supplyAll）


def _round_up_99(x: float) -> float:
    """向上取到「末尾 .99」：返回 ≥x 的最小 n.99（n 为整数）。如 30.78→30.99、31.00→31.99、30.99→30.99。"""
    n = _math.ceil(round(x, 2) - 0.99 - 1e-9)
    return round(n + 0.99, 2)


def _suggest_price(supply_all, multiplier: float = PRICE_MULTIPLIER_DEFAULT) -> Optional[float]:
    """建议售价 = 采购价上限(supplyAll) × 倍率 → 末尾向上取 .99。supply_all 缺则 None。"""
    c = _num(supply_all)
    if c is None:
        return None
    return _round_up_99(c * float(multiplier))


# --------------------------------------------------------------------------- #
# 读：公共商品池列表
# --------------------------------------------------------------------------- #
def _pool_row(raw: dict) -> dict:
    ci = raw.get("categoryInfo") or {}
    return {
        "vprojectId": raw.get("vprojectId"),                 # 标的ID（VPROJ…）
        "spuId": raw.get("spuId"), "spuName": raw.get("spuName"), "spuImage": raw.get("spuImage"),
        "venderId": raw.get("venderId"), "venderName": raw.get("venderName"),
        "shopName": raw.get("shopName"),                     # 供应商工厂店
        "sellerId": raw.get("sellerId"), "sellerName": raw.get("sellerName"),   # 所属店铺
        "categoryName": raw.get("categoryName"),
        "categoryId3": ci.get("categoryId3"),
        "status": raw.get("status"), "decisionType": raw.get("decisionType"),
        "canClaim": raw.get("canClaim"), "needReclaim": raw.get("needReclaim"),
        "createTime": raw.get("createTime"),                 # 提报时间
        "preyName": raw.get("preyName"), "hunterName": raw.get("hunterName"), "shotgunName": raw.get("shotgunName"),
        "appointSellerErp": raw.get("appointSellerErp"),
        "checkFailMsg": raw.get("checkFailMsg"),             # 审核信息（JSON 串）
    }


def pool_list(page: int = 1, page_size: int = 20, create_from: Optional[str] = None,
              create_to: Optional[str] = None, category_info: Optional[dict] = None,
              status: Optional[int] = None, name: Optional[str] = None,
              vender_id: Optional[str] = None, vender_spu: Optional[str] = None) -> dict:
    """公共商品池分页列表（listVenderTask）。供应商提报的标的等采销认领/驳回。
    create_from/create_to='YYYY-MM-DD HH:MM:SS'（提报时间）；status=状态过滤；name=品名模糊。"""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    body = {"pageIndex": page, "pageSize": page_size, "categoryInfo": category_info or {}}
    if create_from:
        body["createBeginTime"] = create_from
    if create_to:
        body["createEndTime"] = create_to
    if status is not None:
        body["status"] = int(status)
    if name:
        body["itemName"] = name
    if vender_id:
        body["venderId"] = str(vender_id)
    if vender_spu:
        body["venderSpu"] = str(vender_spu)
    data = _call("jxzy_selection_listVenderTask", body) or {}
    rows = [_pool_row(x) for x in (data.get("list") or [])]
    total = data.get("total")
    return {"total": total, "page": page, "page_size": page_size,
            "pages": (int(total) + page_size - 1) // page_size if isinstance(total, int) else None,
            "count": len(rows), "rows": rows}


# --------------------------------------------------------------------------- #
# 读：选品任务列表（我名下已认领、在选品8步流程流转的任务）—— getProjectList
# --------------------------------------------------------------------------- #
# 选品8步流程（用于 status 参照）：①发起选品(猎物) ②寻源(猎人) ③品拉审核(猎枪) ④三猎确认
#   ⑤系统审核 ⑥审批中 ⑦待上品(待回填sku=待铺货) ⑧选品完成
# 观测 status 码：40/75/76/80…（行内无 status 名，确切 码↔步 映射待确认）。
# ★用三猎确认标志判进度**可靠**（不依赖 status 码猜测），透出「卡在谁那」。
def _confirm_stage(prey, hunter, shotgun, check_fail=0) -> str:
    """按 preyConfirm/hunterConfirm/shotgunConfirm 推导当前卡在哪步（可靠，不猜 status 码）。"""
    if check_fail:
        return "审核驳回(checkFail)"
    if not prey:
        return "①发起选品(待猎物提交)"
    if not hunter:
        return "②寻源(待猎人确认)"
    if not shotgun:
        return "③品拉审核(待猎枪确认)"
    return "④三猎确认完成(进入⑤系统审核及后续)"


def _task_row(raw: dict) -> dict:
    return {
        "projectId": raw.get("projectId"),               # = vprojectId（可传 osw_pool_detail 下钻）
        "itemName": raw.get("itemName"), "categoryName": raw.get("categoryName"),
        "status": raw.get("status"), "checkFailStatus": raw.get("checkFailStatus"),
        "createTime": raw.get("createTime"),
        "sellerName": raw.get("sellerName"),
        "preyName": raw.get("preyName"), "hunterName": raw.get("hunterName"), "shotgunName": raw.get("shotgunName"),
        "preyConfirm": raw.get("preyConfirm"), "hunterConfirm": raw.get("hunterConfirm"),
        "shotgunConfirm": raw.get("shotgunConfirm"),     # 三猎确认进度（判到第几步）
        "stage": _confirm_stage(raw.get("preyConfirm"), raw.get("hunterConfirm"),
                                raw.get("shotgunConfirm"), raw.get("checkFailStatus")),  # 卡在哪步(可靠)
        "taskVersion": raw.get("taskVersion"),
        "authorityList": raw.get("authorityList"),       # 本任务可操作权限 read/edit/delete
        "organization": raw.get("organization"),
    }


def task_list(page: int = 1, page_size: int = 50, status="", query_type: str = "0") -> dict:
    """选品任务列表（getProjectList）——**我名下已认领、在选品8步流程流转的任务**（区别于公共商品池的待认领标的）。
    status: 阶段码过滤(''=全部；观测 40/75/76/80…，确切码待确认，待铺货=⑦待上品)；queryType 默认 '0'。
    每行含三猎确认标志(preyConfirm/hunterConfirm/shotgunConfirm)可判进度。projectId 可传 project_detail 下钻。"""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    data = _call("jxzy_selection_getProjectList",
                 {"status": ("" if status in (None, "") else str(status)),
                  "pageIndex": page, "pageSize": page_size, "queryType": str(query_type)}) or {}
    rows = [_task_row(x) for x in (data.get("projectList") or [])]
    total = data.get("totalRecord")
    return {"total": total, "page": page, "page_size": page_size,
            "pages": (int(total) + page_size - 1) // page_size if isinstance(total, int) else None,
            "count": len(rows), "rows": rows}


# --------------------------------------------------------------------------- #
# 读：待铺货列表（认领成功、待上品/铺货的商品）—— getBidItemList
# --------------------------------------------------------------------------- #
def _bid_row(raw: dict) -> dict:
    return {
        "projectId": raw.get("projectId"),
        "itemName": raw.get("itemName"), "categoryName": raw.get("categoryName"),
        "spuImage": raw.get("spuImage"),
        "status": raw.get("status"), "checkFailStatus": raw.get("checkFailStatus"),
        "createTime": raw.get("createTime"),
        "createShopName": raw.get("createShopName"),           # 供应商工厂店
        "createSupplierSpuId": raw.get("createSupplierSpuId"),  # 商家SPU
        "preyName": raw.get("preyName"), "hunterName": raw.get("hunterName"), "shotgunName": raw.get("shotgunName"),
        "preyConfirm": raw.get("preyConfirm"), "hunterConfirm": raw.get("hunterConfirm"),
        "shotgunConfirm": raw.get("shotgunConfirm"),
        "stage": _confirm_stage(raw.get("preyConfirm"), raw.get("hunterConfirm"),
                                raw.get("shotgunConfirm"), raw.get("checkFailStatus")),
        "jdPriceMin": raw.get("jdPriceMin"), "jdPriceMax": raw.get("jdPriceMax"),          # 京东价区间
        "profitMarginMin": raw.get("profitMarginMin"), "profitMarginMax": raw.get("profitMarginMax"),  # 毛利率区间
        "addPriceRateMin": raw.get("addPriceRateMin"), "addPriceRateMax": raw.get("addPriceRateMax"),   # 加价率区间
        "categoryLimitMaxPrice": raw.get("categoryLimitMaxPrice"),   # 类目限价
        "jdPriceLimitSkuCount": raw.get("jdPriceLimitSkuCount"),
        "authorityList": raw.get("authorityList"),             # 含 adopt(采纳)/copyWare(铺货/复制商品) 表示可铺货
        "similarProductInfoList": raw.get("similarProductInfoList"),  # 同款信息
        "taskVersion": raw.get("taskVersion"),
    }


def bid_item_list(page: int = 1, page_size: int = 20, status="", query_type: str = "0") -> dict:
    """待铺货列表（getBidItemList）——**认领成功、待上品/铺货的商品**（osw 商品列表页「待铺货」tab）。
    每行带 京东价区间/毛利率区间/加价率区间/类目限价/同款信息，authorityList 含 adopt/copyWare 表示可铺货。
    status: 阶段码过滤(''=全部)；projectId 可传 project_detail 下钻。"""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    data = _call("jxzy_selection_getBidItemList",
                 {"status": ("" if status in (None, "") else str(status)),
                  "pageIndex": page, "pageSize": page_size, "queryType": str(query_type)}) or {}
    rows = [_bid_row(x) for x in (data.get("bidItemList") or [])]
    total = data.get("totalRecord")
    return {"total": total, "page": page, "page_size": page_size,
            "pages": (int(total) + page_size - 1) // page_size if isinstance(total, int) else None,
            "count": len(rows), "rows": rows}


# --------------------------------------------------------------------------- #
# 读：单标的详情（认领页数据模型）
# --------------------------------------------------------------------------- #
def project_detail(project_id: str, raw: bool = False) -> dict:
    """单标的详情（getProjectById，operateType:1=认领页）。返回整洁摘要；raw=True 附完整原始 data。
    含 SPU/SKU（skuBom 成本：supplyAll 单件总成本 / skuCost / expressCost / materialsCost）、
    versionInfo(乐观锁)、roleInfo、buttonInfo(可否 submit/reject)、checkRefuseTypeList(驳回原因枚举)。
    含 specWarnings（同规格·成本字段不一致体检）。⚠️skuBom 无 singlePrice → 售价认领时由 采购价上限×倍率→.99 建议或手填。"""
    data = _call("jxzy_selection_getProjectById",
                 {"projectId": str(project_id), "projectType": 0, "operateType": 1}) or {}
    pi = data.get("projectInfo") or {}
    skus = []
    for spu in (pi.get("productInfo") or []):
        for it in (spu.get("itemInfoList") or []):
            bom = it.get("skuBom") or {}
            rem = it.get("skuRemark") or {}
            skus.append({
                "skuIdx": it.get("skuIdx"),
                "venderSkuId": rem.get("venderSkuId"),        # 商家SKUID（认领 price_map 的 key）
                "skuName": rem.get("skuName"), "saleAttributes": rem.get("saleAttributes"),
                "unit": rem.get("unit"),
                "supplyAll": _num(bom.get("supplyAll")),       # 采购价上限（单件总成本，认领 singleCost 用它）
                "skuCost": _num(bom.get("skuCost")),           # 商品成本
                "expressCost": _num(bom.get("expressCost")),   # 快递成本
                "expressPack": _num(bom.get("expressNanual")), # 物流打包成本（源字段名 expressNanual，含拼写）
                "materialsCost": _num(bom.get("materialsCost")),  # 耗材成本
                "supplyCount": bom.get("supplyCount"),
            })
    summary = {
        "projectId": pi.get("projectId") or str(project_id),
        "status": data.get("status"),
        "sellerId": data.get("sellerId"), "sellerName": pi.get("sellerName"),
        "itemName": pi.get("itemName"), "categoryName": pi.get("categoryName"),
        "venderId": pi.get("venderId"), "projectType": pi.get("projectType"),
        "roleInfo": data.get("roleInfo") or {},
        "versionInfo": data.get("versionInfo") or {},
        "buttonInfo": data.get("buttonInfo") or {},          # {submit:1, reject:1}
        "refuseTypes": data.get("checkRefuseTypeList") or [],
        "skuCount": len(skus), "skus": skus,
        # ★同规格一致性体检：同尺寸大小、成本字段(采购价上限/商品成本/快递/打包/耗材)不一致 → 常见驳回理由(refuseType 30)
        "specWarnings": _spec_consistency(skus),
    }
    if raw:
        summary["_raw"] = data
    return summary


# --------------------------------------------------------------------------- #
# 读：可代理的采销角色 / erp 信息
# --------------------------------------------------------------------------- #
def proxy_roles() -> dict:
    """可代理的采销角色列表（queryProxyRoleList）。用于认领时选 猎人/猎枪 的候选。"""
    data = _call("jxzy_selection_queryProxyRoleList", {}) or {}
    return {"proxyRoleList": data.get("proxyRoleList") or []}


def erp_info(erp: str) -> dict:
    """按 erp 取采销信息（getErpInfo）→ {erp, realName, organizationName, …}。认领填猎人/猎枪 name 用。"""
    if not (erp or "").strip():
        raise BlacklightError("erp 不能为空")
    return _call("jxzy_selection_getErpInfo", {"erp": str(erp).strip()}) or {}


def _erp_name(erp: str) -> str:
    try:
        return (erp_info(erp).get("realName") or "").strip()
    except BlacklightError:
        return ""


# --------------------------------------------------------------------------- #
# 写：认领（modifyProject opt:1）—— 回填猎人/猎枪/售价再提交
# --------------------------------------------------------------------------- #
def _build_claim_body(detail_raw: dict, hunter_erp: str, shotgun_erp: str,
                      price_map: dict = None, multiplier: float = PRICE_MULTIPLIER_DEFAULT,
                      remark: str = "") -> tuple:
    """用 getProjectById 原始 data 回填 猎人/猎枪/售价，组装 modifyProject 提交体。
    返回 (body, preview_rows)。price_map={venderSkuId: 售价}（可选）——未给的 SKU 自动按
    建议价=采购价上限×multiplier→末尾向上取.99；缺采购价上限又未给价的 SKU 才报缺。"""
    project = copy.deepcopy(detail_raw.get("projectInfo") or {})
    version = detail_raw.get("versionInfo") or {}
    role = copy.deepcopy(detail_raw.get("roleInfo") or {})

    hunter_erp = str(hunter_erp).strip()
    shotgun_erp = str(shotgun_erp).strip()
    if not hunter_erp or not shotgun_erp:
        raise BlacklightError("hunter_erp / shotgun_erp（猎人/猎枪 erp）均必填")
    role["hunterId"] = hunter_erp
    role["hunterName"] = _erp_name(hunter_erp) or role.get("hunterName") or ""
    role["shotgunId"] = shotgun_erp
    role["shotgunName"] = _erp_name(shotgun_erp) or role.get("shotgunName") or ""

    pm = {str(k): float(v) for k, v in (price_map or {}).items()}
    preview, missing = [], []
    for spu in (project.get("productInfo") or []):
        for it in (spu.get("itemInfoList") or []):
            bom = it.get("skuBom") or {}
            rem = it.get("skuRemark") or {}
            vsku = str(rem.get("venderSkuId"))
            cost = _num(bom.get("supplyAll"))                  # 采购价上限（单件总成本）
            if vsku in pm:
                price, src = round(pm[vsku], 2), "手填"
            else:
                sug = _suggest_price(cost, multiplier)
                if sug is None:
                    missing.append(vsku)
                    continue
                price, src = sug, f"建议(×{multiplier}→.99)"
            costv = cost or 0.0
            gross = round(price - costv, 2)
            # singlePrice/singleCost 是灰度前端的「输入态」冗余字段，猎物/猎枪端提交时都为 "0"。
            # 真实价/成本由 jdPrice / supplyAll / skuCost / gross 承载。★2026-07-24：认领存 supplyAll
            # 会与猎枪端「通过」提交的 0 不一致，触发灰度新审批规则「采购价相关成本不允许修改」→ 必须存 "0"。
            bom["singlePrice"] = "0"
            bom["singleCost"] = "0"
            bom["gross"] = str(gross)
            bom["grossRate"] = _trunc6(gross / price) if price else "0"
            rem["jdPrice"] = price
            preview.append({"venderSkuId": vsku, "skuName": rem.get("skuName"),
                            "售价": price, "价格来源": src, "采购价上限": costv, "毛利": gross,
                            "毛利率": round(gross / price, 4) if price else None})
    if missing:
        raise BlacklightError(f"以下 SKU 既无采购价上限、又未在 price_map 给售价，无法定价（venderSkuId）：{missing}")

    body = {"opt": 1, "remark": remark or "",
            "versionInfo": version, "projectInfo": project, "roleInfo": role,
            "systemAuditProjectInfo": {}}
    return body, preview


def _claim_token(project_id: str, hunter_erp: str, shotgun_erp: str,
                 price_map: dict, multiplier: float) -> str:
    return _confirm_token({"path": "selection/claim", "projectId": str(project_id),
                           "hunter": str(hunter_erp), "shotgun": str(shotgun_erp),
                           "mult": str(multiplier),
                           "prices": sorted(f"{k}:{v}" for k, v in (price_map or {}).items())})


def plan_claim(project_id: str, hunter_erp: str, shotgun_erp: str, price_map: dict = None,
               multiplier: float = PRICE_MULTIPLIER_DEFAULT, remark: str = "") -> dict:
    """[认领·只读] 拉标的详情 → 回填 猎人/猎枪/售价 → 算毛利 → 出提交预览 + confirm_token。
    price_map={venderSkuId: 售价}（可选）；未给的 SKU 自动按 采购价上限×multiplier→末尾.99 定价。
    先看 specWarnings(同规格异价)，核对 preview 后把相同参数传 claim_dryrun→claim。"""
    detail = project_detail(project_id, raw=True)
    if not detail.get("buttonInfo", {}).get("submit"):
        raise BlacklightError(f"标的 {project_id} 当前不可认领（buttonInfo.submit 非真；status={detail.get('status')}）")
    body, preview = _build_claim_body(detail["_raw"], hunter_erp, shotgun_erp, price_map, multiplier, remark)
    warns = detail.get("specWarnings") or []
    return {"projectId": project_id, "hunter": hunter_erp, "shotgun": shotgun_erp,
            "multiplier": multiplier, "sku_count": len(preview), "preview": preview,
            "specWarnings": warns,
            "confirm_token": _claim_token(project_id, hunter_erp, shotgun_erp, price_map, multiplier),
            "note": ("⚠️存在同规格异价（见 specWarnings），建议核对或驳回。" if warns else "")
                    + "核对 preview(售价/来源→毛利)后，相同参数传 osw_pool_claim_dryrun 再 claim。"}


def claim_dryrun(project_id: str, hunter_erp: str, shotgun_erp: str, price_map: dict = None,
                 multiplier: float = PRICE_MULTIPLIER_DEFAULT, remark: str = "") -> dict:
    """[认领·DRY-RUN] 组装 modifyProject(opt:1) 但**不发送**，回显 payload + confirm_token。
    price_map 可选；未给的 SKU 按 采购价上限×multiplier→.99 自动定价。"""
    detail = project_detail(project_id, raw=True)
    if not detail.get("buttonInfo", {}).get("submit"):
        raise BlacklightError(f"标的 {project_id} 当前不可认领（buttonInfo.submit 非真）")
    body, preview = _build_claim_body(detail["_raw"], hunter_erp, shotgun_erp, price_map, multiplier, remark)
    return {"would_submit": False, "projectId": project_id, "sku_count": len(preview), "preview": preview,
            "specWarnings": detail.get("specWarnings") or [],
            "note": "DRY-RUN：未提交。真执行：相同参数 + confirm=confirm_token 调 osw_pool_claim。",
            "payload_body": body,
            "confirm_token": _claim_token(project_id, hunter_erp, shotgun_erp, price_map, multiplier)}


@audited("selection", "claim")
def claim(project_id: str, hunter_erp: str, shotgun_erp: str, price_map: dict = None,
          multiplier: float = PRICE_MULTIPLIER_DEFAULT, confirm: str = "", remark: str = "") -> dict:
    """[认领·真写] 真提交认领（modifyProject opt:1，推进选品流程①→②）。
    需相同参数先 claim_dryrun 拿 confirm_token 再带 confirm。⚠️未活体验证：首次务必单标的+核对回执。"""
    token = _claim_token(project_id, hunter_erp, shotgun_erp, price_map, multiplier)
    if confirm != token:
        return {"executed": False, "reason": "认领需二次确认：相同参数先跑 osw_pool_claim_dryrun 拿 confirm_token 再带 confirm。"}
    detail = project_detail(project_id, raw=True)
    if not detail.get("buttonInfo", {}).get("submit"):
        return {"executed": False, "reason": f"标的 {project_id} 当前不可认领（buttonInfo.submit 非真）"}
    body, preview = _build_claim_body(detail["_raw"], hunter_erp, shotgun_erp, price_map, multiplier, remark)
    resp = _call("jxzy_selection_modifyProject", body, "POST")
    return {"executed": True, "confirm_token": token, "projectId": project_id,
            "sku_count": len(preview), "preview": preview, "response": resp}


# --------------------------------------------------------------------------- #
# 写：驳回（refuseVenderTask）
# --------------------------------------------------------------------------- #
def _reject_token(vproj_id: str, refuse_type: int, check_msg: str) -> str:
    return _confirm_token({"path": "selection/reject", "vprojId": str(vproj_id),
                           "refuseType": int(refuse_type), "checkMsg": check_msg or ""})


def _validate_reject(refuse_type: int, check_msg: str):
    if int(refuse_type) not in REFUSE_TYPES:
        raise BlacklightError(f"refuse_type 只能是 {list(REFUSE_TYPES)}（{REFUSE_TYPES}），收到 {refuse_type}")
    if not (check_msg or "").strip():
        raise BlacklightError("check_msg（驳回理由）不能为空")


def reject_dryrun(vproj_id: str, refuse_type: int, check_msg: str) -> dict:
    """[驳回·DRY-RUN] 组装 refuseVenderTask 但**不发送**，回显 payload + confirm_token。
    refuse_type: 20=采购价没竞争力 / 30=商品信息填写错误 / 40=大店已有同款。check_msg=理由文本。"""
    _validate_reject(refuse_type, check_msg)
    body = {"vprojId": str(vproj_id), "refuseType": int(refuse_type), "checkMsg": check_msg}
    return {"would_reject": False, "vprojId": str(vproj_id), "refuseType": int(refuse_type),
            "refuseTypeMsg": REFUSE_TYPES[int(refuse_type)], "checkMsg": check_msg,
            "note": "DRY-RUN：未驳回。真执行：相同参数 + confirm=confirm_token 调 osw_pool_reject。",
            "payload_body": body, "confirm_token": _reject_token(vproj_id, refuse_type, check_msg)}


@audited("selection", "reject")
def reject(vproj_id: str, refuse_type: int, check_msg: str, confirm: str = "") -> dict:
    """[驳回·真写] 真驳回标的（refuseVenderTask），退回供应商修改。
    需相同参数先 reject_dryrun 拿 confirm_token 再带 confirm。⚠️未活体验证：首次务必单标的+核对回执。"""
    try:
        _validate_reject(refuse_type, check_msg)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}
    token = _reject_token(vproj_id, refuse_type, check_msg)
    if confirm != token:
        return {"executed": False, "reason": "驳回需二次确认：相同参数先跑 osw_pool_reject_dryrun 拿 confirm_token 再带 confirm。"}
    body = {"vprojId": str(vproj_id), "refuseType": int(refuse_type), "checkMsg": check_msg}
    resp = _call("jxzy_selection_refuseVenderTask", body, "POST")
    return {"executed": True, "confirm_token": token, "vprojId": str(vproj_id),
            "refuseType": int(refuse_type), "response": resp}


# --------------------------------------------------------------------------- #
# 写：取消认领（controlProject opt:20）—— 撤销已认领标的，退回发起态(status→0)可重认
# 用户抓包 2026-07-24：POST controlProject {env:prod, projectId, opt:20, taskVersion}。
# 取消后 status→0、buttonInfo={submit,reject}，可再 claim 重认或 reject 驳回。★2026-07-24 实盘取消 3 品(回执 result:0,成功,taskVersion+1)再重认成功。
# --------------------------------------------------------------------------- #
CANCEL_OPT = 20


def _cancel_token(project_id: str) -> str:
    return _confirm_token({"path": "selection/cancel", "projectId": str(project_id)})


def cancel_dryrun(project_id: str) -> dict:
    """[取消认领·DRY-RUN] 读当前 taskVersion/status，组装 controlProject(opt:20) 但**不发送**，回显 payload + confirm_token。
    取消后标的退回发起态(status→0)，可重新认领(claim)或驳回(reject)。"""
    raw = project_detail(project_id, raw=True)["_raw"]
    tv = (raw.get("versionInfo") or {}).get("taskVersion")
    status = raw.get("status")
    body = {"env": "prod", "projectId": str(project_id), "opt": CANCEL_OPT, "taskVersion": tv}
    return {"would_cancel": False, "projectId": str(project_id), "status": status,
            "taskVersion": tv, "itemName": raw.get("itemName"),
            "note": "DRY-RUN：未取消。真执行：相同 project_id + confirm=confirm_token 调 osw_pool_cancel。取消后 status→0 可重认。",
            "payload_body": body, "confirm_token": _cancel_token(project_id)}


@audited("selection", "cancel")
def cancel(project_id: str, confirm: str = "") -> dict:
    """[取消认领·真写] 撤销已认领标的（controlProject opt:20），退回发起态(status→0)，可重新认领。
    需相同 project_id 先 cancel_dryrun 拿 confirm_token 再带 confirm。taskVersion 自动取当前值。"""
    token = _cancel_token(project_id)
    if confirm != token:
        return {"executed": False, "reason": "取消需二次确认：先跑 osw_pool_cancel_dryrun 拿 confirm_token 再带 confirm。"}
    tv = (project_detail(project_id, raw=True)["_raw"].get("versionInfo") or {}).get("taskVersion")
    body = {"env": "prod", "projectId": str(project_id), "opt": CANCEL_OPT, "taskVersion": tv}
    resp = _call("jxzy_selection_controlProject", body, "POST")
    return {"executed": True, "confirm_token": token, "projectId": str(project_id),
            "taskVersion_used": tv, "response": resp}


# --------------------------------------------------------------------------- #
# 写：铺货（copyWare）—— 认领成功·完成采纳后上品。isCheck 平台预检(=自带 dry-run)
# 链路：公共商品池认领 → 选品流程 → 待铺货采纳(adopt) → 铺货(copyWare)。
# 前置：商品需"全部完成采纳"，否则 isCheck 预检返回业务错误。
# --------------------------------------------------------------------------- #
def _stock_token(project_id: str, spu_idx) -> str:
    return _confirm_token({"path": "selection/stock", "projectId": str(project_id), "spuIdx": str(spu_idx)})


def stock_dryrun(project_id: str, spu_idx: str = "1") -> dict:
    """[铺货·DRY-RUN] 平台预检（copyWare isCheck=True，**不真铺货**）：校验该标的能否铺货（需先完成采纳等）。
    返回 {ready, precheck, confirm_token}。ready=True(预检通过)才出 token 供真铺货；ready=False 时 precheck 给平台拦截原因。"""
    body = {"projectId": str(project_id), "spuIdx": str(spu_idx), "isCheck": True}
    try:
        data = _call("jxzy_selection_copyWare", body, "GET")
        ready, msg = True, "平台预检通过，可铺货"
    except BlacklightError as e:
        ready, msg, data = False, str(e), None
    return {"projectId": str(project_id), "spuIdx": str(spu_idx), "ready": ready,
            "precheck": msg, "response": data,
            "confirm_token": _stock_token(project_id, spu_idx) if ready else None,
            "note": ("预检通过：相同参数 + confirm=confirm_token 调 osw_pool_stock 真铺货。" if ready
                     else "预检未通过——按 precheck 处理(如先完成采纳)后再试；不出 token。")}


@audited("selection", "stock")
def stock(project_id: str, spu_idx: str = "1", confirm: str = "") -> dict:
    """[铺货·真写] 真铺货上品（copyWare isCheck=False）。需相同参数先 stock_dryrun 拿 confirm_token 再带 confirm。
    ⚠️未活体验证：首次务必单标的 + 核对回执 + 到商品列表确认已上品。"""
    token = _stock_token(project_id, spu_idx)
    if confirm != token:
        return {"executed": False, "reason": "铺货需二次确认：相同参数先跑 osw_pool_stock_dryrun 拿 confirm_token(预检通过才有) 再带 confirm。"}
    body = {"projectId": str(project_id), "spuIdx": str(spu_idx), "isCheck": False}
    resp = _call("jxzy_selection_copyWare", body, "GET")
    return {"executed": True, "confirm_token": token, "projectId": str(project_id),
            "spuIdx": str(spu_idx), "response": resp}
