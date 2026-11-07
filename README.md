# AAAFIXsystem

Jetson Orin NX (L4T R35.6.4) 系统修复工具集。

- `1-显示修复/` — HDMI 黑屏修复(根因: ConnectedMonitor DFP)
- `2-网络修复/` — 断网修复(WiFi 省电 + DNS 双服务)
- `3-快捷截图/` — 截图工具
- `LinuxRepair/` 与打包 zip — 通用修复部署包

## 自动同步

本目录由 systemd 用户服务 `aaafix-autosync.service` 监护:
每 15 秒检测一次,发现任何修改即自动 `git commit` 并推送到
https://github.com/LiuBaiPang/LBXLinuxOptimize (main 分支)。

- 脚本: `~/.local/bin/aaafix-autosync.sh`
- 服务: `~/.config/systemd/user/aaafix-autosync.service`(已启用 linger,开机自启)
- 日志: `journalctl --user -u aaafix-autosync` 或 `journalctl -t aaafix-autosync`
- 手动启停: `systemctl --user stop|start aaafix-autosync`
