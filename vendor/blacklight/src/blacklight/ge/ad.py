"""ge 黄金眼 **广告运营** —— SKU/SPU 级广告数据（menu 34047 的第二个 tab）。

2026-08-12 抓包接入。用户明确：**jzt 里没有 SKU 级广告消耗数据** ⇒ ge 是唯一来源。
分工因此定死：

    ge  → 发现「哪个 SKU 在空耗 / ROI 多少」（SKU/SPU 级，唯一数据源）
    jzt → 执行「拉黑 / 停投 / 改预算」（计划·单元级，唯一操作入口）
    两边靠 **SPU** 接，**不要拿 SKU 级消耗去对 jzt 的计划级花费**——
    实测那样算会得出「64% 的广告费没有操作入口」的假结论（计划花费无法按 skuIdList 摊到 SKU）。

## ★★命名陷阱：带 `_discount` 的是**折前**
    jdr_jx_ad_consume_ad_amt_jx_ad@jx...            = **折后**（实际计费，7 折后）
    jdr_jx_ad_consume_ad_amt_jx_ad_discount@jx...   = **折前**

2026-08-12 复刻看板实证（2026-08-11，cate_op_erp=wangruihan9，京喜自营）：

    广告成交金额  33,835.67   看板 ¥3.38万      ✓
    消耗(plain)    2,502.74   看板 ¥0.25万      ✓
    广告投后毛利   4,167.33   看板 ¥4,167.33    ✓ 分毫不差
    成交 ÷ plain     13.52    看板「折后ROI」   ✓
    **plain ÷ discount = 0.7000  ⇒ 正好 7 折**

⇒ `ge.margin` 一直用的 `..._ad@jx`（不带后缀）**就是折后**，没有系统性偏差。
   这跟「京准通 xlsx 那列名叫折后实为折前」是**同一个陷阱换了个地方**。

## ★三个口径必须同时对齐，否则差一倍
同一天同一个人，只因这三项不同就差 2 倍（5,023.72 vs 2,502.74）：

| 维度 | 毛利监控 | 广告运营 |
|---|---|---|
| `page_type` | **2** | **3** |
| 指标后缀 | `@jx`（全量成交） | `@jx_ads_ord1`（**仅广告归因订单**） |
| 经营模式 | 不筛 | `jdr_jx_sku_jx_sale_mode_type=new_jdly`（京喜自营） |

## ⚠️未知指标**静默返回 0**
不报错、不缺列。**判某个指标名对不对，必须塞一个瞎编指标做阴性对照**：
瞎编的回 0，真指标回非零。见 `silent-failure-needs-negative-control`。
"""
from __future__ import annotations

import uuid as _uuid

from blacklight.core import BlacklightError
from blacklight.core.paging import fetch_paged
from blacklight.ge import margin as _gm

SALE_MODE_JX_SELF = "new_jdly"        # 京喜自营
PAGE_TYPE_AD = "3"                    # 广告运营 tab（毛利监控是 2）
SFX = "@jx_ads_ord1"                  # ★广告归因订单口径，不是全量成交

