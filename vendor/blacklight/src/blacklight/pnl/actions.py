"""从「结论」到「动作」—— 输出**可直接执行**的清单，不是判断。

2026-08-11 补：之前链路只到「谁在亏、为什么亏」就停了，
拿到的是判断不是动作，用户得自己再想一遍该干什么。本模块把最后一段补上。

## 四类动作（**互斥**，一个对象只进一类）
| 类 | 对象 | 依据 | 执行工具 |
|---|---|---|---|
| A 谈退圈 | **券/促销** | 对应订单在亏 + 深券池摘不动 | 人工找发券人 |
| B 摘券 | SKU | 顶档真实回血 > 0 且无 SKU 级禁令 | `yx_markettool_strip_except` |
| C 广告 | SKU | 零成交空耗 / 广告占成交比过高 | `jzt_swa_sku_black_*` |
| D 前瞻 | SKU | 预估亏但**还没出单** | 改价 / 退国补 / 退包邮 |

## 三条纪律（写进每条动作的 `_纪律`）
1. **收益一律是上限**——减免会补位，实测退国补线性预测 1,312、实测只有 539（41%）。
2. **先探针 1~2 条回读实测再批量。**
3. **共补券永不批量摘**（平台担>0，摘掉连平台那份一起丢）。
"""
from __future__ import annotations

from blacklight.ge import margin as ge_margin
from .offline import attribute_rt, feasibility, MAX_SKU_FILTER
from .scan import scan_rt, scan_offline

AD_RATIO_ALERT = 0.30          # 广告占成交比超过此值 ⇒ 广告打穿


