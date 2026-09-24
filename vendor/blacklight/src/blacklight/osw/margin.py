"""
jdcore 公共层：osw_margin（京喜**实时毛利监控 + 定价底料**，osw 采销工作台）。

网关 api.m.jd.com（HMAC-MD5 签名），origin osw.jd.com。这是**跨 MCP 共享的毛利参考标准**：
  - list_low_margin：全量列名下亏损/薄毛利 SKU（+根因+实际亏损单）
  - query_pricing / query_pricing_batch：单/批 SKU 定价底料（京东价/券促/到手价/**京喜全口径毛利率** + fixedCost/cpsRate 成本）
  - batch_pricing：到手价/盈亏体检 + 可选建议报名价（本地反解，纯读）

从原 yx_markettool.py 的 A 段切出（2026-07-17，见 [[jd-mcp-architecture]]）。
消费方：osw-mcp（工具面 osw_margin_*）、yx-mcp 的 bybt/ms（批量报名取成本定价）、未来 jzt-mcp（广告判盈亏）。
只依赖 jd_auth / jd_core / osw_pricing，**不反向依赖任何业务 MCP 场域**（券促删除等在 yx_markettool.py 的 B 段）。
"""
from __future__ import annotations

import hashlib
import hmac
import json as _json
import os
import time as _time

from blacklight.core import auth as jd_auth
from blacklight.core import BlacklightError, make_client, gateway

# ---------- 常量（与 jd-tuicu/jd_api.py 一致，勿改） ----------
JD_API_BASE = gateway("markettool") + "/api"
JD_API_SECRET_KEY = "xtl_sqg_mall-^&*-damai_(789)_@#$"
JD_API_COMMON = {
    "appid": "wqadmin", "channel": "jxh5", "clientVersion": "1.2.5",
    "client": "jxh5", "cthr": "1", "loginType": "7",
}

PROMO_TYPE_NAME = {
    1: "单品促销", 26: "单品直降", 19: "单品包邮", 10: "总价促销", 4: "赠品促销",
    6: "套装促销", 18: "平行促销", 5: "附件绑定", 22: "礼金促销", 25: "官方立减",
    2: "平台活动", 11: "平台活动",
}


def _promo_type_name(c):
    return PROMO_TYPE_NAME.get(c, f"类型{c}")


# ---------- 签名（1:1 移植 pageapi.js） ----------
def _serialize_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return _json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v)


def _build_sign_str(body: dict) -> str:
    values = []
    for k in sorted(body.keys()):
        v = body[k]
        if v is None or v == "":   # 0/False 不跳过（与 JS === 一致）
            continue
        values.append(_serialize_value(v))
    msg = "&".join(values)
    return hmac.new(JD_API_SECRET_KEY.encode("utf-8"), msg.encode("utf-8"), hashlib.md5).hexdigest()


def _build_body(body: dict, now_ms: int = None) -> dict:
    if now_ms is None:
        now_ms = int(_time.time() * 1000)
    out = {"env": "prod", **(body or {}), "time": now_ms}
    out["signStr"] = _build_sign_str(out)
    return out


def _build_params(function_id: str, signed: dict) -> dict:
    params = dict(JD_API_COMMON)
    params.update({"functionId": function_id, "t": str(signed["time"]), "uuid": "",
                   "body": _json.dumps(signed, separators=(",", ":"), ensure_ascii=False)})
    return params


# ---------- 客户端 / 请求（Cookie 来自 jd_auth，共用登录态） ----------
def _cookie() -> str:
    return (os.environ.get("JD_COOKIE", "").strip() or jd_auth.get_cookie())


def _client(timeout: float = 20.0):
    ck = _cookie()
    if not ck:
        raise BlacklightError("无登录态：先 yx_login，或设 JD_COOKIE 环境变量")
    # osw.jd.com 来源；content_type 按请求自设（_api 里区分 GET/POST）
    return make_client(ck, origin="https://osw.jd.com", referer="https://osw.jd.com/",
                       content_type=None, timeout=timeout)


def _api(client, function_id: str, body: dict, method: str = "GET") -> dict:
    signed = _build_body(body or {})
    params = _build_params(function_id, signed)
    if method.upper() == "POST":
        r = client.post(JD_API_BASE, data=params,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        r = client.get(JD_API_BASE, params=params, headers={"Content-Type": "text/plain"})
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"code": -1, "msg": "非JSON(可能风控/未登录)", "_raw": r.text[:300]}


def _call(client, function_id: str, body: dict, method: str = "GET"):
    d = _api(client, function_id, body, method)
    if not isinstance(d, dict):
        raise BlacklightError(f"{function_id} 返回异常")
    if d.get("code") not in (0, "0"):
        raise BlacklightError(f"{function_id}: {d.get('msg') or d.get('message') or ('code=' + str(d.get('code')))}")
    return d.get("data")


def _fetch_detail(client, sku_id, bench_price=0, buid=325):
    return _call(client, "jxzy_markettool_queryPreDiscountDetailV2",
                 {"env": "prod", "skuId": int(sku_id), "timeType": 0,
                  "benchPrice": bench_price, "buid": buid, "appCode": ""})


def _home_promos(sku_id, buid=325) -> list:
    """queryPreDiscountHome(strSkuIds=单SKU) 的 promotionList —— 比 Detail 全，**含国补(promoType 5)**。
    注意：Home 的 couponInfoList 只有当前生效券、无 campaignId，不能用于删券（删券仍用 Detail）。"""
    with _client() as client:
        d = _call(client, "jxzy_markettool_queryPreDiscountHome",
                  {"env": "prod", "pageNo": 1, "pageSize": 100, "estimatedProfitChannel": "normal",
                   "timeType": 0, "skuStatus": 1, "strSkuIds": str(sku_id), "buid": buid, "appCode": ""}, "POST")
    items = (d or {}).get("skuPromotionInfoDetails") or []
    return (items[0].get("promotionList") or []) if items else []


# ---------- 只读：单 SKU 定价底料（Home 接口，频道无关） ----------
def _fen2yuan(c) -> float:
    """分→元。**None 视作 0（有意如此，不是疏漏）**：本模块 32 处调用里大量是
    `sum(_fen2yuan(p.get("rewardPrice")) for p in ...)` —— 没有这张券就该贡献 0，返回 None 会让求和炸掉。

    ⚠️代价：**关键金额字段一旦变 null 会静默变成 0 元**（采购价 0 → 毛利虚高、基准价 0 → 毛利率算飞）。
    所以由 `osw/doctor.py::_margin_monitor` 加了 null 哨兵盯着 benchPrice/purchasePrice/jxCostSum，
    而不是在这里改成返回 None（那会连带改坏所有求和）。
    ★区别于 `osw/supplier.py::_fen2yuan`（None→None，单值语义）和 `yx/bybt.py`（根本不是分→元）。"""
    return round((c or 0) / 100.0, 2)


def query_pricing(sku_id: str | int, buid: int = 325) -> dict:
    """从 Home 接口(`queryPreDiscountHome`)取单 SKU 的**定价底料**并重构到手价（金额单位:分→元）。

    这是四频道报名定价的数据源（频道无关、含采购价）。校验实证(SKU 10163347829552)：
      到手价 = benchPrice − couponPriceSum − promotionPriceSum；毛利 = 到手价 − jxCostSum；一分不差。
    返回 promotions 每条带 type/subType/reward(减免额)，供平行式解方程用（固定额 vs 比例由 type/subType 判定）。
    """
    with _client() as client:
        d = _call(client, "jxzy_markettool_queryPreDiscountHome",
                  {"env": "prod", "pageNo": 1, "pageSize": 100, "estimatedProfitChannel": "normal",
                   "timeType": 0, "skuStatus": 1, "strSkuIds": str(sku_id), "buid": buid, "appCode": ""}, "POST")
    items = (d or {}).get("skuPromotionInfoDetails") or []
    if not items:
        raise BlacklightError(f"SKU {sku_id} 无 Home 定价记录（skuPromotionInfoDetails 空）")
    return _parse_pricing_item(items[0])


