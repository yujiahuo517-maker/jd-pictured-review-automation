"""**新品测试计划 逐日追踪**（2026-08-19 起，用户手动建的 5 个计划）。

## 为什么单独做一份，不复用 `ad_ledger`
`jzt_ad_daily_ledger` 是**全账户 SKU 级**快照，用来判「该拉黑/该放出」。
新品测试要回答的是另一个问题：**这个计划该留还是该停**，需要的是
**计划级 × 逐日** 的 消耗/达成ROI/预算利用率，且必须**对着保本 ROI 看**。

## ★判据是 `达成ROI ÷ 保本ROI`，不是达成 ROI 本身
广告口径只知道成交额、不知道成本（见 [[jzt-ad-vs-margin]]）。
ROI 6 对毛利率 22% 的品很安全，对毛利率 14% 的品是**低于保本、投了就亏**。
所以每次快照都**现算保本 ROI**（`1 ÷ 到手价毛利率`）——它会随券促变化漂移：
2026-08-19 给两个 SPU 踢完券，保本 ROI 当天就从 6.85→4.59、5.73→4.88。

## 三条判读线（沿用 3×3 决策表的口径）
- **预算利用率**：<30% 花不动 / >90% 撞线
- **ratio = 达成ROI ÷ 保本ROI**：<1.0 真亏 / 1.0~1.3 薄利 / ≥1.3 安全
- **新品前 3 天不下结论**：冷启动期达成 ROI 天然偏低，别当天就停投

⚠️`花费` 是**折前**口径（jzt），ge 的「消耗」是折后（×0.7）。算续航一律用折前。
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import statistics
from typing import Optional

from blacklight.core import BlacklightError
from blacklight.core import paths as _paths

#: 追踪对象：SPU → 备注（用户 2026-08-19 手动建计划）
TRACKED = {
    "10035611806847": "翻盖大容量",
    "10035805344269": "透黑（已踢券）",
    "10035805425244": "多卡扣（已踢券）",
    "10035835104303": "透白【新品·无促销】",
    "10035835447665": "前开式【新品·无促销】",
}

COLS = ["run_date", "spuId", "campaignId", "groupId", "计划名", "状态",
        "日预算", "目标ROI", "花费", "展现", "点击", "订单行", "成交额",
        "达成ROI", "预算利用率", "保本ROI", "ratio", "判读", "note"]


def _path() -> str:
    d = os.path.join(os.path.dirname(_paths.audit_path()), "ledger")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "newproduct_track.csv")


def _f(v, dflt=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


_BREAKEVEN_DIST: dict = {}      # spu -> 毛利率分布（_breakeven 的副产品，见其注释）


def _breakeven(spu: str) -> Optional[float]:
    """现算保本 ROI = 1 ÷ 到手价毛利率（按该 SPU 全部 SKU 取中位）。

    ★必须**现算**不能缓存：券促一变它就漂。无 Home 底料的新品退回
    「京东价 − 全成本」口径（没有任何促销时 到手价≈京东价，2026-08-19 实证成立）。
    """
    from blacklight.osw import margin, product
    d = product.product_sku_detail(int(spu), with_cost=True)
    rows = d.get("rows") or []
    sk = [str(r["skuId"]) for r in rows]
    if not sk:
        return None
    pr = margin.query_pricing_batch(sk, adv_source="osw24h")
    ms = []
    for r in rows:
        s = str(r["skuId"])
        p = pr.get(s)
        if p and p.get("actualPrice") and p.get("fullCost"):
            ap, fc = p["actualPrice"], p["fullCost"]
            if ap > 0:
                ms.append((ap - fc) / ap)
        elif r.get("jdPrice") and r.get("actualTotalCost"):   # 新品无底料 ⇒ 无促销 ⇒ 到手≈京东价
            jd, c = r["jdPrice"], r["actualTotalCost"]
            if jd > 0:
                ms.append((jd - c) / jd)
    if not ms:
        return None
    m = statistics.median(ms)
    # ★中位数在价格结构分裂时不稳：实测 10035611806847 九个 SKU 里 4 个毛利率为负，
    #   中位数卡在 +0.46% ⇒ 保本 ROI 215；**再掉一个 SKU 中位数就变负**。
    #   所以把分布一并返回，让调用方知道这个数稳不稳，别只看一个中位数。
    neg = sum(1 for x in ms if x <= 0)
    _BREAKEVEN_DIST[str(spu)] = {"SKU数": len(ms), "毛利率为负": neg,
                                 "中位毛利率%": round(m * 100, 2),
                                 "最低%": round(min(ms) * 100, 2),
                                 "最高%": round(max(ms) * 100, 2),
                                 "⚠️": ("过半 SKU 亏损，中位数不可靠" if neg >= len(ms) / 2
                                        else ("有 %d 款亏损，中位数随时会翻负" % neg) if neg else None)}
    return round(1 / m, 2) if m > 0 else None


def _verdict(util, ratio, days_live) -> str:
    if days_live is not None and days_live < 3:
        return "冷启动期(前3天不下结论)"
    if ratio is not None and ratio < 1.0:
        return "★真亏：达成低于保本 ⇒ 抬目标ROI 或停投"
    bits = []
    if util is not None:
        bits.append("花不动" if util < 0.30 else ("撞线" if util > 0.90 else "预算正常"))
    if ratio is not None:
        bits.append("薄利(1.0~1.3)" if ratio < 1.3 else "安全(≥1.3)")
    return " / ".join(bits) or "数据不足"


def snapshot(run_date: str = None, write: bool = True, note: str = "") -> dict:
    """拉某天的 5 个计划表现 + 现算保本 ROI，落 `runtime/ledger/newproduct_track.csv`。

    同 `run_date` 重跑覆盖当日那批（幂等）。`run_date` 留空 = 昨天
    （广告数据 T-1 才完整；取今天会拿到半天数据，判读会偏低）。
    """
    from blacklight.jzt import swa
    if not run_date:
        run_date = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    r = swa.ad_all(start_day=run_date, end_day=run_date)
    rows = [p for p in (r.get("rows") or []) if str(p.get("spuId") or "") in TRACKED]
    if not rows:
        raise BlacklightError(
            f"{run_date} 没取到这 5 个计划的数据。可能：① 计划当天还没建/没消耗 "
            f"② 日期太新（广告数据 T-1 才完整）。已追踪 SPU: {list(TRACKED)}")
    be = {spu: _breakeven(spu) for spu in {str(p.get("spuId")) for p in rows}}
    out = []
    for p in rows:
        spu = str(p.get("spuId"))
        cost, gmv = _f(p.get("花费")), _f(p.get("全站交易额"))
        budget, troi = _f(p.get("日预算")), _f(p.get("出价"))
        roi = round(gmv / cost, 2) if cost > 0 else None
        util = round(cost / budget, 3) if budget > 0 else None
        b = be.get(spu)
        ratio = round(roi / b, 2) if (roi and b) else None
        created = str(p.get("创建日") or "")[:10]
        days = None
        if created:
            try:
                days = (_dt.date.fromisoformat(run_date) - _dt.date.fromisoformat(created)).days + 1
            except ValueError:
                pass
        out.append({
            "run_date": run_date, "spuId": spu, "campaignId": p.get("campaignId"),
            "groupId": p.get("groupId"), "计划名": str(p.get("推广名") or "")[:24],
            "状态": p.get("状态"), "日预算": budget, "目标ROI": troi,
            "花费": cost, "展现": p.get("展现"), "点击": p.get("点击"),
            "订单行": p.get("全站订单行"), "成交额": gmv,
            "达成ROI": roi, "预算利用率": util, "保本ROI": b, "ratio": ratio,
            "判读": _verdict(util, ratio, days), "note": note or TRACKED.get(spu, ""),
        })
    if write:
        _append(out, run_date)
    return {"run_date": run_date, "count": len(out), "rows": out, "path": _path(),
            "_判据": "看 ratio=达成ROI÷保本ROI，不是看达成ROI本身；新品前 3 天不下结论。"}


def _append(rows: list, run_date: str) -> None:
    p = _path()
    old = []
    if os.path.isfile(p):
        with open(p, encoding="utf-8-sig", newline="") as f:
            old = [x for x in csv.DictReader(f) if x.get("run_date") != run_date]
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for x in old + rows:
            w.writerow({k: x.get(k, "") for k in COLS})


def history(spu: str = None, days: int = 14) -> dict:
    """读逐日账本。`spu` 给了只看那一个。用来判「是在爬坡还是一直不行」。"""
    p = _path()
    if not os.path.isfile(p):
        return {"rows": [], "_note": "还没有快照，先跑 snapshot()"}
    with open(p, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if spu:
        rows = [x for x in rows if x.get("spuId") == str(spu)]
    dates = sorted({x["run_date"] for x in rows})[-days:]
    rows = [x for x in rows if x["run_date"] in dates]
    rows.sort(key=lambda x: (x["run_date"], x["spuId"]))
    return {"rows": rows, "天数": len(dates), "path": p}
