"""blacklight.pic.collect —— 同品带图好评**采集器编排**。

采集器本体是同事写的 Playwright 脚本（`vendor/jd_good_reviews/main.py`，原样收编、**未改一行**）：
按输入 SKU 走京东图片识别找同品 → 拉同品的 `club.jd.com` 评论 → 挑「4星以上 + 有图 + 非京喜
+ 无负面词 + 图片全局去重」的晒图好评 → 写出 Excel。

本模块只做三件事，好让它接进 blacklight 全链路：
  1. `plan_input()`  ——把 `client.targets()` 的待补清单写成采集器认的输入表
  2. `run_collector()`——起子进程跑采集（长任务，**建议后台跑**，别在 MCP 工具里干等）
  3. `read_output()` ——把输出表读回成 `client.import_rows()` 直接能吃的行

★列名不用转换：采集器的输出列 `SKUID/商品名称/评价文本/实拍图1..9` 与平台「批量导入带图评价
模板」**逐字一致**（平台前端映射 `{"*SKUID":skuId, 评价文本:evaluateText, 实拍图N:evaluateImageN}`）。

★断点续跑靠采集器自己的 `data/progress.json`：同一份 progress 会跳过已完成 SKU。**换一批 SKU
重跑前**要么换 `--work-dir`，要么清 progress —— 否则老 SKU 会被无脑跳过。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

from blacklight.core import BlacklightError
from blacklight.core import paths as _paths

OUT_HEADERS = ["SKUID", "商品名称", "评价文本"] + [f"实拍图{i}" for i in range(1, 10)]
IN_HEADERS = ["SKUID", "商品名称"]


def collector_dir() -> str:
    d = _paths.review_collector_dir()
    if not os.path.exists(os.path.join(d, "main.py")):
        raise BlacklightError(f"采集器不在 {d}（缺 main.py）。用 BLACKLIGHT_REVIEW_COLLECTOR 指到别处。")
    return d


def _openpyxl():
    try:
        import openpyxl
    except ImportError as e:      # pragma: no cover
        raise BlacklightError("缺少 openpyxl") from e
    return openpyxl


# --------------------------------------------------------------------------- #
# ① 输入表
# --------------------------------------------------------------------------- #
def plan_input(items: list, out_path: str = None) -> dict:
    """把待补清单写成采集器输入表（列：SKUID / 商品名称）。

    `items` 收 `client.targets()["items"]` 或 [{sku_id, sku_name}] 或裸 SKU 列表。"""
    rows = []
    seen = set()
    for it in items or []:
        if isinstance(it, dict):
            sku = str(it.get("sku_id") or it.get("skuId") or it.get("SKUID") or "").strip()
            name = str(it.get("sku_name") or it.get("skuName") or it.get("商品名称") or "")
        else:
            sku, name = str(it).strip(), ""
        if not sku or sku in seen:
            continue
        seen.add(sku)
        rows.append((sku, name))
    if not rows:
        raise BlacklightError("没有可写入的 SKU")

    out_path = out_path or os.path.join(
        _paths.exports_dir("piceval"), time.strftime("targets_%Y%m%d_%H%M%S.xlsx"))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb = _openpyxl().Workbook()
    ws = wb.active
    ws.title = "待采集"
    ws.append(IN_HEADERS)
    for sku, name in rows:
        ws.append([sku, name])
    wb.save(out_path)
    return {"input_path": out_path, "count": len(rows),
            "output_path_hint": _default_output(out_path)}


def _default_output(input_path: str) -> str:
    base, _ = os.path.splitext(os.path.abspath(input_path))
    return base + "_带图好评输出.xlsx"


# --------------------------------------------------------------------------- #
# ② 跑采集
# --------------------------------------------------------------------------- #
def collector_command(input_path: str, output_path: str = None, headless: bool = True,
                      work_dir: str = None, python_exe: str = None) -> dict:
    """只**拼命令不执行** —— 采集一条 SKU 约 3~5 秒，几百条就是几十分钟，
    在 MCP 工具里同步等会把会话卡死。**推荐把这条命令丢后台跑**，再用 `collector_status()` 看进度。"""
    d = collector_dir()
    out = output_path or _default_output(input_path)
    py = python_exe or sys.executable
    cmd = [py, os.path.join(d, "main.py"), "--input", os.path.abspath(input_path),
           "--output", os.path.abspath(out), "--config", os.path.join(d, "config.yaml"),
           "--headless" if headless else "--headful"]
    return {"cmd": cmd, "cmd_str": " ".join(f'"{c}"' if " " in c else c for c in cmd),
            "cwd": work_dir or d, "output_path": out,
            "note": "长任务：建议后台执行。进度看 logs/run.log 与 data/progress.json（collector_status）。"}


def run_collector(input_path: str, output_path: str = None, headless: bool = True,
                  timeout: int = 3600, work_dir: str = None, python_exe: str = None) -> dict:
    """[阻塞] 真跑采集器子进程。`timeout` 秒（默认 1 小时）。超时不删已产出的行 —— 采集器
    每条 SKU 都即时写盘 + 落 progress，超时后重跑会断点续上。"""
    spec = collector_command(input_path, output_path, headless, work_dir, python_exe)
    t0 = time.time()
    try:
        p = subprocess.run(spec["cmd"], cwd=spec["cwd"], timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        tail = (p.stdout or b"").decode("utf-8", "replace")[-2000:]
        return {"finished": True, "returncode": p.returncode, "seconds": round(time.time() - t0, 1),
                "output_path": spec["output_path"], "tail": tail, **collector_status()}
    except subprocess.TimeoutExpired:
        return {"finished": False, "reason": f"超过 {timeout}s 未跑完（进度已落盘，重跑会续上）",
                "output_path": spec["output_path"], "seconds": round(time.time() - t0, 1),
                **collector_status()}


def collector_status(work_dir: str = None) -> dict:
    """读采集器自己的进度：`data/progress.json` 统计 + `logs/run.log` 尾巴。"""
    d = work_dir or collector_dir()
    prog_path = os.path.join(d, "data", "progress.json")
    stat = {"progress_path": prog_path, "done": 0, "success": 0, "empty": 0}
    if os.path.exists(prog_path):
        try:
            with open(prog_path, encoding="utf-8") as f:
                prog = json.load(f)
            stat["done"] = len(prog)
            stat["success"] = sum(1 for v in prog.values() if v.get("status") == "success")
            stat["empty"] = sum(1 for v in prog.values() if v.get("status") == "empty")
        except Exception as e:                    # noqa: BLE001
            stat["progress_error"] = str(e)[:120]
    log_path = os.path.join(d, "logs", "run.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                stat["log_tail"] = f.readlines()[-8:]
        except Exception:                          # noqa: BLE001
            pass
    return stat


def reset_progress(work_dir: str = None, keep_used_images: bool = True) -> dict:
    """清采集器断点（换一批 SKU 重跑前用）。`keep_used_images=True` 保留图片全局去重库
    —— **别乱清**：清掉后不同 SKU 会被分到同一张图，平台机审会判重。"""
    d = work_dir or collector_dir()
    removed = []
    for name in ["progress.json"] + ([] if keep_used_images else ["used_images.json"]):
        p = os.path.join(d, "data", name)
        if os.path.exists(p):
            os.remove(p)
            removed.append(name)
    return {"removed": removed, "kept_used_images": keep_used_images}


# --------------------------------------------------------------------------- #
# ③ 读输出表
# --------------------------------------------------------------------------- #
def read_output(path: str, screen: bool = True, require_text: bool = True,
                allow_review: bool = False, only_filled: bool = None) -> dict:
    """把采集输出表读成可提交的行，并**默认过一道闸**（空值剔除 + 贬损文本筛查）。

    采集器对采不到的 SKU **会写空行占位**（今天那批 227 个里 34 个是这种），
    图空/图文全空的行直接剔掉；文案贬损/带联系方式/委婉阴阳的也剔掉。
    返回 `screen.screen_rows` 的四桶 {ok, needs_text, review, dropped} + `rows`(=ok，兼容旧用法)。
    `screen=False` 退回纯读表（`rows` 给全部非空行，不做任何判断）。"""
    if not os.path.exists(path):
        raise BlacklightError(f"输出表不存在：{path}")
    wb = _openpyxl().load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = [str(c).strip() if c is not None else "" for c in next(rows_iter)]
    except StopIteration:
        raise BlacklightError("输出表是空的")
    idx = {h: i for i, h in enumerate(header)}
    for need in ("SKUID", "评价文本"):
        if need not in idx:
            raise BlacklightError(f"输出表缺列 {need}（实际表头：{header}）")

    def cell(r, name):
        i = idx.get(name)
        if i is None or i >= len(r):
            return ""
        return str(r[i]).strip() if r[i] is not None else ""

    raw_rows, blank = [], 0
    for r in rows_iter:
        if not r or not cell(r, "SKUID"):
            continue
        imgs = [cell(r, f"实拍图{i}") for i in range(1, 10)]
        imgs = [u for u in imgs if u]
        text = cell(r, "评价文本")
        if not imgs and not text:
            blank += 1
        raw_rows.append({"sku_id": cell(r, "SKUID"), "sku_name": cell(r, "商品名称"),
                         "eval_content": text, "images": imgs, "source_type": "real_review"})

    if not screen:
        keep = [r for r in raw_rows if r["images"] or r["eval_content"]]
        return {"path": path, "read": len(raw_rows), "blank_rows": blank,
                "count": len(keep), "rows": keep, "screened": False}

    from blacklight.pic import screen as _s
    res = _s.screen_rows(raw_rows, require_text=require_text, allow_review=allow_review)
    return {"path": path, "read": len(raw_rows), "blank_rows": blank, "screened": True,
            "count": len(res["ok"]), "rows": res["ok"], **res}


def write_template(rows: list, path: str = None, dropped: list = None) -> dict:
    """把行写成**平台批量导入模板**（SKUID/商品名称/评价文本/实拍图1..9）。

    只写传进来的行 —— 请传体检后的 `ok` 桶，别把 dropped 一起写进去。
    传了 `dropped` 会另存一份「已剔除清单」（多一列剔除原因），供人工回看。"""
    rows = rows or []
    if not rows:
        raise BlacklightError("没有可写入的行（体检后 ok 桶是空的？先看 dropped 的原因分布）")
    path = path or os.path.join(_paths.exports_dir("piceval"),
                                time.strftime("导入模板_%Y%m%d_%H%M%S.xlsx"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb = _openpyxl().Workbook()
    ws = wb.active
    ws.title = "批量导入模板"
    ws.append(OUT_HEADERS)
    for r in rows:
        imgs = (r.get("images") or [])[:9]
        ws.append([r.get("sku_id", ""), r.get("sku_name", ""), r.get("eval_content", "")]
                  + list(imgs) + [""] * (9 - len(imgs)))
    out = {"template_path": path, "count": len(rows)}
    if dropped:
        ws2 = wb.create_sheet("已剔除")
        ws2.append(["SKUID", "商品名称", "评价文本", "图片数", "剔除原因"])
        for r in dropped:
            ws2.append([r.get("sku_id", ""), r.get("sku_name", ""),
                        (r.get("eval_content") or "")[:200], len(r.get("images") or []),
                        r.get("reason", "")])
        out["dropped_count"] = len(dropped)
    wb.save(path)
    return out
