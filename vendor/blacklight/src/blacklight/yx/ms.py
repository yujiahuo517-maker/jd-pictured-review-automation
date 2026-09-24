"""
yx-mcp 场域：ms = **单品秒杀**（网关 `oac.jd.com` openness）。

同一 oac.jd.com openness 系统覆盖三频道：**秒杀 / 便宜包邮 / 特价**（端点全同，差活动/area/结构参数）。
要点（扒扩展 + 浏览器抓包实证）：
- 鉴权：**yx 登录态即可**（yx cookie 授权 oac.jd.com，第 5 个网关），**不签名**，POST JSON + cookie。
- 模式：京东自营(applySkuType=2) / **京喜自营(applySkuType=3)**，默认京喜自营。
- 能力：查已报名/可报/可报价、定价(二分法/建议价门槛)、**逐SKU报名(saveApply)** + **表格报名(apply/excel，channel: seckill/baoyou/tejia)**、退出(quit=**GET**，单+批量)、导出可提报清单(export→.xlsx/.zip)、提交进度(querySubmitList)。
- 表格报名 formItemId 走 formset/area/detail 按 **code** 动态取（每活动可能变、code 稳定）；秒杀表格真报+退出已活体验证、便宜包邮用户手动真报过。
- `reduce/price` 是上场前降价工具、非报名接口（用户纠正），本模块不做。
"""
from __future__ import annotations

import io
import json as _json
import os
import re
import time as _time
from typing import Optional

from blacklight.core import auth as jd_auth, canon_num, canon_for_token, paged_scan
from blacklight.core import paths as _paths
from blacklight.core import policy
from blacklight.core import (BlacklightError, confirm_token as _confirm_token, make_client, bare_client,
                     post_multipart, json_post, audited, gateway, scene_cfg, pmap, pmap_batch)

OAC_BASE = gateway("ms")
APPLY_SKU_TYPE_JX = scene_cfg("ms").get("default_apply_sku_type", "3")   # 京喜自营(京客自营)；京东自营=2

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HERE = os.path.dirname(os.path.abspath(__file__))


def _export_dir() -> str:
    """导出产物落 `runtime/exports/`，**不落包源码目录**。
    2026-08-06：旧默认 out_path 写在 `_HERE`(= src/blacklight/yx/)，已在包里堆了 3 个 `_export_*.xlsx`
    ——业务数据混进代码目录，既会被误提交也躲不开 gitignore 讨论。产物可再生 ⇒ 归 runtime。"""
    d = os.path.join(_paths.home(), "exports")
    os.makedirs(d, exist_ok=True)
    return d
_APPLY_SHEET = "报名商品信息（请勿修改sheet名称）"   # 表格报名模板里真正填的 sheet（勿改名）


def _client(timeout: float = 20.0):
    return jd_auth.session_client(timeout)


# ★选品索引是**异步重建**的：`pagingQuerySkuList4Http` 会间歇返回
#   「查询标签选品可报SKU列表拼命加载中，请稍后再试」——**同一批 batch/rule 下
#   连发 6 页实测挂 2 页**（2026-08-11）。这不是参数错、不是没权限、也不是限流，
#   重试即好。不重试的后果是 `plan_seckill_enroll` 翻页翻到一半整个抛掉，
#   前面几页的并发试算全白跑（这次就损失了一轮）。
#   ⚠️只对这一条**瞬时**文案重试；「收品池ID无效」(参数错，实测是把
#   batch_id/area_id 传反了)、「当前时间不支持报名」(日期没到 T+3) 都必须立刻抛。
_TRANSIENT_MS = "拼命加载中"


#   重试次数实测：3 次 → 8 页里仍挂 1 页；5 次 + 线性退避（2/4/6/8s，最坏 20s/页）
#   → 8/8 通过。翻页几十页的 `plan_seckill_enroll` 对单页失败零容忍（整轮抛），
#   所以宁可慢也要重试够。
def _post(client, path: str, body: dict = None, retries: int = 5,
          backoff: float = 2.0) -> dict:
    """★2026-08-24 收编到 `core.policy.retry_throttled`（`policy.py` 文档点名"四处各写一遍"
    里唯一没收的那处）。**退避曲线不变**（backoff×attempt = 2/4/6/8s，与原实现逐位一致），
    换来的是：节流文案统一登记在 THROTTLE_RULES、撞节流时**不再静默退避**（会打印）。
    `backoff` 参数保留只为兼容旧调用方；真正生效的是规则表里的 ms.read。"""
    return policy.retry_throttled(lambda: json_post(client, OAC_BASE, path, body),
                                  "ms.read", rounds=max(1, retries))


# ---------- 已报名列表 ----------
def _parse_applied(it: dict) -> dict:
    desc = it.get("reducePriceWarningCMSDescription") or ""
    m = re.search(r"降低至\s*([\d.]+)", desc)   # 到手价上限藏在预警文案里
    cms = it.get("reducePriceWarningCMSPrice")
    return {
        "applyId": str(it.get("id") or ""),   # id = 报名ID（退出 / reduce 用）
        "skuId": str(it.get("skuId") or ""),
        "skuName": it.get("skuName") or it.get("wareName") or it.get("title"),
        "promoPrice": it.get("promoPrice"),          # 当前促销价
        "purchasePrice": it.get("purchasePrice"),    # 采购价（二分法定价用）
        "maxActualPrice": float(m.group(1)) if m else None,  # 到手价上限（二分法目标）
        "batchId": it.get("batchId"), "promoId": it.get("promoId"),
        "beginTime": it.get("beginTimeStr") or it.get("beginTime"),  # 场次开始时间(区分哪天的场次，无需查batchId)
        "endTime": it.get("endTimeStr") or it.get("endTime"),
        "applyStatus": it.get("applyStatus"),
        "wareId": it.get("wareId"), "currentStatus": it.get("currentStatus"),
        # —— 失效预警三件套（2026-08-11 抓包补齐，见 invalidation_warnings）——
        "erpPin": it.get("erpPin"),                  # ★归属：本接口**不按人筛**，必须自己过滤
        "warnPrice": float(cms) if cms not in (None, "") else None,   # ★权威目标价
        "sameLowPrice": it.get("sameLowPrice"),      # 同款低价
        "priceWarning": it.get("priceWarning"),      # ⚠️不是目标价，见 docstring
        "warnAt": it.get("reducePriceWarningCMSDatetime"),
        "warnStatus": it.get("reducePriceWarningCMSStatus"),   # '1' = 预警生效中
        "warnDesc": desc or None,
        "cmsExposureStatus": it.get("cmsExposureStatus"),
        "cmsNoExposureReasonCode": it.get("cmsNoExposureReasonCode"),
    }


# currentStatus 码值（2026-08-11 逐个点击抓包 + 用 beginTime/endTime 反证，不靠点击顺序对齐）
APPLIED_STATUS = {
    "": "全部", "0": "活动审核中", "1": "促销待审核", "301": "素材驳回",
    "2": "待开始", "3": "进行中", "6": "活动失效预警", "4": "活动期间失效", "5": "已结束",
}


def get_applied(activity_id: str, area_id: int, apply_sku_type: str = APPLY_SKU_TYPE_JX,
                current_status: str = "", page: int = 1, page_size: int = 100,
                sku_id: str | int = "", begin_time: str = None, end_time: str = None) -> dict:
    """已报名秒杀商品（POST /apply/openness/applied/page）。
    current_status: ''全部 / '6'活动失效预警(需降价) 等。
    **sku_id**：传了**多数池**走服务端 skuId 过滤（实证 9.7万→百级），查单SKU别再全量翻页。
    ⚠️**但不是所有池都支持**：实证 areaId `40375202`（特价×微信域池）**完全忽略 sku_id**，
    totalCount 仍返回全池 5723、700 条里只有 1 条命中 → 按单 SKU 查会得到**假的「未落地」**。
    **验证报名落地一律全池拉取 + 本地比对**，别信单 SKU 查询的空结果。
    返回 items(applyId/skuId/beginTime场次时间/当前价/采购价/到手价上限)。一个SKU可能跨多场次多条。

    ⚠️★**本接口没有 batch/场次维度，别拿它做报名前去重**（2026-08-04 血的教训）：
      它返回该活动**所有场次**的报名，而平台判重是**按场次**的。用活动级已报名去重会严重过滤——
      实测 893 个候选里被误判 720 个(81%)已报，平台实际只拒了 119 个(13%)，**差额约 600 个 SKU**。
      ★**正解：别预判，全量提交让平台判重**——失败明细会逐条列出"已经报过名"，平台才是权威，
      而且免费。翻 1157 页去猜既慢又错。一个 SKU 常有 17~42 条跨场次记录（实测最早可追到两个多月前）。"""
    if not str(activity_id).strip():
        raise BlacklightError("需要 activity_id（秒杀报名页 URL 的 activityId）")
    body = {"activityId": str(activity_id), "areaId": int(area_id),
            "applySkuType": str(apply_sku_type), "currentStatus": str(current_status),
            "page": page, "pageSize": page_size}
    if str(sku_id).strip():
        body["skuId"] = str(sku_id).strip()   # 服务端过滤，避免全量翻页
    # ★★按**场次开始时间**筛（2026-08-24 探出，含阴性对照）：137716 → 6492(30天) → 713(单场次)。
    #   这是让「已报名导出」能用的唯一手段（平台 50000 行上限）。别用 `startTime`，语义不同。
    if begin_time:
        body["beginTime"] = str(begin_time)
    if end_time:
        body["endTime"] = str(end_time)
    with _client() as client:
        d = _post(client, "/apply/openness/applied/page", body)
    return {"totalCount": d.get("totalCount"), "totalPage": d.get("totalPage"),
            "items": [_parse_applied(it) for it in (d.get("items") or [])]}


def invalidation_warnings(activity_id: str, area_id: int, sales_erps: str = None,
                          apply_sku_type: str = APPLY_SKU_TYPE_JX,
                          page_size: int = 100, max_pages: int = 50,
                          with_margin: bool = True, min_margin: float = 0.02,
                          adv_source: str = "auto") -> dict:
    """**秒杀活动失效预警**（已报名管理 → 「活动失效预警」标签 = `currentStatus='6'`）。

    报上名 ≠ 活动会生效。平台在**场次开始前**扫一遍价格，不达标的挂预警；
    不处理就到点失效——报了等于没报，而且占着坑。

    ## ★★预警有**两类**，`purchasePrice` 的语义相反（2026-08-12 撞到 B 类才发现）
    | 类型 | 文案特征 | 规则 | `purchasePrice` 是 |
    |---|---|---|---|
    | **A 活动后价差** | 「结束后24小时」 | 秒杀到手价 ≤ **活动后**到手价 − 0.01 | **当前**预估到手价 |
    | **B 活动期间超标** | 「活动期间内/预期最高」 | 活动期间实际到手价 ≤ **提报**到手价 | **提报**到手价（= `warnPrice`）|

    ⇒ B 类若照 A 类算 `需降幅 = purchasePrice − warnPrice` **恒等于 0**，看着"无需处理"，
    实际差 5 元（实证 `10135699084006`：提报 13.98，活动期间预期最高 **18.98**）。
    真正超标的价**只在文案里**（`预期最高单件普惠到手价:X`），本函数按类型分别取，并标 `类型`。
    2026-08-11 那 39 条**碰巧全是 A 类**，所以没暴露。

    A 类实证：31/39 就差 0.01；另 8 条按品类价差折扣（`cateDiscount`，实测 0.99）算，缺口更大。

    ## ★两个陷阱（都实测撞过，别凭字段名用）
    1. **`priceWarning` 不是要降到的价**。实测 39 条里它**没有一条**等于目标价
       （`priceWarning ≈ 当前到手价 ÷ cateDiscount`，是个参考上限）。
       照它算会得出「还差 −0.20，没问题」，实际是**超了 0.01 必须降**。
       ★权威字段是 **`reducePriceWarningCMSPrice`**（本函数的 `需降至`），
       与预警文案里的「降低至 X」**39/39 完全一致**，两者不符时本函数会标 `阈值存疑`。
    2. **本接口不按归属筛**。实测 39 条里混着 `huwenjie50` 4 条、`lvyichen1` 2 条。
       不传 `sales_erps` 就会把别人的品算进你的处置清单——同 `plan_seckill_enroll`
       的老问题（铁律 8）。传了才筛，没传返回全量并在 `口径` 里标出来。

    ## 处置
    缺口通常只有 0.01 元/件，降价成本极低；**真正的代价是不处理 = 整场白报**。
    两条路都能满足规则，方向相反，别只想着降价：
      · 降秒杀到手价到 `需降至`（本函数给的 `需降幅`）
      · 或**抬高活动后的到手价**（摘掉活动结束后仍在生效的券促）——见 `markettool.plan_strip_except`
    """
    st = "6"
    rows, page, tp = [], 1, 1
    while page <= max_pages:
        d = get_applied(activity_id=activity_id, area_id=area_id,
                        apply_sku_type=apply_sku_type, current_status=st,
                        page=page, page_size=page_size)
        rows += d.get("items") or []
        tp = int(d.get("totalPage") or 1)
        if page >= tp or not (d.get("items") or []):
            break
        page += 1
    else:
        # 到顶不许静默 break（2026-08-24 审查）：原实现跑满 max_pages 就跳出，
        # 既不校验 totalPage 也不报错，预警超 max_pages*page_size 行时会悄悄漏掉尾部，
        # 而本函数的输出直接喂 reduce_price（真改价）=> 漏行就是漏处理，还看不出来。
        # 纪律同 core/paging.check_truncation：宁可报错，不可静默少算。
        if tp > max_pages:
            raise BlacklightError(
                "失效预警只取到 %d 页(每页 %d)，平台共 %d 页，被 max_pages 截断。"
                "本函数输出会喂给 reduce_price 改价，漏行=漏处理，故报错不返回半份。"
                "调大 max_pages，或按场次/ERP 收窄后再取。" % (max_pages, page_size, tp))
    total_all = len(rows)
    owners = sorted({r.get("erpPin") for r in rows if r.get("erpPin")})
    if sales_erps:
        keep = {e.strip() for e in str(sales_erps).split(",") if e.strip()}
        rows = [r for r in rows if (r.get("erpPin") or "") in keep]

    out, suspect = [], 0
    for r in rows:
        desc = r.get("warnDesc") or ""
        need = r.get("warnPrice")               # ★权威目标价（两类都适用）
        txt = r.get("maxActualPrice")           # 文案抽出的阈值，用作对账
        # ★★两类预警的 purchasePrice 语义不同（2026-08-12 撞到 B 类才发现）
        if "活动期间" in desc or "预期最高" in desc:
            kind = "B_活动期间超标"
            # B 类：purchasePrice 是**提报到手价**(= need)，真正超标的价只在文案里
            m = re.search(r"预期最高单件普惠到手价[:：]\s*([\d.]+)", desc)
            now = float(m.group(1)) if m else _f(r.get("promoPrice"))
            # ★reduce/price 要求填的到手价**严格小于**原始到手价(=purchasePrice 字段)。
            #   B 类的 warnPrice 恰好等于它 ⇒ 原样提交必被拒
            #   「填写的到手价大于等于原始到手价」，得再让 0.01。
            orig = _f(r.get("purchasePrice"))
            if need is not None and orig is not None and need >= orig:
                need = round(orig - 0.01, 2)
        else:
            kind = "A_活动后价差"
            now = _f(r.get("purchasePrice"))    # A 类：这就是当前预估到手价
        bad = (need is not None and txt is not None and abs(need - txt) > 0.005)
        if bad:
            suspect += 1
        out.append({
            "skuId": r["skuId"], "skuName": r.get("skuName"), "erpPin": r.get("erpPin"),
            "类型": kind,
            "applyId": r["applyId"], "batchId": r.get("batchId"), "promoId": r.get("promoId"),
            "场次": r.get("beginTime"), "促销价": _f(r.get("promoPrice")),
            "当前到手价": now, "需降至": need,
            "需降幅": (round(now - need, 2) if (now is not None and need is not None) else None),
            "阈值存疑": bad, "预警时间": r.get("warnAt"), "文案": desc or None,
        })
    # 降下去还赚不赚？缺口多数只有 0.01（无所谓），但实测有 0.78 的（−5.2%），必须算。
    buckets = {"照降": [], "降了会亏": [], "取数失败": []}
    if with_margin and out:
        try:
            from blacklight.osw import margin as _mt
            cost = _mt.query_pricing_batch(sorted({x["skuId"] for x in out}),
                                           adv_source=adv_source)
        except Exception as e:                      # 取数失败要说出来，不静默当成"能降"
            cost = {}
            buckets["取数失败"] = ["query_pricing_batch 失败: %s" % str(e)[:80]]
        for x in out:
            p = cost.get(x["skuId"]) or {}
            fc = _f(p.get("fixedCost"))             # 秒杀口径：采购+物流+广告，不含 CPS
            need = x["需降至"]
            if fc is None or not need:
                x["降后毛利%"] = None
                x["处置"] = "取数失败"
                buckets["取数失败"].append(x["skuId"])
                continue
            m = round((need - fc) / need * 100, 1)
            x["降后毛利%"] = m
            x["处置"] = "照降" if m >= min_margin * 100 else "降了会亏"
            buckets[x["处置"]].append(x["skuId"])
    out.sort(key=lambda x: -(x["需降幅"] or 0))
    return {
        "rows": out, "count": len(out), "全量(未筛人)": total_all, "涉及ERP": owners,
        "阈值存疑": suspect,
        "分桶": {k: len(v) for k, v in buckets.items()} if with_margin else None,
        "降了会亏": buckets["降了会亏"],
        "口径": ("已按 sales_erps=%s 筛归属" % sales_erps) if sales_erps
                 else "★未按人筛（本接口混多采销，实测混入 %d 个 ERP）" % len(owners),
        "_note": "需降至=reducePriceWarningCMSPrice（权威）；别用 priceWarning。不处理=场次开始即失效。",
    }


def solve_warned_price(sku_id, area_id: int, batch_id: int, cap: float,
                       cur_promo: float, duration: int = 28, steps: int = 14) -> dict:
    """解「到手价 ≤ cap 的**最高**促销价」——全程 `couponInfo` 平台真值二分，**不做线性外推**。

    ★**别用 `−0.01` 直觉**：到手价对促销价的斜率**不是 1**（券促多为折扣型）。
    实证 `10115564065860`：促销价 22.90→22.89 时到手价**纹丝不动**（19.46），
    22.88 才降到 19.45。斜率约 0.8 ⇒ 想压 0.01 得降 ~0.0125。
    32 款实测多数需降 **0.02** 而非 0.01；照 0.01 提交等于白降一次（还占一次审核）。

    ★**也别用本地模型**（`solve_price`）：它算的是京喜/客户口径到手价（实测 20.40 / 17.90），
    与失效预警用的「单件普惠到手价」（19.46）**是第三个口径**，对不上。铁律 3。
    """
    def actual(p):
        d = purchase_coupon_info(sku_id, area_id, batch_id, round(float(p), 2),
                                 duration=duration)
        v = d.get("minPurchasePrice")
        return float(v) if v not in (None, "") else None

    hi = float(cur_promo)
    if actual(hi) is None:
        return {"skuId": str(sku_id), "ok": False, "reason": "couponInfo 取数失败"}
    lo = hi
    for _ in range(12):                      # 先探一个达标的下界
        lo = round(lo - max(0.05, (hi - lo) * 2 or 0.05), 2)
        if lo <= 0.05:
            return {"skuId": str(sku_id), "ok": False, "reason": "降到底也不达标"}
        a = actual(lo)
        if a is not None and a <= cap + 1e-9:
            break
    else:
        return {"skuId": str(sku_id), "ok": False, "reason": "未找到可行下界"}
    for _ in range(steps):                   # 二分求最高可行价（降得越少越好）
        if round(hi - lo, 2) <= 0.01:
            break
        mid = round((hi + lo) / 2, 2)
        a = actual(mid)
        if a is not None and a <= cap + 1e-9:
            lo = mid
        else:
            hi = mid
    return {"skuId": str(sku_id), "ok": True, "promoPrice": lo,
            "purchasePrice": actual(lo), "cap": cap, "降幅": round(float(cur_promo) - lo, 2)}


def _reduce_token(items, area_id) -> str:
    return _confirm_token({"path": "ms/reduce/price", "areaId": str(area_id),
                           "ids": sorted(str(i["parentApplyWareId"]) for i in items)})


def reduce_price_dryrun(rows: list, area_id: int) -> dict:
    """降价 dry-run：组装不发。`rows` 需含 `applyId`(=parentApplyWareId) / `promoPrice` / `purchasePrice`。"""
    items, bad = [], []
    for r in rows or []:
        aid, pp, ap = r.get("applyId"), r.get("promoPrice"), r.get("purchasePrice")
        if not (aid and pp and ap):
            bad.append({"skuId": r.get("skuId"), "缺": [k for k, v in
                        (("applyId", aid), ("promoPrice", pp), ("purchasePrice", ap)) if not v]})
            continue
        items.append({"skuId": str(r.get("skuId") or ""), "parentApplyWareId": str(aid),
                      "promoPrice": round(float(pp), 2), "purchasePrice": str(ap)})
    return {"would_apply": False, "count": len(items), "preview": items[:5], "invalid": bad,
            "confirm_token": _reduce_token(items, area_id),
            "note": "降价=创建同数量新促销并在成功后删除原促销；**需审核通过才生效**。"}


