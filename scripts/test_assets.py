import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, PngImagePlugin
from designspark_client import DesignSpark, save_image, verified_output
from maintain_assets import fingerprint, workspace_lock


class AssetTests(unittest.TestCase):
    def test_original_bytes_dimensions_metadata_and_integrity(self):
        source = io.BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Software", "Provider metadata")
        Image.new("RGB", (720, 960), "white").save(source, format="PNG", pnginfo=metadata)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "draft.png"
            record = save_image(source.getvalue(), target)
            self.assertTrue(verified_output(record))
            self.assertEqual(target.read_bytes(), source.getvalue())
            with Image.open(target) as image:
                self.assertEqual(image.size, (720, 960))
                self.assertEqual(image.info, {"Software": "Provider metadata"})
            with self.assertRaises(FileExistsError):
                save_image(source.getvalue(), target)
            target.write_bytes(b"changed")
            self.assertFalse(verified_output(record))

    def test_non_image_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Exception):
                save_image(b"<html>sign in</html>", Path(folder) / "draft.png")

    def test_saved_result_resumes_download_without_opening_generation(self):
        source = io.BytesIO()
        Image.new("RGB", (720, 960), "white").save(source, format="PNG")
        with tempfile.TemporaryDirectory() as folder:
            client = DesignSpark(Path(folder), {})
            calls = []
            def get(url, **kwargs):
                calls.append(url)
                return SimpleNamespace(ok=True, body=lambda: source.getvalue())
            client.context = SimpleNamespace(request=SimpleNamespace(get=get))
            client.open_page = lambda *args: self.fail("Recovering an image must not open or submit generation")
            url = "https://img12.360buyimg.com/shine/saved-result.png"
            result = client.generate(Path(folder) / "source.png", Path(folder) / "result.png",
                                     lambda *args: None, existing_url="https://designspark.jd.com/generate?generateId=old",
                                     recovery={"result_url": url})
            self.assertEqual(calls, [url])
            self.assertTrue(verified_output(result))
            self.assertEqual(Path(result["path"]).read_bytes(), source.getvalue())

    def test_invalid_recovery_url_does_not_request_a_download(self):
        client = DesignSpark(Path("."), {})
        with self.assertRaises(ValueError):
            client.download_result("http://localhost/private", Path("unused.png"))

    def test_work_identity_changes_with_source(self):
        self.assertNotEqual(fingerprint("123", "456", "a"), fingerprint("123", "456", "b"))

    def test_overlapping_worker_is_blocked_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            with workspace_lock(workspace):
                with self.assertRaises(RuntimeError):
                    with workspace_lock(workspace):
                        self.fail("Second worker should not enter")
            with workspace_lock(workspace):
                pass


if __name__ == "__main__":
    unittest.main()
