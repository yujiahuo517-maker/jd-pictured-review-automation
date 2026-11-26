"""Resume product-display asset work in an existing, explicit EasyBI scope."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from designspark_client import (DesignSpark, GenerationPending, LoginRequired,
                               PROMPT_VERSION, atomic_json, verified_output)

PENDING = {1, 3, 6, 10, 210, 310}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


@contextlib.contextmanager
def workspace_lock(workspace):
    import msvcrt

    path = workspace / "assets-run" / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        raise RuntimeError("Another asset worker is using this workspace") from exc
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def fingerprint(spu, sku, source):
    return hashlib.sha256(f"{PROMPT_VERSION}|{spu}|{sku}|{source}".encode()).hexdigest()[:20]


def prepare(workspace: Path):
    scope = read(workspace / "easybi-pending.json")["result"]
    targets = read(workspace / "targets-all.json")
    tasks = read(workspace / "tasks-all.json")
    if targets["errors"] or tasks["errors"]:
        raise ValueError("Incomplete current platform scope query")
    if targets.get("erp") != scope["erp"] or tasks.get("erp") != scope["erp"]:
        raise ValueError("Account mismatch between scope and platform query")
    spus = scope["spu_ids"]
    if len(spus) != len(set(spus)):
        raise ValueError("Duplicate SPU IDs")
    old_rows = {r["SPU"]: r for r in read(workspace / "SPU处理明细.json")}
    if set(old_rows) != set(spus):
        raise ValueError("Source SPU records differ from the hard scope")
    by_spu = defaultdict(list)
    for row in targets["items"]:
        if row["spu_id"] not in spus:
            raise ValueError("Out-of-scope target")
        if row.get("result_type") == 0 and row.get("sku_image") and int(row.get("used", 0)) < 10:
            by_spu[row["spu_id"]].append(row)
    task_map = defaultdict(list)
    for row in tasks["items"]:
        if row["spu_id"] not in spus:
            raise ValueError("Out-of-scope task")
        task_map[row["spu_id"]].append(row)
    ledger_path = workspace / "assets-run" / "ledger.json"
    previous = {r["spu_id"]: r for r in read(ledger_path)["items"]} if ledger_path.exists() else {}
    entries = []
    for spu in spus:
        if not re.fullmatch(r"\d+", spu):
            raise ValueError("Invalid SPU identifier")
        old = old_rows[spu]
        candidates = sorted(by_spu[spu], key=lambda r: (
            bool(re.search(r"单拍不发|补邮|差价|耳堵", r["sku_name"])),
            r["sku_id"] != old["SKU"], int(r.get("used", 0)), r["sku_id"],
        ))
        states = {r["status"] for r in task_map[spu]}
        status = "queued"
        if 330 in states or 11 in states:
            status = "existing_complete"
        elif states & PENDING:
            status = "existing_pending"
        elif not candidates:
            status = "no_eligible_target"
        target = candidates[0] if candidates else {}
        sku = target.get("sku_id", old["SKU"])
        if not re.fullmatch(r"\d+", sku):
            raise ValueError("Invalid SKU identifier")
        source = target.get("sku_image", "")
        key = fingerprint(spu, sku, source)
        entry = {"spu_id": spu, "sku_id": sku, "title": target.get("sku_name", old["商品名称"]),
                 "source_url": source, "fingerprint": key, "status": status,
                 "previous_category": old["处理结果"], "previous_reason": old["原因"],
                 "tasks": [{k: t.get(k) for k in ("task_id", "sku_id", "status", "status_cn")} for t in task_map[spu]],
                 "purpose": "product_display_draft", "source_selection": "Use target-owned main image; do not transplant other-product customer reviews"}
        prior = previous.get(spu, {})
        if prior.get("fingerprint") == key and status == "queued":
            if prior.get("status") == "ready":
                if verified_output(prior.get("output", {})):
                    entry.update({k: v for k, v in prior.items() if k not in {"tasks", "previous_category", "previous_reason"}})
                else:
                    entry.update(status="output_invalid", error="Existing output failed integrity verification; generation was not repeated")
            elif prior.get("status") in {"generation_started", "generation_pending"}:
                entry.update({k: v for k, v in prior.items() if k not in {"tasks", "previous_category", "previous_reason"}})
        entries.append(entry)
    result = {"erp": scope["erp"], "scope_date": scope["date"], "prepared_at": now(),
              "target_query_time": datetime.fromtimestamp((workspace / "targets-all.json").stat().st_mtime).astimezone().isoformat(),
              "task_query_time": datetime.fromtimestamp((workspace / "tasks-all.json").stat().st_mtime).astimezone().isoformat(),
              "scope_count": len(spus), "prompt_version": PROMPT_VERSION,
              "image_save_mode": "provider_original", "review_submissions": 0, "items": entries}
    atomic_json(ledger_path, result)
    return result


def source_image(job, folder):
    from PIL import Image

    output = folder / "source.png"
    if output.exists():
        with Image.open(output) as image:
            image.verify()
        return output
    url = job["source_url"]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".360buyimg.com"):
        raise ValueError("Expected an HTTPS JD product image")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        final_host = urlparse(response.url).hostname or ""
        if not final_host.endswith(".360buyimg.com"):
            raise ValueError("Unexpected product image redirect")
        data = response.read(25_000_001)
    if len(data) > 25_000_000:
        raise ValueError("Source image exceeds size limit")
    with Image.open(io.BytesIO(data)) as original:
        original.load()
        rgba = original.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
    if min(image.size) < 128:
        raise ValueError("Product main image is too small")
    image.thumbnail((1600, 1600))
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / "source.writing.png"
    image.save(temp, format="PNG")
    temp.replace(output)
    return output


def export_report(workspace: Path, ledger, prefix="SPU执行汇总"):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    names = {"queued": "待生成", "ready": "AI展示草稿已生成", "existing_pending": "原有评价任务处理中",
             "existing_complete": "平台已有完成记录", "no_eligible_target": "无可处理商品",
             "generation_started": "已触发生图，待核实", "generation_pending": "生图待回读",
             "source_failed": "商品主图获取失败", "failed": "素材处理失败", "blocked_login": "需要DesignSpark登录",
             "output_invalid": "已有输出校验失败"}
    rows = []
    for job in ledger["items"]:
        rows.append({"SPU": job["spu_id"], "SKU": job["sku_id"], "商品名称": job["title"],
                     "本次素材状态": names.get(job["status"], job["status"]),
                     "素材文件": job.get("output", {}).get("path", ""),
                     "本次原因": job.get("error", ""), "素材用途": "AI商品展示草稿",
                     "此前评价处理": job["previous_category"], "此前原因": job["previous_reason"],
                     "现有评价任务": "；".join(f"{t['task_id']}: {t['status']} {t['status_cn']}" for t in job["tasks"]),
                     "本次评价提交数": 0, "素材处理时间": job.get("updated_at", ""),
                     "EasyBI日期": ledger["scope_date"], "评价回读时间": ledger["task_query_time"]})
    counts = Counter(j["status"] for j in ledger["items"])
    summary = {"SPU总数": len(rows), "素材结果": {names.get(k, k): v for k, v in counts.items()},
               "本轮生成草稿": counts.get("ready", 0), "本轮评价提交": 0,
               "已有生效330的SPU": sum(any(t["status"] == 330 for t in j["tasks"]) for j in ledger["items"]),
               "说明": "AI展示草稿不是买家评价，不计入完成带图评价。此前评价任务只回读。",
               "报告生成时间": now(), "任务状态回读时间": ledger["task_query_time"]}
    atomic_json(workspace / f"{prefix}.json", {"summary": summary, "rows": rows})
    wb = Workbook()
    sheet = wb.active
    sheet.title = "汇总"
    sheet.append(["指标", "结果"])
    for key, value in summary.items():
        sheet.append([key, json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value])
    for name, selected in (("SPU明细", rows), ("已生成素材", [r for r, j in zip(rows, ledger["items"]) if j["status"] == "ready"]),
                           ("未完成素材", [r for r, j in zip(rows, ledger["items"]) if j["status"] not in {"ready", "existing_pending", "existing_complete"}])):
        sheet = wb.create_sheet(name)
        sheet.append(list(rows[0]))
        for row in selected:
            sheet.append(list(row.values()))
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        sheet.auto_filter.ref = sheet.dimensions
    for sheet in wb:
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="245A81")
        for col in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(col)].width = 55 if col in {3, 5, 6, 9, 10} else 24
        for line in sheet.iter_rows(min_row=2):
            for cell in line:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    path = workspace / f"{prefix}.xlsx"
    wb.save(path)
    check = load_workbook(path, read_only=True)
    assert check["SPU明细"].max_row == len(ledger["items"]) + 1
    assert {r[0] for r in check["SPU明细"].iter_rows(min_row=2, values_only=True)} == {j["spu_id"] for j in ledger["items"]}
    check.close()
    return summary


def execute(workspace, profile, settings, limit, seconds):
    ledger = prepare(workspace)
    path = workspace / "assets-run" / "ledger.json"
    queue = [j for j in ledger["items"] if j["status"] in {"queued", "generation_started", "generation_pending"}]
    if limit:
        queue = queue[:limit]
    print(json.dumps({"scope": len(ledger["items"]), "run_count": len(queue), "event": "start"}), flush=True)
    with DesignSpark(profile, settings) as client:
        probe = client.probe(workspace / "assets-run" / "probe.png")
        atomic_json(workspace / "assets-run" / "probe.json", probe)
        if not probe["ready"]:
            for job in queue:
                if job["status"] == "queued":
                    job.update(status="blocked_login", error=probe["reason"], updated_at=now())
            atomic_json(path, ledger)
            export_report(workspace, ledger)
            raise LoginRequired(probe["reason"])
        for index, job in enumerate(queue, 1):
            folder = workspace / "assets-run" / "drafts" / job["spu_id"] / job["fingerprint"]
            output = folder / f"{job['sku_id']}-designspark.png"
            print(json.dumps({"event": "item_start", "index": index, "count": len(queue), "spu": job["spu_id"]}), flush=True)
            def checkpoint(state, url, recovery=None):
                job.update(status=state, _page_url=url, updated_at=now())
                if recovery:
                    job.setdefault("_generation", {}).update(recovery)
                atomic_json(path, ledger)
            try:
                if job["status"] in {"generation_started", "generation_pending"}:
                    if not job.get("_page_url") and not job.get("_generation", {}).get("result_url"):
                        raise GenerationPending("Existing generation has no page reference; no duplicate requested")
                    result = client.generate(folder / "source.png", output, checkpoint, existing_url=job.get("_page_url"), seconds=seconds, recovery=job.get("_generation"))
                else:
                    try:
                        source = source_image(job, folder)
                    except Exception as exc:
                        job.update(status="source_failed", error=str(exc)[:300])
                        continue
                    result = client.generate(source, output, checkpoint, seconds=seconds)
                job.update(status="ready", output=result, error="")
            except LoginRequired as exc:
                job.update(status="blocked_login", error=str(exc))
                break
            except GenerationPending as exc:
                job.update(status="generation_pending", error=str(exc))
            except Exception as exc:
                state = "generation_pending" if job["status"] in {"generation_started", "generation_pending"} else "failed"
                job.update(status=state, error=f"{type(exc).__name__}: {str(exc)[:300]}")
            finally:
                job["updated_at"] = now()
                atomic_json(path, ledger)
                print(json.dumps({"event": "item_end", "index": index, "spu": job["spu_id"], "status": job["status"], "error": job.get("error", "")}, ensure_ascii=False), flush=True)
    summary = export_report(workspace, ledger)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "probe", "login", "run", "report"])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    if args.limit < 0 or args.seconds < 1:
        parser.error("limit must be nonnegative and seconds positive")
    if args.runtime_root:
        sys.path.insert(0, str(args.runtime_root / "runtime" / "python-libs"))
    workspace = args.workspace.resolve()
    if args.mode == "prepare":
        with workspace_lock(workspace):
            result = prepare(workspace)
            print(json.dumps(export_report(workspace, result), ensure_ascii=False))
        return
    if args.mode == "report":
        print(json.dumps(export_report(workspace, read(workspace / "assets-run" / "ledger.json")), ensure_ascii=False))
        return
    config_path = workspace / "asset-config-local.json"
    config = read(config_path) if config_path.exists() else {}
    default_profile = (args.runtime_root or Path(__file__).parent.parent) / "runtime" / "state" / "designspark-profile"
    profile = args.profile or Path(config.get("profile", default_profile))
    settings = read(Path(__file__).parent.parent / "assets" / "designspark-settings.json")
    if args.mode == "login":
        with workspace_lock(workspace), DesignSpark(profile, settings, visible=True) as client:
            page = client.open_page()
            print("请在已打开的 Chrome 中登录 DesignSpark，登录后自动结束。", flush=True)
            if client.find(page, "new_chat", seconds=600) is None:
                raise LoginRequired("Login did not complete within 10 minutes")
            atomic_json(config_path, {"profile": str(profile.resolve())})
            print("DesignSpark 登录已确认。", flush=True)
    elif args.mode == "probe":
        with DesignSpark(profile, settings) as client:
            result = client.probe(workspace / "assets-run" / "probe.png")
        atomic_json(workspace / "assets-run" / "probe.json", result)
        print(json.dumps(result))
    else:
        with workspace_lock(workspace):
            execute(workspace, profile, settings, args.limit, args.seconds)


if __name__ == "__main__":
    main()