def query_pricing_batch(sku_ids, buid: int = 325, chunk: int = 100,
                        adv_source: str = "auto") -> dict:
    """**批量取定价底料**（`strSkuIds` 逗号多SKU一次取，实证支持；比逐SKU query_pricing 少 N 倍往返）。

    ★`adv_source`（2026-08-11 新增，默认 `auto`）：osw 的 `advCost` 是
      **「过去 24 小时广告费 ÷ 同期广告订单量」的快照**，分母小会炸（实测 69.17 元/件，
      ge 近 7 日实际仅 16.70）、24h 内无广告订单会**记 0**（高估毛利、可能报进真亏款）。
      `auto` 用 ge 近 7 日实际单均广告替换，样本 <5 单自动退回 osw 并标 `advSource`。
      详见 `osw/adv.py`。传 `osw24h` 可关闭。
    返回 {skuId: 定价dict}（结构同 query_pricing）。缺失/非在售的SKU不在dict里→调用方 fallback 单查。"""
    out, ids = {}, [str(s).strip() for s in dict.fromkeys(sku_ids) if str(s).strip()]
    for i in range(0, len(ids), max(1, chunk)):
        part = ids[i:i + max(1, chunk)]
        with _client() as client:
            d = _call(client, "jxzy_markettool_queryPreDiscountHome",
                      {"env": "prod", "pageNo": 1, "pageSize": len(part) + 10, "estimatedProfitChannel": "normal",
                       "timeType": 0, "skuStatus": 1, "strSkuIds": ",".join(part), "buid": buid, "appCode": ""}, "POST")
        for it in ((d or {}).get("skuPromotionInfoDetails") or []):
            out[str(it.get("skuId"))] = _parse_pricing_item(it)
    # ★广告项默认换成 ge 多日实际（osw 的 advCost 是 24h 快照、分母小会炸也会记 0）。
    #   安全默认：`auto` 能换就换、样本不足自动退回并**标记来源**——
    #   不新增「记得传」的参数（见记忆 fix-the-caller-not-just-the-function）。
    from blacklight.osw import adv as _adv
    return _adv.apply(out, source=adv_source)


def reconcile(sku_ids, pricing: dict | None = None, tol: float = 0.02) -> dict:
    """★**归因前置对账**：校验「逐项 jxReward 求和」是否等于「权威汇总 jxCouponSum/jxPromoSum」。

    **正确口径（2026-07-28 定稿）**：促销侧要**先剔掉 `promoType==1`（单品促销）** 再求和。
    因为**`benchPrice` 本身就是单品促销生效之后的前台价**——便宜包邮 / 【预告价】 / 超级补贴(含包邮)
    都是 type1，它们的降价已经含在基数里，再加进减免就是**重复扣**。
    实证：剔 `promoType==1` → **592/592 精确对平**；而按券名匹配（"便宜包邮"/"【预告价】"）会**漏判 36 款**，
    所以**一律按 promoType 判，别按名字**。券侧无此问题（214/214 天然对平，列出来的券都在生效）。

    ⚠️别把「不进 promotionPriceSum」读成「不花钱」：单品促销是**真降价**（中位压 17.3%、最狠 47.7%），
    只是它体现在 benchPrice 的下降上，而不是体现在减免项里。想看降幅用 `origBenchPrice`/`singlePromoCut`。

    返回 {ok, mismatched:[{skuId, 差额, 嫌疑项}], hint}。`嫌疑项` = 剔掉它就能对平的单项。
    """
    ids = [str(s).strip() for s in dict.fromkeys(sku_ids) if str(s).strip()]
    pr = pricing if pricing is not None else query_pricing_batch(ids)
    bad, missing = [], []
    for s in ids:
        p = pr.get(s)
        if not p:
            missing.append(s)
            continue
        # 促销侧剔 promoType==1（其降价已含在 benchPrice 里）+ 剔新人价幽灵
        _pm = [x for x in (p.get("promotions") or [])
               if not x.get("isNewUser") and x.get("type") != 1]
        for kind, items, auth in (("券", p.get("coupons") or [], p.get("jxCouponSum") or 0),
                                  ("促销", _pm, p.get("jxPromoSum") or 0)):
            enum = round(sum(float(c.get("jxReward") or 0) for c in items), 2)
            diff = round(enum - float(auth), 2)
            if abs(diff) <= tol:
                continue
            # 找「剔掉它就对平」的单项 —— 那就是不计入到手价的嫌疑项
            sus = [c.get("name") for c in items
                   if abs(round(enum - float(c.get("jxReward") or 0) - float(auth), 2)) <= tol]
            bad.append({"skuId": s, "类型": kind, "逐项求和": enum, "权威汇总": round(float(auth), 2),
                        "差额": diff, "嫌疑项": sus or ["(无单项可解释，需人工看)"]})
    return {"ok": not bad, "checked": len(ids), "mismatched": bad, "missing": missing,
            "口径": "促销侧已剔 promoType==1（单品促销，其降价已含在 benchPrice 里）+ 新人价幽灵",
            "hint": ("全部对平：列出来的券促都真在生效，可直接按 jxReward 归因。"
                     "单品促销的降价看 origBenchPrice/singlePromoCut，不在减免项里。" if not bad else
                     "有对不上的：`嫌疑项` 是还没被识别出来的「已含在 benchPrice 里」的促销，"
                     "归因时应排除；但**别据此认为它不花钱**——它是真降价，只体现在 benchPrice 下降上。")}


