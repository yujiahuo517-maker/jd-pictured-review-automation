"""流速 —— **排行一律按「近 N 日日均 vs 基线」，累计值只作参考列**。

## 为什么这条必须写进代码（2026-08-11 一天犯两次）
1. SKU：`osw_margin_triage` 的失血是 15 日滚动，D 桶 Top3 全是**已结束的脉冲**，
   8/04 起自行塌掉 91%——据那份清单动手会全打空。这条我守住了（先过 run_rate）。
2. **券：同一个错又犯了一遍。** 按 15 日累计，批次 `1315454844`
   占我券成本 **53.8% 排第一**；拆开看 前 8 日 59,605 → 近 7 日 **3,024（−95%）**，
   早已退潮。而真正在爆发的 `1446132971`（huwenjie50 的 8 折券）
   前 8 日只有 7 元、近 7 日 820 元（**126×**），按累计排根本进不了前列。

⇒ 「向后看的累计」这个陷阱**与维度无关**，SKU / 券 / 促销 / SPU 全都适用。
   所以流速做成通用函数，任何排行都从这里过一道。

## 状态判据
    爆发 ≥2× 基线   持续 0.5~2×   消退 <0.5×   新增 基线=0且近期>0   停止 近期=0
「消退」和「停止」**先别动**——它们已经不流血了，处置成本全是白花的。
"""
from __future__ import annotations

from blacklight.core import BlacklightError


def _days(start: str, end: str) -> int:
    import datetime as dt
    a = dt.date.fromisoformat(start[:10])
    b = dt.date.fromisoformat(end[:10])
    n = (b - a).days + 1
    if n <= 0:
        raise BlacklightError("窗口非法：%s~%s" % (start, end))
    return n


def rank(recent: dict, baseline: dict, *,
         recent_window: tuple, baseline_window: tuple,
         value_name: str = "值", meta: dict = None, top: int = None) -> dict:
    """通用流速排行。

    recent / baseline: {key: 数值}（同一口径！跨口径先折算，见 `caliber`）
    *_window: (start, end)，用于把累计折成**日均**——窗口长度不同却直接比累计，
              是另一种常见错法。

    返回按「近期日均」降序的行；每行带 倍数 与 状态。
    """
    rd = _days(*recent_window)
    bd = _days(*baseline_window)
    meta = meta or {}
    rows = []
    for k in set(list(recent) + list(baseline)):
        rv = float(recent.get(k) or 0)
        bv = float(baseline.get(k) or 0)
        rpd, bpd = rv / rd, bv / bd
        if bpd <= 0:
            ratio, state = (None, "新增") if rpd > 0 else (None, "停止")
        elif rpd <= 0:
            ratio, state = 0.0, "停止"
        else:
            ratio = rpd / bpd
            state = "爆发" if ratio >= 2 else ("消退" if ratio < 0.5 else "持续")
        row = {"key": k, "近期日均": round(rpd, 2), "基线日均": round(bpd, 2),
               "近期累计": round(rv, 2), "基线累计": round(bv, 2),
               "倍数": None if ratio is None else round(ratio, 2), "状态": state,
               "_值口径": value_name}
        row.update(meta.get(k) or {})
        rows.append(row)
    rows.sort(key=lambda r: -r["近期日均"])
    if top:
        rows = rows[:top]
    act = [r for r in rows if r["状态"] in ("爆发", "持续", "新增")]
    return {
        "窗口_近期": list(recent_window), "窗口_基线": list(baseline_window),
        "天数": {"近期": rd, "基线": bd},
        "合计_近期日均": round(sum(r["近期日均"] for r in rows), 2),
        "合计_基线日均": round(sum(r["基线日均"] for r in rows), 2),
        "水位%": round(100 * sum(r["近期日均"] for r in rows) /
                       (sum(r["基线日均"] for r in rows) or 1), 1),
        "状态分布": {s: sum(1 for r in rows if r["状态"] == s)
                     for s in ("爆发", "持续", "新增", "消退", "停止")},
        "待处置": len(act),
        # ★把「该动的」直接给出来，别让调用方自己从 rows 里筛——
        #   实测按「近期日均」排时，已消退的仍可能排在第 2（它确实还在花钱，
        #   只是在快速下降），光看排名很容易忽略状态标签。
        "rows_actionable": act,
        "rows": rows,
        "_用哪个": "★处置看 `rows_actionable`（已剔除 消退/停止）；"
                   "`rows` 是全量含已退潮的，只用于回看。",
        "_纪律": "★按「近期日均」排，**不要按累计**——累计是向后看的，"
                 "已退潮的会赖在榜首（2026-08-11 实证：某券按累计排第 1，"
                 "实际近 7 日已塌 95%）",
    }


def from_ge_drill(recent_result: dict, baseline_result: dict, *,
                  recent_window: tuple, baseline_window: tuple,
                  value_key: str = "减免_采销实担", top: int = None) -> dict:
    """直接吃 `ge.margin.drill()` 的两个窗口结果。

    value_key 常用：
      · `减免_采销实担` —— 券/促销吃掉多少（**采销承担口径**）
      · `预估毛利_投后` —— 毛利本身（注意负值排序方向）
    """
    dim = recent_result.get("dim") or baseline_result.get("dim")
    if not dim:
        raise BlacklightError("drill 结果缺 dim，无法取键")

    def _m(res):
        return {str(r.get(dim)): float(r.get(value_key) or 0) for r in res.get("rows", [])}

    NAME = {"batch_id": "jdr_sch_coupon_batch_name",
            "jdr_sch_page_promotion_id": "jdr_sch_promotion__act_name"}
    nk = NAME.get(dim)
    meta = {}
    for res in (recent_result, baseline_result):
        for r in res.get("rows", []):
            k = str(r.get(dim))
            meta.setdefault(k, {})
            if nk and r.get(nk):
                meta[k]["名称"] = r.get(nk)
            if r.get("预估毛利_投后") is not None:
                meta[k].setdefault("近期投后毛利", r.get("预估毛利_投后"))
    return rank(_m(recent_result), _m(baseline_result),
                recent_window=recent_window, baseline_window=baseline_window,
                value_name=value_key, meta=meta, top=top)
