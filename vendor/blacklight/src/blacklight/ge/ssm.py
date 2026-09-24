"""ge 黄金眼「京喜专区 · 爆品运营 · 顺手买运营」——**顺手买（搭售）看板取数**。

页面 `ge.jd.com/hjysjmh/gep/view/micro-app/224473-jx_ssm_test_v1`。
官方版看板在 `流量-黄金流程-搭售概况`（`micro-app/4402-huangliudapeigou4402`）。

## 与本域另两个模块的接口差异

| | couponbatch / margin | **本模块** |
|---|---|---|
| PATH | `/hjy/ge/rest/api/lowCode/lowCodeDataQuery` | **`/hjy/ge/exd/api/lowCode/postEXDData.ajax`** |
| 寻址 | `resId` + `resAppKey` | **`apiName`**（业务接口名）+ `resId="32564"` |
| menuId | 21663 / 34047 | **32564** |
| `b-ext-device-info` | margin **必需**（不带就 no auth） | **不需要**（2026-08-20 实测） |
| 分页 | `pageSize` | **`pageSizeEXD`**（传 5000 一次拉全） |

## 三个 apiName（同一 body 结构，只改 `apiName` + `group` + `opType`）

| apiName | group / opType | 产出 |
|---|---|---|
| `ssm_major_term_grid_explosive_form1_dept1_test1` | `dept_id_1` / `search` | 京喜整体 + C2 基准（**只有大数**） |
| 同上 | `dept_id_2` / `drillDown` | 自己的 C3 组汇总 |
| `ssm_grid_form2_cate3_v1` | `item_third_cate_cd,dept_id_1,dept_id_2` | 三级类目 |
| `ssm_major_term_grid_dept_sku_v1` | `item_sku_id,...,saler_erp_acct` | 全量 SKU（**带销售员**、带优化建议） |

## ★★三个坑（2026-08-20 实测）

**1. 返回值全是「区间日均」，不是累计值。** 后台把总量除以天数再返回，所以小数点后
一长串。单日口径噪声极大——同一个组 8/19 单日单均损益 **+¥0.57**，8/01–8/19 口径
**−¥0.209**，结论会完全反过来。**任何结论都要用 ≥7 天口径。**

**2. 数据权限只到自己的 C3 组，越权返回的是「200 + status:-1」不是报错。**
改 `deptLevl="1"` 或去掉 `deptId2Str` 会拿到 `{"message":"no auth","status":-1}`。
要 C2 基准只能用「保留 `deptId2Str` + `group="dept_id_1"`」这条路（见 `dept_baseline`）。
本模块一律显式检查 `body` 键，缺了就抛错，不静默返回空。

**3. 用 curl 复现时 `--data-raw` 不解析 `@file`**（那正是 `--data-raw` 的语义），
必须 `--data-binary @file`，否则 Tomcat 直接 400。

## 字段名（`per_ord_loss` 是**损益**不是亏损，正=盈利）

| 字段 | 含义 |
|---|---|
| `deal_parent_sale_ord_num` / `deal_ord_num` | 成交父单（日均） |
| `fixed_price_deal_ord_num` / `fixed_price_parent_sale_ord_num` | **专享价**成交单 |
| `ssm_promt_deal_ord_rate` | 专享价订单占比 |
| `per_ord_loss` / `fixed_price_per_ord_loss` | 单均损益 / 专享价单均损益（**正=盈利**） |
| `jx_expo_qtty` / `ssm_expo_cid3_qtty` / `ssm_pv` | 京喜曝光 / 大盘曝光 / 该 SKU 顺手买曝光 |
| `ssm_pv_rate` | 渗透 = 京喜曝光 / 大盘曝光 |
| `ssm_promt_sku_num` / `ssm_promt_sku_rate` | 专享价提报 SKU 数 / 占在售比 |
| `lower_80_off_promt_sku_rate` | 提报品里低于 8 折的占比 |
| `new_jd_price` / `fixed_price` | 前台价 / 专享价（0 = 未提报） |
| `saler_erp_acct` | **销售员 ERP**——「我自己的」按这个筛，不是采销助理口径 |
| `optimization_suggestion_1/2/3` | 提报专享价 / 检测活动 / 专享价与前台价价差小 |

业务分析逻辑见 `docs/ge/NOTES_ssm.md`。
"""
from __future__ import annotations

