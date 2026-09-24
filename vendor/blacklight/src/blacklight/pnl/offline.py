"""离线线（T-1）—— **归因 + 定责 + 谈判**。

实时线回答「谁现在在亏」，本线回答「**为什么亏、该找谁、能不能动**」。

    L2 流速   ge.drill 两窗 → runrate  ← 先做这步，别对已退潮的动手
    L3 归因   easybi.attribute_loss    ← 无券反事实 + 采销/平台承担（**承担口径以它为准**）
              ge.couponbatch           ← 券名 / 发券人 / 分摊比例（easybi 只有 batch_id）
    L4 决策   markettool.ladder        ← 真实回血（同类券互斥，摘顶档次档顶上）
              core.protected.filter_plan ← 禁令，拍板必过

## 三条铁律（都在今天栽过）
1. **先流速再归因**：15 日累计会把已塌 95% 的券排第一。
2. **范围对齐**：ge 不传 `sku_ids` 是整个 `cate_op_erp` 范围，
   与 easybi 只统计传入 SKU 不可比（实测 221 张券/22,202 vs 395 张/57,092，差 2.6 倍）。
3. **承担口径**：算「我担多少」用 easybi 的采销承担；
   ge 的「优惠券成本」是**全额**，只用来取券名与交叉校验（差 22.3%）。
"""
from __future__ import annotations

from blacklight.core import BlacklightError, protected as _protected
from blacklight.easybi import coupon as eb_coupon
from blacklight.ge import couponbatch as ge_cb
from blacklight.ge import margin as ge_margin
from blacklight.yx import markettool as yx_mt

from .runrate import from_ge_drill
from .scope import Scope, align, warn_basket_dependent
from .sentinel import full_equals_self_plus_platform, run_sentinels

MAX_SKU_FILTER = 500          # ge 页面标注的 SKU 框上限


def _cf_block(sku_rows: list, top: int = 20) -> dict:
    """★无券反事实（**ge 口径**，与判亏同基数）。

    `无券后单均 = (投后毛利 + 采销实担) / 单量`；>0 ⇒ 券致亏（**别去涨价**）。
    ⚠️与 easybi 版（osw 实际单均毛利 + 券我担）**不是同一个基数**：
      2026-08-11 实测 37 款，数值只有 22% 接近、**判定方向一致率 70%**，
      不一致的全在临界带。★判亏认 ge ⇒ 反事实也用 ge，否则是口径混用。
    """
    got = [r for r in sku_rows if r.get("无券后单均_ge") is not None
           and ge_margin.judge_losing(r)]
    got.sort(key=lambda r: -(r["无券后单均_ge"] or 0))
    return {
        "_口径": "ge（投后毛利基数，与 judge_losing 同基数）",
        "亏损款": len(got),
        "券致亏": sum(1 for r in got if (r["无券后单均_ge"] or 0) > 0),
        "结构性": sum(1 for r in got if (r["无券后单均_ge"] or 0) <= 0),
        "rows": [{"sku": r["sku_id"], "投后毛利": r["预估毛利_投后"],
                  "采销实担": r.get("减免_采销实担"), "单量": r.get("单量"),
                  "无券后单均": r["无券后单均_ge"], "判定": r["反事实判定_ge"]}
                 for r in got[:top]],
        "_说明": "券致亏 ⇒ 定价没问题，去摘券/谈退圈，**别涨价**；"
                 "结构性 ⇒ 摘券治不了，走定价/换供/物流",
    }


