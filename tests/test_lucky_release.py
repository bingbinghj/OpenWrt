import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts/update_lucky.py"
spec = importlib.util.spec_from_file_location("update_lucky", MODULE)
lucky = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lucky)


def release(version="3.5.0"):
    name = f"lucky_{version}_Linux_x86_64.tar.gz"
    return {"tag_name": "v" + version, "draft": False, "prerelease": False,
            "assets": [{"name": name, "digest": "sha256:" + "a" * 64,
                        "browser_download_url": f"https://github.com/gdy666/lucky/releases/download/v{version}/{name}"}]}


class LuckyReleaseTests(unittest.TestCase):
    def test_draft_prerelease_missing_asset_digest_and_wrong_host_rejected(self):
        base = release()
        bad = []
        for key in ("draft", "prerelease"):
            item = copy.deepcopy(base); item[key] = True; bad.append(item)
        for key, value in (("assets", []), ("tag_name", "v3.5.0-beta")):
            item = copy.deepcopy(base); item[key] = value; bad.append(item)
        item = copy.deepcopy(base); item["assets"][0]["digest"] = None; bad.append(item)
        item = copy.deepcopy(base); item["assets"][0]["browser_download_url"] = "https://other.example/lucky.tar.gz"; bad.append(item)
        item = copy.deepcopy(base); item["assets"].append(copy.deepcopy(item["assets"][0])); bad.append(item)
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError): lucky.select_asset(item)

    def test_upstream_wget_replaced_and_next_build_can_select_new_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            makefile = package / "Makefile"
            original = "PKG_VERSION:=2.17.8\nPKG_RELEASE:=1\nPKG_SOURCE_VERSION:=old\n\tDEPENDS:=@(x86_64||arm)\ndefine Build/Prepare\n\twget old-version\nendef\ndefine Package/lucky/install\n\t$(INSTALL_BIN) $(PKG_BUILD_DIR)/lucky $(1)/usr/bin/lucky\nendef\n"
            makefile.write_text(original)
            provenance = package / "provenance.json"
            provenance.write_text('{"feed":"original"}')
            for version in ("3.5.0", "3.6.0"):
                lucky.prepare_package(package, provenance, release(version))
                result = makefile.read_text()
                self.assertIn("PKG_VERSION:=" + version, result)
                self.assertIn("PKG_HASH:=" + "a" * 64, result)
                self.assertNotIn("wget", result)
                self.assertIn("$(DL_DIR)/$(PKG_SOURCE)", result)
                self.assertIn("$(1)/usr/bin/lucky", result)
                self.assertEqual(json.loads(provenance.read_text())["feed"], "original")
                self.assertEqual(json.loads(provenance.read_text())["gdy666/lucky (release)"], "v" + version)

    def test_incompatible_makefile_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory); path = package / "Makefile"
            path.write_text("PKG_VERSION:=2.17.8\n")
            before = path.read_bytes()
            with self.assertRaises(ValueError): lucky.prepare_package(package, package / "provenance.json", release())
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
