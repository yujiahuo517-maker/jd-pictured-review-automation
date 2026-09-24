"""
yx-mcp 场域：bybt = **超级补贴（原百亿补贴）竞价活动**。网关 `bid-activity.jd.com`。

要点（扒自「百亿补贴报名助手」扩展 v3.0.2 + 实证）：
- **竞价(bidding)模型**：报名要提交一个**报名价 bidPrice**，平台按价算到手价（不是简单加入活动）。
- 鉴权：**yx 登录态即可**（已实证 yx cookie 授权 bid-activity.jd.com），**不签名**（POST JSON + cookie）。
- 活动标识：`areaId`(收品区，默认 319901) + `activityId`（已报名列表需要；来自活动页 URL）。
- **本模块当前仅查询**（可报/已报/试算/预估到手价）。**报名(saveApply)/退出**在扩展里是纯 UI 点击、无可调 API，
  待在超级补贴页抓到 `saveApply` 与退出请求后再补（报名价按公式算，公式待定）。
"""
from __future__ import annotations

import json as _json
import os
from typing import Optional

from blacklight.core import auth as jd_auth, canon_num, paged_scan
from blacklight.core import paths as _paths
from blacklight.osw import pricing as osw_pricing
from blacklight.core import (BlacklightError, confirm_token as _confirm_token, make_client, json_post, audited,
                     gateway, scene_cfg, REDIRECT_CODES, pmap, pace, THROTTLE_RULES as _POLICY_RULES)

BID_BASE = gateway("bybt")
DEFAULT_AREA_ID = scene_cfg("bybt").get("default_area_id", 319901)
# ★★**判占用别只看 promoCreateStatus**（2026-08-06 实证推翻旧口径）
#
# 旧注释说「promoCreateStatus 1=审核中 2=生效中，其余=已失效/退出」。实测本人名下 1310 条已报名记录：
#     promoCreateStatus=5 → 479 条    =6 → 249 条    =2 → 110 条    =7 → 3 条    =3 → 2 条    空 → 467 条
# 即 **91% 落在白名单 {1,2} 之外**。按旧口径判「占用」会得到 110 款，而真实占用是 226 款
# ——差出来的 116 款会被当成"可报"**重复报名**。
#
# 真正可靠的信号是 `activityStatus`（活动层）：
#     1 = 促销进行中   0 = 已通过待开始/未生效   -1 = 已失效/结束
# 交叉验证：activityStatus=1 的 381 条 promoBeginTime 都在过去且 activityProgressStatus=2(通过)；
#           activityStatus=-1 的 467 条对应 activityProgressStatus=3(驳回)。
#
# ∴ 占用 = activityStatus ∈ {0,1}（在跑或待跑），失效/驳回 = -1。
# 保留 REGISTERED_STATUS 仅为向后兼容；**新代码请用 `is_occupied()`**。
# ★报名写操作的最小间隔（秒）——**单一事实源在 `core.policy.THROTTLE_RULES['bybt.apply']`**，
#   这里只做别名，别在两处各写一个数（那正是「文档说 1、默认 8」那类漂移的来源）。
#   依据与实证见 policy 模块 docstring。
MIN_APPLY_INTERVAL = _POLICY_RULES["bybt.apply"]["min_interval"]

REGISTERED_STATUS = {1, 2}                 # 兼容别名，勿新用
OCCUPIED_ACTIVITY_STATUS = {0, 1}          # 0=待开始 1=进行中 → 都占坑
_KNOWN_PROMO_CREATE_STATUS = {1, 2, 3, 5, 6, 7, None}


def is_occupied(item: dict) -> bool:
    """该报名记录是否**占着坑**（占坑 ⇒ 不能再报，判重要算进去）。

    以 `activityStatus` 为准；缺失时退回 `promoCreateStatus ∈ REGISTERED_STATUS`（旧口径）。"""
    a = item.get("activityStatus")
    if a is not None:
        return a in OCCUPIED_ACTIVITY_STATUS
    return item.get("promoCreateStatus") in REGISTERED_STATUS


def audit_status_drift(items: list) -> dict:
    """**状态漂移哨兵**：统计有多少记录落在已知状态之外。

    平台新增一个占用态而白名单没跟上时，判重会**静默漏判 ⇒ 重复报名**，全程零报错。
    这条就是用来把"静默"变成"有声"的。unknown_ratio 偏高时别急着批量报名。"""
    from collections import Counter
    ac = Counter(i.get("activityStatus") for i in items)
    pc = Counter(i.get("promoCreateStatus") for i in items)
    unknown = sum(n for k, n in pc.items() if k not in _KNOWN_PROMO_CREATE_STATUS)
    unk_act = sum(n for k, n in ac.items() if k not in (0, 1, -1, None))
    total = len(items) or 1
    return {
        "总数": len(items),
        "activityStatus分布": dict(ac),
        "promoCreateStatus分布": dict(pc),
        "未知activityStatus占比%": round(unk_act / total * 100, 1),
        "未知promoCreateStatus占比%": round(unknown / total * 100, 1),
        "告警": ("★有未知状态值，占用判定可能漏判→会重复报名，先人工核对状态含义"
                 if (unk_act or unknown) else "状态值均在已知集合内"),
    }


def _client(timeout: float = 20.0):
    return jd_auth.session_client(timeout)


def _post(client, path: str, body: dict = None) -> dict:
    return json_post(client, BID_BASE, path, body)


def _price_amount(price) -> Optional[float]:
    """取京东价字段的元值：服务端给的是 `{amount,cent,...}` 结构或裸数字，取 `amount`。

    ★**不是分→元换算**（不除 100）。原名 `_yuan` 与 osw 的两个 `_fen2yuan` 形似，
    容易被当成同类互相复制，2026-08-05 改名以示区别。"""
    if isinstance(price, dict):
        return price.get("amount")
    return price


# ---------- 可报商品列表 ----------
def list_eligible(area_id: int = DEFAULT_AREA_ID, page: int = 1, page_size: int = 200,
                  sku_name: str = "", query_item: dict = None) -> dict:
    """可报商品列表（POST /apply/bid/new/ware/list）。返回 {totalCount, totalPage, items}。
    item 含 skuId/name/jdPrice/suggestPrice(建议价)/categoryNames/status/cannotApplyReason。
    sku_name: 按标题模糊过滤（queryItem.skuName）。⚠️ 此接口**不返回 biddingId**（报名需另取）。"""
    qi = dict(query_item or {})
    if sku_name:
        qi["skuName"] = sku_name
    body = {"page": page, "pageSize": page_size, "pageSelectionType": 1,
            "queryItem": qi, "queryQuaOpenCidFlag": True, "areaId": int(area_id)}
    with _client() as client:
        d = _post(client, "/apply/bid/new/ware/list", body)
    items = [{"skuId": it.get("skuId"), "name": it.get("name"),
              "jdPrice": _price_amount(it.get("jdPrice")), "suggestPrice": it.get("suggestPrice"),
              "suggestPriceCondition": it.get("suggestPriceCondition"),
              "categoryNames": it.get("categoryNames"), "status": it.get("status"),
              "cannotApplyReason": it.get("cannotApplyReason")}
             for it in (d.get("dataList") or [])]
    return {"totalCount": d.get("totalCount"), "totalPage": d.get("totalPage"), "items": items}


# ---------- 已报名列表 ----------
def get_applied(activity_id: str, area_id: int = DEFAULT_AREA_ID, page: int = 1,
                page_size: int = 100, only_active: bool = True,
                activity_progress_status=None) -> dict:
    """已报名列表（POST /apply/bid/applied/page）。需 activityId(来自活动页URL)。
    only_active=True 只留 promoCreateStatus∈REGISTERED_STATUS(生效态)。
    ⚠️`activity_progress_status`=活动进度相过滤：**默认 None=全部相**(2026-07-20修)。
      **切勿硬设 2**——新报名先进 **progress=0(未开始，竞价场次还没启动)**，设 2 会整批漏掉→去重不准、误当可报重报。

    ★**progress / applyStatus 语义**（2026-08-04 用户纠正 + 2386 条实证，两字段 1:1）：
      **0=未开始 / 2=通过(进入竞价促销相) / 3=驳回 / 9=已结束**。
      旧注释把 progress=3 写成"审核相"是**错的**，它是**审核驳回**——驳回原因不在本接口
      (`rejectReason`/`promoFailReason` 都空)，要调 `flow_log()`。"""
    if not str(activity_id).strip():
        raise BlacklightError("需要 activity_id（来自超级补贴活动页 URL 的 activityId 参数）")
    body = {"activityId": str(activity_id), "areaId": int(area_id), "page": page,
            "pageSize": page_size, "sortedField": {"created": "desc"},
            "activityProgressStatus": activity_progress_status, "winStatus": "", "channelType": None,
            "applyModel": None, "haveAppealed": None}
    with _client() as client:
        d = _post(client, "/apply/bid/applied/page", body)
    items = d.get("items") or []
    drift = audit_status_drift(items)          # 状态漂移哨兵：白名单跟不上会静默漏判→重复报名
    if only_active:
        # ★用 is_occupied（以 activityStatus 为准），不是旧的 promoCreateStatus 白名单：
        #   实测 91% 的记录落在 {1,2} 之外，旧口径会把 226 款占用判成 110 款。
        items = [it for it in items if is_occupied(it)]
    out = {"totalCount": d.get("totalCount"), "items": items, "状态分布": drift}
    if drift.get("未知promoCreateStatus占比%", 0) > 30:
        out["⚠️状态漂移"] = drift["告警"]
    return out


# 报名占用态（**promoCreateStatus** 层，与 applyStatus 分开看）：
#   None/1=审核中(占用) · 2=生效中 · **5=平台下线**(未出单/到手价涨过建议价→可重报) · 6=退出或驳回(可重报)
#   ⚠️5 **不是"待审核"**（2026-08-04 纠正）：这类记录 `promoFailReason` 直接带原因，
#     如「…未产生超补订单，不符合平台规则，因此做下线处理」。实测占全量 47%，是最大死因。
# 只有 6=退出/驳回 才可重报。故用「排除态」判定更稳(别用白名单会漏 None/新状态码)。
QUIT_STATUS = {6}       # 退出/驳回 → 可重报
OFFLINE_STATUS = {5}    # 平台下线(未出单/到手价涨过建议价) → 促销已不存在，可重报
PENDING_STATUS = {None, 1}   # 审核中(新报名先进审核相 promoCreateStatus=None) → 占用
LIVE_STATUS = 2         # 生效中 —— 还要 promoEndTime 未过期才算真占用


