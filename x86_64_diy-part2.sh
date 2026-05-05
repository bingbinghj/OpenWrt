#!/bin/bash

# --- 1. 定义工具函数 ---

# 替换本地软件包函数 (源路径, 目标路径)
replace_package() {
    local src=$1
    local dest=$2
    if [ -d "$src" ]; then
        rm -rf "$dest"
        cp -r "$src" "$dest"
        echo "已替换本地包: $(basename $dest)"
    else
        echo "跳过本地替换: $src 不存在"
    fi
}

# --- 2. 软件包清理与冲突规避 ---

echo "正在清理潜在冲突的软件包..."
# 彻底删除所有 feed 目录下的 lucky 相关文件，准备手工注入
rm -rf feeds/kenzok8/lucky
rm -rf feeds/kenzok8/luci-app-lucky

# A. 使用 kenzok8 的包 (MosDNS, AdGuardHome, OpenClash)
replace_package "feeds/kenzok8/mosdns" "feeds/packages/net/mosdns"
replace_package "feeds/kenzok8/adguardhome" "feeds/packages/net/adguardhome"
replace_package "feeds/kenzok8/luci-app-adguardhome" "feeds/luci/applications/luci-app-adguardhome"
replace_package "feeds/kenzok8/luci-app-openclash" "feeds/luci/applications/luci-app-openclash"

# --- 3. 强行注入大雕版 Lucky ---

echo "正在获取大雕版 Lucky 源码并注入 kenzok8 目录..."

# 1. 临时克隆 lucky 核心 (packages 仓库)
git clone --depth 1 -b "master" "https://github.com/coolsnowwolf/packages" "/tmp/lucky_core_repo"
if [ -d "/tmp/lucky_core_repo/lucky" ]; then
    mkdir -p feeds/kenzok8
    cp -r "/tmp/lucky_core_repo/lucky" "feeds/kenzok8/"
    echo "成功注入 Lucky 核心"
fi

# 2. 临时克隆 lucky LuCI (luci 仓库 openwrt-25.12 分支)
git clone --depth 1 -b "openwrt-25.12" "https://github.com/coolsnowwolf/luci" "/tmp/lucky_luci_repo"
if [ -d "/tmp/lucky_luci_repo/luci-app-lucky" ]; then
    cp -r "/tmp/lucky_luci_repo/luci-app-lucky" "feeds/kenzok8/"
    echo "成功注入 Lucky LuCI"
fi

# 清理临时克隆的庞大仓库目录
rm -rf /tmp/lucky_core_repo /tmp/lucky_luci_repo

# --- 4. 补丁与权限修复 ---

# 修改 AdGuardHome 权限补丁
AGH_MAKEFILE="feeds/luci/applications/luci-app-adguardhome/Makefile"
if [ -f "$AGH_MAKEFILE" ]; then
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod 755 /usr/share/AdGuardHome/*' "$AGH_MAKEFILE"
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod +x /etc/init.d/AdGuardHome' "$AGH_MAKEFILE"
    echo "AdGuardHome 权限补丁已应用"
fi

# --- 5. 预集成 OpenClash Meta 核心 ---

CORE_PATH="feeds/luci/applications/luci-app-openclash/root/etc/openclash/core"
mkdir -p "$CORE_PATH"
echo "正在下载 OpenClash Meta 核心..."
curl -sL -m 30 --retry 2 "https://raw.githubusercontent.com/vernesong/OpenClash/core/master/meta/clash-linux-amd64-v1.tar.gz" -o /tmp/clash.tar.gz

if [ -f "/tmp/clash.tar.gz" ]; then
    tar zxvf /tmp/clash.tar.gz -C /tmp >/dev/null 2>&1
    chmod +x /tmp/clash
    mv /tmp/clash "$CORE_PATH/clash_meta"
    rm -rf /tmp/clash.tar.gz
    echo "OpenClash 核心集成成功"
fi

# --- 6. 系统默认设置修改 ---

# 修改默认管理 IP
find package/base-files/ -name "config_generate" | xargs sed -i 's/192.168.1.1/192.168.13.254/g'

# 修改默认主题为 Argon
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-light/Makefile
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-nginx/Makefile

# --- 7. 刷新 Feeds 索引 ---

# 关键：先删除旧索引再更新，确保刚刚注入 feeds/kenzok8 的包被正确识别
./scripts/feeds update -i
./scripts/feeds install -a

echo "所有定制化修改已完成！"
