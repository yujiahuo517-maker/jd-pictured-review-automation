"""券促叠加/生效规则查询 —— 官方规则表的**程序化接口**。

**为什么要有这个**：2026-07-28 一整天里，我三次凭印象推叠加规则、三次推错
（"券不叠加取最大"漏了跨类可叠、"券促必然叠加"漏了券×总价促销=×）。
规则表是权威真值，**别再靠记忆推**——用 `can_stack()` / `predict_delta()` 查。

数据源：`stacking_matrix.json`（由《券促叠加和生效规则查询表.xlsx》导出，标注「禁止外传」）。
可读版详见 `docs/yx/NOTES_stacking.md`。
"""
from __future__ import annotations

import json
import os
from typing import Optional

_DATA: Optional[dict] = None
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stacking_matrix.json")

# 监控字段 → 规则表名称。key 是判定线索，value 是矩阵里的规范名。
# promoType 见 margin.PROMO_TYPE_NAME；促销名里的关键词用于区分同为 type1 的各种单品促销。
_PROMO_NAME_HINTS = [
    ("便宜包邮", "特殊单促·便宜包邮"),
    ("超级补贴", "特殊单促·超级补贴"),
    ("秒杀", "特殊单促·普通秒杀"),
    ("顺手买", "特殊单促·顺手买"),
    ("月黑风高", "特殊单促·月黑风高"),
    ("试用", "特殊单促·试用"),
    ("预告价", "普通单品促销·限时优惠价"),   # 【预告价】= 限时性质的单品促销
    ("官方直降", "单品直降·官方直降"),
    ("直降", "单品直降·普通单品直降"),
    ("跨店满减", "总价促销·跨店满减"),
    ("跨品类", "总价促销·跨品类总价促销"),
    ("国补", "国补"),
    ("政府消费券", "政府消费券"),
    ("礼金", "礼金促销"),
]
_PROMO_TYPE_FALLBACK = {
    1: "特殊单促·便宜包邮",          # 单品促销（具体哪种看名字，这里只作兜底）
    2: "单品直降·普通单品直降",
    5: "国补",
    26: "单品直降·官方直降",
}
_COUPON_NAME_HINTS = [
    ("广告智能", "广告智能补贴券"),
    ("神券", "平台神券·POP神券"),
    ("无门槛", "京麦创建的优惠券·无门槛券"),
    ("店铺券", "京麦创建的优惠券·店铺券"),
    ("全品类", "全品类券"),
]
_COUPON_DEFAULT = "普通限品类券"      # 品类券/自建券/拉新券等，绝大多数属这类


def _load() -> dict:
    global _DATA
    if _DATA is None:
        with open(_PATH, encoding="utf-8") as f:
            _DATA = json.load(f)
    return _DATA


def names() -> list:
    """规则表里的全部 51 个券促类型名。"""
    return list(_load()["names"])


def _resolve(name: str) -> Optional[str]:
    """把任意写法解析成矩阵规范名。
    优先级：**精确全名 → 精确子类名(·后一段) → 子串包含**。
    子类名优先很关键——`超级补贴` 必须解析成 `特殊单促·超级补贴` 而不是 `PLUS类·PLUS超级补贴`（子串也含它）。
    """
    d = _load()
    n = str(name or "").strip()
    if not n:
        return None
    if n in d["matrix"]:
        return n
    exact = [k for k in d["names"] if k.split("·")[-1] == n]
    if exact:
        return exact[0]
    loose = [k for k in d["names"] if n in k]
    return loose[0] if loose else None


def classify(item: dict, kind: str = "promo") -> Optional[str]:
    """把 `margin.query_pricing` 的一条 promotions/coupons 记录映射到规则表名称。

    kind='promo' 用 name 关键词 + promoType 兜底；kind='coupon' 用券名关键词，默认归「普通限品类券」。
    """
    nm = str(item.get("name") or item.get("couponName") or "")
    if kind == "coupon":
        for k, v in _COUPON_NAME_HINTS:
            if k in nm:
                return v
        return _COUPON_DEFAULT
    for k, v in _PROMO_NAME_HINTS:
        if k in nm:
            return v
    cat = str(item.get("cat") or "")
    for k, v in _PROMO_NAME_HINTS:
        if k in cat:
            return v
    return _PROMO_TYPE_FALLBACK.get(item.get("type") or item.get("promoType"))


