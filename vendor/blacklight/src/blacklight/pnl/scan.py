"""统一入口：`margin_scan()` —— 毛利监控的一键巡检。

## 两张网，**并集**，不取交集（2026-08-11 用户纠正）
    网A 已发生/正在发生 (ge)  → 谁**现在**在亏 → 止血
    网B 预测           (osw) → 配置已亏但**还没出单** → 前瞻拦截

两者回答**不同问题**，不是同一总体的两个样本。取交集会把 ge 独有的
137 款（占当日成交 19.6%、毛利 61.2%）真实成交直接扔掉，
理由仅仅是 osw 归属口径认不出它们——那只影响**能不能动手**（L4 才判），
不该污染发现阶段。

## 实时 ≠ 一天
ge 实时窗口是「今日 00:00 至此刻」，当日过半天时只有 ~900 款；
同一天的**离线**口径有 5,417 款。**两者不可直接比**，环比要用离线对离线。

## 判亏口径
`ge.margin.judge_losing()`，默认 **投后**（已扣广告）。
投前 vs 投后差 7 倍（35 vs 252 款），务必别用错。
"""
from __future__ import annotations

AD_WASTE_EXTREME = 5.0   # 单日单款空耗≥此值才在毛利监控里点名；全量走 jzt_ad_waste

import datetime as _dt
import json as _json
import os as _os
import time as _time

from blacklight.core import BlacklightError, pmap
from blacklight.core import paths as _paths
from blacklight.ge import margin as ge_margin
from blacklight.osw import margin as osw_margin

from .sentinel import bridge_closes, ownership_overlap, run_sentinels


# ★前瞻网（osw 全量扫）**22.1 秒**，而 ge 全部下钻加起来才 ~7 秒（实测 2026-08-11：
#   bridge 1.1 / sku 1.3 / coupon 2.2 / promotion 2.3 / osw 22.1）。
#   前瞻网算的是**配置**（当前定价与券促决定的预估毛利），只有有人改配置才会变
#   ⇒ 加 TTL 缓存，冷启 ~26 秒、热启 ~4 秒。缓存属机器相关可再生 ⇒ 放 runtime/。
_FORECAST_TTL = 900          # 15 分钟


def _forecast_cache_path():
    d = _os.path.join(_paths.home(), "cache")
    _os.makedirs(d, exist_ok=True)
    return _os.path.join(d, "osw_forecast.json")


def _load_forecast(mode: str, ttl: int = _FORECAST_TTL):
    """mode: 'auto'(缓存新鲜就用) / 'force'(强制刷新) / 'skip'(不跑前瞻网)。"""
    if mode == "skip":
        return {"rows": [], "_cached": None, "_skipped": True}
    fp = _forecast_cache_path()
    if mode == "auto" and _os.path.exists(fp):
        try:
            blob = _json.load(open(fp, encoding="utf-8"))
            age = _time.time() - blob.get("ts", 0)
            if age <= ttl:
                blob["_cached"] = round(age)
                return blob
        except Exception:
            pass          # 缓存坏了就重扫，不要因为缓存问题让巡检失败
    got = osw_margin.scan_portfolio()
    blob = {"ts": _time.time(), "rows": got.get("rows") or []}
    try:
        _json.dump(blob, open(fp, "w", encoding="utf-8"), ensure_ascii=False, default=str)
    except Exception:
        pass
    blob["_cached"] = 0
    return blob


def _today_window():
    now = _dt.datetime.now()
    return now.strftime("%Y-%m-%d 00:00:00"), now.strftime("%Y-%m-%d %H:%M:%S")


