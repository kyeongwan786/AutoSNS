import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import cloud


class _Response:
    def __init__(self, value):
        self.value = value if isinstance(value, bytes) else json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        if size < 0:
            return self.value
        value, self.value = self.value[:size], self.value[size:]
        return value


class CloudUpdateTests(unittest.TestCase):
    def _release_responses(self, minimum="0.2.0", digest="a" * 64):
        installer_url = "https://github.com/owner/repo/releases/download/v0.3.0/AutoSNS-Setup-0.3.0.exe"
        release = {
            "tag_name": "v0.3.0",
            "html_url": "https://github.com/owner/repo/releases/tag/v0.3.0",
            "assets": [
                {"name": "AutoSNS-Setup-0.3.0.exe", "browser_download_url": installer_url},
                {"name": "AutoSNS-update.json", "browser_download_url": "https://github.com/owner/repo/releases/download/v0.3.0/AutoSNS-update.json"},
            ],
        }
        manifest = {
            "version": "0.3.0",
            "minimum_supported_version": minimum,
            "installer_url": installer_url,
            "installer_sha256": digest,
            "release_url": release["html_url"],
        }
        return [_Response(release), _Response(manifest)]

    def test_optional_update_is_available_without_being_required(self):
        with patch.object(cloud, "settings", return_value={
            "github_repository": "owner/repo", "app_version": "0.2.0",
        }), patch.object(cloud.urllib.request, "urlopen", side_effect=self._release_responses("0.2.0")):
            result = cloud.check_update()

        self.assertTrue(result["available"])
        self.assertFalse(result["required"])
        self.assertEqual(result["latest"], "0.3.0")

    def test_minimum_supported_version_marks_update_required(self):
        with patch.object(cloud, "settings", return_value={
            "github_repository": "owner/repo", "app_version": "0.2.0",
        }), patch.object(cloud.urllib.request, "urlopen", side_effect=self._release_responses("0.3.0")):
            result = cloud.check_update()

        self.assertTrue(result["available"])
        self.assertTrue(result["required"])

    def test_manifest_with_invalid_hash_is_rejected(self):
        with patch.object(cloud, "settings", return_value={
            "github_repository": "owner/repo", "app_version": "0.2.0",
        }), patch.object(cloud.urllib.request, "urlopen", side_effect=self._release_responses(digest="bad")):
            result = cloud.check_update()

        self.assertFalse(result["available"])
        self.assertIn("error", result)

    def test_installer_download_must_match_manifest_hash(self):
        data = b"test installer bytes"
        update = {
            "available": True,
            "latest": "0.3.0",
            "url": "https://github.com/owner/repo/releases/download/v0.3.0/AutoSNS-Setup-0.3.0.exe",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(cloud, "settings", return_value={"github_repository": "owner/repo"}), \
                patch.object(cloud.tempfile, "gettempdir", return_value=temp_dir), \
                patch.object(cloud.urllib.request, "urlopen", return_value=_Response(data)):
            installer = cloud.download_update_installer(update)
            self.assertEqual(installer.read_bytes(), data)
            installer.unlink()

        update["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(cloud, "settings", return_value={"github_repository": "owner/repo"}), \
                patch.object(cloud.tempfile, "gettempdir", return_value=temp_dir), \
                patch.object(cloud.urllib.request, "urlopen", return_value=_Response(data)):
            with self.assertRaises(cloud.CloudError):
                cloud.download_update_installer(update)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
