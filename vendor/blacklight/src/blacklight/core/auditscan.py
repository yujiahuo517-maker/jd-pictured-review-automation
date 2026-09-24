"""**把审计日志当回归源**：离线扫 `runtime/audit.log`，不打网络。

## 为什么
这个包有 241 个 MCP 工具，但自动验证只有两处：
  · `tests/smoke_test.py` —— **不打网络**，验的是代码结构与纯函数
  · 各域 `doctor` —— 5 个域合计 **33 项**探活
也就是说绝大多数工具的**真实调用路径没有任何自动回归**。

而 `WRITE_VERIFICATION`（40 条写路径的「活体验证矩阵」）**只能靠人手更新**，
已经证实会过期：2026-08-19 `campaign.table_apply` 还标着「未活体」，
但当天刚用它报了 94 款、全量回读 94/94。

同时每天已经有 2000+ 条真实调用落在 `audit.log` 里 —— **数据已经在了**，
只是没人拿它当回归源。本模块就是把它用起来，成本≈0。

## 四类检查（都不打网络）
1. **活体标记过期** —— 标 `verified=False` 但审计里已有成功记录 ⇒ 该转 True
2. **活体标记存疑** —— 标 `verified=True` 但审计里从没成功过 ⇒ 人工复核
3. **失败率突变** —— 某写路径近窗口失败率显著高于基线 ⇒ 契约可能漂了
4. **未登记的节流文案** —— 出现了 `policy.THROTTLE_RULES` 不认识的疑似节流文案
   ⇒ 有新的限流路径没接策略层

⚠️**本模块只报告不改代码**。活体标记是人工拍板的结论（写进代码要可追溯），
  这里给出「建议改成什么」，由人确认后落库。
"""
from __future__ import annotations

import collections
import datetime as _dt
import json
import os
import re
from typing import Optional

from . import paths as _paths
from .base import WRITE_VERIFICATION
from .policy import THROTTLE_RULES, is_throttle

#: 疑似节流的通用特征（用来发现**还没登记**的限流路径）
_THROTTLE_SMELL = re.compile(
    r"频繁|过于频繁|请稍后|稍后再|超过上限|次数已超|正在处理|正在报名|请勿重复|重复点击|限流|太快|请等待|需要再等")

#: 明显属于业务拒绝的文案（别误报成节流）
_BUSINESS_DENY = re.compile(
    r"不在活动可报范围|不支持|非全网低价|门槛|资质|已报过名|库存|不满足|校验不通过|已达到上限，无法重复报名")


def load(path: str = None, since: str = None) -> list:
    """读审计日志。`since`='YYYY-MM-DD' 只取该日起。坏行跳过（日志曾混入非法代理字符）。"""
    p = path or _paths.audit_path()
    # ★2026-08-24：审计日志开始轮转（audit.log + audit.log.1，见 core/base._rotate_audit_if_big）。
    #   这里必须**把上一代也读进来**，否则轮转当天"活体标记核对/失败率"会突然失去历史，
    #   看着像"这些写路径从没成功过" —— 那正是本文件要防的误判。
    paths_to_read = [p + ".1", p] if os.path.exists(p + ".1") else [p]
    rows = []
    for _p in paths_to_read:
        rows.extend(_load_one(_p, since))
    return rows


