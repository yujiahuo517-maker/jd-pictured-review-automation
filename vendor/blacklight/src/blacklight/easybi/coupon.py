# -*- coding: utf-8 -*-
"""券促**实际成交**归因（黄金眼）—— 补上 osw 实时毛利监控的盲区。

## 为什么需要它

osw 毛利监控给的是「**当前**券促配置下的**预估最低**毛利」，它有两个已知偏差：

1. **系统性偏悲观**：假设每单都吃到最大券，实际多数订单吃不到；
2. ★**看不见后续新拉入的券促**——预估算完之后商品被圈进新的券/活动，预估就失效了。

第 2 条是 2026-08-10 用户提出的，本模块用数据坐实了：**13 款超线禁令款 15 日券我担
14,400 元，其中 31%(4,407 元) 来自 osw 当前配置里根本不存在的批次**（单均我担 5.00 那
31 个批次，osw 侧一张都看不到）。所以「预估最低毛利」和「实际单均毛利」对不上是**必然**的，
不是数据错。**判亏损用实际，找元凶用本模块。**

## 数据源与契约

| 数据集 | id | 关键字段 | 时效 |
|---|---|---|---|
| 黄金眼优惠券数据 | 1043053 | `batch_id` × `sku_id` × **`优惠券成本-采销承担`** / `-平台承担` / `使用优惠券成交子单量` | **T-1** |
| 黄金眼促销数据集 | 1045254 | `equity_act_id`/`equity_act_name` × `sku_id` × `用促优惠金额` / `使用促销成交子单量` | **T-1** |
| 黄金眼红包数据 | 1045257 | —— | **无权限**（CODE 20002，其 dimGroupCodeList 里没有采销岗那组，是真实边界） |

T-1 比京算盘损益(T-2~T-3)快，**可以进日常巡检**；但仍慢于 osw 实时。

⚠️**这两个集曾被我误判为「无权限」**（连吃 CODE 3003）。真因是 `query()` 的两个契约默认值，
  与数据集本身的权限无关，已在 `dataset.py` 修掉（`dataset_type` + `DIM_GROUP_SALER`）。
  教训：**3003 的文案只说"行级数据权限不足"，完全不指向 dimGroupCode/type**，别照字面下结论。

## 承担方分档（决定该不该摘）

`优惠券成本-平台承担` 是本模块最有价值的字段，但**它只是必要条件不是充分条件**：

- **平台担 > 0 → 共补券**：摘掉等于把平台那份一起扔了，**永不批量摘**
  （见 memory `gongbu-coupon-never-strip`）。实测 13 款里共补券占我担成本 59%，
  平台另担 3,310 元——按面额排序会把它排在最前面，然后摘错。
- **平台担 = 0 → 只能说明「平台没分担券成本」，⚠️不等于「摘了没有副作用」。**

### ★★平台担=0 但仍不能摘的反例：B补券（2026-08-10 用户指出）

`2026年京东BOSS微信域B补C1拉通券` 这类**券成本 100% 采销自担**、`优惠券成本-平台承担` 为 0，
但**平台另给团长每单 0.5 元 B 补帮我们推广**——那笔钱走的是**另一本账**，
在券成本表里根本不出现。摘掉券 = 同时砍掉推广渠道。

⇒ 本模块的 `kind`（自担/共补）**只是券成本口径的分类，不是"能不能摘"的结论**。
  判能不能摘**必须再过 `core.protected.filter_plan(action="strip")`**
  （已加规则 `bbu-tuanzhang-promo`，按券名子串 `B补` 匹配，能自动覆盖后续新进的 SKU）。

**教训**：账面上"平台出了多少钱"和"平台在这条链路上出了多少钱"是两个问题。
前者查得到，后者查不到——所以**分档给的是线索，拍板要靠禁令清单**。

## 两个 ERP 字段是两回事（2026-08-10 用户口述，实测吻合）

| 字段 | code | 含义 |
|---|---|---|
| 运营人员 / 商品运营人员 | `cate_op_erp` | **商品的采销 ERP 归属**——"这个货是谁的" |
| 员工erp | `jd_erp`（仅券集） | **录券/录促的操作人**——"这张券是谁开的" |
| 协助运营ERP | `jdr_sch_jd__erp_sku_auth` | 辅助归属，**大量为空**，按它分组会漏掉 99% 的钱，别用来做汇总 |

有了 `cate_op_erp` 就**不必先备好 SKU 清单**，可以直接按归属拉全量。
`jd_erp` 的价值是把成本归到**开券人**头上：实证 13 款禁令款的 6 个录券人里，
`chenlisha10` 那 7 个批次正好就是全部共补券（我担 8,550 / 平台另担 3,295），
其余 4 人 5,851 元全是纯自担——**承担方分档和录券人分档是同一刀**，一眼看出该找谁谈。

⚠️促销集的 `jdr_sch_jd__erp_incumbent`（在职员工ERP）**查不了**（CODE 50001），
  券集的 `spu_id` 和两个渠道维度同样 50001。**录促人拿不到，只有录券人能拿到。**

## ★★归属过滤会被静默丢掉（促销集，2026-08-10 实测）

**`cate_op_erp` 过滤只在「商品侧维度」分组时生效**。按**活动侧维度**分组
（`equity_act_id`/`equity_act_name`/`促销大类型`/`促销子类型`/活动起止时间/`hr_dept_id_*`）
时过滤被**静默丢弃**，返回的是**全库总额**——本人 32.4 万会变成 277.5 万（+755%），
而且返回结构完全正常、不报错。券集没有这个毛病（除 `jdr_sch_jd__erp_sku_auth` 因空值漏数）。

所以：**要按活动拆，就得用 `sku_id` 清单过滤**（实测 sku 清单在任何分组下都准）。
本模块的 `owner_erp` 参数带**自动校验闸** `_assert_owner_filter_applied()`：
拿一次「按归属分组」的控制组比对总额，对不上直接抛错，绝不静默返回全库数字。

## 批次号 ≠ osw 的 couponId

两套 ID 空间，**不能直接 join**。实用映射法：按**单均我担金额**对（黄金眼 `我担/子单量`
对 osw `jxReward`）。实证：批次 1315454844 单均我担 3.00 + 平台占比 25%
↔ osw `Q3_全品类低活拉新4.01-4元_东大`（reward 4.0 / jxReward 3.0 / jxRatio 75），完全吻合。
"""
import collections

