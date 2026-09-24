"""止亏方案生成器 —— 亏损清单 → 归因 → 过禁令 → 过可执行性 → 按手段分组出计划。

**为什么要有这个**（2026-07-28 全天实证，四类错误各踩一次）：
1. 按数据排出的出血榜**不知道哪些负毛利是有意的** → 会去摘用户已拍板保留的引流款（还是共补券）。
2. 不做**对账**就归因 → 单品促销(promoType==1，如 `便宜包邮`)会以 −1178 元排到真凶第一，
   但那是**重复计数**：它的降价早已含在 `benchPrice` 里（原价−reward=benchPrice），再算一遍就是双扣。
   （注意：它**是真降价、真花钱**，只是不该在减免项里再计一次。）
3. 先出方案后查权限 → 「57 款该退国补」缩水成 15 款（42 款是他人报名，没 applyId）。
4. 按 jxReward 线性加法预测收益 → 40 款里 12 款偏离，1 款**摘完反而更差**（减免会补位）。

本模块把这四道闸门固化到流程里，顺序不可调换：
    对账 → jx实担归因 → 禁令过滤 → 可执行性过滤 → 分组 + 时效标注 + 探针建议
"""
from __future__ import annotations

import re
from typing import Optional

from blacklight.core import pmap, protected
from blacklight.osw import margin

# 三种手段的生效时效完全不同，混在一次回读里判成败一定会误判（2026-07-28 实证）
TIMING = {
    "摘券": "即时。回执 couponResult.ok/fail，回读立刻可见。",
    "退国补": "即时。逐个 POST /apply/quit，回执逐条 success。",
    "退活动报名": "★异步审核。接口 code=00000 只代表**受理**，促销仍在跑、毛利零变化；"
                  "用 client.get_sku_status 看 applyProcessStatus=10 才是已提交，**须隔天复查**。",
}

# ★单品促销 promoType==1（便宜包邮 / 【预告价】 / 超级补贴(含包邮)）的降价**已含在 benchPrice 里**，
# 归因时必须排除，否则重复计一遍。**按 promoType 判，不要按券名判**——
# 2026-07-28 实证：剔 promoType==1 → 592/592 对平；按名字匹配("便宜包邮"/"【预告价】") **漏判 36 款**。
# ⚠️排除 ≠ 不花钱：它是真降价（中位压 17.3%、最狠 47.7%），只是体现在 benchPrice 的下降上。
SINGLE_PROMO_TYPE = 1


def _norm(name) -> str:
    return re.sub(r"\d{8,}", "", re.sub(r"[-_]?\d+$", "", str(name or ""))).strip()


def _applying(items: list, drop_single_promo: bool = True) -> list:
    """留下「真正计入当前到手价」的券/促。促销侧剔 promoType==1 + 新人价幽灵；券侧原样。"""
    out = []
    for c in items:
        if float(c.get("jxReward") or 0) <= 0.005 or c.get("isNewUser"):
            continue
        if drop_single_promo and c.get("type") == SINGLE_PROMO_TYPE:
            continue
        out.append(c)
    return out