def attribute(skus: list, recent_window: tuple, baseline_window: tuple,
              *, degree: str = "coupon", top: int = 20,
              with_counterfactual: bool = True, strict: bool = True,
              realtime: bool = False) -> dict:
    """归因：流速 → 逐券承担 → 券名/发券人 → 是否可摘。

    skus：**必须显式给**——不给就是整个 cate_op_erp 范围，跨源没法比。

    `realtime=True`（**今日实时归因**）——见 `attribute_rt()`。
    """
    if realtime:
        return attribute_rt(skus, baseline_window, degree=degree, top=top)
    if not skus:
        raise BlacklightError(
            "必须显式传 skus。不传时 ge 查的是整个 cate_op_erp 范围，"
            "而 easybi 只统计传入的 SKU——两边不可比（今天已因此误判过一次）。")
    skus = [str(s) for s in skus]
    if len(skus) > MAX_SKU_FILTER:
        raise BlacklightError(
            "一次最多 %d 个 SKU（ge 页面标注的上限）。收到 %d 个，请分批。"
            % (MAX_SKU_FILTER, len(skus)))

    rs, re_ = recent_window
    bs, be = baseline_window

    # ---- L2 流速（先做，避免对已退潮的动手）----
    r_drill = ge_margin.drill(rs, re_, degree=degree, realtime=False, sku_ids=skus)
    b_drill = ge_margin.drill(bs, be, degree=degree, realtime=False, sku_ids=skus)
    rr = from_ge_drill(r_drill, b_drill,
                       recent_window=recent_window, baseline_window=baseline_window)

    if degree != "coupon":
        return {"维度": degree, "流速": rr,
                "_说明": "非券维度只做流速；逐券承担与券名仅 coupon 维度可用"}

    # ---- L3 归因（easybi = 承担口径权威）----
    # ★★必须传 `osw_rows`，否则**无券反事实全是 None**——而它正是 easybi 的独占能力
    #   （判「券致亏 vs 结构性」的唯一依据：>0 ⇒ 券致亏、别去涨价）。
    #   2026-08-11 首次完整跑通时就漏了这个参数，反事实一列全空。
    #   复用 scan 的前瞻网缓存（15 分钟 TTL），避免重复 22 秒全量扫。
    osw_rows = None
    try:
        from .scan import _load_forecast
        osw_rows = {str(r["skuId"]): r for r in (_load_forecast("auto").get("rows") or [])}
    except Exception:
        pass          # 拿不到就退化为无反事实，但下面会显式说明，不静默
    eb = eb_coupon.attribute_loss(skus=skus, start=rs, end=re_, osw_rows=osw_rows)
    eb_batches = eb.get("by_batch") or {}

    # 范围对齐后才可做恒等式
    align(Scope.of(skus, rs, re_, label="ge.drill"),
          Scope.of(skus, rs, re_, label="easybi.attribute_loss"))

    # ---- 券名/发券人/分摊比例（ge 独占）。**全量取**，恒等式要用面额分类 ----
    g_by_batch = {str(r.get("batch_id")): r for r in r_drill["rows"]}
    all_batches = list(set(g_by_batch) | set(eb_batches))
    attrs = ge_cb.get_dimension_attr(all_batches, rs, re_) if all_batches else {}

    # ★★恒等式**只对固定面额券成立**（2026-08-11 两次实证）：
    #   满减/折扣券随篮子拆分，ge 与 easybi 的归集方式不同，逐条比不适用。
    #   41 款样本实测：固定面额券 104 张 ge==easybi **差 0.00**；
    #                 篮子相关券 8 张差 84.16（10.8%）——缺口 100% 来自它们。
    #   288 款样本因固定面额券占绝大多数，整体只差 0.07%，掩盖了这一点。
    #   ⇒ 别靠放宽容差蒙混，要把适用范围写对。
    fx_g = fx_self = fx_plat = 0.0
    bk_g = bk_e = 0.0
    n_fx = n_bk = 0
    for b in all_batches:
        gv = float((g_by_batch.get(b) or {}).get("减免_全额") or 0)
        v = eb_batches.get(b) or {}
        sv, pv, qty = v.get("self", 0.0), v.get("plat", 0.0), v.get("qty", 0)
        face = float((attrs.get(b) or {}).get("jdr_sch_coupon_cps_face_value") or 0)
        per = ((sv + pv) / qty) if qty else 0
        if face > 0 and abs(per - face) <= max(0.05, face * 0.02):
            fx_g += gv; fx_self += sv; fx_plat += pv; n_fx += 1
        else:
            bk_g += gv; bk_e += sv + pv; n_bk += 1
    # 承担合计仍取**全部**券（恒等式只是校验手段，不是口径定义）
    eb_self = sum(v.get("self", 0) for v in eb_batches.values())
    eb_plat = sum(v.get("plat", 0) for v in eb_batches.values())
    ge_full = sum(float(r.get("减免_全额") or 0) for r in r_drill["rows"])
    s_identity = full_equals_self_plus_platform(fx_g, fx_self, fx_plat)
    s_identity.name += "（仅固定面额券 %d 张）" % n_fx
    health = run_sentinels(s_identity, strict=strict)
    health["篮子相关券"] = {
        "张数": n_bk, "ge全额": round(bk_g, 2), "easybi合计": round(bk_e, 2),
        "差": round(bk_g - bk_e, 2),
        "_说明": "满减/折扣券随篮子拆分，两边归集方式不同 ⇒ **不纳入恒等式**，"
                 "也别逐条比；看总量趋势即可",
    }

    rows = []
    for r in rr["rows_actionable"][:top]:
        bid = r["key"]
        a = attrs.get(bid, {})
        v = eb_batches.get(bid) or {}
        self_, plat = v.get("self", 0.0), v.get("plat", 0.0)
        face = a.get("jdr_sch_coupon_cps_face_value")
        per = (self_ + plat) / v["qty"] if v.get("qty") else 0
        rows.append({
            "批次": bid,
            "券名": a.get("jdr_sch_coupon_batch_name") or r.get("名称"),
            "发券人": a.get("jd_erp"),
            "发券部门": a.get("hr_dept_name_2"),
            "面额": face,
            "联合承担": a.get("jdr_sch_coupon_dept_cost_union_flag"),
            "近期日均": r["近期日均"], "倍数": r["倍数"], "状态": r["状态"],
            "采销承担": round(self_, 2), "平台承担": round(plat, 2),
            "性质": "共补" if plat > 0 else "自担",
            # ★★**看毛利不是只看承担额**：承担高但对应订单赚钱的是正常营销投入，
            #   承担不高但对应订单在亏的才该动。2026-08-11 实测：85折券采销担 324
            #   但投后 +365（别动）；居家冲单券6-4 采销担 164、投后 **−1.28**（该动）。
            "对应订单投后毛利": (g_by_batch.get(bid) or {}).get("预估毛利_投后"),
            "无减免后毛利": (g_by_batch.get(bid) or {}).get("无减免后毛利"),
            "无减免后单均": (g_by_batch.get(bid) or {}).get("无减免后单均"),
            "载体判定": (g_by_batch.get(bid) or {}).get("反事实判定"),
            "_可比性": warn_basket_dependent(face, per),
        })

    out = {
        "维度": degree, "窗口_近期": list(recent_window), "窗口_基线": list(baseline_window),
        "SKU数": len(skus), "哨兵": health,
        "流速": {"水位%": rr["水位%"], "状态分布": rr["状态分布"], "待处置": rr["待处置"]},
        "承担合计": {"采销承担": round(eb_self, 2), "平台承担": round(eb_plat, 2),
                     "ge全额": round(ge_full, 2), "_口径": "采销承担以 easybi 为准"},
        "候选": rows,
        "★下一步": [
            "1) 只看『状态』为 爆发/持续/新增 的——消退的动手会打空",
            "2) 『共补』(平台承担>0) **永不批量摘**：摘掉连平台那份一起丢",
            "3) 摘之前跑 `feasibility()`：ladder 看真实回血 + protected 过禁令",
            "4) 找谁谈看『发券人』——承担方分档与录券人分档基本是同一刀",
        ],
    }
    if with_counterfactual:
        # ★ge 口径为主（与判亏同基数），easybi 版作交叉参考
        sku_off = ge_margin.drill(rs, re_, degree="sku", realtime=False, sku_ids=skus)
        out["无券反事实"] = _cf_block(sku_off["rows"], top)
        cf = [{"sku": s.get("sku"), "无券后单均": s.get("无券后单均"),
               "判定": s.get("判定")} for s in (eb.get("skus") or [])]
        got = [r for r in cf if r["无券后单均"] is not None]
        out["无券反事实_easybi交叉参考"] = {
            "_说明": "easybi 口径（osw 实际单均毛利基数）——**与 ge 版不同基数**，判定一致率约 70%，不一致的在临界带。主口径看 `无券反事实`。原说明：>0 ⇒ 券致亏（定价没问题，**别去涨价**）；≤0 ⇒ 结构性",
            "可算出": len(got), "总数": len(cf),
            "券致亏": sum(1 for r in got if str(r["判定"]).startswith("券致亏")),
            "结构性": sum(1 for r in got if str(r["判定"]).startswith("结构性")),
            "rows": sorted(got, key=lambda r: -(r["无券后单均"] or 0))[:top],
            **({"_缺口": "有 %d 款算不出反事实——osw 侧没有它们的实际单均毛利"
                         "（多为 ge 独有、osw 归属口径认不出的款）。"
                         "这类判不了『券致亏 vs 结构性』，别硬套结论"
                         % (len(cf) - len(got))} if len(got) < len(cf) else {}),
        }
    return out