def can_stack(a: str, b: str) -> dict:
    """查两类券促能否叠加。返回 {a, b, value: √/×/可选/未知, 可叠加: bool|None}。"""
    d = _load()
    ra, rb = _resolve(a), _resolve(b)
    if not ra or not rb:
        return {"a": a, "b": b, "value": "未知", "可叠加": None,
                "reason": f"没解析到规范名（a→{ra} b→{rb}）；用 names() 看全量类型名"}
    v = (d["matrix"].get(ra) or {}).get(rb) or (d["matrix"].get(rb) or {}).get(ra) or "未知"
    return {"a": ra, "b": rb, "value": v,
            "可叠加": True if v == "√" else (False if v == "×" else None),
            "note": "「可选」= 创建促销时可选择是否叠加" if v == "可选" else None}


def conflicts(name: str) -> dict:
    """列出与某类型**互斥**（×）和**可叠加**（√）的全部类型。做方案前先看这个。"""
    d = _load()
    r = _resolve(name)
    if not r:
        return {"name": name, "error": "没解析到规范名，用 names() 看全量"}
    row = d["matrix"].get(r) or {}
    return {"name": r,
            "互斥(×)": sorted([k for k, v in row.items() if v == "×"]),
            "可叠加(√)": sorted([k for k, v in row.items() if v == "√"]),
            "可选": sorted([k for k, v in row.items() if v == "可选"])}


def effect_rules() -> list:
    """生效/展示数量与优先级（sheet2）。单品促销只生效1个、优先级 百亿补贴>减钱大的>免邮促销>… 等。"""
    return list(_load()["effect_rules"])


def predict_delta(current: list, new_item: dict, new_jx: float, kind: str = "coupon") -> dict:
    """★**预测「给某 SKU 新增一项券/促」对京喜毛利的影响**。

    current: 该 SKU 当前生效的券或促（`query_pricing` 的 coupons/promotions，需含 name/reward/jxReward）。
    new_item: 新增项，至少给 {'name': ...}（用于判类）；new_jx: 新增项的**我担**金额。
    返回 {Δ毛利, 判定, 被替换项}。

    规则：与新增项**互斥**的同类现存项里，按面额(reward)选出当前生效者；
      · 新项面额更大 → 顶替它 ⇒ Δ = 该项我担 − new_jx
      · 新项面额更小 → 不生效     ⇒ Δ = 0
      · 无互斥项               ⇒ Δ = −new_jx（纯新增）
    **别用「现毛利 − new_jx」**——那假设一切可叠加，会严重高估损失。
    """
    new_key = classify(new_item, kind=kind)
    if not new_key:
        return {"Δ毛利": None, "判定": "无法归类新增项，需人工判断", "新增项类型": None}
    rivals = []
    for c in (current or []):
        k = classify(c, kind=kind)
        if not k:
            continue
        if can_stack(new_key, k).get("可叠加") is False and float(c.get("jxReward") or 0) > 0.005:
            rivals.append({"name": c.get("name"), "类型": k,
                           "面额": float(c.get("reward") or 0), "我担": float(c.get("jxReward") or 0)})
    if not rivals:
        return {"Δ毛利": round(-float(new_jx), 2), "判定": "无互斥项，新增项直接生效（纯新增成本）",
                "新增项类型": new_key, "被替换项": None}
    win = max(rivals, key=lambda x: x["面额"])
    new_face = float(new_item.get("reward") or new_item.get("面额") or 0)
    if new_face and new_face < win["面额"]:
        return {"Δ毛利": 0.0, "判定": f"现有「{win['name']}」面额 {win['面额']} 更大，新增项不生效 ⇒ 完全无害",
                "新增项类型": new_key, "被替换项": None, "互斥项": rivals}
    return {"Δ毛利": round(win["我担"] - float(new_jx), 2),
            "判定": f"新增项顶替「{win['name']}」（我担 {win['我担']} → {new_jx}）",
            "新增项类型": new_key, "被替换项": win, "互斥项": rivals}
