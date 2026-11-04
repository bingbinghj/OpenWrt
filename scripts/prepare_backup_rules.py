#!/usr/bin/env python3
"""Prepare package backup manifests without retaining installed proxy cores.

This edits build sources, never a running router. User-added sysupgrade paths
and binaries placed inside the retained data directories remain outside this
policy; no runtime restore hook or change to sysupgrade itself is installed.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile


OPENCLASH_DATA = (
    "/etc/config/openclash",
    "/etc/openclash/config/",
    "/etc/openclash/custom/",
    "/etc/openclash/overwrite/",
    "/etc/openclash/proxy_provider/",
    "/etc/openclash/rule_provider/",
    "/etc/openclash/history/",
    "/etc/openclash/uci/",
    "/etc/openclash/planet/",
    "/etc/openclash/state/",
    "/etc/openclash/cache.db",
    "/etc/openclash/.cache",
    "/etc/openclash/Country.mmdb",
    "/etc/openclash/GeoIP.dat",
    "/etc/openclash/GeoSite.dat",
    "/etc/openclash/ASN.mmdb",
    "/etc/openclash/GeoLite2-ASN.mmdb",
    "/etc/openclash/geoip.dat",
    "/etc/openclash/geosite.dat",
    "/etc/openclash/Model.bin",
    "/etc/openclash/china_ip_route.ipset",
    "/etc/openclash/china_ip6_route.ipset",
)

RECOMMENDED_EXTRA = (
    "/etc/AdGuardHome.yaml",
    "/etc/AdGuardHome/data/",
    "/etc/lucky/",
    "/etc/mosdns/config_custom.yaml",
)


class RecipeError(ValueError):
    """An upstream recipe changed and requires review before patching."""


def normalized(text: str) -> str:
    return text.replace("\r\n", "\n")


def manifest(paths: tuple[str, ...]) -> str:
    return "".join(path + "\n" for path in paths)


def entries(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines()
                 if line.strip() and not line.lstrip().startswith("#"))


def conffiles(text: str, package: str, paths: tuple[str, ...],
              accepted: tuple[tuple[str, ...], ...]) -> str:
    names = (re.escape(package), re.escape("$(PKG_NAME)"))
    pattern = re.compile(
        rf"(?m)^define Package/(?:{'|'.join(names)})/conffiles[ \t]*\n"
        r"(?P<body>.*?)^endef[ \t]*$", re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RecipeError(f"Expected one {package} conffiles definition")
    match = matches[0]
    if entries(match.group("body")) not in (*accepted, paths):
        raise RecipeError(f"Unknown {package} conffiles recipe; review backup paths")
    return text[:match.start("body")] + manifest(paths) + text[match.end("body"):]


def openclash_copy(source: str, destination: str) -> str:
    # Explicit paths keep the package scripts independent of Make's dollar escaping.
    paths = tuple(p for p in OPENCLASH_DATA if p.startswith("/etc/openclash/"))
    indent = "\t"
    lines = [f'{indent}mkdir -p "{destination}"']
    for path in paths:
        basename = path.rstrip("/").rsplit("/", 1)[1]
        lines.append(f'{indent}cp -rf "{source}/{basename}" "{destination}/"'
                     ' >/dev/null 2>&1')
    return "\n".join(lines)


def patch_openclash(makefile: str, defaults: str) -> tuple[str, str]:
    makefile = conffiles(makefile, "luci-app-openclash", OPENCLASH_DATA,
                         ((), ("/etc/openclash/",)))
    old_copy = '\tcp -rf "/etc/openclash" "/tmp/openclash" >/dev/null 2>&1'
    new_copy = openclash_copy("/etc/openclash", "/tmp/openclash")
    if makefile.count(old_copy) == 2 and new_copy not in makefile:
        makefile = makefile.replace(old_copy, new_copy)
    elif old_copy in makefile or makefile.count(new_copy) != 2:
        raise RecipeError("Unknown OpenClash preinst/prerm backup recipe")

    keep_pattern = re.compile(
        r'(?m)^cat[ \t]+>[ \t]+"/lib/upgrade/keep\.d/luci-app-openclash"'
        r'[ \t]+<<-EOF[ \t]*\n(?P<body>.*?)^EOF[ \t]*$', re.DOTALL,
    )
    matches = list(keep_pattern.finditer(defaults))
    if len(matches) != 1:
        raise RecipeError("Unknown OpenClash uci-defaults keep.d writer")
    match = matches[0]
    if entries(match.group("body")) not in (("/etc/openclash/",), OPENCLASH_DATA):
        raise RecipeError("Unknown OpenClash uci-defaults backup paths")
    defaults = (defaults[:match.start("body")] + manifest(OPENCLASH_DATA)
                + defaults[match.end("body"):])
    old_restore = '\tcp -rf "/tmp/openclash/." "/etc/openclash/" >/dev/null 2>&1'
    new_restore = openclash_copy("/tmp/openclash", "/etc/openclash")
    if defaults.count(old_restore) == 1 and new_restore not in defaults:
        defaults = defaults.replace(old_restore, new_restore)
    elif old_restore in defaults or defaults.count(new_restore) != 1:
        raise RecipeError("Unknown OpenClash uci-defaults temporary restore recipe")
    return makefile, defaults


def patch_sysupgrade(text: str) -> str:
    """Narrow known broad paths in the build's default extra backup list."""
    retained = []
    for line in text.splitlines():
        path = line.strip()
        if path in ("/etc", "/etc/", "/usr", "/usr/", "/usr/bin", "/usr/bin/"):
            raise RecipeError(f"Overbroad default backup path {path}; choose data paths")
        if path.rstrip("/") == "/etc/openclash":
            retained.extend(OPENCLASH_DATA)
        elif path.rstrip("/") == "/etc/AdGuardHome":
            retained.extend(RECOMMENDED_EXTRA[:2])
        elif (path.rstrip("/") in ("/etc/openclash/clash", "/usr/bin/AdGuardHome",
                                   "/usr/bin/mosdns", "/usr/bin/lucky",
                                   "/usr/bin/easytier-core", "/usr/bin/easytier-cli",
                                   "/usr/bin/easytier-web")
              or path.startswith("/etc/openclash/core/")
              or path.rstrip("/") == "/etc/openclash/core"
              or path.startswith("/etc/AdGuardHome/agh-backup/")):
            continue
        elif path.startswith(("/etc/openclash/", "/etc/AdGuardHome/")) and any(
                c in path for c in "*?["):
            raise RecipeError(f"Wildcard default backup path needs review: {path}")
        else:
            retained.append(line)
    # Runtime data not declared by the upstream LuCI package is covered explicitly.
    for path in RECOMMENDED_EXTRA:
        if path not in retained:
            retained.append(path)
    return "\n".join(dict.fromkeys(retained)) + "\n"


