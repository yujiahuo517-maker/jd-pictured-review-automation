"""easybi 数据集取数：字段目录 + 程序化拼查询。

## ★★★ 口径：两组数据**不可比**（2026-08-07 用户指出并实测证实）

    成交口径      ：osw 毛利监控（按成交预估）/ 京准通 SKU 表 / 京喜_通用数据集(1025136)
    出库+计费口径 ：**京喜_财务_损益(1096116)** —— 京算盘

**跨这两组不可相加、不可相减、不可互相校验。**
实测同一 SKU（10183643513276 / 2026-08）：
    损益集(出库计费) 金额 6279.15 数量 479 毛利 **+1329.17**
    通用集(成交)     金额 5881.88 数量 444 毛利 **−57.27**
金额只差 6.8%，**毛利一正一负** ⇒ 不是"差不多"，是结论会反向。
（我当天就踩过：拿损益集的 −112964 去跟京准通口径的 +18605 相减，推出"没投广告的在亏"——无效。）

## ★★ 广告消耗有「折前 / 折后(7折)」两套，混用会高估亏损 43%

京准通导出的「指标概况_SKU分析」xlsx 里那列**名叫「折后消耗」，实际填的是折前金额**。
实测 6/6 精确成立：`预估投后履约毛利 = 预估履约毛利 − 折前消耗 × 0.7000`。
字段目录里直接有 `171292 广告消耗金额（7折口径）`、`171293 广告后履约毛利（7折口径）`
⇒ 7 折是明确的口径约定。用折前算投后毛利会**高估广告成本 1/0.7−1 = 43%**、从而高估亏损。
另：xlsx 的「成交金额」= `18900607 京喜广告订单成交金额`（**广告口径**，不是全店成交）。

## 接口

## 接口
- `GET  /api/data-hub/dataset/allMetaDataList?datasetId=<id>` → 字段目录（维度+指标）
- `POST /api/data-hub/data/query`                             → 取数

## 请求体结构（2026-08-07 从前端 XHR 抓的真身，25KB）
前端把**字段元数据整个内联**进请求体，所以本模块的做法是：从 `allMetaDataList` 取字段对象
→ 补上查询用的几个键 → 组装。**不硬搬抓来的 blob**（那样换个看板/换个字段就废了）。

字段对象里三个键的关系（关键，别搞混）：
  - `code` / `key` = **实例码** = `baseCode` + 前端随机后缀（拖进分析时生成，如 `..._eMZMlcsT`）
  - `field`        = **`baseCode`**，才是真正参与查询的字段名
  - 指标另需 `aggType`（默认 `AUTO`）与 `dataSetId`
从 `allMetaDataList` 拿到的对象没有随机后缀，`code == baseCode`，直接用即可。

⚠️**同名指标会有多个**：`投后履约毛利额`/`广告后履约毛利率` 这类是**看板自定义计算字段**，
  不同看板各建各的、code 就是中文名，实测 67 个指标里这类重名的有十几个。
  用 `find_fields` 拿到多条时**必须人工确认用哪一个**，别按名字取第一个。
"""
from __future__ import annotations

import uuid
from typing import Optional

from blacklight.core import BlacklightError
from blacklight.easybi import auth
from blacklight.core.paging import check_total

DATASET_JX_PL = 1096116          # 京喜_财务_损益（京算盘-by天损益，看板 113956）
D_DT_CODE = "dt"                 # 日期维度 code（同环比要用它的 id 当 compareColumnId）

# ★**行级数据权限：查询必须带部门过滤**，否则报
#   `CODE 3003：无权控维度组合维度值权限`（结构完全正确也会被拒）。
#
# ⚠️这**不是**可以自动发现的东西 —— 2026-08-07 实测：把 `saler_dept_id_2` 当维度
#   group by（不加过滤）同样被 3003 拒掉。平台连「你有哪些部门」都不让列，
#   因为账号本身只有单个组的数据权限。所以它是**账号事实**，只能配置不能探测。
#   （试过 `datasetAuthRules/fetchUserPermission`：要 `申请的应用类型`+`appId`，
#     参数形状未解；`metric/auth/dimGroupListAuth`：返回空。都不可用。）
#
# 16333 = 二级部门「收纳用品组」，取自看板 113956 自己发的请求。
# 换人/换组时怎么找：打开看板 → 「采销岗部门」选择器选中你的组 → 点查询 →
#   看 `POST /api/data-hub/data/query` 请求体 `queryParam.filter.children` 里
#   `key=saler_dept_id_2` 那条的 `values`。
# 配置优先级：config/easybi.json 的 `dept_id` > 这里的默认值。
DEPT_SHOUNA = "16333"


