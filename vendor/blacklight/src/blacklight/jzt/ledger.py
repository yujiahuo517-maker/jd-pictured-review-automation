"""
jzt 广告**分析账本**：把每次 SKU 级诊断结果留档，支持跨 run 对比。

前身 `jx-ad-diagnose/scripts/ledger.py` + `ledger/*.csv`，2026-08-04 并入 blacklight。

**核心价值**：某 SKU 上次被拉黑、这次毛利达标了 → `compare` 把它标为「**可放出**」。
没有账本就只能每次从零判，结果是"拉黑了就再也没放回来"，把本来能赚钱的品永久摁死。
**下次分析先 compare，再处理新增拉黑** —— 顺序别反。

⚠️★★**`verdict` 记的是「结论/建议」，不是「实际执行状态」**——两者会长期不一致。
  2026-08-10 实证：08-07 那个 run 里 verdict=拉黑 的一批，实际**一张也没上传执行**
  （当时只出了 xlsx）。于是 compare 报「可放出 33」，抽查 5/5 **都不在黑名单、都在正常
  投放且投后为正**（270/327/88/33/219）。正确读法是「上次**建议**拉黑的 33 款本周实际
  在赚钱——幸好没执行」，而不是「33 款被摁死了要放出来」。
  ⇒ **执行与否写进 `note`**（本模块 `append` 支持 note 透传）：
    `note="已执行"` / `note="仅建议"`。compare 的「可放出/持续拉黑」必须结合 note 读，
    否则会把"从没拉过的"当成"拉了很久该放了"。

★存放位置（按 `core/paths.py` 三分法）：账本含 SKU 级毛利/价格，属**业务数据不入库**
（与 `_analysis_*` 同类），放 `runtime/ledger/`，gitignore 已排除整个 `ledger/` 目录。
但它**不可再生**（历史快照丢了就没了）—— 与 runtime 里其它"可再生"的东西不同，
换机器/清理 runtime 前务必单独备份。这一点在 paths.ledger_path() 上也标了。
"""
from __future__ import annotations

import csv
import io
import os
import re
from collections import defaultdict
from typing import Optional

from blacklight.core import BlacklightError
from blacklight.core import paths

FIELDS = ["run_date", "skuid", "spu", "plan_name", "jd_margin", "ds_margin",
          "post_margin", "cause", "verdict", "spend", "roi", "note",
          # ★2026-08-21 扩展：换 ge 下载中心取数后多出的科目，用来回答「亏在哪一项」。
          #   老 run 没有这些列 ⇒ `_write` 用 r.get(k,"") 补空，向后兼容。
          "ord_qty", "gmv", "sku_cost", "delv_cost", "cps_fee",
          "coupon_plat", "coupon_bu", "promo_bu", "newuser_sub",
          "redpack_total", "redpack_plat", "spend_pre"]

# 放出线：投后毛利率 ≥0 或（无投后时）到手价毛利 ≥12%
RELEASE_POST, RELEASE_DS = 0.0, 0.12

ALIASES = {
    "skuid": ["skuid", "sku id", "商品sku_id", "sku"],
    "spu": ["spuid", "spu id", "商品spu_id", "spu", "所属spu计划"],
    "plan_name": ["计划名", "计划名称", "所属计划", "所属spu计划", "plan"],
    "jd_margin": ["京东价毛利", "京东价毛利率", "jd_margin"],
    "ds_margin": ["到手价毛利", "到手价毛利率", "当前底表毛利率", "ds_margin"],
    "post_margin": ["投后毛利率", "广告投后履约毛利率", "post_margin"],
    "cause": ["归因", "根因", "cause"],
    "verdict": ["最终结论", "时序结论", "分类", "建议", "verdict", "盈亏"],
    "spend": ["消耗", "消耗金额", "月花费", "spend"],
    "roi": ["roi", "投产比", "广告投产比"],
}


def _norm(s):
    return re.sub(r"\s+", "", str(s or "")).strip().lower()


def _to_f(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "nan", "none", "None"):
        return None
    try:
        f = float(s)
        return f / 100 if "%" in str(v) else f
    except ValueError:
        return None