from blacklight.core import BlacklightError

from . import dataset as ds

DATASET_COUPON = 1043053
DATASET_PROMO = 1045254

M_CPN_SELF = "优惠券成本-采销承担"
M_CPN_PLAT = "优惠券成本-平台承担"
M_CPN_QTY = "使用优惠券成交子单量"
M_CPN_GMV = "优惠券成交金额"
M_PRM_CUT = "用促优惠金额"
M_PRM_QTY = "使用促销成交子单量"
M_PRM_GMV = "使用促销成交金额"

D_OWNER = "cate_op_erp"      # 商品的采销 ERP 归属（"这个货是谁的"）
D_CREATOR = "jd_erp"         # 录券人（"这张券是谁开的"）；促销集没有可用等价字段

# ★按这些维度分组时，`cate_op_erp` 归属过滤会被**静默丢弃**（返回全库数字，不报错）。
#   实测 1045254：本人 324,471 → 2,774,869（+755%）。券集暂未发现同类维度。
_OWNER_FILTER_UNSAFE_DIMS = {
    "equity_act_id", "equity_act_name", "pcap_begin_time", "pacp_end_time",
    "jdr_sch_promotion__act_promot_type_1", "jdr_sch_promotion__act_promot_type_2",
    "hr_dept_id_1", "hr_dept_id_2", "hr_dept_id_3", "hr_dept_id_4",
}


def _metric(dataset_id, name):
    meta = ds.list_fields(dataset_id)
    f = [x for x in meta["metrics"] if x["name"] == name]
    if not f:
        raise BlacklightError("数据集 %s 没有指标 %s" % (dataset_id, name))
    return f[0]


def _assert_owner_filter_applied(dataset_id, owner_erp, start, end, metric, got_total):
    """归属过滤的**校验闸**：拿「按归属分组」的控制组比对总额。

    这是唯一可信的判据——按归属分组时过滤一定生效（该维度就在 group by 里），
    所以它的值是真值。对不上就说明本次分组维度触发了静默丢弃，**直接抛错**。
    """
    own = ds._pick(D_OWNER, dataset_id, "dim")
    dt = ds._pick("dt", dataset_id, "dim")
    r = ds.query(dims=[own], metrics=[metric],
                 filters=[(own, "in", [owner_erp]), (dt, ">=", [start]), (dt, "<=", [end])],
                 dataset_id=dataset_id, page_size=50)
    truth = sum(float(x.get(metric["name"]) or 0) for x in r["rows"])
    if truth <= 0:
        return truth
    if got_total > truth * 1.02:
        raise BlacklightError(
            "归属过滤被静默丢弃：本次取到 %.0f，按归属分组的真值只有 %.0f（多了 %.0f%%）。"
            "本数据集在当前分组维度下不支持 owner_erp 过滤——改用 skus 清单过滤。"
            % (got_total, truth, 100 * (got_total - truth) / truth))
    return truth


