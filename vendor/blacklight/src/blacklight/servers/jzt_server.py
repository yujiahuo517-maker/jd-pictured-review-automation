"""
jzt-mcp MCP server（stdio）：**京准通（cxjzt.jd.com）广告投放**的能力面。

当前覆盖 **全站营销（swa，campaignType=101）**——该账号唯一在花钱的渠道。
鉴权与 yx/osw **同一份登录态**：拿 yx cookie POST 一次免密登录接口即换到广告账号身份
（见 `blacklight/jzt/auth.py`），不需要开浏览器、不落盘第二份凭证。

命名：工具带 `jzt_` 前缀。只读工具错误经 `@safe` 转 {"error":...}；
写工具（改预算/改出价/启停删）走 dry-run → confirm_token → 真执行，并全部落审计。

★ 判盈亏别只看 ROI：京准通只知道广告口径的成交额，不知道成本。要判**投后履约毛利**是否真亏，
  把 ad_list 的 skuIdList 拿去 osw-mcp 的 `osw_margin_list_low` / `osw_pricing_query` 算。

注册（与 yx/osw 同：**user 级 + 绝对 python 路径**，裸 python 会解析到无 mcp/httpx 的那个）：
  claude mcp add -s user blacklight-jzt -- \
    "C:/Users/wangruihan9/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m blacklight.servers.jzt_server
"""
from __future__ import annotations

import functools

from mcp.server.fastmcp import FastMCP  # noqa: E402

from blacklight.core import auth as jd_auth  # noqa: E402
from blacklight.jzt import auth as jzt_auth  # noqa: E402
from blacklight.jzt import swa as jzt_swa  # noqa: E402
from blacklight.jzt import finance as jzt_finance  # noqa: E402  (账户余额/续航测算)
from blacklight.jzt import playbook as jzt_playbook  # noqa: E402  (问题库/3×3决策表/阈值)
from blacklight.jzt import diagnose as jzt_diagnose  # noqa: E402  (账户体检/漏斗归因/3×3定位)
from blacklight.jzt import monitor as jzt_monitor  # noqa: E402  (日报:预算节奏/日环比/断投)
from blacklight.jzt import ledger as jzt_ledger  # noqa: E402  (分析账本:跨run对比可放出)
from blacklight.jzt import batch_template as jzt_batch  # noqa: E402  (批量模板 xlsx)
from blacklight.core import BlacklightError  # noqa: E402

mcp = FastMCP("blacklight-jzt")


def safe(fn):
    """只读工具错误包装：捕 BlacklightError → {"error": ...}（保留签名给 FastMCP 建 schema）。"""
    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            return fn(*a, **kw)
        except BlacklightError as e:
            return {"error": str(e)}
    return w


# ------------------------------- 鉴权 / 账号 ------------------------------- #
@mcp.tool()
def jzt_login_status() -> dict:
    """查看京准通登录态：usable(打真接口探活) + 当前广告账号 + yx 底座是否可用。
    京准通登录**依赖 yx/ERP 登录态**——yx 掉了这里必挂，先 yx_login/osw_login。"""
    return jzt_auth.status()


@mcp.tool()
@safe
def jzt_accounts() -> dict:
    """列出当前 ERP **可免密登录的广告账号**（自投/代投/已驳回都在）。切账号用 jzt_set_account。"""
    return jzt_auth.accounts()


@mcp.tool()
@safe
def jzt_set_account(pin: str) -> dict:
    """切换京准通广告账号（如 `*cx_wangruihan9_自投`）。**只存账号名不存凭证**；之后所有 jzt_* 以该身份取数/操作。"""
    return {"pin": jzt_auth.set_account(pin), "note": "已保存。后续 jzt_* 工具以该广告账号身份执行。"}


@mcp.tool()
@safe
def jzt_login(pin: str = None) -> dict:
    """显式做一次免密登录（幂等）。**平时不用调**——掉登录态时各接口会自动续期重试一次。"""
    return jzt_auth.login(pin)


@mcp.tool()
@safe
def jzt_doctor() -> dict:
    """[无人值守] **契约巡检**：打免密登录/账号枚举/推广列表/合计行/转化周期哨兵，校验契约没漂移。
    返回 {healthy, drift[], checks[]}。京准通接口是反解来的，跑批/写操作前先 doctor，healthy=False 转人工。"""
    from blacklight.jzt import doctor
    return doctor.run()