import uuid as _uuid

import httpx

from blacklight.core import BlacklightError, gateway
from blacklight.core import auth as jd_auth

BASE = gateway("ge")
PATH = "/hjy/ge/exd/api/lowCode/postEXDData.ajax"
RES_ID = "32564"
MENU_ID = "32564"
PAGE_URL = "http://ge.jd.com/hjysjmh/gep/view/micro-app/224473-jx_ssm_test_v1"

API_DEPT = "ssm_major_term_grid_explosive_form1_dept1_test1"
API_CATE3 = "ssm_grid_form2_cate3_v1"
API_SKU = "ssm_major_term_grid_dept_sku_v1"

DEFAULT_DEPT_1 = "16267"   # 居家百货采销部
DEFAULT_DEPT_2 = "16333"   # 收纳用品组

_CLIENT = None


def _client() -> httpx.Client:
    """★共享单例，**别 `with _client()`**。2026-08-24 收敛到 `ge/client.ge_client`；
    本模块无 resAppKey、UA 原为 151.0.0.0，均按原样传入。"""
    from blacklight.ge.client import ge_client
    return ge_client(PAGE_URL, MENU_ID, None)
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
    _CLIENT = httpx.Client(
        cookies=jar, timeout=90.0, follow_redirects=True,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/json",
            "LoginErp": jd_auth.current_pin(),
            "Origin": "http://ge.jd.com",
            "Referer": "http://ge.jd.com/",
            "RequestUrl": PAGE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "menuId": MENU_ID,
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
        },
    )
    return _CLIENT


def _post(api_name: str, group: str, start: str, end: str,
          compare_start: str, compare_end: str, op_type: str = "search",
          dept_1: str = DEFAULT_DEPT_1, dept_2: str = DEFAULT_DEPT_2,
          extra: dict = None, page_size: int = 5000) -> list:
    """底层调用。★越权时后台回 200 + `status:-1`，这里一律抛错，不静默返回空表。"""
    q = {
        "erpDeptSign": "cateDeptGlb", "allJdMall": "false",
        "deptLevl": "2", "deptId2Str": dept_2, "dept_id_1": dept_1,
        "dateType": "custom", "startDate": start, "endDate": end, "interval": "DAY",
        "compareConfig": "hb", "compareType": "hb",
        "compareStartDate": compare_start, "compareEndDate": compare_end,
        "opType": op_type, "group": group, "submitParams": {},
    }
    if extra:
        q.update(extra)
    body = {"apiGroupName": "jx_search", "apiName": api_name, "apiType": "api",
            "resId": RES_ID, "easyDataAlias": "VIRGO",
            "pageNumberEXD": 0, "pageSizeEXD": page_size, "queryParams": q}
    r = _client().post(BASE + PATH, json=body, headers={"uuid": str(_uuid.uuid4())})
    r.raise_for_status()
    d = r.json()
    if "body" not in d:
        raise BlacklightError(
            "顺手买接口没回 body：%s（status=%s）。"
            "最常见原因是**数据范围越权**——本账号权限只到自己的 C3 组，"
            "改 deptLevl/去掉 deptId2Str 都会拿到这个，而不是报错。"
            % (d.get("message"), d.get("status")))
    rows = d["body"].get("data") or []
    if len(rows) >= page_size:
        raise BlacklightError("返回行数达到 pageSizeEXD=%d，可能被截断——调大 page_size 重拉，"
                              "别拿这份数据下结论" % page_size)
    return rows


def dept_baseline(start: str, end: str, compare_start: str, compare_end: str,
                  dept_1: str = DEFAULT_DEPT_1, dept_2: str = DEFAULT_DEPT_2) -> list:
    """京喜整体 + C2 部门基准（**只有大数，拿不到兄弟 C3 组**）。

    ★必须保留 `deptId2Str`（自己的 C3）再把 group 换成 `dept_id_1`——
    这是权限内唯一能看到 C2 的路子。去掉 `deptId2Str` 会 no auth。
    """
    return _post(API_DEPT, "dept_id_1", start, end, compare_start, compare_end,
                 op_type="search", dept_1=dept_1, dept_2=dept_2)