@audited("ms", "reduce_price")
def reduce_price(rows: list, area_id: int, confirm: str = "",
                 concurrency: int = 4) -> dict:
    """**发起降价**（POST /apply/openness/reduce/price）—— 秒杀失效预警的处置动作。

    平台语义（弹窗原文）：**创建一个和原促销数量相同的新促销，新促销建立成功后自动删除原促销**；
    且**「修改价格并审核通过后」才生效** —— 不是即时改价，提交完还要过审。

    载荷只有四个字段：`parentApplyWareId`（= 已报名行的 `applyId`）/ `promoPrice`（新促销价）
    / `purchasePrice`（**目标到手价**，即预警的 `需降至`）/ `areaId`。

    ⚠️**`purchasePrice` 必须严格小于该行原始的 `purchasePrice` 字段**，否则报
    「填写的到手价大于等于原始到手价」。A 类天然满足（目标 = 当前 − 0.01）；
    **B 类的目标价恰好等于原始值 ⇒ 原样提交必被拒**，`invalidation_warnings` 已自动再让 0.01。
    回执 `code=00000` + `data{applyWareId, promoteId}` + `message="跟价提报成功"`。

    ⚠️`promoPrice` 必须由 `solve_warned_price` 用 couponInfo 真值解出——见其 docstring 里
    「22.89 白降」的实证。
    """
    d = reduce_price_dryrun(rows, area_id)
    if confirm != d["confirm_token"]:
        raise BlacklightError("confirm 校验失败：先跑 reduce_price_dryrun 拿 confirm_token")
    items = []
    for r in rows or []:
        aid, pp, ap = r.get("applyId"), r.get("promoPrice"), r.get("purchasePrice")
        if aid and pp and ap:
            items.append({"skuId": str(r.get("skuId") or ""), "parentApplyWareId": str(aid),
                          "promoPrice": round(float(pp), 2), "purchasePrice": str(ap)})

    def _one(it):
        body = {"parentApplyWareId": it["parentApplyWareId"], "purchasePrice": it["purchasePrice"],
                "promoPrice": it["promoPrice"], "areaId": int(area_id)}
        try:
            with _client() as cl:
                r = _post(cl, "/apply/openness/reduce/price", body)
            return {**it, "ok": True, "applyWareId": (r or {}).get("applyWareId"),
                    "promoteId": (r or {}).get("promoteId")}
        except Exception as e:
            return {**it, "ok": False, "reason": str(e)[:160]}

    res = pmap(_one, items, workers=max(1, int(concurrency)))
    ok = [x for x in res if x.get("ok")]
    return {"executed": True, "total": len(items), "success": len(ok),
            "fail": len(res) - len(ok), "results": res,
            "_note": "★两个方向都不能只看回执：①提交成功≠生效（需审核通过）"
                     "②**回执失败≠没落地**——实测 ReadTimeout 那条其实已提交成功（超时发生在读响应）。"
                     "★★判据用 `verify_reduced(rows)`：同 SKU 同 batch 出现**新 applyId + 新促销价 + "
                     "applyStatus=2**。**不要拿失效预警桶当判据**（2026-08-24 更正）——降价是"
                     "新建促销、原促销随后才删，预警桶里挂的还是旧行，清空比落地晚半小时以上；"
                     "实测 24 款发完仍 22 款在桶里、10 分钟剩 3、半小时还是那 3，而那 3 款查已报名"
                     "是每款两行（新行 applyStatus=2 已通过、旧行 18/19 待清）⇒ **24/24 其实全落地**。"
                     "照预警桶判会报『3 款失败』并去重复降价，而重复降会再建一条促销。"}


def find_applied(activity_id: str, area_id: int, sku_id: str | int,
                 apply_sku_type: str = APPLY_SKU_TYPE_JX) -> Optional[dict]:
    """按 skuId 在已报名里找**首条**记录（取退出/降价所需 applyId + 采购价/到手价上限）。
    走服务端 skuId 过滤，不再全量翻页。**要一个SKU跨场次的全部记录 / 按日期筛，用 applied_by_skus。**

    ⚠️**找不到时会区分两种情况**（2026-08-05 修）：确认没有 → 返回 `None`；
    **没拉全（空页重试后仍拉不到 totalCount）→ 抛错**，绝不返回 `None`。
    旧写法两种都返回 `None`，调用方会把"没拉全"读成"确认没报名/退不了"——
    那是个看起来很确定的错误答案（memory `subsidy-pool-pull-truncation` 实证过）。"""
    sku_id = str(sku_id).strip()
    hit = {}

    def _fetch(page):
        d = get_applied(activity_id, area_id, apply_sku_type, page=page, page_size=100, sku_id=sku_id)
        return d["items"], d.get("totalCount")

    def _scan(items):
        for it in items:
            if it["skuId"] == sku_id:
                hit["it"] = it
                return True
        return False

    r = paged_scan(_fetch, 100, on_page=_scan)
    if hit:
        return hit["it"]
    if not r["complete"]:
        raise BlacklightError(
            f"SKU {sku_id} 未在已报名里找到，但**清单没拉全**（{r['truncated']}）——"
            f"这不等于「没报名」，别据此判定退不了。稍后重试，或用 get_applied 全池拉取本地比对。")
    return None


def _applied_via_export(activity_id: str, area_id: int, skus: list, dfx,
                        apply_sku_type: str, begin_time: str = None,
                        end_time: str = None) -> dict:
    """`applied_by_skus` 的**导出路**：一次拿全活动的已报名，本地按 skuId 过滤。

    ★★**列名按频道不同**（2026-08-24 实测，秒杀 + 5 个便宜包邮/特价池全查过）：
      · **秒杀**：`报名编号/商品信息/skuId/spuId/短标题/促销价格/秒杀到手价/价格预警/
        活动时长/审核进度/驳回原因/促销开始时间/促销状态/…` —— **没有「报名人」列**
      · **便宜包邮/特价**：`报名编号/报名人/报名SKU/商品名称/类目信息/SPU ID/促销价格/促销数量/
        促销库存/促销生效时间/促销生效状态/报名时间/审核状态` —— **有「报名人」**
      ∴ 下面一律 `新键 or 旧键` 双取，别只认一套（只认便宜包邮那套 ⇒ 秒杀命中恒 0 且不报错）。
    比翻页路强的地方（导出自带、翻页接口没有）：秒杀=**审核进度/驳回原因/价格预警/秒杀到手价/短标题**；
      便宜包邮/特价=**报名人 ERP/促销生效状态/审核状态**。
    ⚠️**状态字段口径不同**：翻页路的 `applyStatus` 是数字(2/3/9)，导出只有中文文案。
      本函数**不做数字映射**（没有实证过的对照表，硬映射就是造一个静默错值），
      而是把 `applyStatus` 置 None、另给 `auditStatusText`/`promoStatusText`，
      并在返回里带 `_warn` 明说——**宁可显式缺，不要悄悄错**。"""
    d = fetch_applied_export(area_id, activity_id, trigger=True, apply_sku_type=apply_sku_type,
                             begin_time=begin_time, end_time=end_time)
    want = set(skus)
    by_sku = {s: [] for s in skus}
    for row in d.get("rows") or []:
        sku = str(row.get("skuId") or row.get("报名SKU") or "").strip()   # ★真列名是 skuId
        if sku.endswith(".0"):          # xls 数字列读成 float → 去尾（与 _parse_export_rows 同处理）
            sku = sku[:-2]
        if sku not in want:
            continue
        begin = str(row.get("促销开始时间") or row.get("促销生效时间") or "")   # ★真列名是 促销开始时间
        if dfx and not begin.startswith(dfx):
            continue
        by_sku[sku].append({
            "applyId": str(row.get("报名编号") or ""),
            "skuId": sku,
            "skuName": row.get("商品信息") or row.get("商品名称"),
            "promoPrice": row.get("促销价格"),
            "beginTime": begin or None,
            "applyStatus": None,                       # ★导出无数字态，别读它
            "auditStatusText": row.get("审核进度") or row.get("审核状态"),        # ★导出独有
            "promoStatusText": row.get("促销状态") or row.get("促销生效状态"),    # ★导出独有
            "rejectReason": row.get("驳回原因"),          # ★导出独有（被拒原因，翻页接口没有）
            "dealPrice": row.get("秒杀到手价"),           # ★导出独有
            "priceWarning": row.get("价格预警"),          # ★导出独有（失效预警文案）
            "shortTitle": row.get("短标题"),              # ★秒杀导出独有
            "applicant": row.get("报名人"),               # ★便宜包邮/特价导出独有（秒杀没有这列）
            "spuId": row.get("spuId") or row.get("SPU ID"),
            "_via": "export",
        })
    matched = [r for s in skus for r in by_sku[s]]
    return {"by_sku": by_sku,
            "matched": matched,
            "apply_ids": [r["applyId"] for r in matched if r["applyId"]],
            "no_apply": [s for s in skus if not by_sku[s]],
            "dates": list(dfx) if dfx else "全部场次",
            "via": "export",
            "export": {"fileName": d.get("fileName"), "taskId": d.get("taskId"),
                       "全表行数": d.get("count")},
            "_warn": "导出路**没有数字 applyStatus**（恒为 None）。判状态请读 "
                     "`auditStatusText`(审核进度) / `promoStatusText`(促销状态)；被拒看 `rejectReason`。"
                     "**「报名人」只有便宜包邮/特价导出有，秒杀导出没有这列**（要按 ERP 分人先看 applicant 是不是 None）。"
                     "要数字态请显式传 via='page'。"}


def applied_by_skus(activity_id: str, area_id: int, sku_ids: list,
                    dates: list = None, apply_sku_type: str = APPLY_SKU_TYPE_JX,
                    via: str = "auto", export_threshold: int = 5) -> dict:
    """**查一批SKU在(可选:指定日期)场次的已报名记录**——「这几个SKU某几天有没有报名」一步到位。

    **两条路，按规模自动选**（`via='auto'`，2026-08-20 加）：
      · `page`   服务端 skuId 过滤 + 并发翻页。**单 SKU 一次请求，少量时最快**
      · `export` 触发「已报名导出」一次拿全活动，本地过滤。固定 ~10~30s，但**与 SKU 数无关**
      · auto：`len(skus) >= export_threshold`(默认5) 走 export，否则 page

    ★**为什么要这条分叉**（2026-08-20 实测）：page 路是 `pmap` 并发，8 个 SKU 同时各自翻页会把
      平台打出「拼命加载中」，`_post` 再退避 2/4/6/8 秒 ⇒ **顶穿 MCP 的 120s**。
      同样 8 个 SKU：整批超时两次，拆成 4+4 两次秒回。交叉点约 3~5 个。
      并发数也从 8 降到 4（写路径的并发自伤 08-19 已由 core.policy 治了，读路径此前一直没治）。

    ⚠️两条路的**状态字段口径不同**：page 给数字 `applyStatus`(2/3/9)，export 给中文
      `auditStatusText`/`promoStatusText` 且 `applyStatus` 恒 None（见 `_applied_via_export`）。
      返回里的 `via` 说明走了哪条；export 路额外带 `_warn`。**要数字态就显式 via='page'**。

    dates: ['2026-07-17','2026-07-18'] 只留这些日期的场次；None=全部场次。
    返回 {by_sku:{sku:[记录...]}, matched:[所有命中记录(含applyId,可直接喂 withdraw_batch)], apply_ids:[...], no_apply:[...], via}。"""
    skus = [str(s).strip() for s in (sku_ids or []) if str(s).strip()]
    dfx = tuple(str(d).strip() for d in dates) if dates else None
    if not skus:
        return {"by_sku": {}, "matched": [], "apply_ids": [], "no_apply": [],
                "dates": list(dfx) if dfx else "全部场次", "via": "none"}

    mode = str(via or "auto").lower()
    if mode not in ("auto", "page", "export"):
        raise BlacklightError(f"via 只能是 auto/page/export，收到 {via!r}")
    # ★★由 `dates` 推导导出时间窗（2026-08-24）：不收窄就撞平台 50000 行上限、任务必失败。
    #   实测 全活动 137716（必失败）→ 未来30天 6492 → 单场次 **713，一次就成**。
    #   没给 dates 时默认取「今天起 30 天」——回读落地关心的都是未来场次。
    # ★★2026-08-24 改：**先不带窗探量，超上限才收窄**（原来无条件套「今天起30天」）。
    #   起因：时间窗的语义**只在秒杀成立**——便宜包邮池实测**忽略** beginTime/endTime
    #   （带「今天起30天」照样返全量 3128 行、含 2~3 月的老促销）。
    #   那些池条数 175~3128 本来就远低于 50000，套窗毫无收益；
    #   而哪天某个池真认这个字段，无条件套窗就会**静默漏数据**。
    #   ⇒ 只在"不收窄就必失败"时才收窄；日期过滤照旧在本地按 dfx 做。
    _bt = _et = None
    if mode in ("auto", "export"):
        _f0 = applied_export_feasible(activity_id, area_id, apply_sku_type)
        if not _f0.get("ok"):
            if dfx:
                _bt, _et = min(dfx) + " 00:00:00", max(dfx) + " 23:59:59"
            else:
                _bt = _time.strftime("%Y-%m-%d 00:00:00")
                _et = _time.strftime("%Y-%m-%d 23:59:59",
                                     _time.localtime(_time.time() + 30 * 86400))

    _cap_note = None
    if mode == "auto":
        mode = "export" if len(skus) >= max(2, int(export_threshold)) else "page"
        if mode == "export":
            # ★先探量再决定：超 50000 的窗口导出必失败，撞它纯属白等（见 APPLIED_EXPORT_ROW_CAP）
            _f = applied_export_feasible(activity_id, area_id, apply_sku_type, _bt, _et)
            if not _f["ok"]:
                mode, _cap_note = "page", _f["why"]
    if mode == "export":
        # ★2026-08-24：export 路会**整条挂掉**——平台侧「已报名导出」任务自己失败（status=2 无文件），
        #   或撞它自己的 3 分钟限流。今天 389 款回读就是这样被打断的，只能现场手写降级。
        #   ⇒ 内建降级：export 失败**自动退到 page 路**（分块串行，别并发——见 docstring 里
        #     「8 个 SKU 并发翻页会把平台打出『拼命加载中』」）。
        #   ⚠️只在 via='auto' 时降级；显式 via='export' 说明调用方就是要这条路，照旧抛错。
        try:
            return _applied_via_export(activity_id, area_id, skus, dfx, apply_sku_type, _bt, _et)
        except BlacklightError as e:
            if str(via or "auto").lower() == "export":
                raise
            _fallback_note = "export 路失败(%s)，已自动降级 page 路分块串行" % str(e)[:80]
    else:
        _fallback_note = _cap_note

    def _one(sku):
        recs, page = [], 1
        while True:
            d = get_applied(activity_id, area_id, apply_sku_type, page=page, page_size=100, sku_id=sku)
            recs += [it for it in d["items"] if it["skuId"] == sku]
            got = (page - 1) * 100 + len(d["items"])
            if not d["items"] or got >= (d.get("totalCount") or 0):
                break
            page += 1
        if dfx:
            recs = [r for r in recs if str(r.get("beginTime") or "").startswith(dfx)]
        return sku, recs

    # ★降级进来的批量通常很大（今天 389 款），一把 pmap 会被限流打散 ⇒ 分块 + 块间留白。
    #   限流看的是**窗口内总调用数**，不是并发数（同 core.reuse.paced 那条）。
    if _fallback_note and len(skus) > 40:
        pairs, CH = [], 4
        for i in range(0, len(skus), CH):
            for attempt in (1, 2):
                try:
                    pairs += pmap(_one, skus[i:i + CH], workers=len(skus[i:i + CH]))
                    break
                except Exception:
                    if attempt == 2:
                        pairs += [(x, []) for x in skus[i:i + CH]]   # 该块查不到，记空别丢
                    else:
                        _time.sleep(4)
            _time.sleep(0.8)
    else:
        pairs = pmap(_one, skus, workers=4)   # ★8→4：8 并发会把平台打限流，见上方 docstring
    by_sku = {s: r for s, r in pairs}
    matched = [r for _s, rs in pairs for r in rs]
    out = {"by_sku": by_sku,
           "matched": matched,
           "apply_ids": [r["applyId"] for r in matched],
           "no_apply": [s for s, r in pairs if not r],
           "dates": list(dfx) if dfx else "全部场次",
           "via": "page"}
    if _fallback_note:
        out["via"] = "page(fallback)"
        out["_降级"] = _fallback_note
    return out


# ---------- 退出（quit，confirm 门） ----------
def _quit_token(apply_id, area_id) -> str:
    return _confirm_token({"path": "ms/quit", "id": str(apply_id), "areaId": str(area_id)})


def withdraw_dryrun(apply_id, area_id: int) -> dict:
    """退出 DRY-RUN（/apply/openness/quit?id=&areaId=）：不发送，返回 confirm_token。apply_id 来自 get_applied/find_applied。"""
    return {"would_withdraw": False,
            "request": {"path": f"/apply/openness/quit?id={apply_id}&areaId={area_id}"},
            "note": "DRY-RUN：未退出。真执行：相同参数 + confirm=confirm_token 调 ms_withdraw。",
            "confirm_token": _quit_token(apply_id, area_id)}


@audited("ms", "withdraw")
def withdraw(apply_id, area_id: int, confirm: str = "") -> dict:
    """**退出真执行**（quit）。需相同参数先 withdraw_dryrun 拿 confirm_token 再带 confirm。"""
    if confirm != _quit_token(apply_id, area_id):
        raise BlacklightError("退出需二次确认：先用相同参数跑 ms_withdraw_dryrun 拿 confirm_token 再带 confirm。")
    with _client() as client:
        r = client.get(f"{OAC_BASE}/apply/openness/quit?id={int(apply_id)}&areaId={int(area_id)}")  # ★GET(非POST,POST→10008请求类型错误)
        r.raise_for_status()
        j = r.json()
    if not j.get("success"):
        raise BlacklightError(f"quit: {j.get('message') or j.get('code')}")
    return {"withdrawn": True, "applyId": str(apply_id), "message": j.get("message")}


# ---------- 批量退出（openness quit 无原生批量：逐个退出，一个 confirm 门） ----------
MAX_WITHDRAW_BATCH = 50


def _quit_ids(apply_ids) -> list:
    ids = [str(a).strip() for a in apply_ids if str(a).strip()]
    if not ids:
        raise BlacklightError("withdraw_batch 需要至少一个 applyId")
    if len(ids) > MAX_WITHDRAW_BATCH:
        raise BlacklightError(f"单次批量退出 {len(ids)} 超上限 {MAX_WITHDRAW_BATCH}，请分批")
    return list(dict.fromkeys(ids))


def _quit_batch_token(ids, area_id) -> str:
    return _confirm_token({"path": "ms/quit#batch", "areaId": str(area_id), "ids": sorted(ids)})


def withdraw_batch_dryrun(apply_ids: list, area_id: int) -> dict:
    """**批量退出 DRY-RUN**（便宜包邮/秒杀/特价；逐个 /apply/openness/quit，不发送）。apply_ids 来自 get_applied/find_applied。"""
    ids = _quit_ids(apply_ids)
    return {"would_withdraw": False, "count": len(ids),
            "requests": [{"path": f"/apply/openness/quit?id={i}&areaId={area_id}"} for i in ids],
            "note": "DRY-RUN：将对每个 applyId 逐个退出，未发送。真执行：相同 applyId 列表 + confirm 调 ms_withdraw_batch。",
            "confirm_token": _quit_batch_token(ids, area_id)}


@audited("ms", "withdraw_batch")
def withdraw_batch(apply_ids: list, area_id: int, confirm: str = "") -> dict:
    """**批量退出真执行**：逐个 openness/quit。需相同 applyId 列表先 withdraw_batch_dryrun 拿 confirm_token 再带 confirm。逐条回报成败。"""
    ids = _quit_ids(apply_ids)
    if confirm != _quit_batch_token(ids, area_id):
        raise BlacklightError("批量退出需二次确认：先用相同 applyId 列表跑 ms_withdraw_batch_dryrun 拿 confirm_token 再带 confirm。")
    results = []
    for aid in ids:
        try:
            with _client() as client:
                r = client.get(f"{OAC_BASE}/apply/openness/quit?id={int(aid)}&areaId={int(area_id)}")  # ★GET
                r.raise_for_status()
                j = r.json()
            ok = bool(j.get("success"))
            results.append({"applyId": aid, "success": ok, "message": j.get("message")})
        except Exception as e:
            results.append({"applyId": aid, "success": False, "message": str(e)})
    return {"executed": True, "count": len(ids), "confirm_token": confirm,
            "all_success": all(x["success"] for x in results), "results": results}


# ---------- 报名（saveApply，openness；真金白银，dry-run + confirm 门） ----------
# 便宜包邮-前置池(batchId 6615177) 的 formItemId（每活动固定，实证）+ applyExtendInfo。
BAOYOU_FORM = {"duration": "179325937", "skuId": "179325932",
               "promoPrice": "179325935", "stock": "179325936", "whiteImg": "1710824301"}
BAOYOU_APPLY_EXT = {"cpsSubsidy": 1, "operateType": 3, "applySkuType": "3",
                    "applyDoublePay": 1, "packageType": 2, "payLaterProtocol": 1}
# 秒杀(seckill) 契约（实证 areaId 313601/batchId 6757127，报名成功 2481608392）。比便宜包邮复杂：
#   applyItems 2项(场次时间+时长)；skuItems 10项；skuExtendInfo 多 structTitle/expectedTime/priceConfirm/
#   freeShipping/**purchaseDetail(带价格校验token)**。⚠️token+structTitle 来自额外接口(query/purchase/couponInfo、
#   generateStructTitle)，body 未抓到 → 秒杀 saveApply 组装**待补这两接口**。batchId 每场次(getBatchId)。
_EXT_FIELDS = ["cid1Name", "cid2Name", "cid3Name", "imgRui", "name", "wareId", "saler",
               "venderId", "stockNum", "brandId", "brandName", "mainBrandId",
               "noFreeShippingRegions", "freeShipping", "l2Invite35271"]


def sku_materials(sku_id, area_id: int) -> dict:
    """商品素材（POST /apply/openness/querySkuMaterials，queryTypes[1,8,11]）→ whitePic(白底图)/clearPic 等。只读。"""
    with _client() as client:
        d = _post(client, "/apply/openness/querySkuMaterials",
                  {"areaId": int(area_id), "skuIds": [int(sku_id)], "queryTypes": [1, 8, 11]})
    return d.get(str(sku_id)) or {}


def short_title(sku_id, area_id: int) -> Optional[str]:
    """商品短标题（POST /apply/openness/queryNewShortTitle）→ shortTitle。只读。"""
    with _client() as client:
        d = _post(client, "/apply/openness/queryNewShortTitle",
                  {"areaId": int(area_id), "skuIds": [int(sku_id)]})
    return (d.get(str(sku_id)) or {}).get("shortTitle")


