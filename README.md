本项目使用 ImmortalWrt 编译 x86-64 固件。主要工作流位于 `.github/workflows/x86_64.yml`；根目录的 `x86_64.yml` 是同步副本，GitHub Actions 不会执行根目录副本。

## 本项目的自动构建与发布

- 在 `.github/upstream.env` 统一设置上游仓库、分支、构建目标和 Go 工具链分支。当前使用 `immortalwrt/immortalwrt` 的 `master` 和 Go `27.x`，满足 Xray 26.9.9 的 Go >= 1.27 要求。
- `Update Checker` 对比上游最新提交和上一次成功公开发布的固件记录；有变化且没有正在执行的构建时，触发 `x86_64`，并传入确切的上游 SHA。失败、草稿和其他设备的 Release 不会推进基线，下一次检查会重试。
- 手动执行 `x86_64` 时，可以留空 `upstream_commit` 编译配置分支的最新版本，或填写该分支历史中的完整 40 位 SHA。
- 工作流使用内置 `GITHUB_TOKEN`：构建工作流声明 `contents: write`，检查工作流声明 `actions: write`。不再依赖 `ACTIONS_TRIGGER_PAT`。仓库或组织策略需要允许这两项权限。
- 包定制后重新建立 feeds 索引再安装；下载和编译失败时各进行一次单线程详细重试。日志 artifact 保留 14 天，固件 artifact 保留 7 天。
- `x86_64_diy-part2.sh` 在替换 OpenClash 软件包后修补其启动脚本：`del_cron()` 使用 `crontab "$CRON_FILE"` 重新加载任务，避免定时订阅更新重启 cron 时终止自身的 OpenClash 重启流程。已包含此修复的上游保持原样；文件缺失或函数不匹配会使定制阶段失败，防止静默生成未修复的固件。
- 每次构建都会重新查询 Lucky、EasyTier 官方 GitHub 的最新稳定 Release，选取 Linux x86-64 核心，把官方 SHA256 写入 OpenWrt 下载配方；获取版本、资产或校验失败时停止构建，不回退旧核心。Lucky 使用官方普通版；EasyTier 保留 LuCI 插件，预装静态 `easytier-core` 和供插件查询状态使用的同版本 CLI，关闭 `EASYTIER_INCLUDE_WEBCONSOLE`，不安装独立 Web 控制台。配置只选入一份 `easytier` 核心包。
- OpenClash 插件和 Smart 核心按 `master` 通道“检查更新”使用的版本清单查询，固定插件源码、清单与核心的 Git SHA，并校验核心 Git blob 和 SHA256；新装默认选用 Smart。Smart 是该通道的最新测试版，不将其描述为稳定 Meta。源码版本与清单不符时，仅允许匹配清单版本的官方标签，无法匹配则停止构建。
- AdGuard Home 每次查询官方最新稳定 Release，固定标签源码与官方前端的哈希，保持 Go 源码编译。MosDNS 每次核对官方基础版本与 sbwml 最新稳定 Release，锁定源码 SHA 并验证 `stats_api` 补丁存在；基础版本不兼容时停止构建，保留统计功能。
- 每个组件的版本、Git SHA 和归档 SHA256 会合并写入 `custom-sources.json`，并出现在发布附件 `build-info.json` 中。每次构建重新查询最新版本，当前构建使用已选的确切版本和哈希。基础固件和其他软件包继续跟随配置的 ImmortalWrt 分支与 feeds。
- OpenClash 的 conffiles、keep.d 及插件升级的临时备份/恢复只保留配置、订阅、覆写、规则、历史、GeoDB 和模型数据，排除 `core/` 和根目录 `clash` 链接。推荐额外备份路径只有 `/etc/lucky/`、`/etc/AdGuardHome.yaml`、`/etc/AdGuardHome/data/` 和自定义 MosDNS YAML，避免带入 AGH 的旧核心备份目录。
- **首次使用此修改升级前**，需要先同步旧设备的保留清单，用 `sysupgrade -l` 确认核心不在备份中；新固件不能改变旧设备在升级前已生成的备份。自定义列表中的整个 `/etc`、`/usr/bin`、`/etc/openclash`、`/etc/AdGuardHome` 仍会扩大保留范围，应改成数据路径。构建不会增加升级后恢复启动脚本的机制。
- 只有成功生成固件镜像才会发布。先上传至草稿 Release，再公开；之后保留最近 3 个由新脚本管理的正式固件 Release。旧版、手工发布、预发布和草稿不参与自动清理，Git 标签保留。

