"""
yx-mcp 场域：markettool（京喜**营销工具**——单 SKU 全券促查询 + 删券促 + 统一退出）。

⚠️ 2026-07-17 拆分（见 [[jd-mcp-architecture]]）：原文件的 **A 段（实时毛利监控 + 定价底料）已迁到
`jdcore/osw_margin.py`**（osw 采销工作台公共层，供 osw-mcp 工具面 + yx bybt/ms 定价 + 未来 jzt 共用）。
本文件只保留 **B 段：券促查询/删除/统一退出**——它耦合 campaign(yx_client)+subsidy(yx_subsidy) 的退出链路，属营销侧。
底层签名/客户端/Home 取数从 osw_margin import 复用（不重复实现）。

补齐 campaign/subsidy 缺的「单 SKU 全券促」能力：一把查出某 SKU 身上挂的**所有券 + 所有促销**，并按能否本工具删除分类：
  - 券(couponList)：canOperate==1 → 可删(deleteDiscount)；==0 → 本工具删不了(带 campaignId，可交 yx_campaign_withdraw 退活动)
  - 促(promoList)：type∈{1,26,19}(单品促销/直降/包邮) → 可删(pm-erp)；其余(总价促销10/官方立减25/平台2,11) → 本工具删不了(活动类)
删除动作走 confirm_token 门（与 campaign/subsidy 一致），dry-run 默认。
"""
from __future__ import annotations

import json as _json
import time as _time

from blacklight.yx import client as yx_client
from blacklight.yx import subsidy as yx_subsidy
from blacklight.core import policy
from blacklight.core import (BlacklightError, confirm_token as _confirm_token, audited,
                             pmap, pmap_batch)
# 底层签名/客户端/Home 取数 + 促销类型名 —— 复用公共层 osw_margin（原 A 段），不重复实现
from blacklight.osw.margin import (_client, _call, _fetch_detail, _home_promos, _promo_type_name,
                                   _fen2yuan)

PM_ERP_DELETE_URL = "https://pm-erp.jd.com/promo/onlyBatchDeletePromo.action"

DELETABLE_PROMO_TYPES = {1, 26, 19}   # 单品促销/单品直降/单品包邮 → 本工具可删
SUBSIDY_PROMO_TYPE = 5   # queryPreDiscountHome promotionList 里「购新国补立减补贴」的 promoType（Detail 查不到国补）
# Home promotionList 里的**活动类**促销 promoType（这些走 campaign 退出报名，Detail 常查不到）：
# 2=平台活动(如跨店满减)、10=总价促销、25=官方立减
_HOME_ACTIVITY_TYPES = {2, 10, 25}