def purchase_coupon_info(sku_id, area_id: int, batch_id: int, promo_price,
                         duration=24, new_gift_type: int = 1, client=None) -> dict:
    """按促销价取价格校验（POST /apply/openness/query/purchase/couponInfo）→ **purchaseDetail(含token)** +
    minPurchasePrice/priceDiff/warningPrice。**token 按 promoPrice 生成，秒杀 saveApply 必需**。只读。
    client 传入则复用（二分逐次调用省建连开销）；缺省自建。

    ★**`minPurchasePrice` = 报名价 − Σ(存量券的「我担」部分) = 京喜到手价（我实收）**，不是客户到手价。
    2026-07-28 用三种券型交叉验证（同一 batch，逐档试算）：
      · 85折全自担 `10167510394550`：29.90→25.41、25.41→21.60（恒 ×0.85）
      · 定额我担3 `10130829981648`（4.01-4券 客4/我3）：4.49→1.49、4.20→1.20、4.00→1.00
      · 定额我担2 `10131083199654`（5.1-5共补 客5/我2）：5.54→3.54、5.20→3.20、5.00→3.00
    `couponKeyList` 返回参与试算的券 id（空=无券叠加）。

    ⚠️三条必须知道的边界：
    1) **定额券是 1:1 自伤**：报名价每降 1 元，我实收就少 1 元，券减免一分不缩（不像折扣券按比例缩）。
       ⇒ 报名价应尽量高、别为做低到手价主动压价。
    2) **试算不校验券的满减门槛**：把 `10130829981648` 报到 3.90（已跌破满4.01）时仍按减 3 算出 0.90，
       但前台该券应当失效。**别拿试算当门槛校验用。**
    3) **频道券（如 5.9-5 钩子券）不参与试算**：`10191500638970` 带 5.9-5 却 couponKeyList 空、到手=报名价。
       它要在微信小程序 9.9 包邮频道领取才生效 ⇒ 真实我实收还要再减该券我担部分（5.9-5 我担 2.45）。
    传超出门槛的报名价会返回**空** minPurchasePrice（实证 40.00 → 空）＝被拒，不是报 0。"""
    body = {"areaId": int(area_id), "batchId": int(batch_id), "activityDuration": duration,
            "skuList": [{"promoPrice": str(promo_price), "skuId": int(sku_id), "newGiftType": new_gift_type}]}
    path = "/apply/openness/query/purchase/couponInfo"
    if client is not None:
        d = _post(client, path, body)
    else:
        with _client() as c:
            d = _post(c, path, body)
    return (d[0] if isinstance(d, list) and d else {}) or {}


def build_apply_body(sku_id, area_id: int, batch_id: int, promo_price, white_img: str = None,
                     duration="30", stock=5000, jd_price=None, form=None, apply_ext=None,
                     ext_overrides: dict = None) -> dict:
    """组装 saveApply body（便宜包邮）——**全自动**：ware_detail(skuExtendInfo+numeric cid) + querySkuMaterials(白底图) + queryNewShortTitle(短标题)。
    numeric cid 映射自 frist/second/thirdLevelCategoryId(拼写frist)。jd_price=京东价 pPrice(填 jdPriceStr)。white_img 不传则自动取 whitePic。
    返回 {body, missing:[缺失关键字段]}——missing 非空不应真发。"""
    form = form or BAOYOU_FORM
    apply_ext = apply_ext or BAOYOU_APPLY_EXT
    d = ware_detail(sku_id, area_id, batch_id, activity_duration=duration)
    rec = (d.get("skuList") or d.get("failSkuList") or [{}])[0]
    ext = {k: rec.get(k) for k in _EXT_FIELDS}
    ext["skuId"] = int(sku_id)
    ext["recommend"] = 1
    # numeric cid：ware_detail 的 cid1/2/3 为 null，取 *LevelCategoryId（注意拼写 frist）
    ext["cid1"] = rec.get("fristLevelCategoryId")
    ext["cid2"] = rec.get("secondLevelCategoryId")
    ext["cid3"] = rec.get("thirdLevelCategoryId")
    ext["jdPriceStr"] = rec.get("jdPriceStr") or (str(jd_price) if jd_price is not None else None)
    # 白底图 + 短标题：各自接口
    mat = sku_materials(sku_id, area_id)
    white = white_img or mat.get("whitePic")
    ext["whiteImgSource"] = mat.get("whiteImgSource") or "20"
    st = short_title(sku_id, area_id) or (rec.get("name") or "")[:10]
    ext["originShortTitle"] = st
    ext["shortTitleSource"] = "20"
    ext["originShortTitleSource"] = "20"
    if ext_overrides:
        ext.update(ext_overrides)
    missing = [k for k in ("cid1", "cid2", "cid3", "jdPriceStr", "wareId") if not ext.get(k)]
    if not white:
        missing.append("whiteImg(白底图)")
    body = {
        "areaId": int(area_id), "batchId": int(batch_id), "applyExtendInfo": dict(apply_ext),
        "applyItems": [{"formItemId": form["duration"], "value": str(duration)}],
        "skuList": [{
            "skuItems": [
                {"formItemId": form["skuId"], "value": int(sku_id)},
                {"formItemId": form["promoPrice"], "value": promo_price},
                {"formItemId": form["stock"], "value": int(stock)},
                {"formItemId": form["whiteImg"], "value": white},
            ],
            "skuId": int(sku_id), "skuExtendInfo": ext,
        }],
    }
    return {"body": body, "missing": missing}


def _apply_token(sku_id, area_id, batch_id, promo_price) -> str:
    return _confirm_token({"path": "ms/saveApply", "sku": str(sku_id), "areaId": str(area_id),
                           "batchId": str(batch_id), "promoPrice": canon_num(promo_price)})


def apply_dryrun(sku_id, area_id: int, batch_id: int, promo_price, white_img: str = "",
                 duration="30", stock=5000, jd_price=None, ext_overrides: dict = None) -> dict:
    """报名 DRY-RUN：组装 saveApply body 但**不发送**，回显 body + 缺失字段 + confirm_token。
    promo_price=定价器解出的报名价。missing 非空(白底图/numeric cid 等)时不应真发。"""
    r = build_apply_body(sku_id, area_id, batch_id, promo_price, white_img,
                         duration=duration, stock=stock, jd_price=jd_price, ext_overrides=ext_overrides)
    return {"would_apply": False, "request": {"path": "/apply/openness/saveApply", "body": r["body"]},
            "missing": r["missing"],
            "note": ("DRY-RUN：未提交。missing 非空表示还差字段(尤其白底图)，补齐再报。"
                     "真执行：相同参数 + confirm=confirm_token 调 ms_apply。"),
            "confirm_token": _apply_token(sku_id, area_id, batch_id, promo_price)}


@audited("ms", "apply")
def apply(sku_id, area_id: int, batch_id: int, promo_price, white_img: str,
          duration="30", stock=5000, jd_price=None, ext_overrides: dict = None, confirm: str = "") -> dict:
    """**报名真执行**（saveApply，真金白银）。需相同参数先 apply_dryrun 拿 confirm_token 再带 confirm。
    白底图必填；missing 非空拒发。"""
    if confirm != _apply_token(sku_id, area_id, batch_id, promo_price):
        raise BlacklightError("报名需二次确认：先用相同参数跑 ms_apply_dryrun 拿 confirm_token 再带 confirm。")
    r = build_apply_body(sku_id, area_id, batch_id, promo_price, white_img,
                         duration=duration, stock=stock, jd_price=jd_price, ext_overrides=ext_overrides)
    if r["missing"]:
        raise BlacklightError(f"缺关键字段，拒绝报名（避免脏数据）：{r['missing']}。补齐 white_img/ext_overrides 再试。")
    with _client() as client:
        d = _post(client, "/apply/openness/saveApply", r["body"])
    return {"applied": True, "skuId": str(sku_id), "applyId": d, "promoPrice": promo_price,
            "confirm_token": confirm}


# ---------- 秒杀场次：getBatchId ----------
# 秒杀报名 **T+3 起报**（如 7.9 最早报 7.12）；每日 **8 个固定场次**。batchId 按(areaId,场次)稳定。
SECKILL_SESSIONS = ["00:00:00", "08:00:00", "10:00:00", "12:00:00",
                    "16:00:00", "18:00:00", "20:00:00", "22:00:00"]


def get_rule_id(area_id: int, activity_id: str) -> str:
    """取该活动的 **ruleId**（POST /openness/area/detail）——list_eligible/price/export 必需，但报名页 URL **不含** rule_id。
    2026-07-14 实证 activity 101666814/area 313601 → `87yoF4oB97mt0bp_tXKW`（ruleIdOpen=true）。只读，免浏览器抓包。"""
    with _client() as client:
        d = _post(client, f"/openness/area/detail?areaId={int(area_id)}&activityId={activity_id}", {})
    return (d or {}).get("ruleId") or ""


def get_batch_id(area_id: int, begin_time: str, register_mode=None, end_time=None) -> dict:
    """取秒杀场次 batchId（POST /apply/openness/getBatchId）。begin_time='YYYY-MM-DD HH:00:00'(日期须≥T+3+场次整点)。只读。

    ⚠️两种报错意思完全不同，别混（2026-08-10 实测）：
      · `当前时间不支持报名，请重试` —— **日期没到 T+3**（说的是"当前时间"不是场次）。
        今天 08-10 试 08-10/11/12 全是这个，08-13 起才正常。
      · `获取时间信息失败` —— **该场次不存在**。实测 10:00 / 20:00 有场次，**14:00 没有**。"""
    with _client() as client:
        d = _post(client, "/apply/openness/getBatchId",
                  {"areaId": int(area_id), "beginTime": begin_time,
                   "registerMode": register_mode, "endTime": end_time})
    return d


# ↓ 秒杀逐SKU报名（seckill_apply / build_seckill_body）已删除（2026-07-14）：
#   被今天真报665次验证过的 table_apply(channel='seckill') 完全取代（表格批量·回执自验证·一次上传全部）。
#   定价改用 solve_seckill_price/plan_seckill_enroll（couponInfo 真值 + 导出门槛约束）。


# ---------- 报名元数据：ware/detail/list（formItems formSet + skuExtendInfo，saveApply 前置） ----------
def ware_detail(sku_ids, area_id: int, batch_id: int, activity_duration=30,
                search_type: int = 0, apply_sku_type: str = APPLY_SKU_TYPE_JX) -> dict:
    """取报名 skuExtendInfo（POST /apply/openness/ware/detail/list）——saveApply 前置读接口。
    ⚠️**必须带 applySkuType='3'(京喜自营)**，否则默认京东自营→返回"无权直接报名"空版(抹掉numeric cid/白底图)。
    skuExtendInfo 含 cid/name/imgRui/wareId/jdPriceStr/saler。只读。"""
    skus = sku_ids if isinstance(sku_ids, str) else ",".join(str(s) for s in sku_ids)
    body = {"searchType": int(search_type), "skuIds": skus, "areaId": int(area_id),
            "batchId": int(batch_id), "activityDuration": activity_duration,
            "applySkuType": str(apply_sku_type)}
    with _client() as client:
        d = _post(client, "/apply/openness/ware/detail/list", body)
    return d


# ---------- 可报商品列表（pagingQuerySkuList4Http，含报名门槛价） ----------
_DISP_FIELDS = ["itemFirstCateName", "itemSecondCateName", "itemSkuId", "itemThirdCateName",
                "lowPrice30d", "pPrice", "skuD7DealSaleQtty", "skuName", "spuId",
                "spuLowestPriceD30", "stockQtty"]

# ★★门槛价字段**按池不同**，必须两个都读（2026-08-06 实测定案）：
#   便宜包邮前置池(357902)/后续池(43813702)：仍返回 `opennessMinPrice` + `appliedStatus`（原样，没变）
#   秒杀池(313601)：**已不返回 opennessMinPrice**，改叫 **`suggestPrice`**；`appliedStatus` 改叫 `selectStatus`
#     —— dispFields 里加回 opennessMinPrice 也不回来，是服务端改的，不是请求少要字段。
#   等价性实测：秒杀池 300 条 suggestPrice vs 导出「报名价格上限」→ **244 条完全相等、0 条不一致**、56 条两边同空。
# ⚠️只认单个字段名 ⇒ 门槛静默变 None ⇒ 报名价上限硬约束**无声消失**（不报错、不降级），
#   所以这里宁可两个都试也不要写死一个。新池若再改名，doctor 的「门槛字段哨兵」会先叫。
_THRESHOLD_KEYS = ("opennessMinPrice", "suggestPrice")

# ⚠️**`selectStatus` 不是「已报名」**（2026-08-06 实证，纠正当天早些时候的错误归一）：
#   秒杀池可报清单里它**恒为 1**（我名下 250/250 全是 1），而这批 SKU 经 `applied_by_skus`
#   查证 **352/352 都未报名** ⇒ 它表示的是「已入选品池」，与报名无关。
#   曾把它当 `appliedStatus` 的兜底别名——**比返回 None 更危险**，因为它看着像真数据，
#   会让人误以为"去重内建"仍然成立。∴ 这里只认真正的 `appliedStatus`；
#   **秒杀池判重请用 `applied_by_skus(activity_id, area_id, skus, dates=[...])`**，别信这个列表。
_APPLIED_KEYS = ("appliedStatus",)


def _pick(it: dict, keys) -> tuple:
    """按顺序取第一个非空字段，返回 (值, 命中的字段名)；都没有 → (None, None)。"""
    for k in keys:
        v = it.get(k)
        if v not in (None, ""):
            return v, k
    return None, None


def _build_filter(erp_assistant: str, sales_erps: str, rule_id: str) -> list:
    """pagingQuerySkuList4Http 的 filterFields（pop + 京喜自营 + ruleId + 采销助理ERP [+ 销售员ERP]）。

    ⚠️**空值的 ERP 条件必须整条不发**，不能发 `{"text": ""}`——各条件是 **AND**，
    多发一条就是多一道过滤（实证：加上 销售员ERP 后 21921 → 3672）。
    """
    rc = [
        {"code": "dataType", "type": 7, "labelName": "商品类型", "data": [{"code": "3", "name": "pop"}]},
        {"code": "el28284", "type": 52, "labelName": "京喜自营模式全量商品", "data": [{"text": "1"}]},
        {"code": "successRuleIds", "type": 52, "labelName": "ruleId", "data": [{"text": rule_id}]},
    ]
    if (erp_assistant or "").strip():
        rc.append({"code": "l2ErpNew35076", "type": 52, "labelName": "采销助理ERP",
                   "data": [{"text": erp_assistant}]})
    if (sales_erps or "").strip():
        rc.append({"code": "erp", "type": 52, "labelName": "销售员ERP", "data": [{"text": sales_erps}]})
    return [{"ruleName": "", "ruleContent": rc}]


def _resolve_erp(erp_assistant, sales_erps) -> tuple:
    """多用户：`erp_assistant`(采销助理ERP) 留空默认取**当前登录用户 PIN**。

    ★★`sales_erps`(销售员ERP) **默认不过滤**（返回 ""，`_build_filter` 会整条跳过）。
    2026-07-28 实证：旧实现让 `销售员ERP 缺省 = 采销助理ERP`，等于多加一道 AND，
    把可报范围从 **21921 砍到 3672（−83%）**——而报名页显示的正是「只按采销助理ERP」的 21921。
    因此害我在 6228 池只报了 136 款。**要按销售员维度筛时显式传 sales_erps。**
    """
    ea = (erp_assistant or "").strip() or jd_auth.current_pin()
    se = (sales_erps or "").strip()          # 空 = 不加这条过滤（与报名页口径一致）
    return ea, se


def list_eligible(batch_id: int, area_id: int, rule_id: str, erp_assistant: str = None, sales_erps: str = None,
                  business_type: int = 122, activity_duration: str = "30",
                  page: int = 1, page_size: int = 50) -> dict:
    """可报商品列表（POST /openness/selection/pagingQuerySkuList4Http）。
    返回 items：skuId/skuName/pPrice/**threshold(报名价上限，规范键)**/appliedStatus/库存/类目。
    batch_id/rule_id 每池特有。

    ★**门槛价读 `threshold`，别读 `opennessMinPrice`**（2026-08-06）：门槛字段名**按池不同**——
      便宜包邮池给 `opennessMinPrice`，**秒杀池已改成 `suggestPrice`**（详见 `_THRESHOLD_KEYS` 注释）。
      `threshold` 是两者归一后的规范键；`thresholdField` 告诉你这池实际用的哪个字段（漂移时好定位）。
      `opennessMinPrice`/`appliedStatus` 保留为**兼容别名**，值已是归一后的值（老调用方不用改也能拿对）。

    ★**归属口径 = 只按 `erp_assistant`(采销助理ERP)，与报名页一致**。`sales_erps`(销售员ERP) 默认**不加**。
      2026-07-28 血的教训：旧实现默认 `销售员ERP=采销助理ERP`，多一道 AND ⇒ **21921 → 3672（−83%）**，
      导致 6228 池只报了 136 款（补报后 315）、7165 池只看到 592（实为 743）。
      各 filterField 之间是 AND，**空值条件必须整条不发**。

    ⚠️**量大别翻页，用导出**：21921 条要翻 110 页、并发高了会静默返空（实测 workers=6 只成功 21 页）。
      正解：`export_eligible()` 触发 → `export_fetch()` 拿直链。翻页仅适合几百条以内，
      且必须 **workers≤3 + 重试 + 校验空页**，最后按 totalCount 对数。"""
    erp_assistant, sales_erps = _resolve_erp(erp_assistant, sales_erps)
    body = {"currentPage": page, "pageSize": page_size, "businessType": business_type,
            "dispFields": _DISP_FIELDS, "batchId": int(batch_id), "areaId": int(area_id),
            "ruleFields": [], "dimType": 1, "ruleType": 0,
            "filterFields": _build_filter(erp_assistant, sales_erps, rule_id),
            "sortFields": [{"ruleContent": []}], "activityDuration": str(activity_duration)}
    with _client() as client:
        d = _post(client, "/openness/selection/pagingQuerySkuList4Http", body)
    items = []
    for it in (d.get("list") or []):
        thr, thr_field = _pick(it, _THRESHOLD_KEYS)            # 门槛价：按池归一(见 _THRESHOLD_KEYS)
        applied, _ = _pick(it, _APPLIED_KEYS)
        items.append({"skuId": str(it.get("itemSkuId") or ""), "skuName": it.get("skuName"),
                      "pPrice": it.get("pPrice"),
                      "threshold": thr, "thresholdField": thr_field,   # 规范键 + 命中的原始字段名
                      "opennessMinPrice": thr, "appliedStatus": applied,   # 兼容别名(值=归一后)
                      "selectStatus": it.get("selectStatus"),   # 入选品池标记，**不是已报状态**(见 _APPLIED_KEYS)
                      "stockQtty": it.get("stockQtty"),
                      "lowPrice30d": it.get("spuLowestPriceD30")})
    return {"totalCount": d.get("totalCount"), "items": items}


# ---------- 定价（秒杀/便宜包邮/特价：报名价=直接设的促销价，本地引擎反解） ----------
def solve_price(sku_id, target_margin: float, cap=None, channel: str = "ms",
                objective: str = "max_margin", max_enroll=None) -> dict:
    """单 SKU 解**建议报名价(=促销价)** —— ms/便宜包邮/特价 同套（都属单品促销，报名替换同类型）。
      cap: **客户到手价上限**（秒杀=applied/page 的 maxActualPrice）。
      max_enroll: 便宜包邮/特价的 `opennessMinPrice` ＝ **报名价(促销价)上限**，且
        **= 该 SKU 在前置收品池的提报价 × 折扣率**（2026-07-28 由平台报错原文定案）：
          「促销价格需<=25.41元; 要求促销小于等于该SKU在收品池【357902】提报价*折扣率(25.41*100%)」
        这解释了为什么它多数等于京东价（前置池报名价就是京东价），少数不等（`10167510394550` 门槛 25.41 / 京东价 29.9）。
        ⚠️**卡死到分**：报价高 0.01 就被拒（实证 22 款报 19.9 被要求 ≤19.89）。直接取 `opennessMinPrice` 当报价最安全。
        （`plan_baoyou_enroll` 里「到手价上限」的说法是错的；`purchase_coupon_info.minPurchasePrice` 是另一回事——见其文档。）
      毛利算**京喜承担口径**，cap 约束客户到手价。复用 osw_pricing 双模型（已实证=平台试算）。"""
    from blacklight.osw import margin as _mt
    from blacklight.osw import pricing as _P
    pr = _mt.query_pricing(sku_id)
    excl = {_P.CHANNEL_CATEGORY.get(channel, "单品促销")}         # 单品促销：报名替换同类型已有
    jx = _P.build_model(pr, exclude_cats=excl, basis="jx")
    cust = _P.build_model(pr, exclude_cats=excl, basis="customer")
    r = _P.solve_enroll_price(jx, pr["fixedCost"], pr["cpsRate"], float(target_margin),
                              hi=pr["benchPrice"], cap=cap, objective=objective, cust_model=cust,
                              max_enroll=max_enroll)
    return {"skuId": pr["skuId"], "name": (pr.get("skuName") or "")[:24],
            "benchPrice": pr["benchPrice"], "cap": cap, "门槛价": max_enroll,
            "jxActualNow": pr["jxActualPrice"], "jxProfitNow": pr["jxFullProfit"],
            "jxMarginNow": pr["jxMargin"], **r}


def price_eligible(batch_id: int, area_id: int, rule_id: str, erp_assistant: str = None, sales_erps: str = None,
                   target_margin: float = 0.10, objective: str = "max_margin", channel: str = "baoyou",
                   page: int = 1, page_size: int = 30, limit: int = 30, concurrency: int = 8) -> dict:
    """对**可报**（还没报名）SKU 批量解建议报名价，**opennessMinPrice 当报名价门槛(上限)**。
    先 list_eligible 拉可报商品，再**并发** solve_price（max_enroll=门槛价）。报名前决策用。⚠️秒杀请用 plan_seckill_enroll(平台真值)，此函数(本地模型)留给便宜包邮/特价。"""
    el = list_eligible(batch_id, area_id, rule_id, erp_assistant, sales_erps,
                       page=page, page_size=page_size)
    items = el["items"][:limit] if limit else el["items"]

    def _one(it):                                             # 单SKU试算(只读)，并发跑；自吞异常
        try:
            r = solve_price(it["skuId"], target_margin, channel=channel, objective=objective,
                            max_enroll=it.get("threshold"))       # 归一后的门槛价(见 list_eligible)
            r["pPrice"] = it.get("pPrice")
            r["建议报名价"] = r.pop("enroll_price", None) if r.get("ok") else None
            r["定价"] = "OK" if r.get("ok") else r.get("reason", "")[:40]
            return r
        except Exception as e:
            return {"skuId": it["skuId"], "error": str(e)[:80]}
    rows = pmap(_one, items, concurrency)                     # 并发试算，保序
    return {"totalEligible": el.get("totalCount"), "priced": len(rows),
            "target_margin": target_margin, "objective": objective, "rows": rows}


