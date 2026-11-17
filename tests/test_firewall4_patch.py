"""Reproduce and prevent the firewall4 FullCone patch context failure."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "scripts/fix_firewall4_patch.py"
spec = importlib.util.spec_from_file_location("fix_firewall4_patch", MODULE)
firewall4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(firewall4)

PATCH_PROGRAM = shutil.which("patch")
if not PATCH_PROGRAM and Path("D:/Git/usr/bin/patch.exe").is_file():
    PATCH_PROGRAM = "D:/Git/usr/bin/patch.exe"

# Taken from ImmortalWrt a4ba8240d8a1aaef7e8c6f2d65e841a9bfd2b8c5.
# Only the first file diff is relevant to this regression; the later file diff
# is a sentinel proving that the working FullCone implementation stays intact.
HEADER = b"""From aa3b56e289fba7425e649a608c333622ffd9c367 Mon Sep 17 00:00:00 2001
Subject: [PATCH] firewall4: add fullcone support

--- a/root/etc/config/firewall
+++ b/root/etc/config/firewall
"""
OLD_HUNK = b"""@@ -5,6 +5,10 @@ config defaults
 \toption forward\t\tREJECT
 # Uncomment this line to disable ipv6 rules
 #\toption disable_ipv6\t1
+\toption flow_offloading\t1
+\toption flow_offloading_hw\t1
+\toption fullcone\t\t1
+\toption fullcone6\t0
\x20
 config zone
 \toption name\t\tlan
"""
STABLE_HUNK = b"""@@ -1,5 +1,9 @@ config defaults
 config defaults
 \toption syn_flood\t1
+\toption flow_offloading\t1
+\toption flow_offloading_hw\t1
+\toption fullcone\t\t1
+\toption fullcone6\t0
 \toption input\t\tREJECT
 \toption output\t\tACCEPT
 \toption forward\t\tREJECT
