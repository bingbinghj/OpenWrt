#!/bin/bash
# Offline integration checks for the real customization scripts.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE=$(mktemp -d)
trap 'rm -rf -- "$FIXTURE"' EXIT
mkdir -p "$FIXTURE/bin" "$FIXTURE/archive"
printf 'fixture core\n' > "$FIXTURE/archive/clash"
tar -czf "$FIXTURE/core.tar.gz" -C "$FIXTURE/archive" clash
export FIXTURE

cat > "$FIXTURE/bin/git" <<'MOCK'
#!/bin/bash
set -eu
if [[ "$1" == '-C' ]]; then printf '%040d\n' 1; exit 0; fi
destination="${@: -1}"
if [[ "$*" == *packages_lang_golang* ]]; then
    [[ "${FAIL_CLONE:-0}" == 0 ]] || exit 31
    [[ "$*" == *'27.x'* ]] || exit 32
fi
mkdir -p "$destination"
if [[ "$*" == *coolsnowwolf/packages* ]]; then mkdir -p "$destination/net/lucky"; fi
if [[ "$*" == *coolsnowwolf/luci* ]]; then mkdir -p "$destination/applications/luci-app-lucky"; fi
MOCK
cat > "$FIXTURE/bin/curl" <<'MOCK'
#!/bin/bash
set -eu
[[ "${FAIL_DOWNLOAD:-0}" == 0 ]] || exit 22
while [[ $# -gt 0 ]]; do
    if [[ "$1" == '-o' ]]; then cp "$FIXTURE/core.tar.gz" "$2"; exit 0; fi
    shift
done
exit 1
MOCK
chmod +x "$FIXTURE/bin/git" "$FIXTURE/bin/curl"
export PATH="$FIXTURE/bin:$PATH"

prepare() {
    mkdir -p "$1"
    cd "$1"
    mkdir -p feeds/packages/net feeds/packages/lang feeds/kenzok8/adguardhome \
        feeds/kenzok8/luci-app-adguardhome feeds/kenzok8/luci-app-openclash \
        feeds/luci/applications feeds/luci/collections/luci-light \
        feeds/luci/collections/luci-nginx package/base-files
    printf 'src-git packages https://example.invalid/packages\n' > feeds.conf.default
    printf 'luci-theme-bootstrap\n' > feeds/luci/collections/luci-light/Makefile
    printf 'luci-theme-bootstrap\n' > feeds/luci/collections/luci-nginx/Makefile
    printf '192.168.1.1\n' > package/base-files/config_generate
    cat > feeds/kenzok8/luci-app-adguardhome/Makefile <<'MAKE'
include $(TOPDIR)/feeds/luci/luci.mk
$(eval $(call BuildPackage,luci-app-adguardhome))
MAKE
}

prepare "$FIXTURE/success"
bash "$ROOT/x86_64_diy-part1.sh"
bash "$ROOT/x86_64_diy-part1.sh"
[[ $(grep -c 'src-git kenzok8 ' feeds.conf.default) == 1 ]]
bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/success.log" 2>&1
test -s feeds/luci/applications/luci-app-openclash/root/etc/openclash/core/clash_meta
test -s custom-sources.json
! grep -qF '$(eval $(call BuildPackage,luci-app-adguardhome))' feeds/luci/applications/luci-app-adguardhome/Makefile
grep -q '192.168.13.254' package/base-files/config_generate

prepare "$FIXTURE/clone-failure"
if FAIL_CLONE=1 bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/clone.log" 2>&1; then
    echo 'Clone failure was ignored' >&2; exit 1
fi
! grep -q 'All tasks completed' "$FIXTURE/clone.log"

prepare "$FIXTURE/download-failure"
if FAIL_DOWNLOAD=1 bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/download.log" 2>&1; then
    echo 'Download failure was ignored' >&2; exit 1
fi
! grep -q 'OpenClash Meta core ready' "$FIXTURE/download.log"
echo 'Customization integration checks passed (success, clone failure, download failure).'
