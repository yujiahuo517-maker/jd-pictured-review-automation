"""blacklight-pnl MCP server（stdio）：**毛利监控 & 损益分析的统一入口**。

⚠️本 server **不对应任何网关**——它是编排层，把 ge / osw / easybi / yx 串成一条链路。
（`osw.margin` / `ge.margin` / `easybi.pnl` 是各域自己的模块，别与本包 `blacklight.pnl` 混。）

## 两条时间线，绝不混用
| | 实时 RT (T-0) | 离线 OFF (T-1) |
|---|---|---|
| 源 | `ge.margin` + `osw.scan_portfolio`(前瞻) | `easybi.coupon` + `ge.couponbatch` |
| 用途 | **发现 + 止血** | **归因 + 定责 + 谈判** |
| 实测 | 热启 ~2 秒（前瞻网 15 分钟缓存） | ~5 秒 |

★**实时只用于止血、不用于定责**——当日广告/物流未结算。

## 五条已程序化的纪律（2026-08-11 重构，每条都有当天的实证）
1. **流速优先**：排行按「近 N 日日均 vs 基线」。按累计排会把已塌 95% 的券排第一。
2. **范围对齐**：跨源比对必须传同一份 `sku_ids`。ge 不传就是整个 `cate_op_erp` 范围。
3. **口径标签**：全额 vs 采销承担差 22.3%，混用直接抛。
4. **截断闸**：行数 == 上限一律按被截断处理（已实撞 3 处静默截断）。
5. **静默忽略探针**：用「瞎编字段」做阴性对照——`filterList` 对未知字段静默吞掉，
   "没报错"证明不了筛选生效。

## 判亏口径（用户 2026-08-11 拍板）
- **ge = 判亏权威**（已发生/正在发生），`judge_losing()` 默认**投后**（已扣广告）
  且**要求有成交**。投前 vs 投后差 7 倍；不要求成交会把亏损款数虚增 6 倍。
- **osw = 预测**（配置已亏、可能还没出单）+ 采购价/物流成本底料。
- 两网**取并集不取交集**——它们回答不同问题。

注册：
  claude mcp add -s user blacklight-pnl -- \
    "C:/Users/wangruihan9/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m blacklight.servers.pnl_server
"""
from __future__ import annotations

import functools

from mcp.server.fastmcp import FastMCP  # noqa: E402

from blacklight.core import BlacklightError  # noqa: E402
from blacklight.ge import margin as ge_margin
from blacklight.ge import ad as ge_ad  # noqa: E402
from blacklight.pnl import offline as pnl_offline  # noqa: E402
from blacklight.pnl import runrate as pnl_runrate  # noqa: E402
from blacklight.pnl import scan as pnl_scan  # noqa: E402

mcp = FastMCP("blacklight-pnl")


def safe(fn):
    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            return fn(*a, **kw)
        except BlacklightError as e:
            return {"error": str(e)}
    return w


@mcp.tool()
@safe
def ge_ad_drill(start: str, end: str, degree: str = "sku", erp: str = None,
                sku_ids: list = None) -> dict:
    """[ge] **SKU/SPU 级广告数据**（广告运营看板 menu 34047 · page_type=3）。
    ★jzt 没有 SKU 级消耗 ⇒ 这是唯一来源。分工：**ge 发现 / jzt 执行**，两边靠 SPU 接，
    别拿 SKU 级消耗去对 jzt 的计划级花费（摊不下去，会得出「大半广告费没有操作入口」的假结论）。
    ★★`消耗`=**折后**(实际计费)；`消耗_折前`=折前——**带 `_discount` 的指标名反而是折前**，实测比值 0.7000。
    ★口径与毛利监控**不可比**：page_type 3 vs 2、后缀 @jx_ads_ord1(仅广告归因订单) vs @jx(全量)、
    且默认只筛京喜自营；同一天同一人实测差 2 倍。返回带阴性对照(瞎编指标必须回 0)。"""
    return ge_ad.drill(start, end, degree=degree, erp=erp, sku_ids=sku_ids)


