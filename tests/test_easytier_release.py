import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE = Path(__file__).resolve().parents[1] / "scripts/update_easytier.py"
spec = importlib.util.spec_from_file_location("update_easytier", MODULE)
easytier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(easytier)


def elf(machine=62, segment_type=1):
    data = bytearray(128)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", data, 18, machine)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1)
    struct.pack_into("<I", data, 64, segment_type)
    return data


def archive_payload(binary=None, missing=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in easytier.BINARIES:
            if name != missing:
                archive.writestr(easytier.PREFIX + name, binary if binary is not None else elf())
    return stream.getvalue()


def release(payload, version="2.6.4"):
    name = f"easytier-linux-x86_64-v{version}.zip"
    return {"tag_name": "v" + version, "draft": False, "prerelease": False,
            "assets": [{"name": name, "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                        "browser_download_url": f"https://github.com/EasyTier/EasyTier/releases/download/v{version}/{name}"}]}


def feed_makefile(name="easytier"):
    return (
        "include $(TOPDIR)/rules.mk\n"
        "-include $(dir $(lastword $(MAKEFILE_LIST)))../version.mk\n"
        f"PKG_NAME:={name}\nPKG_VERSION:=$(or $(EASYTIER_VERSION),2.7.0)\n"
        "PKG_BUILD_DIR:=$(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)\n"
        "include $(INCLUDE_DIR)/package.mk\nRSTRIP:=:\n"
        "define Package/$(PKG_NAME)\n\tDEPENDS:=@(x86_64||arm) +kmod-tun\nendef\n"
        "define Package/$(PKG_NAME)/config\n\tconfig EASYTIER_INCLUDE_WEBCONSOLE\n\t\tdefault y\nendef\n"
        "define Build/Prepare\n\twget old-version\nendef\n"
        "define Build/Compile\n\tsource-build-command\nendef\n"
        "define Package/$(PKG_NAME)/install\n\tinstall old-core\nendef\n"
        "define Package/$(PKG_NAME)/conffiles\n/etc/config/easytier\nendef\n"
        "$(eval $(call BuildPackage,$(PKG_NAME)))\n"
    )


def create_packages(root):
    paths = []
    for prefix in ("feeds/kenzok8", "feeds/kenzok8/luci-app-easytier"):
        for name in ("easytier", "easytier-noweb"):
            directory = root / prefix / name
            directory.mkdir(parents=True)
            path = directory / "Makefile"
            path.write_text(feed_makefile(name), encoding="utf-8")
            paths.append(path)
    config = root / "feeds/kenzok8/luci-app-easytier/luci-app-easytier/root/etc/config/easytier"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"existing configuration\n")
    return paths, config


class EasyTierReleaseTests(unittest.TestCase):
    def test_only_one_official_stable_asset_with_digest_accepted(self):
        base = release(archive_payload())
        cases = []
        for key in ("draft", "prerelease"):
            item = copy.deepcopy(base); item[key] = True; cases.append(item)
        for key, value in (("tag_name", "v2.7.0-rc1"), ("assets", [])):
            item = copy.deepcopy(base); item[key] = value; cases.append(item)
        for key, value in (("digest", None), ("browser_download_url", "https://other.example/core.zip")):
            item = copy.deepcopy(base); item["assets"][0][key] = value; cases.append(item)
        item = copy.deepcopy(base); item["assets"].append(copy.deepcopy(item["assets"][0])); cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                easytier.select_asset(item)

    def test_all_duplicate_packages_updated_and_root_config_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); paths, config = create_packages(root)
            provenance = root / "custom-sources.json"
            provenance.write_text('{"feed":"old"}', encoding="utf-8")
            payload = archive_payload()
            for version in ("2.6.4", "2.6.5"):
                with patch.object(easytier.urllib.request, "urlopen", return_value=io.BytesIO(payload)) as download:
                    easytier.prepare_packages(root, provenance, release(payload, version))
                self.assertEqual(download.call_count, 1)
                for path in paths:
                    text = path.read_text(encoding="utf-8")
                    self.assertIn("PKG_VERSION:=" + version, text)
                    self.assertIn("PKG_HASH:=" + hashlib.sha256(payload).hexdigest(), text)
                    self.assertNotIn("wget", text)
                    self.assertNotIn("source-build-command", text)
                    self.assertIn("$(INSTALL_BIN) $(PKG_BUILD_DIR)/easytier-cli", text)
                    self.assertIn("sha256sum -c -", text)
                    self.assertIn("/etc/config/easytier", text)
                    self.assertIn("$(CP) ./root/.", text)
                    if path.parent.name == "easytier-noweb":
                        self.assertNotIn("$(PKG_BUILD_DIR)/easytier-web-embed", text)
                    else:
                        self.assertIn("ifdef CONFIG_EASYTIER_INCLUDE_WEBCONSOLE", text)
                self.assertEqual(config.read_bytes(), b"existing configuration\n")
                metadata = json.loads(provenance.read_text(encoding="utf-8"))
                self.assertEqual(metadata["feed"], "old")
                self.assertEqual(metadata["EasyTier/EasyTier (release)"], "v" + version)
                self.assertEqual(len(metadata["EasyTier/EasyTier (packages)"].split(",")), 4)

    def test_hash_mismatch_does_not_modify_packages_or_cache_partial_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); paths, _ = create_packages(root)
            before = {path: path.read_bytes() for path in paths}
            payload = archive_payload(); metadata = release(payload)
            metadata["assets"][0]["digest"] = "sha256:" + "0" * 64
            with patch.object(easytier.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    easytier.prepare_packages(root, root / "custom-sources.json", metadata)
            self.assertEqual(list((root / "dl").iterdir()), [])
            self.assertFalse((root / "custom-sources.json").exists())
            for path in paths:
                self.assertEqual(path.read_bytes(), before[path])

    def test_bad_binary_architecture_dynamic_binary_and_missing_core_rejected(self):
        cases = (archive_payload(elf(machine=183)), archive_payload(elf(segment_type=2)),
                 archive_payload(elf(segment_type=3)), archive_payload(missing="easytier-core"), b"not a zip")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            for payload in cases:
                with self.subTest(payload=payload[:15]):
                    path.write_bytes(payload)
                    with self.assertRaises((ValueError, zipfile.BadZipFile)):
                        easytier.validate_archive(path, hashlib.sha256(payload).hexdigest())

    def test_verified_cache_reused_but_corrupt_cache_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); create_packages(root)
            payload = archive_payload(); metadata = release(payload)
            with patch.object(easytier.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
                easytier.prepare_packages(root, root / "provenance.json", metadata)
            with patch.object(easytier.urllib.request, "urlopen") as download:
                easytier.prepare_packages(root, root / "provenance.json", metadata)
                download.assert_not_called()
                (root / "dl" / metadata["assets"][0]["name"]).write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    easytier.prepare_packages(root, root / "provenance.json", metadata)
                download.assert_not_called()

    def test_unknown_feed_recipe_fails_before_download_and_no_files_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); paths, _ = create_packages(root)
            paths[0].write_text("PKG_NAME:=easytier\nPKG_VERSION:=old\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in paths}
            with patch.object(easytier.urllib.request, "urlopen") as download:
                with self.assertRaises(ValueError):
                    easytier.prepare_packages(root, root / "provenance.json", release(archive_payload()))
                download.assert_not_called()
            for path in paths:
                self.assertEqual(path.read_bytes(), before[path])


if __name__ == "__main__":
    unittest.main()
