"""
jzt 广告**批量操作模板**生成：把诊断结论变成运营能直接上传的 xlsx。

前身 `jx-ad-diagnose/scripts/gen_batch_template.py`，2026-08-04 并入 blacklight。

**诊断的终点不是给清单，是给能执行的东西**。默认动作 = 把命中的亏损 SKU 按所属 SPU 分组，
走「修改 + SKU黑名单」剔出全站，**预算/出价原样保留**（只做剔除，不乱动别的）。

为什么不是走接口直接改：SKU 黑名单的写接口尚未抓包（`swa` 只封了预算/出价/启停），
所以这条链路仍以模板交给运营上传。预算/出价类动作可以直接走 `jzt_swa_budget_*`/`bid_*`。

★ 三个实测坑，都已在代码里挡掉：
  1. **必须 .xlsx 不能 .csv** —— `SKU黑名单` 一格内多个 SKU 用英文逗号分隔，
     CSV 会被 Excel 当**千分位数字**把整串合并成一个大数（`10165647591088,1016…` → `1016564759108810000…`）。
  2. **SPUID/SKU黑名单列必须设文本格式**（`number_format='@'`），否则长数字变科学计数。
  3. **SPUID 用「商品编码」那一列**（=jzt 接口的 `spuId`、osw 的 `productId`），
     **别用「供货商spuId」** —— 那是供应商内部 ID，全站后台根本查不到、模板识别不了。
"""
from __future__ import annotations

import os
import re
from typing import Optional

from blacklight.core import BlacklightError

HEADERS = ["广告主PIN", "SPUID", "操作场景", "产品线", "预算设置", "出价设置",
           "出价档位", "智能补贴券", "SKU黑名单"]
NOTE = "必填项\n\n说明：第1行说明、第2行表头请勿删除，从第3行开始填写。"
PRODUCT_LINE = "全站营销-单品推广"
BLACKLIST_PATTERN = "持续亏损|拉黑|blacklist|关停|剔除"

ALIASES = {
    "spu": ["spuid", "spu id", "商品spu_id", "商品spuid", "商品编码", "spu"],
    "sku": ["skuid", "sku id", "商品sku_id", "商品skuid", "sku"],
    # 「结论」是最自然的叫法却一直没在别名里（2026-08-06 补）——诊断产出常直接写「结论」列，
    # 缺了会报「未识别到列 ['verdict']」，让人以为是数据问题而不是别名没覆盖。
    "verdict": ["结论", "处理", "建议", "最终结论", "时序结论", "分类", "盈亏", "动作", "action", "verdict"],
    "budget": ["日预算", "预算", "budget"],
    "troi": ["目标投产比", "目标成交投产比", "出价设置", "troi"],
}


def _norm(s):
    return re.sub(r"\s+", "", str(s or "")).strip().lower()


def _build_map(headers) -> dict:
    hn = {_norm(h): h for h in headers}
    m = {}
    for k, al in ALIASES.items():
        for a in al:
            if _norm(a) in hn:
                m[k] = hn[_norm(a)]
                break
        if k not in m:
            for a in al:
                for kk, orig in hn.items():
                    if _norm(a) in kk:
                        m[k] = orig
                        break
                if k in m:
                    break
    return m


def _troi_str(x) -> str:
    try:
        x = float(str(x).strip())
    except (TypeError, ValueError):
        return ""
    return str(int(x)) if x == int(x) else f"{x:.2f}".rstrip("0").rstrip(".")