def build_plan(max_profit: float = 0.0, limit: int = 300, only_with_orders: bool = False,
               resolve_feasibility: bool = True, workers: int = 6,
               source: str = "triage") -> dict:
    """生成止亏方案。**只读**——不发任何写请求，产出供人确认后再各自走 dry-run+confirm。

    source:
      · `"triage"`（默认，推荐）—— 走 `margin.triage()` 的**双网**，取 **A(真亏) + D(漏检)** 两桶。
        ★为什么必须换：旧的 `list_low_margin` 只是**预估网**，2026-08-10 全量实测漏掉
        **D 桶 272 款 / 15 日失血 −32,625**（是 A 桶的 5.7 倍），旧流程一款都看不见。
      · `"estimate"` —— 退回旧行为（只 `list_low_margin`）。仅在 triage 不可用时临时用。

    only_with_orders：`source="estimate"` 时才生效（triage 的桶定义里已含出单条件）。
    resolve_feasibility=True：并发查国补 applyId / 券 campaignId，把「其实动不了」的提前剔掉。

    ⚠️★**2026-08-11 起「真凶排行」请改用 `pnl.attribute()`**——它走 ge 逐券下钻
      + easybi 承担口径，无盲区且自带流速与恒等式哨兵。本函数的排行保留仅为兼容。
    ⚠️本函数的「真凶排行」建自 **osw 当前券促配置**，有 **~31% 盲区**（osw 看不见后续新圈进来
      的批次）。要拿到实际成交口径的逐券归因，另调 `easybi.coupon.attribute_loss`
      / `blind_spot_rate`（黄金眼 T-1，独立工具，不内置以免拖慢本函数并绑定 easybi 登录态）。
    """
    triage_info = None
    if source == "triage":
        tri = margin.triage(top=0)
        ids_want = set(tri["归因入口"])
        scan = margin.scan_portfolio()["rows"]
        rows = [r for r in scan if str(r["skuId"]) in ids_want][:limit]
        triage_info = {k: {kk: vv for kk, vv in tri[k].items() if kk != "Top"}
                       for k in ("A_真亏", "B_纸面亏", "C_零单", "D_漏检")}
        triage_info["E_禁令"] = {k: v for k, v in (tri["E_禁令"] or {}).items()
                                 if k not in ("over", "ok")}
        low = {"portfolio_total": tri["在售总数"], "loss_count": len(ids_want)}
    else:
        low = margin.list_low_margin(max_profit=max_profit, limit=limit)
        rows = low.get("rows") or []
        if only_with_orders:
            rows = [r for r in rows if (r.get("近15日亏损单") or 0) > 0]
    ids = [str(r["skuId"]) for r in rows]
    if not ids:
        return {"summary": {"亏损款": 0}, "note": "没有符合条件的亏损款"}

    pricing = margin.query_pricing_batch(ids)

    # ① 对账（不通过就把嫌疑项列出来，归因自动排除）
    rec = margin.reconcile(ids, pricing=pricing)

    # ② 按 jx 实担归因，权重 = 失血额（不是数 SKU）
    # ★★权重口径 2026-08-10 修正：原来用 `近15日亏损单 × 预估毛利`，但**预估是最坏情况**
    #   （假设每单吃满券）且有 31% 盲区 ⇒ 会系统性高估、把排行扭曲。
    #   改用 `近15日单量 × 实际单均毛利`（同口径可直接比，见 margin._bleed）；
    #   没有实际值时才退回预估口径，并在输出里标注降级。
    agg, tot_loss, degraded = {}, 0.0, 0
    for r in rows:
        p = pricing.get(str(r["skuId"]))
        if not p:
            continue
        if r.get("实际单均毛利") is None:
            degraded += 1
        loss = margin._bleed(r)
        tot_loss += loss
        items = ([dict(c, _kind="券") for c in _applying(p.get("coupons") or [], drop_single_promo=False)] +
                 [dict(c, _kind=(c.get("cat") or "促销")) for c in _applying(p.get("promotions") or [])])
        for c in items:
            k = f"{c['_kind']}|{_norm(c.get('name'))}"
            a = agg.setdefault(k, {"手段类": c["_kind"], "名称": _norm(c.get("name")), "命中SKU": 0,
                                   "我担合计": 0.0, "关联亏损单": 0, "关联亏损额": 0.0, "共补款数": 0,
                                   "creators": set(), "skus": []})
            a["命中SKU"] += 1
            a["我担合计"] = round(a["我担合计"] + float(c.get("jxReward") or 0), 2)
            a["关联亏损单"] += (r.get("近15日亏损单") or 0)
            a["关联亏损额"] = round(a["关联亏损额"] + loss, 2)
            if float(c.get("reward") or 0) - float(c.get("jxReward") or 0) > 0.005:
                a["共补款数"] += 1
            if c.get("creator"):
                a["creators"].add(c["creator"])
            a["skus"].append(str(r["skuId"]))
    culprits = sorted(agg.values(), key=lambda x: x["关联亏损额"])
    for a in culprits:
        a["creators"] = sorted(a.pop("creators"))
        a["全自担"] = a["共补款数"] == 0

    # ③ 禁令过滤 —— 已拍板保留的结构性决策。止亏方案做的是「摘券/退促」，故 action="strip"
    #    （只拦声明要拦 strip 的规则；像 5.9-5 钩子券那种「存量不许摘但允许新报」的不会误伤报名流程）
    filt = protected.filter_plan(ids, pricing, action="strip")
    blocked = {b["skuId"]: b["hits"] for b in filt["blocked"]}
    actionable = [r for r in rows if str(r["skuId"]) not in blocked]

    # ④ 按手段分组 + 可执行性
    groups = {"摘券": [], "退国补": [], "退活动报名": [], "结构性(券促救不了)": []}
    for r in actionable:
        s = str(r["skuId"])
        p = pricing.get(s) or {}
        base = {"skuId": s, "name": r.get("name"), "现毛利": p.get("jxGrossProfit"),
                "基准价毛利率%": r.get("基准价毛利率%"), "近15日亏损单": r.get("近15日亏损单") or 0,
                "京东价": p.get("benchPrice"), "全成本": p.get("fullCost")}
        if (r.get("基准价毛利率%") or 0) <= 0:
            groups["结构性(券促救不了)"].append(dict(base, 说明="不叠任何券促也亏，只能改价/换供/下架"))
            continue
        for c in _applying(p.get("coupons") or [], drop_single_promo=False):
            joint = float(c.get("reward") or 0) - float(c.get("jxReward") or 0) > 0.005
            groups["摘券"].append(dict(base, 券名=c.get("name"), 我担=c.get("jxReward"),
                                      客减=c.get("reward"), 共补=joint, creator=c.get("creator"),
                                      收益上限=round((p.get("jxGrossProfit") or 0) + float(c.get("jxReward") or 0), 2),
                                      警告="共补券：平台分担成本，摘掉不可逆，勿批量" if joint else None))
        for c in _applying(p.get("promotions") or []):
            cat = c.get("cat") or ""
            tgt = "退国补" if cat == "国补" else ("退活动报名" if cat in ("单品直降", "总价促销") else None)
            if not tgt:
                continue
            groups[tgt].append(dict(base, 促销名=c.get("name"), cat=cat, 我担=c.get("jxReward"),
                                    promoId=c.get("promoId"),
                                    收益上限=round((p.get("jxGrossProfit") or 0) + float(c.get("jxReward") or 0), 2)))

    infeasible = []
    if resolve_feasibility:
        infeasible = _feasibility(groups, workers=workers)

    return {
        "summary": {
            "在售": low.get("portfolio_total"), "亏损款": low.get("loss_count"),
            "取数口径": ("triage 双网（A 真亏 + D 漏检 + E2 超线）" if source == "triage"
                         else "⚠️estimate 单网（只预估<0，会漏掉 D 桶）"),
            "本次纳入": len(rows), "真出过单": sum(1 for r in rows if (r.get("近15日亏损单") or 0) > 0),
            "纸面亏(零单)": sum(1 for r in rows if not (r.get("近15日亏损单") or 0)),
            "近15日失血额": round(tot_loss, 2),
            "失血额口径": "近15日单量 × 实际单均毛利" + (
                "；其中 %d 款无实际值、已降级为预估口径" % degraded if degraded else ""),
            "禁令拦下": len(blocked), "可动": len(actionable),
        },
        "分诊": triage_info,
        "对账": rec,
        "真凶排行": culprits[:20],
        "⚠️真凶排行的覆盖面": "本排行建自 **osw 当前券促配置**，看不见后续新圈进来/已下线的批次。"
                              "2026-08-10 实测盲区 **~31%**（13 款禁令款 14,400 元里 4,407 元在盲区）。"
                              "要实际成交口径的逐券归因跑 `easybi_coupon_attribution`，"
                              "先跑 `easybi_coupon_blind_spot` 看这份排行覆盖了多少。",
        "禁令拦下": [{"skuId": k, "hits": v} for k, v in blocked.items()],
        "分组方案": groups,
        "动不了": infeasible,
        "时效": TIMING,
        "★执行纪律": [
            "1) 收益一律是**上限**，不是预测：摘掉一项后其他减免会补位（别的券顶上/总价促销顶上/"
            "国补按比例基数变大）。实证 40 款里 12 款偏离、1 款摘完更差。",
            "2) 先做**探针**：同类先动 1~2 款 → 回读实测 → 用实测比例外推剩余，再批量。",
            "3) 每批写完必须回读。★回读**两层都要看**：`margin.query_pricing_batch` 看预估有没有转正，"
            "**再用 `easybi_coupon_trend` 看实际券我担成本降没降**——只看预估会误判『止住了』"
            "（补位效应 + 31% 盲区叠加，预估转正而实际照样在花钱）。",
            "4) 摘券/退国补会**抬高到手价**，可能触发生效中百补被平台自动删 → 先跑 bybt.applied_sku_ids 查重叠。",
            "5) 退活动报名是异步审核，当场毛利不变是正常的，别重复提交（会撞「无需重复申请」）。",
        ],
    }


