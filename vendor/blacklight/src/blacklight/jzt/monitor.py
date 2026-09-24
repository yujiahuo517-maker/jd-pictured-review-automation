"""
jzt 广告**日维度运营监控**：预算节奏 / 日环比异常 / 断投预警。

前身 `jx-ad-diagnose/scripts/daily_monitor.py`（喂今天+昨天两份 xlsx），现直接按日期区间取 swa 数据。

★**日报和周报的分工别混**（knowledge/07 的核心）：
  日报只做**当天该处理的运营故障** —— 预算节奏、日环比突变、断投；
  **不判毛利、不拉黑、不放出** —— 那些是周维度的结构决策（走 `ledger` + 毛利侧）。
  日数据噪声大，拿它做结构决策会天天推翻自己。

★**预算节奏是固定月度预算账户最关键的一条**：算"预计几号花完"，提前防月末断投
  （2026-06-28 那次断投就是没盯这个）。可充值余额的账户则看余额倍数（A1）。
"""
from __future__ import annotations

import calendar
import datetime as _dt
from typing import Optional

from blacklight.core import BlacklightError
from blacklight.jzt import playbook as pb
from blacklight.jzt import swa

TH = pb.TH


def _f(x, nd=2):
    return round(x, nd) if isinstance(x, (int, float)) else None


def _pct(x, d="—"):
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else d


def _chg(a, b):
    """环比变化率。b=0 或缺值时返回 None（不是 0）——避免把"从无到有"算成 0% 无变化。"""
    if a is None or b is None or not b:
        return None
    return a / b - 1


def budget_pace(monthly_budget: float, mtd_spend: float, date: str = None) -> dict:
    """**预算节奏**：按 MTD 消耗 vs 月预算 vs 日期，算"预计几号花完"并给红绿灯。

    只对**固定月度预算**账户有意义（当月花完即止、不能追加）。
    `date` 缺省取今天。"""
    if not monthly_budget or mtd_spend is None:
        raise BlacklightError("budget_pace 需要 monthly_budget 与 mtd_spend（本月累计消耗）。")
    d = _dt.date.fromisoformat(date) if date else _dt.date.today()
    dim = calendar.monthrange(d.year, d.month)[1]
    time_prog = d.day / dim
    bud_prog = mtd_spend / monthly_budget
    daily_rate = mtd_spend / d.day
    remain_days = max(1, dim - d.day)
    allowed_daily = (monthly_budget - mtd_spend) / remain_days
    finish_day = (d.day + (monthly_budget - mtd_spend) / daily_rate) if daily_rate > 0 else None

    if bud_prog > time_prog * 1.10:
        flag = "🔴 花太快"
        note = (f"预计约 {finish_day:.0f} 号就花完（月末是 {dim} 号）→ "
                f"现在起把日消耗压到 ≤{allowed_daily:,.0f}/天，防月末断投")
    elif bud_prog < time_prog * 0.90:
        flag = "🟡 花太慢"
        note = (f"进度落后，剩 {monthly_budget - mtd_spend:,.0f} 要在 {remain_days} 天花完"
                f"（可提到 ≤{allowed_daily:,.0f}/天），别月底浪费预算")
    else:
        flag = "🟢 节奏正常"
        note = f"预计约 {finish_day:.0f} 号花完，接近月末" if finish_day else "节奏正常"
    return {"日期": str(d), "flag": flag,
            "本月已花": _f(mtd_spend), "月预算": _f(monthly_budget),
            "预算进度%": _f(bud_prog * 100, 1), "时间进度%": _f(time_prog * 100, 1),
            "应有日均": _f(monthly_budget / dim), "实际日均": _f(daily_rate),
            "预计花完日": _f(finish_day, 1), "剩余天数": remain_days,
            "建议日消耗上限": _f(allowed_daily), "结论": note,
            "_适用": "仅对固定月度预算账户；可充值余额账户看 A1 余额倍数。"}


