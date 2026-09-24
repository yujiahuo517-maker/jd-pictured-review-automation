"""
yx-mcp 场域：product（**商品列表 / 商品信息**，网关 sff.jd.com，dsm.product.manage）。

补齐此前的缺口：商品信息此前只能间接从 markettool（毛利监控，按 SKU 查）取到，没有「全量商品目录」入口。
本场域直连商家后台商品列表接口（页面 wares-jdm.jd.com/ware/wareList 背后的 gateway 调用），
提供：**分页列全部商品 / 按 SKU 批量取 / 按名称搜 / 拉全量**，返回**整洁行**（京东价/京喜采购价/库存/销量/类目/供应商/上下架态…）。

★鉴权实证（2026-07-17）：该接口的 `h5st` 签名 + 设备指纹(`b-ext-device-info`/`dsm-eid`)**并非强制**——
  只凭 yx 登录态 cookie（同 realm）即 code=200 正常返回；无 cookie 则 code=1001「未登录」。
  故本模块**不复刻 h5st**（那套按时间戳+参数签名、易随页面改版失效），只带 cookie + 业务头。
★关键头 `belong-biz-id`（=店铺 bizId）**是权威圈定范围**：带 14691198 → totalCount 6769；不带 → 1563（范围不同）。
  故每请求必带；bizId 默认取 config.json product.biz_id，可 env `YX_PRODUCT_BIZ_ID` 或入参覆盖（多店铺）。
★分页上限：pageSize ≤ 100（≥200 → code=201）。拉全量走 product_all（内部按 100 翻页，带 max_rows 上限 + 截断告警）。
"""
from __future__ import annotations

import os
import re as _re
import time as _time
from typing import Optional

from blacklight.core import auth as jd_auth
from blacklight.core import BlacklightError, DEFAULT_UA, gateway, scene_cfg, confirm_token as _confirm_token, canon_num, audited, pmap

# 允许的排序字段（服务端 sortMap 的 key），防注入乱字段
_SORT_FIELDS = {"onlineTime", "offlineTime", "modified", "created", "jdPrice", "salesVolume", "stockNum"}
_MAX_PAGE_SIZE = 100


def _cfg() -> dict:
    return scene_cfg("product")


def _biz_id(override: Optional[str] = None) -> str:
    return (str(override).strip() if override else "") \
        or os.environ.get("YX_PRODUCT_BIZ_ID", "").strip() \
        or str(_cfg().get("biz_id", "")).strip() \
        or "14691198"


def _api_params() -> dict:
    c = _cfg()
    return {"v": "1.0",
            "appId": c.get("app_id", "3MC69M4R3HFKCQ4S01DN"),
            "api": c.get("api", "dsm.product.manage.ProductInfoReadViewService.queryValidProductList")}


def _client(timeout: float = 25.0):
    from blacklight.core import base as jd_core # 复用 httpx 加载 + verify=False
    ck = jd_auth.session_cookie()
    headers = {
        "Cookie": ck, "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://wares-jdm.jd.com",
        "Referer": "https://wares-jdm.jd.com/",
        "x-referer-page": "https://wares-jdm.jd.com/ware/wareList",
        "x-requested-with": "XMLHttpRequest",
        "dsm-platform": "erp", "dsm-lang": "zh_CN", "dsm-language": "zh_CN",
    }
    return jd_core._httpx().Client(headers=headers, timeout=timeout, verify=False)


def _ms_to_date(ms) -> Optional[str]:
    if not ms:
        return None
    try:
        return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(int(ms) / 1000))
    except Exception:
        return None


