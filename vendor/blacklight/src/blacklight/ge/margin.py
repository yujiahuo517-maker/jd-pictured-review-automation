"""ge 黄金眼「京喜专区 · 业务实时监控 · 毛利监控」——**实时毛利桥**（逐项拆解）。

页面 `ge.jd.com/hjysjmh/gep/view/micro-app/234263-jx-index-overview`，
接口与 `couponbatch` 同一个 `lowCodeDataQuery`，但**参数是另一套**：

| | couponbatch（券批次） | 本模块（毛利监控） |
|---|---|---|
| `resId` / `menuId` | 21663 / 21663 | **34047**（`resId` 传**字符串**） |
| `resAppKey` | lowcode4384 | **lowcode234263** |
| `erpDeptSign` | cateDeptGlb | **cateErpGlb** |
| 指标前缀 | `jdr_sch_*` | **`jdr_jx_*@jx`** |
| 归属过滤 | `saler_dept_id_2` | **`cate_op_erp`**（商品归属采销） |
| 下钻维度 | `groupList=['batch_id'/'sku_id']` | **`par_degree`**（值即维度名） |
| 版本字段 | `versions:21` + `t` | **都不传** |

## ★★`b-ext-device-info` 是必需的（2026-08-11 实测）
不带这个头一律 `{"message":"no auth:专用菜单权限校验","status":-1}`——
换 `menuId`/`resAppKey`/`RequestUrl` 都救不回来，我为此白试了两轮。
它是**浏览器设备指纹**，从用户抓包取得；**会过期**，失效表现就是上面那句 no auth，
到时候重新抓一次该页任意 `lowCodeDataQuery` 请求即可。
存在 `config.json → ge.device_info`，不入代码。

## ★毛利桥公式（2026-08-11 逐项验算，分毫不差）
    预估投后履约毛利 = 成交金额 − 商品成本
                     + 优惠券平台补贴 + 事业部优惠券承担
                     + 新人价平台补贴 + 事业部促销承担
                     − 物流成本 − CPS佣服
                     − 总红包金额 + 平台承担红包
                     − 广告消耗
实测：37,272.47 − 24,061.81 + 152.97 + 0 + 1,157.00 + 0
      − 8,853.80 − 36.50 − 7,597.77 + 7,597.52 − 961.49 = **4,668.59** ✓

★**这条桥证明 easybi 的券成本拆分是真实经济关系**：`优惠券平台补贴` 是**加项**，
平台承担那部分以补贴形式回到毛利里（红包同理）。所以采销只实际承担自己那份，
「采销承担 / 平台承担」不是记账游戏。判「采销担多少」以 easybi / osw 毛利监控为准。

## ★★顶部筛选：`filterList` 对**未知字段静默忽略**，"没报错"证明不了任何事
2026-08-11 逐项探测（判据=行数/金额是否变化），**并塞了一个「完全瞎编的字段」做阴性对照**：
瞎编字段的表现与所有猜测字段**完全一致**（行数 860、金额 52751.35，与不加筛选一模一样）
⇒ 未知字段被静默吞掉，**无法从"没报错"推断筛选生效**。

| 页面筛选 | 字段 | 状态 |
|---|---|---|
| 运营（商品归属采销） | `cate_op_erp` | ✓ 生效（基线就在用） |
| **商品 SKU**（页面写上限 500） | **`sku_id`** | ✓ 生效（860→1 / →2） |
| **SPU** | **`spu_id`** | ✓ 生效（860→8） |
| 京喜经营模式 / 品类 / 平台 / 城市线级 / 小123类目 / CPS团长 / 采购类型 / 事业部内部分摊标识 | 未知 | ✗ **名称未知**，我猜的 `business_model`/`cate_id_3`/`platform`/`city_level`/`bu_share_flag` 全部与瞎编字段无异 |

⚠️`batch_id` / `jdr_sch_page_promotion_id` **能当 `par_degree` 下钻维度，但当筛选无效**
  （与 couponbatch 那边 `cateop_dept_id_2` 能当维度不能当筛选是同一类现象）。
  要「某张券下的明细」用 `drill(degree='coupon')`，或去 `couponbatch.sku_breakdown()`。
⇒ 其余筛选名待从**带筛选的抓包**里取。

## 与 osw 毛利监控的分工
- **osw**（`queryPreDiscountHome`）：预估毛利 / 实际单均毛利 → **判「亏不亏」**（`margin.is_losing()`）。
- **ge 本模块**：同样实时，但给**逐项拆解** → **查「亏在哪一项」**，
  且可按 部门/采销/品类/SPU/SKU/小三类货/供应商/**促销**/**优惠券** 下钻（`par_degree`）。
"""
from __future__ import annotations

