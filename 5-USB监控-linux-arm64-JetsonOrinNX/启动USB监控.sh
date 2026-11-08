#!/bin/bash
# 启动 USB 监控工具
cd "$(dirname "$0")" || exit 1
exec python3 usb_monitor.py "$@"