def _num(v):
    """价格字段可能是 str/float，统一成 float（取不到返回 None）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _btn(raw: dict, code: str) -> dict:
    """取 operateButtonVOMap 里某个操作按钮。show=1 才可用，reason 说明不可用原因。"""
    return (raw.get("operateButtonVOMap") or {}).get(code) or {}


def _btn_url(raw: dict, code: str) -> Optional[str]:
    """按钮的跳转 URL（show!=1 视为不可用，返回 None）。"""
    b = _btn(raw, code)
    return b.get("url") if b.get("show") == 1 else None


_VPROJ_RE = _re.compile(r"(VPROJ\w+)")


def _project_id(raw: dict) -> Optional[str]:
    """从「标的管理」按钮 URL 里抠出 projectId（VPROJ…）。
    ★供应商管理接口(jxzy_adopt_queryItemList)必需 projectId，而它只能从这里拿到。"""
    u = _btn_url(raw, "productSelectTaskButtonInfo") or ""
    m = _VPROJ_RE.search(u)
    return m.group(1) if m else None


def _clean(raw: dict) -> dict:
    """把服务端一条商品记录压成整洁行（只留业务常用字段）。"""
    sku = raw.get("productSkuInfoVO") or {}
    price = raw.get("priceDetailVO") or {}
    cat = raw.get("categoryDetailVO") or {}
    st = raw.get("productStatusVO") or {}
    brand = raw.get("brandVO") or {}
    cmt = raw.get("commentSummaryVO") or {}
    feat = raw.get("productFeatureMap") or {}
    suppliers = raw.get("supplierNameList") or []
    shop_path = raw.get("shopCategoryPath") or []
    return {
        "productId": raw.get("productId"),                       # SPU
        "skuId": sku.get("skuId"),                               # 主 SKU
        "skuCount": sku.get("skuCount"),
        "name": raw.get("productName"),
        "itemNum": (raw.get("itemNum") or "").strip() or None,   # 商家货号
        "state": st.get("productState"), "stateDesc": st.get("statusDesc"),
        "jdPrice": _num(price.get("jdPrice")),                   # 京东价（划线/前台）
        "minJdPrice": _num(price.get("minJdPrice")),
        "maxJdPrice": _num(price.get("maxJdPrice")),
        "jxCgPriceMin": _num(price.get("minJxCgPrice")),         # 京喜采购价（成本参考）
        "jxCgPriceMax": _num(price.get("maxJxCgPrice")),
        "costPrice": _num(price.get("costPrice")),
        "stock": raw.get("stockNum"), "sales": raw.get("salesVolume"),
        "categoryId": cat.get("lastCategoryId"), "categoryName": cat.get("lastCategoryName"),
        "shopCategoryPath": shop_path[0] if shop_path else None,
        "brand": brand.get("brandName"),
        "supplier": suppliers[0] if suppliers else None,
        "commentCount": cmt.get("commentCount"), "goodRate": cmt.get("goodRate"),
        "isGX": feat.get("isGXproduct"),                         # 1=京喜工厂店品
        "created": _ms_to_date(raw.get("created")),              # 首次创建（判「新品」看这个）
        "onlineTime": _ms_to_date(raw.get("onlineTime")),        # 最近一次上架（会被重新上架刷新）
        "offlineTime": _ms_to_date(raw.get("offlineTime")),      # 最近一次下架（已下架/下架列表看这个）
        "modified": _ms_to_date(raw.get("modified")),
        "skuUrl": sku.get("skuUrl"),
        # ---- 行内操作入口（来自 operateButtonVOMap，2026-08-03 抓包）----
        "inquiryLink": _btn_url(raw, "productInviteLinkButtonInfo"),  # ★竞价链接：发给商家参与报价
        "projectId": _project_id(raw),                                # 标的ID（供应商管理接口必需）
        "supplierPageUrl": _btn_url(raw, "productSupplierButtonInfo"),
    }


def _to_ms(v) -> Optional[int]:
    """把 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' / 毫秒数 转成毫秒时间戳（本地时区）。空→None。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(_time.mktime(_time.strptime(s, fmt)) * 1000)
        except ValueError:
            continue
    raise BlacklightError(f"时间格式无法解析：{v}（用 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'）")


def _build_req(*, biz_id: str, page: int, page_size: int, product_state: Optional[str],
               name: Optional[str], sku_ids, product_ids, category_ids,
               min_price, max_price, min_stock, max_stock,
               created_from, created_to, online_from, online_to,
               offline_from, offline_to, erp,
               sort_field: str, sort_order: str) -> dict:
    q = {
        "currentBizId": biz_id, "productName": name or None,
        "skuIdList": [str(s) for s in sku_ids] if sku_ids else None,
        "productIdList": [str(p) for p in product_ids] if product_ids else None,
        "categoryIds": [int(c) for c in category_ids] if category_ids else [],
        "brandIdList": [],
        "filterErpCode": (str(erp).strip() or None) if erp else None,   # 采销 ERP 归属筛(服务端)
        "minJdPrice": min_price, "maxJdPrice": max_price,
        "minStockNum": min_stock, "maxStockNum": max_stock,
        "startCreated": _to_ms(created_from), "endCreated": _to_ms(created_to),
        "startOnlineTime": _to_ms(online_from), "endOnlineTime": _to_ms(online_to),
        "startOfflineTime": _to_ms(offline_from), "endOfflineTime": _to_ms(offline_to),
        "sortMap": {sort_field: sort_order},
        "pageNum": page, "pageSize": page_size,
        "productState": product_state,
    }
    return {"productListQueryReq": q,
            "accessContext": {"source": "web", "businessModel": "2",
                              "proxyBelongBizId": biz_id, "originType": None}}


def _query(req: dict, biz_id: str) -> dict:
    """低层：POST 一次 → 返回 data 块（含 data[]/totalCount/pageNo/pageSize）。"""
    import json as _json
    base = gateway("product")
    with _client() as c:
        r = c.post(f"{base}/api", params=_api_params(),
                   content=_json.dumps(req, ensure_ascii=False),
                   headers={"belong-biz-id": biz_id, "belong-type": "200"})
    if r.status_code in (301, 302, 303, 307, 308):
        raise BlacklightError("商品列表接口被重定向——登录态可能失效，请 yx_login 重登")
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"商品列表未返回 JSON（HTTP {r.status_code}）——登录态可能失效，请 yx_login") from e
    code = j.get("code")
    if code == 1001:
        raise BlacklightError("商品列表：未登录（code=1001），请 yx_login 重登")
    if code not in (200, "200"):
        raise BlacklightError(f"商品列表 code={code}: {j.get('msg') or j.get('message')}")
    return j.get("data") or {}