def _parse_pricing_item(it: dict) -> dict:
    """把 Home 接口**单条 item** 解析成定价底料 dict（query_pricing 单条 + query_pricing_batch 多条共用）。"""
    from blacklight.osw import pricing as _P
    sku_id = it.get("skuId")
    bench = _fen2yuan(it.get("benchPrice"))
    coupon_sum = _fen2yuan(it.get("couponPriceSum"))
    promo_sum = _fen2yuan(it.get("promotionPriceSum"))
    # ★剔新人价幽灵：首单新人价促销(isNewUserPromo)会把 benchPrice 打穿到 0.01 之类(仅新用户享)。
    # 还原正常客户价 = benchPrice + Σ新人价客户减免(rewardPrice)。实证与商品列表 jdPrice 一致。
    _nu = [p for p in (it.get("promotionList") or []) if p.get("isNewUserPromo")]
    new_user_reward = round(sum(_fen2yuan(p.get("rewardPrice")) for p in _nu), 2)
    bench_raw = bench
    bench = round(bench + new_user_reward, 2)                  # 后续 actual/jxActual/毛利全用还原价
    actual = round(bench - coupon_sum - promo_sum, 2)          # 重构到手价(正常客户口径)
    cost_sum = _fen2yuan(it.get("jxCostSum"))
    # ★★benchPrice 是**单品促销(promoType==1)生效之后**的前台价 —— 便宜包邮/【预告价】/超级补贴(含包邮)
    # 都是 type1，它们的减免**已经含在 benchPrice 里**，所以不进 promotionPriceSum/jxPromotionPriceSum
    # （再加就是重复扣）。实证 2026-07-28：`原前台价 = benchPrice + Σtype1.rewardPrice`，
    # 35.9−12.89=23.01、44.99−16.35=28.64 全部吻合；且 benchPrice == oac报名记录 promoPrice
    # == list_eligible.pPrice，502/502 精确相等。
    # ⇒ **报便宜包邮 = 真降价（中位压 17.3%，最狠 47.7%），不是"挂个皮肤"。**
    _sp = [p for p in (it.get("promotionList") or [])
           if p.get("promoType") == 1 and not p.get("isNewUserPromo")]
    single_promo_cut = round(sum(_fen2yuan(p.get("rewardPrice")) for p in _sp), 2)
    orig_bench = round(bench + single_promo_cut, 2)            # 还原：单品促销生效前的原前台价
    # reward=客户口径减免(全额,决定客户到手价)；jxReward=**京喜自营实际承担额**(广告/平台承担的部分京喜不出钱)。
    # ⚠️isNewUser=首单新人价补贴：仅新用户享、不进标准到手价(promotionPriceSum不含)→**幽灵促销**，建模时剔除。
    promotions = [{"promoId": p.get("promoId"), "type": p.get("promoType"), "subType": p.get("subType"),
                   "typeName": _promo_type_name(p.get("promoType")),
                   "cat": _P.home_category(p.get("promoType"), p.get("subType")),  # 规范大类(用于建模/exclude)
                   "name": p.get("promoName"),
                   "reward": _fen2yuan(p.get("rewardPrice")),
                   "jxReward": _fen2yuan(p.get("jxRewardPrice")),   # 京喜承担额
                   "isNewUser": bool(p.get("isNewUserPromo")),      # 幽灵促销标记(不进标准到手价)
                   "canOperate": p.get("canOperate")}
                  for p in (it.get("promotionList") or [])]
    # 券也要枚举（到手价减免常主要来自券，如满X减Y券）。couponType 区分 折扣/定额/满减封顶。
    # ⚠️广告智能券(creator DSP_AD)常 finalJxActualRatio=0/jxReward=0：客户享但**京喜不承担**。
    coupons = [{"couponId": c.get("couponId"), "couponType": c.get("couponType"),
                "name": c.get("couponName"), "reward": _fen2yuan(c.get("rewardPrice")),
                "jxReward": _fen2yuan(c.get("jxRewardPrice")),      # 京喜承担额
                "jxRatio": c.get("finalJxActualRatio"), "creator": c.get("creator"),
                "canOperate": c.get("canOperate")}
               for c in (it.get("couponInfoList") or [])]
    # 对账：权威减免总额 = couponPriceSum + promotionPriceSum。枚举项之和可能≠(如超补type1不计入)，
    # 差额记为 residual，供本地模型校准(保证重构当前到手价精确)。**幽灵促销(新人价)不计入**(不在标准到手价里)。
    real_promos = [p for p in promotions if not p["isNewUser"]]
    enum_sum = round(sum(c["reward"] for c in coupons) + sum(p["reward"] for p in real_promos), 2)
    authoritative = round(coupon_sum + promo_sum, 2)
    residual = round(authoritative - enum_sum, 2)
    jx_enum = round(sum(c["jxReward"] for c in coupons) + sum(p["jxReward"] for p in real_promos), 2)
    cps_cost = _fen2yuan(it.get("cpsCost"))
    adv_cost = _fen2yuan(it.get("jxAdvCost"))
    cps_rate = round((it.get("cpsRate") or 0) / 10000.0, 6)   # cpsRate 50 → 0.005(0.5%)，按到手价比例走
    # 全口径成本 = 采购+物流(固定) + CPS(比例×到手价) + 广告(固定预估)。fixedCost 是与到手价无关的部分。
    fixed_cost = round(cost_sum + adv_cost, 2)
    full_cost = round(fixed_cost + cps_cost, 2)
    full_margin = round((actual - full_cost) / actual, 4) if actual else None
    # ★京喜承担口径(权威盈亏)：京喜到手价 = 基价 − jx承担的券/促；广告/平台承担部分京喜不出→不计亏。
    jx_coupon_sum = _fen2yuan(it.get("jxCouponPriceSum"))
    jx_promo_sum = _fen2yuan(it.get("jxPromotionPriceSum"))
    jx_actual = round(bench - jx_coupon_sum - jx_promo_sum, 2)   # 京喜到手价(实证=平台jx口径)
    jx_gross_profit = round(jx_actual - cost_sum, 2)             # 京喜毛利(不含CPS/广告)=平台jxEstimatedGrossProfitPrice
    jx_full_profit = round(jx_actual - full_cost, 2)             # 京喜毛利(全口径,含CPS+广告)
    jx_margin = round(jx_full_profit / jx_actual, 4) if jx_actual else None
    return {
        "skuId": str(sku_id), "skuName": it.get("skuName"),
        "benchPrice": bench,                                   # 基价(前台红字价，**已含单品促销降价**，已还原新人价幽灵)
        "benchPriceRaw": bench_raw,                            # 监控原始基价(可能被新人价打穿,如0.01)
        "origBenchPrice": orig_bench,                          # ★单品促销(便宜包邮/预告价/超补)生效**前**的原前台价
        "singlePromoCut": single_promo_cut,                    # ★被单品促销压掉的金额(=origBench−bench)
        "singlePromoCutPct": (round(single_promo_cut / orig_bench * 100, 1) if orig_bench else None),
        "newUserReward": new_user_reward,                      # 新人价客户减免(仅新用户享)
        "isNewUserPhantom": new_user_reward > 0,               # 该SKU挂了新人价幽灵促销
        "purchasePrice": _fen2yuan(it.get("purchasePrice")),   # 采购价(可控成本)
        "shippingCost": _fen2yuan(it.get("shippingCost")),     # 物流
        "costSum": cost_sum,                                   # 采购+物流(jxCostSum)
        "cpsCost": cps_cost, "cpsRate": cps_rate, "advCost": adv_cost,  # CPS佣金(比例)/CPS率/预估广告费(固定)
        "fixedCost": fixed_cost, "fullCost": full_cost,        # fixedCost=采购+物流+广告；fullCost=+CPS
        "couponSum": coupon_sum, "promoSum": promo_sum,        # 券/促 减免合计(客户口径)
        "actualPrice": actual,                                 # 客户到手价(基价−全额券促，买家实付)
        "jxCouponSum": jx_coupon_sum, "jxPromoSum": jx_promo_sum,   # 京喜实际承担的券/促
        "jxActualPrice": jx_actual,                            # ★京喜到手价(基价−京喜承担券促，盈亏基准价)
        "jxGrossProfit": jx_gross_profit,                      # 京喜毛利(不含CPS/广告)=平台jxEstimatedGrossProfitPrice
        "jxFullProfit": jx_full_profit,                        # ★京喜毛利(全口径:采购+物流+CPS+广告)
        # ⚠️★★**`jxActualPrice − fullCost`（即本行的 jxFullProfit）不是"这个 SKU 亏不亏"的判据**。
        #   因为 fullCost 含 advCost，而 advCost 与实际单件广告费只有 r=0.392 的弱相关（见下）。
        #   2026-08-10 实证：`10191500638976` 用它算出 **−9.11**，而平台预估 **−0.08**、
        #   实际单均 **+1.01**（该款 advCost=8.19/件，比前台价 8.14 还高）——据此下的"仍在亏"
        #   结论整批是错的（22 款里 16 款实际为正）。
        #   ⇒ **判盈亏一律走 `is_losing()`**，它只认平台的 预估毛利 / 实际单均毛利。
        #     本字段的正确用途：看**成本结构**（谁占大头），不是判正负。
        # ★★`jxMargin` **已经扣过广告费**（fullCost 含 advCost）。接广告数据判盈亏时
        #   **绝不能再减一次广告花费**：`GMV×jxMargin − 折后消耗` 是双扣。
        #   2026-08-07 实证代价：59 条在投计划里 30 条被双扣算成亏损（真实只有 4 条），
        #   3 条被点名「卖越多亏越多」的计划实际全部盈利。
        #   正确写法：广告后利润 = GMV × jxMargin；要广告前用 jxGrossProfit 或 (jxActual−costSum−cpsCost)。
        #
        # ⚠️⚠️**`advCost` 不能拿来判广告效率**（2026-08-07 查清，此前我在这里写过
        #   「预估/实际=0.78、jxMargin 偏乐观 22%」——**那条是错的，已删**）。
        #   它是一个**独立的每件预估**，不是实际消耗的换算：239 款样本里与实际单件广告费
        #   （京准通折前消耗/成交销量）**相关系数只有 r=0.392**，离散度 P25 0.326 / P75 1.137。
        #   （若是"实际×系数"，r 应接近 1。逐款中位比值 0.707 看着像 7 折，是巧合——
        #     这么宽的分布里中位落在哪儿都没有含义，别据此下结论。）
        #   ⇒ **判广告投后盈亏只用京准通/easybi 通用集的 7 折口径**
        #     （`easybi` 1025136 的 `94272 预估投后履约毛利`，由实际消耗算出）。
        #     `jxMargin` 适合判**商品自身的定价/券促健康度**，广告那一项只当旁证。
        #   相关口径见 easybi/dataset.py 顶部「成交 vs 出库计费不可比」「折前/折后(7折)」两节。
        "jxMargin": jx_margin,                                 # ★京喜全口径毛利率 = jxFullProfit/京喜到手价
        "grossProfit": _fen2yuan(it.get("estimatedGrossProfitPrice")),        # 客户口径毛利(全额减，含广告承担部分)
        "grossMarginRate": round((it.get("estimatedGrossMarginRate") or 0) / 10000.0, 4),
        "fullMargin": full_margin,                             # 客户口径全成本毛利率(会高估亏损)
        "promotions": promotions,                              # 促销(promotionList，含 reward/jxReward)
        "coupons": coupons,                                    # 券(couponInfoList，含 reward/jxReward/creator)
        "residual": residual, "jxResidual": round(jx_promo_sum + jx_coupon_sum - jx_enum, 2),
        "couponInfoList": it.get("couponInfoList") or [],
    }


