"""
easybi-mcp MCP server（stdio）：**京东数据平台 Easy BI** 的取数能力面。

数据集 `京喜_财务_损益`（京算盘-by天损益，看板 113956）：25 维度 × 67 指标，
维度到 **SKU / SPU / 采销 / 四级类目 / 日期到天**，指标含 **投后履约毛利额**。
这是全站营销和 osw 都拿不到的那块——广告投后的**财务口径**盈亏。

鉴权与 osw/yx/jzt **同一份登录态**：`.jd.com` 主票 → 一次自动 OIDC 重定向换到域内会话
（见 `blacklight/easybi/auth.py`），不需要开浏览器、不落盘第二份凭证。

★ 工具分两层，**别混用**：
  · `easybi_sku_pnl` / `easybi_dept_pnl` = **单点问数**，一次请求秒回；
  · `easybi_sku_pnl_bulk` = **批量取数**，按类目切片拉全量（实测 31 片 / 90 秒）。
  用单点接口硬凑全量会报错（而不是静默半截）；用批量做单点查询是浪费。

⚠️ 四个已知硬约束（详见 easybi/dataset.py、easybi/pnl.py 注释）：
  1. 查询必须带**二级部门**过滤，否则 `CODE 3003 无权限`（结构再对也没用）。
     这是账号的数据权限边界、**探测不到**（把部门当维度 group by 同样被拒），
     只能配置：config/easybi.json 的 `dept_id`。
  2. `dimGroupCode` + `dataSet.type` 必须**按数据集选对**，选错一样报 3003：
     指标集(1096116/1025136)发 `metric_dataset`，黄金眼那批(custom_standard)必须发
     `standard`；dimGroupCode 认 code `7a43b98a…`（采销岗权限域）**不认下标**
     ——黄金眼那批它排第 3。2026-08-10 就是取了 `[0]` + 错 type，把
     1043053/1045254 误判成"账号无权限"。自动解析已修（`dataset_type` / `DIM_GROUP_SALER`）。
  3. 67 个指标里 35 个是**看板自定义字段**、code 就是中文名、可被任何人改名——
     数字对不上时先跑 `easybi_doctor` 看字段目录有没有漂。
  4. ★★**服务端分页不可信**：翻页会**既重复又缺失**（实测 58264 行只有 38541 个不同 SKU）。
     一律用切片，别翻页。

注册（与 yx/osw/jzt 同：**user 级 + 绝对 python 路径**）：
  claude mcp add -s user blacklight-easybi -- \
    "C:/Users/wangruihan9/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m blacklight.servers.easybi_server
"""
from __future__ import annotations

import functools

from mcp.server.fastmcp import FastMCP  # noqa: E402

from blacklight.core import BlacklightError  # noqa: E402
from blacklight.easybi import auth as eb_auth  # noqa: E402
from blacklight.easybi import dataset as eb_ds  # noqa: E402
from blacklight.easybi import pnl as eb_pnl  # noqa: E402
from blacklight.easybi import pl as eb_pl  # noqa: E402  (京喜损益领域层)
from blacklight.easybi import coupon as eb_cpn  # noqa: E402  (券促实际成交归因)
from blacklight.easybi.doctor import doctor as eb_doctor  # noqa: E402

mcp = FastMCP("blacklight-easybi")


def safe(fn):
    """只读工具错误包装：捕 BlacklightError → {"error": ...}。"""
    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            return fn(*a, **kw)
        except BlacklightError as e:
            return {"error": str(e)}
    return w


# --------------------------------------------------------------------------- #
# 状态 / 元信息
# --------------------------------------------------------------------------- #
@mcp.tool()
@safe
def easybi_login_status() -> dict:
    """查 easybi 身份（会触发一次 OIDC 握手，握手是一次性的、不是每次调用）。"""
    who = eb_auth.login_info()
    return {"pin": who.get("Pin"), "nick": who.get("Nick"),
            "roles": [r.get("roleIdentity") for r in (who.get("roles") or [])][:12],
            "note": "与 osw/yx/jzt 同一份登录态，掉线重跑 python -m blacklight.core.login"}


