#!/usr/bin/env bash
# ============================================================
#  安装 Nautilus 右键"复制完整路径"
#  AAAFIXsystem / 4-右键复制路径-linux-arm64-JetsonOrinNX
#  可重复执行(幂等)。卸载请用同目录的 卸载.sh
#
#  效果分两档:
#   A) 一级菜单(推荐): 右键菜单直接出现"复制完整路径"
#      需系统包 python3-nautilus + gir1.2-nautilus-3.0
#      (在终端里运行本脚本, sudo 会提示输密码)
#   B) 免装包降级: 右键 → 脚本 → 复制完整路径
#      (无法用 sudo 装包时自动降级; 之后装好包再跑本脚本即自动升级)
# ============================================================
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_SRC="$DIR/复制路径扩展.py"
FB_SRC="$DIR/复制完整路径.sh"
EXT_DST="$HOME/.local/share/nautilus-python/extensions/复制路径扩展.py"
FB_DST="$HOME/.local/share/nautilus/scripts/复制完整路径"

[ -f "$EXT_SRC" ] || { echo "找不到 $EXT_SRC" >&2; exit 1; }
[ -f "$FB_SRC" ]  || { echo "找不到 $FB_SRC" >&2;  exit 1; }

# ---------- 自愈图形会话环境(终端/SSH里运行时补齐 DISPLAY/DBUS) ----------
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

pkg_ok() { dpkg -s "$1" >/dev/null 2>&1; }

deploy_extension() {
  python3 - "$EXT_SRC" <<'PY'   # 先语法自检
import py_compile, sys
py_compile.compile(sys.argv[1], cfile='/tmp/aaafix_copy_path_check.pyc', doraise=True)
PY
  mkdir -p "$(dirname "$EXT_DST")"
  cp -f "$EXT_SRC" "$EXT_DST"
  chmod 644 "$EXT_DST"
  rm -f "$FB_DST"        # 一级菜单已可用, 移除"脚本"子菜单里的重复项
  echo "✔ 已部署扩展(一级菜单): $EXT_DST"
}

deploy_fallback() {
  mkdir -p "$(dirname "$FB_DST")"
  cp -f "$FB_SRC" "$FB_DST"
  chmod 755 "$FB_DST"
  echo "✔ 已部署右键脚本(脚本子菜单): $FB_DST"
}

# ---------- 决定装哪一档 ----------
EXT_MODE=0
if pkg_ok python3-nautilus && pkg_ok gir1.2-nautilus-3.0; then
  EXT_MODE=1                                  # 系统包已就绪
elif [ -t 0 ] || sudo -n true 2>/dev/null; then
  # 在自己终端里跑(能输密码)或已免密 → 安装系统包
  echo "正在安装系统包 python3-nautilus gir1.2-nautilus-3.0 ..."
  if sudo apt-get install -y python3-nautilus gir1.2-nautilus-3.0; then
    EXT_MODE=1
  else
    echo "⚠ apt 安装失败(可能断网), 先用降级方案" >&2
  fi
else
  echo "ℹ 当前环境无法输 sudo 密码(从工具/SSH里调用?), 先用降级方案"
fi

if [ "$EXT_MODE" = 1 ]; then
  deploy_extension
else
  deploy_fallback
  echo
  echo "  想升级为右键一级菜单(推荐), 在终端里执行:"
  echo "    sudo apt-get install -y python3-nautilus gir1.2-nautilus-3.0"
  echo "  然后重新运行本脚本即可自动升级:  \"$DIR/安装.sh\""
fi

# ---------- 重启 Nautilus 使其生效 ----------
nautilus -q 2>/dev/null || true
echo "✔ 完成: 重新打开 文件(Nautilus) 即生效"
