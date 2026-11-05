#!/usr/bin/env python3
"""Track successfully published firmware and generate upstream release notes."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


MARKER = re.compile(r"<!-- immortalwrt-build (\{[^\n]*\}) -->")
SHA = re.compile(r"[0-9a-f]{40}")
CATEGORIES = ("新增功能与支持", "问题修复", "版本升级与功能调整", "其他变更")


def github(path, method="GET", body=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "immortalwrt-build"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = Request(f"https://api.github.com/{path}", headers=headers, method=method, data=data)
    # A dispatch can be accepted before a gateway error reaches us. Its caller
    # must observe queued builds before retrying, rather than blindly repeating POST.
    attempts = 1 if method == "POST" else 3
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response) if response.status != 204 else None
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
        except (URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(2 ** attempt)


def active_build(repository, api=github):
    page = 1
    while True:
        result = api(f"repos/{repository}/actions/workflows/x86_64.yml/runs?per_page=100&page={page}")
        runs = result.get("workflow_runs") if isinstance(result, dict) else None
        if not isinstance(runs, list) or any(
                not isinstance(run, dict) or not isinstance(run.get("status"), str)
                for run in runs):
            raise ValueError("GitHub returned an invalid workflow run list; refusing to dispatch")
        if any(run["status"] != "completed" for run in runs):
            return True
        if len(runs) < 100:
            return False
        page += 1


def trigger_build(repository, ref, commit, api=github, sleep=time.sleep, attempts=3):
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError("Invalid upstream commit for workflow dispatch")
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("A workflow ref is required")
    if not 1 <= attempts <= 3:
        raise ValueError("Workflow dispatch permits one to three attempts")
    path = f"repos/{repository}/actions/workflows/x86_64.yml/dispatches"
    for attempt in range(1, attempts + 1):
        if active_build(repository, api):
            print("An existing firmware build is active; defer until the next check.")
            return False
        try:
            api(path, method="POST", body={"ref": ref, "inputs": {"upstream_commit": commit}})
            print(f"Firmware build dispatched for upstream commit {commit}.")
            return True
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                raise
            failure = error
            reason = f"HTTP {error.code}"
        except (URLError, TimeoutError) as error:
            failure = error
            reason = "network timeout or connection failure"
        print(f"Dispatch attempt {attempt}/{attempts} failed ({reason}); checking for an accepted build.")
        # Allow eventually visible queued runs to appear, including after the last
        # failed request. Read failures remain fatal; they never mean 'idle'.
        for delay in (5, 10):
            sleep(delay)
            if active_build(repository, api):
                print("A firmware build appeared after the failed dispatch; do not dispatch again.")
                return False
        if attempt == attempts:
            raise failure


def release_metadata(release, upstream, branch, target):
    if release.get("draft") or release.get("prerelease"):
        return None
    match = MARKER.search(release.get("body") or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if (data.get("schema"), data.get("upstream_repository"),
            data.get("upstream_branch"), data.get("target")) != (1, upstream, branch, target):
        return None
    commit = data.get("upstream_commit")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        return None
    return data


def published_releases(repository, upstream, branch, target, api=github):
    # /releases/latest can refer to a manual release or a different device.
    # Sort by publication time, not the date of the configuration commit/tag.
    candidates = []
    page = 1
    while True:
        releases = api(f"repos/{repository}/releases?per_page=100&page={page}")
        for release in releases:
            data = release_metadata(release, upstream, branch, target)
            if data:
                candidates.append((release, data))
        if len(releases) < 100:
            break
        page += 1
    return sorted(candidates, key=lambda pair: pair[0].get("published_at") or "", reverse=True)


def git(directory, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(directory), *arguments], encoding="utf-8", errors="replace"
    ).strip()


def markdown(text):
    text = html.escape(" ".join(text.split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~])", r"\\\1", text)


def change_category(subject):
    """Conservative title-based classification; do not invent feature details."""
    title = subject.lower()
    # Classify the action, not package names (e.g. package 'adduser').
    action = title.rsplit(":", 1)[-1].strip()
    conventional = title.split(":", 1)[0]
    if re.match(r"fix(?:\(|!|$)", conventional) or re.search(
            r"\b(fix(?:es|ed)?|bugfix|resolve|correct|add missing)\b|修复|修正", action):
        return "问题修复"
    if re.match(r"(remove|drop|disable|revert)\b", action):
        return "版本升级与功能调整"
    if re.match(r"feat(?:\(|!|$)", conventional) or re.search(
            r"^(add|introduce|implement|enable|support)\b|新增|添加", action):
        return "新增功能与支持"
    if re.search(r"\b(update|upgrade|bump|sync|switch|change|adjust|remove|drop|disable|refresh|backport)\b|更新|升级|调整|移除", action):
        return "版本升级与功能调整"
    return "其他变更"


def grouped_changes(entries, url, limit):
    changes = [entry.split("\t", 1) for entry in entries.splitlines() if entry]
    counts = {name: 0 for name in CATEGORIES}
    for _, title in changes:
        counts[change_category(title)] += 1
    lines = ["", "### 更新摘要", ""]
    lines.append("按上游提交标题分类：" + "；".join(f"{name} {counts[name]} 个提交" for name in CATEGORIES) + "。")
    lines.append("保留上游原始标题；分类只概括提交说明，不代表这些功能均已选入本固件。")
    visible = changes[:limit]
    for name in CATEGORIES:
        lines += ["", f"### {name}", ""]
        matching = [(commit, title) for commit, title in visible if change_category(title) == name]
        if matching:
            for commit, title in matching:
                lines.append(f"- {markdown(title)} ([`{commit[:12]}`]({url}/commit/{commit}))")
        elif counts[name]:
            lines.append("本类提交未包含在展示范围内，请查看完整提交对比。")
        else:
            lines.append("所列上游提交中没有明确归入此类的内容。")
    return lines


def changelog(directory, upstream, current, previous=None, limit=100):
    if not SHA.fullmatch(current):
        raise ValueError("Invalid current upstream commit")
    url = f"https://github.com/{upstream}"
    notes = []
    revision = current
    exact_range = False
    if previous:
        base = previous[1]["upstream_commit"]
        if not SHA.fullmatch(base):
            raise ValueError("Invalid previous upstream commit")
        notes.append(f"上次成功发布：[`{base[:12]}`]({url}/commit/{base})")
        notes.append(f"[完整提交对比]({url}/compare/{base}...{current})")
        if base == current:
            return notes + ["", "ImmortalWrt 主仓库无新增提交；本次为重新构建，软件包或本仓库配置可能有变化。"]
        ancestor = subprocess.run(
            ["git", "-C", str(directory), "merge-base", "--is-ancestor", base, current],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if ancestor.returncode == 0:
            revision = f"{base}..{current}"
            exact_range = True
            count = int(git(directory, "rev-list", "--count", revision))
            notes += ["", f"自上次成功发布以来，共 {count} 个上游提交（最多展示 {limit} 个）："]
        else:
            limit = min(limit, 20)
            notes += ["", "上次提交不在当前历史中（可能是上游历史变更）。以下仅列出近期提交，请使用对比链接核查。"]
    else:
        limit = min(limit, 20)
        notes.append("首次生成更新记录：以往 Release 没有可验证的上游提交记录，以下为近期提交，不代表精确的两次固件差异。")
    # Count all commits for the summary, even when the visible list is capped.
    log_options = [] if exact_range else [f"-n{limit}"]
    entries = git(directory, "log", *log_options, "--format=%H%x09%s", revision)
    return notes + grouped_changes(entries, url, limit)


def component_revisions(directory):
    directory = Path(directory)
    paths = sorted((directory / "feeds").glob("*"))
    paths += [directory / name for name in (
        "feeds/packages/lang/golang", "package/mosdns", "package/v2ray-geodata")]
    return {str(path.relative_to(directory)).replace("\\", "/"): git(path, "rev-parse", "HEAD")
            for path in paths if (path / ".git").exists()}


def component_revision(value):
    if value is None or value == "":
        return "无记录"
    value = str(value)
    # Only hashes need shortening; Smart versions must retain their suffix.
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        value = value[:12]
    elif len(value) > 160:
        value = value[:157] + "..."
    value = html.escape(value, quote=False).replace("`", "'").replace("|", r"\|").replace("\n", " ").replace("\r", " ")
    return "`" + value + "`"


def component_changes(components, previous):
    old = previous[1].get("components", {}) if previous else {}
    changed = [(name, old.get(name), revision) for name, revision in sorted(components.items())
               if old.get(name) != revision]
    removed = sorted(set(old) - set(components))
    lines = ["", "## 构建组件变化", ""]
    if not previous:
        lines.append("首次记录各来源的当前版本；后续只展示与上次发布不同的来源。")
    if changed:
        lines += ["", "| 来源 | 上次记录 | 本次记录 |", "| --- | --- | --- |"]
        for name, before, after in changed:
            lines.append(f"| {markdown(name)} | {component_revision(before)} | {component_revision(after)} |")
    for name in removed:
        lines.append(f"- 本次不再记录来源：{markdown(name)}")
    if not changed and not removed:
        lines.append("已记录的组件来源版本及 OpenClash 核心哈希没有变化。")
    return lines


def generate(args, previous):
    current = git(args.source, "rev-parse", "HEAD")
    config = Path(args.source) / ".config"
    components = component_revisions(args.source)
    custom_sources = Path(args.source) / "custom-sources.json"
    if custom_sources.exists():
        components.update(json.loads(custom_sources.read_text(encoding="utf-8")))
    core = Path(args.source) / "feeds/luci/applications/luci-app-openclash/root/etc/openclash/core/clash_meta"
    if core.exists():
        components["openclash/clash_meta (sha256)"] = hashlib.sha256(core.read_bytes()).hexdigest()
    metadata = {
        "schema": 1, "upstream_repository": args.upstream, "upstream_branch": args.branch,
        "upstream_commit": current, "target": args.target,
        "configuration_commit": os.environ.get("GITHUB_SHA", ""),
        "run_url": os.environ.get("GITHUB_SERVER_URL", "https://github.com") + "/" + args.repository
                   + "/actions/runs/" + os.environ.get("GITHUB_RUN_ID", ""),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "components": components,
    }
    lines = [
        "## 固件信息", "",
        f"- 编译目标：`{args.target}`",
        f"- ImmortalWrt：`{args.branch}` / [`{current[:12]}`](https://github.com/{args.upstream}/commit/{current})",
        f"- [本次构建日志]({metadata['run_url']})",
        f"- 构建配置：[`{metadata['configuration_commit'][:12]}`](https://github.com/{args.repository}/commit/{metadata['configuration_commit']})",
        "", "## ImmortalWrt 更新内容", "",
        *changelog(args.source, args.upstream, current, previous),
        *component_changes(components, previous),
        "", "以上范围来自 ImmortalWrt 主仓库。feeds、Go 工具链及自定义软件包的实际版本另见附件 `build-info.json`；主仓库提交可能涉及其他设备。",
        "", "<!-- immortalwrt-build " + json.dumps(metadata, ensure_ascii=True, sort_keys=True) + " -->", "",
    ]
    Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    Path(args.metadata).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prune(repository, releases, keep, current_tag, api=github):
    if keep < 1:
        raise ValueError("At least one published release must be retained")
    if not releases or releases[0][0]["tag_name"] != current_tag:
        raise ValueError("Cleanup requires the newly published release to be the latest managed release")
    for release, _ in releases[keep:]:
        # Only this workflow's structured releases are eligible. Keep Git tags,
        # legacy releases, drafts and manually published releases untouched.
        print(f"Removing old firmware release: {release['tag_name']}")
        api(f"repos/{repository}/releases/{release['id']}", method="DELETE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "notes", "prune", "dispatch"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"), required=not os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--upstream", default=os.environ.get("UPSTREAM_REPOSITORY", "immortalwrt/immortalwrt"))
    parser.add_argument("--branch", default=os.environ.get("REPO_BRANCH", "master"))
    parser.add_argument("--target", default=os.environ.get("BUILD_TARGET", "x86-64"))
    parser.add_argument("--source", default="openwrt")
    parser.add_argument("--output", default="release.md")
    parser.add_argument("--metadata", default="build-info.json")
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--current-tag")
    parser.add_argument("--ref", default=os.environ.get("WORKFLOW_REF"))
    parser.add_argument("--upstream-commit", default=os.environ.get("UPSTREAM_COMMIT"))
    args = parser.parse_args()
    if args.command == "dispatch":
        trigger_build(args.repository, args.ref, args.upstream_commit)
        return
    releases = published_releases(args.repository, args.upstream, args.branch, args.target)
    previous = releases[0] if releases else None
    if args.command == "notes":
        generate(args, previous)
        return
    if args.command == "prune":
        prune(args.repository, releases, args.keep, args.current_tag)
        return
    current = github(f"repos/{args.upstream}/commits/{quote(args.branch, safe='')}")["sha"]
    if not SHA.fullmatch(current):
        raise ValueError("GitHub returned an invalid upstream commit")
    changed = previous is None or previous[1]["upstream_commit"] != current
    output = f"upstream_commit={current}\nchanged={str(changed).lower()}\n"
    print(output, end="")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(output)


if __name__ == "__main__":
    main()