@mcp.tool()
@safe
def easybi_doctor(deep: bool = True) -> dict:
    """[无人值守] **契约巡检**：握手/数据集元信息/字段数量/关键字段/端到端真值锚点。

    比别的域更需要——数据集里 35 个自定义指标 code 就是中文名，谁都能改，
    改了不会报错、只会**静默取到别的列**。取数前先跑，`healthy=False` 转人工。
    """
    return eb_doctor(deep=deep)


@mcp.tool()
@safe
def easybi_list_fields(dataset_id: int = eb_ds.DATASET_JX_PL, keyword: str = "",
                       sort: str = "") -> dict:
    """列/搜字段目录。sort: 'dim' 只看维度 / 'metric' 只看指标 / 空=全部。

    ⚠️同名指标常有多个（自定义字段），返回多条时**别按名字取第一个就用**，
    先用 `easybi_verify_metric_aliases` 确认它们是否等价。
    """
    if keyword:
        rows = eb_ds.find_fields(keyword, dataset_id, sort or None)
        return {"keyword": keyword, "命中": len(rows),
                "fields": [{"id": x.get("id"), "code": x.get("code"), "name": x.get("name"),
                            "sort": x.get("sort"), "type": x.get("type"),
                            "desc": (x.get("desc") or "")[:60]} for x in rows],
                "⚠️": "命中多条时先验证是否等价" if len(rows) > 1 else None}
    m = eb_ds.list_fields(dataset_id)
    return {"datasetId": dataset_id, "维度数": len(m["dims"]), "指标数": len(m["metrics"]),
            "dims": [{"code": x["code"], "name": x["name"]} for x in m["dims"]],
            "metrics": [{"code": x["code"], "name": x["name"], "type": x.get("type")}
                        for x in m["metrics"]]}


@mcp.tool()
@safe
def easybi_list_datasets(scene: str = "all", keyword: str = "") -> dict:
    """**枚举我能看到的数据集**（实测 91 个，其中 88 个是别人负责的——**照样能读**）。

    scene: 'all'(全部 91) / 'individual'(个人+提数推送 75)。keyword 可搜名字。
    拿到 datasetId 后，`easybi_list_fields(dataset_id=...)` 就能读它的维度/指标。
    与本行工作相关的几个：1024500 广告分析_SKU粒度、991816 广告分析_部门粒度、
    1064364 优惠券商品明细、1055803 采销版-营销频道(127指标)、1089366 京喜商品基础信息。
    """
    rows = eb_ds.list_datasets(scene=scene, keyword=keyword)
    return {"scene": scene, "数量": len(rows),
            "datasets": [{"datasetId": x.get("datasetId"), "名称": x.get("datasetCnName"),
                          "连接": x.get("connectTypeDesc"),
                          "负责人": (x.get("managerUserIdList") or [None])[0],
                          "businessScene": x.get("businessScene")} for x in rows]}


@mcp.tool()
@safe
def easybi_top_losers(start: str, end: str, top: int = 30, by: str = "sku_id",
                      mode: str = "new_jdly") -> dict:
    """**按投后履约毛利额排序取最亏的 N 个**（服务端排序，一次请求，不翻页）。

    by: 'sku_id'(默认) / 'spu_id' / 'cate_2' / 'cate_op_erp' …
    ★服务端排序 2026-08-07 打通后，第 1 页就是全局 Top N —— 58264 行不用翻。
    """
    from blacklight.easybi import dataset as _ds
    d = _ds._pick(by, _ds.DATASET_JX_PL, "dim")
    dt = _ds._pick(eb_pnl.D_DT, _ds.DATASET_JX_PL, "dim")
    cus = eb_pnl._post_metric()
    flt = [(dt, ">=", [start]), (dt, "<=", [end])]
    if mode:
        flt.insert(0, (_ds._pick(eb_pnl.D_MODE, _ds.DATASET_JX_PL, "dim"), "in", [mode]))
    r = _ds.query(dims=[d], metrics=[eb_pnl.M_NETGMV, eb_pnl.M_QTY, eb_pnl.M_GROSS, cus],
                  filters=flt, order_by=cus, desc=False, page_size=top)
    return {"区间": "%s ~ %s" % (start, end), "维度": by, "服务端total": r.get("total"),
            "_sort": r.get("_sort"), "rows": r["rows"]}


