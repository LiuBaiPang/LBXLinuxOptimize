#!/bin/bash
# ============================================================
#  Jetson 显示修复 - root 动作脚本 (由图形界面自动调用)
#  用法: actions_display.sh apply <auto|hdmi|dp|both> <auto|分辨率> <restart|no>
#        actions_display.sh restore
# ============================================================
set -u
CONF="/etc/X11/xorg.conf.d/99-nvidia-tegra.conf"
BAK="${CONF}.aaafix.bak"

say(){ echo "$*"; }

do_backup(){
    if [ ! -f "$BAK" ]; then
        cp -a "$CONF" "$BAK" && say "✓ 已备份原配置到 $BAK"
    fi
}

modeline_for(){
    case "$1" in
      1920x1080) echo 'Modeline "1920x1080_60" 148.50 1920 2008 2052 2200 1080 1084 1089 1125 +hsync +vsync';;
      1600x900)  echo 'Modeline "1600x900_60" 118.25 1600 1696 1856 2112 900 903 908 934 +hsync +vsync';;
      1280x720)  echo 'Modeline "1280x720_60" 74.25 1280 1390 1430 1650 720 725 730 750 +hsync +vsync';;
      3840x2160) echo 'Modeline "3840x2160_30" 297.00 3840 4016 4104 4400 2160 2168 2178 2250 +hsync +vsync';;
      *) echo "";;
    esac
}

write_conf(){
    local mode="$1" res="$2"
    local cm=""
    case "$mode" in
      hdmi) cm='    Option "ConnectedMonitor" "HDMI-0"';;
      dp)   cm='    Option "ConnectedMonitor" "DP-0"';;
      both) cm='    Option "ConnectedMonitor" "HDMI-0, DP-0"';;
      auto) cm="";;
    esac

    {
        echo 'Section "Device"'
        echo '    Identifier "Tegra GPU"'
        echo '    Driver "nvidia"'
        echo '    Option "AllowEmptyInitialConfiguration" "true"'
        [ -n "$cm" ] && echo "$cm"
        echo 'EndSection'

        if [ -n "$res" ] && [ "$res" != "auto" ]; then
            local ml; ml=$(modeline_for "$res")
            echo ''
            echo 'Section "Monitor"'
            echo '    Identifier "Monitor0"'
            [ -n "$ml" ] && echo "    $ml"
            echo 'EndSection'
            echo ''
            echo 'Section "Screen"'
            echo '    Identifier "Screen0"'
            echo '    Device "Tegra GPU"'
            echo '    Monitor "Monitor0"'
            if [ "$mode" = "hdmi" ]; then
                echo "    Option \"MetaModes\" \"HDMI-0: ${res}_60\""
            elif [ "$mode" = "dp" ]; then
                echo "    Option \"MetaModes\" \"DP-0: ${res}_60\""
            else
                echo "    Option \"MetaModes\" \"HDMI-0: ${res}_60; DP-0: ${res}_60\""
            fi
            echo 'EndSection'
        fi
    } > "$CONF"
    say "✓ 已写入 $CONF (模式=$mode 分辨率=$res)"
}

restart_dm(){
    say "... 正在重启显示服务 gdm3 (画面会黑几秒,请稍等)"
    systemctl restart gdm3
    if [ $? -eq 0 ]; then say "✓ 显示服务已重启"; else say "✗ 重启显示服务失败"; fi
}

case "${1:-}" in
    apply)
        mode="${2:-auto}"; res="${3:-auto}"
        case "$mode" in auto|hdmi|dp|both) ;; *) say "✗ 未知模式 $mode"; exit 1;; esac
        do_backup
        write_conf "$mode" "$res"
        if [ "${4:-no}" = "restart" ]; then restart_dm; else say "· 未重启显示服务,重启电脑后生效"; fi
        ;;
    restore)
        if [ -f "$BAK" ]; then
            cp -a "$BAK" "$CONF"
            say "✓ 已恢复 NVIDIA 原厂配置"
        else
            say "· 没有找到备份,无需恢复"
        fi
        ;;
    *)
        say "用法: $0 apply <auto|hdmi|dp|both> <auto|分辨率> <restart|no> | restore"
        exit 1
        ;;
esac