def _sff_post(api_name: str, body: dict, biz_id: str):
    """通用 sff.jd.com/api POST（dsm.product.manage.* 各接口共用）→ 返回 data。cookie-only、h5st 非强制。"""
    import json as _json
    base = gateway("product")
    with _client() as c:
        r = c.post(f"{base}/api",
                   params={"v": "1.0", "appId": _cfg().get("app_id", "3MC69M4R3HFKCQ4S01DN"), "api": api_name},
                   content=_json.dumps(body, ensure_ascii=False),
                   headers={"belong-biz-id": biz_id, "belong-type": "200"})
    if r.status_code in (301, 302, 303, 307, 308):
        raise BlacklightError(f"{api_name} 被重定向——登录态可能失效，请 yx_login 重登")
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"{api_name} 未返回 JSON（HTTP {r.status_code}）——登录态可能失效，请 yx_login") from e
    code = j.get("code")
    if code == 1001:
        raise BlacklightError(f"{api_name}：未登录（code=1001），请 yx_login 重登")
    if code not in (200, "200"):
        raise BlacklightError(f"{api_name} code={code}: {j.get('msg') or j.get('message')}")
    return j.get("data")


# --------------------------------------------------------------------------- #
# 对外能力
# --------------------------------------------------------------------------- #
def product_list(page: int = 1, page_size: int = 50, product_state: Optional[str] = "4",
                 name: Optional[str] = None, sku_ids: Optional[list] = None,
                 product_ids: Optional[list] = None, category_ids: Optional[list] = None,
                 min_price: Optional[float] = None, max_price: Optional[float] = None,
                 min_stock: Optional[int] = None, max_stock: Optional[int] = None,
                 created_from=None, created_to=None, online_from=None, online_to=None,
                 offline_from=None, offline_to=None,
                 erp: Optional[str] = None,
                 sort: str = "onlineTime desc", biz_id: Optional[str] = None) -> dict:
    """分页列商品。product_state: **'4'=在售(默认)、'10'=已下架/下架、None/''=全部状态**（实证 4/10；下架列表用 '10'）。
    created_from/created_to=按**首次创建**筛(判「新品」用这个)；online_from/online_to=按**上架时间**筛(含老品翻新)；
    offline_from/offline_to=按**下架时间**筛(配 product_state='10' 拉近期下架款)；时间收 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' / 毫秒。
    erp=**采销 ERP 归属筛**(filterErpCode,服务端;传 ERP 只返其名下商品。注意:行内不吐归属,只能反向按此筛)。
    sort='字段 asc|desc'（字段∈onlineTime/**offlineTime**/created/modified/jdPrice/salesVolume/stockNum；下架列表宜用 'offlineTime desc'）。"""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), _MAX_PAGE_SIZE))
    parts = (sort or "onlineTime desc").split()
    sf = parts[0] if parts and parts[0] in _SORT_FIELDS else "onlineTime"
    so = "asc" if (len(parts) > 1 and parts[1].lower() == "asc") else "desc"
    bid = _biz_id(biz_id)
    ps = (product_state or None)  # '' → None（全部）
    req = _build_req(biz_id=bid, page=page, page_size=page_size, product_state=ps,
                     name=name, sku_ids=sku_ids, product_ids=product_ids, category_ids=category_ids,
                     min_price=min_price, max_price=max_price, min_stock=min_stock, max_stock=max_stock,
                     created_from=created_from, created_to=created_to,
                     online_from=online_from, online_to=online_to,
                     offline_from=offline_from, offline_to=offline_to, erp=erp,
                     sort_field=sf, sort_order=so)
    data = _query(req, bid)
    rows = [_clean(x) for x in (data.get("data") or [])]
    total = data.get("totalCount")
    return {"total": total, "page": page, "page_size": page_size,
            "pages": (int(total) + page_size - 1) // page_size if isinstance(total, int) else None,
            "biz_id": bid, "count": len(rows), "rows": rows}


def product_get(sku_ids: list, biz_id: Optional[str] = None) -> dict:
    """按 SKU 批量取商品（skuIdList 过滤，单次建议 ≤100）。返回 {found, missing, rows}。"""
    sku_ids = [str(s) for s in (sku_ids or [])]
    if not sku_ids:
        raise BlacklightError("sku_ids 不能为空")
    res = product_list(page=1, page_size=_MAX_PAGE_SIZE, product_state=None,
                       sku_ids=sku_ids, biz_id=biz_id)
    got = {str(r.get("skuId")) for r in res["rows"]}
    missing = [s for s in sku_ids if s not in got]
    return {"requested": len(sku_ids), "found": len(res["rows"]),
            "missing": missing, "biz_id": res["biz_id"], "rows": res["rows"]}


def product_search(name: str, page: int = 1, page_size: int = 50,
                   product_state: Optional[str] = "4", biz_id: Optional[str] = None) -> dict:
    """按商品名称模糊搜索。"""
    if not (name or "").strip():
        raise BlacklightError("name 不能为空")
    return product_list(page=page, page_size=page_size, product_state=product_state,
                        name=name.strip(), biz_id=biz_id)