def still_bleeding(sku_ids: list, days: int = 3, min_days: int = 1) -> dict:
    """★**动单款之前先问：它现在还在亏吗**（2026-08-12 血的教训）。

    `runrate` 是按**券批次 / 促销**维度算的：一张券在**整个部门**还在花钱 ⇒ 状态「持续」，
    但**在某个 SKU 上可能早就停了**。两次实证：

        10127205604648  七天投后 −282.91 ⇒ 逐日看 8/11 已 **+30.46**（减免归零）
        10163019223329  可行性报「可摘 7 批次、Δ1.0/单」⇒ 减免自 8/05 起就是 0，券后来也清空了

    七天累计会把「最后一天已转正」平均掉。照那份清单动手 = 对着已停的问题发写操作。

    返回 {skuId: {"仍在亏": bool, "近N日投后": x, "近N日采销实担": y, "有成交天数": n}}。
    **取不到数的标 `None`，不当成「没在亏」**——静默跳过比误判更危险。
    """
    import datetime as _dt
    ids = [str(s) for s in (sku_ids or [])]
    if not ids:
        return {}
    end = _dt.date.today() - _dt.timedelta(days=1)          # ge 离线到 T-1
    out = {}
    for i in range(days):
        d = (end - _dt.timedelta(days=i)).isoformat()
        try:
            g = ge_margin.drill(d, d, degree="sku", realtime=False, sku_ids=ids)
        except Exception:
            continue
        for r in (g.get("rows") or []):
            sid = str(r.get("sku_id"))
            o = out.setdefault(sid, {"投后": 0.0, "采销实担": 0.0, "天数": 0, "末日投后": None})
            m = float(r.get("预估毛利_投后") or 0)
            o["投后"] += m
            o["采销实担"] += float(r.get("减免_采销实担") or 0)
            o["天数"] += 1
            if i == 0:                                       # 最近一天单独留，别被前几天平均掉
                o["末日投后"] = round(m, 2)
    res = {}
    for s in ids:
        o = out.get(s)
        if not o or o["天数"] < min_days:
            res[s] = {"仍在亏": None, "_说明": "近 %d 日无成交或取数失败 ⇒ 无法判断，别当作『没在亏』" % days}
            continue
        last = o["末日投后"]
        res[s] = {
            "仍在亏": (last < 0) if last is not None else (o["投后"] < 0),
            "近N日投后": round(o["投后"], 2), "近N日采销实担": round(o["采销实担"], 2),
            "末日投后": last, "有成交天数": o["天数"],
            "_判据": "**以最近一天为准**；近N日合计只作参考（会把已转正的那天平均掉）",
        }
    return res


