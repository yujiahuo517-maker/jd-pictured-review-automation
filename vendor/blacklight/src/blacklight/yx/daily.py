# -*- coding: utf-8 -*-
"""**yx 侧每日必跑入口**（对标 `jzt/daily.py`）—— 促销/报名这条线以前**全靠人记得**。

2026-08-24 起。直接起因：
· **百补从 8-21 断到 8-24 没人发现**（周末+周一都没报），是用户问「秒杀和百补都报了没」才暴露的，
  而当天 A 桶有 60 款可报；
· **失效预警一天扫一次不够**：平台在场次开始前才挂预警，当天 10:00 扫完 15→0，17:00 又冒出 27 条
  （本人 24）。⇒ 本入口设计成**一天跑两次**（早/晚），`part='morning'|'evening'` 只影响提示措辞。

设计原则（同 jzt/daily）：
· **一项失败不影响其余**（各自 try），最后统一报 `失败`
· **只读、零写**：报名/降价/拉黑都要人拍板，这里只负责**把该做的事顶出来**
· `需要人看` 排在最前面，写清楚"下一步该调哪个函数"
"""
from __future__ import annotations

import datetime as _dt
import traceback

from blacklight.core.base import scene_cfg

# ★2026-08-24 审查修（这条是我自己 8-24 写这个文件时犯的）：这三个 ID 在 core/config.json 里
#   本来就有，硬编码等于同一批 ID 有两份来源，谁改了另一处不改就漂。一律从 config 取。
_MS_CFG = scene_cfg("ms")
_BYBT_CFG = scene_cfg("bybt")
SECKILL_ACTIVITY = _MS_CFG.get("seckill_activity_id", "101666814")
SECKILL_AREA = int(_MS_CFG.get("seckill_area_id", 313601))
BYBT_ACTIVITY = _BYBT_CFG.get("default_activity_id", "101664799")


def _try(name, fn, out):
    try:
        out["结果"][name] = fn()
        out["成功"].append(name)
    except Exception as e:
        out["失败"].append({"项": name, "err": "%s: %s" % (type(e).__name__, str(e)[:200])})
        out["结果"][name] = {"error": str(e)[:200], "trace": traceback.format_exc()[-400:]}