# --------------------------- 资金：余额与续航 --------------------------- #
@mcp.tool()
@safe
def jzt_balance() -> dict:
    """[资金] **投放账户余额**：现金/红包/佣金 三本账（各带可用/冻结/合计）+ `可投放合计`。
    ★该账号钱全在**红包**里、现金为 0 —— 判续航看红包，别去找现金余额。
    红包的有效期/限定场景本接口不返回，大额依赖时去页面确认。"""
    return jzt_finance.balance()


@mcp.tool()
@safe
def jzt_runway(daily_burn: float, days_needed: int = None) -> dict:
    """[资金] **续航测算**：余额 ÷ 日均消耗 = 还能撑几天；给 days_needed 则判够不够 + 需追加多少。
    ⚠️`daily_burn` 必须取**最近一个完整日**的消耗，不是 summary 区间日均——
    起量前的零消耗日会把均值稀释（实测 7 天均值 2937 vs 真实基准 4208，结论会翻）。今天的数不完整不能用。
    关停过计划的话，用「基准日全账户消耗 − 基准日里仍暂停那批的消耗」。"""
    return jzt_finance.runway(daily_burn, days_needed)


# --------------------------- 全站营销：只读取数 --------------------------- #
@mcp.tool()
@safe
def jzt_swa_summary(start_day: str = None, end_day: str = None, status: int = None,
                    conversion_category: int = 15) -> dict:
    """[全站营销] **账户汇总指标**（页面顶部那排数字）：花费/全站投产比/交易额/订单行/订单成本/展现/点击/180天未购新客。
    日期缺省=近 7 天。status 缺省=全部(含下线)，传 2 只算有效推广。
    ⚠️ 都是**实时累计**值，几分钟就变——跨时点对比要记录拉取时刻。"""
    return jzt_swa.summary(start_day, end_day, status, conversion_category)


@mcp.tool()
@safe
def jzt_swa_ad_list(page: int = 1, page_size: int = 20, start_day: str = None, end_day: str = None,
                    status: int = None, conversion_category: int = 15,
                    order_by: str = "cost|desc", sxu_id: str = None) -> dict:
    """[全站营销] **推广列表（单页）**：每条推广的预算/出价/花费/投产比/订单成本 + 派生的**预算利用率%**与**ROI达成率%**。
    status: 1暂停 2有效 3预算用完 -3审核下线 9下线，缺省=全部。order_by 形如 `cost|desc`。
    改预算用行里的 **campaignId**，改出价用 **groupId**（两者不同，别搞混）。"""
    return jzt_swa.ad_list(page, page_size, start_day, end_day, status,
                           conversion_category, order_by, sxu_id)


@mcp.tool()
@safe
def jzt_swa_ad_all(start_day: str = None, end_day: str = None, status: int = None,
                   conversion_category: int = 15, order_by: str = "cost|desc",
                   page_size: int = 100, max_pages: int = 50,
                   with_rows: bool = False, sample: int = 5) -> dict:
    """[全站营销] **全量拉取推广**（自动翻页 + 空页重试 + 与服务端 total 对账）。
    ⚠️ 返回带 `完整`/`truncated`：**为 False/非空时说明没拉全**，别拿去当全量做删减类决策。
    ★默认只回样例+条数（体积保护，上限 page_size×max_pages=5000 行）；要全量传 with_rows=True。"""
    from blacklight.core.mcpio import cap_rows
    r = jzt_swa.ad_all(start_day, end_day, status, conversion_category,
                       order_by, page_size, max_pages)
    for k in ("rows", "items", "plans", "ads"):
        if isinstance(r, dict) and isinstance(r.get(k), list):
            return cap_rows(r, key=k, with_rows=with_rows, sample=sample,
                            where="要全量传 with_rows=True")
    return r