def _promo_alive(it: dict, now=None) -> bool:
    """该条报名记录**当前是否真的占着坑**。

    ⚠️2026-07-27 实证纠正：旧规则「非退出(status≠6)即占用」**把死记录当占用**——
    232 款「已报名」里 **76 款既无生效促销也无在审记录**（全是被下线/已过期/已退出），本可重报却被跳过。
    百补促销会被平台自动下线（24h 未出单 / 实时到手价涨过报名建议价），下线后 status=5，
    生效记录(status=2)也有 promoEndTime 会到期 —— 这两类都必须释放。
    未知状态码保持保守判占用（宁可漏报，不可重报）。见 [[bybt-promo-lifecycle]]。
    """
    # ★★**驳回 = 坑已释放**（2026-08-06 实证补）：报名被拒 ⇒ 促销压根没创建 ⇒
    #   `promoCreateStatus` 为 None，会一路落到函数末尾的「未知状态码→保守判占用」，
    #   于是**驳回的款被永久锁在可报池外**。实证：本人名下 83 款剩余可报里，
    #   38 款（36 驳回 + 2 已结束）被误判占坑，plan_bulk_enroll 的 A 桶因此只剩 1 款。
    #   驳回不是"未知状态"，它语义明确：applyStatus / activityProgressStatus == 3。
    #   （两字段 1:1：0=未开始 2=通过 3=驳回 9=已结束）
    if it.get("applyStatus") == 3 or it.get("activityProgressStatus") == 3:
        return False                                  # 驳回 → 坑释放，可重报
    if it.get("activityProgressStatus") == 9:
        return False                                  # 活动已结束 → 坑释放

    st = it.get("promoCreateStatus")
    if st in QUIT_STATUS or st in OFFLINE_STATUS:
        return False                                  # 已退出 / 已被平台下线 → 坑已释放
    if st in PENDING_STATUS:
        return True                                   # 审核中 → 占用
    if st == LIVE_STATUS:
        end = it.get("promoEndTime")
        if not end:
            return True                               # 生效但无结束时间 → 当占用
        try:
            import datetime as _dt
            now = now or _dt.datetime.now()
            return _dt.datetime.strptime(end, "%Y-%m-%d %H:%M:%S") > now
        except Exception:
            return True                               # 解析不了 → 保守判占用
    return True                                       # 未知状态码 → 保守判占用


def applied_sku_ids(activity_id: str, area_id: int = DEFAULT_AREA_ID, page_size: int = 200,
                    legacy_any_record: bool = False) -> set:
    """**当前真正占坑**的 skuId 集合（翻页取全）——报名前权威去重。

    占用 = 有「审核中」或「生效中且未过期」的记录；**已退出/已被平台下线/已过期的死记录一律释放**。
    一个 SKU 常有多条历史记录（实测 232 款背后 1045 条，平均 4.5 次），只要**任一条**仍存活即算占用。
    legacy_any_record=True 恢复旧口径(非退出即占用)，仅用于对拍排查。
    ⚠️必须 only_active=False 取数：get_applied 的 only_active 默认 True 会按 {1,2} 过滤掉每页大部分行，
    再用 len(items)<page_size 判终止会**提前退出**（实测 totalCount 2232 只拉到 16 条）。故用 totalCount 判终止。
    """
    ids, page = set(), 1
    while True:
        r = get_applied(activity_id, area_id, page=page, page_size=page_size, only_active=False)
        for it in r["items"]:
            if it.get("skuId") is None:
                continue
            alive = (it.get("promoCreateStatus") not in QUIT_STATUS) if legacy_any_record else _promo_alive(it)
            if alive:
                ids.add(str(it.get("skuId")))
        if page * page_size >= (r.get("totalCount") or 0) or not r["items"]:
            break
        page += 1
    return ids


# 生效中的唯一状态组合（2026-08-03 实证反解 2386 条记录）：活动进度2 + 中标1 + 促销2，
# 且该组合 107/107 全部落在 promo 时间窗内。**winStatus=1 才是中标**，=2 是未中标/待定。
LIVE_COMBO = {"progress": 2, "win": 1, "promo": 2}


def _promo_live(it: dict, now=None) -> bool:
    """真·生效中：三字段命中 LIVE_COMBO + promoId 非空 + 当前落在 promo 时间窗内。"""
    if (it.get("activityProgressStatus") != LIVE_COMBO["progress"]
            or it.get("winStatus") != LIVE_COMBO["win"]
            or it.get("promoCreateStatus") != LIVE_COMBO["promo"]
            or not it.get("promoId")):
        return False
    import datetime as _dt
    now = now or _dt.datetime.now()

    def _pt(s):
        try:
            return _dt.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    b, e = _pt(it.get("promoBeginTime")), _pt(it.get("promoEndTime"))
    return (b is None or b <= now) and (e is None or e >= now)


def live_sku_ids(activity_id: str, area_id: int = DEFAULT_AREA_ID, page_size: int = 200) -> set:
    """**真正生效中**的 skuId 集合 —— 与 `applied_sku_ids`（"占坑"，含审核中/竞价中）区分开。

    ★判生效必须**三字段合看**：`activityProgressStatus`(活动进度) × `winStatus`(中标) × `promoCreateStatus`(促销)，
      再加 promoId + 时间窗硬校验。单看 promoCreateStatus 会误判——实测 `=5`(待审核) 有 1076 条、其中 135 条
      有 promoId 且在促销窗内；`=6` 的 342 条则全部已出窗。
    实测差异：占坑 400 个，真生效仅 **107** 个。别把"占坑"当成"在跑"。
    未中标/未建促销的那批后续会**释放回可报池** ⇒ 可报 A 桶偏小时先查这个数，隔几天复查会变大。"""
    ids, page = set(), 1
    while True:
        r = get_applied(activity_id, area_id, page=page, page_size=page_size, only_active=False)
        for it in r["items"]:
            if it.get("skuId") is not None and _promo_live(it):
                ids.add(str(it.get("skuId")))
        if page * page_size >= (r.get("totalCount") or 0) or not r["items"]:
            break
        page += 1
    return ids


def verify_enrolled(activity_id: str, sku_ids: list, area_id: int = DEFAULT_AREA_ID,
                    with_reasons: bool = True, page_size: int = 200) -> dict:
    """★**T+1 回执**：昨天报的这批，今天到底活着几个。

    `enroll_bulk` 的 `verified.newly_reported` 只验到**「占坑」**那一层（提交完立刻查已报名列表前后差）。
    但百补的已知问题是 **报名 ≠ 生效**：`live_sku_ids` 实测**占坑 400 个、真生效仅 107 个**，
    且 **24h 未出单会被平台自动下线**。⇒ 提交当天的回执**答不了「这批到底跑起来没有」**，
    必须隔天（或隔几天）用本函数复检。

    三分桶（口径见 `live_sku_ids` / `applied_sku_ids` docstring，别自己按 promoCreateStatus 判）：

        生效中   在 live_sku_ids   —— 真的在跑
        占坑未生效 在 applied 但不在 live —— 审核中/竞价中，**再等**（可能中标也可能释放）
        已掉出   两个都不在        —— 驳回/退出/过期，`with_reasons=True` 会补驳回原因

    ⚠️未中标/未建促销的那批**会释放回可报池** ⇒ 「已掉出」不等于永久失败，
      隔几天重报常常能成（也是 A 桶会变大的原因）。
    ⚠️自带 `audit_status_drift` 哨兵：平台新增占用态而白名单没跟上时，
      判重会静默漏判 ⇒ `unknown_ratio` 偏高时**别急着据此重报**。
    """
    wantset = {str(s).strip() for s in (sku_ids or []) if str(s).strip()}
    if not wantset:
        raise BlacklightError("sku_ids 不能为空")

    # 一次翻页拿全量记录，供三个用途复用（分桶 / 漂移哨兵 / 驳回定位）
    items, page = [], 1
    while True:
        r = get_applied(activity_id, area_id, page=page, page_size=page_size, only_active=False)
        items.extend(r["items"])
        if page * page_size >= (r.get("totalCount") or 0) or not r["items"]:
            break
        page += 1

    live = {str(it.get("skuId")) for it in items if it.get("skuId") is not None and _promo_live(it)}
    # ★占坑判据必须用 `_promo_alive`（= applied_sku_ids 的口径），**不能用 is_occupied**。
    #   两者在「平台自动下线」(promoCreateStatus=5) 上分叉：is_occupied 认 activityStatus，
    #   会把已下线的判成"还占着坑"。而 24h 未出单自动下线**正是本函数要抓的头号场景** ⇒
    #   用 is_occupied 会把它分进「占坑未生效(再等)」，同时 plan_bulk_enroll 却认为它可重报，
    #   两个工具对同一 SKU 给出相反建议。
    #   2026-08-13 实测分叉 508 款（is_occupied 665 vs _promo_alive 159），
    #   当天报的 71 款里 22 款受影响。（代码评审发现，已修）
    occupied = {str(it.get("skuId")) for it in items
                if it.get("skuId") is not None and _promo_alive(it)}

    live_hit = sorted(wantset & live)
    pending = sorted((wantset & occupied) - live)          # 占坑但没生效
    dropped = sorted(wantset - occupied - live)            # 两个桶都不在

    out = {
        "activityId": str(activity_id), "areaId": int(area_id),
        "送检": len(wantset),                               # ★去重后，保证三桶相加 == 送检
        "生效中": len(live_hit), "占坑未生效": len(pending), "已掉出": len(dropped),
        "生效率%": round(100.0 * len(live_hit) / len(wantset), 1),
        "生效中_skus": live_hit, "占坑未生效_skus": pending, "已掉出_skus": dropped,
        "哨兵_状态漂移": audit_status_drift(items),
        "_口径": "生效=_promo_live(三字段合看+promoId+时间窗)；占坑=_promo_alive"
                 "(与 applied_sku_ids 同口径，**平台下线/驳回/过期一律算释放**)；"
                 "**别用 promoCreateStatus 单判、也别用 is_occupied**(它认 activityStatus，"
                 "会把已下线的判成占坑)。",
        "_note": "★『已掉出』不等于永久失败——未中标/未建促销的会释放回可报池，隔几天重报常能成。",
    }
    if with_reasons and dropped:
        try:
            out["驳回原因"] = reject_reasons(activity_id, sku_ids=dropped, area_id=area_id)
        except Exception as e:                              # 驳回原因是增值信息，取不到不该让整体失败
            out["驳回原因"] = []
            out["驳回原因_err"] = "%s: %s" % (type(e).__name__, str(e)[:80])
    return out