def run(part: str = None, with_bybt_plan: bool = True, pin: str = None) -> dict:
    """跑完 yx 侧每日例行：代码新鲜度 → 秒杀失效预警 → 秒杀当日报名进度 → 百补报名缺口 → 促销池概览。

    `part`: 'morning' / 'evening'（缺省按当前钟点猜）。**一天跑两次**，晚场那次专治
      「场次开始前才挂出来的预警」。
    `with_bybt_plan=False` 跳过最重的百补规划（约 180s）。
    返回 {成功, 失败, 需要人看, 结果}。**只读**。
    """
    now = _dt.datetime.now()
    part = part or ("morning" if now.hour < 14 else "evening")
    out = {"跑批时刻": now.strftime("%Y-%m-%d %H:%M:%S"), "part": part,
           "成功": [], "失败": [], "需要人看": [], "结果": {}}

    # ① 代码新鲜度（放第一条：跑的要是旧代码，后面所有结论都不算数）
    def _fresh():
        from blacklight.core.freshness import code_freshness
        r = code_freshness()
        if r["ok"] is False:
            out["需要人看"].append("**进程里是旧代码**（%d 个模块在启动后被改过）⇒ MCP 请 /mcp 重连，"
                                   "否则新参数会被静默丢弃" % r["陈旧数"])
        return r
    _try("代码新鲜度", _fresh, out)

    # ② 秒杀失效预警（★一天两次；报上名≠会生效，不处理到点作废还占坑）
    def _warn():
        from blacklight.yx import ms
        from blacklight.core import auth as jd_auth
        erp = pin or jd_auth.current_pin()
        w = ms.invalidation_warnings(SECKILL_ACTIVITY, SECKILL_AREA)
        rows = w.get("rows") or []
        mine = [r for r in rows if str(r.get("erpPin") or "") == str(erp)]
        ok_drop = [r for r in mine if str(r.get("处置")) == "照降"]
        lossy = [r for r in mine if str(r.get("处置")) != "照降"]
        if ok_drop:
            out["需要人看"].append("秒杀失效预警：本人 %d 款可**照降**（不处理到点作废且占坑）⇒ "
                                   "`ms.plan_reduce_warned()` 出 rows → dryrun → reduce_price → "
                                   "**`ms.verify_reduced()` 判成败（别看预警桶）**" % len(ok_drop))
        if lossy:
            out["需要人看"].append("秒杀失效预警：本人 %d 款**降了会亏**，要人拍板（降/退出/不动）" % len(lossy))
        return {"全量": len(rows), "本人": len(mine), "照降": len(ok_drop), "降了会亏": len(lossy),
                "分桶": w.get("分桶"), "场次": sorted({str(r.get("场次")) for r in mine})}
    _try("秒杀失效预警", _warn, out)

    # ③ 秒杀当日报名进度（T+3 起报；只看"今天报了没"，不触发导出——那要 ~9 分钟且有 40 分钟触发窗口）
    def _sk():
        from blacklight.yx import ms
        today = now.strftime("%Y-%m-%d")
        tasks = ms.submit_list(SECKILL_AREA, limit=10)["tasks"]
        mine_today = [t for t in tasks if str(t.get("createTime") or "").startswith(today)]
        tgt = (now.date() + _dt.timedelta(days=3)).isoformat()      # 今天能报的最早场次
        if not mine_today:
            out["需要人看"].append("秒杀**今天还没提交过报名**（最早可报场次 %s）⇒ "
                                   "`ms.plan_seckill_enroll()`（先跑，导出约 9 分钟）" % tgt)
        return {"今天提交批次": [{"id": t["id"], "total": t.get("total"), "success": t.get("success"),
                                  "fail": t.get("fail"), "createTime": t.get("createTime")}
                                 for t in mine_today],
                "最早可报场次(T+3)": tgt}
    _try("秒杀报名进度", _sk, out)

    # ④ 百补报名缺口（★断了 3 天没人发现，就是缺这一项）
    def _bybt():
        from blacklight.yx import bybt
        recs, page = [], 1
        while True:
            d = bybt.get_applied(BYBT_ACTIVITY, page=page, page_size=200, only_active=False)
            it = d.get("items") or []
            recs += it
            if not it or len(recs) >= (d.get("totalCount") or 0) or page > 40:
                break
            page += 1
        days = sorted({str(r.get("appliedTime"))[:10] for r in recs if r.get("appliedTime")})
        last = days[-1] if days else None
        today = now.strftime("%Y-%m-%d")
        gap = None
        if last:
            gap = (now.date() - _dt.date.fromisoformat(last)).days
        res = {"已报名总条数": len(recs), "最近报名日": last, "距今天数": gap, "今天已报": last == today}
        if with_bybt_plan:
            p = bybt.plan_bulk_enroll(activity_id=BYBT_ACTIVITY, target_margin=0.05)
            a = len(p.get("A_biddable") or [])
            res["规划"] = p.get("summary")
            if a:
                out["需要人看"].append("百补 A 桶 **%d 款可报**（最近一次报名 %s，距今 %s 天）⇒ "
                                       "`bybt.enroll_bulk_dryrun/enroll_bulk`，报完按 appliedTime 回读"
                                       % (a, last, gap))
        elif last != today:
            out["需要人看"].append("百补**今天还没报**（最近 %s，距今 %s 天）⇒ 先跑 plan_bulk_enroll" % (last, gap))
        return res
    _try("百补报名缺口", _bybt, out)

    # ⑤ 促销池概览（便宜包邮/特价：已报名条数 + 导出可用性；轻量探量，不触发导出）
    def _pools():
        from blacklight.core.base import scene_cfg
        from blacklight.yx import ms
        pools = (scene_cfg("baoyou") or {}).get("pools") or {}
        res = {}
        for name, p in pools.items():
            aid, area = p.get("activityId"), p.get("areaId")
            if not (aid and area):
                continue
            f = ms.applied_export_feasible(str(aid), int(area))
            res[name] = {"已报名条数": f.get("totalCount"), "导出可用": f.get("ok")}
        return res or {"skip": "配置里没有 baoyou.pools"}
    _try("促销池概览", _pools, out)


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
    out["小结"] = ("[%s] 成功 %d 项 / 失败 %d 项 / 需要人看 %d 条"
                   % (part, len(out["成功"]), len(out["失败"]), len(out["需要人看"])))
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=1, default=str))