@mcp.tool()
@safe
def jzt_swa_health(start_day: str = None, end_day: str = None, conversion_category: int = 15,
                   budget_util_low: float = 30.0, roi_gap_low: float = 80.0,
                   min_cost: float = 10.0, gross_margin: float = None) -> dict:
    """[全站营销] **账户体检**：回答「掉量卡在预算还是卡在目标 ROI」。
    算预算利用率分布、实际投产比 vs 目标出价的达成率、订单成本 Top10，并给出方向性结论。
    ★传 `gross_margin`（**到手价口径**毛利率，如 0.16）才会过薄利护栏并给出调价方向——
    不传时只说"判断不了"，因为薄利品下调目标 ROI 是**反向操作**。
    **订单成本高≠亏损**——判真亏要接 osw 毛利。要具体动作用 `jzt_ad_grid`(3×3决策表)。"""
    return jzt_swa.health(start_day, end_day, conversion_category,
                          budget_util_low, roi_gap_low, min_cost, gross_margin)


@mcp.tool()
@safe
def jzt_swa_export_plans(out_path: str = None, start_day: str = None, end_day: str = None,
                         status: int = 2, conversion_category: int = 15) -> dict:
    """[全站营销] **导出计划清单 CSV**，给**外部/人工**用（发给别人、丢 Excel、喂第三方脚本）。
    ⚠️**做诊断不需要它** —— `jzt_ad_account_check`/`jzt_ad_plan_funnel`/`jzt_ad_grid` 直接读实时数据。
    量级列（花费/展现/点击/订单/金额）**已折算日均**（否则 `消耗÷日预算` 会把每条计划都判成撞线），
    CTR/CVR **已转成分数**。附 `SPUID`（=底表商品编码，批量模板认的那列）与 `SKUID列表`（该计划**实际在投**的 SKU）。"""
    return jzt_swa.export_plans(out_path, start_day, end_day, status, conversion_category)


@mcp.tool()
@safe
def jzt_swa_diagnosis(location: str = "swaTodoListPage") -> dict:
    """[全站营销] 京准通「广告建议/待办」问题清单（首页那些"N 个单元出价低于行业水平"）。"""
    return jzt_swa.diagnosis_problems(location)


# --------------------------- 全站营销：写（dry-run 门） --------------------------- #
@mcp.tool()
@safe
def jzt_swa_budget_dryrun(plan: dict) -> dict:
    """[全站营销] **改日预算 DRY-RUN**：plan={campaignId: 新日预算}，0=不限。组装不发送，回 confirm_token。
    预算区间 100~9999999。**先看体检**：预算利用率本来就 <30% 的，加预算不会带来量。"""
    return jzt_swa.budget_update_dryrun(plan)


@mcp.tool()
def jzt_swa_budget_update(plan: dict, confirm: str = "") -> dict:
    """[全站营销] **改日预算真执行**。需先用相同 plan 跑 jzt_swa_budget_dryrun 拿 confirm_token。
    ⚠️ 该写路径**尚未活体验证**：首次请单条小改并用 jzt_swa_ad_list 读回确认。"""
    try:
        return jzt_swa.budget_update(plan, confirm=confirm)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def jzt_swa_bid_dryrun(plan: dict) -> dict:
    """[全站营销] **改出价 DRY-RUN**：plan={groupId: 目标成交投产比}（值传 null=切智能出价）。回 confirm_token。
    ⚠️ 键是 **groupId 不是 campaignId**。⚠️ 平台限制出价可改次数（见列表行 出价可改次数/已改价次数）。
    下调目标投产比=用更低 ROI 换流量；上调=保利润但可能掉量。"""
    return jzt_swa.bid_update_dryrun(plan)


@mcp.tool()
def jzt_swa_bid_update(plan: dict, confirm: str = "", trace_id: str = "") -> dict:
    """[全站营销] **改出价真执行**（目标成交投产比）。需先用相同 plan 跑 jzt_swa_bid_dryrun 拿 confirm_token。
    ⚠️ 该写路径**尚未活体验证**：首次请单条小改并读回确认。"""
    try:
        return jzt_swa.bid_update(plan, confirm=confirm, trace_id=trace_id)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def jzt_swa_status_dryrun(campaign_ids: list, operation: str) -> dict:
    """[全站营销] **启停/删除 DRY-RUN**：operation ∈ stop/start/delete。回 confirm_token。
    ⚠️ delete **不可逆**。"""
    return jzt_swa.status_update_dryrun(campaign_ids, operation)


@mcp.tool()
def jzt_swa_status_update(campaign_ids: list, operation: str, confirm: str = "") -> dict:
    """[全站营销] **启停/删除真执行**。需先用相同参数跑 jzt_swa_status_dryrun 拿 confirm_token。
    ⚠️ 该写路径**尚未活体验证**；delete 不可逆，无人值守场景禁止自动删。"""
    try:
        return jzt_swa.status_update(campaign_ids, operation, confirm=confirm)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