"""
TAIL = b"""--- a/root/usr/share/firewall4/templates/ruleset.uc
+++ b/root/usr/share/firewall4/templates/ruleset.uc
@@ -333,3 +333,6 @@ table inet fw4 {
 {%   for (let redirect in fw4.redirects(`dstnat_${zone.name}`)): %}
 \t\t{%+ include("redirect.uc", { fw4, zone, redirect }) %}
 {%   endfor %}
+{%   if (zone.masq && fw4.default_option("fullcone")): %}
+\t\t{%+ include("zone-fullcone.uc", { fw4, zone, family: 4, direction: "dstnat" }) %}
+{%   endif %}
"""
ORIGINAL = HEADER + OLD_HUNK + TAIL
UPDATED = HEADER + STABLE_HUNK + TAIL
UPSTREAM = (MODULE.parent.parent / "tests/fixtures/firewall4-fullcone-refreshed.patch").read_bytes() + TAIL

# This default block is from firewall4 3722eadc088c3ab81389596ca9796112b436583a.
NEW_DEFAULTS = """config defaults
\toption syn_flood\t1
\toption input\t\tREJECT
\toption output\t\tACCEPT
\toption forward\t\tREJECT

config zone
\toption name\t\tlan
\tlist   network\t\t'lan'
\toption input\t\tACCEPT
\toption output\t\tACCEPT
\toption forward\t\tACCEPT
"""
REMOVED_COMMENTS = "# Uncomment this line to disable ipv6 rules\n#\toption disable_ipv6\t1\n"
OLD_DEFAULTS = NEW_DEFAULTS.replace("\toption forward\t\tREJECT\n", "\toption forward\t\tREJECT\n" + REMOVED_COMMENTS, 1)


def first_file_diff(data):
    return data.split(b"--- a/root/usr/share/firewall4/templates/ruleset.uc", 1)[0]


class Firewall4PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="firewall4-context-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch_path = self.root / "001-firewall4-add-support-for-fullcone-nat.patch"

    def write_patch(self, data=ORIGINAL):
        self.patch_path.write_bytes(data)
        return self.patch_path

    def apply_config(self, patch_bytes, defaults):
        build_root = self.root / "build"
        target = build_root / "root/etc/config/firewall"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(defaults, encoding="utf-8", newline="\n")
        result = subprocess.run([PATCH_PROGRAM, "-p1", "--batch", "--fuzz=0"],
                                input=first_file_diff(patch_bytes), cwd=build_root,
                                capture_output=True)
        return result, target.read_text(encoding="utf-8")

    def assert_default_options(self, defaults):
        block = defaults.split("config zone", 1)[0]
        for key, value in (("flow_offloading", "1"), ("flow_offloading_hw", "1"),
                           ("fullcone", "1"), ("fullcone6", "0")):
            fields = [line.split() for line in block.splitlines()]
            matches = [x for x in fields if len(x) == 3 and x[:2] == ["option", key]]
            self.assertEqual(matches, [["option", key, value]], key)

    @unittest.skipUnless(PATCH_PROGRAM, "GNU patch is required for the regression fixture")
    def test_original_fails_against_new_upstream_without_ipv6_comments(self):
        result, defaults = self.apply_config(ORIGINAL, NEW_DEFAULTS)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Hunk #1 FAILED", result.stdout + result.stderr)
        self.assertEqual(defaults, NEW_DEFAULTS)

    @unittest.skipUnless(PATCH_PROGRAM, "GNU patch is required for the regression fixture")
    def test_repaired_patch_applies_to_old_and_new_defaults_with_zero_fuzz(self):
        self.write_patch()
        self.assertTrue(firewall4.refresh_patch(self.patch_path))
        fixed = self.patch_path.read_bytes()
        for defaults in (NEW_DEFAULTS, OLD_DEFAULTS):
            with self.subTest(old_comments=REMOVED_COMMENTS in defaults):
                result, changed = self.apply_config(fixed, defaults)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(b"fuzz", result.stdout + result.stderr)
                self.assert_default_options(changed)
                self.assertEqual(REMOVED_COMMENTS in changed, REMOVED_COMMENTS in defaults)
                self.assertEqual(changed.split("config zone", 1)[1], defaults.split("config zone", 1)[1])

    def test_only_config_hunk_changes_and_repeated_refresh_is_idempotent(self):
        self.write_patch()
        self.assertTrue(firewall4.refresh_patch(self.patch_path))
        updated = self.patch_path.read_bytes()
        self.assertEqual(updated, UPDATED)
        self.assertFalse(firewall4.refresh_patch(self.patch_path))
        self.assertEqual(self.patch_path.read_bytes(), updated)

    def test_already_refreshed_upstream_patch_is_preserved(self):
        self.write_patch(UPDATED)
        self.assertFalse(firewall4.refresh_patch(self.patch_path))
        self.assertEqual(self.patch_path.read_bytes(), UPDATED)

    def test_immortalwrt_upstream_refresh_is_preserved_including_crlf(self):
        for data in (UPSTREAM, UPSTREAM.replace(b"\n", b"\r\n")):
            with self.subTest(crlf=b"\r\n" in data):
                self.write_patch(data)
                self.assertFalse(firewall4.refresh_patch(self.patch_path))
                self.assertEqual(self.patch_path.read_bytes(), data)

    @unittest.skipUnless(PATCH_PROGRAM, "GNU patch is required for the regression fixture")
    def test_immortalwrt_upstream_refresh_applies_without_local_changes(self):
        self.write_patch(UPSTREAM)
        self.assertFalse(firewall4.refresh_patch(self.patch_path))
        result, defaults = self.apply_config(self.patch_path.read_bytes(), NEW_DEFAULTS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_default_options(defaults)

    def test_cli_accepts_immortalwrt_upstream_refresh(self):
        self.write_patch(UPSTREAM)
        result = subprocess.run([sys.executable, str(MODULE), "--patch", str(self.patch_path)],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.patch_path.read_bytes(), UPSTREAM)

    def test_future_upstream_context_without_obsolete_comments_is_not_rewritten(self):
        updated = UPSTREAM.replace(b"@@ -3,6 +3,10 @@ config defaults",
                                   b"@@ -3,6 +3,10 @@ refreshed defaults section", 1)
        self.assertNotEqual(updated, UPSTREAM)
        self.write_patch(updated)
        self.assertFalse(firewall4.refresh_patch(self.patch_path))
        self.assertEqual(self.patch_path.read_bytes(), updated)

    def test_crlf_and_later_file_diffs_are_preserved(self):
        original = ORIGINAL.replace(b"\n", b"\r\n")
        expected = UPDATED.replace(b"\n", b"\r\n")
        self.write_patch(original)
        self.assertTrue(firewall4.refresh_patch(self.patch_path))
        self.assertEqual(self.patch_path.read_bytes(), expected)
        self.assertFalse(firewall4.refresh_patch(self.patch_path))
        self.assertEqual(self.patch_path.read_bytes(), expected)

    def test_unknown_context_or_changed_default_value_does_not_rewrite(self):
        variants = (
            ORIGINAL.replace(b" \toption forward\t\tREJECT\n", b" \toption forward\t\tACCEPT\n", 1),
            ORIGINAL.replace(b"+\toption fullcone\t\t1\n", b"+\toption fullcone\t\t0\n", 1),
            ORIGINAL.replace(b"@@ -5,6 +5,10 @@ config defaults", b"@@ -5,6 +5,10 @@ different section", 1),
        )
        for index, data in enumerate(variants):
            with self.subTest(case=index):
                self.assertNotEqual(data, ORIGINAL)
                self.write_patch(data)
                with self.assertRaises(ValueError):
                    firewall4.refresh_patch(self.patch_path)
                self.assertEqual(self.patch_path.read_bytes(), data)

    def test_missing_patch_fails(self):
        with self.assertRaises((ValueError, OSError)):
            firewall4.refresh_patch(self.patch_path)

    def test_cli_missing_patch_is_fatal(self):
        result = subprocess.run([sys.executable, str(MODULE), "--patch", str(self.patch_path)],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.patch_path.exists())

    def test_cli_repairs_known_patch_and_succeeds_when_repeated(self):
        self.write_patch()
        for repeat in range(2):
            with self.subTest(repeat=repeat):
                result = subprocess.run([sys.executable, str(MODULE), "--patch", str(self.patch_path)],
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.patch_path.read_bytes(), UPDATED)


if __name__ == "__main__":
    unittest.main()