def read_required(path: Path) -> str:
    if not path.is_file():
        raise RecipeError(f"Missing package source: {path}")
    return normalized(path.read_text(encoding="utf-8"))


def check_keep(path: Path, expected: tuple[str, ...], old: tuple[str, ...]) -> None:
    if path.exists() and entries(read_required(path)) not in (expected, old):
        raise RecipeError(f"Unknown existing package keep.d manifest: {path}")


def prepare(openclash_package: Path, files_root: Path | None = None) -> list[Path]:
    """Validate every recipe before writing any build source."""
    edits: dict[Path, str] = {}
    oc_make = openclash_package / "Makefile"
    oc_defaults = openclash_package / "root/etc/uci-defaults/luci-openclash"
    makefile, defaults = patch_openclash(read_required(oc_make), read_required(oc_defaults))
    edits.update({oc_make: makefile, oc_defaults: defaults})
    oc_keep = openclash_package / "root/lib/upgrade/keep.d/luci-app-openclash"
    check_keep(oc_keep, OPENCLASH_DATA, ("/etc/openclash/",))
    edits[oc_keep] = manifest(OPENCLASH_DATA)

    if files_root is not None:
        sysupgrade_conf = files_root / "etc/sysupgrade.conf"
        text = read_required(sysupgrade_conf) if sysupgrade_conf.exists() else ""
        edits[sysupgrade_conf] = patch_sysupgrade(text)

    changed = []
    for path, content in edits.items():
        encoded = content.encode("utf-8")
        if path.exists() and path.read_bytes() == encoded:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode if path.exists() else 0o644
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            handle.write(encoded)
        try:
            os.chmod(temp, mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        changed.append(path)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openclash-package", required=True, type=Path)
    parser.add_argument("--files-root", type=Path,
                        help="Build overlay files/ directory for recommended backup defaults")
    args = parser.parse_args()
    try:
        changed = prepare(args.openclash_package, args.files_root)
    except (RecipeError, OSError, UnicodeError) as error:
        parser.exit(1, f"Backup manifest preparation failed: {error}\n")
    print(f"Prepared backup manifests; changed {len(changed)} build-source files.")


if __name__ == "__main__":
    main()
