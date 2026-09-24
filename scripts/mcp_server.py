from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import portable_cli


TOOLS = [
    {"name": "osw_pic_doctor", "description": "检查便携运行时、核心模块与登录态。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "osw_pic_userinfo", "description": "读取当前 ERP、带图评价权限和 AI 配额。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "easybi_pic_pending", "description": "从 EasyBI 带图评价数据看板查询指定 ERP 未完成带图评价的 SPU 明细；默认查询 T-1，无数据时回退 T-2。", "inputSchema": {"type": "object", "required": ["erp"], "properties": {"erp": {"type": "string"}, "target_date": {"type": "string", "description": "YYYY-MM-DD；省略时查询 T-1"}, "limit": {"type": "integer", "default": 5000, "minimum": 1, "maximum": 5000}, "fallback_days": {"type": "integer", "default": 1, "minimum": 0, "maximum": 7}}}},
    {"name": "osw_pic_targets", "description": "查询待补带图评价 SKU；默认仅无评价商品。", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}, "page_size": {"type": "integer", "default": 100}, "sku_ids": {"type": "array", "items": {"type": "string"}}, "spu_ids": {"type": "array", "items": {"type": "string"}}, "include_has_eval": {"type": "boolean", "default": False}, "max_pages": {"type": "integer", "default": 200}}}},
    {"name": "osw_llm_status", "description": "检查内网模型网关状态。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "osw_llm_image", "description": "文生图或图生图并把结果保存到本地。", "inputSchema": {"type": "object", "required": ["prompt"], "properties": {"prompt": {"type": "string"}, "source_images": {"type": "array", "items": {"type": "string"}}, "stem": {"type": "string"}, "out_dir": {"type": "string"}}}},
    {"name": "osw_pic_imgzone_upload", "description": "上传本地图片到图片空间并返回 CDN URL。", "inputSchema": {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}, "file_name": {"type": "string"}, "cate_id": {"type": "string", "default": "0"}}}},
    {"name": "osw_pic_imgzone_find", "description": "按文件名回读图片空间并用 GET 验活。", "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "cate_id": {"type": "string", "default": "0"}, "max_pages": {"type": "integer", "default": 5}}}},
    {"name": "osw_pic_ai_text", "description": "根据实拍图生成评价文案，不提交。", "inputSchema": {"type": "object", "required": ["sku_id", "images"], "properties": {"sku_id": {"type": "string"}, "images": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "osw_pic_real_review", "description": "模型无权限时，按已锁定 targets.items 采集同品真实晒单图文并执行严格质检，不提交。", "inputSchema": {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {"type": "object"}}, "output_dir": {"type": "string"}, "stem": {"type": "string"}, "headless": {"type": "boolean", "default": True}, "timeout": {"type": "integer", "default": 1800}}}},
    {"name": "osw_pic_screen", "description": "执行文本、图片和风险内容质检。", "inputSchema": {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "items": {"type": "object"}}, "require_text": {"type": "boolean", "default": True}, "allow_review": {"type": "boolean", "default": False}}}},
    {"name": "osw_pic_import_dryrun", "description": "提交前体检并返回 confirm_token，不产生真实提交。", "inputSchema": {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "items": {"type": "object"}}, "check_quota": {"type": "boolean", "default": True}, "screen_text": {"type": "boolean", "default": True}, "check_used": {"type": "boolean", "default": True}}}},
    {"name": "osw_pic_import", "description": "真实提交带图评价；必须使用 dry-run 返回的 confirm_token。", "inputSchema": {"type": "object", "required": ["rows", "confirm"], "properties": {"rows": {"type": "array", "items": {"type": "object"}}, "confirm": {"type": "string"}, "check_quota": {"type": "boolean", "default": True}, "screen_text": {"type": "boolean", "default": True}}}},
    {"name": "osw_pic_tasks", "description": "查询已提交任务及审核状态。", "inputSchema": {"type": "object", "properties": {"page": {"type": "integer", "default": 1}, "page_size": {"type": "integer", "default": 20}, "sku_ids": {"type": "array", "items": {"type": "string"}}, "spu_ids": {"type": "array", "items": {"type": "string"}}, "status": {"type": "integer"}, "input_erp": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}}}},
    {"name": "osw_pic_task_stats", "description": "汇总任务状态与 330 生效率。", "inputSchema": {"type": "object", "properties": {"sku_ids": {"type": "array", "items": {"type": "string"}}, "input_erp": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}, "scan_pages": {"type": "integer", "default": 20}}}},
]


HANDLERS = {
    "osw_pic_doctor": portable_cli.doctor,
    "osw_pic_userinfo": portable_cli.userinfo,
    "easybi_pic_pending": portable_cli.easybi_pending,
    "osw_pic_targets": portable_cli.targets,
    "osw_llm_status": portable_cli.llm_status,
    "osw_llm_image": portable_cli.llm_image,
    "osw_pic_imgzone_upload": portable_cli.imgzone_upload,
    "osw_pic_imgzone_find": portable_cli.imgzone_find,
    "osw_pic_ai_text": portable_cli.ai_text,
    "osw_pic_real_review": portable_cli.real_review_fallback,
    "osw_pic_screen": portable_cli.screen_rows,
    "osw_pic_import_dryrun": portable_cli.import_dryrun,
    "osw_pic_import": portable_cli.import_rows,
    "osw_pic_tasks": portable_cli.tasks,
    "osw_pic_task_stats": portable_cli.task_stats,
}


def response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        message["result"] = result
    else:
        message["error"] = error
    sys.stdout.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion") or "2024-11-05"
        response(request_id, {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "jd-pictured-review-portable", "version": "1.0.0"}})
        return
    if method == "ping":
        response(request_id, {})
        return
    if method == "tools/list":
        response(request_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            response(request_id, error={"code": -32601, "message": f"未知工具：{name}"})
            return
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = handler(dict(params.get("arguments") or {}))
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            response(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            text = json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False)
            response(request_id, {"content": [{"type": "text", "text": text}], "isError": True})
        return
    response(request_id, error={"code": -32601, "message": f"不支持的方法：{method}"})


def main() -> None:
    if "--self-test" in sys.argv:
        names = [tool["name"] for tool in TOOLS]
        missing = sorted(set(names) - set(HANDLERS))
        extra = sorted(set(HANDLERS) - set(names))
        if missing or extra or len(names) != len(set(names)):
            raise RuntimeError(f"MCP 工具映射异常：missing={missing}, extra={extra}")
        result = portable_cli.doctor({})
        print(json.dumps({"ok": True, "tool_count": len(TOOLS), "doctor": result}, ensure_ascii=False, indent=2))
        return
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                handle(message)
        except Exception as exc:
            print(f"MCP 输入处理失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
