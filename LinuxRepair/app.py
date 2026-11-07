#!/usr/bin/python3
import argparse
import difflib
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from core import command, VERSION
from modules import MODULES
from modules import display
from root_helper import preview

HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description='Linux Repair: GUI or read-only CLI')
    parser.add_argument('--diagnose',action='store_true')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--smoke-test',action='store_true')
    parser.add_argument('--export-edid',type=Path,metavar='DIR',help='只导出真实且校验有效的显示器 EDID，不修改系统')
    args=parser.parse_args()
    if args.export_edid:
        r=command(['xrandr','--verbose'],env=display.xenv())
        found=display.edids(r['text']) if r['code']==0 else {}
        if not found:raise SystemExit('未读到校验有效的真实 EDID；没有创建假 EDID。')
        args.export_edid.mkdir(parents=True,exist_ok=True)
        for name,data in found.items():
            safe=__import__('re').sub(r'[^A-Za-z0-9_.-]','_',name)
            p=args.export_edid/(safe+'-'+__import__('hashlib').sha256(data).hexdigest()[:12]+'.bin')
            p.write_bytes(data);print(str(p))
        return
    if args.diagnose:
        report=display.diagnose()
        text=json.dumps(report,ensure_ascii=False,indent=2)
        if args.output: args.output.write_text(text,encoding='utf-8')
        else: print(text)
        return
    os.environ.update(display.xenv())
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    root=tk.Tk();root.title('Linux 修复中心 · '+VERSION);root.geometry('1080x780')
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('TButton',padding=(10,7));style.configure('TLabel',font=('sans-serif',11))
    outer=ttk.Frame(root,padding=16);outer.pack(fill='both',expand=True)
    ttk.Label(outer,text='Linux 修复中心',font=('sans-serif',21,'bold')).pack(anchor='w')
    ttk.Label(outer,text='当前模块：Jetson NVIDIA / X11 显示   ·   所有修改均需手动发起',foreground='#34546b').pack(anchor='w',pady=5)
    tabs=ttk.Notebook(outer);tabs.pack(fill='both',expand=True,pady=10)
    page=ttk.Frame(tabs,padding=12);tabs.add(page,text='  显示诊断  ')
    actions=ttk.Frame(page);actions.pack(fill='x')
    status=tk.StringVar(value='尚未诊断。建议从「读取诊断」开始。')
    ttk.Label(page,textvariable=status,wraplength=980).pack(anchor='w',pady=8)
    textframe=ttk.Frame(page);textframe.pack(fill='both',expand=True)
    output=tk.Text(textframe,wrap='word',font=('monospace',10),background='#102432',foreground='#e3eff6',insertbackground='white')
    scroll=ttk.Scrollbar(textframe,command=output.yview);output.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right',fill='y');output.pack(fill='both',expand=True)
    messages=queue.Queue();last_report=[None];busy=[False];action_buttons=[]
    def log(s):
        output.insert('end',str(s)+'\n');output.see('end')
    def work(fn,done=None):
        if busy[0]:return
        busy[0]=True
        for b in action_buttons:b.configure(state='disabled')
        status.set('正在执行，请稍候……')
        def worker():
            try:messages.put((True,fn(),done))
            except Exception as e:messages.put((False,str(e),None))
        threading.Thread(target=worker,daemon=True).start()
    def pump():
        try:
            ok,result,done=messages.get_nowait();busy[0]=False
            for b in action_buttons:b.configure(state='normal')
            status.set('操作完成；请查看日志。' if ok else '操作失败，未继续执行后续步骤。')
            if ok and done:done(result)
            else:log(result)
        except queue.Empty:pass
        root.after(100,pump)
    def button(parent,label,fn):
        b=ttk.Button(parent,text=label,command=fn);b.pack(side='left',padx=4,pady=4);action_buttons.append(b);return b
    def show_report(r):
        last_report[0]=r;output.delete('1.0','end');log(display.format_report(r))
        status.set('诊断完成。'+' '.join(r['findings'][:2]))
    button(actions,'读取诊断',lambda:work(display.diagnose,show_report))
    def export():
        if not last_report[0]:messagebox.showinfo('导出','请先读取诊断。');return
        p=filedialog.asksaveasfilename(defaultextension='.json',initialfile='linux-repair-display.json',filetypes=[('JSON 报告','*.json'),('文本报告','*.txt')])
        if p:
            data=display.format_report(last_report[0]) if p.endswith('.txt') else json.dumps(last_report[0],ensure_ascii=False,indent=2)
            Path(p).write_text(data,encoding='utf-8');status.set('报告已保存。含设备、会话和日志信息，分享前请检查。')
    button(actions,'导出报告',export)
    def export_edid():
        target=filedialog.askdirectory(title='保存真实显示器 EDID（只读采集）')
        if target:work(lambda:command(['/usr/bin/python3',str(HERE/'app.py'),'--export-edid',target]),action_done)
    button(actions,'采集真实 EDID',export_edid)
    button(actions,'唤醒当前屏幕',lambda:work(lambda:command(['xset','dpms','force','on'],env=display.xenv())['text'] or '已发送唤醒请求；不代表屏幕必然有信号。'))
    modes=ttk.Frame(page);modes.pack(fill='x',pady=8)
    port=ttk.Combobox(modes,state='readonly',width=16);mode=ttk.Combobox(modes,state='readonly',width=18)
    ttk.Label(modes,text='会话输出 / 支持模式').pack(side='left');port.pack(side='left',padx=6);mode.pack(side='left',padx=6)
    known=[{}]
    def selected(*_):
        mode['values']=known[0].get(port.get(),[])
        if mode['values']:mode.current(0)
    port.bind('<<ComboboxSelected>>',selected)
    def set_outputs(r):
        log(r['text']);known[0]=display.outputs(r['text']) if r['code']==0 else {}
        port['values']=list(known[0])
        if known[0]:port.current(0);selected()
    button(modes,'刷新输出',lambda:work(lambda:command(['xrandr','--query'],env=display.xenv()),set_outputs))
    def open_settings():
        # GNOME's native confirmation dialog handles its own timed mode rollback.
        subprocess.Popen(['gnome-control-center','display'],env=dict(os.environ,**display.xenv()),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        log('已打开系统显示设置。请使用系统的保留/恢复确认；这里只展示模式，不绕过确认直接设置。')
    button(modes,'调整分辨率…',open_settings)
    repair=ttk.Frame(tabs,padding=16);tabs.add(repair,text='  配置测试与恢复  ')
    ttk.Label(repair,text='恢复真实显示器自动检测',font=('sans-serif',17,'bold')).pack(anchor='w')
    ttk.Label(repair,text='只注释指定文件中的 ConnectedMonitor；保留 NVIDIA 驱动和 AllowEmptyInitialConfiguration。\n不改设备树、内核、磁盘、网络、NoMachine 安全设置或 GDM 单元。',wraplength=960).pack(anchor='w',pady=12)
    ttk.Label(repair,text='测试会重启 GDM，关闭桌面程序并断开 NoMachine。\n应用前必须保存工作、连接显示器，并确认 SSH 可恢复。\n180 秒内未确认会自动恢复；待确认期间不要重启整机。\n自动检测不保证拔线后远程桌面持续渲染，必须另做热插拔验收。',foreground='#9b3c16',wraplength=960).pack(anchor='w',pady=10)
    row=ttk.Frame(repair);row.pack(fill='x')
    diff=tk.Text(repair,height=12,wrap='word');diff.pack(fill='both',expand=True,pady=10)
    def diff_preview():
        try:
            old=display.CONFIG.read_text();new=preview(old)
            diff.delete('1.0','end');diff.insert('end',''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='当前配置',tofile='测试配置')))
        except Exception as e:messagebox.showinfo('不能自动测试',str(e))
    def privileged(action,extra=None):
        return command(['pkexec','/usr/bin/python3',str(HERE/'root_helper.py'),action]+(extra or []),timeout=150)
    def action_done(r):
        log(r['text']);status.set('返回码 '+str(r['code'])+'；'+r['text'][:200])
        if r['code']:messagebox.showerror('未成功',r['text'])
    def start_test():
        diff_preview()
        if messagebox.askyesno('开始受保护测试','已保存工作且 SSH 可用？\n会请求管理员认证、启动180秒恢复定时器，再修改配置并重启桌面。\n测试期间不重启整机。继续？'):
            work(lambda:privileged('test-auto'),action_done)
    def keep():
        if messagebox.askyesno('保留配置','确认物理屏已恢复且远程仍可用？\n保留仅代表你确认配置，不代表热插拔已通过。'):
            work(lambda:privileged('confirm'),action_done)
    def restore():
        if messagebox.askyesno('恢复并重启桌面','恢复最近一次测试前的配置，并重启 GDM？桌面程序将关闭。'):
            work(lambda:privileged('restore'),action_done)
    button(row,'预览修改',diff_preview);button(row,'开始180秒测试',start_test)
    button(row,'确认保留',keep);button(row,'恢复原配置',restore)
    def pin_edid():
        if not messagebox.askyesno('本机 DFP-1 无屏运行测试','仅针对已验证映射为 DFP-1 的这台 Jetson。\n先连接同一显示器并确认物理显示正常。\n将采集 DP-1 的真实 EDID，固定 DFP-1 后重启桌面。\n必须测试拔线后的远程操控，未确认时180秒恢复。继续？'):return
        def prepare():
            r=command(['xrandr','--verbose'],env=display.xenv())
            data=display.edids(r['text']).get('DP-1') if r['code']==0 else None
            if not data:raise RuntimeError('DP-1 未提供有效 EDID；拒绝继续。')
            import tempfile
            fd,name=tempfile.mkstemp(prefix='linux-repair-real-edid-',suffix='.bin')
            try:
                with os.fdopen(fd,'wb') as f:f.write(data)
                return privileged('test-edid',['--edid',name])
            finally:Path(name).unlink(missing_ok=True)
        work(prepare,action_done)
    button(row,'测试本机 DFP-1 常亮',pin_edid)
    transaction=tk.StringVar(value='尚无测试状态。')
    ttk.Label(repair,textvariable=transaction,wraplength=960).pack(anchor='w')
    def poll():
        try:
            p=Path('/var/lib/linux-repair-display/state.json')
            s=json.loads(p.read_text()) if p.exists() else {'status':'idle'}
            remain=max(0,int(s.get('deadline',0)-__import__('time').time()))
            names={'idle':'无测试','pending':'测试中，尚未保留','confirmed':'已保留测试配置','restored':'已恢复测试前配置','aborted':'测试中止'}
            transaction.set('测试状态：'+names.get(s['status'],s['status'])+(' ｜ 自动恢复约剩 '+str(remain)+' 秒' if s['status']=='pending' else '')+' ｜ 重启桌面后可重新打开本程序')
        except Exception as e:transaction.set('状态读取失败：'+str(e))
        root.after(1000,poll)
    more=ttk.Frame(tabs,padding=18);tabs.add(more,text='  验收与扩展  ')
    ttk.Label(more,text='热插拔验收清单',font=('sans-serif',17,'bold')).pack(anchor='w')
    ttk.Label(more,text='□ 插着显示器启动：物理屏与 NoMachine 都能显示\n□ NoMachine 连接期间：物理屏持续显示\n□ 拔下视频线：远程画面继续更新，鼠标键盘能操作\n□ 插回同一显示器：物理屏恢复，无需重启桌面\n□ 重复拔插至少两轮，检查应用会话是否保留\n\n以上需要现场人员和实际客户端共同验收，诊断工具无法替代。\n若纯软件未能满足：可能需要匹配载板的 BSP、实际显示器 EDID，\n或经过验证的带透传 EDID 保持设备；本程序不会伪造“修复成功”。\n\n扩展接口：modules/ 下新增模块并在 MODULES 注册。\n现有 core.command、报告导出及后台队列可复用；特权修改必须单独设计恢复。\n网络修复暂未实现。',wraplength=960).pack(anchor='w',pady=12)
    log('欢迎。先读取诊断；本程序不会在启动时自动修复。\n从 SSH 启动会尝试发现当前用户 X11 会话；普通桌面双击时沿用当前会话。')
    poll();pump()
    if args.smoke_test:
        root.after(1500,lambda:(print('GUI_SMOKE_OK',flush=True),root.destroy()))
    root.mainloop()


if __name__=='__main__':main()