# ---------- 只读：单 SKU 全券促 + 分类 ----------
def query_sku(sku_id: str | int) -> dict:
    """
    查单 SKU 全部券促并分类。返回：
      {skuId, coupons:{deletable:[{campaignId,name}], viaActivity:[{campaignId,name}]},
       promos:{deletable:[{promoId,type,name}], viaActivity:[{promoId,type,name}]}, summary}
    - coupons.deletable  : canOperate==1 → 本工具 deleteDiscount 可删
    - coupons.viaActivity: canOperate==0 → 本工具删不了；带 campaignId，可交 yx_campaign_withdraw 退活动
    - promos.deletable   : type∈{1,26,19} → 本工具 pm-erp 可删
    - promos.viaActivity : 活动类(总价/官方立减/平台…) → 本工具删不了；按 promoName 映射活动后走 yx 退出
    """
    sku_id = str(sku_id).strip()
    with _client() as client:
        d = _fetch_detail(client, sku_id) or {}
    coup_del, coup_act, coup_nocid = [], [], []
    for c in (d.get("couponList") or []):
        cid = c.get("campaignId")
        # ★券面额/承担额一直在 Detail 的 couponList 里(rewardPrice/jxRewardPrice，单位分)，
        # 以前只透出名字 → 券名会撞(「拉通8-4」vs「居家8-4」是两张不同券)，只能靠摘完回读试错。
        # 现在直接带上金额，摘券前就能算准，见 [[guobu-zhijiang-stack-rule]]。
        rec = {"campaignId": str(cid) if cid else None, "name": c.get("couponName", ""),
               "couponType": c.get("couponType"),
               "面额": _fen2yuan(c.get("rewardPrice")),        # 客户减免额
               "我承担": _fen2yuan(c.get("jxRewardPrice")),    # 京喜实际承担额
               "我担比例%": c.get("finalJxActualRatio"),
               "couponId": c.get("couponId"),                 # ★= ge 的 batch_id / osw 的 couponId
               "发券人": c.get("creator")}
        # 共补券：我承担 < 客户减免 → 平台/他部门分担差额。摘掉=白丢补贴且不可逆。
        rec["共补"] = rec["我承担"] < rec["面额"]
        if not cid:
            # ★2026-08-13 修：这里原来是 `continue`，把 campaignId 为空的券**静默丢掉**。
            #   实测 10198041344136：41 张里 7 张被扔（4 张「用增_摇优惠_全品类招商券」我担 70%、
            #   机器平台券、广告智能券），于是它们连 viaActivity 都进不去，
            #   而 docstring 承诺的「viaActivity 带 campaignId → yx_campaign_withdraw」对它们完全失效。
            #   ⇒ 归因时会整类漏掉，且「查不到」会被误读成「不存在/已退」。
            #   它们的 `couponId` 就是 ge 的 batch_id，可据此与归因对账；
            #   招商券要退走 campaign 侧（yx_campaign_get_sku_status / yx_campaign_withdraw）。
            coup_nocid.append(rec)
            continue
        (coup_del if c.get("canOperate") == 1 else coup_act).append(rec)
    promo_del, promo_act = [], []
    for p in (d.get("promoList") or []):
        if not p.get("promoId"):
            continue
        name = p.get("promoName", "")
        rec = {"promoId": p.get("promoId"), "type": p.get("promoType"),
               "typeName": _promo_type_name(p.get("promoType")), "name": name}
        # 活动报名类(名字带"报名"，如官方直降)即使 type∈{1,26,19} 也归活动类：应走 yx 退出报名，
        # 而非 pm-erp 删促销（删促销未必等于退出报名）。其余非单品类也归活动类。
        is_activity = ("报名" in name) or (p.get("promoType") not in DELETABLE_PROMO_TYPES)
        if is_activity:
            rec["活动名"] = name.replace("活动报名", "").replace("报名", "").strip()
            promo_act.append(rec)
        else:
            promo_del.append(rec)
    # Home(strSkuIds) 的 promotionList 比 Detail 全：国补(type5)、**满减/平台活动(type2)、总价促销(type10)、官方立减(type25)
    # 都可能只在 Home**（如跨店满减 Detail 查不到）。一次取 Home，既抓国补、也补活动类促销。
    try:
        home_promos = _home_promos(sku_id)
    except Exception as e:
        home_promos, home_err = [], str(e)[:60]
    else:
        home_err = None
    home_gb = [p for p in home_promos if p.get("promoType") == SUBSIDY_PROMO_TYPE]
    # 活动类促销(平台活动/总价促销/官方立减)从 Home 补进 promo_act（走 campaign 退出报名，按活动名对 campaignId）；按名去重。
    _seen = {p["name"] for p in promo_act} | {p["name"] for p in promo_del}
    for p in home_promos:
        nm = p.get("promoName")
        if p.get("promoType") in _HOME_ACTIVITY_TYPES and nm and nm not in _seen:
            _seen.add(nm)
            promo_act.append({"promoId": p.get("promoId"), "type": p.get("promoType"),
                              "typeName": _promo_type_name(p.get("promoType")), "name": nm,
                              "活动名": nm.replace("活动报名", "").replace("报名", "").strip(), "_from": "home"})
    try:
        subsidy = yx_subsidy.find_sku(sku_id)   # 权威：带 applyId/池/力度
    except Exception as e:
        subsidy, subsidy_err = [], str(e)[:60]
    else:
        subsidy_err = None
    if home_gb:                                 # 把 Home 的补贴金额贴到 find_sku 记录
        amt = home_gb[0].get("rewardPrice")
        for r in subsidy:
            r.setdefault("补贴金额", amt)
    # Home(SKU价格层)看到国补、但 find_sku 无 applyId → **该国补报名不是本账号做的**（别人报的，
    # 不在本账号 apply/page 列表里），本账号查不到 applyId、也退不了 → 转人工/找原报名人。
    subsidy_unmatched = ([{"promoId": p.get("promoId"), "name": p.get("promoName"),
                           "补贴金额": p.get("rewardPrice"), "原因": "报名非本账号,无applyId"} for p in home_gb]
                         if (home_gb and not subsidy) else [])
    return {
        "skuId": sku_id,
        "coupons": {"deletable": coup_del, "viaActivity": coup_act,
                    # ★无 campaignId 的券（招商券/机器平台券/广告智能券等）。
                    #   本工具删不了；招商券走 campaign 侧退出。**以前这批被静默丢掉**。
                    "noCampaignId": coup_nocid},
        "promos": {"deletable": promo_del, "viaActivity": promo_act},
        "subsidy": subsidy,                     # 国补报名 [{pool,blockId,applyId,补贴金额,...}]（可退，走 yx_subsidy_withdraw）
        "subsidy_unmatched": subsidy_unmatched,  # Home检测到但无applyId(池未知)→需人工退
        "summary": {
            "券总数": len(coup_del) + len(coup_act) + len(coup_nocid),
            "可删券": len(coup_del), "不可删券": len(coup_act),
            "无campaignId券": len(coup_nocid),
            "促总数": len(promo_del) + len(promo_act), "可删促": len(promo_del), "活动类促": len(promo_act),
            "国补报名": len(subsidy) + len(subsidy_unmatched),
        },
        "subsidy_err": subsidy_err, "home_err": home_err,
        "note": "券删=Detail(campaignId)；国补检测=Home(promoType5)+find_sku(applyId退出)；活动促走 yx_campaign_withdraw。"
                "★noCampaignId 是本工具动不了的一类（招商券等），**别把它当成「没有这张券」**——"
                "用它的 couponId(=ge batch_id) 与归因对账，招商券退出走 yx_campaign_*。",
    }


# ---------- 删除动作（营销工具，真写，confirm_token 门） ----------
def _delete_discount(client, sku_id, campaign_id, buid: int = 325) -> bool:
    data = _call(client, "jxzy_markettool_deleteDiscount",
                 {"env": "prod", "campaignId": str(campaign_id), "skuId": int(sku_id),
                  "type": 3, "buid": buid, "appCode": ""}, "POST")
    return bool(data and data.get("isSuccess") is True)


def _pm_erp_delete(client, promo_ids, site: int = 301) -> dict:
    ids = [int(x) for x in promo_ids if x]
    request = _json.dumps([{"site": int(site), "promoId": i} for i in ids], separators=(",", ":"))
    r = client.post(PM_ERP_DELETE_URL, data={"request": request, "siteId": str(site), "authority": "oswErp"})
    r.raise_for_status()
    j = r.json()
    batch = (j.get("valueMap") or {}).get("batchDeleteList") or []
    failed = [{"promoId": x.get("promoId"), "reason": x.get("reason", "")} for x in batch]
    return {"bizSuccess": bool(j.get("success") is True or j.get("code") == 0),
            "mess": j.get("mess") or j.get("subMessage") or "", "failed": failed}


