"""广告成本估计量的**替换与兜底** —— 把 osw 的 24h 快照换成 ge 的多日实际。

## 为什么要换（2026-08-11 用户说明 + 实测）
osw 的 `jxAdvCost`（本模块记作 `advCost`）口径是：

    此刻至过去 **24 小时**的广告费用 ÷ 同期广告订单量        —— 供预估参考

**分母是 24 小时的广告订单量**，所以两个方向都会失真：
  · 分母很小（一两单）⇒ 比值**炸上天**。实测 `10184665292619` advCost **69.17 元/件**，
    而 ge 近 7 日实际单均广告只有 16.70（**4.1 倍**）；`10115381440318` 10.68 vs 2.19（**4.9 倍**）。
  · 24 小时内恰好没有广告订单 ⇒ **记 0**。实测 `10200474147226` osw 记 0、
    ge 近 7 日实际 2.99 —— 这个方向更危险：**高估毛利，可能把真亏的款报进去**。

35 款可比样本：osw 中位 2.99 vs ge 7 日 2.19；osw 偏高 21 款、偏低 14 款。
这也解释了铁律 3 里那个 `r=0.392` 的弱相关——不是数据错，是**窗口太短、分母太小**。

## 替换口径
    单均广告 = ge.drill(sku, 近 N 日).广告消耗 ÷ 单量

同一个口径（每单广告费），只是窗口从 24 小时拉到 7 日，方差小一个量级。

## ★兜底：别用一个小分母换掉另一个小分母
ge 侧单量 < `min_qty`（默认 5）时**不替换**，退回 osw 值并标 `adv_source='osw24h(ge样本不足)'`。
ge 不可用（未登录/接口漂移）时整体退回 osw，**标记而不静默**。
"""
from __future__ import annotations

import datetime as _dt

DEFAULT_DAYS = 7
DEFAULT_MIN_QTY = 5


def ge_adv_per_order(sku_ids, days: int = DEFAULT_DAYS,
                     min_qty: int = DEFAULT_MIN_QTY) -> dict:
    """取 ge 近 N 日的**实际**单均广告费。

    返回 {skuId: {"adv": 单均广告, "qty": 单量, "ok": 样本是否够}}。
    ge 不可用时返回 {}（调用方据此退回 osw，并标记来源）。
    """
    ids = [str(s) for s in (sku_ids or [])]
    if not ids:
        return {}
    try:
        from blacklight.ge import margin as ge_margin
    except Exception:
        return {}
    end = _dt.date.today() - _dt.timedelta(days=1)          # ge 离线到 T-1
    start = end - _dt.timedelta(days=days - 1)
    out = {}
    CH = 500                                                 # ge sku_id 筛选上限
    for i in range(0, len(ids), CH):
        part = ids[i:i + CH]
        try:
            g = ge_margin.drill(start.isoformat(), end.isoformat(),
                                degree="sku", realtime=False, sku_ids=part)
        except Exception:
            continue                                         # 某片失败不影响其余；缺的自然退回 osw
        for r in g.get("rows", []):
            q = float(r.get("单量") or 0)
            if q <= 0:
                continue
            out[str(r.get("sku_id"))] = {
                "adv": round(float(r.get("广告消耗") or 0) / q, 4),
                "qty": int(q), "ok": q >= min_qty,
            }
    return out


def apply(pricing_map: dict, source: str = "auto", days: int = DEFAULT_DAYS,
          min_qty: int = DEFAULT_MIN_QTY) -> dict:
    """把 `query_pricing_batch` 的结果按 `source` 替换广告项并**重算 fixedCost/fullCost**。

    source: `auto`(默认，能换就换、样本不足退回) / `ge{N}d` / `osw24h`(不替换)。

    ⚠️必须**同时重算 fixedCost 与 fullCost**——报名定价两条线分别用它们
    （百补用 fullCost、秒杀用 fixedCost），只改一个会让两条线口径分叉。
    """
    if source == "osw24h" or not pricing_map:
        for p in (pricing_map or {}).values():
            p.setdefault("advSource", "osw24h")
        return pricing_map

    ge = ge_adv_per_order(list(pricing_map.keys()), days=days, min_qty=min_qty)
    for sid, p in pricing_map.items():
        osw_adv = float(p.get("advCost") or 0)
        p["advCost_osw24h"] = osw_adv
        g = ge.get(str(sid))
        if not g:
            p["advSource"] = "osw24h(ge无数据)"
            p["advCost_ge"] = None
            continue
        p["advCost_ge"] = g["adv"]
        p["advQty_ge"] = g["qty"]
        if not g["ok"]:
            # ★别用一个小分母换掉另一个小分母
            p["advSource"] = "osw24h(ge样本不足 %d<%d 单)" % (g["qty"], min_qty)
            continue
        new_adv = g["adv"]
        delta = round(new_adv - osw_adv, 2)
        p["advCost"] = new_adv
        p["fixedCost"] = round(float(p["fixedCost"]) + delta, 2)
        p["fullCost"] = round(float(p["fullCost"]) + delta, 2)
        p["advSource"] = "ge%dd" % days
        p["advDelta"] = delta
    return pricing_map