def price_applied(activity_id: str, area_id: int, target_margin: float,
                  apply_sku_type: str = APPLY_SKU_TYPE_JX, objective: str = "max_margin",
                  limit: int = 50) -> dict:
    """对某池**已报名** SKU 批量解建议报名价（cap 取各自 maxActualPrice 到手价上限）。
    返回每 SKU：当前促销价/京喜盈亏 + 建议报名价/预测京喜到手价/毛利。"""
    ap = get_applied(activity_id, area_id, apply_sku_type, page_size=100)
    items = ap["items"][:limit] if limit else ap["items"]
    rows = []
    for it in items:
        try:
            r = solve_price(it["skuId"], target_margin, cap=it.get("maxActualPrice"))
            r["promoPriceNow"] = it.get("promoPrice")
            r["建议报名价"] = r.pop("enroll_price", None) if r.get("ok") else None
            r["定价"] = "OK" if r.get("ok") else r.get("reason", "")[:40]
        except Exception as e:
            r = {"skuId": it["skuId"], "error": str(e)[:80]}
        rows.append(r)
    return {"totalApplied": ap.get("totalCount"), "priced": len(rows),
            "target_margin": target_margin, "objective": objective, "rows": rows}


# =========================================================================== #
# ★秒杀批量报名规划（couponInfo 平台真值定价，复用百补方法论，2026-07-14 沉淀）
#   到手价真值=couponInfo(=price_info,已实证等价)；报名价上限=couponInfo 有无 token；
#   毛利=客户到手价口径(保守)；有平台出资券的转 review 待京喜口径复核（同百补）。
# =========================================================================== #
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ci_token(ci: dict) -> str:
    """couponInfo 返回里的报名价格 token（purchaseDetail[0].token），无则空。"""
    pd = ci.get("purchaseDetail")
    pdl = _json.loads(pd) if isinstance(pd, str) and pd else (pd or [])
    return (pdl[0].get("token") if pdl else "") or ""


def solve_seckill_price(sku_id, area_id: int, batch_id: int, fixed_cost, cps_rate, jd_price,
                        target_margin: float = 0.05, duration=28, threshold=None,
                        max_iter: int = 14, client=None, objective: str = "max_margin") -> dict:
    """秒杀解报名价 —— **couponInfo 平台真值**。**到手价对报名价只保证单调递增、不保证线性**
    （满减/国补封顶/多券分段生效有拐点台阶）→ **线性只做快路径初值，实际到手价一律 couponInfo 校验；不达标则二分兜底**（安全靠校验真值，不靠线性外推）。
    约束：到手价∈[保毛利线 T, threshold门槛]（门槛硬约束）。

    ★**objective 决定在这个区间里取哪一端**（2026-08-03 加，此前只有 min_price 一种行为）：
      · `min_price`(默认，保持旧行为)：取**最低**报名价 ⇒ 到手价贴着 T ⇒ **毛利率恰好等于 target_margin**。最激进降价。
      · `max_margin`：取**最高**报名价（到手价顶到门槛）⇒ 毛利率最大。
    ⚠️用户说「毛利率≥X%」通常是**闸**不是**目标**，那种场景应传 `max_margin`。
    实证差异（1053 款秒杀）：min_price 毛利合计 2053 元/单、全部恰好 5.0%；max_margin 中位 18.5%、合计 11434 元/单，**差 5.6 倍**
    （中位只多卖 4.4%，但 min_price 对高毛利款最狠砍 78.5%）。另注意定额券是 1:1 自伤，压价一分不省券钱。

    毛利口径 = `1 − cps − fc/到手价`；`minPurchasePrice` 是**京喜到手价(我实收)**。
    提效：client 复用(整轮省建连) + 线性快路径 + 二分精度0.05。返回 {ok,enroll_price,actual_price,margin,method,objective} 或 {ok:False,reason}。"""
    fc = float(fixed_cost); cps = float(cps_rate or 0); jd = float(jd_price or 0); thr = _f(threshold)
    T = fc / (1 - cps - target_margin)                       # 保 target 毛利的最低到手价
    if thr is not None and T > thr + 1e-6:
        return {"ok": False, "reason": f"门槛{round(thr,2)}<保{target_margin*100:.0f}%到手线{round(T,2)}(定价亏,不可报)"}
    _own = client is None
    cl = _client() if _own else client                       # L1: 整轮二分/试算复用一个 client

    def q(p):                                                # 报名价→到手价(平台真值)；无 token/失败→None
        d = purchase_coupon_info(sku_id, area_id, batch_id, round(float(p), 2), duration=duration, client=cl)
        a = _f(d.get("minPurchasePrice"))
        return a if (a is not None and _ci_token(d)) else None

    def _finish(P, a, method):                               # 保T + ≤门槛 微调 + 校验(逐0.01,真值)
        for _ in range(20):
            if a is not None and a >= T - 1e-9:
                break
            P = round(P + 0.01, 2)
            if P > jd:
                break
            a = q(P)
        for _ in range(30):
            if a is None or thr is None or a <= thr + 1e-6:
                break
            P = round(P - 0.01, 2)
            if P <= fc:
                break
            a = q(P)
        if a is None:
            return {"ok": False, "reason": "报名价无 token(不可试算)"}
        if thr is not None and a > thr + 1e-6:
            return {"ok": False, "reason": f"到手价压不到门槛{round(thr,2)}下且保毛利(到手{round(a,2)})"}
        m = 1 - cps - fc / a
        if m < target_margin - 1e-6:
            return {"ok": False, "reason": f"到手{round(a,2)}毛利{round(m*100,1)}%<{target_margin*100:.0f}%"}
        return {"ok": True, "enroll_price": P, "actual_price": round(a, 2),
                "margin": round(m, 4), "has_token": True, "method": method,
                "objective": objective}
    try:
        if objective == "max_margin":                        # ★前置：不需要 q(jd) 锚点，**每 SKU 仅 1 次调用**
            # ★**报名价 = min(门槛, 京东价)，一次到位、不二分**（2026-08-04 探针定案）。
            # 为什么不用二分找「到手价顶到门槛」的更高报名价：
            #   ① 探针实证 **couponInfo 不校验门槛** —— 门槛+1.00 照样返回 minPurchasePrice
            #      （10229495923536 门槛11.01→报12.01 有值；10229495923534 门槛13.64→报14.64 有值）。
            #      门槛是**提交时**才校验的，试算里没有这个边界，二分等于在搜一个不存在的拐点(白烧 ~14×调用)。
            #   ② 取门槛价**同时满足两种口径**：若门槛卡报名价 ⇒ 正好顶格；若卡到手价 ⇒ 到手价≤报名价=门槛
            #      也成立（券只会往下压）。文档里两种说法并存，取这个值都对。
            # 不能复用 _finish：它为保毛利线会**向上推报名价**，可能推过门槛→提交时被拒。
            # 门槛是硬上限，这里只做一次校验：达不到目标毛利就如实判不可行，不许越界。
            P = round(min(thr, jd), 2) if thr is not None else round(jd, 2)
            a = q(P)
            if a is None:
                return {"ok": False, "reason": f"报名价{P}无 token(不可试算/被拒)"}
            m = 1 - cps - fc / a
            if m < target_margin - 1e-6:
                return {"ok": False,
                        "reason": (f"顶到门槛{P}、到手{round(a,2)} 毛利仅 {round(m*100,1)}%"
                                   f"<{target_margin*100:.0f}%（门槛封顶，再高会被拒）")}
            return {"ok": True, "enroll_price": P, "actual_price": round(a, 2),
                    "margin": round(m, 4), "has_token": True,
                    "method": "max-margin-cap", "objective": objective}
        a_jd = q(jd)                                         # 顶到京东价的到手价 = 可行性 + 线性斜率锚点
        if a_jd is None or a_jd < T - 1e-9:
            return {"ok": False, "reason": f"报名价顶到京东价{jd}、到手{a_jd}仍<保毛利线{round(T,2)}(空间不够)"}
        # L2 线性快路径：估初值 P0，**用 couponInfo 校验实际到手价**——达标(到手∈[T,门槛])直接用+下探到最低，否则二分兜底
        k = a_jd / jd if jd else 0
        if k > 0:
            P0 = min(max(round(T / k, 2), round(fc + 0.01, 2)), round(jd, 2))
            a0 = q(P0)
            if a0 is not None and a0 >= T - 1e-9 and (thr is None or a0 <= thr + 1e-6):
                P, a = P0, a0
                for _ in range(6):                           # 线性初值已达标→下探到"恰好保T的最低价"
                    Pd = round(P - 0.01, 2)
                    if Pd <= fc:
                        break
                    ad = q(Pd)
                    if ad is None or ad < T - 1e-9 or (thr is not None and ad > thr + 1e-6):
                        break
                    P, a = Pd, ad
                return _finish(P, a, "linear-fastpath")
        # L2 兜底 / L3：全区间二分(精度 0.05)——线性初值不达标(非线性/越界)时走这里
        lo, hi = fc, jd
        for _ in range(max_iter):
            if hi - lo <= 0.05:
                break
            mid = round((lo + hi) / 2, 2); a = q(mid)
            if a is None:
                lo = mid; continue
            if a >= T:
                hi = mid
            else:
                lo = mid
        return _finish(round(hi, 2), q(round(hi, 2)), "bisect")
    finally:
        if _own:
            cl.close()


# ★★2026-08-21：进程内缓存在本包里**等于没加**——每次都是新进程跑脚本，一退就没。
#   秒杀门槛导出要等 ~9 分钟，重跑一次就白等一次。改用 core.reuse 的落盘缓存。
#   （仍保留进程内 dict 做一级，省掉同进程内的反复读盘）
_THR_CACHE = {}      # (area_id, batch_id) -> (完成时刻, {sku: 门槛}, meta)   一级：进程内
_THR_CACHE_TTL = 6 * 3600      # 二级：落盘，门槛当天有效
_THR_PENDING = {}    # (area_id, batch_id) -> (已完成任务id快照, createTime下界, 触发时刻)  跨调用续等/续试，不重复排队
_THR_TTL = 1800      # 缓存 30 分钟：同一 batch 内门槛稳定，跨 batch/跨天必重取


def seckill_thresholds(area_id: int, activity_id: str, batch_id: int, rule_id: str,
                       wait_timeout: int = 900, allow_stale: bool = False,
                       cache_ttl: int = None, with_meta: bool = False,
                       reuse_since: str = None, fresh_within_s: float = 21600):
    """导出可提报清单 → 解析每 SKU 的**【报名价格上限】**。返回 {skuId: 门槛(float)}（`with_meta=True` 时返回带元信息的 dict）。

    ⚠️门槛是秒杀报名硬约束（到手价>门槛必被拒）→ 报名前必取。
      注：`list_eligible` 的 `threshold`（秒杀池取自 `suggestPrice`）与导出的「报名价格上限」**实测同值**
      （300 条中 244 相等、0 不一致、56 两边同空），量小时可用它免去 9 分钟导出；
      整池报名仍以导出为准（导出是平台给的完整清单，翻页有静默丢页风险）。

    ★★**2026-08-06 修：旧实现必定读到旧导出**。旧代码第一步就是无过滤的 `export_fetch()`，
      而"最新已完成的导出"**永远存在**（历史任务不会消失）⇒ 直接返回它、后面的"触发+轮询"是死代码。
      实测代价（08-05 旧清单 vs 08-06 新清单，同一 area）：旧 990 条 / 新 4526 条 ⇒
      **3658 个当天可报 SKU 在旧清单里根本没有（漏报）**；交集 868 条中 **274 条门槛已变，
      121 条旧门槛更高 ⇒ 照旧值报价会超上限被拒**；另有 122 条旧清单里的已不可报。
      这正是 `export_fetch` docstring 里 2026-08-04 记的同一个坑——**当时修了 export_fetch 的参数，
      没修调用方**。∴ 这里一律 `exclude_ids` 认领"自己刚触发的那一份"，**绝不接受旧导出**。

    ★**耗时**：文档旧称"1~2 分钟"是错的，实测 4726 条约 **9 分钟**（进度 3%→100%）。
      故 `wait_timeout` 默认 900s；旧默认 150s 必超时，而超时后旧代码又 `return _grab()` 退回旧导出（双重踩坑）。

    - `reuse_since='YYYY-MM-DD HH:MM:SS'`：**复用该时刻之后已完成的导出，不触发新的**。
      用于"刚导过一份、就用它"——避开约 40 分钟的触发窗口（见 `_trigger`）。
      与 `allow_stale` 的区别：这个是**你明确指定了新鲜度下界**，不是兜底吃最新的旧货；
      找不到符合条件的仍会走正常触发流程，绝不悄悄降级。
    - 超时**抛错**，不返回旧值——宁可失败也不能拿错门槛去报价。真要旧值请显式 `allow_stale=True`。
    - 进程内按 (area, batch) 缓存 30 分钟；超时后再次调用会**续等同一个导出任务**（`_THR_PENDING`），
      不重复触发（`/apply/openness/apply/export` 有 5 分钟限流）。
    """
    ttl = _THR_TTL if cache_ttl is None else cache_ttl
    key = (int(area_id), int(batch_id))

    def _parse(r):
        m = {}
        for row in r.get("rows") or []:
            s = str(row.get("skuId") or row.get("itemSkuId") or "")
            t = _f(row.get("门槛") or row.get("报名价格门槛") or row.get("报名价格上限"))
            if s and t is not None:
                m[s] = t
        return m

    def _ret(m, meta):
        return {"thresholds": m, **meta} if with_meta else m

    hit = _THR_CACHE.get(key)                                  # ① 缓存(同 batch 30 分钟内直接用)
    if hit and (_time.time() - hit[0]) < ttl:
        return _ret(hit[1], dict(hit[2], cached=True))

    def _adopt(r, extra):
        """把一份已就绪的导出收进缓存并返回。"""
        m = _parse(r)
        t = r.get("task", {}) or {}
        meta = {"taskId": t.get("id"), "fileName": t.get("fileName"),
                "createTime": t.get("createTime"),
                "rowsInExport": len(r.get("rows") or []),
                "skusWithThreshold": len(m),
                "skusWithoutThreshold": len(r.get("rows") or []) - len(m),
                "cached": False}
        meta.update(extra)
        _THR_CACHE[key] = (_time.time(), m, meta)
        return _ret(m, meta)

    if reuse_since:                                            # ①.5 明确指定复用某时刻后的导出→不触发
        r = export_fetch(area_id, download=True, after=str(reuse_since))
        if r.get("ready") and r.get("rows"):
            return _adopt(r, {"reused_since": str(reuse_since), "_source": "reuse_since"})

    # ①.6 ★★**先看今天是不是已经有一份跑完的导出**（2026-08-24 加）
    #   起因：8-27 场次我从 09:41 起按"触发→每70s重试→等900s"烧了 15 分钟、又白等 40 分钟，
    #   而用户 10:28 手点的那份**就摆在任务列表里**——我从 09:45 之后再没查过列表。
    #   导出是**账号级共享资源**，别人（含用户本人、其它进程）随时可能刚导过一份。
    #   ∴ 触发之前必须先查一次现成的。`fresh_within_s=0` 可关掉这一步。
    #   ⚠️只认**足够新**的：门槛天天变（实测 274 条已变、121 条旧值更高⇒照旧值报价必被拒），
    #     所以窗口默认只给 6 小时，且**绝不退回更旧的**（那是 allow_stale 的语义）。
    if fresh_within_s and not allow_stale:
        cutoff = _time.strftime("%Y-%m-%d %H:%M:%S",
                                _time.localtime(_time.time() - float(fresh_within_s)))
        try:
            r = export_fetch(area_id, download=True, after=cutoff)
            if r.get("ready") and r.get("rows"):
                t = r.get("task", {}) or {}
                return _adopt(r, {"_source": "fresh_existing",
                                  "_note": "复用了 %s 已跑完的导出（%.1f 小时内），未触发新导出"
                                           % (t.get("createTime"), float(fresh_within_s) / 3600.0)})
        except BlacklightError:
            pass                                               # 查不到就当没有，继续走触发

    def _trigger():
        """触发导出。回 True=已起任务；False=被限流(该重试)。其它错原样抛。

        ⚠️限流的**首次**文案是误导性的「产品小姐姐走丢了,请联系研发小哥哥处理」，
          重试一次才显示真实原因「5分钟之内不可重复操作」（2026-08-04 实证，2026-08-06 复现）。
        ⚠️★**服务端说的"5分钟"不是它实际执行的**（2026-08-06 完整时间线实测）：
          窗口从**上次成功导出**起算，实测 **≥39.5 分钟、<40.7 分钟**（约 40 分钟）——
          13:06:17 成功后，13:30(24min)/13:41(35min)/13:45:40(39.5min) 三次全被拒，
          13:46:50(40.7min) 通过。**失败的尝试不会重置窗口**（13:41 连试 4 次后，13:46:50 照常通过）。
        ∴ 触发失败**不能干等**（会白等到超时），在等待循环里定期重试即可——重试无副作用。"""
        try:
            export_eligible(area_id, activity_id, batch_id, rule_id)
            return True
        except BlacklightError as e:
            if "重复" in str(e) or "走丢" in str(e):
                return False
            raise

    pend = _THR_PENDING.get(key)                               # ② 上次没等到的导出→接着等，别重复触发
    if pend:
        before, after_str, trigger_ts = pend
    else:
        # ★快照只记**已完成**的任务 id。不能连"进行中"的一起排除：撞 5 分钟限流时，正在跑的那个
        #   往往就是上一次调用触发的、我们要等的那一份——把它也排除掉就会死等到超时。
        snap = export_list(area_id, limit=20)["tasks"]
        before = [t["id"] for t in snap if t.get("status") == 1]
        trigger_ts = _time.time()
        # 再加一道**时间下界**兜底：只认 10 分钟内创建的任务。exclude_ids 防的是"历史旧导出"，
        # after 防的是"排除名单没盖住的旧任务"（如超过 limit=20 之外的）。两道都过才认领。
        after_str = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(trigger_ts - 600))
        _trigger()                                             # 返回值不可信(见下)，只登记待认领坐标
        _THR_PENDING[key] = (before, after_str, trigger_ts)

    deadline = _time.time() + wait_timeout
    last_try = _time.time()
    while _time.time() < deadline:
        # ★★**不信 `_trigger()` 的返回值，只信任务列表**（2026-08-06 实证）：
        #   `/apply/openness/apply/export` 回 `data:null, success:true`，**成功≠任务被创建**——
        #   已有任务在跑时它照样回 success 却静默不干活（13:49:44 触发回成功，列表里没有任何新任务）。
        #   ∴ 用"有没有 after 之后新建的任务"这个**可观测事实**驱动，没有就按 70s 节奏重试触发。
        #   这样两种失败都能自愈：被限流拒掉的、以及回了 success 却没起来的。
        fresh = [t for t in export_list(area_id, limit=20)["tasks"]
                 if t["id"] not in set(before) and str(t.get("createTime") or "") >= after_str]
        if not fresh and _time.time() - last_try >= 70:
            last_try = _time.time()
            _trigger()
        r = export_fetch(area_id, download=True, exclude_ids=before, after=after_str)   # ★只认新产出
        if r.get("ready") and r.get("rows"):
            m = _parse(r)
            meta = {"taskId": r.get("task", {}).get("id"),
                    "fileName": r.get("task", {}).get("fileName"),
                    "createTime": r.get("task", {}).get("createTime"),
                    "rowsInExport": len(r.get("rows") or []),
                    "skusWithThreshold": len(m),
                    "skusWithoutThreshold": len(r.get("rows") or []) - len(m),
                    "waitedSeconds": int(_time.time() - trigger_ts), "cached": False}
            _THR_CACHE[key] = (_time.time(), m, meta)
            _THR_PENDING.pop(key, None)
            return _ret(m, meta)
        _time.sleep(15)

    if allow_stale:                                            # 显式要旧值才给，且标明来源
        r = export_fetch(area_id, download=True)
        m = _parse(r) if r.get("ready") else {}
        return _ret(m, {"stale": True, "taskId": r.get("task", {}).get("id"),
                        "createTime": r.get("task", {}).get("createTime"),
                        "note": "★旧导出，可能不是本场次——门槛与漏报都不可信"})
    raise BlacklightError(
        f"导出未在 {wait_timeout}s 内完成（batch={batch_id}）。导出本身约需 9 分钟，"
        f"但触发有限流：窗口从**上次成功导出**起算，实测约 40 分钟（服务端嘴上说的『5分钟』不作数）。"
        f"∴ 最坏需要 ~50 分钟。稍后重跑本函数会**续等/续试同一场次**，不会重复排队。"
        f"确需旧清单请显式 allow_stale=True（有报错价/漏报风险）。")


def _run_price_fanout(price_one, items, concurrency: int, checkpoint: str = None,
                      label: str = "plan") -> tuple:
    """把「逐 SKU couponInfo 二分试算」跑成**长批量作业**：探针 / ETA / 熔断 / 断点续跑。

    ★**为什么不是裸 pmap**（2026-08-20）：这步是整个规划里唯一的 N 倍扇出——
      门槛已经由导出一次拿全、成本已经 `query_pricing_batch` 批量取，
      只有二分必须逐 SKU 问平台（导出给的是「上限是多少」，二分要问「报这个价到手价落哪」，
      那是每个 SKU 独有的券栈）。900 款 × 并发 8 会把平台打出「拼命加载中」再叠退避，
      **整批超时 = 0 产出**。换成 pmap_batch 后：超时也已落盘、可续跑、启动就有 ETA。

    `price_one` 须返回**带 `_tag`(A/review/C/err) 和 `skuId` 的 dict**、且永不抛
    （返回 tuple 会毁掉断点：JSON 往返把 tuple 变 list，key 取值不稳定）。
    `log` 收进列表而不是 print —— MCP 走 stdio，**打到 stdout 会污染协议**。
    返回 (buckets, meta)。"""
    logs = []
    r = pmap_batch(price_one, items, workers=max(1, int(concurrency or 1)),
                   checkpoint=(checkpoint or None),
                   key=lambda x: str((x or {}).get("skuId") or ""),
                   is_error=lambda x: isinstance(x, dict) and x.get("_tag") == "err",
                   label=label, log=logs.append)
    bk = {"A": [], "review": [], "C": [], "err": []}
    for row in r.get("rows") or []:
        row = dict(row)
        bk.get(row.pop("_tag", "err"), bk["err"]).append(row)
    meta = {"已完成": r.get("done"), "断点跳过": r.get("skipped_resumed"),
            "错误": r.get("errors"), "熔断": r.get("aborted"),
            "熔断原因": r.get("reason") or None, "耗时秒": r.get("seconds"),
            "ETA秒": r.get("eta_seconds"), "checkpoint": checkpoint or None,
            "进度": logs[-6:]}
    return bk, meta


