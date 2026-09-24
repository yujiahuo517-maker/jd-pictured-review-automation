"""京喜损益「单点问数」与「批量取数」——两层语义分开，别混用。

┌ 单点问数 `sku_pnl` / `dept_pnl`
│   一次请求、秒回、行数可控。回答「某某 SKU 上个月投后毛利多少」这类问题。
│   ★ 传了 skus 时是**服务端按 SKU 过滤**，单页返回并校验 total，不翻页也不会漏。
└ 批量取数 `sku_pnl_bulk` / `fetch_sliced`
    **按类目切片**拉全量，每片单页装下并校验 `len(rows)==total`，装不下自动下钻再切。
    （`fetch_all` 是旧的翻页实现，**结果不可信**，只保留作反例，见其 docstring。）

## ★★服务端分页不可信（2026-08-07 实证，这是本模块最重要的一条）
同一个查询翻 117 页拿回 58264 行，**去重后只剩 38541 个 SKU**——15329 个重复
（最多 5 次、重复行内容完全相同、规律地出现在第 2/4/6…页），同时另一批行一次都没被吐出来。
根因：**没有稳定排序时服务端每页的行集合会漂移**。
⇒ **别翻页**。要全量用切片法（31 片、58264 行、**重复 0**、90 秒；翻页法 6.8 分钟还是错的）；
   要 Top N 用**服务端排序**（`query(order_by=...)`，第 1 页就是全局 Top N，一次请求）。
   排序参数 2026-08-07 已从「分析 → 排序」面板抓到真身，见 dataset.py 的 sort_list 注释。

## 切片为什么按类目而不按天
按天切必须把 `dt` 放进维度，行数变成「天×SKU」组合，**单天就有 2.5~4.2 万行**、比整月 SKU 数还多，
越切越大。按类目切时日期只做**区间过滤**、不进维度，行数只与 SKU 数有关。
另：日期维度不能用 `in` 切，平台强制「时间筛选必填且必须为闭区间」。

## 其他边界
- `page_size` 上限 **30000**（要 50000 只给 30000，100000 报「非法的分页参数」）。
- **行数随指标集变化**：同样过滤，2 个指标 total=46967、4 个指标 total=58264。
  所以「total 对不上」先看是不是指标集不同，别急着怀疑数据。

## 指标口径（2026-08-07 实证）
- `综合毛利`(可控·实收·不含税) 是**广告前**；`投后履约毛利额` 是**广告后**。
  实测同一 SKU 7 月：综合毛利 +390.90，投后 **−531.46** —— 差额就是广告+履约。
- 「投后履约毛利额」在字段目录里**重名 5 个 + 「投后毛利额」1 个**（都是看板自定义字段），
  但实测 **6 个返回完全相同的值**（-531.4555689745057）⇒ 重名是冗余，不是口径分歧。
  仍保留 `verify_metric_aliases()` 供换数据集时复验，别默认它永远成立。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from blacklight.core import BlacklightError
from blacklight.easybi import dataset as ds

# 标准指标（平台标准字段，code 稳定）
M_NETGMV = "cfo_cfo_ordpl_pl_netgmv_gs_cw_pl_jx_amt"
M_QTY = "cfo_cfo_ordpl_pl_netgmv_gs_cw_pl_jx_qtty"
M_GROSS = "cfo_cfo_ordpl_pl_cw_pl_jx_gross_profit_gross_cgp_ctr_ac_notax"
# 投后（自定义字段，code 就是中文名）
M_POST = "投后履约毛利额"

D_SKU, D_SPU, D_DT, D_SALER = "sku_id", "spu_id", "dt", "cate_op_erp"
D_CAT1, D_CAT2, D_CAT3 = "cate_1", "cate_2", "cate_3"
D_MODE = "jdr_jx_sku_jx_sale_mode_type"
MODE_SELF = "new_jdly"                      # 京喜自营


def _post_metric(dataset_id: int = ds.DATASET_JX_PL) -> dict:
    """拿「投后履约毛利额」字段对象（重名取第一个——已实证等价，见模块 docstring）。"""
    c = ds.find_fields(M_POST, dataset_id, sort="metric")
    if not c:
        raise BlacklightError("字段目录里没有「%s」——数据集被改过，先跑 easybi doctor" % M_POST)
    return c[0]


def verify_metric_aliases(sku: str, start: str, end: str,
                          dataset_id: int = ds.DATASET_JX_PL) -> dict:
    """复验：重名的「投后履约毛利额」是否真的等价。换数据集/怀疑口径时跑一次。"""
    cands = {c["id"]: c for c in
             ds.find_fields("投后毛利额", dataset_id, "metric") +
             ds.find_fields("投后履约毛利额", dataset_id, "metric")}
    dt = ds._pick(D_DT, dataset_id, "dim")
    sk = ds._pick(D_SKU, dataset_id, "dim")
    flt = [(sk, "in", [str(sku)]), (dt, ">=", [start]), (dt, "<=", [end])]
    vals = {}
    for cid, c in cands.items():
        try:
            r = ds.query(dims=[sk], metrics=[c], filters=flt, dataset_id=dataset_id, page_size=5)
            vals[cid] = (c["name"], r["rows"][0].get(c["name"]) if r["rows"] else None)
        except Exception as e:
            vals[cid] = (c["name"], "ERR:%s" % str(e)[:60])
    nums = {v[1] for v in vals.values() if isinstance(v[1], str) and not v[1].startswith("ERR")}
    return {"sku": sku, "区间": "%s~%s" % (start, end), "候选数": len(cands),
            "逐个取值": vals, "全部一致": len(nums) <= 1,
            "结论": "等价，可任取" if len(nums) <= 1 else "★不一致！必须人工确认用哪个"}


# --------------------------------------------------------------------------- #
# 单点问数
# --------------------------------------------------------------------------- #
def sku_pnl(skus, start: str, end: str, by_day: bool = False,
            with_post: bool = True, dept: str = None,
            mode: str = MODE_SELF, dataset_id: int = ds.DATASET_JX_PL) -> dict:
    """**单点问数**：1~N 个 SKU 在 [start,end] 的损益。

    skus:    单个 SKU 或列表（服务端过滤，不翻页也不会漏）。
    by_day:  True → 按天展开（能看出「哪天开始转负」，实测很关键）。
    mode:    经营模式，默认**只算京喜自营**（与看板一致）；传 None = 含 C 店全量。
             ★三个接口默认口径必须一致，否则 sku 级和部门级加不起来。
    返回 {区间, rows[...], 合计{...}}；金额已转 float。
    """
    if isinstance(skus, (str, int)):
        skus = [skus]
    skus = [str(s) for s in skus]
    if not skus:
        raise BlacklightError("至少给一个 SKU")

    sk = ds._pick(D_SKU, dataset_id, "dim")
    dt = ds._pick(D_DT, dataset_id, "dim")
    mets = [M_NETGMV, M_QTY, M_GROSS] + ([_post_metric(dataset_id)] if with_post else [])
    dims = ([dt, sk] if by_day else [sk])
    # 单点场景行数上界 = SKU 数 × 天数，直接给足，避免静默截断
    days = 400 if by_day else 1
    flt = [(sk, "in", skus), (dt, ">=", [start]), (dt, "<=", [end])]
    if mode:
        flt.insert(0, (ds._pick(D_MODE, dataset_id, "dim"), "in", [mode]))
    r = ds.query(dims=dims, metrics=mets, filters=flt,
                 dataset_id=dataset_id, dept=dept,
                 page_size=min(max(len(skus) * days, 50), 5000))
    rows = [_norm(x) for x in r["rows"]]
    if r.get("total") and len(rows) < r["total"]:
        raise BlacklightError("单点问数被截断：拿到 %d 行但 total=%s —— 行数超出单页，"
                              "改用 fetch_all()" % (len(rows), r["total"]))
    return {"区间": "%s ~ %s" % (start, end), "SKU数": len(skus), "by_day": by_day,
            "rows": rows, "合计": _sum(rows)}


def dept_pnl(start: str, end: str, by: str = None, top: int = 30,
             dept: str = None, mode: str = MODE_SELF,
             dataset_id: int = ds.DATASET_JX_PL) -> dict:
    """**单点问数**：部门整体损益，可按维度下钻（by='cate_2' / 'cate_op_erp' / 'dt' …）。

    不传 by = 只出一行总计（最快，用来对看板）。
    """
    dt = ds._pick(D_DT, dataset_id, "dim")
    md = ds._pick(D_MODE, dataset_id, "dim")
    dims = [ds._pick(by, dataset_id, "dim")] if by else [md]
    mets = [M_NETGMV, M_QTY, M_GROSS, _post_metric(dataset_id)]
    flt = [(dt, ">=", [start]), (dt, "<=", [end])]
    if mode:
        flt.insert(0, (md, "in", [mode]))
    r = ds.query(dims=dims, metrics=mets, filters=flt, dataset_id=dataset_id,
                 dept=dept, page_size=max(top, 50))
    rows = [_norm(x) for x in r["rows"]]
    rows.sort(key=lambda x: x.get("投后履约毛利额") if x.get("投后履约毛利额") is not None else 0)
    return {"区间": "%s ~ %s" % (start, end), "下钻维度": by or "(总计)",
            "服务端total": r.get("total"), "rows": rows[:top], "合计": _sum(rows)}


# --------------------------------------------------------------------------- #
# 批量取数
# --------------------------------------------------------------------------- #
def fetch_sliced(dims: list, metrics: list, slice_dim=D_CAT1, slice_values: list = None,
                 filters: list = None, dataset_id: int = ds.DATASET_JX_PL,
                 dept: str = None, page_size: int = 30000, verbose: bool = True,
                 sub_dims: tuple = (D_CAT2, D_CAT3, D_SPU)) -> dict:
    """**批量取数·切片法**（推荐）：把大查询按某个维度切成若干小查询，每片单页装下。

    ★★为什么不用翻页：**服务端分页不稳定**（2026-08-07 实证）。
      同一个查询翻 117 页拿回 58264 行，去重后只剩 **38541** 个 SKU
      （15329 个重复、最多重复 5 次，重复行内容完全相同），
      而单页查询报的 total 是 46967 —— **既重复又缺失，聚合值偏差 26%**
      （投后合计 682257 vs 去重后 507054）。
      根因：没有稳定排序时，服务端每页的行集合会漂移。**所以别翻页。**
      （服务端排序后来打通了，但那是给 Top N 用的：`query(order_by=...)` 第 1 页即全局 Top N；
        要**全量明细**仍应走本函数，别指望排序+翻页。）

    切片法保证正确性的方式：**每片都校验 `len(rows) == total`**，装不下自动下钻到
    更细维度（sub_dims），仍装不下直接抛错，而不是静默返回半截。
    ⚠️切片维度选**类目**不要选日期：按天切必须把 dt 放进维度，行数变成「天×SKU」组合，
      单天 2.5~4.2 万行、比整月 SKU 数还多，越切越大。
    """
    t0 = time.time()
    state = {"rows": [], "n": 0, "slices": 0}

    def _cond(sd, v):
        # ⚠️日期维度不能用 `in` 切：平台强制「时间筛选必填且必须为闭区间」
        #   （用 in 报 code=99999「同源分析中时间筛选必填且必须为闭区间」）。
        if sd.get("code") == D_DT or sd.get("fieldTypeCategory") == "DATE":
            return [(sd, ">=", [str(v)]), (sd, "<=", [str(v)])]
        return [(sd, "in", [str(v)])]

    def _values_of(dim, flt):
        """枚举某维度在当前过滤下的取值（用最轻的指标，只为拿 code 列表）。"""
        rr = ds.query(dims=[dim], metrics=[M_NETGMV], filters=flt,
                      dataset_id=dataset_id, dept=dept, page_size=1000)
        col = (dim.get("name") or dim.get("code"))
        return [x.get(col) for x in rr["rows"] if x.get(col) is not None]

    def _run(sd, vals, flt_base, depth):
        for v in vals:
            flt = list(flt_base) + _cond(sd, v)
            r = ds.query(dims=dims, metrics=metrics, filters=flt, dataset_id=dataset_id,
                         dept=dept, page_size=page_size, page_num=1)
            rows = [_norm(x) for x in r["rows"]]
            tot = int(r.get("total") or 0)
            if tot > len(rows):                       # 这一片单页装不下 → 用下一级维度再切
                nxt = sub_dims[depth] if depth < len(sub_dims) else None
                if nxt is None:
                    raise BlacklightError(
                        "切片 %s=%s 有 %d 行、单页只能取 %d，且没有更细的维度可切。"
                        "**不返回半截数据**（分页不可信，半截会被当成全量）。"
                        "请传更细的 sub_dims。" % (sd.get("name"), v, tot, len(rows)))
                nd = ds._pick(nxt, dataset_id, "dim")
                if verbose:
                    print("  %s%s=%s 有 %d 行 → 下钻到「%s」再切"
                          % ("  " * depth, sd.get("name"), v, tot, nd.get("name")), flush=True)
                _run(nd, _values_of(nd, flt), flt, depth + 1)
                continue
            state["rows"].extend(rows)
            state["n"] += len(rows)
            state["slices"] += 1
            if verbose:
                print("  %s[%s=%-10s] %6d 行 (total %d) ✅  累计%-7d 已用%5.1fs"
                      % ("  " * depth, sd.get("name"), v, len(rows), tot,
                         state["n"], time.time() - t0), flush=True)

    sd0 = ds._pick(slice_dim, dataset_id, "dim")
    vals0 = slice_values if slice_values else _values_of(sd0, list(filters or []))
    _run(sd0, vals0, list(filters or []), 0)
    return {"rows": state["rows"], "片数": state["slices"], "拿到": state["n"],
            "完成": True, "耗时秒": round(time.time() - t0, 1),
            "_口径": "切片法：每片单页装下且校验 len(rows)==total（装不下自动下钻再切）"
                     " ⇒ 无翻页、无重复、无遗漏"}


def fetch_all(dims: list, metrics: list, filters: list = None,
              dataset_id: int = ds.DATASET_JX_PL, dept: str = None,
              page_size: int = 500, start_page: int = 1, max_pages: int = None,
              on_page: Optional[Callable] = None, verbose: bool = True,
              max_consecutive_errors: int = 3) -> dict:
    """**批量取数·翻页法**。⚠️⚠️**结果可能既重复又缺失，别用它出聚合数**。

    2026-08-07 实证：翻 117 页拿回 58264 行 → 去重后仅 **38541** 个 SKU
    （15329 个重复、最多 5 次），而单页 total=46967 ⇒ **重复 + 缺失并存**，
    投后合计偏差 26%（682257 → 去重后 507054）。根因是服务端**分页不稳定**
    （无稳定排序时每页行集合漂移），而排序参数没能逆向出来。
    ⇒ **要正确的全量请用 `fetch_sliced`**（按维度切片、每片单页装下并校验）。
    本函数保留仅供「看个样子/估量级」，返回里会带 `⚠️` 提示。

    四道机制（与 core.pmap_batch 同理念——写操作有闸门，昂贵操作也要有）：
      · 探针：先拉第 1 页拿 total，**算出页数和 ETA 再决定继续**；
      · 进度：每页打印 页码/累计/ETA；
      · 熔断：连续 `max_consecutive_errors` 页失败即停，返回已拿到的部分（不假装成功）；
      · 断点：返回 `next_page`，可用 `start_page` 续跑。
    on_page(rows, page, total) 可用来流式落盘，避免全量堆内存。
    """
    t0 = time.time()
    out, page, errs, total = [], start_page, 0, None
    while True:
        try:
            r = ds.query(dims=dims, metrics=metrics, filters=filters, dataset_id=dataset_id,
                         dept=dept, page_size=page_size, page_num=page)
            errs = 0
        except Exception as e:
            errs += 1
            if verbose:
                print("  第%d页失败(%d/%d): %s" % (page, errs, max_consecutive_errors, str(e)[:110]))
            if errs >= max_consecutive_errors:
                return {"rows": out, "total": total, "拿到": len(out), "next_page": page,
                        "完成": False, "中断原因": "连续 %d 页失败" % errs,
                        "耗时秒": round(time.time() - t0, 1)}
            continue
        rows = [_norm(x) for x in r["rows"]]
        if total is None:
            total = r.get("total")
            pages = (int(total) + page_size - 1) // page_size if total else 1
            if verbose:
                print("批量取数：total=%s，页大小 %d ⇒ 约 %d 页" % (total, page_size, pages))
            if max_pages and pages > max_pages:
                if verbose:
                    print("  ⚠️ 超过 max_pages=%d，只取前 %d 页（**这是截断，别当全量**）"
                          % (max_pages, max_pages))
        if on_page:
            on_page(rows, page, total)
        else:
            out.extend(rows)
        got = len(out) if not on_page else (page - start_page + 1) * page_size
        if verbose:
            el = time.time() - t0
            done = page - start_page + 1
            eta = (el / done) * (((int(total) + page_size - 1) // page_size) - page) if total else 0
            print("  第%-4d页  本页%-5d  累计%-7d  已用%5.1fs  ETA%6.1fs" % (page, len(rows), got, el, eta))
        if not rows or (total and got >= int(total)):
            break
        page += 1
        if max_pages and (page - start_page) >= max_pages:
            return {"rows": out, "total": total, "拿到": got, "next_page": page,
                    "完成": False, "中断原因": "达到 max_pages（结果是截断的）",
                    "耗时秒": round(time.time() - t0, 1)}
    return {"rows": out, "total": total, "拿到": len(out) if not on_page else got,
            "next_page": None, "完成": True, "耗时秒": round(time.time() - t0, 1),
            "⚠️分页不可信": "服务端分页不稳定，本结果**可能既重复又缺失**（实证偏差26%）。"
                          "要正确全量请用 fetch_sliced()。"}


def sku_pnl_bulk(start: str, end: str, with_post: bool = True,
                 dept: str = None, mode: str = MODE_SELF,
                 dataset_id: int = ds.DATASET_JX_PL, page_size: int = 30000,
                 verbose: bool = True) -> dict:
    """**批量取数**：全部 SKU 的损益。**按类目切片，不翻页**。

    ★为什么按类目切而不是按天：按天切时维度里必须带 `dt`，行数变成「天×SKU」组合，
      单天就有 2.5~4.2 万行、比整月 SKU 数还多（2026-08-07 实测），越切越大。
      按类目切则日期只做**区间过滤**、不进维度，行数只与 SKU 数有关。
    ★实测（2026-07 自营）：一级类目 21 片，各片行数**合计 58264 = 不切片时的 total**，
      既不重叠也不遗漏；其中「收纳用品」57441 行超单页 → 自动下钻到二级类目（11 片，
      最大 14116），合计仍是 57441。全程 31 次查询。
    """
    sk = ds._pick(D_SKU, dataset_id, "dim")
    dt = ds._pick(D_DT, dataset_id, "dim")
    mets = [M_NETGMV, M_QTY, M_GROSS] + ([_post_metric(dataset_id)] if with_post else [])
    flt = [(dt, ">=", [start]), (dt, "<=", [end])]
    if mode:
        flt.append((ds._pick(D_MODE, dataset_id, "dim"), "in", [mode]))
    r = fetch_sliced(dims=[sk], metrics=mets, slice_dim=D_CAT1, filters=flt,
                     dataset_id=dataset_id, dept=dept, page_size=page_size, verbose=verbose)
    # 同一 SKU 可能跨片出现（理论上不会，但校验一次不亏）
    import collections as _c
    dup = [k for k, v in _c.Counter(x.get("SKU") for x in r["rows"]).items() if v > 1]
    r["重复SKU数"] = len(dup)
    r["区间"] = "%s ~ %s" % (start, end)
    if dup:
        r["⚠️"] = "出现 %d 个跨片重复 SKU（切片维度可能不互斥），聚合前请先去重" % len(dup)
    return r


# --------------------------------------------------------------------------- #
_NUM_COLS = ("NETGMV_商品销售", "数量_销售", "毛利_综合毛利_可控_实收_不含税",
             "投后履约毛利额", "投后毛利额", "广告投后履约毛利额")


def _norm(row: dict) -> dict:
    """金额转 float、去掉 `xxx$value` 冗余键（保留成可读的 `名称`）。"""
    out = {}
    for k, v in row.items():
        if k.endswith("$value"):
            out.setdefault("名称", v)
            continue
        if k in _NUM_COLS:
            try:
                out[k] = round(float(str(v).replace(",", "")), 2)
                continue
            except Exception:
                pass
        out[k] = v
    return out


def _sum(rows: list) -> dict:
    agg = {}
    for k in _NUM_COLS:
        vals = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        if vals:
            agg[k] = round(sum(vals), 2)
    q = agg.get("数量_销售")
    p = agg.get("投后履约毛利额")
    if q and p is not None:
        agg["单均投后毛利"] = round(p / q, 3)
    return agg