# ---------- 京喜实时毛利监控：全量列出名下亏损款（queryPreDiscountHome 分页，无需 strSkuIds） ----------
# 实证 2026-07-13：该接口不传 strSkuIds + 必填(timeType/skuStatus/buid) 即返回**全量分页**，
# **默认按预估毛利升序（最亏在前）** → 从前翻页、毛利转正即停。毛利率字段 raw÷10000=百分数。
def _new_user(it: dict):
    """新人价幽灵检测：返回 (客户减免额, 京喜补贴额)。benchPrice 被首单新人价打穿时用于还原正常客户价。"""
    reward = round(sum(_fen2yuan(p.get("rewardPrice"))
                       for p in (it.get("promotionList") or []) if p.get("isNewUserPromo")), 2)
    allow = _fen2yuan(it.get("jxNewUserAllowanceSum"))
    return reward, allow


def _low_margin_row(it: dict) -> dict:
    bench_raw = _fen2yuan(it.get("benchPrice"))
    nu_reward, nu_allow = _new_user(it)
    bench = round(bench_raw + nu_reward, 2)                       # 还原正常客户价(剔新人价幽灵)
    coupon_cust = _fen2yuan(it.get("couponPriceSum"))       # 客户侧券减免（含广告/平台出的钱，**不是我担**）
    promo_cust = _fen2yuan(it.get("promotionPriceSum"))     # 客户侧促销减免
    # ★2026-07-28 修：归因一律用**京喜实担**(jx*)，不是客户侧。实证某 SKU 客减券 20.02，
    # 京喜实担仅 4.5——差额 15.52 是「广告智能优惠券」，广告侧出钱、不进京喜毛利。
    # 用客户侧归因会把不花我钱的券当成真凶，然后去摘它（见 [[margin-attribution-pitfalls]]）。
    coupon = _fen2yuan(it.get("jxCouponPriceSum"))
    promo = _fen2yuan(it.get("jxPromotionPriceSum"))
    purchase = _fen2yuan(it.get("purchasePrice"))
    ship = _fen2yuan(it.get("shippingCost"))
    profit = _fen2yuan(it.get("jxEstimatedGrossProfitPrice"))     # 京喜预估毛利(监控原值,含新人价成本)
    profit_real = round(profit + nu_allow, 2)                     # 剔新人价补贴后的京喜毛利(正常订单口径)
    rate = it.get("jxEstimatedGrossMarginRate")
    rate_pct = round(rate / 10000, 2) if isinstance(rate, (int, float)) else None   # raw÷10000=%
    actual = round(bench - coupon_cust - promo_cust, 2)           # 客户到手价(买家实付)
    jx_actual = round(bench - coupon - promo, 2)                  # ★京喜到手价(盈亏基准，只减我担的部分)
    bench_profit = round(bench - purchase - ship, 2)              # 无券促裸毛利(京东价-采购-物流)
    # ★减免来源拆分：国补(promoType5) / 券 / 其他促销 —— 三者机制不同、修法不同，必须分开，
    # 否则「国补打穿」会被误报成「券促打穿」→ 去摘券而放过真凶（2026-07-27 实证踩坑）。
    # 金额一律取 jxRewardPrice(我担)，不是 rewardPrice(客户侧)。
    subsidy_amt = round(sum(_fen2yuan(p.get("jxRewardPrice"))
                            for p in (it.get("promotionList") or [])
                            if p.get("promoType") == 5 and not p.get("isNewUserPromo")), 2)
    promo_other = round(promo - subsidy_amt, 2)
    # 共补信号：客户侧 > 我担 ⇒ 平台/广告分担了一部分。**共补券禁止批量摘**（摘=白丢补贴且不可逆）。
    joint_coupon = round(coupon_cust - coupon, 2)
    joint_promo = round(promo_cust - promo, 2)
    # ★★叠加规则见 `docs/yx/NOTES_stacking.md`（官方 51×51 矩阵）。要点：
    #   · **同类券互斥**（限品类/全品类/商品/店铺/无门槛…两两 ×），实证 592 款里 0 款有两张「我担>0」的券；
    #     **但券 × 平台神券 / 广告智能补贴券 = √**（跨类可叠加）→ couponInfoList 里多张券求和是跨类，不是同类。
    #   · **券 × 总价促销(跨店满减) = ×**；券 × 单品促销/单品直降/国补/礼金/PLUS = √。
    #   · 单品促销之间全互斥只生效 1 个（优先级 百亿补贴>减钱大的>免邮促销>…）。
    # ⇒ `jxCouponSum`/`jxPromoSum` 是平台**按规则择优后**的结果，可直接信。
    # ⇒ 预测「加/减一项」用：Δ毛利 = 当前同类项我担 − 新项我担；新项在同类里输了 ⇒ **Δ=0**。
    #   别按「现毛利 − 新项我担」估算（把一切当可叠加会高估损失；实证 538 款里 179 款 Δ=0、15 款反而变好）。
    # 基准价毛利率：无任何券促时的毛利率。**报名门槛线判断用这个**
    # （国补15%+直降5% 需 ≥23.7%；叠9折券需 ≥31.3%，见 [[guobu-zhijiang-stack-rule]]）。
    bench_margin_pct = round(bench_profit / bench * 100, 2) if bench else None
    # 物流占京东价的比重。★**判物流是不是真凶不能只看「裸毛利≤0」**（2026-08-14 实证）：
    #   实测 4 款用户点名「物流才是元凶」的，裸毛利全都是**刚好为正**（0.90/1.11/5.61/5.80），
    #   于是全被归给了减免（券打穿/国补打穿/促销打穿），指向摘券——而摘券对它们毫无用处。
    #   物流占京东价 21%~42%，对照组（新人价款 14~17%、跨店满减款 7~9%）分得干干净净。
    ship_pct = round(ship / bench * 100, 2) if bench else None
    # ★单品促销(promoType==1：便宜包邮/超级补贴含包邮/【预告价】)的减免**已含在 benchPrice 里**，
    #   不进 promotionPriceSum ⇒ 它在减免拆分里恒为 0，**归因会整个漏掉它**。
    #   2026-08-14 实证：10177026585047 超补含包邮压价 **3.27** > 我担券 2.00，
    #   根因却判成「券打穿」；而那张券是共补的、根本摘不得，真正该退的是超补。
    #   ⇒ 必须纳入比较。注意它压的是前台价（origBench → bench），量纲与其他减免一致可直接比。
    _sp_cut_early = round(sum(_fen2yuan(p.get("rewardPrice"))
                              for p in (it.get("promotionList") or [])
                              if p.get("promoType") == 1 and not p.get("isNewUserPromo")), 2)
    _cuts = {"国补": subsidy_amt, "券": coupon, "促销": promo_other,
             "单品促销(超补/包邮)": _sp_cut_early}
    _top = max(_cuts, key=lambda k: _cuts[k])
    _cut_total = round(coupon + promo + _sp_cut_early, 2)
    # ★「剔掉新人价后的正常订单毛利」= 裸毛利 − 我担减免。新人价只对首单用户生效，
    #   拿它判「这款在正常订单上到底赚不赚」。实测与 query_pricing 的 jxGrossProfit 逐款相等。
    ex_nu_profit = round(bench_profit - _cut_total, 2)
    # 根因细分：京东价≤采购=定价问题；再减物流<0=物流过高；裸毛利>0但叠减免后<0=按最大减免项归因；否则薄毛利
    if round(bench - purchase, 2) <= 0:
        root = "定价(京东价≈采购价)"
    elif bench_profit <= 0:
        root = "物流成本过高"
    elif profit_real < 0 and nu_reward > 0 and nu_allow <= 0.005 and ex_nu_profit > 0:
        # ★**新人价补贴未入账 ⇒ 假亏损，别动手**（2026-08-14 修）。
        #   旧代码在减免全 0 时兜底成「券促打穿到手价」，把人直接指向摘券。
        #   实证 10165647591102 / 10137608705386 连着两天占据「值得动手」榜首（−6.30/252单、
        #   −6.21/190单），照着摘券就是对两款**健康品**动手。
        #   机制：新人价促销 jxReward 全额自担(9.6/8.0)，而 jxNewUserAllowanceSum=0（补贴未入账）
        #   ⇒ profit_real = profit + 0，补不回来。这是**数据延时**，不是配置问题。
        #   判据不能只看「减免==0」——实测 10165647591102 我担减免 1.90 仍属此类。
        #   用 `ex_nu_profit`(裸毛利 − 我担减免) 才准：它 >0 说明正常订单是赚的，亏损全来自新人价。
        #   ★该值与 query_pricing 的 jxGrossProfit **逐款相等**（实测 +1.80 / +1.79 两处对上），
        #     是两个独立接口的交叉验证。
        root = "新人价补贴未入账(疑似延时·勿动手)"
    elif profit_real < 0 and _cut_total <= 0.005:
        root = "成本结构(无减免仍亏)"
    elif profit_real < 0:
        # ★减免与物流谁是大头要**比一比**，别默认归给减免：物流比最大单项减免还大、
        #   且占京东价 ≥20% 时，摘券/退促最多治标（实证 10230451021333 物流 13.00 > 促销 11.21）。
        if ship > _cuts[_top] and (ship_pct or 0) >= 20:
            root = "物流成本过高"
        elif _cuts[_top] <= 0:
            root = "券促打穿到手价"
        elif _top == "单品促销(超补/包邮)":
            # 单品促销独立成类：**它锁前台价 ⇒ 涨价无效**，只能退促销（见 product._reprice_transmit）
            root = "单品促销打穿(超补/便宜包邮)"
        else:
            root = f"{_top}打穿到手价"
    else:
        root = "薄毛利"
    # ★单品促销(promoType==1：便宜包邮/【预告价】/超级补贴含包邮)的减免**已含在 benchPrice 里**，
    # 所以不进 promotionPriceSum。还原原前台价看真实降幅（实证 592/592 对平，见 _parse_pricing_item）。
    _sp_cut = round(sum(_fen2yuan(p.get("rewardPrice"))
                        for p in (it.get("promotionList") or [])
                        if p.get("promoType") == 1 and not p.get("isNewUserPromo")), 2)
    row = {"skuId": str(it.get("skuId")), "name": (it.get("skuName") or "")[:24],
           "预估毛利": profit_real, "毛利率%": rate_pct, "京东价": bench, "采购价": purchase,
           # 实际单均毛利(近15日总利润/总单量)——**跟"预估毛利"同口径可直接比**。
           # ⚠️别拿 day15AvgLossPerOrder(只统计亏损单)去跟预估比，那会算出假的"系统性低估"(2026-07-28 踩过)。
           "实际单均毛利": _fen2yuan(it.get("perOrderAvgGrossProfit")),
           "近15日单量": it.get("day15TotalOrderNum"),
           "物流": ship, "物流占京东价%": ship_pct,
           "我担减免": round(coupon + promo, 2),                   # ★归因/摘券按这个（京喜实担）
           "客户侧减免": round(coupon_cust + promo_cust, 2),       # 买家看到的总减免（含广告/平台出的钱）
           "京喜到手价": jx_actual, "到手价": actual,
           "裸毛利": bench_profit, "基准价毛利率%": bench_margin_pct, "根因": root,
           "剔新人价后毛利": ex_nu_profit,   # =裸毛利−我担减免，与 query_pricing 的 jxGrossProfit 对齐
           "减免拆分": {"国补": subsidy_amt, "券": coupon, "其他促销": promo_other},
           "昨日亏损单": it.get("yesterdayLossOrderNum"), "近15日亏损单": it.get("day15LossOrderNum")}
    if joint_coupon > 0.005 or joint_promo > 0.005:               # 有共补 → 显式提示别乱摘
        row["共补金额"] = {"券": joint_coupon, "促销": joint_promo}
        row["共补提示"] = "客户侧>我担：平台/广告分担了一部分，摘之前先确认（共补券摘掉不可逆）"
    if _sp_cut > 0.005:                                           # 该 SKU 的前台价已被单品促销压过
        row["原前台价"] = round(bench + _sp_cut, 2)
        row["单品促销压价"] = _sp_cut
        row["压价幅度%"] = round(_sp_cut / (bench + _sp_cut) * 100, 1)
        row["压价说明"] = "「京东价」是便宜包邮/预告价/超补生效**后**的前台价，降价已在基数里（非隐藏成本）"
    if nu_reward > 0:                                             # 透出新人价信息(该SKU挂了新人价幽灵)
        row["新人价补贴"] = nu_allow
        row["监控原毛利"] = profit                                # 未剔新人价的监控原值
        # ⚠️★对新人价款，**「预估毛利」不可信、「监控原毛利」才接近实际**（2026-08-10 实测 26 款）：
        #   预估(剔后) − 实际单均 中位 **+6.24**；监控原毛利(未剔) − 实际单均 中位 **+0.36**。
        #   典型 10110837571597：预估 5.71 > 裸毛利 3.6（无减免的单都赚不到这么多，一眼假），
        #   监控原毛利 −0.79，实际 −0.41。判亏损请用 `实际单均毛利`，别用本行的「预估毛利」。
        row["预估毛利_警告"] = ("新人价款：剔幻影后的预估毛利实测高估约 6 元（中位），"
                               "判亏损用「实际单均毛利」或「监控原毛利」，别用「预估毛利」")
    return row


