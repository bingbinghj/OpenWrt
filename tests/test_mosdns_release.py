import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts/update_mosdns.py"
spec = importlib.util.spec_from_file_location("update_mosdns", MODULE)
mosdns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mosdns)


def release(tag):
    return {"tag_name": tag, "draft": False, "prerelease": False}


def command(tree, *args):
    return subprocess.check_output(["git", "-C", str(tree), *args], text=True, encoding="utf-8",
                                   stderr=subprocess.DEVNULL).strip()


def source_fixture(root, revision="15", stats=True):
    tree = root / "tree"
    tree.mkdir()
    command(tree, "init", "-q")
    command(tree, "config", "user.name", "Build tests")
    command(tree, "config", "user.email", "build-tests@example.invalid")
    command(tree, "config", "core.autocrlf", "false")
    command(tree, "remote", "add", "origin", mosdns.ORIGIN + ".git")
    recipe = tree / "mosdns/Makefile"
    recipe.parent.mkdir()
    recipe.write_text("old tracked package\n", encoding="utf-8")
    command(tree, "add", ".")
    command(tree, "commit", "-qm", "old package")
    old = command(tree, "rev-parse", "HEAD")
    recipe.write_text(
        "PKG_NAME:=mosdns\nPKG_VERSION:=5.3.4\nPKG_RELEASE:=" + revision + "\n"
        "PKG_SOURCE_URL:=https://codeload.github.com/IrineSistiana/mosdns/tar.gz/v$(PKG_VERSION)?\n"
        "GO_PKG:=github.com/IrineSistiana/mosdns\nPKG_HASH:=" + "a" * 64 + "\n", encoding="utf-8")
    for name, text in {
        "luci-app-mosdns/Makefile": "PKG_NAME:=luci-app-mosdns\n",
        "geo2txt/Makefile": "PKG_NAME:=geo2txt\n",
        "luci-app-mosdns/root/etc/init.d/mosdns": "package init script\n",
        "mosdns/patches/216-plugin-add-stats_api-plugin.patch": (
            '+const PluginType = "stats_api"\n+ _ "github.com/IrineSistiana/mosdns/v5/plugin/executable/stats_api"\n'
            if stats else "patch without plugin registration\n"),
    }.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    command(tree, "add", ".")
    command(tree, "commit", "-qm", "stable package")
    commit = command(tree, "rev-parse", "HEAD")
    command(tree, "checkout", "--detach", "-q", old)
    provenance = root / "custom-sources.json"
    provenance.write_text('{"feed":"original"}', encoding="utf-8")
    return tree, provenance, old, commit


class MosDNSReleaseTests(unittest.TestCase):
    def test_stable_release_flags_and_tags_are_strict(self):
        base = release("v5.3.4-r15")
        for key, value in (("draft", True), ("prerelease", True), ("tag_name", "v5.3.4-r15-beta")):
            item = copy.deepcopy(base); item[key] = value
            with self.subTest(item=item), self.assertRaises(ValueError):
                mosdns.stable_release(item, patched=True)
        self.assertEqual(mosdns.stable_release(base, patched=True), ("v5.3.4-r15", "5.3.4", "15"))

    def test_release_commit_checkout_retains_ui_converter_and_stats_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            tree, provenance, _, commit = source_fixture(Path(directory))
            with patch.object(mosdns, "fetch_tag", return_value=commit) as fetch:
                mosdns.prepare_tree(tree, provenance, release("v5.3.4-r15"), release("v5.3.4"), commit)
            fetch.assert_called_once_with(tree, "v5.3.4-r15")
            self.assertEqual(command(tree, "rev-parse", "HEAD"), commit)
            self.assertEqual(command(tree, "status", "--porcelain"), "")
            self.assertTrue((tree / "geo2txt/Makefile").is_file())
            self.assertTrue((tree / "luci-app-mosdns/root/etc/init.d/mosdns").is_file())
            self.assertIn('PluginType = "stats_api"', (tree / "mosdns/patches/216-plugin-add-stats_api-plugin.patch").read_text())
            metadata = json.loads(provenance.read_text(encoding="utf-8"))
            self.assertTrue(all(isinstance(value, str) for value in metadata.values()))
            self.assertEqual(metadata["feed"], "original")
            self.assertEqual(metadata["sbwml/luci-app-mosdns (commit)"], commit)

    def test_upstream_core_ahead_stops_before_fetch_or_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            tree, provenance, old, commit = source_fixture(Path(directory))
            before = provenance.read_bytes()
            with patch.object(mosdns, "fetch_tag") as fetch, self.assertRaisesRegex(ValueError, "compatibility needs review"):
                mosdns.prepare_tree(tree, provenance, release("v5.3.4-r15"), release("v5.4.0"), commit)
            fetch.assert_not_called()
            self.assertEqual(command(tree, "rev-parse", "HEAD"), old)
            self.assertEqual(provenance.read_bytes(), before)

    def test_dirty_checkout_or_other_origin_never_overwrites_local_work(self):
        for dirty in (True, False):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as directory:
                tree, provenance, old, commit = source_fixture(Path(directory))
                if dirty:
                    (tree / "mosdns/Makefile").write_text("user change\n", encoding="utf-8")
                else:
                    command(tree, "remote", "set-url", "origin", "https://example.invalid/mosdns.git")
                before = (tree / "mosdns/Makefile").read_bytes(), provenance.read_bytes()
                with patch.object(mosdns, "fetch_tag") as fetch, self.assertRaises(ValueError):
                    mosdns.prepare_tree(tree, provenance, release("v5.3.4-r15"), release("v5.3.4"), commit)
                fetch.assert_not_called()
                self.assertEqual(command(tree, "rev-parse", "HEAD"), old)
                self.assertEqual(((tree / "mosdns/Makefile").read_bytes(), provenance.read_bytes()), before)

    def test_wrong_commit_missing_stats_or_recipe_mismatch_leaves_old_checkout(self):
        for error in ("commit", "stats", "revision"):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                tree, provenance, old, commit = source_fixture(Path(directory),
                                                              revision="14" if error == "revision" else "15",
                                                              stats=error != "stats")
                before = provenance.read_bytes()
                with patch.object(mosdns, "fetch_tag", return_value=old if error == "commit" else commit):
                    with self.assertRaises(ValueError):
                        mosdns.prepare_tree(tree, provenance, release("v5.3.4-r15"), release("v5.3.4"), commit)
                self.assertEqual(command(tree, "rev-parse", "HEAD"), old)
                self.assertEqual(provenance.read_bytes(), before)

    def test_annotated_official_tag_resolves_to_commit(self):
        tagged = {"object": {"type": "tag", "sha": "a" * 40}}
        resolved = {"object": {"type": "commit", "sha": "b" * 40}}
        with patch.object(mosdns, "github", side_effect=[tagged, resolved]):
            self.assertEqual(mosdns.resolve_tag_commit("v5.3.4-r15"), "b" * 40)
        with patch.object(mosdns, "github", return_value={"object": {"type": "tree", "sha": "b" * 40}}):
            with self.assertRaises(ValueError):
                mosdns.resolve_tag_commit("v5.3.4-r15")


if __name__ == "__main__":
    unittest.main()