# ---------- 流程日志 / 驳回原因 ----------
def flow_log(apply_id, apply_ware_id, area_id: int = DEFAULT_AREA_ID, refer: int = 1) -> dict:
    """报名单**流程日志**（GET /apply/bid/flow/log）——**驳回原因唯一来源**。

    ★为什么必须有它：`get_applied` 里 `rejectReason`/`promoFailReason` 对审核驳回**都是空的**，
    页面上是「活动进度」列旁一个图标、鼠标悬浮才显示原因（记录里 `showEyeFlag=True` 即它）。
    2026-08-04 由用户抓包定位。cookie-only、不签名，与本模块其他端点一致。

    apply_id/apply_ware_id 来自 `get_applied` 的 `applyId`/`applyWareId`。
    返回 {ok, title, operateTime, reasons[], rejectLabelIds[], raw}。
    `reasons` 已把 `{INPUT| …}` 包装剥掉。实证驳回样例（rejectLabelId=238）：
      「商详主图请只展示售卖规格/型号商品 或在对应图片及商详标题明确清晰标注对应商品规格/型号。」"""
    import re as _re
    with _client() as client:
        r = client.get(f"{BID_BASE}/apply/bid/flow/log",
                       params={"applyId": apply_id, "applyWareId": apply_ware_id,
                               "areaId": int(area_id), "refer": int(refer)},
                       headers={"origin": "https://yx.jd.com", "referer": "https://yx.jd.com/"})
        r.raise_for_status()
        d = r.json()
    if not d.get("success"):
        raise BlacklightError(f"flow/log: {d.get('message') or d.get('code')}")
    data = d.get("data") or []
    reasons, labels, title, ts = [], [], "", ""
    for blk in data:
        if blk.get("title"):
            title, ts = blk["title"], blk.get("operateTime") or ts
        for it in (blk.get("flowLogDetailVOList") or []):
            rm = (it.get("remark") or "").strip()
            m = _re.match(r"^\{[A-Z]+\|\s*(.*?)\s*\}$", rm, _re.S)   # 剥 {INPUT| … } 包装
            if m:
                rm = m.group(1)
            if rm:
                reasons.append(rm)
            if it.get("rejectLabelId") is not None:
                labels.append(it["rejectLabelId"])
    return {"ok": True, "applyId": apply_id, "title": title, "operateTime": ts,
            "reasons": reasons, "rejectLabelIds": labels, "raw": data}


def reject_reasons(activity_id: str, sku_ids=None, area_id: int = DEFAULT_AREA_ID,
                   concurrency: int = 4) -> list:
    """批量取**被驳回**报名单的原因：扫已报名列表挑 `applyStatus==3`(驳回)，逐条查 flow_log。
    sku_ids 给定则只看这些。返回 [{skuId, applyId, appliedTime, title, operateTime, reasons, rejectLabelIds}]。
    ⚠️判驳回看 `applyStatus`/`activityProgressStatus` ==3，别看 promoCreateStatus（那是促销层）。"""
    want = {str(s) for s in (sku_ids or [])}
    recs, page = [], 1
    while page <= 60:
        r = get_applied(activity_id, area_id, page=page, page_size=100, only_active=False)
        items = r.get("items") or []
        recs += items
        if not items or (r.get("totalCount") and len(recs) >= r["totalCount"]):
            break
        page += 1
    tgt = [x for x in recs if x.get("applyStatus") == 3
           and (not want or str(x.get("skuId")) in want)]

    def one(x):
        try:
            fl = flow_log(x.get("applyId"), x.get("applyWareId"), area_id)
        except Exception as e:
            return {"skuId": str(x.get("skuId")), "applyId": x.get("applyId"), "error": str(e)[:80]}
        return {"skuId": str(x.get("skuId")), "applyId": x.get("applyId"),
                "appliedTime": x.get("appliedTime"), "title": fl["title"],
                "operateTime": fl["operateTime"], "reasons": fl["reasons"],
                "rejectLabelIds": fl["rejectLabelIds"]}
    return list(pmap(one, tgt, concurrency))


# ---------- 报名价试算 / 券信息 ----------
def price_info(sku_id: str | int, area_id: int = DEFAULT_AREA_ID,
               bid_price: float = None, bidding_id: int = -2, client=None) -> dict:
    """报名价试算 / 券信息（POST /common/sku/price/info）。
    bidding_id=-2 取券/满减信息；给活动 biddingId + bid_price 做试算，返回到手价等。
    client 传入则复用（二分逐次调用省建连开销）；缺省自建。"""
    body = {"startTime": "", "endTime": "", "biddingId": bidding_id, "priceType": 8,
            "skuId": int(sku_id), "bidPrice": (float(bid_price) if bid_price is not None else None),
            "areaId": int(area_id)}
    if client is not None:
        d = _post(client, "/common/sku/price/info", body)
    else:
        with _client() as c:
            d = _post(c, "/common/sku/price/info", body)
    return {
        "skuId": sku_id, "bidPrice": bid_price,
        "actualPrice": (d.get("avgPriceInfoVO") or {}).get("price"),        # 试算到手价
        "promoPrice": (d.get("promoInfoVO") or {}).get("promoPrice"),
        "selfSubPrice": (d.get("selfSubInfoVO") or {}).get("selfSubPrice"),
        "preferentialPrice": (d.get("preferentialInfoVO") or {}).get("preferentialPrice"),
        "vouchers": [v.get("activityInfo") for v in
                     ((d.get("preferentialInfoVO") or {}).get("preferentialDetailList") or [])
                     if v.get("activityInfo")],
        "_raw": d,
    }


# ---------- 报名元数据：ware/detail/list（biddingId/bidBatchId + skuExtendInfo 来源） ----------
def ware_detail(sku_ids: list, area_id: int = DEFAULT_AREA_ID, bidding_type: int = 1) -> list:
    """取可报 SKU 的报名元数据（POST /apply/bid/new/ware/detail/list）——**报名 saveApply 的 biddingId/bidBatchId 来源**。
    body `{biddingType,mode:1,skuIds,areaId}` → skuList[]（每条 406 字段，含 biddingId/bidBatchId/bind*/venderId/saler/cidNames…）。"""
    body = {"biddingType": bidding_type, "mode": 1,
            "skuIds": [int(s) for s in sku_ids], "areaId": int(area_id)}
    with _client() as client:
        d = _post(client, "/apply/bid/new/ware/detail/list", body)
    return d.get("skuList") or []


def build_sku_extend_info(detail: dict, jd_price=None) -> dict:
    """把 detail/list 记录组装成 saveApply 的 skuExtendInfo（**逐字段对齐真实 capture**，2026-07-10 实证）。
    numeric cid 从 *LevelCategoryId 取；jdPriceStr 需外部给（ware/list 的 jdPrice.amount）；biddingSkuId 缺则默认=skuId。"""
    d = detail
    sku = d.get("skuId")
    return {
        "biddingId": d.get("biddingId"), "bidBatchId": d.get("bidBatchId"),
        "biddingSkuId": d.get("biddingSkuId") or sku, "bindFrom": d.get("bindFrom"),
        "bindStatus": d.get("bindStatus", 1), "biddingType": d.get("biddingType") or 1,
        "cid1": d.get("fristLevelCategoryId"), "cid2": d.get("secondLevelCategoryId"),
        "cid3": d.get("thirdLevelCategoryId"), "jdPriceStr": jd_price,
        "officeWebPriceImage": None, "exclusiveOfficeWebPriceImage": None,
        "bidSpecialSubsidyImageList": None, "exclusiveSpecialSubsidyImageList": None,
        "productShortTitle": "", "shortTitleFlag": True,
        "cid1Name": d.get("cid1Name"), "cid2Name": d.get("cid2Name"), "cid3Name": d.get("cid3Name"),
        "imgRui": d.get("imgRui"), "name": d.get("name"), "skuId": sku, "wareId": d.get("wareId"),
        "lowPrice7d": None, "saleVolumeNDayNum": None, "salesVolumeNDay": None,
        "stockNum": d.get("stockNum"), "saler": d.get("saler"), "venderId": d.get("venderId"),
        "freeShipping": d.get("freeShipping"), "noFreeShippingRegions": d.get("noFreeShippingRegions"),
        "modePriceDay": "", "modePrice": "", "pickDaysInfo": "", "presaleCurrentPrice": None,
        "whiteImgSource": None, "whiteImgSourceOld": None, "shortTitleContentOld": "",
        "shortTitleSourceOld": None, "whiteImgSourceList": "", "whiteImgList": "",
    }


# ---------- 报名价定价（二分+试算，复刻插件 rescueSingleSku） ----------
def bid_price_for(sku_id, cost, suggest_price, jd_price, area_id: int = DEFAULT_AREA_ID,
                  bidding_id=None, target_margin: float = 0.0) -> dict:
    """给一个可报 SKU 算最优报名价（cap=建议价、hi=京东价、quote=price_info 试算、目标到手价毛利率）。
    bidding_id 缺则自动 ware_detail 取。二分无解时：建议价自身达标→兜底建议价；否则 ok:False。"""
    if bidding_id is None:
        dl = ware_detail([sku_id], area_id)
        if not dl:
            raise BlacklightError(f"SKU {sku_id} 取不到 biddingId（ware_detail 空）")
        bidding_id = dl[0].get("biddingId")

    def quote(bp):
        return price_info(sku_id, area_id, bid_price=bp, bidding_id=bidding_id).get("actualPrice")

    r = osw_pricing.compute_bid_price(quote, cost, cap=suggest_price, hi=jd_price, target_margin=target_margin)
    if r.get("ok"):
        r["source"] = "二分"
        return r
    a = quote(suggest_price)   # 兜底：建议价自身试算达标就用建议价
    if a is not None and a <= float(suggest_price) and osw_pricing.margin_of(a, cost) >= target_margin:
        return {"ok": True, "enroll_price": round(float(suggest_price), 2), "actual_price": round(a, 2),
                "margin": round(osw_pricing.margin_of(a, cost), 4), "source": "建议价兜底"}
    return {"ok": False, "reason": r.get("reason", "无达标报名价"), "biddingId": bidding_id}


