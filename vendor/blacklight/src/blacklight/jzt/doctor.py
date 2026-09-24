"""
jzt-mcp 契约巡检（doctor）—— 京准通接口是从**前端 bundle 反解 + 抓包**来的，页面改版就会漂。
尤其 `/swa/ad/list` 的 `conversionCategory`（转化周期）：这个字段名一变，返回的就不是"少几列"，
而是整个接口 400 —— 无人值守跑批前先 doctor，`healthy=False` 转人工再放手。

覆盖：免密登录 / 推广列表(字段+分页+合计行) / 指标口径 / 转化周期哨兵 / 账户余额。只读、无写操作。
（2026-08-05 撤掉「账号枚举」一项：skippwd/list 契约稳定、从没漂过，每轮多打一次网络请求换不到信息；
 而且免密登录那一项已经间接覆盖了同一套 atoms 网关与信封。要单独查账号用 `jzt_accounts`。）
"""
from blacklight.jzt import auth as jzt_auth
from blacklight.jzt import finance
from blacklight.jzt import swa


def _check(name, fn):
    try:
        ok, detail = fn()
        return {"check": name, "ok": bool(ok), "detail": detail}
    except Exception as e:
        return {"check": name, "ok": False, "detail": f"异常: {str(e)[:120]}"}


def _login():
    jzt_auth.session(force=True)
    return (True, "免密登录成功（skpp_p/skpp_s 已种）")


def _ad_list():
    """最关键的一条：conversionCategory 还认不认。"""
    r = swa.ad_list(page=1, page_size=2)
    if not isinstance(r.get("total"), int):
        return (False, f"total 非 int（分页契约漂移）：{r.get('total')}")
    rows = r.get("rows") or []
    if not rows:
        return (False, f"列表空（total={r.get('total')}）——可能日期区间无数据，或返回结构漂移")
    need = ["campaignId", "groupId", "推广名", "状态", "日预算", "出价", "花费", "全站投产比"]
    miss = [f for f in need if f not in rows[0]]
    none_keys = [f for f in ("campaignId", "groupId") if rows[0].get(f) is None]
    if none_keys:
        return (False, f"主键为空 {none_keys}——写操作(改预算/改出价)会打空，字段名可能已改")
    return (not miss, f"total={r['total']}｜缺字段:{miss}" if miss else f"字段齐, total={r['total']}")


def _totals():
    """合计行只有带 product='swa_dsp' 才回——这条挂了说明基线 params 被改瘦了。"""
    r = swa.summary()
    t = r.get("合计") or {}
    if all(v is None for v in t.values()):
        return (False, "合计行(ext)全空——检查 params 是否还带 product='swa_dsp'")
    need = ["花费", "全站投产比", "全站交易额", "全站订单行", "全站订单成本"]
    miss = [f for f in need if t.get(f) is None]
    return (not miss, f"合计缺口径:{miss}" if miss else
            f"口径齐（花费{t['花费']}/投产比{t['全站投产比']}/订单{t['全站订单行']}）")


def _conversion_category():
    """反向验证：**故意不传**转化周期，应当报「转化周期不允许为空」——报别的说明字段名换了。"""
    from blacklight.core import BlacklightError
    body = swa._params(1, 1, *swa._default_days(None, None))
    body.pop("conversionCategory")
    try:
        jzt_auth.post("/swa/ad/list", body)
        return (False, "缺 conversionCategory 竟然成功了——服务端已改，本模块的必填假设过期")
    except BlacklightError as e:
        return ("转化周期" in str(e), f"哨兵报错：{str(e)[:80]}")


def _balance():
    """余额三本账。**必须验原始字段名存在**——`balance()` 用 `d.get(k) or 0` 取值，
    JD 一改名（cashBalance→cashAmount 之类）就会**静默返回 0**，
    外面看到的是"余额耗尽/断投预警"而不是报错，是最危险的一类漂移。
    同时这条兼作 `code==1` 信封的哨兵：信封变了 auth.post 会直接抛。"""
    b = finance.balance()
    raw = b.get("_raw") or {}
    need_raw = ["cashBalance", "awardBalance", "commissionBalance",
                "cashFreeze", "awardFreeze", "commissionFreeze"]
    miss = [f for f in need_raw if f not in raw]
    if miss:
        return (False, f"原始字段缺失 {miss} —— 余额会被静默读成 0，误报「断投」，先改 finance.py 映射")
    if not all(isinstance(raw.get(f), (int, float)) for f in need_raw):
        bad = [f for f in need_raw if not isinstance(raw.get(f), (int, float))]
        return (False, f"字段非数值 {bad} —— 可能改成了字符串/分为单位，float() 口径要重核")
    return (True, "字段齐（现金{}/红包{}/佣金{}，可投放合计 {}）".format(
        b["现金可用"], b["红包可用"], b["佣金可用"], b["可投放合计"]))


def run() -> dict:
    checks = [_check("免密登录", _login),
              _check("推广列表(ad/list)", _ad_list),
              _check("合计行/指标口径", _totals),
              _check("转化周期必填哨兵", _conversion_category),
              _check("账户余额(allbalance)", _balance)]
    drift = [c["check"] for c in checks if not c["ok"]]
    return {"healthy": not drift, "drift": drift, "checks": checks,
            "账号": jzt_auth.current_account()}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