import uuid as _uuid

import httpx

from blacklight.core import BlacklightError, gateway, scene_cfg, fetch_paged
from blacklight.core import auth as jd_auth

BASE = gateway("ge")
PATH = "/hjy/ge/rest/api/lowCode/lowCodeDataQuery"
RES_ID = "34047"                      # ★字符串，不是 int
MENU_ID = "34047"
RES_APP_KEY = "lowcode234263"
PAGE_URL = "http://ge.jd.com/hjysjmh/gep/view/micro-app/234263-jx-index-overview"

# 毛利桥各项（顺序即公式顺序）
BRIDGE = [
    ("成交金额", "jdr_jx_trade_deal_ord_ord_amt_jx_trade@jx", +1),
    ("商品成本", "jdr_jx_trade_deal_ord_sku_cost_jx_trade@jx", -1),
    ("优惠券平台补贴", "jdr_jx_mkt_platform_subsidy_coupon__ord_amt_jx_mkt@jx", +1),
    ("事业部优惠券承担", "jdr_jx_mkt_bu_subsidy_coupon_amt_jx_mkt@jx", +1),
    ("新人价平台补贴", "jdr_jx_mkt_nw_price_platform_subsidy_promotion__ord_amt_jx_mkt@jx", +1),
    ("事业部促销承担", "jdr_jx_mkt_bu_subsidy_promotion__ord_amt_jx_mkt@jx", +1),
    ("物流成本", "jdr_jx_delv_delv_ord_expense_jx_delv@jx", -1),
    ("CPS佣服", "jdr_jx_mkt_compute_commission_ord_amt_jx_mkt@jx", -1),
    ("总红包金额", "jdr_jx_mkt_use_red_pack_red_pack__ord_amt_jx_mkt@jx", -1),
    ("平台承担红包", "jdr_jx_mkt_platform_subsidy_red_pack__ord_amt_jx_mkt@jx", +1),
    ("广告消耗", "jdr_jx_ad_consume_ad_amt_jx_ad@jx", -1),
]
# ★★命名与直觉相反，已用算术定死（2026-08-11，893 款当日实测）：
#     ord_gp 17,756.67 − 广告 1,226.67 = 16,530.00 = sku_gp = after_ad
#   ⇒ **`ord_` 是投前（预估履约毛利）**、**`sku_` 是投后**（与 fo_..._after_ad 逐行完全相等，
#     最大差 0.0000）。别按前缀猜，`sku_`/`ord_` 不表示"按SKU/按订单"。
#   （曾误标成 sku_=投前 并记为"存疑"，现已解决。）
M_AFTER_AD = "fo_jdr_jx_after_ad_gross_profit_saler_view@jx"         # 预估投后履约毛利（桥右边）
M_GROSS = "jdr_jx_trade_promise_sku_gross_profit_jx_trade@jx"        # ★同上，也是**投后**
M_PRE_AD = "jdr_jx_trade_promise_ord_gross_profit_jx_trade@jx"       # 预估履约毛利（**投前**）
METRICS = [M_GROSS, M_AFTER_AD] + [c for _, c, _ in BRIDGE]

_CLIENT = None


def _device_info() -> str:
    di = (scene_cfg("ge") or {}).get("device_info")
    if not di:
        raise BlacklightError(
            "缺 ge.device_info（b-ext-device-info 头）。这个头是**必需**的，"
            "缺了一律 no auth:专用菜单权限校验。请在 ge 毛利监控页抓一条 "
            "lowCodeDataQuery 请求，把 b-ext-device-info 的值写进 "
            "core/config.json 的 ge.device_info。")
    return di


