#!/usr/bin/env bash
# ============================================================
#  截图并复制  ——  AAAFIXsystem / 3-快捷截图
#  用法: ./截图复制.sh [area|full|window]   (默认 area = 鼠标框选)
#    area   : 框选屏幕区域(默认)
#    full   : 整个屏幕
#    window : 当前活动窗口
#  功能: 截图后自动复制到剪贴板, 并保存到 ~/Pictures/截图/
#  剪贴板用系统自带 GTK 实现, 无需安装 xclip
# ============================================================
set -u
MODE="${1:-area}"

# ---------- 自愈图形会话环境(在终端/SSH里直接运行时补齐 DISPLAY/DBUS) ----------
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

notify_err() { notify-send -u critical -i dialog-error "截图失败" "$1" 2>/dev/null; echo "错误: $1" >&2; }

# ---------- 保存目录 ----------
PIC_DIR="$(xdg-user-dir PICTURES 2>/dev/null || echo "$HOME/Pictures")"
SAVE_DIR="$PIC_DIR/截图"
mkdir -p "$SAVE_DIR" 2>/dev/null || { notify_err "无法创建保存目录 $SAVE_DIR"; exit 1; }
FILE="$SAVE_DIR/截图_$(date +%Y%m%d_%H%M%S).png"

# ---------- 抓图 ----------
case "$MODE" in
  area)   gnome-screenshot -a -f "$FILE" 2>/dev/null ;;
  window) gnome-screenshot -w -f "$FILE" 2>/dev/null ;;
  full)   gnome-screenshot    -f "$FILE" 2>/dev/null ;;
  *)  echo "用法: $0 [area|full|window]" >&2; exit 1 ;;
esac
if [ ! -s "$FILE" ]; then
  # 框选时按 Esc 取消属正常情况, 静默退出
  [ "$MODE" = "area" ] && exit 0
  notify_err "gnome-screenshot 未生成截图文件"; exit 1
fi

# ---------- 复制到剪贴板 ----------
CLIP_MSG="已复制到剪贴板
已保存: $FILE"

python3 - "$FILE" "$CLIP_MSG" <<'PY' 2>/dev/null
import sys, os, time, gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

path, msg = sys.argv[1], sys.argv[2]
try:
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
except Exception:
    sys.exit(2)

cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
cb.set_image(pb)
cb.store()

try:
    import subprocess
    subprocess.Popen(['notify-send', '-i', 'camera-photo', '-t', '3000',
                      '截图完成', msg])
except Exception:
    pass

set_time = time.monotonic()
loop = GLib.MainLoop()

def on_owner_change(_cb, _ev):
    # 剪贴板被新内容覆盖(或交给剪贴板管理器)后自动退出;
    # 设置后 1 秒内的事件是自己的, 忽略
    if time.monotonic() - set_time > 1.0:
        loop.quit()

cb.connect('owner-change', on_owner_change)
GLib.timeout_add_seconds(1800, loop.quit)   # 最多驻留30分钟

print('CLIP_OK', flush=True)
sys.stdout.flush()
# 父进程立即退出(终端/快捷键不用等待); 子进程驻留为剪贴板供图,
# 剪贴板被新内容覆盖或超时后自动退出
if os.fork() > 0:
    sys.exit(0)
os.setsid()
loop.run()
PY

RC=$?
case "$RC" in
  0) echo "完成: $FILE" ;;
  2) notify_err "读取截图文件失败"; exit 2 ;;
  *) notify_err "剪贴板复制失败(缺 python3-gi), 可安装 xclip 后重试"; exit 3 ;;
esac
