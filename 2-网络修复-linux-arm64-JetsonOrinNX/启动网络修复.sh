#!/bin/bash
# 双击即可启动 网络修复工具
cd "$(dirname "$0")"
exec python3 network_fix.py "$@"