def _delete_token(coupons, promos, site) -> str:
    return _confirm_token({"path": "markettool/delete",
                           "coupons": sorted(f"{c.get('skuId')}:{c.get('campaignId')}" for c in coupons),
                           "promos": sorted(str(p.get("promoId")) for p in promos), "site": str(site)})


def delete_dryrun(coupons: list, promos: list, site: str = "301") -> dict:
    """删券/删促 DRY-RUN：不发送，回显将删清单 + confirm_token。
    coupons:[{skuId,campaignId}]  promos:[{promoId}]。"""
    coupons = coupons or []
    promos = promos or []
    if not coupons and not promos:
        raise BlacklightError("没有要删的券或促销")
    return {"would_delete": False,
            "note": "DRY-RUN：未删除。真执行：相同参数 + confirm=confirm_token 调 markettool_delete。",
            "coupons": coupons, "promos": promos, "site": site,
            # 规划阶段就把禁碰清单亮出来，别等真执行才被拒（真执行也会再查一次，闸在原语上）
            "protected_blocked": _protected_block(coupons, promos, action="strip"),
            "confirm_token": _delete_token(coupons, promos, site)}




def _protected_block(coupons: list, promos: list, action: str = "strip") -> list:
    """真删/真摘之前过一遍禁碰清单。返回命中项（空=可以做）。

    ★★**为什么要在这里，而不是只在 plan_strip_except 里**（2026-08-24 审查）：
      `protected.py` 的用意是"已拍板保留的结构性决策不许被自动化误伤"，但此前**只有
      plan_strip_except 这一条规划路径**真正调用它。任何绕开规划、直接拼 coupons/promos
      调 delete() 的调用方（包括 MCP 工具 markettool_delete 本身），禁碰清单**完全不生效**。
      闸放在真执行原语上，才不依赖"调用方记得先规划"。
    结论若命中一律**拒绝执行**：清单是人工拍板的结论，改主意的正道是
      `osw_protected_remove` 把规则去掉（可追溯），不是在这里加 override 开关。
    """
    from blacklight.core import protected as _prot
    rows = list(coupons or []) + list(promos or [])
    hits = []
    for r in rows:
        sku = r.get("skuId") or r.get("sku_id") or ""
        cid = r.get("campaignId") or r.get("campaign_id")
        h = _prot.check(sku, coupons=[r] if r in (coupons or []) else None,
                        promos=[r] if r in (promos or []) else None,
                        campaign_ids=[cid] if cid else None, action=action)
        for x in h:
            hits.append({"skuId": str(sku), "campaignId": cid,
                         "name": r.get("name") or r.get("couponName"),
                         "rule": x.get("id"), "reason": x.get("reason")})
    return hits

@audited("markettool", "delete")
def delete(coupons: list, promos: list, site: str = "301", confirm: str = "") -> dict:
    """**真删**券/促（营销工具，不可逆）。需相同参数先 delete_dryrun 拿 confirm_token 再带 confirm。

    ★还会过一遍**禁碰清单**（`core/protected`，action=strip）：命中直接拒绝，
      要改主意请用 `osw_protected_remove` 删规则（可追溯），别在调用处绕过。"""
    coupons = coupons or []
    promos = promos or []
    if not coupons and not promos:
        raise BlacklightError("没有要删的券或促销")
    token = _delete_token(coupons, promos, site)
    if confirm != token:
        raise BlacklightError("删除需二次确认：先用相同参数跑 markettool_delete_dryrun 拿 confirm_token 再带 confirm。")
    blocked = _protected_block(coupons, promos, action="strip")
    if blocked:
        raise BlacklightError(
            "命中禁碰清单 %d 条，已拒绝删除：%s ——"
            "这些是人工拍板要保留的结构性决策（引流款/钩子券等）。"
            "确实要动请先 osw_protected_remove 去掉对应规则（留痕），别绕过闸。"
            % (len(blocked), blocked[:5]))
    out = {"executed": True, "confirm_token": token}
    with _client() as client:
        if coupons:
            ok = fail = 0
            failed = []
            for c in coupons:
                try:
                    if _delete_discount(client, c.get("skuId"), c.get("campaignId")):
                        ok += 1
                    else:
                        fail += 1
                        failed.append({**c, "reason": "isSuccess!=true"})
                except Exception as e:
                    fail += 1
                    failed.append({**c, "reason": str(e)[:60]})
                policy.pace("markettool.delete")     # 原来是裸 sleep(0.4)；规则见 core/policy
            out["couponResult"] = {"ok": ok, "fail": fail, "failed": failed}
        if promos:
            ids = list(dict.fromkeys(int(p.get("promoId")) for p in promos if p.get("promoId")))
            r = _pm_erp_delete(client, ids, site=int(site))
            failed_set = {str(f["promoId"]) for f in r["failed"]}
            out["promoResult"] = {"ok": sum(1 for i in ids if str(i) not in failed_set),
                                  "fail": len(failed_set), "failed": r["failed"], "mess": r["mess"]}
    return out


