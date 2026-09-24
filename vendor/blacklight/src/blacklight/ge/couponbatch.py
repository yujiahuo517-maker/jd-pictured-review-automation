"""ge 黄金眼「优惠券批次明细」—— **券名 / 发券方 / 成本分摊比例 / 全额券成本** 的来源。

平台 **ge（黄金眼数据门户）**，网关 `ge.back.jd.com`，页面
`ge.jd.com/hjysjmh/gep/view/micro-app/4384-couponBatch`。
⚠️**不是 easybi**——见 `blacklight/ge/__init__.py` 的两平台对照表。

## ★页面是两个接口拼出来的（这就是「嵌套结构」）
    POST /hjy/ge/rest/api/lowCode/lowCodeDataQuery  → **只回指标**（券成本/ROI/单量）
    POST /hjy/gep/api/getDimensionAttr              → **券名/发券人/部门/面额/分摊比例**
只调第一个永远拿不到券名（第一版就是这么错的）。用 `batch_detail()` 一步 join。

## 为什么需要它
easybi 券数据集 `1043053` **只有 `batch_id`、没有券名字段**——此前只能靠
「我担 + 平台担 = 面额」的金额指纹去猜是哪张券。本接口直接给批次名称与分摊比例。

## 三个坑（都实测撞过）
1. ★**`pageSize` 默认 50，是请求体里的参数不是平台硬限**。页面上那句
   「仅展示50条数据」+ 分页器只显示 1 页 ⇒ **被截断了完全看不出来**。
   本模块默认 `page_size=500` 并**校验返回行数 < page_size**，够不上就抛错。
2. ★**`indexFreq`**：`REALTIME` 只能查最近 2 天（页面日期选择器只开放两天）；
   要历史区间必须 `OFFLINE`。本模块默认 OFFLINE。
3. ⚠️**`jdr_sch_coupon_inner_new_user` 过滤**：抓包里带了 `="1"`（只看站内新客）。
   这会**大幅缩小结果集**，做成本归因时不要带。本模块默认不带，
   要复现页面数字才传 `inner_new_user=1`。

## ★★分摊比例三源一致，标签会读反（2026-08-11 用户确认）
批次 1315454844（三类货…品类新收纳用品4.01-4元，面额 4.00）：

| 源 | 采销承担 |
|---|---|
| easybi 券集 `1043053` | 3.00/单（75%） |
| **osw/yx 实时毛利监控**（`markettool.query_sku` 的 `我承担`/`我担比例%`） | 3.0 / **75%** |
| ge 本接口 `jdr_sch_coupon_dept_self_ratio` | **75.00%** |

⚠️**ge 页面把这个字段标成「发券业务部门自营商品承担比例」，字面像是"发券方担 75%"，
  但数值等于采销承担**——它说的是**采销/商品部门**。别照字面读反了方向。
（同页 `jdr_sch_coupon_pop_ratio`「POP商品部门承担比例」常年 100%，与
  `dept_self_ratio` 并列时看着矛盾，同样别硬解释，以 easybi / 毛利监控的数值为准。）

## ★口径：算「采销承担」用 easybi，不要用本接口的全额（2026-08-11 用户拍板）
本接口 `优惠券成本` 是**全额**（面额 × 单量）；easybi `1043053` 拆
`优惠券成本-采销承担` / `-平台承担`。**以 easybi 的承担比例为准**——
本接口只用来取**券名/发券人/部门/面额/分摊比例**这些元数据，以及做交叉校验。

两平台严格自洽（sku_id 对齐口径后实测）：
    ge 全额  ==  easybi(采销承担 + 平台承担)
  · 单 SKU 10163019223322：625 == 538 + 87，18 个批次两边互无遗漏
  · A+D 288 款 15 日：106,773 == 87,274 + 19,496（差 3 元进位）
  · `联合承担=否` 的券，ge 全额与 easybi 采销承担**逐条精确相等**（采销 100%）
  · `联合承担=是` 的券才出现拆分（18 个批次，几乎都是 chenlisha10）

⚠️**别拿 ge 全额当采销成本**：会把共补券的平台那份算到自己头上，
  A+D 288 款会虚增 19,496 元（+22.3%），并让录券人排行完全换人
  （chenlisha10 8.9% → 64.8%）——那是错的。
⚠️反证也在数据里：券名自带「（采销承担50%）」的批次 1625513845，
  easybi 恰好记 50%（ge 全额 20 / easybi 我担 10）——**券名写的比例与 easybi 一致**。
⚠️比对两平台前必须先用 `sku_ids=` 把范围对齐：ge 默认是整个部门、easybi 只有你给的 SKU，
  满减券的篮子构成不同会造出假差异（2026-08-11 曾据此误判「规律不成立」）。
"""

