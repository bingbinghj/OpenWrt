"""Build OpenClash and Smart from the same channels as its version-check UI."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import tarfile
import tempfile
import time
import urllib.request

API = "https://api.github.com/repos/vernesong/OpenClash"
RAW = "https://raw.githubusercontent.com/vernesong/OpenClash"
CORE_PATH = "master/smart/clash-linux-amd64-v1.tar.gz"
MAX_BYTES = 128 * 1024 * 1024


def fetch_bytes(url):
    headers = {"User-Agent": "OpenWrt-custom-build"}
    if url.startswith(API + "/"):
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as response:
                data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("OpenClash response exceeds size limit")
            return data
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def git_sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError(f"Invalid OpenClash {label} SHA")
    return value


def source_version(text):
    versions = re.findall(r"^PKG_VERSION\s*:?=\s*(\d+\.\d+\.\d+)\s*$", text, re.M)
    if len(versions) != 1:
        raise ValueError("OpenClash Makefile needs one numeric PKG_VERSION")
    return versions[0]


def resolve_ref(ref):
    data = json.loads(fetch_bytes(API + "/git/ref/" + ref))
    if data.get("ref") != "refs/" + ref:
        raise ValueError("Unexpected OpenClash reference")
    obj = data.get("object", {})
    for step in range(4):
        sha = git_sha(obj.get("sha"), ref)
        if obj.get("type") == "commit":
            return sha
        if obj.get("type") != "tag" or step == 3:
            raise ValueError("OpenClash reference does not resolve to a commit")
        obj = json.loads(fetch_bytes(API + "/git/tags/" + sha)).get("object", {})
    raise ValueError("Cannot resolve OpenClash reference")


def fetch_snapshot():
    with ThreadPoolExecutor(max_workers=3) as pool:
        master, package, core = list(pool.map(resolve_ref, ("heads/master", "heads/package", "heads/core")))
    urls = (f"{RAW}/{package}/master/version", f"{RAW}/{core}/master/core_version",
            f"{RAW}/{master}/luci-app-openclash/Makefile", f"{API}/contents/{CORE_PATH}?ref={core}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        plugin_text, core_text, makefile, asset = list(pool.map(fetch_bytes, urls))
    plugin_lines = plugin_text.decode("utf-8").splitlines()
    core_lines = core_text.decode("utf-8").splitlines()
    if not plugin_lines or len(core_lines) < 2:
        raise ValueError("OpenClash UI version manifests are incomplete")
    plugin = plugin_lines[0].strip()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", plugin):
        raise ValueError("Unexpected OpenClash UI plugin version")
    version = source_version(makefile.decode("utf-8"))
    selected = master
    source_ref = "refs/heads/master"
    if version != plugin[1:]:
        selected = resolve_ref("tags/" + plugin)
        source_ref = "refs/tags/" + plugin
        version = source_version(fetch_bytes(f"{RAW}/{selected}/luci-app-openclash/Makefile").decode("utf-8"))
    asset = json.loads(asset)
    if asset.get("type") != "file" or asset.get("path") != CORE_PATH or asset.get("download_url") != f"{RAW}/{core}/{CORE_PATH}":
        raise ValueError("Unexpected pinned Smart archive metadata")
    snapshot = {"source_sha": selected, "source_ref": source_ref, "master_sha": master,
            "package_sha": package, "core_sha": core,
            "plugin_version": plugin, "smart_version": core_lines[1].strip(),
            "source_version": version,
            "core_blob_sha": git_sha(asset.get("sha"), "Smart blob")}
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot):
    for field in ("source_sha", "package_sha", "core_sha", "core_blob_sha"):
        git_sha(snapshot.get(field), field)
    plugin = snapshot.get("plugin_version")
    if not isinstance(plugin, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", plugin):
        raise ValueError("Unexpected OpenClash UI plugin version")
    if "master_sha" in snapshot:
        git_sha(snapshot["master_sha"], "master")
    if "source_ref" in snapshot and snapshot["source_ref"] not in ("refs/heads/master", "refs/tags/" + plugin):
        raise ValueError("Unexpected OpenClash source reference")
    smart = snapshot.get("smart_version")
    if not isinstance(smart, str) or not re.fullmatch(r"alpha-smart-g[0-9a-f]{7,40}", smart):
        raise ValueError("Unexpected OpenClash UI Smart version")
    if snapshot.get("source_version") != plugin[1:]:
        raise ValueError(f"OpenClash UI master version {plugin} does not match source PKG_VERSION {snapshot.get('source_version')}; refusing a misleading build")


def safe_package_path(package):
    absolute = package.absolute()
    if absolute.name != "luci-app-openclash" or absolute.is_symlink() or not absolute.is_dir():
        raise ValueError("OpenClash target must be an existing build feed package directory")
    feed_roots = [parent for parent in absolute.parents if parent.name == "feeds"]
    if len(feed_roots) != 1:
        raise ValueError("OpenClash target must be inside a build feeds directory")
    resolved = absolute.resolve()
    if not resolved.is_relative_to(feed_roots[0].resolve()):
        raise ValueError("OpenClash target escapes its build feed directory")
    text = (resolved / "Makefile").read_text(encoding="utf-8")
    if re.findall(r"^PKG_NAME\s*:?=\s*(\S+)\s*$", text, re.M) != ["luci-app-openclash"]:
        raise ValueError("Target package is not luci-app-openclash")
    source_version(text)
    return resolved


def member_path(name):
    if "\\" in name or ":" in name or name.startswith("/") or ".." in PurePosixPath(name).parts:
        raise ValueError("Unsafe OpenClash archive path")
    return PurePosixPath(name)


def extract_package(data, snapshot, destination):
    prefix = PurePosixPath("OpenClash-" + snapshot["source_sha"], "luci-app-openclash")
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            path = member_path(member.name)
            if not path.is_relative_to(prefix):
                continue
            relative = path.relative_to(prefix)
            if not relative.parts:
                continue
            if not member.isdir() and not member.isfile():
                raise ValueError("OpenClash package archive contains links or special files")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            if total > 256 * 1024 * 1024:
                raise ValueError("OpenClash package exceeds extracted size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Cannot read OpenClash package member")
            with stream, target.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
            target.chmod(member.mode & 0o777)
    required = ("Makefile", "root/etc/init.d/openclash", "root/etc/config/openclash",
                "root/usr/share/openclash/openclash_version.lua")
    if any(not (destination / path).is_file() for path in required):
        raise ValueError("Pinned OpenClash source package is incomplete")
    if source_version((destination / "Makefile").read_text(encoding="utf-8")) != snapshot["plugin_version"][1:]:
        raise ValueError("Source archive PKG_VERSION differs from the UI version manifest")


def smart_binary(data, expected_blob_sha):
    digest = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    if digest != expected_blob_sha:
        raise ValueError("Smart archive does not match its pinned Git blob hash")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        entries = []
        for member in archive.getmembers():
            path = member_path(member.name)
            if path == PurePosixPath("clash"):
                entries.append(member)
        if len(entries) != 1 or not entries[0].isfile() or not 64 <= entries[0].size <= MAX_BYTES:
            raise ValueError("Smart archive needs one bounded regular clash binary")
        stream = archive.extractfile(entries[0])
        if stream is None:
            raise ValueError("Cannot read Smart core")
        with stream:
            binary = stream.read()
    if (binary[:7] != b"\x7fELF\x02\x01\x01" or struct.unpack_from("<H", binary, 18)[0] != 62
            or struct.unpack_from("<H", binary, 16)[0] not in (2, 3)
            or struct.unpack_from("<I", binary, 20)[0] != 1):
        raise ValueError("Smart core is not an x86_64 ELF")
    offset = struct.unpack_from("<Q", binary, 32)[0]
    entry_size, count = struct.unpack_from("<HH", binary, 54)
    if not count or offset < 64 or entry_size < 56 or offset + count * entry_size > len(binary):
        raise ValueError("Smart core ELF headers are invalid")
    if any(struct.unpack_from("<I", binary, offset + index * entry_size)[0] in (2, 3) for index in range(count)):
        raise ValueError("Smart core must be statically linked")
    return binary


def configure_package(package):
    config_path = package / "root/etc/config/openclash"
    text = config_path.read_text(encoding="utf-8")
    sections = list(re.finditer(r"(?m)^[ \t]*config[ \t]+", text))
    matches = []
    for index, section in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        block = text[section.start():end]
        if re.match(r"\s*config\s+openclash\s+['\"]?config['\"]?\s*(?:\n|$)", block):
            matches.append((section.start(), end, block))
    if len(matches) != 1:
        raise ValueError("OpenClash defaults need one named config section")
    start, end, block = matches[0]
    block, count = re.subn(r"(?m)^([ \t]*)option\s+core_type\s+.*$", "\toption core_type 'Smart'", block)
    if count > 1:
        raise ValueError("OpenClash defaults contain ambiguous core_type options")
    if count == 0:
        first, separator, remaining = block.partition("\n")
        block = first + separator + "\toption core_type 'Smart'\n" + remaining
    config_path.write_text(text[:start] + block + text[end:], encoding="utf-8", newline="\n")
    makefile = package / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    def compile_block(match):
        body = match[1].strip()
        if body and f"{RAW}/core/master/meta/" not in body:
            raise ValueError("Unknown OpenClash Build/Compile implementation")
        return "define Build/Compile\nendef"

    text, count = re.subn(r"(?ms)^define Build/Compile\n(.*?)^endef$", compile_block, text)
    if count not in (1, 2):
        raise ValueError("Unexpected OpenClash compile blocks")
    text, count = re.subn(r"(?m)^([ \t]*)PKGARCH:=.*$", "\tPKGARCH:=$(ARCH_PACKAGES)", text)
    if count != 1:
        raise ValueError("OpenClash package architecture declaration is missing")
    text, count = re.subn(r"(?m)^([ \t]*DEPENDS:=)(.*)$", lambda m: m[1] + "@x86_64 " + m[2], text)
    if count != 1:
        raise ValueError("OpenClash package dependency declaration is missing")
    text += "\nRSTRIP:=:\n"
    makefile.write_text(text, encoding="utf-8", newline="\n")


def prepare_package(package, provenance, snapshot, download_dir):
    package = safe_package_path(package)
    validate_snapshot(snapshot)
    provenance = provenance.absolute()
    if provenance.is_symlink() or provenance.resolve().is_relative_to(package):
        raise ValueError("OpenClash provenance must be outside the package and not a symlink")
    metadata = json.loads(provenance.read_text(encoding="utf-8")) if provenance.exists() else {}
    if not isinstance(metadata, dict) or any(not isinstance(value, str) for value in metadata.values()):
        raise ValueError("OpenClash provenance must contain string values")
    source_url = f"https://codeload.github.com/vernesong/OpenClash/tar.gz/{snapshot['source_sha']}"
    core_url = f"{RAW}/{snapshot['core_sha']}/{CORE_PATH}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        source_data, core_data = list(pool.map(fetch_bytes, (source_url, core_url)))
    binary = smart_binary(core_data, snapshot["core_blob_sha"])
    metadata.update({"vernesong/OpenClash (plugin version)": snapshot["plugin_version"],
                     "vernesong/OpenClash (source sha)": snapshot["source_sha"],
                     "vernesong/OpenClash (source ref)": snapshot.get("source_ref", "refs/heads/master"),
                     "vernesong/OpenClash (master sha)": snapshot.get("master_sha", snapshot["source_sha"]),
                     "vernesong/OpenClash (package sha)": snapshot["package_sha"],
                     "vernesong/OpenClash (Smart version)": snapshot["smart_version"],
                     "vernesong/OpenClash (core sha)": snapshot["core_sha"],
                     "vernesong/OpenClash (source archive sha256)": hashlib.sha256(source_data).hexdigest(),
                     "vernesong/OpenClash (Smart archive sha256)": hashlib.sha256(core_data).hexdigest(),
                     "vernesong/OpenClash (Smart binary sha256)": hashlib.sha256(binary).hexdigest()})
    with tempfile.TemporaryDirectory(prefix=".openclash-update-", dir=package.parent) as directory:
        transaction = Path(directory)
        staged = transaction / "new"
        staged.mkdir()
        extract_package(source_data, snapshot, staged)
        configure_package(staged)
        core = staged / "root/etc/openclash/core/clash_meta"
        core.parent.mkdir(parents=True, exist_ok=True)
        core.write_bytes(binary)
        core.chmod(0o755)
        download_dir.mkdir(parents=True, exist_ok=True)
        (download_dir / f"OpenClash-{snapshot['source_sha']}.tar.gz").write_bytes(source_data)
        (download_dir / f"openclash-smart-{snapshot['core_sha']}.tar.gz").write_bytes(core_data)
        provenance.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=provenance.parent, delete=False) as stream:
            temporary_metadata = Path(stream.name)
            stream.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        previous = transaction / "previous"
        try:
            package.rename(previous)
            try:
                staged.rename(package)
                temporary_metadata.replace(provenance)
            except BaseException:
                if package.exists():
                    package.rename(transaction / "rejected")
                previous.rename(package)
                raise
        finally:
            temporary_metadata.unlink(missing_ok=True)
    return snapshot["plugin_version"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, default=Path("dl"))
    args = parser.parse_args()
    try:
        safe_package_path(args.package)
        version = prepare_package(args.package, args.provenance, fetch_snapshot(), args.download_dir)
    except (OSError, ValueError, tarfile.TarError) as error:
        raise SystemExit(f"Cannot prepare OpenClash UI channel and Smart; refusing an old-core fallback: {error}")
    print(f"OpenClash UI master {version} and its pinned Smart core prepared for x86_64.")


if __name__ == "__main__":
    main()
