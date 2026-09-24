"""口径标签与混用拦截。

## 为什么需要
2026-08-11 实撞：同一批 SKU 的券成本，
- ge「优惠券成本」 = **全额** 106,773
- easybi「采销承担」 = **87,274**（另有平台承担 19,496）
两者都对，但**把它们相加或互相替代就全错**：差 22.3%，而且按哪个排序会让
「找谁谈」的名单完全换人（chenlisha10 8.9% ↔ 64.8%）。

时效同理：ge 实时是 T-0（当日广告/物流未结算）、easybi 券集 T-1、京算盘 T-2~T-3。
**跨时效相减去做同比/环比，差的那部分全是结算滞后，不是业务变化。**

⇒ 金额一律带标签，聚合时**不同标签直接抛**，而不是悄悄算出一个数。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from blacklight.core import BlacklightError


class Caliber(str, Enum):
    """承担口径。"""
    FULL = "全额"          # 减免总额（ge「优惠券成本」是这个）
    SELF = "采销承担"       # ★判「我亏多少」用这个（easybi / osw 毛利监控）
    PLATFORM = "平台承担"   # 平台补贴，会以加项回到毛利桥里
    BU = "事业部承担"


class Freshness(str, Enum):
    """时效。"""
    RT = "rt"        # T-0 实时，当日；广告/物流未结算
    T1 = "T-1"       # easybi 券集 / ge 离线
    T2 = "T-2"       # 京算盘损益（实际 T-2~T-3）


@dataclass(frozen=True)
class Tagged:
    """带口径与时效的金额。"""
    value: float
    caliber: Caliber
    freshness: Freshness
    source: str = ""          # 'ge.margin' / 'easybi.coupon' / ...

    def __post_init__(self):
        if not isinstance(self.caliber, Caliber):
            raise BlacklightError("caliber 必须是 Caliber 枚举，收到 %r" % (self.caliber,))
        if not isinstance(self.freshness, Freshness):
            raise BlacklightError("freshness 必须是 Freshness 枚举，收到 %r" % (self.freshness,))

    def __repr__(self):
        return "Tagged(%.2f, %s, %s%s)" % (
            self.value, self.caliber.value, self.freshness.value,
            ", " + self.source if self.source else "")


def tagged_sum(items, *, allow_mixed_source: bool = True) -> Tagged:
    """求和，但**口径或时效不一致直接抛**。

    这是本层最核心的一道闸：宁可报错，也不要算出一个"看着像数、其实答非所问"的值。
    真要跨口径合并，先显式转换（例如用承担比例把全额折成采销承担），再求和。
    """
    items = [t for t in items if t is not None]
    if not items:
        raise BlacklightError("tagged_sum 收到空集合——空集合的口径无从判断，请显式处理")
    cals = {t.caliber for t in items}
    frs = {t.freshness for t in items}
    if len(cals) > 1:
        raise BlacklightError(
            "口径混用：%s。全额与采销承担相加没有意义（2026-08-11 实测差 22.3%%）。"
            "先折算到同一口径再求和。" % "、".join(sorted(c.value for c in cals)))
    if len(frs) > 1:
        raise BlacklightError(
            "时效混用：%s。跨时效相加/相减，差额里混着结算滞后，不是业务变化。"
            % "、".join(sorted(f.value for f in frs)))
    if not allow_mixed_source:
        srcs = {t.source for t in items if t.source}
        if len(srcs) > 1:
            raise BlacklightError("数据源混用：%s" % "、".join(sorted(srcs)))
    c, f = items[0].caliber, items[0].freshness
    src = items[0].source if len({t.source for t in items}) == 1 else "mixed"
    return Tagged(sum(t.value for t in items), c, f, src)


def to_self(full: Tagged, platform_ratio: float) -> Tagged:
    """把「全额」按平台承担比例折成「采销承担」。

    `platform_ratio`: 平台承担占比（0~1）。ge 券维度直接给
    `jdr_jx_coupon_platform_ratio`；easybi 可用 平台承担/(采销+平台) 反算。

    ⚠️只在**确实拿到该批次的比例**时用。今天验证过三源一致（均 75% 采销承担），
      但那是**逐批次**的比例，不能拿一个平均比例去折算一整堆券。
    """
    if full.caliber is not Caliber.FULL:
        raise BlacklightError("to_self 只接受全额口径，收到 %s" % full.caliber.value)
    if not (0.0 <= platform_ratio <= 1.0):
        raise BlacklightError("platform_ratio 应在 0~1，收到 %r" % platform_ratio)
    return Tagged(round(full.value * (1.0 - platform_ratio), 4),
                  Caliber.SELF, full.freshness, full.source + "→self")
