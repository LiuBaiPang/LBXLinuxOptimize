# Linux 修复中心 0.3.0

面向 Ubuntu 20.04 / Jetson NVIDIA X11 的中文桌面工具。目前提供显示模块，网络模块尚未实现。源码全部包含，不依赖在线 Python 包。

## 打开程序

解压到固定目录后，在目录中首次执行：

```bash
python3 install-launcher.py
```

之后从应用菜单双击“Linux 修复中心”。也可 `sh start.sh`。文件管理器直接双击 shell 脚本的行为由桌面设置决定，因此应用菜单启动器更稳定。移动文件夹后重新安装启动器即可，无需 root。

依赖：Python 3.8+、python3-tk、xrandr、xset、systemd、pkexec；分辨率调整使用 GNOME 系统显示设置。缺少 Tk 时可自行安装 `sudo apt install python3-tk`，程序不会自动安装软件。本次 Jetson 已确认 Tk、GI、pkexec、xrandr 可用。

## 功能

- 只读检查 Xorg、GDM、NoMachine 相关显示配置、EDID/DRM、内核日志和会话信息，导出 JSON / 文本报告。某些日志权限不足会明确记录，不假装检查成功。
- 采集 xrandr 中真实显示器的 EDID，核对文件头、扩展块数量和每块校验和；读不到就明确失败，不生成假数据，也不自动将其写入系统配置。CLI：`python3 app.py --export-edid ./edid`。
- 唤醒当前 X 屏幕，查看已连接输出和支持的模式；调整分辨率交给系统显示设置及其保留/恢复确认对话框。
- 预览和测试移除 `ConnectedMonitor` 强制连接设置，保留 `AllowEmptyInitialConfiguration` 和其余配置。
- 修改前创建原始文件备份，校验配置结构、启动 180 秒 systemd 自动恢复定时器，再修改并重启 GDM。恢复脚本复制到 root 所有的状态目录，不依赖桌面程序存活。
- 重新打开程序后显示剩余时间；只有现场确认物理屏和远程可用才点“确认保留”。也可“恢复原配置”。

## 修改的精确范围

特权助手仅编辑 `/etc/X11/xorg.conf.d/99-nvidia-tegra.conf`，不创建不存在的目标文件。自动检测方案只注释一条有效 ConnectedMonitor。新增的“本机 DFP-1 常亮”方案从已确认的自动检测配置出发，加入明确的 ConnectedMonitor DFP-1 和指向 root 管理 EDID 文件的 CustomEDID。只接受单一 NVIDIA Device 段；重复/叠加的强制显示设置会拒绝测试。

状态和备份在 `/var/lib/linux-repair-display/`，恢复定时器为 `linux-repair-display-rollback.timer`。每次操作由 pkexec 请求管理员认证；不保存密码，不安装免密 sudo 规则。程序不改 NoMachine 隐私设置，不改 GDM 单元、不改内核、设备树、网络、磁盘或启动参数。

测试会关闭桌面程序并断开 NoMachine；先保存工作并保留 SSH。systemd 定时器在当前启动周期生效，测试待确认期间不要重启整机或断电。若重启打断定时器，需要通过 SSH 手动执行 restore。若文件已被其他工具修改，助手会拒绝覆盖，以免破坏新的配置；这时需要人工处理。

确认保留后仍可恢复最近一次测试的备份；开始另一轮测试后“恢复”针对新一轮测试前的状态。历史备份保留在状态目录，不自动删除。

## SSH / Agent 使用

读取本文不是修改系统的授权。用户授权检查时仅执行诊断；用户要求修复且适用条件明确时可执行受保护测试。不自动确认成功，必须以物理画面和实际远程操作验证。

