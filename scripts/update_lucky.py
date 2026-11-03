"""Resolve the official latest stable Lucky binary for this x86-64 build."""
import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.request

API = "https://api.github.com/repos/gdy666/lucky/releases/latest"


def fetch_release():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "OpenWrt-custom-build"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(API, headers=headers), timeout=30) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def select_asset(release):
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("Lucky release is not a published stable release")
    tag = release.get("tag_name", "")
    match = re.fullmatch(r"v?(\d+\.\d+\.\d+)", tag)
    if not match:
        raise ValueError("Unexpected Lucky stable tag")
    version = match[1]
    filename = f"lucky_{version}_Linux_x86_64.tar.gz"
    assets = [a for a in release.get("assets", []) if a.get("name") == filename]
    if len(assets) != 1:
        raise ValueError("Latest Lucky release must contain one Linux x86_64 archive")
    asset = assets[0]
    url = f"https://github.com/gdy666/lucky/releases/download/{tag}/{filename}"
    if asset.get("browser_download_url") != url:
        raise ValueError("Unexpected Lucky archive download URL")
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("Lucky archive has no official SHA256 digest")
    return tag, version, filename, url, digest[7:].lower()


def replace_one(text, pattern, value, label):
    text, count = re.subn(pattern, lambda _: value, text, flags=re.M)
    if count != 1:
        raise ValueError(f"Expected one Lucky {label}; upstream Makefile needs review")
    return text


def prepare_package(package, provenance, release):
    tag, version, filename, url, digest = select_asset(release)
    path = package / "Makefile"
    text = path.read_text(encoding="utf-8")
    text = replace_one(text, r"^PKG_VERSION\s*:?=.*$", f"PKG_VERSION:={version}", "version")
    text = replace_one(text, r"^PKG_RELEASE\s*:?=.*$", "PKG_RELEASE:=1", "release")
    # Replace the feed's unverified wget in Build/Prepare with OpenWrt's normal
    # download machinery. make download validates PKG_HASH before packaging.
    text = re.sub(r"^PKG_(?:SOURCE|SOURCE_URL|HASH|SOURCE_VERSION)\s*:?=.*\n?", "", text, flags=re.M)
    source = f"PKG_SOURCE:={filename}\nPKG_SOURCE_URL:={url.rsplit('/', 1)[0]}\nPKG_HASH:={digest}\n"
    text = replace_one(text, r"^PKG_RELEASE:=1$", "PKG_RELEASE:=1\n\n" + source, "source block")
    text = replace_one(text, r"(?ms)^define Build/Prepare\n.*?^endef$",
                       "define Build/Prepare\n\tmkdir -p $(PKG_BUILD_DIR)\n\ttar -xzf $(DL_DIR)/$(PKG_SOURCE) -C $(PKG_BUILD_DIR)\n\ttest -s $(PKG_BUILD_DIR)/lucky\nendef", "prepare block")
    text = replace_one(text, r"^[ \t]*DEPENDS:=.*$", "\tDEPENDS:=@x86_64", "target dependency")
    metadata = json.loads(provenance.read_text(encoding="utf-8")) if provenance.exists() else {}
    if not isinstance(metadata, dict):
        raise ValueError("Build provenance must be a JSON object")
    metadata.update({"gdy666/lucky (release)": tag, "gdy666/lucky (archive sha256)": digest})
    # Validate everything before changing the package.
    path.write_text(text, encoding="utf-8", newline="\n")
    provenance.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    args = parser.parse_args()
    try:
        tag = prepare_package(args.package, args.provenance, fetch_release())
    except (OSError, ValueError) as error:
        raise SystemExit(f"Cannot prepare latest stable Lucky; refusing an old-core fallback: {error}")
    print(f"Lucky official stable {tag}: Linux x86_64 archive pinned with SHA256 for this build.")


if __name__ == "__main__":
    main()