def product_all(product_state: Optional[str] = "4", name: Optional[str] = None,
                category_ids: Optional[list] = None, max_rows: int = 2000,
                created_from=None, created_to=None, online_from=None, online_to=None,
                offline_from=None, offline_to=None,
                erp: Optional[str] = None,
                sort: str = "onlineTime desc", biz_id: Optional[str] = None) -> dict:
    """拉全量（内部按 100 翻页直到取完或达 max_rows 上限）。达上限会在 truncated 标出，别当「全部」用。
    product_state：'4'=在售(默认)/'10'=已下架/None=全部。支持 created/online/**offline** 时间区间、erp（采销归属筛）。
    拉下架款：product_state='10'（可配 offline_from/to + sort='offlineTime desc'）。"""
    max_rows = max(1, int(max_rows))
    rows: list = []
    total = None
    bid = _biz_id(biz_id)
    page = 1
    while len(rows) < max_rows:
        res = product_list(page=page, page_size=_MAX_PAGE_SIZE, product_state=product_state,
                           name=name, category_ids=category_ids,
                           created_from=created_from, created_to=created_to,
                           online_from=online_from, online_to=online_to,
                           offline_from=offline_from, offline_to=offline_to, erp=erp,
                           sort=sort, biz_id=bid)
        total = res["total"]
        batch = res["rows"]
        rows.extend(batch)
        if len(batch) < _MAX_PAGE_SIZE:      # 最后一页
            break
        page += 1
    truncated = isinstance(total, int) and len(rows[:max_rows]) < total
    return {"total": total, "fetched": min(len(rows), max_rows), "truncated": truncated,
            "biz_id": bid, "rows": rows[:max_rows]}


def _sku_variant(sku_name: Optional[str], base_name: Optional[str]) -> Optional[str]:
    """从 skuName 里剥掉 SPU 基础标题，得到该 SKU 的销售属性变体（如「卡通款粉色-中号」）。取不到返回整名。"""
    if not sku_name:
        return None
    s = sku_name.strip()
    if base_name and s.startswith(base_name.strip()):
        v = s[len(base_name.strip()):].strip(" -　")
        return v or s
    # 常见格式："<基础标题> <变体>"，退而取最后一段空格后内容
    return s


def product_sku_detail(product_id, biz_id: Optional[str] = None, with_cost: bool = True) -> dict:
    """**SPU→SKU 明细**（补商品列表 SPU 粒度的缺口）。给一个 productId，列其全部 SKU：
    skuId/货号/销售属性变体/京东价/采购价(供货价) + （with_cost）全成本 actualTotalCost + 裸毛利/毛利率。
    数据源：querySkuPrice（SKU 列表+京东价+采购价，仅需 productId）+ getPriceApprovalStatus（每 SKU 全成本）。"""
    bid = _biz_id(biz_id)
    ac = {"source": "web", "businessModel": "2", "proxyBelongBizId": bid, "originType": None}
    price = _sff_post("dsm.product.manage.PriceReadViewService.querySkuPrice",
                      {"req": {"scene": "priceStar",
                               "skuPriceQueries": [{"productId": int(product_id), "skuIds": []}]},
                       "accessContext": ac}, bid) or []
    cost_map = {}
    if with_cost:
        try:
            appr = _sff_post("dsm.product.manage.PriceReadViewService.getPriceApprovalStatus",
                             {"req": {"productIds": [int(product_id)]}, "accessContext": ac}, bid) or []
            for p in appr:
                for s in (p.get("skuPriceApprovalStatusList") or []):
                    cost_map[str(s.get("skuId"))] = _num(s.get("actualTotalCost"))
        except BlacklightError:
            pass   # 全成本取不到不致命，京东价/采购价照给
    # 基础标题 = 所有 skuName 的最长公共前缀（各 skuName = 基础标题 + 变体）
    names = [s.get("skuName") or "" for s in price]
    import os as _os
    base_name = _os.path.commonprefix(names).strip() if len(names) > 1 else None
    rows = []
    for s in price:
        sid = str(s.get("skuId"))
        pv = s.get("priceVO") or {}
        ft = s.get("features") or {}
        jd = _num(s.get("jdPrice"))
        cg = _num(pv.get("purchasePrice")) if pv.get("purchasePrice") is not None else _num(ft.get("cgPrice"))
        cost = cost_map.get(sid)
        gp = round(jd - cost, 2) if (jd is not None and cost is not None) else None
        rows.append({
            "skuId": sid,
            "itemNum": (s.get("outerId") or "").strip() or None,       # 货号
            "skuName": s.get("skuName"),
            "variant": _sku_variant(s.get("skuName"), base_name),      # 销售属性变体
            "jdPrice": jd,                                             # SKU 京东价
            "purchasePrice": cg,                                       # 采购价(供货价)
            "actualTotalCost": cost,                                   # 全成本(采购+物流等,landed)
            "grossProfit": gp,                                         # 裸毛利=京东价−全成本(无券促口径)
            "grossMargin": round(gp / jd, 4) if (gp is not None and jd) else None,
            "productType": s.get("productType"),                       # 改价 payload 需要
            "pStatus": s.get("pStatus"),
        })
    return {"productId": str(product_id), "count": len(rows), "biz_id": bid, "rows": rows}


