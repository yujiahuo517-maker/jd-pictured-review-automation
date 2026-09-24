"""yx 场域：ssm（顺手买 / 黄流结算页专享价），网关 mac.jd.com + actcenter.jd.com。

页面 `yx.jd.com/activity-manage/myCreate/daily-activities-operation/pitEnroll/roomlist?activityId=101757021`。

## ★★这是第三套报名体系，别和另外两套混（2026-08-21 实证）

| | campaign(招商) | ms/openness(秒杀·包邮) | **本模块(顺手买)** |
|---|---|---|---|
| 查活动 | `/campaign/getCampaignApplyDetailById` | `/openness/area/detail` | `mac/area/page` + `actcenter/room/...` |
| 报名 | mcpman 表格 | `/apply/openness/apply/excel` | **`mac/common/fileProcess`**（同国补） |
| 定位键 | campaignId | areaId + batchId | **areaId → resourceId**（两个不同的号！） |

拿 `yx_campaign_get_detail(101757021)` 会报「活动id不存在」；
拿 `ms.find_applied(...)` 会报「收品池信息不存在」——**都不是登录态问题，是走错体系**。

## ★两个门槛：价格松、资质紧（266 款实盘）

- **价格门槛只有「低于前台价 9 折」**（`room_rules()` 的 profit / benefitsIntroduction 写明）。
  活动配置里那段「到手价 ≤ 活动上线前 15 天历史最低成交价」是**大促模板套话**，
  266 款实盘**一条都没被价格拒**。报更深是业务选择（换量），不是平台要求。
- **真正卡人的是商品资质**：266 款失败 15 款 = 14 款「好评率要求 88.0% 以上」+ 1 款「价格星级不达标」。
  **改价救不了**，只能换品。
- ⚠️**别用选品表的好评率预筛**：平台口径与表格有出入（表 0.86→平台 87%，甚至表 0.50→平台 85%），
  按表格 <0.88 筛会误杀 10 款实际能过的。**全量提交让平台判**。

## ★回执怎么读

`apply_result()` 拿 `successCount/failCount` + **failUrl 的 xlsx 最后一列是逐行失败原因**。
重复上传同一批会逐行报「在该场次下已报名，报名人为：xxx」——**这条可以当回读用**，
反向证明上一批确实落地了。落地数也可用 `list_pools()` 的 `已报/容量` 交叉验证。

## ⚠️resourceId 按池而异，且**不是 areaId**

上传定位用 `resourceId`（池3 142833326 → 159010800），与 areaId 是两个号。
新池的 resourceId 目前只能从该池上传动作的抓包里拿，拿到后补进 `POOL_RESOURCE`。
"""
from __future__ import annotations

import os
import time as _time
from typing import Optional

from blacklight.core import (BlacklightError, audited, bare_client as _client,
                             confirm_token as _confirm_token, post_multipart,
                             retry_throttled)
from blacklight.core import auth as jd_auth
from blacklight.yx.subsidy import MAC, XLSX_MIME, _mac_post   # 复用 mac 网关 form-post + 常量

ACTIVITY_ID = "101757021"
ACTCENTER = "https://actcenter.jd.com"
SCENE = "GOLD_EASY_BUY_UPLOAD_SKU_EXCEL_TEMPLATE"

#: 模板 6 列（逐字取自平台导出的模板，**列名必须一字不差**，否则回执报「Excel读取数据异常」）
EXCEL_HEADERS = [
    "商品SKU",
    "结算页专享价(结算页专享价建议小于京东价、小于等于近30天最低普惠到手价。)",
    "专享价库存(如必填，建议专享价库存应小于等于当前库存；专享价库存需大于限购数量。如非必填，不填则表示不限制库存上限。)",
    "用户单次限购数量上限(含义：用户单次购买能享受优惠的商品数量上下限。  限购数量需小于专享价库存。若未填值，默认系统填10)",
    "每个账号限购订单数上限",
    "失败原因（请勿填写！当有素材上传失败时，失败原因将展示在此列）",
]

#: areaId(收品池) → resourceId(上传定位键)。**抓包实证，新池需补**。
POOL_RESOURCE = {
    142833326: 159010800,     # 京喜顺手买收品池3号-主用（容量 2263）
}
RESOURCE_TYPE = 3