def _feasibility(groups: dict, workers: int = 6) -> list:
    """并发解可执行性：国补要 applyId（他人报名的退不了）、券要 campaignId。动不了的从分组里剔除并返回。"""
    from blacklight.yx import markettool as mt, subsidy as sb

    out = []
    gb = groups.get("退国补") or []
    if gb:
        def _g(x):
            try:
                return (x, sb.find_sku(x["skuId"]))
            except Exception as e:
                return (x, {"_err": str(e)[:100]})
        keep = []
        for x, recs in pmap(_g, gb, workers=workers):
            if isinstance(recs, list) and recs:
                x["applyId"] = recs[0].get("applyId")
                x["blockId"] = recs[0].get("blockId")
                keep.append(x)
            else:
                out.append(dict(x, 原因="国补非本账号报名，无 applyId，退不了 → 只能改价或找报名方"))
        groups["退国补"] = keep

    cp = groups.get("摘券") or []
    if cp:
        want = sorted({x["skuId"] for x in cp})

        def _c(s):
            try:
                return (s, (mt.withdraw_plan(s) or {}).get("券档位") or [])
            except Exception:
                return (s, [])
        tiers = dict(pmap(_c, want, workers=workers))
        keep = []
        for x in cp:
            t = next((t for t in tiers.get(x["skuId"], []) if _norm(t.get("name")) == _norm(x.get("券名"))), None)
            if t and t.get("campaignId"):
                x["campaignId"] = t["campaignId"]
                keep.append(x)
            else:
                out.append(dict(x, 原因="未解析到 campaignId（可能是平台券/不可删）→ 需人工确认"))
        groups["摘券"] = keep
    return out
