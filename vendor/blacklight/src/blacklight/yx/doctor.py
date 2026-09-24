"""
yx-mcp 契约巡检（doctor）—— 抓包封装的接口会漂移（字段/参数变、页面改版）。
定期打关键接口、校验返回结构没变；**漂移了先告警/转人工，别让无人值守 Agent 拿变形的契约去自动执行**。
只读、无写操作。跑：  py doctor.py   或 MCP 工具 yx_doctor。
"""
from blacklight.core import auth as jd_auth
from blacklight.yx import ms as yx_ms
from blacklight.yx import client as yx_client
from blacklight.yx import markettool as mt


def _check(name, fn):
    try:
        ok, detail = fn()
        return {"check": name, "ok": bool(ok), "detail": detail}
    except Exception as e:
        return {"check": name, "ok": False, "detail": f"异常: {str(e)[:90]}"}


def _formset(area):
    m = yx_ms.fetch_form_set(area)
    # 各频道通用的活动级+每SKU核心 code（promoStock/promoNum 分频道，不通用，不查）
    need = ["activityBeginHour", "activityDuration", "skuId", "promoPrice"]
    miss = [c for c in need if c not in m]
    return (not miss, f"code总数{len(m)}｜缺code:{miss}" if miss else f"code总数{len(m)} 关键code齐")


def _margin_monitor():
    with mt._client() as c:
        d = mt._call(c, "jxzy_markettool_queryPreDiscountHome",
                     {"env": "prod", "pageNo": 1, "pageSize": 1, "estimatedProfitChannel": "normal",
                      "timeType": 0, "skuStatus": 1, "strSkuIds": "", "buid": 325, "appCode": ""}, "POST") or {}
    items = d.get("skuPromotionInfoDetails") or []
    if not items:
        return (False, "监控列表返回空（可能字段名/参数漂移）")
    need = ["skuId", "jxEstimatedGrossProfitPrice", "jxEstimatedGrossMarginRate",
            "benchPrice", "promotionList", "purchasePrice"]
    miss = [f for f in need if f not in items[0]]
    return (not miss, f"totalCount={d.get('totalCount')}｜缺字段:{miss}" if miss
            else f"字段齐, totalCount={d.get('totalCount')}")


def _batchid(area):
    import datetime
    day = (datetime.date.today() + datetime.timedelta(days=4)).strftime("%Y-%m-%d")  # T+4 稳过 T+3 起报
    b = yx_ms.get_batch_id(area, f"{day} 20:00:00")
    bid = b.get("batchId") if isinstance(b, dict) else b
    return (bool(bid), f"batchId={bid} ({day})")


def _threshold_field(area, activity):
    """★**报名价上限字段哨兵**（2026-08-06 加）。

    门槛是报名硬约束，但它**只是响应里的一个字段名**——改了名不会报错，`list_eligible` 直接返回 None，
    上限约束就**无声消失**（实证：秒杀池 `opennessMinPrice` 改叫 `suggestPrice`，
    `plan_baoyou_enroll`/`price_eligible` 的 threshold 全变 None，无任何告警）。
    ∴ 这里不校验"某个字段在不在"，而是校验**归一后的 `threshold` 有没有值**——
    平台再改名，只要 `_THRESHOLD_KEYS` 没跟上，这条就会红。
    """
    rid = yx_ms.get_rule_id(area, activity)
    rid = rid.get("ruleId") if isinstance(rid, dict) else rid
    if not rid:
        return (False, "取不到 ruleId")
    import datetime
    day = (datetime.date.today() + datetime.timedelta(days=4)).strftime("%Y-%m-%d")
    b = yx_ms.get_batch_id(area, f"{day} 20:00:00")
    bid = b.get("batchId") if isinstance(b, dict) else b
    if not bid:
        return (False, f"取不到 batchId({day})")
    el = yx_ms.list_eligible(bid, area, rid, page=1, page_size=50)
    items = el.get("items") or []
    if not items:
        return (False, f"可报列表返回空(totalCount={el.get('totalCount')})")
    hit = [it for it in items if it.get("threshold") not in (None, "")]
    fields = {it.get("thresholdField") for it in hit}
    rate = len(hit) * 100 // len(items)
    # 全空 = 字段又漂了（或该池确实不给上限）→ 必须人工确认，不能默默按"无约束"跑
    return (bool(hit), f"门槛非空 {len(hit)}/{len(items)}({rate}%)｜命中字段{sorted(fields) or '无'}"
            + ("" if hit else "｜★全空：字段可能又改名，_THRESHOLD_KEYS 需跟进，否则上限约束静默失效"))


def _freshness():
    """跑着的进程用的是不是磁盘最新代码。ok=None(测不出)按通过算但把话说明白。"""
    from blacklight.core.freshness import code_freshness
    r = code_freshness()
    if r["ok"] is None:
        return (True, "无法判定（拿不到进程启动时间）——别把'没测出来'当'没问题'")
    return (r["ok"], "进程 %s｜%s" % (r.get("进程启动"), r.get("note")))