# ---------- 统一退出：分流计划 + 一键执行 ----------
def _plan(sku_id: str | int) -> dict:
    """构建单 SKU 的退出分流计划（含活动名→campaignId 映射解析）。"""
    q = query_sku(sku_id)
    sku = q["skuId"]
    mt_coupons = [{"skuId": sku, "campaignId": c["campaignId"], "name": c["name"],
                   "面额": c.get("面额"), "我承担": c.get("我承担"), "共补": c.get("共补")}
                  for c in q["coupons"]["deletable"]]
    mt_promos = [{"promoId": p["promoId"], "name": p["name"]} for p in q["promos"]["deletable"]]
    # 走 yx 退出报名：canOperate==0 券(带 campaignId) + 活动报名促(按活动名对 campaignId)
    campaigns, unresolved = {}, []
    for c in q["coupons"]["viaActivity"]:                    # canOp==0 券
        cid = c["campaignId"]
        campaigns.setdefault(cid, {"campaignId": cid, "name": c["name"], "via": "券"})
    for p in q["promos"]["viaActivity"]:                     # 活动报名促
        cid = yx_client.resolve_campaign_id(p.get("活动名"))
        if cid:
            campaigns.setdefault(cid, {"campaignId": cid, "name": p["name"], "via": "促"})
        else:
            unresolved.append({"promoId": p["promoId"], "活动名": p.get("活动名"), "name": p["name"]})
    subsidy = [{"blockId": r["blockId"], "applyId": r["applyId"], "pool": r["pool"]}
               for r in (q.get("subsidy") or [])]
    # Home 检测到但无 applyId 的国补（报名不是本账号做的）→ 本账号退不了，转人工/找原报名人
    manual = [{"type": "国补(非本账号报名,无applyId,退不了)", "promoId": u.get("promoId"), "name": u.get("name")}
              for u in (q.get("subsidy_unmatched") or [])]
    # ★券档位汇总：同一 campaignId 常有几十条同名分身(「…券05-1」「…券05-2」…)，逐条看没意义。
    # 按 campaignId 去重、按面额降序 —— 这才是摘券决策要的视图：
    # 打穿到手价的永远是**面额最大的那张**，摘掉它后**次大档会自动顶上**，所以要一档一档往下摘。
    tiers = {}
    for c in mt_coupons:
        t = tiers.setdefault(c["campaignId"], {"campaignId": c["campaignId"], "name": c["name"],
                                               "面额": c.get("面额"), "我承担": c.get("我承担"),
                                               "共补": c.get("共补"), "条数": 0})
        t["条数"] += 1
    tiers = sorted(tiers.values(), key=lambda x: -(x["面额"] or 0))
    joint = [t for t in tiers if t.get("共补")]
    return {"skuId": sku, "mt_coupons": mt_coupons, "mt_promos": mt_promos,
            "券档位": tiers,          # 去重按面额降序：摘券只看这个，别逐条看 mt_coupons
            "共补券": joint,          # ⚠️平台分担成本，摘掉=白丢补贴且不可逆，须单独判断
            "campaigns": list(campaigns.values()), "subsidy": subsidy,
            "unresolved": unresolved, "manual": manual, "summary": q["summary"]}


def _plan_token(plan) -> str:
    return _confirm_token({"path": "promo/withdraw_all", "sku": plan["skuId"],
                           "mt_coupons": sorted(f"{c['skuId']}:{c['campaignId']}" for c in plan["mt_coupons"]),
                           "mt_promos": sorted(str(p["promoId"]) for p in plan["mt_promos"]),
                           "campaigns": sorted(c["campaignId"] for c in plan["campaigns"]),
                           "subsidy": sorted(str(s["applyId"]) for s in plan.get("subsidy", []))})


def withdraw_plan(sku_id: str | int) -> dict:
    """单 SKU 全券促的**退出分流建议**（只读，不执行）。含活动名→campaignId 解析。"""
    plan = _plan(sku_id)
    return {**plan, "confirm_token": _plan_token(plan),
            "note": ("统一退出：mt_coupons/mt_promos 走营销工具删；campaigns 走退出活动报名(逐条 operateList 校验)；"
                     "unresolved=活动名没对到 campaignId(需你给)；商家不可退券已并入 campaigns 按其 campaignId 尝试退。"
                     "真执行：相同 sku + confirm=confirm_token 调 promo_withdraw。"
                     "★摘券看【券档位】(已去重按面额降序)，别逐条看 mt_coupons；券名会撞(同名不同 campaignId)，"
                     "以面额为准。摘掉最大档后次大档会自动顶上，须逐档往下摘并用 pricing_batch 回读。"
                     "⚠️【共补券】平台分担成本，摘掉=白丢补贴且不可逆，必须单独判断、勿混入批量摘券。")}


