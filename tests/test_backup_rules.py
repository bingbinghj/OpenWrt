"""Offline tests for package backups, including both install and upgrade paths."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_backup_rules", ROOT / "scripts/prepare_backup_rules.py")
backup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backup)


OC_MAKEFILE = '''PKG_NAME:=luci-app-openclash
define Package/$(PKG_NAME)/conffiles
endef
define Package/$(PKG_NAME)/preinst
#!/bin/sh
\tcp -f "/etc/config/openclash" "/tmp/openclash.bak" >/dev/null 2>&1
\tcp -rf "/etc/openclash" "/tmp/openclash" >/dev/null 2>&1
endef
define Package/$(PKG_NAME)/prerm
#!/bin/sh
\tcp -rf "/etc/openclash" "/tmp/openclash" >/dev/null 2>&1
endef
'''

OC_DEFAULTS = '''#!/bin/bash
mkdir -p /lib/upgrade/keep.d
cat > "/lib/upgrade/keep.d/luci-app-openclash" <<-EOF
/etc/openclash/
EOF
if [ -f "/tmp/openclash.bak" ]; then
\tcp -rf "/tmp/openclash/." "/etc/openclash/" >/dev/null 2>&1
fi
'''

class BackupRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.oc = self.root / "openclash"
        self.files = self.root / "files"
        self.defaults = self.oc / "root/etc/uci-defaults/luci-openclash"
        self.defaults.parent.mkdir(parents=True)
        self.defaults.write_text(OC_DEFAULTS, encoding="utf-8")
        (self.oc / "Makefile").write_text(OC_MAKEFILE, encoding="utf-8")

    def test_manifest_retains_data_and_excludes_cores(self) -> None:
        backup.prepare(self.oc, self.files)
        entries = (self.oc / "root/lib/upgrade/keep.d/luci-app-openclash").read_text().splitlines()
        for path in ["/etc/config/openclash", "/etc/openclash/config/",
                     "/etc/openclash/custom/", "/etc/openclash/overwrite/",
                     "/etc/openclash/history/", "/etc/openclash/cache.db",
                     "/etc/openclash/GeoIP.dat"]:
            self.assertIn(path, entries)
        # Model sysupgrade's recursive selection against actual files in a fixture.
        fixtures = ["etc/config/openclash", "etc/openclash/config/subscription.yaml",
                    "etc/openclash/custom/openclash_custom_overwrite.sh",
                    "etc/openclash/overwrite/user-module.ini",
                    "etc/openclash/history/group.yaml", "etc/openclash/cache.db",
                    "etc/openclash/GeoIP.dat", "etc/openclash/core/clash_meta",
                    "etc/openclash/core/clash_tun", "etc/openclash/clash",
                    "etc/init.d/openclash"]
        fsroot = self.root / "filesystem"
        for path in fixtures:
            file = fsroot / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"user data" if "core/" not in path else b"\x7fELFcore")
        symlink = fsroot / "etc/openclash/clash"
        try:
            symlink.unlink()
            symlink.symlink_to("core/clash_meta")
        except OSError:
            symlink.write_bytes(b"symlink placeholder on Windows without symlink privilege")
        included = set()
        for entry in entries:
            p = fsroot / entry.lstrip("/")
            if p.is_dir():
                included.update("/" + f.relative_to(fsroot).as_posix()
                                for f in p.rglob("*") if f.is_file())
            elif p.exists():
                included.add(entry)
        self.assertIn("/etc/openclash/config/subscription.yaml", included)
        self.assertIn("/etc/openclash/custom/openclash_custom_overwrite.sh", included)
        self.assertNotIn("/etc/openclash/core/clash_meta", included)
        self.assertNotIn("/etc/openclash/core/clash_tun", included)
        self.assertNotIn("/etc/openclash/clash", included)
        self.assertNotIn("/etc/init.d/openclash", included)
        extra = (self.files / "etc/sysupgrade.conf").read_text()
        self.assertIn("/etc/AdGuardHome/data/\n", extra)
        self.assertNotIn("/etc/AdGuardHome/\n", extra)
        self.assertNotIn("agh-backup", extra)
        self.assertIn("/etc/mosdns/config_custom.yaml\n", extra)

    def test_package_upgrade_and_defaults_use_data_manifest(self) -> None:
        backup.prepare(self.oc)
        defaults = self.defaults.read_text()
        makefile = (self.oc / "Makefile").read_text()
        self.assertNotIn('\tcp -rf "/etc/openclash" "/tmp/openclash"', makefile)
        self.assertNotIn('\tcp -rf "/tmp/openclash/." "/etc/openclash/"', defaults)
        self.assertIn('cp -rf "/etc/openclash/config" "/tmp/openclash/"', makefile)
        self.assertIn('cp -rf "/tmp/openclash/config" "/etc/openclash/"', defaults)
        keep_body = defaults.split('<<-EOF\n', 1)[1].split('\nEOF', 1)[0]
        self.assertIn('/etc/config/openclash', keep_body.splitlines())
        self.assertNotIn('/etc/openclash/', keep_body.splitlines())
        self.assertNotIn('/etc/openclash/core/', keep_body.splitlines())

    def test_existing_defaults_restores_user_data_without_restoring_old_core(self) -> None:
        bash = shutil.which("bash")
        if os.name == "nt":
            git = shutil.which("git")
            git_bash = Path(git).parents[1] / "bin/bash.exe" if git else None
            bash = str(git_bash) if git_bash and git_bash.is_file() else None
        if bash is None:
            self.skipTest("bash unavailable for package install-script check")
        backup.prepare(self.oc)
        fsroot = self.root / "router"
        files = {
            "etc/openclash/core/clash_meta": b"new firmware core",
            "tmp/openclash/core/clash_meta": b"old package pre-removal backup core",
            "tmp/openclash/config/subscription.yaml": b"user subscription",
            "tmp/openclash/custom/custom.list": b"user customization",
            "tmp/openclash/china_ip6_route.ipset": b"user geo data",
            "tmp/openclash.bak": b"user UCI configuration",
        }
        for path, data in files.items():
            p = fsroot / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        code = self.defaults.read_text()
        for prefix in ("/etc/", "/tmp/", "/lib/"):
            code = code.replace(prefix, fsroot.as_posix() + prefix)
        script = self.root / "run-defaults.sh"
        script.write_bytes((code + "\nexit 0\n").encode())
        result = subprocess.run([bash, script.as_posix()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((fsroot / "etc/openclash/core/clash_meta").read_bytes(),
                         b"new firmware core")
        self.assertEqual((fsroot / "etc/openclash/config/subscription.yaml").read_bytes(),
                         b"user subscription")
        self.assertEqual((fsroot / "etc/openclash/custom/custom.list").read_bytes(),
                         b"user customization")
        kept = (fsroot / "lib/upgrade/keep.d/luci-app-openclash").read_text().splitlines()
        self.assertFalse(any(path.rstrip("/") == fsroot.as_posix() + "/etc/openclash"
                             for path in kept))
        self.assertFalse(any("/etc/openclash/core" in path for path in kept))

    def test_idempotent_and_configuration_content_unchanged(self) -> None:
        user_config = self.oc / "root/etc/config/openclash"
        user_config.parent.mkdir(parents=True)
        content = "config openclash 'config'\n option config_path '/etc/openclash/config/user.yaml'\n"
        user_config.write_text(content, encoding="utf-8")
        backup.prepare(self.oc, self.files)
        self.assertEqual(backup.prepare(self.oc, self.files), [])
        self.assertEqual(user_config.read_text(), content)

    def test_unknown_recipe_stops_before_any_changes(self) -> None:
        original_makefile = (self.oc / "Makefile").read_bytes()
        original_defaults = self.defaults.read_bytes()
        (self.oc / "Makefile").write_text(
            OC_MAKEFILE.replace('/conffiles\nendef', '/conffiles\n/etc/unknown/\nendef'),
            encoding="utf-8")
        original_makefile = (self.oc / "Makefile").read_bytes()
        with self.assertRaisesRegex(backup.RecipeError, "Unknown.*conffiles"):
            backup.prepare(self.oc, self.files)
        self.assertEqual((self.oc / "Makefile").read_bytes(), original_makefile)
        self.assertEqual(self.defaults.read_bytes(), original_defaults)
        self.assertFalse((self.oc / "root/lib/upgrade/keep.d/luci-app-openclash").exists())

    def test_changed_defaults_writer_requires_review(self) -> None:
        self.defaults.write_text(OC_DEFAULTS.replace('<<-EOF', '<<OTHER'), encoding="utf-8")
        with self.assertRaisesRegex(backup.RecipeError, "uci-defaults keep.d writer"):
            backup.prepare(self.oc)

    def test_narrow_build_default_extra_paths(self) -> None:
        conf = self.files / "etc/sysupgrade.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("# user data\n/etc/openclash/\n/etc/AdGuardHome/\n"
                        "/etc/lucky/\n/usr/bin/AdGuardHome\n", encoding="utf-8")
        backup.prepare(self.oc, self.files)
        paths = conf.read_text().splitlines()
        self.assertIn('/etc/lucky/', paths)
        self.assertIn('/etc/openclash/config/', paths)
        self.assertIn('/etc/AdGuardHome/data/', paths)
        self.assertNotIn('/etc/openclash/', paths)
        self.assertNotIn('/etc/AdGuardHome/', paths)
        self.assertNotIn('/usr/bin/AdGuardHome', paths)
        self.assertEqual(backup.prepare(self.oc, self.files), [])

    def test_overbroad_extra_paths_fail_before_mutations(self) -> None:
        conf = self.files / "etc/sysupgrade.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text('/etc\n', encoding="utf-8")
        original = self.defaults.read_bytes()
        with self.assertRaisesRegex(backup.RecipeError, "Overbroad"):
            backup.prepare(self.oc, self.files)
        self.assertEqual(self.defaults.read_bytes(), original)

    def test_crlf_source_is_supported(self) -> None:
        self.defaults.write_bytes(OC_DEFAULTS.replace('\n', '\r\n').encode())
        backup.prepare(self.oc)
        self.assertNotIn(b'\r', self.defaults.read_bytes())


if __name__ == "__main__":
    unittest.main()
