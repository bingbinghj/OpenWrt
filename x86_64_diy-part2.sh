#!/bin/bash

# 1. 定义软件包替换函数
replace_package() {
    local src=$1
    local dest=$2
    if [ -d "$src" ]; then
        rm -rf "$dest"
        cp -r "$src" "$dest"
        echo "Successfully replaced: $(basename $dest)"
    else
        echo "Warning: Source path $src not found, skipping..."
    fi
}

# === 彻底清理既有的 MosDNS 相关路径 ===
echo "Purging existing MosDNS files from feeds..."
rm -rf feeds/kenzok8/mosdns
rm -rf feeds/kenzok8/luci-app-mosdns
rm -rf feeds/packages/net/mosdns
rm -rf feeds/packages/net/v2ray-geodata

# === 准备 Golang 环境 (编译 MosDNS v5 必须使用新版 Go) ===
echo "Updating Golang to 26.x..."
rm -rf feeds/packages/lang/golang
git clone --depth 1 https://github.com/sbwml/packages_lang_golang -b 26.x feeds/packages/lang/golang

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

# === 处理 Lucky 相关软件包 ===
echo "Updating Lucky and Luci-app-lucky..."
rm -rf feeds/kenzok8/lucky
rm -rf feeds/kenzok8/luci-app-lucky

git clone --depth 1 -b master https://github.com/coolsnowwolf/packages /tmp/lean_packages
if [ -d "/tmp/lean_packages/net/lucky" ]; then
    cp -r /tmp/lean_packages/net/lucky feeds/kenzok8/lucky
    echo "Lucky core updated."
fi

git clone --depth 1 -b openwrt-25.12 https://github.com/coolsnowwolf/luci /tmp/lean_luci
if [ -d "/tmp/lean_luci/applications/luci-app-lucky" ]; then
    cp -r /tmp/lean_luci/applications/luci-app-lucky feeds/kenzok8/luci-app-lucky
    echo "Luci-app-lucky updated."
fi

rm -rf /tmp/lean_packages
rm -rf /tmp/lean_luci

# 权限修复
AGH_MAKEFILE="feeds/luci/applications/luci-app-adguardhome/Makefile"
if [ -f "$AGH_MAKEFILE" ]; then
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod 755 /usr/share/AdGuardHome/*' "$AGH_MAKEFILE"
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod +x /etc/init.d/AdGuardHome' "$AGH_MAKEFILE"
    echo "Fixed AdGuardHome Makefile permissions."
fi

# 3. 预集成 OpenClash Meta 核心
echo "Integrating OpenClash Meta core..."
CORE_URL="https://raw.githubusercontent.com/vernesong/OpenClash/core/master/meta/clash-linux-amd64-v1.tar.gz"
CORE_PATH="feeds/luci/applications/luci-app-openclash/root/etc/openclash/core"

mkdir -p "$CORE_PATH"
curl -sL -m 30 --retry 2 "$CORE_URL" -o /tmp/clash.tar.gz

if [ -f "/tmp/clash.tar.gz" ]; then
    tar zxvf /tmp/clash.tar.gz -C /tmp >/dev/null 2>&1
    chmod +x /tmp/clash
    mv /tmp/clash "$CORE_PATH/clash_meta"
    rm -rf /tmp/clash.tar.gz
    echo "OpenClash Meta core ready."
fi

# 4. 修改默认 IP
find package/base-files/ -name "config_generate" | xargs sed -i 's/192.168.1.1/192.168.13.254/g'

# 5. 修改默认主题
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-light/Makefile
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-nginx/Makefile

echo "All tasks completed!"
