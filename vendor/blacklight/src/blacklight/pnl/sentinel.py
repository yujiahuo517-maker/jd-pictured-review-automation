"""恒等式哨兵 —— 验「数对不对」，不是验「接口通不通」。

`*_doctor` 验的是接口契约（字段还在不在）。它全绿，数也可能是错的：
今天 ge 的 `code=2000` 就属于**接口正常返回、但悄悄少了几个指标**——
少的若正好是毛利桥里的减项，毛利会被算高，而任何契约检查都发现不了。

⇒ 这里验三条**恒等式**。任一不成立 ⇒ 红灯，**拒绝出结论**（而不是标个警告继续算）。
"""
from __future__ import annotations

from dataclasses import dataclass

from blacklight.core import BlacklightError


@dataclass
class Sentinel:
    """一条恒等式的结果。允许挂额外属性（如 ownership_overlap 的交集）。"""
    name: str
    ok: bool
    detail: str = ""
    lhs: float = 0.0
    rhs: float = 0.0

    @property
    def gap(self) -> float:
        return round(self.lhs - self.rhs, 4)


def bridge_closes(bridge_result: dict, tol: float = 0.05) -> Sentinel:
    """① ge 毛利桥闭合：逐项加总 == 预估投后履约毛利。

    不闭合的两种原因都致命：公式漂了，或 `code=2000` 少返了指标。
    """
    calc = float(bridge_result.get("桥算出") or 0)
    after = float(bridge_result.get("预估投后履约毛利") or 0)
    missing = bridge_result.get("缺失指标") or []
    ok = (abs(calc - after) <= tol) and not missing
    detail = ""
    if missing:
        detail = "缺 %d 个指标：%s（code=2000 会静默少返，少的若是减项则毛利被算高）" % (
            len(missing), "、".join(str(m)[:40] for m in missing[:3]))
    elif not ok:
        detail = "桥算出 %.2f ≠ 投后 %.2f，公式可能已变" % (calc, after)
    return Sentinel("ge 毛利桥闭合", ok, detail, calc, after)


def full_equals_self_plus_platform(ge_full: float, eb_self: float, eb_plat: float,
                                   tol: float = 1.0) -> Sentinel:
    """② 跨平台恒等：ge 全额 == easybi(采销承担 + 平台承担)。

    2026-08-11 实测：
      · 单 SKU 10163019223322：625 == 538 + 87（18 个批次两边互无遗漏）
      · A+D 288 款 15 日：106,773 == 87,274 + 19,496（差 3 元进位）
    ⚠️**比之前必须先 `scope.align()`**——范围不对齐时这条恒等式必然不成立，
      但那是范围问题不是数据问题，会误导（今天就误导过一次）。
    ⚠️满减/折扣券随篮子变化，逐条比不适用；整体加总仍应成立。
    """
    rhs = eb_self + eb_plat
    ok = abs(ge_full - rhs) <= max(tol, abs(rhs) * 0.005)
    detail = "" if ok else (
        "ge 全额 %.2f ≠ easybi 采销 %.2f + 平台 %.2f = %.2f。"
        "先确认两侧 scope 已对齐（同一份 sku_ids、同一时间窗）"
        % (ge_full, eb_self, eb_plat, rhs))
    return Sentinel("ge全额 == easybi(采销+平台)", ok, detail, ge_full, rhs)


def ownership_overlap(ge_skus, osw_skus, *, max_ge_only_ratio: float = None) -> Sentinel:
    """③ 归属口径一致性 —— **不是包含关系，是两套口径**。

    ★2026-08-11 首次运行本哨兵就抓到：ge 当日成交 888 款里有 **137 款
      在 osw 里完全不存在**（`product_search` total=0、无 Home 定价记录），
      而且**不是上下架问题**（osw 各 sku_status 一个都不覆盖）。

      两边归属定义不同：
        · ge  `cate_op_erp`  = 商品归属 / 品类运营 —— **范围宽**
        · osw 登录态         = 采销归属           —— **范围窄**
      与铁律 8（采销助理 ERP vs 销售员 ERP，后者仅占 7.1%）同类。

      量级不容忽视：那 137 款占**当日成交 19.6%、毛利 61.2%**。

    ★★**不要取交集**（2026-08-11 用户纠正，我最初的设计就错在这）。
      两张网回答的是**不同问题**，不是同一总体的两个样本：
        · ge  = **已发生 / 正在发生** → 谁现在在亏，止血
        · osw = **预测**             → 配置已亏但还没出单，前瞻拦截
      取交集会把那 137 款**真实正在成交**的货直接扔掉，理由仅仅是 osw 不认识它们
      —— 那是 osw 的归属覆盖问题，**只在执行阶段（能不能改价/摘券）才需要关心**，
      不该污染发现阶段。⇒ **发现用并集 + 来源标记，可执行性推迟到 L4 再判。**

    ⇒ 本哨兵**只度量不断言**（`max_ge_only_ratio=None` 时恒过），
      用于让调用方知道两套口径差多少，而不是用来裁剪总体。
    """
    g = {str(s) for s in (ge_skus or [])}
    o = {str(s) for s in (osw_skus or [])}
    ge_only, both = g - o, g & o
    ratio = (len(ge_only) / len(g)) if g else 0.0
    ok = True if max_ge_only_ratio is None else (ratio <= max_ge_only_ratio)
    detail = ("ge 当日 %d 款：两边都有 %d、仅 ge %d（%.1f%%）。"
              "两套归属口径（ge=商品归属 / osw=采销归属），非包含关系。"
              "**发现阶段取并集、别取交集**——仅 ge 有的是真实成交，"
              "osw 认不认识只影响能不能动手（L4 再判）。"
              % (len(g), len(both), len(ge_only), 100 * ratio))
    if not ok:
        detail += " 超过阈值 %.0f%%" % (100 * max_ge_only_ratio)
    s = Sentinel("归属口径一致性", ok, detail, len(g), len(o))
    s.ge_only = ge_only        # 供调用方取交集用
    s.both = both
    return s


def run_sentinels(*sentinels: Sentinel, strict: bool = True) -> dict:
    """汇总。strict=True 时只要有一条不过就抛——**红灯不出结论**。"""
    ss = [s for s in sentinels if s is not None]
    bad = [s for s in ss if not s.ok]
    res = {"healthy": not bad, "checks": [
        {"name": s.name, "ok": s.ok, "detail": s.detail,
         "lhs": s.lhs, "rhs": s.rhs, "gap": s.gap} for s in ss]}
    if bad and strict:
        raise BlacklightError(
            "恒等式哨兵红灯，拒绝出结论：\n" +
            "\n".join("  ✗ %s —— %s" % (s.name, s.detail) for s in bad))
    return res
