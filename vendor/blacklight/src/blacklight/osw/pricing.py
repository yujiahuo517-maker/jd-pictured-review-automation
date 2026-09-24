"""
通用报名价定价器 —— 复刻超级补贴插件 rescueSingleSku 的「试算 + 二分」。

报名价 → 实际到手价 之间隔着平台补贴/券（黑盒），无法直接算，只能**试价 + 调各频道的试算接口**。
在 [下限, 上限] 区间里找**到手价最高（到手价毛利率最大）、且满足约束**的报名价，最后取 floor-0.01（X.99 价）。

到手价毛利率(插件口径) = (到手价 − 采购价) / 到手价
约束：① 实际到手价 ≤ cap（bybt=建议价 / ms·便宜包邮=门槛价）  ② 到手价毛利率 ≥ target_margin

各频道只需提供三样：`quote(报名价)->到手价` 回调、cap、采购价，即可复用。
"""
from __future__ import annotations

import math


def margin_of(actual, cost) -> float:
    """到手价毛利率（小数）。"""
    actual = float(actual)
    return (actual - float(cost)) / actual if actual else float("-inf")


# ============================================================================
# 本地两层到手价模型（省试算）—— 从 markettool Home 券促明细直接算到手价
# ----------------------------------------------------------------------------
# 实证(SKU 10163347829552)：真实叠加是**两层**，非纯平铺：
#   第一层(平行,基于基价P)：满减(比例Y/X)、官方直降(定额) → 中间价 = P·(1−Σ比例) − Σ固定
#   第二层(层叠比例)：国补 15% 作用在中间价上 → 到手价 = 中间价·(1−国补率)
#   验证：(86.90−8.69−10.50)·0.85 = 57.55 = 到手价，精确吻合。
# 各 rate 从当前 Home 快照反推(reward/相应基数)，其余券促按当前 reward 当固定额。
# ============================================================================
# --- Home(queryPreDiscountHome) 的 promoType/subType → 促销大类(规范/pm-erp 口径) ---
# ⚠️ Home 编号 ≠ 规范编号：官方直降 Home=(2,11)/规范26；跨店满减 Home=(2,6)/规范10；单品促销/国补一致。
def home_category(ptype, subtype=None) -> str:
    if ptype == 1:
        return "单品促销"                       # 秒杀/便宜包邮/特价/超补(规范1)
    if ptype == 5:
        return "国补"
    if ptype == 26:
        return "单品直降"
    if ptype == 10:
        return "总价促销"
    if ptype == 25:
        return "官方立减"
    if ptype == 2:
        if subtype == 11:
            return "单品直降"                   # 官方直降(规范26)
        if subtype == 6:
            return "总价促销"                   # 跨店满减(规范10)
        return "平台活动"
    return f"type{ptype}"


# 大类 → 叠加建模(比例/层叠/固定)。按大类判定，不依赖脆弱的原始编号。
RATE_PARALLEL_CATS = {"总价促销", "单品直降"}   # 平行比例层：跨店满减(Y/X)、官方直降(力度%)
FINAL_LAYER_CATS = {"国补"}                     # 层叠最后一层比例：国补 15%
# 其余(单品促销/平台活动/券…)：按当前 reward 当固定额扣减
# 频道 → 其报名促销的大类（解价时排除同大类已有促销=替换语义，同类型只生效1个）
CHANNEL_CATEGORY = {"bybt": "单品促销", "ms": "单品促销", "seckill": "单品促销",
                    "baoyou": "单品促销", "tejia": "单品促销",
                    "campaign_zhijiang": "单品直降", "官方直降": "单品直降"}


def target_actual_price(fixed_cost, cps_rate: float, target_margin: float = 0.0):
    """由成本口径反算**目标到手价**。CPS 佣金按到手价比例(cps_rate)走→折进分母：
      到手价·(1−cps_rate) − fixed_cost = target_margin·到手价
      ⟹ 目标到手价 = fixed_cost / (1 − cps_rate − target_margin)
    fixed_cost = 采购+物流+广告(与到手价无关部分)。denom≤0 返回 None(口径无解)。"""
    denom = 1.0 - float(cps_rate) - float(target_margin)
    if denom <= 0:
        return None
    return round(float(fixed_cost) / denom, 2)