# 名称 → 指标编码（后缀统一 @jx_ads_ord1）
METRICS = {
    "广告成交金额": "jdr_jx_trade_deal_ord_ord_amt_jx_trade",
    "广告单量": "jdr_jx_trade_deal_ord_parent__ord_dis_qtty_jx_trade",
    "广告商品成本": "jdr_jx_trade_deal_ord_sku_cost_jx_trade",
    "广告履约毛利": "jdr_jx_trade_promise_sku_gross_profit_jx_trade",
    "广告投后毛利": "fo_jdr_jx_after_ad_gross_profit_saler_view",
    "消耗": "jdr_jx_ad_consume_ad_amt_jx_ad",                    # ★折后（实际计费）
    "消耗_折前": "jdr_jx_ad_consume_ad_amt_jx_ad_discount",       # ★名字骗人，这个是折前
    "物流成本": "jdr_jx_delv_delv_ord_expense_jx_delv",
    "优惠券平台补贴": "jdr_jx_mkt_platform_subsidy_coupon__ord_amt_jx_mkt",
    "事业部优惠券承担": "jdr_jx_mkt_bu_subsidy_coupon_amt_jx_mkt",
    "新人价平台补贴": "jdr_jx_mkt_nw_price_platform_subsidy_promotion__ord_amt_jx_mkt",
    "事业部促销承担": "jdr_jx_mkt_bu_subsidy_promotion__ord_amt_jx_mkt",
    "CPS佣服": "jdr_jx_mkt_compute_commission_ord_amt_jx_mkt",
    "总红包金额": "jdr_jx_mkt_use_red_pack_red_pack__ord_amt_jx_mkt",
    "平台承担红包": "jdr_jx_mkt_platform_subsidy_red_pack__ord_amt_jx_mkt",
}
DEGREES = {"sku": "sku_id", "spu": "spu_id"}
_PROBE = "jdr_jx_ad_consume_ad_amt_zzz_bogus_probe"   # 阴性对照：真回 0 才说明筛选/指标层可信


