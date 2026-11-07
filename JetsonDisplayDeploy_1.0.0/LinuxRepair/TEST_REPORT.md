# Jetson 实机测试记录

日期：2026-11-01（Asia/Shanghai）。本报告只记录实际证据，不宣称热插拔验收完成。

## 环境

- NVIDIA Orin NX，Ubuntu 20.04.6，Jetson Linux R35.6.4，5.10.216-tegra，aarch64。
- NoMachine 9.7.3；本地 GDM/Xorg 会话。
- Tk、GI、xrandr、xset、pkexec 可用。

## 已通过

1. 经独立 SSH 连接实机，运行 `app.py --diagnose`，成功生成 JSON 报告。
2. 运行 `app.py --smoke-test`，真实 X11 会话创建 GUI 并返回 `GUI_SMOKE_OK`。
3. 启动完整 GUI，采集窗口画面并检查：中文标签正常、主要按钮和下拉框未裁切。gui-preview.png 为0.2阶段界面示例；0.3新增功能后已另做GUI启动测试。
4. 5 项 unittest：配置只改变指定选项、重复执行拒绝、复杂/其他驱动配置拒绝、断开输出模式排除、EDID 文件头/校验和/扩展块完整性校验。全部在 Jetson Python 上通过。
5. 用户在实机运行受保护配置测试，systemd 日志显示两次恢复完成；其中一次测试于 22:29:31 左右开始，22:32:31 自动恢复，状态文件为 restored。原始文件 SHA256 与记录一致。
6. 测试期间旧 Xorg 日志显示真实显示器 `HIV QHD (DFP-1)` 被识别为 connected，TMDS 输出启用，显示模式为 `DFP-1:nvidia-auto-select`。用户报告物理屏曾亮起。
7. 自动恢复后日志重新出现 `ConnectedMonitor DFP` 被映射为 `DFP-0`，该输出没有 EDID；DFP-1 显示 disconnected。用户报告物理屏再次黑屏。
8. 后续用户再次运行 test-auto，物理屏恢复，并执行 confirm 返回“已保留测试配置”。经 SSH 复核状态为 confirmed，配置中 ConnectedMonitor 已注释，恢复定时器不再列出。物理屏恢复及配置保留已验证。
9. 后续日志记录到 DFP-1 断开、模式切为 NULL，随后再次识别 HIV QHD 并恢复 DP-1 模式，再次断开。可确认驱动记录了插拔/连接状态变化，但这不等于远程画面和键鼠操控已验收。

## 结论

证据支持：本机强制使用泛称 `DFP` 的配置导致实际使用 DFP-0；移除该项后可检测到真实显示器 DFP-1。此次亮屏后黑屏与自动恢复旧配置一致。尚未证明这条配置由 NoMachine 创建。

NoMachine 的有效配置为 `EnableScreenBlanking 0`，不是主动开启隐私黑屏。

## GPU 安装弹窗

弹窗来自软件包 nvidia-l4t-factory-service 提供的 `/etc/xdg/autostart/nvgpusetupexec.sh`。检查脚本发现，它根据 `/dev/fb0` 是否存在选择显示 dGPU 或 iGPU 文案，点击“是”将尝试启动管理员 GPU 安装脚本。因此提示不能作为发现独立显卡或驱动损坏的证据。测试中不安装该驱动，建议选择“否”。没有禁用或删除该自启动项。

## 后续热插拔实测与新方案

- 用户确认：自动检测方案下，拔掉视频线后 NoMachine 黑屏或卡住。因此自动检测方案只解决物理屏恢复，未满足无屏持续操控。
- 用户重新插回显示器后，通过 xrandr 成功读取 DP-1 的真实 EDID，256字节，扩展块数量和校验和通过；SHA256 前12位为 b8707baf4ed7，当前模式1920×1080，实际尺寸344×215mm。
- 新增 DFP-1 + 真实 EDID 固定方案，加入输出结构与防叠加测试，实机回归检查增至6项且全部通过；更新后 GUI 启动测试通过。
- 用户实际启动固定方案，报告：物理屏正常、拔线后 NoMachine 画面与操作正常、插回后物理屏恢复。完成至少一轮三项现场验收。
- 随后用户执行 confirm，SSH 最终复核：status=confirmed，profile=test-edid，当前配置SHA256匹配测试记录，恢复定时器不再active。固定方案已保留。
- 本机本显示器这轮热插拔目标通过。尚不外推为所有显示器、所有载板、无限次插拔或无屏冷启动均通过。

## 尚未完成

- 已确认保留自动检测配置，长期运行和重启后的表现仍待验证。
- 真实 EDID 已在该 Jetson 本地成功采集；通用分发包不包含用户显示器的专用 EDID，避免他人误用。
- 至少一轮拔线后远程画面/操控和插回恢复已由用户确认；重复两轮及长时间运行仍未形成独立记录。
- 未测试不同显示器、不同载板、重启后无屏运行；不能承诺任意 HDMI 热插拔均无缝。
- GUI 内各特权按钮没有逐一手动操作验收；助手的实际 test-auto/定时恢复/confirm 已由用户运行并经 SSH 查证。

本程序没有更换驱动、修改设备树、内核、网络、磁盘、启动参数或 NoMachine 隐私设置。SSH 专用公钥由用户手动添加；程序包不含私钥、SSH 凭据或 sudo 免密规则。