def plan_seckill_enroll(area_id: int = None, activity_id: str = None, begin_time: str = None, rule_id: str = None,
                        target_margin: float = 0.05, duration=28, limit: int = None,
                        skus: list = None, use_threshold: bool = True, concurrency: int = 4,
                        checkpoint: str = None,
                        objective: str = "max_margin", erp_assistant: str = None,
                        sales_erps: str = None, reuse_since: str = None,
                        min_actual_price: float = None,
                     adv_source: str = "auto") -> dict:
    """★秒杀批量报名**规划**（只读、零写，复用百补方法）：本人可报(list_eligible服务端按ERP过滤·可报=未报内建去重)
    → **导出清单取报名价格门槛** → couponInfo 平台真值定价(保 target_margin·**到手价≤门槛**·报名价不 overshoot) → 分桶。
    area_id/activity_id/rule_id 缺则取 config(313601/101666814/ruleId)；begin_time='YYYY-MM-DD HH:00:00'(场次,必填,自动换 batchId)。
    A 行已带 promoQty=config默认5000(可直接喂 table_apply)。use_threshold=False 跳过门槛(不推荐,会撞卡控)。
    **skus 给定时短路、不整池翻页**(2026-08-03 修，旧实现翻完 4996 条再过滤，慢且易撞瞬时错误)。
    **objective**: min_price(默认,毛利率恰好=target) / max_margin(到手价顶门槛,毛利最大) —— 见 solve_seckill_price。

    ★**归属**（铁律8）：`erp_assistant`(采销助理,默认当前登录人)是**取数**口径、范围宽；
      `sales_erps`(销售员)才是用户说的"**我名下的**"，实测仅占 7.1%。**报名务必传 `sales_erps`**，
      否则会把同店其他采销的货一起报了。
    ★`reuse_since='YYYY-MM-DD HH:MM:SS'`：复用该时刻后已完成的导出取门槛，**不触发新导出**
      （触发窗口约 40 分钟，见 `_trigger`）。
    ★`min_actual_price`：**到手价下限**（业务筛选，如只报到手价≥9.9的）。在定价完成后过滤，
      不参与二分——低于该价的 SKU 归入 C 桶并注明原因，不会静默消失。
    A 行键名双写：`enroll_price/actual_price/margin`(与 solve_* 一致) + 中文键(兼容) + `promoPrice/promoQty`(喂 table_apply)。
    review 桶带 `ub_margin`/`ub_verdict`(京喜零承担上界)，低于 target 即确定性出局。
    返回 {A_biddable, review_platform, C_infeasible, errors, summary, batchId}。
    ★`adv_source`（2026-08-11）：成本里的广告项来源。默认 `auto` = **ge 近 7 日实际单均广告**；
      `osw24h` 退回 osw 的 24 小时快照（分母小会炸、无广告订单会记 0 ⇒ 高估毛利）。
      做 A/B 归因时用它把「advCost 修复」与「占坑/状态变化」分开。见 `osw/adv.py`。

    ## ★耗时与超时（2026-08-18 实操教训，走 MCP 必看）
    整池规划 = 翻页 + **触发导出并等它跑完** + 并发试算。导出实测 4726 条约 **9 分钟**，
    远超 MCP 工具的空闲超时 ⇒ **直接走 MCP 调用大概率被判超时杀掉**（实测两次：全量、
    limit=30 都卡到 1800s 被 abort）。三条应对，按优先级：
      1. **已经导过一份就复用**：传 `reuse_since='YYYY-MM-DD HH:MM:SS'`（该时刻之后已完成的导出）。
         实测同一批规划从 1800s 超时 → **44 秒完成**。这是最有效的一招。
      2. **走 Python 层后台跑**，别走 MCP（大批量取数本来就该走 Python，见 SKILL 铁律 10）。
      3. 探针用 `limit`：**limit 已在分页循环内短路**（2026-08-18 修）；
         此前切片在翻页之后 ⇒ limit 只减试算量、不减翻页，探针和全量一样慢，
         会让人误判"瓶颈在试算"而反复调 concurrency。
    ⚠️`reuse_since` 与 `allow_stale` 不同：前者是**你明确指定新鲜度下界**，找不到符合条件的
      仍走正常触发流程，绝不悄悄降级到旧导出（旧导出门槛会变：实测 274 条已变、121 条旧值更高
      ⇒ 照旧值报价必被拒）。

    """
    from blacklight.osw import margin as _mt
    from blacklight.core import scene_cfg
    _mscfg = scene_cfg("ms")
    area_id = area_id or _mscfg.get("seckill_area_id", 313601)          # 缺则取 config
    activity_id = activity_id or _mscfg.get("seckill_activity_id")
    if not begin_time:
        raise BlacklightError("plan_seckill_enroll 需 begin_time（场次 'YYYY-MM-DD HH:00:00'，须≥T+3）")
    rid = rule_id or _mscfg.get("seckill_rule_id") or get_rule_id(area_id, activity_id)
    batch = get_batch_id(area_id, begin_time)
    batch_id = batch.get("batchId") if isinstance(batch, dict) else batch
    if not batch_id:
        return {"error": f"取不到 batchId（场次 {begin_time} 可能未开放，须≥T+3）", "batch_raw": batch}
    # ★给定 skus 时**短路**，不整池翻页（2026-08-03 修）：旧实现先翻完全池(实测 4996 条/100 页)再过滤，
    #   既慢又常撞服务端瞬时错误「查询标签选品可报SKU列表拼命加载中」。价格从 ware_detail 取。
    if skus:
        # 只带 skuId：京东价/名称由下面的 query_pricing_batch 回填(benchPrice)，不额外打接口
        items = [{"skuId": str(s).strip()} for s in skus if str(s).strip()]
    else:
        items, page = [], 1                                   # 全量可报（分页）；传 sales_erps 才是"我名下"
        while True:
            el = list_eligible(batch_id, area_id, rid, erp_assistant=erp_assistant,
                               sales_erps=sales_erps, page=page, page_size=50)
            items.extend(el["items"])
            # ★2026-08-18 修：`limit` 提前到**分页循环内**短路。旧实现先翻完整池再 items[:limit]，
            #   于是 limit 只减少试算量、**一点不减翻页耗时** —— 实测 limit=30 与全量同样卡到超时，
            #   让人误判"瓶颈在试算"而反复调 concurrency。探针要的就是"快",这才对得上探针的用途。
            if limit and len(items) >= limit:
                break
            if page * 50 >= (el.get("totalCount") or 0) or not el["items"]:
                break
            page += 1
    if limit:
        items = items[:limit]
    # ★2026-08-24：剔除**已知资料问题**（短标题非法字符/主推校验），它们改价没用、报了必被拒。
    #   来源是历次 recover_thresholds 的 others（见 record_material_issues）。
    #   ⚠️不是静默丢弃——单独成桶输出，让人看得见"这些该去修资料"。
    _mat = material_issues()
    material_blocked = [{"skuId": str(it["skuId"]),
                         "name": (it.get("skuName") or "")[:36],
                         "reason": (_mat.get(str(it["skuId"])) or {}).get("reason"),
                         "首次出现": (_mat.get(str(it["skuId"])) or {}).get("first_seen"),
                         "累计被拒": (_mat.get(str(it["skuId"])) or {}).get("hits")}
                        for it in items if str(it["skuId"]) in _mat]
    if material_blocked:
        _blocked = {x["skuId"] for x in material_blocked}
        items = [it for it in items if str(it["skuId"]) not in _blocked]
    # ★门槛必须来自**本场次刚触发**的导出（旧实现会静默用旧导出，见 seckill_thresholds docstring）。
    #   取不到就抛——宁可整批失败，也不拿旧门槛去报价。
    thr_meta = {}
    if use_threshold:
        _t = seckill_thresholds(area_id, activity_id, batch_id, rid, with_meta=True,
                                reuse_since=reuse_since)
        thr_map, thr_meta = _t["thresholds"], {k: v for k, v in _t.items() if k != "thresholds"}
    else:
        thr_map = {}
    cost_map = _mt.query_pricing_batch([str(it["skuId"]) for it in items], adv_source=adv_source)   # 批量取成本(省N倍往返)

    def _price_one(it):                                       # 单SKU试算(只读)，并发跑；**全程 try→永不抛(不丢批)**
        s = ""
        try:
            s = str(it["skuId"])
            pr = cost_map.get(s) or _mt.query_pricing(s)      # 批量命中优先，缺失回退单查
            jd = _f(it.get("pPrice"))
            if jd is None:                                    # skus 短路路径没有 pPrice → 用监控基价
                jd = _f(pr.get("benchPrice"))
            row = {"skuId": s, "name": (it.get("skuName") or pr.get("skuName") or "")[:36],
                   "京东价": jd, "全成本": pr["fullCost"], "门槛": thr_map.get(s)}
            if use_threshold and thr_map.get(s) is None:
                # ★★**导出清单门槛列为空 ≠ 平台没有门槛**（2026-08-06 真报实证，推翻了先前的假设）：
                #   352 款报名里 60 款被拒，其中 **47 款是"到手价高于报名价格门槛"，且 47/47 全部来自
                #   导出清单无门槛的那批；导出有门槛的 204 款零卡控**。⇒ 空白是**导出的数据缺口**，
                #   平台自己有门槛且严格执行。本行等于**没有上限保护，提交时大概率被拒**。
                #   好消息：拒绝文案会直接给出真实门槛（"预估到手价:X高于报名价格门槛:Y"），
                #   可据此重解价格补报（实测 47 款中 29 款重解后成功、18 款按真实门槛保不住毛利）。
                row["警告"] = "导出未给报名价上限≠无门槛，本行未施加约束，提交可能被拒(拒绝文案会给真实门槛)"
            if jd is None:
                row["reason"] = "无京东价"; row["_tag"] = "C"; return row
            # 每 SKU 独立 client(线程间不共享=安全；client=None→solve 内自建自闭·SKU内复用保留L1)
            r = solve_seckill_price(s, area_id, batch_id, pr["fixedCost"], pr["cpsRate"], jd,
                                    target_margin=target_margin, duration=duration,
                                    threshold=thr_map.get(s), objective=objective)
            if r["ok"] and min_actual_price is not None and _f(r["actual_price"]) is not None \
                    and _f(r["actual_price"]) < float(min_actual_price) - 1e-9:
                # 到手价下限是**业务筛选**，不进二分。放 C 桶并写明白，别让它从结果里凭空消失。
                row.update({"到手价": r["actual_price"], "毛利%": round(r["margin"] * 100, 1),
                            "reason": "到手价%.2f<下限%.2f" % (r["actual_price"], float(min_actual_price))})
                row["_tag"] = "C"; return row
            if r["ok"]:
                # ★键名双写(2026-08-03)：`enroll_price/actual_price/margin` 与底层 solve_* 一致(跨场域通用读取)；
                #   中文键保留向后兼容；`promoPrice/promoQty` 可直接喂 table_apply。
                row.update({"报名价": r["enroll_price"], "到手价": r["actual_price"],
                            "毛利%": round(r["margin"] * 100, 1),
                            "enroll_price": r["enroll_price"], "actual_price": r["actual_price"],
                            "margin": round(r["margin"] * 100, 1),
                            "promoPrice": r["enroll_price"],
                            "promoQty": _mscfg.get("default_promo_qty", 5000)})
                row["_tag"] = "A"; return row
            if any((x.get("reward") or 0) - (x.get("jxReward") or 0) > 0.5
                   for x in pr["promotions"] + pr["coupons"]):
                # ★给出**上界**供直接判定(2026-08-03)：假设京喜零承担时的毛利率。低于 target 即确定性出局，
                #   不必再人工按京喜口径算一遍（此前人工重算两次都算错）。
                cap = _f(thr_map.get(s)) or jd
                ubm = (1 - pr["cpsRate"] - pr["fixedCost"] / cap) if cap else None
                row["ub_margin"] = round(ubm * 100, 1) if ubm is not None else None
                row["ub_verdict"] = ("上界仍<目标⇒确定性出局" if (ubm is not None and ubm < target_margin)
                                     else "上界达标⇒值得人工按京喜口径复核")
                row["reason"] = r["reason"] + "（含平台出资券，京喜口径或可报）"
                row["_tag"] = "review"; return row
            row["reason"] = r["reason"]; row["_tag"] = "C"; return row
        except Exception as e:
            return {"skuId": s, "error": str(e)[:80], "_tag": "err"}
    # ★长批量作业：探针/ETA/熔断/断点（裸 pmap 整批超时=0产出，见 _run_price_fanout）
    _bk, _run = _run_price_fanout(_price_one, items, concurrency, checkpoint, label="seckill.plan")
    A, review, C, err = _bk["A"], _bk["review"], _bk["C"], _bk["err"]
    return {"A_biddable": A, "review_platform": review, "C_infeasible": C, "errors": err,
            "资料问题(已剔除)": material_blocked,
            "batchId": batch_id, "ruleId": rid, "跑批": _run,
            "summary": {("本人可报" if (sales_erps or erp_assistant)
                         else "全量可报(未按人筛←sales_erps 未传)"): len(items), "可报名A": len(A), "待复核(平台券)": len(review),
                        "不可行C": len(C), "错误": len(err),
                        "资料问题(已剔除·改价没用)": len(material_blocked), "场次": begin_time,
                        "门槛来源": (("%s task=%s (%s)｜清单%s行/有门槛%s/无门槛%s"
                                      % ({"reuse_since": "复用(显式指定)", "fresh_existing": "复用已有导出(6h内现成的)"}
                                            .get(thr_meta.get("_source"), "本场次新导出"),
                                         thr_meta.get("taskId"), thr_meta.get("createTime"),
                                         thr_meta.get("rowsInExport"), thr_meta.get("skusWithThreshold"),
                                         thr_meta.get("skusWithoutThreshold")))
                                     if thr_meta else "未取(use_threshold=False,有卡控风险)"),
                        "命中门槛": sum(1 for x in items if thr_map.get(str(x["skuId"])) is not None),
                        "无门槛(未施加上限约束)": sum(1 for x in items if thr_map.get(str(x["skuId"])) is None) if use_threshold else None,
                        "口径": ("couponInfo平台真值+二分(只依赖单调不假设线性);毛利=客户到手价口径(保守);"
                                 + ("**到手价≤报名价格门槛(硬约束)**;" if use_threshold
                                    else "**未取门槛⇒未施加上限约束(报名可能被卡控拒)**;")
                                 + "目标%.0f%%" % (target_margin * 100))}}


def plan_baoyou_enroll(area_id: int, activity_id: str, begin_time: str, rule_id: str = None,
                       target_margin: float = 0.05, duration=30, channel: str = "baoyou",
                       limit: int = None, skus: list = None, concurrency: int = 4,
                       objective: str = "max_margin", checkpoint: str = None,
                     adv_source: str = "auto") -> dict:
    """★**便宜包邮/特价 批量报名规划**（只读、零写）—— 与秒杀**完全同套 couponInfo 二分真值定价**，唯一差异：
    **报名价上限 = list_eligible 的 `opennessMinPrice`（= 该SKU在前置收品池的提报价×折扣率，2026-07-28 由平台报错定案；卡死到分，高0.01即被拒）**、促销时长按天(duration=30)、channel=baoyou/tejia。
    归属/去重同秒杀(list_eligible 服务端按ERP过滤·可报=未报)。begin_time 换 batchId；rule_id 缺自动取。
    返回 {A_biddable(可报名+报名价), review_platform, C_infeasible, batchId, summary}。报名走 table_apply(channel)。
    ★`adv_source`（2026-08-11）：成本里的广告项来源。默认 `auto` = **ge 近 7 日实际单均广告**；
      `osw24h` 退回 osw 的 24 小时快照（分母小会炸、无广告订单会记 0 ⇒ 高估毛利）。
      做 A/B 归因时用它把「advCost 修复」与「占坑/状态变化」分开。见 `osw/adv.py`。
    """
    from blacklight.osw import margin as _mt
    rid = rule_id or get_rule_id(area_id, activity_id)
    batch = get_batch_id(area_id, begin_time)
    batch_id = batch.get("batchId") if isinstance(batch, dict) else batch
    if not batch_id:
        return {"error": f"取不到 batchId（{begin_time}）", "batch_raw": batch}
    items, page = [], 1
    while True:
        el = list_eligible(batch_id, area_id, rid, page=page, page_size=50)
        items.extend(el["items"])
        if page * 50 >= (el.get("totalCount") or 0) or not el["items"]:
            break
        page += 1
    if skus:
        want = {str(s).strip() for s in skus}
        items = [it for it in items if str(it["skuId"]) in want]
    if limit:
        items = items[:limit]
    cost_map = _mt.query_pricing_batch([str(it["skuId"]) for it in items], adv_source=adv_source)   # 批量取成本

    def _price_one(it):                                       # 单SKU试算(只读)，并发跑；**全程 try→永不抛(不丢批)**
        s = ""
        try:
            s = str(it["skuId"]); jd = _f(it.get("pPrice")); thr = _f(it.get("threshold"))
            pr = cost_map.get(s) or _mt.query_pricing(s)
            # 便宜包邮/特价 opennessMinPrice=**报名价上限**(=前置池提报价×折扣率；不是到手价上限，2026-07-28 平台报错定案)
            row = {"skuId": s, "name": (it.get("skuName") or "")[:36], "京东价": jd,
                   "全成本": pr["fullCost"], "报名价上限": thr}
            if thr is None:
                # 门槛缺失 ⇒ 下面 solve 的 threshold=None ⇒ **上限约束不生效**。不拦(平台本来就有一部分不给上限)，
                # 但必须标出来，否则跟"有门槛且已满足"长得一模一样——这正是 2026-08-06 查出的静默失效点。
                row["警告"] = "平台未给报名价上限，本行未施加上限约束"
            if jd is None:
                row["reason"] = "无京东价"; row["_tag"] = "C"; return row
            r = solve_seckill_price(s, area_id, batch_id, pr["fixedCost"], pr["cpsRate"], jd,
                                    target_margin=target_margin, duration=duration, threshold=thr,
                                    objective=objective)
            if r["ok"]:
                row.update({"报名价": r["enroll_price"], "到手价": r["actual_price"],
                            "毛利%": round(r["margin"] * 100, 1)}); row["_tag"] = "A"; return row
            if any((x.get("reward") or 0) - (x.get("jxReward") or 0) > 0.5
                   for x in pr["promotions"] + pr["coupons"]):
                row["reason"] = r["reason"] + "（含平台出资券，京喜口径或可报，人工复核）"; row["_tag"] = "review"; return row
            row["reason"] = r["reason"]; row["_tag"] = "C"; return row
        except Exception as e:
            return {"skuId": s, "error": str(e)[:80], "_tag": "err"}
    _bk, _run = _run_price_fanout(_price_one, items, concurrency, checkpoint,
                                  label="%s.plan" % channel)
    A, review, C, err = _bk["A"], _bk["review"], _bk["C"], _bk["err"]
    return {"A_biddable": A, "review_platform": review, "C_infeasible": C, "errors": err,
            "batchId": batch_id, "ruleId": rid, "channel": channel, "跑批": _run,
            "summary": {("本人可报" if (sales_erps or erp_assistant)
                         else "全量可报(未按人筛←sales_erps 未传)"): len(items), "可报名A": len(A), "待复核(平台券)": len(review),
                        "不可行C": len(C), "错误": len(err), "场次": begin_time, "channel": channel,
                        "无报名价上限(未施加约束)": sum(1 for x in items if _f(x.get("threshold")) is None),
                        "口径": "couponInfo平台真值+二分;**报名价≤opennessMinPrice(=前置池提报价×折扣率,卡死到分)**;毛利=客户到手价口径;目标%.0f%%" % (target_margin * 100)}}


# =========================================================================== #
# 导出「可提报清单」（异步导出整份可报 SKU + 报名价格门槛）—— 批量报名的上游
#   ① POST /apply/openness/apply/export        触发导出（异步起任务，不回文件）
#   ② POST /apply/openness/apply/queryExportList 查任务进度 + 下载直链
# =========================================================================== #
_EXPORT_DISP_FIELDS = ["itemSkuId", "skuName", "skuMainPic", "pPrice", "spuId",
                       "spuLowestPriceD30", "skuD7DealSaleQtty", "stockQtty", "suggestPrice",
                       "itemFirstCateName", "itemSecondCateName", "itemThirdCateName",
                       "l2BrandCode31895", "mainBrandCode", "mainBarndnameFull"]


def export_eligible(area_id: int, activity_id: str, batch_id: int, rule_id: str,
                    erp_assistant: str = None, sales_erps: str = None, activity_duration: int = 28,
                    business_type: int = 122) -> dict:
    """触发导出「可提报清单」（POST /apply/openness/apply/export）。异步起任务，只回 success；

    ★★**`success:true` ≠ 任务被创建**（2026-08-06 实证）：接口回 `data:null, success:true`，不含 jobId；
      **已有任务在跑时它照样回 success 却静默不干活**（13:49:44 触发返回成功，任务列表里没有任何新任务）。
      ∴ 判断"到底起没起来"**只能查任务列表**（`export_list` 里有没有 createTime 更新的新条目），
      不能信本函数的返回值。`seckill_thresholds` 已按这个事实驱动重试。

    用 export_list / export_fetch 拿下载直链。business_type：秒杀=122（便宜包邮/特价各不同）。

    ⏱**耗时约 9 分钟**（2026-08-06 实测 4726 条，进度 3%→100%），不是旧文档写的"1~2 分钟"。
      轮询超时设小了会退回旧导出（正是 `seckill_thresholds` 那个坑），取门槛请直接用 `seckill_thresholds`。
    **erp 留空默认当前登录用户**。selectQueryJson 内层 = list_eligible 的查询体。

    ⚠️**有节流，且首次撞上时的错误文案是误导性的**（2026-08-04 实证，2026-08-06 复现）：
    服务端回**通用文案**「产品小姐姐走丢了,请联系研发小哥哥处理」，**重试一次才显示真实原因**
    「5分钟之内不可重复操作」。看到"走丢了"别当接口坏了/别去查登录态。

    ⚠️★**别信"5分钟"这个数**（2026-08-06 完整时间线实测推翻）：真实窗口从**上次成功导出**起算，
      实测 **≥39.5 分钟、<40.7 分钟**（约 40 分钟）：13:06:17 成功后，24/35/39.5 分钟三次触发全被拒，
      40.7 分钟时通过。**失败的尝试不会重置窗口**（35 分钟处连试 4 次，不影响 40.7 分钟处通过）。
    ∴ 调用方不能"触发一次失败就干等"（会白等到超时），定期重试即可、无副作用。
      取秒杀门槛请直接用 `seckill_thresholds`：它会先看是否已有任务在跑，没有则每 70s 重试触发。
      **规划一整场秒杀报名前请预留 ~50 分钟**（最坏 40 分钟等窗口 + 9 分钟跑导出）。"""
    erp_assistant, sales_erps = _resolve_erp(erp_assistant, sales_erps)
    select = {"currentPage": 1, "pageSize": 20, "businessType": int(business_type),
              "dispFields": _EXPORT_DISP_FIELDS, "batchId": str(batch_id), "areaId": int(area_id),
              "ruleFields": [], "dimType": 1, "ruleType": 0,
              "filterFields": _build_filter(erp_assistant, sales_erps, rule_id),
              "sortFields": [{"ruleContent": []}]}
    body = {"areaId": int(area_id), "activityId": str(activity_id),
            "selectQueryJson": _json.dumps(select, ensure_ascii=False),
            "itemLevel": 0, "activityDuration": int(activity_duration), "batchId": int(batch_id)}
    with _client() as client:
        _post(client, "/apply/openness/apply/export", body)
    return {"triggered": True, "areaId": int(area_id), "activityId": str(activity_id),
            "note": "导出任务已提交（异步，**约9分钟**，非旧文档的1~2分钟）。用 ms_export_fetch(带 exclude_ids/after) 取，"
                    "否则会拿到旧导出；取秒杀门槛请直接用 seckill_thresholds（已内建认领+续等）。"}


