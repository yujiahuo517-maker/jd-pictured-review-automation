"""范围对齐器 —— 跨源比对前必须先把「看的是哪批 SKU」对齐。

## 为什么需要（2026-08-11 亲身踩坑）
拿 ge 的券成本和 easybi 的券成本比对时，我没注意到：
- **ge 默认整个部门**（`saler_dept_id_2`），
- **easybi 只统计我传进去的 SKU**。

于是「联合承担=否」的 56 张券两边对不上，我据此得出**「规律不成立」——这是错的**。
用 `sku_ids` 把范围对齐后，逐条**精确相等**（单 SKU 实测：ge 625 == easybi 538 + 87）。

更隐蔽的是满减券：篮子构成不同，单均天然不同，**即使口径一样也不可比**。
所以对齐要求的不是「都限定 SKU」，而是**限定成同一个集合**。

⇒ 任何跨源比对，先 `align()`；对不齐就抛，不要"差不多就行"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from blacklight.core import BlacklightError


@dataclass
class Scope:
    """一次取数的范围声明。"""
    skus: frozenset = field(default_factory=frozenset)
    start: str = ""
    end: str = ""
    erp: str = ""
    label: str = ""          # 'ge.drill' / 'easybi.attribute_loss' ...

    @classmethod
    def of(cls, skus, start, end, erp="", label=""):
        return cls(frozenset(str(s) for s in (skus or [])), start, end, erp, label)

    @property
    def is_bounded(self) -> bool:
        """是否限定了 SKU 集合。未限定 = 全部门/全店，跨源比对时必然对不齐。"""
        return bool(self.skus)

    def describe(self) -> str:
        n = len(self.skus) if self.skus else "全部(未限定)"
        return "%s[%s SKU, %s~%s%s]" % (self.label or "?", n, self.start, self.end,
                                        ", erp=" + self.erp if self.erp else "")


def align(*scopes: Scope, require_bounded: bool = True) -> Scope:
    """校验多个 Scope 是否可比；不可比直接抛，返回公共 Scope。

    require_bounded：要求每个 scope 都限定了 SKU 集合。
      **默认 True** —— 未限定的那个通常是"整部门"，和"我的 288 款"比是没有意义的。
    """
    scopes = [s for s in scopes if s is not None]
    if len(scopes) < 2:
        raise BlacklightError("align 至少需要两个 Scope")

    if require_bounded:
        loose = [s for s in scopes if not s.is_bounded]
        if loose:
            raise BlacklightError(
                "范围未限定，无法跨源比对：%s。"
                "ge 默认整个部门、easybi 只统计传入的 SKU——"
                "不对齐会造出假差异（2026-08-11 曾据此误判『规律不成立』）。"
                "给每一侧都传同一份 sku_ids。"
                % "、".join(s.describe() for s in loose))

    win = {(s.start, s.end) for s in scopes}
    if len(win) > 1:
        raise BlacklightError(
            "时间窗不一致：%s。窗口不同则金额天然不同，不是口径问题。"
            % "、".join("%s~%s" % w for w in sorted(win)))

    if require_bounded:
        sets = [s.skus for s in scopes]
        base = sets[0]
        for s, sc in zip(sets[1:], scopes[1:]):
            if s != base:
                only_a, only_b = base - s, s - base
                raise BlacklightError(
                    "SKU 集合不一致：%s 独有 %d 个、%s 独有 %d 个。"
                    "请用同一份清单分别查两侧。"
                    % (scopes[0].label or "A", len(only_a), sc.label or "B", len(only_b)))

    return Scope(scopes[0].skus, scopes[0].start, scopes[0].end,
                 scopes[0].erp, "aligned(" + ",".join(s.label for s in scopes) + ")")


def warn_basket_dependent(coupon_face_value, per_order_amount, tol: float = 0.05) -> str:
    """满减/折扣券的单均随篮子变化，即使范围对齐也不可逐条比。

    判据：`单均 ≈ 面额` ⇒ 固定面额券，可比；差得多 ⇒ 篮子相关，别逐条比。
    2026-08-11：198/220 是固定面额券，其中 194 条满足 ge全额 == easybi(采销+平台)。
    """
    try:
        fv = float(coupon_face_value or 0)
        po = float(per_order_amount or 0)
    except (TypeError, ValueError):
        return "无法判断（面额或单均缺失）"
    if fv <= 0:
        return "折扣型券（面额=0）——按面额匹配必然失效，别逐条比"
    if abs(po - fv) <= max(tol, fv * 0.02):
        return ""
    return ("篮子相关（单均 %.2f vs 面额 %.2f）——满减券随篮子变化，"
            "即使范围对齐也不可逐条比" % (po, fv))