def _cfg_dept() -> str:
    """从 config/easybi.json 读部门（决策配置入库可追溯），缺省回落到 DEPT_SHOUNA。"""
    try:
        import json as _j
        import os as _os
        from blacklight.core import paths as _p
        fp = _os.path.join(_p.config_dir(), "easybi.json")
        if _os.path.isfile(fp):
            with open(fp, encoding="utf-8") as f:
                v = (_j.load(f) or {}).get("dept_id")
                if v:
                    return str(v)
    except Exception:
        pass
    return DEPT_SHOUNA


DEPT = _cfg_dept()

PATH_META = "/api/data-hub/dataset/allMetaDataList"
PATH_QUERY = "/api/data-hub/data/query"
PATH_INFO = "/api/data-hub/dataset/dataSetInfo"

_META_CACHE: dict = {}
_INFO_CACHE: dict = {}

# 信封：{"header":{"code":"0","desc":null},"body":{...}}
# ⚠️`code` 是**字符串** '0' 不是数字，消息字段叫 `desc` 不是 `msg`（2026-08-07 实证；
#   按数字比会把成功当失败）。`data/query` 那边又是 `msg`——所以两个都取。
_OK_CODES = {"0", "200", 0, 200, None, ""}


def _envelope(j: dict, what: str) -> dict:
    hdr = j.get("header") or {}
    code = hdr.get("code", j.get("code"))
    if code not in _OK_CODES:
        msg = str(hdr.get("msg") or hdr.get("desc") or j.get("msg") or j.get("message") or "")
        # 把平台那句很长的权限报错翻译成可操作的（实测最容易卡住新用户的就是这条）
        if "3003" in msg or "维度值权限" in msg:
            raise BlacklightError(
                "行级数据权限不足（CODE 3003）。当前部门过滤 dept=%s。\n"
                "  · 这不是代码问题：该账号只有单个部门的数据权限，"
                "平台连部门列表都不让列（把 saler_dept_id_2 当维度 group by 同样被拒）；\n"
                "  · 换人/换组请在 config/easybi.json 写 {\"dept_id\": \"你的二级部门ID\"}；\n"
                "  · ID 怎么找：看板选中你的组 → 点查询 → 看 data/query 请求体里 "
                "filter.children 中 key=saler_dept_id_2 那条的 values。\n"
                "  原始报错：%s" % (DEPT, msg[:180]))
        raise BlacklightError("%s 失败 code=%s msg=%s" % (what, code, msg))
    return j.get("body") or j.get("data") or {}


PATH_LIST = "/api/data-hub/dataset/list"
PATH_SCENES = "/api/datasource/model/businessSceneList"


def list_datasets(scene: str = "all", keyword: str = "", page_size: int = 200,
                  offset: int = 0, auth_type: str = "") -> list:
    """**枚举我能看到的数据集**（2026-08-07 抓「选取数据集」弹窗得到）。

    `GET /api/data-hub/dataset/list`，八个参数**一个都不能少**（少了报
    「缺少参数: pageInfo」——其实缺的是 `offset`/`pageSize`，不是嵌套对象；
    `businessScene` 为空则报 `businessScene is empty`）。

    ⚠️返回结构：`body.data.<scene>.data` —— **桶名与传入的 scene 同名**
      （传 all 就在 `all` 桶、传 individual 就在 `individual` 桶）。按 `all` 取会取空。
    scene: 'all'(全部，实测 91 个) / 'individual'(个人+提数推送，75 个) / 'metric_dataset'(空)。
    ★不是自己负责的数据集**也能用**——只要在这个列表里出现就有读权限（用户 2026-08-07 确认）。
    """
    p = {"businessScene": scene, "pageSize": page_size, "sceneIdList": "",
         "searchValue": keyword, "datasetTopic": "", "datasetOrderType": "",
         "datasetAuthType": auth_type, "offset": offset}
    r = auth.request("GET", PATH_LIST, params=p)
    d = _envelope(r.json(), "dataset/list").get("data") or {}
    bucket = d.get(scene) or {}
    return bucket.get("data") or []


