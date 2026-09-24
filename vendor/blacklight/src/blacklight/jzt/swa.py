"""
jzt 场域：**京准通 - 全站营销（swa / 全站推广）**。

页面 `cxjzt.jd.com/swa/index.html#/promote`，网关 `cxjzt-api.jd.com`，鉴权见 `jzt/auth.py`（免密登录）。

★ `/swa/ad/list` 的入参一度卡死在 `转化周期不允许为空;` —— 谜底是 **`conversionCategory`**
  （前端叫 `clickOrOrderDay`，从 bundle `index-43dd7041` 反解，2026-08-04 实证）。
  这里把前端 `data.params` 的**完整基线**照抄下来，别再自己拼半套：

    {page, pageSize, platform:"", status:"", sxuType:"", sxuId:"", filters:[], obys:"",
     product:"swa_dsp", startDay, endDay, conversionCategory:15, campaignType:101}

  - `conversionCategory` = 转化周期，即「广告点击后 N 天内累计的成交订单」：0=投放期间/1/3/7/15 天。
    **平台建议 15**，页面默认也是 15 —— 换值会换一整套口径，跨时点对比必须锁同一个值。
  - `product:"swa_dsp"` 不是可选项：**缺了它 `data.ext`（合计行）返回 null**。
  - `obys` 排序格式 `"<字段>|asc|desc"`（如 `cost|desc`）。

指标口径（2026-08-04 与页面逐项核对 ext 合计行）：
    cost=花费 / totalOrderROI=全站投产比 / totalOrderSum=全站交易额 / totalOrderCnt=全站订单行
    orderCPA=全站订单成本（**不是** totalOrderCpo）/ impressions,clicks=核心位置展现·点击
    newCustomer180Days=180天未购新客数
⚠️ 这些是**实时累计**值，几分钟就会变；跨时点对比要么同一次拉取，要么明确记录拉取时刻。

金额单位一律元。写操作（预算/出价/启停）走 dry-run + confirm_token + 审计。
"""
from __future__ import annotations

from typing import Optional

import io
from blacklight.core import BlacklightError, pace
from blacklight.core.base import ConfirmGate, audited, scene_cfg
from blacklight.core import paths
from blacklight.jzt import auth as jzt_auth

# ---- 常量（bundle 反解 + 抓包实证）----
BUSINESS_TYPE = 600000004
CAMPAIGN_TYPE = 101            # 101=全站营销(推广管理)；118=全站商品，走的是另一个列表接口
SEARCH_TYPE = 2
CONVERSION_DEFAULT = 15        # 转化周期，平台建议值；换值=换口径
ORDER_STATUS_CATEGORY = 15

# 推广状态（bundle 常量表）
STATUS = {1: "暂停", 2: "有效", 3: "预算用完", -3: "审核下线", 9: "下线"}
# status/update 的动作码
OP = {"stop": 1, "start": 2, "delete": 3}

# 出价方式（biddingType）—— 全站营销只有这两种
BIDDING_TYPE = {8192: "目标成交投产比", 65536: "智能出价"}

BUDGET_MIN, BUDGET_MAX = 100.0, 9999999.0      # 前端校验：新业务下限 50，常规 100
MAX_WRITE_BATCH = 50                            # 单次写操作条数护栏

_G_BUDGET = ConfirmGate("jzt/swa/budget")
_G_BID = ConfirmGate("jzt/swa/bid")
_G_STATUS = ConfirmGate("jzt/swa/status")