## Release 会写明什么

1. **更新摘要**：按上游原始提交标题归类为“新增功能与支持”“问题修复”“版本升级与功能调整”“其他变更”，分别列出内容和提交链接。版本号升级会归为版本调整；没有明确新增说明时，不会补写不存在的新功能。
2. **精确范围**：从上次成功发布记录的 ImmortalWrt SHA 到本次实际编译 SHA，统计完整范围，最多展示最近 100 个提交，并提供完整对比链接。上游提交可能涉及其他设备，并不表示相关功能全部选入本固件。
3. **组件变化表**：列出与上次发布相比发生变化的 feeds、Go、自定义软件包来源提交和 OpenClash 核心哈希；完整值保存在附件 `build-info.json`。这些组件的版本变化单独记录，不冒充 ImmortalWrt 主仓库的新功能。
4. **构建信息**：包含目标、源码 SHA、本仓库配置 SHA、配置文件 SHA256 和本次 Actions 链接。

第一次使用新脚本时，旧 Release 没有上游版本记录，因此会明确标为“首次生成更新记录”，展示最近 20 个提交；第二次成功发布起提供精确增量。相同上游 SHA 的手动重编会标记“主仓库无新增提交”，并继续显示组件变化。

Release 正文底部有 `<!-- immortalwrt-build ... -->` 隐藏记录，供后续构建和更新检查器读取；编辑正文时请保留。API 读取失败会报错，不会被误认为首次发布。

修改后请将 `.github/`、`scripts/`、`tests/` 和 DIY 脚本一起提交，再对新提交运行 **Run workflow**。

## 本地验证

以下测试使用临时 Git 仓库和模拟命令，不会编译固件或发布到 GitHub：

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -v
bash tests/test_customization.sh
actionlint .github/workflows/x86_64.yml .github/workflows/update-checker.yml
```

构建工作流在耗时编译前会自动执行不依赖第三方 Python 库的 Release 测试和定制脚本场景测试。

---

以下保留原模板说明。

**English** | [中文](https://p3terx.com/archives/build-openwrt-with-github-actions.html)

# Actions-OpenWrt

[![LICENSE](https://img.shields.io/github/license/mashape/apistatus.svg?style=flat-square&label=LICENSE)](https://github.com/P3TERX/Actions-OpenWrt/blob/master/LICENSE)
![GitHub Stars](https://img.shields.io/github/stars/P3TERX/Actions-OpenWrt.svg?style=flat-square&label=Stars&logo=github)
![GitHub Forks](https://img.shields.io/github/forks/P3TERX/Actions-OpenWrt.svg?style=flat-square&label=Forks&logo=github)

A template for building OpenWrt with GitHub Actions

## Usage

- Click the [Use this template](https://github.com/P3TERX/Actions-OpenWrt/generate) button to create a new repository.
- Generate `.config` files using [Lean's OpenWrt](https://github.com/coolsnowwolf/lede) source code. ( You can change it through environment variables in the workflow file. )
- Push `.config` file to the GitHub repository.
- Select `Build OpenWrt` on the Actions page.
- Click the `Run workflow` button.
- When the build is complete, click the `Artifacts` button in the upper right corner of the Actions page to download the binaries.

## Tips

- It may take a long time to create a `.config` file and build the OpenWrt firmware. Thus, before create repository to build your own firmware, you may check out if others have already built it which meet your needs by simply [search `Actions-Openwrt` in GitHub](https://github.com/search?q=Actions-openwrt).
- Add some meta info of your built firmware (such as firmware architecture and installed packages) to your repository introduction, this will save others' time.

## Credits

- [Microsoft Azure](https://azure.microsoft.com)
- [GitHub Actions](https://github.com/features/actions)
- [OpenWrt](https://github.com/openwrt/openwrt)
- [coolsnowwolf/lede](https://github.com/coolsnowwolf/lede)
- [Mikubill/transfer](https://github.com/Mikubill/transfer)
- [softprops/action-gh-release](https://github.com/softprops/action-gh-release)
- [Mattraks/delete-workflow-runs](https://github.com/Mattraks/delete-workflow-runs)
- [dev-drprasad/delete-older-releases](https://github.com/dev-drprasad/delete-older-releases)
- [peter-evans/repository-dispatch](https://github.com/peter-evans/repository-dispatch)

## License

[MIT](https://github.com/P3TERX/Actions-OpenWrt/blob/main/LICENSE) © [**P3TERX**](https://p3terx.com)