# --------------------------------------------------------------------------- #
# 改价（写操作：PriceWriteViewService.updatePrices）—— dry-run + confirm_token 门 + 审计
# --------------------------------------------------------------------------- #
_REPRICE_CAP = 50   # 单次改价 SKU 数上限


def _resolve_product_id(sku_id: str, biz_id: str) -> Optional[str]:
    """skuId → 所属 productId（product 列表按 skuIdList 过滤，行内 productId 即 SPU；非主sku也能命中 SPU）。"""
    g = product_list(page=1, page_size=1, product_state=None, sku_ids=[str(sku_id)], biz_id=biz_id)
    return str(g["rows"][0]["productId"]) if g["rows"] else None


def _reprice_transmit(sku: str) -> dict:
    """★**涨价能传导到「到手价」多少** —— 涨价前必查，否则会照着裸毛利定出无效的目标价。

    2026-08-14 实测 5 款（涨价后回读实际到手价）：

        10218472570037  +4.47 → 到手 +4.47   **100%**  纯定额券
        10222490881204  +4.22 → 到手 +3.59    85%      国补 15%
        10230451021332  +3.07 → 到手 +2.34    76%      国补 + 直降
        10230451021333  +9.14 → 到手 +6.57    72%      国补×2 + 直降 + 跨店满减
        10130829981648  +1.56 → 到手 **+0.00**  **0%**  ★便宜包邮

    ★★**便宜包邮是「目标价型」促销：京东价涨多少，它的 reward 自动跟涨多少，前台价纹丝不动。**
      实证该款 origBench 5.00→6.56，便宜包邮 reward 0.51→**2.07**，bench 恒为 4.49，
      毛利 −1.41 分文未变。**对这类款涨价是纯无效操作，只能先退便宜包邮。**

    ★同日**盲测验证**（先出估计、再改价回读）：`10230451021333` 第二次涨价
      估计 0.77 / 实测 **0.74**（涨 3.61 → 到手价 +2.67，毛利 −0.96→+1.71）；
      `10130829981648` 估计 0 / 实测 0（分文未动）。⇒ 模型可用于定目标价。

    ★**便宜包邮解锁后，先前「无效」的涨价会一次性释放**：该款退包邮前 bench 恒 4.49，
      退掉后 bench 直接回到已涨的 6.56、到手价 1.49→3.56、毛利 −1.41→**+0.66**。
      （单退包邮不涨价只到 −0.91，仍亏 ⇒ **涨价 + 退包邮要成对做**，顺序无所谓。）

    ⚠️本函数给的是**结构性上界估计**，不是精确预测（比例型券促无法从配置反推斜率）。
      真值一律以**改完回读 query_pricing** 为准 —— 这跟 [[margin-attribution-pitfalls]]
      「收益是上限、必须回读实测」是同一条纪律的涨价方向镜像。
    """
    from blacklight.osw import margin as _om
    try:
        p = _om.query_pricing(sku)
    except Exception as e:
        return {"传导率估计": None, "警告": f"取价失败，无法判断涨价是否有效：{str(e)[:60]}"}
    def _amt(x):
        try:
            return float(x.get("reward") or 0)
        except Exception:
            return 0.0
    # ⚠️**必须过滤 reward=0 的空占位行**：promotionList 里普遍存在
    #   `{promoId:null, type:1, name:null, reward:0.0}` 这种占位记录（实测 10218472570037 /
    #   10230451021333 都有）。只按 `type==1` 判会把它们当成便宜包邮，
    #   于是给传导 100% 的款报「涨价完全无效」——正好把结论判反。
    promos = [x for x in (p.get("promotions") or []) if x and _amt(x) > 0]
    baoyou = [x for x in promos if x.get("type") == 1 and not x.get("isNewUser")]
    warn, rate = [], 1.0
    if baoyou:
        rate = 0.0
        warn.append("★便宜包邮/单品促销锁定前台价（%s，当前 reward %.2f）⇒ **涨价完全无效**，"
                    "先退促销再谈涨价"
                    % ((baoyou[0].get("name") or "?")[:24], _amt(baoyou[0])))
    else:
        if any(x.get("type") == 5 for x in promos):
            rate *= 0.85
            warn.append("含国补：按新价 15% 回补 ⇒ 涨价约 85% 传导")
        if any(str(x.get("cat") or "") == "总价促销" or x.get("type") == 26 for x in promos):
            rate *= 0.9
            warn.append("含总价促销/直降：比例型，会随涨价多吃 ⇒ 传导再打折")
    return {"当前到手价": p.get("jxActualPrice"), "当前我担减免": p.get("jxCouponSum"),
            "当前毛利": p.get("jxGrossProfit"), "传导率估计": round(rate, 2),
            "警告": "；".join(warn) or None}