def _load_one(p: str, since: str = None) -> list:
    rows = []
    try:
        f = open(p, encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue                      # 单行坏了不该让整份日志不可用
            if since and str(r.get("ts", "")) < since:
                continue
            if r.get("scene") and r.get("action"):
                rows.append(r)
    return rows


def _key(r) -> str:
    return f"{r.get('scene')}.{r.get('action')}"


def _text(r) -> str:
    return str(r.get("response") or "")


def stale_verification(rows: list) -> dict:
    """**活体标记核对**：拿审计里的真实成功记录去对 `WRITE_VERIFICATION`。

    返回 {应转True, 存疑, 无记录}：
      · `应转True`  标着未活体，但审计里已有 ok=True 的真实调用 ⇒ 标记过期了
      · `存疑`      标着已活体，但审计里从没成功过 ⇒ 可能是很久以前手工验的，值得复核
      · `无记录`    双方都没有记录，正常（还没用过）
    """
    ok_cnt, any_cnt, last_ok = collections.Counter(), collections.Counter(), {}
    for r in rows:
        k = _key(r)
        any_cnt[k] += 1
        if r.get("ok"):
            ok_cnt[k] += 1
            last_ok[k] = r.get("ts")
    upgrade, doubt, unused, manual = [], [], [], []
    for k, meta in WRITE_VERIFICATION.items():
        v = bool(meta.get("verified"))
        n_ok = ok_cnt.get(k, 0)
        if not v and n_ok > 0:
            upgrade.append({"path": k, "审计成功次数": n_ok, "最近一次": last_ok.get(k),
                            "现note": (meta.get("note") or "")[:80]})
        elif v and n_ok == 0:
            # ★带 `evidence` 的：人工验过、只是审计没覆盖到（@audited 后补 / 日志窗口之外）。
            #   这类**不该天天报"存疑"**——一个已知且已回答的问题反复报，只会训练人忽略这份报告。
            #   放进安静的桶，等它真跑一次、审计里出现成功记录后自动消失。
            (manual if meta.get("evidence") else doubt).append(
                {"path": k, "审计调用次数": any_cnt.get(k, 0),
                 "evidence": meta.get("evidence"),
                 "现note": (meta.get("note") or "")[:80]})
        elif n_ok == 0 and any_cnt.get(k, 0) == 0:
            unused.append(k)
    return {"应转True": sorted(upgrade, key=lambda x: -x["审计成功次数"]),
            "存疑": doubt, "人工验证·审计未覆盖": manual, "无记录": sorted(unused),
            "_note": "只报告不改代码：活体标记是人工拍板的结论，确认后再落库（要可追溯）。",
            "_存疑两种可能": "① 很久以前手工验的、日志已轮转或当时没落审计 ② **刚补上 @audited 还没真跑过**"
                             "（2026-08-19 给 material/pic 七条补审计后就是这种）——跑一次真写即自愈。"}


def failure_rates(rows: list, recent_days: int = 7, min_calls: int = 10) -> dict:
    """**失败率突变**：近 `recent_days` 天 vs 更早的基线，按写路径比。

    突变通常意味着契约漂了或平台改了规则——比等到下次手工发现要早。
    ⚠️失败率高**不等于**有问题：二次确认门、业务拒绝都会计入 ok=False。
      所以同时给出 `节流占比`，那才是「真不稳定」的部分。
    """
    if not rows:
        return {"rows": [], "_note": "无审计数据"}
    latest = max(str(r.get("ts") or "") for r in rows)[:10]
    try:
        cut = (_dt.date.fromisoformat(latest) - _dt.timedelta(days=recent_days)).isoformat()
    except ValueError:
        return {"rows": [], "_note": "时间戳异常"}
    agg = collections.defaultdict(lambda: {"近_n": 0, "近_fail": 0, "近_throttle": 0,
                                           "基线_n": 0, "基线_fail": 0})
    for r in rows:
        a = agg[_key(r)]
        recent = str(r.get("ts", ""))[:10] >= cut
        a["近_n" if recent else "基线_n"] += 1
        if not r.get("ok"):
            a["近_fail" if recent else "基线_fail"] += 1
            if recent and is_throttle(_text(r)):
                a["近_throttle"] += 1
    out = []
    for k, a in agg.items():
        if a["近_n"] < min_calls:
            continue
        cur = a["近_fail"] / a["近_n"]
        base = (a["基线_fail"] / a["基线_n"]) if a["基线_n"] >= min_calls else None
        out.append({"path": k, "近_调用": a["近_n"], "近_失败率%": round(cur * 100, 1),
                    "基线_失败率%": (round(base * 100, 1) if base is not None else None),
                    "变化pp": (round((cur - base) * 100, 1) if base is not None else None),
                    "近_节流占比%": round(a["近_throttle"] / max(1, a["近_fail"]) * 100),
                    "判读": ("节流为主⇒接策略层" if a["近_fail"] and
                              a["近_throttle"] / max(1, a["近_fail"]) > 0.5
                              else ("失败率抬升⇒查契约" if base is not None and cur - base > 0.15
                                    else "正常"))})
    out.sort(key=lambda x: -(x["变化pp"] or 0))
    return {"rows": out, "窗口": f"{cut} 起为『近』",
            "_note": "失败率含二次确认门与业务拒绝，别只看总失败率；看『近_节流占比%』才是真不稳定。"}


def unregistered_throttles(rows: list, top: int = 12) -> dict:
    """**未登记的节流文案**：出现了疑似限流、但 `policy.THROTTLE_RULES` 不认识的。

    命中即说明**有新的限流路径还没接策略层**——正是 2026-08-19 之前
    subsidy/markettool 的状态（撞了也没人管，只表现为「有时候不稳定」）。
    """
    hits = collections.Counter()
    sample = {}
    for r in rows:
        if r.get("ok"):
            continue
        t = _text(r)
        if not t or is_throttle(t):                      # 已登记的跳过
            continue
        m = _THROTTLE_SMELL.search(t)
        if not m or _BUSINESS_DENY.search(t):
            continue
        seg = t[max(0, m.start() - 30):m.end() + 30]
        norm = re.sub(r"\d+", "N", seg)[:70]
        hits[(_key(r), norm)] += 1
        sample.setdefault((_key(r), norm), r.get("ts"))
    out = [{"path": k, "文案": s, "次数": n, "首见": sample.get((k, s))}
           for (k, s), n in hits.most_common(top)]
    return {"rows": out, "已登记路径": sorted(THROTTLE_RULES),
            "_note": "命中=有限流路径没接 core.policy；确认后往 THROTTLE_RULES 加一条即可。"}


def unaudited_writes() -> dict:
    """**登记在 `WRITE_VERIFICATION` 却没有 `@audited` 的写路径**（纯静态，不读日志）。

    ★这是「存疑」桶的真正根因：不是没用过，是**用了也不落审计**。
    2026-08-19 首次扫出：`osw/material.py`、`pic/imgzone.py` 里一个 `@audited` 都没有，
    于是 material_bind / material_sellpoints / material_reuse / imgzone_upload /
    imgzone_delete 五条**真写路径全程无审计**——出了问题查不到是谁、什么时候、传了什么。

    「写操作三重门」（dry-run → confirm_token → @audited 落审计）第三道对该域是缺的。
    """
    import ast
    import glob
    import os
    seen = set()
    for p in glob.glob(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "blacklight", "**", "*.py"), recursive=True) or []:
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for p in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
        try:
            tree = ast.parse(open(p, encoding="utf-8", errors="replace").read())
        except Exception:
            continue
        for f in ast.walk(tree):
            if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in f.decorator_list:
                call = d.func if isinstance(d, ast.Call) else d
                name = getattr(call, "id", None) or getattr(call, "attr", None)
                if name != "audited":
                    continue
                args = [a.value for a in getattr(d, "args", []) if isinstance(a, ast.Constant)]
                if len(args) >= 2:
                    seen.add(f"{args[0]}.{args[1]}")
    missing = sorted(k for k in WRITE_VERIFICATION if k not in seen)
    return {"已挂@audited": len(seen), "登记的写路径": len(WRITE_VERIFICATION),
            "缺审计": missing,
            "_note": "写操作三重门的第三道（落 audit.log）缺了：出问题查不到是谁/何时/传了什么。"}


