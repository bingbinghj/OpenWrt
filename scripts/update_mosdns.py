"""Pin the latest stable sbwml MosDNS tree while retaining its stats API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

PATCHED_REPOSITORY = "sbwml/luci-app-mosdns"
CORE_REPOSITORY = "IrineSistiana/mosdns"
ORIGIN = "https://github.com/" + PATCHED_REPOSITORY
SHA = re.compile(r"[0-9a-f]{40}")


def github(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "OpenWrt-custom-build"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/" + path, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def stable_release(release, patched=False):
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("MosDNS release must be published and stable")
    tag = release.get("tag_name", "")
    pattern = r"v(\d+\.\d+\.\d+)-r(\d+)" if patched else r"v(\d+\.\d+\.\d+)"
    match = re.fullmatch(pattern, tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError("Unexpected stable MosDNS release tag")
    return tag, match[1], match[2] if patched else ""


def resolve_tag_commit(tag):
    data = github(f"repos/{PATCHED_REPOSITORY}/git/ref/tags/{tag}")
    for _ in range(4):
        obj = data.get("object", {}) if isinstance(data, dict) else {}
        commit = obj.get("sha", "")
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise ValueError("Invalid MosDNS release tag SHA")
        if obj.get("type") == "commit":
            return commit
        if obj.get("type") != "tag":
            raise ValueError("MosDNS release tag does not resolve to a commit")
        data = github(f"repos/{PATCHED_REPOSITORY}/git/tags/{commit}")
    raise ValueError("MosDNS release tag has too many indirections")


def git(tree, *args):
    result = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    if result.returncode:
        raise ValueError(f"MosDNS git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout if args[0] == "show" else result.stdout.strip()


def fetch_tag(tree, tag):
    git(tree, "fetch", "--depth", "1", "origin", "refs/tags/" + tag)
    return git(tree, "rev-parse", "FETCH_HEAD^{commit}")


def assignment(text, name):
    values = re.findall(r"^" + re.escape(name) + r"[ \t]*:?=[ \t]*(.*)$", text, re.M)
    if len(values) != 1:
        raise ValueError(f"Expected one MosDNS {name}; upstream recipe needs review")
    return values[0].strip()


def validate_tree(tree, commit, version, revision):
    recipe = git(tree, "show", commit + ":mosdns/Makefile")
    expected = {
        "PKG_NAME": "mosdns", "PKG_VERSION": version, "PKG_RELEASE": revision,
        "PKG_SOURCE_URL": "https://codeload.github.com/IrineSistiana/mosdns/tar.gz/v$(PKG_VERSION)?",
        "GO_PKG": "github.com/IrineSistiana/mosdns",
    }
    for name, value in expected.items():
        if assignment(recipe, name) != value:
            raise ValueError(f"MosDNS release tag and {name} do not match")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", assignment(recipe, "PKG_HASH")):
        raise ValueError("MosDNS core source hash is missing")
    ui = git(tree, "show", commit + ":luci-app-mosdns/Makefile")
    converter = git(tree, "show", commit + ":geo2txt/Makefile")
    git(tree, "cat-file", "-e", commit + ":luci-app-mosdns/root/etc/init.d/mosdns")
    if assignment(ui, "PKG_NAME") != "luci-app-mosdns" or assignment(converter, "PKG_NAME") != "geo2txt":
        raise ValueError("MosDNS release package tree is incomplete")
    files = git(tree, "ls-tree", "-r", "--name-only", commit, "--", "mosdns/patches").splitlines()
    for path in files:
        if not path.endswith(".patch") or "stats_api" not in path:
            continue
        patch = git(tree, "show", commit + ":" + path)
        if '+const PluginType = "stats_api"' in patch and 'plugin/executable/stats_api"' in patch:
            return path, hashlib.sha256(patch.encode("utf-8")).hexdigest()
    raise ValueError("MosDNS stable release is missing the stats_api registration patch")


def prepare_tree(tree, provenance, release, core_release, commit):
    tag, version, revision = stable_release(release, patched=True)
    core_tag, core_version, _ = stable_release(core_release)
    if version != core_version:
        raise ValueError(f"Official MosDNS {core_tag} and sbwml {tag} differ; stats_api compatibility needs review")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError("Invalid expected MosDNS release commit")
    if not (tree / ".git").exists() or Path(git(tree, "rev-parse", "--show-toplevel")).resolve() != tree.resolve():
        raise ValueError("MosDNS package tree must be its own git checkout")
    if git(tree, "remote", "get-url", "origin").rstrip("/") not in (ORIGIN, ORIGIN + ".git"):
        raise ValueError("MosDNS git origin is not the official sbwml repository")
    if git(tree, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("MosDNS package tree has local changes; refusing to overwrite them")
    metadata = json.loads(provenance.read_text(encoding="utf-8")) if provenance.exists() else {}
    if not isinstance(metadata, dict):
        raise ValueError("Build provenance must be a JSON object")
    actual = fetch_tag(tree, tag)
    if actual != commit:
        raise ValueError("Fetched MosDNS tag SHA does not match the official release metadata")
    stats_path, stats_hash = validate_tree(tree, commit, version, revision)
    metadata.update({
        "sbwml/luci-app-mosdns (release)": tag,
        "sbwml/luci-app-mosdns (commit)": commit,
        "sbwml/luci-app-mosdns (stats patch)": stats_path,
        "sbwml/luci-app-mosdns (stats patch sha256)": stats_hash,
        "IrineSistiana/mosdns (release)": core_tag,
    })
    git(tree, "checkout", "--detach", commit)
    provenance.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-tree", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    args = parser.parse_args()
    try:
        release = github(f"repos/{PATCHED_REPOSITORY}/releases/latest")
        core = github(f"repos/{CORE_REPOSITORY}/releases/latest")
        tag, _, _ = stable_release(release, patched=True)
        commit = resolve_tag_commit(tag)
        tag = prepare_tree(args.package_tree, args.provenance, release, core, commit)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"Cannot prepare stable MosDNS with stats_api; refusing a fallback: {error}")
    print(f"MosDNS official stable {tag}: matching upstream core and stats_api package tree pinned.")


if __name__ == "__main__":
    main()
