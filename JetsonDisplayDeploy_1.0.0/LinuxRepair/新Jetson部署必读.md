# 同型号新 Jetson 部署说明（固化版 1.0.0）

本包固化的是部署流程、配置生成器、检查和恢复机制，不把参考机的 EDID 或密码写进所有机器。

## 支持范围

- 与参考机相同的 Orin NX 和载板、相同显示接口布局。
- Jetson Linux R35.6.4 / Ubuntu 20.04 / NVIDIA X11 + GDM。
- 已安装并能使用 ARM64 NoMachine。参考版本9.7.3；其他版本另做远程操控验收。
- 当前目标文件为 `/etc/X11/xorg.conf.d/99-nvidia-tegra.conf`，单个 NVIDIA Device 段，且没有其他自定义 Device/Screen/ServerLayout 冲突配置。
- 已实际确认真实显示路径 DP-1 / DFP-1。系统识别 Orin NX 不等于确认载板相同；操作者仍需核对载板型号和视频接法。

不匹配时工具停止，不自动安装驱动、覆盖复杂配置或修改设备树。若新机没有目标文件，先运行图形诊断导出报告适配，不能随便新建配置去覆盖未知布局。此包不会将不支持的系统误报为部署成功。

## 每台新机的标准操作

1. 保存工作、连接并打开要使用的显示器；建立 SSH，验证 NoMachine 可登录。把ZIP解压到固定目录，例如 `~/AAAFIXsystem/LinuxRepair`。
2. 普通用户在目录中执行：

```bash
python3 install-launcher.py
python3 deploy.py check
```

启动器安装仅作用于当前用户。从应用菜单可打开“Linux 修复中心”。缺少 Tk 时先安装 `sudo apt install python3-tk`；命令行部署本身不依赖 Tk。

3. 如果检测到旧 `ConnectedMonitor DFP`，先完成物理自动检测测试：

```bash
sudo python3 root_helper.py test-auto
```

物理屏恢复且桌面正常时，在180秒内执行：

```bash
python3 deploy.py confirm
```

这一步会请求单次 sudo。若测试失败，不确认，等待恢复后导出诊断。不需要执行这一步的正常新机直接进入第4步。

4. 显示器保持插着，普通用户执行：

```bash
python3 deploy.py check
python3 deploy.py apply
```

脚本读取本机型号/系统版本、当前真实 DP-1 EDID 和 Xorg DFP-1 连接记录；检查配置冲突。输入 `APPLY` 后才请求 sudo，将真实 EDID 保存到 root 管理目录，备份原配置、启动180秒恢复保护、写入正确输出和 CustomEDID 并重启桌面。NoMachine 会断开，稍候重新连接。

5. 在180秒内验收：物理屏正常；拔掉视频线，在 NoMachine 拖动窗口/输入文字；插回，物理屏恢复。全部通过再执行：

```bash
python3 deploy.py confirm
python3 deploy.py status
```

必须看到“已保留测试配置”和 `status: confirmed`、`profile: test-edid`。在聊天里回复“已确认”不等于执行系统命令。

失败时不确认，等待恢复，状态通常变为 restored。不要在待确认时重启或断电；保护定时器限本次启动周期。需要撤销本轮修改可执行 `python3 deploy.py restore`，该命令会恢复本轮测试前配置并重启桌面。

6. 再重复一轮拔插。安排维护窗口验证插屏重启、无屏重启及长时间远程操控；这些不由脚本自动完成。

## 固化与幂等

确认后 Xorg 配置和 EDID 保存在系统磁盘上，不需要每次开机重新运行程序。相同EDID、相同已确认配置再次运行 check/apply 会报告已部署，不重复重启桌面。若屏幕已断开且无法读取EDID，检查会停止；这不是需要重新修复的依据。

更换显示器时不得继续假定旧EDID兼容。先备份并恢复上一轮配置，接上新显示器重新采集测试。注意恢复是最近一轮备份，不一定是出厂配置。历史备份位于 `/var/lib/linux-repair-display/`。

如果批量机器使用完全相同显示器，也仍建议逐台读取 EDID，避免同名不同批次/不同面板导致显示模式差异。包内不含参考机专用EDID、SSH私钥、用户密码或NoMachine账户。

## Agent 执行约束

用户要求部署时执行上述检查和受保护测试；仅要求查看时只运行 check/diagnose/status。必须保留完整返回码和错误输出；不自动执行 confirm，不把 xrandr connected 当作用户实际拔插验收。配置映射、系统版本、EDID或权限不符合时停止依赖性修改，不降低检查标准来“强行成功”。

批量分发前应校验根目录 SHA256SUMS.txt。它用于检查传输完整性，不是软件签名。回归测试用 `python3 -m unittest discover -s tests -v`。实测边界和参考机记录见 TEST_REPORT.md。
