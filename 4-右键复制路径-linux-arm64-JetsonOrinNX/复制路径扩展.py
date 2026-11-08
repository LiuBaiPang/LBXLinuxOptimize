# -*- coding: utf-8 -*-
# ============================================================
#  Nautilus 右键扩展: 复制完整路径
#  AAAFIXsystem / 4-右键复制路径-linux-arm64-JetsonOrinNX
#
#  部署位置: ~/.local/share/nautilus-python/extensions/复制路径扩展.py
#  依赖: python3-nautilus + gir1.2-nautilus-3.0 (安装.sh 会装)
#  效果: Nautilus 右键菜单一级菜单出现"复制完整路径"
#        右键空白处出现"复制当前文件夹完整路径"
#  剪贴板用系统自带 GTK, 无需 xclip
# ============================================================
import gi

gi.require_version('Nautilus', '3.0')
gi.require_version('Gtk', '3.0')
from gi.repository import Nautilus, GObject, Gtk, Gdk


def _paths_of(files):
    """取选中项的完整路径; 非本地文件(回收站等)退回 URI"""
    paths = []
    for f in files:
        p = None
        try:
            p = f.get_location().get_path()
        except Exception:
            p = None
        if not p:
            try:
                p = f.get_uri()
            except Exception:
                p = ''
        paths.append(p)
    return paths


class CopyPathExtension(GObject.GObject, Nautilus.MenuProvider):

    # Nautilus 3.36(本机)的签名是 (window, files); 43+ 去掉了 window。
    # 用 *args 只取最后一个参数, 新旧版本都兼容。
    def get_file_items(self, *args):
        files = args[-1]
        if not files:
            return []
        n = len(files)
        if n == 1:
            label, tip = '复制完整路径', '复制该文件/文件夹的完整路径到剪贴板'
        else:
            label, tip = '复制 %d 个完整路径' % n, '复制所有选中项的完整路径(每行一个)到剪贴板'
        item = Nautilus.MenuItem(
            name='CopyPathExtension::CopyPath',
            label=label,
            tip=tip,
        )
        item.connect('activate', self._on_activate, files)
        return [item]

    def get_background_items(self, *args):
        folder = args[-1]
        if folder is None:
            return []
        item = Nautilus.MenuItem(
            name='CopyPathExtension::CopyFolderPath',
            label='复制当前文件夹完整路径',
            tip='复制当前打开文件夹的完整路径到剪贴板',
        )
        item.connect('activate', self._on_activate_bg, folder)
        return [item]

    def _copy_text(self, text):
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(text, -1)
        cb.store()

    def _on_activate(self, _menu, files):
        self._copy_text('\n'.join(_paths_of(files)))

    def _on_activate_bg(self, _menu, folder):
        self._copy_text('\n'.join(_paths_of([folder])))