def plan_reprice(price_map: dict, biz_id: Optional[str] = None,
                 check_transmit: bool = True) -> dict:
    """[只读] 把 {skuId: 目标京东价} 解析成可写行 + 预览。
    解析每个 sku 的 productId / actualTotalCost / productType / 现价，算新裸毛利，供核对后再 dry-run→改价。

    ★★**「新毛利率」是裸毛利率（新价 − 全成本），不是到手价毛利率** —— 券促减免不在里面。
      2026-08-14 踩坑：本函数给 10130829981648 报「新毛利率 55.79%」，改完实测到手价毛利
      **−1.41 分文未变**（便宜包邮锁价）。⇒ `check_transmit=True`（默认）会逐款补上
      当前到手价/减免结构/传导率估计与警告，**别只看新毛利率就下单**。
    """
    bid = _biz_id(biz_id)
    items = [(str(s), float(p)) for s, p in (price_map or {}).items()]
    if not items:
        raise BlacklightError("price_map 不能为空")

    def _resolve(one):
        sku, newp = one
        try:
            pid = _resolve_product_id(sku, bid)
            if not pid:
                return {"skuId": sku, "error": "找不到所属 productId(非在售/无权限?)"}
            det = product_sku_detail(pid, biz_id=bid, with_cost=True)
            hit = next((x for x in det["rows"] if x["skuId"] == sku), None)
            if not hit:
                return {"skuId": sku, "error": f"sku 不在 SPU {pid} 的 SKU 列表"}
            cost = hit["actualTotalCost"]
            row = {"productId": pid, "skuId": sku, "jdPrice": round(newp, 2),
                   "actualTotalCost": cost, "productType": hit.get("productType") or 1,
                   "现价": hit["jdPrice"], "新价": round(newp, 2),
                   "全成本": cost, "新裸毛利": round(newp - cost, 2) if cost is not None else None,
                   "新毛利率(裸·不含券促)": round((newp - cost) / newp, 4) if (cost is not None and newp) else None,
                   "货号": hit.get("itemNum"), "变体": hit.get("variant")}
            if check_transmit:
                t = _reprice_transmit(sku)
                row.update({k: v for k, v in t.items() if v is not None})
                up = round(newp - (hit["jdPrice"] or 0), 2)
                r = t.get("传导率估计")
                if r is not None and t.get("当前到手价") is not None:
                    row["预估新到手价"] = round(t["当前到手价"] + up * r, 2)
                    if cost is not None:
                        row["预估新到手价毛利"] = round(row["预估新到手价"] - cost, 2)
            return row
        except Exception as e:
            return {"skuId": sku, "error": str(e)[:80]}

    resolved = pmap(_resolve, items, workers=8)
    ok = [r for r in resolved if "error" not in r]
    bad = [r for r in resolved if "error" in r]
    return {"resolved": len(ok), "failed": len(bad), "biz_id": bid,
            "rows": ok, "errors": bad,
            "note": "核对 rows(现价→新价/新毛利)后，把 rows 传 update_prices_dryrun 拿 confirm_token 再改价。"}


def _reprice_token(rows: list) -> str:
    return _confirm_token({"path": "product/updatePrices",
                           "rows": sorted(f"{int(r['productId'])}:{int(r['skuId'])}:{canon_num(r.get('jdPrice'))}"
                                          for r in rows)})


def _reprice_payload(rows: list, biz_id: str) -> dict:
    ups = [{"productId": int(r["productId"]), "skuId": int(r["skuId"]),
            "jdPrice": str(r["jdPrice"]), "productType": int(r.get("productType") or 1),
            "actualTotalCost": (str(r["actualTotalCost"]) if r.get("actualTotalCost") is not None else None)}
           for r in rows]
    return {"updatePriceQuery": {"updatePrices": ups},
            "accessContext": {"source": "web", "businessModel": "2",
                              "proxyBelongBizId": biz_id, "originType": None}}


def update_prices_dryrun(rows: list, biz_id: Optional[str] = None) -> dict:
    """[dry-run] 改价组装但**不发送**：校验行(productId/skuId/jdPrice 必填、jdPrice>0)、回显 payload + confirm_token。"""
    rows = rows or []
    if not rows:
        raise BlacklightError("没有要改价的行")
    if len(rows) > _REPRICE_CAP:
        raise BlacklightError(f"单次改价 ≤{_REPRICE_CAP} 个 SKU（本次 {len(rows)}）")
    for r in rows:
        if not (r.get("productId") and r.get("skuId") and r.get("jdPrice")):
            raise BlacklightError(f"行缺 productId/skuId/jdPrice：{r}")
        if float(r["jdPrice"]) <= 0:
            raise BlacklightError(f"jdPrice 必须 >0：{r}")
    bid = _biz_id(biz_id)
    return {"would_update": False, "count": len(rows),
            "note": "DRY-RUN：未改价。真执行：相同 rows + confirm=confirm_token 调 update_prices。",
            "payload": _reprice_payload(rows, bid), "rows": rows,
            "protected_blocked": _protected_reprice(rows),   # 规划阶段就亮出来，真执行还会再查
            "confirm_token": _reprice_token(rows)}