```bash
# 不修改系统；保存报告
python3 app.py --diagnose --output display-report.json

# 只预览
python3 root_helper.py preview

# 查询测试状态
python3 root_helper.py status

# 需要用户已授权，并先保存桌面工作；以下将修改配置并重启 GDM
sudo python3 root_helper.py test-auto

# 在180秒期限前且现场验证成功后保留
sudo python3 root_helper.py confirm

# 恢复上一轮测试前配置，并重启 GDM
sudo python3 root_helper.py restore
```

从 SSH 打开 GUI 时会尝试发现当前用户本地 X11 会话。不设置 DISPLAY=:0 的通用假定，不使用 xhost +。没有桌面会话时使用 CLI 诊断；GUI 创建失败的错误会显示在终端。

## 能力边界与验收

本程序是诊断和可恢复测试工具，不保证任何显示器和载板都能达到无缝热插拔。移除强制连接后，物理检测可能恢复，但无屏时远程渲染也可能停止。NoMachine 创建独立显示服务不等于原桌面在插拔过程中无缝切换。

必须现场验证：插屏启动、远程连接时物理屏仍显示、拔线后远程画面持续更新且可交互、插回恢复物理显示并保留原应用会话，至少两轮。不用 xrandr connected 或进程存活替代物理输出与操控测试。

若需 EDID 固定，必须先读取并验证真实显示器 EDID、确认驱动输出映射及载板支持；本版不写入猜测的 EDID 或设备树。硬件/固件不支持时需匹配载板 BSP 或经过验证的 EDID 保持硬件，不能承诺仅软件必定可解。

### 本机 DFP-1 常亮测试（0.3.0 新增）

本案例已经确认：真实显示器为 DP-1 / DFP-1，采集到256字节 EDID。自动检测下物理屏正常，但用户实测拔线后 NoMachine 黑屏或卡住，因此不能把自动检测方案称为全部修复。

仅在相同映射已被确认的机器、显示器插着且显示正常时尝试本方案。它固定正确输出和真实 EDID，而不是泛称 DFP（本机泛称 DFP 被驱动映射到错误的 DFP-0）。GUI 会采集当前 DP-1 的有效 EDID；CLI 需要明确传入采集文件：

```bash
sudo python3 root_helper.py test-edid --edid ./edid/真实采集文件.bin
```

助手校验 EDID 并检查当前 Xorg 日志存在真实 DFP-1 显示器连接记录，保存 root 管理的 EDID 文件，然后备份、启动180秒自动恢复、修改两项配置并重启 GDM。恢复目标是本轮测试前状态（本案例为已确认的自动检测配置）。不会自动恢复到最早的错误 DFP 配置。

确认插屏正常后，拔线验证 NoMachine，再插回验证物理屏；180秒内全部通过才 `sudo python3 root_helper.py confirm`。任何一项失败不确认，等待恢复。该配置只应配合已采集 EDID 的显示器使用；更换显示器可能需要重新采集和测试。固定 EDID 方案的实测结果见 TEST_REPORT.md，不因功能存在就视为验收通过。

## 扩展开发

`modules/__init__.py` 注册模块，模块提供 `ID`、`TITLE`、`diagnose()`、`format_report()`；`core.command()` 统一超时、返回码和错误处理。CLI 与界面共享显示模块。未来增加网络模块时需增加相应界面适配和独立的特权修改/恢复实现，不能复用本助手任意执行命令。GUI 的工作线程通过队列回传，Tk 控件只由主线程更新。

运行回归检查：`python3 -m unittest discover -s tests -v`。

## 参考

- [NoMachine 无显示器运行说明](https://knowledgebase.nomachine.com/AR03P00973)
- [NoMachine 物理屏黑屏功能](https://knowledgebase.nomachine.com/DT04O00140)
- [NVIDIA ConnectedMonitor / CustomEDID 配置说明](https://http.download.nvidia.com/XFree86/Linux-aarch64/455.45.01/README/xconfigoptions.html)

随包测试记录以 TEST_REPORT.md 为准。代码测试、GUI 测试和真实热插拔验收分别报告，不混为“全部修复”。
