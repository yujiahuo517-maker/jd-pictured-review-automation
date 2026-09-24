"""
规则效用统计：读 `runtime/audit.log`，回答**「哪条规则该删」**。

起因（2026-08-05）：护栏越加越多，但没有任何数据说哪一条真的挣回了它的复杂度。
凭感觉删规则不敢下手，于是只加不减——这本身就是负债。

审计日志里其实已经有答案，只是从没被读过。935 条记录一读就暴露了：
`jzt_swa.budget_update` 连续 10 次失败、0 次成功，**每次都是被自己的 confirm 门拦下**——
那道门当时没在防危险，它在挡操作者本人（已修，见 `ConfirmGate.body_token`）。

三张表：
  1. **写路径使用度** —— 次数/成功/被拦/末次时间。长期不用的进候删清单。
  2. **闸门拦截统计** —— 每道闸拦了多少次。★判据：**拦截>0 且成功=0 → 这道闸在挡自己人**。
  3. **矩阵对账** —— `WRITE_VERIFICATION`(声明) vs 审计(事实)，报不一致。
     矩阵靠人手工维护，人会忘；对账后就不靠纪律。

纯只读：不联网、不写文件。跑：`python -m blacklight.core.rulestat`
"""
from __future__ import annotations

import json as _json
import re
from collections import defaultdict
from typing import Optional

from blacklight.core import paths

# 拦截文案 → 闸门类型。这些字符串来自各模块 raise 的原文，改文案要同步改这里
# （所以尽量匹配**稳定的关键词**而不是整句）。
GATE_PATTERNS = [
    ("二次确认门", r"需二次确认"),
    ("批量上限", r"超过护栏|单次改价 ≤|条超过"),
    ("取值范围", r"超出范围|必须 >0|不能为空"),
    ("参数白名单", r"只能是"),
    ("禁止触碰清单", r"禁止触碰|protected"),
    ("前置校验未过", r"预检|未通过|资格"),
]

_STALE_DAYS = 30      # 超过这么久没用过 → 进候删清单
_MIN_BLOCKS = 3       # 少于这么多次拦截，不下「挡自己人」的结论（n=1 时那结论不可信）

# ★★本工具的两条硬限制——**不写出来它就会诱导你删掉有用的闸**：
#
# 1) **审计只记录 `@audited` 的真执行，dry-run 完全不落盘**。而批量上限/取值范围/参数白名单
#    这类闸，实际最常在 dry-run 阶段就把人拦住了。所以"从未触发"**不等于**"没用"，
#    只等于"没在真执行路径上触发过"。
# 2) **小样本下「成功=0」说明不了问题**。`product.update_title` 只被拦过 1 次、
#    随后就没人再试——那是"试了一次没继续"，不是"闸坏了"。实测它的令牌是稳定的。
#    所以拦截次数 < _MIN_BLOCKS 时只报事实、不下判语。
#
# 这两条正是本工具最容易产出"看着合理但是错的结论"的地方，删任何东西前先回头读它们。
_LIMITS = [
    "审计只记真执行（@audited），**dry-run 的拦截不落盘** → 「从未触发」≠「没用」，"
    "尤其批量上限/取值范围/白名单这类多在 dry-run 就拦住了",
    f"拦截次数 < {_MIN_BLOCKS} 时不下「挡自己人」判语——n=1 的结论不可信",
    "「长期闲置」也可能只是最近没做那类业务，不是路径该删",
]


def _load(path: Optional[str] = None) -> list:
    p = path or paths.audit_path()
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(_json.loads(line))
                except ValueError:
                    continue          # 坏行跳过，不让一条脏数据废掉整份统计
    except FileNotFoundError:
        pass
    return out


def _gate_of(resp: str) -> Optional[str]:
    """从失败回执里认出是哪道闸拦的。认不出返回 None（那多半是平台/业务失败，不是闸）。"""
    for name, pat in GATE_PATTERNS:
        if re.search(pat, resp or ""):
            return name
    return None


def _days_since(ts: str, today: Optional[str] = None) -> Optional[int]:
    import datetime as _dt
    try:
        d = _dt.date.fromisoformat(ts[:10])
        t = _dt.date.fromisoformat(today) if today else _dt.date.today()
        return (t - d).days
    except (TypeError, ValueError):
        return None