def dataset_info(dataset_id: int = DATASET_JX_PL, refresh: bool = False) -> dict:
    """数据集元信息（含 `dimGroupCodeList` / `resAppKey`）。进程内缓存。"""
    if not refresh and dataset_id in _INFO_CACHE:
        return _INFO_CACHE[dataset_id]
    r = auth.request("GET", PATH_INFO, params={"datasetId": dataset_id})
    d = (_envelope(r.json(), "dataSetInfo").get("data")) or {}
    if not d:
        raise BlacklightError("dataSetInfo 空——datasetId 不对或没权限")
    _INFO_CACHE[dataset_id] = d
    return d


# ★采销岗维度组。前端不管哪个数据集都发这个 code——它对应「采销岗部门」这条
#   行级权限链（saler_dept_id_1..4）。**不是列表第一个**：黄金眼那批 4 个组里它排第 3。
DIM_GROUP_SALER = "7a43b98aa558f13279c5add3c0367bc3"


def dim_group_code(dataset_id: int = DATASET_JX_PL) -> str:
    """取 `DIM_GROUP_CODE`（query 必传，缺了报 CODE 1007）。

    ⚠️`dimGroupCodeList` 实测**有多个**（指标集 2 个、黄金眼那批 4 个），是不同的
      **行级权限域**，不是可选装饰。**取错组 = CODE 3003 行级数据权限不足**，
      而报错文案只说"权限不足"、完全不指向维度组，会误判成"这个数据集我没权限"。
      —— 2026-08-10 就是这么把 1043053/1045254 误判成不可用的（取了 `[0]`，
      而正确的是 `7a43b98a...`，在列表里排第 3）。

    所以：**认 code 不认下标**，命中 `DIM_GROUP_SALER` 就用它（前端各数据集都发这个），
    实在没有才退回 `[0]`。
    """
    lst = dataset_info(dataset_id).get("dimGroupCodeList") or []
    if not lst:
        raise BlacklightError("dataSetInfo 里没有 dimGroupCodeList，无法构造查询"
                              "（mode=sql 的 ClickHouse 数据集走的是另一套契约）")
    return DIM_GROUP_SALER if DIM_GROUP_SALER in lst else lst[0]