def _protected_reprice(rows: list) -> list:
    """改价前过禁碰清单（action=reprice）。返回命中项（空=可以改）。

    ★`protected.py` 的 ACTIONS 里本来就有 `reprice` 这个动作，文档也写着"任何摘券/退促/**改价**
      方案生成时先过 filter_plan()"，但 2026-08-24 审查发现 **osw/product.py 全文零引用 protected** ——
      即禁碰清单对改价这条真写路径从来没生效过。闸补在真执行原语上。"""
    from blacklight.core import protected as _prot
    hits = []
    for r in (rows or []):
        sku = str(r.get("skuId") or "")
        for x in _prot.check(sku, action="reprice"):
            hits.append({"skuId": sku, "jdPrice": r.get("jdPrice"),
                         "rule": x.get("id"), "reason": x.get("reason")})
    return hits


@audited("product", "update_price")
def update_prices(rows: list, confirm: str = "", biz_id: Optional[str] = None) -> dict:
    """[写] **真改京东价**（PriceWriteViewService.updatePrices，不可轻易撤回）。
    需相同 rows 先 update_prices_dryrun 拿 confirm_token 再带 confirm。单次 ≤50。
    ★还会过**禁碰清单**（`core/protected`，action=reprice）：命中直接拒绝。"""
    rows = rows or []
    if not rows:
        return {"executed": False, "reason": "没有要改价的行"}
    if len(rows) > _REPRICE_CAP:
        return {"executed": False, "reason": f"单次改价 ≤{_REPRICE_CAP}（本次 {len(rows)}）"}
    token = _reprice_token(rows)
    if confirm != token:
        return {"executed": False, "reason": "改价需二次确认：相同 rows 先跑 update_prices_dryrun 拿 confirm_token 再带 confirm。"}
    blocked = _protected_reprice(rows)
    if blocked:
        return {"executed": False, "reason": "命中禁碰清单，已拒绝改价", "protected_blocked": blocked,
                "how": "这些 SKU 是人工拍板保留的；确实要改请先 osw_protected_remove 去掉规则（留痕）。"}
    bid = _biz_id(biz_id)
    data = _sff_post("dsm.product.manage.PriceWriteViewService.updatePrices", _reprice_payload(rows, bid), bid)
    return {"executed": True, "confirm_token": token, "count": len(rows), "response": data,
            "rows": [{"skuId": r["skuId"], "jdPrice": r["jdPrice"]} for r in rows]}


# --------------------------------------------------------------------------- #
# 改商品名/长标题（写：ProductInfoWriteViewService.updateProducts）—— dry-run + confirm_token 门 + 审计
# ⚠️改的是 **productName（商品名/长标题，≤60字，getWareTitleRule.titleMaxLength）**，SPU 级；
#   不是秒杀「短标题(≤26)」——那是另一字段/接口。
# --------------------------------------------------------------------------- #
_TITLE_CAP = 50          # 单次改标题 SPU 数上限
_TITLE_MAX_LEN = 60      # 长标题(去品牌)字数上限，实证 getWareTitleRule.titleMaxLength=60


def _title_token(updates: list) -> str:
    return _confirm_token({"path": "product/updateProducts",
                           "rows": sorted(f"{u.get('productId')}:{u.get('productName')}" for u in updates)})


def _title_payload(updates: list) -> dict:
    return {"accessContext": {"businessModel": 0, "source": "web", "originType": 1},
            "updateProductVO": {"updateProducts": [
                {"productId": int(u["productId"]), "productName": str(u["productName"]).strip()} for u in updates]}}


def _title_validate(updates: list):
    if not updates:
        raise BlacklightError("没有要改标题的行")
    if len(updates) > _TITLE_CAP:
        raise BlacklightError(f"单次改标题 ≤{_TITLE_CAP} 个（本次 {len(updates)}）")
    for u in updates:
        if not u.get("productId") or not u.get("productName"):
            raise BlacklightError(f"行缺 productId/productName：{u}")
        n = str(u["productName"]).strip()
        if not n:
            raise BlacklightError(f"标题不能为空：{u}")
        if len(n) > _TITLE_MAX_LEN:
            raise BlacklightError(f"标题 {len(n)} 字超上限 {_TITLE_MAX_LEN}：{n[:24]}…")


def update_titles_dryrun(updates: list, biz_id: Optional[str] = None) -> dict:
    """[dry-run] 改商品名/长标题组装但**不发送**：校验(productId/productName必填·≤60字·≤50个)、回显 payload + confirm_token。
    updates=[{productId, productName}]。改的是**商品名(长标题)**，非秒杀短标题。"""
    updates = updates or []
    _title_validate(updates)
    return {"would_update": False, "count": len(updates),
            "note": "DRY-RUN：未改。真执行：相同 updates + confirm=confirm_token 调 update_titles。改的是商品名(长标题≤60)。",
            "payload": _title_payload(updates), "rows": updates,
            "confirm_token": _title_token(updates)}


