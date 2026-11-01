#!/bin/bash
set -euo pipefail

CUSTOM_TMP=$(mktemp -d)
trap 'rm -rf -- "$CUSTOM_TMP"' EXIT
trap 'echo "::error file=x86_64_diy-part2.sh,line=$LINENO::Package customization failed"' ERR

# 1. 定义软件包替换函数
replace_package() {
    local src=$1
    local dest=$2
    if [ -d "$src" ]; then
        rm -rf "$dest"
        cp -r "$src" "$dest"
        echo "Successfully replaced: $(basename "$dest")"
    else
        echo "Required package source $src not found" >&2
        exit 1
    fi
}

# === 彻底清理既有的 MosDNS 相关路径 ===
echo "Purging existing MosDNS files from feeds..."
rm -rf feeds/kenzok8/mosdns
rm -rf feeds/kenzok8/luci-app-mosdns
rm -rf feeds/packages/net/mosdns
rm -rf feeds/packages/net/v2ray-geodata

# === Xray 26.9.9 要求 Go >= 1.27；与工作流共用版本设置 ===
GO_BRANCH=${GO_BRANCH:-27.x}
echo "Updating Golang to $GO_BRANCH..."
rm -rf feeds/packages/lang/golang
git clone --depth 1 https://github.com/sbwml/packages_lang_golang -b "$GO_BRANCH" feeds/packages/lang/golang

# === 拉取 sbwml 版 MosDNS v5 (直接放入 package 目录) ===
echo "Cloning sbwml MosDNS v5 and geodata to /package..."
rm -rf package/mosdns
rm -rf package/v2ray-geodata
git clone --depth 1 https://github.com/sbwml/luci-app-mosdns -b v5 package/mosdns
git clone --depth 1 https://github.com/sbwml/v2ray-geodata package/v2ray-geodata

# 2. 执行其他软件包替换操作
replace_package "feeds/kenzok8/adguardhome" "feeds/packages/net/adguardhome"
replace_package "feeds/kenzok8/luci-app-adguardhome" "feeds/luci/applications/luci-app-adguardhome"
replace_package "feeds/kenzok8/luci-app-openclash" "feeds/luci/applications/luci-app-openclash"

# 仅重新加载任务表，避免 cron 发起的 OpenClash 重启杀掉自己的 cgroup。
# 修补复制后的打包源文件；已修复的上游保持原样，不匹配时停止编译。
OPENCLASH_INIT="feeds/luci/applications/luci-app-openclash/root/etc/init.d/openclash"
python3 - "$OPENCLASH_INIT" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"Missing OpenClash init script: {path}")
data = path.read_bytes()
functions = list(re.finditer(
    rb"(?ms)^del_cron\(\)[ \t]*\r?\n\{[ \t]*\r?\n(?P<body>.*?)^\}[^\r\n]*(?:\r?\n|$)",
    data,
))
if len(functions) != 1:
    raise SystemExit(f"Expected one OpenClash del_cron() function: {path}")
function = functions[0]
body = function.group("body")
old = re.compile(rb"(?m)^(?P<indent>[ \t]*)/etc/init\.d/cron[ \t]+restart[ \t]*(?P<cr>\r?)$")
restarts = re.findall(rb"/etc/init\.d/cron[ \t]+restart\b", body)
reloads = re.findall(rb'(?m)^[ \t]*crontab[ \t]+(?:"\$CRON_FILE"|\$CRON_FILE)[ \t]*\r?$', body)
if not restarts and len(reloads) == 1:
    print("OpenClash del_cron() already reloads crontab.")
elif len(restarts) == 1 and len(old.findall(body)) == 1 and not reloads:
    fixed = old.sub(lambda m: m.group("indent") + b'crontab "$CRON_FILE"' + m.group("cr"), body, count=1)
    path.write_bytes(data[:function.start("body")] + fixed + data[function.end("body"):])
    print("Fixed OpenClash del_cron(): reload crontab instead of restarting cron.")
else:
    raise SystemExit(f"Unexpected OpenClash del_cron() commands; cron fix needs review: {path}")
PY

# === 处理 Lucky 相关软件包 ===
echo "Updating Lucky and Luci-app-lucky..."
rm -rf feeds/kenzok8/lucky
rm -rf feeds/kenzok8/luci-app-lucky

git clone --depth 1 -b master https://github.com/coolsnowwolf/packages "$CUSTOM_TMP/lean_packages"
replace_package "$CUSTOM_TMP/lean_packages/net/lucky" feeds/kenzok8/lucky

git clone --depth 1 -b openwrt-25.12 https://github.com/coolsnowwolf/luci "$CUSTOM_TMP/lean_luci"
replace_package "$CUSTOM_TMP/lean_luci/applications/luci-app-lucky" feeds/kenzok8/luci-app-lucky

# Record the temporary source revisions before cleanup for release provenance.
printf '{"coolsnowwolf/packages (lucky)":"%s","coolsnowwolf/luci (lucky)":"%s"}\n' \
    "$(git -C "$CUSTOM_TMP/lean_packages" rev-parse HEAD)" \
    "$(git -C "$CUSTOM_TMP/lean_luci" rev-parse HEAD)" > custom-sources.json

# 权限修复
AGH_MAKEFILE="feeds/luci/applications/luci-app-adguardhome/Makefile"
if [ -f "$AGH_MAKEFILE" ]; then
    # luci.mk already calls BuildPackage; the extra call caused duplicate recipes.
    if grep -qF 'include $(TOPDIR)/feeds/luci/luci.mk' "$AGH_MAKEFILE"; then
        sed -i '/^$(eval $(call BuildPackage,luci-app-adguardhome))$/d' "$AGH_MAKEFILE"
    fi
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod 755 /usr/share/AdGuardHome/*' "$AGH_MAKEFILE"
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod +x /etc/init.d/AdGuardHome' "$AGH_MAKEFILE"
    echo "Fixed AdGuardHome Makefile permissions."
fi

# 3. 预集成 OpenClash Meta 核心
echo "Integrating OpenClash Meta core..."
CORE_URL="https://raw.githubusercontent.com/vernesong/OpenClash/core/master/meta/clash-linux-amd64-v1.tar.gz"
CORE_PATH="feeds/luci/applications/luci-app-openclash/root/etc/openclash/core"

mkdir -p "$CORE_PATH"
curl --fail --location --connect-timeout 15 --max-time 120 --retry 3 \
    "$CORE_URL" -o "$CUSTOM_TMP/clash.tar.gz"
tar -xzf "$CUSTOM_TMP/clash.tar.gz" -C "$CUSTOM_TMP"
test -s "$CUSTOM_TMP/clash"
install -m 0755 "$CUSTOM_TMP/clash" "$CORE_PATH/clash_meta"
echo "OpenClash Meta core ready."

# 4. 修改默认 IP
find package/base-files/ -name config_generate -exec sed -i 's/192\.168\.1\.1/192.168.13.254/g' '{}' +

# 5. 修改默认主题
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-light/Makefile
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-nginx/Makefile

echo "All tasks completed!"
