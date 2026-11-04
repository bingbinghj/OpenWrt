import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts/update_adguardhome.py"
spec = importlib.util.spec_from_file_location("update_adguardhome", MODULE)
agh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agh)


def archive_payload(version="0.107.80", frontend=False, missing=None):
    stream = io.BytesIO()
    names = ["./build/static/index.html"] if frontend else [f"AdGuardHome-{version}/go.mod", f"AdGuardHome-{version}/main.go"]
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in names:
            if name == missing:
                continue
            data = b"source or frontend fixture\n"
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return stream.getvalue()


def release(payload, version="0.107.80"):
    return {"id": 123, "tag_name": "v" + version, "draft": False, "prerelease": False,
            "assets": [{"name": agh.FRONTEND_ASSET,
                        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                        "browser_download_url": agh.RELEASE_PREFIX + "v" + version + "/" + agh.FRONTEND_ASSET}]}


def feed_makefile():
    return (
        "PKG_NAME:=adguardhome\nPKG_VERSION:=0.107.79\nPKG_RELEASE:=1\n"
        "PKG_SOURCE:=$(PKG_NAME)-$(PKG_VERSION).tar.gz\n"
        "PKG_SOURCE_URL:=" + agh.SOURCE_PREFIX + "v$(PKG_VERSION)?\n"
        "PKG_HASH:=" + "a" * 64 + "\n"
        "PKG_BUILD_DIR:=$(BUILD_DIR)/AdGuardHome-$(PKG_VERSION)\n"
        "FRONTEND_DEST:=$(PKG_NAME)-frontend-$(PKG_VERSION).tar.gz\n"
        "FRONTEND_URL:=" + agh.RELEASE_PREFIX + "v$(PKG_VERSION)/\n"
        "FRONTEND_HASH:=" + "b" * 64 + "\n"
        "GO_PKG:=github.com/AdguardTeam/AdGuardHome\nGO_PKG_BUILD_PKG:=$(GO_PKG)\n"
        "define Download/adguardhome-frontend\n"
        "\tURL:=$(FRONTEND_URL)\n\tURL_FILE:=AdGuardHome_frontend.tar.gz\n"
        "\tFILE:=$(FRONTEND_DEST)\n\tHASH:=$(FRONTEND_HASH)\nendef\n"
        "define Build/Prepare\n\t$(call Build/Prepare/Default)\n"
        "\tgzip -dc $(DL_DIR)/$(FRONTEND_DEST) | $(HOST_TAR) -C $(PKG_BUILD_DIR)/ $(TAR_OPTIONS)\nendef\n"
        "define Package/adguardhome/install\n"
        "\t$(call GoPackage/Package/Install/Bin,$(1))\n"
        "\t$(INSTALL_CONF) ./files/adguardhome.conf $(1)/etc/config/adguardhome\nendef\n"
        "define Package/adguardhome/conffiles\n/etc/adguardhome/adguardhome.yaml\nendef\n"
        "$(eval $(call GoBinPackage,adguardhome))\n"
    )


def fixture(root, text=None):
    package = root / "package"
    package.mkdir()
    path = package / "Makefile"
    path.write_text(text if text is not None else feed_makefile(), encoding="utf-8")
    settings = package / "files/adguardhome.conf"
    settings.parent.mkdir()
    settings.write_bytes(b"user-facing package defaults\n")
    provenance = root / "custom-sources.json"
    provenance.write_text('{"feed":"original"}', encoding="utf-8")
    return package, path, settings, provenance, root / "dl"