DEFAULT_STOCK = 100000
DEFAULT_LIMIT_QTY = 20
DEFAULT_LIMIT_ORD = 20


def _resource_id(area_id: int, resource_id=None) -> int:
    if resource_id:
        return int(resource_id)
    rid = POOL_RESOURCE.get(int(area_id))
    if not rid:
        raise BlacklightError(
            f"收品池 {area_id} 的 resourceId 未知（它**不是 areaId**，是另一个号）。"
            f"已知：{POOL_RESOURCE}。从该池一次上传动作的抓包里取 form 的 resourceId，"
            f"或直接传 resource_id= 覆盖。")
    return rid


# --------------------------------------------------------------------------- #
# 读：池子 / 规则
# --------------------------------------------------------------------------- #
def list_pools(activity_id: str = ACTIVITY_ID) -> dict:
    """列出该活动全部收品池 + **容量与已报数**（`mac/area/page` 合并 `area/batquery/resource/ratio4Node`）。

    `applyResourceRatio` = "已报/容量"，是判落地最省事的口径（实证 251/2263 与回执完全一致）。
    每个池还带 `handPriceRuleSetting` 等规则文案——但**以 `room_rules()` 的 9 折为准**，
    那段「前15天最低成交价」是大促模板套话。"""
    d = _mac_post("/area/page", {"activityId": str(activity_id), "page": 1, "pageSize": 50,
                                 "id": "", "applyValidStatus": ""})
    items = ((d or {}).get("data") or {}).get("items") or []
    if not items:
        raise BlacklightError(f"area/page 没返回收品池：{str(d)[:200]}")
    ids = [str(i.get("id")) for i in items]
    ratio = {}
    try:
        r = _mac_post("/area/batquery/resource/ratio4Node",
                      {"type": 0, "areaIds": ",".join(ids), "activityId": str(activity_id)})
        for x in (r or {}).get("data") or []:
            ratio[str(x.get("areaId"))] = x.get("applyResourceRatio")
    except Exception:
        pass
    pools = []
    for it in items:
        aid = int(it.get("id"))
        pools.append({"areaId": aid, "name": it.get("name"),
                      "已报/容量": ratio.get(str(aid)),
                      "resourceId": POOL_RESOURCE.get(aid),
                      "templateName": it.get("templateName"),
                      "checkInApply": it.get("checkInApply"),
                      "createPin": it.get("createPin")})
    return {"activityId": activity_id, "pools": pools,
            "note": "resourceId 为 None 的池需先抓包补 POOL_RESOURCE 才能上传"}


def room_rules(activity_id: str = ACTIVITY_ID) -> dict:
    """取活动的**真实价格门槛**（`actcenter/room/getDetailByRoomId`）。

    实证返回 `profit="顺手买专享价9折及以下"` / `benefitsIntroduction="顺手买专享价要低于京东前台价9折"`
    —— 这才是硬门槛，别信池配置里那段大促模板话术。"""
    cookie = jd_auth.ensure_session()
    with _client() as c:
        r = c.get(f"{ACTCENTER}/room/getDetailByRoomId?activityId={activity_id}",
                  headers={"Cookie": cookie, "Origin": "https://yx.jd.com",
                           "Referer": "https://yx.jd.com/"})
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"room/getDetailByRoomId 未返回 JSON（HTTP {r.status_code}）") from e
    d = (j or {}).get("data") or {}
    return {"roomName": d.get("roomName"), "价格门槛": d.get("benefitsIntroduction") or d.get("profit"),
            "description": d.get("description"), "leader": d.get("leader"),
            "startTime": d.get("startTime"), "endTime": d.get("endTime")}