# ==================== 报名 write-path（applyRemind 预检 + saveApply，confirm 门） ====================
# 竞价报名走「开放化动态表单」（2026-07-10 全链路 capture 实证，activity 101664799 报名成功 applyId 141671277）：
# skuList[].skuItems 是 **formSet 数组** `[{formItemId,value}]`，覆盖该活动**全量** formItemId（未填的填 ""）。
# formItemId **每活动不同**，逐活动存 config.json → bybt.forms[activityId]（formItemOrder + roles + resourceId）。
BID_APPLY_EXTEND_BASE = {"cfFile": "", "businessType": 1, "installmentInfo": {}}


def _apply_form(activity_id):
    """从 config.json bybt.forms 取该活动的表单模板 {formItemOrder, roles, resourceId, areaId}（缺则 None）。"""
    return (scene_cfg("bybt").get("forms") or {}).get(str(activity_id))


def build_skuitems(form_order: list, roles: dict, sku_id, bid_price, bid_num=50000) -> list:
    """把 role 值填进**全量 formItemOrder**，产出 saveApply 的 skuItems 数组 `[{formItemId,value}]`。
    **值类型对齐 capture**：skuId/bidNum=int、bidPrice=float(数字)、const1="1"(字符串)、其余="".。"""
    vals = {int(roles["skuId"]): int(sku_id), int(roles["bidPrice"]): float(bid_price),
            int(roles["bidNum"]): int(bid_num)}
    if roles.get("const1"):
        vals[int(roles["const1"])] = "1"
    return [{"formItemId": int(fid), "value": vals.get(int(fid), "")} for fid in form_order]


def _resolve_form(activity_id, resource_id, form_order, roles):
    """按 activity_id 从 config bybt.forms 补齐 form_order/roles/resource_id（显式入参优先）。"""
    tmpl = _apply_form(activity_id) or {}
    return (form_order or tmpl.get("formItemOrder"), roles or tmpl.get("roles"),
            resource_id if resource_id is not None else tmpl.get("resourceId"))


def _sku_entry(sku_id, bid_price, form_order, roles, area_id, detail, jd_price, bid_num) -> dict:
    """组装单条 skuList entry（{skuItems, skuId, skuExtendInfo}）+ 该 SKU 的 missing + biddingId。detail 缺则自动取。"""
    missing = []
    if detail is None:
        dl = ware_detail([sku_id], area_id)
        detail = dl[0] if dl else None
    if not detail:
        missing.append(f"SKU {sku_id}：ware_detail 取不到报名元数据")
    bidding_id = (detail or {}).get("biddingId")
    # ⚠️ biddingId=-2/bidBatchId=-2/bindStatus=-1 是「未绑固定房间·实时生效」的合法态，平台照收
    # （2026-07-14 实证：SKU 10210905825406 biddingId=-2 手动报名成功 applyId141806007）。
    # 只有 detail 整个取不到才拦；别再按 biddingId>0 误杀。
    if detail is None:
        missing.append(f"SKU {sku_id}：ware_detail 取不到报名元数据")
    if jd_price is None:
        # ★2026-08-18 修：兜底读的是 ware_detail 的 `jdPrice`（{amount,...} 或裸数字），
        #   不是 `jdPriceStr` —— 后者是 build_sku_extend_info 的**输出**键，detail 里根本没有。
        #   读错键 ⇒ 兜底永远拿不到值 ⇒ 整批落进 missing「拒发」，而 docstring 一直承诺
        #   「缺则取 ware_detail」。实测 64 款百补报名因此 0 成功（请求根本没发出去）。
        jd_price = _price_amount((detail or {}).get("jdPrice"))
        if jd_price is None:
            missing.append(f"SKU {sku_id}：缺 jd_price（ware/list 的 jdPrice.amount）")
    ext = build_sku_extend_info(detail or {}, jd_price=jd_price) if detail else {}
    sku_items = build_skuitems(form_order, roles, sku_id, bid_price, bid_num) if (form_order and roles) else []
    return {"entry": {"skuItems": sku_items, "skuId": int(sku_id), "skuExtendInfo": ext},
            "missing": missing, "biddingId": bidding_id}


def _base_body(activity_id, resource_id, area_id, entries) -> dict:
    return {"activityId": str(activity_id or ""), "areaId": int(area_id),
            "resourceId": str(resource_id) if resource_id is not None else "",
            "bidApplyExtendInfo": dict(BID_APPLY_EXTEND_BASE),
            "skuList": entries, "remindVOList": []}


def build_apply_body(sku_id, bid_price, activity_id, resource_id=None, form_order: list = None,
                     roles: dict = None, area_id: int = DEFAULT_AREA_ID, detail: dict = None,
                     jd_price=None, bid_num: int = 50000) -> dict:
    """组装**单 SKU** applyRemind/saveApply 基础 body（两接口共用；saveApply 再叠增量），回报缺失项。
    form_order/roles/resource_id 缺则按 activity_id 从 config bybt.forms 自动取。返回 {body, missing, biddingId}。"""
    form_order, roles, resource_id = _resolve_form(activity_id, resource_id, form_order, roles)
    missing = []
    if not form_order or not roles:
        missing.append(f"form_order/roles（活动 {activity_id} 无表单模板——需一次真实报名 capture 存入 config bybt.forms）")
    if not str(activity_id or "").strip():
        missing.append("activity_id（报名页 URL 的 activityId）")
    r = _sku_entry(sku_id, bid_price, form_order, roles, area_id, detail, jd_price, bid_num)
    missing.extend(r["missing"])
    body = _base_body(activity_id, resource_id, area_id, [r["entry"]])
    return {"body": body, "missing": missing, "biddingId": r["biddingId"]}


def apply_remind(body: dict) -> dict:
    """报名预检（POST /apply/bid/new/applyRemind）——**只预检、不报名**（安全）。
    返回 dict（按 remindCode 键：FREE_SHIPPING/DOUBLE_COMPENSATION/INVOICE_CONFIRM/…，各带 success/validSkuList）。"""
    with _client() as client:
        return _post(client, "/apply/bid/new/applyRemind", body)


def _remind_volist(remind_data: dict) -> list:
    """把 applyRemind 返回的**成功**条款置 selected:true，组成 saveApply 的 remindVOList（INVOICE_CONFIRM 等 success:false 剔除）。"""
    out = []
    for code, blk in (remind_data or {}).items():
        if isinstance(blk, dict) and blk.get("success"):
            out.append({"remindCode": code, "selected": True,
                        "validSkuList": blk.get("validSkuList"), "inValidSkuList": blk.get("inValidSkuList")})
    return out


def _finalize_save_body(base_body: dict, remind: dict) -> dict:
    """基础 body + applyRemind 结果 → 完整 saveApply body（逐字段对齐 capture）。"""
    body = _json.loads(_json.dumps(base_body))  # deep copy
    ext = dict(BID_APPLY_EXTEND_BASE)
    ext["agreeDoublePay"] = bool((remind.get("DOUBLE_COMPENSATION") or {}).get("success"))
    ext["invoiceConfirm"] = bool((remind.get("INVOICE_CONFIRM") or {}).get("success"))
    ext["applyFixBatch"] = {"isFixTime": False, "beginTime": "", "endTime": "",
                            "batchName": "实时生效", "optional": True, "fixBatchId": None}
    body["bidApplyExtendInfo"] = ext
    body["remindVOList"] = _remind_volist(remind)
    body["remindCheckFlag"] = True
    body["businessType"] = 1
    return body


def _apply_token(sku_id, area_id, activity_id, bid_price) -> str:
    return _confirm_token({"path": "bybt/saveApply", "sku": str(sku_id), "areaId": str(area_id),
                           "activityId": str(activity_id), "bidPrice": canon_num(bid_price)})


def apply_dryrun(sku_id, bid_price, activity_id, resource_id=None, form_order: list = None,
                 roles: dict = None, area_id: int = DEFAULT_AREA_ID, jd_price=None,
                 bid_num: int = 50000) -> dict:
    """报名 DRY-RUN（竞价 saveApply，**不发送**）。组装 body → 若齐全则跑 **applyRemind 活体预检**硬校验，
    通过才发 confirm_token 并回显**完整 saveApply body** 供逐字段核对。missing 非空 / 预检不过 → 不发 token。"""
    a = build_apply_body(sku_id, bid_price, activity_id, resource_id, form_order, roles,
                         area_id=area_id, jd_price=jd_price, bid_num=bid_num)
    if a["missing"]:
        return {"would_apply": False, "request": {"path": "/apply/bid/new/saveApply", "body": a["body"]},
                "missing": a["missing"], "confirm_token": None,
                "note": "DRY-RUN：缺项未补齐，未发 confirm_token。"}
    try:
        remind = apply_remind(a["body"])
    except BlacklightError as e:
        return {"would_apply": False, "request": {"path": "/apply/bid/new/saveApply", "body": a["body"]},
                "missing": [f"applyRemind 预检未过：{e}"], "confirm_token": None,
                "note": "DRY-RUN：预检失败（body 形状/表单可能不对），未发 confirm_token。"}
    save_body = _finalize_save_body(a["body"], remind)
    return {"would_apply": False, "request": {"path": "/apply/bid/new/saveApply", "body": save_body},
            "missing": [], "remind": remind, "biddingId": a["biddingId"],
            "note": "DRY-RUN：预检通过、未提交。真执行：相同参数 + confirm=confirm_token 调 bybt_apply。",
            "confirm_token": _apply_token(sku_id, area_id, activity_id, bid_price)}


def _qua_errors(data: dict) -> list:
    """从 saveApply 返回的 quaCheckResultList 抽每 SKU 的卡控原因（errorMsg/errorMsgList）。"""
    out = []
    for q in (data.get("quaCheckResultList") or []):
        em = q.get("errorMsg") or ((q.get("errorMsgList") or [None])[0])
        if em:
            out.append(em)
    return out


