"""DesignSpark product-display drafts. This module has no review submission code."""
from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from urllib.parse import urlparse

PROMPT_VERSION = "product-display-v1"
# Keep PROMPT_VERSION stable so existing saved jobs retain their recovery keys.
PROMPT_REVISION = "2026-11-05"
PROMPT = (Path(__file__).resolve().parents[1] / "assets" / "designspark-prompt.txt").read_text(encoding="utf-8").strip()


class LoginRequired(RuntimeError):
    pass


class GenerationPending(RuntimeError):
    pass


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def save_image(data: bytes, output: Path) -> dict:
    """Validate and save the provider's bytes without adding or removing content."""
    from PIL import Image

    if len(data) > 25_000_000:
        raise ValueError("Image exceeds 25 MB")
    with Image.open(io.BytesIO(data)) as loaded:
        loaded.load()
        width, height = loaded.size
        image_format = loaded.format
    if min(width, height) < 128:
        raise ValueError("Generated image is too small")
    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(image_format)
    if suffix is None:
        raise ValueError(f"Unsupported generated image format: {image_format}")
    output = output.with_suffix(suffix)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("Existing output must be verified and reused, not overwritten")
    temp = output.with_suffix(output.suffix + ".writing")
    temp.write_bytes(data)
    temp.replace(output)
    return {"path": str(output.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
            "width": width, "height": height, "format": image_format,
            "source_type": "ai_generated", "save_mode": "provider_original"}


def verified_output(record: dict) -> bool:
    from PIL import Image

    try:
        path = Path(record["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            return False
        with Image.open(path) as image:
            image.load()
            return image.size == (record["width"], record["height"]) and min(image.size) >= 128
    except (OSError, KeyError, ValueError):
        return False


class DesignSpark:
    def __init__(self, profile: Path, settings: dict, visible=False):
        self.profile = profile.resolve()
        self.settings = settings
        self.visible = visible
        self.context = None
        self.engine = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        self.engine = sync_playwright().start()
        try:
            self.context = self.engine.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=not self.visible,
                accept_downloads=True, viewport={"width": 1440, "height": 1000},
            )
        except Exception:
            self.engine.stop()
            raise
        return self

    def __exit__(self, *args):
        if self.context:
            self.context.close()
        if self.engine:
            self.engine.stop()

    def find(self, page, key, seconds=15, visible=True):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for selector in self.settings["selectors"][key]:
                locator = page.locator(selector)
                for index in range(min(locator.count(), 30) - 1, -1, -1):
                    item = locator.nth(index)
                    if not visible or item.is_visible():
                        return item
            time.sleep(0.25)
        return None

    def open_page(self, url=None):
        target = url or self.settings["url"]
        if urlparse(target).hostname != "designspark.jd.com":
            raise ValueError("Only the DesignSpark origin can be opened")
        page = self.context.new_page()
        page.goto(target, wait_until="domcontentloaded", timeout=45000)
        return page

    def probe(self, screenshot: Path | None = None) -> dict:
        page = self.open_page()
        try:
            ready = self.find(page, "new_chat", seconds=15) is not None
            host = urlparse(page.url).hostname
            if screenshot:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=False)
            return {"ready": ready, "page_host": host, "reason": "ready" if ready else ("login_required" if host != "designspark.jd.com" else "login_or_page_changed")}
        finally:
            page.close()

    def download_result(self, url: str, output: Path) -> dict:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".360buyimg.com"):
            raise ValueError("Expected a saved HTTPS JD image result")
        response = self.context.request.get(url, timeout=45000)
        if not response.ok:
            raise GenerationPending(f"Image download HTTP {response.status}; retain the result reference")
        result = save_image(response.body(), output)
        result["source_url"] = url
        return result

    def wait_for_result(self, page, output: Path, seconds: int, checkpoint=None) -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready = self.find(page, "generated_ready", seconds=1, visible=False)
            control = self.find(page, "download_control", seconds=1, visible=False) if ready is not None else None
            if control is not None:
                try:
                    image = control.locator("xpath=ancestor::*[.//img][1]//img").last
                    src = image.get_attribute("src") or ""
                    if src.startswith("https://"):
                        if checkpoint:
                            checkpoint("generation_pending", page.url, {"result_url": src})
                        return self.download_result(src, output)
                    image.hover(force=True)
                    with page.expect_download(timeout=15000) as info:
                        control.click(force=True)
                    return save_image(Path(info.value.path()).read_bytes(), output)
                except FileExistsError:
                    raise
                except Exception as exc:
                    raise GenerationPending("Generation may be complete but image download failed; resume the existing page") from exc
            for phrase in ("额度不足", "积分不足", "无权限操作", "暂无使用权限"):
                if page.get_by_text(phrase, exact=False).count():
                    raise GenerationPending("DesignSpark reported a quota or permission issue")
            time.sleep(0.5)
        raise GenerationPending("Timed out awaiting the existing generation; do not resubmit")

    def generate(self, source: Path, output: Path, checkpoint, existing_url=None, seconds=300, recovery=None):
        if recovery and recovery.get("result_url"):
            return self.download_result(recovery["result_url"], output)
        page = self.open_page(existing_url)
        def save_reference(request):
            parsed = urlparse(request.url)
            if parsed.hostname != "yhd-origin-llm.jd.com" or parsed.path != "/designspark/assets/save":
                return
            try:
                data = request.post_data_json
                images = data.get("resultImages", [])
                if len(images) != 1 or not images[0].get("imageUrl"):
                    return
                checkpoint("generation_pending", page.url, {
                    "result_url": images[0]["imageUrl"],
                    "asset_request_id": str(data.get("requestId", "")),
                })
            except (KeyError, TypeError, ValueError):
                return
        page.on("request", save_reference)
        try:
            if self.find(page, "new_chat", seconds=15) is None:
                raise LoginRequired("DesignSpark login required or page selectors changed")
            if existing_url is None:
                self.find(page, "new_chat", seconds=1).click()
                upload = self.find(page, "file_input", seconds=15, visible=False)
                if upload is None:
                    control = self.find(page, "upload_control", seconds=10)
                    if control is None:
                        raise RuntimeError("Reference-image upload control missing")
                    with page.expect_file_chooser(timeout=15000) as choice:
                        control.click()
                    choice.value.set_files(str(source))
                else:
                    upload.set_input_files(str(source))
                if self.find(page, "uploaded_reference", seconds=60) is None:
                    raise RuntimeError("Reference-image upload not confirmed")
                prompt = self.find(page, "prompt_input", seconds=15)
                if prompt is None:
                    raise RuntimeError("DesignSpark prompt field missing")
                prompt.fill(PROMPT)
                checkpoint("generation_started", page.url)
                prompt.press("Control+Enter")
                page.wait_for_timeout(1500)
                checkpoint("generation_pending", page.url)
            return self.wait_for_result(page, output, seconds, checkpoint=checkpoint)
        except Exception:
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output.parent / "designspark-diagnostic.png"), full_page=False)
            except Exception:
                pass
            raise
        finally:
            page.remove_listener("request", save_reference)
            page.close()
