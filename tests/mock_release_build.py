"""Offline release transport fixtures; all package preparation remains real."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import test_adguardhome_release as adguardhome
from test_backup_rules import OC_DEFAULTS, OC_MAKEFILE
import test_easytier_release as easytier
from test_lucky_release import release as lucky_release
import test_mosdns_release as mosdns
import test_openclash_build as openclash


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def prepare_packages(root):
    for name in ("easytier", "easytier-noweb"):
        write(root / name / "Makefile", easytier.feed_makefile(name))
    write(root / "adguardhome/Makefile", adguardhome.feed_makefile())
    package = root / "luci-app-openclash"
    write(package / "Makefile", openclash.package_makefile("0.48.002") + OC_MAKEFILE.partition("\n")[2])
    write(package / "root/etc/uci-defaults/luci-openclash", OC_DEFAULTS)
    write(package / "root/etc/config/openclash",
          "config openclash 'config'\n\toption enable '0'\n\toption smart_enable '0'\n\toption store_fakeip '0'\n")
    write(package / "root/usr/share/openclash/openclash_version.lua", "return {}\n")


def prepare_mosdns(destination):
    fixture_root = Path(os.environ["CUSTOM_FIXTURE_NATIVE"]).resolve()
    if not destination.resolve().is_relative_to(fixture_root):
        raise ValueError("MosDNS fixture destination escapes the integration test directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.rmdir()
    def real_git(tree, *arguments):
        return subprocess.check_output([os.environ["CUSTOM_REAL_GIT"], "-C", str(tree), *arguments],
                                       text=True, encoding="utf-8", stderr=subprocess.DEVNULL).strip()
    with tempfile.TemporaryDirectory(prefix="mosdns-source-", dir=destination.parent) as directory:
        with patch.object(mosdns, "command", real_git):
            tree, _, _, commit = mosdns.source_fixture(Path(directory))
        if not tree.resolve().is_relative_to(fixture_root):
            raise ValueError("MosDNS fixture staging directory escapes the test directory")
        tree.replace(destination)
    (destination / ".git/offline-release-commit").write_text(commit + "\n", encoding="utf-8")


def argument(name):
    return Path(sys.argv[sys.argv.index(name) + 1])


def failure_selected(script):
    return os.environ.get("FAIL_RELEASE") == "1" and os.environ.get("FAIL_HELPER", script.name) == script.name


def run_helper(script):
    spec = importlib.util.spec_from_file_location("fixture_build_helper", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.argv = [str(script), *sys.argv[3:]]
    def fail(*unused):
        raise OSError("simulated latest release API failure")
    def download(url, payloads):
        if os.environ.get("FAIL_DOWNLOAD") == "1":
            raise OSError("simulated archive download failure")
        if url not in payloads:
            raise OSError("Unexpected offline fixture request: " + url)
        return payloads[url]
    if script.name == "update_openclash.py":
        snapshot, wire = openclash.fixture_payloads()
        package = argument("--package")
        prefix = f"OpenClash-{snapshot['source_sha']}/luci-app-openclash/"
        entries = {prefix + path.relative_to(package).as_posix(): path.read_bytes()
                   for path in package.rglob("*") if path.is_file()}
        source_url = f"https://codeload.github.com/vernesong/OpenClash/tar.gz/{snapshot['source_sha']}"
        wire[source_url] = openclash.archive_bytes(entries)
        def fetch(url):
            if failure_selected(script):
                return fail()
            return download(url, wire) if url.endswith(".tar.gz") else wire[url]
        with patch.object(module, "fetch_bytes", side_effect=fetch):
            module.main()
    elif script.name == "update_mosdns.py":
        tree = argument("--package-tree")
        commit = (tree / ".git/offline-release-commit").read_text().strip()
        def github(path):
            if failure_selected(script):
                return fail()
            if path == f"repos/{module.PATCHED_REPOSITORY}/releases/latest":
                return mosdns.release("v5.3.4-r15")
            if path == f"repos/{module.CORE_REPOSITORY}/releases/latest":
                return mosdns.release("v5.3.4")
            if path == f"repos/{module.PATCHED_REPOSITORY}/git/ref/tags/v5.3.4-r15":
                return {"object": {"type": "commit", "sha": commit}}
            raise ValueError("Unexpected offline MosDNS API request")
        with patch.object(module, "github", side_effect=github), patch.object(module, "fetch_tag", return_value=commit):
            module.main()
    elif script.name == "update_adguardhome.py":
        source, frontend = adguardhome.archive_payload(), adguardhome.archive_payload(frontend=True)
        module.fetch_release = fail if failure_selected(script) else lambda: adguardhome.release(frontend)
        payloads = {module.SOURCE_PREFIX + "v0.107.80": source,
                    module.RELEASE_PREFIX + "v0.107.80/" + module.FRONTEND_ASSET: frontend}
        with patch.object(module.urllib.request, "urlopen", side_effect=lambda request, **unused: io.BytesIO(download(request.full_url, payloads))):
            module.main()
    elif script.name == "update_easytier.py":
        payload = easytier.archive_payload()
        module.fetch_release = fail if failure_selected(script) else lambda: easytier.release(payload)
        name = "easytier-linux-x86_64-v2.6.4.zip"
        payloads = {"https://github.com/EasyTier/EasyTier/releases/download/v2.6.4/" + name: payload}
        with patch.object(module.urllib.request, "urlopen", side_effect=lambda request, **unused: io.BytesIO(download(request.full_url, payloads))):
            module.main()
    elif script.name == "update_lucky.py":
        module.fetch_release = fail if failure_selected(script) else lucky_release
        module.main()
    else:
        raise ValueError("Unexpected integration helper: " + script.name)


def verify_build(root):
    metadata = json.loads((root / "custom-sources.json").read_text(encoding="utf-8"))
    expected = {"gdy666/lucky (release)": "v3.5.0", "EasyTier/EasyTier (release)": "v2.6.4",
                "AdguardTeam/AdGuardHome (release)": "v0.107.80", "sbwml/luci-app-mosdns (release)": "v5.3.4-r15",
                "vernesong/OpenClash (plugin version)": "v0.48.002",
                "vernesong/OpenClash (Smart version)": "alpha-smart-gdaf3ad0"}
    for key, value in expected.items():
        assert metadata.get(key) == value, key
    assert all(isinstance(value, str) for value in metadata.values())
    assert "coolsnowwolf/packages (lucky)" in metadata
    package = root / "feeds/luci/applications/luci-app-openclash"
    assert (package / "root/etc/openclash/core/clash_meta").read_bytes() == openclash.static_elf()
    assert "option core_type 'Smart'" in (package / "root/etc/config/openclash").read_text()
    keep = (package / "root/lib/upgrade/keep.d/luci-app-openclash").read_text().splitlines()
    for path in ("/etc/config/openclash", "/etc/openclash/config/", "/etc/openclash/overwrite/", "/etc/openclash/cache.db"):
        assert path in keep
    assert "/etc/openclash/" not in keep
    assert not any("/core" in path or path == "/etc/openclash/clash" for path in keep)
    makefile = (package / "Makefile").read_text()
    assert 'cp -rf "/etc/openclash" "/tmp/openclash"' not in makefile
    assert 'cp -rf "/etc/openclash/config" "/tmp/openclash/"' in makefile
    defaults = (package / "root/etc/uci-defaults/luci-openclash").read_text()
    assert 'cp -rf "/tmp/openclash/." "/etc/openclash/"' not in defaults
    extra = (root / "files/etc/sysupgrade.conf").read_text().splitlines()
    assert "/etc/AdGuardHome/data/" in extra and "/etc/lucky/" in extra
    assert "/etc/AdGuardHome/" not in extra
    assert not any(path in extra for path in ("/usr/bin/AdGuardHome", "/usr/bin/lucky", "/usr/bin/mosdns", "/etc/openclash/core/"))
    for filename, key in (("adguardhome-0.107.80.tar.gz", "source"), ("adguardhome-frontend-0.107.80.tar.gz", "frontend")):
        assert metadata[f"AdguardTeam/AdGuardHome ({key} sha256)"] == hashlib.sha256((root / "dl" / filename).read_bytes()).hexdigest()
    print("All release helpers, matching Smart core, stats API tree and data-only backup rules verified.")


def main():
    action = sys.argv[1]
    if action == "prepare-packages":
        prepare_packages(Path(sys.argv[2]))
    elif action == "prepare-mosdns":
        prepare_mosdns(Path(sys.argv[2]))
    elif action == "prepare-lucky":
        write(Path(sys.argv[2]) / "Makefile", "PKG_NAME:=lucky\nPKG_VERSION:=2.17.8\nPKG_RELEASE:=1\n"
              "PKG_BUILD_DIR:=$(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)\nPKG_SOURCE_VERSION:=old\n"
              "\tDEPENDS:=@(x86_64||arm)\ndefine Build/Prepare\n\twget old-version\nendef\n"
              "define Build/Compile\nendef\ndefine Package/$(PKG_NAME)/install\n"
              "\t$(INSTALL_BIN) $(PKG_BUILD_DIR)/lucky $(1)/usr/bin/lucky\nendef\n")
    elif action == "prepare-lucky-ui":
        write(Path(sys.argv[2]) / "root/usr/bin/luckyarch", "#!/bin/sh\necho x86_64\n")
    elif action == "verify-build":
        verify_build(Path(sys.argv[2]))
    elif action == "run":
        run_helper(Path(sys.argv[2]))
    else:
        raise ValueError("Unexpected integration fixture action")


if __name__ == "__main__":
    main()