def _norm_verdict(*texts) -> str:
    """把各种写法的结论归一到 拉黑 / 复核 / 放出 / 保留。**归一化是 compare 的地基**——
    上次写"持续亏损"这次写"剔出全站"，不归一就会被当成两种状态、反复横跳统计全废。

    `复核`（边际/待复核）是**独立状态不是保留**：它表示"还没判"，
    从 复核→拉黑 不该算"新增拉黑"（本来就没说它好）。历史账本里已有这个值。

    ⚠️★**本函数会同时扫 `verdict` 和 `cause`**（append 传的是 `(verdict, cause)`）——
      于是**归因文案里的措辞会把结论带偏**。2026-08-10 实证：我给 192 条写
      `最终结论="复核"`，但 cause 写成「…计划已**暂停**…」，命中下面的 `暂停` 关键词，
      整批被判成 `拉黑`，note 却是"仅建议"，自相矛盾。
      ⇒ **写 cause 时避开这些触发词**：拉黑/剔出/黑名单/持续亏损/关停/暂停/放出/恢复/复核/边际/待定/观察。
        要描述"所属计划非在投"就别写"已暂停"。"""
    # ★★2026-08-18 加固：**调用方显式给了标准 verdict 就直接采信，不再扫 cause**。
    #   此前无条件把 (verdict, cause) 拼起来做关键词匹配 ⇒ 归因文案里的措辞会把结论整批带偏，
    #   同一个坑已踩两次：08-10 写 cause="…计划已**暂停**…" 把 192 条 复核 判成 拉黑；
    #   08-18 写 cause="**已拉黑**；拉黑后自然流量…" 把 113 条 可放出(保留) 判成 拉黑。
    #   docstring 里的"避开触发词"是**约定**，挡不住手滑；这里改成机制。
    #   非标准写法（"持续亏损"/"剔出全站"…）仍走下面的关键词归一，向后兼容历史账本。
    first = str(texts[0]).strip() if texts and texts[0] is not None else ""
    if first in ("拉黑", "放出", "复核", "保留"):
        return first
    t = "".join(str(x) for x in texts if x is not None)
    if any(k in t for k in ("拉黑", "剔出", "黑名单", "持续亏损", "关停", "暂停")):
        return "拉黑"
    if any(k in t for k in ("放出", "恢复", "可能已恢复")):
        return "放出"
    if any(k in t for k in ("复核", "边际", "待定", "观察")):
        return "复核"
    return "保留"


def path() -> str:
    return paths.ledger_path()


def _read(p: Optional[str] = None) -> list:
    p = p or path()
    if not os.path.isfile(p):
        return []
    with io.open(p, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r.get("skuid")]


def _write(rows: list, p: Optional[str] = None) -> None:
    p = p or path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _map_row(r: dict) -> dict:
    """把任意列名的输入行映射到账本字段。"""
    hn = {_norm(k): k for k in r}
    out = {}
    for key, al in ALIASES.items():
        col = None
        for a in al:
            if _norm(a) in hn:
                col = hn[_norm(a)]
                break
        if not col:
            for a in al:
                for k, orig in hn.items():
                    if _norm(a) in k:
                        col = orig
                        break
                if col:
                    break
        if col:
            out[key] = r.get(col)
    return out