def list_low_margin(max_profit: float = 0.0, limit: int = 200, page_size: int = 100,
                    channel: str = "normal", sku_status: int = 1, buid: int = 325) -> dict:
    """[京喜实时毛利监控] **列出名下预估毛利 < max_profit(元) 的 SKU**（默认 0=亏损款），带根因+实际亏损单。
    走 queryPreDiscountHome 全量分页（默认按预估毛利升序，最亏在前）；毛利转正即停，无需扫全量。
    sku_status: 1在售。limit=最多返回条数。

    ⚠️★**这只是双网里的「预估网」，单独用会漏掉一整类真亏**。
      预估毛利是**当前券促配置下的最坏情况**，它有两个方向相反的偏差：
        · 偏悲观：假设每单都吃满最大券，实际多数订单吃不到；
        · 有盲区：只看得见**当前**配置，后续新圈进来的券促它看不到。
      2026-08-10 全量实测（8,284 款在售）：
        预估<0 且 实际<0  ..... 32 款
        预估<0 但 实际≥0  .... 169 款（纸面亏，多数不该动）
        **预估≥0 但 实际<0 ... 189 款，15 日实际失血 −30,485 元 —— 本函数一个都看不见**
      日常巡检请走 **`triage()`**（双网并行 + 五桶分诊），别直接用本函数下结论。"""
    rows, total, page = [], None, 1
    phantom_excluded = 0
    with _client() as c:
        while len(rows) < limit and page <= 60:
            body = {"env": "prod", "pageNo": page, "pageSize": page_size,
                    "estimatedProfitChannel": channel, "timeType": 0, "skuStatus": sku_status,
                    "strSkuIds": "", "buid": buid, "appCode": ""}
            d = _call(c, "jxzy_markettool_queryPreDiscountHome", body, "POST") or {}
            total = d.get("totalCount")
            items = d.get("skuPromotionInfoDetails") or []
            if not items:
                break
            stop = False
            for it in items:
                prof = _fen2yuan(it.get("jxEstimatedGrossProfitPrice"))   # 监控原值(升序)
                if prof >= max_profit:
                    stop = True
                    break                                         # 已升序，监控原毛利转正 → 后面都不亏
                nu_reward, nu_allow = _new_user(it)
                # ★剔新人价幽灵：剔掉新人价补贴后不亏(profit+补贴≥阈值)→非真亏，跳过不计入
                # ⚠️★**但实际在亏的绝不能剔**（2026-08-10 实测修）：加回 `jxNewUserAllowanceSum`
                #   会**严重高估**。26 款有成交的新人价款上：
                #     预估毛利(剔后) − 实际单均 中位 **+6.24 元**；监控原毛利(未剔) − 实际单均 中位 **+0.36 元**
                #   —— 未剔的原值几乎就是实际，剔完反而离谱。据此排除掉了 6 款**真在亏**的
                #   （合计 15 日失血 −5,759，含 10186130760890 −3,222）。
                #   ⇒ 幻影逻辑只用来压"首单 0.01 造成的虚亏告警"，**不能盖过已发生的实际亏损**。
                real_per = _fen2yuan(it.get("perOrderAvgGrossProfit"))
                really_losing = (it.get("day15TotalOrderNum") or 0) > 0 and real_per < 0
                if ((nu_reward > 0 or nu_allow > 0) and round(prof + nu_allow, 2) >= max_profit
                        and not really_losing):
                    phantom_excluded += 1
                    continue
                rows.append(_low_margin_row(it))
                if len(rows) >= limit:
                    break
            if stop or len(items) < page_size:
                break
            page += 1
    by_root = {}
    for r in rows:
        by_root[r["根因"]] = by_root.get(r["根因"], 0) + 1
    return {"portfolio_total": total, "loss_count": len(rows), "threshold_yuan": max_profit,
            "by_root": by_root, "newuser_phantom_excluded": phantom_excluded,
            "note": ("已剔除新人价幽灵(首单0.01等虚亏)" + (f"共{phantom_excluded}款" if phantom_excluded else "")),
            "rows": rows}