# ★别加 hasattr 兜底：取不到 ERP 时退化成某个人的 ERP，会让**别人拿着他的归属静默取数**，
#   一个报错都没有。缺就抛。（2026-08-13 从 jxmargin 回流，那边已按此收口到 core.identity）
from __future__ import annotations

import time
import uuid as _uuid

import httpx

from blacklight.core import BlacklightError, gateway
from blacklight.core import auth as jd_auth
from blacklight.core.paging import check_truncation

BASE = gateway("ge")
PATH = "/hjy/ge/rest/api/lowCode/lowCodeDataQuery"
RES_ID = 21663
MENU_ID = "21663"
RES_APP_KEY = "lowcode4384"
PAGE_URL = "http://ge.jd.com/hjysjmh/gep/view/micro-app/4384-couponBatch"
DEFAULT_DEPT_2 = "16333"          # saler_dept_id_2，收纳用品组所属二级部门

# 券批次属性（随 attributeList 一起回来，都是字符串）
ATTRS = [
    "jdr_sch_coupon_batch_name",           # ★券名
    "jd_erp",                              # 发券人 ERP
    "hr_dept_name_1", "hr_dept_name_2", "hr_dept_name_3",
    "jdr_sch_coupon_cps_type_cd", "jdr_sch_coupon_cps_cate_cd",
    "jdr_sch_coupon_cps_face_value",       # 面额
    "jdr_sch_coupon_consume_lim",          # 限额
    "jdr_sch_coupon_dept_cost_union_flag", # 部门成本联合承担 是/否
    "jdr_sch_coupon_dept_self_ratio",      # 发券业务部门·自营商品承担比例
    "jdr_sch_coupon_dept_pop_ratio",       # 发券业务部门·POP商品承担比例
    "jdr_sch_coupon_pop_ratio",            # POP商品部门承担比例
    "pcap_begin_time", "pacp_end_time",
]
METRICS = [
    "jdr_sch_trade_deal_ord_ord_amt_trade_deal_snapshot_jdr_sch_use_coupon",  # 成交金额
    "jdr_sch_mkt_deal_ord_coupon__ord_cost_coupon_ord",                       # ★券成本(全额)
    "fo_jdr_sch_coupon_roi",                                                  # ROI
    "ge_deal_standard_deal_sub_ord_qtty_jdr_sch_use_coupon",                  # 成交子单
    "jdr_sch_trade_deal_ord_parent__ord_dis_qtty_trade_deal_snapshot_jdr_sch_use_coupon",
    "jdr_sch_user_deal_ord_user_qtty_user_deal_snapshot_jdr_sch_use_coupon",
    "ge_deal_standard_deal_item_qtty_jdr_sch_use_coupon",
]
# 友好列名
ALIAS = {
    "batch_id": "批次号",
    "jdr_sch_coupon_batch_name": "券名",
    "jd_erp": "发券人",
    "hr_dept_name_2": "发券部门", "hr_dept_name_3": "发券组",
    "jdr_sch_coupon_cps_face_value": "面额", "jdr_sch_coupon_consume_lim": "限额",
    "jdr_sch_coupon_cps_type_cd": "券类型",
    "jdr_sch_coupon_dept_cost_union_flag": "联合承担",
    "jdr_sch_coupon_dept_self_ratio": "发券部门自营担%",
    "jdr_sch_coupon_dept_pop_ratio": "发券部门POP担%",
    "jdr_sch_coupon_pop_ratio": "POP商品部门担%",
    "pcap_begin_time": "生效", "pacp_end_time": "结束",
    "jdr_sch_mkt_deal_ord_coupon__ord_cost_coupon_ord": "券成本全额",
    "jdr_sch_trade_deal_ord_ord_amt_trade_deal_snapshot_jdr_sch_use_coupon": "成交金额",
    "fo_jdr_sch_coupon_roi": "ROI",
    "ge_deal_standard_deal_sub_ord_qtty_jdr_sch_use_coupon": "成交子单",
    "jdr_sch_user_deal_ord_user_qtty_user_deal_snapshot_jdr_sch_use_coupon": "成交用户数",
}

