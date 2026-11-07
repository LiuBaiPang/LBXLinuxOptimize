#!/usr/bin/env bash
# ============================================================
#  安装 GNOME 自定义快捷键: Shift+Alt+S → 截图并复制
#  可重复执行(已注册过则原地更新, 不会产生重复条目)
#  卸载请用同目录的 卸载快捷键.sh
# ============================================================
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/截图复制.sh"
NAME="快捷截图(复制到剪贴板)"
BINDING="<Alt><Shift>s"
BASE="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
SCHEMA=org.gnome.settings-daemon.plugins.media-keys

[ -f "$SCRIPT" ] || { echo "找不到 $SCRIPT" >&2; exit 1; }
chmod +x "$SCRIPT"

# ---------- 确保使用图形会话的 dbus(终端/SSH里运行时) ----------
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

# ---------- 已注册路径列表 ----------
PATHS="$(echo "$CUR" | sed -e 's/^@as \{0,1\}//' -e 's/^\[//' -e 's/\]$//' -e "s/'//g" \
        | tr ',' '\n' | sed 's/^ \{1,\}//; s/ \{1,\}$//' | grep -v '^$' || true)"

# ---------- 冲突检查(Shift+Alt+S 是否已被其它功能占用) ----------
CONFLICT="$(gsettings list-recursively 2>/dev/null \
  | grep -iE "'<alt><shift>s'|'<shift><alt>s'" || true)"
if [ -n "$CONFLICT" ]; then
  echo "⚠ 提示: 该组合键似乎已被占用:"
  echo "$CONFLICT"
  echo "  (仍将继续安装, 如冲突可在 设置→键盘 中调整)"
fi

# ---------- 本工具已注册过 → 复用原条目 ----------
TARGET=""
for p in $PATHS; do
  cmd="$(gsettings get $SCHEMA.custom-keybinding:$p command 2>/dev/null || true)"
  case "$cmd" in *"3-快捷截图"*) TARGET="$p"; break ;; esac
done

if [ -z "$TARGET" ]; then
  USED="$(echo "$PATHS" | grep -oE 'custom[0-9]+' | grep -oE '[0-9]+$' || true)"
  N=0
  while echo "$USED" | grep -qx "$N"; do N=$((N+1)); done
  TARGET="${BASE}custom${N}/"
fi

# ---------- 写入条目 ----------
# 注意: gsettings 的值按 GVariant 解析, 含空格/括号/< > 时必须整体用 '...' 包成字符串
gsettings set $SCHEMA.custom-keybinding:$TARGET name    "'$NAME'"
gsettings set $SCHEMA.custom-keybinding:$TARGET command "'\"$SCRIPT\" area'"
gsettings set $SCHEMA.custom-keybinding:$TARGET binding "'$BINDING'"

# ---------- 把条目加入总列表 ----------
NEW_LIST="$(python3 - "$CUR" "$TARGET" <<'PY'
import sys, re
cur, target = sys.argv[1], sys.argv[2]
m = re.search(r'\[(.*)\]', cur)
items = re.findall(r"'([^']*)'", m.group(1)) if m else []
if target not in items:
    items.append(target)
print('[' + ', '.join("'%s'" % i for i in items) + ']')
PY
)"
gsettings set $SCHEMA custom-keybindings "$NEW_LIST"

echo "✔ 快捷键安装完成 (立即生效, 无需重启)"
echo "    快捷键: Shift + Alt + S"
echo "    名称  : $NAME"
echo "    命令  : \"$SCRIPT\" area"
echo "  现在就可以按 Shift+Alt+S 框选屏幕区域 → 自动复制到剪贴板并保存"