def scan_portfolio(sku_status: int = 1, page_size: int = 100, channel: str = "normal",
                   buid: int = 325, max_pages: int = 200) -> dict:
    """**实际网的取数底座**：扫全量在售，不按毛利截断。

    `list_low_margin` 按预估毛利升序、转正即停 —— 那是**预估网**。要发现
    「预估≥0 但实际在亏」的款（2026-08-10 实测 189 款 / −30,485 元），只能扫全量。

    ⚠️★**截断必须抛错，不能静默少算**。旧的 `list_low_margin` 有 `page <= 60` 硬上限，
      page_size=100 ⇒ 最多 6,000 行，而在售 8,284 —— 今天的全量扫就被静默切掉 2,284 款，
      返回结构完全正常、毫无迹象。本函数校验 `len(rows) == totalCount`，不足直接抛。
      （与 `easybi/coupon.py::_rows` 的截断闸同一原则。）
    """
    rows, total, page = [], None, 1
    with _client() as c:
        while page <= max_pages:
            body = {"env": "prod", "pageNo": page, "pageSize": page_size,
                    "estimatedProfitChannel": channel, "timeType": 0, "skuStatus": sku_status,
                    "strSkuIds": "", "buid": buid, "appCode": ""}
            d = _call(c, "jxzy_markettool_queryPreDiscountHome", body, "POST") or {}
            if total is None:
                total = d.get("totalCount")
            items = d.get("skuPromotionInfoDetails") or []
            if not items:
                break
            rows.extend(_low_margin_row(it) for it in items)
            if len(items) < page_size:
                break
            page += 1
    if total is not None and len(rows) < int(total or 0):
        raise BlacklightError(
            "全量扫描被截断：取回 %d 行但在售 %s 款（少 %d）。"
            "调大 max_pages（当前 %d，每页 %d）；**截断是静默少算，不能当成扫完了**。"
            % (len(rows), total, int(total) - len(rows), max_pages, page_size))
    return {"portfolio_total": total, "scanned": len(rows), "rows": rows}


def is_losing(row: dict, by: str = "any") -> bool:
    """**预估口径**的盈亏判据（osw 侧）。只认平台字段，不做本地减法。

    ⚠️★**2026-08-11 起不再是「唯一入口」**（用户拍板）：判「已发生 / 正在发生的亏」
      改用 **`ge.margin.judge_losing()`**（实时+离线、毛利桥逐项闭合、已扣广告）。
      本函数定位改为**预测 / 前瞻网**——osw 扫全量在售（**含零单款**），
      能拦「配置已亏但还没出单」的，这是 ge 做不到的（ge 只看得见当日有成交的，
      实测当日 915 行 vs osw 在售 8,284 款）。
      两者**取并集不取交集**——回答的是不同问题。详见 `blacklight/pnl/scan.py`。

    by: 'estimate' 只看预估 / 'actual' 只看实际单均 / 'any' 任一为负（默认，双网口径）。

    ⚠️★**永远不要用 `jxActualPrice − fullCost` 判盈亏**。`fullCost` 含 `advCost`，
      而 advCost 是一个**独立的每件预估**、与实际单件广告费只有 r=0.392 的弱相关
      （239 款样本，P25 0.326 / P75 1.137）。2026-08-10 实测：`10191500638976`
      平台预估 **−0.08**、实际单均 **+1.01**，用 `jxActualPrice − fullCost` 却算出 **−9.11**
      （该款 advCost=8.19/件，比前台价 8.14 还高）。
      判负一律用 `预估毛利`(jxEstimatedGrossProfitPrice) 与 `实际单均毛利`(perOrderAvgGrossProfit)
      —— 这两个同口径、可直接比。
    """
    est = row.get("预估毛利")
    act = row.get("实际单均毛利")
    if by == "estimate":
        return est is not None and est < 0
    if by == "actual":
        return act is not None and act < 0
    return (est is not None and est < 0) or (act is not None and act < 0)


def _bleed(r: dict) -> float:
    """15 日实际失血额（负数）。有实际单均就用实际，没有才退回预估。"""
    q = r.get("近15日单量") or 0
    per = r.get("实际单均毛利")
    if per is None:
        per = r.get("预估毛利") or 0
        q = r.get("近15日亏损单") or 0
    return round(per * q, 2) if per < 0 else 0.0