def shared_client() -> httpx.Client:
    """★**共享**客户端（全局单例）——**绝对不要 `with shared_client()`**，
    with 退出会把它关掉，后续所有 ge 调用报
    `Cannot send a request, as the client has been closed`（2026-08-21 踩）。
    注意本包里同名的 `osw/margin._client()` / `yx/bybt._client()` 是**每次新建**，
    那两个用 `with` 是对的 —— 同名不同义，所以这个改叫 shared_client。
    2026-08-24：构造收敛到 `ge/client.ge_client`（UA 保留本模块原来的 150.0.0.0）。"""
    from blacklight.ge.client import ge_client
    return ge_client(PAGE_URL, MENU_ID, RES_APP_KEY,
                     ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"))
    ck = jd_auth.session_cookie()
    if not ck:
        raise BlacklightError("没有登录态 cookie，先跑 python -m blacklight.core.login")
    jar = httpx.Cookies()
    for part in ck.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar.set(k.strip(), v.strip(), domain=".jd.com")
    erp = jd_auth.current_pin()
    _CLIENT = httpx.Client(
        cookies=jar, timeout=90.0, follow_redirects=True,
        headers={
            "Accept": "*/*", "Content-Type": "application/json",
            "LoginErp": erp, "Origin": "http://ge.jd.com", "Referer": "http://ge.jd.com/",
            "RequestUrl": PAGE_URL, "X-Requested-With": "XMLHttpRequest",
            "menuId": MENU_ID, "resAppKey": RES_APP_KEY,
            "b-ext-device-info": _device_info(),
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
        },
    )
    return _CLIENT


# ★下钻维度（`par_degree` 的取值 = 维度字段名本身；同时要进 dimList/groupList/attributeList）
#   页面「多维分析」那排 tab 与之对应。2026-08-11 实测通过。
DEGREES = {
    "sku": "sku_id",
    "spu": "spu_id",                               # SPU tab（2026-08-11 实测 337 行）
    "coupon": "batch_id",                          # 优惠券 tab
    "promotion": "jdr_sch_page_promotion_id",      # 促销 tab
}
# 下钻表里额外带的属性（券名/促销名/平台承担比例），随维度不同
# ⚠️★**`attributeList` 不校验字段名**：写什么它就回显什么列，字段不存在时**值全是 null**。
#   实测连「完全瞎编的字段」这种中文串都能回显成一列 ⇒ **只看列在不在会被骗，必须看值**。
#   `spu_name` / `jdr_jx_spu_name` / `product_name` 三个候选都是这么"假成功"的，
#   值全 null ⇒ **SPU 维度这条路拿不到名称**，要名字得另走商品接口
#   （SKU 有 `postProductInfoBySku`，SPU 侧未找到对应端点，可先按 SPU→SKU 再查）。
DEGREE_ATTRS = {
    "coupon": ["jdr_sch_coupon_batch_name", "jdr_jx_coupon_platform_ratio"],
    "promotion": ["jdr_sch_promotion__act_name"],
    "sku": [],
    "spu": [],
}
M_COUPON_AMT = "jdr_jx_mkt_use_cps_coupon__ord_amt_jx_mkt@jx"          # 优惠券优惠金额(全额)
M_COUPON_PLAT = "jdr_jx_mkt_platform_subsidy_coupon__ord_amt_jx_mkt@jx"  # 其中平台补贴
M_COUPON_BU = "jdr_jx_mkt_bu_subsidy_coupon_amt_jx_mkt@jx"             # 其中事业部承担
M_GP = M_PRE_AD          # 下钻里的「预估毛利」= **投前**；投后另取 M_AFTER_AD
M_AMT = "jdr_jx_trade_deal_ord_ord_amt_jx_trade@jx"                    # 成交金额
M_QTY = "jdr_jx_trade_deal_ord_parent__ord_dis_qtty_jx_trade@jx"       # 成交父单量
M_PROMO_AMT = "jdr_jx_mkt_use_promotion_promotion__ord_amt_jx_mkt@jx"  # 促销优惠金额(全额)
M_PROMO_BU = "jdr_jx_mkt_bu_subsidy_promotion__ord_amt_jx_mkt@jx"      # 其中事业部承担
M_AD_SPEND = "jdr_jx_ad_consume_ad_amt_jx_ad@jx"                       # 广告消耗



_client = shared_client        # 兼容旧调用点；新代码请用 shared_client
#   ↑ 保留别名是为了不动 ge/ad.py 等已有调用；**新写的代码用 shared_client**，
#     名字里带 shared 才能让 `with` 的误用在读代码时就看得出来。
def drill(start: str, end: str, degree: str = "coupon", erp: str = None,
          realtime: bool = True, page_size: int = 2000, sku_ids: list = None) -> dict:
    """★**哪张券 / 哪个促销吃掉了多少毛利** —— 按 `优惠券` / `促销` / `SKU` 下钻。

    对应页面「多维分析」那排 tab。`degree` 取 coupon / promotion / sku。

    ★**采销实际承担 = 优惠券优惠金额 − 平台补贴 − 事业部承担**
      （`平台承担比例` 字段 `jdr_jx_coupon_platform_ratio` 也直接给，可交叉验算）

    ⚠️`code 2000` 是部分失败不是失败（见模块 docstring），本函数照 `bridge()` 处理。
    `sku_ids`：把口径**限定到指定 SKU 集合**。跨源比对前必须用它对齐范围——
      不加时是**整个 cate_op_erp 范围**，与 easybi 只统计传入 SKU 的口径不可比
      （2026-08-11 曾因此误判「规律不成立」）。★页面标注 SKU 框上限 500。

    ⚠️`pageSize` 硬上限 2000，且**离线窗口的 SKU 数远超它**（当日 895 / 离线 7 日 >2000）
      ⇒ 本函数自动翻页（走 `core.fetch_paged`，重复吐页也能发现）。
      实测 `promotion` 维度用 pageSize=200 时**正好回 200 行**——那就是被截断。
    """
    erp = erp or jd_auth.current_pin()
    dim = DEGREES.get(degree, degree)
    attrs = DEGREE_ATTRS.get(degree, [])
    # ⚠️**减免指标要跟维度配套**：促销维度用「促销优惠金额/事业部促销承担」，
    #   券维度才用「优惠券优惠金额/平台补贴/事业部优惠券承担」。
    #   混用不会报错，只会算出一堆「促销行上的券成本」，看着像数、其实答非所问。
    if degree == "promotion":
        cut, plat, bu = M_PROMO_AMT, None, M_PROMO_BU
    else:
        cut, plat, bu = M_COUPON_AMT, M_COUPON_PLAT, M_COUPON_BU
    mets = [M_AMT, M_GP, M_AFTER_AD, M_QTY, M_AD_SPEND, cut] + [m for m in (plat, bu) if m]
    trace = str(_uuid.uuid4())
    body = {
        "filterList": [
            {"propertyName": "dt", "values": [start], "op": ">=", "type": "String"},
            {"propertyName": "dt", "values": [end], "op": "<=", "type": "String"},
            {"propertyName": "time_interval",
             "values": ["BY_SECOND" if realtime else "BY_DAY"], "op": "=", "type": "string"},
            {"propertyName": "cate_op_erp", "values": [erp], "op": "in", "type": "string"},
            {"propertyName": "par_degree", "values": [dim], "op": "=", "type": "String"},
            {"propertyName": "datetype", "values": ["rt" if realtime else "offline"],
             "op": "=", "type": "String"},
            {"propertyName": "page_type", "values": ["2"], "op": "=", "type": "String"},
        ] + ([{"propertyName": "sku_id", "values": [str(x) for x in sku_ids],
               "op": "in", "type": "String"}] if sku_ids else []),
        "dimList": ["dt", "time_interval", "cate_op_erp", "par_degree",
                    "datetype", "page_type", dim],
        "metricList": mets, "groupList": [dim], "attributeList": [dim] + attrs,
        "commonParam": {
            "platformId": 0, "userErp": erp, "pageManagerErp": "zhouantao",
            "period": 0, "startTime": 0, "endTime": 0,
            "indexFreq": "REALTIME" if realtime else "OFFLINE",
            "description": "指标数据集-毛利指标拆解", "allJdMall": False,
            "annotation": "基础", "page": 1, "pageSize": int(page_size),
            "resAppKey": RES_APP_KEY, "traceId": trace, "batchId": str(_uuid.uuid4()),
        },
        "resId": RES_ID, "erpDeptSign": "cateErpGlb",
    }
    # ★离线维度的 SKU 数远超单页上限（2026-08-11 实测：当日 895 款，离线 7 日 >2000），
    #   `pageSize` 硬上限 2000 ⇒ **必须翻页**。走 core 的统一闸，重复吐页也能发现。
    meta_box = {}

    def _one_page(page, size):
        body["commonParam"]["page"] = page
        body["commonParam"]["pageSize"] = size
        body["commonParam"]["traceId"] = str(_uuid.uuid4())
        r = _client().post(BASE + PATH, json=body, headers={"uuid": body["commonParam"]["traceId"]})
        d = r.json()
        if d.get("status") == -1 or "no auth" in str(d.get("message") or ""):
            raise BlacklightError("ge 毛利监控 %s —— b-ext-device-info 多半已过期"
                                  % d.get("message"))
        b = d.get("body") or {}
        m = [x.get("di") for x in (b.get("metaData") or {}).get("meta", [])]
        if m:
            meta_box["meta"] = m
        return b.get("data") or []

    page_size = min(int(page_size), 2000)
    data = fetch_paged(_one_page, page_size,
                       key=lambda x: str(x[0]) if x else None,
                       what="ge drill(%s)" % degree)
    meta = meta_box.get("meta") or []
    rows = []
    for x in data:
        o = {meta[i]: x[i] for i in range(min(len(meta), len(x)))}
        amt = float(o.get(cut) or 0)
        pv = float(o.get(plat) or 0) if plat else 0.0
        bv = float(o.get(bu) or 0) if bu else 0.0
        o["减免_全额"] = round(amt, 2)
        o["减免_平台担"] = round(pv, 2)
        o["减免_事业部担"] = round(bv, 2)
        o["减免_采销实担"] = round(amt - pv - bv, 2)
        o["预估毛利_投前"] = round(float(o.get(M_GP) or 0), 2)
        o["预估毛利_投后"] = round(float(o.get(M_AFTER_AD) or 0), 2)
        o["预估毛利"] = o["预估毛利_投后"]      # ★判亏用投后
        o["广告消耗"] = round(float(o.get(M_AD_SPEND) or 0), 2)
        o["成交金额"] = round(float(o.get(M_AMT) or 0), 2)
        o["单量"] = o.get(M_QTY)
        o["行类型"] = classify(o)      # ★必须在「成交金额」赋值之后
        # ★★无券反事实（**ge 自己就能算，不必等 easybi 的 T-1**）：
        #   毛利桥里券是通过「成交金额已扣券 + 平台补贴加回」体现的
        #   ⇒ 把**采销实担**加回去就是没有券时的毛利。
        #   `无券后单均 = (投后毛利 + 采销实担) / 单量`，>0 ⇒ 券致亏（别涨价）。
        #   ⚠️这是**ge 口径**（投后：含广告/物流/红包/CPS），与 easybi 的
        #     「osw 实际单均毛利 + 券我担」**不是同一个基数**：2026-08-11 实测
        #     37 款里数值只有 22% 接近、**判定方向一致率 70%**，不一致的全在临界带。
        #   ★既然判亏认 ge，反事实就该用同一基数——**用 easybi 口径算反事实
        #     再拿 ge 判亏，是口径混用**。easybi 版留作交叉参考。
        # ★★**任何维度都能算反事实**，不止 SKU：
        #   券/促销维度下，`预估毛利_投后` 就是「**这张券对应的那批订单**赚了多少」，
        #   加回该券的采销实担 = 没有这张券时那批订单的毛利。
        #   ⇒ **这才是对的分析单元**——动作（摘券/退圈/退促）本来就在券促这一级，
        #     SKU 级反事实反而要把多张券混在一起、说不清该动哪张。
        #   促销维度用的是促销减免（见 drill 里 cut/plat/bu 的配套），
        #   所以「促销致亏 vs 结构性」同样算得出来。
        _q = float(o.get("单量") or 0)
        _cut = float(o.get("减免_采销实担") or 0)
        o["无减免后毛利"] = round(o["预估毛利_投后"] + _cut, 2)
        if _q > 0:
            o["无减免后单均"] = round(o["无减免后毛利"] / _q, 4)
            o["反事实判定"] = ("减免致亏（定价没问题，别涨价）"
                               if o["无减免后单均"] > 0 else "结构性（摘券退促都治不了）")
        else:
            o["无减免后单均"] = None
            o["反事实判定"] = None
        # 兼容旧名（SKU 维度语义相同）
        o["无券后单均_ge"] = o["无减免后单均"]
        o["反事实判定_ge"] = o["反事实判定"]
        rows.append(o)
    rows.sort(key=lambda z: -z["减免_采销实担"])
    return {"degree": degree, "dim": dim, "count": len(rows),
            "window": [start, end], "rows": rows}


def bridge(start: str, end: str, erp: str = None, degree: str = "sku_id",
           realtime: bool = True, page_size: int = 100) -> dict:
    """毛利桥：一段时间内各项拆解 + 校验等式是否闭合。

    start/end: 实时用 'YYYY-MM-DD HH:MM:SS'（同日），离线用日期。
    degree: `par_degree`，取 sku_id / spu_id / cate / saler_erp / promotion / coupon 等。
    """
    erp = erp or jd_auth.current_pin()
    trace = str(_uuid.uuid4())
    flt = [
        {"propertyName": "dt", "values": [start], "op": ">=", "type": "String"},
        {"propertyName": "dt", "values": [end], "op": "<=", "type": "String"},
        {"propertyName": "time_interval", "values": ["BY_SECOND" if realtime else "BY_DAY"],
         "op": "=", "type": "string"},
        {"propertyName": "cate_op_erp", "values": [erp], "op": "in", "type": "string"},
        {"propertyName": "par_degree", "values": [degree], "op": "=", "type": "String"},
        {"propertyName": "datetype", "values": ["rt" if realtime else "offline"],
         "op": "=", "type": "String"},
        {"propertyName": "page_type", "values": ["2"], "op": "=", "type": "String"},
    ]
    body = {
        "filterList": flt,
        "dimList": ["dt", "time_interval", "cate_op_erp", "par_degree", "datetype", "page_type"],
        "metricList": METRICS, "groupList": [], "attributeList": [],
        "commonParam": {
            "platformId": 0, "userErp": erp, "pageManagerErp": "zhouantao",
            "period": 0, "startTime": 0, "endTime": 0,
            "indexFreq": "REALTIME" if realtime else "OFFLINE",
            "description": "指标数据集-毛利指标拆解", "allJdMall": False,
            "annotation": "基础", "page": -1, "pageSize": int(page_size),
            "resAppKey": RES_APP_KEY, "traceId": trace, "batchId": str(_uuid.uuid4()),
        },
        "resId": RES_ID, "erpDeptSign": "cateErpGlb",
    }
    r = _client().post(BASE + PATH, json=body, headers={"uuid": trace})
    d = r.json()
    if d.get("status") == -1 or "no auth" in str(d.get("message") or ""):
        raise BlacklightError("ge 毛利监控 %s —— b-ext-device-info 多半已过期，重抓一次"
                              % d.get("message"))
    hdr = d.get("header") or {}
    code = str(hdr.get("code"))
    b = d.get("body") or {}
    meta = [m.get("di") for m in (b.get("metaData") or {}).get("meta", [])]
    data = b.get("data") or []
    # ★★`code 2000`（"部分指标查询失败"）**不是硬错误**——它照样带数据回来，
    #   只是**悄悄少了几个指标**。曾把它当致命错误抛，白查了好几轮。
    #   真正危险的是「少的那几个正好是桥里的减项」⇒ 毛利会算高。
    #   所以：有数据就用，但**必须显式报出缺了哪些指标**，绝不静默。
    missing = [m for m in METRICS if m not in meta]
    if not data:
        raise BlacklightError("ge 毛利监控无数据 (code=%s) %s"
                              % (code, str(hdr.get("desc"))[:100]))
    if code not in ("200", "2000", "None"):
        raise BlacklightError("ge 毛利监控: %s" % str(hdr.get("desc"))[:120])
    row = {meta[i]: data[0][i] for i in range(min(len(meta), len(data[0])))}

    items, calc = [], 0.0
    for name, mcode, sign in BRIDGE:
        got = mcode in row
        v = float(row.get(mcode) or 0)
        calc += sign * v
        items.append({"项": name, "值": round(v, 2) if got else None,
                      "符号": "+" if sign > 0 else "−",
                      "缺失": not got})
    after = float(row.get(M_AFTER_AD) or 0)
    closed = abs(calc - after) < 0.05
    return {
        "window": [start, end], "erp": erp, "degree": degree,
        "预估履约毛利": round(float(row.get(M_GROSS) or 0), 2),
        "预估投后履约毛利": round(after, 2),
        "桥算出": round(calc, 2),
        # ★不闭合 = 公式漂了 或 有指标缺失，**别把结果当数**
        "闭合": closed,
        "缺失指标": missing,
        "_警告": ("有 %d 个指标未返回(code=%s)，桥必然不闭合，别当数用" % (len(missing), code)
                  if missing else (None if closed else "指标齐全但桥不闭合，公式可能已变")),
        "拆解": items, "_raw": row,
    }


# --------------------------------------------------------------------------- #
# 判亏权威（2026-08-11 用户拍板：判亏认 ge，osw 降为预测/成本底料）
# --------------------------------------------------------------------------- #
ROW_KINDS = ("有成交亏损", "广告空耗", "幽灵行", "零毛利", "盈利")


def classify(row: dict) -> str:
    """★**一行到底是什么** —— 判亏之前必须先分类，否则会把三个不同的问题混成一堆。

    2026-08-11 实测（离线 08-10，5,417 行）：

    | 类型 | 款数 | 说明 |
    |---|---:|---|
    | 有成交 | 1,179 | 真正的**商品毛利**问题 |
    | **广告空耗**（零成交、只有广告费） | **1,128** | 亏损 −2,259.45 **100% 是广告费**（商品成本/物流/券全 0） |
    | **幽灵行**（除单量外全零） | **3,109** | 纯噪声 |

    ⚠️★这三类是**三个不同的问题**，混在一起报「亏损 1,190 款」是错的：
      · 有成交亏损 → 止亏主线（改价/摘券）
      · 广告空耗   → **广告线**（jzt），不是商品定价问题
      · 幽灵行     → 剔除
    ⚠️零成交行的 `单量` 恒为 **1**，那是**占位值不是真实单量**
      （成交金额与商品成本同时为 0，根本没有订单）。别拿它算单均。

    实时同样受影响：当日 915 行里 206 行是零成交，
    「247 款在亏」其实只有 **41 款**是商品毛利问题，其余是广告空耗。
    """
    amt = float(row.get("成交金额") or 0)
    gp = row.get("预估毛利_投后")
    gp = float(gp) if gp is not None else 0.0
    if amt > 0:
        return "有成交亏损" if gp < 0 else ("零毛利" if gp == 0 else "盈利")
    # 零成交
    ad = float(row.get(M_AD_SPEND) or 0) if M_AD_SPEND in row else 0.0
    if gp < 0 or ad > 0:
        return "广告空耗"
    return "幽灵行"


def judge_losing(row: dict, by: str = "after_ad", require_gmv: bool = True) -> bool:
    """★**判「这款商品亏不亏」的唯一入口（ge 口径）**。

    by='after_ad'（默认）—— 用**预估投后履约毛利**，即已扣广告。
    by='pre_ad'        —— 投前，只在专门讨论「广告前就亏」时用。
    require_gmv=True（默认）—— **零成交的行不算「商品在亏」**，见 `classify()`：
        那批的亏损 100% 是广告空耗，属广告线。关掉它会把亏损款数虚增 6 倍
        （实时当日 41 → 247）。

    ★★**必须用投后**：2026-08-11 当日实测，同一批 915 款，
      投后判亏 **252 款**、投前只有 **35 款**——差 7 倍。
      前缀会骗人：`ord_` 是投前、`sku_` 是投后（见文件头算术证明）。

    ⚠️与 `osw.margin.is_losing()` 的分工（**别混用**）：
      · 本函数 = **已发生/正在发生**（ge 实际成交口径）→ 止血
      · osw    = **预测**（配置已亏、可能还没出单）→ 前瞻拦截
      两者不是同一总体的两种算法，**发现阶段取并集、不要取交集**。
    """
    key = "预估毛利_投后" if by == "after_ad" else "预估毛利_投前"
    v = row.get(key)
    if v is None:
        raise BlacklightError(
            "行内缺 %s，无法判亏。ge drill() 应已提供；"
            "若为 code=2000 少返指标，宁可报错也别当 0 处理" % key)
    if require_gmv and float(row.get("成交金额") or 0) <= 0:
        return False
    return float(v) < 0
