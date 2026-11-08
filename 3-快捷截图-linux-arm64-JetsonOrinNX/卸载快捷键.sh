#!/usr/bin/env bash
# ============================================================
#  卸载本工具注册的 GNOME 自定义快捷键 (Shift+Alt+S)
# ============================================================
set -euo pipefail
BASE="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
SCHEMA=org.gnome.settings-daemon.plugins.media-keys

# ---------- 确保使用图形会话的 dbus ----------
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
  PID="$(pgrep -u "$USER" -x gnome-session 2>/dev/null | head -n1)"
  [ -z "$PID" ] && PID="$(pgrep -u "$USER" -x gnome-shell 2>/dev/null | head -n1)"
  if [ -n "$PID" ] && [ -r "/proc/$PID/environ" ]; then
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in DISPLAY|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY) export "$k=$v" ;; esac
    done < <(tr '\0' '\n' < "/proc/$PID/environ")
  fi
fi

CUR="$(gsettings get $SCHEMA custom-keybindings 2>/dev/null || echo '@as []')"
PATHS="$(echo "$CUR" | sed -e 's/^@as \{0,1\}//' -e 's/^\[//' -e 's/\]$//' -e "s/'//g" \
        | tr ',' '\n' | sed 's/^ \{1,\}//; s/ \{1,\}$//' | grep -v '^$' || true)"

REMOVED=""
KEEP=""
for p in $PATHS; do
  cmd="$(gsettings get $SCHEMA.custom-keybinding:$p command 2>/dev/null || true)"
  case "$cmd" in
    *"3-快捷截图"*) REMOVED="$p"; continue ;;
    *) KEEP="$KEEP'$p', " ;;
  esac
done

if [ -z "$REMOVED" ]; then
  echo "未找到本工具注册的快捷键, 无需卸载"
  exit 0
fi

gsettings set $SCHEMA custom-keybindings "[${KEEP%, }]"
gsettings reset $SCHEMA.custom-keybinding:$REMOVED name
gsettings reset $SCHEMA.custom-keybinding:$REMOVED command
gsettings reset $SCHEMA.custom-keybinding:$REMOVED binding

echo "✔ 已卸载快捷键 ($REMOVED), Shift+Alt+S 恢复空闲"
