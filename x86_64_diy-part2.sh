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

# 远程克隆特定软件包函数 (仓库地址, 分支, 目标父目录, 软件包名)
clone_pkg() {
    local repo=$1
    local branch=$2
    local target_dir=$3
    local pkg_name=$4
    
    echo "正在从 $repo ($branch) 获取 $pkg_name..."
    rm -rf "/tmp/clone_temp"
    git clone --depth 1 -b "$branch" "$repo" "/tmp/clone_temp"
    
    if [ -d "/tmp/clone_temp/$pkg_name" ]; then
        rm -rf "$target_dir/$pkg_name"
        mkdir -p "$target_dir"
        mv "/tmp/clone_temp/$pkg_name" "$target_dir/"
        echo "成功获取远程包: $pkg_name"
    else
        echo "错误: 在仓库中未找到 $pkg_name"
    fi
    rm -rf "/tmp/clone_temp"
}

# --- 2. 软件包替换操作 ---

# A. 使用 kenzok8 的包 (MosDNS, AdGuardHome, OpenClash)
replace_package "feeds/kenzok8/mosdns" "feeds/packages/net/mosdns"
replace_package "feeds/kenzok8/adguardhome" "feeds/packages/net/adguardhome"
replace_package "feeds/kenzok8/luci-app-adguardhome" "feeds/luci/applications/luci-app-adguardhome"
replace_package "feeds/kenzok8/luci-app-openclash" "feeds/luci/applications/luci-app-openclash"

# B. 使用 coolsnowwolf (大雕) 的 Lucky 包
# Lucky 核心: 来自 packages 仓库 master 分支
clone_pkg "https://github.com/coolsnowwolf/packages" "master" "feeds/packages/net" "lucky"
# Lucky LuCI: 来自 luci 仓库 openwrt-25.12 分支
clone_pkg "https://github.com/coolsnowwolf/luci" "openwrt-25.12" "feeds/luci/applications" "luci-app-lucky"

# --- 3. 补丁与权限修复 ---

# 修改 AdGuardHome Makefile 权限，确保编译后二进制文件可执行
AGH_MAKEFILE="feeds/luci/applications/luci-app-adguardhome/Makefile"
if [ -f "$AGH_MAKEFILE" ]; then
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod 755 /usr/share/AdGuardHome/*' "$AGH_MAKEFILE"
    sed -i '/\/etc\/init.d\/AdGuardHome enable/i \	chmod +x /etc/init.d/AdGuardHome' "$AGH_MAKEFILE"
    echo "AdGuardHome 权限补丁已应用"
fi

# --- 4. 预集成 OpenClash Meta 核心 ---

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

# --- 5. 系统默认设置修改 ---

# 修改默认管理 IP
find package/base-files/ -name "config_generate" | xargs sed -i 's/192.168.1.1/192.168.13.254/g'

# 修改默认主机名为 OpenWrt
#find package/base-files/ -name "config_generate" | xargs sed -i 's/ImmortalWrt/OpenWrt/g'

# 修改默认主题为 Argon
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-light/Makefile
sed -i 's/luci-theme-bootstrap/luci-theme-argon/g' feeds/luci/collections/luci-nginx/Makefile

# --- 6. 刷新 Feeds ---
./scripts/feeds update -i
./scripts/feeds install -a

echo "所有定制化修改已完成！"
