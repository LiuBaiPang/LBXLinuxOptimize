#!/bin/bash
# ============================================================
#  Jetson 网络修复 - root 动作脚本 (由图形界面自动调用)
#  用法: actions_network.sh apply <off|on|keep> <auto|aliyun|114|tencent|keep> <on|off|keep>
#        actions_network.sh repair
# ============================================================
set -u
NM_CONF_DIR="/etc/NetworkManager/conf.d"
PS_CONF="$NM_CONF_DIR/aaafix-wifi-powersave.conf"
DNS_NM_CONF="$NM_CONF_DIR/aaafix-dns.conf"
RC_DIR="/etc/aaafix"
RESOLV="/etc/resolv.conf"
WD_BIN="/usr/local/sbin/aaafix-net-watchdog"
UNIT="/etc/systemd/system/aaafix-net-watchdog.service"
TIMER="/etc/systemd/system/aaafix-net-watchdog.timer"
LOG="/var/log/aaafix-net-watchdog.log"

say(){ echo "$*"; }
nm_changed=0

# ---------- WiFi 省电 ----------
apply_powersave(){
    case "$1" in
      off)
        mkdir -p "$NM_CONF_DIR"
        printf '[connection]\nwifi.powersave = 2\n' > "$PS_CONF"
        for i in /sys/class/net/wl*; do
            [ -e "$i" ] || continue
            d=$(basename "$i")
            if iw dev "$d" set power_save off 2>/dev/null; then
                say "✓ $d 已实时关闭省电"
            fi
        done
        say "✓ WiFi省电已永久关闭(配置: $PS_CONF)"
        nm_changed=1;;
      on)
        rm -f "$PS_CONF"
        for i in /sys/class/net/wl*; do
            [ -e "$i" ] && iw dev "$(basename "$i")" set power_save on 2>/dev/null
        done
        say "✓ WiFi省电已恢复系统默认"
        nm_changed=1;;
      *) : ;;
    esac
}

# ---------- DNS ----------
apply_dns(){
    case "$1" in
      auto)
        rm -f "$DNS_NM_CONF"
        if [ -f "$RC_DIR/resolv.conf.orig" ] || [ -f "$RC_DIR/resolv.conf.target" ]; then
            rm -f "$RESOLV"
            tgt=$(cat "$RC_DIR/resolv.conf.target" 2>/dev/null || true)
            if [ -n "$tgt" ] && [ -e "$tgt" ]; then
                ln -s "$tgt" "$RESOLV"
            else
                ln -s ../run/resolvconf/resolv.conf "$RESOLV"
            fi
            say "✓ DNS已恢复为自动(跟随路由器下发)"
            nm_changed=1
        else
            say "· DNS本来就是自动模式,无需恢复"
        fi;;
      aliyun|114|tencent)
        case "$1" in
          aliyun)  p=223.5.5.5;     s=223.6.6.6;;
          114)     p=114.114.114.114; s=114.114.115.115;;
          tencent) p=119.29.29.29;  s=182.254.116.116;;
        esac
        mkdir -p "$RC_DIR" "$NM_CONF_DIR"
        if [ ! -f "$RC_DIR/resolv.conf.orig" ]; then
            if [ -L "$RESOLV" ]; then
                readlink -f "$RESOLV" > "$RC_DIR/resolv.conf.target"
            fi
            cp -aL "$RESOLV" "$RC_DIR/resolv.conf.orig" 2>/dev/null || true
        fi
        printf '[main]\ndns=none\n' > "$DNS_NM_CONF"
        rm -f "$RESOLV"
        printf 'nameserver %s\nnameserver %s\n' "$p" "$s" > "$RESOLV"
        chmod 644 "$RESOLV"
        say "✓ DNS已切换为静态直连: $p / $s"
        say "  (不再依赖 127.0.0.53,可规避 resolvconf/systemd-resolved 冲突导致的间歇断网)"
        nm_changed=1;;
      *) : ;;
    esac
}

# ---------- 断网自动守护 ----------
apply_watchdog(){
    case "$1" in
      on)
        mkdir -p /usr/local/sbin
        cat > "$WD_BIN" <<'WDEOF'