@audited("product", "update_title")
def update_titles(updates: list, confirm: str = "", biz_id: Optional[str] = None) -> dict:
    """[写] **真改商品名/长标题**（ProductInfoWriteViewService.updateProducts，≤60字，SPU级，不可轻易撤回）。
    需相同 updates 先 update_titles_dryrun 拿 confirm_token 再带 confirm。单次 ≤50。⚠️非秒杀短标题。"""
    updates = updates or []
    try:
        _title_validate(updates)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}
    token = _title_token(updates)
    if confirm != token:
        return {"executed": False, "reason": "改标题需二次确认：相同 updates 先跑 update_titles_dryrun 拿 confirm_token 再带 confirm。"}
    bid = _biz_id(biz_id)
    data = _sff_post("dsm.product.manage.ProductInfoWriteViewService.updateProducts", _title_payload(updates), bid)
    return {"executed": True, "confirm_token": token, "count": len(updates), "response": data,
            "rows": [{"productId": u["productId"], "productName": str(u["productName"]).strip()} for u in updates]}


# --------------------------------------------------------------------------- #
# 上/下架（ProductStatusUpdateViewService.updateProductStatus）—— SPU 级、客户可见
# --------------------------------------------------------------------------- #
_STATUS_CAP = 100                     # 单次上下架 SPU 数上限
_OPS = {"up": "上架", "down": "下架"}   # operation 取值（抓包实证 2026-07-21）


def _norm_pids(product_ids) -> list:
    """归一化为 int productId 列表，去重保序。收 [pid,...] 或 [{'productId':pid},...]。"""
    out, seen = [], set()
    for p in (product_ids or []):
        pid = int(p.get("productId") if isinstance(p, dict) else p)
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def _status_token(product_ids: list, operation: str, down_reason: str = "") -> str:
    return _confirm_token({"path": "product/updateProductStatus", "op": operation,
                           "reason": down_reason or "", "pids": sorted(_norm_pids(product_ids))})


def _status_payload(product_ids: list, operation: str, biz_id: str, down_reason: str = "") -> dict:
    """字节对齐抓包：{productStatusReq:{operation,skuGroups:[{productId}],downReason},accessContext{...businessModel:'2'...}}。"""
    return {"productStatusReq": {"operation": operation,
                                 "skuGroups": [{"productId": pid} for pid in _norm_pids(product_ids)],
                                 "downReason": down_reason or ""},
            "accessContext": {"source": "web", "businessModel": "2",
                              "proxyBelongBizId": str(biz_id), "originType": None}}


def _status_validate(product_ids: list, operation: str) -> list:
    if operation not in _OPS:
        raise BlacklightError(f"operation 只能是 'up'(上架)/'down'(下架)，收到 {operation!r}")
    pids = _norm_pids(product_ids)
    if not pids:
        raise BlacklightError("没有要上下架的 productId")
    if len(pids) > _STATUS_CAP:
        raise BlacklightError(f"单次上下架 ≤{_STATUS_CAP} 个（本次 {len(pids)}）")
    return pids


def update_status_dryrun(product_ids: list, operation: str, down_reason: str = "",
                         biz_id: Optional[str] = None) -> dict:
    """[dry-run] 上/下架组装但**不发送**：校验 operation('up'/'down')、productId 列表(≤100·去重)，回显 payload + confirm_token。
    product_ids=[productId,...] 或 [{productId},...]。SPU 级。⚠️下架=客户端立即不可见。"""
    pids = _status_validate(product_ids, operation)
    bid = _biz_id(biz_id)
    return {"would_update": False, "operation": operation, "operation_cn": _OPS[operation], "count": len(pids),
            "product_ids": pids,
            "note": f"DRY-RUN：未{_OPS[operation]}。真执行：相同参数 + confirm=confirm_token 调 update_status。",
            "payload": _status_payload(pids, operation, bid, down_reason),
            "confirm_token": _status_token(pids, operation, down_reason)}


@audited("product", "update_status")
def update_status(product_ids: list, operation: str, confirm: str = "", down_reason: str = "",
                  biz_id: Optional[str] = None) -> dict:
    """[写] **真上/下架商品**（ProductStatusUpdateViewService.updateProductStatus，SPU 级，**客户端立即生效**！）。
    operation='down'(下架)/'up'(上架)。需相同参数先 update_status_dryrun 拿 confirm_token 再带 confirm。单次 ≤100。
    回执 data=[{data:productId, success:bool}] → 逐品自验证 success_count/failed。"""
    try:
        pids = _status_validate(product_ids, operation)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}
    token = _status_token(product_ids, operation, down_reason)
    if confirm != token:
        return {"executed": False,
                "reason": "上下架需二次确认：相同参数先跑 update_status_dryrun 拿 confirm_token 再带 confirm。"}
    bid = _biz_id(biz_id)
    data = _sff_post("dsm.product.manage.ProductStatusUpdateViewService.updateProductStatus",
                     _status_payload(pids, operation, bid, down_reason), bid)
    rows = data if isinstance(data, list) else []
    ok_ids = [r.get("data") for r in rows if isinstance(r, dict) and r.get("success")]
    fail = [r for r in rows if isinstance(r, dict) and not r.get("success")]
    return {"executed": True, "confirm_token": token, "operation": operation, "operation_cn": _OPS[operation],
            "count": len(pids), "success_count": len(ok_ids), "succeeded": ok_ids,
            "failed": fail, "response": data}