def ladder(sku_id: str | int) -> dict:
    """★**券档位阶梯 + 真实回血上限**——摘券前必看，否则会把收益高估好几倍。

    ## 为什么必须有（2026-08-10 实证）

    同类券**互斥、每单只生效面额最大的 1 张**。所以摘掉最高档，**次高档立刻顶上**：

      `10163019223327`（洗脸盆）挂着 **232 张券**、9 个档位。
      摘掉最高的 5.00 档（45 张）后 4.00 档（43 张）顶上
      ⇒ **实际回血只有 1.00 元/单，不是 5.00**；要真降到 1.00 档得摘 **226 张**。
      —— 这种深度的券池，**摘券根本不是可行手段**，该走涨价或找录券人退出圈选。

      对照 `10168121856759` 只有 2 张券：一张共补、一张 B补，**零可摘**。

    ## ★★「摘本档后顶上」只是上界估计，**两次实测都是 0 回血**（2026-08-14）

    本函数按「我担金额」排序推断次高档会顶上，但**平台择优规则比金额排序复杂**，
    实测顶上来的可能是**另一张同档共补券**，于是我担分文未变：

      `10130829981648` 同日两次：
        ① 摘 20 张全自担冲单券 → 预测受共补券压制回血 0 → 实测 **0**（毛利 −1.41 不变）✓
        ② 涨价解锁 5.00 档后摘 6 个 campaignId / 25 张
           → 预测顶上招商券 4.20、回血 **+0.80/单**
           → 实测 **0**：生效券从「Q3_全品类低活拉新4.01-4」换成
             「三类货_Q2_全品类低活拉新6.01-6」，**我担同样 3.00**，毛利 0.66 纹丝不动。

    ⇒ **`真实回血/单` 是上界，不是预测**。正收益档也可能一分钱回不来。
      动手前先摘**一个 campaignId** 回读 `query_pricing`，别按整档估算铺开。
    ⇒ 生效券始终是那张**共补拉新券**时，摘全自担券**碰不到它** —— 这类款摘券无解，
      走涨价（涨价会改变券门槛可达性，见 `osw.product._reprice_transmit`）或找录券人退出圈选。

    ## 输出

    每档给 `张数 / 可摘张数 / 受保护张数`，并算出**逐档下探的真实回血**
    （= 本档我担 − 摘完本档后能顶上的最高档我担）。

    ⚠️「可摘」= 非共补 **且** 不命中禁令清单（`protected.filter_plan(action="strip")`）。
      **平台担=0 不等于可摘**——B补券 100% 自担却不能摘（平台的钱走团长补贴，另一本账）。
    """
    from blacklight.core import protected as _prot

    sku = str(sku_id)
    _q = (query_sku(sku).get("coupons") or {})
    coupons = _q.get("deletable") or []
    # ★**摘不掉但会顶上来的券**也必须进档位计算：`noCampaignId`（招商券/机器平台券/广告智能券）
    #   本工具删不了，可它们照样参与「同类互斥、面额最大者生效」。只用 deletable 建档 ⇒
    #   摘光可摘档之后以为归零，实际是这类顶上来。实测 `10198041344136` 的 4 张招商券
    #   **我担 70%、面额最高 6.00**，比可摘档还贵 ⇒ 真实回血会被**高估到反向**。
    #   （2026-08-13 代码评审指出；此前 noCampaignId 刚从静默丢弃改为透出，但没接进决策函数。）
    floor_rows = [c for c in (_q.get("noCampaignId") or []) if (c.get("我承担") or 0) > 0]
    floor = round(max([float(c.get("我承担") or 0) for c in floor_rows], default=0.0), 2)
    floor_name = max(floor_rows, key=lambda c: c.get("我承担") or 0).get("name") if floor_rows else None

    # ★禁令必须按 **yx 的券名逐张**匹配，不能拿 osw 的券列表去过。
    #   osw 只显示**择优后生效的那张**，2026-08-10 实测 `10168121856759`：
    #   osw 看到的是「外卖cross…」，而真正在流的 B补券它根本没列 ⇒ 用 osw 过会把 B补券放行。
    tiers, sku_hits = {}, set()
    for c in coupons:
        amt = round(float(c.get("我承担") or 0), 2)
        t = tiers.setdefault(amt, {"我担": amt, "张数": 0, "共补": 0, "受保护": 0, "可摘": 0,
                                   "campaigns": set(), "示例": None, "保护规则": set()})
        t["张数"] += 1
        t["campaigns"].add(c.get("campaignId"))
        t["示例"] = t["示例"] or str(c.get("name"))
        hits = _prot.check(sku, coupons=[{"name": c.get("name")}], action="strip")
        if c.get("共补"):
            t["共补"] += 1
        elif hits:
            t["受保护"] += 1
            for h in hits:
                t["保护规则"].add(h.get("id"))
                sku_hits.add(h.get("id"))
        else:
            t["可摘"] += 1
    for t in tiers.values():
        t["保护规则"] = sorted(t["保护规则"]) or None
    sku_hits = sorted(sku_hits)

    order = sorted(tiers.values(), key=lambda x: -x["我担"])
    for i, t in enumerate(order):
        # 摘完本档（含以上各档的可摘部分）之后，还能顶上来的最高档
        # ★下限是 `floor`（摘不掉的 noCampaignId 券），不是 0——摘到底也降不到它以下
        rest = order[i + 1:]
        nxt = max(rest[0]["我担"] if rest else 0.0, floor)
        t["摘本档后顶上"] = nxt
        t["真实回血/单"] = round(t["我担"] - nxt, 2)
        t["campaigns"] = sorted(x for x in t["campaigns"] if x)
    strippable = sum(t["可摘"] for t in order)
    top = order[0] if order else None
    to_bottom = len(coupons) - order[-1]["张数"] if order else 0
    if strippable == 0:
        verdict = "**无券可摘**（全部共补或受禁令保护）⇒ 只能走涨价 / 找录券人退出圈选。"
    elif top and floor >= top["我担"]:
        verdict = ("**摘了也没用**：有摘不掉的券（%s，我担 %.2f）不低于最高可摘档 %.2f，"
                   "摘光可摘的它照样顶上 ⇒ 真实回血 0。走涨价 / 找录券人退出圈选。"
                   % (str(floor_name)[:28], floor, top["我担"]))
    elif to_bottom >= 20:
        verdict = ("**券池太深，摘券不是可行手段**：共 %d 张 / %d 档，摘最高档 %.2f（%d 张）"
                   "真实回血仅 **%.2f 元/单**（次档 %.2f 立刻顶上）；要降到最低档得摘 %d 张。"
                   "⇒ 走涨价 或 找录券人整批退出圈选。"
                   % (len(coupons), len(order), top["我担"], top["张数"],
                      top["真实回血/单"], top["摘本档后顶上"], to_bottom))
    elif top["真实回血/单"] < top["我担"] * 0.5:
        verdict = ("摘最高档 %.2f（%d 张）真实回血仅 **%.2f 元/单**（次档 %.2f 顶上）；"
                   "摘完 %d 张才能到底。收益是否值得，按单量折算后再定。"
                   % (top["我担"], top["张数"], top["真实回血/单"],
                      top["摘本档后顶上"], to_bottom))
    else:
        verdict = ("摘最高档 %.2f（%d 张）真实回血 **%.2f 元/单**，值得做。"
                   % (top["我担"], top["张数"], top["真实回血/单"]))
    return {
        "skuId": sku, "券总数": len(coupons), "档位数": len(order),
        "受禁令保护": sku_hits or None,
        # ★摘不掉的券构成的**回血下限**：摘到底也降不到它以下
        "摘不掉的最高我担": floor or None,
        "摘不掉的那张": floor_name,
        "可摘张数": strippable,
        "摘到底需摘张数": to_bottom,
        "档位": order,
        "★结论": verdict,
        "_纪律": "收益是**上限**；每摘一档必须 `query_pricing_batch` 回读实测，别按线性外推。",
    }