def _rows(dataset_id, dims, metric_names, skus, start, end, page_size=1000,
          owner_erp: str = None, include_sku: bool = True):
    """取数底座。`skus` 与 `owner_erp` 二选一（都给则以 skus 为准，更精确）。

    ⚠️`include_sku=False` 用于只要汇总的场景。**别无脑带上 sku 维度**——
      多一个高基数维度就多几万行，撞上单页上限会**静默截断**（少算而不是报错）。
      实证：按归属看录券人，带 sku 分组只取回 156,510，真值 167,614（少 7%）。
    """
    if not skus and not owner_erp:
        raise BlacklightError("skus 和 owner_erp 至少给一个，否则会拉全库")
    dt = ds._pick("dt", dataset_id, "dim")
    dim_objs = [ds._pick(d, dataset_id, "dim") for d in dims]
    if include_sku:
        dim_objs.insert(0, ds._pick("sku_id", dataset_id, "dim"))
    sku = ds._pick("sku_id", dataset_id, "dim")
    mets = [_metric(dataset_id, n) for n in metric_names]

    # ⚠️过滤值一律是**列表**，日期也不例外。传裸字符串会得到
    #   `code=99999 查询失败` 这种完全不指向参数形状的报错。
    flt = [(dt, ">=", [start]), (dt, "<=", [end])]
    if skus:
        flt.insert(0, (sku, "in", [str(s) for s in skus]))
    else:
        unsafe = _OWNER_FILTER_UNSAFE_DIMS.intersection(dims)
        if unsafe:
            raise BlacklightError(
                "维度 %s 下归属过滤会被静默丢弃（返回全库数字且不报错）。"
                "按活动侧维度拆时请改用 skus 清单过滤。" % sorted(unsafe))
        flt.insert(0, (ds._pick(D_OWNER, dataset_id, "dim"), "in", [owner_erp]))

    r = ds.query(dims=dim_objs, metrics=mets, filters=flt,
                 dataset_id=dataset_id, page_size=page_size,
                 order_by=mets[0], desc=True)
    rows = r["rows"]
    # ★截断是**静默少算**，服务端不报错。总行数超过本页就说明拿到的是半截。
    total = r.get("total")
    if total is not None and len(rows) < int(total or 0):
        raise BlacklightError(
            "取回 %d 行但 total=%s —— 被分页截断了（会静默少算）。"
            "缩小 SKU 范围/日期区间，或去掉高基数维度（include_sku=False）。"
            % (len(rows), total))
    if owner_erp and not skus:
        got = sum(float(x.get(mets[0]["name"]) or 0) for x in rows)
        _assert_owner_filter_applied(dataset_id, owner_erp, start, end, mets[0], got)
    return rows, total


def cost_by_creator(start: str, end: str, owner_erp: str = None,
                    skus: list = None) -> dict:
    """★**这些券是谁开的** —— 按 `jd_erp`(录券人) 汇总我担/平台担。

    实证：承担方分档和录券人分档**基本是同一刀**——共补券集中在个别开券人身上。
    找谁谈判/申请退出，看这张表比看批次号有用。

    ⚠️仅**券集**可用：促销集的 `jdr_sch_jd__erp_incumbent` 查询直接 CODE 50001。
    """
    rows, _ = _rows(DATASET_COUPON, [D_CREATOR],
                    [M_CPN_SELF, M_CPN_PLAT, M_CPN_QTY],
                    skus, start, end, owner_erp=owner_erp,
                    include_sku=bool(skus))   # 只要汇总时别带 sku，否则会被截断
    agg = collections.defaultdict(lambda: {"self": 0.0, "plat": 0.0, "qty": 0.0})
    for x in rows:
        a = agg[str(x.get("员工erp") or "(空)")]
        a["self"] += float(x.get(M_CPN_SELF) or 0)
        a["plat"] += float(x.get(M_CPN_PLAT) or 0)
        a["qty"] += float(x.get(M_CPN_QTY) or 0)
    tot = sum(v["self"] for v in agg.values())
    out = []
    for erp, v in sorted(agg.items(), key=lambda t: -t[1]["self"]):
        out.append({"录券人": erp, "我担": round(v["self"], 2),
                    "平台担": round(v["plat"], 2), "子单量": v["qty"],
                    "占我担%": round(100 * v["self"] / tot, 1) if tot else None,
                    "单均我担": round(v["self"] / v["qty"], 2) if v["qty"] else None,
                    # ⚠️「纯自担」只是券成本口径，**不等于可摘**（B补券即反例）。
                    #   要不要摘以 protected.filter_plan(action="strip") 为准。
                    "分类": "共补(别批量摘)" if v["plat"] > 0 else "纯自担(仍需过禁令)"})
    return {"creators": out, "total_self": round(tot, 2),
            "total_plat": round(sum(v["plat"] for v in agg.values()), 2)}