def feasibility(sku_ids: list, *, workers: int = 5, use_plan: bool = True,
                check_live: bool = True, live_days: int = 3) -> dict:
    """L4 可行性。★**默认委托 `markettool.plan_strip_except`**，别自己从 ladder 挑。

    ⚠️★2026-08-11 教训：我最初在这里自己算「顶档可摘回血」，**漏掉了保留券语义**，
      把两款实际零回血的 SKU 报成「回血 1.0/单、可动手」。真实情况是
      `plan_strip_except` 判定的：

          10163019223323  券176 保留6  待摘170 → **本次要摘 0**，预计Δ/单 **0.0**
          10201271089506  券199 保留15 待摘184 → **本次要摘 0**，预计Δ/单 **0.0**
          原因：预计生效券是共补券「三类货品类新4.01-4」(我担3.0)，**它本身已是最高档**
                ⇒ 摘掉它下面所有券一分不回血

      摘不动的真因是「**保留券已是最高档**」，不是「档位多所以差值小」。

    ⚠️**写操作按 `campaignId` 不按张**：176 张券 = 20 个批次、199 张 = 36 个批次。
      按张估写操作次数会高估一个数量级。

    ⚠️`plan_strip_except` 已内置三个闸，别绕开：
      `无收益(已剔除)` / `无保留券`（全摘会把到手价抬回裸价，需单独决策）/ `取数失败`。

    ★**收益一律是上限**：同类券互斥、只生效面额最大的一张 ⇒ 摘掉顶档次档立刻顶上。
    ★**平台担=0 只是必要条件**：B补券 100% 自担却不能摘（平台另给团长 0.5/单推广），
      所以必须**同时**过 `protected.filter_plan`。

    `use_plan=False` 退回旧的 ladder 自算路径——**仅用于对照，不要拿它下结论**。
    """
    ids = [str(s) for s in (sku_ids or [])]
    if not ids:
        raise BlacklightError("feasibility 需要 sku_ids")

    if use_plan:
        pl = yx_mt.plan_strip_except(ids)
        plans = pl.get("plans") or []
        prot = _protected.filter_plan(ids, action="strip")
        blocked = {str(x) for x in (prot.get("blocked") or [])}
        rows = []
        for p_ in plans:
            sid = str(p_.get("skuId"))
            rows.append({
                "skuId": sid, "券总数": p_.get("券总数"),
                "保留": p_.get("保留"), "待摘全部": p_.get("待摘(全部)"),
                "本次要摘": p_.get("★本次要摘"),
                "预计Δ每单": p_.get("预计Δ/单"),
                "预计生效券": (p_.get("预计生效券") or {}).get("name"),
                "SKU级禁令": sid in blocked,
                "可动手": (not sid in blocked) and (p_.get("★本次要摘") or 0) > 0
                          and (p_.get("预计Δ/单") or 0) > 0,
            })
        # ★动手前必查该 SKU 自己的逐日曲线（券批次维度的流速会掩盖 SKU 级消退）
        live = still_bleeding(ids, days=live_days) if check_live else {}
        for r in rows:
            lv = live.get(r["skuId"]) or {}
            r["仍在亏"] = lv.get("仍在亏")
            r["末日投后"] = lv.get("末日投后")
            if check_live and r["可动手"] and lv.get("仍在亏") is False:
                r["可动手"] = False
                r["不可动手原因"] = "近 %d 日已转正（末日投后 %s）——七天累计在亏是历史，别动" % (
                    live_days, lv.get("末日投后"))
            elif check_live and r["可动手"] and lv.get("仍在亏") is None:
                r["可动手"] = False
                r["不可动手原因"] = "近 %d 日无成交/取数失败，无法确认是否仍在亏 ⇒ 转人工" % live_days
        # 工具已分好的三个桶，原样透出——**它们不是错误，是明确的不可行原因**
        return {
            "rows": rows,
            "可动手": sum(1 for r in rows if r["可动手"]),
            "已自愈(近日转正)": [r["skuId"] for r in rows if r.get("仍在亏") is False],
            "无法确认": [r["skuId"] for r in rows if r.get("仍在亏") is None],
            "写操作总数": pl.get("写操作总数"),
            "省下的写操作": pl.get("省下的写操作"),
            "无收益(已剔除)": pl.get("无收益(已剔除)"),
            "无保留券": pl.get("无保留券"),
            "无券(亏在促销/国补)": pl.get("无券(亏在促销/国补)"),
            "取数失败": pl.get("取数失败"),
            "protected": prot,
            "_口径": "★写操作按 **campaignId** 不按张（176张券=20批次）",
            "_纪律": pl.get("_纪律"),
        }

    from blacklight.core import pmap_batch

    def one(s):
        out = {"skuId": s}
        try:
            L = yx_mt.ladder(s)
            out["券总数"] = L.get("券总数")
            out["档位数"] = L.get("档位数")
            out["可摘张数"] = L.get("可摘张数")
            out["受禁令保护"] = L.get("受禁令保护") or []
            gain = 0.0
            for t in (L.get("档位") or []):
                if t.get("可摘", 0) > 0:
                    gain = t.get("真实回血/单", 0.0)
                    break
                if t.get("受保护", 0) > 0:
                    break            # 顶档被禁令挡住，下面摘了也顶不上来
            out["顶档真实回血"] = gain
            # ★0 张券按取数失败处理（query_sku 会静默返回空券列表）
            if not L.get("券总数"):
                out["_警告"] = "券总数=0 —— 按**取数失败**处理，不是『没券可摘』"
        except Exception as e:
            out["_错误"] = str(e)[:100]
        return out

    res = pmap_batch(one, ids, workers=workers, label="feasibility")
    rows = res["rows"]
    prot = _protected.filter_plan(ids, action="strip")
    blocked = {str(x) for x in (prot.get("blocked") or [])}
    for r in rows:
        # ★两个粒度**别混**（2026-08-11 差点混掉）：
        #   · SKU级  = `protected.filter_plan` —— 这款整体不许动
        #   · 档位级 = `ladder.受禁令保护`      —— 这款上**某些券档**受规则保护
        #   实测 10163019223322：SKU 级允许，但档位级有 `yongzeng-gongjian`。
        #   两个都为真不矛盾；写成一个字段会被读成「没有任何保护」。
        r["SKU级禁令"] = r["skuId"] in blocked
        r["档位级禁令"] = r.get("受禁令保护") or []
        ok = (not r["SKU级禁令"]) and (r.get("顶档真实回血") or 0) > 0
        r["可动手"] = ok
        r["_可动手说明"] = (
            "SKU 级被禁令拦下" if r["SKU级禁令"] else
            ("取数失败，不是没券" if r.get("_警告") or r.get("_错误") else
             ("顶档无可摘或回血为 0" if not ok else
              ("可摘，但注意档位级禁令 %s" % "、".join(r["档位级禁令"])
               if r["档位级禁令"] else "可摘"))))
    return {"rows": rows, "protected": prot,
            "可动手": sum(1 for r in rows if r.get("可动手")),
            "SKU级禁令": sum(1 for r in rows if r.get("SKU级禁令")),
            "含档位级禁令": sum(1 for r in rows if r.get("档位级禁令")),
            "取数失败": sum(1 for r in rows if r.get("_警告") or r.get("_错误")),
            "_纪律": "收益是上限不是预测——先探针 1~2 款回读实测再批量。"
                     "★『可动手』只代表 SKU 级没被拦且顶档有回血，"
                     "**不代表这款上没有受保护的券档**（见 档位级禁令）"}