def query_sql_dataset(dataset_id: int, dims: list, metrics: list, filters: list = None,
                      page_size: int = 500, page_num: int = 1, agg: str = "SUM") -> dict:
    """**`mode=sql` 的 ClickHouse 数据集取数**——第三套查询契约，与指标集/标准集都不同。

    典型：`1044828 黄金眼商品数据集`（75 维 × 256 指标，**按频道拆的成交额**）。

    三处和 `query()` 不一样（2026-08-10 逐个撞出来）：
      1. `dataSet.type` 发 **`individual`**（发 standard→50001，发 metric_dataset→9999）；
      2. **不传 `dimGroupCode`**（这类集根本没有 `dimGroupCodeList`）；
      3. ★**基础指标必须显式给 `aggType`**（默认为空会生成 `AUTO(字段)`，
         ClickHouse 报 `Unknown function AUTO`）。`type=custom` 的派生指标自带 customSql，不受影响。

    ⚠️**行级权限是数据集自带的**，不用也不能传部门过滤：实测 SQL 里已内置
      `oper_erp_acct in (...) OR oper_dept_id_2 in ('16333')`。

    ★频道成交额字段（从 `秒杀占比` 等派生指标的 customSql 反解）：
      `d_amt` 成交金额 / `dms_damt` 大秒杀 / `bybt_damt` 百亿补贴 / `pyby_damt` 便宜包邮 / `live_amt` 直播。
      另有 `全周期/7日/T_2日` 三个时间窗版本 + 同比，以及 `百补是否可提报/已提报`。
    """
    import uuid as _uuid

    meta = list_fields(dataset_id)

    def _pick_local(name, pool):
        for x in pool:
            if str(x.get("name")) == str(name) or str(x.get("code")) == str(name):
                return x
        raise BlacklightError("数据集 %s 里没有 %r" % (dataset_id, name))

    dim_objs = [_as_dim(_pick_local(d, meta["dims"])) for d in (dims or [])]
    met_objs = []
    for mname in (metrics or []):
        raw = _pick_local(mname, meta["metrics"])
        o = _as_metric(raw, dataset_id)
        if not raw.get("customSql"):          # 基础指标才需要补聚合；派生的有自己的 SQL
            o["aggType"] = agg
        met_objs.append(o)
    if not met_objs:
        raise BlacklightError("至少要一个指标")

    children = []
    for spec in (filters or []):
        fld, op, vals = spec
        children.append(_as_filter(_pick_local(fld, meta["dims"] + meta["metrics"]), op, vals))

    body = {"requestId": str(_uuid.uuid4()),
            "dataSet": {"id": dataset_id, "type": "individual",
                        "option": {"resAppKey": dataset_info(dataset_id).get("resAppKey")
                                                or "analysis_%d" % dataset_id,
                                   "authVersion": "v3", "measureType": "offline",
                                   "interval": "day"}},
            "queryParam": {"groupList": dim_objs, "sortList": [],
                           "filter": {"op": "AND", "children": children},
                           "pageInfo": {"pageSize": page_size, "pageNum": page_num,
                                        "isPage": True},
                           "calculation": {}},
            "resultParam": {"dimensionList": dim_objs, "measureList": met_objs},
            "queryType": "NORMAL", "refresh": False, "cacheConfig": {}}
    b = _envelope(auth.request("POST", PATH_QUERY, json_body=body).json(), "query_sql")
    cols = {}
    for it in (b.get("metadata") or []):
        for code, name in (it or {}).items():
            cols[code] = name
    rows = [{cols.get(k, k): v for k, v in (row or {}).items()} for row in (b.get("data") or [])]
    # ★★截断是**静默少算**，服务端照常 200。高基数维度（sku/spu）一分组就是几十万行：
    #   实测 `dt × item_sku_id` 三天 total=386,299，page_size=20000 只取回 2 万，
    #   求和得到 4,747 而真值 103,699（**只有 4.6%**），看着像"数据大跌"。
    #   按 `dt × 三级类目` 分组 total=632、取回 632，合计与仅按 dt 分组**完全相等** ⇒ 数据自洽，是我截断了。
    total = b.get("total")
    # 2026-08-24：判据同上，实现收回 core/paging.check_total（本文件是它 docstring 里的当事模块之一）
    check_total(len(rows), total, what="数据集查询",
                hint="换低基数维度（如三级类目/spu）先定位再下钻；或加过滤缩小范围；或分页取后自行合并。")
    return {"total": total, "columns": cols, "rows": rows}


def dataset_type(dataset_id: int = DATASET_JX_PL) -> str:
    """`dataSet.type` —— query 里另一个会导致 3003 的坑。

    实测：指标集（`businessScene=metric_dataset`，如 1096116/1025136）发 `metric_dataset`；
    黄金眼那批（`custom_standard`）必须发 **`standard`**，发 `metric_dataset` 一律 3003。
    个人 ClickHouse 集（`individual`）发 `individual` 能命中引擎，但聚合表达式是另一套
    （报 "未知函数"），本模块暂不支持。
    """
    scene = dataset_info(dataset_id).get("businessScene") or ""
    if scene == "metric_dataset":
        return "metric_dataset"
    if scene == "individual":
        return "individual"
    return "standard"


# --------------------------------------------------------------------------- #
# 字段目录
# --------------------------------------------------------------------------- #
def _cache_file(dataset_id: int) -> str:
    import os as _os
    from blacklight.core import paths as _p
    d = _os.path.join(_p.home(), "easybi_meta")
    _os.makedirs(d, exist_ok=True)
    return _os.path.join(d, "fields_%d.json" % dataset_id)


