#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description="JD image-search candidate extractor")
    parser.add_argument("--url", required=True, help="JD item URL")
    parser.add_argument("--profile", default="", help="Persistent browser profile dir")
    parser.add_argument("--channel", default="msedge", help="Playwright browser channel")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    profile = Path(args.profile).expanduser() if args.profile else Path.cwd() / ".browser_profile"
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel=args.channel,
            headless=args.headless,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            result = extract_candidates(page, args.url, args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            context.close()


def extract_candidates(page, source_url, limit):
    page.goto(source_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    image_url = read_main_image_url(page)
    if not image_url:
        return {"source_url": source_url, "status": "failed", "reason": "main image not found", "candidates": []}

    imgpath = to_imgpath(image_url)
    search_url = f"https://search.jd.com/image?imgpath={quote(imgpath, safe='/:')}&from=mainPlugin"
    data = None
    try:
        with page.expect_response(
            lambda response: "functionId=pc_search_image_search" in response.url,
            timeout=45000,
        ) as response_info:
            page.goto(search_url, wait_until="domcontentloaded")
        data = response_info.value.json()
    except Exception as exc:
        return {
            "source_url": source_url,
            "image_url": image_url,
            "search_url": search_url,
            "status": "failed",
            "reason": str(exc),
            "candidates": [],
        }

    candidates = parse_response(data)
    if limit > 0:
        candidates = candidates[:limit]
    return {
        "source_url": source_url,
        "image_url": image_url,
        "search_url": search_url,
        "status": "ok",
        "count": len(candidates),
        "candidates": candidates,
    }


def read_main_image_url(page):
    selectors = [
        "#spec-img",
        ".jqzoom img",
        "#preview [class*='jqzoom'] img",
        "#preview img[src*='360buyimg.com/n1']",
        "#preview img[src*='360buyimg.com/n5']",
        "#preview img[src*='360buyimg']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if not locator.count():
                continue
            src = (
                locator.get_attribute("src")
                or locator.get_attribute("currentSrc")
                or locator.get_attribute("data-lazy-img")
            )
            if src and not looks_like_placeholder(src):
                return normalize_image_url(src)
        except Exception:
            continue
    return ""


def to_imgpath(image_url):
    match = re.search(r"(jfs/.+)$", normalize_image_url(image_url))
    return match.group(1) if match else image_url


def normalize_image_url(url):
    if not url:
        return ""
    return "https:" + url if url.startswith("//") else url


def looks_like_placeholder(src):
    src = (src or "").lower()
    return any(word in src for word in ["blank", "placeholder", "loading", ".gif"])


def parse_response(data):
    payload = data.get("data") if isinstance(data, dict) else {}
    ware_list = find_first_list(payload, ["wareList", "wareInfoList", "searchWareList", "items", "list"])
    candidates = []
    seen = set()
    for item in ware_list:
        if not isinstance(item, dict):
            continue
        sku_id = re.sub(r"\D", "", str(first_value(item, ["wareId", "skuId", "sku", "pid", "itemId"]) or ""))
        if not sku_id or sku_id in seen:
            continue
        seen.add(sku_id)
        image_url = normalize_api_image(first_value(item, ["imageurl", "imageUrl", "imgUrl", "image", "picUrl"]))
        candidates.append(
            {
                "sku_id": sku_id,
                "url": f"https://item.jd.com/{sku_id}.html",
                "title": first_value(item, ["wname", "wareName", "skuName", "name", "title"]),
                "shop_name": first_value(item, ["shopName", "seller", "venderName"]),
                "price": first_value(item, ["newerPrice", "lowestPrice", "pPrice", "jdPrice", "price"]),
                "promo_price": first_value(item, ["lowestPrice", "newerPrice", "promotionPrice", "realPrice"]),
                "image_url": image_url,
            }
        )
    return candidates


def find_first_list(value, names):
    if isinstance(value, dict):
        for name in names:
            item = value.get(name)
            if isinstance(item, list):
                return item
        for item in value.values():
            found = find_first_list(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first_list(item, names)
            if found:
                return found
    return []


def first_value(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return ""


def normalize_api_image(url):
    url = str(url or "")
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("jfs/"):
        return "https://img10.360buyimg.com/n1/" + url
    return url


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