# --------------------------- 广告诊断（原 jx-ad-diagnose） --------------------------- #
@mcp.tool()
@safe
def jzt_ad_playbook(code: str = None, keyword: str = "", prefix: str = "") -> dict:
    """[诊断] **问题库**（《广告投放商家自查指南》45 条，症状→根因→动作）。
    `code` 取单条（如 A1/KW8/QZ5）；否则按 `keyword` 全文搜、`prefix` 按分类前缀筛
    （A账户 / KG快车通用 / KW关键词 / QD全店 / ZN智能化 / RQ人群 / DP顶部店铺 / QZ全站营销）。
    诊断结论里的编号都能在这查到全文。"""
    if code:
        p = jzt_playbook.problem(code)
        return p or {"error": f"没有编号 {code}；用 keyword/prefix 搜，或看 分类={dict(jzt_playbook.GROUPS)}"}
    return jzt_playbook.search(keyword, prefix)


@mcp.tool()
@safe
def jzt_ad_plan_guard(start_day: str = None, end_day: str = None,
                      max_budget_rate: float = 50.0, min_cost: float = 50.0) -> dict:
    """[诊断] ★**花不动的计划该不该降目标 ROI** —— 补上 account_check 缺的那一步（它拿不到毛利率）。
    毛利率取 ge `预估毛利_投前 ÷ 成交金额`（**投前**，用投后=广告费双扣；成交金额是实付⇒天然到手价口径），
    按计划 skuIdList 汇总 → 保本ROI → 薄利护栏 → 给建议新目标，按 ratio 排序。
    ★实测推翻「收纳类薄利、高目标是刻意护栏」的印象：60 个花不动计划毛利率中位 20%、ratio 1.35~2.48 全过线。
    ⚠️出价可改次数有限(5次/计划) ⇒ 分批调，调完 2~3 天再用 jzt_ad_daily_compare 回读。"""
    return jzt_diagnose.plan_guard(start=start_day, end=end_day,
                                   max_budget_rate=max_budget_rate, min_cost=min_cost)


@mcp.tool()
@safe
def jzt_ad_account_check(start_day: str = None, end_day: str = None, balance: float = None,
                         allocatable: float = None, gross_margin: float = None,
                         budget_mode: str = "月度预算", conversion_category: int = 15) -> dict:
    """[诊断] **账户级体检**（钱→结构→货品→计划横比），结论带问题库编号+动作+观察周期。
    **直接读实时数据，不用导表**。`gross_margin` 请给**到手价口径**毛利率（如 0.16）才能判亏损；
    `budget_mode` 为「月度预算」时跳过 A1 充值建议（那条只适用可充值余额账户）。"""
    return jzt_diagnose.account_check(start_day, end_day, balance, allocatable,
                                      gross_margin, conversion_category, budget_mode=budget_mode)


@mcp.tool()
@safe
def jzt_ad_plan_funnel(plan: str = None, start_day: str = None, end_day: str = None,
                       benchmark_ctr: float = None, benchmark_cvr: float = None,
                       benchmark_cpc: float = None, top: int = 20,
                       conversion_category: int = 15) -> dict:
    """[诊断] **单计划漏斗归因**：CTR/CVR/CPC 对基准找偏离最差的那层当瓶颈，给问题库编号。
    ROI ≈ CTR×CVR×客单/CPC，所以瓶颈只可能在这三层之一。基准默认账户加权均值，
    有**类目基准**请用 benchmark_* 传入（更准）。`plan` 按名称子串筛，不传看花费 Top N。"""
    return jzt_diagnose.plan_funnel(plan, start_day, end_day, benchmark_ctr,
                                    benchmark_cvr, benchmark_cpc, conversion_category, top)


