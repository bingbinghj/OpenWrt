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
    mkdir -p feeds/kenzok8/luci-app-openclash/root/etc/init.d
    cat > feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash <<'INIT'
#!/bin/sh
# This unrelated service restart must remain untouched by the build fix.
unrelated_restart() {
   /etc/init.d/cron restart
}
del_cron()
{
   sed -i '/#openclash-cron-task/d' $CRON_FILE
   sed -i '/#openclash-overwrite-download/d' $CRON_FILE
   /etc/init.d/cron restart
} >/dev/null 2>&1
INIT
    chmod 755 feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash
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
cp feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash "$FIXTURE/expected-init"
sed -i '/^del_cron()/,/^}/s@/etc/init.d/cron restart@crontab "$CRON_FILE"@' "$FIXTURE/expected-init"
bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/success.log" 2>&1
OPENCLASH_INIT=feeds/luci/applications/luci-app-openclash/root/etc/init.d/openclash
cmp "$FIXTURE/expected-init" "$OPENCLASH_INIT"
test -x "$OPENCLASH_INIT"
# Exercise the patched function against a disposable crontab, without services.
CRON_FILE="$FIXTURE/root-crontab"
printf '0 2 * * * mosdns-update\n0 6 * * 1 subscription #openclash-cron-task\n0 1 * * * overwrite #openclash-overwrite-download\n' > "$CRON_FILE"
(
    source "$OPENCLASH_INIT"
    crontab() { printf '%s\n' "$1" > "$FIXTURE/crontab-reloaded"; }
    del_cron
)
[[ $(cat "$FIXTURE/crontab-reloaded") == "$CRON_FILE" ]]
[[ $(cat "$CRON_FILE") == '0 2 * * * mosdns-update' ]]
test -s feeds/luci/applications/luci-app-openclash/root/etc/openclash/core/clash_meta
test -s custom-sources.json
! grep -qF '$(eval $(call BuildPackage,luci-app-adguardhome))' feeds/luci/applications/luci-app-adguardhome/Makefile
grep -q '192.168.13.254' package/base-files/config_generate

prepare "$FIXTURE/already-fixed"
sed -i '/^del_cron()/,/^}/s@/etc/init.d/cron restart@crontab $CRON_FILE@' feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash
cp feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash "$FIXTURE/already-fixed-init"
bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/already-fixed.log" 2>&1
cmp "$FIXTURE/already-fixed-init" feeds/luci/applications/luci-app-openclash/root/etc/init.d/openclash
grep -q 'already reloads crontab' "$FIXTURE/already-fixed.log"

prepare "$FIXTURE/crlf"
python3 - <<'PY'
from pathlib import Path
path = Path('feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash')
path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
PY
bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/crlf.log" 2>&1
python3 - <<'PY'
from pathlib import Path
data = Path('feeds/luci/applications/luci-app-openclash/root/etc/init.d/openclash').read_bytes()
assert b'crontab "$CRON_FILE"\r\n' in data
assert b'\n' not in data.replace(b'\r\n', b'')
PY

prepare "$FIXTURE/missing-init"
rm feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash
if bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/missing-init.log" 2>&1; then
    echo 'Missing OpenClash init was ignored' >&2; exit 1
fi
grep -q 'Missing OpenClash init script' "$FIXTURE/missing-init.log"

prepare "$FIXTURE/incompatible-init"
sed -i 's@/etc/init.d/cron restart$@/etc/init.d/cron restart >/dev/null@' feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash
if bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/incompatible-init.log" 2>&1; then
    echo 'Unsupported cron restart command was ignored' >&2; exit 1
fi
grep -q 'cron fix needs review' "$FIXTURE/incompatible-init.log"

prepare "$FIXTURE/mixed-init"
sed -i '/^del_cron()/,/^}/s@/etc/init.d/cron restart@crontab $CRON_FILE@' feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash
sed -i '/^}/i\   if :; then /etc/init.d/cron restart; fi' feeds/kenzok8/luci-app-openclash/root/etc/init.d/openclash
if bash "$ROOT/x86_64_diy-part2.sh" > "$FIXTURE/mixed-init.log" 2>&1; then
    echo 'Mixed crontab reload and cron restart were ignored' >&2; exit 1
fi
grep -q 'cron fix needs review' "$FIXTURE/mixed-init.log"

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
echo 'Customization integration checks passed (cron reload, already-fixed, CRLF, missing/incompatible/mixed init, clone/download failures).'