_CLIENT = None


def _client() -> httpx.Client:
    """★共享单例，**别 `with _client()`**（with 会关掉它，后续 ge 调用全挂）。
    2026-08-24：实现收敛到 `ge/client.ge_client`（三处曾各写一份，差异只有 header 三个值）。
    UA 保留本模块原来的 150.0.0.0，逐字节不变。"""
    from blacklight.ge.client import ge_client
    return ge_client(PAGE_URL, MENU_ID, RES_APP_KEY,
                     ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"))
    ck = jd_auth.session_cookie()
    if not ck:
        raise BlacklightError("没有登录态 cookie，先跑 python -m blacklight.core.login")
    jar = httpx.Cookies()
    n = 0
    for part in ck.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar.set(k.strip(), v.strip(), domain=".jd.com")
            n += 1
    if not n:
        raise BlacklightError("cookie 串解析出 0 条，格式不对")
    erp = jd_auth.current_pin()
    _CLIENT = httpx.Client(
        cookies=jar, timeout=90.0, follow_redirects=True,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/json",
            "LoginErp": erp,
            "Origin": "http://ge.jd.com",
            "Referer": "http://ge.jd.com/",
            "RequestUrl": PAGE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "menuId": MENU_ID,
            "resAppKey": RES_APP_KEY,
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
        },
    )
    return _CLIENT


