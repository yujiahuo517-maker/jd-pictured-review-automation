"""easybi 契约巡检。

比别的域更需要 doctor：数据集里 **67 个指标有十几个是「看板自定义计算字段」**，
`code` 就是中文名、可以被任何人改名/删除/新建重名的。字段一变，脚本不会报错，
只会**静默取到别的列或空值**——所以字段目录本身要当契约来盯。
"""
from __future__ import annotations

from blacklight.easybi import auth, dataset as ds

# 基线：2026-08-07 首测，2026-08-24 复核对齐。任一项对不上 → 数据集被改过，取数前先人工核。
#
# ★★**锚点必须用「已结束的闭区间」**（2026-08-24 更正）：原锚点取的是 `dt in 2026-08-01..08-31`
#   即**当月累计**，而基线值是 08-07 那天记的 —— 月份往前走，这个数只会越来越大，
#   到 08-24 已经差 4 倍（实测 NETGMV 10,834,168 vs 基线 2,637,082）。
#   那不是数据错，是**判据自己会漂**：这种"看着像故障的假警报"比没有检查更糟，
#   它会训练人忽略 doctor。现改用 **2026-07 整月（已结束、不再变动）**，值 2026-08-24 实测。
#   ⇒ 以后换锚点，只准换成**已经结束**的区间。
# ⚠️`dims/metrics` 从 25/67 → 28/74：本次复核确认是**新增了自定义指标**（key_dims/key_metrics
#   全部仍在），属正常增长，故对齐基线；真被删改会在"关键字段齐"那条露头。
BASELINE = {
    "datasetId": ds.DATASET_JX_PL,
    "datasetCnName": "京喜_财务_损益",
    "dims": 28, "metrics": 74,
    "key_dims": ["sku_id", "spu_id", "cate_op_erp", "dt", "saler_dept_id_2",
                 "jdr_jx_sku_jx_sale_mode_type"],
    "key_metrics": ["cfo_cfo_ordpl_pl_netgmv_gs_cw_pl_jx_amt",
                    "cfo_cfo_ordpl_pl_netgmv_gs_cw_pl_jx_qtty",
                    "cfo_cfo_ordpl_pl_cw_pl_jx_gross_profit_gross_cgp_ctr_ac_notax"],
    # 端到端真值锚点：**2026 年 7 月整月**（已结束的闭区间，不会再变）/ 收纳用品组 / new_jdly
    "probe_window": ("2026-07-01", "2026-07-31"),
    "probe": {"NETGMV_商品销售": 14655238.98, "数量_销售": 2264310},
}


def doctor(deep: bool = True) -> dict:
    checks, drift = [], []

    def ck(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            drift.append(name)

    # 1) 握手
    try:
        who = auth.login_info()
        ck("OIDC 握手 + 身份", bool(who.get("Pin")), "pin=%s nick=%s" % (who.get("Pin"), who.get("Nick")))
    except Exception as e:
        ck("OIDC 握手 + 身份", False, str(e)[:160])
        return {"healthy": False, "drift": drift, "checks": checks,
                "note": "握手就失败，后续不用查了 —— 多半是主票过期，重登。"}

    # 2) 数据集元信息 + dimGroupCode
    try:
        info = ds.dataset_info()
        ck("dataSetInfo", info.get("datasetCnName") == BASELINE["datasetCnName"],
           "name=%s dimGroups=%d" % (info.get("datasetCnName"),
                                     len(info.get("dimGroupCodeList") or [])))
        ck("dimGroupCode 可解析", bool(ds.dim_group_code()), ds.dim_group_code()[:16] + "…")
    except Exception as e:
        ck("dataSetInfo", False, str(e)[:160])

    # 3) 字段目录规模（自定义指标被增删会在这里露头）
    try:
        m = ds.list_fields(refresh=True)
        nd, nm = len(m["dims"]), len(m["metrics"])
        ck("字段数量", nd == BASELINE["dims"] and nm == BASELINE["metrics"],
           "维度 %d(基线%d) / 指标 %d(基线%d)" % (nd, BASELINE["dims"], nm, BASELINE["metrics"]))
        codes = {x.get("code") for x in m["dims"] + m["metrics"]}
        miss = [c for c in BASELINE["key_dims"] + BASELINE["key_metrics"] if c not in codes]
        ck("关键字段齐", not miss, "缺失: %s" % miss if miss else "全部命中")
        dup = [x["name"] for x in m["metrics"] if x.get("type") == "custom"]
        ck("自定义指标重名情况", True,
           "custom 指标 %d 个，重名 %d 组（用 find_fields 拿到多条时必须人工选）"
           % (len(dup), len(dup) - len(set(dup))))
    except Exception as e:
        ck("字段目录", False, str(e)[:160])

    # 4) 端到端真值锚点
    if deep:
        try:
            mode = ds._pick("jdr_jx_sku_jx_sale_mode_type", ds.DATASET_JX_PL, "dim")
            dt = ds._pick("dt", ds.DATASET_JX_PL, "dim")
            w0, w1 = BASELINE["probe_window"]
            # ★闭区间自检：锚点窗口必须已经结束，否则这条检查会自己漂（见 BASELINE 注释）
            import datetime as _dt
            if w1 >= _dt.date.today().isoformat():
                ck("锚点窗口是闭区间", False,
                   "锚点窗口 %s~%s 还没结束 ⇒ 这条检查会随时间漂，必须换成已结束的区间" % (w0, w1))
            r = ds.query(dims=[mode],
                         metrics=["cfo_cfo_ordpl_pl_netgmv_gs_cw_pl_jx_amt",
                                  "cfo_cfo_ordpl_pl_netgmv_gs_cw_pl_jx_qtty"],
                         filters=[(mode, "in", ["new_jdly"]),
                                  (dt, ">=", [w0]), (dt, "<=", [w1])],
                         page_size=5)
            row = (r["rows"] or [{}])[0]
            bad = []
            for k, exp in BASELINE["probe"].items():
                try:
                    got = float(str(row.get(k)).replace(",", ""))
                except Exception:
                    bad.append("%s 取不到" % k); continue
                if abs(got - exp) / exp > 0.005:
                    bad.append("%s 实测%.0f vs 基线%d" % (k, got, exp))
            ck("端到端真值锚点(%s~%s 收纳用品组)" % (w0, w1), not bad,
               "; ".join(bad) if bad else "与看板一致")
        except Exception as e:
            ck("端到端真值锚点", False, str(e)[:200])

    healthy = not drift
    return {"healthy": healthy, "drift": drift, "checks": checks,
            "note": ("easybi 契约正常。" if healthy else
                     "有 drift → 数据集/字段可能被改过，取数前人工核对再放手。"),
            "_提醒": "自定义指标(code=中文名)不受平台保护，任何人都能改；"
                     "数字对不上时先查是不是取到了同名的另一个。"}
