"""**每日必跑入口** —— 一次跑完、逐项报告成败，别再靠人记得。

2026-08-21 起。直接起因：`nptrack.snapshot()` 属于「每天必跑」，
但**断了两天没人发现**，是用户问「新品有继续追踪吗」才暴露的。
散落在记忆和待办里的"每天要做的事"没有任何机制保证它被执行。

设计原则：
· **一项失败不影响其余**（各自 try），最后统一报 `失败` 列表——别一个报错整轮停摆
· **只做只读 + 记账**，不含任何写操作（拉黑/放出/报名都要人决定）
· 结果里带 `需要人看` 汇总，把"要拍板的事"顶到最前面
"""
from __future__ import annotations

import datetime as _dt
import traceback

from blacklight.core import BlacklightError


def _try(name, fn, out):
    try:
        out["结果"][name] = fn()
        out["成功"].append(name)
    except Exception as e:
        out["失败"].append({"项": name, "err": "%s: %s" % (type(e).__name__, str(e)[:200])})
        out["结果"][name] = {"error": str(e)[:200], "trace": traceback.format_exc()[-400:]}


def run(run_date: str = None, pin: str = None, with_ledger: bool = True) -> dict:
    """跑完每日例行：账本快照 → 新品快照 → 广告空耗 → 账户体检 → 放出批次跟踪。

    `run_date` 缺省用**昨天**（ge 离线到 T-1）。`with_ledger=False` 跳过最重的账本那步。
    返回 {成功:[...], 失败:[...], 需要人看:[...], 结果:{...}}。
    """
    y = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    run_date = run_date or y
    out = {"run_date": run_date, "成功": [], "失败": [], "需要人看": [], "结果": {}}

    # ① 全量账本快照（compare 的地基，局部批次会让它失效 ⇒ 必须全量）
    if with_ledger:
        def _ledger():
            from blacklight.jzt import diagnose
            r = diagnose.daily_ledger(run_date=run_date, days=1, end_date=run_date,
                                      pin=pin, note="%s 每日必跑" % run_date)
            w = (r.get("written") or {}).get("回读") or {}
            if w.get("整列为空"):
                out["需要人看"].append("账本有整列为空：%s" % w["整列为空"])
            return {k: r.get(k) for k in ("run_date", "窗口", "在投款", "总行数",
                                          "verdict分布", "跳过的脏日", "黑名单", "written")}
        _try("账本快照", _ledger, out)

    # ② 新品测试计划逐日快照（★断过两天，就是因为没人跑）
    def _np():
        from blacklight.jzt import nptrack
        r = nptrack.snapshot(run_date=run_date, note="%s 每日必跑" % run_date)
        rows = r.get("rows") or []
        zero = [x for x in rows if not x.get("订单行")]
        if len(zero) == len(rows) and rows:
            out["需要人看"].append("新品 %d 个计划**全部零订单**——先看是不是「投不出去」"
                                   "（预算利用率），别当成效果差就停投" % len(rows))
        for x in rows:
            be = x.get("保本ROI")
            if be and be > 50:
                out["需要人看"].append("新品 %s 保本ROI %.0f（到手价毛利率仅 %.2f%%）"
                                       "⇒ 广告不可能打正，**先修定价**"
                                       % (x.get("spuId"), be, 100.0 / be))
        return {"count": r.get("count"), "rows": rows}
    _try("新品快照", _np, out)

    # ③ 广告空耗（近 7 日零成交仍消耗；★别用今日口径）
    def _waste():
        from blacklight.jzt import diagnose
        r = diagnose.ad_waste(days=7, min_cost=10.0, top=20, pin=pin, end_date=run_date)
        if (r.get("达标款数") or 0) > 0:
            out["需要人看"].append("广告空耗 ≥10 元的有 %s 款、合计 %s 元，按 `按计划` 分流处置"
                                   % (r.get("达标款数"), r.get("达标金额")))
        return {k: r.get(k) for k in ("窗口", "空耗款数", "空耗合计(折后)",
                                      "达标款数", "达标金额", "占空耗金额%")}
    _try("广告空耗", _waste, out)

    # ④ 账户体检
    def _acct():
        from blacklight.jzt import diagnose
        r = diagnose.account_check()
        s = r.get("摘要") or {}
        util = s.get("预算利用率%")
        if util is not None and util < 30:
            out["需要人看"].append("账户预算利用率 %.1f%%（花不动）——效率不差是投不出去，"
                                   "属策略问题" % util)
        return {"健康度": r.get("健康度"), "摘要": s,
                "findings": [f.get("依据") for f in (r.get("findings") or [])][:6]}
    _try("账户体检", _acct, out)

    # ⑤ 放出批次跟踪（有 watchlist 才跑）
    def _rel():
        import json
        import os
        from blacklight.jzt import ledger
        wp = os.path.join(os.path.dirname(ledger.path()), "release_watchlist.json")
        if not os.path.isfile(wp):
            return {"skip": "没有 release_watchlist.json"}
        w = json.load(open(wp, encoding="utf-8"))
        skus = [x["sku"] for x in (w.get("第一批_已放出") or [])]
        if not skus:
            return {"skip": "watchlist 为空"}
        since = w.get("放出日")
        a = ledger.agg_days(skus=skus, since=since)
        rows = a["rows"]
        days = max([v["天数"] for v in rows.values()] or [0])
        bad = [s for s, v in rows.items() if v["GMV"] > 0 and not v["达标"]]
        if days < 3:
            out["需要人看"].append("放出批次只有 %d 天数据，**不足 3 天别下结论**" % days)
        elif bad:
            out["需要人看"].append("放出批次有 %d 款多日聚合仍亏损 ⇒ 建议重新拉黑：%s"
                                   % (len(bad), bad[:8]))
        return {"放出日": since, "跟踪款数": len(skus), "已有天数": days,
                "多日亏损款": bad}
    _try("放出跟踪", _rel, out)


    # ⑥ 自检四件套（静态部分，不联网）—— 一个没人跑的检查等于不存在
    def _audit():
        from blacklight.core import contract_lint, auditscan
        l = contract_lint.lint()
        if not l.get("ok"):
            bad = {k: len(v) for k, v in l.items() if isinstance(v, list) and v}
            out["需要人看"].append("contract_lint 有 %s ⇒ 跑 `blacklight audit` 看细节" % bad)
        a = auditscan.scan()
        v = a.get("活体标记核对") or {}
        if v.get("应转True"):
            out["需要人看"].append("写验证矩阵过期：%s 已有成功记录却仍标未活体"
                                   % [x.get("path") for x in v["应转True"]])
        return {"lint_ok": l.get("ok"), "auditscan_问题数": a.get("问题数")}
    _try("自检(静态)", _audit, out)
    out["小结"] = ("成功 %d 项 / 失败 %d 项 / 需要人看 %d 条"
                   % (len(out["成功"]), len(out["失败"]), len(out["需要人看"])))
    return out