def scan_rt(erp: str = None, top: int = 20, with_carriers: bool = True,
            strict: bool = True, forecast: str = "auto") -> dict:
    """实时巡检：谁在亏 / 亏在哪一项 / 挂哪张券或促销 / 前瞻拦截。

    strict=True 时恒等式红灯直接抛——**红灯不出结论**。
    """
    start, end = _today_window()

    # ---- 并发取数：桥 + 三个下钻 + osw 前瞻网 ----
    def _bridge(_):
        return ge_margin.bridge(start, end, erp=erp)

    def _sku(_):
        return ge_margin.drill(start, end, degree="sku", erp=erp)

    def _coupon(_):
        return ge_margin.drill(start, end, degree="coupon", erp=erp)

    def _promo(_):
        return ge_margin.drill(start, end, degree="promotion", erp=erp)

    def _osw(_):
        return _load_forecast(forecast)

    jobs = [_bridge, _sku, _osw] + ([_coupon, _promo] if with_carriers else [])
    got = pmap(lambda f: f(None), jobs, workers=len(jobs))
    bridge = got[0]
    sku = got[1]
    osw = got[2]
    fc_age = osw.get("_cached")
    fc_skipped = bool(osw.get("_skipped"))
    coupon = got[3] if with_carriers else {"rows": []}
    promo = got[4] if with_carriers else {"rows": []}

    # ---- 哨兵：桥闭合 + 归属口径度量 ----
    ge_skus = [r["sku_id"] for r in sku["rows"]]
    osw_rows = osw.get("rows") or []
    osw_skus = [r["skuId"] for r in osw_rows]
    s_bridge = bridge_closes(bridge)
    s_own = ownership_overlap(ge_skus, osw_skus)
    health = run_sentinels(s_bridge, s_own, strict=strict)

    # ---- 网A：已发生（ge，判亏用投后 + 必须有成交）----
    # ★三类必须分开报：混在一起会把亏损款数虚增 6 倍（41 → 247）
    import collections as _c
    kinds = _c.Counter(r["行类型"] for r in sku["rows"])
    realized = [r for r in sku["rows"] if ge_margin.judge_losing(r)]
    realized.sort(key=lambda r: r["预估毛利_投后"])
    ad_waste = [r for r in sku["rows"] if r["行类型"] == "广告空耗"]
    ad_waste.sort(key=lambda r: r["预估毛利_投后"])

    # ---- 网B：预测（osw 预估<0）。★不与网A取交集 ----
    osw_by_sku = {str(r["skuId"]): r for r in osw_rows}
    ge_set = {str(s) for s in ge_skus}
    forecast_rows = []
    for r in osw_rows:
        try:
            if not osw_margin.is_losing(r, by="estimate"):
                continue
        except Exception:
            continue
        forecast_rows.append({
            "skuId": str(r["skuId"]), "name": r.get("name"),
            "预估毛利": r.get("预估毛利"),
            "近15日单量": r.get("近15日单量"),
            "今日有成交": str(r["skuId"]) in ge_set,
            "根因": r.get("根因"),
        })
    forecast_rows.sort(key=lambda x: (x["预估毛利"] or 0))
    # 前瞻的价值在「还没出单就拦住」——把这批单独标出来
    not_yet = [f for f in forecast_rows if not f["今日有成交"]]

    for r in realized:
        s = str(r["sku_id"])
        r["osw可见"] = s in osw_by_sku
        r["名称"] = (osw_by_sku.get(s) or {}).get("name")

    def _carrier(rows, name_key):
        out = [r for r in rows if (r.get("减免_采销实担") or 0) > 0]
        out.sort(key=lambda r: -(r.get("减免_采销实担") or 0))
        return [{"id": r.get(name_key[0]), "名称": r.get(name_key[1]),
                 "采销实担": r.get("减免_采销实担"),
                 "预估毛利_投后": r.get("预估毛利_投后"),
                 "单量": r.get("单量")} for r in out[:top]]

    return {
        "窗口": [start, end],
        "口径": "ge 实时=已发生（判亏用投后，已扣广告）；osw=预测（前瞻）",
        "哨兵": health,
        "毛利桥": {"投后": bridge["预估投后履约毛利"], "闭合": bridge["闭合"],
                   "拆解": bridge["拆解"]},
        "网A_已发生": {
            "总行数": sku["count"], "行类型分布": dict(kinds),
            "有成交亏损款数": len(realized),
            "亏损合计": round(sum(r["预估毛利_投后"] for r in realized), 2),
            "Top": realized[:top],
            # ★`Top` 只是展示用的前 N；下游（如 both 模式的归因）**必须用这个全量清单**，
            #   否则会静默只处理前 N 款（2026-08-11 写 both 时就踩了，靠哨兵红灯才发现）
            "全部亏损SKU": [r["sku_id"] for r in realized],
            "_说明": "ge 口径，含 osw 认不出的款（osw可见=False）——它们是真实成交。"
                     "★只统计**有成交**的亏损款；★下游取 `全部亏损SKU` 不是 `Top`",
        },
        "广告空耗": {
            "款数": len(ad_waste),
            "合计": round(sum(r["预估毛利_投后"] for r in ad_waste), 2),
            # ★用户 2026-08-12 定的边界：毛利监控**只提一句 + 报极端值**，全量处置走 jzt。
            #   极端值门槛按单日消耗给（当日口径），常规量级不在这里展开。
            "极端值(单日≥%.0f元)" % AD_WASTE_EXTREME: [
                {"sku_id": r["sku_id"], "广告消耗": r.get("广告消耗")}
                for r in ad_waste if (r.get("广告消耗") or 0) >= AD_WASTE_EXTREME],
            "_说明": "★零成交但有广告消耗——亏损**100% 是广告费**，"
                     "属**广告线(jzt)**不是商品定价问题，**本表只提示不处置**。",
            "_处置入口": "jzt_ad_waste（近7日口径）。**别拿这里的当日数去拉黑**——"
                         "今日零成交可能只是还没出单；七天一单没出才叫空耗。"
                         "实测七日真空耗 2,220 款/8,410 元，**8% 的款占一半金额**。",
        },
        "网B_预测": dict({
            "预估亏损款数": len(forecast_rows),
            "其中今日还没出单": len(not_yet),
            "Top未出单": not_yet[:top],
            "缓存年龄秒": fc_age, "已跳过": fc_skipped,
            "_说明": "osw 预估<0。**前瞻拦截的价值全在『还没出单』这批**。"
                     "前瞻网 22 秒且只随配置变化 ⇒ 默认 15 分钟缓存；"
                     "刚改过券促请用 forecast='force'",
        }, **_forward_triage(forecast_rows)),
        "载体_券": _carrier(coupon["rows"], ("batch_id", "jdr_sch_coupon_batch_name")),
        "载体_促销": _carrier(promo["rows"],
                              ("jdr_sch_page_promotion_id", "jdr_sch_promotion__act_name")),
        "★下一步": [
            "1) 网A『有成交亏损』才是止亏主线，按投后毛利升序处置",
            "1b) 『广告空耗』**本表只提示不处置** ⇒ 走 `jzt_ad_waste`（近7日口径）；"
            "只有『极端值』才在毛利监控里点名",
            "2) 网B **先看 `按根因`、再看 `值得动手`** —— 实测 210/211 是减免打穿，"
            "动作是**退券/退国补/退促销**不是改价；且 206/211 亏五毛且半月卖 1~2 单，动手是净亏",
            "3) 载体表只有今日累计、**没有基线**——要判是不是在爆发，走 `attribute_rt(skus, 基线窗口)`（实时归因，2.7 秒）",
            "3b) 要「券致亏 vs 结构性」的无券反事实，**只能走离线**（easybi 是 T-1）",
            "4) ⚠️实时只用于止血，**定责用离线**（当日广告/物流未结算）",
        ],
    }