def drill(start: str, end: str, degree: str = "sku", erp: str = None,
          sale_mode: str = SALE_MODE_JX_SELF, sku_ids: list = None,
          page_size: int = 2000, probe: bool = True) -> dict:
    """按 SKU/SPU 拉广告数据（离线 T-1）。

    `sale_mode=None` 可取消经营模式过滤（**会与看板对不上**，看板默认只看京喜自营）。
    `probe=True` 会额外请求一个瞎编指标做阴性对照——它必须回 0，否则指标层不可信。
    """
    dim = DEGREES.get(degree)
    if not dim:
        raise BlacklightError("degree 仅支持 %s" % list(DEGREES))
    erp = erp or _gm.jd_auth.current_pin()
    names = list(METRICS)
    mets = [METRICS[n] + SFX for n in names] + ([_PROBE + SFX] if probe else [])

    flt = [
        {"propertyName": "cate_op_erp", "values": [erp], "op": "in", "type": "string"},
        {"propertyName": "par_degree", "values": [dim], "op": "=", "type": "String"},
        {"propertyName": "datetype", "values": ["offline"], "op": "=", "type": "String"},
        {"propertyName": "page_type", "values": [PAGE_TYPE_AD], "op": "=", "type": "String"},
        {"propertyName": "time_interval", "values": ["BY_DAY"], "op": "=", "type": "string"},
        {"propertyName": "dt", "values": [start], "op": ">=", "type": "String"},
        {"propertyName": "dt", "values": [end], "op": "<=", "type": "String"},
    ]
    dims = ["cate_op_erp", "par_degree", "dt", "datetype", "page_type", "time_interval", dim]
    if sale_mode:
        flt.insert(1, {"propertyName": "jdr_jx_sku_jx_sale_mode_type",
                       "values": [sale_mode], "op": "in", "type": "String"})
        dims.insert(1, "jdr_jx_sku_jx_sale_mode_type")
    if sku_ids:
        flt.append({"propertyName": "sku_id", "values": [str(x) for x in sku_ids],
                    "op": "in", "type": "String"})

    body = {
        "filterList": flt, "dimList": dims, "metricList": mets,
        "groupList": [dim], "attributeList": [dim],
        "commonParam": {
            "platformId": 0, "userErp": erp, "pageManagerErp": "zhouantao",
            "period": 0, "startTime": 0, "endTime": 0, "indexFreq": "OFFLINE",
            "description": "指标数据集-广告毛利指标拆解", "allJdMall": False,
            "annotation": "基础", "page": 1, "pageSize": min(int(page_size), 2000),
            "resAppKey": _gm.RES_APP_KEY, "traceId": str(_uuid.uuid4()),
            "batchId": str(_uuid.uuid4()),
        },
        "resId": _gm.RES_ID, "erpDeptSign": "cateErpGlb",
    }
    meta_box = {}

    def _one_page(page, size):
        body["commonParam"]["page"] = page
        body["commonParam"]["pageSize"] = size
        body["commonParam"]["traceId"] = str(_uuid.uuid4())
        r = _gm.shared_client().post(_gm.BASE + _gm.PATH, json=body,
                               headers={"uuid": body["commonParam"]["traceId"]})
        d = r.json()
        if d.get("status") == -1 or "no auth" in str(d.get("message") or ""):
            raise BlacklightError("ge 广告运营 %s —— b-ext-device-info 多半已过期"
                                  % d.get("message"))
        b = d.get("body") or {}
        m = [x.get("di") for x in (b.get("metaData") or {}).get("meta", [])]
        if m:
            meta_box["meta"] = m
        return b.get("data") or []

    data = fetch_paged(_one_page, min(int(page_size), 2000),
                       key=lambda x: str(x[0]) if x else None, what="ge ad drill(%s)" % degree)
    meta = meta_box.get("meta") or []
    idx = {m: meta.index(m) for m in meta}

    def _v(row, code):
        i = idx.get(code + SFX)
        if i is None or i >= len(row):
            return None
        try:
            return float(row[i] or 0)
        except Exception:
            return None

    rows = []
    for r in data:
        o = {dim: r[idx[dim]] if dim in idx and idx[dim] < len(r) else None}
        for n in names:
            o[n] = round(_v(r, METRICS[n]) or 0, 2)
        c, amt = o["消耗"], o["广告成交金额"]
        o["折后ROI"] = round(amt / c, 2) if c else None
        o["空耗"] = (c > 0 and amt == 0)      # ★零成交仍花钱
        # ★★2026-08-18 加派生字段「裸毛利」＝**真正的投前毛利**（未扣任何广告费）。
        #   恒等式（实测分毫不差）：
        #       裸毛利 = 成交 − 商品成本 − 物流 − CPS佣服 + 优惠券平台补贴
        #              = 广告履约毛利 + 消耗_折前
        #              = 广告投后毛利 + 消耗          （消耗=折后）
        #   ⇒ **`广告履约毛利` 不是投前毛利**，它已经扣了**折前**广告费（比投后口径更严）；
        #     `广告投后毛利` 扣的是**折后**。两个都是投后，只是扣费口径不同。
        #   把 `广告履约毛利` 当投前用会得出**方向相反**的结论：2026-08-18 我据此报出
        #   「74 款投前就亏、是定价/成本问题」，用本式重算后**真正投前为负的是 0 款**，
        #   48 款的亏损 100% 来自广告费（处置该走拉黑，不是改价/换供）。
        #   旁证：同一行会出现「投后 > 履约」(如 +223.72 vs −347.46)，差额恰为折前−折后。
        o["裸毛利"] = round(o["广告履约毛利"] + o["消耗_折前"], 2)
        o["裸毛利率"] = round(o["裸毛利"] / amt, 4) if amt else None
        rows.append(o)

    warn = None
    if probe:
        bogus = sum((_v(r, _PROBE) or 0) for r in data)
        if bogus:
            warn = "★阴性对照失败：瞎编指标回了非零(%s) ⇒ 指标层不可信，结论作废" % bogus
    return {
        "窗口": "%s~%s" % (start, end), "degree": degree, "dim": dim,
        "经营模式": sale_mode or "(未筛，会与看板对不上)",
        "count": len(rows), "rows": rows, "阴性对照": warn or "通过（瞎编指标回 0）",
        "_口径": "★`消耗`=**折后**(实际计费)；`消耗_折前`=折前，两者比值实测 0.7000。"
                 "★**`广告履约毛利` 不是投前毛利**——它已扣**折前**广告费；`广告投后毛利` 扣**折后**。"
                 "要真·投前请用派生字段 **`裸毛利`**(=履约毛利+消耗_折前=投后毛利+消耗)。"
                 "指标后缀 %s = **仅广告归因订单**，与毛利监控的 @jx(全量) 不可比。" % SFX,
    }