@mcp.tool()
@safe
def jzt_ad_grid(start_day: str = None, end_day: str = None, gross_margin: float = None,
                top: int = 30, conversion_category: int = 15) -> dict:
    """[诊断] **全站 3×3 决策表定位（QZ5，全站最核心）**：把每条推广落到
    「消耗速度 × ROI达成」的格子里，给该格的动作与观察周期。
    ★每条都过**薄利护栏**：达成ROI/保本ROI ≤1.3 时，即使格子说"降目标"也会被否决——
    薄利品降目标会「花钱飞快+达成ROI跳水」打到保本线下。**所以 gross_margin 一定要给**（到手价口径）——护栏已内建于本工具，不需要另外单独调。
    ⚠️接口无分时数据，「跑满且到23点后」与「下午4点前花完」分不出来，跑满的一律按前者取建议。"""
    return jzt_diagnose.grid_3x3(start_day, end_day, gross_margin, conversion_category, top)


# --------------------------- 日维度运营监控 --------------------------- #
@mcp.tool()
@safe
def jzt_ad_budget_pace(monthly_budget: float, mtd_spend: float, date: str = None) -> dict:
    """[日报] **预算节奏**：算"预计几号花完"+红绿灯，防月末断投。
    只对**固定月度预算**账户有意义（当月花完即止）。这是该类账户最关键的一条日常监控。"""
    return jzt_monitor.budget_pace(monthly_budget, mtd_spend, date)


@mcp.tool()
@safe
def jzt_ad_daily_compare(day: str = None, prev_day: str = None, conversion_category: int = 15) -> dict:
    """[日报] **日环比异常 + 断投预警**：消耗/CTR/CVR/ROI/CPC 谁突变，哪些计划昨天在花今天掉零。
    ⚠️当天数据未跑完（实时累计），跟完整的昨天比必然显示下跌 —— 判真异常请用 day=昨天、prev_day=前天。
    日报只做运营预警；毛利/拉黑/放出是周维度结构决策。"""
    return jzt_monitor.daily_compare(day, prev_day, conversion_category)


# --------------------------- 分析账本（跨 run 对比） --------------------------- #
@mcp.tool()
@safe
def jzt_ad_ledger_append(rows: list, run_date: str) -> dict:
    """[账本] 把本次 SKU 级诊断明细留档（同 run_date 重跑覆盖当日那批，幂等）。
    列名自动识别：SKUID/SPU/计划名/京东价毛利/到手价毛利/投后毛利率/归因/最终结论/消耗/ROI。"""
    return jzt_ledger.append(rows, run_date)


@mcp.tool()
@safe
def jzt_np_track(run_date: str = "", note: str = "", write: bool = True) -> dict:
    """[新品追踪] **新品测试计划逐日快照**（2026-08-19 用户手动建的 5 个计划）。
拉当日计划级 消耗/达成ROI/预算利用率，并**现算保本ROI**（1÷到手价毛利率，券促一变就漂，必须现算不能缓存）。
★**判据是 `ratio = 达成ROI ÷ 保本ROI`，不是达成ROI 本身** —— 广告口径只知成交额不知成本：
  ROI 6 对毛利率 22% 的品安全，对 14% 的品是低于保本、投了就亏。
  <1.0 真亏 / 1.0~1.3 薄利 / ≥1.3 安全；**新品前 3 天冷启动不下结论**。
`run_date` 留空=昨天（广告数据 T-1 才完整，取今天会拿到半天数据、判读偏低）。同日重跑幂等覆盖。"""
    from blacklight.jzt import nptrack
    return nptrack.snapshot(run_date=run_date or None, write=write, note=note)


@mcp.tool()
@safe
def jzt_np_history(spu: str = "", days: int = 14) -> dict:
    """[新品追踪] 读新品测试计划的**逐日账本**，判「在爬坡还是一直不行」。`spu` 给了只看那一个。"""
    from blacklight.jzt import nptrack
    return nptrack.history(spu=spu or None, days=days)


@mcp.tool()
@safe
def jzt_ad_daily_ledger(run_date: str = "", days: int = 7, note: str = "", write: bool = True) -> dict:
    """[账本] ★★**每日广告诊断收尾必做：存一份「全量在投 SKU」快照**（用户 2026-08-18 定为日常动作）。

    自动做四件事：① 按日体检剔脏日 ② 在投款算 `post_margin` ③ **给已拉黑的补 `ds_margin`**
    （否则它们没有广告数据、`post_margin` 永远取不到值 ⇒ **永远放不出来**）④ 写账本。

    ⚠️**别只存"本次动手的那几款"**：局部批次会让 `ledger_compare` 失效——实测 5 条 vs 1167 条、
    交集 5 ⇒ 报「可放出 0」，那是**没得比**不是没机会；补跑全量后挖出 113 款被长期摁死的赚钱品。
    跑完接着跑 `jzt_ad_ledger_compare`。"""
    return jzt_diagnose.daily_ledger(run_date=run_date or None, days=days,
                                     note=note, write=write)


