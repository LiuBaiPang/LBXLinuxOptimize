#!/bin/sh
cd -- "$(dirname -- "$0")" || exit 1
if ! /usr/bin/python3 -c 'import tkinter' 2>/dev/null; then
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --text='缺少 Python Tk。请在终端安装：sudo apt install python3-tk'
  else
    echo '缺少 Python Tk。请运行：sudo apt install python3-tk'
  fi
  exit 1
fi
exec /usr/bin/python3 app.py "$@"