def append(rows: list, run_date: str, ledger_path: str = None) -> dict:
    """把本次分析的 SKU 明细写进账本。**同 run_date 重跑会覆盖当日那批**（幂等，可反复重跑）。

    `rows` 是 dict 列表，列名自动识别（SKUID/SPU/计划名/京东价毛利/到手价毛利/投后毛利率/归因/最终结论/消耗/ROI）。
    ★另外认 `note`/`备注`：**记「是否真执行」**（如 `已执行` / `仅建议`）。
      verdict 是结论、note 是事实，别指望它们一致——见模块注释里 2026-08-10 的实证。"""
    if not rows:
        raise BlacklightError("rows 为空，不写账本。")
    if not run_date:
        raise BlacklightError("必须给 run_date（如 2026-08-04），否则无法跨 run 对比。")
    p = ledger_path or path()
    led = [r for r in _read(p) if r.get("run_date") != run_date]   # 覆盖同日
    added = []
    for r in rows:
        m = _map_row(r)
        sku = m.get("skuid")
        if sku is None or str(sku).strip() == "":
            continue
        try:
            sku = str(int(float(sku)))          # 防科学计数/浮点串
        except (TypeError, ValueError):
            sku = str(sku).strip()
        added.append({"run_date": run_date, "skuid": sku,
                      "spu": m.get("spu", ""), "plan_name": m.get("plan_name", ""),
                      "jd_margin": m.get("jd_margin", ""), "ds_margin": m.get("ds_margin", ""),
                      "post_margin": m.get("post_margin", ""), "cause": m.get("cause", ""),
                      "verdict": _norm_verdict(m.get("verdict"), m.get("cause")),
                      "spend": m.get("spend", ""), "roi": m.get("roi", ""),
                      # ★note 记「是否真执行」——verdict 只是结论，两者会不一致，见模块注释
                      "note": str(r.get("note") or r.get("备注") or ""),
                      # ★2026-08-21：扩展科目**按同名键直接透传**。
                      #   上一版只显式列了固定键 ⇒ FIELDS 加了 12 列、daily_ledger 也算了值，
                      #   但全被这里丢掉，落盘后整列为空（回读闸抓到）。
                      #   ——「加了字段≠落了盘」，同 fix-the-caller-not-just-the-function。
                      **{k: r.get(k, "") for k in FIELDS
                         if k not in ("run_date", "skuid", "spu", "plan_name", "jd_margin",
                                      "ds_margin", "post_margin", "cause", "verdict",
                                      "spend", "roi", "note")}})
    _write(led + added, p)
    runs = sorted({r["run_date"] for r in led + added})

    # ★★写完**回读自检**（2026-08-21）：扩展了 FIELDS、`daily_ledger` 也算出了值，
    #   但中间的 append 当时显式列键、没透传 ⇒ **落盘整列为空**，还是靠外部脚本临时
    #   写的回读才发现。这种「加了字段没落盘」不报错，必须内建检查。
    back = [r for r in _read(p) if r.get("run_date") == run_date]
    filled = {k: sum(1 for r in back if (r.get(k) or "") not in ("", "0", "0.0"))
              for k in FIELDS if k not in ("run_date", "skuid")}
    empty_cols = [k for k, v in filled.items() if v == 0]
    out = {"path": p, "本次写入": len(added), "账本总行数": len(led) + len(added),
           "run_date": run_date, "已有run": runs[-6:], "run总数": len(runs),
           "回读": {"落盘行数": len(back), "非空列数": len(filled) - len(empty_cols),
                    "整列为空": empty_cols or None}}
    if len(back) != len(added):
        out["⚠️行数不符"] = "写入 %d 行但回读 %d 行" % (len(added), len(back))
    if empty_cols:
        out["⚠️整列为空"] = ("这些列一行都没值：%s —— 若是本次新加的字段，"
                             "多半是**中间层没透传**（同 fix-the-caller-not-just-the-function）"
                             % ", ".join(empty_cols[:8]))
    return out


