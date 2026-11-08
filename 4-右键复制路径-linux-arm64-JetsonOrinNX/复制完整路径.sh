#!/usr/bin/env bash
# ============================================================
#  Nautilus 右键脚本(降级方案): 复制选中文件完整路径
#  AAAFIXsystem / 4-右键复制路径-linux-arm64-JetsonOrinNX
#
#  菜单位置: 右键 → 脚本 → 复制完整路径
#  部署位置: ~/.local/share/nautilus/scripts/复制完整路径
#  (不带 .sh 后缀, 这样菜单里显示干净的名字)
#  依赖: 系统自带 python3-gi, 无需安装 xclip
# ============================================================
set -u

# Nautilus 约定: 选中项路径通过环境变量传入(每行一个)
export AAAFIX_PATHS="${NAUTILUS_SCRIPT_SELECTED_FILE_PATHS:-}"
export AAAFIX_URIS="${NAUTILUS_SCRIPT_SELECTED_URIS:-}"

# 注意: heredoc 后面不能再跟 "|| { ... }" —— bash 会把花括号组吞进 heredoc,
# 报"未预期的文件结尾"。所以这里先跑完再用退出码判断。
python3 - <<'PY' 2>/dev/null
import os, sys, time, gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

text = os.environ.get('AAAFIX_PATHS', '').rstrip('\n')
if not text:
    # 本地路径变量为空(回收站/网络位置等)时退回 URI 解码
    from urllib.parse import urlparse, unquote
    uris = [u for u in os.environ.get('AAAFIX_URIS', '').splitlines() if u]
    text = '\n'.join(unquote(urlparse(u).path) for u in uris)
if not text:
    sys.exit(2)

cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
cb.set_text(text, -1)
cb.store()

set_time = time.monotonic()
loop = GLib.MainLoop()

def on_owner_change(_cb, _ev):
    # 剪贴板被新内容覆盖(或交给剪贴板管理器)后自动退出;
    # 设置后 1 秒内的事件是自己的, 忽略
    if time.monotonic() - set_time > 1.0:
        loop.quit()

cb.connect('owner-change', on_owner_change)
GLib.timeout_add_seconds(1800, loop.quit)   # 最多驻留30分钟

# 父进程立即退出(菜单不用等待); 子进程驻留为剪贴板供图,
# 本机无剪贴板管理器, 进程退出剪贴板内容就没了
if os.fork() > 0:
    sys.exit(0)
os.setsid()
loop.run()
PY
RC=$?

# 2 = 没有选中项(静默); 其它非0 = python3-gi 缺失等, 弹错误通知
if [ "$RC" -ne 0 ] && [ "$RC" -ne 2 ]; then
  notify-send -u critical -i dialog-error "复制路径失败" \
    "剪贴板复制失败(缺 python3-gi?)" 2>/dev/null
fi

exit 0
