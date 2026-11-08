#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usbmon 深度监控助手 —— 由 usb_monitor.py 通过 `sudo -S python3 usbmon_helper.py`
以 root 运行,解析 /sys/kernel/debug/usb/usbmonNu 文本流,统计每个 USB 设备
累计收发字节数,每秒向 stdout 输出一行 JSON:
  {"type":"usbmon","t":1234.5,"stats":[{"bus":"1","dev":"2","rx":12345,"tx":678}]}
出错输出 {"type":"error","msg":"..."} 后退出。密码由 stdin 提供,本脚本不读。
"""
import glob
import json
import os
import re
import select
import sys
import time

DEBUG_USB = "/sys/kernel/debug/usb"

# usbmon 文本格式(1T):
#   <URB tag> <时间戳us> <S|C|E> <类型+方向>:<bus>:<devnum>:<epnum> [状态 长度 ...]
# 例: ffffa0... 3848299487 C Bi:1:002:0 0 13 = 0501...
LINE_RE = re.compile(
    r"^\S+\s+\d+\s+[SCE]\s+([A-Z])([io]):(\d+):(\d+):(\d+)\s+(.*)$")


def open_files():
    """打开所有 bus 的 usbmon{N}u 文件 -> {fd: (bus, buffer)}"""
    files = {}
    for path in sorted(glob.glob(os.path.join(DEBUG_USB, "usbmon*u"))):
        m = re.match(r"usbmon(\d+)u$", os.path.basename(path))
        if not m:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            files[fd] = [m.group(1), b""]
        except Exception as e:
            sys.stdout.write(json.dumps(
                {"type": "error", "msg": "打开 %s 失败: %s" % (path, e)}) + "\n")
    return files


def parse_count(line, counters):
    m = LINE_RE.match(line)
    if not m:
        return
    cls, dirn, bus, dev, _ep, rest = m.groups()
    # 只统计完成事件(C),提交(S)不重复计
    if not line.split()[2] == "C":
        return
    toks = rest.split()
    length = None
    if cls == "Z":                       # 等时传输: "0:-1:1:4096" 后跟长度
        for t in toks:
            if ":" in t:
                continue
            if re.match(r"^-?\d+$", t):
                length = int(t)
                break
    else:                                # 控制/批量/中断: 状态 长度
        seen_status = False
        for t in toks:
            if ":" in t or t.startswith("="):
                continue
            if not re.match(r"^-?\d+$", t):
                continue
            if not seen_status:
                seen_status = True       # 第一个纯数字是状态
                continue
            length = int(t)
            break
    if length is None or length <= 0:
        return
    key = (bus, dev)
    c = counters.setdefault(key, [0, 0])
    if dirn == "i":
        c[0] += length
    else:
        c[1] += length


def main():
    if os.geteuid() != 0:
        sys.stdout.write(json.dumps(
            {"type": "error", "msg": "本助手需以 root 运行"}) + "\n")
        return 1
    files = open_files()
    if not files:
        os.system("modprobe usbmon 2>/dev/null")
        time.sleep(0.4)
        files = open_files()
    if not files:
        sys.stdout.write(json.dumps(
            {"type": "error", "msg": "usbmon 不可用(模块未加载或 debugfs 无权限)——"
             "先在主面板点\"启用usbmon模块\""}) + "\n")
        return 1

    counters = {}
    last_emit = time.time()
    while True:
        fds = list(files.keys())
        try:
            r, _, _ = select.select(fds, [], [], 0.2)
        except (OSError, InterruptedError):
            r = []
        for fd in r:
            bus, buf = files[fd]
            try:
                data = os.read(fd, 262144)
            except (BlockingIOError, OSError):
                continue
            if not data:
                continue
            buf += data
            lines = buf.split(b"\n")
            buf = lines.pop()          # 最后一段可能是半行,留下次拼
            files[fd][1] = buf
            for ln in lines:
                try:
                    parse_count(ln.decode("ascii", "ignore"), counters)
                except Exception:
                    pass
        now = time.time()
        if now - last_emit >= 1.0:
            last_emit = now
            stats = [{"bus": b, "dev": d, "rx": v[0], "tx": v[1]}
                     for (b, d), v in sorted(counters.items())]
            sys.stdout.write(json.dumps(
                {"type": "usbmon", "t": now, "stats": stats}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
