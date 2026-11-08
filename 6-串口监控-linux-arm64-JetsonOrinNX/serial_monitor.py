#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
串口监控工具 —— Jetson Orin NX 串口实时监控 / 串口终端 / 飞控 MAVLink / 资料手册。

背景: 本机有多个串口(40-pin 排针 UART、板载 Tegra UART、调试口、USB 虚拟串口),
     用户要与 PX4/ArduPilot 飞控连接但缺少趁手的工具。本工具四个标签页:
  - 串口总览: 扫描 /dev 全部串口设备, 展示类型/硬件来源/权限/占用状态/硬件活动
              (IRQ 计数增量, 不打开设备即可知道"有没有数据在流动"),
              可对空闲口开"实时预览"(HEX+ASCII)。
  - 串口终端: 完整的波特率/数据位/校验/停止位/流控配置, 收发(文本/HEX)、
              定时发送、保存日志 —— 用来与任何串口设备对话。
  - 飞控 MAVLink: 纯自研解析(mavlink_min.py, 零依赖)实时显示心跳/模式/解锁状态/
              姿态/高度/电量/GPS/HUD, 可发指令: 切模式、解锁/上锁(双重确认)、
              请求数据流、Ping、读参数、原始 COMMAND_LONG。
  - 资料: 40-pin 串口引脚速查 + 一键释放 40-pin UART(它默认被系统控制台占用)
          + 打开本地《资料手册.html》(针脚定义/用法/原文链接/示例)。

权限: 只读功能全部免 root(用户在 dialout 组即可读写 ttyTHS*/ttyACM*);
     释放 40-pin UART 走 sudo -S 调用同目录 serial_actions.sh(密码仅内存不落盘)。