def waste(days: int = 7, min_cost: float = 10.0, erp: str = None, top: int = 50,
          end_date: str = None) -> dict:
    """**广告空耗**（近 N 日零成交仍在花钱），折后口径。

    ⚠️**别用今日口径**：今日零成交可能只是还没出单，拿它拉黑会误杀。
    ⚠️金额极度长尾——实测七日 2,220 款/8,410 元，**8% 的款占一半金额** ⇒ 按金额切不按款数。
    """
    import datetime as _dt
    # ★窗口自由：ge 离线本就按 `dt>=start / dt<=end` 取（抓包实证，其他看板同理）。
    #   `end_date` 显式指定窗口末日 ⇒ 可回溯历史；默认仍是「截至昨天」。
    if end_date:
        end = _dt.date.fromisoformat(str(end_date))
        if end >= _dt.date.today():
            raise BlacklightError("end_date 必须 ≤ 昨天（ge 离线到 T-1），收到 %s" % end_date)
    else:
        end = _dt.date.today() - _dt.timedelta(days=1)
    start = end - _dt.timedelta(days=days - 1)
    d = drill(start.isoformat(), end.isoformat(), degree="sku", erp=erp)
    w = [r for r in d["rows"] if r["空耗"]]
    w.sort(key=lambda r: -r["消耗"])
    tot = sum(r["消耗"] for r in w)
    hit = [r for r in w if r["消耗"] >= min_cost]
    return {
        "窗口": d["窗口"], "阴性对照": d["阴性对照"],
        "空耗款数": len(w), "空耗合计(折后)": round(tot, 2),
        "门槛": "单款 ≥%.1f 元" % min_cost,
        "达标款数": len(hit), "达标金额": round(sum(r["消耗"] for r in hit), 2),
        "占空耗金额%": round(sum(r["消耗"] for r in hit) / tot * 100) if tot else 0,
        "Top": [{"skuId": r["sku_id"], "消耗": r["消耗"]} for r in hit[:top]],
        "_下一步": "★ge 只负责发现，处置走 jzt（计划/单元级）：按 **SPU** 找计划，"
                   "个别 SKU 空耗→拉黑；整计划都空耗→停投。"
                   "别拿 SKU 级消耗去对 jzt 的计划级花费（摊不下去）。",
    }


# ---------- 下载中心：SKU 级全科目明细（比 drill 多 13 个科目） ----------
DL_PATH = "/hjy/ge/rest/api/lowCode/downloadDataservice"
LINK_PATH = "/hjy/gep/api/downloadCenter/getDownloadLink"
DL_MENU_ID = 3497                       # ★下载中心的 menuId，与看板 34047 不同