# ---------- 批量退券：保留清单语义（保住计划内的，踢掉计划外的） ----------
def _classify(sku: str, coupons: list, keep_names: list = None) -> list:
    """给每张券打「保留/待摘」及原因。keep_names=额外要保留的券名子串。"""
    from blacklight.core import protected as _prot
    keep_names = [k for k in (keep_names or []) if k]
    out = []
    for c in coupons:
        nm = str(c.get("name") or "")
        amt = round(float(c.get("我承担") or 0), 2)
        why = None
        if c.get("共补"):
            why = "共补券（平台分担，摘掉白丢补贴且不可逆）"
        elif any(k in nm for k in keep_names):
            why = "keep_names 指定保留"
        else:
            hits = _prot.check(sku, coupons=[{"name": nm}], action="strip")
            if hits:
                why = "禁令：" + ",".join(h.get("id") for h in hits)
        out.append({"skuId": sku, "campaignId": c.get("campaignId"), "name": nm,
                    "面额": c.get("面额"), "我担": amt,
                    "保留": bool(why), "原因": why})
    return out


def plan_strip_except(sku_ids: list, keep_names: list = None,
                      only_above_kept: bool = True, workers: int = 5) -> dict:
    """★**批量退券（保留清单语义）**：保住计划内的券，踢掉计划外的。

    ## 解决什么问题（用户 2026-08-10 提出）

    一个 SKU 上常同时挂着**品类新**（受禁令保护、会控单亏、**计划内**）和一大堆
    冲单券/会场券/复购券（**计划外**）。同类券互斥、每单只生效面额最大的一张，
    所以**只要有更高档的计划外券在，品类新就永远轮不上**，单亏也就失控。
    要的不是"摘某一张"，而是**把计划外的整批清掉、让品类新成为生效那张**。

    ## ★关键优化：只摘「我担 **>** 保留券」的部分

    两类券留着无害，摘了纯属白写：
      · **我担更低的**：永远赢不了（同类互斥只生效面额最大的一张）。
      · **我担相等的**：赢了也是同样的成本。⚠️注意是**严格 `>` 不是 `≥`**——
        实测 `10186130760892` 的 4.00 档有 41 张全自担券，与保留的品类新
        （我担同为 4.00）成本完全一样，摘掉它们 **Δ=0**。第一版用 `≥` 会白写 41 次（48%）。
        （面额可能不同 ⇒ 顾客看到的减免不同，但**我担一样、毛利一样**，止亏视角无差别。）

    ## ★★再按 campaignId 去重

    `deleteDiscount` 的参数是 **(skuId, campaignId)**，一个 campaign 删一次就够；
    而 yx 返回的是**券实例**级列表，同一 campaign 常有几十张（券名带 `-1/-2/-17` 后缀）。
    实测 45 张券只对应 **7 个 campaignId** —— 不去重就是重复写 38 次。

    两项叠加，`10186130760892`：**229 张待摘 → 实际只需 7 次写（省 97%）**。
    `only_above_kept=False` 可关掉档位优化（全摘，一般没必要）；去重恒定生效。

    ## 输出

    每个 SKU 给：保留券（及原因）/ 待摘券 / **预计生效券** / 当前我担减免 → 预计我担减免。
    ⚠️没有任何保留券的 SKU 会被单独列进 `无保留券`，**默认不放进执行清单**——
      全摘会把到手价抬回裸价，那是另一个决策（涨价级别的动作），必须显式确认。

    只读，不发任何写请求。执行走 `strip_except_dryrun` → `strip_except`。
    """
    ids = [str(s) for s in sku_ids]

    def _q(s):                       # pmap 要求 fn 自己吞异常
        try:
            return {"skuId": s, "data": query_sku(s)}
        except Exception as e:
            return {"skuId": s, "error": str(e)[:80]}

    res = pmap(_q, ids, workers=workers)
    plans, no_keep, errs, no_coupon = [], [], [], []
    for item in res:
        if item.get("error"):
            errs.append(item)
            continue
        sku, d = item["skuId"], item["data"]
        coupons = (d.get("coupons") or {}).get("deletable") or []
        summ = d.get("summary") or {}
        cls = _classify(sku, coupons, keep_names)
        keep = [c for c in cls if c["保留"]]
        drop = [c for c in cls if not c["保留"]]
        cur = max([c["我担"] for c in cls], default=0.0)
        # ★「0 张券」有两种截然不同的原因，别混为一谈（2026-08-10 我第一版全判成取数失败，错了）：
        #   (a) 这款**本来就没有券**，亏在国补/促销上 ⇒ 摘券工具管不了，该走
        #       `yx_subsidy_withdraw` / `yx_promo_withdraw`。94 款里绝大多数是这种。
        #   (b) `query_sku` **静默返回空**（实测 `10163019223327` 前一刻还有 232 张）⇒ 真取数失败。
        #   判据：还有没有别的减免。全是 0 才是可疑，有促/有国补就是真没券。
        if not cls:
            other = {"促销": int(summ.get("可删促") or 0) + int(summ.get("活动类促") or 0),
                     "国补报名": int(summ.get("国补报名") or 0)}
            if any(other.values()):
                no_coupon.append({"skuId": sku, **other,
                                  "说明": "本来就没有券，亏在促销/国补上 ⇒ 摘券工具管不了，"
                                          "走 yx_promo_withdraw / yx_subsidy_withdraw"})
            else:
                errs.append({"skuId": sku,
                             "error": "券促国补全为 0（可疑：query_sku 可能静默返回空），已跳过"})
            continue
        if not keep:
            no_keep.append({"skuId": sku, "券总数": len(cls), "当前我担减免": cur,
                            "说明": "没有任何保留券 —— 全摘会把到手价抬回裸价，需单独决策"})
            continue
        keep_max = max(c["我担"] for c in keep)
        # ★严格 `>`：我担相等的券摘了 Δ=0（赢了也是同样成本），是纯白写。
        todo = [c for c in drop if c["我担"] > keep_max + 1e-9] if only_above_kept else drop
        # ★★按 campaignId 去重：`deleteDiscount` 的参数是 **(skuId, campaignId)**，
        #   一个 campaign 删一次就够。而 yx 返回的是**券实例**级列表，同一 campaign 常有几十张
        #   （券名带 -1/-2/-17 后缀）。实测 45 张券只对应 **7 个 campaignId** ⇒ 不去重会重复写 38 次。
        seen, dedup = set(), []
        for c in sorted(todo, key=lambda x: -x["我担"]):
            k = (c["skuId"], c["campaignId"])
            if k in seen:
                continue
            seen.add(k)
            dedup.append(c)
        todo = dedup
        kept_win = max(keep, key=lambda c: c["我担"])
        plans.append({
            "skuId": sku, "券总数": len(cls),
            "保留": len(keep), "待摘(全部)": len(drop), "★本次要摘": len(todo),
            "省下的写操作": len(drop) - len(todo),
            "当前我担减免": cur, "预计我担减免": keep_max,
            "预计Δ/单": round(cur - keep_max, 2),
            "预计生效券": {"campaignId": kept_win["campaignId"], "name": kept_win["name"],
                           "我担": kept_win["我担"], "原因": kept_win["原因"]},
            "保留清单": [{k: c[k] for k in ("campaignId", "name", "我担", "原因")} for c in keep],
            "摘除清单": [{"skuId": c["skuId"], "campaignId": c["campaignId"],
                          "name": c["name"], "我担": c["我担"]} for c in todo],
        })
    # ★Δ≤0 的不放进执行清单：保留券已经是最高档，摘掉下面那些一分钱不回血。
    #   实测 `10163019223323` 当前我担 3.00 == 保留券 3.00，若不拦会白写 82 次。
    useless = [p for p in plans if p["预计Δ/单"] <= 0]
    plans = [p for p in plans if p["预计Δ/单"] > 0]
    for p in useless:
        p["说明"] = "保留券已是最高档，摘掉低档券零回血（本可白写 %d 次）" % p["★本次要摘"]
        p["摘除清单"] = []
        p["★本次要摘"] = 0

    plans.sort(key=lambda p: -p["预计Δ/单"])
    writes = sum(p["★本次要摘"] for p in plans)
    return {
        "SKU数": len(plans), "无保留券": no_keep,
        "无券(亏在促销/国补)": no_coupon or None,
        "无收益(已剔除)": useless or None,
        "取数失败": errs or None,          # 静默丢款是最坏的，宁可显式列出来
        "写操作总数": writes,
        "预计耗时": "%.1f 分钟（串行 0.4s/条，实际走 pmap_batch 并发会更快）" % (writes * 0.4 / 60),
        "省下的写操作": sum(p["省下的写操作"] for p in plans),
        "plans": plans,
        "_纪律": ["1) 收益是**上限**：摘完可能有别的券/促销补位，必须 query_pricing_batch 回读实测",
                  "2) 先探针 1 款回读，再批量（`sku_ids` 传 1 个即可）",
                  "3) 摘券会抬高到手价 ⇒ 可能触发生效中百补被平台自动删，先查重叠"],
    }