@mcp.tool()
@safe
def easybi_analyze(dims: list, metrics: list, start: str, end: str,
                   top_n: int = 0, order_by: str = "", asc: bool = False,
                   with_total: bool = False, compare_start: str = "", compare_end: str = "",
                   mode: str = "new_jdly", page_size: int = 100,
                   dataset_id: int = eb_ds.DATASET_JX_PL) -> dict:
    """**通用分析**：把 EasyBI「分析」tab 的四个能力全部工具化（2026-08-07 逐个抓包实现）。

    · `order_by` **服务端排序** —— 第 1 页就是全局 Top N，不用翻页。
    · `top_n`    **TopN**（服务端截断）。★优先级**高于** order_by；排序在 TopN 结果内生效。
    · `with_total` **合计** —— 平台把它以 `合计_<code>` 挂在每行上，本工具汇总到返回的 `合计` 键。
      ⚠️合计基于**结果数据**算、**不跟随 TopN**（仍是全量合计，平台原文）。
    · `compare_start/compare_end` **同环比** —— 为每个指标多出一列 `<名称>_同比`，值是**比率**
      （如 0.0977 = +9.77%）。⚠️同比列与基准列中文名相同，本工具已按 code 后缀区分，
      否则会互相覆盖、看着像"没取到"。

    dims/metrics 传 code 或中文名（歧义会报错不猜）。日期 'YYYY-MM-DD'。
    """
    from blacklight.easybi import dataset as _ds
    dt = _ds._pick(eb_pnl.D_DT, dataset_id, "dim")
    flt = [(dt, ">=", [start]), (dt, "<=", [end])]
    if mode:
        flt.insert(0, (_ds._pick(eb_pnl.D_MODE, dataset_id, "dim"), "in", [mode]))
    kw = {}
    if top_n:
        kw["top_n"] = {"by": order_by or metrics[0], "n": top_n, "desc": not asc}
    if order_by:
        kw["order_by"] = order_by
        kw["desc"] = not asc
    if compare_start and compare_end:
        kw["compare"] = {"start": start, "end": end,
                         "compare_start": compare_start, "compare_end": compare_end}
    r = _ds.query(dims=dims, metrics=metrics, filters=flt, dataset_id=dataset_id,
                  page_size=page_size, with_total=with_total, **kw)
    return {"区间": "%s ~ %s" % (start, end), **r,
            "_提示": "行数多时别全塞上下文；>50 行请走 Python 层 easybi.pnl 写 JSON 再聚合。"}


@mcp.tool()
@safe
def easybi_pl_chain(start: str, end: str, by: str = "", top: int = 30,
                    mode: str = "new_jdly") -> dict:
    """[京喜损益] **链路拆解**：Net GMV → 还原 → 可控综合毛利 → 履约毛利 → 广告后履约毛利。

    ★率值分母**自动用 Net GMV还原**（自营）——用 Net GMV 实测高估 **1.74pp**，别自己再算。
    ★「投后履约毛利」=「广告后履约毛利」（26年1月改名，公式相同）。
    by 留空=总计；常用下钻 'cate_2' / 'cate_op_erp' / 'dt' / 'sku_id'。结果按广告后毛利升序。
    ⚠️看 by天 前先读 `easybi_pl_caveats(by_day=True)`：错期会让「某天转负」变成假拐点。
    """
    return eb_pl.pl_chain(start, end, by=by or None, top=top, mode=mode or None)


@mcp.tool()
@safe
def easybi_pl_overall_rate(self_after: float, self_restored: float,
                           c_ly: float, c_netgmv: float) -> dict:
    """[京喜损益] **整体广告后履约毛利率考核口径**（两侧不对称，别统一）。

        = (自营广告后履约毛利 + C店履约毛利) / (自营NetGMV还原 + C店NetGMV)
    分子自营用「广告后」、C店用「履约」（C店无采销京准通）；分母自营用「还原」、C店用 NetGMV。
    """
    return eb_pl.overall_rate(self_after, self_restored, c_ly, c_netgmv)