def build(rows: list, out_path: str, pin: str = "【请填写采销PIN】",
          action: str = "修改", subsidy: str = "启动",
          blacklist_when: str = BLACKLIST_PATTERN, default_budget=None,
          template: Optional[str] = None) -> dict:
    """按分类好的 SKU 清单生成批量模板。

    `rows`：dict 列表，需含 SPUID / SKUID / 结论列（列名自动识别）。
    `blacklist_when`：结论列命中该正则的 SKU 进黑名单。
    `template`：传官方空模板路径可保留其说明行/格式。

    ⚠️只填**高置信度**动作（持续亏损）；边际/待复核别进模板，另出复核清单。
    ⚠️`广告主PIN` 与 `智能补贴券` 是填不准的两项：PIN 需与投手 ERP 绑定；
       智能补贴券在**修改场景会覆盖现状**——不确定就保持默认并在上传前人工核对。"""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as e:
        raise BlacklightError("缺少依赖 openpyxl") from e
    if not rows:
        raise BlacklightError("rows 为空。")
    if not str(out_path).lower().endswith((".xlsx", ".xlsm")):
        raise BlacklightError(
            "必须输出 .xlsx —— SKU黑名单 一格多个 SKU 用逗号分隔，CSV 会被 Excel 当千分位数字合并成一个大数。")

    m = _build_map(rows[0].keys())
    missing = [k for k in ("spu", "sku", "verdict") if k not in m]
    if missing:
        raise BlacklightError(f"未识别到列 {missing}（现有列：{list(rows[0].keys())}）")
    pat = re.compile(blacklist_when)

    grp = {}
    for r in rows:
        if not pat.search(str(r.get(m["verdict"], ""))):
            continue
        spu = str(r.get(m["spu"], "")).strip()
        sku = str(r.get(m["sku"], "")).strip()
        if not spu or not sku:
            continue
        g = grp.setdefault(spu, {"skus": [], "budget": None, "troi": None})
        if sku not in g["skus"]:
            g["skus"].append(sku)
        if "budget" in m and g["budget"] is None:
            b = str(r.get(m["budget"], "")).strip()
            if b:
                g["budget"] = b
        if "troi" in m and g["troi"] is None:
            g["troi"] = r.get(m["troi"])
    if not grp:
        raise BlacklightError(f"没有命中拉黑规则的 SKU（blacklist_when={blacklist_when!r}），未生成模板。")

    if template:
        wb = openpyxl.load_workbook(template)
        ws = wb.active
        for r in range(3, ws.max_row + 1):          # 清掉示例行，保留说明+表头
            for c in range(1, 10):
                ws.cell(r, c).value = None
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        for j, h in enumerate(HEADERS, 1):
            ws.cell(1, j).value = NOTE if j == 1 else ""
            c = ws.cell(2, j)
            c.value = h
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="C00000")
            c.alignment = Alignment(wrap_text=True, vertical="center")

    def budget_of(g):
        if g["budget"]:
            return str(int(float(g["budget"])))
        return str(default_budget) if default_budget is not None else ""

    for i, (spu, g) in enumerate(sorted(grp.items(), key=lambda kv: -len(kv[1]["skus"]))):
        vals = [pin, spu, action, PRODUCT_LINE, budget_of(g), _troi_str(g["troi"]),
                "", subsidy, ",".join(g["skus"])]
        for j, v in enumerate(vals, 1):
            c = ws.cell(3 + i, j)
            c.value = v
            c.number_format = "@"                   # 全列文本，防科学计数
    ws.column_dimensions["I"].width = 50
    ws.column_dimensions["I"].alignment = Alignment(horizontal="left")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)

    total = sum(len(g["skus"]) for g in grp.values())
    no_budget = [s for s, g in grp.items() if not budget_of(g)]
    return {"path": out_path, "SPU计划数": len(grp), "SKU数": total, "操作场景": action,
            "对照清单": [{"SPUID": s, "SKU数": len(g["skus"]), "SKU黑名单": ",".join(g["skus"]),
                          "日预算": budget_of(g), "目标投产比": _troi_str(g["troi"])}
                         for s, g in sorted(grp.items(), key=lambda kv: -len(kv[1]["skus"]))],
            "⚠️上传前必须人工确认": [
                f"广告主PIN = {pin}（需与投手 ERP 绑定）",
                f"智能补贴券 = {subsidy}（**修改场景会覆盖现状**，不知道现状就先去后台核对）",
            ] + ([f"{len(no_budget)} 个 SPU 缺日预算（留空），上传前需补"] if no_budget else []),
            "_提示": "若某 SPU 的黑名单消耗占该计划 >90%（基本全亏），改用 action='暂停' 停整条计划，"
                     "别逐个拉黑。"}