def _f(v, nd: int = 2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 只读：推广列表
# --------------------------------------------------------------------------- #
def _params(page: int, page_size: int, start_day: str, end_day: str, *,
            status=None, conversion_category: int = CONVERSION_DEFAULT,
            order_by: str = "", sxu_id="", filters=None) -> dict:
    """照抄前端 data.params 基线；少一个字段就可能换一种报错，别精简。"""
    return {"page": int(page), "pageSize": int(page_size),
            "platform": "", "status": "" if status in (None, "") else status,
            "sxuType": "", "sxuId": "" if sxu_id in (None, "") else str(sxu_id),
            "filters": list(filters or []), "obys": order_by or "",
            "product": "swa_dsp",
            "startDay": start_day, "endDay": end_day,
            "conversionCategory": int(conversion_category),
            "campaignType": CAMPAIGN_TYPE,
            # 下面三个不在前端 params 基线里，但 forbidden/list 等同族接口都带，服务端接受
            "businessType": BUSINESS_TYPE, "searchType": SEARCH_TYPE, "requestFrom": 0}


def _row(r: dict, days: int = 1) -> dict:
    """压成业务行。保留 campaignId/groupId —— 改预算按 campaignId、改出价按 groupId。

    ⚠️`days` 是查询区间天数：`cost` 是**区间累计**花费，而 `dayBudget` 是**每日**预算，
    两者直接相除会得到 132% 这种假象（7 天花完 7 天预算的 93%，看着像"超预算"）。
    预算利用率必须用**日均花费**比日预算。"""
    budget = _f(r.get("dayBudget"))
    cost = _f(r.get("cost"))
    bid = _f(r.get("groupBid"))
    roi = _f(r.get("totalOrderROI"))
    daily_cost = (cost / days) if (cost is not None and days) else None
    return {
        # ---- 主键（写操作要用）----
        "campaignId": r.get("campaignId"),          # 改预算 / 启停 / 删除
        "groupId": r.get("groupId"),                # 改出价
        "推广名": r.get("adName"),
        "spuId": r.get("spuId"),
        "skuIdList": r.get("skuIdList") or [],
        "商品名": r.get("spuName"),
        # ---- 状态 / 设置 ----
        "状态": STATUS.get(r.get("status"), r.get("status")),
        "statusCode": r.get("status"),
        "日预算": budget,
        "出价方式": BIDDING_TYPE.get(r.get("biddingType"), r.get("biddingType")),
        "出价": bid,                                 # 目标成交投产比时=目标ROI
        "出价说明": r.get("biddingInfo"),
        "创建日": r.get("createdTime"),
        # ---- 效果 ----
        "花费": cost,
        "日均花费": _f(daily_cost),
        "展现": _i(r.get("impressions")), "点击": _i(r.get("clicks")),
        "点击率%": _f(r.get("ctr")), "CPC": _f(r.get("cpc")), "CPM": _f(r.get("cpm")),
        "全站订单行": _i(r.get("totalOrderCnt")),
        "全站交易额": _f(r.get("totalOrderSum")),
        "全站投产比": roi,
        "全站订单成本": _f(r.get("orderCPA")),
        "转化率%": _f(r.get("orderCVS")),
        "180天未购新客": _i(r.get("newCustomer180Days")),
        # ---- 诊断用派生 ----
        "预算利用率%": (round(daily_cost / budget * 100, 1)      # 日均花费 ÷ 日预算
                        if (budget and daily_cost is not None and budget > 0) else None),
        "ROI达成率%": (round(roi / bid * 100, 1) if (bid and roi is not None and bid > 0) else None),
        # ---- 其它有业务含义的透出 ----
        "一键起量可开": (r.get("speedUpSetting") or {}).get("canStart") == 1,
        "起量提示": (r.get("speedUpSetting") or {}).get("msg"),
        "赔付状态": r.get("refundStatusDesc"),
        "出价可改次数": r.get("bidChangeLimit"),
        "已改价次数": r.get("priceChangeCount"),
        "诊断建议": r.get("diagnosisMsgs") or None,
    }


def _totals(ext: Optional[dict]) -> dict:
    ext = ext or {}
    return {"花费": _f(ext.get("cost")), "展现": _i(ext.get("impressions")),
            "点击": _i(ext.get("clicks")), "点击率%": _f(ext.get("ctr")),
            "CPC": _f(ext.get("cpc")), "CPM": _f(ext.get("cpm")),
            "全站订单行": _i(ext.get("totalOrderCnt")),
            "全站交易额": _f(ext.get("totalOrderSum")),
            "全站投产比": _f(ext.get("totalOrderROI")),
            "全站订单成本": _f(ext.get("orderCPA")),
            "转化率%": _f(ext.get("orderCVS")),
            "180天未购新客": _i(ext.get("newCustomer180Days"))}


def _default_days(start_day, end_day):
    """缺省取近 7 天（含今天）。用本地日期——京准通报表按北京时区。"""
    import datetime as _dt
    if start_day and end_day:
        return start_day, end_day
    today = _dt.date.today()
    return (start_day or str(today - _dt.timedelta(days=6))), (end_day or str(today))


def _span_days(start_day: str, end_day: str) -> int:
    """区间天数（含头尾）。用来把区间累计花费还原成日均，好跟**日**预算比。解析不了退回 1。"""
    import datetime as _dt
    try:
        s = _dt.date.fromisoformat(start_day)
        e = _dt.date.fromisoformat(end_day)
        return max(1, (e - s).days + 1)
    except (TypeError, ValueError):
        return 1


def ad_list(page: int = 1, page_size: int = 20, start_day: str = None, end_day: str = None,
            status=None, conversion_category: int = CONVERSION_DEFAULT,
            order_by: str = "cost|desc", sxu_id=None, pin: str = None) -> dict:
    """**推广列表（单页）**。status 缺省=全部（含已删/下线）；传 2 只看有效。order_by 如 `cost|desc`。

    返回 {日期区间, 转化周期, total, page, rows[], 合计}。**合计是整个筛选结果的合计，不是本页**。"""
    s, e = _default_days(start_day, end_day)
    d = jzt_auth.post("/swa/ad/list",
                      _params(page, page_size, s, e, status=status,
                              conversion_category=conversion_category,
                              order_by=order_by, sxu_id=sxu_id), pin=pin)
    pg = d.get("paginator") or {}
    days = _span_days(s, e)
    return {"日期区间": f"{s}~{e}", "天数": days,
            "转化周期": f"{conversion_category}天" if conversion_category else "投放期间",
            "账号": pin or jzt_auth.current_account(),
            "total": pg.get("items"), "pages": pg.get("pages"), "page": pg.get("page"),
            "rows": [_row(x, days) for x in (d.get("data") or [])],
            "合计": _totals(d.get("ext")),
            "_提示": "花费/订单为实时累计值，几分钟即变；跨时点对比请记录拉取时刻。"
                     "预算利用率=**日均**花费÷日预算（花费是区间累计，别直接除）。"}


def ad_all(start_day: str = None, end_day: str = None, status=None,
           conversion_category: int = CONVERSION_DEFAULT, order_by: str = "cost|desc",
           page_size: int = 100, max_pages: int = 50, pin: str = None) -> dict:
    """**全量拉取推广**（自动翻页）。

    ⚠️翻页会**静默截断**（京东分页接口通病：中间某页偶发返回空）——这里不吞：
    空页**重试 2 次**，仍空才停并在 `truncated` 里标出；末了拿 `total` 对账，
    条数对不上会在返回里明说，**别把不全的清单当全量用**。"""
    s, e = _default_days(start_day, end_day)
    days = _span_days(s, e)
    rows, total, totals, truncated, page = [], None, {}, None, 1
    seen = set()
    while page <= max_pages:
        d = None
        for attempt in range(3):
            d = jzt_auth.post("/swa/ad/list",
                              _params(page, page_size, s, e, status=status,
                                      conversion_category=conversion_category,
                                      order_by=order_by), pin=pin)
            if d.get("data"):
                break
        batch = d.get("data") or []
        if total is None:
            total = (d.get("paginator") or {}).get("items")
            totals = _totals(d.get("ext"))
        if not batch:
            if total is not None and len(rows) < total:
                truncated = f"第 {page} 页连续 3 次返回空，已停在 {len(rows)}/{total} 条"
            break
        for x in batch:
            cid = x.get("campaignId")
            if cid in seen:                      # 翻页错位时的重复保护
                continue
            seen.add(cid)
            rows.append(_row(x, days))
        if total is not None and len(rows) >= total:
            break
        page += 1
    complete = (total is not None and len(rows) == total)
    return {"日期区间": f"{s}~{e}", "天数": days,
            "转化周期": f"{conversion_category}天" if conversion_category else "投放期间",
            "账号": pin or jzt_auth.current_account(),
            "count": len(rows), "total": total, "完整": complete,
            "truncated": truncated or (None if complete else f"拉到 {len(rows)} 条 / 服务端 total {total}，对不上"),
            "rows": rows, "合计": totals}


def summary(start_day: str = None, end_day: str = None, status=None,
            conversion_category: int = CONVERSION_DEFAULT, pin: str = None) -> dict:
    """**账户汇总指标**（只要合计行，不拉明细）。等价于页面顶部那排数字。"""
    r = ad_list(page=1, page_size=1, start_day=start_day, end_day=end_day, status=status,
                conversion_category=conversion_category, pin=pin)
    return {k: r[k] for k in ("日期区间", "天数", "转化周期", "账号", "total", "合计", "_提示")}


def _roi_conclusion(mostly_attained: bool, guard: dict) -> str:
    """把「达成率」结论与「薄利护栏」合成一句话。

    ⚠️分开说会出事：光看达成率会得出"基本达标 → 降目标换量"，但薄利品降目标是**反向操作**
    （会把达成 ROI 打到保本线下）。所以护栏能否通过必须与结论绑在一起给。"""
    if not mostly_attained:
        return "较多推广明显打不到目标 ROI → 先查素材/人群/商品转化，别急着调价"
    base = "多数推广基本打到目标 ROI（tROI 系统天然略微欠投）→ 系统是在按目标控量。"
    can = guard.get("可降目标")
    if can is True:
        return base + f"护栏够厚（{guard['ratio']}x 保本）→ 想恢复量可**下调目标成交投产比**换流量，而不是加预算"
    if can is False:
        return base + f"★但**禁止下调目标**：{guard['结论']}"
    if can == "谨慎":
        return base + f"★下调需谨慎：{guard['结论']}"
    return base + f"★能否下调目标**尚不能判断**——{guard['结论']}（传 gross_margin 到手价毛利率即可判）"


def health(start_day: str = None, end_day: str = None,
           conversion_category: int = CONVERSION_DEFAULT,
           budget_util_low: float = 30.0, roi_gap_low: float = 80.0,
           min_cost: float = 10.0, gross_margin: float = None, pin: str = None) -> dict:
    """**账户体检**：只看有效推广，回答「掉量到底卡在预算还是卡在目标 ROI」。

    三类结论（2026-08-04 的核心假设，这里把它变成可复算的数字）：
      - `预算不是约束`：预算利用率（**日均**花费÷日预算）< budget_util_low% 的条数/占比 —— 加预算没用；
      - `ROI 目标偏高`：实际投产比 / 目标出价 < roi_gap_low% —— 系统按目标 ROI 控量，控狠了就掉量；
      - `订单成本畸高`：全站订单成本排前列的，交给毛利侧判是否真亏（本工具不判盈亏）。
    只统计 花费 ≥ min_cost 的推广（零花费的算不出利用率，纳入会稀释结论）。

    ★给了 `gross_margin`（**到手价口径**毛利率，如 0.16）才会过**薄利护栏**：
    「下调目标ROI换量」这条建议有前提——离保本 ROI 还有足够空间。薄利品降目标会
    「花钱飞快 + 达成ROI跳水」打到保本线下。**不给毛利率时本工具不敢给调价方向**。

    ★`roi_gap_low` 默认 **80** 不是 90：tROI 出价的系统**天然略微欠投**，
    2026-08-04 实测 79 条的达成率分布 p25=79.7 / 中位=87.8 / p75=93.2 / max=110 ——
    卡 90% 正好切在中位数上，结论会在"达标/不达标"之间反复横跳。
    80% 才是"明显没打到目标"。返回里带 `ROI达成率分位` 供自行判断，别只看这一个布尔。"""
    all_ = ad_all(start_day=start_day, end_day=end_day, status=2,
                  conversion_category=conversion_category, pin=pin)
    rows = [r for r in all_["rows"] if (r["花费"] or 0) >= min_cost]
    if not rows:
        return {**{k: all_[k] for k in ("日期区间", "天数", "转化周期", "账号", "完整", "truncated")},
                "样本": 0, "note": f"没有花费≥{min_cost}元的有效推广"}

    low_budget = [r for r in rows if r["预算利用率%"] is not None and r["预算利用率%"] < budget_util_low]
    low_roi = [r for r in rows if r["ROI达成率%"] is not None and r["ROI达成率%"] < roi_gap_low]
    utils = [r["预算利用率%"] for r in rows if r["预算利用率%"] is not None]
    cpo = sorted([r for r in rows if r["全站订单成本"] is not None],
                 key=lambda r: -r["全站订单成本"])[:10]

    def _pct(vals):
        """分位数（阈值是人定的，分布是客观的——两个都给出来，别只给布尔结论）。"""
        v = sorted(x for x in vals if x is not None)
        if not v:
            return None
        return {"min": v[0], "p25": v[len(v) // 4], "中位": v[len(v) // 2],
                "p75": v[3 * len(v) // 4], "max": v[-1]}

    def brief(r):
        return {k: r[k] for k in ("campaignId", "groupId", "推广名", "花费", "日均花费", "日预算",
                                  "预算利用率%", "出价", "全站投产比", "ROI达成率%",
                                  "全站订单成本", "全站订单行")}

    # 薄利护栏：拿账户整体达成 ROI 对保本 ROI 比，决定"降目标"这条建议能不能给
    from blacklight.jzt import playbook as _pb
    guard = _pb.thin_margin_guard(all_["合计"].get("全站投产比"),
                                  _pb.breakeven_roi(gross_margin))

    return {
        **{k: all_[k] for k in ("日期区间", "天数", "转化周期", "账号", "完整", "truncated")},
        "样本": len(rows), "有效推广总数": all_["count"], "合计": all_["合计"],
        "预算利用率分位%": _pct(utils),
        "ROI达成率分位%": _pct([r["ROI达成率%"] for r in rows]),
        "预算不是约束": {
            "阈值": f"预算利用率<{budget_util_low}%",
            "条数": len(low_budget), "占比%": round(len(low_budget) / len(rows) * 100, 1),
            "结论": ("多数推广远没花完预算 → 掉量不是预算卡的，加预算无效"
                     if len(low_budget) >= len(rows) * 0.6 else "预算利用率不低，预算可能确实是约束之一"),
            "样例": [brief(r) for r in sorted(low_budget, key=lambda r: r["预算利用率%"] or 0)[:10]]},
        "ROI目标偏高": {
            "阈值": f"实际投产比/目标出价<{roi_gap_low}%",
            "条数": len(low_roi), "占比%": round(len(low_roi) / len(rows) * 100, 1),
            "结论": _roi_conclusion(len(low_roi) < len(rows) * 0.4, guard),
            "薄利护栏": guard,
            "样例": [brief(r) for r in sorted(low_roi, key=lambda r: r["ROI达成率%"] or 0)[:10]]},
        "订单成本最高10条": [brief(r) for r in cpo],
        "_下一步": ("订单成本高≠亏损。判真亏接 **ge 投后毛利**"
                    "（`pnl_margin_scan` / `ge.margin.judge_losing`）——"
                    "`osw_margin_list_low` 已退役。"
                    "★另接手 `pnl_margin_scan` 的「广告空耗」：零成交但有广告消耗的款，"
                    "亏损 100% 是广告费，改价摘券治不了，正是本域该处理的。"),
    }


def export_plans(out_path: str = None, start_day: str = None, end_day: str = None,
                 status=2, conversion_category: int = CONVERSION_DEFAULT,
                 pin: str = None) -> dict:
    """**导出「计划清单」CSV**，给**外部/人工**用（发给别人、丢进 Excel 自己看、喂第三方分析脚本）。

    ⚠️**做诊断不需要它**：`jzt_ad_account_check` / `jzt_ad_plan_funnel` / `jzt_ad_grid` 直接读实时数据。
    （原本它是为独立 skill `jx-ad-diagnose` 的手工导表步准备的，那个 skill 2026-08-04 已打散进本包。）

    ★**所有量级列一律折算成日均**（花费/展现/点击/订单量/订单金额），这不是可选项：
      判「花不动 / 撞线」要用 `消耗 ÷ 日预算`，而接口给的 `cost` 是**区间累计**、`dayBudget` 是**每日**
      —— 不折算会让每条计划都算成撞线（本账号 7 天口径下虚高 7 倍）。在数据出口就消掉，不指望下游记得除。
      量级全部同比例缩放 → **比率类指标（CTR/CVR/CPC/ROI）不受影响**，同时 `消耗/预算` 变正确。
    同时把 ROI/CTR/CVR/CPC 作为**现成列**写出，且 **CTR/CVR 写成分数（0.0227）不是百分数**，
    避免下游重算口径错位（详见 `docs/jzt/NOTES_playbook.md`）。

    另附 `SPUID`/`SKUID列表`/`campaignId`/`groupId`：
      - `SPUID` = 接口 `spuId`，**已实证等于 osw 商品列表的 `productId`（即底表「商品编码」）**，
        正是批量模板认的那一列（**别用「供货商spuId」**，那是供应商内部 ID，全站后台查不到）。
      - `SKUID列表` 是**该计划实际在投的 SKU**（可能少于该 SPU 全部 SKU —— 差额是被拉黑/不可投的），
        做 SKU 黑名单时别拿 osw 的全量 SKU 当基准。"""
    import csv as _csv
    import os as _os
    data = ad_all(start_day=start_day, end_day=end_day, status=status,
                  conversion_category=conversion_category, pin=pin)
    days = data["天数"] or 1
    if not out_path:
        out_path = _os.path.join(_os.getcwd(),
                                 f"_jzt_plans_{data['日期区间'].replace('~', '_')}.csv")

    def per_day(v):
        return round(v / days, 2) if isinstance(v, (int, float)) else None

    def frac(v):
        """接口的 CTR/CVR 是**百分数**(2.27)，而 ad_diagnose 按**分数**处理
        （它自己的兜底算法是 clicks/impr，且用 `:.0%` 格式化）。不换算会打出 `CTR 227%`。"""
        return round(v / 100.0, 6) if isinstance(v, (int, float)) else None

    cols = ["计划名", "渠道", "日预算", "消耗", "展现", "点击", "订单量", "订单金额",
            "投产比", "点击率", "转化率", "平均点击成本",
            "SPUID", "SKUID列表", "campaignId", "groupId", "状态", "出价方式", "目标投产比",
            "花费(区间累计)", "订单金额(区间累计)", "全站订单成本", "180天未购新客", "创建日"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(cols)
        for r in data["rows"]:
            w.writerow([
                r["推广名"], "全站营销", r["日预算"],
                per_day(r["花费"]), per_day(r["展现"]), per_day(r["点击"]),
                per_day(r["全站订单行"]), per_day(r["全站交易额"]),
                r["全站投产比"], frac(r["点击率%"]), frac(r["转化率%"]), r["CPC"],
                r["spuId"], ",".join(str(x) for x in (r["skuIdList"] or [])),
                r["campaignId"], r["groupId"], r["状态"], r["出价方式"], r["出价"],
                r["花费"], r["全站交易额"], r["全站订单成本"], r["180天未购新客"], r["创建日"],
            ])
    return {"path": out_path, "rows": len(data["rows"]), "天数": days,
            "日期区间": data["日期区间"], "完整": data["完整"], "truncated": data["truncated"],
            "口径": f"量级列已折算日均(÷{days}天)；点击率/转化率已由百分数转分数(ad_diagnose 口径)",
            "⚠️余额检查会误报": "任何用「余额÷单日总预算」判是否该充值的规则(A1)，在全站营销下都会误报——"
                                "全站日预算是**没人碰得到的天花板**（本账号实测利用率中位 3.6%），"
                                "加总必然远超余额。真实资金安全度看「余额÷单日总**消耗**」。",
            "_做诊断不用这个文件": "账户体检/漏斗归因/3×3 决策表直接用 jzt_ad_account_check / "
                                   "jzt_ad_plan_funnel / jzt_ad_grid，它们读实时数据、无需导出。"}


def diagnosis_problems(location: str = "swaTodoListPage", pin: str = None) -> dict:
    """京准通「广告建议 / 待办」问题清单（首页那些"XX个单元出价低于行业水平"）。"""
    d = jzt_auth.post(f"/optimize/tool/diagnosis/jzt/helper-problem-list?location={location}",
                      {}, pin=pin)
    return {"location": location, "data": d}


# --------------------------------------------------------------------------- #
# 写：日预算 / 出价 / 启停删  —— dry-run + confirm + 审计
# --------------------------------------------------------------------------- #
def _check_batch(n: int, what: str):
    if n <= 0:
        raise BlacklightError(f"{what}：一条都没有，不发送。")
    if n > MAX_WRITE_BATCH:
        raise BlacklightError(f"{what}：单次 {n} 条超过护栏 {MAX_WRITE_BATCH} 条，请拆批。")


def _budget_body(plan: dict) -> dict:
    """plan: {campaignId: dayBudget}。dayBudget=0 表示**不限预算**（前端用字符串 "0"）。"""
    cmds = []
    for cid, budget in plan.items():
        b = float(budget)
        if b != 0 and not (BUDGET_MIN <= b <= BUDGET_MAX):
            raise BlacklightError(
                f"campaignId {cid} 日预算 {b} 超出范围 [{BUDGET_MIN}, {BUDGET_MAX}]（0=不限）。")
        cmds.append({"campaignId": int(cid),
                     "dayBudget": "0" if b == 0 else b,
                     "campaignType": CAMPAIGN_TYPE})
    cmds.sort(key=lambda c: c["campaignId"])      # 定序：令牌基于本 body，入参顺序不该改变令牌
    return {"campaignType": CAMPAIGN_TYPE, "campaignBudgetUpdateCommandList": cmds}


def budget_update_dryrun(plan: dict, pin: str = None) -> dict:
    """**改日预算 DRY-RUN**：plan={campaignId: 新日预算}（0=不限）。组装 body 不发送，回 confirm_token。"""
    _check_batch(len(plan or {}), "改日预算")
    body = _budget_body(plan)
    return {"executed": False, "count": len(body["campaignBudgetUpdateCommandList"]),
            "confirm_token": _G_BUDGET.body_token(body["campaignBudgetUpdateCommandList"]),
            "body": body,
            "_note": f"预算区间 {BUDGET_MIN}~{BUDGET_MAX}，0=不限。真执行请带 confirm。"}


@audited("jzt_swa", "budget_update")
def budget_update(plan: dict, confirm: str = "", pin: str = None) -> dict:
    """**改日预算真执行**。需先用相同 plan 跑 budget_update_dryrun 拿 confirm_token。"""
    _check_batch(len(plan or {}), "改日预算")
    body = _budget_body(plan)
    _G_BUDGET.check_body(confirm, body["campaignBudgetUpdateCommandList"])
    d = jzt_auth.post("/swa/budget/update", body, pin=pin)
    fails = d.get("failList") or []
    return {"executed": True, "count": len(body["campaignBudgetUpdateCommandList"]),
            "成功": len(body["campaignBudgetUpdateCommandList"]) - len(fails),
            "失败": len(fails), "failList": fails, "raw": d}


def _bid_body(plan: dict, trace_id: str = "") -> dict:
    """plan: {groupId: 目标成交投产比}。全站营销的 tROI 出价：biddingType 8192 / controlType 2 / target 22。
    值为 None → 切「智能出价」（biddingType 65536, tcpaBid=null, controlType 1）。"""
    cmds = []
    for gid, roi in plan.items():
        if roi is None:
            cmds.append({"id": int(gid), "biddingType": 65536, "tcpaBid": None,
                         "biddingTarget": 22, "biddingControlType": 1,
                         "campaignType": CAMPAIGN_TYPE})
        else:
            v = float(roi)
            if v <= 0:
                raise BlacklightError(f"groupId {gid}: 目标成交投产比必须 >0（传 None 才是切智能出价）。")
            cmds.append({"id": int(gid), "biddingType": 8192, "tcpaBid": v,
                         "biddingTarget": 22, "biddingControlType": 2,
                         "campaignType": CAMPAIGN_TYPE})
    cmds.sort(key=lambda c: c["id"])              # 定序，理由同 _budget_body
    return {"adGroupBiddingUpdateCommandList": cmds, "bidSuggestTraceId": trace_id,
            "campaignType": CAMPAIGN_TYPE}


def bid_update_dryrun(plan: dict, pin: str = None) -> dict:
    """**改出价 DRY-RUN**：plan={groupId: 目标成交投产比}（None=切智能出价）。

    ⚠️ groupId **不是** campaignId —— 从 ad_list 行里取 `groupId`。
    ⚠️ 每条推广的出价修改次数有上限（行里 `出价可改次数`/`已改价次数`），改前先看。"""
    _check_batch(len(plan or {}), "改出价")
    body = _bid_body(plan)
    return {"executed": False, "count": len(body["adGroupBiddingUpdateCommandList"]),
            # 令牌只取命令列表，不含信封——bid_update 真执行时会带 trace_id 而 dry-run 不带，
            # 把信封计入会引入和上面同一类的"同一意图不同令牌"
            "confirm_token": _G_BID.body_token(body["adGroupBiddingUpdateCommandList"]),
            "body": body,
            "_note": "下调目标成交投产比=用更低 ROI 换更多流量；上调=保利润但可能掉量。真执行请带 confirm。"}


@audited("jzt_swa", "bid_update")
def bid_update(plan: dict, confirm: str = "", trace_id: str = "", pin: str = None) -> dict:
    """**改出价真执行**（目标成交投产比）。需先用相同 plan 跑 bid_update_dryrun 拿 confirm_token。"""
    _check_batch(len(plan or {}), "改出价")
    body = _bid_body(plan, trace_id)
    _G_BID.check_body(confirm, body["adGroupBiddingUpdateCommandList"])
    d = jzt_auth.post("/swa/bid/update", body, pin=pin)
    fails = d.get("failList") or []
    return {"executed": True, "count": len(body["adGroupBiddingUpdateCommandList"]),
            "成功": len(body["adGroupBiddingUpdateCommandList"]) - len(fails),
            "失败": len(fails), "failList": fails, "raw": d}


def status_update_dryrun(campaign_ids: list, operation: str, pin: str = None) -> dict:
    """**启停/删除 DRY-RUN**：operation ∈ stop(暂停) / start(启动) / delete(删除)。"""
    op = OP.get(str(operation).lower())
    if op is None:
        raise BlacklightError(f"operation 只能是 {list(OP)}，收到 {operation!r}")
    ids = [int(x) for x in (campaign_ids or [])]
    _check_batch(len(ids), f"推广{operation}")
    body = {"ids": ids, "status": op, "campaignType": CAMPAIGN_TYPE}
    return {"executed": False, "operation": operation, "count": len(ids),
            "confirm_token": _G_STATUS.token(op=op, ids=",".join(map(str, sorted(ids)))),
            "body": body,
            "_warn": "delete 不可逆（推广删除后不可恢复），确认再执行。" if op == 3 else None}


@audited("jzt_swa", "status_update")
def status_update(campaign_ids: list, operation: str, confirm: str = "", pin: str = None) -> dict:
    """**启停/删除真执行**。需先用相同参数跑 status_update_dryrun 拿 confirm_token。"""
    op = OP.get(str(operation).lower())
    if op is None:
        raise BlacklightError(f"operation 只能是 {list(OP)}，收到 {operation!r}")
    ids = [int(x) for x in (campaign_ids or [])]
    _check_batch(len(ids), f"推广{operation}")
    _G_STATUS.check(confirm, op=op, ids=",".join(map(str, sorted(ids))))
    d = jzt_auth.post("/swa/status/update",
                      {"ids": ids, "status": op, "campaignType": CAMPAIGN_TYPE}, pin=pin)
    fails = d.get("failList") or []
    return {"executed": True, "operation": operation, "count": len(ids),
            "成功": len(ids) - len(fails), "失败": len(fails), "failList": fails, "raw": d}


# ---------- SKU 黑名单（计划/单元级屏蔽投放） ----------
_G_BLACK = ConfirmGate("jzt/swa/skublack")

# ★★`/swa/skublack/update` 是**全量覆盖**，不是追加。
#   证据（2026-08-10 逆向）：整个 skublack 只有 `query` 和 `update` 两个接口，
#   探测 add/delete/remove/save/cancel **全部 404** —— 既然没有删除接口，
#   取消拉黑只能靠"提交一个不含它的集合"，那 update 必然是覆盖语义。
#   ⇒ **直接照抓包的形状调用（只传要新增的 skuIds），会把该单元已有的黑名单全部清空。**
#   本模块所有写路径一律 **先 query 读现状 → 求并集/差集 → 提交全集**，
#   这在"覆盖"和"追加"两种语义下都正确，不依赖对语义的猜测。


def sku_black_query(campaign_id, ad_group_id, pin: str = None) -> dict:
    """读某个单元的 SKU 黑名单现状。

    返回 `{已拉黑, 在投候选, limit}`：
      · `blackSkuList`    —— **已经拉黑的**（真正的黑名单）
      · `effectiveSkuList`—— 该单元**在投的候选 SKU**（可选进黑名单的池子），**不是黑名单**
      两个名字很容易看反，2026-08-10 我第一次就读错了。

    ★**恒等式（2026-08-10 实测 8/8 成立）**：
        `len(ad_list 的 skuIdList)` + `已拉黑数` == `在投候选数`
      即 **`/swa/ad/list` 的 `skuIdList` 已经排除了被拉黑的 SKU**，它是「当前真在投」的集合。
      ⇒ 拿 skuIdList 把 SKU 明细挂回计划**是对的**，不会漏；
        那些"挂不上任何计划"的 SKU 恰恰是**已处于拉黑状态**的（它们的广告费是拉黑生效前产生的）。
      ⚠️比对时两边要同一时点：我拉黑完再去验这个恒等式，10 个里 8 个"不成立"——
        因为 skuIdList 是拉黑前的快照、已拉黑数是拉黑后的。换回拉黑前的现状立刻 8/8 成立。

    ⚠️★**有 1 分钟窗口限流**：`您的操作次数已超过上限，请一分钟后再进行操作`。
      实测 workers=5 并发查 51 个计划，**22 个被限流**；改串行 + 0.25s 间隔仍有 12 个撞上。
      ⇒ 批量扫计划时**必须限速**（建议串行 + ≥0.5s，或分批跨分钟跑），
        且**别把限流当成"没权限/没黑名单"**——它和真失败长得不一样，要看报错文案。
    """
    d = jzt_auth.post("/swa/skublack/query",
                      {"campaignId": int(campaign_id), "adGroupId": int(ad_group_id),
                       "requestFrom": 0}, pin=pin) or {}
    black = d.get("blackSkuList") or []
    eff = d.get("effectiveSkuList") or []
    return {
        "campaignId": int(campaign_id), "adGroupId": int(ad_group_id),
        "已拉黑": [{"skuId": x.get("skuId"), "skuName": x.get("skuName")} for x in black],
        "已拉黑数": len(black),
        "在投候选": [{"skuId": x.get("skuId"), "skuName": x.get("skuName")} for x in eff],
        "在投候选数": len(eff),
        "limit": d.get("limit"),
    }


SKU_BLACK_MAX = 50
"""★单元黑名单上限 **50 个**（2026-08-10 二分实测）。

超了平台回 `超出sku黑名单个数上限`——**是提交后才报，不是提交前拦**，所以整批会失败。
本模块在 dry-run 阶段就按 `投后毛利升序` 截断并把放不下的列出来，别等平台报错。
"""


def _black_target(campaign_id, ad_group_id, add=None, remove=None, pin=None):
    """算出**提交用的全集**（现状 ∪ add − remove），并回带现状供 dry-run 展示。"""
    cur = sku_black_query(campaign_id, ad_group_id, pin=pin)
    have = [int(x["skuId"]) for x in cur["已拉黑"]]
    add = [int(x) for x in (add or [])]
    remove = set(int(x) for x in (remove or []))
    target = [s for s in have if s not in remove]
    for s in add:
        if s not in target:
            target.append(s)
    cand = {int(x["skuId"]) for x in cur["在投候选"]}
    # ★上限 50：超了平台**提交后才报错**，整批失败。这里前置截断（保留现状 + 按传入顺序取新增），
    #   把放不下的显式返回，别让调用方以为全提交了。调用方若想优先保住最亏的，自己先排好 add 的顺序。
    overflow = []
    if len(target) > SKU_BLACK_MAX:
        overflow = target[SKU_BLACK_MAX:]
        target = target[:SKU_BLACK_MAX]
    return (cur, have, target,
            [s for s in add if s not in cand and s not in have], overflow)


def sku_black_update_dryrun(campaign_id, ad_group_id, add: list = None,
                            remove: list = None, pin: str = None) -> dict:
    """SKU 黑名单 DRY-RUN：读现状 → 算全集 → 回显将提交的 skuIds + confirm_token，不发送。"""
    cur, have, target, not_in_cand, overflow = _black_target(campaign_id, ad_group_id, add, remove, pin)
    body = {"campaignId": int(campaign_id), "adGroupId": int(ad_group_id),
            "skuIds": target, "requestFrom": 0}
    return {
        "executed": False,
        "现状_已拉黑": have, "将提交_全集": target,
        "新增": [s for s in target if s not in have],
        "移除": [s for s in have if s not in target],
        "⚠️不在该单元在投候选里的": not_in_cand or None,
        "⚠️超上限放不下的": overflow or None,
        "上限": SKU_BLACK_MAX,
        "confirm_token": _G_BLACK.body_token(body),
        "body": body,
        "_note": "update 是**全量覆盖**：提交的 skuIds 就是最终黑名单。本工具已自动并入现状，"
                 "别绕过它直接调接口（只传新增会清空已有）。",
    }


@audited("jzt_swa", "skublack_update")
def sku_black_update(campaign_id, ad_group_id, add: list = None, remove: list = None,
                     confirm: str = "", pin: str = None) -> dict:
    """**SKU 黑名单真执行**（覆盖语义，已自动并入现状）。需先跑 dryrun 拿 confirm_token。

    执行后**自动回读** `query` 比对，落地不符会在返回里标出来——别信回执。
    """
    cur, have, target, _, overflow = _black_target(campaign_id, ad_group_id, add, remove, pin)
    body = {"campaignId": int(campaign_id), "adGroupId": int(ad_group_id),
            "skuIds": target, "requestFrom": 0}
    _G_BLACK.check_body(confirm, body)
    # ★限流闸：/swa/skublack/* 有 1 分钟窗口限流（实测串行 0.6s 仍挂 38%）。
    #   闸放在被调用方——批量放出/拉黑时调用方一循环就会撞上。见 core.policy['swa.skublack']。
    pace("swa.skublack")
    d = jzt_auth.post("/swa/skublack/update", body, pin=pin)
    pace("swa.skublack")                      # 回读也走同一个限流窗口
    back = sku_black_query(campaign_id, ad_group_id, pin=pin)
    landed = sorted(int(x["skuId"]) for x in back["已拉黑"])
    ok = landed == sorted(target)
    return {"executed": True, "提交": sorted(target), "回读": landed,
            "落地一致": ok, "新增": [s for s in target if s not in have],
            "移除": [s for s in have if s not in target],
            "超上限放不下的": overflow or None,
            "raw": d,
            "_warn": None if ok else "★回读与提交不一致，请人工核对该单元黑名单。"}


_BLACK_SCAN_CACHE = {}          # {key: (ts, result)} —— 见下方 cache_s 说明


def sku_black_scan(units: list = None, start_day: str = None, end_day: str = None,
                   min_coverage: float = 0.95, spacing: float = 1.2,
                   retry_rounds: int = 3, retry_wait: float = 65.0,
                   pin: str = None, cache_s: float = 1800.0,
                   refresh: bool = False) -> dict:
    """**全账户扫单元黑名单**，带覆盖率闸 —— 低于阈值**拒绝出结论**。

    ## ★为什么要有这个函数（2026-08-12 教训）
    我用 `workers=1 + 0.6s` 串行查 89 个单元，**34 个被限流失败（覆盖率仅 62%）**，
    然后拿这份残缺结果得出「那 10 款完全不在我账户任何单元」——**结论站不住**，
    它们完全可能就在没查到的 34 个单元里。
    docstring 里记的是「0.25s 仍有 12 个撞上」，实测 **0.6s 挂 34 个，限流比记录的更严**。

    ⇒ 本函数做三件事：**加大间隔**、**跨分钟重试失败单元**、**覆盖率不达标就抛错**。
    「查了但没查全」和「查了没有」是两回事，前者绝不能当后者用。

    返回 {已拉黑, 在投候选, 覆盖率, 失败单元, units}。
    ★`已拉黑` / `在投候选` 是 **{skuId: [(campaignId, adGroupId), ...]}** ——
      值是**列表**不是单个坐标，因为同一 SKU 可能在多个单元里各被拉黑一次；
      放出/拉黑要**遍历它的全部单元**，只动第一个会把其余单元悄悄留着。
    """
    import datetime as _dt
    import time as _t
    if units is None:
        if not end_day:
            end_day = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        if not start_day:
            start_day = (_dt.date.fromisoformat(end_day) - _dt.timedelta(days=6)).isoformat()
        rows = (ad_all(start_day=start_day, end_day=end_day, pin=pin).get("rows") or [])
        units = [(p.get("campaignId"), p.get("groupId")) for p in rows
                 if p.get("campaignId") and p.get("groupId")]
    units = [(c, g) for c, g in units]

    # ★★进程内缓存（2026-08-21）：本函数**故意慢**——94 单元 × spacing 1.2s + 跨分钟重试
    #   实测 **367.83s**，占 `daily_ledger` 单日总耗时的 99%（其余三步合计 2.7s）。
    #   但黑名单是**当前状态**、与 run_date 无关：回填 4 天会把同一份数据扫 4 遍 ≈ 24 分钟白等。
    #   ⇒ 默认 30 分钟内复用。**间隔本身绝不能减**（0.6s 实测 34/89 被限流、覆盖率 62%，
    #     据此得出的「不在任何单元」是错结论，见本函数上方教训）。
    #   ⚠️结果里带 `_cached_age_s`，**别让缓存把实验骗了**（见 stateful-cache-and-untested-path）：
    #     刚做过拉黑/放出写操作，必须 `refresh=True`。
    import time as _tt
    import json as _js
    import hashlib as _hl
    import os as _os
    _key = (str(pin or ""), tuple(sorted((str(c), str(g)) for c, g in units)))
    # ★★缓存必须**落盘**，不能只放进程内（2026-08-21 教训）：
    #   本包的用法是「每次起一个新 Python 进程跑脚本」，模块级 dict 一退就没，
    #   于是 30 分钟缓存形同虚设，同一天反复付 6 分钟冷扫的代价。
    # ★2026-08-24 收敛：键/读/写三件事都用 core.reuse 现成的
    #   （此前同一个函数里账号锁用了 core.reuse，缓存却自己 md5 + 自己拼路径 + 自己判 TTL）。
    from blacklight.core import reuse as _reuse
    _sig = _reuse.cache_key(sorted((str(a), str(b)) for a, b in units), str(pin or ""))
    _fp = _sig                                  # 保留变量名，写盘处统一走 disk_put

    def _from_disk():
        d = _reuse.disk_get("black_scan", _sig, cache_s)
        if d is None:
            return None
        # 领域特有的一步：JSON 没有 tuple，读回来要还原成 (campaignId, adGroupId)
        d["已拉黑"] = {k: [tuple(x) for x in v] for k, v in (d.get("已拉黑") or {}).items()}
        d["在投候选"] = {k: [tuple(x) for x in v] for k, v in (d.get("在投候选") or {}).items()}
        return d

    if not refresh and cache_s > 0:
        hit = _BLACK_SCAN_CACHE.get(_key)
        if hit and (_tt.time() - hit[0]) < cache_s:
            out = dict(hit[1])
            out["_cached_age_s"] = round(_tt.time() - hit[0], 1)
            out["_cache_from"] = "memory"
            return out
        disk = _from_disk()
        if disk is not None:
            _BLACK_SCAN_CACHE[_key] = (_tt.time() - disk["_cached_age_s"], disk)
            return disk

    # ★账号级互斥（2026-08-21）：并行跑两个进程会抢同一份限流额度 ⇒ 双双降级
    #   （实测 94 个单元只过 84 个、覆盖率 89% 被闸拦下）。第二个进程**等**，别一起挂。
    from blacklight.core.reuse import account_lock
    with account_lock("swa.skublack", timeout_s=1800):
        return _scan_units(units, min_coverage, spacing, retry_rounds, retry_wait,
                           pin, _key, cache_s, _fp)


def _scan_units(units, min_coverage, spacing, retry_rounds, retry_wait,
                pin, _key, cache_s, _fp) -> dict:
    """真正的扫描体（被 `sku_black_scan` 在账号锁内调用）。"""
    import io
    import json as _js
    import os as _os
    import time as _tt
    _t = _tt                      # 原函数体用的是 _t，抽出来时别改名（漏了会 NameError）
    black, cand, pending = {}, {}, list(units)
    for rnd in range(retry_rounds):
        if not pending:
            break
        if rnd:
            _t.sleep(retry_wait)                       # ★跨过 1 分钟窗口再重试
        nxt = []
        for cid, gid in pending:
            try:
                q = sku_black_query(cid, gid, pin=pin)
                # ★键必须是 skuId：sku_black_query 返回的是 {skuId, skuName} 字典，
                #   早先这里写 str(s) 把整个字典当键，按 skuId 查永远查不到，
                #   会得出「该 SKU 不在任何单元」——正是本函数要防的那种假结论（2026-08-13 修）。
                # ★值必须是**单元列表**：同一 SKU 可能在多个单元里各被拉黑一次。
                #   早先 black 用覆盖(后来居上)、cand 用 setdefault(先到先得)，两边策略还不一致 ⇒
                #   拿结果去放出/拉黑只会动到其中一个单元，**其余单元悄悄留着**——
                #   在一个「不许把残缺当完整」的函数里，这是同一类错（2026-08-13 代码评审补）。
                for s in (q.get("已拉黑") or []):
                    sid = s.get("skuId")
                    if sid is not None:                    # 缺 skuId 别造出 "None" 这个假键
                        black.setdefault(str(sid), []).append((cid, gid))
                for s in (q.get("在投候选") or []):
                    sid = s.get("skuId")
                    if sid is not None:
                        cand.setdefault(str(sid), []).append((cid, gid))
            except Exception:
                nxt.append((cid, gid))
            pace("swa.skublack_query", spacing)   # 原来是裸 sleep(spacing)
        pending = nxt
    done = len(units) - len(pending)
    cov = done / len(units) if units else 0.0
    if cov < min_coverage:
        raise BlacklightError(
            "单元黑名单只扫到 %d/%d（覆盖率 %.0f%% < %.0f%%），**拒绝出结论**：\n"
            "  『查了但没查全』不能当『查了没有』用——判断某 SKU『不在任何单元』时，"
            "它可能就在没查到的那些单元里。\n"
            "  建议：调大 spacing / 增加 retry_rounds，或分批跨分钟跑。"
            % (done, len(units), cov * 100, min_coverage * 100))
    _ret = {"已拉黑": black, "在投候选": cand, "单元数": len(units),
            "覆盖率": round(cov, 4), "失败单元": pending,
            "_口径": "★`已拉黑`=blackSkuList；`在投候选`=effectiveSkuList(可选进黑名单的池子，**不是黑名单**)。"
                     "恒等式：len(ad_list.skuIdList) + 已拉黑数 == 在投候选数。"
                     "★两者的值都是 **[(campaignId, adGroupId), ...] 列表**（一个 SKU 可跨多单元），"
                     "处置时要遍历全部坐标。",
            "_限流": "实测 spacing=0.6s 会挂 38%%；本函数默认 %.1fs + 跨分钟重试 %d 轮。"
                     % (spacing, retry_rounds)}
    if cache_s > 0:
        _BLACK_SCAN_CACHE[_key] = (_tt.time(), dict(_ret))
        from blacklight.core import reuse as _reuse
        _reuse.disk_put("black_scan", _fp, _ret)   # 落盘跨进程复用；失败静默（同 core 语义）
    return _ret