冒烟测试: AAAFIX_SMOKE=1 采集一轮快照打 JSON 退出(与 5 号工具约定一致)。
"""
import glob
import json
import os
import re
import math
import array
import subprocess
import sys
import threading
import time
import queue
import errno
import select
import fcntl
import termios
from collections import deque
import tkinter as tk
import webbrowser
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog

HERE = os.path.dirname(os.path.abspath(__file__))
ACTION = os.path.join(HERE, "serial_actions.sh")
MANUAL = os.path.join(HERE, "资料手册.html")
sys.path.insert(0, HERE)
import mavlink_min as mav          # noqa: E402  同目录自研模块

if not os.environ.get("DISPLAY"):
    os.environ["DISPLAY"] = ":0"
os.environ["PATH"] = ("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                      "/sbin:/bin:" + os.environ.get("PATH", ""))

SMOKE = os.environ.get("AAAFIX_SMOKE")   # 自动化冒烟测试: 采一轮快照打JSON退出
REFRESH_S = 1.0

# ---------------- 配色(深色,与 1/2/5 号工具一致) ----------------
C_BG     = "#14181f"
C_CARD   = "#1c2330"
C_CARD2  = "#242d3c"
C_LINE   = "#2b3547"
C_FG     = "#e9eef6"
C_SUB    = "#93a1b5"
C_ACC    = "#3d8bfd"
C_OK     = "#2ecc71"
C_BAD    = "#e05561"
C_WARN   = "#f0a020"
C_INPUT  = "#10151d"

FONT    = ("Noto Sans CJK SC", 11)
FONT_B  = ("Noto Sans CJK SC", 11, "bold")
FONT_H  = ("Noto Sans CJK SC", 17, "bold")
FONT_HH = ("Noto Sans CJK SC", 22, "bold")
FONT_S  = ("Noto Sans CJK SC", 10)
FONT_M  = ("Noto Sans Mono CJK SC", 10)

# ---------------- 串口设备识别 ----------------
PORT_PATTERNS = ["/dev/ttyTHS[0-9]*", "/dev/ttyAMA[0-9]*", "/dev/ttyTCU[0-9]*",
                 "/dev/ttyS[0-9]", "/dev/ttyACM[0-9]*", "/dev/ttyUSB[0-9]*",
                 "/dev/ttyGS[0-9]*"]

PORT_KIND = {   # 设备名前缀 -> 人类可读类型
    "ttyTHS": "板载高速UART(Tegra)",
    "ttyAMA": "PL011 UART(40-pin排针)",
    "ttyTCU": "调试串口(控制台)",
    "ttyS":   "8250遗留探测口",
    "ttyACM": "USB虚拟串口(CDC-ACM)",
    "ttyUSB": "USB转串口(FT232等)",
    "ttyGS":  "USB gadget虚拟串口",
}


def port_kind(name):
    for k in sorted(PORT_KIND, key=len, reverse=True):
        if name.startswith(k):
            return PORT_KIND[k]
    return "其他串口"


def _read1(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def scan_ports():
    """只读扫描全部串口设备(不开口, 不影响任何正在使用串口的程序)。

    返回 dict 列表: dev name kind hw perms group busy_users irq console_getty
    """
    import grp
    devs = set()
    for pat in PORT_PATTERNS:
        for d in glob.glob(pat):
            devs.add(d)
    out = []
    me = os.getpid()
    for dev in sorted(devs):
        name = os.path.basename(dev)
        try:
            st = os.stat(dev)
            mode = "%04o" % (st.st_mode & 0o7777)
            try:
                g = grp.getgrgid(st.st_gid).gr_name
            except KeyError:
                g = str(st.st_gid)
            can_r = os.access(dev, os.R_OK)
            can_w = os.access(dev, os.W_OK)
            perms = ("可读写" if can_r and can_w else
                     "只读" if can_r else "只写" if can_w else "无权限")
        except OSError:
            mode, g, perms = "----", "-", "无权限"
        # 硬件来源: /sys/class/tty/<name>/device 的 realpath
        devlink = os.path.realpath("/sys/class/tty/%s/device" % name)
        hw = os.path.basename(devlink) if "/devices/" in devlink else ""
        if not hw and name.startswith("ttyS"):
            hw = "(无真实硬件)"
        driver = ""
        try:
            driver = os.path.basename(os.readlink(
                "/sys/class/tty/%s/device/driver" % name))
        except OSError:
            pass
        # 占用检测: 扫 /proc/*/fd (只能看到自己有权限看的进程)
        users = []
        for pid_dir in glob.glob("/proc/[0-9]*"):
            pid = pid_dir.rsplit("/", 1)[1]
            if pid == str(me):
                continue
            try:
                fds = os.listdir(pid_dir + "/fd")
            except OSError:
                continue
            hit = False
            for f in fds:
                try:
                    if os.readlink(pid_dir + "/fd/" + f) == dev:
                        hit = True
                        break
                except OSError:
                    pass
            if hit:
                cmd = _read1(pid_dir + "/cmdline").replace("\x00", " ").strip()
                users.append("pid%s:%s" % (pid, (cmd or "?")[:24]))
        out.append(dict(dev=dev, name=name, kind=port_kind(name), hw=hw or "-",
                        driver=driver, group=g, mode=mode, perms=perms,
                        busy_users=users, irq=None))
    return out


# 设备名/驱动名 -> /proc/interrupts 中断行末 token 的别名
# (实测本内核: ttyAMA0 的中断行叫 uart-pl011; serial-tegra 的 ttyTHS* 走 DMA,
#  中断不单列, 这些口看不到活动指示 —— 表格里显示"—", 想看流量用预览。)
PORT_IRQ_ALIAS = {"ttyAMA0": "uart-pl011"}


def read_irq_counts():
    """读 /proc/interrupts, 返回 {中断行末token: 各CPU计数之和}。"""
    res = {}
    try:
        with open("/proc/interrupts") as f:
            for ln in f:
                m = re.match(r"\s*(\d+):\s+(.*)", ln)
                if not m:
                    continue
                rest = m.group(2).split()
                if not rest:
                    continue
                total = 0
                for tok in rest[:-1]:       # 倒数第1个是设备名, 前面数字是各CPU计数
                    if tok.isdigit():
                        total += int(tok)
                res[rest[-1]] = res.get(rest[-1], 0) + total
    except OSError:
        pass
    return res


def getty_units():
    """当前 active 的 serial-getty 单元(占用串口做登录终端的)。"""
    try:
        p = subprocess.run(["systemctl", "list-units", "serial-getty@*",
                            "--no-legend", "--no-pager"],
                           capture_output=True, text=True, timeout=4)
        return [ln.split()[0] for ln in (p.stdout or "").splitlines()
                if ln.strip() and " active " in ln]
    except Exception:
        return []


def console_active():
    """内核 printk 当前会打到的串口控制台(这些口的数据流会被日志污染)。"""
    return _read1("/sys/class/tty/console/active")


def collect_snapshot():
    """总览页一轮快照(冒烟测试也用这个)。"""
    ports = scan_ports()
    irqs = read_irq_counts()
    for p in ports:
        # 多候选匹配中断行: 设备树节点名 / 驱动名 / 别名表
        for cand in (p["hw"], p["driver"], PORT_IRQ_ALIAS.get(p["name"])):
            if cand and cand in irqs:
                p["irq"] = irqs[cand]
                break
    return dict(ts=time.time(), ports=ports, getty=getty_units(),
                console=console_active())


# ---------------- 串口打开(termios 原生) ----------------

def _baud_list():
    out = []
    for b in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400,
              460800, 500000, 576000, 921600, 1000000, 1152000, 1500000,
              2000000, 2500000, 3000000, 4000000):
        if hasattr(termios, "B%d" % b):
            out.append(b)
    return out


BAUDS = _baud_list()


def open_serial(dev, baud, bytesize=8, parity="N", stopbits=1,
                xonxoff=False, rtscts=False, flush=False):
    """按参数打开并配置串口(原生 termios, 非阻塞)。返回 fd。
    打开时拉起 DTR/RTS(与 pyserial 行为一致)——某些 USB 设备(飞控等)
    会等主机这两个信号才开始发数据。"""
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        a = termios.tcgetattr(fd)
        # raw 模式: 关输入转换/回显/行缓冲/信号
        a[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                  termios.ISTRIP | termios.INLCR | termios.IGNCR |
                  termios.ICRNL | termios.IXON | termios.IXOFF)
        a[1] &= ~termios.OPOST
        a[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                  termios.ISIG | termios.IEXTEN)
        c = a[2] & ~termios.CSIZE
        c |= termios.CS8 if bytesize == 8 else termios.CS7
        c |= termios.CREAD | termios.CLOCAL
        if parity == "N":
            c &= ~termios.PARENB
        else:
            c |= termios.PARENB
            if parity == "O":
                c |= termios.PARODD
            else:
                c &= ~termios.PARODD
        if stopbits == 2:
            c |= termios.CSTOPB
        else:
            c &= ~termios.CSTOPB
        if rtscts:
            c |= termios.CRTSCTS
        else:
            c &= ~termios.CRTSCTS
        a[2] = c
        if xonxoff:
            a[0] |= termios.IXON | termios.IXOFF
        sp = getattr(termios, "B%d" % baud, termios.B115200)
        a[4] = a[5] = sp
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, a)
        if flush:
            termios.tcflush(fd, termios.TCIOFLUSH)
        # 拉起 DTR/RTS(个别设备/虚拟口不支持, 失败忽略)
        try:
            fcntl.ioctl(fd, 0x5418, array.array('i', [0x0002 | 0x0004]), True)
        except OSError:
            pass
    except Exception:
        os.close(fd)
        raise
    return fd


def open_error_msg(e):
    """把打开串口常见异常翻成中文提示。"""
    if isinstance(e, PermissionError) or e.errno == errno.EACCES:
        return ("权限不足: 当前用户无法读写该设备。\n"
                "解决办法: ① 用 ttyTHS*/ttyACM* 等免root的口; "
                "② 40-pin 的 ttyAMA0 用本工具\"释放40-pin串口\"; ③ 或临时 sudo 运行。")
    if e.errno == errno.ENOENT:
        return "设备不存在(可能刚被拔出?)"
    if e.errno == errno.EBUSY:
        return "设备忙(被其他程序独占?)"
    return "%s" % e


def to_hex(data):
    return " ".join("%02X" % b for b in data)


def from_hex(s):
    t = re.sub(r"0x|0X|[\s:,;\-]", "", s or "")
    if len(t) % 2:
        raise ValueError("HEX 长度应为偶数")
    return bytes.fromhex(t)


def printable(data):
    """bytes -> 可读 ASCII(不可见字符显示为 ·, 保留换行/制表)。"""
    out = []
    for b in data:
        out.append(chr(b) if (32 <= b < 127 or b in (9, 10, 13)) else "·")
    return "".join(out)


# ================= 预览窗口 =================

class PreviewWindow(tk.Toplevel):
    """单口实时预览: O_RDONLY|O_NONBLOCK 打开, 读到的数据被消费(有警告)。"""

    def __init__(self, app, dev):
        super().__init__(app)
        self.app = app
        self.dev = dev
        self.fd = None
        self.rx = 0
        self.last_rx = 0
        self.last_ts = time.time()
        self.paused = False
        self.hex_mode = False
        self.title("实时预览 %s(读取会消费数据)" % dev)
        self.configure(bg=C_BG)
        self.geometry("900x560")
        self.protocol("WM_DELETE_WINDOW", self.close)

        top = tk.Frame(self, bg=C_CARD)
        top.pack(fill="x", padx=6, pady=4)
        tk.Button(top, text="暂停/继续", command=self.toggle_pause,
                  bg=C_CARD2, fg=C_FG, relief="flat").pack(side="left", padx=3)
        tk.Button(top, text="显示HEX" if not self.hex_mode else "显示ASCII",
                  command=self.toggle_hex, bg=C_CARD2, fg=C_FG,
                  relief="flat").pack(side="left", padx=3)
        tk.Button(top, text="清空", command=lambda: self.txt.delete("1.0", "end"),
                  bg=C_CARD2, fg=C_FG, relief="flat").pack(side="left", padx=3)
        tk.Button(top, text="保存日志", command=self.save, bg=C_CARD2, fg=C_FG,
                  relief="flat").pack(side="left", padx=3)
        self.stat = tk.Label(top, text="...", bg=C_CARD, fg=C_SUB, font=FONT_S)
        self.stat.pack(side="right", padx=6)

        self.txt = scrolledtext.ScrolledText(
            self, bg=C_INPUT, fg=C_FG, insertbackground=C_FG, font=FONT_M,
            relief="flat")
        self.txt.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        try:
            self.fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            self.stat.config(text="打开失败: %s" % open_error_msg(e), fg=C_BAD)
            messagebox.showwarning("打开失败", open_error_msg(e), parent=self)
        else:
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
        self.bind("<Destroy>", lambda e: self.close() if self.fd else None)

    def toggle_pause(self):
        self.paused = not self.paused

    def toggle_hex(self):
        self.hex_mode = not self.hex_mode
        for b in self.winfo_children():        # 更新按钮文字
            if isinstance(b, tk.Frame):
                for c in b.winfo_children():
                    if isinstance(c, tk.Button) and c["text"] in ("显示HEX", "显示ASCII"):
                        c.config(text="显示ASCII" if self.hex_mode else "显示HEX")

    def save(self):
        fn = filedialog.asksaveasfilename(defaultextension=".log",
                                          initialfile="预览_%s.log"
                                          % os.path.basename(self.dev))
        if not fn:
            return
        with open(fn, "w", errors="replace") as f:
            f.write(self.txt.get("1.0", "end"))
        self.app.log("✓ 预览日志已保存: %s" % fn)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            self.app.previews.pop(self.dev, None)
            self.app.log("预览已关闭: %s" % self.dev)

    def _loop(self):
        fd = self.fd
        while fd is not None and self.app and not self.app._stop:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
            except OSError:
                break
            if fd is None:
                break
            if r:
                try:
                    data = os.read(fd, 4096)
                except (BlockingIOError, OSError):
                    data = b""
                if data:
                    self.app.q.put(("prev", self.dev, data))
            else:
                self.app.q.put(("prevtick", self.dev))
            fd = self.fd

    def append(self, data):
        self.rx += len(data)
        if self.paused:
            return
        self.txt.insert("end", to_hex(data) + "  " if self.hex_mode
                        else printable(data))
        if float(self.txt.index("end-1c").split(".")[0]) > 4000:
            self.txt.delete("1.0", "1500.0")
        self.txt.see("end")

    def tick(self):
        now = time.time()
        dt = now - self.last_ts
        rate = (self.rx - self.last_rx) / dt if dt > 0.4 else 0
        self.last_rx, self.last_ts = self.rx, now
        self.stat.config(text="已收 %d 字节 | %.0f B/s%s"
                         % (self.rx, rate, "  [已暂停]" if self.paused else ""))


# ================= 主窗口 =================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("串口监控工具 —— Jetson Orin NX(6号)")
        self.configure(bg=C_BG)
        self.geometry("1240x800")
        self.minsize(1080, 680)

        self._stop = False
        self.q = queue.Queue()
        self._pw = None                    # sudo 密码缓存(仅内存)
        self._last_irq = {}
        self._io_stat = {}                 # dev -> {rx, tx}(本工具实测收发计数)
        self._open_fds = {}                # dev -> 用途名(本工具占用的口)
        self.previews = {}                 # dev -> PreviewWindow

        self._setup_style()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.nb = nb
        self.tab_over = tk.Frame(nb, bg=C_BG)
        self.tab_term = tk.Frame(nb, bg=C_BG)
        self.tab_mav = tk.Frame(nb, bg=C_BG)
        self.tab_doc = tk.Frame(nb, bg=C_BG)
        nb.add(self.tab_over, text=" 串口总览 ")
        nb.add(self.tab_term, text=" 串口终端 ")
        nb.add(self.tab_mav, text=" 飞控 MAVLink ")
        nb.add(self.tab_doc, text=" 资料 ")

        self._build_overview()
        self._build_terminal()
        self._build_mavlink()
        self._build_docs()
        self._refresh_ports()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.after(80, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("工具已启动。总览页默认只读不打扰任何串口;"
                 "预览/终端/MAVLink 打开串口前会有明确提示。")

    # ---------------- 样式 ----------------
    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=C_CARD, foreground=C_FG, fieldbackground=C_CARD)
        s.configure("TNotebook", background=C_BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=C_CARD, foreground=C_SUB,
                    padding=(14, 7), font=FONT_B)
        s.map("TNotebook.Tab", background=[("selected", C_ACC)],
              foreground=[("selected", "#ffffff")])
        s.configure("Treeview", background=C_CARD, foreground=C_FG,
                    fieldbackground=C_CARD, rowheight=26, font=FONT_M,
                    bordercolor=C_LINE)
        s.configure("Treeview.Heading", background=C_CARD2, foreground=C_FG,
                    font=FONT_B, relief="flat")
        s.map("Treeview", background=[("selected", C_ACC)],
              foreground=[("selected", "#ffffff")])
        s.configure("TCombobox", fieldbackground=C_INPUT, background=C_CARD2,
                    foreground=C_FG, arrowcolor=C_FG, bordercolor=C_LINE)
        s.map("TCombobox", fieldbackground=[("readonly", C_INPUT)])
        s.configure("TCheckbutton", background=C_CARD, foreground=C_FG)
        s.configure("TLabel", background=C_CARD, foreground=C_FG)

    def btn(self, parent, text, cmd, bg=C_CARD2, fg=C_FG, **kw):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=C_ACC, activeforeground="#ffffff",
                      relief="flat", font=FONT, padx=10, pady=4, **kw)
        return b

    def log(self, msg):
        self.q.put(("log", msg))

    # ================= 总览页 =================
    def _build_overview(self):
        f = self.tab_over
        top = tk.Frame(f, bg=C_BG)
        top.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(top, text="本机全部串口设备", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")
        tk.Label(top, text="活动列 = 内核中断增量, 不动设备就能看到数据流动"
                 "(内核不暴露中断的口显示—, 想看流量请用预览)",
                 bg=C_BG, fg=C_SUB, font=FONT_S).pack(side="left", padx=12)
        self.btn(top, "刷新", self._refresh_ports).pack(side="right")
        self.auto_refresh = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自动刷新", variable=self.auto_refresh
                        ).pack(side="right", padx=8)

        mid = tk.Frame(f, bg=C_BG)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("kind", "hw", "perm", "busy", "act", "io", "note")
        self.tv = ttk.Treeview(mid, columns=cols, show="headings",
                               selectmode="browse")
        heads = dict(kind="类型", hw="硬件来源", perm="权限", busy="占用状态",
                     act="活动", io="本工具收发", note="备注")
        wids = dict(kind=200, hw=150, perm=80, busy=260, act=90, io=150, note=240)
        for c in cols:
            self.tv.heading(c, text=heads[c])
            self.tv.column(c, width=wids[c], anchor="w")
        self.tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, command=self.tv.yview)
        sb.pack(side="right", fill="y")
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.tag_configure("flow", foreground=C_OK)
        self.tv.tag_configure("perm", foreground=C_WARN)
        self.tv.tag_configure("busy", foreground=C_WARN)
        self.tv.bind("<Double-1>", lambda e: self._act_preview())

        bot = tk.Frame(f, bg=C_BG)
        bot.pack(fill="x", padx=8, pady=4)
        self.btn(bot, "实时预览选中口", self._act_preview,
                 bg=C_ACC, fg="#ffffff").pack(side="left")
        self.btn(bot, "用终端页打开", self._act_term_open).pack(side="left", padx=6)
        self.btn(bot, "释放40-pin串口(需密码)", self._act_free40,
                 bg=C_WARN, fg="#000000").pack(side="left", padx=6)
        self.btn(bot, "打开完整手册", self._open_manual).pack(side="left", padx=6)

        self.ev = scrolledtext.ScrolledText(f, height=6, bg=C_INPUT, fg=C_SUB,
                                            font=FONT_M, relief="flat")
        self.ev.pack(fill="x", padx=8, pady=(0, 8))

    def _refresh_ports(self):
        try:
            snap = collect_snapshot()
            self.q.put(("snap", snap))
        except Exception as e:
            self.log("✗ 采集异常: %r" % e)

    def _apply_snap(self, snap):
        ports = snap["ports"]
        irqs = {p["dev"]: p["irq"] for p in ports if p["irq"] is not None}
        sel = self.tv.selection()
        self.tv.delete(*self.tv.get_children())
        for p in ports:
            dev = p["dev"]
            # IRQ 流量: 与上一轮比较
            act, tags = "—", []
            if p["irq"] is not None:
                d = p["irq"] - self._last_irq.get(dev, p["irq"])
                act = "● 有流量" if d > 0 else "○ 静默"
                if d > 0:
                    tags.append("flow")
            self._last_irq[dev] = p["irq"] or 0
            busy = ("本工具:%s" % self._open_fds[dev]) if dev in self._open_fds \
                else ("、".join(p["busy_users"]) if p["busy_users"] else "空闲")
            if busy != "空闲" and dev not in self._open_fds:
                tags.append("busy")
            if p["perms"] in ("只读", "只写", "无权限"):
                tags.append("perm")
            note = []
            if dev in snap["console"].split():
                note.append("内核控制台(数据被日志污染)")
            if "serial-getty@%s" % p["name"] in snap["getty"]:
                note.append("getty 登录终端占用")
            io = "-"
            st = self._io_stat.get(dev)
            if st:
                io = "RX %d / TX %d" % (st["rx"], st["tx"])
            vals = (p["kind"], p["hw"],
                    "%s %s(%s)" % (p["perms"], p["mode"], p["group"]),
                    busy, act, io, " ".join(note))
            self.tv.insert("", "end", iid=dev, values=vals, tags=tags)
        if sel and self.tv.exists(sel[0]):
            self.tv.selection_set(sel[0])
        self._console_txt = snap["console"]
        self._getty_txt = ",".join(snap["getty"]) or "无"
        self._update_port_combos([p["dev"] for p in ports])
        # 40-pin(ttyAMA0)占用状态提示(资料页)
        if hasattr(self, "lbl_40stat"):
            occupied = ("ttyAMA0" in snap["console"].split()
                        or "serial-getty@ttyAMA0" in snap["getty"])
            self.lbl_40stat.config(
                text=("⚠ ttyAMA0(40-pin 8/10 脚)当前被内核控制台/getty 占用,"
                      "飞控使用前请点\"释放40-pin串口\"并重启"
                      if occupied else
                      "✓ ttyAMA0(40-pin 8/10 脚)未被系统占用, 可直接用于飞控"),
                fg=C_WARN if occupied else C_OK)

    def _update_port_combos(self, devs):
        """用最新端口列表刷新终端页/MAVLink页下拉(保留已有选择)。"""
        for cmb in (self.cmb_tport, self.cmb_mport):
            old = cmb.get()
            cmb["values"] = devs
            if old in devs:
                cmb.set(old)
            elif devs:
                cmb.set(devs[0])
        # 飞控口智能默认: 有 ttyACM* 优先(USB 飞控), 其次 ttyTHS*
        preferred = [d for d in devs if d.startswith("/dev/ttyACM")] or \
                    [d for d in devs if d.startswith("/dev/ttyTHS")] or devs
        if self.cmb_mport.get() not in preferred and preferred:
            self.cmb_mport.set(preferred[0])

    def _sel_dev(self):
        s = self.tv.selection()
        return s[0] if s else None

    def _act_preview(self):
        dev = self._sel_dev()
        if not dev:
            messagebox.showinfo("提示", "请先在列表中选中一个串口", parent=self)
            return
        if dev in self._open_fds:
            messagebox.showwarning("已被占用",
                                   "%s 已被本工具(%s)打开。" % (dev, self._open_fds[dev]),
                                   parent=self)
            return
        if dev not in self.previews:
            go = messagebox.askyesno(
                "预览会消费数据",
                "预览会以只读方式打开 %s 并把读到的数据消费掉。\n\n"
                "如果飞控/其他程序正在用这个口, 数据会被抢走, 请勿预览!\n"
                "确定继续?" % dev, icon="warning", parent=self)
            if not go:
                return
        try:
            w = PreviewWindow(self, dev)
            if w.fd is None:
                w.destroy()
                return
            self.previews[dev] = w
            self._open_fds[dev] = "预览"
            self.log("✓ 预览打开: %s(数据流被本窗口消费)" % dev)
        except OSError as e:
            messagebox.showwarning("打开失败", open_error_msg(e), parent=self)
            self.log("✗ 打开 %s 失败: %s" % (dev, open_error_msg(e)))

    def _act_term_open(self):
        dev = self._sel_dev()
        if not dev:
            messagebox.showinfo("提示", "请先在列表中选中一个串口", parent=self)
            return
        self.cmb_tport.set(dev)
        self.nb.select(self.tab_term)

    def _act_free40(self):
        if messagebox.askyesno(
                "释放 40-pin 串口",
                "将执行(需管理员密码, 改动需重启生效):\n"
                "① 停用 serial-getty@ttyAMA0(串口登录终端)\n"
                "② 从 /boot/extlinux/extlinux.conf 去掉 console=ttyAMA0\n"
                "③ ttyAMA0 设备组改为 dialout(免 sudo 读写)\n\n"
                "这会释放 40-pin 排针 8/10 脚的 UART 给飞控用。\n"
                "恢复系统默认请用\"恢复40-pin串口\"。确定继续?", parent=self):
            if self.run_root(["free-40pin"]):
                self.log("✓ 已释放 40-pin 串口, 请重启后生效。之后 8/10 脚 = ttyAMA0 可自由使用")

    def _open_manual(self):
        if os.path.exists(MANUAL):
            webbrowser.open("file://" + MANUAL)
            self.log("✓ 已在浏览器打开资料手册")
        else:
            messagebox.showwarning("未找到手册", "缺少文件: %s" % MANUAL, parent=self)

    # ================= 终端页 =================
    def _build_terminal(self):
        f = self.tab_term
        cfg = tk.Frame(f, bg=C_CARD)
        cfg.pack(fill="x", padx=8, pady=8)

        tk.Label(cfg, text="端口", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=0, padx=4, pady=4, sticky="e")
        self.cmb_tport = ttk.Combobox(cfg, width=14, state="readonly",
                                      font=FONT_M)
        self.cmb_tport.grid(row=0, column=1, padx=2, pady=4)

        tk.Label(cfg, text="波特率", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=2, padx=4, sticky="e")
        self.cmb_tbaud = ttk.Combobox(cfg, width=8, state="readonly",
                                      values=[str(b) for b in BAUDS],
                                      font=FONT_M)
        self.cmb_tbaud.set("115200")
        self.cmb_tbaud.grid(row=0, column=3, padx=2)

        tk.Label(cfg, text="数据位", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=4, padx=4, sticky="e")
        self.cmb_tdata = ttk.Combobox(cfg, width=3, state="readonly",
                                      values=["8", "7"], font=FONT_M)
        self.cmb_tdata.set("8")
        self.cmb_tdata.grid(row=0, column=5, padx=2)

        tk.Label(cfg, text="校验", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=6, padx=4, sticky="e")
        self.cmb_tpar = ttk.Combobox(cfg, width=3, state="readonly",
                                     values=["N", "E", "O"], font=FONT_M)
        self.cmb_tpar.set("N")
        self.cmb_tpar.grid(row=0, column=7, padx=2)

        tk.Label(cfg, text="停止位", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=8, padx=4, sticky="e")
        self.cmb_tstop = ttk.Combobox(cfg, width=3, state="readonly",
                                      values=["1", "2"], font=FONT_M)
        self.cmb_tstop.set("1")
        self.cmb_tstop.grid(row=0, column=9, padx=2)

        tk.Label(cfg, text="流控", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=10, padx=4, sticky="e")
        self.cmb_tflow = ttk.Combobox(cfg, width=7, state="readonly",
                                      values=["无", "RTS/CTS", "XON/XOFF"],
                                      font=FONT_M)
        self.cmb_tflow.set("无")
        self.cmb_tflow.grid(row=0, column=11, padx=2)

        self.tflush = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg, text="打开时清缓冲", variable=self.tflush
                        ).grid(row=0, column=12, padx=8)

        self.btn_term = self.btn(cfg, "连接", self._term_connect,
                                 bg=C_OK, fg="#000000")
        self.btn_term.grid(row=0, column=13, padx=6)
        self.btn(cfg, "断开", self._term_disconnect).grid(row=0, column=14, padx=2)
        self.lbl_tstat = tk.Label(cfg, text="未连接", bg=C_CARD, fg=C_SUB,
                                  font=FONT_S)
        self.lbl_tstat.grid(row=1, column=0, columnspan=15, sticky="w", padx=6)

        # 接收区
        rxb = tk.Frame(f, bg=C_CARD)
        rxb.pack(fill="both", expand=True, padx=8, pady=4)
        bar = tk.Frame(rxb, bg=C_CARD)
        bar.pack(fill="x")
        self.thex = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="HEX显示", variable=self.thex).pack(side="left")
        self.tts = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="时间戳", variable=self.tts).pack(side="left", padx=8)
        self.tscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动滚动", variable=self.tscroll).pack(side="left")
        self.tpause = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="暂停接收(底层仍计数)", variable=self.tpause
                        ).pack(side="left", padx=8)
        tk.Label(bar, text="", bg=C_CARD).pack(side="left", expand=True)
        self.lbl_trx = tk.Label(bar, text="RX 0 | TX 0", bg=C_CARD, fg=C_SUB,
                                font=FONT_M)
        self.lbl_trx.pack(side="left", padx=8)
        self.btn(bar, "清空", lambda: self.ttxt.delete("1.0", "end")).pack(side="left")
        self.btn(bar, "保存日志", self._term_save).pack(side="left", padx=6)
        self.ttxt = scrolledtext.ScrolledText(
            rxb, bg=C_INPUT, fg=C_FG, insertbackground=C_FG, font=FONT_M,
            relief="flat", height=16)
        self.ttxt.pack(fill="both", expand=True, pady=(4, 0))
        for tag, col in (("rx", C_FG), ("ts", C_SUB), ("tx", C_OK), ("err", C_BAD)):
            self.ttxt.tag_configure(tag, foreground=col)

        # 发送区
        txb = tk.Frame(f, bg=C_CARD)
        txb.pack(fill="x", padx=8, pady=(4, 8))
        tk.Label(txb, text="发送", bg=C_CARD, fg=C_SUB, font=FONT_S).pack(side="left")
        self.txt_tx = tk.Entry(txb, bg=C_INPUT, fg=C_FG, insertbackground=C_FG,
                               font=FONT_M, relief="flat")
        self.txt_tx.pack(side="left", fill="x", expand=True, padx=6, ipady=5)
        self.txt_tx.bind("<Return>", lambda e: self._term_send())
        self.thexsend = tk.BooleanVar(value=False)
        ttk.Checkbutton(txb, text="HEX", variable=self.thexsend).pack(side="left")
        tk.Label(txb, text="行尾", bg=C_CARD, fg=C_SUB, font=FONT_S).pack(side="left", padx=(8, 0))
        self.cmb_teol = ttk.Combobox(txb, width=4, state="readonly",
                                     values=["无", "CR", "LF", "CRLF"], font=FONT_M)
        self.cmb_teol.set("无")
        self.cmb_teol.pack(side="left", padx=4)
        self.btn(txb, "发送", self._term_send, bg=C_ACC, fg="#ffffff"
                 ).pack(side="left", padx=4)
        self.btn(txb, "发送文件", self._term_sendfile).pack(side="left", padx=4)
        self.btn(txb, "↵回车", lambda: self._term_write_raw(b"\r\n", "回车")
                 ).pack(side="left", padx=4)

        tk.Label(txb, text="定时(秒)", bg=C_CARD, fg=C_SUB, font=FONT_S).pack(side="left", padx=(14, 0))
        self.ent_interval = tk.Entry(txb, width=5, bg=C_INPUT, fg=C_FG,
                                     font=FONT_M, relief="flat")
        self.ent_interval.insert(0, "1.0")
        self.ent_interval.pack(side="left", padx=4, ipady=3)
        self.btn(txb, "定时发送开/关", self._term_toggle_periodic).pack(side="left", padx=4)
        self.lbl_tstat2 = tk.Label(txb, text="", bg=C_CARD, fg=C_WARN, font=FONT_S)
        self.lbl_tstat2.pack(side="left", padx=6)

    def _term_connect(self):
        dev = self.cmb_tport.get()
        if not dev:
            messagebox.showinfo("提示", "没有可选串口", parent=self)
            return
        if dev in self._open_fds:
            messagebox.showwarning("已被占用",
                                   "%s 已被本工具(%s)打开, 请先断开。" % (dev, self._open_fds[dev]),
                                   parent=self)
            return
        try:
            flow = self.cmb_tflow.get()
            fd = open_serial(dev, int(self.cmb_tbaud.get()),
                             bytesize=int(self.cmb_tdata.get()),
                             parity=self.cmb_tpar.get(),
                             stopbits=int(self.cmb_tstop.get()),
                             xonxoff=(flow == "XON/XOFF"),
                             rtscts=(flow == "RTS/CTS"),
                             flush=self.tflush.get())
        except OSError as e:
            messagebox.showwarning("打开失败", open_error_msg(e), parent=self)
            self.log("✗ 终端打开 %s 失败: %s" % (dev, open_error_msg(e)))
            return
        self.tfd = fd
        self.tdev = dev
        self._open_fds[dev] = "终端"
        self._io_stat.setdefault(dev, dict(rx=0, tx=0))
        self.t_rx = self.t_tx = 0
        self.btn_term.config(text="已连接", state="disabled")
        self.lbl_tstat.config(
            text="已连接 %s @ %s  数据位%s 校验%s 停止位%s 流控%s"
                 % (dev, self.cmb_tbaud.get(), self.cmb_tdata.get(),
                    self.cmb_tpar.get(), self.cmb_tstop.get(), flow),
            fg=C_OK)
        self.log("✓ 终端连接 %s @ %s" % (dev, self.cmb_tbaud.get()))
        threading.Thread(target=self._term_reader, daemon=True).start()

    def _term_disconnect(self):
        fd, dev = getattr(self, "tfd", None), getattr(self, "tdev", None)
        self._term_stop_periodic()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._open_fds.pop(dev, None)
            self.tfd = None
            self.btn_term.config(text="连接", state="normal")
            self.lbl_tstat.config(text="未连接", fg=C_SUB)
            self.log("终端断开: %s" % dev)

    def _term_reader(self):
        fd = self.tfd
        while self.tfd == fd and fd is not None and not self._stop:
            try:
                r, _, _ = select.select([fd], [], [], 0.15)
            except OSError:
                break
            if not r:
                continue
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if data:
                self.t_rx += len(data)
                if self._io_stat.get(self.tdev):
                    self._io_stat[self.tdev]["rx"] += len(data)
                self.q.put(("term_rx", data))
        self.q.put(("term_closed",))

    def _term_write_raw(self, data, desc=""):
        fd = getattr(self, "tfd", None)
        if fd is None:
            self.log("✗ 终端未连接")
            return
        try:
            os.write(fd, data)
        except OSError as e:
            self.log("✗ 写入失败: %s" % e)
            return
        self.t_tx += len(data)
        if self._io_stat.get(self.tdev):
            self._io_stat[self.tdev]["tx"] += len(data)
        if not self.tpause.get():
            s = to_hex(data) if self.thexsend.get() else printable(data)
            self.ttxt.insert("end", s, "tx")
            if self.tscroll.get():
                self.ttxt.see("end")
        if desc:
            self.log("$ 发送 %s: %s" % (self.tdev, desc))

    def _term_send(self):
        s = self.txt_tx.get()
        if not s:
            return
        try:
            data = from_hex(s) if self.thexsend.get() else s.encode("utf-8")
        except ValueError as e:
            messagebox.showwarning("HEX格式错误", str(e), parent=self)
            return
        eol = {"无": b"", "CR": b"\r", "LF": b"\n", "CRLF": b"\r\n"}[self.cmb_teol.get()]
        if not self.thexsend.get():
            data += eol
        self._term_write_raw(data)
        self.ttxt.insert("end", "\n" if not self.thexsend.get() else " ")
        if self.tscroll.get():
            self.ttxt.see("end")

    def _term_sendfile(self):
        fn = filedialog.askopenfilename(title="选择要发送的文件")
        if not fn:
            return
        try:
            with open(fn, "rb") as f:
                data = f.read()
        except OSError as e:
            messagebox.showwarning("读取失败", str(e), parent=self)
            return
        self._term_write_raw(data)
        self.log("$ 已发送文件 %s(%d 字节) → %s" % (os.path.basename(fn), len(data),
                                                   self.tdev))

    def _term_toggle_periodic(self):
        if getattr(self, "_periodic", False):
            self._term_stop_periodic()
            return
        if getattr(self, "tfd", None) is None:
            self.log("✗ 终端未连接")
            return
        try:
            iv = float(self.ent_interval.get())
            assert iv > 0.05
        except (ValueError, AssertionError):
            messagebox.showwarning("参数错误", "定时间隔需 > 0.05 秒", parent=self)
            return
        self._periodic = True
        self.lbl_tstat2.config(text="定时发送中: 每 %.1fs" % iv)

        def loop():
            while getattr(self, "_periodic", False) and getattr(self, "tfd", None):
                self.q.put(("term_periodic",))
                time.sleep(iv)
            self.q.put(("term_periodic_off",))
        threading.Thread(target=loop, daemon=True).start()

    def _term_stop_periodic(self):
        self._periodic = False
        self.lbl_tstat2.config(text="")

    def _term_save(self):
        fn = filedialog.asksaveasfilename(defaultextension=".log",
                                          initialfile="串口日志_%s.log"
                                          % time.strftime("%H%M%S"))
        if not fn:
            return
        with open(fn, "w", errors="replace") as f:
            f.write(self.ttxt.get("1.0", "end"))
        self.log("✓ 串口日志已保存: %s" % fn)

    # ================= MAVLink 页 =================
    def _build_mavlink(self):
        f = self.tab_mav
        cfg = tk.Frame(f, bg=C_CARD)
        cfg.pack(fill="x", padx=8, pady=8)
        tk.Label(cfg, text="端口", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=0, padx=4, sticky="e")
        self.cmb_mport = ttk.Combobox(cfg, width=14, state="readonly", font=FONT_M)
        self.cmb_mport.grid(row=0, column=1, padx=2)
        tk.Label(cfg, text="波特率", bg=C_CARD, fg=C_SUB, font=FONT_S).grid(
            row=0, column=2, padx=4, sticky="e")
        self.cmb_mbaud = ttk.Combobox(cfg, width=8, state="readonly",
                                      values=[str(b) for b in BAUDS], font=FONT_M)
        self.cmb_mbaud.set("115200")
        self.cmb_mbaud.grid(row=0, column=3, padx=2)
        self.btn_mconn = self.btn(cfg, "连接", self._mav_connect,
                                  bg=C_OK, fg="#000000")
        self.btn_mconn.grid(row=0, column=4, padx=6)
        self.btn(cfg, "断开", self._mav_disconnect).grid(row=0, column=5, padx=2)
        self.btn(cfg, "自动嗅探波特率", self._mav_sniff).grid(row=0, column=6, padx=6)
        self.lbl_mstat = tk.Label(cfg, text="未连接(USB直连的飞控一般是 ttyACM0 @115200;"
                                  " 40-pin 接线是 ttyAMA0,常见 57600)",
                                  bg=C_CARD, fg=C_SUB, font=FONT_S)
        self.lbl_mstat.grid(row=1, column=0, columnspan=8, sticky="w", padx=6)

        # ---- 状态卡片 ----
        cards = tk.Frame(f, bg=C_BG)
        cards.pack(fill="x", padx=8, pady=2)

        def card(title, row, col):
            lf = tk.LabelFrame(cards, text=title, bg=C_CARD, fg=C_SUB, font=FONT_S,
                               padx=8, pady=6, relief="flat",
                               highlightbackground=C_LINE, highlightthickness=1)
            lf.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            return lf

        cards.columnconfigure((0, 1, 2, 3, 4), weight=1)
        c1 = card("飞行状态", 0, 0)
        self.lbl_mode = tk.Label(c1, text="--", bg=C_CARD, fg=C_ACC, font=FONT_HH)
        self.lbl_mode.pack()
        self.lbl_armed = tk.Label(c1, text="--", bg=C_CARD, fg=C_SUB, font=FONT_B)
        self.lbl_armed.pack()
        self.lbl_sysstate = tk.Label(c1, text="系统: --", bg=C_CARD, fg=C_SUB,
                                     font=FONT_S)
        self.lbl_sysstate.pack()
        c2 = card("电池", 0, 1)
        self.lbl_batt_v = tk.Label(c2, text="--.- V", bg=C_CARD, fg=C_FG, font=FONT_H)
        self.lbl_batt_v.pack()
        self.lbl_batt_i = tk.Label(c2, text="电流 --.- A", bg=C_CARD, fg=C_SUB,
                                   font=FONT_S)
        self.lbl_batt_i.pack()
        self.lbl_batt_r = tk.Label(c2, text="剩余 --%", bg=C_CARD, fg=C_SUB,
                                   font=FONT_S)
        self.lbl_batt_r.pack()
        c3 = card("姿态(度)", 0, 2)
        self.lbl_att = tk.Label(c3, text="R --.-\nP --.-\nY --.-", bg=C_CARD,
                                fg=C_FG, font=FONT_M, justify="left")
        self.lbl_att.pack()
        c4 = card("高度 / 速度", 0, 3)
        self.lbl_alt = tk.Label(c4, text="相对 --.- m\n海拔 --.- m\n爬升 --.- m/s",
                                bg=C_CARD, fg=C_FG, font=FONT_M, justify="left")
        self.lbl_alt.pack()
        self.lbl_hud = tk.Label(c4, text="地速 -- 空速 --\n油门 --%", bg=C_CARD,
                                fg=C_SUB, font=FONT_S, justify="left")
        self.lbl_hud.pack()
        c5 = card("GPS", 0, 4)
        self.lbl_gps = tk.Label(c5, text="定位: --\n卫星: --", bg=C_CARD, fg=C_FG,
                                font=FONT_M, justify="left")
        self.lbl_gps.pack()
        self.lbl_gpspos = tk.Label(c5, text="lat --\nlon --", bg=C_CARD, fg=C_SUB,
                                   font=FONT_S, justify="left")
        self.lbl_gpspos.pack()

        c6 = card("消息速率(帧/秒)", 1, 0)
        cols = ("name", "rate", "n")
        self.tv_mav = ttk.Treeview(c6, columns=cols, show="headings", height=7)
        for c, t, w in (("name", "消息", 130), ("rate", "速率", 60), ("n", "计数", 70)):
            self.tv_mav.heading(c, text=t)
            self.tv_mav.column(c, width=w, anchor="w")
        self.tv_mav.pack(fill="both", expand=True)

        c7 = card("飞控文本消息(STATUSTEXT)", 1, 1)
        self.txt_status = scrolledtext.ScrolledText(
            c7, bg=C_INPUT, fg=C_FG, font=FONT_M, relief="flat", height=9, width=44)
        self.txt_status.pack(fill="both", expand=True)
        for i, tag in enumerate(("sev0", "sev1", "sev2", "sev3", "sev4",
                                 "sev5", "sev6", "sev7")):
            self.txt_status.tag_configure(tag, foreground=(
                C_BAD, C_BAD, C_BAD, C_BAD, C_WARN, C_FG, C_OK, C_SUB)[i])

        c8 = card("指令日志 / 参数", 1, 2)
        self.txt_cmdlog = scrolledtext.ScrolledText(
            c8, bg=C_INPUT, fg=C_FG, font=FONT_M, relief="flat", height=9, width=48)
        self.txt_cmdlog.pack(fill="both", expand=True)

        # ---- 控制区 ----
        ctrl = tk.Frame(f, bg=C_CARD)
        ctrl.pack(fill="x", padx=8, pady=6)
        tk.Label(ctrl, text="模式", bg=C_CARD, fg=C_SUB, font=FONT_S).pack(side="left")
        self.cmb_mmode = ttk.Combobox(ctrl, width=14, state="readonly", font=FONT_M)
        self.cmb_mmode.set("(连接后按固件加载)")
        self.cmb_mmode.pack(side="left", padx=4)
        self.btn(ctrl, "切换模式", self._mav_setmode).pack(side="left", padx=2)
        self.btn(ctrl, "✋ 上锁", self._mav_disarm).pack(side="left", padx=(16, 2))
        self.btn(ctrl, "⚡ 解锁(危险)", self._mav_arm, bg=C_BAD,
                 fg="#ffffff").pack(side="left", padx=2)
        self.btn(ctrl, "请求数据流", self._mav_reqstream).pack(side="left", padx=(16, 2))
        self.btn(ctrl, "Ping", self._mav_ping).pack(side="left", padx=2)
        tk.Label(ctrl, text="读参数", bg=C_CARD, fg=C_SUB, font=FONT_S).pack(
            side="left", padx=(16, 0))
        self.ent_param = tk.Entry(ctrl, width=14, bg=C_INPUT, fg=C_FG,
                                  font=FONT_M, relief="flat")
        self.ent_param.insert(0, "SER_TEL2_BAUD")
        self.ent_param.pack(side="left", padx=4, ipady=3)
        self.btn(ctrl, "读", self._mav_readparam).pack(side="left")
        self.lbl_param = tk.Label(ctrl, text="", bg=C_CARD, fg=C_OK, font=FONT_M)
        self.lbl_param.pack(side="left", padx=8)

        adv = tk.Frame(f, bg=C_CARD)
        adv.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(adv, text="原始指令 COMMAND_LONG:", bg=C_CARD, fg=C_SUB,
                 font=FONT_S).pack(side="left")
        self.ent_cmdid = tk.Entry(adv, width=6, bg=C_INPUT, fg=C_FG, font=FONT_M,
                                  relief="flat")
        self.ent_cmdid.insert(0, "400")
        self.ent_cmdid.pack(side="left", padx=4, ipady=3)
        self.ent_cmdp = []
        for i in range(7):
            e = tk.Entry(adv, width=6, bg=C_INPUT, fg=C_FG, font=FONT_M,
                         relief="flat")
            e.insert(0, "0")
            e.pack(side="left", padx=2, ipady=3)
            self.ent_cmdp.append(e)
        self.btn(adv, "发送指令", self._mav_rawcmd).pack(side="left", padx=6)
        self.lbl_link = tk.Label(adv, text="", bg=C_CARD, fg=C_SUB, font=FONT_S)
        self.lbl_link.pack(side="right", padx=8)

        # mavlink 内部状态
        self._mav_reset()

    def _mav_reset(self):
        self.mfd = None
        self.mparser = mav.Parser()
        self.mdev = None
        self.m_sysid = 1          # 目标(飞控)的 sysid/compid, 收到心跳后自动更新
        self.m_compid = 1
        self.m_autopilot = None
        self.m_type = None
        self.m_mode = None
        self.m_armed = None
        self.m_sysstate = None
        self.m_seq = 0
        self.m_rx = self.m_tx = 0
        self.m_hb_ts = []         # 心跳时间戳 → 频率
        self.m_rates = {}         # name -> [deque(时间戳), 累计帧数]
        self.m_batt_v = self.m_batt_i = self.m_batt_r = None
        self.m_att = self.m_pos = self.m_gps = self.m_hud = self.m_rc = None
        self.m_req_sent = False
        self.m_ping_t0 = {}
        self.m_rtt = None
        self._mav_rate_ts = 0

    def _mav_connect(self):
        dev = self.cmb_mport.get()
        if not dev:
            messagebox.showinfo("提示", "没有可选串口", parent=self)
            return
        if dev in self._open_fds:
            messagebox.showwarning("已被占用",
                                   "%s 已被本工具(%s)打开, 请先断开。" % (dev, self._open_fds[dev]),
                                   parent=self)
            return
        try:
            fd = open_serial(dev, int(self.cmb_mbaud.get()))
        except OSError as e:
            messagebox.showwarning("打开失败", open_error_msg(e), parent=self)
            self.log("✗ MAVLink 打开 %s 失败: %s" % (dev, open_error_msg(e)))
            return
        self._mav_reset()
        self.mfd = fd
        self.mdev = dev
        self._open_fds[dev] = "MAVLink"
        self._io_stat.setdefault(dev, dict(rx=0, tx=0))
        self.btn_mconn.config(text="已连接", state="disabled")
        self.lbl_mstat.config(text="连接 %s, 已发地面站心跳, 等待飞控应答…" % dev,
                              fg=C_WARN)
        self.log("✓ MAVLink 连接 %s @ %s" % (dev, self.cmb_mbaud.get()))
        self.txt_cmdlog.delete("1.0", "end")
        self.txt_status.delete("1.0", "end")
        # 不少飞控(如 PX4)要"看到"地面站心跳才开始发数据, 连接后立即发一个
        self._mav_last_gcs_hb = 0
        threading.Thread(target=self._mav_reader, daemon=True).start()

    def _mav_disconnect(self):
        fd, dev = self.mfd, self.mdev
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._open_fds.pop(dev, None)
            self.mfd = None
            self.btn_mconn.config(text="连接", state="normal")
            self.lbl_mstat.config(text="未连接", fg=C_SUB)
            self.log("MAVLink 断开: %s" % dev)

    def _mav_reader(self):
        fd = self.mfd
        while self.mfd == fd and fd is not None and not self._stop:
            # 地面站心跳: 每秒一发(飞控看到它才会持续发流, 掉发会停更)
            now = time.time()
            if now - getattr(self, "_mav_last_gcs_hb", 0) > 1.0:
                self._mav_last_gcs_hb = now
                try:
                    raw = mav.heartbeat(
                        autopilot=0, mav_type=6, base_mode=0, custom_mode=0,
                        seq=self._mav_next_seq())
                    os.write(fd, raw)
                    self.m_tx += len(raw)
                    if self._io_stat.get(self.mdev):
                        self._io_stat[self.mdev]["tx"] += len(raw)
                except OSError:
                    break
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
            except OSError:
                break
            if not r:
                continue
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if data:
                self.m_rx += len(data)
                if self._io_stat.get(self.mdev):
                    self._io_stat[self.mdev]["rx"] += len(data)
                frames = self.mparser.feed(data)
                if frames:
                    self.q.put(("mav", frames))

    def _mav_write(self, raw, desc):
        if self.mfd is None:
            self.log("✗ MAVLink 未连接")
            return
        try:
            os.write(self.mfd, raw)
        except OSError as e:
            self.log("✗ MAVLink 写入失败: %s" % e)
            return
        self.m_tx += len(raw)
        if self._io_stat.get(self.mdev):
            self._io_stat[self.mdev]["tx"] += len(raw)
        self.log("$ MAVLink → %s" % desc)
        self.txt_cmdlog.insert("end", "[%s] → %s\n"
                               % (time.strftime("%H:%M:%S"), desc))
        self.txt_cmdlog.see("end")

    def _mav_next_seq(self):
        self.m_seq = (self.m_seq + 1) & 0xFF
        return self.m_seq

    def _mav_on_heartbeat(self, fr):
        f = fr.fields
        self.m_sysid, self.m_compid = fr.sysid, fr.compid
        self.m_autopilot = f.get("autopilot")
        self.m_type = f.get("type")
        base, custom = f.get("base_mode", 0), f.get("custom_mode", 0)
        self.m_mode = mav.mode_name(self.m_autopilot, self.m_type, base, custom)
        self.m_armed = bool(base & mav.MAV_MODE_FLAG_SAFETY_ARMED)
        self.m_sysstate = mav.MAV_STATE_NAMES.get(f.get("system_status"), "--")
        now = time.time()
        self.m_hb_ts.append(now)
        self.m_hb_ts = self.m_hb_ts[-20:]
        # 首次心跳: 请求遥测数据流(两种流派都发, 兼容 ArduPilot/PX4)
        if not self.m_req_sent:
            self.m_req_sent = True
            self.lbl_mstat.config(
                text="已连接 %s | %s | sysid=%d compid=%d"
                     % (self.mdev, mav.MAV_AUTOPILOT_NAMES.get(
                         self.m_autopilot, "固件%d" % (self.m_autopilot or -1)),
                        self.m_sysid, self.m_compid), fg=C_OK)
            self._mav_load_mode_choices()
            self._mav_write(mav.request_data_stream(
                self.m_sysid, self.m_compid, 10, seq=self._mav_next_seq()),
                "REQUEST_DATA_STREAM 10Hz")
            self._mav_write(mav.set_message_interval(33, 100000,
                             self.m_sysid, self.m_compid,
                             seq=self._mav_next_seq()),
                            "SET_MESSAGE_INTERVAL GLOBAL_POSITION_INT 10Hz")
            self._mav_write(mav.encode(148, dict(), seq=self._mav_next_seq()),
                            "请求 AUTOPILOT_VERSION")
            self.log("✓ 收到心跳: %s sysid=%d" % (
                mav.MAV_AUTOPILOT_NAMES.get(self.m_autopilot, "?"), self.m_sysid))

    def _mav_load_mode_choices(self):
        if self.m_autopilot == 12:       # PX4
            choices = ["%s|%d|%d" % c for c in mav.PX4_MODE_CHOICES]
        elif self.m_autopilot == 3:      # ArduPilot
            choices = ["%s|%d" % c for c in mav.ARDU_MODE_CHOICES]
        else:
            return
        self.cmb_mmode["values"] = choices
        self.cmb_mmode.set(choices[0])

    def _mav_handle_frame(self, fr):
        name, flds = fr.name, fr.fields
        now = time.time()
        lst = self.m_rates.get(name)
        if lst is None:
            lst = self.m_rates[name] = [deque(maxlen=600), 0]
        lst[0].append(now)
        lst[1] += 1
        if name == "HEARTBEAT":
            self._mav_on_heartbeat(fr)
        elif name == "SYS_STATUS":
            self.m_batt_v = flds.get("voltage_battery", 0) / 1000.0
            cur = flds.get("current_battery", -1)
            self.m_batt_i = cur / 100.0 if cur >= 0 else None
            rem = flds.get("battery_remaining", -1)
            self.m_batt_r = rem if rem >= 0 else None
        elif name == "ATTITUDE":
            self.m_att = (math.degrees(flds.get("roll", 0)),
                          math.degrees(flds.get("pitch", 0)),
                          math.degrees(flds.get("yaw", 0)))
        elif name == "ATTITUDE_QUATERNION":
            r, p, y = mav.quat_to_rpy(flds.get("q1", 1), flds.get("q2", 0),
                                      flds.get("q3", 0), flds.get("q4", 0))
            self.m_att = (math.degrees(r), math.degrees(p), math.degrees(y))
        elif name == "GLOBAL_POSITION_INT":
            self.m_pos = (flds.get("relative_alt", 0) / 1000.0,
                          flds.get("alt", 0) / 1000.0,
                          flds.get("lat", 0) / 1e7, flds.get("lon", 0) / 1e7,
                          flds.get("vx", 0) / 100.0, flds.get("vy", 0) / 100.0,
                          flds.get("vz", 0) / 100.0)
        elif name == "GPS_RAW_INT":
            self.m_gps = (mav.MAV_FIX_NAMES.get(flds.get("fix_type"), "?"),
                          flds.get("satellites_visible", 0),
                          flds.get("lat", 0) / 1e7, flds.get("lon", 0) / 1e7)
        elif name == "VFR_HUD":
            self.m_hud = (flds.get("groundspeed", 0), flds.get("airspeed", 0),
                          flds.get("throttle", 0), flds.get("climb", 0),
                          flds.get("heading", 0))
        elif name == "RC_CHANNELS":
            self.m_rc = (flds.get("rssi", 0), flds.get("chancount", 0),
                         [flds.get("chan%d_raw" % i, 0) for i in range(1, 9)])
        elif name == "STATUSTEXT":
            sev = flds.get("severity", 6)
            txt = flds.get("text", "")
            tag = "sev%d" % min(sev, 7)
            self.txt_status.insert("end", "[%s] %s\n"
                                   % (mav.STATUS_SEVERITY_NAMES[min(sev, 7)], txt),
                                   tag)
            self.txt_status.see("end")
        elif name == "COMMAND_ACK":
            cmd = flds.get("command", 0)
            res = flds.get("result", 255)
            cname = mav.MAV_CMD_NAMES.get(cmd, "命令%d" % cmd)
            rname = mav.CMD_RESULT_NAMES.get(res, "结果%d" % res)
            ok = res == 0
            self.txt_cmdlog.insert("end", "[%s] ← ACK %s: %s\n"
                                   % (time.strftime("%H:%M:%S"), cname, rname))
            self.txt_cmdlog.see("end")
            self.log("%s ACK %s → %s" % ("✓" if ok else "✗", cname, rname))
        elif name == "PARAM_VALUE":
            val = flds.get("param_value")
            self.lbl_param.config(text="%s = %s" % (flds.get("param_id", "?"), val))
            self.txt_cmdlog.insert("end", "[%s] ← 参数 %s = %s\n"
                                   % (time.strftime("%H:%M:%S"),
                                      flds.get("param_id", "?"), val))
            self.txt_cmdlog.see("end")
        elif name == "AUTOPILOT_VERSION":
            ver = mav.fw_version_str(flds.get("flight_sw_version"))
            self.txt_cmdlog.insert("end", "[%s] ← 固件版本 %s\n"
                                   % (time.strftime("%H:%M:%S"), ver))
            self.txt_cmdlog.see("end")
            self.log("✓ 飞控固件版本: %s" % ver)
        elif name == "PING":
            t0 = self.m_ping_t0.get(flds.get("seq"))
            if t0 is not None:
                self.m_rtt = (now * 1e6 - flds.get("time_usec", 0)) / 1000.0
                self.txt_cmdlog.insert("end", "[%s] ← PING 回应 RTT %.1f ms\n"
                                       % (time.strftime("%H:%M:%S"), self.m_rtt))
                self.txt_cmdlog.see("end")

    def _mav_update_labels(self):
        if self.mfd is None:
            return
        now = time.time()
        # 心跳频率
        hb = [t for t in self.m_hb_ts if now - t < 3]
        hz = len(hb) / 3.0 if hb else 0
        self.lbl_mode.config(text=self.m_mode or "等待心跳…")
        if self.m_armed is not None:
            self.lbl_armed.config(text="🔓 已解锁(电机可能转动!)" if self.m_armed
                                  else "🔒 已上锁", fg=C_BAD if self.m_armed else C_OK)
        self.lbl_sysstate.config(text="系统: %s | 心跳 %.1f Hz | 链路 %s"
                                 % (getattr(self, "m_sysstate", "--"), hz,
                                    ("%.0f ms" % self.m_rtt) if self.m_rtt else "—"))
        if getattr(self, "m_batt_v", None):
            self.lbl_batt_v.config(text="%.2f V" % self.m_batt_v)
        self.lbl_batt_i.config(text=("电流 %+.1f A" % self.m_batt_i)
                               if getattr(self, "m_batt_i", None) is not None
                               else "电流 --")
        self.lbl_batt_r.config(text=("剩余 %d%%" % self.m_batt_r)
                               if getattr(self, "m_batt_r", None) is not None
                               else "剩余 --")
        att = getattr(self, "m_att", None)
        if att:
            self.lbl_att.config(text="R %+6.1f\nP %+6.1f\nY %+6.1f" % att)
        pos = getattr(self, "m_pos", None)
        if pos:
            self.lbl_alt.config(text="相对 %+7.1f m\n海拔 %7.1f m\n爬升 %+5.1f m/s"
                                % (pos[0], pos[1], pos[6]))
            self.lbl_gpspos.config(text="lat %.6f\nlon %.6f" % (pos[2], pos[3]))
        gps = getattr(self, "m_gps", None)
        if gps:
            self.lbl_gps.config(text="定位: %s\n卫星: %d" % (gps[0], gps[1]))
        hud = getattr(self, "m_hud", None)
        if hud:
            self.lbl_hud.config(text="地速 %.1f 空速 %.1f\n油门 %d%%  航向 %d°"
                                % (hud[0], hud[1], hud[2], hud[4]))
        # 速率表(每 0.5s 刷一次)
        if now - getattr(self, "_mav_rate_ts", 0) > 0.5:
            self._mav_rate_ts = now
            self.tv_mav.delete(*self.tv_mav.get_children())
            rows = []
            for name, (tsq, cnt) in self.m_rates.items():
                rate = sum(1 for t in tsq if now - t < 2.0) / 2.0
                rows.append((rate, name, cnt))
            rows.sort(reverse=True)
            for rate, name, cnt in rows[:14]:
                self.tv_mav.insert("", "end", values=(
                    name, "%.1f" % rate, cnt))
        self.lbl_link.config(text="RX %d / TX %d 字节 | 坏帧 %d | 未知 %d"
                             % (self.m_rx, self.m_tx, self.mparser.bad_frames,
                                self.mparser.unknown_frames))

    # --- MAVLink 发送动作 ---
    def _mav_setmode(self):
        v = self.cmb_mmode.get()
        if self.mfd is None or "|" not in v:
            self.log("✗ 请先连接飞控并选择模式")
            return
        parts = v.split("|")
        if self.m_autopilot == 12:
            name, main, sub = parts[0], int(parts[1]), int(parts[2])
            custom = (main << 24) | (sub << 16)
            base = 1                    # MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        else:
            name, custom = parts[0], int(parts[1])
            base = 209                  # custom+test+guided+stabilize 常用组合
        self._mav_write(mav.command_long(
            176, p1=float(base), p2=float(custom),
            target_system=self.m_sysid, target_component=self.m_compid,
            seq=self._mav_next_seq()), "切换模式 → %s" % name)

    def _mav_arm(self):
        if self.mfd is None:
            self.log("✗ MAVLink 未连接")
            return
        if not messagebox.askyesno(
                "解锁确认(1/2)",
                "确定要发送【解锁(ARM)】指令吗?\n\n"
                "解锁后电机随时可能转动!\n"
                "· 请确认桨叶周围无人和杂物\n"
                "· 请勿在室内无固定情况下解锁\n"
                "· 保持对遥控器的随时接管",
                icon="warning", parent=self):
            self.log("已取消解锁")
            return
        s = simpledialog.askstring(
            "解锁确认(2/2)", "输入 ARM 三个字母以最终确认:",
            parent=self)
        if s != "ARM":
            self.log("已取消解锁(未输入 ARM)")
            return
        self._mav_write(mav.command_long(
            400, p1=1.0, target_system=self.m_sysid,
            target_component=self.m_compid, seq=self._mav_next_seq()),
            "COMPONENT_ARM_DISARM 解锁")

    def _mav_disarm(self):
        if self.mfd is None:
            self.log("✗ MAVLink 未连接")
            return
        self._mav_write(mav.command_long(
            400, p1=0.0, target_system=self.m_sysid,
            target_component=self.m_compid, seq=self._mav_next_seq()),
            "COMPONENT_ARM_DISARM 上锁")

    def _mav_reqstream(self):
        if self.mfd is None:
            return
        self._mav_write(mav.request_data_stream(
            self.m_sysid, self.m_compid, 10, seq=self._mav_next_seq()),
            "REQUEST_DATA_STREAM 10Hz")
        self._mav_write(mav.set_message_interval(33, 100000, self.m_sysid,
                         self.m_compid, seq=self._mav_next_seq()),
                        "SET_MESSAGE_INTERVAL POSITION 10Hz")

    def _mav_ping(self):
        if self.mfd is None:
            return
        seqn = int(time.time()) & 0xFFFF
        self.m_ping_t0[seqn] = time.time()
        self._mav_write(mav.ping(int(time.time() * 1e6), seqn,
                                 self.m_sysid, self.m_compid,
                                 seq=self._mav_next_seq()),
                        "PING #%d" % seqn)

    def _mav_readparam(self):
        if self.mfd is None:
            return
        name = self.ent_param.get().strip()[:16]
        self._mav_write(mav.request_param_read(
            name, self.m_sysid, self.m_compid, seq=self._mav_next_seq()),
            "读参数 %s" % name)

    def _mav_rawcmd(self):
        if self.mfd is None:
            return
        try:
            cmd = int(self.ent_cmdid.get(), 0)
            ps = [float(e.get() or 0) for e in self.ent_cmdp]
        except ValueError as e:
            messagebox.showwarning("格式错误", str(e), parent=self)
            return
        cname = mav.MAV_CMD_NAMES.get(cmd, "命令%d" % cmd)
        self._mav_write(mav.command_long(
            cmd, *ps, target_system=self.m_sysid, target_component=self.m_compid,
            seq=self._mav_next_seq()),
            "COMMAND_LONG %s %s" % (cname,
                                    " ".join("%g" % p for p in ps)))

    def _mav_sniff(self):
        """逐个波特率尝试, 找到能解出 MAVLink 帧的那个(USB 的飞控一般 115200)。"""
        dev = self.cmb_mport.get()
        if not dev:
            return
        if dev in self._open_fds:
            messagebox.showwarning("已被占用",
                                   "%s 已被本工具打开, 请先断开。" % dev, parent=self)
            return
        self.btn_mconn.config(state="disabled")

        def work():
            cands = [115200, 57600, 921600, 38400, 230400, 460800, 19200, 9600]
            found = None
            for b in cands:
                self.log("$ 嗅探 %s @ %d …" % (dev, b))
                try:
                    fd = open_serial(dev, b)
                except OSError as e:
                    self.log("✗ %s: %s" % (b, open_error_msg(e)))
                    break
                p = mav.Parser()
                t0 = time.time()
                n = 0
                while time.time() - t0 < 1.6:
                    r, _, _ = select.select([fd], [], [], 0.2)
                    if r:
                        try:
                            n = len(p.feed(os.read(fd, 8192)))
                        except OSError:
                            break
                        if n:
                            break
                os.close(fd)
                if n:
                    found = b
                    break
            if found:
                self.q.put(("sniffed", found))
            else:
                self.log("✗ 未嗅探到 MAVLink 数据(飞控没接/没上电/波特率特殊?)")
                self.q.put(("sniffed", None))

        threading.Thread(target=work, daemon=True).start()

    # ================= 资料页 =================
    def _build_docs(self):
        f = self.tab_doc
        top = tk.Frame(f, bg=C_BG)
        top.pack(fill="x", padx=8, pady=8)
        tk.Label(top, text="40-pin 排针与串口资料", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")

        box = tk.Frame(f, bg=C_CARD)
        box.pack(fill="x", padx=8, pady=4)
        tk.Label(box, text="串口最常用引脚:", bg=C_CARD, fg=C_FG,
                 font=FONT_B).pack(anchor="w", padx=8, pady=(8, 0))
        pins = [
            ("8",  "UART1_TX(发送)", "接飞控/设备的 RX"),
            ("10", "UART1_RX(接收)", "接飞控/设备的 TX"),
            ("6 或 14", "GND 共地", "必须接!否则乱码/无数据"),
            ("2/4", "5V 电源", "一般不接(飞控自己供电)"),
            ("1/17", "3.3V 电源", "本板串口电平为 3.3V,严禁接 5V 电平设备"),
        ]
        for pin, name, use in pins:
            tk.Label(box, text="  脚 %s  %s   ——  %s" % (pin, name, use),
                     bg=C_CARD, fg=C_SUB, font=FONT_M).pack(anchor="w")
        self.lbl_40stat = tk.Label(box, text="", bg=C_CARD, fg=C_WARN, font=FONT_S)
        self.lbl_40stat.pack(anchor="w", padx=8, pady=4)

        acts = tk.Frame(f, bg=C_BG)
        acts.pack(fill="x", padx=8, pady=6)
        self.btn(acts, "打开完整手册(针脚图/教程/原文链接/示例)", self._open_manual,
                 bg=C_ACC, fg="#ffffff").pack(side="left")
        self.btn(acts, "释放40-pin串口(需密码)", self._act_free40,
                 bg=C_WARN, fg="#000000").pack(side="left", padx=6)
        self.btn(acts, "恢复40-pin为系统默认(需密码)", self._act_restore40
                 ).pack(side="left", padx=6)

        tip = tk.Frame(f, bg=C_CARD)
        tip.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        tk.Label(tip, text="使用要点", bg=C_CARD, fg=C_FG,
                 font=FONT_B).pack(anchor="w", padx=8, pady=(8, 0))
        for line in (
                "1. 资料手册里有: 完整 40 针彩色图、每个串口的针脚定义、一般用法、"
                "原文链接(可点击)和 C/Python/stty 示例。",
                "2. ttyAMA0(40-pin 8/10 脚)出厂被系统当控制台+登录终端占用,"
                "要先点\"释放40-pin串口\"并重启, 飞控才能用。",
                "3. 你现在插在 USB 上的 PX4 飞控就是 ttyACM0, 免 sudo 直接连,"
                "选\"飞控 MAVLink\"页 → ttyACM0 → 115200 → 连接即可看到遥测。",
                "4. 接线口诀: TX→RX 交叉接、GND 必须共地、别接电源脚、"
                "两边都是 3.3V 电平。"):
            tk.Label(tip, text=line, bg=C_CARD, fg=C_SUB, font=FONT_S,
                     wraplength=1100, justify="left").pack(anchor="w", padx=8, pady=2)

    def _act_restore40(self):
        if messagebox.askyesno(
                "恢复 40-pin 串口为系统默认",
                "将撤销全部释放动作: 恢复 extlinux.conf 备份、恢复 serial-getty、"
                "删除 udev 规则。需重启生效。确定继续?", parent=self):
            if self.run_root(["restore-40pin"]):
                self.log("✓ 已恢复 40-pin 串口为系统默认, 重启后生效")

    # ---------------- sudo 动作(仿 5 号工具) ----------------
    def _ask_password(self):
        try:
            p = subprocess.run(["zenity", "--password", "--title=需要管理员密码"],
                               capture_output=True, text=True, timeout=300)
        except Exception as e:
            self.log("✗ 系统密码框启动失败: %s" % e)
            return None
        if p.returncode != 0:
            return None
        return (p.stdout or "").strip()

    def run_root(self, args, timeout=120):
        """sudo -S 执行同目录 serial_actions.sh 子命令(密码仅内存缓存)。"""
        if self._pw is None:
            pw = self._ask_password()
            if pw is None:
                self.log("✗ 未取得管理员密码,操作中止")
                return False
            self._pw = pw
        self.log("$ sudo bash serial_actions.sh " + " ".join(args))
        try:
            p = subprocess.run(["sudo", "-S", "-p", "", "bash", ACTION] + args,
                               input=self._pw + "\n", capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log("✗ 执行超时")
            return False
        out = (p.stdout or "") + (p.stderr or "")
        if "incorrect password" in out.lower():
            self._pw = None
            self.log("✗ 密码错误,请重试")
            return False
        for ln in out.rstrip().splitlines():
            if ln.strip():
                self.log("  " + ln)
        if p.returncode != 0:
            self.log("✗ 退出码 %d" % p.returncode)
            return False
        return True

    # ---------------- 轮询与事件泵 ----------------
    def _poll_loop(self):
        while not self._stop:
            if self.auto_refresh.get():
                try:
                    self.q.put(("snap", collect_snapshot()))
                except Exception as e:
                    self.q.put(("log", "✗ 采集异常: %r" % e))
            time.sleep(REFRESH_S)

    def _pump(self):
        if self._stop:
            return
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "snap":
                    self._apply_snap(item[1])
                elif kind == "log":
                    self.ev.insert("end", "[%s] %s\n"
                                   % (time.strftime("%H:%M:%S"), item[1]))
                    self.ev.see("end")
                elif kind == "prev":
                    dev, data = item[1], item[2]
                    w = self.previews.get(dev)
                    if w:
                        w.append(data)
                        st = self._io_stat.setdefault(dev, dict(rx=0, tx=0))
                        st["rx"] += len(data)
                elif kind == "prevtick":
                    w = self.previews.get(item[1])
                    if w:
                        w.tick()
                elif kind == "term_rx":
                    data = item[1]
                    if not self.tpause.get():
                        if self.tts.get():
                            self.ttxt.insert("end",
                                             "[%s] " % time.strftime("%H:%M:%S.%f")[:-3],
                                             "ts")
                        self.ttxt.insert("end", to_hex(data) + "  "
                                         if self.thex.get() else printable(data), "rx")
                        if float(self.ttxt.index("end-1c").split(".")[0]) > 6000:
                            self.ttxt.delete("1.0", "2000.0")
                        if self.tscroll.get():
                            self.ttxt.see("end")
                    self.lbl_trx.config(text="RX %d | TX %d" % (self.t_rx, self.t_tx))
                elif kind == "term_periodic":
                    self._term_write_raw((from_hex(self.txt_tx.get())
                                          if self.thexsend.get()
                                          else self.txt_tx.get().encode("utf-8")))
                elif kind == "term_periodic_off":
                    self.lbl_tstat2.config(text="")
                elif kind == "term_closed":
                    if getattr(self, "tfd", None) is not None:
                        self.log("⚠ 终端读线程退出(设备可能被拔出)")
                        self._term_disconnect()
                elif kind == "mav":
                    for fr in item[1]:
                        try:
                            self._mav_handle_frame(fr)
                        except Exception as e:
                            self.log("✗ MAVLink 帧处理异常: %r" % e)
                    self._mav_update_labels()
                elif kind == "sniffed":
                    b = item[1]
                    self.btn_mconn.config(state="normal")
                    if b:
                        self.cmb_mbaud.set(str(b))
                        self.log("✓ 嗅探成功: %s 使用 %d 波特" % (self.cmb_mport.get(), b))
                        self._mav_connect()
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _on_close(self):
        self._stop = True
        self._term_stop_periodic()
        for dev in list(self.previews):
            self.previews[dev].close()
        for closer in (self._term_disconnect, self._mav_disconnect):
            try:
                closer()
            except Exception:
                pass
        self.after(150, self.destroy)


# ================= 入口 =================

def main():
    if SMOKE:
        snap = collect_snapshot()
        out = dict(
            ts=snap["ts"],
            console=snap["console"],
            getty=snap["getty"],
            port_count=len(snap["ports"]),
            ports=[dict(dev=p["dev"], kind=p["kind"], hw=p["hw"],
                        driver=p["driver"], group=p["group"], mode=p["mode"],
                        perms=p["perms"], busy_users=p["busy_users"],
                        irq=p["irq"])
                   for p in snap["ports"]],
        )
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if os.geteuid() == 0:
        print("请不要用 root 运行本工具(需要时会主动要密码)。")
        return 1
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