def triage(rows: list = None, protected_line: float = 1.0, top: int = 15,
           with_protected: bool = True) -> dict:
    """
    ⚠️★**2026-08-11 起不再是止亏入口**：巡检走 `blacklight.pnl.scan.margin_scan()`。
    本函数保留为**前瞻网底料**（全量在售、含零单，能拦「配置已亏但还没出单」）。
    ⚠️它的「15 日失血」是**向后看的滚动累计**——排序前必须过流速
    （`pnl.runrate`），否则已退潮的会赖在榜首。
★**日常巡检入口**：双网发现 + 五桶分诊。

    `rows` 省略时自己调 `scan_portfolio()` 扫全量在售。

    **为什么要两张网**（失效模式正交，不能二选一）：
      · 预估网（`预估毛利<0`）：**前瞻**——还没出单就能拦住新配的券促；但过度悲观 + 31% 盲区。
      · 实际网（`实际单均毛利<0`）：**兜底**——覆盖 osw 看不见的批次；但 15 日滚动均值**滞后**，
        新挂上去的亏损券要几天才反映出来。

    五个桶（2026-08-10 **全量 8,284 款**实测基线，数字对不上说明口径漂了）：

    | 桶 | 判据 | 实测 | 15日失血 | 处置 |
    |---|---|---|---|---|
    | A 真亏     | 预估<0 且 实际<0 且 亏损单>0 | 32  | −5,759  | 进归因 |
    | B 纸面亏   | 预估<0 但 实际≥0             | 130 | 0       | **不动**；`昨日亏损单>0` 标"正在恶化" |
    | C 零单     | 近15日单量=0                 | 40  | —       | 不动 |
    | D 漏检     | **预估≥0 但 实际<0**         | 272 | **−32,625** | **进归因（最大失血源）** |
    | E 禁令     | 命中 protected（A+D 内）     | 31  | 超线20/−6,894 | 按 1 元线分 E1 线内 / E2 超线 |

    ⚠️**D 桶失血是 A 桶的 5.7 倍**，而旧流程（只用 `list_low_margin`）一款都看不见。
    ⚠️基线本身曾被截断扭曲：先前用 6,000 行（`list_low_margin` 的 60 页上限）算出 D=189/−30,485，
      扫满 8,284 后是 272/−32,625。**所以 `scan_portfolio` 的截断闸不是洁癖，是基线的前提。**

    ⚠️B 桶不能简单丢：`昨日亏损单>0` 说明**正在恶化而 15 日均值还没反应过来**，
      那是实际网的滞后盲点，必须靠这个信号补。
    ⚠️E 桶与 A/D 桶**会重叠**（禁令款本身也是亏的）；E 是"处置权限"维度不是"亏不亏"维度，
      所以单独成桶而不是从 A/D 里扣掉。归因入口取 **A + D + E2**。

    ⚠️★★**本函数的 `_失血` 排序是向后看的，不能直接当处置顺序**。
      `实际单均毛利` 是 15 日滚动均值 ⇒ 一次**已经结束的脉冲**会在榜首赖上半个月。
      2026-08-10 实证：按失血排出的 D 桶 Top3 全是 7/29–8/03 的一次脉冲，
      8/04 起已自行塌掉 91%，osw 上那张券也早不在商品上了——**据此动手会全打空**。
      ⇒ 排处置顺序**必须再过一道 `easybi.coupon.run_rate()`**（近 3 日日均 vs 前 12 日基线），
        取「爆发」和「持续」，把「消退」放回观察。实测重排后 Top 完全换人。
    """
    if rows is None:
        rows = scan_portfolio()["rows"]

    A, B, C, D = [], [], [], []
    for r in rows:
        est, act = r.get("预估毛利"), r.get("实际单均毛利")
        q = r.get("近15日单量") or 0
        r = dict(r, _失血=_bleed(r))
        if not q:
            if est is not None and est < 0:
                C.append(r)
            continue
        if est is not None and est < 0:
            if act is not None and act < 0:
                A.append(r)
            else:
                r["_恶化信号"] = "昨日已有亏损单，15日均值可能还没反应过来" \
                    if (r.get("昨日亏损单") or 0) > 0 else None
                B.append(r)
        elif act is not None and act < 0:
            D.append(r)

    for g in (A, B, C, D):
        g.sort(key=lambda x: x["_失血"])

    E = {}
    if with_protected:
        from blacklight.core import protected as _prot
        cand = A + D
        if cand:
            E = _prot.health_check(cand, line=protected_line)

    def _pack(g, name):
        return {"桶": name, "款数": len(g), "15日失血": round(sum(x["_失血"] for x in g), 0),
                "Top": [{k: x.get(k) for k in
                         ("skuId", "name", "预估毛利", "实际单均毛利", "近15日单量",
                          "近15日亏损单", "根因", "_失血", "_恶化信号")} for x in g[:top]]}

    worsening = [x for x in B if x.get("_恶化信号")]
    return {
        "在售总数": len(rows),
        "A_真亏": _pack(A, "A 真亏（预估<0 且 实际<0）→ 进归因"),
        "B_纸面亏": {**_pack(B, "B 纸面亏（预估<0 但实际≥0）→ 不动"),
                     "正在恶化": len(worsening),
                     "恶化Top": [{k: x.get(k) for k in ("skuId", "name", "预估毛利",
                                                        "实际单均毛利", "昨日亏损单")}
                                 for x in sorted(worsening,
                                                 key=lambda z: -(z.get("昨日亏损单") or 0))[:top]]},
        "C_零单": {"桶": "C 零单 → 不动", "款数": len(C)},
        "D_漏检": _pack(D, "D 漏检（预估≥0 但实际<0）→ 进归因，**旧流程完全看不见**"),
        "E_禁令": E,
        "归因入口": sorted({x["skuId"] for x in A + D}
                           | {x["sku"] for x in (E.get("over") or [])}),
        "★下一步": [
            "0) ★**先过 `easybi_coupon_run_rate` 排流速**——本表的失血是 15 日滚动、向后看的，"
            "已结束的脉冲会赖在榜首（实证 D 桶 Top3 全是已停的，91% 自行塌掉）。取「爆发/持续」，「消退」放回观察。",
            "1) A + D + E2 → `easybi_coupon_attribution` 逐券归因（黄金眼 T-1，能看到 osw 看不见的批次）",
            "2) 无券反事实 >0 ⇒ 券致亏，**别去涨价**；≤0 ⇒ 结构性，才改价/换供",
            "3) 承担方分档：平台担=0 可摘；**平台担>0 是共补券，摘掉连平台那份一起丢**",
            "4) B 桶只监控；但『正在恶化』那几款下次巡检要重点复查",
            "5) 收益一律是上限（摘一项其他会补位）→ 探针 1~2 款回读实测再批量",
        ],
    }


# ---------- 报名门槛闸门（报名前卡，别等亏了再止损） ----------
# ★★**为什么要有这道闸**（2026-08-06 实证）：当天 100 款零成交的账面亏损里，
#   国补打穿 50 款 —— **50/50 的基准价毛利率全部 <23.7%**；券打穿 37 款里 34 款 <31.3%。
#   也就是说这批不是"运气不好被打穿"，而是**从一开始就不该报**：定价的先天毛利就撑不住
#   活动叠加后的减免。事后止亏（摘券/退报名/涨价）只是补救，把闸门前移才是根治。
#
# 门槛来自 [[guobu-zhijiang-stack-rule]]：
#   · 国补15% + 官方直降5%           → 基准价毛利率需 ≥ 23.7%
#   · 上面再叠 9 折券                 → 需 ≥ 31.3%
# 用**基准价毛利率**（无任何券促时的毛利率）判，不是当前毛利率——后者已被券促压过，判不出先天条件。
ENROLL_MARGIN_GATE = {
    "guobu": 23.7,        # 国补(15%)+直降(5%)
    "guobu_coupon": 31.3,  # 上面再叠 9 折券
}