def report(path: Optional[str] = None, today: Optional[str] = None,
           stale_days: int = _STALE_DAYS) -> dict:
    """三张表 + 候删清单。`today` 仅供测试注入。"""
    recs = _load(path)
    if not recs:
        return {"error": f"审计日志为空或不存在（{path or paths.audit_path()}）"}

    # ---- 表1：写路径使用度 ----
    agg = defaultdict(lambda: {"总次数": 0, "成功": 0, "失败": 0, "被闸拦": 0, "末次": ""})
    gates = defaultdict(lambda: defaultdict(int))          # gate -> path -> 次数
    for r in recs:
        key = f"{r.get('scene')}.{r.get('action')}"
        a = agg[key]
        a["总次数"] += 1
        ok = r.get("ok")
        if ok is True:
            a["成功"] += 1
        elif ok is False:
            a["失败"] += 1
            g = _gate_of(r.get("response") or "")
            if g:
                a["被闸拦"] += 1
                gates[g][key] += 1
        a["末次"] = max(a["末次"], r.get("ts") or "")

    rows = []
    for k, a in sorted(agg.items(), key=lambda kv: -kv[1]["总次数"]):
        rows.append({"写路径": k, **a, "闲置天数": _days_since(a["末次"], today)})

    # ---- 表2：闸门拦截统计 ----
    gate_rows = []
    for g, per in sorted(gates.items(), key=lambda kv: -sum(kv[1].values())):
        for k, n in sorted(per.items(), key=lambda kv: -kv[1]):
            succ = agg[k]["成功"]
            if succ == 0 and n >= _MIN_BLOCKS:
                verdict = f"★挡自己人：拦了 {n} 次、该路径从没成功过 —— 先查闸本身"
            elif succ == 0:
                verdict = f"样本不足（仅 {n} 次）：可能只是试了一次没继续，别据此判闸有问题"
            else:
                verdict = "正常：拦截与成功并存，像是在防真参数变更"
            gate_rows.append({"闸门": g, "写路径": k, "拦截次数": n,
                              "该路径成功次数": succ, "判读": verdict})

    # ---- 表3：矩阵对账 ----
    from blacklight.core.base import WRITE_VERIFICATION
    drift = []
    for k in sorted(set(WRITE_VERIFICATION) | set(agg)):
        declared = WRITE_VERIFICATION.get(k, {}).get("verified")
        succ = agg.get(k, {}).get("成功", 0)
        if k not in WRITE_VERIFICATION:
            drift.append({"写路径": k, "问题": "未登记到 WRITE_VERIFICATION", "审计成功": succ})
        elif declared is False and succ > 0:
            drift.append({"写路径": k, "问题": f"矩阵说未活体验证，但审计里已成功 {succ} 次 → 该升 True",
                          "审计成功": succ})
        elif declared is True and succ == 0:
            drift.append({"写路径": k, "问题": "矩阵说已验证，但审计里没有任何成功记录 → 存疑",
                          "审计成功": 0})

    # ---- 候删清单 ----
    candidates = []
    for r in rows:
        if r["成功"] == 0 and r["被闸拦"] >= _MIN_BLOCKS:
            candidates.append({"对象": r["写路径"], "类型": "闸门可能有问题",
                               "理由": f"被闸拦 {r['被闸拦']} 次、成功 0 次", "可直接动手": True})
        elif r["闲置天数"] is not None and r["闲置天数"] >= stale_days:
            candidates.append({"对象": r["写路径"], "类型": "长期闲置",
                               "理由": f"末次 {r['末次'][:10]}，已闲置 {r['闲置天数']} 天",
                               "可直接动手": False, "先确认": "是不是只是最近没做这类业务"})
    for name in [n for n, _ in GATE_PATTERNS if n not in gates]:
        candidates.append({"对象": f"闸门:{name}", "类型": "真执行路径上未触发",
                           "理由": "审计期内没在真执行时拦过",
                           "可直接动手": False,
                           "先确认": "这道闸是不是主要在 dry-run 起作用——dry-run 不落审计，"
                                     "所以这条**不能**当作『可以删』的证据"})

    return {
        "审计范围": f"{min(r.get('ts','') for r in recs)[:16]} ~ {max(r.get('ts','') for r in recs)[:16]}",
        "记录数": len(recs), "写路径数": len(rows),
        "写路径使用度": rows,
        "闸门拦截": gate_rows or [{"note": "审计期内没有任何闸门拦截记录"}],
        "矩阵对账": drift or [{"note": "✅ WRITE_VERIFICATION 与审计事实一致"}],
        "候删清单": candidates or [{"note": "无候删项"}],
        "⚠️本工具的限制": _LIMITS,
        "_怎么用": "只有标了 `可直接动手: True` 的才有数据支撑；其余都要先回答 `先确认` 那一栏。"
                   "本工具用来**排除凭感觉删**，不是用来代替判断。",
    }


def _fmt(rep: dict) -> str:
    if "error" in rep:
        return rep["error"]
    L = [f"审计 {rep['记录数']} 条  {rep['审计范围']}", "=" * 72, "", "【1】写路径使用度"]
    L.append(f"  {'写路径':30s}{'次':>5}{'成功':>6}{'失败':>6}{'被闸拦':>7}{'闲置天':>7}")
    for r in rep["写路径使用度"]:
        L.append(f"  {r['写路径']:30s}{r['总次数']:5d}{r['成功']:6d}{r['失败']:6d}"
                 f"{r['被闸拦']:7d}{(r['闲置天数'] if r['闲置天数'] is not None else -1):7d}")
    L += ["", "【2】闸门拦截"]
    for r in rep["闸门拦截"]:
        if r.get("note"):
            L.append("  " + r["note"])
        else:
            L.append(f"  {r['闸门']:12s} {r['写路径']:28s} 拦 {r['拦截次数']:3d} 次 → {r['判读']}")
    L += ["", "【3】矩阵对账（声明 vs 事实）"]
    for r in rep["矩阵对账"]:
        L.append("  " + (r.get("note") or f"{r['写路径']:30s} {r['问题']}"))
    L += ["", "【4】候删清单"]
    for r in rep["候删清单"]:
        if r.get("note"):
            L.append("  " + r["note"])
            continue
        mark = "✅可动手" if r.get("可直接动手") else "⚠️先确认"
        L.append(f"  {mark} [{r['类型']}] {r['对象']:26s} {r['理由']}")
        if r.get("先确认"):
            L.append(f"           └ {r['先确认']}")
    L += ["", "⚠️ 本工具的限制（删任何东西前先读）"]
    for i, s in enumerate(rep["⚠️本工具的限制"], 1):
        L.append(f"  {i}. {s}")
    L += ["", rep["_怎么用"]]
    return "\n".join(L)


def main(argv=None) -> int:
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="规则效用统计（读审计日志，产出候删清单）")
    ap.add_argument("--log", default=None, help="审计日志路径，默认 runtime/audit.log")
    ap.add_argument("--stale-days", type=int, default=_STALE_DAYS)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = report(a.log, stale_days=a.stale_days)
    print(_json.dumps(rep, ensure_ascii=False, indent=2) if a.json else _fmt(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
