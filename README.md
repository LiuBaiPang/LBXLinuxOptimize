# AAAFIXsystem

Jetson Orin NX (L4T R35.6.4) 系统修复工具集。

- `1-显示修复-linux-arm64-JetsonOrinNX/` — HDMI 黑屏修复(根因: ConnectedMonitor DFP)
- `2-网络修复-linux-arm64-JetsonOrinNX/` — 断网修复(WiFi 省电 + DNS 双服务)
- `3-快捷截图-linux-arm64-JetsonOrinNX/` — 截图工具
- `4-右键复制路径-linux-arm64-JetsonOrinNX/` — Nautilus 右键"复制完整路径"(支持一级菜单/脚本子菜单两档)
- `LinuxRepair/` 与打包 zip — 通用修复部署包

## 项目命名规范(新项目必须遵守)

每个修复项目目录名 = **编号-功能-系统内核-芯片架构-机型**

| 字段 | 说明 | 示例 |
|---|---|---|
| 编号 | 递增序号 | `4-` |
| 功能 | 干什么的 | `声卡修复` |
| 系统内核 | 操作系统内核 | `linux` |
| 芯片架构 | CPU 架构 | `arm64`(PC 一般为 `x64`) |
| 机型 | 设备型号 | `JetsonOrinNX`;非特定机型写 `普通PC` |

示例:`4-声卡修复-linux-arm64-JetsonOrinNX`、`5-蓝牙修复-linux-x64-普通PC`

## 自动同步

本目录由 systemd 用户服务 `aaafix-autosync.service` 监护:
每 15 秒检测一次,发现任何修改即自动 `git commit` 并推送到
https://github.com/LiuBaiPang/LBXLinuxOptimize (main 分支, 私有)。

- 脚本: `~/.local/bin/aaafix-autosync.sh`
- 服务: `~/.config/systemd/user/aaafix-autosync.service`(已启用 linger,开机自启)
- 日志: `journalctl --user -u aaafix-autosync` 或 `journalctl -t aaafix-autosync`
- 手动启停: `systemctl --user stop|start aaafix-autosync`