def _save_apply(client, body: dict) -> dict:
    """发 saveApply 并**透出卡控明细**：成功→data；失败→抛 BlacklightError 带 quaCheckResultList[].errorMsg
    （json_post 会吞掉 data，故这里绕开它直接读原始返回，把"预估到手价需<建议价"这类真原因带出来）。"""
    r = client.post(f"{BID_BASE}/apply/bid/new/saveApply",
                    content=_json.dumps(body, ensure_ascii=False))
    if r.status_code in REDIRECT_CODES:
        raise BlacklightError("saveApply 被重定向——登录态失效，请 yx_login 重登")
    r.raise_for_status()
    j = r.json()
    data = j.get("data") or {}
    if not j.get("success"):
        errs = _qua_errors(data)
        raise BlacklightError("报名卡控：" + ("；".join(errs) if errs else (j.get("message") or j.get("code") or "未知")))
    return data


@audited("bybt", "apply")
def apply(sku_id, bid_price, activity_id, resource_id=None, form_order: list = None, roles: dict = None,
          area_id: int = DEFAULT_AREA_ID, jd_price=None, bid_num: int = 50000, confirm: str = "") -> dict:
    """**报名真执行**（竞价 saveApply，真金白银）。需相同参数先 apply_dryrun 拿 confirm_token 再带 confirm。
    流程：build 基础 body → applyRemind（派生 remindVOList/agreeDoublePay/invoiceConfirm）→ saveApply。"""
    if confirm != _apply_token(sku_id, area_id, activity_id, bid_price):
        raise BlacklightError("报名需二次确认：先用相同参数跑 bybt_apply_dryrun 拿 confirm_token 再带 confirm。")
    a = build_apply_body(sku_id, bid_price, activity_id, resource_id, form_order, roles,
                         area_id=area_id, jd_price=jd_price, bid_num=bid_num)
    if a["missing"]:
        raise BlacklightError(f"报名缺项，拒发：{a['missing']}")
    remind = apply_remind(a["body"])
    body = _finalize_save_body(a["body"], remind)
    with _client() as client:
        d = _save_apply(client, body)
    return {"applied": True, "skuId": sku_id, "bidPrice": bid_price,
            "applyIds": ((d.get("result") or {}).get("applyIds")),
            "successCount": ((d.get("result") or {}).get("successCount")),
            "response": d, "confirm_token": confirm}


# ↓ 批量报名旧路径（apply_batch：多 SKU 一单 saveApply）已删除，改用 enroll_bulk（逐条 apply，更稳/有回执/见文件末尾）。


# ---------- 退出（申请退出，appeal/save type:4，confirm 门） ----------
MAX_WITHDRAW_BATCH = 50


def find_applied(activity_id: str, sku_id: str | int, area_id: int = DEFAULT_AREA_ID) -> Optional[dict]:
    """在已报名列表里按 skuId 找到该 SKU 的报名记录（取退出所需 applyId/applyWareId/biddingType）。逐页扫。

    ⚠️**找不到时区分两种情况**（2026-08-05 修，同 `ms.find_applied`）：确认没有 → `None`；
    **没拉全 → 抛错**，不返回 `None`。百补池动辄数千条、churn 极快，空页更容易撞上；
    把"没拉全"读成"没报名"会直接判错"退不了"。"""
    sku_id = str(sku_id).strip()
    hit = {}

    def _fetch(page):
        d = get_applied(activity_id, area_id, page=page, page_size=100, only_active=False)
        return d["items"], d.get("totalCount")

    def _scan(items):
        for it in items:
            if str(it.get("skuId")) == sku_id:
                hit["it"] = {"skuId": sku_id, "applyId": it.get("applyId"),
                             "applyWareId": it.get("applyWareId"), "globalId": it.get("globalId"),
                             "biddingType": it.get("biddingType"), "bidPrice": it.get("bidPrice"),
                             "promoCreateStatus": it.get("promoCreateStatus")}
                return True
        return False

    r = paged_scan(_fetch, 100, on_page=_scan)
    if hit:
        return hit["it"]
    if not r["complete"]:
        raise BlacklightError(
            f"SKU {sku_id} 未在已报名里找到，但**清单没拉全**（{r['truncated']}）——"
            f"这不等于「没报名」，别据此判定退不了。稍后重试，或用 get_applied 分页自查。")
    return None


def _withdraw_body(apply_id, apply_ware_id, area_id, bidding_type=1, global_id=None) -> dict:
    return {"areaId": int(area_id), "applyId": int(apply_id), "applyWareId": int(apply_ware_id),
            "globalId": int(global_id if global_id is not None else apply_id),
            "biddingType": int(bidding_type), "type": 4}   # type:4 = 申请退出


def _withdraw_token(body) -> str:
    return _confirm_token({"path": "bybt/withdraw", **{k: str(body[k]) for k in
                          ("areaId", "applyId", "applyWareId", "globalId", "biddingType", "type")}})


def withdraw_dryrun(apply_id, apply_ware_id, area_id: int = DEFAULT_AREA_ID,
                    bidding_type: int = 1, global_id=None) -> dict:
    """退出 DRY-RUN（申请退出 /apply/bid/appeal/save type:4）：组装但**不发送**，回显 body + confirm_token。
    apply_id/apply_ware_id/bidding_type 来自 get_applied / find_applied。"""
    body = _withdraw_body(apply_id, apply_ware_id, area_id, bidding_type, global_id)
    return {"would_withdraw": False, "request": {"path": "/apply/bid/appeal/save", "body": body},
            "note": "DRY-RUN：未提交。真执行：相同参数 + confirm=confirm_token 调 bybt_withdraw。此为**申请退出**（可能需审核）。",
            "confirm_token": _withdraw_token(body)}


@audited("bybt", "withdraw")
def withdraw(apply_id, apply_ware_id, area_id: int = DEFAULT_AREA_ID, bidding_type: int = 1,
             global_id=None, confirm: str = "") -> dict:
    """**退出真执行**（申请退出）。需相同参数先 withdraw_dryrun 拿 confirm_token 再带 confirm。"""
    body = _withdraw_body(apply_id, apply_ware_id, area_id, bidding_type, global_id)
    if confirm != _withdraw_token(body):
        raise BlacklightError("退出需二次确认：先用相同参数跑 bybt_withdraw_dryrun 拿 confirm_token 再带 confirm。")
    with _client() as client:
        d = _post(client, "/apply/bid/appeal/save", body)
    return {"withdrawn": True, "applyId": apply_id, "response": d, "confirm_token": confirm}


# ---------- 批量退出（无原生批量：逐个 appeal/save，一个 confirm 门） ----------
def _norm_withdraw_records(records: list) -> list:
    """规整退出记录：每条取 applyId/applyWareId/biddingType/globalId（来自 get_applied/find_applied）。"""
    out = []
    for r in records:
        aid = r.get("applyId", r.get("apply_id"))
        awid = r.get("applyWareId", r.get("apply_ware_id"))
        if aid is None or awid is None:
            raise BlacklightError(f"退出记录缺 applyId/applyWareId：{r}")
        out.append({"apply_id": int(aid), "apply_ware_id": int(awid),
                    "bidding_type": int(r.get("biddingType", r.get("bidding_type", 1))),
                    "global_id": r.get("globalId", r.get("global_id"))})
    if not out:
        raise BlacklightError("withdraw_batch 需要至少一条记录")
    if len(out) > MAX_WITHDRAW_BATCH:
        raise BlacklightError(f"单次批量退出 {len(out)} 超上限 {MAX_WITHDRAW_BATCH}，请分批")
    return out


def _withdraw_batch_token(recs, area_id) -> str:
    key = sorted((str(r["apply_id"]), str(r["apply_ware_id"])) for r in recs)
    return _confirm_token({"path": "bybt/withdraw#batch", "areaId": str(area_id), "recs": key})


def withdraw_batch_dryrun(records: list, area_id: int = DEFAULT_AREA_ID) -> dict:
    """**批量退出 DRY-RUN**（逐个 appeal/save type:4，不发送）。records=[{applyId,applyWareId,biddingType?,globalId?}]（来自 get_applied/find_applied）。"""
    recs = _norm_withdraw_records(records)
    return {"would_withdraw": False, "count": len(recs),
            "requests": [{"path": "/apply/bid/appeal/save",
                          "body": _withdraw_body(r["apply_id"], r["apply_ware_id"], area_id,
                                                 r["bidding_type"], r["global_id"])} for r in recs],
            "note": "DRY-RUN：将对每条逐个 POST appeal/save(type:4=申请退出)，未发送。真执行：相同 records + confirm 调 bybt_withdraw_batch。",
            "confirm_token": _withdraw_batch_token(recs, area_id)}


@audited("bybt", "withdraw_batch")
def withdraw_batch(records: list, area_id: int = DEFAULT_AREA_ID, confirm: str = "") -> dict:
    """**批量退出真执行**：逐个 appeal/save。需相同 records 先 withdraw_batch_dryrun 拿 confirm_token 再带 confirm。逐条回报成败。"""
    recs = _norm_withdraw_records(records)
    if confirm != _withdraw_batch_token(recs, area_id):
        raise BlacklightError("批量退出需二次确认：先用相同 records 跑 bybt_withdraw_batch_dryrun 拿 confirm_token 再带 confirm。")
    results = []
    for r in recs:
        body = _withdraw_body(r["apply_id"], r["apply_ware_id"], area_id, r["bidding_type"], r["global_id"])
        try:
            with _client() as client:
                d = _post(client, "/apply/bid/appeal/save", body)
            results.append({"applyId": r["apply_id"], "success": True, "response": d})
        except BlacklightError as e:
            results.append({"applyId": r["apply_id"], "success": False, "message": str(e)})
    return {"executed": True, "count": len(recs), "confirm_token": confirm,
            "all_success": all(x["success"] for x in results), "results": results}


