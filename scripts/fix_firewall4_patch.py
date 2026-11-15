"""Refresh the stale defaults context in ImmortalWrt's FullCone patch."""

import argparse
from pathlib import Path
import re


LEGACY_HUNK = b"""@@ -5,6 +5,10 @@ config defaults
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

# firewall4 358d421 removed the obsolete disable_ipv6 comments. Anchor inside
# the shared defaults block, with trailing context for GNU patch, and retain
# all four options and the remaining template/ucode file diffs.
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


def refresh_patch(path: Path) -> bool:
    data = path.read_bytes()
    headers = list(re.finditer(
        rb"(?m)^--- a/root/etc/config/firewall\r?\n"
        rb"\+\+\+ b/root/etc/config/firewall\r?\n", data))
    if len(headers) != 1:
        raise ValueError("Expected one firewall defaults patch; upstream patch needs review")
    start = headers[0].end()
    next_file = re.search(rb"(?m)^(?:--- (?:a/|/dev/null)|diff --git |-- \r?$)", data[start:])
    end = start + next_file.start() if next_file else len(data)
    hunk = data[start:end]
    newline = b"\r\n" if headers[0].group().endswith(b"\r\n") else b"\n"
    stable = STABLE_HUNK.replace(b"\n", newline)
    if hunk == stable:
        return False
    if hunk != LEGACY_HUNK.replace(b"\n", newline):
        raise ValueError("Unknown firewall defaults hunk; upstream patch needs review")
    path.write_bytes(data[:start] + stable + data[end:])
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", required=True, type=Path)
    args = parser.parse_args()
    try:
        changed = refresh_patch(args.patch)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Cannot refresh firewall4 FullCone patch: {error}")
    if changed:
        print("firewall4 FullCone patch: refreshed defaults context; existing options retained.")
    else:
        print("firewall4 FullCone patch: defaults context already compatible.")


if __name__ == "__main__":
    main()