def margin_scan(mode: str = "rt", *, erp: str = None, top: int = 20,
                skus: list = None, recent_window: tuple = None,
                baseline_window: tuple = None, strict: bool = True,
                forecast: str = "auto") -> dict:
    """★**统一入口**。mode: 'rt' / 'offline' / 'both'。

        rt      —— 实时（T-0）：谁现在在亏 / 亏在哪一项 / 前瞻拦截。热启 ~2 秒
        offline —— 离线（T-1）：流速 → 逐券承担 → 券名发券人 → 可不可摘。~5 秒
        both    —— 先 rt 发现，再拿 rt 的亏损款去 offline 归因

    ⚠️`offline` 必须给 `skus`（不给则跨源没法对齐范围）；
      `both` 会自动用 rt 的「有成交亏损」款作为 offline 的 skus。
    ⚠️**实时只用于止血、不用于定责**——当日广告/物流未结算。
    """
    from .offline import attribute, MAX_SKU_FILTER

    if mode not in ("rt", "offline", "both"):
        raise BlacklightError("mode 只能是 rt / offline / both，收到 %r" % mode)

    out = {"mode": mode}
    rt = None
    if mode in ("rt", "both"):
        rt = scan_rt(erp=erp, top=top, strict=strict, forecast=forecast)
        out["实时"] = rt
    if mode in ("offline", "both"):
        if mode == "both":
            # ★用**全量亏损清单**，不是 `Top`（Top 只是展示用的前 N）。
            #   2026-08-11 这里写成 Top 过，`both` 会静默只归因前 N 款、其余全丢；
            #   靠恒等式哨兵红灯才发现（样本太小导致偏差 3.2%）。
            allsku = rt["网A_已发生"]["全部亏损SKU"]
            if len(allsku) > MAX_SKU_FILTER:
                out["_截断提示"] = ("有成交亏损 %d 款，超过单次 %d 上限，"
                                    "本次只归因前 %d 款；其余请分批"
                                    % (len(allsku), MAX_SKU_FILTER, MAX_SKU_FILTER))
            skus = allsku[:MAX_SKU_FILTER]
            if not skus:
                out["离线"] = {"_跳过": "实时没有『有成交亏损』的款，无需归因"}
                return out
        if not skus:
            raise BlacklightError(
                "mode='offline' 必须给 skus——不给的话 ge 查整个 cate_op_erp 范围，"
                "与 easybi 只统计传入 SKU 不可比")
        rw = recent_window or _default_windows()[0]
        bw = baseline_window or _default_windows()[1]
        out["离线"] = attribute(skus, rw, bw, top=top, strict=strict)
    return out


