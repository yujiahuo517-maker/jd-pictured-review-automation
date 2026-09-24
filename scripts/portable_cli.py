from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


SKILL_ROOT = Path(__file__).resolve().parents[1]
VENDOR_SRC = SKILL_ROOT / "vendor" / "blacklight" / "src"
PYTHON_LIBS = SKILL_ROOT / "runtime" / "python-libs"
STATE_DIR = SKILL_ROOT / "runtime" / "state"

sys.path.insert(0, str(VENDOR_SRC))
sys.path.insert(0, str(PYTHON_LIBS))
os.environ.setdefault("BLACKLIGHT_HOME", str(STATE_DIR))


def load_payload(raw_json: str | None, input_path: str | None) -> dict[str, Any]:
    if raw_json and input_path:
        raise ValueError("--json 和 --input 只能使用一个")
    if input_path:
        with Path(input_path).open("r", encoding="utf-8-sig") as file:
            value = json.load(file)
    elif raw_json:
        value = json.loads(raw_json)
    else:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("输入 JSON 顶层必须是对象")
    return value


def doctor(_: dict[str, Any]) -> dict[str, Any]:
    from blacklight.core import auth
    from blacklight.pic import client, imgzone, screen

    return {
        "ok": True,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "skill_root": str(SKILL_ROOT),
        "state_dir": str(STATE_DIR),
        "modules": [client.__name__, imgzone.__name__, screen.__name__],
        "login": auth.status_dict(),
    }


def userinfo(_: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.client import user_info

    return user_info()


def easybi_pending(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.easybi_pending import pending_spus

    return pending_spus(**payload)


def targets(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.client import targets as get_targets

    include_has_eval = bool(payload.pop("include_has_eval", False))
    payload["image_filter"] = 0 if include_has_eval else 1
    return get_targets(**payload)


def llm_status(_: dict[str, Any]) -> dict[str, Any]:
    from blacklight.llm.gateway import status

    return status()


def llm_image(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.llm.gateway import image_edit, image_generate

    source_images = payload.pop("source_images", None)
    return image_edit(images=source_images, **payload) if source_images else image_generate(**payload)


def imgzone_upload(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.imgzone import upload

    return upload(**payload)


def imgzone_find(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.imgzone import check_alive, find_by_name

    images = find_by_name(**payload)
    checks = [check_alive(item["url"]) for item in images if item.get("url")]
    return {"count": len(images), "images": images, "checks": checks}


def ai_text(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.client import ai_generate

    return ai_generate(**payload)

def real_review_fallback(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.collect import plan_input, read_output, run_collector

    items = payload.pop("items", None) or []
    if not items:
        raise ValueError("items 不能为空，必须传入已锁定范围的 targets.items")
    output_dir = Path(payload.pop("output_dir", STATE_DIR / "exports" / "real_reviews"))
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = payload.pop("stem", f"real_reviews_{time.strftime('%Y%m%d_%H%M%S')}")
    input_path = output_dir / f"{stem}_input.xlsx"
    output_path = output_dir / f"{stem}_output.xlsx"
    plan = plan_input(items, out_path=str(input_path))
    run = run_collector(
        str(input_path),
        output_path=str(output_path),
        headless=bool(payload.pop("headless", True)),
        timeout=int(payload.pop("timeout", 1800)),
        python_exe=sys.executable,
    )
    if payload:
        raise ValueError(f"未知参数：{sorted(payload)}")
    if not run.get("finished") or run.get("returncode") not in (0, None):
        return {"plan": plan, "run": run, "screened": False, "rows": []}
    screened = read_output(str(output_path), screen=True, require_text=True, allow_review=False)
    return {"plan": plan, "run": run, "screened": True, **screened}


def screen_rows(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.screen import screen_rows as run_screen

    return run_screen(**payload)


def import_dryrun(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.client import import_rows_dryrun

    return import_rows_dryrun(**payload)


def import_rows(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.client import import_rows as run_import

    return run_import(**payload)


def tasks(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.client import task_list

    return task_list(**payload)


def task_stats(payload: dict[str, Any]) -> dict[str, Any]:
    from blacklight.pic.client import task_stats as get_task_stats

    return get_task_stats(**payload)


COMMANDS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "doctor": doctor,
    "userinfo": userinfo,
    "easybi-pending": easybi_pending,
    "targets": targets,
    "llm-status": llm_status,
    "llm-image": llm_image,
    "imgzone-upload": imgzone_upload,
    "imgzone-find": imgzone_find,
    "ai-text": ai_text,
    "real-review": real_review_fallback,
    "screen": screen_rows,
    "import-dryrun": import_dryrun,
    "import": import_rows,
    "tasks": tasks,
    "task-stats": task_stats,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="京喜带图评价便携调用器")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--json", dest="raw_json")
    parser.add_argument("--input", dest="input_path")
    parser.add_argument("--output", dest="output_path")
    args = parser.parse_args()

    try:
        payload = load_payload(args.raw_json, args.input_path)
        result = COMMANDS[args.command](payload)
        rendered = json.dumps({"ok": True, "command": args.command, "result": result}, ensure_ascii=False, indent=2, default=str)
        if args.output_path:
            output = Path(args.output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    except Exception as exc:
        error = {"ok": False, "command": args.command, "error_type": type(exc).__name__, "error": str(exc)}
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