def query_batches(start: str, end: str, batch_ids: list = None,
                  sku_ids: list = None,
                  dept_id_2: str = DEFAULT_DEPT_2, page_size: int = 500,
                  offline: bool = True, inner_new_user: bool = False,
                  erp: str = None) -> dict:
    """按批次号 / **商品 SKU** / 部门拉券批次明细。

    start/end: 'YYYY-MM-DD'。batch_ids、sku_ids 都为空则拉该部门全部批次。
    ★返回行数达到 page_size 一律抛错——**宁可报错，不静默截断**（页面就是靠这个坑人的）。

    ## 可用筛选字段（2026-08-11 逐个探测，判据=行数是否下降而非是否报错）
    | 页面筛选 | 字段 | 状态 |
    |---|---|---|
    | 券批次号 | `batch_id` | ✓ |
    | **商品 SKU** | **`sku_id`** | ✓（933→18，且与 easybi 批次数一致） |
    | 运营部门 | `saler_dept_id_2` | ✓ |

    ⚠️**`saler_dept_id_3` 会被静默忽略**——传了不报错、行数与不传完全相同（933=933），
      会让人以为按三级部门筛过了，实际拿的是全量。别用。
    ⚠️`item_sku_id` / `sku` / `main_sku_id` / `cateop_dept_id_2` 作为**筛选**都直接报
      「部分指标查询失败」——注意 `cateop_dept_id_2` **能当维度不能当筛选**，两者不通用。
    """
    erp = erp or jd_auth.current_pin()
    flt = [
        {"propertyName": "dt", "values": [start], "op": ">=", "type": "String"},
        {"propertyName": "dt", "values": [end], "op": "<=", "type": "String"},
        {"propertyName": "saler_dept_id_2", "values": [str(dept_id_2)],
         "op": "in", "type": "string"},
        {"propertyName": "time_interval", "values": ["BY_DAY"], "op": "=", "type": "string"},
    ]
    dims = ["dt", "cateop_dept_id_2", "batch_id", "time_interval"]
    if batch_ids:
        flt.append({"propertyName": "batch_id",
                    "values": [str(b) for b in batch_ids], "op": "in", "type": "String"})
    if sku_ids:
        flt.append({"propertyName": "sku_id",
                    "values": [str(s) for s in sku_ids], "op": "in", "type": "String"})
    if inner_new_user:
        # ⚠️只看站内新客，会大幅缩小结果集；做成本归因别开
        flt.append({"propertyName": "jdr_sch_coupon_inner_new_user",
                    "values": ["1"], "op": "=", "type": "String"})
        dims.append("jdr_sch_coupon_inner_new_user")

    trace = str(_uuid.uuid4())
    body = {
        "filterList": flt,
        "dimList": dims,
        "metricList": METRICS,
        "groupList": ["batch_id"],
        "attributeList": ["batch_id"],
        "commonParam": {
            "platformId": 0, "userErp": erp, "pageManagerErp": "zengxi1",
            "period": 0, "startTime": 0, "endTime": 0,
            "indexFreq": "OFFLINE" if offline else "REALTIME",
            "description": "指标数据集-指标服务-表格", "allJdMall": False,
            "annotation": "基础", "page": -1, "pageSize": int(page_size),
            "sortField": "jdr_sch_mkt_deal_ord_coupon__ord_cost_coupon_ord",
            "sortAsc": False, "resAppKey": RES_APP_KEY,
            "traceId": trace, "batchId": str(_uuid.uuid4()),
        },
        "resId": RES_ID, "erpDeptSign": "cateDeptGlb", "versions": 21,
        "t": int(time.time() * 1000),
    }
    c = _client()
    r = c.post(BASE + PATH, json=body, headers={"uuid": trace})
    if r.status_code != 200:
        raise BlacklightError("lowCodeDataQuery HTTP %s" % r.status_code)
    try:
        d = r.json()
    except Exception:
        raise BlacklightError("lowCodeDataQuery 返回非 JSON（多半是掉登录态跳了登录页）")
    if not isinstance(d, dict):
        raise BlacklightError("lowCodeDataQuery 返回结构异常")
    if d.get("success") is False or (d.get("code") not in (None, 0, "0", 200, "200")):
        raise BlacklightError("lowCodeDataQuery: %s" % (d.get("message") or d.get("code")))

    rows = _extract_rows(d)
    # 2026-08-24：这里原本手写了一份与 core/paging 等价的判断 —— 而 core/paging 的 docstring
    # 里点名的"当事模块"正是本文件。修了 core 却没回头替换，等于两份实现各活各的。
    check_truncation(len(rows), page_size, what="券批次明细",
                     hint="调大 page_size 或按 batch_ids 切片。")
    return {"rows": rows, "count": len(rows), "window": [start, end],
            "offline": offline, "inner_new_user": inner_new_user}