@mcp.tool()
@safe
def easybi_pick_source(question: str = "") -> dict:
    """[京喜损益] **该用哪个数据源**——按问题类型判定，别按手边有什么数据凑。

    ★★铁律：**实时毛利监控 / 当天要动手止亏 → 只能用 osw 毛利监控**。
      2026-08-10 实测三档时效：osw **实时** ｜ easybi 通用集 **T-1** ｜ 京算盘损益 **T-2~T-3**。
      拿京算盘做当天的止亏判断必然滞后 2~3 天。
    ★★铁律：成交口径（osw/京准通/通用集）与出库计费口径（京算盘损益）**不可相加减互校**
      —— 实测同 SKU 同月金额差 6.8% 但毛利一正一负。
    """
    return eb_pl.pick_source(question)


@mcp.tool()
@safe
def easybi_pl_caveats(by_day: bool = False) -> dict:
    """[京喜损益] 数据坑清单：错期/税后1.06/时效/物流9折取消/口径不可比/广告折前折后。

    **做 by天或单日结论前必读**——配送费记妥投日、京准通均摊记当月1号，
    「某天开始转负」很可能是**错期**不是经营拐点。
    """
    return eb_pl.caveats(by_day=by_day)


@mcp.tool()
@safe
def easybi_verify_metric_aliases(sku: str, start: str, end: str) -> dict:
    """复验重名的「投后履约毛利额」是否等价（2026-08-07 实测 6 个全等，但别默认永远成立）。"""
    return eb_pnl.verify_metric_aliases(sku, start, end)


# --------------------------------------------------------------------------- #
# 单点问数
# --------------------------------------------------------------------------- #
@mcp.tool()
@safe
def easybi_sku_pnl(skus: list, start: str, end: str, by_day: bool = False,
                   mode: str = "new_jdly") -> dict:
    """**单点问数**：1~N 个 SKU 在 [start,end] 的损益（NETGMV/数量/综合毛利/**投后履约毛利额**）。

    日期格式 'YYYY-MM-DD'。`by_day=True` 按天展开——**看拐点用这个**
    （实测某 SKU 7/26 还是 +37，7/27 起转负，量越大亏越多）。
    ★`综合毛利` 是广告前、`投后履约毛利额` 是广告后，差额=广告+履约。ROI 高≠不亏。
    行数超单页会**报错而不是静默截断**；真要全量用 `easybi_fetch_all`。
    """
    return eb_pnl.sku_pnl(skus, start, end, by_day=by_day, mode=mode or None)


@mcp.tool()
@safe
def easybi_dept_pnl(start: str, end: str, by: str = "", top: int = 30,
                    mode: str = "new_jdly") -> dict:
    """**单点问数**：部门整体损益，可下钻。

    by 留空=只出总计（最快，用来对看板）；
    常用下钻：'cate_2'(二级类目) / 'cate_op_erp'(采销) / 'dt'(按天) / 'brand' / 'shop'。
    结果按投后履约毛利**升序**（最亏的在前）。
    """
    return eb_pnl.dept_pnl(start, end, by=by or None, top=top, mode=mode or None)


# --------------------------------------------------------------------------- #
# 批量取数
# --------------------------------------------------------------------------- #
@mcp.tool()
@safe
def easybi_sku_pnl_bulk(start: str, end: str, mode: str = "new_jdly") -> dict:
    """**批量取数**：全部 SKU 的损益，**按类目切片**（不翻页）。

    ⚠️★**服务端分页不可信**：翻页实测 58264 行里只有 38541 个不同 SKU
      （15329 个重复、规律出现在第 2/4/6… 页），同时另一批行一次都没吐出来。
      所以本工具改用切片：按一级类目切，超单页(30000)自动下钻二级类目，
      **每片校验 len(rows)==total**，装不下直接报错而不是返回半截。
      实测 2026-07 自营：31 片 / 58264 行 / **重复 0** / 90 秒。
    行数很多，本工具只回样例与汇总；要全量明细请走 Python 层
    `easybi.pnl.sku_pnl_bulk` 写 JSON 再聚合（别把几万行塞进上下文）。
    """
    r = eb_pnl.sku_pnl_bulk(start, end, mode=mode or None, verbose=False)
    rows = r.pop("rows", [])
    neg = [x for x in rows if (x.get("投后履约毛利额") or 0) < 0]
    agg = {"SKU数": len(rows), "投后为负": len(neg),
           "投后合计": round(sum(x.get("投后履约毛利额") or 0 for x in rows), 2),
           "负投后合计": round(sum(x["投后履约毛利额"] for x in neg), 2)}
    neg.sort(key=lambda x: x.get("投后履约毛利额") or 0)
    return {**r, "汇总": agg, "亏损Top20": neg[:20],
            "_提示": "全量明细请走 Python 层，勿全塞上下文。"}