def _one_day(day: str, conversion_category: int, pin) -> dict:
    """取某一天的账户合计 + 每条推广的当日消耗。"""
    data = swa.ad_all(start_day=day, end_day=day, status=None,
                      conversion_category=conversion_category, pin=pin)
    per = {r["campaignId"]: r for r in data["rows"] if (r["花费"] or 0) > 0}
    t = data["合计"]
    return {"合计": t, "per": per, "完整": data["完整"]}


def daily_compare(day: str = None, prev_day: str = None, conversion_category: int = 15,
                  spend_jump: float = None, rate_drop: float = None,
                  dead_spend: float = None, pin: str = None) -> dict:
    """**日环比异常 + 断投预警**。`day` 缺省=今天，`prev_day` 缺省=前一天。

    ⚠️ 当天的数据是**未跑完的**（实时累计），跟完整的昨天比必然显示"下跌"。
    要判真异常请用 `day=昨天, prev_day=前天`，或至少知道今天这条是残缺对比。"""
    sj = spend_jump if spend_jump is not None else TH["spend_jump"]
    rd = rate_drop if rate_drop is not None else TH["rate_drop"]
    ds = dead_spend if dead_spend is not None else TH["dead_spend"]
    d1 = _dt.date.fromisoformat(day) if day else _dt.date.today()
    d0 = _dt.date.fromisoformat(prev_day) if prev_day else (d1 - _dt.timedelta(days=1))
    today, prev = _one_day(str(d1), conversion_category, pin), _one_day(str(d0), conversion_category, pin)

    hints = {"花费": "查计划状态/余额/撞线/出价", "点击率%": "查素材/掉打标/差评/缺货",
             "转化率%": "查商详/涨价/缺货/差评", "全站投产比": "查竞争/成交结构",
             "CPC": "查竞争/出价被改"}
    checks = [("花费", "both"), ("点击率%", "drop"), ("转化率%", "drop"),
              ("全站投产比", "drop"), ("CPC", "rise")]
    anomalies = []
    for key, direction in checks:
        tv, pv = today["合计"].get(key), prev["合计"].get(key)
        c = _chg(tv, pv)
        if c is None:
            continue
        thr = sj if key in ("花费", "CPC") else rd
        flag = None
        if direction in ("both", "drop") and c <= -thr:
            flag = "🔴 断崖↓"
        if direction in ("both", "rise") and c >= thr:
            flag = "🔴 突增↑"
        if flag:
            anomalies.append({"指标": key, "flag": flag, "昨": pv, "今": tv,
                              "变化%": _f(c * 100, 1), "排查": hints[key]})

    dead = []
    for cid, pr in prev["per"].items():
        if (pr["花费"] or 0) >= ds:
            tv = (today["per"].get(cid, {}) or {}).get("花费", 0) or 0
            if tv <= pr["花费"] * 0.1:
                dead.append({"campaignId": cid, "计划": pr["推广名"],
                             "昨消耗": pr["花费"], "今消耗": tv, "排查": "查暂停/断货/余额"})
    dead.sort(key=lambda x: -(x["昨消耗"] or 0))

    is_today = d1 == _dt.date.today()
    return {"日期": str(d1), "对比日": str(d0),
            "今日合计": today["合计"], "对比日合计": prev["合计"],
            "异常数": len(anomalies), "异常": anomalies or [{"flag": "🟢 账户级无显著突变"}],
            "断投数": len(dead), "断投": dead[:10],
            "阈值": {"消耗/CPC突变": sj, "率类断崖": rd, "断投起报消耗": ds},
            "⚠️": ("当天数据未跑完（实时累计），与完整的昨天比必然偏低——"
                   "要判真异常请用 day=昨天、prev_day=前天") if is_today else None,
            "_纪律": "日报只做运营预警(节奏/断投/突变)；毛利/拉黑/放出是周维度结构决策。"}