def agg_days(skus=None, since: str = None, until: str = None,
             ledger_path: str = None) -> dict:
    """把账本按 SKU 做**多日聚合**，返回 {sku: {天数, GMV, 消耗, 投前, 投后, 投后毛利率}}。

    ## ★为什么必须多日：单日判据会翻转
    2026-08-21 实测 `10191500638968` **单日 +16.44** 进了 compare 的「可放出」，
    **四天聚合 −154.20**；compare 的「反复横跳」占交集 **13.7%(121/886)** 就是这个噪声。
    放出/拉黑一律用本函数，别拿单个 run 的 `post_margin` 下结论。

    口径：投前(裸毛利) = GMV − 商品成本 − 物流 − CPS佣服 + 优惠券平台补贴；投后 = 投前 − 折后消耗。
    （这些列 2026-08-21 起才有，早于该日的 run 取不到 ⇒ 结果里 `缺列天数` 会标出来）
    """
    led = _read(ledger_path)
    if not led:
        raise BlacklightError("账本为空，先 append。")
    want = {str(s) for s in skus} if skus else None
    rows = [r for r in led
            if (want is None or r["skuid"] in want)
            and (since is None or r["run_date"] >= since)
            and (until is None or r["run_date"] <= until)]

    def _f(r, k):
        try:
            return float(r.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    agg, miss = {}, 0
    for r in rows:
        a = agg.setdefault(r["skuid"], {"天数": set(), "GMV": 0.0, "消耗": 0.0,
                                        "商品成本": 0.0, "物流": 0.0, "CPS": 0.0, "券平台补贴": 0.0})
        if not (r.get("gmv") or "").strip():
            miss += 1
        a["天数"].add(r["run_date"])
        a["GMV"] += _f(r, "gmv"); a["消耗"] += _f(r, "spend")
        a["商品成本"] += _f(r, "sku_cost"); a["物流"] += _f(r, "delv_cost")
        a["CPS"] += _f(r, "cps_fee"); a["券平台补贴"] += _f(r, "coupon_plat")
    out = {}
    for s, a in agg.items():
        pre = a["GMV"] - a["商品成本"] - a["物流"] - a["CPS"] + a["券平台补贴"]
        post = pre - a["消耗"]
        out[s] = {"天数": len(a["天数"]), "GMV": round(a["GMV"], 2),
                  "消耗": round(a["消耗"], 2), "投前": round(pre, 2), "投后": round(post, 2),
                  "投后毛利率": (round(post / a["GMV"] * 100, 1) if a["GMV"] else None),
                  "达标": (a["GMV"] > 0 and post >= 0)}
    return {"rows": out, "SKU数": len(out), "窗口": "%s~%s" % (since or "最早", until or "最新"),
            "缺新科目的行": miss or None,
            "_判据": "达标 = 有成交 且 多日聚合投后 ≥0。**别用单日**：实测单日会翻转。"}


def compare(ledger_path: str = None, release_post: float = RELEASE_POST,
            release_ds: float = RELEASE_DS, limit: int = 20,
            verify_platform: bool = True) -> dict:
    """对比账本最近两个 run：**可放出 / 新增拉黑 / 持续拉黑 / 反复横跳**。

    「可放出」= 上次拉黑、这次达标（投后毛利率 ≥ release_post，无投后则到手价毛利 ≥ release_ds）。
    ⚠️最好**连续 2 次达标**再真放出，防临界抖动导致反复拉黑/放出（看「反复横跳」那组）。

    ## ★★`verify_platform=True`（默认）：与平台真实黑名单对账
    **账本的 `verdict` 是分析结论，不是平台状态**——两个方向都出过事：
      · 2026-08-07：平台**拉黑了、账本没记**（note 全空）⇒ compare 报出**假的可放出**
      · 2026-08-21：账本标了「拉黑」、**平台根本没拉** ⇒ 12 款「可放出」扫完 94 个单元
        发现**一款都不在黑名单里**，无处可放，白算一场
    ⇒ 本函数调 `swa.sku_black_scan()`（带 95% 覆盖率闸 + 30 分钟缓存）拿平台真值，把桶拆成：
        可放出        = 账本达标 **且** 平台确实在黑名单里 → 真能操作
        无需操作(未拉黑) = 账本说拉黑但平台没拉 → **别当行动清单**
        新增拉黑      = 账本判拉黑 **且** 平台还没拉 → 真要动手
        已在黑名单    = 平台早拉了 → 无需重复
    ⚠️**刚做过拉黑/放出写操作要传 `refresh` 语义**：本函数用的是 30 分钟缓存，
      写完立刻 compare 可能读到旧状态（见 stateful-cache-and-untested-path）。
    ⚠️对账失败（覆盖率闸没过/取数异常）**不静默降级**：桶原样返回并置 `_平台对账.失败`，
      此时「可放出」只是账本口径，**不可直接执行**。
    `verify_platform=False` 跳过对账（纯账本口径，快）。"""
    led = _read(ledger_path)
    if not led:
        raise BlacklightError(f"账本为空（{ledger_path or path()}），先 append。")
    runs = sorted({r["run_date"] for r in led})
    if len(runs) < 2:
        raise BlacklightError(f"账本只有 1 个 run（{runs[0]}），无法对比。再 append 一次新分析后再 compare。")
    prev, cur = runs[-2], runs[-1]
    P = {r["skuid"]: r for r in led if r["run_date"] == prev}
    C = {r["skuid"]: r for r in led if r["run_date"] == cur}

    hist = defaultdict(list)
    for r in sorted(led, key=lambda x: x["run_date"]):
        hist[r["skuid"]].append(r["verdict"])

    def qualifies(r):
        pm, dm = _to_f(r.get("post_margin")), _to_f(r.get("ds_margin"))
        if pm is not None:
            return pm >= release_post
        if dm is not None:
            return dm >= release_ds
        return False

    release, newblack, stayblack, flip = [], [], [], []
    for sku, c in C.items():
        p = P.get(sku)
        pv = p["verdict"] if p else None
        if pv == "拉黑" and qualifies(c):
            release.append(c)
        elif pv in ("保留", "放出") and c["verdict"] == "拉黑":
            newblack.append(c)
        elif pv == "拉黑" and c["verdict"] == "拉黑":
            stayblack.append(c)
        v = hist[sku][-3:]
        if sum(1 for i in range(1, len(v)) if v[i] != v[i - 1]) >= 2:
            flip.append(c)

    def brief(lst):
        return [{k: r.get(k) for k in ("skuid", "spu", "plan_name", "post_margin",
                                       "ds_margin", "cause", "spend")} for r in lst[:limit]]

    # ★覆盖度体检：两个 run 的规模差太多 / 交集太小时，全零结果是"没可比"而不是"没变化"。
    # 不显式说出来，读的人会把"可放出 0"理解成"没有能放出的"，从而漏掉真正能放回投放的品。
    overlap = len(set(P) & set(C))
    warn = None
    if not overlap:
        warn = (f"两个 run 没有共同 SKU（{prev} {len(P)} 条 / {cur} {len(C)} 条）→ "
                f"**本次对比无意义**，不是『没有变化』。")
    elif len(C) < len(P) * 0.5 or overlap < min(len(P), len(C)) * 0.5:
        warn = (f"覆盖度差异大：{prev} {len(P)} 条 vs {cur} {len(C)} 条，交集仅 {overlap} 条 → "
                f"最近一次可能是**局部批次而非全量分析**，全零结果代表『没可比』不代表『没变化』。"
                f"要看真实变化，请先跑一次全量分析再 append。")

    # ---- ★平台状态对账（硬闸）----
    plat = {"启用": verify_platform}
    noop_release, noop_black = [], []
    if verify_platform:
        try:
            from blacklight.jzt import swa as _swa
            scan = _swa.sku_black_scan()
            blackset = {str(k) for k in (scan.get("已拉黑") or {})}
            plat.update({"单元数": scan.get("单元数"), "覆盖率": scan.get("覆盖率"),
                         "平台黑名单SKU数": len(blackset),
                         "缓存年龄秒": scan.get("_cached_age_s")})
            _rel = [r for r in release if str(r["skuid"]) in blackset]
            noop_release = [r for r in release if str(r["skuid"]) not in blackset]
            _nb = [r for r in newblack if str(r["skuid"]) not in blackset]
            noop_black = [r for r in newblack if str(r["skuid"]) in blackset]
            plat["修正"] = ("可放出 %d→%d（%d 款平台本就没拉，无处可放）；"
                            "新增拉黑 %d→%d（%d 款平台早已拉黑）"
                            % (len(release), len(_rel), len(noop_release),
                               len(newblack), len(_nb), len(noop_black)))
            release, newblack = _rel, _nb
        except Exception as e:
            plat["失败"] = "%s: %s" % (type(e).__name__, str(e)[:160])
            plat["⚠️"] = ("平台对账失败 ⇒ 下面的「可放出/新增拉黑」**只是账本口径**，"
                          "**不可直接执行**。账本 verdict ≠ 平台状态，两个方向都出过事。")

    return {"对比": f"{prev} → {cur}", "账本": ledger_path or path(),
            "覆盖": {f"{prev}条数": len(P), f"{cur}条数": len(C), "交集": overlap},
            "⚠️": warn,
            "_平台对账": plat,
            "可放出": {"count": len(release),
                       "含义": ("上次拉黑、本次达标**且平台确在黑名单**" if verify_platform
                                else "上次拉黑、本次达标（未与平台对账）"),
                       "rows": brief(release)},
            "无需操作(账本说拉黑·平台没拉)": {"count": len(noop_release), "rows": brief(noop_release)},
            "新增拉黑": {"count": len(newblack), "rows": brief(newblack)},
            "已在黑名单(无需重复)": {"count": len(noop_black), "rows": brief(noop_black)},
            "持续拉黑": {"count": len(stayblack), "rows": brief(stayblack)},
            "反复横跳": {"count": len(flip), "含义": "近3run翻转≥2次，阈值临界，别再来回动", "rows": brief(flip)},
            "放出线": {"投后毛利率≥": release_post, "或到手价毛利≥": release_ds},
            "_下一步": "「可放出」做成批量模板（修改+清空该SKU黑名单）放回投放；「新增拉黑」做成加黑名单模板。"
                       "最好连续2次达标再真放出。"}


def history(sku, ledger_path: str = None) -> dict:
    """看某个 SKU 的历次快照（判断它是稳定亏还是临界抖动）。"""
    rows = [r for r in _read(ledger_path) if r["skuid"] == str(sku)]
    rows.sort(key=lambda r: r["run_date"])
    return {"skuid": str(sku), "count": len(rows), "rows": rows}
