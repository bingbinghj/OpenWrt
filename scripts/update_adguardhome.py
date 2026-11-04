"""Pin the official stable AdGuard Home source and frontend for each build."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
import time
import urllib.request

API = "https://api.github.com/repos/AdguardTeam/AdGuardHome/releases/latest"
SOURCE_PREFIX = "https://codeload.github.com/AdguardTeam/AdGuardHome/tar.gz/"
RELEASE_PREFIX = "https://github.com/AdguardTeam/AdGuardHome/releases/download/"
FRONTEND_ASSET = "AdGuardHome_frontend.tar.gz"
DOWNLOAD_LIMIT = 128 * 1024 * 1024


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


def select_release(release):
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("AdGuard Home release must be published and stable")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Unexpected AdGuard Home stable tag")
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise ValueError("Invalid AdGuard Home release assets")
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == FRONTEND_ASSET]
    if len(matching) != 1:
        raise ValueError("AdGuard Home stable release needs one frontend archive")
    asset = matching[0]
    url = RELEASE_PREFIX + tag + "/" + FRONTEND_ASSET
    if asset.get("browser_download_url") != url:
        raise ValueError("Unexpected AdGuard Home frontend URL")
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("AdGuard Home frontend has no official SHA256 digest")
    return tag, tag[1:], url, digest[7:].lower()


def assignment(text, key):
    values = re.findall(r"^" + re.escape(key) + r"[ \t]*:?=[ \t]*(.*)$", text, re.M)
    if len(values) != 1:
        raise ValueError(f"Expected one AdGuard Home {key}; upstream recipe needs review")
    return values[0].strip()


def validate_recipe(text):
    expected = {
        "PKG_NAME": "adguardhome",
        "PKG_SOURCE": "$(PKG_NAME)-$(PKG_VERSION).tar.gz",
        "PKG_SOURCE_URL": SOURCE_PREFIX + "v$(PKG_VERSION)?",
        "PKG_BUILD_DIR": "$(BUILD_DIR)/AdGuardHome-$(PKG_VERSION)",
        "FRONTEND_DEST": "$(PKG_NAME)-frontend-$(PKG_VERSION).tar.gz",
        "FRONTEND_URL": RELEASE_PREFIX + "v$(PKG_VERSION)/",
        "GO_PKG": "github.com/AdguardTeam/AdGuardHome",
        "GO_PKG_BUILD_PKG": "$(GO_PKG)",
    }
    for key, value in expected.items():
        if assignment(text, key) != value:
            raise ValueError(f"Unexpected AdGuard Home {key}; upstream recipe needs review")
    if not re.fullmatch(r"\d+\.\d+\.\d+", assignment(text, "PKG_VERSION")):
        raise ValueError("Unexpected AdGuard Home package version")
    for key in ("PKG_HASH", "FRONTEND_HASH"):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", assignment(text, key)):
            raise ValueError(f"Unexpected AdGuard Home {key}")
    blocks = {
        "Download/adguardhome-frontend": ("URL:=$(FRONTEND_URL)", "URL_FILE:=" + FRONTEND_ASSET,
                                        "FILE:=$(FRONTEND_DEST)", "HASH:=$(FRONTEND_HASH)"),
        "Build/Prepare": ("$(call Build/Prepare/Default)",
                          "gzip -dc $(DL_DIR)/$(FRONTEND_DEST) | $(HOST_TAR) -C $(PKG_BUILD_DIR)/ $(TAR_OPTIONS)"),
        "Package/adguardhome/install": ("$(call GoPackage/Package/Install/Bin,$(1))",),
    }
    for name, required in blocks.items():
        matches = re.findall(r"(?ms)^define " + re.escape(name) + r"[ \t]*\n(.*?)^endef[ \t]*$", text)
        if len(matches) != 1 or any(line not in matches[0] for line in required):
            raise ValueError(f"Unexpected AdGuard Home {name}; upstream recipe needs review")
    if "$(eval $(call GoBinPackage,adguardhome))" not in text:
        raise ValueError("AdGuard Home Go packaging entry point is missing")


def archive_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(path, version, frontend=False):
    required = {"build/static/index.html"} if frontend else {
        f"AdGuardHome-{version}/go.mod", f"AdGuardHome-{version}/main.go",
    }
    found = set()
    total_size = 0
    with tarfile.open(path, "r:gz") as archive:
        for count, member in enumerate(archive, 1):
            if count > 100000 or member.size > DOWNLOAD_LIMIT:
                raise ValueError("AdGuard Home archive exceeds member limit")
            total_size += member.size
            if total_size > 512 * 1024 * 1024:
                raise ValueError("AdGuard Home archive exceeds unpacked size limit")
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError("Unsafe path in AdGuard Home archive")
            if name in required and member.isfile() and member.size:
                if name in found:
                    raise ValueError("Duplicate required file in AdGuard Home archive")
                found.add(name)
    if found != required:
        raise ValueError("AdGuard Home archive layout does not match the source build recipe")


def download_archive(directory, filename, url, version, expected_hash=None, frontend=False):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if expected_hash and target.is_file():
        if archive_hash(target) != expected_hash:
            raise ValueError("Cached AdGuard Home archive SHA256 mismatch")
        validate_archive(target, version, frontend)
        return expected_hash
    # GitHub's generated source archive has no asset digest. Fetch it from the
    # official tag on every build, then pin those exact bytes for make download.
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "OpenWrt-custom-build"})
            with urllib.request.urlopen(request, timeout=90) as response:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > DOWNLOAD_LIMIT:
                        raise ValueError("AdGuard Home archive exceeds download size limit")
                    stream.write(chunk)
            stream.close()
            digest = archive_hash(temporary)
            if expected_hash and digest != expected_hash:
                raise ValueError("AdGuard Home frontend SHA256 mismatch")
            validate_archive(temporary, version, frontend)
            temporary.replace(target)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)
    return digest


def replace_assignment(text, key, value):
    return re.sub(r"^" + re.escape(key) + r"[ \t]*:?=.*$", key + ":=" + value, text, flags=re.M)


def prepare_package(package, provenance, release, download_dir=None):
    tag, version, frontend_url, frontend_hash = select_release(release)
    path = package / "Makefile"
    original = path.read_text(encoding="utf-8")
    validate_recipe(original)
    metadata = json.loads(provenance.read_text(encoding="utf-8")) if provenance.exists() else {}
    if not isinstance(metadata, dict):
        raise ValueError("Build provenance must be a JSON object")
    downloads = download_dir or Path("dl")
    source_url = SOURCE_PREFIX + tag
    source_hash = download_archive(downloads, f"adguardhome-{version}.tar.gz", source_url, version)
    download_archive(downloads, f"adguardhome-frontend-{version}.tar.gz", frontend_url,
                     version, frontend_hash, frontend=True)
    text = original
    for key, value in (("PKG_VERSION", version), ("PKG_HASH", source_hash), ("FRONTEND_HASH", frontend_hash)):
        text = replace_assignment(text, key, value)
    metadata.update({
        "AdguardTeam/AdGuardHome (release)": tag,
        "AdguardTeam/AdGuardHome (release id)": str(release.get("id") or ""),
        "AdguardTeam/AdGuardHome (source url)": source_url,
        "AdguardTeam/AdGuardHome (source sha256)": source_hash,
        "AdguardTeam/AdGuardHome (frontend sha256)": frontend_hash,
        "AdguardTeam/AdGuardHome (build mode)": "Go source build with official release frontend",
    })
    path.write_text(text, encoding="utf-8", newline="\n")
    provenance.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--download-dir", type=Path, default=Path("dl"))
    args = parser.parse_args()
    try:
        tag = prepare_package(args.package, args.provenance, fetch_release(), args.download_dir)
    except (OSError, ValueError, tarfile.TarError) as error:
        raise SystemExit(f"Cannot prepare stable AdGuard Home; refusing an old-core fallback: {error}")
    print(f"AdGuard Home official stable {tag}: tagged source and SHA256-verified frontend pinned for this build.")


if __name__ == "__main__":
    main()
