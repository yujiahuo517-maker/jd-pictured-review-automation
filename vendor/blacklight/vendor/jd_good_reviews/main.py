#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import logging
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from openpyxl import Workbook, load_workbook
from playwright.sync_api import sync_playwright

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


HEADERS = [
    "SKUID",
    "商品名称",
    "评价文本",
    "实拍图1",
    "实拍图2",
    "实拍图3",
    "实拍图4",
    "实拍图5",
    "实拍图6",
    "实拍图7",
    "实拍图8",
    "实拍图9",
]

NEGATIVE_WORDS = [
    "差",
    "烂",
    "垃圾",
    "不好",
    "不值",
    "失望",
    "退货",
    "退款",
    "破损",
    "掉色",
    "异味",
    "缩水",
    "起球",
    "变形",
    "太薄",
    "太小",
    "太大",
    "不合适",
    "色差",
    "质量差",
    "做工差",
    "客服差",
    "物流慢",
    "瑕疵",
    "坏了",
    "断了",
    "开线",
    "不推荐",
    "不行",
    "不达标",
    "尺寸不达标",
    "不够",
    "少了",
    "少一",
    "没有反应",
    "催了",
    "态度不行",
    "快递小哥",
    "不是宣传",
    "不像正品",
    "品牌不对",
    "假货",
    "山寨",
    "与描述不符",
]

DEFAULT_REVIEW_TEXTS = [
    "此用户未填写评价",
    "默认好评",
    "好评",
    "评价方未及时做出评价",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="JD same-product good review image collector")
    parser.add_argument("--input", required=True, help="Input xlsx/csv/txt with SKUID and optional 商品名称")
    parser.add_argument("--output", required=True, help="Output xlsx")
    parser.add_argument("--config", default="config.yaml", help="Config yaml path")
    parser.add_argument("--user-data-dir", default="", help="Playwright persistent browser profile")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--headful", action="store_true", help="Run browser headful")
    args = parser.parse_args()

    work_dir = Path(__file__).resolve().parent
    config = load_config(work_dir / args.config)
    if args.user_data_dir:
        config["user_data_dir"] = args.user_data_dir
    if args.headless:
        config["headless"] = True
    if args.headful:
        config["headless"] = False

    data_dir = work_dir / "data"
    log_dir = work_dir / "logs"
    data_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)
    setup_logging(log_dir / "run.log")

    input_rows = read_input(Path(args.input))
    rows = dedupe_skus(input_rows)
    output_path = Path(args.output)
    progress_path = data_dir / "progress.json"
    used_images_path = data_dir / "used_images.json"
    progress = load_json(progress_path, {})
    used_images = load_used_images(used_images_path)

    ensure_output_workbook(output_path, config.get("template_path"))

    script_dir = resolve_path(work_dir, str(config["jd_image_search_script_dir"]))
    jd_image_search = load_jd_image_search(script_dir)

    with sync_playwright() as playwright:
        launch_args = extension_launch_args(work_dir, config)
        browser_channel = config.get("browser_channel") or "msedge"
        browser = None
        if bool(config.get("no_login_mode", False)):
            browser = playwright.chromium.launch(
                channel=browser_channel,
                headless=bool(config.get("headless", False)),
                args=launch_args,
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
        else:
            profile = resolve_path(work_dir, str(config["user_data_dir"]))
            profile.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=browser_channel,
                headless=bool(config.get("headless", False)),
                viewport={"width": 1440, "height": 1000},
                args=launch_args,
            )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for item in rows:
                sku = item["sku"]
                if progress.get(sku, {}).get("status") in {"success", "empty"}:
                    if output_has_sku(output_path, sku):
                        logging.info("skip completed sku=%s status=%s", sku, progress[sku]["status"])
                        continue
                    logging.info("progress exists but output row missing; reprocessing sku=%s", sku)
                result = process_sku(page, jd_image_search, sku, item.get("name", ""), config, used_images)
                append_or_update_output(output_path, result)
                progress[sku] = result["progress"]
                save_json(progress_path, progress)
                save_json(used_images_path, used_images)
                logging.info("sku=%s status=%s reason=%s", sku, result["progress"]["status"], result["progress"].get("reason", ""))
                time.sleep(float(config.get("request_delay", 0.2)))
        finally:
            context.close()
            if browser is not None:
                browser.close()

    return 0


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        if yaml is not None:
            return yaml.safe_load(f) or {}
        return parse_simple_yaml(f.read())


def parse_simple_yaml(text: str) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.lower() in {"true", "false"}:
            parsed: Any = value.lower() == "true"
        else:
            try:
                parsed = int(value)
            except ValueError:
                try:
                    parsed = float(value)
                except ValueError:
                    parsed = value.strip("'\"")
        config[key.strip()] = parsed
    return config


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


