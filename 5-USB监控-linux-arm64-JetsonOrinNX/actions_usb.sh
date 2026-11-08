#!/bin/bash
# USB 监控工具 —— root 动作脚本(由 usb_monitor.py 通过 sudo -S 调用,勿手工改格式)
# 用法: actions_usb.sh <子命令> [参数]
# 全部子命令幂等,可重复执行。
set -uo pipefail

INA_RULE="/etc/udev/rules.d/99-aaafix-ina3221.rules"
USBMON_PERSIST="/etc/modules-load.d/aaafix-usbmon.conf"

# ---------- 工具函数 ----------
usb_sysfs_devices() {
    # 列出所有设备(不含接口,即不含冒号)
    for d in /sys/bus/usb/devices/*; do
        [ -e "$d" ] || continue
        case "$(basename "$d")" in *:*) continue ;; esac
        echo "$d"
    done
}

# ---------- 子命令 ----------

# 1) INA3221 电压/电流读取权限修复:
#    板载 ina3221 的 hwmon 数值文件是 0400 root-only,普通用户读不到。
#    装一条 udev 规则(设备出现时自动放只读权限)并立即对当前生效。
ina-readfix() {
    cat > "$INA_RULE" <<'EOF'
# AAAFIXsystem: 允许普通用户读取 INA3221 电源监测数据(只读)
ACTION=="add", SUBSYSTEM=="i2c", ATTR{name}=="ina3221", \
  RUN+="/bin/sh -c 'for d in /sys$devpath/hwmon/hwmon*; do [ -d \"$d\" ] && chmod 0444 \"$d\"/in*_input \"$d\"/in*_label \"$d\"/curr*_input 2>/dev/null; done; true'"
EOF
    udevadm control --reload-rules 2>/dev/null
    # 立即对当前已存在的 ina3221 生效
    for d in /sys/class/hwmon/hwmon*; do
        [ -r "$d/name" ] || continue
        if [ "$(cat "$d/name" 2>/dev/null)" = "ina3221" ]; then
            chmod 0444 "$d"/in*_input "$d"/in*_label "$d"/curr*_input 2>/dev/null
            echo "已放权: $d"
        fi
    done
    echo "OK: INA3221 读取权限修复完成(udev 规则: $INA_RULE)"
}

ina-readundo() {
    rm -f "$INA_RULE"
    udevadm control --reload-rules 2>/dev/null
    echo "OK: 已移除 INA3221 放权规则(重启后恢复 root-only)"
}

# 2) 启用 jetson-stats 的 jtop 守护服务(jtop 组用户可免 root 读电源功率)
jtop-enable() {
    if systemctl enable --now jtop.service 2>/dev/null; then
        echo "OK: jtop.service 已启用"
    else
        echo "FAIL: 系统里没有 jtop.service(未安装 jetson-stats?),面板会继续用其他数据源"
    fi
}

# 3) USB 自动休眠开关: on=禁止休眠(设备常供电,治掉线/掉速), auto=恢复系统默认
autosuspend-off() {
    local n=0 f
    for f in $(usb_sysfs_devices); do
        if [ -w "$f/power/control" ]; then
            echo on > "$f/power/control" 2>/dev/null && n=$((n+1))
        fi
    done
    echo "OK: 已禁止 $n 个 USB 设备的自动休眠(power/control=on)"
}

autosuspend-auto() {
    local n=0 f
    for f in $(usb_sysfs_devices); do
        if [ -w "$f/power/control" ]; then
            echo auto > "$f/power/control" 2>/dev/null && n=$((n+1))
        fi
    done
    echo "OK: 已恢复 $n 个 USB 设备的自动休眠(power/control=auto)"
}

# 4) 单设备电源策略: dev-power <设备名如 2-1.2> on|auto
dev-power() {
    local dev="$1" mode="$2"
    [ -n "${dev:-}" ] || { echo "FAIL: 缺少设备名参数"; return 1; }
    local p="/sys/bus/usb/devices/$dev/power/control"
    if [ ! -w "$p" ]; then
        echo "FAIL: 找不到 $p(设备不在线?)"
        return 1
    fi
    echo "$mode" > "$p" && echo "OK: $dev power/control=$mode"
}

# 5) 重新枚举设备: reenum <设备名>  (authorized 0->1,相当于重新插拔,不动数据)
reenum() {
    local dev="$1"
    [ -n "${dev:-}" ] || { echo "FAIL: 缺少设备名参数"; return 1; }
    local p="/sys/bus/usb/devices/$dev/authorized"
    if [ ! -w "$p" ]; then
        echo "FAIL: 找不到 $p(设备不在线?)"
        return 1
    fi
    echo 0 > "$p"; sleep 0.3; echo 1 > "$p"
    echo "OK: $dev 已重新枚举"
}

# 6) usbmon 模块加载/开机自启(深度模式用)
usbmon-on() {
    modprobe usbmon 2>/dev/null
    if ls /sys/kernel/debug/usb/usbmon*u >/dev/null 2>&1; then
        echo usbmon > "$USBMON_PERSIST"
        echo "OK: usbmon 已加载并设为开机自动加载"
    else
        echo "FAIL: usbmon 加载后仍无 /sys/kernel/debug/usb/usbmon*u(内核可能未编译 USB_MON)"
        return 1
    fi
}

usbmon-off() {
    rm -f "$USBMON_PERSIST"
    modprobe -r usbmon 2>/dev/null
    echo "OK: usbmon 已卸载并取消开机自动加载"
}

# 7) 功耗模式切换: powermode <0|1|2|3>
powermode() {
    [ -n "${1:-}" ] || { echo "FAIL: 缺少模式ID(0=MAXN 1=10W 2=15W 3=25W)"; return 1; }
    if command -v nvpmodel >/dev/null 2>&1; then
        nvpmodel -n "$1" && echo "OK: 功耗模式已切换(ID=$1)"
    else
        echo "FAIL: 找不到 nvpmodel"
        return 1
    fi
}

# ---------- 分发 ----------
case "${1:-}" in
    ina-readfix|ina-readundo|jtop-enable|autosuspend-off|autosuspend-auto|usbmon-on|usbmon-off)
        "$1" ;;
    dev-power|reenum)   "$1" "${2:-}" ;;
    powermode)          powermode "${2:-}" ;;
    *)  echo "用法: actions_usb.sh <ina-readfix|ina-readundo|jtop-enable|autosuspend-off|autosuspend-auto|usbmon-on|usbmon-off|dev-power <dev> on|auto|reenum <dev>|powermode <id>>"
        exit 2 ;;
esac