# --------------------------------------------------------------------------- #
# 券促实际成交归因（黄金眼，T-1）
# --------------------------------------------------------------------------- #
@mcp.tool()
@safe
def easybi_coupon_attribution(skus: list, start: str, end: str,
                              with_osw: bool = True) -> dict:
    """★**亏损到底出在哪张券**——用实际成交数据，不是 osw 的预估。

    osw 毛利监控只看得见**当前**券促配置；商品后续被圈进新券，预估就失效了。
    实证（2026-08-10，13 款禁令款/15 日）：券我担 14,400 元中 **31% 来自 osw
    当前配置里根本不存在的批次**，所以「预估最低毛利」对不上「实际单均毛利」是必然的。

    返回三块：
      · `skus`   —— 逐 SKU：券我担/平台担/促销让利 + **无券后单均**（>0 ⇒ 券致亏，
                    定价没问题，别去涨价；≤0 ⇒ 结构性，摘券治不了）
      · `by_batch` —— 逐券批次，`kind` 分 `自担`(平台担=0，摘了全额回血) /
                    **`共补`**(平台担>0，摘掉连平台那份一起扔，**永不批量摘**)
      · `by_act`   —— 逐促销活动

    ⚠️时效 **T-1**（比京算盘 T-2~T-3 快，可进日常巡检；仍慢于 osw 实时）。
    ⚠️批次号与 osw `couponId` 是**两套 ID**，不能 join；按「单均我担」金额对。
    ⚠️SKU 别一次给太多（>200 走 Python 层 `easybi.coupon` 写 JSON 再聚合）。
    """
    osw_rows = None
    if with_osw:
        from blacklight.osw import margin as _mt
        low = _mt.list_low_margin(max_profit=0, limit=3000).get("rows") or []
        osw_rows = {r["skuId"]: r for r in low}
    return eb_cpn.attribute_loss([str(s) for s in skus], start, end, osw_rows=osw_rows)


@mcp.tool()
@safe
def easybi_coupon_by_creator(start: str, end: str, owner_erp: str = "",
                             skus: list = None) -> dict:
    """★**这些券是谁开的**——按录券人(`jd_erp`)汇总我担/平台担。

    `owner_erp`（商品的采销 ERP 归属）传了就**不必先备好 SKU 清单**，直接拉全量；
    也可以给 `skus` 只看指定商品。两者至少给一个（都不给会拉全库，直接报错）。

    实证 2026-08-10（归属 wangruihan9 / 15 日 / 我担 16.8 万）：
    `huwenjie50` 35% 全自担、`chenlisha10` 32% 全是共补券（平台另担 1.9 万）。
    **承担方分档和录券人分档基本是同一刀**——找谁谈判看这张表比看批次号有用。

    ⚠️仅券集可用（促销集的录促人字段查询直接 CODE 50001，拿不到）。
    ⚠️返回若报「被分页截断」，缩短日期区间——**截断是静默少算**，宁可报错不给半截。
    """
    return eb_cpn.cost_by_creator(start, end, owner_erp=owner_erp or None,
                                  skus=[str(s) for s in (skus or [])] or None)


@mcp.tool()
@safe
def easybi_coupon_blind_spot(skus: list, start: str, end: str) -> dict:
    """★**osw 预估的盲区率**：实际用掉的券里，多少钱是 osw 当前配置看不见的。

    osw 只看得见**当前**挂在 SKU 上的券。后续圈进来的新券、已下线的旧券，它都不知道
    —— 那部分钱在 osw 侧根本不存在。所以「预估毛利」和「实际单均毛利」对不上是**必然**的。

    实测 2026-08-10（13 款禁令款/15 日）：券我担 14,400 元里 **30.6% 在盲区**，
    全部来自「单均我担 5.00」那 31 个批次，osw 一张都没有。

    用途：① 判断 `yx_stoploss_plan` 的「真凶排行」覆盖了多少；
          ② 盲区率高的 SKU，**osw 预估对它没参考价值**，判亏损必须用实际单均毛利。
    """
    return eb_cpn.blind_spot_rate([str(s) for s in skus], start, end)


