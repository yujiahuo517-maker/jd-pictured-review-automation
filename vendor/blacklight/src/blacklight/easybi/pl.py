"""京喜损益（P&L）领域层：把口径、考核公式、数据坑固化成代码，别再凭记忆套公式。

口径出处：京喜损益培训（26年1月版 joyspace MvBvGMZl5kcFW6o4aWZl）+ `jx-pnl-analysis` skill，
字段名与数值均在 easybi 数据集 `1096116` 上**逐条实测验证**（2026-08-07）。

## 链路（自营）
    Net GMV → Net GMV还原 → 可控综合毛利 → 履约毛利 → 广告后履约毛利

实测锚点（收纳用品组 2026-07 京喜自营，用于换人复核）：
    Net GMV            14,655,239
      + |非可控商品补贴|  2,044,855
      + |非可控优惠券|    1,387,635
    = Net GMV还原       18,087,729   （比 Net GMV 大 23.4%）
    可控综合毛利         6,062,504
      + 费用_变动       −4,720,752   （其中配送 −4,478,634，占 95%）
    = 履约毛利           1,341,752   率 7.42%
      − 京准通             515,691
    = 广告后履约毛利       826,061    率 4.57%

## ★三条最容易算错的
1. **率值分母是 Net GMV还原，不是 Net GMV**（自营）。用 Net GMV 实测高估 **1.74pp**
   （履约毛利率 7.42% → 9.16%）。C店率值分母才用 Net GMV（非还原）。
2. **符号约定**：两个「非可控」收入项在数据集里是**负值**，算还原 GMV 要取绝对值再加；
   `费用_变动` / `京准通` 也是负值，所以链路上是**相加**不是相减。
   （我第一次直接相加非可控项，把还原 GMV 算小了 40%。）
3. **「投后履约毛利」=「广告后履约毛利」**，25年7月叫前者、**26年1月起改叫后者，公式相同**。
   这解释了 easybi 里大量同名/近名自定义指标（通用集 439 指标里 26 组重名），不是口径分歧。

## ★★选源铁律：实时止亏只能用 osw，京算盘 T-2~T-3 必然滞后
2026-08-10 实测三档时效（同日查最新可用数据）：
    osw 毛利监控              **实时**        —— 成交预估口径
    easybi 通用集 1025136     **T-1**（08-09）—— 成交口径
    easybi 损益集 1096116     **T-2~T-3**（08-08）—— 出库+计费口径（京算盘）
⇒ **当天要动手止亏（摘券/退国补/改价/退报名）只能用 osw**，
  拿京算盘损益做当天判断必然滞后 2~3 天。见 `pick_source()`。
⇒ 反过来，**财务复盘/绩效以京算盘月度损益为准**（by天无万人团回算、无京准通月度分摊）。

## ★整体考核口径两侧不对称，别统一
    整体广告后履约毛利率 = (自营广告后履约毛利 + C店履约毛利) / (自营NetGMV还原 + C店NetGMV)
  分子：自营用「广告后」、C店用「履约」（**C店无采销京准通**）；
  分母：自营用「还原」、C店用 **Net GMV**。
"""
from __future__ import annotations

from blacklight.core import BlacklightError
from blacklight.easybi import dataset as ds

# --------------------------------------------------------------------------- #
# 字段映射（2026-08-07 在 1096116 上逐条实测）
# --------------------------------------------------------------------------- #
F_NETGMV = "NETGMV_商品销售"
F_QTY = "数量_销售"
F_CG_CTR = "毛利_综合毛利_可控_权责_不含税"          # 可控综合毛利
F_CG_ACTUAL = "毛利_综合毛利_可控_实收_不含税"        # 可控综合毛利(实收)
F_LY = "贡献利润_边际_权责"                          # ★京喜统一叫「履约毛利」
F_VAR = "费用_变动"
F_VAR_DELIV = "费用_变动_配送"
F_VAR_STOR = "费用_变动_仓储"
F_VAR_AS = "费用_变动_售后"
F_VAR_CS = "费用_变动_客服_客诉"
F_VAR_ORD = "费用_变动_订单交易"
F_VAR_CPS = "费用_变动_市场_渠道_CPS"
F_AD = "费用_固定_市场_广告_京准通"                   # 不带后缀那个
F_SUB_UNCTR = "收入_商品补贴_非可控_不含税"
F_CPN_UNCTR = "收入_商品销售_自营券豆_优惠券_非可控_不含税"
F_GROSS_FRONT = "毛利_商品销售_前台_不含税"           # 前台买卖价差（亏损归因用）
F_NET_PROFIT = "净利润_权责"

D_MODE = "jdr_jx_sku_jx_sale_mode_type"
MODE_SELF = "new_jdly"                                # 京喜自营
TAX = 1.06                                            # 损益是税后：税后 = 税前 / 1.06


