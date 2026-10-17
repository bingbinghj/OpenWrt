import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upstream_release", ROOT / "scripts/upstream_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def published(commit="a" * 40, **overrides):
    data = {"schema": 1, "upstream_repository": "immortalwrt/immortalwrt",
            "upstream_branch": "master", "upstream_commit": commit, "target": "x86-64"}
    result = {"id": 1, "draft": False, "prerelease": False,
              "tag_name": "firmware-1", "published_at": "2026-10-01T00:00:00Z",
              "body": "<!-- immortalwrt-build " + json.dumps(data) + " -->"}
    result.update(overrides)
    return result


def metadata(item):
    return release.release_metadata(item, "immortalwrt/immortalwrt", "master", "x86-64")


class BaselineTests(unittest.TestCase):
    def test_update_summary_distinguishes_new_features_fixes_and_versions(self):
        cases = {"net: add support for device X": "新增功能与支持",
                 "feat(ui): show connection status": "新增功能与支持",
                 "kernel: fix network crash": "问题修复",
                 "build: add missing dependency": "问题修复",
                 "fix(api): handle empty requests": "问题修复",
                 "xray-core: update to 26.9.9": "版本升级与功能调整",
                 "kernel: bump to 6.12.1": "版本升级与功能调整",
                 "target: drop support for old hardware": "版本升级与功能调整",
                 "adduser: refresh patches": "版本升级与功能调整",
                 "treewide: whitespace cleanup": "其他变更"}
        for title, category in cases.items():
            with self.subTest(title=title):
                self.assertEqual(release.change_category(title), category)

    def test_component_summary_shows_only_changed_added_or_removed_sources(self):
        item = published()
        before = metadata(item)
        before["components"] = {"same": "a" * 40, "changed": "b" * 40, "removed": "c" * 40}
        lines = "\n".join(release.component_changes(
            {"same": "a" * 40, "changed": "d" * 40, "added": "e" * 40}, (item, before)))
        self.assertNotIn("same", lines)
        self.assertIn("changed", lines)
        self.assertIn("added", lines)
        self.assertIn("本次不再记录来源：removed", lines)

    def test_filters_unpublished_legacy_invalid_and_other_targets(self):
        wrong = published()
        wrong["body"] = wrong["body"].replace("x86-64", "arm64")
        for item in (published(draft=True), published(prerelease=True),
                     published(body="Old release"), published(body="<!-- immortalwrt-build {bad} -->"),
                     published(commit="--help"), wrong):
            with self.subTest(item=item):
                self.assertIsNone(metadata(item))
        self.assertEqual(metadata(published())["upstream_commit"], "a" * 40)

    def test_paginates_and_orders_by_publication_date(self):
        pages = [[published(body="legacy") for _ in range(100)], [
            published(tag_name="older"),
            published(tag_name="newer", published_at="2026-10-02T00:00:00Z")]]
        calls = []

        def api(path):
            calls.append(path)
            return pages[len(calls) - 1]

        result = release.published_releases("me/repo", "immortalwrt/immortalwrt", "master", "x86-64", api)
        self.assertEqual([x[0]["tag_name"] for x in result], ["newer", "older"])
        self.assertIn("page=2", calls[1])

    def test_api_failure_is_not_treated_as_first_release(self):
        def api(path):
            raise HTTPError(path, 403, "Forbidden", {}, None)
        with self.assertRaises(HTTPError):
            release.published_releases("me/repo", "immortalwrt/immortalwrt", "master", "x86-64", api)

    def test_failed_build_does_not_advance_checker_baseline(self):
        item = published()
        with patch.object(release, "published_releases", return_value=[(item, metadata(item))]), \
                patch.object(release, "github", return_value={"sha": "b" * 40}), \
                patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), \
                patch("sys.argv", ["upstream_release.py", "check", "--repository", "me/repo"]), \
                patch("builtins.print") as output:
            release.main()
            release.main()
        self.assertEqual(output.call_count, 2)
        self.assertTrue(all("changed=true" in call.args[0] for call in output.call_args_list))

    def test_published_revision_stops_repeated_dispatch(self):
        item = published()
        with patch.object(release, "published_releases", return_value=[(item, metadata(item))]), \
                patch.object(release, "github", return_value={"sha": "a" * 40}), \
                patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), \
                patch("sys.argv", ["upstream_release.py", "check", "--repository", "me/repo"]), \
                patch("builtins.print") as output:
            release.main()
        self.assertIn("changed=false", output.call_args.args[0])

    def test_cleanup_preserves_latest_three_and_requires_current_release(self):
        items = [published(tag_name=f"firmware-{i}", id=i) for i in range(5)]
        releases = [(item, metadata(item)) for item in items]
        calls = []
        release.prune("me/repo", releases, 3, "firmware-0", lambda *a, **kw: calls.append((a, kw)))
        self.assertEqual([x[0][0] for x in calls], ["repos/me/repo/releases/3", "repos/me/repo/releases/4"])
        self.assertTrue(all(x[1] == {"method": "DELETE"} for x in calls))
        with self.assertRaises(ValueError):
            release.prune("me/repo", releases, 3, "unpublished", lambda *a, **kw: self.fail("Unexpected deletion"))


class ChangelogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.path = Path(cls.temp.name)
        release.git(cls.path, "init", "-q", "-b", "main")
        cls.commits = []
        for title in ("base", "fix network", "update kernel", "fix [UI] <script> `code`"):
            release.git(cls.path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-qm", title)
            cls.commits.append(release.git(cls.path, "rev-parse", "HEAD"))
        (cls.path / ".config").write_text("CONFIG_TARGET_x86_64=y\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def previous(self, commit):
        item = published(commit=commit)
        return item, metadata(item)

    def test_exact_incremental_range_excludes_previous_commit(self):
        text = "\n".join(release.changelog(self.path, "immortalwrt/immortalwrt", self.commits[-1], self.previous(self.commits[0])))
        self.assertIn("共 3 个上游提交", text)
        self.assertIn("fix network", text)
        self.assertNotIn(") base", text)
        self.assertIn(f"compare/{self.commits[0]}...{self.commits[-1]}", text)

    def test_display_limit_does_not_change_total_count(self):
        lines = release.changelog(self.path, "immortalwrt/immortalwrt", self.commits[-1], self.previous(self.commits[0]), limit=2)
        self.assertEqual(sum(line.startswith("- ") for line in lines), 2)
        self.assertIn("共 3 个", "\n".join(lines))
        self.assertIn("问题修复 2 个提交", "\n".join(lines))
        self.assertIn("版本升级与功能调整 1 个提交", "\n".join(lines))

    def test_first_release_is_explicit_about_missing_baseline(self):
        text = "\n".join(release.changelog(self.path, "immortalwrt/immortalwrt", self.commits[-1]))
        self.assertIn("首次生成更新记录", text)
        self.assertIn("不代表精确", text)

    def test_same_revision_has_no_fake_updates(self):
        lines = release.changelog(self.path, "immortalwrt/immortalwrt", self.commits[-1], self.previous(self.commits[-1]))
        self.assertIn("无新增提交", "\n".join(lines))
        self.assertFalse(any(line.startswith("- ") for line in lines))

    def test_missing_or_divergent_history_falls_back_honestly(self):
        text = "\n".join(release.changelog(self.path, "immortalwrt/immortalwrt", self.commits[-1], self.previous("f" * 40)))
        self.assertIn("不在当前历史中", text)
        self.assertIn("fix network", text)

    def test_commit_titles_are_escaped(self):
        text = "\n".join(release.changelog(self.path, "immortalwrt/immortalwrt", self.commits[-1]))
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn(r"\[UI\]", text)

    def test_invalid_commit_cannot_be_a_git_option(self):
        with self.assertRaises(ValueError):
            release.changelog(self.path, "immortalwrt/immortalwrt", "--help")

    def test_generated_release_is_a_reusable_baseline(self):
        args = argparse.Namespace(source=str(self.path), repository="me/repo", upstream="immortalwrt/immortalwrt",
                                 branch="master", target="x86-64", output=str(self.path / "release.md"),
                                 metadata=str(self.path / "build-info.json"))
        with patch.dict(os.environ, {"GITHUB_SHA": "c" * 40, "GITHUB_RUN_ID": "123"}):
            release.generate(args, self.previous(self.commits[0]))
        body = Path(args.output).read_text(encoding="utf-8")
        manifest = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
        self.assertEqual(metadata(published(body=body)), manifest)
        self.assertEqual(manifest["upstream_commit"], self.commits[-1])
        self.assertEqual(len(manifest["config_sha256"]), 64)
        self.assertIn("/actions/runs/123", body)


if __name__ == "__main__":
    unittest.main()