# ==================== ★端到端批量报名编排（2026-07-14 沉淀本次流程） ====================
# 一条龙：筛本人(saler)→剔已超补→真试算定价(到手价<建议价·保目标毛利)→分桶→dry-run→逐条 apply(带回执)。
def filter_own(sku_ids: list, area_id: int = DEFAULT_AREA_ID, saler: str = None,
               chunk: int = 15, concurrency: int = 8) -> dict:
    """按 `ware_detail.saler` 筛**本人**的 SKU——可报清单混整店多采销的货，报名前必须过滤别误碰别人。
    saler 缺省=当前登录 erp(current_pin)。返回 {owned:[skuId...], by_saler:{erp:count}, meta}。
    ★并发+小分片+单片容错：ware_detail 每片≤15(单次<20s超时)、pmap 并发查、某片超时/失败不拖累其他(记 failed_chunks)。
    大批量(如 2060 款)串行会撞超时→本函数并发化(2026-07-20)。"""
    me = (saler or jd_auth.current_pin() or "").strip()
    ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    chunks = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]

    def _q(c):
        try:
            return list(ware_detail(c, area_id)) or []
        except Exception:
            return None   # 单片失败(超时/异常)→None，不抛，不拖累其他片

    results = pmap(_q, chunks, workers=concurrency)
    owned, by, failed = [], {}, 0
    for r in results:
        if r is None:
            failed += 1
            continue
        for d in r:
            sal = (d.get("saler") or "").strip()
            by[sal] = by.get(sal, 0) + 1
            if sal == me:
                owned.append(str(d.get("skuId")))
    return {"owned": owned, "by_saler": dict(sorted(by.items(), key=lambda x: -x[1])),
            "meta": {"me": me, "queried": len(ids), "owned_count": len(owned),
                     "failed_chunks": failed, "total_chunks": len(chunks)}}


def own_eligible(area_id: int = DEFAULT_AREA_ID, saler: str = None, page_size: int = 200) -> dict:
    """可报清单里**只属于本人**的 SKU（list_eligible 全量 → saler 过滤）。

    返回 {items, by_saler, total_eligible, owned_count, **归属覆盖率**}。

    ★★**`owned_count` 是下限不是全部**（2026-08-18 补）：归属判定要逐 SKU 打 `ware_detail`，
      分片超时会被 `filter_own` 静默跳过（`meta.failed_chunks`），那些 SKU 既不算本人、
      也不算别人，直接从统计里消失。实测 total_eligible=1770 而 by_saler 各人加总仅 1627，
      **143 款没拿到 saler**，里面完全可能还有本人的品。
      此前本函数把 `meta` 吞了 ⇒ 覆盖率不可见 ⇒ 会把"查到的"当成"全部的"用
      （同 [[subsidy-pool-pull-truncation]] 的教训）。现在 `归属覆盖率` 直接给出
      `已判定/总数`、`未判定数` 与 `failed_chunks`，低于 0.95 时带 `warning`。"""
    items, page = [], 1
    while True:
        r = list_eligible(area_id, page=page, page_size=page_size)
        items.extend([it for it in r["items"] if it.get("status") == 1 and not it.get("cannotApplyReason")])
        if page >= (r.get("totalPage") or 1) or not r["items"]:
            break
        page += 1
    by_sku = {str(it["skuId"]): it for it in items}
    f = filter_own(list(by_sku), area_id, saler)
    judged = sum(f["by_saler"].values())                       # 真正拿到 saler 的条数
    total = len(items)
    cov = (judged / total) if total else 1.0
    meta = f.get("meta") or {}
    out = {"items": [{"skuId": s, "name": by_sku[s].get("name"), "jdPrice": by_sku[s].get("jdPrice"),
                      "suggestPrice": by_sku[s].get("suggestPrice")} for s in f["owned"]],
           "by_saler": f["by_saler"], "total_eligible": total, "owned_count": len(f["owned"]),
           # ★归属覆盖率：owned_count 是**下限**，未判定的那批里可能还有本人的品
           "归属覆盖率": {"已判定": judged, "总数": total, "未判定": total - judged,
                          "覆盖率": round(cov, 4),
                          "failed_chunks": meta.get("failed_chunks"),
                          "total_chunks": meta.get("total_chunks")}}
    if cov < 0.95:
        out["归属覆盖率"]["warning"] = (
            "★归属判定覆盖率 %.1f%%（%d/%d）——%d 款没拿到 saler（ware_detail 分片超时被跳过）。"
            "`owned_count` 是**下限**，别当成「我全部的可报品」；要补全请对未判定的 SKU 重跑 filter_own。"
            % (cov * 100, judged, total, total - judged))
    return out


def _enrolled_baibu(pricing: dict) -> bool:
    """query_pricing 促销带"超级补贴"=已报名超补→报名候选应跳过（用户 2026-07-14 确认）。"""
    return any("超级补贴" in ((p.get("name") or "") + (p.get("typeName") or ""))
               for p in (pricing.get("promotions") or []))


def _has_platform_coupon(pricing: dict) -> bool:
    """有平台/广告出资券（reward>jxReward，京喜少出的部分）→客户口径会高估亏，值京喜口径复核。"""
    return any((x.get("reward") or 0) - (x.get("jxReward") or 0) > 0.5
               for x in (pricing.get("promotions") or []) + (pricing.get("coupons") or []))