def _parse_export_task(it: dict) -> dict:
    return {"id": it.get("id"), "fileName": it.get("fileName"),
            "status": it.get("exportStatus"),          # -1刚触发/0进行中 / 1完成
            "progress": it.get("exportProgress"),
            "fileAddress": it.get("fileAddress") or "", "fileSize": it.get("fileSize"),
            "createTime": it.get("createTime"), "creator": it.get("creator")}


# --------------------------------------------------------------------------- #
# 导出**已报名**商品（与「导出可报名」是两套端点，2026-07-28 由用户提供 curl 补全）
#   ① POST /apply/openness/applied/export/v2   {areaId, activityId, applySkuType}  触发（异步，约10秒）
#   ② POST /apply/openness/async/task/list     {taskType:"OPENNESS_APPLIED_EXPORT_TASK_TEMPLATE",
#                                               bussinessKey:"<areaId>", areaId}   查任务（注意 bussinessKey 是平台拼写）
#      → [{id, taskStatus(1完成), taskBeginTime/EndTime, pin, ext:{fileName, fileAddress, fileSize}}]
#   产物 .zip，内含**老式 .xls**（需 xlrd），列 13：
#     报名编号/商品信息/skuId/spuId/是否主推/促销数量/促销id/短标题/促销价格/秒杀到手价/价格预警/
#     活动时长/审核进度/驳回原因/促销开始时间/促销状态/…（2026-08-24 实测真列名；**没有「报名人」**）
#   ★比 get_applied 翻页强得多：一次拿全，且带**审核进度/驳回原因/价格预警/秒杀到手价**（翻页接口没有），
#     翻页接口都没有；而且 get_applied 深翻页会静默丢页（实证 8099 只取回 4837）。
# --------------------------------------------------------------------------- #
APPLIED_EXPORT_TASK_TYPE = "OPENNESS_APPLIED_EXPORT_TASK_TEMPLATE"

# ★★平台「已报名导出」有 **50000 条**行数上限，超了任务直接 status=2 无文件（2026-08-24 用户告知并实测）。
#   实测该活动 totalCount=**137716**，是上限的 2.8 倍 ⇒ 这条路**结构性走不通**，不是偶发。
#   历史印证：近 6 次任务 5 次 status=2，唯一成功的是 **2025-04-05**（那时记录还少）。
#   而且只会越积越多（一个 SKU 跨场次常有 17~42 条记录）⇒ 不会自愈。
#   ∴ 别再每次去撞一次（每次白等 6~10 秒还报一个误导性的错），**先探量、超了直接走 page 路**。
APPLIED_EXPORT_ROW_CAP = 50000


def applied_export_feasible(activity_id: str, area_id: int, apply_sku_type: str = "3",
                            begin_time: str = None, end_time: str = None) -> dict:
    """「已报名导出」这条路当前能不能用 —— 只看行数是否超平台 50000 上限。一次轻量分页探量。

    ★带上 `begin_time`/`end_time` 探的就是**加了时间窗之后**的量（这才是导出实际会跑的范围）。
      实测：全活动 137716（必失败）→ 未来30天 6492 → 单场次 713。
    """
    try:
        d = get_applied(activity_id, area_id, apply_sku_type, page=1, page_size=1,
                        begin_time=begin_time, end_time=end_time)
        n = int(d.get("totalCount") or 0)
    except Exception as e:
        return {"ok": True, "totalCount": None, "why": "探量失败(%s)，不拦" % str(e)[:60]}
    return {"ok": n <= APPLIED_EXPORT_ROW_CAP, "totalCount": n, "cap": APPLIED_EXPORT_ROW_CAP,
            "window": [begin_time, end_time],
            "why": ("已报名 %d 条 > 上限 %d ⇒ 导出必失败(status=2)。**先加 beginTime/endTime 收窄**"
                    "（实测全活动 137716→未来30天 6492→单场次 713）；收不窄才走 page 路"
                    % (n, APPLIED_EXPORT_ROW_CAP)) if n > APPLIED_EXPORT_ROW_CAP else ""}


def export_applied(area_id: int, activity_id: str, apply_sku_type: str = APPLY_SKU_TYPE_JX,
                   begin_time: str = None, end_time: str = None) -> dict:
    """触发导出「**已报名**商品」（POST /apply/openness/applied/export/v2）。异步，约 10 秒出文件。

    ★★**必须带 `begin_time`/`end_time`**（2026-08-24，用户点破）：不带就是全活动，
      该活动 137716 条 > 平台 **50000 行上限** ⇒ 任务必 `status=2 无文件`，且**不会自愈**
      （记录只增不减）。加上场次时间窗后实测 **713 条、一次就成**（id=49502060）。
    ⚠️字段名是 `beginTime`/`endTime`（按**场次开始时间**筛），不是 `startTime`——
      后者语义不同（137716→136860，几乎没筛掉）。ERP 维度服务端**不支持**
      （`erp`/`pin`/`applyErp`/`createPin`/`salesErp` 五个候选名全被忽略，
      已用瞎编字段做阴性对照确认判据有效）。
    随后用 `applied_task_list()` 拿 fileAddress，或直接 `fetch_applied_export()` 一步到位。"""
    body = {"areaId": int(area_id), "activityId": str(activity_id), "applySkuType": str(apply_sku_type)}
    if begin_time:
        body["beginTime"] = str(begin_time)
    if end_time:
        body["endTime"] = str(end_time)
    with _client() as client:
        _post(client, "/apply/openness/applied/export/v2", body)
    return {"triggered": True, "areaId": int(area_id), "activityId": str(activity_id),
            "note": "已报名导出任务已提交（异步，约10秒）。用 fetch_applied_export 取并解析。"}


def applied_task_list(area_id: int, limit: int = 10) -> list:
    """查「已报名导出」任务列表（POST /apply/openness/async/task/list）。倒序。
    ⚠️body 的 key 是 **`bussinessKey`**（平台拼写，少一个 i），传字符串形式的 areaId。"""
    body = {"taskType": APPLIED_EXPORT_TASK_TYPE, "bussinessKey": str(area_id), "areaId": int(area_id)}
    with _client() as client:
        d = _post(client, "/apply/openness/async/task/list", body)
    out = []
    for t in (d or [])[:limit]:
        ext = t.get("ext") or {}
        out.append({"id": t.get("id"), "status": t.get("taskStatus"),      # 1=完成
                    "begin": t.get("taskBeginTime"), "end": t.get("taskEndTime"),
                    "pin": t.get("pin"), "fileName": ext.get("fileName"),
                    "fileAddress": ext.get("fileAddress"), "fileSize": ext.get("fileSize")})
    return out


def fetch_applied_export(area_id: int, activity_id: str = "", trigger: bool = False,
                         wait_timeout: int = 120, out_dir: str = None,
                         allow_stale: bool = False,
                         apply_sku_type: str = APPLY_SKU_TYPE_JX,
                         begin_time: str = None, end_time: str = None) -> dict:
    """取**已报名**导出并解析成行。trigger=True 先触发一份新的（需 activity_id），否则用最新已完成的。
    返回 {rows:[dict], columns:[...], fileName, count}。产物是 zip→xls，需 `xlrd`。

    ★★**2026-08-06 修：trigger=True 时会静默返回旧文件**（与 `seckill_thresholds` 同一个坑，
      就在本文件里，上次修那个时漏了这个兄弟函数）。
      旧实现的等待循环判据是「最新任务 status==1 且有 fileAddress」——**历史已完成任务永远满足**，
      所以循环第一轮就 break，然后 `tasks[0]` 取到的还是那份旧的。
      实证：触发后新任务 `status=2`（服务端失败、无文件），函数**返回了 16 个月前的 2025-04-05 导出**，
      行数/列名都正常，完全看不出异常。
      现改为：先记下触发前已完成的任务 id 快照，只认**不在快照里**的新任务；
      新任务失败(status=2)立即抛错；超时也抛错，**绝不退回旧文件**（`allow_stale=True` 才给）。"""
    before_ids = set()
    if trigger:
        if not str(activity_id).strip():
            raise BlacklightError("trigger=True 需要 activity_id")
        # ★★快照必须记**所有**任务 id，不能只记 status==1（2026-08-24 修）：
        #   历史失败任务(status=2)不会消失，只记完成态会让它们变成"不在快照里的新任务"，
        #   下一轮轮询立刻判「我刚触发的这次失败了」并抛错 —— **假失败**。
        #   实测：本次触发的 49495566 六秒后就成功了(713 行)，却被 11:45 的旧失败任务
        #   49488039 顶掉，还照着话术报"超 50000 上限"，把调用方白推去 223s 的降级 page 路。
        before_ids = {str(t.get("id")) for t in applied_task_list(area_id, limit=20)}
        export_applied(area_id, activity_id, apply_sku_type,
                       begin_time=begin_time, end_time=end_time)
        deadline = _time.time() + wait_timeout
        got = None
        while _time.time() < deadline:
            _time.sleep(6)
            fresh = [t for t in applied_task_list(area_id, limit=20)
                     if str(t.get("id")) not in before_ids]
            done = [t for t in fresh if t.get("status") == 1 and t.get("fileAddress")]
            if done:
                got = done[0]
                break
            failed = [t for t in fresh if t.get("status") == 2]
            if failed:
                # ★别拿"看起来合理"的机制去解释失败（这条错误文案自己就犯过）：
                #   status=2 不带原因，**当场探一次量**再说是哪一种。实证两种都见过：
                #   窗口内 0 条（平台不产出空文件，2026-08-24 拿 9-05 空窗口复现）／超 50000 上限。
                _fz = applied_export_feasible(activity_id, area_id, apply_sku_type,
                                              begin_time, end_time)
                _n = _fz.get("totalCount")
                if _n == 0:
                    _why = ("**该时间窗内已报名 0 条**（%s ~ %s）——平台不产出空文件，"
                            "这不是故障，换个有记录的场次时间窗即可。" % (begin_time, end_time))
                elif _n is not None and _n > APPLIED_EXPORT_ROW_CAP:
                    _why = ("**窗口内 %d 条 > 平台 %d 行上限**⇒必失败且不会自愈。"
                            "把 begin_time/end_time 收窄到单场次（实测 8-27 单场次 713 条一次就成）。"
                            % (_n, APPLIED_EXPORT_ROW_CAP))
                else:
                    _why = ("窗口内 %s 条，未超上限也非空 ⇒ **原因未知**，别急着编机制："
                            "先重试一次（3 分钟限流），仍失败再查平台侧。" % _n)
                raise BlacklightError(
                    f"『已报名导出』任务失败（id={failed[0].get('id')}，status=2 无文件）。" + _why +
                    "**不会退回旧导出**——旧文件可能是几个月前的，据此判重会漏报或重复报名。")
        if got is None and not allow_stale:
            raise BlacklightError(
                f"『已报名导出』{wait_timeout}s 内未产出新文件。**不退回旧导出**（会拿到过期数据）；"
                "确需旧文件请显式 allow_stale=True。")
        if got is not None:
            task = got
        else:
            task = None
    else:
        task = None

    if task is None:
        tasks = [t for t in applied_task_list(area_id, limit=20)
                 if t.get("status") == 1 and t.get("fileAddress")]
        if not tasks:
            raise BlacklightError("没有已完成的『已报名导出』任务；用 trigger=True 先触发。")
        task = tasks[0]
    import urllib.request as _u, zipfile as _z, tempfile as _tmp
    out_dir = out_dir or _tmp.mkdtemp()
    zp = os.path.join(out_dir, f"applied_{area_id}_{task['id']}.zip")
    with open(zp, "wb") as f:
        f.write(_u.urlopen(task["fileAddress"], timeout=180).read())
    zf = _z.ZipFile(zp)
    inner = zf.extract(zf.namelist()[0], out_dir)
    grid = _read_grid(inner)                       # [(表头...), (行...), ...]
    if not grid:
        return {"fileName": task.get("fileName"), "taskId": task.get("id"), "count": 0,
                "columns": [], "rows": [], "path": inner}
    cols = [str(c).strip() for c in grid[0]]
    rows = [dict(zip(cols, [("" if v is None else v) for v in r])) for r in grid[1:]]
    return {"fileName": task.get("fileName"), "taskId": task.get("id"), "end": task.get("end"),
            "count": len(rows), "columns": cols, "rows": rows, "path": inner}


def export_list(area_id: int, limit: int = 10) -> dict:
    """查导出任务列表（POST /apply/openness/apply/queryExportList），倒序。
    返回每条：id/fileName/status(-1刚触发/0进行中/1完成)/progress/fileAddress(签名直链)/createTime。
    body 带 id:""（便宜包邮/特价必需，秒杀无害）。便宜包邮导出产物是 .zip(内含清单)，秒杀是 .xlsx。"""
    with _client() as client:
        d = _post_list(client, "/apply/openness/apply/queryExportList",
                       {"id": "", "areaId": int(area_id)})
    rows = [_parse_export_task(it) for it in (d or [])][:limit]
    return {"count": len(rows), "tasks": rows}


def export_fetch(area_id: int, download: bool = True, out_path: str = None,
                 min_progress: int = 100, after: str = None, exclude_ids=None) -> dict:
    """取**最新已完成**的导出任务，可选下载 xlsx 并解析出每 SKU 的【报名价格门槛】。
    download=True 时把签名直链下到本地并 best-effort 解析门槛列（含'门槛'的列 + SKU 列）。

    ⚠️★**必须用 `after` 或 `exclude_ids` 锁定"你刚触发的那一个"**（2026-08-04 血的教训）：
      本函数返回的是"最新**已完成**"的任务。你刚 `export_eligible()` 触发的那个通常还在跑(status=0)，
      此时它会**静默返回上一次的旧导出**——文件名/行数都正常，看不出异常。
      实证：为 08-07 场触发导出后立刻轮询，拿到的是**昨天 08-06 场**的清单
      (`秒杀可报名商品_..._202608032032.xlsx`)，据此报了 890 条，其中 119 条因"已报过名"被拒
      （那批 SKU 对 08-06 可报、但早已报过 08-07），且**漏评估了约 620 个 08-07 真正可报的 SKU**。
      所幸门槛价跨场次基本稳定（180 条完全一致、14 条微差、**0 条超标**），没造成报价违规。
    用法：
      `before = {t["id"] for t in ms.export_list(area)["tasks"]}` → 触发导出 →
      轮询 `ms.export_fetch(area, exclude_ids=before)`（或 `after="2026-08-04 09:35:00"`）。
      拿不到就是还没跑完，**继续等，别接受旧的**。"""
    lst = export_list(area_id, limit=20)["tasks"]
    done = [t for t in lst if t.get("status") == 1 and (t.get("progress") or 0) >= min_progress
            and t.get("fileAddress")]
    if exclude_ids:                                   # 排除触发前就存在的任务 → 只认新产出
        ex = {str(x) for x in exclude_ids}
        done = [t for t in done if str(t.get("id")) not in ex]
    if after:                                         # 只认这个时间点之后创建的
        done = [t for t in done if str(t.get("createTime") or "") >= str(after)]
    if not done:
        pend = [t for t in lst if t.get("status") == 0]
        note = "暂无符合条件的已完成导出任务。"
        if exclude_ids or after:
            note += "（已按 after/exclude_ids 过滤掉旧任务——这是刻意的，别退回去用旧导出）"
        return {"ready": False, "note": note +
                (f"进行中 {len(pend)} 条，稍后再试。" if pend else "先 ms_export_eligible 触发导出。"),
                "tasks": lst[:5]}
    task = done[0]
    res = {"ready": True, "task": task}
    if download:
        addr = task["fileAddress"]
        is_zip = addr.lower().split("?")[0].endswith(".zip") or str(task.get("fileName", "")).lower().endswith(".zip")
        ext = "zip" if is_zip else "xlsx"
        out_path = out_path or os.path.join(_export_dir(), f"_export_{area_id}_{task['id']}.{ext}")
        try:                                              # ★下载别抛：签名直链会过期(Expires,~7天)→403
            with bare_client(timeout=120) as c:           # 抛会冲垮 seckill_thresholds 轮询→返回 ready:False 让上层触发新导出
                r = c.get(addr)
                r.raise_for_status()
                open(out_path, "wb").write(r.content)
        except Exception as e:
            return {"ready": False, "task": task, "tasks": lst[:3],
                    "note": f"最新导出下载失败(签名URL可能过期:{str(e)[:60]})——请 ms_export_eligible 触发新导出后重试。"}
        res["file"] = out_path
        try:
            inner = _unzip_export(out_path) if is_zip else out_path   # 便宜包邮=.zip(内含清单)，秒杀=.xlsx 直用
            res["parsed_from"] = inner
            res["rows"] = _parse_export_rows(inner)
            res["skuCount"] = len(res["rows"])
        except Exception as e:
            res["parse_error"] = str(e)[:120]
    return res


def _unzip_export(zip_path: str) -> str:
    """解压便宜包邮导出的 .zip，返回内部第一个 xlsx/xls/csv 的路径（解到同目录）。"""
    import zipfile
    out_dir = os.path.join(os.path.dirname(zip_path), os.path.basename(zip_path).rsplit(".", 1)[0] + "_x")
    os.makedirs(out_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".xlsx", ".xls", ".csv"))]
        if not names:
            raise BlacklightError(f"zip 内无 xlsx/xls/csv：{z.namelist()[:5]}")
        z.extract(names[0], out_dir)
        return os.path.join(out_dir, names[0])


_OLE2_MAGIC = bytes([0xD0, 0xCF, 0x11, 0xE0])    # 老式 BIFF .xls（OLE2 复合文档）
_ZIP_MAGIC = bytes([0x50, 0x4B, 0x03, 0x04])     # zip 容器 = xlsx（哪怕文件名写着 .xls）


def _read_grid(path: str) -> list:
    """读 xlsx/xls/csv → 行列表(每行 = 单元格值 tuple)。**按魔数分派，扩展名只用来认 csv**。"""
    low = path.lower()
    if low.endswith(".csv"):
        import csv, io
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                with io.open(path, encoding=enc, newline="") as f:
                    return [tuple(r) for r in csv.reader(f)]
            except UnicodeDecodeError:
                continue
        with io.open(path, encoding="utf-8", errors="replace", newline="") as f:
            return [tuple(r) for r in csv.reader(f)]
    # ★★**按魔数嗅探，不信扩展名**（2026-08-24）：平台「已报名导出」zip 内的文件**名叫 .xls、
    #   实为 xlsx**（PK 03 04 头）；而 2025-04-05 那份还是真 BIFF（D0 CF 11 E0）⇒ 平台今年换了格式。
    #   靠扩展名分派会**两头都读不了**：xlrd 报「Excel xlsx file; not supported」，
    #   openpyxl 又按扩展名拒读（InvalidFileException）⇒ 这里一律喂**文件对象**绕开它的扩展名校验。
    with open(path, "rb") as _f:
        blob = _f.read()
    head = blob[:4]
    def _via_openpyxl(b):
        import io as _io
        import openpyxl
        wb = openpyxl.load_workbook(_io.BytesIO(b), data_only=True)   # BytesIO：绕开扩展名校验
        return [row for row in wb.worksheets[0].iter_rows(values_only=True)]
    try:
        if head == _ZIP_MAGIC:             # zip 容器 = xlsx（今天的「已报名导出」就是它，文件名却写 .xls）
            return _via_openpyxl(blob)
        if head == _OLE2_MAGIC:            # OLE2 复合文档 = 老式 BIFF .xls（2025 那份、便宜包邮导出）
            try:
                import xlrd
            except ImportError as e:
                raise BlacklightError("读老式 .xls(BIFF) 需 xlrd，请先 pip install xlrd") from e
            book = xlrd.open_workbook(file_contents=blob)
            sh = book.sheet_by_index(0)
            return [tuple(sh.row_values(r)) for r in range(sh.nrows)]
        return _via_openpyxl(blob)                   # 未知魔数：兜底按 xlsx 试
    except BlacklightError:
        raise
    except Exception as e:
        # 解析失败一律转 BlacklightError：调用方的自动降级只 catch 这一类，
        # 裸 XLRDError/InvalidFileException 会直接穿出去（applied_by_skus 的降级就吃不到）。
        raise BlacklightError("导出文件解析失败(%s，魔数 %r)：%s"
                              % (os.path.basename(path), head, str(e)[:120])) from e


def _parse_export_rows(path: str) -> list:
    """解析导出的可报清单(xlsx/xls/csv)：定位 SKU 列 + 含'门槛'的列，返回 [{skuId, 门槛, name?}]。"""
    grid = _read_grid(path)
    if not grid:
        return []
    hdr = [("" if c is None else c) for c in grid[0]]
    def _find(*keys):
        for i, h in enumerate(hdr):
            if any(k in str(h) for k in keys):
                return i
        return None
    i_sku = _find("sku ID", "sku编号", "SKUID", "SKU ID", "商品编号")  # 便宜包邮列名"sku ID"(有空格)；避开"spu ID"
    if i_sku is None:
        i_sku = _find("SKU", "sku")
    i_thr = _find("报名价格上限", "价格上限", "门槛")                  # 秒杀列名"报名价格上限"；便宜包邮无该列(用建议价)
    i_name = _find("sku 名称", "商品名", "名称", "标题")
    i_price = _find("京东价", "到手价", "售价")
    i_sugg = _find("建议价")                                          # 便宜包邮有
    i_low = _find("近30天最低价", "最低价")                            # 便宜包邮有
    def _cell(row, i):
        return row[i] if i is not None and i < len(row) else None
    rows = []
    for row in grid[1:]:
        if i_sku is None or i_sku >= len(row) or not row[i_sku]:
            continue
        sku = str(row[i_sku]).strip()
        if sku.endswith(".0"):        # xls 数字读成 float → 去尾
            sku = sku[:-2]
        sugg = _cell(row, i_sugg)
        # **门槛=报名价格上限**：秒杀有显式"门槛"列；便宜包邮/特价无该列，**"建议价"即上限门槛**(用户确认)→用建议价当门槛
        thr = _cell(row, i_thr)
        if (thr is None or thr == "") and sugg not in (None, ""):
            thr = sugg
        r = {"skuId": sku, "门槛": thr,
             "name": (str(_cell(row, i_name))[:24] if _cell(row, i_name) else None),
             "price": _cell(row, i_price)}
        if i_sugg is not None:
            r["建议价"] = sugg          # 便宜包邮/特价：建议价==门槛(报名价上限)
        if i_low is not None:
            r["近30天最低价"] = _cell(row, i_low)
        rows.append(r)
    return rows


