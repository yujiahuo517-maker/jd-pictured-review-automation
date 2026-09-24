"""
yx-mcp 场域：subsidy（国家补贴 / 京喜-政府补贴），网关 mac.jd.com。

与 campaign(mcpman) 完全不同的模型：
  - 单位是"收品池(收品池/block)"，每个池固定一个力度档位(15%/10%/5%)。
  - 报名 = 把 SKU 报进某个池；退出 = 按报名编号(applyId)退出。
  - 请求为 **form-urlencoded**（不是 JSON）。

契约见 NOTES_endpoints.md（block-and-capture + 用户直取实证）。
写操作(apply/withdraw)走二次确认令牌：先 *_dryrun 拿 confirm_token，再带 confirm 真执行。
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import os
import time as _time
from typing import Optional
from urllib.parse import quote

from blacklight.core import auth as jd_auth, canon_num, paged_scan, pmap, retry_throttled
# 公共核心（错误类型、裸 httpx client、令牌、配置）来自 jd_core
from blacklight.core import (BlacklightError, bare_client as _client, confirm_token as _confirm_token,
                     post_multipart, audited, load_config)

MAC = "https://mac.jd.com"
MAX_BATCH = 50
# 表格报名(Excel 上传)——国补 SKU 场景码（实证）
SUBSIDY_EXCEL_SCENE = "SYSTEM_STATE_SUBSIDIES_SKU_EXCEL_TEMPLATE"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# 表格报名模板列（实证，8 列）
EXCEL_HEADERS = [
    "SKU",
    "报名模式(仅能填写:10,20,30(10:非超链-单店模式,20:超链1.0,30:超链2.0))",
    "促销生效时间", "商家名称",
    "能效等级(仅能填写:0,1,2,3,4,5(无:0,一级:1,二级:2,三级:3,四级:4,五级:5),选择其中一项)",
    "商品基准价", "产品品牌",
    "失败原因（请勿填写！当有素材上传失败时，失败原因将展示在此列）",
]

# --------------------------------------------------------------------------- #
# 收品池模板（pool config）：resourceList + formItemId 角色映射，均为**每池特有**。
# 下表是 block-and-capture 实证的"京喜-政府补贴 / 商品-15%力度"池（blockId 142902257）。
# 其它池需各自捕获一次（用 yx_subsidy.py capture 或抓一次 batch/create）。
# --------------------------------------------------------------------------- #
_POOLS_FALLBACK = {   # config.json 缺失时的兜底；正常从 config.json 的 subsidy_pools 读
    # blockId(收品池编号) -> 模板
    "142902257": {
        "desc": "京喜-政府补贴 / 商品-15%力度",
        "strength": "15", "activityId": "101854276",  # 该池力度档位；报名 discount 默认取此值（与池名一致）
        "resourceList": [{"resourceId": 159022372, "resourceType": 3, "nodeResourceId": 8068810511}],
        # 报名表单项的完整顺序（保持与页面一致）；roles 标注可填字段
        "formItemOrder": [1711228304, 1711228108, 1711228818, 1711228305, 1711229154,
                          1711228109, 1711228819, 1711228306, 1711229155, 1711229156,
                          1711228821, 1711228111, 1711228822, 1711228308, 1711228112,
                          1711228113, 1711228309, 1711228824, 1711228825, 1711229162],
        "roles": {
            "sku": 1711228304,          # 商品ID
            "discount": 1711228108,     # 直降力度（如 "10"=10%）
            "promoTime": 1711228818,    # 促销生效时间 "开始~结束"
            "merchant": 1711228306,     # 商家名称
            "energyLevel": 1711229155,  # 能效等级（"0"=无）
            "basePrice": 1711228821,    # 商品基准价
        },
    },
    # 注意：formItemId **每池不同**（非活动级共享），必须逐池捕获。
    "142901856": {
        "desc": "京喜-政府补贴 / 商品-10%力度",
        "strength": "10", "activityId": "101854276",
        "resourceList": [{"resourceId": 159022494, "resourceType": 3, "nodeResourceId": 8068807684}],
        "formItemOrder": [1711229145, 1711228807, 1711229146, 1711228292, 1711229147,
                          1711228098, 1711229148, 1711228293, 1711228808, 1711228809,
                          1711228100, 1711229150, 1711228810, 1711228811, 1711229151,
                          1711228295, 1711229152, 1711228812, 1711228298, 1711228303],
        "roles": {
            "sku": 1711229145,
            "discount": 1711228807,
            "promoTime": 1711229146,
            "merchant": 1711228293,
            "energyLevel": 1711228808,
            "basePrice": 1711228100,
        },
    },
    "142899998": {
        "desc": "京喜-政府补贴 / 商品-5%力度",
        "strength": "5", "activityId": "101854276",
        "resourceList": [{"resourceId": 159016715, "resourceType": 3, "nodeResourceId": 8068806859}],
        "formItemOrder": [1711222841, 1711222842, 1711222843, 1711222279, 1711222631,
                          1711222844, 1711222280, 1711222281, 1711222282, 1711222632,
                          1711222633, 1711222285, 1711222286, 1711222845, 1711222287,
                          1711222846, 1711222288, 1711222636, 1711222637, 1711223370],
        "roles": {
            "sku": 1711222841,
            "discount": 1711222842,
            "promoTime": 1711222843,
            "merchant": 1711222281,
            "energyLevel": 1711222282,
            "basePrice": 1711222633,
        },
    },
}

# 收品池模板：优先 config.json 的 subsidy_pools（加新池不用改代码），缺失时用上面兜底。
POOLS = load_config().get("subsidy_pools") or _POOLS_FALLBACK


def get_pool(block_id: str | int) -> dict:
    t = POOLS.get(str(block_id))
    if not t:
        raise BlacklightError(f"未知收品池 {block_id}；请先捕获其 resourceList/formItemId 模板（见 NOTES）。"
                      f"已知: {list(POOLS)}")
    return t


# --------------------------------------------------------------------------- #
# form-urlencoded 请求
# --------------------------------------------------------------------------- #
def _headers_form(cookie: str) -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://yx.jd.com",
        "Referer": "https://yx.jd.com/",
        "Cookie": cookie,
    }


def _encode_form(params: dict) -> str:
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())


def _mac_post(path: str, form: dict, cookie: Optional[str] = None, send: bool = True) -> dict:
    """POST form-urlencoded 到 mac.jd.com。send=False 时不发送，仅返回将发送内容。"""
    cookie = cookie or jd_auth.ensure_session()
    body = _encode_form(form)
    if not send:
        return {"would_send": False, "url": f"{MAC}{path}", "body_form": body}
    with _client() as c:
        r = c.post(f"{MAC}{path}", content=body, headers=_headers_form(cookie))
    if r.status_code in (301, 302, 303, 307, 308):
        raise BlacklightError(f"{path} 被重定向（{r.status_code}）——登录态可能失效，请 jd_auth --login")
    try:
        return r.json()
    except Exception as e:
        raise BlacklightError(f"{path} 未返回 JSON（HTTP {r.status_code}）") from e


# --------------------------------------------------------------------------- #
# 只读
# --------------------------------------------------------------------------- #
MAX_PAGE_SIZE = 100   # 实证 2026-07-27：传 500 服务端**静默降级**到 100，不报错 → 本地先挡住


def get_applied(block_id: str | int, page: int = 1, page_size: int = 10,
                charge_mode: str = "", check_status: str = "", sync_status: str = "",
                sku_id: str | int = "") -> dict:
    """已报名素材列表（POST /apply/page, form-encoded）。areaId=收品池编号(blockId)。
    sku_id: 精确过滤某 SKU（服务端支持，比翻页找快得多）。
    ⚠️page_size 服务端上限 100（传更大会**静默降级**，不报错）→ 超了本地直接报错，避免误判"已拉全"。
    全量拉取用 list_applied_skus（紧凑字段，不会撑爆上下文）。"""
    if int(page_size) > MAX_PAGE_SIZE:
        raise BlacklightError(
            f"page_size={page_size} 超服务端上限 {MAX_PAGE_SIZE}（传大值会被静默降级成 100，"
            f"会让你以为已拉全其实没有）。改用 list_applied_skus(block_id) 做全量紧凑拉取。")
    form = {"areaId": str(block_id), "applyId": "", "chargeMode": charge_mode,
            "checkStatus": check_status, "syncStatus": sync_status,
            "page": page, "pageSize": page_size, "crowdId": ""}
    if str(sku_id).strip():
        form["skuId"] = str(sku_id).strip()
    return _mac_post("/apply/page", form)


def list_applied_skus(block_id: str | int, limit: int = 5000) -> dict:
    """**全量拉某收品池已报名 SKU**，只返回 {skuId, applyId, 基准价} 紧凑三元组。

    为什么要有这个：`get_applied` 每行 ~2KB 完整报名对象，2015 行 = 4MB，翻 21 页会直接撑爆上下文
    （2026-07-27 实证）。做批量复核/退出时只需要 skuId(算毛利) + applyId(退出) + 基准价，其余全是噪音。
    典型用法：拉全量 → osw batch_pricing 算基准价毛利率 → 低于门槛线(23.7%)的批量退出。
    """
    def _fetch(page):
        d = (get_applied(block_id, page=page, page_size=MAX_PAGE_SIZE).get("data") or {})
        return (d.get("items") or []), d.get("totalCount")

    # 走公共 paged_scan：**空页会重试**（此前只报 truncated 不重试，实测撞过 1300/2015）
    r = paged_scan(_fetch, MAX_PAGE_SIZE, max_pages=max(1, -(-limit // MAX_PAGE_SIZE)))
    out, total = [], r["total"]
    for it in r["items"][:limit]:
        info = (it.get("appliedDataPO") or {}).get("itemInfo") or {}
        out.append({"skuId": info.get("skuId"), "applyId": it.get("applyId"),
                    "基准价": info.get("goodsBasePrice")})
    # 按**本函数自己的口径**报截断（paged_scan 的计数是切 limit 之前的，直接透出会对不上）
    capped = total is not None and len(out) < total
    note = (None if not capped else
            (f"只返回 {len(out)}/{total} 条——**是入参 limit={limit} 截的**，不是接口问题；要全量请调大 limit"
             if len(out) >= limit else
             f"只拉到 {len(out)}/{total} 条（{r['stop_reason']}）——**别当全量用**"))
    return {"blockId": str(block_id), "totalCount": total, "fetched": len(out),
            "complete": not capped, "truncated": note,
            "items": out,
            "note": "紧凑三元组(skuId/applyId/基准价)。批量复核：喂 osw batch_pricing 算基准价毛利率，"
                    "低于报名门槛线的用 applyId 走 subsidy_withdraw_batch 退出。"}


def find_sku(sku_id: str | int) -> list:
    """查某 SKU 在各国补收品池的报名（apply/page 支持 skuId 精确过滤）。
    返回 [{pool, blockId, applyId, checkStatus, strength}]。用于「单SKU全券促」把国补也纳进来。"""
    sku_id = str(sku_id).strip()
    out = []
    for blk, tpl in POOLS.items():
        form = {"areaId": str(blk), "applyId": "", "chargeMode": "", "checkStatus": "",
                "syncStatus": "", "page": 1, "pageSize": 50, "crowdId": "", "skuId": sku_id}
        d = _mac_post("/apply/page", form).get("data") or {}
        for it in (d.get("items") or []):
            info = (it.get("appliedDataPO") or {}).get("itemInfo") or {}
            if str(info.get("skuId")) != sku_id:
                continue
            if it.get("checkStatus") == 4:   # 4=已退出，跳过（记录仍在列表但已失效）
                continue
            out.append({"pool": tpl.get("desc", str(blk)), "blockId": str(blk),
                        "applyId": it.get("applyId"), "checkStatus": it.get("checkStatus"),
                        "strength": tpl.get("strength")})
    return out


def check_sku(block_id: str | int, sku_id: str | int) -> dict:
    """
    取 SKU 商品信息 + 基本校验（POST /apply/sku/check）。只读。
    返回 {skuId, eligible(code==0), code, message, jdPrice(前台京东价/基准价), categoryName}。
    ⚠️ 注意：此接口**不区分 POP/自营**（实测 POP 与自营返回相同 code=0）；POP 等报名限制由平台在
    batch/create 时才拦截（响应 success=false）。故 eligible=True 仅表示"SKU 有效/可取价"，不保证可报。
    """
    t = get_pool(block_id)
    act = t.get("activityId")
    if not act:
        raise BlacklightError(f"池 {block_id} 未配置 activityId，无法 check_sku")
    j = _mac_post("/apply/sku/check",
                  {"skuIds": str(sku_id), "areaId": str(block_id), "activityId": str(act)})
    arr = j.get("data") or []
    row = arr[0] if arr else {}
    info = {}
    try:
        info = _json.loads(row.get("data") or "{}")
    except Exception:
        pass
    code = row.get("code")
    return {"skuId": str(sku_id), "eligible": code == 0, "code": code,
            "message": row.get("message") or info.get("checkMsg") or "",
            "jdPrice": info.get("jdPrice") or info.get("jdBackendPrice"),
            "categoryName": info.get("categoryName")}


def check_skus(block_id: str | int, sku_ids: list) -> dict:
    """**批量**取多 SKU 商品信息 + 基本校验（逐 SKU 查，保证与输入一一对应）。返回 {skuId: {...}}。"""
    sku_ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    return {s: check_sku(block_id, s) for s in dict.fromkeys(sku_ids)}


def _resolve_base_price(block_id, sku_id, base_price) -> str:
    """base_price 未给时，用 check_sku 自动取前台京东价。
    注：check_sku 不拦 POP（见其 docstring）；真正的报名限制由 batch/create 响应体现。"""
    if base_price not in (None, ""):
        return str(base_price)
    chk = check_sku(block_id, sku_id)
    if not chk["eligible"]:
        raise BlacklightError(f"SKU {sku_id} check 未通过: code={chk['code']} {chk['message']}")
    if not chk.get("jdPrice"):
        raise BlacklightError(f"未能自动获取 SKU {sku_id} 基准价，请显式传 base_price")
    return str(chk["jdPrice"])


# --------------------------------------------------------------------------- #
# 报名（apply）—— apply/batch/create，dry-run + 真执行(confirm 门)
# --------------------------------------------------------------------------- #
def _apply_item_dto(block_id, t, sku_id, discount, base_price,
                    merchant, energy_level, promo_time) -> list:
    """单个 SKU 的 applyItemDTOList。base_price 未给则自动取前台京东价。"""
    base_price = _resolve_base_price(block_id, sku_id, base_price)
    roles = t["roles"]
    values = {roles["sku"]: str(sku_id), roles["discount"]: str(discount),
              roles["promoTime"]: promo_time, roles["merchant"]: merchant,
              roles["energyLevel"]: str(energy_level), roles["basePrice"]: str(base_price)}
    return [{"formItemId": fid, "value": values.get(fid, "")} for fid in t["formItemOrder"]]


def default_promo_time() -> str:
    """国补促销生效时间**规定**：报名当日 00:00:00 ~ 当年 12-31 23:59:59（用户确认的口径，留空时默认此值）。"""
    today = _dt.date.today()
    return f"{today:%Y-%m-%d} 00:00:00~{today.year}-12-31 23:59:59"


def _build_apply_bulk(block_id, sku_ids, discount=None, base_prices=None,
                      merchant="京喜自营", energy_level="0", promo_time="") -> dict:
    """
    组装 batch/create 的 applyBulkData（**支持多 SKU**：applyList 每 SKU 一条）。
    discount 留空取池力度；base_prices 是可选 {sku:price}，缺的自动取前台京东价。
    promo_time 留空 → 默认「报名当日~当年12/31」(default_promo_time)。
    ⚠️ 多 SKU 的 applyList 结构为**推断**（实证样本均为单 SKU），启用批量真执行前建议先 dry-run 核对。
    """
    if not promo_time:
        promo_time = default_promo_time()
    t = get_pool(block_id)
    if discount in (None, ""):
        discount = t.get("strength")
        if discount in (None, ""):
            raise BlacklightError(f"池 {block_id} 未配置 strength，需显式传 discount(力度)")
    bp = base_prices or {}
    apply_list = [{"applyItemDTOList": _apply_item_dto(
        block_id, t, s, discount, bp.get(str(s)), merchant, energy_level, promo_time)}
        for s in sku_ids]
    return {"resourceList": t["resourceList"], "applyList": apply_list}


def _apply_form(bulk: dict) -> dict:
    # 与页面一致：applyBulkData=<urlencoded JSON>&finalCheckAutoPass=
    return {"applyBulkData": _json.dumps(bulk, ensure_ascii=False, separators=(",", ":")),
            "finalCheckAutoPass": ""}


def _apply_envelope(bulk: dict, single: bool) -> dict:
    form = _apply_form(bulk)
    token = _confirm_token({"path": "/apply/batch/create", **form})
    n = len(bulk["applyList"])
    note = ("DRY-RUN：报名请求未发送。真执行：相同参数 + confirm=confirm_token。"
            + ("" if single else " ⚠️多SKU applyList 结构为推断，请核对 decoded 后再真执行。"))
    return {"would_send": False, "note": note,
            "request": {"method": "POST", "url": f"{MAC}/apply/batch/create",
                        "content_type": "application/x-www-form-urlencoded",
                        "form": form, "decoded_applyBulkData": bulk},
            "confirm_token": token, "sku_count": n}


def apply_dryrun(block_id, sku_id, base_price=None, discount=None, merchant="京喜自营",
                 energy_level="0", promo_time="") -> dict:
    """报名 DRY-RUN（单 SKU）。discount 默认取池力度；base_price 未给自动取前台京东价。"""
    bp = {str(sku_id): base_price} if base_price not in (None, "") else None
    bulk = _build_apply_bulk(block_id, [sku_id], discount, bp, merchant, energy_level, promo_time)
    return _apply_envelope(bulk, single=True)


# ⚠️ batch/create 的 applyItemDTOList **不含"报名模式"字段**——真报名会被平台默认成错误模式(超链)，导致报名无效。
# 实证教训(2026-07-08)：这样报的国补全被判失败、需退出重报。**国补真报名一律走 table_apply**(报名模式默认10=非超链单店，正确)。
_BATCH_CREATE_BROKEN = ("国补 batch/create(apply/apply_batch) 缺「报名模式」字段，真报会被平台默认成错误的超链模式→报名失败。"
                        "**请改用 subsidy_table_apply(报名模式默认 10=非超链-单店，正确)**。dry-run 仍可用 *_dryrun 看结构。")


@audited("subsidy", "apply")
def apply(block_id, sku_id, base_price=None, discount=None, merchant="京喜自营",
          energy_level="0", promo_time="", confirm="") -> dict:
    """⚠️ **已停真执行**（batch/create 缺报名模式，会报错模式）。国补真报名请用 `table_apply`。"""
    raise BlacklightError(_BATCH_CREATE_BROKEN)


def apply_batch_dryrun(block_id, sku_ids, discount=None, merchant="京喜自营",
                       energy_level="0", promo_time="") -> dict:
    """**批量**报名 DRY-RUN：多 SKU 一次 batch/create。每 SKU 自动取前台京东价。返回 confirm_token。"""
    sku_ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not sku_ids:
        raise BlacklightError("apply_batch 需要至少一个 skuId")
    if len(sku_ids) > MAX_BATCH:
        raise BlacklightError(f"单次报名 SKU 数 {len(sku_ids)} 超过上限 {MAX_BATCH}，请分批")
    bulk = _build_apply_bulk(block_id, sku_ids, discount, None, merchant, energy_level, promo_time)
    return _apply_envelope(bulk, single=False)


@audited("subsidy", "apply_batch")
def apply_batch(block_id, sku_ids, discount=None, merchant="京喜自营",
                energy_level="0", promo_time="", confirm="") -> dict:
    """⚠️ **已停真执行**（batch/create 缺报名模式，会报错模式）。国补批量真报名请用 `table_apply`。"""
    raise BlacklightError(_BATCH_CREATE_BROKEN)


# --------------------------------------------------------------------------- #
# 退出（withdraw）—— apply/quit，dry-run + 真执行(confirm 门)
# --------------------------------------------------------------------------- #
def withdraw_dryrun(apply_id: str | int) -> dict:
    """退出 DRY-RUN：组装 /apply/quit 但**不发送**，返回将 POST 的 form + confirm_token。"""
    form = {"applyId": str(apply_id)}
    token = _confirm_token({"path": "/apply/quit", **form})
    return {"would_send": False,
            "note": "DRY-RUN：退出请求未发送。真执行：相同 applyId + confirm=confirm_token 调 subsidy_withdraw。",
            "request": {"method": "POST", "url": f"{MAC}/apply/quit",
                        "content_type": "application/x-www-form-urlencoded", "form": form},
            "confirm_token": token}


@audited("subsidy", "withdraw")
def withdraw(apply_id: str | int, confirm: str = "") -> dict:
    """**真执行**退出（按报名编号 applyId 退出该 SKU 的国补报名）。需 confirm_token。"""
    form = {"applyId": str(apply_id)}
    token = _confirm_token({"path": "/apply/quit", **form})
    if confirm != token:
        raise BlacklightError("退出真执行需二次确认：先用相同 applyId 跑 subsidy_withdraw_dryrun 拿 confirm_token 再带 confirm。")
    resp = _mac_post("/apply/quit", form)
    ok = bool(isinstance(resp, dict) and resp.get("success") is True)
    return {"executed": True, "confirm_token": token, "response": resp, "success": ok}


def _quit_ids(apply_ids) -> list:
    ids = [str(a).strip() for a in apply_ids if str(a).strip()]
    if not ids:
        raise BlacklightError("withdraw_batch 需要至少一个 applyId")
    if len(ids) > MAX_BATCH:
        raise BlacklightError(f"单次退出 {len(ids)} 超过上限 {MAX_BATCH}，请分批")
    return list(dict.fromkeys(ids))


def resolve_withdrawable(sku_ids: list, workers: int = 4) -> dict:
    """★**批量退国补前必须先过这一步**：把 SKU 解析成真正可退的 applyId，并分桶。

    返回 `{可退:[{skuId,applyId,checkStatus,pool}], 无报名:[skuId...], 失败:{skuId:err}, summary}`。

    ## 为什么必须有它（2026-08-18 实证）
    毛利监控/前瞻网给的**根因「国补打穿到手价」≠ 这款能退国补**——它看得见**国补减免**，
    看不见**报名归属**：国补可能是别人（别的采销/平台）报的，你账号下根本没有 applyId。
    实测按根因取 31 款直接批量退，**只有 11 款查得到 applyId，20 款（65%）会打空**。
    ⇒ 「根因=国补」只是**线索**，`find_sku` 拿到 applyId 才是**可操作性**。
      （同一条区分见 `docs/pnl/NOTES_playbook.md`：归因答"谁吃了钱"，可行性答"摘了能省多少"。）

    ⚠️退出是**异步**的：`withdraw` 回执 success 后**立即回读 find_sku 仍会看到记录**
      （checkStatus 还是 2）。实测等 ~120 秒后 11/11 记录才全部消失。
      **别拿立即回读判失败**，见 [[campaign-quit-status9-not-live]]。
    """
    ids = [str(s).strip() for s in (sku_ids or []) if str(s).strip()]
    ids = list(dict.fromkeys(ids))

    def _q(s):
        try:
            return s, find_sku(s)
        except Exception as e:
            return s, {"err": str(e)[:80]}

    ok, none_, err = [], [], {}
    for s, v in pmap(_q, ids, workers):
        if isinstance(v, dict):
            err[s] = v.get("err")
        elif v:
            for it in v:
                ok.append({"skuId": s, "applyId": it.get("applyId"),
                           "checkStatus": it.get("checkStatus"), "pool": it.get("pool")})
        else:
            none_.append(s)
    return {"可退": ok, "无报名": none_, "失败": err,
            "summary": {"输入": len(ids), "可退SKU": len({x["skuId"] for x in ok}),
                        "可退applyId": len(ok), "无本账号报名": len(none_), "查询失败": len(err),
                        "_口径": "无报名=国补减免存在但报名不在本账号名下 ⇒ 退不了，别浪费写操作"},
            "apply_ids": [str(x["applyId"]) for x in ok]}


def withdraw_batch_dryrun(apply_ids: list) -> dict:
    """**批量**退出 DRY-RUN：apply/quit 无原生批量接口，按 applyId **逐个**退出（循环单退）。返回 confirm_token。"""
    ids = _quit_ids(apply_ids)
    token = _confirm_token({"path": "/apply/quit#batch", "ids": ids})
    return {"would_send": False,
            "note": "DRY-RUN：未发送。将对每个 applyId 逐个 POST /apply/quit。真执行：相同 applyId 列表 + confirm=confirm_token。",
            "requests": [{"url": f"{MAC}/apply/quit", "form": {"applyId": i}} for i in ids],
            "confirm_token": token, "count": len(ids)}


@audited("subsidy", "withdraw_batch")
def withdraw_batch(apply_ids: list, confirm: str = "") -> dict:
    """**批量真执行**退出：逐个 apply/quit。需相同 applyId 列表先 dry-run 拿 confirm_token 再带 confirm。"""
    ids = _quit_ids(apply_ids)
    token = _confirm_token({"path": "/apply/quit#batch", "ids": ids})
    if confirm != token:
        raise BlacklightError("批量退出需二次确认：先用相同 applyId 列表跑 subsidy_withdraw_batch_dryrun 拿 confirm_token 再带 confirm。")
    results = []
    for aid in ids:
        try:
            resp = _mac_post("/apply/quit", {"applyId": aid})
            results.append({"applyId": aid, "success": bool(resp.get("success") is True),
                            "message": resp.get("message")})
        except BlacklightError as e:
            results.append({"applyId": aid, "success": False, "message": str(e)})
    return {"executed": True, "confirm_token": token, "count": len(ids),
            "all_success": all(r["success"] for r in results), "results": results}


# --------------------------------------------------------------------------- #
# 表格报名（Excel 文件上传，单次≤100,000 行）—— 大批量正道
# 上传 POST /common/fileProcess（multipart: sceneCode, resourceId, resourceType, file）
# 轮询 POST /common/fileProcess/page → 导入成功/失败数
# --------------------------------------------------------------------------- #
def build_apply_xlsx(block_id, sku_ids, out_path, promo_time="",
                     merchant="京喜自营", energy_level="0", report_mode=10,
                     base_prices=None) -> dict:
    """生成表格报名 xlsx（8 列）。每 SKU 基准价缺省自动取前台京东价(check_sku)。promo_time 留空→默认报名当日~当年12/31。返回 {path, rows, prices}。"""
    try:
        import openpyxl
    except ImportError as e:
        raise BlacklightError("缺少依赖 openpyxl，请先 pip install openpyxl") from e
    if not promo_time:
        promo_time = default_promo_time()
    sku_ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    bp = dict(base_prices or {})
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "sheet1"
    ws.append(EXCEL_HEADERS)
    rows = []
    for sku in sku_ids:
        price = bp.get(sku) or _resolve_base_price(block_id, sku, None)
        # A SKU, B 报名模式, C 促销时间, D 商家名称, E 能效等级, F 基准价, G 品牌, H 失败原因(空)
        ws.append([sku, report_mode, promo_time, merchant, energy_level, str(price), "", ""])
        rows.append({"sku": sku, "basePrice": str(price)})
    wb.save(out_path)
    return {"path": out_path, "rows": len(rows), "items": rows}


def upload_excel(block_id, xlsx_path) -> dict:
    """
    上传表格报名 Excel（POST /common/fileProcess, multipart）。**真实提交**：成功即进入导入队列。
    返回 {success, taskId(=data), response}。taskId 用于轮询 file_process_page。

    ## ★文件上传闸是**跨频道共享**的（2026-08-19 实证）
    本函数走 mac 网关、campaign 表格报名走 mcpman 网关，是两套完全不同的活动体系，
    但**共用同一个上传节流窗口**（约 90~120 秒一个文件）。所以连着跑两条报名线必然撞上：
    当天直降 92 款上传成功后立刻传国补 16 款，被挡回
    `{"success": false, "code": "10001", "message": "多个文件上传需要再等81秒"}`。

    ★判「撞节流」而不是「部分失败」的判据：**`taskId is None` + `success=False`**
      ⇒ 文件根本没进队列，**一条都没落地**，可原样干净重试（已回读证实 0/16 落地）。
      别当部分失败去做增量补报——那会把已落地的又报一遍。
    ⇒ 已接 `core.policy` 的 `upload.file` 闸 + 退避重试（文案自带秒数时优先采信）。
    """
    t = get_pool(block_id)
    res = t["resourceList"][0]
    if not os.path.isfile(xlsx_path):
        raise BlacklightError(f"文件不存在: {xlsx_path}")
    cookie = jd_auth.ensure_session()
    data = open(xlsx_path, "rb").read()
    files = {"file": (os.path.basename(xlsx_path), data, XLSX_MIME)}
    form = {"sceneCode": SUBSIDY_EXCEL_SCENE,
            "resourceId": str(res["resourceId"]), "resourceType": str(res["resourceType"])}
    # ★跨频道共享的文件上传闸 + 退避重试（clean_reject=True：taskId 为空即一条没落地）
    #   `trace` 必传：退避可能耗时十几分钟，不透出来调用方只会觉得"卡住了"（2026-08-19 实撞）。
    trace: list = []
    t0 = _time.monotonic()
    j = retry_throttled(
        lambda: post_multipart(MAC, "/common/fileProcess", form, files, cookie),  # 公共 multipart（jd_core）
        "upload.file",
        is_bad=lambda r: (r or {}).get("message") if not (r or {}).get("success") else None,
        trace=trace)
    j = j or {}
    return {"success": bool(j.get("success") is True), "taskId": j.get("data"), "response": j,
            "耗时s": round(_time.monotonic() - t0, 1), "限速重试轨迹": trace or None}


def file_process_page(block_id, page=1, page_size=10) -> dict:
    """轮询导入进度（POST /common/fileProcess/page, form）。返回该池最近的导入任务列表(成功/失败数)。"""
    res = get_pool(block_id)["resourceList"][0]
    form = {"sceneCode": SUBSIDY_EXCEL_SCENE, "resourceId": str(res["resourceId"]),
            "resourceType": str(res["resourceType"]), "page": page, "pageSize": page_size}
    return _mac_post("/common/fileProcess/page", form)


def _table_token(block_id, sku_ids, promo_time, merchant, energy_level, report_mode) -> str:
    return _confirm_token({"path": "/common/fileProcess", "block": str(block_id),
                           "skus": sorted(sku_ids), "pt": promo_time, "m": merchant,
                           "e": canon_num(energy_level), "mode": str(report_mode)})


def table_apply_dryrun(block_id, sku_ids, promo_time="", merchant="京喜自营",
                       energy_level="0", report_mode=10) -> dict:
    """表格报名 DRY-RUN：不生成不上传，回显将上传的行数/参数 + confirm_token。真执行：相同参数 + confirm 调 table_apply。
    promo_time 留空→默认报名当日~当年12/31。"""
    sku_ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not sku_ids:
        raise BlacklightError("table_apply 需要至少一个 skuId")
    promo_time = promo_time or default_promo_time()
    if len(sku_ids) > 100000:
        raise BlacklightError(f"单次表格报名 SKU 数 {len(sku_ids)} 超过平台上限 100000")
    return {"would_upload": False, "sku_count": len(sku_ids),
            "note": "DRY-RUN：未生成/未上传。真执行：相同参数 + confirm=confirm_token 调 table_apply。",
            "target": {"pool": get_pool(block_id)["desc"], "report_mode": report_mode,
                       "merchant": merchant, "energy_level": energy_level, "promo_time": promo_time},
            "confirm_token": _table_token(block_id, sku_ids, promo_time, merchant, energy_level, report_mode)}


def _await_import(block_id, task_id, timeout: int = 120, interval: int = 5) -> dict:
    """轮询 fileProcess/page 到本次导入任务终态(**processStatus==1 完成**)，回执自验证。
    fileProcess 回执结构=`data.items[]`：`id/processStatus(1完成)/successCount/failCount/failUrl(失败明细直链)/successUrl`。"""
    deadline = _time.time() + timeout
    tid = str(task_id)
    last = None
    while _time.time() < deadline:
        d = file_process_page(block_id, page=1, page_size=10)
        items = (d.get("data") or {}).get("items") or []
        me = next((t for t in items if str(t.get("id")) == tid), None)
        if me:
            last = me
            if me.get("processStatus") == 1:                      # 1=完成
                succ, fail = me.get("successCount") or 0, me.get("failCount") or 0
                return {"done": True, "processStatus": 1, "total": succ + fail,
                        "success": succ, "fail": fail,
                        "failUrl": me.get("failUrl") or "", "successUrl": me.get("successUrl") or ""}
        _time.sleep(interval)
    return {"done": False, "note": f"轮询{timeout}s未到终态，稍后 subsidy_import_progress 查", "last": last}


@audited("subsidy", "table_apply")
def table_apply(block_id, sku_ids, promo_time="", merchant="京喜自营", energy_level="0",
                report_mode=10, confirm="", xlsx_path=None, wait: bool = True, wait_timeout: int = 120) -> dict:
    """
    **表格报名真执行**：生成 xlsx → 上传 /common/fileProcess（真实提交，进入异步导入队列）。单次≤100,000 行。
    需相同参数先 table_apply_dryrun 拿 confirm_token 再带 confirm。**wait=True 自动轮询到终态并回执自验证**
    （返回 result: 成功/失败数 + 失败明细直链 failUrl），别看有延迟的监控数据。promo_time 留空→默认报名当日~当年12/31。
    """
    sku_ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not sku_ids:
        raise BlacklightError("table_apply 需要至少一个 skuId")
    promo_time = promo_time or default_promo_time()
    if len(sku_ids) > 100000:
        raise BlacklightError(f"单次表格报名 SKU 数 {len(sku_ids)} 超过平台上限 100000")
    token = _table_token(block_id, sku_ids, promo_time, merchant, energy_level, report_mode)
    if confirm != token:
        raise BlacklightError("表格报名真执行需二次确认：先用相同参数跑 subsidy_table_apply_dryrun 拿 confirm_token 再带 confirm。")
    if not xlsx_path:
        xlsx_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 f"_tableapply_{block_id}.xlsx")
    built = build_apply_xlsx(block_id, sku_ids, xlsx_path, promo_time,
                             merchant=merchant, energy_level=energy_level, report_mode=report_mode)
    up = upload_excel(block_id, xlsx_path)
    out = {"executed": True, "taskId": up["taskId"], "success": up["success"],
           "rows": built["rows"], "confirm_token": token, "response": up["response"],
           # ★把上传耗时与限速/退避轨迹带出来：没有它，一次十几分钟的退避在返回值和
           #   审计里都看不出原因（@audited 包在最外层，重试在里面转）。2026-08-19 实撞。
           "上传耗时s": up.get("耗时s"), "限速重试轨迹": up.get("限速重试轨迹")}
    if wait:
        out["result"] = _await_import(block_id, up["taskId"], timeout=wait_timeout)
        out["note"] = "回执自验证：判成败以 result.success/fail 为准（失败明细见 result.failUrl），别看延迟监控。"
    else:
        out["note"] = "已进入异步导入队列，wait=False；用 subsidy_import_progress 轮询成功/失败数。"
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="yx subsidy(国补) 客户端")
    ap.add_argument("--applied", metavar="BLOCKID")
    ap.add_argument("--check-sku", nargs=2, metavar=("BLOCKID", "SKU"))
    ap.add_argument("--dryrun-apply", nargs=2, metavar=("BLOCKID", "SKU"))
    ap.add_argument("--base-price", default=None, help="基准价；留空自动取前台京东价")
    ap.add_argument("--discount", default=None, help="力度；留空默认取池力度档位")
    ap.add_argument("--promo-time", default="2026-07-06 00:00:00~2026-12-31 23:59:59")
    ap.add_argument("--dryrun-quit", metavar="APPLYID")
    a = ap.parse_args()
    out: object
    if a.applied:
        out = get_applied(a.applied, page_size=5)
    elif a.check_sku:
        out = check_sku(a.check_sku[0], a.check_sku[1])
    elif a.dryrun_apply:
        b, s = a.dryrun_apply
        out = apply_dryrun(b, s, base_price=a.base_price, discount=a.discount, promo_time=a.promo_time)
    elif a.dryrun_quit:
        out = withdraw_dryrun(a.dryrun_quit)
    else:
        out = {"pools": {k: v["desc"] for k, v in POOLS.items()}}
    print(_json.dumps(out, ensure_ascii=False, indent=2, default=str))