def list_fields(dataset_id: int = DATASET_JX_PL, refresh: bool = False,
                cache: bool = True) -> dict:
    """字段目录 → {"dims": [...], "metrics": [...]}。进程内 + 落盘双层缓存。

    ⚠️大数据集这一步很重（1025136 有 **612 个字段**），所以默认落盘到
    `runtime/easybi_meta/fields_<id>.json`。字段会被人改名/增删 ⇒ **怀疑口径不对时先
    `refresh=True`**，或跑 doctor 看字段数量有没有漂。
    """
    if not refresh and dataset_id in _META_CACHE:
        return _META_CACHE[dataset_id]
    if not refresh and cache:
        import json as _j, os as _os
        fp = _cache_file(dataset_id)
        if _os.path.isfile(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    out = _j.load(f)
                if out.get("dims") or out.get("metrics"):
                    _META_CACHE[dataset_id] = out
                    return out
            except Exception:
                pass
    r = auth.request("GET", PATH_META, params={"datasetId": dataset_id})
    rows = (_envelope(r.json(), "allMetaDataList").get("data")) or []
    if not rows:
        raise BlacklightError("字段目录为空——datasetId 不对或没权限")
    out = {"datasetId": dataset_id,
           "dims": [x for x in rows if x.get("sort") == "dim"],
           "metrics": [x for x in rows if x.get("sort") != "dim"]}
    _META_CACHE[dataset_id] = out
    if cache:
        try:
            import json as _j
            with open(_cache_file(dataset_id), "w", encoding="utf-8") as f:
                _j.dump(out, f, ensure_ascii=False)
        except Exception:
            pass
    return out


DATASET_JX_GENERAL = 1025136     # 京喜_通用数据集：**173 维 × 439 指标 = 612 字段**
#   覆盖面比损益集宽得多：流量/搜索漏斗、评价好评率、履约考核(及时出库/揽收)、售后品退率、
#   商品内容质量标识(缺白底图/缺短标/缺主图视频…)、店铺招商与注册地、预估履约毛利。
#   ⚠️体量带来的三个实际问题（2026-08-07 实测）：
#     1) 278 个自定义指标、**26 组重名** → `_pick` 按名字取会歧义报错，必须用 id 或分组定位；
#     2) 194 个指标没有 groupKey（分组只覆盖一半）；
#     3) 每次 allMetaDataList 拉 612 个字段很重 → 用 `list_fields(cache=True)` 落盘。
#   ★与损益集的权限差异：**通用集不带部门过滤也能查**（损益集不带就 3003）；
#     但带上错误部门号仍会 3003 —— 说明行级权限一直在，只是通用集不强制显式声明。
#     实测「无过滤」与「dept=16333」返回完全相同，因为账号本来就被锁在该部门。


def field_groups(dataset_id: int = DATASET_JX_PL) -> dict:
    """字段分组概览（`groupKey`）——**大数据集先看这个再找字段**。

    实测 1025136 的指标分组：广告44/成交36/财务36/毛利31/服务体验26/履约15/商品13/
    商家12/搜索8/用户6/价格6/流量5/出库4，另有 **194 个没有分组**。
    """
    import collections
    m = list_fields(dataset_id)
    out = {}
    for kind in ("dims", "metrics"):
        c = collections.Counter((x.get("groupKey") or "(未分组)") for x in m[kind])
        out[kind] = dict(c.most_common())
    return out


def browse_fields(dataset_id: int = DATASET_JX_PL, group: str = None, sort: str = None,
                  keyword: str = "", limit: int = 200) -> list:
    """按分组/关键词浏览字段，返回精简行（大数据集用这个，别 dump 全量对象）。

    每行带 `id`——**重名时用 id 定位是唯一可靠的办法**（`_pick` 支持直接传字段对象）。
    另带 `modifierType`（L1D=昨日 / L30D=近30天 / MTD=月至今），选指标时别忽略它。
    """
    m = list_fields(dataset_id)
    pool = (m["dims"] if sort == "dim" else m["metrics"] if sort in ("metric", "measure")
            else m["dims"] + m["metrics"])
    k = (keyword or "").lower()
    rows = []
    for x in pool:
        if group is not None and (x.get("groupKey") or "(未分组)") != group:
            continue
        if k and k not in str(x.get("name") or "").lower() and k not in str(x.get("code") or "").lower():
            continue
        rows.append({"id": x.get("id"), "name": x.get("name"), "code": x.get("code"),
                     "sort": x.get("sort"), "type": x.get("type"),
                     "group": x.get("groupKey") or "(未分组)",
                     "modifier": x.get("modifierType") or "",
                     "desc": (x.get("desc") or "")[:50]})
    return rows[:limit]


def field_by_id(field_id: int, dataset_id: int = DATASET_JX_PL) -> dict:
    """按 id 精确取字段对象——**重名指标唯一可靠的定位方式**。"""
    m = list_fields(dataset_id)
    for x in m["dims"] + m["metrics"]:
        if x.get("id") == field_id:
            return x
    raise BlacklightError("dataset %s 里没有 id=%s 的字段" % (dataset_id, field_id))


def find_fields(keyword: str, dataset_id: int = DATASET_JX_PL, sort: str = None) -> list:
    """按中文名/code 模糊找字段。**返回多条时必须人工确认**（自定义指标常重名）。"""
    m = list_fields(dataset_id)
    pool = (m["dims"] if sort == "dim" else m["metrics"] if sort in ("metric", "measure")
            else m["dims"] + m["metrics"])
    k = keyword.lower()
    return [x for x in pool
            if k in str(x.get("name") or "").lower() or k in str(x.get("code") or "").lower()]


def _pick(field_or_code, dataset_id: int, sort: str = None) -> dict:
    """把「字段对象 / code / 中文名」统一成字段对象。歧义时报错，不猜。"""
    if isinstance(field_or_code, dict):
        return field_or_code
    m = list_fields(dataset_id)
    pool = (m["dims"] if sort == "dim" else m["metrics"] if sort in ("metric", "measure")
            else m["dims"] + m["metrics"])
    exact = [x for x in pool if x.get("code") == field_or_code]
    if len(exact) == 1:
        return exact[0]
    named = [x for x in pool if x.get("name") == field_or_code]
    if len(named) == 1:
        return named[0]
    cands = exact or named
    if not cands:
        raise BlacklightError("找不到字段 %r（用 find_fields 看看有哪些）" % field_or_code)
    nl = chr(10)
    raise BlacklightError(
        "字段 %r 命中 %d 条，歧义 —— 自定义指标常重名（1025136 有 26 组）。%s"
        "  用 `field_by_id(id)` 精确取，或直接传字段对象。候选：%s%s"
        % (field_or_code, len(cands), nl, nl,
           nl.join("    id=%-8s group=%-8s modifier=%-5s %s"
                   % (c.get("id"), c.get("groupKey") or "-",
                        c.get("modifierType") or "-", (c.get("desc") or "")[:40])
                     for c in cands[:8])))


def _as_dim(f: dict) -> dict:
    base = f.get("baseCode") or f.get("code")
    return {**f, "key": f.get("code"), "field": base, "aggConf": f.get("aggConf") or {}}


def _as_metric(f: dict, dataset_id: int, agg: str = "AUTO") -> dict:
    base = f.get("baseCode") or f.get("code")
    return {**f, "key": f.get("code"), "field": base,
            "aggType": agg, "dataSetId": dataset_id}


def _sort_field(f: dict) -> str:
    """排序/TopN 里的 `field`：自定义指标用它的 customSql，标准指标用 baseCode。"""
    return f.get("customSql") or f.get("baseCode") or f.get("code")


def _build_topn(spec: dict, dataset_id: int) -> dict:
    """TopN（2026-08-07 从「分析 → TopN」抓的真身）。

    形状比 sortList **简单**：不带 calculation / manualSort* / depend*。
        {"type": "by_result"|"by_dim", "limit": N,
         "sortList": [{**字段对象, "sortType": "asc"|"desc", "key": code, "field": customSql或baseCode}]}
    ★平台原文提示：**TopN 优先级高于排序**，排序在 TopN 的结果内生效；
      而**合计/参考线基于结果数据计算、不跟随 TopN 变化**（即合计仍是全量的合计）。
    """
    f = _pick(spec["by"], dataset_id, "metric")
    return {"type": spec.get("type") or "by_result", "limit": int(spec.get("n") or 50),
            "sortList": [{**f, "sortType": "desc" if spec.get("desc", True) else "asc",
                          "key": f.get("code"), "field": _sort_field(f)}]}


def _build_total(metrics: list, name: str = "合计") -> dict:
    """合计行（`calculation.commonTableCombined`）。method=AUTO 让平台按指标类型自选聚合。"""
    return {"commonTableCombined": {
        "sumName": name,
        "applyMethodsList": [{"fieldKey": m.get("key") or m.get("code"), "method": "AUTO"}
                             for m in metrics]}}


# 同环比：compareType 取值（2026-08-07 抓到 YEAR_TB；其余按平台命名推断，用前先验证）
COMPARE_YEAR = "YEAR_TB"          # 年同比（已实证）


def _build_compare(base: dict, date_dim: dict, start: str, end: str,
                   compare_start: str, compare_end: str,
                   compare_type: str = COMPARE_YEAR, value_type: str = "RATE",
                   period_unit: str = "YEAR", period_num: int = -1) -> dict:
    """同环比列：在 `measureList` 里**额外加一项**，靠 `dependKey` 指回基准指标。

    2026-08-07 实证（NETGMV 上年同比）的差异字段只有这些：
        compareColumnId   = 日期维度的 id（不是 code）
        compareColumnType = "original"
        compareType       = "YEAR_TB"
        compareValueType  = "RATE"（比率）
        dependKey         = 基准指标的 key
        compareStartDate / compareEndDate = 对比期区间
        comparePeriod     = {"periodUnit": "YEAR", "periodNum": -1}
    其余字段与基准指标完全相同（含 code/key 要换成新的实例码）。
    """
    return {**base,
            "code": (base.get("code") or "") + "_cmp",
            "key": (base.get("key") or base.get("code") or "") + "_cmp",
            "name": "%s_%s" % (base.get("name"), "上年同比(比率)" if value_type == "RATE" else "同比"),
            "compareColumnId": date_dim.get("id"), "compareColumnType": "original",
            "compareType": compare_type, "compareValueType": value_type,
            "dependKey": base.get("key") or base.get("code"), "originAlias": "",
            "compareStartDate": compare_start, "compareEndDate": compare_end,
            "comparePeriod": {"periodUnit": period_unit, "periodNum": period_num}}


def _as_filter(f: dict, op: str, values: list) -> dict:
    base = f.get("baseCode") or f.get("code")
    return {**f, "op": op, "key": base, "field": base,
            "values": [str(v) for v in values], "feature": "common"}


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def query(dims: list, metrics: list, filters: list = None,
          dataset_id: int = DATASET_JX_PL, page_size: int = 200, page_num: int = 1,
          interval: str = "day", dim_group: str = None, order_by=None, desc: bool = True,
          dept: str = None, raw: bool = False,
          top_n: dict = None, with_total: bool = False, compare: dict = None) -> dict:
    """按维度×指标取数。

    dims/metrics: 字段对象 或 code 或中文名（歧义会报错，不猜）。
    filters:      [(字段, op, [值...])]，op 如 'in' / '>=' / '<=' / '='。
                  ★日期用 `dt` 字段，两条 `>=` / `<=` 组成区间（前端就是这么发的）。
    dept:         二级部门（行级权限，**必须带**，见 DEPT_SHOUNA 注释）；
                  传 None 表示自己已在 filters 里给了部门条件。
    order_by:     字段对象/code，**服务端排序**（第 1 页即全局 Top N，不用翻页）。
    top_n:        {"by": 指标, "n": 50, "desc": False, "type": "by_result"|"by_dim"}
                  ★TopN 优先级**高于** order_by；排序在 TopN 结果内生效。
    with_total:   True 时加「合计」行（`calculation.commonTableCombined`）。
                  ⚠️合计基于**结果数据**算，**不跟随 TopN**（即仍是全量合计，平台原文）。
    compare:      同环比，{"start","end","compare_start","compare_end",
                          "type":"YEAR_TB","value_type":"RATE"}；
                  会为每个指标额外生成一列，靠 dependKey 指回基准指标。
    返回 {total, columns, rows}（raw=True 时返回原始 body）。
    """
    dim_objs = [_as_dim(_pick(d, dataset_id, "dim")) for d in (dims or [])]
    met_objs = [_as_metric(_pick(m, dataset_id, "metric"), dataset_id) for m in (metrics or [])]
    if not met_objs:
        raise BlacklightError("至少要一个指标")

    children = []
    dept = DEPT if dept is None else dept
    if dept:
        children.append(_as_filter(_pick("saler_dept_id_2", dataset_id, "dim"), "in", [dept]))
    for spec in (filters or []):
        fld, op, vals = spec
        children.append(_as_filter(_pick(fld, dataset_id), op, vals))

    # ★服务端排序（2026-08-07 从「分析 → 排序」面板抓到真身；此前按猜的形状填过一次
    #   → `code=9999 服务异常` 且报错文案完全不指向排序，很难查。别再自己编）。
    #   关键差异：排序键是 **`sortType`**（"desc"/"asc"）不是 `order`；
    #   `aggType` 要 **"NONE"** 不是 AUTO；且必须带 calculation/manualSort*/depend* 这几个键。
    #   ⚠️自定义指标（type=custom）的 `field` **不是 code，是它的 customSql**
    #   （如 投后履约毛利额 → "sum([贡献利润_边际_权责])+sum([费用_固定_市场_广告_京准通])"）。
    sort_list = []
    if order_by is not None:
        ob = _pick(order_by, dataset_id, "metric")
        fld = ob.get("customSql") or ob.get("baseCode") or ob.get("code")
        sort_list = [{**ob, "key": ob.get("code"), "field": fld,
                      "dataSetId": dataset_id, "aggType": "NONE",
                      "calculation": {"type": "NONE"},
                      "sortType": "desc" if desc else "asc",
                      "manualSortType": "list", "manualSortList": [],
                      "dependKey": "", "dependSortType": "desc" if desc else "asc"}]

    if compare:
        dd = _pick(D_DT_CODE, dataset_id, "dim")
        met_objs = met_objs + [
            _build_compare(m, dd, compare.get("start"), compare.get("end"),
                           compare.get("compare_start"), compare.get("compare_end"),
                           compare.get("type") or COMPARE_YEAR,
                           compare.get("value_type") or "RATE",
                           compare.get("period_unit") or "YEAR",
                           compare.get("period_num", -1))
            for m in list(met_objs)]

    body = {
        "requestId": str(uuid.uuid4()),
        "dataSet": {"id": dataset_id, "type": dataset_type(dataset_id),
                    "option": {"dimGroupCode": dim_group or dim_group_code(dataset_id),
                               "resAppKey": dataset_info(dataset_id).get("resAppKey")
                                            or "analysis_%d" % dataset_id,
                               "authVersion": "v3", "measureType": "offline",
                               "interval": interval}},
        "queryParam": {"groupList": dim_objs, "sortList": sort_list,
                       "filter": {"op": "AND", "children": children},
                       "pageInfo": {"pageSize": page_size, "pageNum": page_num,
                                    "isPage": True},
                       "calculation": (_build_total(met_objs) if with_total else {}),
                       **({"topN": _build_topn(top_n, dataset_id)} if top_n else {})},
        "resultParam": {"dimensionList": dim_objs, "measureList": met_objs},
        "queryType": "NORMAL", "refresh": False, "cacheConfig": {},
    }
    r = auth.request("POST", PATH_QUERY, json_body=body)
    b = _envelope(r.json(), "query")
    if raw:
        return b
    # metadata 是 [{字段码: 中文名}, ...]
    # ⚠️同环比列与基准列**中文名完全相同**（都叫 "NETGMV_商品销售"），直接建 dict 会互相覆盖
    #   ——2026-08-07 踩过：同比值明明在返回里（0.0977），但被基准值盖掉、看着像"没取到"。
    #   所以重名的按 code 后缀区分。
    cols, seen = {}, {}
    for it in (b.get("metadata") or []):
        for code, name in (it or {}).items():
            nm = name
            if name in seen:
                nm = "%s_同比" % name if str(code).endswith("_cmp") else "%s#%d" % (name, seen[name] + 1)
            seen[name] = seen.get(name, 0) + 1
            cols[code] = nm

    total_row = {}
    rows = []
    for row in (b.get("data") or []):
        if not isinstance(row, dict):
            rows.append(row)
            continue
        out = {}
        for k, v in row.items():
            # ★合计不是单独一行：平台把它以 `合计_<指标code>` 为键挂在**每一行**上
            if str(k).startswith("合计_"):
                total_row[cols.get(k[3:], k[3:])] = v
                continue
            out[cols.get(k, k)] = v
        rows.append(out)

    return {"total": b.get("total"), "columns": cols, "rows": rows,
            **({"合计": total_row} if total_row else {}),
            "_sort": ("服务端排序 by %s %s ⇒ 第 1 页就是全局 Top N（不用翻页）"
                      % (_pick(order_by, dataset_id, "metric").get("name"),
                         "降序" if desc else "升序")) if order_by is not None else None}