class AdGuardHomeReleaseTests(unittest.TestCase):
    def test_unstable_tags_missing_digest_duplicates_and_wrong_host_rejected(self):
        base = release(archive_payload(frontend=True))
        cases = []
        for key in ("draft", "prerelease"):
            item = copy.deepcopy(base); item[key] = True; cases.append(item)
        for key, value in (("tag_name", "v0.107.80-beta.1"), ("tag_name", None), ("assets", [])):
            item = copy.deepcopy(base); item[key] = value; cases.append(item)
        for key, value in (("digest", None), ("browser_download_url", "https://other.example/frontend.tar.gz")):
            item = copy.deepcopy(base); item["assets"][0][key] = value; cases.append(item)
        item = copy.deepcopy(base); item["assets"].append(copy.deepcopy(item["assets"][0])); cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                agh.select_release(item)

    def test_new_build_refreshes_stable_version_and_pins_matching_source_frontend(self):
        with tempfile.TemporaryDirectory() as directory:
            package, path, settings, provenance, downloads = fixture(Path(directory))
            original = path.read_text(encoding="utf-8")
            for version in ("0.107.80", "0.107.81"):
                source = archive_payload(version)
                frontend = archive_payload(version, frontend=True)
                with patch.object(agh.urllib.request, "urlopen", side_effect=[io.BytesIO(source), io.BytesIO(frontend)]) as download:
                    agh.prepare_package(package, provenance, release(frontend, version), downloads)
                self.assertEqual(download.call_count, 2)
                self.assertEqual(download.call_args_list[0].args[0].full_url, agh.SOURCE_PREFIX + "v" + version)
                text = path.read_text(encoding="utf-8")
                expected = original.replace("PKG_VERSION:=0.107.79", "PKG_VERSION:=" + version)
                expected = expected.replace("PKG_HASH:=" + "a" * 64, "PKG_HASH:=" + hashlib.sha256(source).hexdigest())
                expected = expected.replace("FRONTEND_HASH:=" + "b" * 64, "FRONTEND_HASH:=" + hashlib.sha256(frontend).hexdigest())
                self.assertEqual(text, expected)
                self.assertEqual(settings.read_bytes(), b"user-facing package defaults\n")
                self.assertEqual((downloads / f"adguardhome-{version}.tar.gz").read_bytes(), source)
                metadata = json.loads(provenance.read_text(encoding="utf-8"))
                self.assertEqual(metadata["feed"], "original")
                self.assertTrue(all(isinstance(value, str) for value in metadata.values()))
                self.assertEqual(metadata["AdguardTeam/AdGuardHome (release)"], "v" + version)
                self.assertEqual(metadata["AdguardTeam/AdGuardHome (source sha256)"], hashlib.sha256(source).hexdigest())

    def test_frontend_hash_failure_leaves_package_and_provenance_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            package, path, _, provenance, downloads = fixture(Path(directory))
            before = path.read_bytes(), provenance.read_bytes()
            frontend = archive_payload(frontend=True)
            metadata = release(frontend); metadata["assets"][0]["digest"] = "sha256:" + "0" * 64
            with patch.object(agh.urllib.request, "urlopen", side_effect=[io.BytesIO(archive_payload()), io.BytesIO(frontend)]):
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    agh.prepare_package(package, provenance, metadata, downloads)
            self.assertEqual((path.read_bytes(), provenance.read_bytes()), before)
            self.assertFalse((downloads / "adguardhome-frontend-0.107.80.tar.gz").exists())
            self.assertEqual(list(downloads.glob("*.part")), [])

    def test_unknown_recipe_and_invalid_provenance_fail_before_download(self):
        cases = [feed_makefile().replace(agh.SOURCE_PREFIX, "https://other.example/"),
                 feed_makefile() + "PKG_VERSION:=1.2.3\n",
                 feed_makefile().replace("$(call Build/Prepare/Default)", "unknown prepare command")]
        for text in cases:
            with tempfile.TemporaryDirectory() as directory:
                package, path, _, provenance, downloads = fixture(Path(directory), text)
                before = path.read_bytes()
                with patch.object(agh.urllib.request, "urlopen") as download, self.assertRaises(ValueError):
                    agh.prepare_package(package, provenance, release(archive_payload(frontend=True)), downloads)
                download.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
        with tempfile.TemporaryDirectory() as directory:
            package, path, _, provenance, downloads = fixture(Path(directory))
            provenance.write_text("[]", encoding="utf-8")
            with patch.object(agh.urllib.request, "urlopen") as download, self.assertRaises(ValueError):
                agh.prepare_package(package, provenance, release(archive_payload(frontend=True)), downloads)
            download.assert_not_called()

    def test_html_truncated_or_wrong_version_source_never_changes_package(self):
        for source in (b"HTML error", archive_payload("0.107.79"), archive_payload(missing="AdGuardHome-0.107.80/go.mod")):
            with self.subTest(source=source[:16]), tempfile.TemporaryDirectory() as directory:
                package, path, _, provenance, downloads = fixture(Path(directory))
                before = path.read_bytes(), provenance.read_bytes()
                with patch.object(agh.urllib.request, "urlopen", return_value=io.BytesIO(source)):
                    with self.assertRaises((ValueError, tarfile.TarError)):
                        agh.prepare_package(package, provenance, release(archive_payload(frontend=True)), downloads)
                self.assertEqual((path.read_bytes(), provenance.read_bytes()), before)
                self.assertEqual(list(downloads.iterdir()), [])

    def test_source_refetched_and_frontend_cache_checked_on_each_build(self):
        with tempfile.TemporaryDirectory() as directory:
            package, path, _, provenance, downloads = fixture(Path(directory))
            source, frontend = archive_payload(), archive_payload(frontend=True)
            with patch.object(agh.urllib.request, "urlopen", side_effect=[io.BytesIO(source), io.BytesIO(frontend)]):
                agh.prepare_package(package, provenance, release(frontend), downloads)
            with patch.object(agh.urllib.request, "urlopen", return_value=io.BytesIO(source)) as download:
                agh.prepare_package(package, provenance, release(frontend), downloads)
                self.assertEqual(download.call_count, 1)
            before = path.read_bytes(), provenance.read_bytes()
            (downloads / "adguardhome-frontend-0.107.80.tar.gz").write_bytes(b"corrupt cache")
            with patch.object(agh.urllib.request, "urlopen", return_value=io.BytesIO(source)):
                with self.assertRaisesRegex(ValueError, "Cached.*SHA256 mismatch"):
                    agh.prepare_package(package, provenance, release(frontend), downloads)
            self.assertEqual((path.read_bytes(), provenance.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