# --------------------------------------------------------------------------- #
# 实时归因（T-0）—— 今天哪张券/哪个促销正在吃毛利
# --------------------------------------------------------------------------- #
def attribute_rt(skus: list, baseline_window: tuple, *, degree: str = "coupon",
                 top: int = 20) -> dict:
    """★**今日实时归因**：哪张券/哪个促销**此刻**正在吃毛利。

    与离线归因的差别（**都是真实约束，不是偷懒**）：

    | | 离线 (T-1) | 实时 (T-0，本函数) |
    |---|---|---|
    | 承担口径 | easybi 采销承担（权威） | **ge 三方拆分**：全额 − 平台补贴 − 事业部承担 |
    | 无券反事实 | ✓ easybi 独占 | **✗ 拿不到**（easybi 是 T-1，覆盖不到今天） |
    | 恒等式哨兵 | ✓ ge vs easybi 互校 | **✗ 只有 ge 一个源，无从互校** |
    | 券名/发券人 | ge couponbatch | 同（券维度实时也带券名） |

    用户 2026-08-11 确认「实时毛利监控里的承担比例也是准确」，
    所以实时用 ge 的三方拆分算采销承担是成立的；但**少了一个交叉校验源**，
    结论强度不如离线 ⇒ **实时用于止血，定责仍以离线为准**。

    ## ★基线怎么取（这里最容易出错）
    今日只过了一部分，**直接拿今日累计和离线日均比就是「实时≠一天」的陷阱**
    （实测同一天：实时窗口 915 款 vs 离线 5,417 款）。
    本函数按**已过时长折算**成日速率再比，并把 `已过时长比例` 与 `_外推` 显式返回。
    ⚠️外推假设「当日均匀发生」——大促/整点场次会破坏它，看 `_外推` 标记别当精确值。
    """
    from blacklight.core import BlacklightError as _E
    if not skus:
        raise _E("必须显式传 skus（不传则 ge 查整个 cate_op_erp 范围）")
    skus = [str(s) for s in skus]
    if len(skus) > MAX_SKU_FILTER:
        raise _E("一次最多 %d 个 SKU，收到 %d 个" % (MAX_SKU_FILTER, len(skus)))

    import datetime as _dt
    now = _dt.datetime.now()
    S = now.strftime("%Y-%m-%d 00:00:00")
    E = now.strftime("%Y-%m-%d %H:%M:%S")
    elapsed = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    frac = max(elapsed / 86400.0, 1e-6)

    rt = ge_margin.drill(S, E, degree=degree, realtime=True, sku_ids=skus)
    # ★无券反事实：ge 自己就能算（投后毛利 + 采销实担），**实时也有**
    sku_rt = ge_margin.drill(S, E, degree="sku", realtime=True, sku_ids=skus)
    bs, be = baseline_window
    base = ge_margin.drill(bs, be, degree=degree, realtime=False, sku_ids=skus)

    dim = rt.get("dim")
    NAME = {"batch_id": "jdr_sch_coupon_batch_name",
            "jdr_sch_page_promotion_id": "jdr_sch_promotion__act_name"}
    nk = NAME.get(dim)

    def _days(a, b):
        d1 = _dt.date.fromisoformat(a[:10]); d2 = _dt.date.fromisoformat(b[:10])
        return max((d2 - d1).days + 1, 1)
    bdays = _days(bs, be)

    bmap = {str(r.get(dim)): float(r.get("减免_采销实担") or 0) for r in base["rows"]}
    rows = []
    for r in rt["rows"]:
        k = str(r.get(dim))
        today = float(r.get("减免_采销实担") or 0)
        proj = today / frac                      # 按已过时长折算的**全日预计**
        bpd = bmap.get(k, 0.0) / bdays
        if bpd <= 0:
            ratio, state = (None, "新增") if today > 0 else (None, "停止")
        elif proj <= 0:
            ratio, state = 0.0, "停止"
        else:
            ratio = proj / bpd
            state = "爆发" if ratio >= 2 else ("消退" if ratio < 0.5 else "持续")
        rows.append({
            "key": k, "名称": r.get(nk),
            "今日累计_采销担": round(today, 2),
            "折算日速率": round(proj, 2), "基线日均": round(bpd, 2),
            "倍数": None if ratio is None else round(ratio, 2), "状态": state,
            "对应订单投后毛利": r.get("预估毛利_投后"),
            "无减免后毛利": r.get("无减免后毛利"),
            "无减免后单均": r.get("无减免后单均"),
            "载体判定": r.get("反事实判定"),
            "今日全额": r.get("减免_全额"), "今日平台担": r.get("减免_平台担"),
            "性质": "共补" if (r.get("减免_平台担") or 0) > 0 else "自担",
            "单量": r.get("单量"),
        })
    rows.sort(key=lambda x: -x["今日累计_采销担"])
    act = [r for r in rows if r["状态"] in ("爆发", "持续", "新增")]
    # ★真正该动的：**对应订单在亏**的载体（不是承担额最大的）
    bleeding = [r for r in rows if (r.get("对应订单投后毛利") or 0) < 0]
    bleeding.sort(key=lambda r: r["对应订单投后毛利"])

    return {
        "口径": "实时 T-0（ge 单源）", "维度": degree,
        "窗口": [S, E], "基线窗口": [bs, be],
        "已过时长比例": round(frac, 4),
        "_外推": ("『折算日速率』= 今日累计 ÷ 已过时长(%.1f%%)，"
                  "假设当日均匀发生；大促/整点场次会破坏该假设，别当精确值"
                  % (100 * frac)),
        "SKU数": len(skus),
        "今日采销承担合计": round(sum(r["今日累计_采销担"] for r in rows), 2),
        "状态分布": {s: sum(1 for r in rows if r["状态"] == s)
                     for s in ("爆发", "持续", "新增", "消退", "停止")},
        "待处置": len(act),
        "★亏损载体": {
            "个数": len(bleeding),
            "合计投后毛利": round(sum(r["对应订单投后毛利"] or 0 for r in bleeding), 2),
            "rows": bleeding[:top],
            "_说明": "★**这才是该动的**——对应订单在亏。承担额大但对应订单赚钱的"
                     "（如 85折券 担324/赚365）是正常营销投入，别动。",
        },
        "rows_actionable": act[:top],
        "rows": rows[:top],
        "无券反事实": _cf_block(sku_rt["rows"], top),
        "_缺失能力": [
            "ge×easybi 恒等式互校 —— 实时只有 ge 一个源",
            "easybi 口径的反事实（osw 实际单均毛利基数）—— T-1 覆盖不到今天；"
            "但**ge 口径的反事实实时可算**（见 `无券反事实`），"
            "且与判亏同基数、更自洽",
        ],
        "★下一步": [
            "1) 只看 爆发/持续/新增；消退的动手会打空",
            "2) 『共补』(平台担>0) 永不批量摘",
            "3) 实时用于**止血**；要定责/谈判走 `attribute()` 离线口径",
        ],
    }