#!/bin/bash
# 断网自动守护: 每2分钟体检一次,断网自动重连,DNS坏了自动重启DNS服务
LOG=/var/log/aaafix-net-watchdog.log
ts(){ date '+%F %T'; }
ok=0
for ip in 223.5.5.5 119.29.29.29; do
    ping -c1 -W3 "$ip" >/dev/null 2>&1 && ok=1 && break
done
if [ "$ok" = 0 ]; then
    echo "$(ts) 检测到断网,尝试自动恢复..." >> "$LOG"
    nmcli networking off 2>/dev/null; sleep 2; nmcli networking on 2>/dev/null
    sleep 8
    ok=0
    for ip in 223.5.5.5 119.29.29.29; do
        ping -c1 -W3 "$ip" >/dev/null 2>&1 && ok=1 && break
    done
    if [ "$ok" = 0 ]; then
        echo "$(ts) 仍未恢复,重启NetworkManager..." >> "$LOG"
        systemctl restart NetworkManager
    fi
elif ! getent hosts www.baidu.com >/dev/null 2>&1; then
    echo "$(ts) 网络通但DNS解析失败,重启DNS服务..." >> "$LOG"
    systemctl restart systemd-resolved 2>/dev/null
fi
[ -f "$LOG" ] && tail -n 300 "$LOG" > "$LOG.t" 2>/dev/null && mv "$LOG.t" "$LOG"
WDEOF
        chmod 755 "$WD_BIN"
        cat > "$UNIT" <<'UNEOF'
[Unit]
Description=AAA网络自动守护(断网自动重连)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/aaafix-net-watchdog
UNEOF
        cat > "$TIMER" <<'TREOF'
[Unit]
Description=每2分钟运行一次网络体检

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
TREOF
        systemctl daemon-reload
        systemctl enable --now aaafix-net-watchdog.timer >/dev/null 2>&1
        say "✓ 断网自动守护已开启(每2分钟体检,日志: $LOG)";;
      off)
        systemctl disable --now aaafix-net-watchdog.timer >/dev/null 2>&1
        say "✓ 断网自动守护已关闭";;
      *) : ;;
    esac
}

# ---------- 一键修复 ----------
do_repair(){
    say "... 正在重启网络服务"
    systemctl restart NetworkManager
    say "✓ NetworkManager 已重启"
    sleep 5
    for d in $(nmcli -t -f DEVICE,TYPE,STATE device 2>/dev/null \
               | awk -F: '($2=="wifi"||$2=="ethernet") && $3!="不可用" && $3!="unavailable"' \
               | cut -d: -f1); do
        if nmcli device connect "$d" >/dev/null 2>&1; then
            say "✓ $d 已连接"
        else
            say "· $d 暂时连不上(没插网线/没WiFi信号属正常)"
        fi
    done
    resolvectl flush-caches 2>/dev/null
    sleep 2
    if ping -c1 -W3 223.5.5.5 >/dev/null 2>&1; then
        say "✓ 出网测试通过(IP连通)"
    else
        say "✗ 出网仍不通 → 检查路由器/网线/宽带"
    fi
    if getent hosts www.baidu.com >/dev/null 2>&1; then
        say "✓ DNS域名解析正常"
    else
        say "✗ DNS解析仍失败 → 建议在界面里选\"静态DNS直连\"再应用"
    fi
}

# ---------- 主入口 ----------
case "${1:-}" in
    apply)
        apply_powersave "${2:-keep}"
        apply_dns      "${3:-keep}"
        apply_watchdog "${4:-keep}"
        if [ "$nm_changed" = 1 ]; then
            say "... 正在重启 NetworkManager 使配置生效"
            systemctl restart NetworkManager
            say "✓ 网络配置已生效"
        fi
        ;;
    repair)
        do_repair
        ;;
    *)
        say "用法: $0 apply <powersave:off|on|keep> <dns:auto|aliyun|114|tencent|keep> <watchdog:on|off|keep>"
        say "      $0 repair"
        exit 1
        ;;
esac