# (指标编码, 安全列名)。★顺序即 fieldOrder 即 xlsx 列序。
DL_METRICS = [
    ("jdr_jx_trade_deal_ord_parent__ord_dis_qtty_jx_trade@jx_ads_ord1&parent_ord_not_null_str", "成交父单量"),
    ("jdr_jx_trade_deal_ord_ord_amt_jx_trade@jx_ads_ord1", "成交金额"),
    ("jdr_jx_trade_deal_ord_user_price_jx_trade@jx_ads_ord1", "客单价"),
    ("jdr_jx_trade_promise_ord_gross_profit_jx_trade@jx_ads_ord1", "预估履约毛利"),
    ("jdr_jx_trade_promise_ord_gross_profit_rate_jx_trade@jx_ads_ord1", "预估履约毛利率"),
    ("jdr_jx_trade_promise_sku_gross_profit_jx_trade@jx_ads_ord1", "预估投后履约毛利"),
    ("jdr_jx_trade_promise_sku_gross_profit_rate_jx_trade@jx_ads_ord1", "预估投后履约毛利率"),
    # ★账本 post_margin 一直用的就是这个（saler_view），下载页默认没勾；不补进来换源
    #   会**悄悄改掉 post_margin 口径**，历史 run 之间不可比。
    ("fo_jdr_jx_after_ad_gross_profit_saler_view@jx_ads_ord1", "广告投后毛利"),
    # ★★平台把下面这个标成「折后消耗」是**错的** —— 2026-08-21 同请求对照实测
    #   折后/折前 = 0.7000（403 行），`_discount` 数值更大 ⇒ 它是**折前**。
    #   照平台列名当折后用，广告消耗会**高估 43%**。这里改名，别再传播错标签。
    ("jdr_jx_ad_consume_ad_amt_jx_ad_discount@jx_ads_ord1", "消耗_折前"),
    ("jdr_jx_ad_consume_ad_amt_jx_ad@jx_ads_ord1", "消耗_折后"),      # ★这个才是实际计费
    ("jdr_jx_ad_impression_ad_qtty", "展现"),
    ("jdr_jx_ad_click_ad_qtty", "点击"),
    ("jdr_jx_ad_consume_ad_rate@jx_ads_ord1", "广告费率"),
    ("jdr_jx_ad_deal_ord_ord_roi@jx_ads_ord1", "ROI"),
    ("jdr_jx_ad_click_ad_ctr", "CTR"),
    ("jdr_jx_ad_click_ad_cvr", "CVR"),
    ("fo_jdr_jx_ad_cpc@jx_ads_ord1", "CPC"),
    ("fo_jdr_jx_ad_cpa@jx_ads_ord1", "CPA"),
    ("jdr_jx_trade_deal_ord_sku_cost_jx_trade@jx_ads_ord1", "商品成本"),
    ("jdr_jx_mkt_platform_subsidy_coupon__ord_amt_jx_mkt@jx_ads_ord1", "优惠券平台补贴"),
    ("jdr_jx_mkt_bu_subsidy_coupon_amt_jx_mkt@jx_ads_ord1", "事业部优惠券承担"),
    ("jdr_jx_mkt_nw_price_platform_subsidy_promotion__ord_amt_jx_mkt@jx_ads_ord1", "新人价补贴"),
    ("jdr_jx_mkt_bu_subsidy_promotion__ord_amt_jx_mkt@jx_ads_ord1", "事业部促销承担"),
    ("jdr_jx_delv_delv_ord_expense_jx_delv@jx_ads_ord1", "物流成本"),
    ("jdr_jx_mkt_compute_commission_ord_amt_jx_mkt@jx_ads_ord1", "CPS佣服"),
    ("jdr_jx_mkt_use_red_pack_red_pack__ord_amt_jx_mkt@jx_ads_ord1", "总红包金额"),
    ("jdr_jx_mkt_platform_subsidy_red_pack__ord_amt_jx_mkt@jx_ads_ord1", "平台承担红包"),
]
_DL_PROBE = ("jdr_jx_zzz_bianzao_metric@jx_ads_ord1", "阴性对照")