def scan(path: str = None, since: str = None, recent_days: int = 7) -> dict:
    """跑全部检查。**离线、不打网络、秒级**，适合并进 smoke_test 或每日收尾。"""
    rows = load(path, since)
    if not rows:
        return {"ok": True, "审计条数": 0, "_note": "无审计数据，跳过"}
    sv = stale_verification(rows)
    fr = failure_rates(rows, recent_days=recent_days)
    ut = unregistered_throttles(rows)
    ua = unaudited_writes()
    # 「存疑」里凡是本来就没挂 @audited 的，根因是缺审计不是没用过 —— 归到 ua，别重复计数
    no_audit = set(ua["缺审计"])
    sv["存疑"] = [x for x in sv["存疑"] if x["path"] not in no_audit]
    sv["_存疑已剔除"] = f"其中 {len(no_audit & set(WRITE_VERIFICATION))} 条根因是缺 @audited，见『缺审计』"
    problems = len(sv["应转True"]) + len(sv["存疑"]) + len(ut["rows"]) + len(ua["缺审计"]) \
        + sum(1 for x in fr["rows"] if x["判读"] == "失败率抬升⇒查契约")
    return {"ok": problems == 0, "问题数": problems,
            "审计条数": len(rows),
            "时间跨度": [rows[0].get("ts"), rows[-1].get("ts")],
            "活体标记核对": sv, "缺审计": ua, "失败率": fr, "未登记节流": ut}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="审计日志离线回归扫描")
    ap.add_argument("--since", default=None, help="只看该日期起 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=7, help="近 N 天算『近』")
    ap.add_argument("--path", default=None)
    a = ap.parse_args()
    print(json.dumps(scan(a.path, a.since, a.days), ensure_ascii=False, indent=1, default=str))
