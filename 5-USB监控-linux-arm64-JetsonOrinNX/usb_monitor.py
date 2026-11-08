#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USB 监控工具 —— Jetson Orin NX USB 实时监控面板。

背景: 本机 USB 经常因供电不足导致卡顿/设备掉线(内核日志可见鼠标口被 hub
     禁用(EMI)、RealSense 掉线等)。本工具实时展示:
  - 设备树: 所有 USB 设备/Hub 层级、协商速率、请求电流(bMaxPower)
  - 供电健康: 板载 INA3221 各通道 电压/电流/功率、欠压告警、每总线 Σ请求电流
  - 设备详情: VID/PID/序列号/驱动/运行时状态/降速与超预算检测
  - 实时传输: USB 存储读写速率(/proc/diskstats) + 深度模式每设备收发速率(usbmon,需root)
  - 摄像头预览: OpenCV 抓帧实时画面 + FPS/吞吐/掉帧统计
  - USB 事件日志: tail /var/log/kern.log 过滤 usb 相关事件并分级着色

只读数据全部来自 sysfs//proc,免 root;深度监控与修改类操作走 sudo -S
调用同目录 actions_usb.sh / usbmon_helper.py(密码仅存内存,不落盘)。
"""
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext

HERE   = os.path.dirname(os.path.abspath(__file__))
ACTION = os.path.join(HERE, "actions_usb.sh")
HELPER = os.path.join(HERE, "usbmon_helper.py")

if not os.environ.get("DISPLAY"):
    os.environ["DISPLAY"] = ":0"
os.environ["PATH"] = ("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                      "/sbin:/bin:" + os.environ.get("PATH", ""))

SMOKE = os.environ.get("AAAFIX_SMOKE")  # 自动化冒烟测试: 采一轮快照打JSON退出
REFRESH_S = 1.0

# ---------------- 配色(深色,与 1/2 号工具一致) ----------------
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
FONT_S  = ("Noto Sans CJK SC", 10)
FONT_M  = ("Noto Sans Mono CJK SC", 10)

USB2_MA, USB3_MA = 500, 900        # 单口供电预算(USB 规格)
VBUS_LOW_WARN, VBUS_LOW_BAD = 4.75, 4.60   # 5V 欠压阈值(V)

USB_SYS = "/sys/bus/usb/devices"

# kern.log 里关心的 USB 事件
USB_EVT_RE = re.compile(
    r"\b(usb|xhci|hub)\b|over-current|uvcvideo|\buas\b|usb-storage|tegra-xusb",
    re.IGNORECASE)

IFACE_CLASS = {
    "00": "设备特定", "01": "音频", "02": "CDC控制", "03": "HID人机",
    "06": "图像", "07": "打印机", "08": "大容量存储", "09": "集线器",
    "0a": "CDC数据", "0e": "视频(UVC)", "e0": "无线蓝牙", "ff": "厂商自定义",
}


def rd(path, name=None):
    """读 sysfs 小文件,失败返回空串。"""
    try:
        with open(os.path.join(path, name) if name else path, "r",
                  errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def scan_usb():
    """扫描 /sys/bus/usb/devices,返回 {name: dev_dict}。"""
    devs = {}
    try:
        names = sorted(os.listdir(USB_SYS))
    except Exception:
        return devs
    for name in names:
        if ":" in name:          # 接口目录,单独扫
            continue
        p = os.path.join(USB_SYS, name)
        d = {
            "name": name, "path": p, "children": [], "ifaces": [],
            "is_root": name.startswith("usb"),
            "vid": rd(p, "idVendor"), "pid": rd(p, "idProduct"),
            "product": rd(p, "product"), "manufacturer": rd(p, "manufacturer"),
            "serial": rd(p, "serial"), "version": rd(p, "version"),
            "speed": rd(p, "speed"), "maxchild": rd(p, "maxchild"),
            "removable": rd(p, "removable"),
            "authorized": rd(p, "authorized"),
            "cfg": rd(p, "bConfigurationValue"),
            "busnum": rd(p, "busnum"), "devnum": rd(p, "devnum"),
            "devpath": rd(p, "devpath"),
            "runtime": rd(p, "power/runtime_status"),
            "control": rd(p, "power/control"),
        }
        mp = rd(p, "bMaxPower")  # "720mA"
        m = re.match(r"(\d+)", mp)
        d["maxpower"] = int(m.group(1)) if m else 0
        try:
            d["speed_i"] = int(float(d["speed"]))
        except Exception:
            d["speed_i"] = 0
        devs[name] = d

    for n, d in devs.items():
        if d["is_root"]:
            d["parent"] = ""
        elif "." in n:
            d["parent"] = n.rsplit(".", 1)[0]
        else:
            d["parent"] = "usb" + d["busnum"] if d["busnum"] else ""
    for n, d in devs.items():
        par = d.get("parent", "")
        if par in devs:
            devs[par]["children"].append(n)

    for n, d in devs.items():
        # 接口与其驱动(接口子目录名是完整名,如 "1-2.1:1.0")
        for ifdir in sorted(glob.glob(os.path.join(d["path"],
                                                   d["name"] + ":*"))):
            ifname = os.path.basename(ifdir)
            drv = os.path.basename(os.path.realpath(
                os.path.join(ifdir, "driver")))
            cls = rd(ifdir, "bInterfaceClass").lower()
            if cls.startswith("0x"):
                cls = cls[2:]
            d["ifaces"].append({
                "name": ifname, "driver": drv if drv != ifname else "(无)",
                "cls": IFACE_CLASS.get(cls, cls or "?"),
            })
        # 大容量存储对应的块设备
        d["block"] = usb_block_dev(d["path"], d["name"])

    return devs


def usb_block_dev(path, name):
    """USB 大容量存储 -> 块设备名(如 sda),无则空串。"""
    for ip in sorted(glob.glob(os.path.join(path, name + ":[0-9]*"))):
        for b in glob.glob(os.path.join(ip, "host*", "target*", "*", "block", "*")):
            return os.path.basename(b)
    return ""


def disk_sectors():
    """读 /proc/diskstats -> {devname: (读扇区, 写扇区)}。"""
    out = {}
    try:
        with open("/proc/diskstats", "r") as f:
            for ln in f:
                fs = ln.split()
                if len(fs) > 9:
                    out[fs[2]] = (int(fs[5]), int(fs[9]))
    except Exception:
        pass
    return out


def is_degraded(d):
    """USB3.x 描述符设备却协商在 <5Gbps => 降速(2.x 设备无法判断,不算)。"""
    if d["is_root"] or not d["version"]:
        return False
    try:
        return float(d["version"]) >= 3.0 and d["speed_i"] < 5000
    except Exception:
        return False


def budget_of(d):
    return USB3_MA if d["speed_i"] >= 5000 else USB2_MA


def over_budget(d):
    return (not d["is_root"]) and d["maxpower"] > budget_of(d)


# ---------------- 供电传感器 (INA3221) ----------------

def find_ina3221():
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if rd(h, "name") == "ina3221":
            return h
    return ""


def _int_or_none(s):
    try:
        return int(s)
    except Exception:
        return None


def _jtop_rails(raw):
    """jetson-stats 4.x jtop.power 结构:
      {"rail": {名: {volt(mV), curr(mA), power(mW), warn, crit, avg,...}},
       "tot":  {同上, name:"VDD_IN"}}
    返回统一 rails 列表;结构对不上时返回空。"""
    rails = []
    if not isinstance(raw, dict):
        return rails
    tot = raw.get("tot")
    if isinstance(tot, dict) and tot.get("power") is not None:
        rails.append({
            "label": "%s·总输入" % (tot.get("name") or "VDD_IN"),
            "v": (tot.get("volt") or 0) / 1000.0,
            "a": (tot.get("curr") or 0) / 1000.0,
            "power_mW": tot.get("power"),
            "warn": tot.get("warn"), "crit": tot.get("crit"),
        })
    for name, r in (raw.get("rail") or {}).items():
        if isinstance(r, dict) and r.get("power") is not None:
            rails.append({
                "label": name,
                "v": (r.get("volt") or 0) / 1000.0,
                "a": (r.get("curr") or 0) / 1000.0,
                "power_mW": r.get("power"),
                "warn": r.get("warn"), "crit": r.get("crit"),
            })
    return rails


def _flatten_jtop_power(raw):
    """兜底: 老版本 jtop.power 结构宽容拍平,只剩功率(mW)。"""
    rails, total = [], None
    if not isinstance(raw, dict):
        return rails, total
    for k, v in raw.items():
        if k == "power":          # 3.x 的外层包裹
            r2, t2 = _flatten_jtop_power(v)
            rails += r2
            total = t2 if total is None else total
            continue
        if isinstance(v, dict):
            pw = v.get("power", v.get("mW"))
            if isinstance(pw, (int, float)):
                if k == "total":
                    total = pw
                else:
                    rails.append({"label": k, "power_mW": pw})
        elif isinstance(v, (int, float)):
            if k == "total":
                total = v
            else:
                rails.append({"label": str(k), "power_mW": v})
    return rails, total


def read_power():
    """读取电源轨。返回 (source, rails, need_fix)
    source: 'hwmon' / 'jtop' / None; rail 统一为:
      {label, v:V|None, a:A|None, power_mW|None, warn:mA|None, crit:mA|None}"""
    h = find_ina3221()
    if h:
        rails = []
        ok = False
        for ch in (1, 2, 3):
            label = rd(h, "in%d_label" % ch) or ("通道%d" % ch)
            mv, ma = rd(h, "in%d_input" % ch), rd(h, "curr%d_input" % ch)
            if mv == "" and ma == "":
                continue
            ok = True
            rails.append({
                "label": label,
                "v": (int(mv) / 1000.0) if mv else None,
                "a": (int(ma) / 1000.0) if ma else None,
                "power_mW": None,
                "warn": _int_or_none(rd(h, "curr%d_max" % ch)),
                "crit": _int_or_none(rd(h, "curr%d_crit" % ch)),
            })
        if ok:
            return "hwmon", rails, False

    # 兜底: jetson-stats 的 root 守护服务(jsz 在 jtop 组,免 root 可用)
    try:
        from jtop import jtop  # noqa
        with jtop() as jt:
            raw = jt.power
        rails = _jtop_rails(raw)
        if not rails:
            fl, total = _flatten_jtop_power(raw)
            if total is not None:
                fl = [r for r in fl if r["label"] != "total"]
                fl.insert(0, {"label": "总输入", "power_mW": total})
            rails = fl
        if rails:
            return "jtop", rails, False
    except Exception:
        pass
    return None, [], True


def nvpmodel_info():
    cur = ""
    s = rd("/var/lib/nvpmodel/status")  # "pmode:0000"
    m = re.search(r"pmode:(\w+)", s)
    if m:
        try:
            cur = str(int(m.group(1), 16))
        except Exception:
            cur = ""
    names = {}
    try:
        with open("/etc/nvpmodel.conf", "r", errors="replace") as f:
            txt = f.read()
        for m in re.finditer(r"<\s*POWER_MODEL\s+ID=(\d+)\s+NAME=(\S+)\s*>", txt):
            names[m.group(1)] = m.group(2)
    except Exception:
        pass
    if not names:
        names = {"0": "MAXN", "1": "10W", "2": "15W", "3": "25W"}
    return cur, names


def collect_snapshot():
    """采一轮完整快照(供 GUI 轮询线程与 SMOKE 共用)。"""
    devs = scan_usb()
    src, rails, need_fix = read_power()
    pmode, pnames = nvpmodel_info()
    disk = disk_sectors()

    warnings = []
    for n, d in sorted(devs.items()):
        if over_budget(d):
            warnings.append("⚠ %s 请求 %dmA 超过当前速率口预算 %dmA(多因降速后仍高耗电)"
                            % (n, d["maxpower"], budget_of(d)))
        if is_degraded(d):
            warnings.append("⚠ %s 是 USB%s 设备但只协商在 %sMbps(降速)"
                            % (n, d["version"], d["speed"]))
        if d["authorized"] == "0":
            warnings.append("⚠ %s 未被系统授权(authorized=0)" % n)
    for r in rails:
        v, a = r.get("v"), r.get("a")
        # 电流超阈值(jtop/hwmon 都带 mA 级 warn/crit)
        if a is not None and r.get("crit") and a * 1000 >= r["crit"]:
            warnings.append("⚡ 供电轨 %s 电流 %.2fA 已达临界值 %.2fA!"
                            % (r["label"], a, r["crit"] / 1000.0))
        elif a is not None and r.get("warn") and a * 1000 >= r["warn"]:
            warnings.append("⚡ 供电轨 %s 电流 %.2fA 超过告警值 %.2fA"
                            % (r["label"], a, r["warn"] / 1000.0))
        # 电压过低
        if v is not None and re.search(r"5V|VIN|IN_5", r["label"], re.I):
            if v < VBUS_LOW_BAD:
                warnings.append("⚡ 供电轨 %s 电压 %.2fV 严重偏低(<%.2fV),USB 供电不足!"
                                % (r["label"], v, VBUS_LOW_BAD))
            elif v < VBUS_LOW_WARN:
                warnings.append("⚡ 供电轨 %s 电压 %.2fV 偏低(<%.2fV)"
                                % (r["label"], v, VBUS_LOW_WARN))
        elif v is not None and "总输入" in r["label"] and v < 9.0:
            warnings.append("⚡ 整板输入电压 %.2fV 过低(<9.0V),超出 Orin NX 供电规格!"
                            % v)

    return {
        "t": time.time(), "devices": devs, "power_src": src,
        "rails": rails, "need_fix": need_fix,
        "nvpmodel": pmode, "nvpmodel_names": pnames,
        "disk": disk, "warnings": warnings,
    }


def spd_str(speed_i):
    if speed_i >= 10000:
        return "10G"
    if speed_i >= 5000:
        return "5G"
    if speed_i == 1:
        return "1.5M"
    return "%dM" % speed_i if speed_i else "?"


def dev_label(d):
    return d["product"] or d["name"]


def classify_evt(line):
    low = line.lower()
    if "over-current" in low:
        return "bad"
    if ("disconnect" in low or "unable to enumerate" in low or
            "device descriptor read" in low or "cannot enable" in low or
            "no reply from device" in low):
        return "bad"
    if ("disabled by hub" in low or "reset" in low or
            "device not accepting address" in low or "port status" in low):
        return "warn"
    return "info"


# ================= GUI =================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("USB 监控 · Jetson Orin NX")
        self.configure(bg=C_BG)
        self.geometry("1220x800")
        self.minsize(1020, 660)
        self._center()
        self.q = queue.Queue()
        self._pw = None
        self._stop = False
        self._sel = ""                  # 当前选中设备(树 iid)
        self._snap = None               # 最近一轮快照
        self._tree_iids = set()
        self._bd2name = {}              # (busnum, devnum) -> name
        self._prev_disk = None          # (t, {dev:(r,w)})
        self._disk_rate = {}            # {dev: "12.3 / 0.4 MB/s"}
        self._deep_prev = None          # (t, {(bus,dev):(rx,tx)})
        self._deep_rate = {}            # {(bus,dev): (rx_bps, tx_bps)}
        self._deep_proc = None
        self._deep_on = False
        self._cam_gen = 0               # 摄像头线程代次
        self._cam_on = False
        self._cam_idx = None
        self._cam_img = None
        self._lf = None                 # kern.log 文件句柄
        self._lf_hist_done = False
        self._lfpos = 0

        self._build_style()
        self._build_ui()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._log_loop, daemon=True).start()
        self.after(80, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _center(self):
        self.update_idletasks()
        w, h = 1220, 800
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 4)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ---------------- UI 构建 ----------------
    def _build_style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TCombobox", fieldbackground=C_INPUT, background=C_CARD2,
                    foreground=C_FG, arrowcolor=C_FG, bordercolor=C_LINE,
                    lightcolor=C_CARD2, darkcolor=C_CARD2, padding=6, font=FONT)
        s.map("TCombobox",
              fieldbackground=[("readonly", C_INPUT)],
              foreground=[("readonly", C_FG)])
        self.option_add("*TCombobox*Listbox.background", C_CARD2)
        self.option_add("*TCombobox*Listbox.foreground", C_FG)
        self.option_add("*TCombobox*Listbox.selectBackground", C_ACC)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", FONT)
        s.configure("TNotebook", background=C_BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        s.configure("TNotebook.Tab", background=C_CARD, foreground=C_SUB,
                    padding=(16, 7), font=FONT, borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", C_CARD2)],
              foreground=[("selected", C_FG)])
        s.configure("Aaa.Treeview", background=C_CARD, fieldbackground=C_CARD,
                    foreground=C_FG, rowheight=27, font=FONT, borderwidth=0,
                    relief="flat")
        s.map("Aaa.Treeview",
              background=[("selected", C_ACC)],
              foreground=[("selected", "#ffffff")])
        s.configure("Aaa.Treeview.Heading", background=C_CARD2,
                    foreground=C_SUB, relief="flat", font=FONT_S)

    def _card(self, parent, title):
        card = tk.Frame(parent, bg=C_CARD, highlightbackground=C_LINE,
                        highlightthickness=1)
        head = tk.Frame(card, bg=C_CARD)
        head.pack(fill="x", padx=14, pady=(9, 2))
        tk.Label(head, text=title, bg=C_CARD, fg=C_FG, font=FONT_B,
                 anchor="w").pack(side="left")
        return card

    def _btn(self, parent, text, cmd, primary=False, small=False):
        bg = C_ACC if primary else C_CARD2
        fg = "#ffffff" if primary else C_FG
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=C_ACC if primary else C_LINE,
                      activeforeground="#ffffff", relief="flat",
                      font=FONT_S if small else FONT_B, cursor="hand2",
                      padx=10, pady=6 if small else 8, justify="center")
        return b

    def _build_ui(self):
        # 头部
        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", padx=18, pady=(12, 2))
        tk.Label(head, text="▣ USB 监控", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")
        tk.Label(head, text="  供电不足 / 掉线 / 降速 实时监控",
                 bg=C_BG, fg=C_SUB, font=FONT_S).pack(side="left", pady=(8, 0))
        self.lb_pwr = tk.Label(head, text="", bg=C_BG, fg=C_SUB, font=FONT_B)
        self.lb_pwr.pack(side="right")
        self.lb_mode = tk.Label(head, text="", bg=C_BG, fg=C_SUB, font=FONT)
        self.lb_mode.pack(side="right", padx=(0, 18))
        self.dot = tk.Label(head, text="● 采集中...", bg=C_BG, fg=C_WARN,
                            font=FONT_B)
        self.dot.pack(side="right", padx=(0, 18))

        # 中部: 左树+电源, 右 Notebook
        mid = tk.Frame(self, bg=C_BG)
        mid.pack(fill="both", expand=True, padx=18, pady=(6, 2))

        # ---- 左列 ----
        left = tk.Frame(mid, bg=C_BG, width=400)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        c_tree = self._card(left, "USB 设备树 (速率 · 请求电流)")
        c_tree.pack(fill="both", expand=True, pady=(0, 8))
        tf = tk.Frame(c_tree, bg=C_CARD)
        tf.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.tree = ttk.Treeview(tf, style="Aaa.Treeview", show="tree",
                                 selectmode="browse")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("warn", foreground=C_WARN)
        self.tree.tag_configure("bad", foreground=C_BAD)
        self.tree.tag_configure("sus", foreground="#5b6b80")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_sel)

        c_pwr = self._card(left, "供电健康 (INA3221)")
        c_pwr.pack(fill="x")
        self.power_body = tk.Frame(c_pwr, bg=C_CARD)
        self.power_body.pack(fill="x", padx=14, pady=(2, 10))
        self._build_power_card()

        # ---- 右列 Notebook ----
        right = tk.Frame(mid, bg=C_BG)
        right.pack(side="left", fill="both", expand=True)
        self.nb = ttk.Notebook(right, style="TNotebook")
        self.nb.pack(fill="both", expand=True)

        # Tab1 设备详情
        tab1 = tk.Frame(self.nb, bg=C_CARD)
        self.nb.add(tab1, text="  设备详情  ")
        self.detail_body = tk.Frame(tab1, bg=C_CARD)
        self.detail_body.pack(fill="both", expand=True, padx=16, pady=12)
        self._build_detail()

        # Tab2 摄像头预览
        tab2 = tk.Frame(self.nb, bg=C_CARD)
        self.nb.add(tab2, text="  摄像头预览  ")
        bar = tk.Frame(tab2, bg=C_CARD)
        bar.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(bar, text="设备:", bg=C_CARD, fg=C_SUB,
                 font=FONT).pack(side="left")
        self.cmb_cam = ttk.Combobox(bar, state="readonly", width=46, font=FONT_S)
        self.cmb_cam.pack(side="left", padx=(6, 8))
        self._btn(bar, "↻", self._cam_refresh_list, small=True).pack(side="left")
        self.btn_cam = self._btn(bar, "开始预览", self._cam_toggle, primary=True)
        self.btn_cam.pack(side="left", padx=10)
        self.lb_cam = tk.Label(tab2, text="未开始 · 先选 /dev/video 节点",
                               bg=C_CARD, fg=C_SUB, font=FONT_S)
        self.lb_cam.pack(anchor="w", padx=16)
        self.cam_canvas = tk.Canvas(tab2, bg=C_INPUT, height=430,
                                    highlightthickness=0)
        self.cam_canvas.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        self.cam_canvas.create_text(
            10, 10, anchor="nw", text="", fill=C_SUB, font=FONT,
            tags=("camhint",))
        self._cam_refresh_list()

        # Tab3 事件日志
        tab3 = tk.Frame(self.nb, bg=C_CARD)
        self.nb.add(tab3, text="  USB 事件日志  ")
        tip = tk.Label(tab3, text="来源 /var/log/kern.log 实时跟踪 + 启动时回看历史  ·  "
                       "红=掉线/过流  橙=复位/端口禁用",
                       bg=C_CARD, fg=C_SUB, font=FONT_S)
        tip.pack(anchor="w", padx=16, pady=(10, 2))
        self.log = scrolledtext.ScrolledText(
            tab3, bg=C_INPUT, fg=C_FG, insertbackground=C_FG, font=FONT_M,
            relief="flat", state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=16, pady=(4, 14))
        self.log.tag_configure("bad", foreground=C_BAD)
        self.log.tag_configure("warn", foreground=C_WARN)
        self.log.tag_configure("info", foreground=C_OK)

        # 底部快捷操作
        bot = tk.Frame(self, bg=C_BG)
        bot.pack(fill="x", padx=18, pady=(6, 12))
        tk.Label(bot, text="快捷操作:", bg=C_BG, fg=C_SUB,
                 font=FONT_S).pack(side="left")
        self._btn(bot, "INA3221读取修复", self._act_ina, small=True).pack(
            side="left", padx=(8, 6))
        self._btn(bot, "启用jtop服务", self._act_jtop, small=True).pack(
            side="left", padx=6)
        self._btn(bot, "全部禁止USB休眠", self._act_as_off, small=True).pack(
            side="left", padx=6)
        self._btn(bot, "恢复自动休眠", self._act_as_auto, small=True).pack(
            side="left", padx=6)
        self.btn_usbmon = self._btn(bot, "启用usbmon模块", self._act_usbmon,
                                    small=True)
        self.btn_usbmon.pack(side="left", padx=6)
        tk.Frame(bot, bg=C_BG, width=14).pack(side="left")
        tk.Label(bot, text="深度模式(usbmon·需root):", bg=C_BG, fg=C_SUB,
                 font=FONT_S).pack(side="left")
        self.lb_deep = tk.Label(bot, text="关", bg=C_BG, fg=C_SUB, font=FONT_B)
        self.lb_deep.pack(side="left", padx=(6, 4))
        self.btn_deep = self._btn(bot, "开启", self._deep_toggle, small=True)
        self.btn_deep.pack(side="left")

    # ---------------- 电源卡片 ----------------
    def _build_power_card(self):
        self.lb_psrc = tk.Label(self.power_body, text="INA3221: 检测中...",
                                bg=C_CARD, fg=C_SUB, font=FONT_S, anchor="w")
        self.lb_psrc.pack(fill="x")
        self.btn_fix = self._btn(self.power_body, "修复读取权限(需root)",
                                 self._act_ina, small=True)
        self.rails_frame = tk.Frame(self.power_body, bg=C_CARD)
        self.rails_frame.pack(fill="x", pady=(4, 2))
        self.rail_rows = []
        tk.Frame(self.power_body, bg=C_LINE, height=1).pack(fill="x", pady=6)
        self.lb_bus = tk.Label(self.power_body, text="", bg=C_CARD, fg=C_FG,
                               font=FONT_S, anchor="w", justify="left")
        self.lb_bus.pack(fill="x")
        self.lb_over = tk.Label(self.power_body, text="", bg=C_CARD, fg=C_WARN,
                                font=FONT_S, anchor="w", justify="left")
        self.lb_over.pack(fill="x")

        row = tk.Frame(self.power_body, bg=C_CARD)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="功耗模式:", bg=C_CARD, fg=C_SUB,
                 font=FONT_S).pack(side="left")
        self.cmb_pm = ttk.Combobox(row, state="readonly", width=12, font=FONT_S)
        self.cmb_pm.pack(side="left", padx=(6, 6))
        self._btn(row, "切换", self._act_powermode, small=True).pack(side="left")

    def _update_rails(self, src, rails, need_fix):
        if src == "hwmon":
            self.lb_psrc.configure(text="INA3221: hwmon 直读 ✓ (V/A/W 全量)",
                                   fg=C_OK)
            self.btn_fix.pack_forget()
        elif src == "jtop" and rails and rails[0].get("a") is not None:
            self.lb_psrc.configure(text="INA3221: 经 jtop 服务读取 ✓ (V/A/W 全量)",
                                   fg=C_OK)
            self.btn_fix.pack_forget()
        elif src == "jtop":
            self.lb_psrc.configure(
                text="INA3221: 经 jtop 服务(仅功率W) — 想看电压/电流请点\"INA3221读取修复\"",
                fg=C_WARN)
            self.btn_fix.pack(anchor="w", pady=(2, 0))
        else:
            self.lb_psrc.configure(
                text="INA3221: 数值文件无读取权限 ✗ (板载传感器存在)",
                fg=C_BAD)
            self.btn_fix.pack(anchor="w", pady=(2, 0))

        # 轨道行重建(数量一般固定为3-4)
        for w in self.rails_frame.winfo_children():
            w.destroy()
        self.rail_rows = []
        for r in rails:
            row = tk.Frame(self.rails_frame, bg=C_CARD)
            row.pack(fill="x")
            nm = tk.Label(row, text=r["label"], bg=C_CARD, fg=C_FG,
                          font=FONT_S, width=16, anchor="w")
            nm.pack(side="left")
            val = tk.Label(row, text="", bg=C_CARD, fg=C_SUB, font=FONT_M,
                           anchor="w")
            val.pack(side="left")
            self.rail_rows.append((r["label"], val))

    # ---------------- 详情页 ----------------
    def _build_detail(self):
        self._dv = {}
        rows = [
            ("设备", "name"), ("厂商", "manufacturer"), ("VID:PID", "vidpid"),
            ("序列号", "serial"), ("USB 版本", "version"),
            ("协商速率", "speed"), ("请求电流", "maxpower"),
            ("运行状态", "runtime"), ("电源策略", "control"),
            ("授权", "authorized"), ("所在端口", "port"),
            ("降速检测", "degraded"), ("实时收发", "deep"),
            ("存储读写", "disk"), ("接口/驱动", "ifaces"),
        ]
        grid = tk.Frame(self.detail_body, bg=C_CARD)
        grid.pack(fill="x")
        for i, (label, key) in enumerate(rows):
            r = i // 2
            c = (i % 2) * 2
            tk.Label(grid, text=label, bg=C_CARD, fg=C_SUB, font=FONT_S,
                     width=10, anchor="w").grid(row=r, column=c,
                                                sticky="nw", padx=(0, 6),
                                                pady=3)
            v = tk.Label(grid, text="—", bg=C_CARD, fg=C_FG, font=FONT_M,
                         anchor="w", justify="left", wraplength=430)
            v.grid(row=r, column=c + 1, sticky="nw", pady=3)
            grid.columnconfigure(c + 1, weight=1)
            self._dv[key] = v

        bf = tk.Frame(self.detail_body, bg=C_CARD)
        bf.pack(fill="x", pady=(14, 4))
        self._btn(bf, "重新枚举此设备", self._act_reenum,
                  small=True).pack(side="left")
        self._btn(bf, "禁止此设备休眠", self._act_dev_on,
                  small=True).pack(side="left", padx=8)
        self._btn(bf, "允许自动休眠", self._act_dev_auto,
                  small=True).pack(side="left")
        tk.Label(self.detail_body,
                 text="提示: bMaxPower 是设备向总线声明的电流上限,并非实时消耗;"
                      "实时电流看左侧 INA3221。\"请求电流>口预算\"多发生于"
                      "USB3 设备降速到 USB2 口(900mA 限 500mA)后,是供电不足的典型诱因。",
                 bg=C_CARD, fg=C_SUB, font=FONT_S, wraplength=880,
                 justify="left", anchor="w").pack(fill="x", pady=(10, 0))

    # ---------------- 日志/状态 ----------------
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "snap":
                    self._on_snap(payload)
                elif kind == "evt":
                    self._on_evt(payload)
                elif kind == "cam":
                    self._on_cam(payload)
                elif kind == "deep":
                    self._on_deep(payload)
                elif kind == "deepdead":
                    self._on_deepdead(payload)
                elif kind == "log":
                    self._log_append(payload)
        except queue.Empty:
            pass
        if not self._stop:
            self.after(80, self._pump)

    def _log_append(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 900:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def log_line(self, s=""):
        self.q.put(("log", s))

    # ---------------- 快照 -> UI ----------------
    def _on_snap(self, snap):
        self._snap = snap
        devs = snap["devices"]
        self._bd2name = {(d["busnum"], d["devnum"]): n
                         for n, d in devs.items() if not d["is_root"]}
        self._update_tree(devs)
        self._update_power(snap)
        self._update_disk_rate(snap)
        self._update_header(snap)
        self._update_detail()
        self._update_cam_device_status(snap)

    def _update_header(self, snap):
        if snap["warnings"]:
            n = len(snap["warnings"])
            self.dot.configure(text="● %d 项告警" % n, fg=C_BAD)
        else:
            self.dot.configure(text="● 正常", fg=C_OK)
        pwr = ""
        for r in snap["rails"]:
            if re.search(r"VDD.*IN|VIN|总输入|IN_5|5V", r["label"], re.I):
                parts = []
                pw = r.get("power_mW")
                if pw is None and r.get("v") and r.get("a"):
                    pw = r["v"] * r["a"] * 1000.0
                if pw:
                    parts.append("%.1fW" % (pw / 1000.0))
                if r.get("a"):
                    parts.append("%.2fA" % r["a"])
                if parts:
                    pwr = " ".join(parts)
                    break
        self.lb_pwr.configure(text=("系统供电: " + pwr) if pwr else "")
        cur, names = snap["nvpmodel"], snap["nvpmodel_names"]
        self.lb_mode.configure(text="nvpmodel: %s" % names.get(cur, cur or "?"))
        vals = [("%s · %s" % (k, v)) for k, v in sorted(
            names.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 99)]
        self.cmb_pm.configure(values=vals)
        if cur and not self.cmb_pm.get():
            self.cmb_pm.set("%s · %s" % (cur, names.get(cur, "")))

    def _dev_row_text(self, d):
        ma = "%dmA" % d["maxpower"] if d["maxpower"] else "0mA"
        txt = "%s   %s · %s" % (dev_label(d), spd_str(d["speed_i"]), ma)
        if d["is_root"]:
            txt += " · %d口" % int(d["maxchild"] or 0)
        badge = ""
        if over_budget(d) or is_degraded(d) or d["authorized"] == "0":
            badge = "  ⚠"
        if d["runtime"] == "suspended":
            badge += " Zz"
        return txt + badge

    def _dev_tag(self, d):
        if over_budget(d) or d["authorized"] == "0":
            return "bad"
        if is_degraded(d):
            return "warn"
        if d["runtime"] == "suspended":
            return "sus"
        return ""

    def _update_tree(self, devs):
        def walk(name, parent_iid):
            d = devs[name]
            if name not in self._tree_iids:
                self.tree.insert(parent_iid, "end", iid=name,
                                 text=self._dev_row_text(d), open=True)
                self._tree_iids.add(name)
            else:
                self.tree.item(name, text=self._dev_row_text(d),
                               tags=(self._dev_tag(d),))
            for ch in sorted(d["children"]):
                walk(ch, name)

        for root in sorted([n for n, d in devs.items() if d["is_root"]]):
            walk(root, "")
        for iid in list(self._tree_iids):
            if iid not in devs:
                self.tree.delete(iid)
                self._tree_iids.discard(iid)
                if self._sel == iid:
                    self._sel = ""

    def _on_tree_sel(self, _e=None):
        sel = self.tree.selection()
        self._sel = sel[0] if sel else ""
        self._update_detail()

    def _update_power(self, snap):
        self._update_rails(snap["power_src"], snap["rails"], snap["need_fix"])
        vals = {lb: v for lb, v in self.rail_rows}
        for r in snap["rails"]:
            w = vals.get(r["label"])
            if not w:
                continue
            v, a, pw = r.get("v"), r.get("a"), r.get("power_mW")
            if pw is None and v is not None and a is not None:
                pw = v * a * 1000.0        # hwmon 无功率字段时按 V×A 补算
            if v is not None or a is not None:
                txt = "%s V  %s A  %s W" % (
                    ("%.2f" % v) if v is not None else "—",
                    ("%.3f" % a) if a is not None else "—",
                    ("%.2f" % (pw / 1000.0)) if pw is not None else "—")
                col = C_FG
                if a is not None and r.get("crit") and a * 1000 >= r["crit"]:
                    col = C_BAD
                elif a is not None and r.get("warn") and a * 1000 >= r["warn"]:
                    col = C_WARN
                elif (v is not None and re.search(r"5V|VIN", r["label"], re.I)
                        and v < VBUS_LOW_WARN):
                    col = C_BAD if v < VBUS_LOW_BAD else C_WARN
                elif v is not None and "总输入" in r["label"] and v < 9.0:
                    col = C_BAD
            elif pw is not None:
                watt = pw / 1000.0
                # 拿不到电压/电流时按 5V 粗略折算电流,供直观感受
                txt = "%.2f W   (~%.2f A @5V)" % (watt, watt / 5.0)
                col = C_FG
            else:
                txt, col = "—", C_SUB
            w.configure(text=txt, fg=col)

        bus_sum, bus_ports = {}, {}
        for n, d in snap["devices"].items():
            if d["is_root"]:
                bus_ports[d["busnum"]] = int(d["maxchild"] or 0)
            elif d["busnum"]:
                bus_sum[d["busnum"]] = bus_sum.get(d["busnum"], 0) + d["maxpower"]
        lines = []
        for b in sorted(bus_sum, key=lambda x: int(x) if x else 0):
            lines.append("Bus%s  Σ设备请求 %d mA  (根口数 %d)" %
                         (b, bus_sum[b], bus_ports.get(b, 0)))
        self.lb_bus.configure(text="\n".join(lines) or "无设备")

        over = [w for w in snap["warnings"] if "超" in w and "预算" in w]
        oth = [w for w in snap["warnings"]
               if w not in over and ("降速" in w or "未授权" in w)]
        self.lb_over.configure(
            text=("\n".join(over + oth)) if (over or oth) else "无降速/超预算设备 ✓",
            fg=C_WARN if (over or oth) else C_SUB)

    def _update_disk_rate(self, snap):
        now, cur = snap["t"], snap["disk"]
        if self._prev_disk:
            t0, d0 = self._prev_disk
            dt = now - t0
            if dt > 0.2:
                for dev, (r1, w1) in cur.items():
                    if dev in d0:
                        r0, w0 = d0[dev]
                        self._disk_rate[dev] = (
                            "%.1f / %.1f MB/s" % (
                                (r1 - r0) * 512.0 / dt / 1048576.0,
                                (w1 - w0) * 512.0 / dt / 1048576.0))
        self._prev_disk = (now, dict(cur))

    def _update_detail(self):
        d = (self._snap or {}).get("devices", {}).get(self._sel)
        if not d:
            for v in self._dv.values():
                v.configure(text="—  (左侧选择设备)")
            return
        sp = spd_str(d["speed_i"])
        self._dv["name"].configure(text="%s (%s)" % (dev_label(d), d["name"]))
        self._dv["manufacturer"].configure(text=d["manufacturer"] or "—")
        self._dv["vidpid"].configure(
            text="%s:%s" % (d["vid"] or "—", d["pid"] or "—"))
        self._dv["serial"].configure(text=d["serial"] or "—")
        self._dv["version"].configure(text="USB " + (d["version"] or "—"))
        self._dv["speed"].configure(
            text="%s Mbps%s" % (d["speed"] or "?",
                                "   [⚠降速]" if is_degraded(d) else ""))
        bud = budget_of(d)
        mt = "%d mA" % d["maxpower"]
        if over_budget(d):
            mt += "   [⚠超过 %dmA 口预算]" % bud
        else:
            mt += "   (口预算 %dmA)" % bud
        self._dv["maxpower"].configure(
            text=mt, fg=C_BAD if over_budget(d) else C_FG)
        rt = d["runtime"] or "?"
        self._dv["runtime"].configure(
            text=rt + ("   Zz 已自动挂起" if rt == "suspended" else ""),
            fg=C_SUB if rt == "suspended" else C_OK)
        self._dv["control"].configure(
            text="%s (%s)" % (d["control"] or "?",
                              "系统会自动休眠它" if d["control"] == "auto"
                              else "常供电不休眠" if d["control"] == "on" else ""))
        self._dv["authorized"].configure(
            text="是" if d["authorized"] == "1" else d["authorized"] or "—",
            fg=C_BAD if d["authorized"] == "0" else C_FG)
        self._dv["port"].configure(
            text="Bus%s Port%s  (dev %s)" % (d["busnum"] or "?",
                                             d["devpath"] or d["name"],
                                             d["devnum"] or "?"))
        self._dv["degraded"].configure(
            text="⚠ 降速(USB%s 设备跑在 %s)" % (d["version"], sp)
            if is_degraded(d) else "正常",
            fg=C_WARN if is_degraded(d) else C_OK)

        # 实时收发(深度模式)
        key = (d["busnum"], d["devnum"])
        rate = self._deep_rate.get(key)
        if rate and self._deep_on:
            self._dv["deep"].configure(
                text="RX %.1f KB/s  ·  TX %.1f KB/s" %
                     (rate[0] / 1024.0, rate[1] / 1024.0), fg=C_FG)
        else:
            self._dv["deep"].configure(
                text="未开启 (底部\"深度模式\")", fg=C_SUB)

        # 存储读写
        if d["block"]:
            self._dv["disk"].configure(
                text="%s  读/写 %s" % (d["block"],
                                       self._disk_rate.get(d["block"],
                                                           "统计中...")),
                fg=C_FG)
        else:
            self._dv["disk"].configure(text="非存储设备", fg=C_SUB)

        if d["ifaces"]:
            self._dv["ifaces"].configure(text="\n".join(
                "  %s  %-12s %s" % (i["name"], i["cls"], i["driver"])
                for i in d["ifaces"]))
        else:
            self._dv["ifaces"].configure(text="—")

    # ---------------- 事件日志线程 ----------------
    def _log_loop(self):
        path = "/var/log/kern.log"
        while not self._stop:
            try:
                if self._lf is None:
                    f = open(path, "r", errors="replace")
                    size = os.fstat(f.fileno()).st_size
                    f.seek(max(0, size - 131072))
                    f.readline()  # 丢掉可能不完整的半行
                    self._lf = f
                    self._lfpos = f.tell()
                    if not self._lf_hist_done:
                        self._lf_hist_done = True
                        hist = [ln for ln in f.read().splitlines()
                                if USB_EVT_RE.search(ln)]
                        for ln in hist[-120:]:
                            self.q.put(("evt", ln))
                else:
                    size = os.path.getsize(path)
                    if size < self._lfpos:      # 被轮转了
                        try:
                            self._lf.close()
                        except Exception:
                            pass
                        self._lf = None
                        continue
                    data = self._lf.read()
                    if data:
                        for ln in data.splitlines():
                            if USB_EVT_RE.search(ln):
                                self.q.put(("evt", ln))
                        self._lfpos = self._lf.tell()
            except Exception:
                self._lf = None
            time.sleep(0.6)

    def _on_evt(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", classify_evt(line))
        if int(self.log.index("end-1c").split(".")[0]) > 900:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 摄像头 ----------------
    def _cam_refresh_list(self):
        items = []
        for node in sorted(glob.glob("/dev/video*"),
                           key=lambda x: int(re.search(r"(\d+)$", x).group(1))):
            idx = int(re.search(r"(\d+)$", node).group(1))
            name = rd("/sys/class/video4linux/video%d" % idx, "name")
            # 找到所属 USB 设备(如 2-1.2)
            real = os.path.realpath("/sys/class/video4linux/video%d/device" % idx)
            m = re.search(r"/usb(\d+)/([\d.-]+)/", real)
            usb = ("%s" % m.group(2)) if m else "?"
            items.append("/dev/video%d  %s  [%s]" % (idx, name[:34], usb))
        self._cam_items = items
        self.cmb_cam.configure(values=items)
        if items and not self.cmb_cam.get():
            self.cmb_cam.current(0)
        self.cam_canvas.itemconfigure(
            "camhint", text="共 %d 个 video 节点:同一摄像头的彩色/深度/红外各占一个节点,"
                            "想看彩色画面就切换节点试(深度/红外是灰度点阵图,属正常)。"
                            % len(items) if items else "未发现摄像头设备")

    def _cam_toggle(self):
        if self._cam_on:
            self._cam_on = False
            self.btn_cam.configure(text="开始预览", bg=C_ACC)
            self.lb_cam.configure(text="已停止")
            return
        i = self.cmb_cam.current()
        if i < 0:
            self.lb_cam.configure(text="没有可选的摄像头节点", fg=C_BAD)
            return
        idx = int(re.search(r"video(\d+)", self._cam_items[i]).group(1))
        self._cam_gen += 1
        self._cam_on = True
        self._cam_idx = idx
        self.btn_cam.configure(text="停止预览", bg=C_CARD2)
        self.lb_cam.configure(text="正在打开 /dev/video%d ..." % idx, fg=C_SUB)
        threading.Thread(target=self._cam_loop, args=(idx, self._cam_gen),
                         daemon=True).start()

    def _cam_loop(self, idx, gen):
        try:
            import cv2
        except Exception as e:
            self.q.put(("cam", (None, {"err": "OpenCV 不可用: %s" % e})))
            return
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        try:
            cap.set(3, 640)
            cap.set(4, 480)
        except Exception:
            pass
        if not cap.isOpened():
            self.q.put(("cam", (None, {"err": "打不开 /dev/video%d(换其它节点试试)"
                                       % idx})))
            return
        n, t0, errs, last = 0, time.time(), 0, 0.0
        while self._cam_on and gen == self._cam_gen and not self._stop:
            ok, frame = cap.read()
            if not ok:
                errs += 1
                self.q.put(("cam", (None, {"err": "读取失败 x%d — 设备可能掉线/被挂起"
                                           % errs})))
                if errs > 40:
                    self.q.put(("cam", (None, {"err": "连续读取失败,已停止"})))
                    break
                time.sleep(0.25)
                continue
            errs = max(0, errs - 1)
            n += 1
            now = time.time()
            if now - last >= 1 / 14.0:          # 限制预览帧率,减轻负担
                h, w = frame.shape[:2]
                ok2, buf = cv2.imencode(".png", frame)
                if not ok2:
                    continue
                stats = {
                    "fps": n / (now - t0) if now > t0 else 0,
                    "size": "%dx%d" % (w, h),
                    "mbps": n * (w * h * 3) / max(0.001, now - t0) / 1048576.0,
                    "errs": errs,
                }
                self.q.put(("cam",
                            (base64.b64encode(buf).decode("ascii"), stats)))
                last = now
        try:
            cap.release()
        except Exception:
            pass
        if gen == self._cam_gen and self._cam_on:
            self._cam_on = False
            self.q.put(("cam", (None, {"stopped": True})))

    def _on_cam(self, payload):
        b64, stats = payload
        if b64:
            try:
                self._cam_img = tk.PhotoImage(data=b64)
                self.cam_canvas.delete("all")
                self.cam_canvas.create_image(10, 10, anchor="nw",
                                             image=self._cam_img)
                self.lb_cam.configure(
                    text="FPS %.1f  ·  %s  ·  吞吐估算 %.1f MB/s(摄像头→主机)  ·  错误 %d"
                         % (stats["fps"], stats["size"], stats["mbps"],
                            stats["errs"]),
                    fg=C_OK)
            except Exception as e:
                self.lb_cam.configure(text="解码失败: %s" % e, fg=C_BAD)
        elif stats.get("err"):
            self.lb_cam.configure(text=stats["err"], fg=C_BAD)
        elif stats.get("stopped"):
            self.btn_cam.configure(text="开始预览", bg=C_ACC)

    def _update_cam_device_status(self, snap):
        # 正在预览的摄像头所属 USB 设备消失 => 大警告
        if self._cam_on and self._cam_idx is not None:
            real = os.path.realpath(
                "/sys/class/video4linux/video%d/device" % self._cam_idx)
            if not os.path.exists("/dev/video%d" % self._cam_idx):
                self.lb_cam.configure(text="⚠ /dev/video%d 已消失(设备掉线?)"
                                      % self._cam_idx, fg=C_BAD)

    # ---------------- 深度模式 (usbmon) ----------------
    def _deep_toggle(self):
        if self._deep_on:
            self._deep_stop()
            return
        self._deep_start()

    def _deep_start(self):
        if not os.path.exists(HELPER):
            self.log_line("✗ 找不到 usbmon_helper.py")
            return
        if self._pw is None:
            pw = self._ask_password()
            if pw is None:
                self.log_line("✗ 未取得管理员密码,深度模式未开启")
                return
            self._pw = pw
        self.log_line("$ sudo python3 usbmon_helper.py  (统计每设备 RX/TX)")
        try:
            self._deep_proc = subprocess.Popen(
                ["sudo", "-S", "-p", "", sys.executable, HELPER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            self._deep_proc.stdin.write((self._pw + "\n").encode())
            self._deep_proc.stdin.flush()
        except Exception as e:
            self.log_line("✗ 启动失败: %s" % e)
            self._deep_proc = None
            return
        self._deep_on = True
        self.lb_deep.configure(text="开", fg=C_OK)
        self.btn_deep.configure(text="关闭")
        threading.Thread(target=self._deep_reader, daemon=True).start()
        threading.Thread(target=self._deep_err_reader, daemon=True).start()

    def _deep_stop(self):
        self._deep_on = False
        self._deep_prev = None
        self._deep_rate = {}
        if self._deep_proc:
            try:
                self._deep_proc.terminate()
            except Exception:
                pass
            self._deep_proc = None
        self.lb_deep.configure(text="关", fg=C_SUB)
        self.btn_deep.configure(text="开启")
        self.log_line("· 深度模式已关闭")

    def _deep_reader(self):
        p = self._deep_proc
        try:
            for line in iter(p.stdout.readline, b""):
                try:
                    msg = json.loads(line.decode())
                except Exception:
                    continue
                if msg.get("type") == "usbmon":
                    self.q.put(("deep", msg.get("stats", [])))
                elif msg.get("type") == "error":
                    self.q.put(("log", "✗ usbmon: " + msg.get("msg", "")))
            self.q.put(("deepdead", None))
        except Exception:
            self.q.put(("deepdead", None))

    def _deep_err_reader(self):
        p = self._deep_proc
        try:
            data = p.stderr.read()
            if data and b"incorrect password" in data.lower():
                self._pw = None
                self.q.put(("log", "✗ 密码错误,深度模式未开启"))
        except Exception:
            pass

    def _on_deepdead(self, _p):
        if self._deep_on:
            self._deep_on = False
            self.lb_deep.configure(text="关", fg=C_BAD)
            self.btn_deep.configure(text="开启")
            self.log_line("✗ 深度模式进程退出(usbmon 模块未加载? 点\"启用usbmon模块\")")

    def _on_deep(self, stats):
        now = time.time()
        cur = {}
        for s in stats:
            try:
                cur[(str(s["bus"]), str(s["dev"]))] = (int(s["rx"]), int(s["tx"]))
            except Exception:
                continue
        if self._deep_prev:
            t0, d0 = self._deep_prev
            dt = now - t0
            if dt > 0.2:
                for k, (r1, t1) in cur.items():
                    if k in d0:
                        r0, t0_ = d0[k]
                        self._deep_rate[k] = (max(0, r1 - r0) / dt,
                                              max(0, t1 - t0_) / dt)
        self._deep_prev = (now, cur)
        self._update_detail()

    # ---------------- sudo 动作 ----------------
    def _ask_password(self):
        """内置密码窗口,出错兜底 zenity(与 1/2 号工具一致)。"""
        dlg = tk.Toplevel(self)
        self._pwd_dlg = dlg
        result = {"pw": None}
        try:
            dlg.title("需要管理员权限")
            dlg.configure(bg=C_CARD)
            dlg.transient(self)
            w, h = 440, 230
            x = max(0, (dlg.winfo_screenwidth() - w) // 2)
            y = max(0, (dlg.winfo_screenheight() - h) // 2)
            dlg.geometry("%dx%d+%d+%d" % (w, h, x, y))
            tk.Label(dlg, text="此操作需要管理员密码", bg=C_CARD, fg=C_FG,
                     font=FONT_B).pack(padx=24, pady=(18, 2))
            tk.Label(dlg, text="(就是你登录 Jetson 时输入的密码)",
                     bg=C_CARD, fg=C_SUB, font=FONT_S).pack()
            ent = tk.Entry(dlg, show="●", font=FONT, width=22, bg=C_INPUT,
                           fg=C_FG, insertbackground=C_FG, relief="flat")
            ent.pack(padx=24, pady=14, ipady=4)
            ent.focus_set()

            def ok(_e=None):
                result["pw"] = ent.get()
                dlg.destroy()

            def cancel():
                dlg.destroy()

            bf = tk.Frame(dlg, bg=C_CARD)
            bf.pack(pady=(0, 18))
            tk.Button(bf, text="确定", command=ok, bg=C_ACC, fg="#ffffff",
                      relief="flat", font=FONT_B, padx=18, pady=4,
                      cursor="hand2").pack(side="left", padx=8)
            tk.Button(bf, text="取消", command=cancel, bg=C_CARD2, fg=C_FG,
                      relief="flat", font=FONT, padx=14, pady=4,
                      cursor="hand2").pack(side="left", padx=8)
            ent.bind("<Return>", ok)
            dlg.protocol("WM_DELETE_WINDOW", cancel)

            def grab_later():
                try:
                    if dlg.winfo_exists():
                        dlg.lift()
                        dlg.grab_set()
                        dlg.focus_force()
                except Exception:
                    pass
            dlg.after(50, grab_later)
            dlg.after(250, grab_later)
            dlg.wait_window()
        except Exception:
            try:
                dlg.destroy()
            except Exception:
                pass
            pw = self._ask_password_zenity()
            if pw is not None:
                return pw
        finally:
            self._pwd_dlg = None
        return result["pw"]

    def _ask_password_zenity(self):
        if not shutil.which("zenity"):
            self.log_line("✗ 找不到 zenity,无法弹出密码框")
            return None
        try:
            p = subprocess.run(["zenity", "--password",
                                "--title=需要管理员密码"],
                               capture_output=True, text=True, timeout=300)
        except Exception as e:
            self.log_line("✗ 系统密码框启动失败: %s" % e)
            return None
        if p.returncode != 0:
            return None
        return (p.stdout or "").strip()

    def run_root(self, args, timeout=120):
        """sudo -S 执行同目录 actions_usb.sh 子命令(密码仅内存缓存)。"""
        if self._pw is None:
            pw = self._ask_password()
            if pw is None:
                self.log_line("✗ 未取得管理员密码,操作中止")
                return False
            self._pw = pw
        self.log_line("$ sudo bash actions_usb.sh " + " ".join(args))
        try:
            p = subprocess.run(["sudo", "-S", "-p", "", "bash", ACTION] + args,
                               input=self._pw + "\n", capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log_line("✗ 执行超时")
            return False
        out = (p.stdout or "") + (p.stderr or "")
        if "incorrect password" in out.lower():
            self._pw = None
            self.log_line("✗ 密码错误,请重试")
            return False
        for ln in out.rstrip().splitlines():
            if ln.strip():
                self.log_line("  " + ln)
        if p.returncode != 0:
            self.log_line("✗ 退出码 %d" % p.returncode)
            return False
        return True

    def _act_ina(self):
        if self.run_root(["ina-readfix"]):
            self.log_line("✓ INA3221 读取权限已修复,1 秒后自动刷新")

    def _act_jtop(self):
        if self.run_root(["jtop-enable"]):
            self.log_line("✓ jtop 服务已启用,1 秒后自动刷新")

    def _act_as_off(self):
        if self.run_root(["autosuspend-off"]):
            self.log_line("✓ 已全部禁止 USB 自动休眠(立即生效,重启后失效;可再次执行)")

    def _act_as_auto(self):
        if self.run_root(["autosuspend-auto"]):
            self.log_line("✓ 已恢复 USB 自动休眠(系统默认)")

    def _act_usbmon(self):
        if self.run_root(["usbmon-on"]):
            self.log_line("✓ usbmon 模块已加载,现在可开启\"深度模式\"")

    def _act_powermode(self):
        v = self.cmb_pm.get()
        m = re.match(r"(\d+)", v)
        if not m:
            self.log_line("✗ 未选择功耗模式")
            return
        if self.run_root(["powermode", m.group(1)]):
            self.log_line("✓ 功耗模式已切换为 %s" % v)

    def _act_reenum(self):
        if not self._sel:
            self.log_line("✗ 请先在左侧选中设备")
            return
        if self.run_root(["reenum", self._sel]):
            self.log_line("✓ %s 已重新枚举(authorized 0→1)" % self._sel)

    def _act_dev_on(self):
        if not self._sel:
            self.log_line("✗ 请先在左侧选中设备")
            return
        if self.run_root(["dev-power", self._sel, "on"]):
            self.log_line("✓ %s 已禁止自动休眠(power/control=on)" % self._sel)

    def _act_dev_auto(self):
        if not self._sel:
            self.log_line("✗ 请先在左侧选中设备")
            return
        if self.run_root(["dev-power", self._sel, "auto"]):
            self.log_line("✓ %s 已恢复自动休眠(power/control=auto)" % self._sel)

    # ---------------- 轮询线程 ----------------
    def _poll_loop(self):
        while not self._stop:
            try:
                snap = collect_snapshot()
                self.q.put(("snap", snap))
            except Exception as e:
                self.q.put(("log", "✗ 采集异常: %r" % e))
            time.sleep(REFRESH_S)

    def _on_close(self):
        self._stop = True
        self._cam_on = False
        self._deep_stop()
        try:
            if self._lf:
                self._lf.close()
        except Exception:
            pass
        self.after(150, self.destroy)


# ================= 入口 =================

def main():
    if SMOKE:
        snap = collect_snapshot()
        print(json.dumps(snap, ensure_ascii=False, indent=1, default=str))
        return 0
    if os.geteuid() == 0:
        print("请不要用 root 运行本工具(需要时会主动要密码)。")
        return 1
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