@mcp.tool()
@safe
def ge_ad_waste(days: int = 7, min_cost: float = 10.0, erp: str = None) -> dict:
    """[ge] **广告空耗**：近 N 日零成交仍在花钱（折后口径）。
    ⚠️别用今日口径——今日零成交可能只是还没出单，拿它拉黑会误杀。
    ⚠️极度长尾：实测七日 2,220 款/8,410 元，**8% 的款占一半金额** ⇒ 按金额切不按款数。
    处置走 jzt（计划/单元级）：个别 SKU 空耗→拉黑；整计划都空耗→停投。"""
    return ge_ad.waste(days=days, min_cost=min_cost, erp=erp)


@mcp.tool()
@safe
def pnl_margin_scan(mode: str = "rt", top: int = 20, skus: list = None,
                    erp: str = "", strict: bool = True,
                    forecast: str = "auto") -> dict:
    """★★**毛利巡检统一入口**（替代旧的 `osw_margin_triage`）。

    mode：
      · `rt`      实时（T-0）：谁现在在亏 / 亏在哪一项 / 挂哪张券或促销 / 前瞻拦截。热启 ~2 秒
      · `offline` 离线（T-1）：流速 → 逐券承担 → 券名发券人 → 可不可摘。**必须给 skus**
      · `both`    先 rt 发现，自动拿 rt 的**全部**有成交亏损款去 offline 归因

    输出分三块，**别混**：
      · `网A_已发生`「有成交亏损」= 止亏主线
      · `广告空耗` = 零成交但有广告消耗，亏损**100% 是广告费** ⇒ 走广告线(jzt)
      · `网B_预测`「今日还没出单」= 前瞻拦截，改价/摘券成本最低

    ⚠️`forecast`：前瞻网（osw 全量扫）22 秒且只随配置变化 ⇒ 默认 15 分钟缓存。
      **刚改过价/摘过券请传 `force`**；只想看已发生传 `skip`。
    ⚠️`strict=True` 时恒等式哨兵红灯直接返回 error——**红灯不出结论**。
    """
    return pnl_scan.margin_scan(mode, erp=erp or None, top=top, skus=skus,
                                strict=strict, forecast=forecast)


@mcp.tool()
@safe
def pnl_attribute(skus: list, baseline_start: str, baseline_end: str,
                  recent_start: str = "", recent_end: str = "",
                  degree: str = "coupon", top: int = 20,
                  strict: bool = True, realtime: bool = False) -> dict:
    """归因：**哪张券/哪个促销在吃毛利、谁开的、能不能摘**。

    `realtime=False`（默认，离线 T-1）—— 需要 `recent_start/end`。
    `realtime=True`（**今日实时**）—— 忽略 recent_*，自动取「今日 00:00 至此刻」，
      与 baseline 窗口比。**能力有差，都是真实约束**：

    | | 离线 T-1 | 实时 T-0 |
    |---|---|---|
    | 承担口径 | easybi 采销承担（权威） | ge 三方拆分：全额−平台补贴−事业部承担 |
    | 无券反事实 | ✓ | **✗**（easybi 是 T-1，覆盖不到今天）⇒ 判「券致亏 vs 结构性」必须走离线 |
    | 恒等式互校 | ✓ ge×easybi | **✗** 只有 ge 一个源 |

    ⚠️实时的「折算日速率」= 今日累计 ÷ 已过时长，**假设当日均匀发生**；
      大促/整点场次会破坏该假设。直接拿今日累计和离线日均比是
      「实时≠一天」的陷阱（同日实时 915 款 vs 离线 5,417 款）。

    ★`skus` 必填且 ≤500——不给的话 ge 查整个 `cate_op_erp` 范围，与 easybi 不可比。
    ★离线的恒等式**只对固定面额券成立**：满减/折扣券随篮子拆分，
      单独列在 `哨兵.篮子相关券` 里，不纳入校验也别逐条比。
    """
    if realtime:
        return pnl_offline.attribute_rt(skus, (baseline_start, baseline_end),
                                        degree=degree, top=top)
    if not (recent_start and recent_end):
        return {"error": "离线模式必须给 recent_start/recent_end（实时模式才可省）"}
    return pnl_offline.attribute(skus, (recent_start, recent_end),
                                 (baseline_start, baseline_end),
                                 degree=degree, top=top, strict=strict)