@mcp.tool()
@safe
def jzt_ad_ledger_compare(release_post: float = 0.0, release_ds: float = 0.12,
                          limit: int = 20) -> dict:
    """[账本] 对比最近两个 run：**可放出**（上次拉黑、这次达标）/ 新增拉黑 / 持续拉黑 / 反复横跳。
    ★**每次分析先跑它**：先把能放出的放回投放，再处理新增拉黑，别每次从零判。
    「反复横跳」是阈值临界的抖动，别再来回动。"""
    return jzt_ledger.compare(None, release_post, release_ds, limit)


@mcp.tool()
@safe
def jzt_ad_ledger_history(sku: str) -> dict:
    """[账本] 看某个 SKU 的历次快照（判断它是稳定亏还是临界抖动）。"""
    return jzt_ledger.history(sku)


# --------------------------- 批量执行模板 --------------------------- #
@mcp.tool()
@safe
def jzt_ad_batch_template(rows: list, out_path: str, pin: str = "【请填写采销PIN】",
                          action: str = "修改", subsidy: str = "启动",
                          blacklist_when: str = "持续亏损|拉黑|blacklist|关停|剔除",
                          default_budget: str = None, template: str = None) -> dict:
    """[执行] 把分类好的 SKU 清单生成**「全站营销-单品推广」批量操作模板 xlsx**，给运营上传。
    默认把命中的亏损 SKU 按 SPU 分组填进 `SKU黑名单`，**预算/出价原样保留**。
    必须 .xlsx（CSV 会把逗号分隔的 SKU 串当千分位数字合并）。返回附人读对照清单 + 上传前必须人工确认项。
    ⚠️只填高置信度动作；`广告主PIN` 和 `智能补贴券`（修改场景会覆盖现状）务必上传前核对。"""
    return jzt_batch.build(rows, out_path, pin, action, subsidy, blacklist_when,
                           default_budget, template)


def main():
    mcp.run()


if __name__ == "__main__":
    main()


# ---------------------- SKU 黑名单（全站营销单元级屏蔽） ---------------------- #
@mcp.tool()
@safe
def jzt_swa_sku_black_query(campaign_id: int, ad_group_id: int, pin: str = "") -> dict:
    """[全站营销] 读某单元的 **SKU 黑名单**现状。

    ⚠️两个列表很容易看反：
      · `已拉黑`(blackSkuList)      —— 真正的黑名单
      · `在投候选`(effectiveSkuList) —— 该单元在投、**可选进黑名单**的池子，不是黑名单
    """
    return jzt_swa.sku_black_query(campaign_id, ad_group_id, pin=pin or None)


@mcp.tool()
@safe
def jzt_swa_sku_black_dryrun(campaign_id: int, ad_group_id: int, add: list = None,
                             remove: list = None, pin: str = "") -> dict:
    """[全站营销] SKU 黑名单 DRY-RUN：读现状 → 算全集 → 回 confirm_token，不发送。

    ★★`/swa/skublack/update` 是**全量覆盖不是追加**（skublack 只有 query/update，
      add/delete/remove 全 404 ⇒ 取消拉黑只能提交不含它的集合）。
      **直接照抓包形状只传新增的 skuIds，会把该单元已有的黑名单全部清空。**
      本工具已自动并入现状，请始终走它，别绕过去裸调接口。
    """
    return jzt_swa.sku_black_update_dryrun(campaign_id, ad_group_id,
                                           add=add, remove=remove, pin=pin or None)


@mcp.tool()
@safe
def jzt_swa_sku_black_update(campaign_id: int, ad_group_id: int, confirm: str,
                             add: list = None, remove: list = None, pin: str = "") -> dict:
    """[全站营销] **SKU 黑名单真执行**（覆盖语义，已自动并入现状）。需先跑 dryrun 拿 confirm_token。

    执行后自动回读比对，返回 `落地一致`。⚠️写路径**未活体验证**，首次先单条探针再批量。
    """
    return jzt_swa.sku_black_update(campaign_id, ad_group_id, add=add, remove=remove,
                                    confirm=confirm, pin=pin or None)
