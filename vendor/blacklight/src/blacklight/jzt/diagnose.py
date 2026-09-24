"""
jzt 广告**诊断引擎**：账户级体检 / 单计划漏斗归因 / 全站 3×3 定位。

前身是 `jx-ad-diagnose/scripts/ad_diagnose.py`（喂 CSV 的规则脚本），2026-08-04 打散并入 blacklight。
**最大的变化：不再吃 CSV，直接吃 `swa.ad_all()` 的实时数据** —— 原来"导表→改口径→喂脚本"
三步里最容易错的两处换算（花费折日均、CTR 百分数转分数）现在根本不存在，因为数据没离开过进程。

结论纪律（沿用指南，别丢）：
  - 每条结论必须落到 **漏斗某层 + 问题库某编号**，否则不下结论；
  - 一次只建议**一个动作**，并给**观察周期**（智能/全站 3~7 天，近7天≥30单才稳）；
  - 阈值是相对/经验值，全在 `playbook.TH`，别在这里再写一份。

★**盈亏口径**：本模块只判"广告效率"，**不判盈亏**。ROI 高 ≠ 赚钱（实测有 ROI 16 但投后毛利率 −9% 的 SKU）。
  要判真盈亏得接毛利。★**2026-08-11 起判亏权威是 ge 不是 osw**：走
  `pnl_margin_scan` / `ge.margin.judge_losing()`（投后口径、已扣广告）；
  `osw_margin_list_low` 已退役、`osw.is_losing` 降为预估口径。
  ★本域另需接手 `pnl_margin_scan` 输出的「**广告空耗**」——零成交但有广告消耗的款，
  亏损 100% 是广告费（实测当日 207 款 −207.50），**不在商品毛利线上**。
  见 `docs/jzt/NOTES_playbook.md` 与 `pnl/scan.py`。
"""
from __future__ import annotations

from typing import Optional

from blacklight.core import BlacklightError
from blacklight.jzt import playbook as pb
from blacklight.jzt import swa

TH = pb.TH


def _f(x, nd=2):
    return round(x, nd) if isinstance(x, (int, float)) else None


def _pct(x, d="—"):
    return f"{x * 100:.2f}%" if isinstance(x, (int, float)) else d


def _num(x, d="—"):
    return f"{x:.2f}" if isinstance(x, (int, float)) else d


def _F(level, code, target, evidence, action):
    """finding 容器。code 是问题库编号，前端/agent 可据此 `jzt_ad_playbook(code)` 取全文。"""
    return {"level": level, "code": code, "target": target, "依据": evidence, "动作": action}


def _plans(start_day=None, end_day=None, status=2, conversion_category=15, pin=None) -> dict:
    """取推广清单并换算成诊断用的口径。

    ★这里是**唯一**做日均换算的地方：接口 `cost` 是区间累计、`dayBudget` 是日值，
    `消耗/预算` 必须用日均比，否则每条计划都会被判成撞线。"""
    data = swa.ad_all(start_day=start_day, end_day=end_day, status=status,
                      conversion_category=conversion_category, pin=pin)
    days = data["天数"] or 1
    rows = []
    for r in data["rows"]:
        cost, budget = r["花费"], r["日预算"]
        daily = (cost / days) if isinstance(cost, (int, float)) else None
        rows.append({
            "name": r["推广名"], "campaignId": r["campaignId"], "groupId": r["groupId"],
            "spuId": r["spuId"], "skuIdList": r["skuIdList"],
            "budget": budget, "spend_total": cost, "spend": _f(daily),
            "impr": r["展现"], "clicks": r["点击"], "orders": r["全站订单行"],
            "amount": r["全站交易额"],
            # 接口的 CTR/CVR 是百分数(2.27)，内部统一用**分数**（与漏斗计算/格式化一致）
            "ctr": (r["点击率%"] / 100.0) if isinstance(r["点击率%"], (int, float)) else None,
            "cvr": (r["转化率%"] / 100.0) if isinstance(r["转化率%"], (int, float)) else None,
            "cpc": r["CPC"], "roi": r["全站投产比"], "target_roi": r["出价"],
            "spend_ratio": (daily / budget) if (daily is not None and budget) else None,
            "cpo": r["全站订单成本"], "新客": r["180天未购新客"],
            "起量可开": r["一键起量可开"], "出价可改次数": r["出价可改次数"],
            "已改价次数": r["已改价次数"],
        })
    return {"rows": rows, "days": days, "meta": {k: data[k] for k in
            ("日期区间", "天数", "转化周期", "账号", "完整", "truncated")},
            "合计": data["合计"]}