def coupon_breakdown(skus: list, start: str, end: str, owner_erp: str = None) -> dict:
    """逐 SKU × 券批次 的实际成交归因。

    返回 {"by_sku": {sku: {batch: {self,plat,qty,gmv,kind}}},
          "by_batch": {batch: {self,plat,qty,skus,kind}},
          "total_self", "total_plat", "rows_total"}

    `kind`：`自担`（平台担=0）/ `共补`（平台担>0，**别批量摘**）。

    ⚠️★`kind` 是**券成本口径的分类，不是"能不能摘"的结论**。`自担` 只说明平台没分担券成本，
      不代表摘了没副作用——B补券就是 100% 自担却不能摘（平台的钱走团长补贴，另一本账）。
      **拿到清单后必须再过 `core.protected.filter_plan(action="strip")`**。
    """
    rows, total = _rows(DATASET_COUPON, ["batch_id"],
                        [M_CPN_SELF, M_CPN_PLAT, M_CPN_QTY, M_CPN_GMV],
                        skus, start, end, owner_erp=owner_erp)
    by_sku, by_batch = {}, collections.defaultdict(
        lambda: {"self": 0.0, "plat": 0.0, "qty": 0.0, "gmv": 0.0, "skus": set()})
    for x in rows:
        s, b = str(x.get("SKU")), str(x.get("批次号"))
        cell = by_sku.setdefault(s, {}).setdefault(
            b, {"self": 0.0, "plat": 0.0, "qty": 0.0, "gmv": 0.0})
        agg = by_batch[b]
        for key, col in (("self", M_CPN_SELF), ("plat", M_CPN_PLAT),
                         ("qty", M_CPN_QTY), ("gmv", M_CPN_GMV)):
            v = float(x.get(col) or 0)
            cell[key] += v
            agg[key] += v
        agg["skus"].add(s)

    for b, v in by_batch.items():
        v["kind"] = "共补" if v["plat"] > 0 else "自担"
        v["per_order_self"] = round(v["self"] / v["qty"], 4) if v["qty"] else None
        v["skus"] = sorted(v["skus"])
    for s, d in by_sku.items():
        for b, v in d.items():
            v["kind"] = by_batch[b]["kind"]

    return {"by_sku": by_sku, "by_batch": dict(by_batch),
            "total_self": round(sum(v["self"] for v in by_batch.values()), 2),
            "total_plat": round(sum(v["plat"] for v in by_batch.values()), 2),
            "rows_total": total,
            "_caveat": "时效 T-1；批次号与 osw couponId 是两套 ID，按单均我担金额对"}