def _batchid_ignored(area, activity, rule):
    """★契约：`pagingQuerySkuList4Http` **不吃 batchId**（2026-08-24 实测：瞎编 batchId 返回同一个
    totalCount，瞎编 ruleId 才返 None）。我们据此得出"用 list_eligible 核不出某场次的可报池"，
    只能以导出为准。哪天平台开始认 batchId 了，这条会红 —— 那正是要复查那个结论的时候。"""
    from blacklight.yx import ms as _ms
    body = dict(currentPage=1, pageSize=1, businessType=122, dispFields=_ms._DISP_FIELDS,
                areaId=int(area), ruleFields=[], dimType=1, ruleType=0,
                filterFields=_ms._build_filter(jd_auth.current_pin(), "", rule),
                sortFields=[{"ruleContent": []}], activityDuration="28")
    def _n(bid, rid=None):
        b = dict(body, batchId=int(bid))
        if rid is not None:
            b["filterFields"] = _ms._build_filter(jd_auth.current_pin(), "", rid)
        with _ms._client() as c:
            return (_ms._post(c, "/openness/selection/pagingQuerySkuList4Http", b) or {}).get("totalCount")
    import datetime as _d
    day = (_d.date.today() + _d.timedelta(days=4)).strftime("%Y-%m-%d")   # T+4 稳过 T+3 门槛
    real = _ms.get_batch_id(area, day + " 20:00:00")
    real = real if isinstance(real, int) else (real or {}).get("batchId")
    a, b = _n(real), _n(9999999)
    c = _n(real, "zzz_not_a_rule")            # 阴性对照：ruleId 必须有区分度
    ok = (a == b) and (c != a)
    return (ok, "batchId 真/瞎编 = %s/%s（应相同）；瞎编 ruleId = %s（应不同，证明判据有区分度）"
                % (a, b, c))


def _applied_cap(activity, area):
    """★契约：已报名导出有 **50000 行上限**，全活动必超、单场次必不超（2026-08-24 实测 137716）。
    这条决定了"导出前必须收时间窗"这个作业规则还成不成立。"""
    from blacklight.yx import ms as _ms
    import datetime as _d
    day = (_d.date.today() + _d.timedelta(days=3)).isoformat()
    whole = _ms.applied_export_feasible(activity, area)
    one = _ms.applied_export_feasible(activity, area, "3", day + " 00:00:00", day + " 23:59:59")
    ok = (whole.get("ok") is False) and (one.get("ok") is True)
    return (ok, "全活动 %s 条(ok=%s，应 False) / 单场次 %s 条(ok=%s，应 True)"
                % (whole.get("totalCount"), whole.get("ok"), one.get("totalCount"), one.get("ok")))


def _campaign(cid):
    d = yx_client.get_activity_detail(cid)
    return (isinstance(d, dict) and bool(d), f"字段{len(d)}个" if isinstance(d, dict) else "无返回")


def run(area_seckill: int = 313601, area_baoyou: int = 357902, campaign_id: str = "2497067",
        activity_seckill: str = "101666814", activity_baoyou: str = "101682440") -> dict:
    """打关键接口校验结构。返回 {healthy, drift:[漂移的检查], checks:[...]}。"""
    results = [
        # ★放第一条：跑的要是旧代码，下面所有检查验的都不是你改的东西（2026-08-24 教训）
        _check("代码新鲜度(进程 vs 磁盘)", _freshness),
        _check("登录探活(mcpman)", lambda: (jd_auth.yx_is_logged_in() is True, "已授权")),
        _check("ms.formset 活动级formItemId(按code)", lambda: _formset(area_seckill)),
        _check("markettool 毛利监控字段(queryPreDiscountHome)", _margin_monitor),
        _check("ms.getBatchId 场次", lambda: _batchid(area_seckill)),
        # 门槛字段按池不同(秒杀=suggestPrice / 便宜包邮=opennessMinPrice)，两池分别探——单探一池会漏
        _check("ms 报名价上限字段(秒杀池)", lambda: _threshold_field(area_seckill, activity_seckill)),
        _check("ms 报名价上限字段(便宜包邮池)", lambda: _threshold_field(area_baoyou, activity_baoyou)),
        _check("ms 可报清单忽略 batchId（★平台契约）",
               lambda: _batchid_ignored(area_seckill, activity_seckill,
                                        "87yoF4oB97mt0bp_tXKW")),
        _check("已报名导出 50000 行上限仍成立", lambda: _applied_cap(activity_seckill, area_seckill)),
        _check("campaign 活动详情", lambda: _campaign(campaign_id)),
    ]
    drift = [r["check"] for r in results if not r["ok"]]
    return {"healthy": not drift, "drift": drift,
            "note": "有 drift → 相关接口字段/参数可能漂移，写路径先转人工核对再放手。" if drift else "关键接口契约正常。",
            "checks": results}


if __name__ == "__main__":
    import json
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    raise SystemExit(0 if r["healthy"] else 1)