def strip_except_dryrun(sku_ids: list, keep_names: list = None,
                        only_above_kept: bool = True) -> dict:
    """批量退券 DRY-RUN：出完整计划 + confirm_token，不执行。"""
    plan = plan_strip_except(sku_ids, keep_names, only_above_kept)
    return {**plan, "would_delete": False,
            "confirm_token": _confirm_token({
                "path": "markettool/strip_except",
                "items": sorted("%s:%s" % (c["skuId"], c["campaignId"])
                                for p in plan["plans"] for c in p["摘除清单"])}),
            "note": "DRY-RUN：未删除。真执行：相同参数 + confirm=confirm_token 调 strip_except。"}


@audited("markettool", "strip_except")
def strip_except(sku_ids: list, keep_names: list = None, only_above_kept: bool = True,
                 confirm: str = "", workers: int = 4, checkpoint: str = None) -> dict:
    """**真执行**批量退券（保留清单语义）。需相同参数先 `strip_except_dryrun` 拿 confirm_token。

    走 `pmap_batch`（探针/ETA/熔断/断点）——这是长批量的标配，别裸用 pmap：
    单款就可能上百次写，全挂了也会"跑完"。给 `checkpoint`（.jsonl）可断点续跑。

    ⚠️执行完**必须回读** `osw.margin.query_pricing_batch` 比对「我担减免」是否落到
      `预计我担减免`；对不上说明有补位，别信回执就收工。
    """
    plan = plan_strip_except(sku_ids, keep_names, only_above_kept)
    items = [c for p in plan["plans"] for c in p["摘除清单"]]
    if not items:
        return {"executed": False, "reason": "没有要摘的券", **plan}
    token = _confirm_token({"path": "markettool/strip_except",
                            "items": sorted("%s:%s" % (c["skuId"], c["campaignId"]) for c in items)})
    if confirm != token:
        raise BlacklightError("批量退券需二次确认：先用相同参数跑 strip_except_dryrun 拿 confirm_token 再带 confirm。")

    def _one(c):
        try:
            with _client() as cl:
                ok = _delete_discount(cl, c["skuId"], c["campaignId"])
            return {**c, "ok": bool(ok), "error": None if ok else "isSuccess!=true"}
        except Exception as e:
            return {**c, "ok": False, "error": str(e)[:80]}

    r = pmap_batch(_one, items, workers=workers, probe=2, checkpoint=checkpoint,
                   key=lambda x: "%s:%s" % (x.get("skuId"), x.get("campaignId")),
                   is_error=lambda x: not x.get("ok"), label="strip_except")
    rows = r.get("rows") or []
    ok = [x for x in rows if x.get("ok")]
    bad = [x for x in rows if not x.get("ok")]
    return {"executed": True, "confirm_token": token,
            "计划": {k: plan[k] for k in ("SKU数", "写操作总数", "省下的写操作")},
            "成功": len(ok), "失败": len(bad), "中止": r.get("aborted"),
            "中止原因": r.get("reason"), "耗时秒": r.get("seconds"),
            "失败明细": bad[:20],
            "★下一步": "立刻回读 `osw_pricing_batch`，比对「我担减免」是否落到各 SKU 的 `预计我担减免`；"
                        "有偏差就是补位（铁律 6），按实测重算别按线性外推。"}


