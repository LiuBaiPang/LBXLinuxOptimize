#!/usr/bin/env bash
# ============================================================
#  卸载 Nautilus 右键"复制完整路径"
#  AAAFIXsystem / 4-右键复制路径-linux-arm64-JetsonOrinNX
#  同时移除一级菜单扩展与"脚本"子菜单脚本(哪个存在删哪个)
# ============================================================
set -u

# ---------- 自愈图形会话环境(终端/SSH里运行时) ----------
if [ -z "${DISPLAY:-}" ] || [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
  PID="$(pgrep -u "$USER" -x gnome-session 2>/dev/null | head -n1)"
  [ -z "$PID" ] && PID="$(pgrep -u "$USER" -x gnome-shell 2>/dev/null | head -n1)"
  if [ -n "$PID" ] && [ -r "/proc/$PID/environ" ]; then
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in DISPLAY|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY) export "$k=$v" ;; esac
    done < <(tr '\0' '\n' < "/proc/$PID/environ")
  fi
fi

rm -fv "$HOME/.local/share/nautilus-python/extensions/复制路径扩展.py"
rm -fv "$HOME/.local/share/nautilus/scripts/复制完整路径"

nautilus -q 2>/dev/null || true
echo "✔ 已卸载: 重新打开 文件(Nautilus) 即生效"
