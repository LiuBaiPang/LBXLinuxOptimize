#!/bin/bash
# 串口监控工具 —— root 动作脚本(由 serial_monitor.py 通过 sudo -S 调用,勿手工改格式)
# 用法: serial_actions.sh <子命令>
# 全部子命令幂等,可重复执行。
#
# 背景: 40-pin 排针 8(TX)/10(RX) 脚的 UART 是 /dev/ttyAMA0, 出厂被系统用作
# 内核控制台(console=ttyAMA0) + 登录终端(serial-getty@ttyAMA0), 外接设备
# (如飞控)的数据流会被内核日志/登录提示污染, 且权限是 root:tty 普通用户只写。
# free-40pin 一键释放: 去掉 console 参数(需重启) + 停用 getty(立即) + udev 放权。
set -uo pipefail

EXTLINUX="/boot/extlinux/extlinux.conf"
GETTY_UNIT="serial-getty@ttyAMA0.service"
UDEV_RULE="/etc/udev/rules.d/99-aaafix-uart.rules"
BACKUP="/boot/extlinux/extlinux.conf.aaafix-40pin.bak"

# ---------- 子命令 ----------

# 1) 状态报告(不需要 root 也可看个大概)
status() {
    echo "== console= 启动参数 =="
    if [ -r "$EXTLINUX" ]; then
        grep -o "console=[^ ]*" "$EXTLINUX" || echo "(extlinux.conf 里没有 console=)"
    else
        echo "(extlinux.conf 不可读, 需 root)"
    fi
    echo "== serial-getty@ttyAMA0 =="
    echo "enabled : $(systemctl is-enabled "$GETTY_UNIT" 2>/dev/null || echo 未启用)"
    echo "active  : $(systemctl is-active "$GETTY_UNIT" 2>/dev/null || echo 未激活)"
    echo "== udev 放权规则 =="
    [ -f "$UDEV_RULE" ] && echo "存在: $UDEV_RULE" || echo "不存在(未释放过)"
    echo "== extlinux 备份 =="
    [ -f "$BACKUP" ] && echo "存在: $BACKUP" || echo "不存在(未释放过)"
    echo "== 设备现状 =="
    ls -l /dev/ttyAMA0 2>/dev/null
    echo "== 内核控制台 =="
    cat /sys/class/tty/console/active 2>/dev/null
}

# 2) 释放 40-pin 串口(ttyAMA0)给用户程序/飞控 —— 幂等
free-40pin() {
    # 2.1 备份 extlinux.conf(只备份第一次, 保留出厂原样)
    if [ -f "$EXTLINUX" ] && [ ! -f "$BACKUP" ]; then
        cp "$EXTLINUX" "$BACKUP" && echo "已备份: $BACKUP"
    fi
    # 2.2 从 APPEND 行去掉 console=ttyAMA0,115200(幂等; 不动 ttyTCU0 调试口)
    if [ -f "$EXTLINUX" ] && grep -q "console=ttyAMA0" "$EXTLINUX"; then
        sed -i -e 's/ console=ttyAMA0,[0-9]*//g' \
               -e 's/console=ttyAMA0,[0-9]* //g' \
               -e 's/console=ttyAMA0,//g' "$EXTLINUX"
        if grep -q "console=ttyAMA0" "$EXTLINUX"; then
            echo "警告: extlinux.conf 中仍有 console=ttyAMA0, 请手工检查 $EXTLINUX"
        else
            echo "OK: 已从 extlinux.conf 移除 console=ttyAMA0(重启后内核日志不再打进 40-pin)"
        fi
    else
        echo "跳过: extlinux.conf 已无 console=ttyAMA0(或文件不存在)"
    fi
    # 2.3 停用串口登录终端(立即生效, 重启后也不会再拉起)
    if systemctl disable --now "$GETTY_UNIT" 2>/dev/null; then
        echo "OK: 已停用 $GETTY_UNIT(登录提示不再占用 40-pin)"
    else
        echo "跳过: $GETTY_UNIT 不存在或停用失败"
    fi
    # 2.4 udev 规则: ttyAMA0 归 dialout 组, dialout 组用户免 sudo 读写
    cat > "$UDEV_RULE" <<'EOF'
# AAAFIXsystem: 40-pin 串口 ttyAMA0 放权给 dialout 组(串口监控工具管理)
KERNEL=="ttyAMA0", GROUP="dialout", MODE="0660"
EOF
    udevadm control --reload-rules 2>/dev/null
    # 立即对当前已存在的设备节点生效
    chgrp dialout /dev/ttyAMA0 2>/dev/null && chmod 0660 /dev/ttyAMA0 2>/dev/null
    echo "OK: udev 放权规则已写入($UDEV_RULE)并即时生效"
    echo "完成: 重启后 40-pin 8/10 脚(ttyAMA0)完全释放, 可直连飞控"
}

# 3) 恢复 40-pin 串口为系统默认 —— 幂等
restore-40pin() {
    if [ -f "$BACKUP" ]; then
        cp "$BACKUP" "$EXTLINUX" && echo "OK: 已从备份恢复 extlinux.conf"
    else
        echo "跳过: 无备份($BACKUP 不存在), console= 参数应未被改过"
    fi
    if systemctl enable --now "$GETTY_UNIT" 2>/dev/null; then
        echo "OK: 已恢复 $GETTY_UNIT"
    else
        echo "跳过: 无法启用 $GETTY_UNIT(可能本来就没有)"
    fi
    rm -f "$UDEV_RULE" && echo "OK: 已删除 udev 放权规则"
    udevadm control --reload-rules 2>/dev/null
    chgrp tty /dev/ttyAMA0 2>/dev/null
    chmod 0620 /dev/ttyAMA0 2>/dev/null
    echo "完成: 重启后恢复系统默认(40-pin 8/10 脚 = 内核控制台 + 登录终端)"
}

# ---------- 分发 ----------
case "${1:-}" in
    status)        status ;;
    free-40pin)    free-40pin ;;
    restore-40pin) restore-40pin ;;
    *)  echo "用法: serial_actions.sh <status|free-40pin|restore-40pin>"
        exit 2 ;;
esac