# --------------------------------------------------------------------------- #
# 账户级体检（钱 → 结构 → 货品 → 计划横比）
# --------------------------------------------------------------------------- #
def account_check(start_day: str = None, end_day: str = None, balance: float = None,
                  allocatable: float = None, gross_margin: float = None,
                  conversion_category: int = 15, pin: str = None,
                  budget_mode: str = "月度预算") -> dict:
    """**账户级体检**：按 钱→结构→货品→计划横比 找问题计划，结论带问题库编号。

    - `balance` 投放账户余额、`allocatable` 商家可分配现金：不给则跳过资金层。
    - `gross_margin` **到手价口径**毛利率（如 0.16）→ 保本 ROI=1/它。不给则不判亏损。
    - `budget_mode`：`月度预算`（京喜部分运营即此机制，当月花完即止）会**跳过 A1 充值建议**，
      因为那条只适用「可充值余额」；固定月度预算下正确的抓手是**预算节奏**（见 `monitor.budget_pace`）。
    """
    d = _plans(start_day, end_day, 2, conversion_category, pin)
    plans, days = d["rows"], d["days"]
    findings = []
    total_budget = sum(p["budget"] for p in plans if p["budget"])
    total_daily_spend = sum(p["spend"] for p in plans if p["spend"])
    be = pb.breakeven_roi(gross_margin)

    # ★★**取数完整性哨兵**（2026-08-06 加）：这个接口会**瞬时失败但不报错**，
    #   返回一批 花费/展现 全 0 的行。后果不是"看着不对"，而是**给出方向相反的确定性结论**——
    #   实证：某次调用返回全 0，体检直接判「🔴需处理 + 60 条计划花不动，建议降目标ROI」，
    #   而账户实际那 7 天花了 2.3 万、ROI 9.58（重跑即正常）。
    #   若按那份结论去降目标 ROI，会把一个只有 4% 安全垫的账户直接打穿保本线。
    # ∴ 宁可拒答也不能给错的确定结论：有计划但**全员零花费零展现**时直接判为取数异常。
    if plans:
        zero_spend = sum(1 for p in plans if not p.get("spend"))
        zero_impr = sum(1 for p in plans if not p.get("impr"))
        if zero_spend == len(plans) and zero_impr == len(plans):
            return {
                "错误": "取数异常：%d 个在投计划的花费与展现**全部为 0**" % len(plans),
                "判读": "这几乎不可能是真实业务状态（真断投也会有历史展现）。"
                        "更可能是接口瞬时失败但未抛错。**本次不出结论**——"
                        "带着全 0 数据做体检会给出方向相反的建议（把正常账户判成『花不动，该降目标ROI』）。",
                "下一步": "隔几秒重跑 jzt_ad_account_check；若仍全 0，用 jzt_swa_summary "
                          "指定日期区间交叉验证，确认是真断投还是取数问题。",
                "日期区间": f"{d.get('start')}~{d.get('end')}" if d.get("start") else None,
                "计划数": len(plans),
            }

    # ---- ① 资金层 ----
    if allocatable and balance == 0:
        findings.append(_F("🔴", "A2", "账户",
                           f"商家可分配现金 {_num(allocatable)} 元，但投放账户余额为 0",
                           pb.problem("A2")["动作"]))
    if balance is not None and total_budget:
        ratio = balance / total_budget
        spend_days = (balance / total_daily_spend) if total_daily_spend else None
        if budget_mode == "月度预算":
            findings.append(_F("🔵", "A1", "账户",
                               f"余额 {_num(balance)} / 单日总预算 {_num(total_budget)} = {ratio:.1f}x —— "
                               f"**但全站日预算是没人碰得到的天花板，这个比值无意义**；"
                               f"按实际日均消耗 {_num(total_daily_spend)} 算还能撑 "
                               f"{_num(spend_days)} 天",
                               "固定月度预算机制下不适用『充值到3~5倍』；抓手是**预算节奏**"
                               "（jzt_ad_budget_pace），防月末花光断投"))
        elif ratio < TH["balance_ratio"]:
            findings.append(_F("🔴" if ratio < 1 else "🟡", "A1", "账户",
                               f"投放账户余额 {_num(balance)} / 单日总预算 {_num(total_budget)} "
                               f"= {ratio:.1f}x（建议 {TH['balance_ratio']:.0f}~5x）；"
                               f"按实际日均消耗算可撑 {_num(spend_days)} 天",
                               pb.problem("A1")["动作"]))

    # ---- ② 结构层 ----
    for p in plans:
        b = p["budget"]
        if b and b < TH["plan_budget_low"]:
            capped = p["spend_ratio"] is not None and p["spend_ratio"] >= TH["spend_capped"]
            if capped or p["spend"] == 0:
                findings.append(_F("🟡", "KG2", p["name"],
                                   f"单计划日预算仅 {_num(b)}（<{TH['plan_budget_low']:.0f}）"
                                   + ("，且已撞线" if capped else "，且无消耗"),
                                   pb.problem("KG2")["动作"]))

    # ---- ③ 货品层 ----
    for p in plans:
        if (p["spend"] or 0) > 0 and not p["orders"]:
            findings.append(_F("🟡", "A4", p["name"],
                               f"有消耗 {_num(p['spend_total'])}（{days}天累计）但订单=0（疑似新品空烧）",
                               pb.problem("A4")["动作"]))

    # ---- ④ 计划横比层 ----
    rois = sorted([p["roi"] for p in plans if p["roi"] is not None])
    med = rois[len(rois) // 2] if rois else None
    total_spend_all = sum(p["spend_total"] for p in plans if p["spend_total"]) or 0
    for p in plans:
        sr, roi = p["spend_ratio"], p["roi"]
        if sr is not None:
            if sr < TH["spend_slow"]:
                guard = pb.thin_margin_guard(roi, be)
                act = (f"全站花不动多是『目标ROI设太高』(QZ4) → 按 3×3 表(QZ5)定格子。"
                       f"★但先过薄利护栏：{guard['结论']}")
                findings.append(_F("🟡", "P1/QZ4", p["name"],
                                   f"日均消耗/预算 = {sr:.0%}（花不动）| 展现 {_num(p['impr'])} "
                                   f"CTR {_pct(p['ctr'])} | 达成ROI {_num(roi)} 目标 {_num(p['target_roi'])}",
                                   act))
            elif sr >= TH["spend_capped"]:
                tag = "效果达成、加预算" if (roi and be and roi >= be) else "先拆漏斗再决定是否扩"
                findings.append(_F("🔵", "P2", p["name"],
                                   f"日均消耗/预算 = {sr:.0%}（撞线）| ROI {_num(roi)}", f"见分支P2：{tag}"))
        if roi is not None and be and roi < be:
            findings.append(_F("🔴", "亏损/A5", p["name"],
                               f"达成ROI {_num(roi)} < 保本ROI {_num(be)}"
                               f"（毛利率{_pct(gross_margin)}，**到手价口径**）",
                               "确认是否可接受(拉新/上升期A6)；否则按漏斗归因(P3)找瓶颈或换品/换渠道(A5)。"
                               "★薄利品应**升目标ROI或优化品/关停**，不是降目标"))
        if (roi is not None and med and roi < 0.5 * med and p["spend_total"]
                and total_spend_all and p["spend_total"] / total_spend_all > 0.1):
            findings.append(_F("🟡", "拖后腿", p["name"],
                               f"ROI {_num(roi)} < 账户中位 {_num(med)} 的一半，"
                               f"却占了 {p['spend_total'] / total_spend_all:.0%} 的消耗",
                               "重点下钻该计划（jzt_ad_plan_funnel）优先优化或降其消耗占比"))

    score = max(0, 100 - sum({"🔴": 25, "🟡": 12, "🔵": 0}.get(f["level"], 0) for f in findings))
    health = "🔴需处理" if score < 60 else ("🟡关注" if score < 80 else "🔵健康")
    order = {"🔴": 0, "🟡": 1, "🔵": 2}
    return {**d["meta"],
            "摘要": {"计划数": len(plans), "单日总预算": _f(total_budget),
                     "实际日均消耗": _f(total_daily_spend),
                     "预算利用率%": _f(total_daily_spend / total_budget * 100) if total_budget else None,
                     "投放账户余额": balance, "预算机制": budget_mode,
                     "保本ROI": be, "毛利率口径": "到手价" if gross_margin else None},
            "健康分": score, "健康度": health, "合计": d["合计"],
            "findings": sorted(findings, key=lambda x: order.get(x["level"], 3)),
            "_纪律": "一次只做一个动作，观察满周期(全站3~7天)再调；ROI 只是广告效率，判盈亏要接毛利。"}


# --------------------------------------------------------------------------- #
# 单计划漏斗归因（CTR/CVR/CPC 对基准找瓶颈层）
# --------------------------------------------------------------------------- #
def plan_funnel(plan: str = None, start_day: str = None, end_day: str = None,
                benchmark_ctr: float = None, benchmark_cvr: float = None,
                benchmark_cpc: float = None, conversion_category: int = 15,
                top: int = 20, pin: str = None) -> dict:
    """**单计划漏斗归因**：把 CTR/CVR/CPC 跟基准比，取偏离最差的一层当瓶颈，给问题库编号。

    基准优先级：显式传入 > 账户加权均值（本函数默认）。类目基准更准，能拿到就用 `benchmark_*` 传。
    `plan` 为计划名子串筛选；不传则按花费降序看前 `top` 条。
    ROI ≈ CTR × CVR × 客单 / CPC —— 所以瓶颈只可能在这三层之一。"""
    d = _plans(start_day, end_day, 2, conversion_category, pin)
    plans = d["rows"]
    ti = sum(p["impr"] for p in plans if p["impr"]) or 0
    tc = sum(p["clicks"] for p in plans if p["clicks"]) or 0
    to = sum(p["orders"] for p in plans if p["orders"]) or 0
    ts = sum(p["spend_total"] for p in plans if p["spend_total"]) or 0
    bench = {"ctr": (tc / ti) if ti else None, "cvr": (to / tc) if tc else None,
             "cpc": (ts / tc) if tc else None}
    for k, v in (("ctr", benchmark_ctr), ("cvr", benchmark_cvr), ("cpc", benchmark_cpc)):
        if v:
            bench[k] = v

    targets = [p for p in plans if plan in (p["name"] or "")] if plan else \
        sorted(plans, key=lambda p: -(p["spend_total"] or 0))[:top]

    out = []
    for p in targets:
        phenom = None
        sr = p["spend_ratio"]
        if sr is not None:
            if sr < TH["spend_slow"]:
                phenom = f"花不动(日均消耗/预算 {sr:.0%})"
            elif sr >= TH["spend_capped"]:
                phenom = f"撞线(日均消耗/预算 {sr:.0%})"
        if (p["spend"] or 0) > 0 and not p["orders"]:
            phenom = "有消耗无订单(新品空烧A4)"

        layers = []
        if p["ctr"] and bench["ctr"]:
            dev = 1 - p["ctr"] / bench["ctr"]
            if dev > TH["funnel_dev"]:
                layers.append((dev, "CTR低(门头)",
                               f"CTR {_pct(p['ctr'])} vs 基准 {_pct(bench['ctr'])}",
                               ["KG4", "KW1", "KW8", "KG5", "RQ4"]))
        if p["cvr"] and bench["cvr"]:
            dev = 1 - p["cvr"] / bench["cvr"]
            if dev > TH["funnel_dev"]:
                layers.append((dev, "CVR低(店内)",
                               f"CVR {_pct(p['cvr'])} vs 基准 {_pct(bench['cvr'])}",
                               ["A4", "KW1"]))
        if p["cpc"] and bench["cpc"]:
            dev = p["cpc"] / bench["cpc"] - 1
            if dev > TH["funnel_dev"]:
                layers.append((dev, "CPC高(成本)",
                               f"CPC {_num(p['cpc'])} vs 基准 {_num(bench['cpc'])}",
                               ["KW6", "KW11", "A6"]))
        layers.sort(key=lambda x: -x[0])
        out.append({"计划": p["name"], "campaignId": p["campaignId"], "groupId": p["groupId"],
                    "现象": phenom, "ROI": p["roi"], "目标ROI": p["target_roi"],
                    "CTR": _pct(p["ctr"]), "CVR": _pct(p["cvr"]), "CPC": p["cpc"],
                    "瓶颈层": layers[0][1] if layers else "各分项接近基准",
                    "归因": [{"层": l[1], "依据": l[2], "问题库": l[3]} for l in layers]})
    return {**d["meta"],
            "账户基准": {"CTR": _pct(bench["ctr"]), "CVR": _pct(bench["cvr"]), "CPC": _f(bench["cpc"])},
            "count": len(out), "rows": out,
            "_提示": "基准用的是账户加权均值；有类目基准请用 benchmark_* 传入，结论更准。"}


# --------------------------------------------------------------------------- #
# 全站 3×3 决策表定位（QZ5）—— 全站最核心的调优动作来源
# --------------------------------------------------------------------------- #
def grid_3x3(start_day: str = None, end_day: str = None, gross_margin: float = None,
             conversion_category: int = 15, top: int = 30, pin: str = None) -> dict:
    """**把每条推广落到 QZ5 的 3×3 格子里**，给出该格的动作与观察周期。

    行（消耗速度）：本工具只能从日均消耗率区分「花不动」与「跑满」——
    **「跑满且到23点后」vs「下午4点前花完」分不出来**（接口无分时数据），
    跑满的一律按「跑满且到23点后」取建议，要精确得去后台看分时曲线。
    列（ROI达成）：达成ROI / 目标ROI，<0.8 / 0.8~1.2 / >1.2。

    ★每条都会过**薄利护栏**：`达成ROI/保本ROI ≤1.3` 时，即使格子建议"降目标"也会被改写成
    "别降"——这是实战反馈对 QZ5 的修正，降下去会「花钱飞快+达成ROI跳水」打到保本线下。

    ⚠️⚠️**`护栏倍数` 是账户级判据，不是计划级盈亏结论**（2026-08-07 踩坑，务必别再误读）：
      `be = breakeven_roi(gross_margin)` 只算**一次**，对所有计划用**同一个**账户毛利率。
      所以 `护栏倍数 = 该计划达成ROI ÷ **账户**保本ROI`，它回答的是
      「这条计划的 ROI 是否低于账户平均保本线」，**不是**「这条计划亏不亏」。
      当时据此点名 3 条计划「卖越多亏越多、日均 316 元」，接上 SKU 级广告表后实测**三条全部盈利**
      （投后毛利 +71 / +738 / +45）。
      判单条计划盈亏必须用**该计划自己 SKU 的毛利**，且优先接 SKU 级广告表的「预估投后履约毛利」；
      没有 SKU 级花费时只能给区间（最高毛利SKU都撑不住⇒确定亏；最低毛利SKU都够⇒确定不亏；中间⇒不确定）。"""
    d = _plans(start_day, end_day, 2, conversion_category, pin)
    be = pb.breakeven_roi(gross_margin)
    rows = []
    for p in sorted(d["rows"], key=lambda x: -(x["spend_total"] or 0))[:top]:
        sr, roi, tgt = p["spend_ratio"], p["roi"], p["target_roi"]
        if sr is None or not roi or not tgt:
            continue
        speed = pb.SPEED[0] if sr < TH["spend_slow"] else pb.SPEED[1]
        att = roi / tgt
        attain = (pb.ATTAIN[0] if att < TH["roi_attain_low"] else
                  pb.ATTAIN[2] if att > TH["roi_attain_high"] else pb.ATTAIN[1])
        cell = pb.grid(speed, attain)
        guard = pb.thin_margin_guard(roi, be)
        action, overridden = cell.get("动作"), None
        if "降" in (action or "") and guard.get("可降目标") is False:
            overridden = action
            action = "**不要降目标**（薄利护栏否决）→ 维持现状；真要动只能升目标/优化品/关停"
        rows.append({
            "计划": p["name"], "campaignId": p["campaignId"], "groupId": p["groupId"],
            "日均消耗": p["spend"], "日预算": p["budget"], "消耗率%": _f(sr * 100, 1),
            "达成ROI": roi, "目标ROI": tgt, "达成倍数": _f(att),
            "格子": f"{speed} × {attain}", "判读": cell.get("判读"),
            "动作": action, "观察周期": cell.get("观察"),
            "薄利护栏": guard.get("结论"), "护栏倍数": guard.get("ratio"),
            "★原表建议已被否决": overridden,
        })
    blocked = [r for r in rows if r["★原表建议已被否决"]]
    return {**d["meta"], "保本ROI": be, "count": len(rows), "rows": rows,
            "被薄利护栏否决的条数": len(blocked),
            "_行维度限制": "接口无分时数据，「跑满且到23点后」与「下午4点前花完」无法区分，"
                          "跑满的一律按前者取建议；要精确请看后台分时曲线。",
            "_纪律": "按格子只做**一个**动作，观察满周期再重新定位；系统回溯近7天，别看单天、别一天多调。"}


# ---------- 计划级薄利护栏（2026-08-11 建，原先只在 scratchpad 手工算） ----------
def plan_guard(start: str = None, end: str = None, max_budget_rate: float = 50.0,
               min_cost: float = 50.0, pin: str = None) -> dict:
    """**花不动的计划该不该降目标 ROI** —— 用 ge 实际成交算保本 ROI，逐个过薄利护栏。

    `account_check` 会报「日均消耗/预算低 ⇒ 目标 ROI 设太高(QZ4)」，但它拿不到毛利率，
    只能停在「**先把到手价口径毛利率取到**再谈调目标」。本函数补的就是这一步。

    ## ★口径：毛利率 = ge `预估毛利_投前` ÷ `成交金额`
    · **必须用「投前」**：`预估毛利_投后` 已扣广告费，再拿它算保本 ROI 是**双扣**（铁律，见
      `jzt-ad-sku-level-pnl` 的教训）。
    · `成交金额` 是**实付**金额 ⇒ 天然就是「到手价口径」，正是 `breakeven_roi` 要的那个。
    · 计划是 SPU 级、明细是 SKU 级 ⇒ 按 `skuIdList` 汇总（GMV 加权，不是简单平均）。

    ## ★实证推翻了一个默认假设（2026-08-11）
    playbook 里「收纳类薄利、高目标 ROI 往往是刻意护栏」是按**京东价**毛利率推的。
    按 ge 到手价实算：60 个花不动的计划毛利率 **15.6%~30.7%（中位 20%）**、
    保本 ROI 3.26~6.43、`达成/保本` **1.35~2.48 全部过线** ⇒ **不是护栏在保命，是目标真设高了**。
    ⚠️所以这条护栏**必须真算，不能靠类目印象走捷径**——两个方向的结论是反的。

    ⚠️`出价可改次数` 有限（实测 5 次/计划）⇒ 返回按 `ratio` 排序，**分批调、别一次全下**。
    """
    import datetime as _dt
    from blacklight.ge import margin as _gm
    if not end:
        end = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    if not start:
        start = (_dt.date.fromisoformat(end) - _dt.timedelta(days=6)).isoformat()

    a = swa.ad_all(start_day=start, end_day=end, pin=pin)
    plans = a.get("rows") or []
    cand = [p for p in plans
            if (p.get("预算利用率%") or 0) < max_budget_rate
            and (p.get("花费") or 0) > min_cost
            and (p.get("全站投产比") or 0) > 0]
    skus = sorted({str(s) for p in cand for s in (p.get("skuIdList") or [])})
    if not skus:
        return {"候选": 0, "rows": [], "_note": "没有符合条件的花不动计划"}

    g = _gm.drill(start, end, degree="sku", realtime=False, sku_ids=skus)
    by = {str(r.get("sku_id")): r for r in (g.get("rows") or [])}
    # ★阴性对照：列名漂了就炸，别静默算出「毛利率全 0 ⇒ 全都别降」
    if not any(r.get("预估毛利_投前") is not None for r in by.values()):
        raise BlacklightError(
            "ge drill 里没有『预估毛利_投前』列——列名可能已漂移。"
            "拒绝按 0 毛利出结论（2026-08-11 就是这么算出假的『护栏拦下 60』的）")

    rows = []
    for p in cand:
        ss = [str(s) for s in (p.get("skuIdList") or [])]
        gmv = sum(float(by.get(s, {}).get("成交金额") or 0) for s in ss)
        prof = sum(float(by.get(s, {}).get("预估毛利_投前") or 0) for s in ss)
        if gmv <= 0:
            continue
        m = prof / gmv
        be = pb.breakeven_roi(m)
        att = p.get("全站投产比")
        gd = pb.thin_margin_guard(att, be) if be else {
            "可降目标": False, "结论": "毛利率≤0：先止亏，别谈扩量"}
        ratio = gd.get("ratio")
        tgt = p.get("出价")            # 全站营销：出价 = 目标成交投产比
        cut = 0.15 if (ratio or 0) >= 1.8 else (0.10 if (ratio or 0) >= 1.5 else 0.05)
        new = (max(round(tgt * (1 - cut), 2), round(be * 1.3, 2))
               if (tgt and be and gd.get("可降目标")) else None)
        rows.append({
            "计划": p.get("推广名"), "campaignId": p.get("campaignId"),
            "日预算": p.get("日预算"), "预算利用率%": p.get("预算利用率%"),
            "花费": p.get("花费"), "SKU数": len(ss), "GMV": round(gmv, 0),
            "毛利率%": round(m * 100, 1), "达成ROI": att, "保本ROI": be, "ratio": ratio,
            "目标ROI": tgt, "可降目标": gd.get("可降目标"), "建议新目标": new,
            "降幅%": (round((1 - new / tgt) * 100, 1) if (new and tgt) else None),
            "出价可改次数": p.get("出价可改次数"), "结论": gd.get("结论"),
            "ge覆盖": sum(1 for s in ss if s in by),
        })
    rows.sort(key=lambda r: -(r["ratio"] or 0))
    ok = [r for r in rows if r["可降目标"]]
    return {
        "日期区间": "%s~%s" % (start, end), "计划总数": len(plans), "花不动候选": len(cand),
        "可算毛利": len(rows), "可降目标": len(ok), "护栏拦下": len(rows) - len(ok),
        "ge覆盖SKU": "%d/%d" % (len(by), len(skus)), "rows": rows,
        "_note": "★出价可改次数有限(实测5次/计划)：按 ratio 从高到低分批调，"
                 "先探针再铺开；调完 2~3 天用 ad_daily_compare 回读，别当天判断。",
    }


# ---------- 广告空耗（2026-08-12 从毛利监控迁入：这是投放效率问题，不是商品定价问题） ----------
def ad_waste(days: int = 7, min_cost: float = 10.0, top: int = 30,
             pin: str = None, end_date: str = None) -> dict:
    """**零成交却在花广告费的 SKU** —— 用户 2026-08-12 定的边界：
    毛利监控只**提一句 + 报极端值**，全量处置放这儿。

    ## ★口径两个坑（都实测撞过）
    1. **必须用「近 N 日」不能用「今日」**。今日零成交的款可能只是**还没到出单的时候**，
       拿它去拉黑会误杀正常品。七天一单没出还在花钱，那才叫空耗。
    2. **零成交行的 `单量` 恒为 1（占位值）** —— 筛选条件必须是 `成交金额 == 0`，
       用 `单量 == 0` 会**一款都筛不出来**（`ge.margin.classify` 的 docstring 早写了，我照样踩）。

    ## 实测分布（2026-08-05~08-11）
        2,220 款、合计 8,410.56 元、单款中位 1.20 / P90 8.33 / **max 179.22**
        ≥1 元 1205 款(占额 95%) ｜ ≥3 元 611(82%) ｜ **≥10 元 177(54%)** ｜ ≥20 元 69(36%)
    **8% 的款占掉一半金额** ⇒ 别按款数处置，按金额切，默认门槛 `min_cost=10`。

    ## 处置分流（广告计划是 SPU 维度设的，SKU 明细只能喂拉黑/释放）
      · 计划里**只有个别 SKU 空耗** → 拉黑那几个（`jzt_swa_sku_black_*`）
      · 计划里**全部 SKU 都空耗**   → 别拉黑，直接停投 / 调预算
    ⚠️三个硬约束：`skublack/update` 是**全量覆盖不是追加**（先 query 现状再求并集）、
      单元黑名单**上限 50**（超了提交后才报且整批失败）、`skublack/query` 有**1 分钟限流**。
    """
    import datetime as _dt
    from blacklight.ge import ad as _ge_ad
    end = _dt.date.today() - _dt.timedelta(days=1)          # ge 离线到 T-1
    start = end - _dt.timedelta(days=days - 1)
    # ★2026-08-12 改为委托 ge.ad：广告运营口径（page_type=3 / @jx_ads_ord1 / 京喜自营），
    #   `消耗` 是**折后**（实际计费）。原先直接读毛利监控的 @jx 全量口径，同一天差 2 倍。
    w = _ge_ad.waste(days=days, min_cost=min_cost, erp=pin, top=500, end_date=end_date)
    if "★" in str(w.get("阴性对照")):
        raise BlacklightError("ge.ad 阴性对照失败，拒绝出空耗结论：%s" % w["阴性对照"])
    hit = [{"sku_id": x["skuId"], "广告消耗": x["消耗"]} for x in (w.get("Top") or [])]
    waste = hit
    total = w.get("空耗合计(折后)") or 0
    hit_amt = w.get("达标金额") or 0

    def _f(r, k):
        try:
            return float(r.get(k) or 0)
        except Exception:
            return 0.0

    # SPU 上卷：找这些 SKU 归属哪个在投计划，决定「拉黑」还是「停投」
    plans, by_plan = [], {}
    try:
        a = swa.ad_all(start_day=start.isoformat(), end_day=end.isoformat(), pin=pin)
        plans = a.get("rows") or []
    except Exception as e:
        by_plan = {"_error": "ad_all 取数失败：%s" % str(e)[:100]}
    if plans:
        wset = {str(r.get("sku_id")) for r in hit}
        for p in plans:
            ss = [str(s) for s in (p.get("skuIdList") or [])]
            if not ss:
                continue
            bad = [s for s in ss if s in wset]
            if not bad:
                continue
            by_plan[str(p.get("campaignId"))] = {
                "计划": p.get("推广名"), "在投SKU数": len(ss), "空耗SKU数": len(bad),
                "空耗金额": round(sum(_f(r, "广告消耗") for r in hit
                                      if str(r.get("sku_id")) in set(bad)), 2),
                "日预算": p.get("日预算"), "花费": p.get("花费"),
                "建议": ("★全部 SKU 都空耗 ⇒ 停投/调预算，别拉黑"
                         if len(bad) == len(ss) else
                         "拉黑这 %d 个 SKU（单元上限 50，先 query 现状再求并集）" % len(bad)),
                "空耗SKU": bad[:50],
            }
    return {
        "窗口": "%s~%s（%d 日）" % (start, end, days),
        "空耗款数": w.get("空耗款数"), "空耗合计(折后)": round(total, 2),
        "门槛": "单款 ≥%.1f 元" % min_cost,
        "达标款数": w.get("达标款数"), "达标金额": round(hit_amt, 2),
        "占空耗金额%": round(hit_amt / total * 100) if total else 0,
        "Top": [{"skuId": r.get("sku_id"), "消耗(折后)": round(_f(r, "广告消耗"), 2)}
                for r in hit[:top]],
        "按计划": by_plan,
        "_口径": "★近 %d 日**零成交**仍有广告消耗，**折后**(实际计费)口径，来自 ge.ad。"
                 "**别用今日口径**——今日零成交可能只是还没出单；"
                 "**别用 单量==0 筛**——零成交行单量恒为 1(占位值)；"
                 "★ge 指标里带 `_discount` 的是**折前**，不带的才是折后(实测比值 0.7000)" % days,
        "_下一步": "① 先 jzt_ad_ledger_compare 看『可放出』②按 `按计划` 分流拉黑/停投 "
                   "③ skublack/update 是全量覆盖，先 query 再求并集 ④ jzt_ad_ledger_append 记账(note 写已执行/仅建议)",
    }


def daily_ledger(run_date: str = None, days: int = 7, pin: str = None,
                 note: str = "", write: bool = True, end_date: str = None,
                 source: str = "download") -> dict:
    """★★**每日广告诊断的收尾动作：把「全量在投 SKU」写进分析账本**（用户 2026-08-18 定为日常必做）。

    ## 为什么必须是「全量」而不是「本次动手的那几款」
    账本是 `ledger.compare` 的地基。局部批次会让它**彻底失效**：
    实测 08-17 只写了 5 条、08-10 是 1167 条，交集仅 5 ⇒ compare 报「可放出 0」，
    看着像"没有机会"，实际是**没得比**。补跑一次全量后立刻挖出 **113 款**
    拿掉广告仍赚钱（到手价毛利率 ≥12%，最高 61%）却被长期摁在黑名单里的品。

    ## ★为什么必须给已拉黑的补 `ds_margin`
    **被拉黑的 SKU 从此不产生任何广告数据** ⇒ `post_margin`（投后毛利率）永远取不到值。
    只用投后毛利率判「可放出」的话，**任何拉黑款都不可能被放出来**，跑多少次全量都不自愈
    （账本自己警告过"拉黑了就再也没放回来，把本来能赚钱的品永久摁死"）。
    `compare` 的放出线本就留了后门——「投后≥0 **或** 到手价毛利≥12%」——`ds_margin` 就是它，
    数据源是 ge 毛利监控（自然销量口径）：`预估毛利_投前 ÷ 成交金额`。

    ## 口径
    · 在投款：`ge.ad.drill(page_type=3 / 京喜自营 / 折后消耗)`，`post_margin = 广告投后毛利 ÷ 广告成交金额`
    · 已拉黑：`swa.sku_black_scan()`（带 95% 覆盖率闸）+ `ge.margin.drill(sku_ids=…)` 补 `ds_margin`
    · SPU/计划名：`swa.ad_all()` 的 `skuIdList`（已排除被拉黑的，是"当前真在投"）
    · **cause 文案避开触发词**（拉黑/剔出/黑名单/持续亏损/关停/暂停/放出/恢复/复核/边际/待定/观察）——
      `ledger._norm_verdict` 虽已改为"显式 verdict 优先"，但别再依赖兜底。

    ⚠️**先按日体检再聚合**：ge 广告 tab 出现过整日交易侧全零而消耗照常，
      混进去会把当天算成巨亏。本函数自动剔除「消耗>0 且成交=0」的整日（记在 `跳过的脏日`）。
    """
    import datetime as _dt
    from blacklight.ge import ad as _ge_ad
    from blacklight.ge import margin as _ge_margin
    from blacklight.jzt import ledger as _ledger

    # ★窗口：默认「截至昨天的 days 天」；`end_date` 显式指定窗口末日 ⇒ 可回填历史。
    #   ge 广告离线本就按 `dt>=start / dt<=end` 取（抓包实证），窗口自由，
    #   **回填别去伪造「今天」**（2026-08-21：monkeypatch datetime 是错解法，已改成本参数）。
    #   回填单日：daily_ledger(run_date=D, days=1, end_date=D) ⇒ 窗口 D~D。
    if end_date:
        end = _dt.date.fromisoformat(str(end_date))
        if end >= _dt.date.today():
            raise BlacklightError("end_date 必须 ≤ 昨天（ge 离线到 T-1），收到 %s" % end_date)
    else:
        end = _dt.date.today() - _dt.timedelta(days=1)         # ge 离线到 T-1
    start = end - _dt.timedelta(days=days - 1)
    run_date = run_date or _dt.date.today().isoformat()

    # ---- ① 按日体检，剔脏日 ----
    dirty, good = [], []
    # ★取数源（2026-08-21）：`download` = ge 下载中心全科目明细（默认），
    #   `drill` = 老的 lowCodeDataQuery（指标少，保留做退路）。
    #   两者已逐项对账：成交金额/消耗折后/消耗折前/广告投后毛利/广告履约毛利 **分毫不差**，
    #   换源不改 `post_margin` 口径（都用 saler_view）⇒ 历史 run 仍可比。
    _day_cache = {}

    def _fetch(d):
        if d in _day_cache:
            return _day_cache[d]
        if source == "drill":
            rs = _ge_ad.drill(d, d, degree="sku", erp=pin)["rows"]
        else:
            raw = _ge_ad.download_detail(d, d, erp=pin, degree="sku")["rows"]
            rs = []
            for r in raw:
                sid = str(r.get("商品ID") or "").strip()
                if not sid.isdigit():           # 合计行/空行，别混进来
                    continue
                def _f(k):
                    try:
                        return float(r.get(k) or 0)
                    except (TypeError, ValueError):
                        return 0.0
                rs.append({
                    "sku_id": sid,
                    "广告成交金额": _f("成交金额"), "消耗": _f("消耗_折后"),
                    "消耗_折前": _f("消耗_折前"), "广告投后毛利": _f("广告投后毛利"),
                    "广告履约毛利": _f("预估投后履约毛利"),
                    "裸毛利": round(_f("预估投后履约毛利") + _f("消耗_折前"), 2),
                    "单量": _f("成交父单量"), "商品成本": _f("商品成本"),
                    "物流成本": _f("物流成本"), "CPS佣服": _f("CPS佣服"),
                    "优惠券平台补贴": _f("优惠券平台补贴"), "事业部优惠券承担": _f("事业部优惠券承担"),
                    "事业部促销承担": _f("事业部促销承担"), "新人价补贴": _f("新人价补贴"),
                    "总红包金额": _f("总红包金额"), "平台承担红包": _f("平台承担红包"),
                })
        _day_cache[d] = rs
        return rs

    for i in range(days):
        d = (start + _dt.timedelta(days=i)).isoformat()
        try:
            rs = _fetch(d)
        except Exception:
            continue
        c = sum(x["消耗"] for x in rs); a = sum(x["广告成交金额"] for x in rs)
        (dirty if (c > 0 and a == 0) else good).append(d)
    if not good:
        raise BlacklightError("按日体检后没有可用日期（全部脏日或取数失败），拒绝写账本。")

    # ---- ② 在投款（逐日聚合，天然跳过脏日）----
    import collections as _c
    agg = _c.defaultdict(lambda: _c.defaultdict(float))
    FL = ["消耗", "消耗_折前", "广告成交金额", "广告投后毛利", "广告履约毛利", "裸毛利",
          "单量", "商品成本", "物流成本", "CPS佣服", "优惠券平台补贴",
          "事业部优惠券承担", "事业部促销承担", "新人价补贴", "总红包金额", "平台承担红包"]
    for d in good:
        for r in _fetch(d):
            for f in FL:
                agg[str(r["sku_id"])][f] += (r.get(f) or 0)

    # ---- ③ SPU / 计划名（skuIdList = 当前真在投）----
    sku2plan = {}
    try:
        a = swa.ad_all(start_day=start.isoformat(), end_day=end.isoformat(), pin=pin)
        for p in (a.get("rows") if isinstance(a, dict) else a) or []:
            for s in (p.get("skuIdList") or []):
                sku2plan.setdefault(str(s), p)
    except Exception:
        pass

    rows, cnt = [], _c.Counter()
    for k, v in agg.items():
        p = sku2plan.get(k) or {}
        amt, cost, post = v["广告成交金额"], v["消耗"], v["广告投后毛利"]
        if amt <= 0:
            vd = "拉黑" if cost >= 10 else "复核"
            cause = "近%d日零成交仍消耗%.2f元（空耗）" % (len(good), cost); pm = ""
        else:
            pm = round(post / amt, 4)
            vd = "保留" if pm >= 0.02 else ("复核" if pm >= 0 else "拉黑")
            cause = "近%d日投后毛利率%.1f%%、投后%.2f、裸毛利%.2f" % (len(good), pm * 100, post, v["裸毛利"])
        cnt[vd] += 1
        rows.append({"skuid": k, "spu": p.get("spuId", ""), "plan_name": p.get("推广名", "") or p.get("商品名", ""),
                     "ds_margin": "", "post_margin": pm, "cause": cause, "verdict": vd,
                     "spend": round(cost, 2), "roi": round(amt / cost, 2) if cost else 0,
                     "note": note or "全量在投快照",
                     # ★换源后多出的科目：答「亏在哪一项」
                     "ord_qty": round(v["单量"], 0), "gmv": round(amt, 2),
                     "sku_cost": round(v["商品成本"], 2), "delv_cost": round(v["物流成本"], 2),
                     "cps_fee": round(v["CPS佣服"], 2), "coupon_plat": round(v["优惠券平台补贴"], 2),
                     "coupon_bu": round(v["事业部优惠券承担"], 2), "promo_bu": round(v["事业部促销承担"], 2),
                     "newuser_sub": round(v["新人价补贴"], 2), "redpack_total": round(v["总红包金额"], 2),
                     "redpack_plat": round(v["平台承担红包"], 2), "spend_pre": round(v["消耗_折前"], 2)})

    # ---- ④ 已拉黑的补 ds_margin（否则永远放不出来）----
    black_info = {}
    try:
        scan = swa.sku_black_scan(start_day=start.isoformat(), end_day=end.isoformat(), pin=pin)
        black = [str(x) for x in scan.get("已拉黑") or {}]
        black_info = {"单元数": scan.get("单元数"), "覆盖率": scan.get("覆盖率"), "黑名单SKU数": len(black)}
        if black:
            got = {}
            for i in range(0, len(black), 500):                # ge sku_ids 上限 500
                for r in _ge_margin.drill(start.isoformat(), end.isoformat(), degree="sku",
                                          sku_ids=black[i:i + 500], realtime=False)["rows"]:
                    got[str(r["sku_id"])] = r
            for s in black:
                if s in agg:                                   # 还在投的不重复写
                    continue
                r = got.get(s)
                base = {"skuid": s, "spu": "", "plan_name": "", "post_margin": "", "spend": 0, "roi": 0,
                        "note": note or "当前不在投，用自然销量毛利判达标"}
                if not r:
                    base.update({"ds_margin": "", "verdict": "复核",
                                 "cause": "当前不在投且无自然销量，两项指标均缺⇒本轮判不了"})
                else:
                    am = r.get("成交金额") or 0; pre = r.get("预估毛利_投前") or 0
                    m = (pre / am) if am > 0 else None
                    base.update({"ds_margin": round(m, 4) if m is not None else "",
                                 "verdict": "保留" if (m is not None and m >= 0.12)
                                            else ("复核" if (m is not None and m >= 0) else "拉黑"),
                                 "cause": "当前不在投；自然流量成交%.2f、投前毛利%.2f、到手价毛利率%.1f%%"
                                          % (am, pre, (m or 0) * 100)})
                cnt[base["verdict"]] += 1
                rows.append(base)
    except Exception as e:
        black_info = {"error": str(e)[:120]}

    out = {"run_date": run_date, "窗口": "%s~%s" % (start, end), "跳过的脏日": dirty,
           "在投款": len(agg), "总行数": len(rows), "verdict分布": dict(cnt), "黑名单": black_info,
           "_口径": "在投=ge广告tab(page_type=3/京喜自营/折后)；已拉黑=补 ds_margin(ge毛利监控自然销量口径)",
           "_下一步": "跑 jzt_ad_ledger_compare 看『可放出』——它认的就是 ds_margin≥0.12 这条后门"}
    if write:
        out["written"] = _ledger.append(rows, run_date)
    else:
        out["rows_preview"] = rows[:5]
    return out