def _n(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
def pl_chain(start: str, end: str, by: str = None, top: int = 50,
             mode: str = MODE_SELF, dept: str = None,
             dataset_id: int = ds.DATASET_JX_PL) -> dict:
    """**损益链路拆解**：Net GMV → 还原 → 可控综合毛利 → 履约毛利 → 广告后履约毛利。

    by: 下钻维度（'cate_2'/'cate_op_erp'/'dt'/'sku_id'…），不传=只出总计。
    返回每行都带**按正确分母算好的率值**，不用自己再算（算错的正是分母）。
    ⚠️`mode=None`（含 C店）时率值分母的口径不同，本函数会在返回里标注，别混着看。
    """
    dt = ds._pick("dt", dataset_id, "dim")
    md = ds._pick(D_MODE, dataset_id, "dim")
    dims = [ds._pick(by, dataset_id, "dim")] if by else [md]
    mets = [F_NETGMV, F_QTY, F_CG_CTR, F_LY, F_VAR, F_AD, F_SUB_UNCTR, F_CPN_UNCTR]
    flt = [(dt, ">=", [start]), (dt, "<=", [end])]
    if mode:
        flt.insert(0, (md, "in", [mode]))

    r = ds.query(dims=dims, metrics=mets, filters=flt, dataset_id=dataset_id,
                 dept=dept, page_size=max(top, 50), with_total=True)
    rows = []
    for x in r["rows"]:
        rows.append(_chain_row(x, by))
    tot = _chain_row(r.get("合计") or {}, None) if r.get("合计") else None
    rows.sort(key=lambda z: z["广告后履约毛利"])
    return {"区间": "%s ~ %s" % (start, end), "口径": "京喜自营" if mode == MODE_SELF else (mode or "全部(含C店)"),
            "下钻": by or "(总计)", "服务端total": r.get("total"),
            "rows": rows[:top], "合计": tot,
            "_分母": ("自营：率值分母 = Net GMV还原（不是 Net GMV，用后者实测高估 1.74pp）"
                     if mode == MODE_SELF else
                     "⚠️含 C店：C店率值分母用 Net GMV(非还原)，与自营不可混算，见 overall_rate()"),
            "_提醒": "损益是**税后**（税后=税前/1.06）；by天有错期，见 caveats()"}


def _chain_row(x: dict, by: str) -> dict:
    net = _n(x.get(F_NETGMV))
    sub = abs(_n(x.get(F_SUB_UNCTR)))          # ★非可控项在数据集里是负值，取绝对值再加
    cpn = abs(_n(x.get(F_CPN_UNCTR)))
    restored = net + sub + cpn                 # 仅自营；红包/京豆不加回
    cg = _n(x.get(F_CG_CTR))
    var = _n(x.get(F_VAR))                     # 负值
    ly = _n(x.get(F_LY))
    ad = _n(x.get(F_AD))                       # 负值
    after = ly + ad                            # 京准通是负数，所以是相加
    # ⚠️维度中文名在原始行里是 `<dimCode>$value`（`名称` 是 pnl._norm 才加的键，
    #   本模块直接用 ds.query 所以没有）——按后缀找，取不到再退回维度码。
    label = "合计"
    if by:
        label = next((v for k, v in x.items() if str(k).endswith("$value")), None) \
            or x.get(by) or "?"
    out = {"维度": label,
           "NetGMV": round(net, 2), "非可控补贴": round(sub, 2), "非可控券": round(cpn, 2),
           "NetGMV还原": round(restored, 2),
           "可控综合毛利": round(cg, 2), "费用_变动": round(var, 2),
           "履约毛利": round(ly, 2), "京准通": round(ad, 2),
           "广告后履约毛利": round(after, 2),
           "履约毛利率%": round(ly / restored * 100, 2) if restored else None,
           "广告后履约毛利率%": round(after / restored * 100, 2) if restored else None}
    if restored and net:
        out["★若误用NetGMV当分母会高估"] = round(ly / net * 100 - ly / restored * 100, 2)
    return out


def overall_rate(self_after: float, self_restored: float,
                 c_ly: float, c_netgmv: float) -> dict:
    """**整体广告后履约毛利率考核口径**（两侧不对称，别统一）。

        = (自营广告后履约毛利 + C店履约毛利) / (自营NetGMV还原 + C店NetGMV)

    分子：自营用「广告后」、C店用「履约」（**C店无采销京准通，广告后=履约**）；
    分母：自营用「还原」、C店用 **Net GMV**（非还原）。
    """
    num = self_after + c_ly
    den = self_restored + c_netgmv
    if den <= 0:
        raise BlacklightError("分母为 0/负：自营还原 %.2f + C店NetGMV %.2f" % (self_restored, c_netgmv))
    return {"整体广告后履约毛利率%": round(num / den * 100, 2),
            "分子": round(num, 2), "分母": round(den, 2),
            "自营广告后履约毛利": round(self_after, 2), "自营NetGMV还原": round(self_restored, 2),
            "C店履约毛利": round(c_ly, 2), "C店NetGMV": round(c_netgmv, 2),
            "_口径": "分子自营用广告后/C店用履约；分母自营用还原/C店用NetGMV —— 两侧不对称是**对的**"}


# --------------------------------------------------------------------------- #
# 亏损 SKU 七类归因（A~G）
# --------------------------------------------------------------------------- #
CAUSES = {
    "A": ("卖破成本", "提价 / 降采购价"),
    "B": ("采销可控让利过度", "收紧自设券/红包"),
    "C": ("补贴不足·毛利空", "争取补贴 / 提价"),
    "D": ("配送·履约", "提客单(组合装) / 降抛重"),
    "E": ("退货·售后", "治理退货"),
    "F": ("广告ROI", "下调 / 关停京准通"),
    "G": ("其他变动费用", "核查仓储 / CPS"),
}


def attribute_loss(qg: float, cg: float, ly: float, after: float,
                   unit_netgmv: float = None, purchase: float = None,
                   ctr_redpack: float = 0.0, ctr_coupon: float = 0.0,
                   deliv: float = 0.0, afs_cs: float = 0.0) -> dict:
    """对**投后亏损**的单个 SKU 定主因（两阶段，顺序不可换）。

    qg=前台毛利(买卖价差) / cg=可控综合毛利 / ly=履约毛利 / after=广告后履约毛利。

    ①**毛利端就亏**（cg≤0）：
        A 卖破成本      —— qg<0 且 单均NetGMV < 采购价
        B 采销可控让利过度 —— qg<0 且 (可控红包+可控券) > 前台亏损的一半
        C 补贴不足·毛利空 —— qg≥0 但 cg≤0
    ②**含补贴毛利为正**（cg>0）被费用吃：
        ⚠️**必须先判 ly>0**：ly>0 说明履约层面还是赚的，是**广告**把它拖负 ⇒ F。
          不先判这一步，会把「履约正、仅广告致亏」误判成配送(D)。
        F 广告ROI    —— ly > 0
        D 配送·履约  —— ly≤0 且配送占最大
        E 退货·售后  —— ly≤0 且售后+客诉为主
        G 其他变动    —— ly≤0 其他

    ⭐**补贴依赖标签**（cg>0 但 qg<0）：商品前台已破成本、全靠平台非可控补贴顶住、
      再被费用吃亏 —— **补贴一退坡立即崩**。这是三分法看不到的隐藏问题，独立于主因单独打标。
    ⚠️A 类判定用「单均 NetGMV（真实到手）」比采购价，**别用名义到手价**（券/补贴让利会打穿）。
    """
    tags = []
    if cg > 0 and qg < 0:
        tags.append("补贴依赖")

    if cg <= 0:
        if qg < 0 and unit_netgmv is not None and purchase is not None and unit_netgmv < purchase:
            c = "A"
        elif qg < 0 and (ctr_redpack + ctr_coupon) > abs(qg) / 2:
            c = "B"
        elif qg >= 0:
            c = "C"
        else:
            c = "A" if qg < 0 else "C"
        stage = "①毛利端就亏"
    else:
        stage = "②含补贴毛利为正、被费用吃"
        if ly > 0:                       # ★必须先判这条
            c = "F"
        elif abs(deliv) >= abs(afs_cs) and abs(deliv) > 0:
            c = "D"
        elif abs(afs_cs) > 0:
            c = "E"
        else:
            c = "G"
    name, fix = CAUSES[c]
    return {"主因": "%s %s" % (c, name), "阶段": stage, "治理方向": fix,
            "标签": tags,
            "_判据": {"qg": qg, "cg": cg, "ly": ly, "广告后": after},
            "_注意": ("②阶段先判 ly>0 —— 履约正、仅广告致亏的不能算配送问题"
                      if cg > 0 else
                      "A 类要用单均 NetGMV(真实到手) 比采购价，别用名义到手价")}


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 选源：问什么问题 → 用哪个数据源（2026-08-10 实测时效）
# --------------------------------------------------------------------------- #
SOURCES = {
    "osw 毛利监控": {
        "时效": "**实时**",
        "口径": "成交预估",
        "适合": ["当天/实时毛利监控", "止亏决策(摘券/退国补/改价)前后的回读实测",
                 "报名定价的成本底料(全成本=采购+物流+CPS+广告预估)"],
        "不适合": ["财务口径复盘", "绩效考核", "广告投后效率判定(内含的 advCost 只是预估)"],
    },
    "easybi 通用集 1025136": {
        "时效": "T-1（2026-08-10 实测最新 08-09）",
        "口径": "成交",
        "适合": ["流量/搜索漏斗", "评价好评率", "履约考核", "售后品退率",
                 "商品内容质量标识", "**广告投后毛利(7折口径)**"],
        "不适合": ["财务绩效口径"],
    },
    "easybi 损益集 1096116（京算盘）": {
        "时效": "**T-2~T-3**（2026-08-10 实测最新 08-08 = T-2）",
        "口径": "**出库 + 计费**",
        "适合": ["财务口径损益复盘", "绩效考核", "费用结构拆解", "月度损益"],
        "不适合": ["**当天止亏判断**（滞后 2~3 天）", "与成交口径的数相加减"],
    },
}


def pick_source(question: str = "") -> dict:
    """**该用哪个数据源**——按问题类型给判定，别按手边有什么数据凑。

    ★铁律：**实时毛利监控 / 当天止亏 → osw 毛利监控**（京算盘 T-2~T-3，用它必然滞后）。
    ★铁律：成交口径与出库计费口径**不可相加减互校**（同 SKU 同月毛利可以一正一负）。
    """
    q = question or ""
    # 「毛利监控」是 osw 那个实时源的**本名**，必须命中——2026-08-10 漏了它，
    # 问「检查一下目前的毛利监控」时反而判不出来。
    hot = ["实时", "今天", "当天", "现在", "止亏", "马上", "立刻",
           "毛利监控", "监控", "盯", "巡检", "看一下毛利", "目前"]
    fin = ["财务", "绩效", "考核", "损益", "月度", "费用结构"]
    ad = ["广告", "投后", "京准通", "ROI", "投放"]
    if any(k in q for k in hot):
        pick = "osw 毛利监控"
        why = "问的是实时/当天/止亏 —— 京算盘 T-2~T-3 会滞后，必须用实时源"
    elif any(k in q for k in ad):
        pick = "easybi 通用集 1025136"
        why = "广告投后盈亏用 7 折口径（id=94272 预估投后履约毛利），由实际消耗算出"
    elif any(k in q for k in fin):
        pick = "easybi 损益集 1096116（京算盘）"
        why = "财务/绩效口径以京算盘月度损益为准"
    else:
        pick = None
        why = "问题里没有明确信号，请先明确：要实时决策 / 广告效率 / 还是财务复盘"
    return {"问题": question, "建议数据源": pick, "理由": why,
            "全部数据源": SOURCES,
            "★不可比": "成交口径(osw/京准通/通用集) 与 出库计费口径(京算盘损益) 不可相加减互校"}


def caveats(by_day: bool = False) -> dict:
    """损益数据坑清单。**做 by天/单日结论前必读**。"""
    d = {
        "税后": "损益是税后，税后 = 税前 / 1.06（所以边际利润比手算小）",
        "错期": ("商品补贴记**出库**日、配送费记**妥投**日、市场费记**订单完成**日、"
                 "京准通按出库数量**均摊记当月 1 号**"),
        "时效": ("京算盘损益 **T-2~T-3**（2026-08-10 实测最新 08-08）、easybi 通用集 **T-1**、"
                 "黄金眼 by天看板 T+3；**月初 5 号前数据不稳**"),
        "★实时止亏只能用 osw": ("京算盘损益滞后 2~3 天 ⇒ **当天要动手止亏（摘券/退国补/改价/退报名）"
                               "必须用 osw 毛利监控**（实时·成交预估口径），别拿京算盘当依据。"
                               "反过来财务复盘/绩效以京算盘月度损益为准。见 pick_source()"),
        "by天vs月度": ("by天无万人团补贴回算、无物流小哥错录修正、京准通月度分摊体现不了 "
                       "⇒ **绩效以月度损益为准**"),
        "物流9折取消": "26-01-01 起物流费不再打 9 折，物流成本上升；跨期比单均/毛利率注意此断点",
        "口径不可比": ("**成交预估（osw毛利监控/京准通/通用集1025136）** 与 "
                       "**出库+计费（京算盘损益1096116）** 两组数据不可相加/相减/互校 —— "
                       "实测同 SKU 同月金额差 6.8% 但毛利一正一负"),
        "广告消耗折前折后": ("广告消耗有折前/折后(**×0.7**)两套；京准通 xlsx 那列名叫「折后消耗」"
                             "实际填的是**折前** ⇒ 用它算投后毛利高估广告成本 43%、高估亏损"),
    }
    if by_day:
        d["⚠️你在看 by天"] = ("因**错期**，单日数字会失真：配送费看 **T-4** 前，"
                              "京准通全月均摊在 1 号 ⇒ **「某天开始转负」很可能是错期不是经营拐点**。"
                              "要判趋势请拉长看单均、或改用月度。")
    return d