def build(mode: str = "rt", *, start: str = None, end: str = None,
          baseline_window: tuple = None, erp: str = None, top: int = 15,
          probe_feasibility: int = 12) -> dict:
    """★生成动作清单。

    mode='rt'      今日实时（止血）
    mode='offline' 指定历史窗口（复盘），需 start/end
    `probe_feasibility`：对前 N 个亏损 SKU 跑 ladder+禁令（每个约 0.5 秒）。
    """
    bw = baseline_window or ("2026-08-04", "2026-08-10")
    if mode == "offline":
        if not (start and end):
            from blacklight.core import BlacklightError
            raise BlacklightError("mode='offline' 需要 start/end")
        disc = scan_offline(start, end, erp=erp, top=top, strict=False)
        losing_rows = disc["有成交亏损"]["Top"]
        losing_ids = disc["有成交亏损"]["全部亏损SKU"]
        ad_waste = disc["广告空耗"]
        window = [start, end]
        forward = None
    else:
        disc = scan_rt(erp=erp, top=top, strict=False)
        losing_rows = [{"sku": r["sku_id"], "投后毛利": r["预估毛利_投后"],
                        "成交金额": r["成交金额"], "单量": r["单量"],
                        "采销实担": r.get("减免_采销实担"),
                        "广告消耗": r.get("广告消耗"),
                        "无减免后单均": r.get("无减免后单均"),
                        "判定": r.get("反事实判定"), "名称": r.get("名称")}
                       for r in disc["网A_已发生"]["Top"]]
        losing_ids = disc["网A_已发生"]["全部亏损SKU"]
        ad_waste = disc["广告空耗"]
        window = disc["窗口"]
        forward = disc["网B_预测"]

    # ---------- A 谈退圈：对应订单在亏的券/促销 ----------
    A = []
    for deg, lab in (("coupon", "券"), ("promotion", "促销")):
        try:
            r = attribute_rt(losing_ids[:MAX_SKU_FILTER], bw, degree=deg, top=top)
        except Exception as e:
            A.append({"_错误": "%s 维度归因失败：%s" % (lab, str(e)[:80])})
            continue
        for x in r["★亏损载体"]["rows"]:
            A.append({
                "类型": "A 谈退圈", "维度": lab, "ID": x["key"], "名称": x["名称"],
                "今日采销担": x["今日累计_采销担"],
                "对应订单投后毛利": x["对应订单投后毛利"],
                "无减免后单均": x["无减免后单均"],
                "流速": x["状态"], "倍数": x["倍数"], "性质": x["性质"],
                "动作": ("找发券人谈**退圈/缩圈品范围**"
                         if x["性质"] == "自担" else "★共补券——**不可批量摘**，只能谈退圈"),
                "证据": "对应订单投后毛利 %.2f（在亏）；无减免后单均 %s（%s）"
                        % (x["对应订单投后毛利"] or 0, x["无减免后单均"],
                           "商品本身赚钱" if (x["无减免后单均"] or 0) > 0 else "商品本身也不赚"),
            })
    A.sort(key=lambda z: (z.get("对应订单投后毛利") or 0))

    # ---------- B 摘券：SKU 级、有真实回血且无禁令 ----------
    fe = feasibility(losing_ids[:probe_feasibility]) if losing_ids else {"rows": []}
    B = []
    for x in fe["rows"]:
        if x.get("可动手"):
            B.append({
                "类型": "B 摘券", "SKU": x["skuId"],
                "券总数": x.get("券总数"), "可摘张数": x.get("可摘张数"),
                "顶档真实回血": x.get("顶档真实回血"),
                "档位级禁令": x.get("档位级禁令") or [],
                "动作": "yx_markettool_strip_except_plan → _dryrun → 执行",
                "预期回血上限": x.get("顶档真实回血"),
                "证据": x.get("_可动手说明"),
            })
    B.sort(key=lambda z: -(z["顶档真实回血"] or 0))
    blocked = [x for x in fe["rows"] if not x.get("可动手")]

    # ---------- C 广告：空耗 + 打穿 ----------
    C = []
    for r in losing_rows:
        amt = float(r.get("成交金额") or 0)
        ad = float(r.get("广告消耗") or 0)
        if amt > 0 and ad > 0 and ad / amt >= AD_RATIO_ALERT:
            C.append({
                "类型": "C 广告-打穿", "SKU": r.get("sku") or r.get("sku_id"),
                "成交金额": round(amt, 2), "广告消耗": round(ad, 2),
                "广告占成交%": round(100 * ad / amt, 1),
                "投后毛利": r["投后毛利"],
                "动作": "jzt_swa_sku_black_dryrun/_update 拉黑，或降该计划目标 ROI"
                        "（★先过薄利护栏：达成/保本 ≤1.3 禁止降）",
                "证据": "广告占成交 %.1f%%，改价摘券治不了" % (100 * ad / amt),
            })
    C.append({
        "类型": "C 广告-空耗", "SKU": "（批量）",
        "款数": ad_waste["款数"], "合计": ad_waste["合计"],
        "动作": "jzt 侧核这批的投放效率；零成交却在花广告费",
        "证据": "亏损 100% 是广告费（商品成本/物流/券全为 0）",
    })
    C.sort(key=lambda z: -(z.get("广告占成交%") or 0))

    # ---------- D 前瞻：还没出单的 ----------
    D = []
    if forward:
        for x in forward["Top未出单"][:top]:
            root = x.get("根因") or ""
            act = ("退国补（★先 yx_subsidy_check_skus 确认能不能退——"
                   "实测 57 款里 42 款非本账号报名、退不了）" if "国补" in root else
                   "退包邮/退促" if "促销" in root or "包邮" in root else
                   "摘券" if "券" in root else "改价 / 换供 / 调物流模板")
            D.append({
                "类型": "D 前瞻", "SKU": x["skuId"], "预估毛利": x["预估毛利"],
                "近15日单量": x["近15日单量"], "根因": root,
                "动作": act,
                "证据": "**今日还没出单** ⇒ 现在动手一分钱没花出去，成本最低",
            })
        D.sort(key=lambda z: (z["预估毛利"] or 0))

    return {
        "窗口": window, "口径": disc.get("口径"),
        "计数": {"A谈退圈": len([a for a in A if "_错误" not in a]),
                 "B摘券": len(B), "C广告": len(C), "D前瞻": len(D)},
        "A_谈退圈": A[:top], "B_摘券": B, "C_广告": C[:top], "D_前瞻": D[:top],
        "B_不可动手": [{"SKU": x["skuId"], "原因": x.get("_可动手说明")} for x in blocked],
        "_纪律": [
            "★收益一律是**上限**：减免会补位（实测退国补线性预测 1,312、实测 539，41%）",
            "★先探针 1~2 条回读实测再批量；每批写完必回读",
            "★共补券（性质=共补）**永不批量摘**——摘掉连平台那份一起丢",
            "★A 类是**人工动作**（找人谈），B/C/D 才有工具可执行",
            "★动手前查百补重叠；写操作一律走 dry-run + confirm_token",
        ],
    }