def promo_breakdown(skus: list, start: str, end: str, owner_erp: str = None) -> dict:
    """逐 SKU × 权益活动 的实际成交归因。

    ⚠️`用促优惠金额` **没有承担方拆分**（不像券有采销/平台/商家三列），自营基本视为采销担。
    ⚠️本函数按**活动侧维度**分组 ⇒ **`owner_erp` 在这里必然被拒**（会被静默丢弃，见模块注释）。
      要按归属看促销，只能先拿到 SKU 清单再传 `skus`。
    """
    rows, total = _rows(DATASET_PROMO,
                        ["equity_act_id", "equity_act_name",
                         "jdr_sch_promotion__act_promot_type_1"],
                        [M_PRM_CUT, M_PRM_QTY, M_PRM_GMV], skus, start, end,
                        owner_erp=owner_erp)
    by_sku, by_act = {}, collections.defaultdict(
        lambda: {"cut": 0.0, "qty": 0.0, "gmv": 0.0, "name": "", "type": "", "skus": set()})
    for x in rows:
        s = str(x.get("商品_编号"))
        aid = str(x.get("权益活动id"))
        cell = by_sku.setdefault(s, {}).setdefault(aid, {"cut": 0.0, "qty": 0.0, "gmv": 0.0})
        agg = by_act[aid]
        agg["name"] = x.get("权益活动名称") or agg["name"]
        agg["type"] = x.get("促销大类型") or agg["type"]
        for key, col in (("cut", M_PRM_CUT), ("qty", M_PRM_QTY), ("gmv", M_PRM_GMV)):
            v = float(x.get(col) or 0)
            cell[key] += v
            agg[key] += v
        agg["skus"].add(s)
    for v in by_act.values():
        v["skus"] = sorted(v["skus"])
        v["per_order_cut"] = round(v["cut"] / v["qty"], 4) if v["qty"] else None
    return {"by_sku": by_sku, "by_act": dict(by_act),
            "total_cut": round(sum(v["cut"] for v in by_act.values()), 2),
            "rows_total": total}


def blind_spot_rate(skus: list, start: str, end: str, pricing: dict = None) -> dict:
    """★**osw 预估的盲区率**：实际用掉的券里，有多少钱是 osw 当前配置看不见的。

    osw 只看得见**当前**挂在 SKU 上的券；商品后续被圈进新券、或券已过期下线，
    它都不知道。这部分钱在 osw 侧完全不存在，所以：
      · 预估毛利与实际单均毛利**必然对不上**（不是数据错）；
      · `stoploss` 从 `pricing.coupons` 建的「真凶排行」会**系统性漏掉**这部分。

    做法：批次号与 osw couponId 是两套 ID 不能 join，**按「单均我担」金额档位对**
    （实证 batch 1315454844 单均我担 3.00/平台 25% ↔ osw `Q3_…4元_东大` jxReward 3.00/jxRatio 75）。
    档位在 osw 当前券里出现 ⇒ 看得见；否则 ⇒ 盲区。

    2026-08-10 实测（13 款禁令款/15 日）：券我担 14,400 元中 **31%(4,407) 落在盲区**
    —— 全部来自「单均我担 5.00」那 31 个批次，osw 侧一张都没有。

    **盲区率高的 SKU，osw 预估对它没有参考价值**，判定必须走实际层（`margin.triage` 的 D 桶）。
    """
    from blacklight.osw import margin as _mt
    skus = [str(s) for s in skus]
    pricing = pricing or _mt.query_pricing_batch(skus)
    live = set()
    for s in skus:
        for c in ((pricing.get(s) or {}).get("coupons") or []):
            jx = float(c.get("jxReward") or 0)
            if jx > 0:
                live.add(round(jx, 2))

    cb = coupon_breakdown(skus, start, end)
    vis = inv = 0.0
    tiers = collections.defaultdict(lambda: {"cost": 0.0, "qty": 0.0, "n": 0})
    for b, v in cb["by_batch"].items():
        per = v.get("per_order_self")
        if not v["qty"]:
            continue
        t = tiers[round(per, 2)]
        t["cost"] += v["self"]
        t["qty"] += v["qty"]
        t["n"] += 1
        if round(per, 2) in live:
            vis += v["self"]
        else:
            inv += v["self"]
    tot = vis + inv
    return {
        "券我担合计": round(tot, 2),
        "osw看得见": round(vis, 2),
        "osw看不见": round(inv, 2),
        "盲区率%": round(100 * inv / tot, 1) if tot else None,
        "osw当前券的我担档位": sorted(live),
        "档位明细": [{"单均我担": p, "批次数": v["n"], "我担": round(v["cost"], 2),
                      "子单量": v["qty"], "占比%": round(100 * v["cost"] / tot, 1) if tot else None,
                      "在osw配置里": p in live}
                     for p, v in sorted(tiers.items(), key=lambda t: -t[1]["cost"])],
        "_判读": "盲区率高 ⇒ osw 预估对这批 SKU 没有参考价值，判亏损必须用实际单均毛利",
    }