# =========================================================================== #
# 表格报名（上传填好的模板 = 批量提交报名）—— ms 批量报名正道
#   ③ POST /apply/openness/apply/excel        multipart 上传即报名（真写，confirm 门）
#   ④ POST /apply/openness/apply/querySubmitList 轮询提交进度 + 失败明细直链
# 模板：官方空模板(6列) → 只填 sheet2「报名商品信息」→ 上传。
# =========================================================================== #
def _post_multipart(path: str, form: dict, files: dict, timeout: float = 120) -> dict:
    """表格上传（multipart）。

    ⚠️**`/apply/openness/apply/excel` 有节流：「1分钟之内不可重复操作」** —— 连续批次之间
    等 ~75 秒再发下一批，否则整批被拒。（subsidy 的 `/common/fileProcess` 同样有节流，
    且**退避时间递增** 69s → 120s；那边失败时 `taskId=null` 表示一条都没传，不是部分成功。）"""
    j = post_multipart(OAC_BASE, path, form, files, jd_auth.session_cookie(), timeout=timeout)  # 公共 multipart（jd_core）
    if not j.get("success"):
        raise BlacklightError(f"{path}: {j.get('message') or j.get('code')}")
    return j


def _post_list(client, path: str, body: dict) -> list:
    """同 _post，但 data 是 list（导出/提交记录接口返回数组）。"""
    r = client.post(OAC_BASE + path, content=_json.dumps(body, ensure_ascii=False))
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise BlacklightError(f"{path}: {j.get('message') or j.get('code')}")
    return j.get("data") or []


def _template_path() -> str:
    """下载并缓存官方空模板（storage.360buyimg.com 公开 CDN，无需登录态）。"""
    cache = os.path.join(_HERE, "_seckill_template.xlsx")
    if os.path.isfile(cache) and os.path.getsize(cache) > 2000:
        return cache
    url = scene_cfg("ms").get("table_template_url")
    if not url:
        raise BlacklightError("config.json ms.table_template_url 缺失")
    with bare_client(timeout=60) as c:
        r = c.get(url)
        r.raise_for_status()
        open(cache, "wb").write(r.content)
    return cache


# 表格报名的场域差异（实证）：秒杀 6 列/2 activityItem(场次+时长,按小时)/有 expectedTime；
# 便宜包邮/特价 3 列/1 activityItem(仅时长,按天)/无 expectedTime/applyExtendInfo 带 packageType 等。
_TABLE_CHANNEL = {
    "seckill": {
        "required": ["skuId", "promoPrice", "promoQty"],
        "optional": ["limitQty", "limitMode", "isMain"],
        "sheet": _APPLY_SHEET, "official_template": True,   # 官方 6 列模板
        "item_codes": ["activityBeginHour", "activityDuration"],
        "send_expected_time": True, "default_duration": "28",
        "apply_ext": {"manualRebateFlag": 0, "operateType": 3, "displayChannel": "0", "seckillSkin": "0"},
        "apply_sku_type_str": False,   # 秒杀 applySkuType=int
    },
    "baoyou": {
        "required": ["skuId", "promoPrice", "promoStock"],
        "optional": [],
        "sheet": "默认", "official_template": False,        # 自生成 3 列（sheet"默认"）
        "headers": ["SKU(*必填，备注：无)", "促销价格(*必填，备注：无)", "促销库存(*必填，备注：无)"],
        "item_codes": ["activityDuration"],                 # 无场次
        "send_expected_time": False, "default_duration": "30",   # 便宜包邮时长按天
        "apply_ext": {"manualRebateFlag": 0, "operateType": 3, "applyDoublePay": 1,
                      "packageType": 2, "payLaterProtocol": 1},
        "apply_sku_type_str": True,    # 便宜包邮 applySkuType="3"(字符串)
    },
}
_TABLE_CHANNEL["tejia"] = _TABLE_CHANNEL["baoyou"]   # 特价同便宜包邮同套系统


def _chan(channel: str) -> dict:
    c = _TABLE_CHANNEL.get(channel)
    if not c:
        raise BlacklightError(f"未知 channel={channel}（可选 {list(_TABLE_CHANNEL)}）")
    return c


def _session_default(session_value, expected_time, channel: str) -> str:
    """场次值缺省解析（2026-08-03 加）。

    秒杀有「场次」概念：`session_value` 写入 `activityBeginHour`，`expected_time` 写入 `expectedTime`，
    二者应当一致。旧代码把 session_value 默认写死 `"00:00:00"`、expected_time 默认 `"20:00:00"`，
    用默认值报名会**报到 00:00 场**而 expectedTime 却是 20:00。故留空时继承 expected_time。
    便宜包邮/特价无场次概念，保持 "00:00:00"（个别池要求必填 activityBeginHour，走 extra_items 覆盖）。"""
    if session_value not in (None, ""):
        return session_value
    return expected_time if _chan(channel).get("send_expected_time") else "00:00:00"


def _norm_rows(rows: list, channel: str = "seckill") -> list:
    """规整报名行。秒杀必填 skuId/promoPrice(promoQty 缺则默认 config 5000)；
    便宜包邮/特价必填 skuId/promoPrice/promoStock。price 兼容 promoPrice/price；数量兼容 promoQty/qty/promoStock/stock。
    ★秒杀促销数量(2026-07-20)：缺则默认 **config ms.default_promo_qty=5000**、且**下限 100**(<100会被"促销数量不能小于100"拒)——
      别按实际库存填(库存<100会被拒;固定 5000 是运营口径)。"""
    from blacklight.core import scene_cfg
    spec = _chan(channel)
    default_qty = scene_cfg("ms").get("default_promo_qty", 5000)
    out = []
    for r in rows:
        sku = str(r.get("skuId") or r.get("sku") or "").strip()
        price = r.get("promoPrice", r.get("price"))
        qty = r.get("promoStock", r.get("promoQty", r.get("stock", r.get("qty"))))
        if channel == "seckill":
            if qty in (None, ""):
                qty = default_qty                       # 秒杀缺数量→默认 5000
            try:
                if int(qty) < 100:
                    qty = 100                           # 秒杀下限 100
            except (TypeError, ValueError):
                pass
        if not sku or price in (None, "") or qty in (None, ""):
            raise BlacklightError(f"[{channel}] 报名行缺必填({'/'.join(spec['required'])})：{r}")
        row = {"skuId": sku, "promoPrice": price, "promoQty": qty, "promoStock": qty}
        for k in ("limitQty", "limitMode", "isMain"):
            row[k] = r.get(k)
        out.append(row)
    return out


#: 便宜包邮/特价 模板里可能出现的「商品短标题」列（**按收品池而异**）
SHORT_TITLE_HEADER = "商品短标题(非必填，备注：无)"


def build_table_xlsx(rows: list, out_path: str = None, channel: str = "seckill",
                     with_short_title: bool = False) -> dict:
    """生成填好模板。秒杀=下官方 6 列模板填 sheet2「报名商品信息」；便宜包邮/特价=自生成(sheet"默认")。

    ⚠️**便宜包邮/特价的列数按收品池而异**（2026-07-28 实证）：
      · 常见 3 列：`SKU / 促销价格 / 促销库存`
      · 有的池 4 列：`SKU / **商品短标题(非必填)** / 促销价格 / 促销库存`（如 areaId 40375202）
    列数不对 → 提交后 `submit_list` 的 `remark` 报「**Excel读取数据异常**」，且 total=0、无失败明细文件。
    **怎么确认**：拿该 area 历史上成功过的一次提交（`ms.submit_list(area_id)` → `successFileAddress`）下载看表头。
    然后用 `with_short_title=True` 生成 4 列版。
    """
    import openpyxl
    spec = _chan(channel)
    rows = _norm_rows(rows, channel)
    if spec["official_template"]:                       # 秒杀：官方模板
        wb = openpyxl.load_workbook(_template_path())
        ws = wb[spec["sheet"]] if spec["sheet"] in wb.sheetnames else wb.worksheets[-1]
        for r in rows:
            ws.append([r["skuId"], r["promoPrice"], r["promoQty"],
                       r["limitQty"], r["limitMode"], r["isMain"]])
    else:                                                # 便宜包邮/特价：自生成 3 或 4 列
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = spec["sheet"]
        head = list(spec["headers"])
        if with_short_title:
            head.insert(1, SHORT_TITLE_HEADER)
        ws.append(head)
        for r in rows:
            line = [r["skuId"], r["promoPrice"], r["promoStock"]]
            if with_short_title:
                line.insert(1, r.get("shortTitle") or None)   # 非必填，留空即可
            ws.append(line)
    # 上传模板同样落 runtime/exports/，不落包源码目录（与导出产物同一处理，2026-08-06）
    out_path = out_path or os.path.join(_export_dir(),
                                        f"_tableapply_{channel}_{int(rows[0]['skuId']) % 100000}.xlsx")
    wb.save(out_path)
    return {"path": out_path, "rows": len(rows)}


def _table_token(area_id, batch_id, activity_duration, apply_sku_type, rows, channel,
                 ext_override=None, with_short_title=False, extra_items=None) -> str:
    """confirm_token 必须覆盖**所有影响提交内容的参数**——ext_override(协议开关) 和 with_short_title(模板列数)
    都会改变实际发出去的东西，不纳入哈希就等于 dry-run 校验的不是真正要发的那份（2026-07-28 发现的缺口）。"""
    sig = sorted((str(r["skuId"]), canon_num(r["promoPrice"]), canon_num(r["promoQty"])) for r in rows)
    return _confirm_token({"path": "/apply/openness/apply/excel", "area": str(area_id),
                           "batch": str(batch_id), "chan": channel,
                           "dur": str(activity_duration), "type": str(apply_sku_type), "rows": sig,
                           "ext": _json.dumps(canon_for_token(ext_override or {}), sort_keys=True),
                           "st": bool(with_short_title),
                           "xi": _json.dumps(canon_for_token(extra_items or {}), sort_keys=True)})


def fetch_form_set(area_id: int) -> dict:
    """取某 area 的报名 formSet（POST /openness/formset/area/detail?areaId=，空 body）→ **{code: formItemId}** 映射。
    formItemId **每活动/批次可能变，但 code 稳定** → 按 code 定位才对（用户纠正）。实证 code：
    活动级 `activityBeginHour`(场次/促销开始时间)、`activityDuration`(促销时长)；
    每SKU `skuId/whitePic/shortName/promoPrice/promoNum/limitNum/purchasePrice/minPurchasePrice` 等。只读。"""
    with _client() as client:
        r = client.post(f"{OAC_BASE}/openness/formset/area/detail?areaId={int(area_id)}")
        r.raise_for_status()
        j = r.json()
    if not j.get("success"):
        raise BlacklightError(f"formset/area/detail: {j.get('message') or j.get('code')}")
    m = {}
    def _walk(o):
        if isinstance(o, list):
            for x in o:
                _walk(x)
        elif isinstance(o, dict):
            # 表单项 VO 特征：同时有 formSetId + code + id；VO.id == formItemId（实证）
            if o.get("code") and o.get("id") is not None and "formSetId" in o and o["code"] not in m:
                m[o["code"]] = o["id"]
            for v in o.values():
                _walk(v)
    _walk(j.get("data"))
    return m


def _apply_items(area_id: int, activity_duration, channel: str = "seckill",
                 session_value: str = "00:00:00", form_override: dict = None,
                 extra_items: dict = None) -> tuple:
    """活动级 applyItems，按 channel 的 item_codes 动态解 formItemId（formset/area/detail 按 code）。
    秒杀=[activityBeginHour(场次), activityDuration]；便宜包邮/特价=[activityDuration] 仅时长。
    form_override 可传 {code: formItemId} 覆盖某项。返回 (items, source)。

    ⚠️**必填项按收品池而异**（2026-07-28 实证）：便宜包邮默认只传 `activityDuration`，
    但 areaId `40375202`（特价×微信域 5.9-5 池）还**必填 `activityBeginHour`（促销开始时间）**，
    不传会：`submit_list` 的 `status=2 / progress=0 / remark="促销开始时间该项为必填项"`——
    **而回执里的 success/fail 仍显示全部成功**（那是行级解析数，任务本身失败了）。
    → 用 `extra_items={"activityBeginHour": "00:00:00"}` 补。可用 code 见 `fetch_form_set(area_id)`。
    """
    spec = _chan(channel)
    fs, source = {}, "formset(dynamic)"
    try:
        fs = fetch_form_set(area_id)
    except Exception as e:
        source = f"config兜底(formset失败:{str(e)[:36]})"
    # config.table_form 兜底是**秒杀专属**（179075941/942）→ 仅 seckill 用；便宜包邮/特价 formset 失败则报错(别错用秒杀ID)
    cfg_fb = scene_cfg("ms").get("table_form", {}) if channel == "seckill" else {}
    _CODE_FB = {"activityBeginHour": cfg_fb.get("sessionTime"), "activityDuration": cfg_fb.get("duration")}
    ov = form_override or {}
    items = []
    for code in spec["item_codes"]:
        fid = ov.get(code) or fs.get(code) or _CODE_FB.get(code)
        if fid is None:
            raise BlacklightError(f"[{channel}] 取不到 formItemId(code={code})：formset 未返回且无兜底；用 form_override={{'{code}':<id>}} 传。")
        val = session_value if code == "activityBeginHour" else str(activity_duration)
        items.append({"formItemId": str(fid), "value": val})
    for code, val in (extra_items or {}).items():           # 该池额外必填项（如 activityBeginHour）
        if code in spec["item_codes"]:
            continue                                        # 已在上面组过，不重复
        fid = ov.get(code) or fs.get(code)
        if fid is None:
            raise BlacklightError(f"[{channel}] extra_items 里的 code={code} 在 formset 未找到；"
                                  f"可用 code：{sorted(fs)}")
        items.append({"formItemId": str(fid), "value": str(val)})
        source += "+extra"
    if ov:
        source += "+override"
    return items, source


def _apply_ext(channel: str, apply_sku_type, ext_override: dict = None) -> dict:
    """按 channel 组 applyExtendInfo。秒杀 applySkuType=int；便宜包邮/特价=str "3" + packageType/包邮协议。

    ⚠️**协议开关按收品池而异，不是按 channel 固定**（2026-07-28 实证）：默认 `payLaterProtocol=1`（先享后付），
    但 5.9-5 钩子券池（areaId 42948202）没开这个协议，整批 308 条全被拒：
    「当前收品池没有开启先享后付协议, 不能签署此协议报名」。
    → 换池报名前先小批试，被拒就用 `ext_override={"payLaterProtocol": 0}` 关掉。
    同类开关还有 `applyDoublePay` / `cpsSubsidy` / `packageType`。
    """
    spec = _chan(channel)
    t = str(apply_sku_type) if spec["apply_sku_type_str"] else int(apply_sku_type)
    ext = {"applySkuType": t, **spec["apply_ext"]}
    if ext_override:
        ext.update(ext_override)
    return ext


def _build_upload_form(area_id, batch_id, activity_duration, apply_sku_type, channel,
                       expected_time, session_value, form_override, ext_override=None,
                       extra_items=None) -> tuple:
    """组 multipart 表单(除 file 外) + applyItems 来源。返回 (form_dict, apply_items, source)。"""
    spec = _chan(channel)
    apply_items, form_src = _apply_items(int(area_id), activity_duration, channel,
                                         session_value, form_override, extra_items)
    extend = _apply_ext(channel, apply_sku_type, ext_override)
    form = {"areaId": str(area_id), "batchId": str(batch_id),
            "activityDuration": str(activity_duration),
            "applyExtendInfo": _json.dumps(extend, ensure_ascii=False),
            "applyItems": _json.dumps(apply_items, ensure_ascii=False)}
    if spec["send_expected_time"]:          # 秒杀有 expectedTime；便宜包邮/特价无
        form["expectedTime"] = expected_time
    return form, apply_items, form_src


def table_apply_dryrun(area_id: int, batch_id: int, rows: list = None, channel: str = "seckill",
                       activity_duration=None, apply_sku_type: int = 3,
                       expected_time: str = "20:00:00", session_value: str = None,
                       form_override: dict = None, ext_override: dict = None,
                       with_short_title: bool = False, extra_items: dict = None) -> dict:
    """表格报名 DRY-RUN：规整行 + 生成填好模板（不上传），回显 form/行数 + confirm_token。
    **channel**：seckill(秒杀,6列,场次+时长按小时,有expectedTime) / baoyou / tejia(便宜包邮/特价,3列,仅时长按天,无场次)。
    rows：秒杀=[{skuId,promoPrice,promoQty,limitQty?,limitMode?,isMain?}]；便宜包邮/特价=[{skuId,promoPrice,promoStock}]。
    activity_duration 留空→按 channel 默认(秒杀28小时/便宜包邮30天)；batch_id=yx_ms_get_batch_id。formItemId 走 formset 动态取。
    ⚠️`session_value`(写入 activityBeginHour=场次) **留空默认继承 expected_time**（2026-08-03 修）——
      旧默认写死 `"00:00:00"` 而 expected_time 默认 `"20:00:00"`，两者矛盾，用默认值报名会报到 00:00 场。
      **batchId 本身也按场次小时区分**(实证同日 00/08/20/22 点四个不同 batchId)，三者必须一致。"""
    session_value = _session_default(session_value, expected_time, channel)
    spec = _chan(channel)
    activity_duration = spec["default_duration"] if activity_duration in (None, "") else activity_duration
    rows = _norm_rows(rows or [], channel)
    if not rows:
        raise BlacklightError("table_apply 需要至少一行")
    if len(rows) > 100000:
        raise BlacklightError(f"单次表格报名 {len(rows)} 行超上限 100000")
    built = build_table_xlsx(rows, channel=channel, with_short_title=with_short_title)
    form, apply_items, form_src = _build_upload_form(area_id, batch_id, activity_duration,
                                                     apply_sku_type, channel, expected_time,
                                                     session_value, form_override, ext_override,
                                                     extra_items)
    return {"would_apply": False, "channel": channel, "rows": built["rows"], "xlsx": built["path"],
            "form": form, "applyItems_parsed": apply_items, "formItemId_source": form_src,
            "note": "DRY-RUN：已生成 xlsx 未上传。核对 form(尤其 applyItems.formItemId 与 source)/行数后，相同参数+confirm 调 ms_table_apply。",
            "confirm_token": _table_token(area_id, batch_id, activity_duration, apply_sku_type, rows,
                                          channel, ext_override, with_short_title, extra_items)}


def _await_submit(area_id: int, apply_sku_type: int, pre_id, timeout: int = 120, interval: int = 6) -> dict:
    """轮询 querySubmitList 顶部**新**任务(id≠pre_id)直到终态(status 1完成/2失败)或超时。
    回执自验证：Agent/人不用碰有延迟的监控数据，直接拿 success/fail/失败明细。"""
    deadline = _time.time() + timeout
    last = None
    while _time.time() < deadline:
        tasks = submit_list(area_id, apply_sku_type=apply_sku_type, limit=1).get("tasks") or []
        if tasks and str(tasks[0].get("id")) != str(pre_id):
            last = tasks[0]
            if last.get("status") in (1, 2):     # 终态
                st, remark = last.get("status"), (last.get("remark") or "")
                succ, prog = last.get("success") or 0, last.get("progress")
                # ★★`success/fail` 只是**行级解析数**，任务整体成败看 `status`/`remark`。
                # 2026-07-28 实证：status=2 / progress=0 / remark="促销开始时间该项为必填项"，
                # 但 success=134 / fail=0 —— **一条都没落地**，差点当成功收工。
                res = {"done": True, "ok": bool(st == 1 and not remark),
                       "status": st, "progress": prog, "remark": remark,
                       "total": last.get("total"), "success": succ,
                       "fail": last.get("fail"), "successFileAddress": last.get("successFileAddress"),
                       "failFileAddress": last.get("failFileAddress")}
                if st != 1 and succ:
                    res["⚠️假成功"] = (f"success={succ} 但 status={st}、remark=「{remark}」"
                                     "——success/fail 是行级解析数，**任务整体失败、一条未落地**。"
                                     "按 remark 修参数后重报，并以全池 get_applied 比对确认。")
                return res
        _time.sleep(interval)
    return {"done": False, "note": f"轮询{timeout}s仍处理中，稍后 ms_submit_list 查", "last": last}