def build_model(pricing: dict, base=None, exclude_cats=(), basis: str = "customer") -> dict:
    """从 `osw_margin.query_pricing` 结果推导两层模型参数（按**大类**分类，见 home_category）。
      base: 默认取 benchPrice(无单品促销时的基价)。
      exclude_cats: 解价时排除的大类=本频道报名会**替换**的同大类已有促销(同类型只生效1个)。
        如给 bybt 定价传 {'单品促销'}、给官方直降定价传 {'单品直降'}。
      basis: 'customer'=客户口径(用 reward,预测客户到手价,管平台cap) /
             'jx'=**京喜承担口径**(用 jxReward,预测京喜到手价,算毛利)。广告/平台承担部分 jxReward=0 自动不计。
    返回 {parallel_fixed, parallel_rate, final_rate, base_now, basis}。"""
    base = float(pricing["benchPrice"] if base is None else base)
    exclude_cats = set(exclude_cats or ())
    rkey = "jxReward" if basis == "jx" else "reward"
    reskey = "jxResidual" if basis == "jx" else "residual"
    p_fixed, p_rate, finals, excluded = 0.0, 0.0, [], 0.0
    for pr in pricing.get("promotions", []):
        if pr.get("isNewUser"):                 # 幽灵促销(首单新人价)：不进标准到手价，不建模
            continue
        rw = pr.get(rkey) or 0
        cat = pr.get("cat") or home_category(pr.get("type"), pr.get("subType"))
        if not rw:
            continue
        if cat in exclude_cats:                 # 本频道替换掉的同类型促销：从叠加中移除
            excluded += rw
            continue
        if cat in FINAL_LAYER_CATS:
            finals.append(rw)
        elif cat in RATE_PARALLEL_CATS:
            p_rate += (rw / base) if base else 0.0
        else:
            p_fixed += rw
    # 券：Home 无逐券力度语义→默认按固定额(定额券/满X减Y达门槛后近似固定)扣减；比例券偏差交试算校验。
    if "券" not in exclude_cats:
        for c in pricing.get("coupons", []):
            p_fixed += (c.get(rkey) or 0)
    # residual：权威减免 − 枚举项之和。负值=有 phantom(枚举了但不进标准到手价的项，如未生效的同类单品促销/超补标记)。
    # ★排除某大类时：该类里被排掉的 phantom 对应的 residual 也要一并去掉，否则残留负 residual → 到手>基价。
    residual = pricing.get(reskey, 0) or 0
    phantom_total = max(0.0, -residual)                 # 枚举超出权威的总额(phantom)
    excluded_phantom = min(excluded, phantom_total)     # 被排除大类里的 phantom 部分
    p_fixed += residual + excluded_phantom              # 抵消被排除项对应的 phantom residual
    mid_now = base * (1 - p_rate) - p_fixed
    f_rate = sum((rw / mid_now) if mid_now else 0.0 for rw in finals)
    return {"parallel_fixed": round(p_fixed, 4), "parallel_rate": round(p_rate, 6),
            "final_rate": round(f_rate, 6), "base_now": round(base, 2),
            "excluded_reward": round(excluded, 2), "basis": basis}


def predict_actual(enroll_price, model: dict) -> float:
    """给报名价(=单品促销价/基价)预测到手价：中间价=P·(1−Σ比例)−Σ固定；到手价=中间价·(1−国补率)。"""
    mid = float(enroll_price) * (1 - model["parallel_rate"]) - model["parallel_fixed"]
    return round(mid * (1 - model["final_rate"]), 2)


def invert_enroll(target_actual, model: dict) -> float:
    """反解：给目标到手价，求报名价 P = (目标到手价/(1−国补率) + Σ固定) / (1−Σ比例)。"""
    mid = float(target_actual) / (1 - model["final_rate"]) if model["final_rate"] != 1 else float("inf")
    denom = 1 - model["parallel_rate"]
    return round((mid + model["parallel_fixed"]) / denom, 2) if denom else float("inf")