@mcp.tool()
@safe
def easybi_coupon_run_rate(skus: list, asof: str, recent_days: int = 3,
                           baseline_days: int = 12) -> dict:
    """★★**还在流血，还是已经停了**——排处置顺序必须先过这个。

    `osw_margin_triage` 的失血是 **15 日滚动、向后看的**：一次已经结束的脉冲会在榜首
    赖上半个月。2026-08-10 实证：按失血排的 D 桶 Top3 全是 7/29–8/03 的一次脉冲
    （8/02 峰值 10,036 元/天），**8/04 起已自行塌掉 91%**，osw 上那张券也早不在商品上，
    摘券无事可做 —— **据那份清单动手会全打空**。

    换流速重排后 Top 完全换人：`10168121856759` 近 3 日日均 369 元（基线 31，+1,076%）、
    `10165875240276` 293 元（基线 2.5）—— 这两款 15 日失血只有 −400/−104，旧排序里进不了前 20。

    状态：**爆发**(≥2× 基线，优先处置) / **持续**(0.5~2×) / **消退**(<0.5×，先别动)。
    `asof` 填 T-1（黄金眼时效）。
    """
    return eb_cpn.run_rate([str(s) for s in skus], asof,
                           recent_days=recent_days, baseline_days=baseline_days)


@mcp.tool()
@safe
def easybi_coupon_trend(skus: list, before_start: str, before_end: str,
                        after_start: str, after_end: str) -> dict:
    """★**止亏动作的回读判据**：比对动作前后两个区间的**实际**券我担成本。

    为什么不能只看「osw 预估是否转正」：
      1. **补位效应**——摘掉一项，别的券/总价促销/国补会顶上（实证 40 款里 12 款偏离线性预测）；
      2. **31% 盲区**——osw 看不见的批次照样在花钱。
    两者叠加会让人误判"止住了"。真正的判据是**实际券我担单均降没降**。

    ⚠️两个区间天数应一致，否则总额不可比（本工具会算单均并提醒）。
    ⚠️只覆盖券；若摘券后促销/国补补位，还要看 `easybi_coupon_attribution` 的促销部分。
    """
    return eb_cpn.cost_trend([str(s) for s in skus],
                             (before_start, before_end), (after_start, after_end))


if __name__ == "__main__":
    mcp.run()


@mcp.tool()
@safe
def easybi_channel_gmv(start: str, end: str, dims: list = None, top: int = 30) -> dict:
    """★[黄金眼商品数据集 1044828] **商品在各频道场的成交数据**——百补/秒杀/便宜包邮/直播。

    `dims` 缺省按天（`dt`）；也可给 `['item_sku_id']` 看逐 SKU、`['dt','item_sku_id']` 看逐日逐 SKU。

    字段：`d_amt` 成交金额 / `dms_damt` 大秒杀 / `bybt_damt` 百亿补贴 /
    `pyby_damt` 便宜包邮 / `live_amt` 直播（引导口径）。
    该集另有 `全周期/7日/T_2日` 三个时间窗版本 + 同比，以及 **`百补是否可提报/已提报`**。

    ⚠️它是 `mode=sql` 的 ClickHouse 集，走的是**第三套查询契约**（见 `query_sql_dataset`）：
      type=individual、不传 dimGroupCode、基础指标必须显式 SUM。
    ⚠️**行级权限数据集自带**，不用传部门。
    """
    cols = ["成交金额", "大秒杀业务成交金额", "百亿补贴成交金额",
            "便宜包邮成交金额", "直播成交金额（引导口径）"]
    r = eb_ds.query_sql_dataset(1044828, dims or ["dt"], cols,
                                filters=[("dt", ">=", [start]), ("dt", "<=", [end])],
                                page_size=max(top, 30))
    rows = r["rows"]
    for x in rows:
        tot = float(x.get("成交金额") or 0)
        ch = sum(float(x.get(k) or 0) for k in cols[1:])
        x["四频道合计"] = round(ch, 2)
        x["四频道占比%"] = round(100 * ch / tot, 2) if tot else None
    return {"rows": rows[:top], "行数": len(rows), "columns": r["columns"],
            "_note": "四频道 = 秒杀+百补+便宜包邮+直播；占比分母是该维度下的总成交金额。"}