def withdraw_all_dryrun(sku_id: str | int) -> dict:
    """统一退出 DRY-RUN：给出完整分流计划 + confirm_token，不执行。"""
    return withdraw_plan(sku_id)


@audited("promo", "withdraw_all")
def withdraw_all(sku_id: str | int, confirm: str = "") -> dict:
    """
    **统一退出真执行**：营销工具删可删券促 + 逐个退出活动报名（operateList 逐条可退，跳过带原因）。
    需相同 sku 先 withdraw_all_dryrun 拿 confirm_token 再带 confirm。unresolved 的活动需你补 campaignId 单独退。
    """
    plan = _plan(sku_id)
    token = _plan_token(plan)
    if confirm != token:
        raise BlacklightError("统一退出需二次确认：先用相同 sku 跑 promo_withdraw_dryrun 拿 confirm_token 再带 confirm。")
    out = {"executed": True, "skuId": plan["skuId"], "confirm_token": token}
    # 1) 营销工具删
    if plan["mt_coupons"] or plan["mt_promos"]:
        out["markettool"] = delete(plan["mt_coupons"], plan["mt_promos"],
                                   confirm=_delete_token(plan["mt_coupons"], plan["mt_promos"], "301"))
    # 2) 退出活动报名（逐活动，按 SKU；operateList 逐条可退，不可退跳过带原因）
    camp_res = []
    for cp in plan["campaigns"]:
        cid, sku = cp["campaignId"], plan["skuId"]
        try:
            dr = yx_client.withdraw_dryrun(cid, sku_ids=[sku])
            if not dr.get("request"):
                camp_res.append({"campaignId": cid, "name": cp["name"], "result": "跳过",
                                 "reason": (dr.get("eligibility", {}).get("skipped") or dr.get("warning"))})
                continue
            r = yx_client.withdraw(cid, sku_ids=[sku], confirm=dr["confirm_token"])
            camp_res.append({"campaignId": cid, "name": cp["name"], "success": r.get("success"),
                             "msg": (r.get("response") or {}).get("message")})
        except Exception as e:
            camp_res.append({"campaignId": cid, "name": cp["name"], "error": str(e)[:80]})
    out["campaigns"] = camp_res
    # 3) 退出国补报名（按 applyId，走 subsidy withdraw）
    sub_res = []
    for sb in plan.get("subsidy", []):
        aid = sb["applyId"]
        try:
            tok = yx_subsidy.withdraw_dryrun(aid)["confirm_token"]
            r = yx_subsidy.withdraw(aid, confirm=tok)
            sub_res.append({"applyId": aid, "pool": sb.get("pool"), "success": r.get("success"),
                            "msg": (r.get("response") or {}).get("message")})
        except Exception as e:
            sub_res.append({"applyId": aid, "pool": sb.get("pool"), "error": str(e)[:80]})
    out["subsidy"] = sub_res
    out["unresolved"] = plan["unresolved"]
    out["manual"] = plan.get("manual", [])   # 未知池国补等，无法自动退，需人工
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="yx markettool（京喜营销工具 查/删券促）")
    ap.add_argument("--query", metavar="SKU")
    a = ap.parse_args()
    if a.query:
        print(_json.dumps(query_sku(a.query), ensure_ascii=False, indent=2, default=str))