@audited("ms", "table_apply")
def table_apply(area_id: int, batch_id: int, rows: list = None, confirm: str = "",
                channel: str = "seckill", activity_duration=None, apply_sku_type: int = 3,
                expected_time: str = "20:00:00", session_value: str = None,
                form_override: dict = None, xlsx_path: str = None,
                wait: bool = True, wait_timeout: int = 120,
                ext_override: dict = None, with_short_title: bool = False,
                extra_items: dict = None) -> dict:
    """**表格报名真执行**：生成填好模板 → multipart 上传 /apply/openness/apply/excel（真金白银批量报名）。
    需相同参数先 ms_table_apply_dryrun 拿 confirm_token 再带 confirm。**wait=True 自动轮询到终态并回执自验证**。
    channel: seckill / baoyou / tejia。formItemId 走 formset 动态取，可 form_override={code:formItemId} 覆盖。
    ⚠️`session_value`(场次/activityBeginHour) **留空继承 expected_time**（2026-08-03 修，旧默认写死 00:00:00 与
      expected_time 默认 20:00:00 矛盾）。**batchId 也按场次小时区分**，三者必须一致。

    ★★**看 `result.ok` / `status` / `remark`，别看 `success/fail`**：后者只是行级解析数，
    实证 status=2 / progress=0 / remark="促销开始时间该项为必填项" 时 success 仍显示 134、**实际一条没落地**
    （此时 result 里会有 `⚠️假成功` 字段）。落地与否**最终以全池 `get_applied` 比对为准**。
    ⚠️2026-08-07 补充：`result.ok` **也不能反过来当失败信号** —— 实测 0/3、2/3、202/202 三种落地率下
      它**都返回 false**。`ok=false` 只说明「不是完美回执」，不代表没报上。唯一判据仍是全池比对。

    ★★**落地判定必须等 ~90 秒再比对**（2026-08-07 实证，当天连坑两次）：
      · 199 款批次提交后立刻比对 → 只见 30 款；等 110 秒后再比 → **199/199 全部落地**；
      · 另一批 3 款立刻比对 0/3，我据此误判「该场次不存在」，实际 15 分钟后 3/3 都在。
      ⇒ 提交后 `time.sleep(~110)` 再 `applied_by_skus(...)` 比对本场次 batchId；
        读太早会把成功误报成失败，进而触发不必要的重报（重报会撞判重）。

    ★**无门槛桶别再默认不报**（2026-08-07 推翻旧结论）：导出「报名价格上限」为空的那批，
      旧口径是「空白≠无门槛，预期被拒，默认不进模板」。实测 363 款无门槛里 **297 款落地(82%)**：
      第一轮直接过 230 款，被拒的从**拒绝文案里能回收真实门槛**
      （格式 `预估到手价:X高于报名价格门槛:Y`）→ `solve_seckill_price(threshold=Y)` 重解 → 47/47 补报成功。
      剩下 66 款是**顶到门槛仍<目标毛利**（该拦），不是报名失败。
      旁证：同一 SKU 在 10:00 场次有门槛、20:00 场次没门槛 ⇒ 空白确实是**导出的数据缺口**。

    ⚠️**三样东西按收品池而异，换池必须先小批(3款)试**：
      1) `ext_override` 协议开关：默认 `payLaterProtocol=1`，有的池没开先享后付 → 整批被拒（7165池 308/308 全挂）。
      2) `with_short_title` 模板列数：常见 3 列，有的池 4 列(多「商品短标题」) → `remark="Excel读取数据异常"`、total=0 无明细（6228池）。
      3) `extra_items` 额外必填项：便宜包邮默认只传 activityDuration，有的池还必填 `activityBeginHour`(促销开始时间)（6228池）。
    确认列数的办法：`submit_list(area_id)` 找该池历史成功过的一次 → 下 `successFileAddress` 看表头。"""
    session_value = _session_default(session_value, expected_time, channel)
    spec = _chan(channel)
    activity_duration = spec["default_duration"] if activity_duration in (None, "") else activity_duration
    rows = _norm_rows(rows or [], channel)
    if not rows:
        raise BlacklightError("table_apply 需要至少一行")
    if len(rows) > 100000:
        raise BlacklightError(f"单次表格报名 {len(rows)} 行超上限 100000")
    token = _table_token(area_id, batch_id, activity_duration, apply_sku_type, rows,
                         channel, ext_override, with_short_title, extra_items)
    if confirm != token:
        raise BlacklightError("表格报名真执行需二次确认：先用相同参数跑 ms_table_apply_dryrun 拿 confirm_token 再带 confirm。")
    pre = (submit_list(area_id, apply_sku_type=apply_sku_type, limit=1).get("tasks") or [{}])
    pre_id = pre[0].get("id") if pre else None      # 记录提交前顶部任务，好识别我这次的新任务
    built = build_table_xlsx(rows, xlsx_path, channel=channel, with_short_title=with_short_title)
    form, apply_items, form_src = _build_upload_form(area_id, batch_id, activity_duration,
                                                     apply_sku_type, channel, expected_time,
                                                     session_value, form_override, ext_override,
                                                     extra_items)
    data = open(built["path"], "rb").read()
    files = {"file": (os.path.basename(built["path"]), data, XLSX_MIME)}
    j = _post_multipart("/apply/openness/apply/excel", form, files)
    out = {"applied": True, "channel": channel, "rows": built["rows"], "xlsx": built["path"],
           "formItemId_source": form_src, "message": j.get("message"), "code": j.get("code"),
           "confirm_token": token}
    if wait:
        out["result"] = _await_submit(area_id, apply_sku_type, pre_id, timeout=wait_timeout)  # ★回执自验证
    else:
        out["note"] = "已进入异步队列，用 ms_submit_list 轮询(成功/失败明细在 successFileAddress/failFileAddress)。"
    return out


def _parse_submit_task(it: dict) -> dict:
    return {"id": it.get("id"),
            "status": it.get("submitStatus"),      # 0进行中 / 1完成 / 2失败
            "progress": it.get("submitProgress"),
            "total": it.get("submitTotalNum"), "success": it.get("submitSuccessNum"),
            "fail": it.get("submitFailNum"),
            "successFileAddress": it.get("successFileAddress") or "",   # 便宜包邮/特价有(成功商品清单)
            "failFileAddress": it.get("failFileAddress") or "",         # 失败明细清单
            "submitEndTime": it.get("submitEndTime"), "remark": it.get("remark"),
            "createTime": it.get("createTime"), "creator": it.get("creator")}


# ---------- 被拒回收：从失败明细里把平台的**真实门槛**捞回来 ----------
# ★2026-08-10 实证：导出清单「无门槛」**不等于平台没门槛**，只是导出文件缺这列。
#   479 款 A 桶首轮落地 432（90%），47 款被拒中 42 款来自无门槛桶；
#   按本函数回收真实门槛、重解价格补报后 **68/68 落地，总落地率 90%→98%**。
REJECT_THRESHOLD_PAT = re.compile(r"SKU：(\d+).*?预估到手价:([\d.]+)高于报名价格门槛:([\d.]+)")



# ============================ 资料问题清单（2026-08-24 加） ============================
# ★起因：`recover_thresholds` 早就把「非价格原因」分到 `others` 并注明"改价没用别补报"，
#   但它**没有任何下游**——于是同一个 SPU 连着两场重复拖累：
#   8-21 拖掉 28 款、8-24 拖掉 12 款，**每场都要先白提交一次才知道**。
#   ⇒ 把 others 沉下来，规划阶段直接剔除，并单独输出成待修工单。
# ⚠️只收**改价没用**的那类（短标题/主推校验/商品资料）。
#   「已经报过名」是**场次相关**的，下一场就不成立，**绝不能沉**（沉了会永久漏报）。

_MATERIAL_STALE_DAYS = 21        # 超过这么久没再出现就不再拦（资料可能已修好）
_MATERIAL_PAT = ("短标题", "主推", "商品资料", "非法字符", "禁止包含")
_MATERIAL_SKIP_PAT = ("已经报过名", "已报名", "重复报名")


def _material_path() -> str:
    return os.path.join(_paths.home(), "material_issues.json")


def material_issues(include_stale: bool = False) -> dict:
    """已知「资料问题」SKU（改价没用、重报无用）。返回 {skuId: {reason, first_seen, last_seen, hits}}。"""
    try:
        with io.open(_material_path(), encoding="utf-8") as fh:
            d = _json.load(fh)
    except Exception:
        return {}
    if include_stale:
        return d
    cut = _time.strftime("%Y-%m-%d", _time.localtime(_time.time() - _MATERIAL_STALE_DAYS * 86400))
    return {k: v for k, v in d.items() if str(v.get("last_seen") or "") >= cut}


def record_material_issues(others, source: str = "") -> dict:
    """把 `recover_thresholds` 的 `others` 沉进资料问题清单（幂等，重复出现只累加 hits）。

    `others` 形如 [(skuId, 原因文案), ...]。**只收资料类**，「已经报过名」这类场次相关的跳过。
    """
    try:
        with io.open(_material_path(), encoding="utf-8") as fh:
            d = _json.load(fh)
    except Exception:
        d = {}
    today = _time.strftime("%Y-%m-%d")
    added, skipped = 0, 0
    for row in others or []:
        try:
            sku, why = str(row[0]), str(row[1])
        except Exception:
            continue
        if any(p in why for p in _MATERIAL_SKIP_PAT):         # 场次相关，别沉
            skipped += 1
            continue
        if not any(p in why for p in _MATERIAL_PAT):          # 不认识的原因，宁可不拦
            skipped += 1
            continue
        e = d.get(sku) or {"first_seen": today, "hits": 0}
        e.update({"reason": why[:80], "last_seen": today, "hits": e.get("hits", 0) + 1,
                  "source": source or e.get("source", "")})
        d[sku] = e
        added += 1
    tmp = _material_path() + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _material_path())
    return {"记录": added, "跳过(非资料类/场次相关)": skipped, "清单总数": len(d),
            "path": _material_path()}

def recover_thresholds(area_id: int, since: str = None, limit: int = 10,
                       apply_sku_type: int = 3) -> dict:
    """从**本次**提交的失败明细里回收平台真实门槛。

    `since`='YYYY-MM-DD HH:MM:SS'：**只看这个时刻之后创建的任务**。
      ⚠️不传会把历史任务的失败文件也捞进来（`submit_list` 是账号维度的），
        2026-08-10 我就因此把 4 天前的失败 SKU 又报了一遍。

    返回 `{thresholds:{sku:{报的到手价,真实门槛}}, others:[(sku,原因)], tasks:[...]}`。
    `others` 是**非价格原因**（短标题含非法字符 / 主推 SKU 短标题校验 / 已报过名），
    它们改价没用，别混进补报。
    """
    import httpx as _httpx
    import openpyxl as _openpyxl

    d = submit_list(area_id, apply_sku_type=apply_sku_type, limit=limit)
    tasks = [t for t in (d.get("tasks") or []) if t.get("failFileAddress")]
    if since:
        tasks = [t for t in tasks if str(t.get("createTime") or "") >= since]
    th, others = {}, []
    for t in tasks:
        r = _httpx.get(t["failFileAddress"], timeout=60, follow_redirects=True)
        tmp = os.path.join(_export_dir(), "_fail_%s.xlsx" % t["id"])
        with open(tmp, "wb") as f:
            f.write(r.content)
        ws = _openpyxl.load_workbook(tmp, data_only=True).active
        for row in list(ws.iter_rows(values_only=True))[1:]:
            if not row or not row[0]:
                continue
            msg = str(row[-1] or "")
            m = REJECT_THRESHOLD_PAT.search(msg)
            if m:
                th[m.group(1)] = {"报的到手价": float(m.group(2)), "真实门槛": float(m.group(3))}
            else:
                others.append((str(row[0]), msg[:60]))
    # ★2026-08-24：把「非价格原因」沉进资料问题清单，供下次规划直接剔除。
    #   在此之前 others 是**死路一条**——注释写着"改价没用别补报"，但没有任何下游，
    #   于是同一个 SPU 连着两场各拖掉 28 / 12 款，每场都要先白提交一次才知道。
    _rec = record_material_issues(others, source="recover_thresholds")
    return {"thresholds": th, "others": others, "资料问题已登记": _rec,
            "tasks": [{"id": t["id"], "createTime": t.get("createTime"),
                       "total": t.get("total"), "fail": t.get("fail")} for t in tasks],
            "_note": "others 是非价格原因（短标题非法字符/主推短标题校验/已报过名），改价没用别补报。"}


def replan_rejected(rows: list, thresholds: dict, min_margin: float = 0.02) -> dict:
    """按回收的真实门槛重解价格。`rows` 是原规划行（需含 报名价/到手价/全成本）。

    折算比例取原行的 `到手价/报名价`（平台真值二分出来的），把到手价压到 **门槛 − 0.01**。
    压完毛利 < `min_margin` 的进 `giveup` —— 那种是"平台门槛低于我的成本"，报了就是亏。
    2026-08-10 实测 74 款里 68 款可补、6 款放弃（有一款门槛 2.21 而成本 3.99）。
    """
    by = {str(x.get("skuId")): x for x in (rows or [])}
    out, giveup = [], []
    for sku, info in (thresholds or {}).items():
        p = by.get(str(sku))
        if not p:
            continue
        cap = float(info["真实门槛"])
        enroll, actual, cost = p.get("报名价"), p.get("到手价"), p.get("全成本")
        if not enroll or actual is None or cost is None:
            continue
        ratio = (actual / enroll) if enroll else 1.0
        new_enroll = round((cap - 0.01) / ratio, 2) if ratio else cap
        new_actual = round(new_enroll * ratio, 2)
        margin = (new_actual - cost) / new_actual if new_actual else -1
        rec = {**p, "报名价": new_enroll, "到手价": new_actual,
               "enroll_price": new_enroll, "actual_price": new_actual,
               "promoPrice": new_enroll, "毛利%": round(100 * margin, 1),
               "_原报名价": enroll, "_真实门槛": cap}
        (out if margin >= min_margin else giveup).append(rec)
    return {"rows": out, "giveup": giveup,
            "_note": "giveup = 压到门槛后毛利 <%.0f%%，报了就是亏，别硬报。" % (100 * min_margin)}


def submit_list(area_id: int, apply_sku_type: int = 3, limit: int = 10) -> dict:
    """查表格报名提交进度（POST /apply/openness/apply/querySubmitList），倒序。
    每条：status(0进行中/1完成/2失败)/progress/total/success/fail/**successFileAddress(成功清单,便宜包邮有)+failFileAddress(失败明细)**/remark。

    ⚠️★**返回的是账号维度的历史任务，不只是你刚提交的那批**。2026-08-10 我拿 limit=8 去捞
      失败文件做补报，**混进了 4 天前(08-06)那次的失败明细**，于是把一批早已落地的 SKU
      又报了一遍（平台回"已报过名"挡回，无副作用但白跑）。
      ⇒ 补报前**按 `createTime` 过滤出本次提交之后的任务**，别整页拿来用。
      返回结构是 `{count, tasks:[...]}` —— 键是 **`tasks`** 不是 `rows`。"""
    with _client() as client:
        d = _post_list(client, "/apply/openness/apply/querySubmitList",
                       {"areaId": int(area_id), "applySkuType": int(apply_sku_type)})
    rows = [_parse_submit_task(it) for it in (d or [])][:limit]
    return {"count": len(rows), "tasks": rows}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="单品秒杀 查已报名/退出干跑")
    ap.add_argument("--activity", required=True)
    ap.add_argument("--area", type=int, required=True)
    ap.add_argument("--applied", action="store_true")
    ap.add_argument("--sku-type", default=APPLY_SKU_TYPE_JX)
    a = ap.parse_args()
    if a.applied:
        r = get_applied(a.activity, a.area, a.sku_type, page_size=5)
        print("已报名 totalCount:", r["totalCount"])
        for it in r["items"]:
            print(" ", it["applyId"], it["skuId"], it["promoPrice"], "采购", it["purchasePrice"],
                  "到手上限", it["maxActualPrice"], str(it["skuName"])[:24])


# ---------- 失效预警处置：规划 → 发起 → **回读判定**（2026-08-24 固化） ----------

def plan_reduce_warned(activity_id: str, area_id: int, erp: str = None,
                       sessions: list = None, include_lossy: bool = False,
                       limit: int = None, **warn_kw) -> dict:
    """★**失效预警降价「规划」**（只读）：扫预警 → 筛本人×可降 → 逐款解出新促销价 → 出可直接发的 rows。

    以前这套是每次现搭的（扫→筛→solve→拼 rows），三步里每步都有坑，固化在这儿：
    · **必须按人筛**：本接口混多个 ERP（2026-08-24 实测 27 条里本人 24、另 3 人各 1）。
      `erp` 缺省 = 当前登录 PIN。
    · **只取 `处置='照降'`**；`降了会亏` 默认不动（`include_lossy=True` 才带上，需人拍板）。
    · **新促销价必须解**：`到手价` 对 `促销价` 斜率不是 1，照 −0.01 提交常常白降
      （见 `solve_warned_price`）。这里逐款用 couponInfo 真值二分，解不出的进 `解析失败` 桶。

    返回 {rows(可直接喂 reduce_price_dryrun→reduce_price), 汇总, 别人的, 降了会亏, 解析失败}。
    `rows` 每条含 skuId/applyId/promoPrice/purchasePrice/batchId/场次/降幅/降后毛利%/类型。"""
    erp = (erp or "").strip() or jd_auth.current_pin()
    w = invalidation_warnings(activity_id, area_id, **warn_kw)
    allrows = w.get("rows") or []
    mine = [r for r in allrows if str(r.get("erpPin") or "") == str(erp)]
    if sessions:
        keep = {str(x) for x in sessions}
        mine = [r for r in mine if str(r.get("场次") or "") in keep]
    others = {}
    for r in allrows:
        p = str(r.get("erpPin") or "?")
        if p != str(erp):
            others[p] = others.get(p, 0) + 1
    lossy = [r for r in mine if str(r.get("处置") or "") != "照降"]
    todo = [r for r in mine if str(r.get("处置") or "") == "照降"]
    if include_lossy:
        todo += lossy
    if limit:
        todo = todo[:int(limit)]
    rows, failed, already = [], [], []
    for r in todo:
        try:
            # ★★**先查这款是不是已经降过了**（2026-08-24 加）：降价=新建促销、旧行等平台清，
            #   预警桶里挂的还是**旧行** ⇒ 不查就会对同一款反复降，每降一次多建一条促销。
            #   判据同 verify_reduced：同 batch 存在「另一个 applyId + 更低促销价 + applyStatus=2」。
            if _already_reduced(activity_id, area_id, r):
                already.append({"skuId": r.get("skuId"), "applyId": r.get("applyId"),
                                "why": "已降过，新促销已通过(applyStatus=2)，旧行等平台清"})
                continue
            sol = solve_warned_price(r["skuId"], area_id, int(r["batchId"]),
                                     float(r["需降至"]), float(r["促销价"]))
            if not sol.get("ok"):
                failed.append({"skuId": r["skuId"], "reason": sol.get("reason") or "solve 未成功"})
                continue
            rows.append({"skuId": str(r["skuId"]), "applyId": str(r["applyId"]),
                         "promoPrice": sol["promoPrice"],        # 新促销价（平台真值解出）
                         "purchasePrice": r["需降至"],            # 目标到手价（权威=reducePriceWarningCMSPrice）
                         "解出到手价": sol.get("purchasePrice"),  # couponInfo 实测值，应 ≤ 需降至
                         "batchId": r.get("batchId"), "场次": r.get("场次"),
                         "原促销价": r.get("促销价"), "降幅": sol.get("降幅"),
                         "降后毛利%": r.get("降后毛利%"), "类型": r.get("类型"),
                         "阈值存疑": r.get("阈值存疑"), "name": str(r.get("skuName") or "")[:24]})
        except Exception as e:
            failed.append({"skuId": r.get("skuId"), "reason": "%s: %s" % (type(e).__name__, str(e)[:90])})
    return {"rows": rows,
            "汇总": {"全量预警": len(allrows), "本人": len(mine), "可降(照降)": len(todo),
                     "解出": len(rows), "已降过(跳过)": len(already),
                     "解析失败": len(failed), "降了会亏": len(lossy), "erp": erp},
            "别人的": others, "降了会亏": lossy, "解析失败": failed, "已降过": already,
            "next": "reduce_price_dryrun(rows, area_id) → reduce_price(..., confirm) → "
                    "**verify_reduced(rows, ...) 判成败**（别看预警桶，它是滞后指标）"}


def _already_reduced(activity_id: str, area_id: int, warn_row: dict,
                     apply_sku_type: str = APPLY_SKU_TYPE_JX) -> bool:
    """这条预警是不是**已经降过了**（预警桶里挂的是没被清掉的旧行）。判据同 `verify_reduced`。"""
    try:
        d = get_applied(activity_id, area_id, apply_sku_type, page=1, page_size=100,
                        sku_id=str(warn_row.get("skuId")))
    except Exception:
        return False                     # 查不到就别拦，宁可多算一次也别漏降
    cur = _f(warn_row.get("促销价"))
    bid = warn_row.get("batchId")
    for i in (d.get("items") or []):
        if bid is not None and str(i.get("batchId")) != str(bid):
            continue
        if str(i.get("applyId")) == str(warn_row.get("applyId")):
            continue
        p = _f(i.get("promoPrice"))
        if p is not None and cur is not None and p < cur - 1e-9 and i.get("applyStatus") == 2:
            return True
    return False


def verify_reduced(activity_id: str, area_id: int, rows: list,
                   apply_sku_type: str = APPLY_SKU_TYPE_JX) -> dict:
    """★★**降价成没成的权威回读**（2026-08-24 定案）：看「同 SKU 同 batch 出现**新 applyId +
    新促销价 + applyStatus=2**」，**不看失效预警桶**。

    降价的平台语义是**新建一条促销、原促销随后才被删**，所以刚发完时预警桶里躺的还是旧行
    （applyId/促销价/预警时间都没变）。实测 24 款：发完立刻回读仍 22 款在桶里、10 分钟后剩 3、
    半小时还是那 3 —— 而那 3 款查已报名列表是**每款两行**：新行 applyStatus=2 已通过、
    旧行 applyStatus=18/19（被替换态待清）。**24/24 其实全落地**。
    拿预警桶判会得出"3 款失败"，再去重复降价 ⇒ 又建一条促销。

    `rows` = plan_reduce_warned 的 rows（或任何含 skuId/applyId/promoPrice/batchId 的行）。
    返回 {已落地, 未落地, 明细, 汇总}。"""
    out, ok_n = [], 0
    for r in rows or []:
        sku = str(r.get("skuId"))
        want = round(float(r.get("promoPrice")), 2)
        bid = r.get("batchId")
        try:
            d = get_applied(activity_id, area_id, apply_sku_type, page=1, page_size=100, sku_id=sku)
            items = [i for i in (d.get("items") or [])
                     if (bid is None or str(i.get("batchId")) == str(bid))]
        except Exception as e:
            out.append({"skuId": sku, "落地": None, "err": str(e)[:100]})
            continue
        new = [i for i in items
               if _f(i.get("promoPrice")) is not None
               and abs(_f(i.get("promoPrice")) - want) < 0.005
               and str(i.get("applyId")) != str(r.get("applyId"))]
        passed = [i for i in new if i.get("applyStatus") == 2]
        old = [i for i in items if str(i.get("applyId")) == str(r.get("applyId"))]
        landed = bool(passed)
        ok_n += 1 if landed else 0
        out.append({"skuId": sku, "落地": landed,
                    "新applyId": (passed or new or [{}])[0].get("applyId"),
                    "新促销价": want,
                    "新行状态": (passed or new or [{}])[0].get("applyStatus"),
                    "旧applyId": r.get("applyId"),
                    "旧行状态": (old or [{}])[0].get("applyStatus"),   # 18/19 = 被替换态
                    "场次": r.get("场次")})
    bad = [x for x in out if not x.get("落地")]
    return {"已落地": ok_n, "未落地": len(bad), "明细": out, "未落地明细": bad,
            "汇总": "%d/%d 落地（判据=新 applyId + 新促销价 + applyStatus=2）" % (ok_n, len(rows or [])),
            "note": "旧行停在 18/19 是平台还没清，**不代表失败**；预警桶清空是滞后指标，别拿它判。"}
