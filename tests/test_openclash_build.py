import copy
from contextlib import redirect_stdout
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts/update_openclash.py"
spec = importlib.util.spec_from_file_location("update_openclash", MODULE)
openclash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(openclash)


def static_elf(machine=62, segment=1):
    data = bytearray(128)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", data, 16, 2)
    struct.pack_into("<H", data, 18, machine)
    struct.pack_into("<I", data, 20, 1)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1)
    struct.pack_into("<I", data, 64, segment)
    return bytes(data)


def archive_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith("clash") else 0o644
            archive.addfile(member, io.BytesIO(data))
    return gzip.compress(stream.getvalue(), mtime=0)


def package_makefile(version):
    return (f"PKG_NAME:=luci-app-openclash\nPKG_VERSION:={version}\n"
            "define Package/$(PKG_NAME)\n\tPKGARCH:=all\n\tDEPENDS:=+dnsmasq-full +bash\nendef\n"
            "define Build/Compile\nendef\n"
            "ifeq ($(CONFIG_LUCI_APP_OPENCLASH_INCLUDE_CORE),y)\n"
            "define Build/Compile\n\tcurl " + openclash.RAW + "/core/master/meta/clash.tar.gz\nendef\nendif\n")


def fixture_payloads(version="0.48.002", smart="alpha-smart-gdaf3ad0", tag_fallback=False,
                     annotated_tag=False, binary=None, source_extra=None):
    master, package, core, tag_commit, tag_object = (digit * 40 for digit in "12345")
    selected = tag_commit if tag_fallback else master
    binary = static_elf() if binary is None else binary
    core_data = archive_bytes({"clash": binary})
    prefix = f"OpenClash-{selected}/luci-app-openclash/"
    config = b"\nconfig openclash 'config'\n\toption enable '0'\n\toption smart_enable '0'\n\toption store_fakeip '0'\n\nconfig dns_servers\n\toption type 'udp'\n"
    entries = {prefix + "Makefile": package_makefile(version).encode(),
               prefix + "root/etc/config/openclash": config,
               prefix + "root/etc/init.d/openclash": b"#!/bin/sh\ndel_cron() { :; }\n",
               prefix + "root/usr/share/openclash/openclash_version.lua": b"return {}\n"}
    entries.update(source_extra or {})
    source_data = archive_bytes(entries)
    snapshot = {"source_sha": selected, "master_sha": master, "package_sha": package, "core_sha": core,
                "source_ref": "refs/tags/v" + version if tag_fallback else "refs/heads/master",
                "source_version": version, "plugin_version": "v" + version, "smart_version": smart,
                "core_blob_sha": hashlib.sha1(b"blob " + str(len(core_data)).encode() + b"\0" + core_data).hexdigest()}
    wire = {}
    for name, sha in (("master", master), ("package", package), ("core", core)):
        wire[openclash.API + "/git/ref/heads/" + name] = json.dumps({"ref": "refs/heads/" + name,
                                                              "object": {"type": "commit", "sha": sha}}).encode()
    wire[f"{openclash.RAW}/{package}/master/version"] = ("v" + version + "\n").encode()
    wire[f"{openclash.RAW}/{core}/master/core_version"] = ("alpha-g1234567\n" + smart + "\n").encode()
    wire[f"{openclash.RAW}/{master}/luci-app-openclash/Makefile"] = package_makefile("0.48.008" if tag_fallback else version).encode()
    wire[f"{openclash.API}/contents/{openclash.CORE_PATH}?ref={core}"] = json.dumps({
        "type": "file", "path": openclash.CORE_PATH, "sha": snapshot["core_blob_sha"],
        "download_url": f"{openclash.RAW}/{core}/{openclash.CORE_PATH}"}).encode()
    wire[f"https://codeload.github.com/vernesong/OpenClash/tar.gz/{selected}"] = source_data
    wire[f"{openclash.RAW}/{core}/{openclash.CORE_PATH}"] = core_data
    if tag_fallback:
        wire[f"{openclash.API}/git/ref/tags/v{version}"] = json.dumps({"ref": "refs/tags/v" + version,
            "object": {"type": "tag" if annotated_tag else "commit", "sha": tag_object if annotated_tag else tag_commit}}).encode()
        wire[f"{openclash.RAW}/{tag_commit}/luci-app-openclash/Makefile"] = package_makefile(version).encode()
        if annotated_tag:
            wire[f"{openclash.API}/git/tags/{tag_object}"] = json.dumps({"object": {"type": "commit", "sha": tag_commit}}).encode()
    return snapshot, wire