def _default_windows():
    """默认近 7 日 / 前 8 日。离线口径以 T-1 为最新可用日。"""
    today = _dt.date.today()
    t1 = today - _dt.timedelta(days=1)
    r_end = t1
    r_start = t1 - _dt.timedelta(days=6)
    b_end = r_start - _dt.timedelta(days=1)
    b_start = b_end - _dt.timedelta(days=7)
    f = lambda d: d.isoformat()
    return (f(r_start), f(r_end)), (f(b_start), f(b_end))


def scan_offline(start: str, end: str, erp: str = None, top: int = 20,
                 strict: bool = True) -> dict:
    """★**离线历史发现**：某个已结束的窗口里，谁亏了、亏了多少。

    补上第三层——原来只有「实时(已发生) + 预测(前瞻)」，
    缺「**历史**」这层，也就答不了「昨天亏了哪些品/亏了多少钱」。

    | 层 | 函数 | 问题 |
    |---|---|---|
    | 历史 | `scan_offline` | 昨天/上周**亏了**哪些、多少 |
    | 实时 | `scan_rt`      | **现在**谁在亏 → 止血 |
    | 预测 | `scan_rt` 的网B | 配置已亏但**还没出单** → 前瞻拦截 |

    ⚠️**离线覆盖面比实时大一个量级**（同一天：实时窗口 915 款 vs 离线 5,417 款），
      因为实时只到「此刻」。**两者不可直接比**，环比要离线对离线。
    ⚠️离线同样要按 `classify()` 三分——离线 08-10 的 5,417 行里
      **3,110 行是幽灵行、1,128 行是广告空耗**，真正有成交亏损的只有 62 款。
    """
    sku = ge_margin.drill(start, end, degree="sku", erp=erp, realtime=False)
    bridge = ge_margin.bridge(start, end, erp=erp, realtime=False)
    s_bridge = bridge_closes(bridge)
    health = run_sentinels(s_bridge, strict=strict)

    import collections as _c
    kinds = _c.Counter(r["行类型"] for r in sku["rows"])
    losing = [r for r in sku["rows"] if ge_margin.judge_losing(r)]
    losing.sort(key=lambda r: r["预估毛利_投后"])
    ad_waste = [r for r in sku["rows"] if r["行类型"] == "广告空耗"]
    ad_waste.sort(key=lambda r: r["预估毛利_投后"])
    profit = [r for r in sku["rows"] if r["行类型"] == "盈利"]

    cf_bad = [r for r in losing if (r.get("无减免后单均") or 0) <= 0]
    return {
        "窗口": [start, end], "口径": "ge 离线（T-1 及更早，已结算）",
        "哨兵": health,
        "毛利桥": {"投后": bridge["预估投后履约毛利"], "闭合": bridge["闭合"],
                   "拆解": bridge["拆解"]},
        "总行数": sku["count"], "行类型分布": dict(kinds),
        "有成交亏损": {
            "款数": len(losing),
            "亏损合计": round(sum(r["预估毛利_投后"] for r in losing), 2),
            "其中结构性": len(cf_bad),
            "其中减免致亏": len(losing) - len(cf_bad),
            "Top": [{"sku": r["sku_id"], "投后毛利": r["预估毛利_投后"],
                     "成交金额": r["成交金额"], "单量": r["单量"],
                     "采销实担": r.get("减免_采销实担"),
                     "广告消耗": r.get("广告消耗"),
                     "无减免后单均": r.get("无减免后单均"),
                     "判定": r.get("反事实判定")} for r in losing[:top]],
            "全部亏损SKU": [r["sku_id"] for r in losing],
        },
        "广告空耗": {"款数": len(ad_waste),
                     "合计": round(sum(r["预估毛利_投后"] for r in ad_waste), 2),
                     "_说明": "零成交但有广告消耗 ⇒ 广告线(jzt)"},
        "盈利": {"款数": len(profit),
                 "合计": round(sum(r["预估毛利_投后"] for r in profit), 2)},
        "★下一步": [
            "1) 『减免致亏』的去 attribute() 逐券归因 → feasibility 看能不能摘",
            "2) 『结构性』的走定价/换供/物流，摘券退促都治不了",
            "3) 环比要**离线对离线**，别拿实时窗口比整天",
        ],
    }


