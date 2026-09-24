"""京准通资金域：账户余额 + 续航测算。

网关同 swa（`cxjzt-api.jd.com`），但**信封是 `code==1` 不是 `success`**（见 `auth.post` 的 envelope）。

★ 这个账号的钱**全在红包里、现金为 0**（2026-08-05 实证 cashBalance=0 / awardBalance=40243.71）
  ⇒ 判断「还能撑几天」只能看红包，别去找现金余额。
"""
from __future__ import annotations

from typing import Optional

from blacklight.jzt import auth as jzt_auth


BALANCE_PATH = "/financecore/subaccount/allbalance/get"


def balance(pin: Optional[str] = None) -> dict:
    """[资金] **投放账户余额**（现金/红包/佣金 三本账，各带 可用/冻结/合计）。

    返回中文键 + `可投放合计`（= 现金可用 + 红包可用 + 佣金可用，即真正能烧的钱）。"""
    d = jzt_auth.post(BALANCE_PATH, {"requestFrom": 0}, pin=pin, envelope="code1")
    f = lambda k: float(d.get(k) or 0)                                   # noqa: E731
    cash, award, comm = f("cashBalance"), f("awardBalance"), f("commissionBalance")
    return {
        "现金可用": cash, "现金冻结": f("cashFreeze"), "现金合计": f("cashTotal"),
        "红包可用": award, "红包冻结": f("awardFreeze"), "红包合计": f("awardTotal"),
        "佣金可用": comm, "佣金冻结": f("commissionFreeze"), "佣金合计": f("commissionTotal"),
        "可投放合计": round(cash + award + comm, 2),
        "_raw": d,
        "_note": "红包可能有有效期/限定投放场景，本接口不返回——大额依赖红包时去页面确认过期时间。",
    }


def runway(daily_burn: float, days_needed: int = None, pin: Optional[str] = None) -> dict:
    """[资金] **续航测算**：余额 ÷ 日均消耗 = 还能撑几天。

    ⚠️`daily_burn` 必须是**最近一个完整日**的消耗，不是区间日均——账户起量前的零消耗日会把
    区间均值稀释（2026-08-05 实证：7天均值 2937 vs 真实基准 4208，差 30%，结论从「不够」翻成「够」）。
    今天的数不完整，不能用。关停计划后要用「基准日全账户消耗 − 基准日里仍暂停那批的消耗」。"""
    if not daily_burn or daily_burn <= 0:
        raise ValueError("daily_burn 必须 >0，且应取最近一个完整日的消耗")
    b = balance(pin)
    total = b["可投放合计"]
    can = total / daily_burn
    out = {"可投放合计": total, "日均消耗": round(daily_burn, 2), "可撑天数": round(can, 1),
           "构成": {"现金": b["现金可用"], "红包": b["红包可用"], "佣金": b["佣金可用"]},
           "_note": b["_note"]}
    if days_needed:
        out["需覆盖天数"] = days_needed
        out["够不够"] = can >= days_needed
        out["富余天数"] = round(can - days_needed, 1)
        out["需追加"] = 0 if can >= days_needed else round(daily_burn * days_needed - total, 2)
    return out
