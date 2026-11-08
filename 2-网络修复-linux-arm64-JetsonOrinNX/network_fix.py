#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson 网络修复工具 —— 修复"无线有线都间歇性连不上网"的问题。

已知根因(本机):
  1) WiFi省电模式开启 → 无线网卡间歇掉线
  2) resolvconf 与 systemd-resolved 双服务抢管DNS(127.0.0.53) → 间歇性解析全挂
  3) DNS兜底是8.8.8.8(国内不可达) → DHCP偶尔拿不到DNS就全断
"""
import os
import shutil
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

HERE   = os.path.dirname(os.path.abspath(__file__))
ACTION = os.path.join(HERE, "actions_network.sh")

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

SMOKE = os.environ.get("AAAFIX_SMOKE")


def run_cmd(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


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
        body = C_OK if self.state else "#556070"
        if self.state:
            self.create_oval(0, 0, 2 * r, 2 * r, fill=body, outline=body)
            self.create_oval(w - 2 * r, 0, w, 2 * r, fill=body, outline=body)
            self.create_rectangle(r, 0, w - r, 2 * r, fill=body, outline=body)
            self.create_oval(w - h + 4, 3, w - 5, h - 3, fill="#ffffff",
                             outline="")
        else:
            self.create_oval(0, 0, 2 * r, 2 * r, fill=body, outline=body)
            self.create_oval(w - 2 * r, 0, w, 2 * r, fill=body, outline=body)
            self.create_rectangle(r, 0, w - r, 2 * r, fill=body, outline=body)
            self.create_oval(4, 3, h - 3, h - 3, fill="#dfe5ee", outline="")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Jetson 网络修复工具 · 间歇断网一键修复")
        self.configure(bg=C_BG)
        self.geometry("880x760")
        self.minsize(820, 680)
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
        w, h = 880, 760
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 4)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))

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

        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", **pad, pady=(12, 2))
        tk.Label(head, text="◈ 网络修复", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")
        tk.Label(head, text="  修复:无线有线都间歇性连不上网",
                 bg=C_BG, fg=C_SUB, font=FONT_S).pack(side="left", pady=(8, 0))
        self.dot = tk.Label(head, text="● 检测中...", bg=C_BG, fg=C_WARN,
                            font=FONT_B)
        self.dot.pack(side="right")

        # WiFi省电卡片
        c1 = self._card(self, "WiFi 稳定性")
        c1.pack(fill="x", **pad, pady=(8, 2))
        r1 = tk.Frame(c1, bg=C_CARD)
        r1.pack(fill="x", padx=16, pady=(2, 12))
        tk.Label(r1, text="关闭WiFi省电模式(防掉线)", bg=C_CARD, fg=C_FG,
                 font=FONT).pack(side="left")
        tk.Label(r1, text="  Jetson无线网卡省电掉线的经典原因,强烈推荐",
                 bg=C_CARD, fg=C_SUB, font=FONT_S).pack(side="left")
        self.sw_power = Switch(r1, initial=False)
        self.sw_power.pack(side="right")

        # DNS卡片
        c2 = self._card(self, "DNS 方案")
        c2.pack(fill="x", **pad, pady=(8, 2))
        r2 = tk.Frame(c2, bg=C_CARD)
        r2.pack(fill="x", padx=16, pady=(2, 12))
        tk.Label(r2, text="DNS:", bg=C_CARD, fg=C_SUB, font=FONT).pack(
            side="left")
        self.cmb_dns = ttk.Combobox(r2, state="readonly", font=FONT, width=46,
                                    values=[
            "自动(跟随路由器下发)",
            "静态直连·阿里DNS 223.5.5.5(推荐,防间歇断网)",
            "静态直连·114DNS 114.114.114.114",
            "静态直连·腾讯DNS 119.29.29.29",
        ])
        self.cmb_dns.current(0)
        self.cmb_dns.pack(side="left", padx=(8, 0))

        # 守护卡片
        c3 = self._card(self, "自动守护")
        c3.pack(fill="x", **pad, pady=(8, 2))
        r3 = tk.Frame(c3, bg=C_CARD)
        r3.pack(fill="x", padx=16, pady=(2, 12))
        tk.Label(r3, text="断网自动守护", bg=C_CARD, fg=C_FG,
                 font=FONT).pack(side="left")
        tk.Label(r3, text="  每2分钟体检一次,断了自动重连,DNS坏了自动重启",
                 bg=C_CARD, fg=C_SUB, font=FONT_S).pack(side="left")
        self.sw_watch = Switch(r3, initial=False)
        self.sw_watch.pack(side="right")

        # 按钮区
        btns = tk.Frame(self, bg=C_BG)
        btns.pack(fill="x", **pad, pady=(12, 2))
        self._btn(btns, "应 用 配 置", self.apply_cfg, primary=True)
        self._btn(btns, "一键修复\n(立即重启网络)", self.repair_now)
        self._btn(btns, "刷新诊断", self.diagnose)

        logf = tk.Frame(self, bg=C_BG)
        logf.pack(fill="both", expand=True, **pad, pady=(10, 14))
        tk.Label(logf, text="诊断 / 执行日志", bg=C_BG, fg=C_SUB,
                 font=FONT_S, anchor="w").pack(fill="x")
        self.log = scrolledtext.ScrolledText(
            logf, bg=C_INPUT, fg=C_FG, insertbackground=C_FG, font=FONT_M,
            height=13, relief="flat", state="disabled", wrap="word")
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

    # ---------------- sudo ----------------
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
        self.log_line("$ sudo " + " ".join(args))

        def worker():
            try:
                p = subprocess.run(["sudo", "-S", "-p", ""] + args,
                                   input=self._pw + "\n",
                                   capture_output=True, text=True, timeout=180)
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
        self.log_line("【诊断】" + __import__("time").strftime(
            "%Y-%m-%d %H:%M:%S"))

        def worker():
            problems = []

            # 设备状态
            rc, out = run_cmd(["nmcli", "-t", "-f",
                               "DEVICE,TYPE,STATE,CONNECTION", "device"])
            self.log_line("网络设备:")
            for ln in out.rstrip().splitlines():
                if ln and not ln.startswith("lo:"):
                    parts = ln.split(":")
                    if len(parts) >= 4:
                        self.log_line("  %-10s %-9s %s  (%s)" % (
                            parts[0], parts[1], parts[2], parts[3]))

            # WiFi省电
            rc, out = run_cmd(["sh", "-c", "ls /sys/class/net/wl* 2>/dev/null"])
            wl = [l.rstrip("/").split("/")[-1] for l in
                  out.split() if l.strip()]
            for d in wl:
                rc, out = run_cmd(["iw", "dev", d, "get", "power_save"])
                if "on" in out:
                    self.log_line("WiFi省电(%s): 开启 ⚠ 无线间歇掉线的经典原因" % d)
                    problems.append("WiFi省电开着 → 打开上面的开关应用即可")
                elif "off" in out:
                    self.log_line("WiFi省电(%s): 已关闭 ✓" % d)

            # 服务状态
            for svc in ["NetworkManager", "systemd-networkd",
                        "systemd-resolved", "resolvconf"]:
                rc, out = run_cmd(["systemctl", "is-active", svc])
                mark = "✓" if out.strip() == "active" else "·"
                self.log_line("服务 %-18s %s (%s)" % (svc, mark, out.strip()))
            rc, out = run_cmd(["systemctl", "is-active", "resolvconf"])
            if out.strip() == "active":
                rc2, out2 = run_cmd(["systemctl", "is-active",
                                     "systemd-resolved"])
                if out2.strip() == "active":
                    self.log_line("⚠ resolvconf 和 systemd-resolved 同时在管"
                                  "DNS → 间歇性全断的经典根因")
                    problems.append("DNS双服务冲突 → 选\"静态直连\"DNS并应用即可根治")

            # DNS 实际状态
            try:
                with open("/etc/resolv.conf") as f:
                    dns_lines = [l.strip() for l in f
                                 if l.strip() and not l.startswith("#")]
                self.log_line("当前DNS配置(/etc/resolv.conf): %s" %
                              " ".join(dns_lines))
                if "127.0.0.53" in " ".join(dns_lines):
                    self.log_line("  (经过本机DNS中转 127.0.0.53,中转挂了就全断)")
            except Exception as e:
                self.log_line("✗ 读不到 /etc/resolv.conf: %s" % e)

            # 连通性
            rc, _ = run_cmd(["ping", "-c", "1", "-W", "3", "223.5.5.5"],
                            timeout=8)
            if rc == 0:
                self.log_line("出网测试(直连IP): ✓ 通")
            else:
                self.log_line("出网测试(直连IP): ✗ 不通")
                problems.append("现在就断网 → 点【一键修复】试试")
            rc, _ = run_cmd(["getent", "hosts", "www.baidu.com"], timeout=8)
            if rc == 0:
                self.log_line("DNS域名解析: ✓ 正常")
            else:
                self.log_line("DNS域名解析: ✗ 失败(能连WiFi却打不开网页就是这个)")
                problems.append("DNS解析故障 → 选\"静态直连\"DNS并应用")

            if problems:
                self.log_line("")
                self.log_line("★ 诊断发现的问题:")
                for p in problems:
                    self.log_line("  - " + p)
                self.set_status(C_WARN, "发现%d个问题,见日志" % len(problems))
            else:
                self.log_line("")
                self.log_line("✓ 未发现问题,网络一切正常")
                self.set_status(C_OK, "网络正常")
            self.log_line("-" * 56)
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 动作 ----------------
    def _dns_key(self):
        return ["auto", "aliyun", "114", "tencent"][self.cmb_dns.current()]

    def apply_cfg(self):
        ps = "off" if self.sw_power.get() else "keep"
        dns = self._dns_key()
        wd = "on" if self.sw_watch.get() else "off"
        self.set_status(C_WARN, "正在应用配置...")
        self.run_root(["bash", ACTION, "apply", ps, dns, wd])

    def repair_now(self):
        if not messagebox.askyesno(
                "确认", "将立即重启网络服务并重连所有网络。\n\n"
                "· 断网约10~20秒,NoMachine可能闪断\n继续?"):
            return
        self.set_status(C_WARN, "正在修复网络...")
        self.run_root(["bash", ACTION, "repair"])


if __name__ == "__main__":
    App().mainloop()