def solve_bid(sku_id, suggest_price, full_cost, cps_rate, jd_price, area_id: int = DEFAULT_AREA_ID,
              target_margin: float = 0.05, bidding_id=None, max_iter: int = 14, client=None,
              objective: str = "max_margin") -> dict:
    """真试算解报名价：到手价∈[保毛利线 T, 建议价)（**严格<建议价**=平台硬规则，实证卡控）。

    ★**objective 决定取区间哪一端**（2026-08-03 加，默认保持旧行为）：
      · `min_price`(默认)：取**最低报名价** ⇒ 毛利率恰好等于 target_margin（最激进降价）。
      · `max_margin`：取**最高报名价**（到手价顶到建议价下沿）⇒ 毛利率最大。
    用户说「毛利率≥X%」是**闸**不是**目标** ⇒ 那种场景传 `max_margin`。见 [[enrollment-pricing-defaults-trap]]。

    **只依赖到手价随报名价单调增，不假设线性**（线性只做快路径初值，实际到手价一律 price_info 校验，不达标二分兜底）。
    毛利=客户到手价口径 `1−cps−fc/到手`（投后履约,含CPS/广告）。提效：client复用(整轮省建连)+线性快路径+二分精度0.05。
    返回 {ok,enroll_price,actual_price,margin,method} 或 {ok:False,reason}。"""
    def _f2(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    sug = float(suggest_price); fc = float(full_cost); cps = float(cps_rate or 0); jd = float(jd_price)
    T = fc / (1 - cps - target_margin)                         # 保目标毛利的最低到手价
    if T >= sug:
        return {"ok": False, "reason": f"建议价{sug}太低：到手价须<建议价，保不住{target_margin*100:.0f}%毛利(需到手≥{round(T,2)})"}
    _own = client is None
    cl = _client() if _own else client                        # L1: 整轮试算复用一个 client

    def q(p):
        return _f2((price_info(sku_id, area_id, bid_price=round(float(p), 2),
                               bidding_id=bidding_id, client=cl) or {}).get("actualPrice"))

    def _finish(P, a, method):                                # 保T + 严格<建议价 微调 + 校验(逐0.01,真值)
        for _ in range(20):
            if a is not None and a >= T - 1e-9:
                break
            P = round(P + 0.01, 2)
            if P > jd:
                break
            a = q(P)
        for _ in range(30):
            if a is not None and a < sug - 1e-9:
                break
            P = round(P - 0.01, 2)
            if P <= fc:
                break
            a = q(P)
        if a is None:
            return {"ok": False, "reason": "报名价试算无到手价"}
        if a >= sug - 1e-9:
            return {"ok": False, "reason": f"到手价压不到建议价{sug}下且保毛利(到手{round(a,2)})"}
        m = 1 - cps - fc / a
        if m < target_margin - 1e-6:
            return {"ok": False, "reason": f"到手{round(a,2)}毛利{round(m*100,1)}%<{target_margin*100:.0f}%"}
        return {"ok": True, "enroll_price": P, "actual_price": round(a, 2),
                "margin": round(m, 4), "method": method, "objective": objective}
    try:
        a_jd = q(jd)                                          # 顶到京东价的到手价 = 可行性 + 线性斜率锚点
        if a_jd is None or a_jd < T - 1e-9:
            return {"ok": False, "reason": f"报名价顶到京东价{jd}、到手{a_jd}仍<保毛利线{round(T,2)}(空间不够)"}
        if objective == "max_margin":
            # 到手价随报名价单调增 ⇒ 毛利率随之单调增 ⇒ 取「到手价**严格<建议价**」的最高报名价。
            if a_jd < sug - 1e-9:
                return _finish(round(jd, 2), a_jd, "max-margin-cap-jd")   # 京东价即合规→顶格
            lo, hi = fc, jd                                   # 不变式：q(hi)≥建议价(违规)；q(lo)合规
            for _ in range(max_iter):
                if hi - lo <= 0.01:
                    break
                mid = round((lo + hi) / 2, 2)
                a = q(mid)
                if a is None:
                    hi = mid
                elif a < sug - 1e-9:
                    lo = mid
                else:
                    hi = mid
            return _finish(round(lo, 2), q(round(lo, 2)), "max-margin-bisect")
        k = a_jd / jd if jd else 0                            # L2 线性快路径(实际到手价一律校验)
        if k > 0:
            P0 = min(max(round(T / k, 2), round(fc + 0.01, 2)), round(jd, 2))
            a0 = q(P0)
            if a0 is not None and a0 >= T - 1e-9 and a0 < sug - 1e-9:
                P, a = P0, a0
                for _ in range(6):                            # 下探到"恰好保T的最低价"
                    Pd = round(P - 0.01, 2)
                    if Pd <= fc:
                        break
                    ad = q(Pd)
                    if ad is None or ad < T - 1e-9:
                        break
                    P, a = Pd, ad
                return _finish(P, a, "linear-fastpath")
        lo, hi = fc, jd                                       # L3 全区间二分(精度0.05)——线性初值不达标时
        for _ in range(max_iter):
            if hi - lo <= 0.05:
                break
            mid = round((lo + hi) / 2, 2); a = q(mid)
            if a is None:
                lo = mid; continue
            if a >= T:
                hi = mid
            else:
                lo = mid
        return _finish(round(hi, 2), q(round(hi, 2)), "bisect")
    finally:
        if _own:
            cl.close()


# ---------- 「站内同款」卡控账本（2026-08-12 建） ----------
# 平台报名时会判「站内同款商品」，同款里只让一个进百补。实测**同一批 3 款连着两天
# 被拒、被指向的同款 SKU 完全相同** ⇒ 这是确定性规则，不是偶发，每天重报都是白撞一次
# （还占审核）。其中只有 1/3 是同 SPU（可预测），另 2/3 是**跨 SPU** 的同款
# （不同商品被平台判为同款）——所以光靠 SPU 去重预测不到，必须把平台真实拒过的对子记下来。
def _same_item_path() -> str:
    return os.path.join(_paths.config_dir(), "bybt_same_item_blocks.json")


def load_same_item_blocks() -> dict:
    """已知会被「站内同款」卡控的 {skuId: 同款skuId}。取不到就返回空，不阻断主流程。"""
    try:
        with open(_same_item_path(), encoding="utf-8") as f:
            return _json.load(f) or {}
    except Exception:
        return {}


SAME_ITEM_TTL_DAYS = 30      # ★账本条目的复测周期，见 record_same_item_blocks


def record_same_item_blocks(results: list) -> dict:
    """从 `enroll_bulk` 回执里学习新的同款卡控对，落 `config/`（入库可追溯）。

    ⚠️只记**平台明确说同款**的，别把超时/限流当成规则学进去——那会永久漏报一款。

    ★**每条带 `since` 时间戳，超过 `SAME_ITEM_TTL_DAYS` 会自动放行复测**：
      同款关系不是永久的（对方下架 / 平台判定变了都会解除）。
      **永久剔除 = 这款永远报不上且没人会发现** —— 危险的默认值不能躺着，
      所以过期后放它再试一次；仍被拒就刷新时间戳，等于自动续期。
      见 `fix-the-caller-not-just-the-function`。
    """
    import re as _re
    import datetime as _dt
    today = _dt.date.today().isoformat()
    known = load_same_item_blocks()
    added, renewed = {}, []
    for x in results or []:
        if x.get("ok"):
            continue
        rsn = str(x.get("reason") or "")
        if "同款" not in rsn:
            continue
        m = _re.search(r"SKUID\s*(\d+)|item\.jd\.com/(\d+)\.html", rsn)
        sid = str(x.get("skuId") or "")
        peer = (m.group(1) or m.group(2)) if m else None
        if not sid:
            continue
        if sid in known:
            known[sid] = {"peer": peer, "since": today}      # 又被拒 ⇒ 续期
            renewed.append(sid)
        else:
            known[sid] = {"peer": peer, "since": today}
            added[sid] = peer
    if added or renewed:
        with open(_same_item_path(), "w", encoding="utf-8") as f:
            _json.dump(known, f, ensure_ascii=False, indent=1, sort_keys=True)
    return {"新增": added, "续期": renewed, "累计": len(known),
            "TTL天": SAME_ITEM_TTL_DAYS, "path": _same_item_path()}


def _filter_same_item(A: list) -> list:
    """就地从 A 桶剔除**未过期**的同款卡控款，返回被剔的明细。

    ★过期条目**不剔除**（放它复测）——同款关系会解除，永久剔除等于永远报不上。
    """
    import datetime as _dt
    known = load_same_item_blocks()
    if not known:
        return []
    today = _dt.date.today()
    out = []
    for x in list(A):
        sid = str(x.get("skuId") or "")
        ent = known.get(sid)
        if not ent:
            continue
        # 兼容旧格式（值是裸 peer 字符串，无时间戳）：当作刚记的，不放行
        if isinstance(ent, dict):
            since = ent.get("since")
            peer = ent.get("peer")
            try:
                age = (today - _dt.date.fromisoformat(since)).days if since else 0
            except Exception:
                age = 0
            if age >= SAME_ITEM_TTL_DAYS:
                continue                                     # ★过期 ⇒ 放行复测
        else:
            peer = ent
        A.remove(x)
        out.append({"skuId": sid, "同款": peer, "原因": "站内同款卡控(历史实证)"})
    return out


def plan_bulk_enroll(area_id: int = DEFAULT_AREA_ID, saler: str = None, target_margin: float = 0.05,
                     limit: int = None, skus: list = None, activity_id: str = None,
                     concurrency: int = 8, objective: str = "max_margin",
                     adv_source: str = "auto") -> dict:
    """★批量报名**规划**（只读、零写）：筛本人→剔已报名/已超补→真试算定价(到手价<建议价,保目标毛利)→分桶。
    skus 给定则只算这些(仍按 saler 校验归属)。activity_id 给定则用已报名列表**权威去重**(补监控延迟)。

    **objective**: min_price(默认,毛利率恰好=target) / max_margin(到手价顶建议价下沿,毛利最大)——见 solve_bid。
    **skipped 桶已按生效态拆分**(2026-08-03)：summary 里「已生效」vs「占坑未生效(审核/竞价中)」分开计数——
      占坑≠在跑，实测 400 占坑仅 107 真生效；未中标的会释放回可报池，A 桶偏小时先看这个数。见 [[bybt-live-status-judgement]]。
    review 桶带 `ub_margin`/`ub_verdict`(京喜零承担上界)：低于 target 即**确定性出局**，不必再人工按京喜口径算。
    返回 {A_biddable, review_platform, C_infeasible, skipped_enrolled, errors, summary}。
    ★`adv_source`（2026-08-11）：成本里的广告项来源。默认 `auto` = **ge 近 7 日实际单均广告**；
      `osw24h` 退回 osw 的 24 小时快照（分母小会炸、无广告订单会记 0 ⇒ 高估毛利）。
      做 A/B 归因时用它把「advCost 修复」与「占坑/状态变化」分开。见 `osw/adv.py`。
    """
    from blacklight.osw import margin as _mt
    activity_id = activity_id or scene_cfg("bybt").get("default_activity_id")   # 缺则取 config(101664799)
    me = (saler or jd_auth.current_pin() or "").strip()
    applied = applied_sku_ids(activity_id, area_id) if activity_id else set()
    live = live_sku_ids(activity_id, area_id) if activity_id else set()   # 真生效(三字段+时间窗)，用于拆分 skipped

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if skus:                                                  # 指定 SKU：直接 ware_detail(快),不全量扫
        cand = [str(s).strip() for s in skus]
        meta = {}
        for i in range(0, len(cand), 40):
            for d in ware_detail(cand[i:i + 40], area_id):
                meta[str(d.get("skuId"))] = {"saler": (d.get("saler") or "").strip(),
                    "jd": _price_amount(d.get("jdPrice")), "sug": _num(d.get("bidApplyMaxPrice")),
                    "bidding_id": d.get("biddingId"), "name": d.get("name")}
        items = [{"skuId": s, "name": meta[s]["name"], "jdPrice": meta[s]["jd"],
                  "suggestPrice": meta[s]["sug"], "bidding_id": meta[s]["bidding_id"]}
                 for s in cand if meta.get(s, {}).get("saler") == me]
    else:                                                     # 全量：list_eligible→saler过滤(重,建议 limit 或后台跑)
        oe = own_eligible(area_id, saler)
        items = oe["items"]
        if limit:
            items = items[:limit]
        for it in items:
            it["bidding_id"] = None
    cost_map = _mt.query_pricing_batch([str(x["skuId"]) for x in items], adv_source=adv_source)   # 批量取成本(strSkuIds多SKU,省N倍往返)

    def _price_one(it):                                       # 单SKU试算(只读)，并发跑；**全程 try→永不抛(不丢批)**
        s = ""
        try:
            s = str(it["skuId"]); sug = it.get("suggestPrice"); jd = it.get("jdPrice")
            if s in applied:
                return ("skipped", {"skuId": s, "live": s in live,
                                    "reason": ("已生效(活动进度2+中标1+促销2)" if s in live
                                               else "占坑未生效(审核中/竞价中→未中标会释放)")})
            pr = cost_map.get(s) or _mt.query_pricing(s)      # 批量命中优先，缺失回退单查
            if _enrolled_baibu(pr):
                return ("skipped", {"skuId": s, "reason": "已报超补(监控促销)"})
            row = {"skuId": s, "name": (it.get("name") or "")[:36], "jdPrice": jd,
                   "suggestPrice": sug, "fullCost": pr["fullCost"]}
            if sug is None:
                row["reason"] = "无建议价"; return ("C", row)
            r = solve_bid(s, sug, pr["fullCost"], pr["cpsRate"], jd, area_id, target_margin,
                          bidding_id=it.get("bidding_id"), objective=objective)
            if r["ok"]:
                # ★键名双写(2026-08-03)：`enroll_price/actual_price` 与底层 solve_* 一致，跨场域通用读取；
                #   `bidPrice` 保留(enroll_bulk 直接吃这个键)。此前误用 actual_price 读 camel 键导致整桶读成 0。
                row.update({"bidPrice": r["enroll_price"], "actualPrice": r["actual_price"],
                            "enroll_price": r["enroll_price"], "actual_price": r["actual_price"],
                            "margin": round(r["margin"] * 100, 1)}); return ("A", row)
            if _has_platform_coupon(pr):                      # 客户口径不达标但有平台券→京喜口径可能可报
                # ★上界(2026-08-03)：报名价须严格<建议价，取建议价-0.01 且假设京喜零承担 = 最有利情形。
                cap = round(float(sug) - 0.01, 2)
                ubm = (1 - pr["cpsRate"] - pr["fullCost"] / cap) if cap > 0 else None
                row["ub_margin"] = round(ubm * 100, 1) if ubm is not None else None
                row["ub_verdict"] = ("上界仍<目标⇒确定性出局" if (ubm is not None and ubm < target_margin)
                                     else "上界达标⇒值得人工按京喜口径复核")
                row["reason"] = r["reason"] + "（含平台出资券，京喜口径或可报）"
                return ("review", row)
            row["reason"] = r["reason"]; return ("C", row)
        except Exception as e:
            return ("err", {"skuId": s, "error": str(e)[:80]})
    A, review, C, skipped, err = [], [], [], [], []
    _bk = {"A": A, "review": review, "C": C, "skipped": skipped, "err": err}
    for tag, payload in pmap(_price_one, items, concurrency):   # 并发试算，保序
        _bk[tag].append(payload)
    blocked = _filter_same_item(A)          # ★平台「站内同款」卡控：已知会被拒的先剔掉
    return {"A_biddable": A, "review_platform": review, "C_infeasible": C,
            "same_item_blocked": blocked,
            "skipped_enrolled": skipped, "errors": err,
            "summary": {"本人可报": len(items), "可报名A": len(A), "待复核(平台券)": len(review),
                        "不可行C": len(C), "错误": len(err),
                        # ★占坑≠在跑：拆开看，未生效的那批未中标后会释放回可报池
                        "已跳过(占坑)": len(skipped),
                        "├已生效": sum(1 for x in skipped if x.get("live")),
                        "└占坑未生效": sum(1 for x in skipped if not x.get("live")),
                        "口径": "全成本=采购+物流+cps+jxAdv广告;毛利=客户到手价口径(保守);到手价严格<建议价;目标%.0f%%" % (target_margin * 100)}}


def _bulk_token(rows, activity_id, area_id) -> str:
    key = sorted((str(r["skuId"]), canon_num(r["bidPrice"])) for r in rows)
    return _confirm_token({"path": "bybt/bulkEnroll", "areaId": str(area_id),
                           "activityId": str(activity_id), "items": key})


def enroll_bulk_dryrun(rows: list, activity_id, resource_id=None, area_id: int = DEFAULT_AREA_ID) -> dict:
    """批量报名 DRY-RUN：回显将报的 SKU+报名价，发覆盖整批的 confirm_token（逐条预检在真执行时做）。"""
    valid = [r for r in rows if r.get("skuId") and r.get("bidPrice") is not None]
    if not valid:
        return {"would_apply": False, "count": 0, "confirm_token": None, "note": "无有效行(需 skuId+bidPrice)"}
    return {"would_apply": False, "count": len(valid),
            "preview": [{"skuId": r["skuId"], "bidPrice": r["bidPrice"]} for r in valid[:100]],
            "note": "真执行：相同 rows + confirm 调 bybt_enroll_bulk（逐条 apply，各带独立回执/卡控）。",
            "confirm_token": _bulk_token(valid, activity_id, area_id)}


@audited("bybt", "enroll_bulk")
def enroll_bulk(rows: list, activity_id, resource_id=None, area_id: int = DEFAULT_AREA_ID,
                confirm: str = "", concurrency: int = 1, verify: bool = True, verify_wait: float = 8.0,
                min_interval: float = MIN_APPLY_INTERVAL, retry_rounds: int = 2) -> dict:
    """★批量报名真执行：**逐条 apply 真报·串行限速提交**（各带独立回执/卡控明细、一条失败不拖累其他）。
    rows=[{skuId,bidPrice,jdPrice?}]（jdPrice 建议带上，缺则取 ware_detail）。需相同 rows 先 enroll_bulk_dryrun 拿 confirm。

    ## ★★不变量是「间隔秒数」，不是「并发数」（2026-08-19 从 2307 条审计定死）
    该卡控**按账号串行**，真正决定撞不撞的是**两次 apply 之间的间隔**：

    | 中位间隔 | 天数 | 调用 | 撞「正在报名中」 |
    |---|---|---|---|
    | **≥ 4.0s** | 9 | 573 | **0%** |
    | ≤ 2.0s | 6 | 479 | **35~44%** |

    零例外，阈值落在 2~4s 之间（4.0 是实测安全值不是最优值，有数据可下调）。

    ⚠️**别再把 `concurrency=1` 当护栏**——它只是「间隔」在某台机器上的代理变量：
    串行时的实际间隔 = 单次 apply 往返延迟，**机器越快 / 网络越近，间隔越小**。
    本机串行恰好跑出 ~4s 所以 0%；同事机器更快，同样 concurrency=1 跑出 ~2s 就掉进 35% 那档。
    ∴ 本函数用 `min_interval` **显式补齐间隔**（`sleep(min_interval - 本次耗时)`），
    在快机器上才真正起作用；本机成本≈0。这是机器无关的写法。

    `concurrency>1` 仍可传但**不推荐**：并发下 `min_interval` 只能约束单线程，总速率仍会翻倍。

    ## retry_rounds：只重投「已证实没报上」的
    卡控被挡 ≠ 没报上（回执会被异步坑到偏低）。故**先 verify 再重投**：
    每轮只拿 `verified.not_reported_skus`（报前/报后已报名集合的差集，权威）重投，
    **零重复报名风险**。2026-07-27 那 34 条被挡的正是这么重投后 34/34 全成功。
    返回的 `verified` 是**最后一轮之后**的终态；`rounds` 记录每轮新增。

    **★verify=True 回执自验证(2026-07-20)**：报名回执 success 会被异步坑到不准(「正在报名中」返回失败但实际异步报上→回执偏低)。
    故报完**前后对比已报名集合(含审核中)**，返回 `verified.newly_reported`=本次真新报上数(权威)。判成败**看 verified 别看 success**。"""
    from concurrent.futures import ThreadPoolExecutor
    import time as _t
    valid = [r for r in rows if r.get("skuId") and r.get("bidPrice") is not None]
    if confirm != _bulk_token(valid, activity_id, area_id):
        raise BlacklightError("批量报名需二次确认：先用相同 rows 跑 bybt_enroll_bulk_dryrun 拿 confirm_token 再带 confirm。")
    submitted = {str(r["skuId"]) for r in valid}
    before = applied_sku_ids(activity_id, area_id) if verify else set()   # 报前占用态(含审核中)

    def _one(r):                                             # **全程 try→永不抛(并发不丢批)**
        s = str(r.get("skuId") or ""); bp = r.get("bidPrice")
        try:
            bp = float(bp); jd = r.get("jdPrice")
            tok = _apply_token(s, area_id, activity_id, bp)   # 直接算 per-SKU token，免冗余 dry-run 的一次 applyRemind
            rr = apply(s, bp, activity_id, resource_id, area_id=area_id, jd_price=jd, confirm=tok)
            aid = (rr.get("applyIds") or [None])
            if rr.get("applied") and rr.get("successCount"):
                return {"skuId": s, "bidPrice": bp, "ok": True, "applyId": aid[0] if aid else None}
            return {"skuId": s, "bidPrice": bp, "ok": False, "reason": "successCount=0"}
        except Exception as e:
            return {"skuId": s, "bidPrice": bp, "ok": False, "reason": str(e)[:90]}
    gap = max(0.0, float(min_interval or 0.0))
    workers = max(1, min(int(concurrency or 1), 16))

    def _run(batch):
        """跑一轮。串行时走 `core.pace('bybt.apply')` 限速。

        ★限速状态在 core 里按 key 持有，**天然跨轮次共享**——早期本地实现每轮重置，
          重投轮第一条紧贴上一轮最后一条、间隔只剩 `verify_wait`；生产上 verify_wait=8s
          恰好遮住了，但那是**靠无关参数意外兜底**，调小就漏。护栏不能依赖别的参数取值。"""
        if workers <= 1 or len(batch) <= 1:
            out_rows = []
            for r in batch:
                pace("bybt.apply", gap)      # 补「上次开始→本次开始」，快机器上才真正等待
                out_rows.append(_one(r))
            return out_rows
        with ThreadPoolExecutor(max_workers=workers) as ex:   # ex.map 保持输入顺序
            return list(ex.map(_one, batch))

    results = _run(valid)
    ok = sum(1 for x in results if x["ok"])
    out = {"executed": True, "total": len(valid), "success_receipt": ok, "fail_receipt": len(valid) - ok,
           "concurrency": workers, "min_interval": gap, "confirm_token": confirm, "results": results,
           "_note_success": "success_receipt=apply回执成功(异步会偏低,别当准);真实报上看 verified.newly_reported"}
    if verify:
        by_sku = {str(r["skuId"]): r for r in valid}
        rounds = []
        for rd in range(max(0, int(retry_rounds or 0)) + 1):
            _t.sleep(max(0.0, verify_wait))                   # 等异步/审核落库
            after = applied_sku_ids(activity_id, area_id)
            not_reported = sorted(submitted - after)          # 确实没报上(多为跨活动已有超补/卡控)
            rounds.append({"round": rd, "reported_so_far": len((after - before) & submitted),
                           "still_not_reported": len(not_reported)})
            # ★只重投**已证实没报上**的 —— 前后集合差是权威，故零重复报名风险。
            #   卡控被挡 ≠ 没报上，所以绝不能拿 results 里的 ok=False 去重投。
            if rd >= int(retry_rounds or 0) or not not_reported:
                break
            retry_rows = [by_sku[s] for s in not_reported if s in by_sku]
            if not retry_rows:
                break
            more = _run(retry_rows)
            results.extend(more)
            rounds[-1]["retried"] = len(retry_rows)
        newly = sorted((after - before) & submitted)          # 本次真·新报上(含审核中)
        already = sorted(before & submitted)                  # 报前就已占用(去重本应挡的)
        out["results"] = results
        out["rounds"] = rounds
        out["verified"] = {"newly_reported": len(newly), "already_occupied": len(already),
                           "not_reported": len(not_reported), "newly_reported_skus": newly,
                           "not_reported_skus": not_reported,
                           "note": "以已报名(含审核中)前后差为准,权威。newly=本次新报上,not_reported=没报上(查卡控原因看 results)"}
    # ★把「站内同款」卡控学进账本 —— 它是确定性的（实测同一批 3 款连着两天被拒、
    #   指向的同款 SKU 一模一样），不记就每天白撞一次。下次 plan_bulk_enroll 自动剔除。
    try:
        out["same_item_learned"] = record_same_item_blocks(out.get("results") or [])
    except Exception as e:                                     # 学习失败不能影响报名结果
        out["same_item_learned"] = {"error": str(e)[:120]}
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="超级补贴(原百亿补贴) 查询自测")
    ap.add_argument("--area", type=int, default=DEFAULT_AREA_ID)
    ap.add_argument("--eligible", action="store_true")
    ap.add_argument("--applied", metavar="ACTIVITY_ID")
    ap.add_argument("--price", metavar="SKU")
    a = ap.parse_args()
    if a.eligible:
        r = list_eligible(a.area, page_size=5)
        print("可报 totalCount:", r["totalCount"])
        for it in r["items"]:
            print(" ", it["skuId"], it["suggestPrice"], str(it["name"])[:30])
    if a.applied:
        r = get_applied(a.applied, a.area, page_size=5)
        print("已报名 totalCount:", r["totalCount"], "有效条数:", len(r["items"]))
    if a.price:
        print(_json.dumps(price_info(a.price, a.area), ensure_ascii=False, indent=1, default=str))
