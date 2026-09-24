"""静默忽略探针 —— 用**阴性对照**判断「筛选到底生效了没有」。

## 这是今天最有价值的一条发现（2026-08-11）
探 ge 毛利看板的顶部筛选时，我按「行数没变 ⇒ 字段名错」逐个试，
准备下结论说「平台不支持这些筛选」。顺手加了个**阴性对照**——
塞进一个叫「完全瞎编的字段」的筛选项，结果：

    瞎编字段      → 行数 860、金额 52751.35（与不加筛选完全相同）
    business_model→ 行数 860、金额 52751.35
    cate_id_3     → 行数 860、金额 52751.35
    city_level    → 行数 860、金额 52751.35

**一模一样**。所以 `filterList` 对未知字段是**静默吞掉**的，
「行数没变」既可能是字段名错、也可能是筛选层压根不生效——**两者无法区分**。
没有那个对照，我会把「我没猜中名字」错报成「平台不支持」。

同族陷阱：`attributeList` **不校验字段名**，写什么回显什么列，字段不存在时值全 `null`
（连中文瞎编串都能回显）。⇒ **只看列在不在会被骗，必须看值。**

## 规则
凡是「筛选/属性不校验」的接口，**每次探测都要带阴性对照**。
判据不是"报没报错"，而是**阴性对照与真实筛选是否可区分**。
"""
from __future__ import annotations

NONSENSE_FIELD = "wanquan_xiabian_zhanduan_zd"   # 阴性对照用的不存在字段
NONSENSE_VALUE = "__no_such_value__"


def silent_ignore_probe(run, make_filter, field: str, value,
                        *, tol: float = 1e-6) -> dict:
    """判断某个筛选字段是否真的生效。

    run(filters) -> 可比较的指纹（建议 `(行数, 金额)` 元组）
    make_filter(field, value) -> 该接口的筛选项结构

    返回 {verdict, trustworthy, baseline, nonsense, actual, note}
      · verdict='生效'        —— 阴性对照==基线 且 实际筛选!=基线
      · verdict='无效或不存在' —— 阴性对照==基线 且 实际筛选==基线（**无法区分**）
      · verdict='筛选层不可信' —— 阴性对照!=基线（服务端行为不确定，别信任何筛选结论）
    """
    base = run([])
    nons = run([make_filter(NONSENSE_FIELD, NONSENSE_VALUE)])
    act = run([make_filter(field, value)])

    def same(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(a - b) <= tol
        if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)) and len(a) == len(b):
            return all(same(x, y) for x, y in zip(a, b))
        return a == b

    if not same(nons, base):
        return {"field": field, "verdict": "筛选层不可信", "trustworthy": False,
                "baseline": base, "nonsense": nons, "actual": act,
                "note": "阴性对照（不存在的字段）竟然改变了结果——服务端行为不确定，"
                        "本次所有筛选结论都不可信"}
    if same(act, base):
        return {"field": field, "verdict": "无效或不存在", "trustworthy": True,
                "baseline": base, "nonsense": nons, "actual": act,
                "note": "与阴性对照表现一致 ⇒ **无法区分「字段名错」和「筛选被忽略」**。"
                        "不要据此断言平台不支持该筛选——很可能只是没猜中字段名，"
                        "去抓一份带该筛选的包拿真名。"}
    return {"field": field, "verdict": "生效", "trustworthy": True,
            "baseline": base, "nonsense": nons, "actual": act, "note": ""}


def probe_many(run, make_filter, cases: dict) -> list:
    """批量探测。cases: {字段名: 取值}。阴性对照只跑一次，省调用。"""
    base = run([])
    nons = run([make_filter(NONSENSE_FIELD, NONSENSE_VALUE)])
    trustworthy = (nons == base)
    out = []
    for f, v in cases.items():
        act = run([make_filter(f, v)])
        if not trustworthy:
            verdict = "筛选层不可信"
        elif act == base:
            verdict = "无效或不存在"
        else:
            verdict = "生效"
        out.append({"field": f, "verdict": verdict, "actual": act,
                    "baseline": base, "nonsense": nons})
    return out
