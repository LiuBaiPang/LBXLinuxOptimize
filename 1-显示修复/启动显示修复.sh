#!/bin/bash
# 双击即可启动 显示修复工具
cd "$(dirname "$0")"
exec python3 display_fix.py "$@"