def solve_enroll_price(model: dict, fixed_cost, cps_rate: float, target_margin: float,
                       hi, cap=None, objective: str = "min_price", cust_model: dict = None,
                       max_enroll=None) -> dict:
    """**本地解报名价**（到手价随报名价单调增；已实证本地模型=bybt平台试算到分）。
      model: **京喜口径** build_model(basis='jx') 结果——预测京喜到手价，毛利算在其上。
      fixed_cost/cps_rate: query_pricing 的全口径成本分量。
      hi:  报名价上限(=京东价，不能报更高)。
      cap: **客户到手价**上限(bybt建议价 / ms·便宜包邮门槛价，None=无)——平台硬规则，作用在客户到手价上。
      cust_model: 客户口径 build_model(basis='customer')——把报名价映射到客户到手价做 cap 约束；None 则 cap 退化作用在 model 上。
      objective: 'min_price'=京喜到手价压到保毛利线T(报最低价保毛利) / 'max_margin'=顶到上限(受京东价&cap限)求最大毛利。
    返回 {ok, enroll_price, actual_price(京喜), cust_actual(客户), margin, target_actual} 或 {ok:False, reason}。"""
    hi = float(hi)
    if max_enroll is not None:                                       # 报名价门槛(便宜包邮/特价 opennessMinPrice):报名价≤门槛
        hi = min(hi, float(max_enroll))
    cm = cust_model or model
    T = target_actual_price(fixed_cost, cps_rate, target_margin)     # 保目标毛利的最低**京喜到手价**
    if T is None:
        return {"ok": False, "reason": "cps_rate+target_margin≥1，口径无解"}
    # 报名价上限 = min(京东价hi, 令客户到手价=cap 的报名价)——cap 约束在客户到手价上
    p_cap = hi
    if cap is not None:
        p_at_cap = invert_enroll(float(cap), cm)
        p_cap = min(hi, p_at_cap)
    a_hi = predict_actual(p_cap, model)                             # 报名价顶到上限时的京喜到手价(最高可达)
    if a_hi < T:
        return {"ok": False, "reason": f"报名价顶到上限{round(p_cap,2)}(受京东价/建议价限)，京喜到手价{round(a_hi,2)}仍<保毛利线{T}",
                "a_hi": round(a_hi, 2), "target_actual": T}
    if objective == "max_margin":
        P = p_cap                                                   # 顶到上限求最大京喜毛利
    else:
        P = min(invert_enroll(T, model), p_cap)                     # 报最低价，京喜到手价恰好=T
    a = predict_actual(P, model)
    # 保毛利兜底：取整/夹逼致京喜到手价略低于T时，微调报名价+0.01直至达标(最多几步)
    for _ in range(20):
        if a >= T or P >= p_cap:
            break
        P = round(min(P + 0.01, p_cap), 2)
        a = predict_actual(P, model)
    cust_a = predict_actual(P, cm)
    m = (1 - float(cps_rate) - float(fixed_cost) / a) if a else None
    return {"ok": True, "enroll_price": round(P, 2), "actual_price": round(a, 2),
            "cust_actual": round(cust_a, 2), "margin": round(m, 4) if m is not None else None,
            "target_actual": T, "objective": objective}


def compute_bid_price(quote, cost, cap, hi, target_margin: float = 0.0,
                      lo=None, min_gap: float = 0.5, max_iter: int = 20,
                      round_floor: bool = True) -> dict:
    """
    二分找最优报名价。
      quote(bid_price) -> 到手价(float) 或 None（试算失败）—— 各频道自己的试算接口封装成回调。
      cost:  采购价（可控成本）
      cap:   到手价上限（bybt=建议价 suggestPrice；ms/便宜包邮=门槛价）
      hi:    报名价上限（一般=京东价 jdPrice）
      lo:    报名价下限（默认=cap；即报名价不低于建议价/门槛）
      target_margin: 目标到手价毛利率（小数，0.05=5%）
    返回 {ok, enroll_price, actual_price, margin} 或 {ok:False, reason}（区间内无达标解）。
    """
    cost = float(cost)
    cap = float(cap)
    hi = float(hi)
    lo = cap if lo is None else float(lo)
    if hi <= 0 or cap <= 0 or hi < lo:
        return {"ok": False, "reason": f"价格区间无效(lo={lo} cap={cap} hi={hi})"}

    # Step1 探顶：报名价=hi（京东价）。连最高报名价的到手价都不达标 → 无解。
    top = quote(hi)
    if top is None or margin_of(top, cost) < target_margin:
        return {"ok": False, "reason": "报名价开到京东价，到手价毛利仍不达标（无调价空间）",
                "top_actual": top}
    top_pass_cap = top <= cap

    best_price, best_actual = (hi, top) if top_pass_cap else (None, None)

    # Step5 二分：找到手价最高、≤cap 的报名价。方向由 top_pass_cap 决定（券/满减 vs 比例折扣）。
    l, h, it = lo, hi, 0
    while h - l > min_gap and it < max_iter:
        it += 1
        mid = round((l + h) / 2, 2)
        a = quote(mid)
        if a is None:
            l = mid
            continue
        if a <= cap:
            if best_actual is None or a > best_actual:
                best_price, best_actual = mid, a
            l = mid                       # 到手价没触顶 → 抬报名价求更高到手价/毛利
        else:
            l, h = (mid, h) if top_pass_cap else (l, mid)

    if best_price is None:
        return {"ok": False, "reason": "二分未找到到手价≤上限的报名价"}

    # Step6 报名价向下取整 -0.01（X.99），逐个整数往上找到达标价。
    if not round_floor:
        return {"ok": True, "enroll_price": round(best_price, 2),
                "actual_price": round(best_actual, 2), "margin": round(margin_of(best_actual, cost), 4)}
    fi = math.floor(best_price)
    while fi - 0.01 < lo:
        fi += 1
    for _ in range(30):
        rounded = round(fi - 0.01, 2)
        if rounded > hi:
            break
        a = quote(rounded)
        if a is not None and a <= cap and margin_of(a, cost) >= target_margin:
            return {"ok": True, "enroll_price": rounded, "actual_price": round(a, 2),
                    "margin": round(margin_of(a, cost), 4)}
        fi += 1
    return {"ok": False, "reason": "取整(-0.01)后无达标价"}