def group_summary(start: str, end: str, compare_start: str, compare_end: str,
                  dept_1: str = DEFAULT_DEPT_1, dept_2: str = DEFAULT_DEPT_2) -> dict:
    """自己 C3 组的汇总（单行）。"""
    rows = _post(API_DEPT, "dept_id_2", start, end, compare_start, compare_end,
                 op_type="drillDown", dept_1=dept_1, dept_2=dept_2)
    if not rows:
        raise BlacklightError("C3 组汇总返回空——检查 dept_2 是否是自己有权限的组")
    return rows[0]


def by_cate3(start: str, end: str, compare_start: str, compare_end: str,
             dept_1: str = DEFAULT_DEPT_1, dept_2: str = DEFAULT_DEPT_2) -> list:
    """三级类目明细。找机会盘看 `ssm_expo_cid3_qtty`（大盘曝光）大而 `ssm_pv_rate` 低的。"""
    return _post(API_CATE3, "item_third_cate_cd,dept_id_1,dept_id_2",
                 start, end, compare_start, compare_end,
                 op_type="search", dept_1=dept_1, dept_2=dept_2)


def by_sku(start: str, end: str, compare_start: str, compare_end: str,
           dept_1: str = DEFAULT_DEPT_1, dept_2: str = DEFAULT_DEPT_2,
           sku_dia: str = "598252e2-bd9f-4fae-a68e-f0aabbd71761") -> list:
    """全量 SKU 明细（带 `saler_erp_acct` 销售员、`optimization_suggestion_*` 优化建议）。

    `sku_dia` 是页面「商品诊断」tab 的会话标识，看板换版可能变；变了从页面重新抓一次。
    """
    return _post(API_SKU, "item_sku_id,dept_id_1,dept_id_2,item_third_cate_cd,saler_erp_acct",
                 start, end, compare_start, compare_end, op_type="search",
                 dept_1=dept_1, dept_2=dept_2, extra={"sku_dia": sku_dia})


def channel_compare(sku_rows: list) -> dict:
    """★**同 SKU 内「走专享价 vs 走非专享价」的单均损益对比**——本看板最有价值的一个算法。

    专享价是独立促销、**不与券促叠加**，所以走专享价成交时那些我担 100% 的冲单券用不上；
    没走专享价的订单反而叠券、亏得更深。只取「既有专享价成交又有非专享价成交」的 SKU
    做同 SKU 对比，**消除选品偏差**（不同 SKU 的毛利结构差异会淹没这个信号）。

    非专享价单均 = (总单 × 总单均 − 专享单 × 专享单均) / (总单 − 专享单)

    实证（收纳用品组 2026-08-01~19，26 个可对比 SKU）：
    专享价 **+¥0.289** vs 非专享价 **−¥0.445**，**差 +¥0.734/单**，69% 的 SKU 专享价更赚。

    ⇒ 提专享价不是「让利换量要花预算」，是**屏蔽自担券的止亏动作**。
    """
    g = lambda r, k: (r.get(k) or 0)
    out = []
    for r in sku_rows:
        o, fo = g(r, "deal_ord_num"), g(r, "fixed_price_parent_sale_ord_num")
        pl, fpl = r.get("per_ord_loss"), r.get("fixed_price_per_ord_loss")
        if pl is None or fpl is None or fo <= 0 or o - fo <= 0:
            continue
        non = (o * pl - fo * fpl) / (o - fo)
        out.append({"sku": r.get("item_sku_id"), "cate": r.get("item_third_cate_name"),
                    "ord": o, "fixed_ord": fo, "fixed_pl": fpl, "non_fixed_pl": non,
                    "gap": fpl - non})
    if not out:
        return {"n": 0, "note": "没有同时存在两种成交方式的 SKU，无法做同 SKU 对比"}
    w = sum(x["fixed_ord"] for x in out)
    return {
        "n": len(out),
        "fixed_pl_w": round(sum(x["fixed_ord"] * x["fixed_pl"] for x in out) / w, 4),
        "non_fixed_pl_w": round(sum(x["fixed_ord"] * x["non_fixed_pl"] for x in out) / w, 4),
        "gap_w": round(sum(x["fixed_ord"] * x["gap"] for x in out) / w, 4),
        "win_rate": round(len([x for x in out if x["gap"] > 0]) / len(out), 3),
        "rows": sorted(out, key=lambda x: -x["ord"]),
    }
