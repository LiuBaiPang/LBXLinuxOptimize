#!/bin/bash
# 启动 串口监控工具
cd "$(dirname "$0")" || exit 1
exec python3 serial_monitor.py "$@"
