"""Pin official stable EasyTier static x86-64 binaries for an OpenWrt build."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import time
import urllib.request
import zipfile

API = "https://api.github.com/repos/EasyTier/EasyTier/releases/latest"
PREFIX = "easytier-linux-x86_64/"
BINARIES = ("easytier-core", "easytier-cli", "easytier-web-embed")


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
        raise ValueError("EasyTier release must be published and stable")
    tag = release.get("tag_name", "")
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag)
    if not match:
        raise ValueError("Unexpected EasyTier stable tag")
    version = match[1]
    filename = f"easytier-linux-x86_64-{tag}.zip"
    assets = [a for a in release.get("assets", []) if a.get("name") == filename]
    if len(assets) != 1:
        raise ValueError("Latest EasyTier release needs one Linux x86_64 archive")
    url = f"https://github.com/EasyTier/EasyTier/releases/download/{tag}/{filename}"
    asset = assets[0]
    if asset.get("browser_download_url") != url:
        raise ValueError("Unexpected EasyTier archive URL")
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("EasyTier archive has no official SHA256 digest")
    return tag, version, filename, url, digest[7:].lower()


def validate_static_elf(data, name):
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 62:
        raise ValueError(f"{name} is not a Linux x86_64 ELF")
    offset = struct.unpack_from("<Q", data, 32)[0]
    entry_size, count = struct.unpack_from("<HH", data, 54)
    if entry_size < 56 or not count or offset + count * entry_size > len(data):
        raise ValueError(f"Invalid ELF program headers in {name}")
    types = [struct.unpack_from("<I", data, offset + index * entry_size)[0] for index in range(count)]
    if 2 in types or 3 in types:
        raise ValueError(f"{name} is dynamically linked; a static release is required")


def validate_archive(path, digest):
    with path.open("rb") as stream:
        archive_hash = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            archive_hash.update(chunk)
        if archive_hash.hexdigest() != digest:
            raise ValueError("EasyTier archive SHA256 mismatch")
    with zipfile.ZipFile(path) as archive:
        for name in BINARIES:
            entries = [i for i in archive.infolist() if i.filename == PREFIX + name]
            if len(entries) != 1 or not 64 <= entries[0].file_size <= 128 * 1024 * 1024:
                raise ValueError(f"EasyTier archive needs one bounded {name} binary")
            validate_static_elf(archive.read(entries[0]), name)


def download_archive(directory, filename, url, digest):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if target.exists():
        validate_archive(target, digest)
        return target
    # Never replace a verified download with partial or unverified bytes.
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "OpenWrt-custom-build"})
            with urllib.request.urlopen(request, timeout=90) as response:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise ValueError("EasyTier archive exceeds download size limit")
                    stream.write(chunk)
            stream.close()
            validate_archive(temporary, digest)
            temporary.replace(target)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)
    return target


def replace_one(text, pattern, value, label):
    text, count = re.subn(pattern, lambda _: value, text, flags=re.M)
    if count != 1:
        raise ValueError(f"Expected one EasyTier {label}; upstream package needs review")
    return text


def package_makefile(original, name, version, filename, url, digest):
    text = replace_one(original, r"^PKG_NAME\s*:?=.*$", "PKG_NAME:=" + name, "package name")
    text = re.sub(r"^PKG_(?:SOURCE(?:_\w+)?|HASH|RELEASE)\s*:?=.*\n?", "", text, flags=re.M)
    source = f"PKG_VERSION:={version}\nPKG_RELEASE:=1\nPKG_SOURCE:={filename}\nPKG_SOURCE_URL:={url.rsplit('/', 1)[0]}\nPKG_HASH:={digest}"
    text = replace_one(text, r"^PKG_VERSION\s*:?=.*$", source, "version")
    text = replace_one(text, r"^[ \t]*DEPENDS:=.*$", "\tDEPENDS:=@x86_64 +kmod-tun", "target dependency")
    binaries = BINARIES if name == "easytier" else BINARIES[:2]
    members = " ".join("'" + PREFIX + binary + "'" for binary in binaries)
    prepare = (
        "define Build/Prepare\n"
        "\tmkdir -p $(PKG_BUILD_DIR)\n"
        "\tprintf '%s  %s\\n' '$(PKG_HASH)' '$(DL_DIR)/$(PKG_SOURCE)' | sha256sum -c -\n"
        f"\tunzip -o -j $(DL_DIR)/$(PKG_SOURCE) {members} -d $(PKG_BUILD_DIR)\n"
        "\ttest -s $(PKG_BUILD_DIR)/easytier-core\n"
        "\ttest -s $(PKG_BUILD_DIR)/easytier-cli\nendef"
    )
    text = replace_one(text, r"(?ms)^define Build/Prepare\n.*?^endef$", prepare, "prepare block")
    text = replace_one(text, r"(?ms)^define Build/Compile\n.*?^endef$", "define Build/Compile\nendef", "compile block")
    install = (
        "define Package/$(PKG_NAME)/install\n"
        "\t$(INSTALL_DIR) $(1)/usr/bin\n"
        "\t$(INSTALL_BIN) $(PKG_BUILD_DIR)/easytier-core $(1)/usr/bin/easytier-core\n"
        "\t$(INSTALL_BIN) $(PKG_BUILD_DIR)/easytier-cli $(1)/usr/bin/easytier-cli\n"
    )
    if name == "easytier":
        if "config EASYTIER_INCLUDE_WEBCONSOLE" not in text:
            raise ValueError("EasyTier web-console configuration is missing")
        install += (
            "ifdef CONFIG_EASYTIER_INCLUDE_WEBCONSOLE\n"
            "\t$(INSTALL_BIN) $(PKG_BUILD_DIR)/easytier-web-embed $(1)/usr/bin/easytier-web\nendif\n"
        )
    install += "\tif [ -d ./root ]; then $(CP) ./root/. $(1)/; fi\nendef"
    return replace_one(text, r"(?ms)^define Package/\$\(PKG_NAME\)/install\n.*?^endef$", install, "install block")


def find_packages(source_root):
    packages = []
    feeds = source_root / "feeds"
    for name in ("easytier", "easytier-noweb"):
        for path in sorted(feeds.rglob(name + "/Makefile")):
            if re.search(r"^PKG_NAME\s*:?=" + re.escape(name) + r"\s*$", path.read_text(encoding="utf-8"), re.M):
                packages.append((path, name))
    if not any(name == "easytier" for _, name in packages):
        raise ValueError("No EasyTier feed package found in this OpenWrt source tree")
    return packages


def prepare_packages(source_root, provenance, release, download_dir=None):
    tag, version, filename, url, digest = select_asset(release)
    packages = find_packages(source_root)
    replacements = [(path, package_makefile(path.read_text(encoding="utf-8"), name, version, filename, url, digest))
                    for path, name in packages]
    metadata = json.loads(provenance.read_text(encoding="utf-8")) if provenance.exists() else {}
    if not isinstance(metadata, dict):
        raise ValueError("Build provenance must be a JSON object")
    metadata.update({"EasyTier/EasyTier (release)": tag, "EasyTier/EasyTier (archive sha256)": digest,
                     "EasyTier/EasyTier (packages)": ",".join(str(path.relative_to(source_root)).replace("\\", "/") for path, _ in packages)})
    download_archive(download_dir or source_root / "dl", filename, url, digest)
    # No package changes until metadata, recipe shape and static binaries pass.
    for path, text in replacements:
        path.write_text(text, encoding="utf-8", newline="\n")
    provenance.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("."))
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path)
    args = parser.parse_args()
    try:
        tag = prepare_packages(args.source_root, args.provenance, fetch_release(), args.download_dir)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise SystemExit(f"Cannot prepare stable EasyTier; refusing an old-core fallback: {error}")
    print(f"EasyTier official stable {tag}: SHA256-verified static x86_64 core and matching CLI prepared.")


if __name__ == "__main__":
    main()
