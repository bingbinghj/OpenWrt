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

# 2. 执行批量替换操作
replace_package "feeds/kenzok8/mosdns" "feeds/packages/net/mosdns"
replace_package "feeds/kenzok8/adguardhome" "feeds/packages/net/adguardhome"
replace_package "feeds/kenzok8/luci-app-adguardhome" "feeds/luci/applications/luci-app-adguardhome"
replace_package "feeds/kenzok8/luci-app-openclash" "feeds/luci/applications/luci-app-openclash"

# === 新增：处理 Lucky 相关软件包 ===
echo "Updating Lucky and Luci-app-lucky from coolsnowwolf..."
# 删除旧的 kenzok8 中的 lucky 相关包
rm -rf feeds/kenzok8/lucky
rm -rf feeds/kenzok8/luci-app-lucky

# 临时克隆 Lean 的 packages 仓库 (master 分支) 获取 lucky
git clone --depth 1 -b master https://github.com/coolsnowwolf/packages /tmp/lean_packages
if [ -d "/tmp/lean_packages/net/lucky" ]; then
    cp -r /tmp/lean_packages/net/lucky feeds/kenzok8/lucky
    echo "Lucky core updated."
fi

# 临时克隆 Lean 的 luci 仓库 (openwrt-25.12 分支) 获取 luci-app-lucky
git clone --depth 1 -b openwrt-25.12 https://github.com/coolsnowwolf/luci /tmp/lean_luci
if [ -d "/tmp/lean_luci/applications/luci-app-lucky" ]; then
    cp -r /tmp/lean_luci/applications/luci-app-lucky feeds/kenzok8/luci-app-lucky
    echo "Luci-app-lucky updated."
fi

# 清理临时拉取的目录
rm -rf /tmp/lean_packages
rm -rf /tmp/lean_luci
# =================================

# 特殊处理：AdGuardHome Makefile 权限修复
AGH_MAKEFILE="feeds/luci/applications/luci-app-adguardhome/Makefile"
if [ -f "$AGH_MAKEFILE" ]; then
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod 755 /usr/share/AdGuardHome/*' "$AGH_MAKEFILE"
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod +x /etc/init.d/AdGuardHome' "$AGH_MAKEFILE"
    echo "Fixed AdGuardHome Makefile permissions."
fi

# 3. 预集成 OpenClash Meta 核心
echo "Downloading OpenClash Meta core..."
CORE_URL="https://raw.githubusercontent.com/vernesong/OpenClash/core/master/meta/clash-linux-amd64-v1.tar.gz"
CORE_PATH="feeds/luci/applications/luci-app-openclash/root/etc/openclash/core"

mkdir -p "$CORE_PATH"
curl -sL -m 30 --retry 2 "$CORE_URL" -o /tmp/clash.tar.gz

if [ -f "/tmp/clash.tar.gz" ]; then
    tar zxvf /tmp/clash.tar.gz -C /tmp >/dev/null 2>&1
    chmod +x /tmp/clash
    mv /tmp/clash "$CORE_PATH/clash_meta"
    rm -rf /tmp/clash.tar.gz
    echo "OpenClash Meta core integrated successfully."
else
    echo "Error: Failed to download OpenClash core!"
fi

# 4. 修改默认 IP、主机名
find package/base-files/ -name "config_generate" | xargs sed -i 's/192.168.1.1/192.168.13.254/g'

# 5. 修改默认主题
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-light/Makefile
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-nginx/Makefile

echo "All modifications completed!"