def cost_trend(skus: list, before: tuple, after: tuple, owner_erp: str = None) -> dict:
    """★**止亏动作的回读判据**：比对动作前后两个区间的**实际**券我担成本。

    before/after 各是 ("YYYY-MM-DD","YYYY-MM-DD") 区间。

    为什么不能只看「osw 预估是否转正」：
      1. **补位效应**（铁律 6）——摘掉一项，别的券/总价促销/国补会顶上，实证 40 款里 12 款偏离线性预测；
      2. **31% 盲区**——osw 看不见的批次照样在花钱，预估转正不代表真的止住了。
    所以真正的判据是「**实际券我担成本降了没有**」，黄金眼 T-1 拿得到。

    ⚠️两个区间**天数应当一致**，否则金额不可比（本函数会算单均并给出天数提醒）。
    """
    import datetime as _D

    def _span(rg):
        a, b = _D.date.fromisoformat(rg[0]), _D.date.fromisoformat(rg[1])
        return (b - a).days + 1

    out = {}
    for lbl, rg in (("before", before), ("after", after)):
        cb = coupon_breakdown(skus, rg[0], rg[1], owner_erp=owner_erp)
        qty = sum(v["qty"] for v in cb["by_batch"].values())
        out[lbl] = {"区间": "%s~%s" % rg, "天数": _span(rg),
                    "券我担": cb["total_self"], "平台担": cb["total_plat"],
                    "子单量": qty, "批次数": len(cb["by_batch"]),
                    "单均我担": round(cb["total_self"] / qty, 3) if qty else None}
    b, a = out["before"], out["after"]
    d_per = None
    if b["单均我担"] and a["单均我担"] is not None:
        d_per = round(a["单均我担"] - b["单均我担"], 3)
    return {**out,
            "Δ券我担": round(a["券我担"] - b["券我担"], 2),
            "Δ单均我担": d_per,
            "_判据": ("单均我担下降才算止住（总额受单量影响不可直接比）"
                      + ("；⚠️两区间天数不同(%d vs %d)，总额更不可比" % (b["天数"], a["天数"])
                         if b["天数"] != a["天数"] else "")),
            "_盲点": "本表只覆盖券。若摘券后促销/国补补位，总成本可能不降——同时看 promo_breakdown"}


def run_rate(skus: list, asof: str, recent_days: int = 3, baseline_days: int = 12,
             owner_erp: str = None) -> dict:
    """★★**还在流血，还是已经停了**——排处置顺序必须用这个，不能用 15 日失血。

    ## 为什么必须有

    `实际单均毛利` 是 **15 日滚动均值**，所以一次**已经结束的脉冲**会在榜首赖上半个月。
    2026-08-10 实证：我按 15 日失血排出的 D 桶 Top3
    （`10193046394164` −7,004 / `10193024733121` −3,676 / `10186130760890` −3,222），
    逐日拉开一看是 **7/29–8/03 的一次脉冲**（8/02 单日峰值 10,036 元），
    **8/04 起已塌到 ~84 元/天，91% 自己停了**——osw 上那张券也早就不在商品上了，
    摘券根本无事可做。**那份清单是向后看的，据它动手会全打空。**

    换成本函数按流速重排后 Top 完全不同：`10168121856759` 近 3 日日均 369 元
    （基线 31，**+1,076%**）、`10165875240276` 293 元（基线 2.5）——
    这两款 15 日失血只有 −400 / −104，在旧排序里连前 20 都进不去。
    D 桶整体：近 3 日日均 2,648 元 vs 前 12 日 5,578 元 ⇒ **当前水位仅 47%**。

    ## 判读

    | 状态 | 判据 | 含义 |
    |---|---|---|
    | 爆发 | 近期日均 ≥ 2× 基线 | 新圈进来的券，**优先处置** |
    | 持续 | 0.5× ~ 2× | 稳定失血 |
    | 消退 | < 0.5× | 大概率已结束，**先别动**，下次巡检复核 |

    `asof` 一般填 T-1（黄金眼时效）。窗口：`[asof-recent_days+1, asof]` vs 其前 `baseline_days` 天。
    """
    import datetime as _D
    end = _D.date.fromisoformat(asof)
    r_start = end - _D.timedelta(days=recent_days - 1)
    b_end = r_start - _D.timedelta(days=1)
    b_start = b_end - _D.timedelta(days=baseline_days - 1)

    def _pull(s, e):
        out = {}
        skl = [str(x) for x in skus] if skus else None
        step = 120
        chunks = [skl[i:i + step] for i in range(0, len(skl), step)] if skl else [None]
        for ck in chunks:
            cb = coupon_breakdown(ck, s.isoformat(), e.isoformat(), owner_erp=owner_erp)
            for sk, d in cb["by_sku"].items():
                out[sk] = out.get(sk, 0.0) + sum(v["self"] for v in d.values())
        return out

    recent, base = _pull(r_start, end), _pull(b_start, b_end)
    rows = []
    for s in {*recent, *base}:
        rp = recent.get(s, 0.0) / recent_days
        bp = base.get(s, 0.0) / baseline_days
        ratio = (rp / bp) if bp > 0 else (float("inf") if rp > 0 else 0.0)
        rows.append({"sku": s, "近期日均": round(rp, 1), "基线日均": round(bp, 1),
                     "倍数": (None if ratio == float("inf") else round(ratio, 2)),
                     "状态": ("爆发" if ratio >= 2 else "持续" if ratio >= 0.5 else "消退"),
                     "近期我担": round(recent.get(s, 0.0), 2)})
    rows.sort(key=lambda x: -x["近期日均"])
    tot_r = sum(x["近期日均"] for x in rows)
    tot_b = sum(x["基线日均"] for x in rows)
    return {"近期窗口": "%s~%s" % (r_start, end), "基线窗口": "%s~%s" % (b_start, b_end),
            "合计近期日均": round(tot_r, 0), "合计基线日均": round(tot_b, 0),
            "当前水位%": round(100 * tot_r / tot_b, 0) if tot_b else None,
            "爆发": len([x for x in rows if x["状态"] == "爆发"]),
            "消退": len([x for x in rows if x["状态"] == "消退"]),
            "rows": rows,
            "_判据": "排处置顺序用「近期日均」，**不要用 15 日失血**——后者会把已结束的脉冲排在最前面"}