# --------------------------------------------------------------------------- #
# 定价
# --------------------------------------------------------------------------- #
def plan_prices(sku_ids, floor: float = -0.5, disc: float = 0.7,
                listed_prices: dict = None, coupon_self: dict = None,
                platform_max_ratio: float = 0.9) -> dict:
    """按**成本底料**算建议专享价（三个候选取最低，底线兜住）。

        成本   = 采购价 + 物流 + CPS          ← 走 osw.margin.query_pricing_batch
        底线价 = 成本 + floor                 （floor 为负，如 -0.5 = 每单最多亏 5 毛）
        目标价 = min(前台价×disc, 表格价×0.95, 前台价−单均自担券−0.1, 前台价×platform_max_ratio)
        建议价 = max(目标价, 底线价)

    ⚠️**底料必须用 osw 成本，不能拿看板的 `per_ord_loss` 倒算**——后者是成交后的结果值、
      已含现有让利，倒算等于重复扣减，会把跑量款判成「该涨价」（实测把 21 单/日、
      现价 ¥2.90 的款判成该涨到 ¥4.87）。见 memory `pricing-basis-not-result-metric`。
    ⚠️**成本要剔 `advCost`**：osw 的广告成本是 24h 快照，分母小会炸（实测把前台 ¥7.50 的
      搬家袋算成全成本 ¥42.54）。剔掉后「成本倒挂」从 60 个降到 5 个。

    `listed_prices` {sku: 普惠页面价}、`coupon_self` {sku: 单均自担券}（可选，来自券归因）。
    返回 {rows, skipped, summary}；`状态` 非「正常」的行不要提交。"""
    from blacklight.osw import margin as _osw
    ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not ids:
        raise BlacklightError("plan_prices 需要至少一个 skuId")
    pr = {}
    for i in range(0, len(ids), 100):
        pr.update(_osw.query_pricing_batch(ids[i:i + 100]))
    listed_prices = {str(k): float(v) for k, v in (listed_prices or {}).items()}
    coupon_self = {str(k): float(v) for k, v in (coupon_self or {}).items()}
    rows, skipped = [], []
    for s in ids:
        p = pr.get(s)
        if not p:
            skipped.append({"skuId": s, "状态": "⚠无成本底料-人工"})
            continue
        bench = float(p.get("benchPrice") or 0)
        cost = round(float(p.get("purchasePrice") or 0) + float(p.get("shippingCost") or 0)
                     + float(p.get("cpsCost") or 0), 2)          # ★剔 advCost
        fl = round(cost + floor, 2)
        listed = listed_prices.get(s)
        if bench <= 0 or bench - fl <= 0:
            skipped.append({"skuId": s, "状态": "⚠成本倒挂-不可报",
                            "前台价": bench, "成本": cost, "底线价": fl})
            continue
        cands = [(round(bench * disc, 2), "折扣线%.0f折" % (disc * 10)),
                 (round(bench * platform_max_ratio, 2), "平台门槛%.0f折" % (platform_max_ratio * 10))]
        if listed:
            cands.append((round(listed * 0.95, 2), "表格价95折"))
        cs = coupon_self.get(s)
        if cs and cs > 0:
            cands.append((round(bench - cs - 0.1, 2), "券后价下方0.1(自担券¥%.2f)" % cs))
        tgt, why = min(cands, key=lambda x: x[0])
        sug = round(max(tgt, fl), 2)
        st = "正常"
        if listed and cost >= listed:
            st = "⚠成本高于表格价-人工"
        elif listed and sug >= listed:
            st = "⚠无有效降幅-底线卡住"
        if abs(sug - fl) < 0.005:
            why += " ★触底线"
        rows.append({"skuId": s, "前台价": bench, "成本": cost, "底线价": fl,
                     "表格价": listed, "建议专享价": sug,
                     "折扣率": round(sug / bench, 3), "专享价毛利": round(sug - cost, 3),
                     "定价依据": why, "状态": st})
    ok = [r for r in rows if r["状态"] == "正常"]
    return {"rows": rows, "skipped": skipped,
            "summary": {"入参": len(ids), "可报": len(ok), "需人工": len(rows) - len(ok) + len(skipped),
                        "毛利中位": (sorted(r["专享价毛利"] for r in ok)[len(ok) // 2] if ok else None),
                        "毛利最低": (min((r["专享价毛利"] for r in ok), default=None)),
                        "达8折线": sum(1 for r in ok if r["折扣率"] <= 0.8)}}


# --------------------------------------------------------------------------- #
# 写：报名（dry-run + confirm）
# --------------------------------------------------------------------------- #
def _norm_rows(rows: list) -> list:
    out = []
    for r in rows:
        sku = str(r.get("skuId") or r.get("sku") or "").strip()
        price = r.get("price", r.get("promoPrice", r.get("建议专享价")))
        if not sku or price in (None, ""):
            raise BlacklightError(f"报名行缺 skuId/price：{r}")
        out.append({"skuId": sku, "price": float(price),
                    "stock": int(r.get("stock", DEFAULT_STOCK)),
                    "limitQty": int(r.get("limitQty", DEFAULT_LIMIT_QTY)),
                    "limitOrd": int(r.get("limitOrd", DEFAULT_LIMIT_ORD))})
    return out


def build_apply_xlsx(rows: list, out_path: str = None) -> dict:
    """生成 6 列报名 xlsx。列名逐字取自平台模板（改一个字回执就报「Excel读取数据异常」）。"""
    try:
        import openpyxl
    except ImportError as e:
        raise BlacklightError("缺少依赖 openpyxl，请先 pip install openpyxl") from e
    rows = _norm_rows(rows)
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "sheet1"
    ws.append(EXCEL_HEADERS)
    for r in rows:
        ws.append([int(r["skuId"]), r["price"], r["stock"], r["limitQty"], r["limitOrd"], None])
    out_path = out_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ssm_apply.xlsx")
    wb.save(out_path)
    return {"path": out_path, "rows": len(rows)}


def _token(area_id, rows) -> str:
    return _confirm_token({"path": "/common/fileProcess", "scene": SCENE, "area": str(area_id),
                           "rows": sorted("%s@%s" % (r["skuId"], r["price"]) for r in rows)})


def apply_dryrun(area_id: int, rows: list, resource_id=None) -> dict:
    """报名 DRY-RUN：校验行 + 生成 xlsx（不上传）+ 回显 form 与 confirm_token。"""
    n = _norm_rows(rows)
    rid = _resource_id(area_id, resource_id)
    built = build_apply_xlsx(n)
    return {"would_upload": False, "areaId": int(area_id), "resourceId": rid,
            "rows": built["rows"], "xlsx": built["path"],
            "form": {"sceneCode": SCENE, "resourceId": str(rid), "resourceType": str(RESOURCE_TYPE)},
            "价格门槛提醒": "平台只要求低于前台价9折；报更深是业务选择。资质门槛(好评率≥88%)改价救不了。",
            "note": "DRY-RUN：已生成 xlsx 未上传。相同参数 + confirm=confirm_token 调 ssm_apply。",
            "confirm_token": _token(area_id, n)}


def _await(rid: int, task_id, timeout: int = 180, interval: int = 5) -> dict:
    """轮询 fileProcess/page 到终态。processStatus: 1=完成 / 2=全失败。"""
    deadline = _time.time() + timeout
    tid, last = str(task_id), None
    while _time.time() < deadline:
        items = ((file_process_page(None, resource_id=rid) or {}).get("data") or {}).get("items") or []
        me = next((t for t in items if str(t.get("id")) == tid), None)
        if me:
            last = me
            if me.get("processStatus") in (1, 2):
                return {"done": True, "processStatus": me.get("processStatus"),
                        "errMsg": me.get("errMsg"),
                        "success": me.get("successCount") or 0, "fail": me.get("failCount") or 0,
                        "failUrl": me.get("failUrl") or "", "successUrl": me.get("successUrl") or ""}
        _time.sleep(interval)
    return {"done": False, "note": f"轮询{timeout}s未到终态，稍后 ssm_apply_result 查", "last": last}


@audited("ssm", "apply")
def apply(area_id: int, rows: list, confirm: str = "", resource_id=None,
          wait: bool = True, wait_timeout: int = 180, parse_fail: bool = True) -> dict:
    """**报名真执行**：生成 xlsx → 上传 `mac/common/fileProcess` → 轮询回执 → 解析失败明细。

    需相同参数先 `apply_dryrun` 拿 confirm_token。
    ⚠️**文件上传闸是跨频道共享的**（与国补/直降共用，约 90~120 秒一个文件），
      已接 `core.policy` 的 `upload.file` 闸与退避；`taskId is None` + `success=False`
      ⇒ 一条都没落地，可原样干净重试。
    ⚠️重复提交同一批会逐行报「在该场次下已报名」——那是判重不是失败，可当上一批的落地回读。"""
    n = _norm_rows(rows)
    rid = _resource_id(area_id, resource_id)
    if confirm != _token(area_id, n):
        raise BlacklightError("报名真执行需二次确认：先用相同参数跑 ssm_apply_dryrun 拿 confirm_token 再带 confirm。")
    built = build_apply_xlsx(n)
    cookie = jd_auth.ensure_session()
    files = {"file": (os.path.basename(built["path"]),
                      open(built["path"], "rb").read(), XLSX_MIME)}
    form = {"sceneCode": SCENE, "resourceId": str(rid), "resourceType": str(RESOURCE_TYPE)}
    trace: list = []
    t0 = _time.monotonic()
    j = retry_throttled(lambda: post_multipart(MAC, "/common/fileProcess", form, files, cookie),
                        "upload.file",
                        is_bad=lambda r: (r or {}).get("message") if not (r or {}).get("success") else None,
                        trace=trace) or {}
    out = {"executed": True, "areaId": int(area_id), "resourceId": rid,
           "rows": built["rows"], "success": bool(j.get("success") is True),
           "taskId": j.get("data"), "response": j,
           "耗时s": round(_time.monotonic() - t0, 1), "限速重试轨迹": trace or None}
    if not out["success"] and out["taskId"] is None:
        out["⚠撞上传闸"] = "taskId 为空 ⇒ 一条都没落地，可原样干净重试（别当部分失败去补报）"
        return out
    if wait and out["taskId"]:
        out["result"] = _await(rid, out["taskId"], timeout=wait_timeout)
        if parse_fail and (out["result"].get("failUrl")):
            out["失败分类"] = parse_fail_file(out["result"]["failUrl"])
    return out


def file_process_page(area_id=None, page: int = 1, page_size: int = 10, resource_id=None) -> dict:
    """查上传任务列表（POST `/common/fileProcess/page`）。area_id 与 resource_id 给其一。"""
    rid = int(resource_id) if resource_id else _resource_id(area_id)
    return _mac_post("/common/fileProcess/page",
                     {"sceneCode": SCENE, "resourceId": str(rid),
                      "resourceType": str(RESOURCE_TYPE), "page": page, "pageSize": page_size})


def parse_fail_file(url: str, top: int = 8) -> dict:
    """下载 failUrl 的 xlsx，把最后一列的失败原因**归类**（把数字抹成 N 后聚合）。

    实证四类：`好评率要求为N%以上` / `价格星级在N以上` / `在该场次下已报名`（判重，非失败） / 其它。"""
    import re
    try:
        import openpyxl, httpx, io
    except ImportError as e:
        raise BlacklightError("缺少依赖 openpyxl/httpx") from e
    r = httpx.get(url, timeout=60)
    r.raise_for_status()
    ws = openpyxl.load_workbook(io.BytesIO(r.content), data_only=True).worksheets[0]
    buckets, samples = {}, {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        msg = str(row[-1] or "").strip()
        key = re.sub(r"\d+\.?\d*", "N", msg)[:46] or "(无原因)"
        buckets[key] = buckets.get(key, 0) + 1
        samples.setdefault(key, []).append(str(row[0]))
    dup = sum(v for k, v in buckets.items() if "已报名" in k)
    return {"总行": sum(buckets.values()),
            "分类": dict(sorted(buckets.items(), key=lambda x: -x[1])[:top]),
            "样例SKU": {k: v[:5] for k, v in list(samples.items())[:top]},
            "其中判重(已报名)": dup,
            "提示": "「已报名」是判重不是失败，反证上一批已落地；「好评率/价格星级」是资质门槛，改价救不了。"}


def apply_result(area_id=None, resource_id=None, limit: int = 10, parse_fail: bool = True) -> dict:
    """查最近若干次上传的回执（含成功/失败数），并自动解析最新一次的失败明细。"""
    d = file_process_page(area_id, page=1, page_size=limit, resource_id=resource_id)
    items = ((d or {}).get("data") or {}).get("items") or []
    tasks = [{"id": t.get("id"), "fileName": t.get("fileName"), "created": t.get("created"),
              "processStatus": t.get("processStatus"), "errMsg": t.get("errMsg"),
              "success": t.get("successCount"), "fail": t.get("failCount"),
              "failUrl": t.get("failUrl"), "successUrl": t.get("successUrl")} for t in items]
    out = {"tasks": tasks}
    if parse_fail and tasks and tasks[0].get("failUrl"):
        out["最近一次失败分类"] = parse_fail_file(tasks[0]["failUrl"])
    return out