def sku_breakdown(batch_id, start: str, end: str,
                  dept_id_2: str = DEFAULT_DEPT_2, page_size: int = 2000,
                  offline: bool = True, erp: str = None) -> dict:
    """★**一张券摊在哪些商品上**——逐 SKU 的券成本 / ROI / 单量。

    对应页面：批次明细行最右「详情」→ `4015-effectAnalysis` → **单品分析** tab
    （URL 全参数化：`?batch_id=&st=&et=`，可直接跳转）。
    页面那张表 10 条/页、实测 115 页——**别翻页**，这里 `groupList=['sku_id']` 一次取全。

    ⚠️★**`pageSize` 硬上限 2000**，给 2500/3000 直接回 `code 1008 分页参数异常`
      （是报错不是截断，还好）。一张券可以覆盖 >2000 个 SKU
      （实测 1315454844 在 07-27~08-03 就超了），所以**必须翻页**——
      本函数自动 `page=1,2,3…` 循环到某页不满为止，并按 `sku_id` 去重。
    ⚠️返回里 `sku_id` 是**真 SKU 号**；页面表格显示的「商品名称」在
      `sku_id$echoName` 那套字段里，接口这边直接给的就是号，不用再解。
    ⚠️`优惠券成本` 同批次级，是**全额**口径；采销承担要按 easybi 的比例折算。
    """
    erp = erp or jd_auth.current_pin()
    page_size = min(int(page_size), 2000)      # >2000 平台直接 1008 报错
    seen, rows, page = set(), [], 1
    while True:
        trace = str(_uuid.uuid4())
        body = {
            "filterList": [
                {"propertyName": "dt", "values": [start], "op": ">=", "type": "String"},
                {"propertyName": "dt", "values": [end], "op": "<=", "type": "String"},
                {"propertyName": "saler_dept_id_2", "values": [str(dept_id_2)],
                 "op": "in", "type": "string"},
                {"propertyName": "batch_id", "values": [str(batch_id)],
                 "op": "in", "type": "String"},
                {"propertyName": "time_interval", "values": ["BY_DAY"],
                 "op": "=", "type": "string"},
            ],
            "dimList": ["dt", "cateop_dept_id_2", "batch_id", "sku_id", "time_interval"],
            "metricList": METRICS, "groupList": ["sku_id"], "attributeList": ["sku_id"],
            "commonParam": {
                "platformId": 0, "userErp": erp, "pageManagerErp": "zengxi1",
                "period": 0, "startTime": 0, "endTime": 0,
                "indexFreq": "OFFLINE" if offline else "REALTIME",
                "description": "指标数据集-指标服务-表格", "allJdMall": False,
                "annotation": "基础", "page": page, "pageSize": page_size,
                "sortField": "jdr_sch_mkt_deal_ord_coupon__ord_cost_coupon_ord",
                "sortAsc": False, "resAppKey": RES_APP_KEY,
                "traceId": trace, "batchId": str(_uuid.uuid4()),
            },
            "resId": RES_ID, "erpDeptSign": "cateDeptGlb", "versions": 21,
            "t": int(time.time() * 1000),
        }
        r = _client().post(BASE + PATH, json=body, headers={"uuid": trace})
        d = r.json()
        hdr = d.get("header") or {}
        if str(hdr.get("code")) != "200":
            raise BlacklightError("sku_breakdown p%d: %s" % (page, str(hdr.get("desc"))[:110]))
        got = _extract_rows(d)
        fresh = [x for x in got if str(x.get("sku_id")) not in seen]
        for x in fresh:
            seen.add(str(x.get("sku_id")))
        rows += fresh
        # ⚠️翻页去重后没有新行也要停，否则服务端重复吐同一页会死循环
        if len(got) < page_size or not fresh:
            break
        page += 1
        if page > 50:
            raise BlacklightError("翻页超过 50 页（%d 行），怀疑分页异常" % len(rows))
    return {"batch_id": str(batch_id), "rows": rows, "count": len(rows),
            "pages": page, "window": [start, end]}


def _extract_rows(d: dict) -> list:
    """★`body.data` 是**位置数组**（不是 dict），列定义在 `body.metaData.meta`。

    形如 `["1315454844", 42916.26, 159006.0, 0.2699, 39769, ...]`——
    直接找 "含 batch_id 键的 dict" 是找不到的（第一版就栽在这，返回 0 行）。
    """
    b = d.get("body") or {}
    data = b.get("data") or []
    meta = ((b.get("metaData") or {}).get("meta")) or []
    cols = [(m.get("di") or m.get("code") or m.get("name")) for m in meta]
    if not data:
        return []
    if not cols or len(cols) < len(data[0]):
        # 元信息缺失时退回按已知顺序拼（batch_id + METRICS）
        cols = ["batch_id"] + METRICS
    out = []
    for row in data:
        out.append({cols[i]: row[i] for i in range(min(len(cols), len(row)))})
    return out