def attribute_loss(skus: list, start: str, end: str, osw_rows: dict = None) -> dict:
    """把「黄金眼实际券促成本」和「osw 实际毛利」摆一起，回答两件事。

    osw_rows: {sku: {"实际单均毛利":…, "近15日单量":…}}，来自
              `osw.margin.list_low_margin()`。不传则只出券促侧。

    ★核心判据是**无券反事实**：
        无券后单均 ≈ (实际单均毛利 × 单量 + 券我担成本) / 单量
      为正 ⇒ 亏损纯由券造成，**定价本身没问题，别去涨价**；
      仍为负 ⇒ 结构性，摘券治不了。

    ⚠️「券覆盖率」（券子单量/osw 单量）实测会出现 100~120%：两侧统计窗口与口径不同
      （osw 近15日 vs 黄金眼 dt 区间），小样本上偏差明显。**它是校验信号不是精确值**，
      >130% 或 <50% 才值得怀疑取数区间搞错了。
    """
    cpn = coupon_breakdown(skus, start, end)
    prm = promo_breakdown(skus, start, end)
    out = []
    for s in [str(x) for x in skus]:
        c = cpn["by_sku"].get(s, {})
        p = prm["by_sku"].get(s, {})
        self_cost = sum(v["self"] for v in c.values())
        plat_cost = sum(v["plat"] for v in c.values())
        cpn_qty = sum(v["qty"] for v in c.values())
        prm_cut = sum(v["cut"] for v in p.values())
        row = {"sku": s, "券我担": round(self_cost, 2), "券平台担": round(plat_cost, 2),
               "券子单量": cpn_qty, "促销让利": round(prm_cut, 2),
               "券批次数": len(c), "促销活动数": len(p)}
        o = (osw_rows or {}).get(s) or {}
        q = float(o.get("近15日单量") or 0)
        per = o.get("实际单均毛利")
        if q and per is not None:
            profit = per * q
            row.update({"osw单量": q, "osw实际单均毛利": per,
                        "期间毛利额": round(profit, 1),
                        "券覆盖率%": round(100 * cpn_qty / q, 1),
                        "无券后单均": round((profit + self_cost) / q, 2)})
            row["判定"] = ("券致亏（定价没问题）" if row["无券后单均"] > 0
                           else "结构性亏损（摘券治不了）")
        out.append(row)
    out.sort(key=lambda r: r.get("期间毛利额", 0))
    return {"skus": out, "by_batch": cpn["by_batch"], "by_act": prm["by_act"],
            "total_self": cpn["total_self"], "total_plat": cpn["total_plat"],
            "total_promo_cut": prm["total_cut"]}