def create_target(root):
    package = root / "feeds/luci/applications/luci-app-openclash"
    package.mkdir(parents=True)
    (package / "Makefile").write_text(package_makefile("0.48.008"), encoding="utf-8")
    (package / "old-marker").write_bytes(b"old-package")
    provenance = root / "custom-sources.json"
    provenance.write_text('{"feed":"original"}', encoding="utf-8")
    return package, provenance


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class OpenClashBuildTests(unittest.TestCase):
    def test_master_and_advertised_tag_selection_with_annotated_tag(self):
        for fallback, annotated in ((False, False), (True, False), (True, True)):
            snapshot, wire = fixture_payloads(tag_fallback=fallback, annotated_tag=annotated)
            with self.subTest(fallback=fallback, annotated=annotated), patch.object(openclash, "fetch_bytes", side_effect=wire.__getitem__):
                self.assertEqual(openclash.fetch_snapshot(), snapshot)

    def test_missing_advertised_tag_or_mismatched_tag_source_is_fatal(self):
        snapshot, wire = fixture_payloads(tag_fallback=True)
        tag_url = openclash.API + "/git/ref/tags/v0.48.002"
        def missing(url):
            if url == tag_url:
                raise OSError("Advertised tag not found")
            return wire[url]
        with patch.object(openclash, "fetch_bytes", side_effect=missing), self.assertRaises(OSError):
            openclash.fetch_snapshot()
        wire[f"{openclash.RAW}/{snapshot['source_sha']}/luci-app-openclash/Makefile"] = package_makefile("0.48.009").encode()
        with patch.object(openclash, "fetch_bytes", side_effect=wire.__getitem__), self.assertRaisesRegex(ValueError, "does not match"):
            openclash.fetch_snapshot()

    def test_consecutive_ui_updates_replace_plugin_and_matching_smart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, provenance = create_target(root)
            for version, smart in (("0.48.002", "alpha-smart-gdaf3ad0"), ("0.48.003", "alpha-smart-g1234567")):
                snapshot, wire = fixture_payloads(version, smart)
                with patch.object(openclash, "fetch_bytes", side_effect=wire.__getitem__):
                    openclash.prepare_package(package, provenance, snapshot, root / "dl")
                self.assertEqual(openclash.source_version((package / "Makefile").read_text()), version)
                self.assertEqual((package / "root/etc/openclash/core/clash_meta").read_bytes(), static_elf())
                self.assertFalse((package / "old-marker").exists())
                config = (package / "root/etc/config/openclash").read_text()
                self.assertEqual(config.count("option core_type 'Smart'"), 1)
                self.assertTrue(config.index("config openclash 'config'") < config.index("option core_type"))
                self.assertIn("option store_fakeip '0'", config)
                self.assertIn("option enable '0'", config)
                self.assertIn("option smart_enable '0'", config)
                self.assertIn("config dns_servers\n\toption type 'udp'", config)
                makefile = (package / "Makefile").read_text()
                self.assertNotIn("curl", makefile)
                self.assertIn("PKGARCH:=$(ARCH_PACKAGES)", makefile)
                self.assertIn("DEPENDS:=@x86_64", makefile)
                metadata = json.loads(provenance.read_text())
                self.assertEqual(metadata["feed"], "original")
                self.assertEqual(metadata["vernesong/OpenClash (Smart version)"], smart)
                self.assertEqual(metadata["vernesong/OpenClash (Smart binary sha256)"], hashlib.sha256(static_elf()).hexdigest())
                self.assertTrue(all(isinstance(value, str) for value in metadata.values()))

    def test_wrong_git_blob_hash_invalid_architecture_and_dynamic_binary_keep_old_package(self):
        fixtures = []
        snapshot, wire = fixture_payloads(); snapshot["core_blob_sha"] = "a" * 40; fixtures.append((snapshot, wire))
        fixtures.append(fixture_payloads(binary=static_elf(machine=183)))
        fixtures.extend(fixture_payloads(binary=static_elf(segment=segment)) for segment in (2, 3))
        for snapshot, wire in fixtures:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); package, provenance = create_target(root); before = tree_bytes(root)
                with patch.object(openclash, "fetch_bytes", side_effect=wire.__getitem__), self.assertRaises(ValueError):
                    openclash.prepare_package(package, provenance, snapshot, root / "dl")
                self.assertEqual(tree_bytes(root), before)

    def test_ui_source_mismatch_unknown_defaults_and_path_traversal_keep_old_package(self):
        snapshot, wire = fixture_payloads(); mismatch = copy.deepcopy(snapshot); mismatch["source_version"] = "0.48.008"
        prefix = "OpenClash-" + snapshot["source_sha"] + "/luci-app-openclash/"
        fixtures = [(mismatch, wire), fixture_payloads(source_extra={prefix + "../escape": b"bad"}),
                    fixture_payloads(source_extra={prefix + "root/etc/config/openclash": b"config unexpected 'config'\n"})]
        for snapshot, wire in fixtures:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); package, provenance = create_target(root); before = tree_bytes(root)
                with patch.object(openclash, "fetch_bytes", side_effect=wire.__getitem__), self.assertRaises(ValueError):
                    openclash.prepare_package(package, provenance, snapshot, root / "dl")
                self.assertEqual(tree_bytes(root), before)

    def test_non_feed_target_and_provenance_inside_package_are_rejected_without_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, provenance = create_target(root)
            outside = root / "luci-app-openclash"; outside.mkdir(); (outside / "Makefile").write_text(package_makefile("0.48.002"))
            snapshot, wire = fixture_payloads()
            with patch.object(openclash, "fetch_bytes") as download:
                with self.assertRaises(ValueError): openclash.prepare_package(outside, provenance, snapshot, root / "dl")
                with self.assertRaises(ValueError): openclash.prepare_package(package, package / "provenance.json", snapshot, root / "dl")
                download.assert_not_called()

    def test_metadata_failure_rolls_back_already_swapped_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, provenance = create_target(root)
            before = tree_bytes(package); old_metadata = provenance.read_bytes(); snapshot, wire = fixture_payloads()
            original_replace = Path.replace
            def replace(path, target):
                if Path(target) == provenance:
                    raise OSError("Metadata destination unavailable")
                return original_replace(path, target)
            with patch.object(openclash, "fetch_bytes", side_effect=wire.__getitem__), patch.object(Path, "replace", replace):
                with self.assertRaises(OSError): openclash.prepare_package(package, provenance, snapshot, root / "dl")
            self.assertEqual(tree_bytes(package), before)
            self.assertEqual(provenance.read_bytes(), old_metadata)

    def test_cli_uses_ui_snapshot_and_persists_smart_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, provenance = create_target(root); snapshot, wire = fixture_payloads()
            argv = ["update_openclash.py", "--package", str(package), "--provenance", str(provenance), "--download-dir", str(root / "dl")]
            with patch.object(sys, "argv", argv), patch.object(openclash, "fetch_bytes", side_effect=wire.__getitem__), redirect_stdout(io.StringIO()) as output:
                openclash.main()
            self.assertIn("OpenClash UI master v0.48.002", output.getvalue())
            self.assertEqual(json.loads(provenance.read_text())["vernesong/OpenClash (Smart version)"], snapshot["smart_version"])


if __name__ == "__main__":
    unittest.main()