def extension_launch_args(base_dir: Path, config: dict[str, Any]) -> list[str]:
    if not bool(config.get("load_extension", False)):
        return []
    if bool(config.get("headless", False)):
        logging.warning("load_extension=true ignored in headless mode")
        return []
    extension_dir = resolve_path(base_dir, str(config.get("extension_dir", "")))
    if not extension_dir.exists():
        logging.warning("extension_dir not found: %s", extension_dir)
        return []
    return [
        f"--disable-extensions-except={extension_dir}",
        f"--load-extension={extension_dir}",
    ]


def setup_logging(path: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def read_input(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        header = [normalize_header(cell.value) for cell in ws[1]]
        sku_idx = find_column(header, ["skuid", "sku", "商品id", "商品编号"])
        name_idx = find_column(header, ["商品名称", "名称", "title", "name"])
        if sku_idx is None:
            sku_idx = 0
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            sku = normalize_sku(row[sku_idx] if sku_idx < len(row) else "")
            if not sku:
                continue
            name = str(row[name_idx] or "").strip() if name_idx is not None and name_idx < len(row) else ""
            rows.append({"sku": sku, "name": name})
        return rows
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(2048)
            f.seek(0)
            if "," not in sample and "\t" not in sample:
                return [{"sku": normalize_sku(line), "name": ""} for line in f if normalize_sku(line)]
            reader = csv.DictReader(f)
            rows = []
            for row in reader:
                sku = normalize_sku(first_present(row, ["SKUID", "sku", "SKU", "商品ID", "商品编号"]))
                if not sku and row:
                    sku = normalize_sku(next(iter(row.values())))
                if not sku:
                    continue
                name = str(first_present(row, ["商品名称", "名称", "title", "name"]) or "").strip()
                rows.append({"sku": sku, "name": name})
            return rows
    with path.open("r", encoding="utf-8") as f:
        return [{"sku": normalize_sku(line), "name": ""} for line in f if normalize_sku(line)]


def normalize_header(value: Any) -> str:
    return str(value or "").strip().lower()


def find_column(headers: list[str], names: list[str]) -> int | None:
    wanted = {name.lower() for name in names}
    for idx, header in enumerate(headers):
        if header in wanted:
            return idx
    return None


def first_present(row: dict[str, Any], names: list[str]) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name in row:
            return row[name]
        if name.lower() in lower:
            return lower[name.lower()]
    return ""


def normalize_sku(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return re.sub(r"\D", "", text)


def dedupe_skus(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result = []
    for row in rows:
        sku = row["sku"]
        if sku in seen:
            logging.warning("duplicate sku skipped: %s", sku)
            continue
        seen.add(sku)
        result.append(row)
    return result


def ensure_output_workbook(output_path: Path, template_path: str | None) -> None:
    if output_path.exists():
        return
    if template_path and Path(template_path).exists():
        shutil.copyfile(template_path, output_path)
        wb = load_workbook(output_path)
        ws = wb.active
        for row_idx in range(ws.max_row, 1, -1):
            ws.delete_rows(row_idx)
        for col, header in enumerate(HEADERS, 1):
            ws.cell(1, col).value = header
        wb.save(output_path)
        return
    wb = Workbook()
    ws = wb.active
    ws.title = "批量导入模板"
    ws.append(HEADERS)
    wb.save(output_path)


def load_jd_image_search(script_dir: Path):
    path = script_dir / "jd_image_search.py"
    spec = importlib.util.spec_from_file_location("jd_image_search", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process_sku(page, jd_image_search, sku: str, source_name: str, config: dict[str, Any], used_images: dict[str, Any]) -> dict[str, Any]:
    source_url = f"https://item.jd.com/{sku}.html"
    source_name = source_name or fetch_source_product_name_http(sku)
    progress = {"status": "empty", "source_sku": sku, "source_url": source_url, "checked": [], "skipped": []}
    try:
        if bool(config.get("fast_same_product_search", True)):
            candidates_result = extract_candidates_fast(
                page,
                jd_image_search,
                source_url,
                int(config.get("max_same_products_per_sku", 30)),
                config,
            )
            if candidates_result.get("status") != "ok" and bool(config.get("same_product_fallback_slow", True)):
                logging.warning(
                    "fast same-product search failed sku=%s reason=%s; retrying slow path",
                    sku,
                    candidates_result.get("reason", ""),
                )
                candidates_result = jd_image_search.extract_candidates(
                    page,
                    source_url,
                    int(config.get("max_same_products_per_sku", 30)),
                )
        else:
            candidates_result = jd_image_search.extract_candidates(page, source_url, int(config.get("max_same_products_per_sku", 30)))
    except Exception as exc:
        candidates_result = {"status": "failed", "reason": str(exc), "candidates": []}

    if candidates_result.get("status") != "ok":
        reason = candidates_result.get("reason") or "same product search failed"
        progress["reason"] = reason
        source_name = source_name or str(candidates_result.get("source_title") or "")
        return empty_result(sku, source_name, progress)

    source_name = source_name or str(candidates_result.get("source_title") or "")

    candidates = []
    for candidate in candidates_result.get("candidates", []):
        if should_skip_candidate(candidate, bool(config.get("skip_jingxi", True))):
            progress["skipped"].append({"sku": candidate.get("sku_id"), "reason": "京喜商品/店铺", "candidate": candidate})
            continue
        candidates.append(candidate)

    if not candidates:
        progress["reason"] = "无非京喜同品候选"
        return empty_result(sku, source_name, progress)

    candidates = rank_and_filter_candidates(candidates, config, progress)
    if bool(config.get("fast_interface_mode", True)):
        found = find_review_for_candidates_fast(candidates, config, used_images)
        progress["checked"].extend(found["logs"])
        if found.get("review"):
            candidate = found["candidate"]
            return success_result(sku, source_name, candidate, found["review"], progress, used_images)
    else:
        for candidate in candidates:
            found = find_review_for_candidate(candidate, config, used_images)
            progress["checked"].append(found["log"])
            if found.get("review"):
                return success_result(sku, source_name, candidate, found["review"], progress, used_images)
            time.sleep(float(config.get("request_delay", 0.2)))

    # A successful image-search response can still contain only products with
    # no usable晒单.  Previously keyword fallback was attempted only when the
    # image-search request itself failed, leaving these valid targets stuck.
    # Try title-search candidates as a second phase while keeping the same
    # 京喜 exclusion, review validation and global image-dedupe gates.
    if bool(config.get("keyword_search_fallback", True)):
        checked_ids = {str(item.get("sku") or item.get("same_product_sku") or "") for item in progress["checked"]}
        keyword_candidates = []
        for candidate in keyword_search_candidates(page, source_name, int(config.get("max_same_products_per_sku", 30)), config):
            candidate_id = str(candidate.get("sku_id") or "")
            if not candidate_id or candidate_id in checked_ids:
                continue
            if should_skip_candidate(candidate, bool(config.get("skip_jingxi", True))):
                progress["skipped"].append({"sku": candidate_id, "reason": "京喜商品/店铺", "candidate": candidate})
                continue
            keyword_candidates.append(candidate)
        keyword_candidates = rank_and_filter_candidates(keyword_candidates, config, progress)
        if keyword_candidates:
            found = find_review_for_candidates_fast(keyword_candidates, config, used_images)
            progress["checked"].extend(found["logs"])
            if found.get("review"):
                candidate = found["candidate"]
                return success_result(sku, source_name, candidate, found["review"], progress, used_images)

    progress["reason"] = "非京喜同品候选未找到可用晒图好评"
    return empty_result(sku, source_name, progress)


def success_result(
    sku: str,
    source_name: str,
    candidate: dict[str, Any],
    review: dict[str, Any],
    progress: dict[str, Any],
    used_images: dict[str, Any],
) -> dict[str, Any]:
    for key in review["image_keys"]:
        used_images["url_keys"].append(key)
    for digest in review.get("image_hashes", []):
        used_images["hashes"].append(digest)
    progress.update(
        {
            "status": "success",
            "same_product_sku": candidate.get("sku_id"),
            "same_product_url": candidate.get("url"),
            "same_product_title": candidate.get("title"),
            "shop_name": candidate.get("shop_name"),
            "review_text": review["text"],
            "images": review["images"],
        }
    )
    return row_result(sku, source_name, review["text"], review["images"], progress)


def should_skip_candidate(candidate: dict[str, Any], skip_jingxi: bool) -> bool:
    if not skip_jingxi:
        return False
    text = " ".join(str(candidate.get(k) or "") for k in ["title", "shop_name", "url"]).lower()
    return any(word in text for word in ["京喜", "jingxi"])


def rank_and_filter_candidates(candidates: list[dict[str, Any]], config: dict[str, Any], progress: dict[str, Any]) -> list[dict[str, Any]]:
    max_candidates = int(config.get("max_same_products_per_sku", 30))
    candidates = candidates[:max_candidates]
    if not bool(config.get("high_sales_mode", True)):
        return candidates

    for idx, candidate in enumerate(candidates):
        candidate["_original_rank"] = idx
        candidate["_popularity_score"] = candidate_popularity_score(candidate)

    ranked = sorted(candidates, key=lambda item: (-int(item.get("_popularity_score") or 0), int(item.get("_original_rank") or 0)))
    min_score = int(config.get("min_popularity_score", 0))
    if min_score > 0:
        hot = [candidate for candidate in ranked if int(candidate.get("_popularity_score") or 0) >= min_score]
    else:
        hot = ranked

    limit = int(config.get("max_hot_candidates_to_check", 8))
    selected = hot[:limit]
    selected_ids = {candidate.get("sku_id") for candidate in selected}
    for candidate in ranked:
        if candidate.get("sku_id") not in selected_ids:
            progress["skipped"].append(
                {
                    "sku": candidate.get("sku_id"),
                    "reason": "低热度候选跳过",
                    "popularity_score": candidate.get("_popularity_score"),
                    "popularity_text": candidate.get("popularity_text", ""),
                    "candidate": compact_candidate(candidate),
                }
            )
    return selected


def compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    keep = ["sku_id", "url", "title", "shop_name", "price", "promo_price", "popularity_text", "_popularity_score"]
    return {key: candidate.get(key) for key in keep if key in candidate}


def candidate_popularity_score(candidate: dict[str, Any]) -> int:
    text = " ".join(str(candidate.get(key) or "") for key in ["popularity_text", "sales_text", "comment_text", "good_rate_text"])
    score = 0
    for match in re.finditer(r"(?:已售|销量|售出)\s*([0-9.]+)\s*(万)?\+?", text):
        value = float(match.group(1))
        score += int(value * (10000 if match.group(2) else 1))
    for match in re.finditer(r"([0-9.]+)\s*(万)?\s*(?:人种草|条评价|评价|评论|人看过)", text):
        value = float(match.group(1))
        score += int(value * (10000 if match.group(2) else 1))
    for match in re.finditer(r"([0-9.]+)\s*%\s*好评", text):
        value = float(match.group(1))
        if value >= 95:
            score += 1000
    for key in ["commentCount", "comment_count", "comments", "commentNum", "evaluateCount", "sales", "saleCount", "soldCount"]:
        value = parse_number(candidate.get(key))
        if value:
            score += value
    if not score:
        # Keep API order as a fallback; image search often ranks better matches first.
        score = max(1, 100 - int(candidate.get("_original_rank") or 0))
    return score


def parse_number(value: Any) -> int:
    text = str(value or "")
    match = re.search(r"([0-9.]+)\s*(万)?", text)
    if not match:
        return 0
    number = float(match.group(1))
    return int(number * (10000 if match.group(2) else 1))


def extract_candidates_fast(page, jd_image_search, source_url: str, limit: int, config: dict[str, Any]) -> dict[str, Any]:
    page.set_default_timeout(int(config.get("product_page_timeout", 6000)))
    page.goto(source_url, wait_until="domcontentloaded", timeout=int(config.get("product_page_timeout", 6000)))
    for selector in ["#spec-img", "#preview img[src*='360buyimg']", ".jqzoom img"]:
        try:
            page.wait_for_selector(selector, timeout=int(config.get("product_page_timeout", 6000)))
            break
        except Exception:
            continue

    source_title = read_source_product_name(page)
    image_url = jd_image_search.read_main_image_url(page)
    if not image_url:
        sku = normalize_sku(source_url)
        image_url = fetch_source_main_image_http(sku)
    if not image_url:
        return {
            "source_url": source_url,
            "source_title": source_title,
            "status": "failed",
            "reason": "main image not found",
            "candidates": [],
        }

    imgpath = jd_image_search.to_imgpath(image_url)
    if bool(config.get("plugin_image_search_mode", True)):
        return extract_candidates_via_plugin_image_page(page, jd_image_search, source_url, source_title, image_url, imgpath, limit, config)

    search_url = f"https://search.jd.com/image?imgpath={jd_image_search.quote(imgpath, safe='/:')}&from=mainPlugin"
    try:
        with page.expect_response(
            lambda response: "functionId=pc_search_image_search" in response.url,
            timeout=int(config.get("same_product_response_timeout", 25000)),
        ) as response_info:
            page.goto(search_url, wait_until="domcontentloaded", timeout=int(config.get("same_product_response_timeout", 25000)))
        data = response_info.value.json()
    except Exception as exc:
        if bool(config.get("dom_fallback_on_search_timeout", True)):
            dom_candidates = parse_search_dom_candidates(page, limit, config)
            if dom_candidates:
                return {
                    "source_url": source_url,
                    "source_title": source_title,
                    "image_url": image_url,
                    "search_url": search_url,
                    "status": "ok",
                    "count": len(dom_candidates),
                    "source": "dom_fallback",
                    "network_reason": str(exc),
                    "candidates": dom_candidates,
                }
        if bool(config.get("keyword_search_fallback", True)):
            keyword_candidates = keyword_search_candidates(page, source_title, limit, config)
            if keyword_candidates:
                return {
                    "source_url": source_url,
                    "source_title": source_title,
                    "image_url": image_url,
                    "search_url": search_url,
                    "status": "ok",
                    "count": len(keyword_candidates),
                    "source": "keyword_search_fallback",
                    "network_reason": str(exc),
                    "candidates": keyword_candidates,
                }
        return {
            "source_url": source_url,
            "source_title": source_title,
            "image_url": image_url,
            "search_url": search_url,
            "status": "failed",
            "reason": str(exc),
            "candidates": [],
        }

    candidates = parse_response_rich(data, jd_image_search)
    if limit > 0:
        candidates = candidates[:limit]
    return {
        "source_url": source_url,
        "source_title": source_title,
        "image_url": image_url,
        "search_url": search_url,
        "status": "ok",
        "count": len(candidates),
        "candidates": candidates,
    }


def extract_candidates_via_plugin_image_page(
    page,
    jd_image_search,
    source_url: str,
    source_title: str,
    image_url: str,
    imgpath: str,
    limit: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    search_url = "https://jd-pc-search-image-pro.pf.jd.com/"
    timeout = int(config.get("same_product_response_timeout", 12000))
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        try:
            with page.expect_response(
                lambda response: "functionId=pc_search_image_search" in response.url,
                timeout=timeout,
            ) as response_info:
                page.evaluate(
                    """(imgpath) => {
                      window.postMessage({
                        type: 'DRAWER_STATE',
                        visible: true,
                        imgpath,
                        from: 'mainPlugin',
                        currentVersion: '1.0.9'
                      }, '*')
                    }""",
                    imgpath,
                )
            data = response_info.value.json()
            candidates = parse_response_rich(data, jd_image_search)
        except Exception as exc:
            close_login_popup_if_present(page)
            candidates = parse_search_dom_candidates(page, limit, config)
            if not candidates:
                raise exc
        if limit > 0:
            candidates = candidates[:limit]
        return {
            "source_url": source_url,
            "source_title": source_title,
            "image_url": image_url,
            "search_url": search_url,
            "status": "ok",
            "count": len(candidates),
            "source": "plugin_image_page",
            "candidates": candidates,
        }
    except Exception as exc:
        return {
            "source_url": source_url,
            "source_title": source_title,
            "image_url": image_url,
            "search_url": search_url,
            "status": "failed",
            "reason": str(exc),
            "candidates": [],
        }


def close_login_popup_if_present(page) -> None:
    selectors = [
        ".login-modal-close",
        ".dialog-close",
        ".close",
        "[class*='close']",
        "text=×",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).last
            if locator.count():
                locator.click(timeout=800)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


def parse_response_rich(data: dict[str, Any], jd_image_search) -> list[dict[str, Any]]:
    candidates = jd_image_search.parse_response(data)
    payload = data.get("data") if isinstance(data, dict) else {}
    raw_list = jd_image_search.find_first_list(payload, ["wareList", "wareInfoList", "searchWareList", "items", "list"])
    raw_by_sku: dict[str, dict[str, Any]] = {}
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        sku_id = re.sub(r"\D", "", str(jd_image_search.first_value(item, ["wareId", "skuId", "sku", "pid", "itemId"]) or ""))
        if sku_id:
            raw_by_sku[sku_id] = item
    for candidate in candidates:
        raw = raw_by_sku.get(str(candidate.get("sku_id") or ""), {})
        enrich_candidate(candidate, raw)
    return candidates


def parse_search_dom_candidates(page, limit: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    wait_ms = int(config.get("dom_parse_wait_ms", 2500))
    try:
        page.wait_for_timeout(wait_ms)
    except Exception:
        pass
    script = """
    () => {
      const seen = new Set();
      const anchors = [...document.querySelectorAll('a[href*="item.jd.com/"]')];
      const rows = [];
      for (const a of anchors) {
        const href = a.href || '';
        const m = href.match(/item\\.jd\\.com\\/(\\d+)\\.html/);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);
        let node = a;
        for (let i = 0; i < 6 && node && node.parentElement; i++) {
          const text = (node.innerText || '').trim();
          if (/已售|好评|人看过|人种草|￥|京东自营|京喜自营/.test(text) && text.length > 20) break;
          node = node.parentElement;
        }
        const text = (node && node.innerText || a.innerText || '').replace(/\\s+/g, ' ').trim();
        const img = node && node.querySelector('img');
        rows.push({
          sku_id: m[1],
          url: `https://item.jd.com/${m[1]}.html`,
          title: (a.innerText || '').replace(/\\s+/g, ' ').trim() || text.slice(0, 80),
          shop_name: '',
          price: '',
          promo_price: '',
          image_url: img ? (img.currentSrc || img.src || '') : '',
          popularity_text: text,
        });
        if (rows.length >= 80) break;
      }
      return rows;
    }
    """
    try:
        candidates = page.evaluate(script)
    except Exception as exc:
        logging.warning("DOM candidate parse failed: %s", exc)
        return []
    cleaned = []
    seen = set()
    for item in candidates or []:
        sku_id = normalize_sku(item.get("sku_id"))
        if not sku_id or sku_id in seen:
            continue
        seen.add(sku_id)
        item["sku_id"] = sku_id
        item["url"] = f"https://item.jd.com/{sku_id}.html"
        cleaned.append(item)
        if limit and len(cleaned) >= limit:
            break
    return cleaned


def keyword_search_candidates(page, source_title: str, limit: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    keyword = build_search_keyword(source_title)
    if not keyword:
        return []
    # Long marketplace titles are often an exact variant plus many attributes.
    # If that exact query has no usable review, progressively shorten it so the
    # fallback can discover more same-kind products while downstream screening,
    # image-only requirements and dedupe checks remain unchanged.
    queries = []
    # Variant names are commonly placed at the very end of JD titles (for
    # example "花篮星星人").  Include suffix queries so those exact variants
    # are not lost when only the common title prefix is searched.
    tokens = [token for token in re.split(r"\s+", keyword) if token]
    suffix = tokens[-1] if tokens else ""
    for value in (suffix, suffix[-8:], keyword, keyword[:32], keyword[:20], keyword[:12]):
        value = value.strip()
        if value and value not in queries:
            queries.append(value)
    candidates = []
    seen = set()
    for query in queries:
        # JD desktop search uses odd-numbered page indexes for successive
        # result pages. Niche variants often have no reviewed listing on the
        # first page, so inspect a few more pages before broadening the query.
        for search_page in (1, 3, 5):
            start = (search_page - 1) * 30 + 1
            url = f"https://search.jd.com/Search?keyword={quote(query)}&enc=utf-8&psort=3&page={search_page}&s={start}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(int(config.get("dom_parse_wait_ms", 2500)))
            except Exception as exc:
                logging.warning("keyword search failed keyword=%s page=%s error=%s", query, search_page, exc)
                continue
            for item in parse_jd_search_candidates(page, limit):
                sku_id = str(item.get("sku_id") or "")
                if not sku_id or sku_id in seen:
                    continue
                seen.add(sku_id)
                candidates.append(item)
                if limit and len(candidates) >= limit:
                    return candidates
    return candidates


def build_search_keyword(source_title: str) -> str:
    text = normalize_space(source_title)
    text = re.sub(r"【[^】]*】", " ", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*[*xX×]\s*\d+(?:\.\d+)?\s*cm\b", " ", text, flags=re.I)
    text = re.sub(r"[（）()【】\\[\\],，:：;；/\\\\]", " ", text)
    tokens = [token for token in re.split(r"\s+", text) if token]
    keyword = " ".join(tokens[:8])
    return keyword[:80]


def parse_jd_search_candidates(page, limit: int) -> list[dict[str, Any]]:
    script = """
    () => [...document.querySelectorAll('a[href*="chat.jd.com/index.action"]')].map(a => a.href)
    """
    try:
        hrefs = page.evaluate(script)
    except Exception as exc:
        logging.warning("keyword DOM parse failed: %s", exc)
        return []
    candidates = []
    seen = set()
    for href in hrefs or []:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        sku_id = normalize_sku(first_param(params, "pid"))
        if not sku_id or sku_id in seen:
            continue
        seen.add(sku_id)
        title = decode_repeated(first_param(params, "wname"))
        seller = decode_repeated(first_param(params, "seller"))
        img_path = decode_repeated(first_param(params, "imgUrl"))
        comment_num = decode_repeated(first_param(params, "commentNum"))
        good_rate = decode_repeated(first_param(params, "evaluationRate"))
        image_url = "https://img10.360buyimg.com/n1/" + img_path if img_path.startswith("jfs/") else img_path
        popularity = " ".join(part for part in [f"{comment_num}评价" if comment_num else "", f"{good_rate}%好评" if good_rate else ""] if part)
        candidates.append(
            {
                "sku_id": sku_id,
                "url": f"https://item.jd.com/{sku_id}.html",
                "title": title,
                "shop_name": seller,
                "price": "",
                "promo_price": "",
                "image_url": image_url,
                "commentNum": comment_num,
                "goodRate": good_rate,
                "popularity_text": popularity,
            }
        )
        if limit and len(candidates) >= limit:
            break
    return candidates


def first_param(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or []
    return values[0] if values else ""


def decode_repeated(value: str) -> str:
    text = str(value or "")
    for _ in range(3):
        new_text = unquote(text)
        if new_text == text:
            break
        text = new_text
    return text


def enrich_candidate(candidate: dict[str, Any], raw: dict[str, Any]) -> None:
    popularity_fields = [
        "sales",
        "saleCount",
        "soldCount",
        "commentCount",
        "commentNum",
        "evaluateCount",
        "goodRate",
        "goodRateShow",
        "goodCommentRate",
        "tagText",
        "comment",
        "ext",
    ]
    for key in popularity_fields:
        if key in raw and raw[key] not in (None, ""):
            candidate[key] = raw[key]
    text = flatten_scalar_text(raw)
    useful = []
    for piece in text:
        if re.search(r"已售|销量|售出|好评|评价|评论|人看过|人种草|[0-9.]+\s*万", piece):
            useful.append(piece)
    candidate["popularity_text"] = " ".join(useful[:20])


def flatten_scalar_text(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            result.extend(flatten_scalar_text(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(flatten_scalar_text(item))
    elif isinstance(value, (str, int, float)):
        text = normalize_space(str(value))
        if text:
            result.append(text)
    return result


def read_source_product_name(page) -> str:
    selectors = [
        ".sku-name",
        "#name .sku-name",
        ".itemInfo-wrap .sku-name",
        "h1",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if not locator.count():
                continue
            text = normalize_space(locator.inner_text(timeout=1500))
            if text:
                return text
        except Exception:
            continue
    try:
        title = normalize_space(page.title())
        if "最小单价计算器" in title:
            return ""
        title = re.sub(r"【行情 报价 价格 评测】-京东$", "", title)
        title = clean_jd_product_title(title)
        return title
    except Exception:
        return ""


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def fetch_source_product_name_http(sku: str) -> str:
    url = f"https://item.jd.com/{sku}.html"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    except Exception as exc:
        logging.warning("source title fetch failed sku=%s error=%s", sku, exc)
        return ""
    match = re.search(r"<title>(.*?)</title>", html, flags=re.S | re.I)
    if not match:
        return ""
    return clean_jd_product_title(normalize_space(match.group(1)))


def fetch_source_main_image_http(sku: str) -> str:
    url = f"https://item.m.jd.com/product/{sku}.html"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    except Exception as exc:
        logging.warning("source image fetch failed sku=%s error=%s", sku, exc)
        return ""
    candidates: list[tuple[int, str]] = []
    for match in re.finditer(r"jfs/[^\"']+?\.(?:jpg|jpeg|png|avif)", html, flags=re.I):
        path = match.group(0)
        if any(skip in path for skip in ["imagetools", "jdphoto"]):
            continue
        parts = path.split("/")
        size = int(parts[-3]) if len(parts) >= 3 and parts[-3].isdigit() else 0
        if size >= 50000:
            candidates.append((size, path))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return "https://img10.360buyimg.com/n1/" + candidates[0][1]


def clean_jd_product_title(title: str) -> str:
    title = re.sub(r"【图片\s*价格\s*品牌\s*报价】-京东$", "", title)
    title = re.sub(r"【行情\s*报价\s*价格\s*评测】-京东$", "", title)
    title = re.sub(r"[-_]?京东.*$", "", title)
    return title.strip()


def find_review_for_candidate(candidate: dict[str, Any], config: dict[str, Any], used_images: dict[str, Any]) -> dict[str, Any]:
    sku = str(candidate.get("sku_id") or "")
    max_reviews = int(config.get("max_reviews_per_product", 30))
    page_size = 10
    max_pages = max(1, (max_reviews + page_size - 1) // page_size)
    log = {
        "same_product_sku": sku,
        "same_product_url": candidate.get("url"),
        "title": candidate.get("title"),
        "shop_name": candidate.get("shop_name"),
        "comments_seen": 0,
        "image_comments": 0,
        "skip_reasons": [],
    }
    for page_num in range(max_pages):
        try:
            data = fetch_comment_page(sku, page_num, page_size)
        except Exception as exc:
            log["skip_reasons"].append(f"评论接口失败: {exc}")
            break
        comments = data.get("comments") or []
        log["imageListCount"] = data.get("imageListCount")
        if not comments:
            break
        for comment in comments:
            log["comments_seen"] += 1
            review = validate_comment(comment, config, used_images, log)
            if review:
                return {"review": review, "log": log}
            if log["comments_seen"] >= max_reviews:
                break
        if log["comments_seen"] >= max_reviews:
            break
    return {"review": None, "log": log}


def find_review_for_candidates_fast(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    used_images: dict[str, Any],
) -> dict[str, Any]:
    workers = max(1, int(config.get("comment_workers", 10)))
    indexed = list(enumerate(candidates))
    logs_by_index: dict[int, dict[str, Any]] = {}
    reviews_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(find_review_for_candidate, candidate, config, used_images): (idx, candidate)
            for idx, candidate in indexed
        }
        for future in as_completed(future_map):
            idx, _candidate = future_map[future]
            try:
                found = future.result()
            except Exception as exc:
                found = {
                    "review": None,
                    "log": {
                        "same_product_sku": _candidate.get("sku_id"),
                        "same_product_url": _candidate.get("url"),
                        "title": _candidate.get("title"),
                        "shop_name": _candidate.get("shop_name"),
                        "comments_seen": 0,
                        "image_comments": 0,
                        "skip_reasons": [f"并发评论检查失败: {exc}"],
                    },
                }
            logs_by_index[idx] = found["log"]
            if found.get("review"):
                reviews_by_index[idx] = found["review"]

    logs = [logs_by_index[idx] for idx, _candidate in indexed if idx in logs_by_index]
    for idx, candidate in indexed:
        review = reviews_by_index.get(idx)
        if review:
            return {"review": review, "candidate": candidate, "logs": logs}
    return {"review": None, "candidate": None, "logs": logs}


def fetch_comment_page(sku: str, page: int, page_size: int) -> dict[str, Any]:
    url = (
        "https://club.jd.com/comment/skuProductPageComments.action"
        f"?productId={sku}&score=4&sortType=5&page={page}&pageSize={page_size}&isShadowSku=0&fold=1"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://item.jd.com/{sku}.html"})
    raw = urlopen(req, timeout=15).read()
    return json.loads(raw.decode("utf-8"))


def validate_comment(comment: dict[str, Any], config: dict[str, Any], used_images: dict[str, Any], log: dict[str, Any]) -> dict[str, Any] | None:
    text = str(comment.get("content") or comment.get("vcontent") or "").strip()
    images = comment.get("images") or []
    if images:
        log["image_comments"] += 1
    ok, reason = validate_text(text)
    if not ok:
        log["skip_reasons"].append(reason)
        return None
    if int(comment.get("score") or 0) < 4:
        log["skip_reasons"].append("星级不足")
        return None
    if len(images) < int(config.get("min_images_required", 1)):
        log["skip_reasons"].append("无图")
        return None

    selected_urls = []
    selected_keys = []
    selected_hashes = []
    used_url_keys = set(used_images.get("url_keys", []))
    used_hashes = set(used_images.get("hashes", []))
    for image in images[:9]:
        raw = image.get("imgUrl") if isinstance(image, dict) else str(image)
        large_url = to_large_jd_image_url(raw)
        key = normalize_image_key(large_url)
        if not key:
            continue
        if key in used_url_keys or key in selected_keys:
            log["skip_reasons"].append(f"图片URL重复: {key}")
            return None
        digest = ""
        if bool(config.get("enable_image_hash_dedupe", True)):
            digest = fetch_image_sha256(large_url)
            if digest and (digest in used_hashes or digest in selected_hashes):
                log["skip_reasons"].append(f"图片内容重复: {key}")
                return None
        selected_urls.append(large_url)
        selected_keys.append(key)
        if digest:
            selected_hashes.append(digest)

    if len(selected_urls) < int(config.get("min_images_required", 1)):
        log["skip_reasons"].append("图片链接无效")
        return None
    return {"text": text, "images": selected_urls, "image_keys": selected_keys, "image_hashes": selected_hashes}


def validate_text(text: str) -> tuple[bool, str]:
    if len(text) < 12:
        return False, "评价文本太短"
    if any(default in text for default in DEFAULT_REVIEW_TEXTS):
        return False, "默认/空评价"
    hits = find_negative_hits(text)
    if hits:
        return False, "负面词: " + ",".join(hits)
    return True, ""


def find_negative_hits(text: str) -> list[str]:
    protected = str(text or "")
    for phrase in ["没有色差", "无色差", "没色差", "不存在色差"]:
        protected = protected.replace(phrase, "")
    return [word for word in NEGATIVE_WORDS if word in protected]


def to_large_jd_image_url(url: str) -> str:
    url = normalize_protocol(url)
    parsed = urlparse(url)
    path = re.sub(r"/s\d+x\d+_", "/", parsed.path)
    return f"https://{parsed.netloc}{path}"


def normalize_image_key(url: str) -> str:
    url = normalize_protocol(url)
    parsed = urlparse(url)
    path = re.sub(r"/s\d+x\d+_", "/", parsed.path)
    match = re.search(r"/(jfs/.+)$", path)
    return match.group(1) if match else path


def normalize_protocol(url: str) -> str:
    url = str(url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def fetch_image_sha256(url: str) -> str:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urlopen(req, timeout=12).read()
        return hashlib.sha256(data).hexdigest()
    except Exception as exc:
        logging.warning("image hash failed url=%s error=%s", url, exc)
        return ""


def row_result(sku: str, product_name: str, text: str, images: list[str], progress: dict[str, Any]) -> dict[str, Any]:
    row = [sku, product_name, text] + images[:9] + [""] * (9 - len(images[:9]))
    return {"row": row, "progress": progress}


def empty_result(sku: str, product_name: str, progress: dict[str, Any]) -> dict[str, Any]:
    progress.setdefault("status", "empty")
    row = [sku, product_name, ""] + [""] * 9
    return {"row": row, "progress": progress}


def append_or_update_output(output_path: Path, result: dict[str, Any]) -> None:
    wb = load_workbook(output_path)
    ws = wb.active
    for col, header in enumerate(HEADERS, 1):
        ws.cell(1, col).value = header
    sku = str(result["row"][0])
    target_row = None
    for row_idx in range(2, ws.max_row + 1):
        if str(ws.cell(row_idx, 1).value or "").strip() == sku:
            target_row = row_idx
            break
    if target_row is None:
        target_row = ws.max_row + 1
    for col, value in enumerate(result["row"], 1):
        ws.cell(target_row, col).value = value
    wb.save(output_path)


def output_has_sku(output_path: Path, sku: str) -> bool:
    if not output_path.exists():
        return False
    try:
        wb = load_workbook(output_path, read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
            if str(row[0] or "").strip() == str(sku):
                return True
    except Exception as exc:
        logging.warning("output sku check failed path=%s sku=%s error=%s", output_path, sku, exc)
    return False


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_used_images(path: Path) -> dict[str, list[str]]:
    data = load_json(path, {})
    return {
        "url_keys": list(dict.fromkeys(data.get("url_keys", []))),
        "hashes": list(dict.fromkeys(data.get("hashes", []))),
    }


if __name__ == "__main__":
    raise SystemExit(main())