def _forward_triage(rows: list, min_loss: float = 1.0, min_qty: int = 5) -> dict:
    """前瞻网分诊：**按根因分流 + 按处置成本筛**（2026-08-12 补）。

    此前只报「预估亏损 N 款」，动作笼统写成「改价」——**两处都错**：

    ① **动作指错方向**。实测 211 款里 **210 款有我担减免**：
       券打穿 179 / 国补 21 / 促销 8 / **物流成本过高仅 2**。
       只有那 2 款该改价，其余 209 款动作是「**退**」不是「涨」。
       而且 osw 本来就逐款给了 `根因` 字段，不需要猜。

    ② **绝大部分不值得动手**。211 款预估亏损合计仅 −112.5（均 −0.53/款），
       券打穿的 179 款里 104 款（58%）近 15 日 ≤2 单。
       摘券**按 campaignId 计费**，一款几十次写操作 ⇒ 为 −0.5 元动手是净亏。
       按「亏损 ≥1 元 **且** 近 15 日 ≥5 单」(可调 min_loss/min_qty) 筛，
       实测只剩 5 款，占总亏损 10%。
    """
    import collections as _c
    ACT = {"券打穿到手价": "摘券 strip_except",
           "国补打穿到手价": "退国补报名 subsidy_withdraw",
           "促销打穿到手价": "退促销 promo_withdraw",
           "券promo打穿": "摘券 + 退促销",
           "券促打穿到手价": "摘券 + 退促销",
           "物流成本过高": "★改物流模板/改包装/下架（摘券治不了）",
           "采购价过高": "★找供应商谈/换供（摘券治不了）",
           # ★2026-08-14 新增两类，都**不该动手**，别混进「值得动手」：
           "新人价补贴未入账(疑似延时·勿动手)":
               "★不动手：补贴未入账的数据延时，非配置问题（实测 2 款实际毛利 +1.79/+1.80）",
           "成本结构(无减免仍亏)": "★无减免仍亏 ⇒ 涨价/换供/改物流，摘券退促都无从下手",
           # ★单品促销锁前台价 ⇒ **涨价无效**，只能退促销（2026-08-14 实证 5 款传导率 0）
           "单品促销打穿(超补/便宜包邮)": "退单品促销 markettool_delete(promos)（★涨价对它无效）",
           "定价(京东价≈采购价)": "★涨价或换供（摘券治不了）"}
    # ★「疑似延时」类**不进值得动手**——它压根不是问题，动了就是对健康品动手。
    SKIP_ROOTS = {"新人价补贴未入账(疑似延时·勿动手)"}
    def _f(r, k):
        try:
            return float(r.get(k) or 0)
        except Exception:
            return 0.0
    by = _c.defaultdict(lambda: {"款数": 0, "预估毛利合计": 0.0})
    for r in rows:
        k = str(r.get("根因") or "(未标注)")
        by[k]["款数"] += 1
        by[k]["预估毛利合计"] += _f(r, "预估毛利")
    cause = {k: {"款数": v["款数"], "预估毛利合计": round(v["预估毛利合计"], 1),
                 "动作": ACT.get(k, "?需人工判断")}
             for k, v in sorted(by.items(), key=lambda t: -t[1]["款数"])}
    worth = [r for r in rows
             if _f(r, "预估毛利") <= -min_loss and _f(r, "近15日单量") >= min_qty
             and str(r.get("根因") or "") not in SKIP_ROOTS]
    worth.sort(key=lambda r: _f(r, "预估毛利"))
    tot = sum(_f(r, "预估毛利") for r in rows) or 1.0
    return {
        "按根因": cause,
        "值得动手": {
            "门槛": "预估亏损 ≥%.1f 元 且 近15日 ≥%d 单" % (min_loss, min_qty),
            "款数": len(worth), "占总亏损%": round(
                sum(_f(r, "预估毛利") for r in worth) / tot * 100),
            "rows": [{"skuId": r.get("skuId"), "预估毛利": _f(r, "预估毛利"),
                      "我担减免": _f(r, "我担减免"), "近15日单量": _f(r, "近15日单量"),
                      "根因": r.get("根因"), "动作": ACT.get(str(r.get("根因")), "?")}
                     for r in worth[:30]],
            "_说明": "★不在这张表里的**别动**：写操作按 campaignId 计费，"
                     "为 −0.5 元的滞销款发几十次写是净亏",
        },
    }