def download_detail(start: str, end: str, erp: str = None, degree: str = "sku",
                    sale_mode: str = SALE_MODE_JX_SELF, probe: bool = True,
                    out_dir: str = None, timeout_s: int = 300, poll_s: int = 1,
                    refresh: bool = False) -> dict:
    """**下载中心**取 SKU/SPU 级全科目明细（离线）。触发 → 取链接 → 下 xlsx → 解析成行。

    比 `drill` 多出判「亏在哪一项」要的科目：商品成本 / 优惠券平台补贴 / 事业部优惠券承担 /
    新人价补贴 / 事业部促销承担 / 物流成本 / CPS佣服 / 红包 / 预估投后履约毛利率。

    ## 三步链路（2026-08-21 抓包接入并跑通）
      1. `POST /hjy/ge/rest/api/lowCode/downloadDataservice` → **异步**，只回 `taskId`
      2. `GET  /hjy/gep/api/downloadCenter/getDownloadLink?taskId=&menuId=3497` → S3 直链
         （`X-Amz-Expires=50000` 约 14 小时；★**menuId 是 3497**，不是看板的 34047）
      3. GET 直链 → xlsx（`PK` 开头）

    ## ★★别信平台给的中文列名
    平台把 `..._ad_amt_jx_ad_discount` 标成「折后消耗」，**实为折前**。
    本函数改名为 `消耗_折前`，并**额外取不带后缀的 `消耗_折后`**（实际计费口径）。
    实测同请求对照 403 行：`折后/折前 = 0.7000`。照平台列名用会把消耗高估 43%。

    ⚠️`probe=True` 塞一个瞎编指标做阴性对照：该列必须**全空**，否则指标层不可信。
    ⚠️窗口自由（`dt>=start / dt<=end`），单日就传 start==end；ge 离线只到 T-1。
    """
    import datetime as _dt
    import os as _os
    import time as _time
    import uuid as _uuid2

    dim = DEGREES.get(degree)
    if not dim:
        raise BlacklightError("degree 仅支持 %s" % list(DEGREES))
    if _dt.date.fromisoformat(str(end)) >= _dt.date.today():
        raise BlacklightError("end 必须 ≤ 昨天（ge 离线到 T-1），收到 %s" % end)
    erp = erp or _gm.jd_auth.current_pin()
    mets = list(DL_METRICS) + ([_DL_PROBE] if probe else [])

    flt = [
        {"propertyName": "dt", "values": [start], "op": ">=", "type": "String"},
        {"propertyName": "dt", "values": [end], "op": "<=", "type": "String"},
        {"propertyName": "datetype", "values": ["offline"], "op": "=", "type": "String"},
        {"propertyName": "cate_op_erp", "values": [erp], "op": "in", "type": "string"},
        {"propertyName": "par_degree", "values": [dim], "op": "=", "type": "String"},
        {"propertyName": "page_type", "values": [PAGE_TYPE_AD], "op": "=", "type": "String"},
        {"propertyName": "time_interval", "values": ["BY_DAY"], "op": "=", "type": "string"},
    ]
    dims = ["dt", "datetype", "cate_op_erp", "par_degree", "page_type", "time_interval", dim]
    if sale_mode:
        flt.insert(4, {"propertyName": "jdr_jx_sku_jx_sale_mode_type",
                       "values": [sale_mode], "op": "in", "type": "String"})
        dims.insert(3, "jdr_jx_sku_jx_sale_mode_type")

    fname = "bl_%s_%s_%s" % (degree, start, end)

    # ★★本地缓存：**每调一次就在平台建一个下载任务**，调试时反复调会把用户的
    #   「下载中心」刷满同名任务（2026-08-21 一天刷了 5 条 bl_sku_2026-08-17_2026-08-17）。
    #   ge 离线是 T-1 定稿的历史数据，**同窗口重复下必然同结果** ⇒ 有本地文件就直接用。
    #   要强制重下传 `refresh=True`。
    _cache_dir = out_dir or _os.getcwd()
    _cached = _os.path.join(_cache_dir, fname + ".xlsx")
    if (not refresh) and _os.path.isfile(_cached) and _os.path.getsize(_cached) > 1024:
        return _parse_dl_xlsx(_cached, mets, start, end, probe, task_id="(本地缓存)")

    tid = str(_uuid2.uuid4())
    body = {
        "filterList": flt, "dimList": dims, "metricList": [m for m, _ in mets],
        "groupList": [dim], "attributeList": [dim],
        "commonParam": {
            "platformId": 0, "userErp": erp, "pageManagerErp": "zhouantao",
            "period": 0, "startTime": 0, "endTime": 0, "indexFreq": "OFFLINE",
            "description": "指标数据集-多维分析_广告SKUSPU", "allJdMall": False,
            "annotation": "下载", "page": -1, "pageSize": 2000,
            "sortField": "jdr_jx_trade_deal_ord_ord_amt_jx_trade@jx_ads_ord1", "sortAsc": False,
            "resAppKey": _gm.RES_APP_KEY, "traceId": tid,
        },
        "resId": _gm.RES_ID, "erpDeptSign": "cateErpGlb",
        "download": {
            "menuId": int(_gm.RES_ID),
            "dimensionInfos": [{"column": dim, "columnName": dim.upper(),
                                "dimensionExtras": {"mappingType": "SKU_INFO", "mappingInfos": [
                                    {"dimkey": "skuProductName", "dimName": "商品名称"},
                                    {"dimkey": "skuProductId", "dimName": "商品ID"}]}}],
            "indicatorInfos": [{"column": m, "columnName": n,
                                "compareCalType": "COMPARE_WITH_ABS", "significantFigures": 2}
                               for m, n in mets],
            "fileName": fname, "maxSize": 400000, "sheetName": "数据",
            "fieldOrder": [dim] + [m for m, _ in mets],
        },
    }

    # ⚠️**别 `with _gm.shared_client()`**：它是**共享**客户端，with 退出会把它关掉，
    #   后续所有 ge 调用报 "Cannot send a request, as the client has been closed"（2026-08-21 踩）。
    c = _gm.shared_client()
    if True:
        r = c.post(_gm.BASE + DL_PATH, json=body,
                   headers={"uuid": tid, "menuId": str(_gm.RES_ID)})
        j = r.json()
        task_id = j.get("taskId")
        if not task_id:
            raise BlacklightError("触发下载失败，无 taskId：%s" % str(j)[:200])

        url, deadline = None, _time.time() + timeout_s
        while _time.time() < deadline:
            lr = c.get(_gm.BASE + LINK_PATH,
                       params={"taskId": task_id, "menuId": DL_MENU_ID},
                       headers={"uuid": str(_uuid2.uuid4()), "menuId": str(DL_MENU_ID)})
            b = (lr.json() or {}).get("body")
            if isinstance(b, str) and b.startswith("http"):
                url = b
                break
            _time.sleep(poll_s)
        if url is None:
            raise BlacklightError("下载任务 %s 在 %ds 内没出链接（去下载中心看）" % (task_id, timeout_s))

        # ★2026-08-24：原来默认写**当前工作目录** —— MCP server 的 cwd 通常是仓库根，
        #   于是每天往仓库里掉一个 bl_sku_*.xlsx（还不在 .gitignore 里，一次 git add -A 就误入库）。
        #   产物一律落 runtime/exports，与其它导出同处。
        from blacklight.core import paths as _bl_paths
        out_dir = out_dir or _bl_paths.exports_dir("ge_download")
        _os.makedirs(out_dir, exist_ok=True)
        path = _os.path.join(out_dir, fname + ".xlsx")
        fr = c.get(url)
        if fr.content[:2] != b"PK":
            raise BlacklightError("下到的不是 xlsx（前16字节 %r）" % fr.content[:16])
        with open(path, "wb") as fh:
            fh.write(fr.content)

    return _parse_dl_xlsx(path, mets, start, end, probe, task_id=task_id)