@mcp.tool()
@safe
def pnl_feasibility(sku_ids: list, workers: int = 5,
                    check_live: bool = True, live_days: int = 3) -> dict:
    """摘券可行性：**真实回血**（券档位阶梯） + **禁令**（protected）。

    ★收益是**上限**：同类券互斥、只生效面额最大的一张 ⇒ 摘顶档次档立刻顶上
      （实测 232 张券/9 档的池子，摘 5.00 档真实回血只有 1.00 元/单）。
    ★两个粒度别混：`SKU级禁令`(整款不许动) vs `档位级禁令`(某些券档受保护)，
      `可动手=True` **不代表这款上没有受保护的券档**。
    ★`券总数=0` 按**取数失败**处理，不是「没券可摘」（query_sku 会静默返空）。
    ★★`check_live=True`（默认）会先查**该 SKU 自己的逐日曲线**，把「近日已转正」的
      踢出 `可动手`，单列 `已自愈(近日转正)` / `无法确认`。
      2026-08-12 两次实证：七天累计在亏、**最后一天已转正**（−282.91 ⇒ +30.46），
      照旧清单动手就是对着已停的问题发写操作。判据**以最近一天为准**。
      关掉只在「明知要看历史窗口」时用，日常别关。
    """
    return pnl_offline.feasibility(sku_ids, workers=workers,
                                   check_live=check_live, live_days=live_days)


@mcp.tool()
@safe
def pnl_runrate(degree: str, recent_start: str, recent_end: str,
                baseline_start: str, baseline_end: str,
                skus: list = None, value_key: str = "减免_采销实担",
                top: int = 30) -> dict:
    """★**流速排行**（任何维度处置前都先过这个）。

    `degree`: sku / spu / coupon / promotion。
    状态：爆发 ≥2× / 持续 0.5~2× / 消退 <0.5× / 新增 / 停止。
    ⇒ **处置看 `rows_actionable`**（已剔除 消退/停止）；`rows` 只用于回看。

    2026-08-11 实证：按 15 日累计，某券排第 1；按流速它是**消退 0.06×**，
    而真正在爆发的是另一张（0.93 → 117.43/天，**126×**）。
    """
    rw = (recent_start, recent_end)
    bw = (baseline_start, baseline_end)
    r = ge_margin.drill(rw[0], rw[1], degree=degree, realtime=False, sku_ids=skus)
    b = ge_margin.drill(bw[0], bw[1], degree=degree, realtime=False, sku_ids=skus)
    return pnl_runrate.from_ge_drill(r, b, recent_window=rw, baseline_window=bw,
                                     value_key=value_key, top=top)


@mcp.tool()
@safe
def pnl_bridge(start: str, end: str, realtime: bool = True, erp: str = "") -> dict:
    """毛利桥逐项拆解 —— **亏在哪一项**（商品成本/物流/CPS/券/促销/红包/广告）。

    ★返回 `闭合` 与 `缺失指标`：**不闭合就别当数用**。
      ge 的 `code=2000`「部分指标查询失败」**照样带数据回来**，只是悄悄少几个指标；
      少的若正好是桥里的减项，毛利会被算高。
    实时用 'YYYY-MM-DD HH:MM:SS'（同日），离线用日期。
    """
    return ge_margin.bridge(start, end, erp=erp or None, realtime=realtime)


@mcp.tool()
@safe
def ge_ssm_overview(start: str, end: str, compare_start: str, compare_end: str,
                    dept_2: str = "16333", dept_1: str = "16267") -> dict:
    """[ge] **顺手买（搭售）盘面**：京喜整体 + C2 基准 + 自己 C3 组，一次给齐。

    ★**返回值是区间日均不是累计**，且单日噪声极大（同组 8/19 单日单均损益 +¥0.57、
      8/01-19 口径 −¥0.209，结论会反）⇒ **一律用 ≥7 天口径**。
    ★`per_ord_loss` 名为 loss 实为**损益**，正数=盈利。
    ★数据权限只到自己的 C3 组：C2 只能拿到大数、看不到兄弟组；越权时后台回
      200+`status:-1` 而不是报错（本工具会显式抛错）。
    看渗透 `ssm_pv_rate`、专享价订单占比 `ssm_promt_deal_ord_rate`、
    以及**专享价 vs 非专享价的单均损益方向**——方向和大盘相反就不能照抄大盘打法。"""
    from blacklight.ge import ssm as _s
    A = (start, end, compare_start, compare_end)
    return {"baseline": _s.dept_baseline(*A, dept_1=dept_1, dept_2=dept_2),
            "group": _s.group_summary(*A, dept_1=dept_1, dept_2=dept_2)}