def enroll_gate(rows: list, scene: str = "guobu", margin_key: str = "基准价毛利率%") -> dict:
    """报名门槛体检：把**先天毛利撑不住**的挑出来，别报进去。

    `rows`：含 skuId 与基准价毛利率的 dict 列表（`list_low_margin`/`batch_pricing` 的行可直接喂）。
    `scene`：`guobu`(≥23.7%) / `guobu_coupon`(≥31.3%)，也可直接传数字自定义门槛。
    返回 {threshold, pass, block, pass_count, block_count}；`block` 里每条带 `gap` = 差多少个点。
    """
    thr = ENROLL_MARGIN_GATE.get(scene) if isinstance(scene, str) else float(scene)
    if thr is None:
        raise BlacklightError(f"未知 scene={scene}（可选 {list(ENROLL_MARGIN_GATE)} 或直接给数字）")
    ok, bad = [], []
    for r in rows or []:
        try:
            m = float(r.get(margin_key))
        except (TypeError, ValueError):
            bad.append(dict(r, gap=None, reason=f"取不到 {margin_key}，无法判门槛"))
            continue
        (ok if m >= thr else bad).append(
            dict(r, gap=round(thr - m, 2)) if m < thr else r)
    return {"threshold": thr, "scene": scene,
            "pass": ok, "block": bad,
            "pass_count": len(ok), "block_count": len(bad),
            "note": (f"基准价毛利率 <{thr}% 的 {len(bad)} 款**不建议报名**："
                     "先天毛利撑不住活动叠加后的减免，报进去大概率变成负毛利。"
                     "要报就先涨价/降成本把基准价毛利率抬过门槛。")}


# ---------- 共补券识别（跨模块共用） ----------
def _is_joint(c: dict) -> bool:
    """共补券判定：京喜承担额 < 客户减免额 → 平台/其他部门分担了差额。
    ⚠️**摘掉共补券 = 白丢平台补贴，且不可逆**。批量摘券前必须先筛出来单独判断。
    注意广告智能券(jxReward=0)也落在这里 —— 它是京喜完全不承担，摘了反而无损失但也无必要。"""
    return round(c.get("jxReward") or 0, 2) < round(c.get("reward") or 0, 2)


def _fmt_coupon(c: dict) -> str:
    """券的紧凑展示：名称 + 我承担额 + 客户减免额，共补券打 ★共补 标记。"""
    tag = "★共补" if _is_joint(c) else ""
    return f'{(c["name"] or "")[:8]}jx{c.get("jxReward", 0)}(客{c["reward"]}){tag}'


# ---------- 批量：到手价 / 盈亏 计算器（纯本地，零写操作） ----------
def batch_pricing(sku_ids: list, target_margin=None, objective: str = "max_margin",
                  caps: dict = None, channel: str = None) -> dict:
    """一批 SKU 的**到手价/毛利/盈亏体检**（每 SKU 一次 Home 接口，纯本地算，无写操作）。
      target_margin: 给了则附**建议报名价**（本地反解，objective 默认 max_margin=撞上限最大毛利）。
      caps: {skuId: 到手价上限}（如 bybt 建议价 / ms 门槛价）；缺则 max_margin 取报名价=京东价。
      channel: 报名频道(bybt/ms/seckill/baoyou/tejia/官方直降)——解价时**排除同大类已有促销**(替换语义)。
    返回 {rows:[...], summary:{总数,亏损数,...}}。

    ⚠️★**本函数的「亏损数」是报名场景的成本体检，不是"这个 SKU 现在亏不亏"的判据**。
      它基于 `jxFullProfit = jxActualPrice − fullCost`，而 fullCost 含 advCost
      （与实际单件广告费 r=0.392 的弱相关，见 `_parse_pricing_item` 注释）。
      **判 SKU 现在亏不亏一律走 `is_losing()` / `triage()`**，它们只认平台的
      预估毛利与实际单均毛利。2026-08-10 混用过一次，22 款里误判 16 款。"""
    from blacklight.osw import pricing as P
    caps = caps or {}
    excl = {P.CHANNEL_CATEGORY[channel]} if channel in P.CHANNEL_CATEGORY else ()
    rows, loss = [], 0
    for sku in sku_ids:
        s = str(sku).strip()
        if not s:
            continue
        try:
            pr = query_pricing(s)
        except Exception as e:
            rows.append({"skuId": s, "error": str(e)[:80]})
            continue
        # ★盈亏用**京喜承担口径**（广告/平台承担的减免京喜不出钱，不计亏）
        profit = pr["jxFullProfit"]
        is_loss = profit < 0
        loss += 1 if is_loss else 0
        # 基价裸毛利：无任何券促(到手价=基价)时的全口径毛利，判"结构性亏"vs"被券促打穿"
        bench_cost = round(pr["fixedCost"] + pr["cpsRate"] * pr["benchPrice"], 2)
        bench_profit = round(pr["benchPrice"] - bench_cost, 2)
        row = {"skuId": pr["skuId"], "name": (pr.get("skuName") or "")[:24],
               "benchPrice": pr["benchPrice"], "fullCost": pr["fullCost"],
               "purchase": pr["purchasePrice"], "shipping": pr["shippingCost"],
               "custActual": pr["actualPrice"], "jxActual": pr["jxActualPrice"],  # 客户到手价 / 京喜到手价
               "jxProfit": profit, "jxMargin": pr["jxMargin"], "盈亏": "亏" if is_loss else "盈",
               "custProfit": round(pr["actualPrice"] - pr["fullCost"], 2),  # 客户口径(会高估亏,仅参考)
               "benchProfit": bench_profit,      # 基价裸毛利(无券促)
               # 基准价毛利率：无券促时的毛利率。**报名门槛线判断用这个**
               # (国补15%+直降5% 需 ≥23.7%；叠9折券需 ≥31.3%，见 [[guobu-zhijiang-stack-rule]])
               "benchMargin": round(bench_profit / pr["benchPrice"], 4) if pr["benchPrice"] else None,
               "promos": ";".join(f'{p["cat"]}{p.get("jxReward",0)}(客{p["reward"]})' for p in pr["promotions"] if p["reward"]),
               "coupons": ";".join(_fmt_coupon(c) for c in pr["coupons"]),
               # ★共补券(jxReward<reward)：平台分担部分成本，**摘掉=白丢平台补贴且不可逆**。
               # 批量摘券前必须先把这些筛出来单独判断，见 [[gongbu-coupon-never-strip]]。
               "共补券": [{"name": c["name"], "客减": c["reward"], "我担": c.get("jxReward", 0),
                           "我担比例%": c.get("jxRatio")}
                          for c in pr["coupons"] if _is_joint(c)]}
        if target_margin is not None:
            jx_model = P.build_model(pr, exclude_cats=excl, basis="jx")        # 京喜口径→算毛利
            cust_model = P.build_model(pr, exclude_cats=excl, basis="customer")  # 客户口径→cap约束
            r = P.solve_enroll_price(jx_model, pr["fixedCost"], pr["cpsRate"],
                                     float(target_margin), hi=pr["benchPrice"],
                                     cap=caps.get(s), objective=objective, cust_model=cust_model)
            row["建议报名价"] = r.get("enroll_price") if r.get("ok") else None
            row["预测京喜到手价"] = r.get("actual_price") if r.get("ok") else None
            row["预测客户到手价"] = r.get("cust_actual") if r.get("ok") else None
            row["预测毛利"] = r.get("margin") if r.get("ok") else None
            row["定价"] = "OK" if r.get("ok") else r.get("reason", "")[:36]
        rows.append(row)
    ok = [r for r in rows if "error" not in r]
    return {"rows": rows,
            "summary": {"总数": len(rows), "成功": len(ok), "失败": len(rows) - len(ok),
                        "亏损": loss, "盈利": len(ok) - loss,
                        "口径": "全成本=采购+物流+CPS+广告；毛利率=(到手价−全成本)/到手价",
                        "target_margin": target_margin, "objective": objective}}