def get_dimension_attr(batch_ids: list, start: str, end: str,
                       dept_id_2: str = DEFAULT_DEPT_2, chunk: int = 100) -> dict:
    """★**券名/发券人/分摊比例走这个接口**，`lowCodeDataQuery` 只回指标。

    页面是两个请求拼出来的（这就是「嵌套结构」）：
      · `lowCodeDataQuery`  → 指标（券成本/ROI/单量…），位置数组
      · `getDimensionAttr`  → 批次属性（券名/发券人/部门/面额/分摊比例）
    返回 {batch_id: attrs}。
    """
    c = _client()
    out = {}
    ids = [str(b) for b in batch_ids]
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        body = {"dimName": "jdr_sch_coupon_entity",
                "expandParam": {"jdr_sch_coupon_watching_characters": "2",
                                "realtimeGroup": "1",
                                "startTime": start, "endTime": end,
                                "cateop_dept_id_2": str(dept_id_2),
                                "batch_id": ",".join(part),
                                "time_interval": "BY_DAY",
                                "data": [{"batch_id": b} for b in part]}}
        r = c.post(BASE + "/hjy/gep/api/getDimensionAttr", json=body,
                   headers={"uuid": str(_uuid.uuid4())})
        if r.status_code != 200:
            raise BlacklightError("getDimensionAttr HTTP %s" % r.status_code)
        d = r.json()
        for it in ((d.get("body") or {}).get("data") or []):
            a = it.get("attrs") or {}
            if a.get("batch_id"):
                out[str(a["batch_id"])] = a
    return out


def batch_detail(start: str, end: str, batch_ids: list = None,
                 sku_ids: list = None,
                 dept_id_2: str = DEFAULT_DEPT_2, page_size: int = 500,
                 offline: bool = True) -> dict:
    """★一步到位：指标 + 属性 join，返回带券名的完整批次明细。

    `sku_ids` 传了就是「**这个 SKU 挂了哪些券**」——比 easybi 多出券名与分摊比例。

    ⚠️★**券排行也要先过流速，别按累计窗口排**——「15 日失血是向后看的」这条纪律
      对券和对 SKU 一样成立，2026-08-11 实测栽过：
      批次 1315454844 按 15 日累计占我券成本 **53.8% 排第一**，
      拆开看 前8日 59,605 → **近7日只剩 3,024（−95%）**，早已退潮，
      据此去找发券人谈基本是打空。做法：同一批 `sku_ids` 跑两个窗口再比日均。
    ⚠️**它还是部门级大券**：15 日全额 120 万、覆盖 2,235 个 SKU，
      我名下 288 款只占 5.2% —— 「这张券花了多少」和「花在我头上多少」差 20 倍，
      不加 `sku_ids` 就会把别人的成本当成自己的。
    """
    q = query_batches(start, end, batch_ids=batch_ids, sku_ids=sku_ids,
                      dept_id_2=dept_id_2, page_size=page_size, offline=offline)
    rows = q["rows"]
    ids = [str(r.get("batch_id")) for r in rows if r.get("batch_id")]
    attrs = get_dimension_attr(ids, start, end, dept_id_2=dept_id_2) if ids else {}
    merged = []
    for r in rows:
        m = dict(r)
        m.update(attrs.get(str(r.get("batch_id")), {}))
        cost = m.get("jdr_sch_mkt_deal_ord_coupon__ord_cost_coupon_ord")
        qty = m.get("ge_deal_standard_deal_sub_ord_qtty_jdr_sch_use_coupon")
        if cost and qty:
            m["单均券成本"] = round(float(cost) / float(qty), 4)
        merged.append(m)
    merged.sort(key=lambda x: -(x.get("jdr_sch_mkt_deal_ord_coupon__ord_cost_coupon_ord") or 0))
    missing = [i for i in ids if i not in attrs]
    return {"rows": merged, "count": len(merged), "window": [start, end],
            "attrs_missing": missing}


def pretty(rows: list) -> list:
    """把内部指标 code 换成中文列名，并补 单均券成本。"""
    out = []
    for r in rows:
        o = {}
        for k, v in r.items():
            if k.startswith("$") or k.endswith("$value") or k.endswith("$echo") \
               or k.endswith("$realCode") or k.endswith("$echoDim"):
                continue
            o[ALIAS.get(k, k)] = v
        cost = r.get("jdr_sch_mkt_deal_ord_coupon__ord_cost_coupon_ord")
        qty = r.get("ge_deal_standard_deal_sub_ord_qtty_jdr_sch_use_coupon")
        if cost and qty:
            o["单均券成本"] = round(float(cost) / float(qty), 4)
        out.append(o)
    return out