def _parse_dl_xlsx(path, mets, start, end, probe, task_id=None) -> dict:
    """解析下载中心 xlsx → 行。缓存命中与新下载共用同一段，避免两处逻辑跑偏。"""
    import openpyxl as _ox
    # ⚠️**别用 read_only=True**：平台导出的 xlsx 缺 dimension 信息，read_only 下
    #   `ws.values` 只吐表头就停、**不报错** ⇒ 静默返回 0 行（2026-08-21 实测 508 行被读成 0）。
    wb = _ox.load_workbook(path)
    ws = wb["数据"] if "数据" in wb.sheetnames else wb.active
    raw = list(ws.values)
    wb.close()
    if not raw:
        return {"rows": [], "count": 0, "file": path, "taskId": task_id}
    names = ["商品名称", "商品ID"] + [n for _, n in mets]
    rows = []
    for rr in raw[1:]:
        rows.append({names[i]: rr[i] for i in range(min(len(names), len(rr)))})

    out = {"rows": rows, "count": len(rows), "file": path, "taskId": task_id,
           "窗口": "%s~%s" % (start, end), "columns": names,
           "_口径": "page_type=3 + @jx_ads_ord1(广告归因订单) + 京喜自营；"
                    "**消耗_折后**才是实际计费；平台把 _discount 标成「折后」是错标签(实为折前,比值0.7000)"}
    if probe:
        bad = [r for r in rows if r.get("阴性对照") not in (None, "", 0, "0")]
        out["阴性对照"] = ("✓ 瞎编指标全空，指标层可信" if not bad
                          else "✗ 瞎编指标有值 %d 行 —— 指标层不可信，别用这批数" % len(bad))
    return out
