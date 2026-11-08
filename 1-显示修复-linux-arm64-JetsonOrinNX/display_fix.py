#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson 显示修复工具 —— 修复"HDMI无输出/物理显示器黑屏,但NoMachine能连"的问题。

根因: /etc/X11/xorg.conf.d/99-nvidia-tegra.conf 里 Option "ConnectedMonitor" "DFP"
      强制NVIDIA驱动只认第一个数字输出口(DP-0),HDMI被完全屏蔽。
"""
import os
import re
import shutil
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

HERE   = os.path.dirname(os.path.abspath(__file__))
ACTION = os.path.join(HERE, "actions_display.sh")
CONF   = "/etc/X11/xorg.conf.d/99-nvidia-tegra.conf"

os.environ["PATH"] = ("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                      "/sbin:/bin:" + os.environ.get("PATH", ""))

# ---------------- 配色(深色) ----------------
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

SMOKE = os.environ.get("AAAFIX_SMOKE")  # 自动化冒烟测试用


def xenv():
    """返回用于访问物理屏 :0 的环境变量(自动探测Xauthority)。"""
    uid = os.getuid()
    cands = ["/run/user/%d/gdm/Xauthority" % uid,
             os.path.expanduser("~/.Xauthority"),
             os.environ.get("XAUTHORITY", "")]
    for xa in cands:
        if xa and os.path.exists(xa):
            env = dict(os.environ, DISPLAY=":0", XAUTHORITY=xa)
            try:
                subprocess.run(["xrandr", "--query"], env=env,
                               capture_output=True, timeout=4)
                return env
            except Exception:
                continue
    return dict(os.environ, DISPLAY=":0")


class Switch(tk.Canvas):
    """手机风格的开关滑块。"""
    def __init__(self, master, command=None, initial=False, w=62, h=30):
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         bg=master["bg"], cursor="hand2")
        self.w, self.h = w, h
        self.state = bool(initial)
        self.command = command
        self.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e):
        self.state = not self.state
        self._draw()
        if self.command:
            self.command()

    def set(self, v):
        self.state = bool(v)
        self._draw()

    def get(self):
        return self.state

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        r = h // 2
        body = C_OK if self.state else "#55607080"[:7]
        body = C_OK if self.state else "#556070"
        if self.state:
            self.create_oval(0, 0, 2 * r, 2 * r, fill=body, outline=body)
            self.create_oval(w - 2 * r, 0, w, 2 * r, fill=body, outline=body)
            self.create_rectangle(r, 0, w - r, 2 * r, fill=body, outline=body)
            self.create_oval(w - h + 4, 3, w - 5, h - 3, fill="#ffffff", outline="")
        else:
            self.create_oval(0, 0, 2 * r, 2 * r, fill=body, outline=body)
            self.create_oval(w - 2 * r, 0, w, 2 * r, fill=body, outline=body)
            self.create_rectangle(r, 0, w - r, 2 * r, fill=body, outline=body)
            self.create_oval(4, 3, h - 3, h - 3, fill="#dfe5ee", outline="")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Jetson 显示修复工具 · HDMI无输出一键修复")
        self.configure(bg=C_BG)
        self.geometry("880x720")
        self.minsize(820, 640)
        self._center()
        self.q = queue.Queue()
        self._pw = None

        self._build_style()
        self._build_ui()
        self.after(80, self._pump)
        self.after(300, self.diagnose)
        if SMOKE:
            self.after(1500, self.destroy)

    def _center(self):
        self.update_idletasks()
        w, h = 880, 720
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 4)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ---------------- UI ----------------
    def _build_style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TCombobox", fieldbackground=C_INPUT, background=C_CARD2,
                    foreground=C_FG, arrowcolor=C_FG, bordercolor=C_LINE,
                    lightcolor=C_CARD2, darkcolor=C_CARD2, padding=7,
                    font=FONT)
        s.map("TCombobox",
              fieldbackground=[("readonly", C_INPUT)],
              foreground=[("readonly", C_FG)])
        self.option_add("*TCombobox*Listbox.background", C_CARD2)
        self.option_add("*TCombobox*Listbox.foreground", C_FG)
        self.option_add("*TCombobox*Listbox.selectBackground", C_ACC)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", FONT)

    def _card(self, parent, title):
        card = tk.Frame(parent, bg=C_CARD, highlightbackground=C_LINE,
                        highlightthickness=1)
        head = tk.Frame(card, bg=C_CARD)
        head.pack(fill="x", padx=16, pady=(10, 2))
        tk.Label(head, text=title, bg=C_CARD, fg=C_FG, font=FONT_B,
                 anchor="w").pack(side="left")
        return card

    def _build_ui(self):
        pad = {"padx": 18}

        # 头部
        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", **pad, pady=(12, 2))
        tk.Label(head, text="▣ 显示修复", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")
        tk.Label(head, text="  修复:HDMI物理显示器黑屏,但NoMachine远程正常",
                 bg=C_BG, fg=C_SUB, font=FONT_S).pack(side="left", pady=(8, 0))

        self.dot = tk.Label(head, text="● 检测中...", bg=C_BG, fg=C_WARN,
                            font=FONT_B)
        self.dot.pack(side="right")

        # 模式卡片
        c1 = self._card(self, "显示输出模式")
        c1.pack(fill="x", **pad, pady=(8, 2))
        row1 = tk.Frame(c1, bg=C_CARD)
        row1.pack(fill="x", padx=16, pady=(2, 12))
        tk.Label(row1, text="输出到:", bg=C_CARD, fg=C_SUB,
                 font=FONT).pack(side="left")
        self.cmb_mode = ttk.Combobox(row1, state="readonly", font=FONT,
                                     width=44, values=[
            "自动检测(推荐 · 修复HDMI黑屏)",
            "强制HDMI输出(HDMI口专用)",
            "强制DP输出(DisplayPort口专用)",
            "HDMI + DP 同时输出(双屏)",
        ])
        self.cmb_mode.current(0)
        self.cmb_mode.pack(side="left", padx=(8, 0))

        # 分辨率卡片
        c2 = self._card(self, "强制分辨率(一般保持\"跟随显示器\")")
        c2.pack(fill="x", **pad, pady=(8, 2))
        row2 = tk.Frame(c2, bg=C_CARD)
        row2.pack(fill="x", padx=16, pady=(2, 12))
        tk.Label(row2, text="分辨率:", bg=C_CARD, fg=C_SUB,
                 font=FONT).pack(side="left")
        self.cmb_res = ttk.Combobox(row2, state="readonly", font=FONT,
                                    width=44, values=[
            "跟随显示器(推荐)",
            "1920x1080 @60Hz",
            "1600x900  @60Hz",
            "1280x720  @60Hz",
            "3840x2160 @30Hz(仅4K屏)",
        ])
        self.cmb_res.current(0)
        self.cmb_res.pack(side="left", padx=(8, 0))

        # 开关卡片
        c3 = self._card(self, "选项")
        c3.pack(fill="x", **pad, pady=(8, 2))
        box = tk.Frame(c3, bg=C_CARD)
        box.pack(fill="x", padx=16, pady=(4, 12))

        r3a = tk.Frame(box, bg=C_CARD)
        r3a.pack(fill="x", pady=6)
        tk.Label(r3a, text="应用后立即重启显示服务(推荐)", bg=C_CARD, fg=C_FG,
                 font=FONT).pack(side="left")
        tk.Label(r3a, text="  画面/NoMachine会闪断几秒后自动恢复", bg=C_CARD,
                 fg=C_SUB, font=FONT_S).pack(side="left")
        self.sw_restart = Switch(r3a, initial=True)
        self.sw_restart.pack(side="right")

        r3b = tk.Frame(box, bg=C_CARD)
        r3b.pack(fill="x", pady=6)
        tk.Label(r3b, text="强制点亮(检测不到显示器也点亮)", bg=C_CARD, fg=C_FG,
                 font=FONT).pack(side="left")
        tk.Label(r3b, text="  仅当强制HDMI后仍黑屏才开启", bg=C_CARD, fg=C_SUB,
                 font=FONT_S).pack(side="left")
        self.sw_force = Switch(r3b, initial=False)
        self.sw_force.pack(side="right")

        # 按钮区
        btns = tk.Frame(self, bg=C_BG)
        btns.pack(fill="x", **pad, pady=(12, 2))
        self._btn(btns, "应 用 配 置", self.apply_cfg, primary=True)
        self._btn(btns, "立即点亮HDMI\n(不重启,实时)", self.live_fix)
        self._btn(btns, "恢复NVIDIA原厂", self.restore)
        self._btn(btns, "刷新诊断", self.diagnose)

        # 日志
        logf = tk.Frame(self, bg=C_BG)
        logf.pack(fill="both", expand=True, **pad, pady=(10, 14))
        tk.Label(logf, text="诊断 / 执行日志", bg=C_BG, fg=C_SUB,
                 font=FONT_S, anchor="w").pack(fill="x")
        self.log = scrolledtext.ScrolledText(
            logf, bg=C_INPUT, fg=C_FG, insertbackground=C_FG, font=FONT_M,
            height=12, relief="flat", state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(4, 0))

    def _btn(self, parent, text, cmd, primary=False):
        bg = C_ACC if primary else C_CARD2
        fg = "#ffffff" if primary else C_FG
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=C_ACC if primary else C_LINE,
                      activeforeground="#ffffff", relief="flat",
                      font=FONT_B, cursor="hand2", padx=12, pady=8,
                      justify="center")
        b.pack(side="left", padx=(0, 10), ipadx=6)
        return b

    # ---------------- 日志/状态 ----------------
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "status":
                    color, text = payload
                    self.dot.configure(text="● " + text, fg=color)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _append(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def log_line(self, s=""):
        self.q.put(("log", s))

    def set_status(self, color, text):
        self.q.put(("status", (color, text)))

    # ---------------- sudo 执行 ----------------
    def _ask_password(self):
        dlg = tk.Toplevel(self)
        self._pwd_dlg = dlg  # 持有引用,防止窗口因无引用被意外销毁
        result = {"pw": None}
        try:
            dlg.title("需要管理员权限")
            dlg.configure(bg=C_CARD)
            dlg.transient(self)
            # 居中显示,保证一定在可见范围内
            w, h = 440, 230
            x = max(0, (dlg.winfo_screenwidth() - w) // 2)
            y = max(0, (dlg.winfo_screenheight() - h) // 2)
            dlg.geometry("%dx%d+%d+%d" % (w, h, x, y))
            tk.Label(dlg, text="修改系统配置需要管理员密码", bg=C_CARD, fg=C_FG,
                     font=FONT_B).pack(padx=24, pady=(18, 2))
            tk.Label(dlg, text="(就是你登录 Jetson 时输入的密码)", bg=C_CARD,
                     fg=C_SUB, font=FONT_S).pack()
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
                # 窗口真正显示后再抓键盘焦点(避免 grab failed: window not viewable)
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
            raise
        finally:
            self._pwd_dlg = None
        return result["pw"]

    def _ask_password_zenity(self):
        """内置密码窗口出错时的兜底:用系统自带的 zenity 弹密码框。"""
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
            return None  # 用户点了取消
        return (p.stdout or "").strip()

    def run_root(self, args):
        if self._pw is None:
            pw = None
            try:
                pw = self._ask_password()
            except Exception as e:
                self.log_line("· 内置密码窗口出错(%s),改用系统密码框" % e)
                pw = self._ask_password_zenity()
            if pw is None:
                self.log_line("✗ 未取得管理员密码(已取消或密码窗口不可用),操作中止")
                return
            self._pw = pw
        self.log_line("$ sudo " + " ".join(args[2:] if args[:2] == ["bash", ACTION] else args))

        def worker():
            try:
                p = subprocess.run(["sudo", "-S", "-p", ""] + args,
                                   input=self._pw + "\n",
                                   capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                self.log_line("✗ 执行超时")
                self.set_status(C_BAD, "执行超时")
                return
            out = (p.stdout or "") + (p.stderr or "")
            if "incorrect password" in out.lower():
                self._pw = None
                self.log_line("✗ 密码错误,请重新操作并输入正确密码")
                self.set_status(C_BAD, "密码错误")
                return
            for ln in out.rstrip().splitlines():
                if ln.strip():
                    self.log_line(ln)
            if p.returncode == 0:
                self.set_status(C_OK, "操作完成")
            else:
                self.set_status(C_BAD, "执行出错(退出码%d)" % p.returncode)
            self.log_line("-" * 56)
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 诊断 ----------------
    def diagnose(self):
        self.set_status(C_WARN, "检测中...")
        self.log_line("=" * 56)
        self.log_line("【诊断】" + __import__("time").strftime("%Y-%m-%d %H:%M:%S"))

        def worker():
            env = xenv()
            # X输出状态
            try:
                r = subprocess.run(["xrandr", "--query"], env=env,
                                   capture_output=True, text=True, timeout=6)
                outs = []
                for ln in r.stdout.splitlines():
                    m = re.match(r"^(\S+)\s+(connected|disconnected)", ln)
                    if m:
                        outs.append("  %-8s %s%s" % (
                            m.group(1),
                            "已连接✓" if m.group(2) == "connected" else "未连接✗",
                            "  [当前正在输出]" if "*" in ln else ""))
                if outs:
                    self.log_line("X服务器输出口状态(xrandr):")
                    self.log_line("\n".join(outs))
                else:
                    self.log_line("✗ 无法读取X服务器输出(可能不在本机屏幕会话)")
            except Exception as e:
                self.log_line("✗ xrandr 失败: %s" % e)

            # NVIDIA配置
            try:
                with open(CONF) as f:
                    conf = f.read()
                self.log_line("NVIDIA显示配置(%s):" % CONF)
                for ln in conf.rstrip().splitlines():
                    self.log_line("  " + ln)
                if re.search(r'Option\s+"ConnectedMonitor"\s+"(DFP|DP-0)"\s*$',
                             conf, re.M):
                    self.log_line("")
                    self.log_line("★ 发现根因: ConnectedMonitor 强制只认DP,"
                                  "把HDMI屏蔽了!")
                    self.log_line("  → 上方选\"自动检测\"点【应用配置】即可修复")
                    self.set_status(C_WARN, "发现根因:HDMI被屏蔽,点应用配置修复")
                    return
                if 'ConnectedMonitor" "HDMI-0"' in conf:
                    self.set_status(C_OK, "当前:强制HDMI模式")
                else:
                    self.set_status(C_OK, "配置正常(自动检测)")
            except FileNotFoundError:
                self.log_line("✗ 未找到 %s" % CONF)
                self.set_status(C_OK, "配置正常(自动检测)")
            self.log_line("-" * 56)
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 动作 ----------------
    def _mode_key(self):
        return ["auto", "hdmi", "dp", "both"][self.cmb_mode.current()]

    def _res_key(self):
        return ["auto", "1920x1080", "1600x900", "1280x720",
                "3840x2160"][self.cmb_res.current()]

    def apply_cfg(self):
        mode = self._mode_key()
        res = self._res_key()
        if self.sw_force.get() and res == "auto":
            res = "1920x1080"   # 强制点亮需要具体刷新参数
            self.log_line("· 已按\"强制点亮\"自动附加 1920x1080@60 刷新参数")
        restart = "restart" if self.sw_restart.get() else "no"
        if restart == "restart":
            if not messagebox.askyesno(
                    "确认", "即将修改显示配置并重启显示服务。\n\n"
                    "· 物理屏和NoMachine画面会黑几秒,自动恢复\n"
                    "· 若配置不合适可点\"恢复NVIDIA原厂\"撤销\n\n继续?"):
                return
        self.set_status(C_WARN, "正在应用配置...")
        self.run_root(["bash", ACTION, "apply", mode, res, restart])
        self.log_line("提示: 若HDMI仍未亮 → ①确认显示器输入源选了HDMI  "
                      "②开启\"强制点亮\"开关再应用一次")

    def live_fix(self):
        def worker():
            env = xenv()
            try:
                r = subprocess.run(["xrandr", "--query"], env=env,
                                   capture_output=True, text=True, timeout=6)
            except Exception as e:
                self.log_line("✗ 无法连接显示服务: %s" % e)
                return
            hdmi = None
            dp = None
            for ln in r.stdout.splitlines():
                m = re.match(r"^(\S+)\s+(connected|disconnected)", ln)
                if not m:
                    continue
                name, st = m.group(1), m.group(2)
                if "HDMI" in name:
                    hdmi = (name, st == "connected")
                elif "DP" in name:
                    dp = (name, st == "connected")
            if hdmi is None:
                self.log_line("⚠ X服务器里完全没有HDMI输出口 —— "
                              "这就是黑屏根因(NVIDIA配置屏蔽了HDMI)")
                self.log_line("  → xrandr点不亮,请点【应用配置】选\"自动检测\""
                              "并重启显示服务")
                self.set_status(C_WARN, "HDMI被屏蔽:请用【应用配置】")
            elif not hdmi[1]:
                self.log_line("⚠ HDMI口存在但系统认为\"未插线\"")
                self.log_line("  → 先试【应用配置】强制HDMI;"
                              "仍不行就开\"强制点亮\"开关再应用")
                self.set_status(C_WARN, "HDMI未激活:请用【应用配置】")
            else:
                self.log_line("$ xrandr --output %s --auto" % hdmi[0])
                p = subprocess.run(["xrandr", "--output", hdmi[0], "--auto"],
                                   env=env, capture_output=True, text=True,
                                   timeout=8)
                if p.returncode == 0:
                    self.log_line("✓ HDMI已点亮!")
                    self.set_status(C_OK, "HDMI已点亮")
                else:
                    self.log_line("✗ 点亮失败: %s" % (p.stderr or "").strip())
                    self.set_status(C_BAD, "点亮失败")
        threading.Thread(target=worker, daemon=True).start()

    def restore(self):
        if not messagebox.askyesno("确认",
                                   "恢复NVIDIA原厂配置(即恢复出厂时的"
                                   "ConnectedMonitor=DFP强制项)?"):
            return
        self.run_root(["bash", ACTION, "restore"])


if __name__ == "__main__":
    App().mainloop()