@mcp.tool()
@safe
def ge_ssm_cate3(start: str, end: str, compare_start: str, compare_end: str,
                 dept_2: str = "16333", dept_1: str = "16267") -> list:
    """[ge] 顺手买**三级类目**明细——找机会盘。

    判据：`ssm_expo_cid3_qtty`（大盘曝光）大而 `ssm_pv_rate`（渗透）低。
    ★**零单且大盘曝光大 = 没进场，不是渗透低**（收纳实测 29 个零单类目占 82.5% 大盘曝光）。
    ★排序**别只按曝光**，要叠该类目 `per_ord_loss` 过滤——优先毛利为正的类目，
      否则会把预算投到亏损类目上。也要防类目映射带过来的假机会（京喜曝光=0 且提报=0 的
      巨量类目先线上验证归属真伪）。"""
    from blacklight.ge import ssm as _s
    return _s.by_cate3(start, end, compare_start, compare_end, dept_1=dept_1, dept_2=dept_2)


@mcp.tool()
@safe
def ge_ssm_sku(start: str, end: str, compare_start: str, compare_end: str,
               dept_2: str = "16333", dept_1: str = "16267",
               saler: str = None, channel_compare: bool = True,
               with_rows: bool = False, sample: int = 5) -> dict:
    """[ge] 顺手买**全量 SKU** + ★同 SKU 双通道对比。

    `saler` 按**销售员 ERP**（`saler_erp_acct`）过滤——「我自己的」是这个口径，
    不是采销助理口径（实测同一组里自己名下只占 1.6% 的单量）。

    ★★`channel_compare` 是本工具的核心：专享价是**独立促销、不与券促叠加**，
      所以走专享价成交时我担 100% 的冲单券用不上，没走专享价的反而叠券亏更深。
      同 SKU 内对比（消除选品偏差）实证 **专享价 +¥0.289 vs 非专享价 −¥0.445，
      差 +¥0.734/单、69% 胜率** ⇒ **提专享价是屏蔽自担券的止亏动作，不是让利换量**。
      判一个组该不该铺专享价，先看它的非专享价单均损益：负得越深收益越大。

    ⚠️**别拿 `per_ord_loss` 去倒算定价让利额度**——它是成交后的结果值、已含现有让利，
      倒算等于重复扣减，会把跑量款判成「该涨价」（实测把 21 单/日、现价 ¥2.90 的款
      判成该涨到 ¥4.87）。定价底料用 `osw.margin.query_pricing_batch`，
      成本口径取采购+物流+CPS 并**剔掉 advCost**（24h 快照会炸）。
    ⚠️判生效只认 `fixed_price_deal_ord_num` 起量，提报数上涨不算数。"""
    from blacklight.ge import ssm as _s
    rows = _s.by_sku(start, end, compare_start, compare_end, dept_1=dept_1, dept_2=dept_2)
    if saler:
        rows = [r for r in rows if r.get("saler_erp_acct") == saler]
    out = {"n": len(rows), "rows": rows}
    if channel_compare:
        out["channel_compare"] = _s.channel_compare(rows)   # 汇总先算完，再对 rows 做体积保护
    from blacklight.core.mcpio import cap_rows
    return cap_rows(out, with_rows=with_rows, sample=sample,
                    where="要全量传 with_rows=True；`n` 与 `channel_compare` 是按全量算的，不受影响")


@mcp.tool()
@safe
def ge_doctor() -> dict:
    """[无人值守] ge 契约巡检（6 项）。取数/巡检前先跑，`healthy=False` 转人工。

    ★比别的域多两个**只能靠探活发现**的失效模式：
      · `b-ext-device-info` **过期** → 毛利监控整个不可用（`no auth`）
      · `code=2000` 静默少返指标 → 用**毛利桥闭合**当哨兵
    还含**投前/投后语义哨兵**（`ord_` 必须 > `sku_`，差额=广告）与
    **筛选静默忽略探针**（sku_id 筛选必须真的让行数下降）。
    """
    from blacklight.ge.doctor import doctor as _d
    return _d()


if __name__ == "__main__":
    mcp.run()
