#!/usr/bin/python3
"""Installs only the current user's application launcher, without root."""
from pathlib import Path
import os

folder=Path(__file__).resolve().parent
dest=Path.home()/'.local/share/applications/linux-repair.desktop'
dest.parent.mkdir(parents=True,exist_ok=True)
path=str(folder/'start.sh')
# Desktop Entry Exec uses its own escaping rules, not shell quoting.
safe=path.replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$').replace('%','%%')
dest.write_text('[Desktop Entry]\nType=Application\nName=Linux 修复中心\nComment=Jetson 显示诊断与受保护配置测试\nExec=/bin/sh "'+safe+'"\nIcon=preferences-system\nTerminal=false\nCategories=System;Settings;\n',encoding='utf-8')
os.chmod(str(folder/'start.sh'),0o755)
print('启动器已创建：'+str(dest)+'\n可从应用菜单打开“Linux 修复中心”。移动程序目录后请重新运行本脚本。')
