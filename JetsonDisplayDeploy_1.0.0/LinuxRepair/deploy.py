#!/usr/bin/python3
"""Fleet deployment entry point: inspect first, collect local EDID, never copy a sample EDID."""
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from core import command
from modules.display import CONFIG, edids, xenv
from root_helper import BASE_CONFIG

HERE=Path(__file__).resolve().parent


def compatibility(model,release,arch):
    if arch not in ('aarch64','arm64'):raise ValueError('仅适用于 ARM64 Jetson。')
    if 'Orin NX' not in model:raise ValueError('型号不是已验证的 Orin NX；停止，不能套用。')
    if not re.search(r'# R35 \(release\), REVISION: 6\.4,',release):
        raise ValueError('本固化包仅允许已验证的 R35.6.4；其他版本先单独适配测试。')


def evaluate():
    model=Path('/proc/device-tree/model').read_text().rstrip('\0')
    release=Path('/etc/nv_tegra_release').read_text()
    compatibility(model,release,platform.machine())
    if not Path('/usr/NX/bin/nxserver').is_file():raise RuntimeError('未发现 NoMachine，请先安装 ARM64 NoMachine。')
    if CONFIG.is_symlink():raise RuntimeError('目标配置是符号链接，需要人工检查。')
    cfg=CONFIG.read_text() if CONFIG.is_file() else BASE_CONFIG
    active=re.findall(r'^\s*Option\s+"ConnectedMonitor"\s+"([^"\n]+)"',cfg,re.M|re.I)
    if active and active!=['DFP-1']:
        raise RuntimeError('发现旧的强制输出 '+str(active)+'。先执行 root_helper.py test-auto 并在物理屏正常后 confirm，再重新部署。')
    r=command(['xrandr','--verbose'],env=xenv())
    if r['code']:raise RuntimeError('无法读取当前桌面。请从本机桌面终端运行，或确认 SSH 可发现同用户 X11 会话。\n'+r['text'])
    data=edids(r['text']).get('DP-1')
    if not data:raise RuntimeError('DP-1 没有有效真实 EDID。先连接显示器并恢复物理显示，不复制其他屏幕的 EDID。')
    log=Path('/var/log/Xorg.0.log').read_text(errors='replace')
    if not re.search(r'\([^\n]*DFP-1\): connected',log):raise RuntimeError('没有真实 DFP-1 连接记录，拒绝按接口编号猜测映射。')
    # Other Device/Screen definitions may override this file. Refuse custom layouts.
    for f in [Path('/etc/X11/xorg.conf')]+list(Path('/etc/X11/xorg.conf.d').glob('*.conf'))+list(Path('/usr/share/X11/xorg.conf.d').glob('*.conf')):
        if f==CONFIG or not f.is_file():continue
        if re.search(r'^\s*Section\s+"(Device|Screen|ServerLayout)"',f.read_text(errors='replace'),re.M|re.I):
            raise RuntimeError('另有显示布局配置 '+str(f)+'，请人工检查后适配。')
    statepath=Path('/var/lib/linux-repair-display/state.json')
    state=json.loads(statepath.read_text()) if statepath.exists() else {'status':'idle'}
    if state['status']=='pending':raise RuntimeError('已有待确认测试，先确认或恢复，不能叠加部署。')
    sha=hashlib.sha256(data).hexdigest()
    done=(CONFIG.is_file() and state.get('status')=='confirmed' and state.get('profile')=='test-edid'
          and hashlib.sha256(CONFIG.read_bytes()).hexdigest()==state.get('tested')
          and ('edid-'+sha+'.bin') in cfg)
    return {'model':model,'l4t':release.strip(),'connector':'DFP-1','edid_sha256':sha,
            'edid_bytes':len(data),'already_deployed':done},data


def main():
    p=argparse.ArgumentParser(description='Orin NX R35.6.4 / NoMachine 同型号部署')
    p.add_argument('action',choices=['check','apply','confirm','restore','status'])
    args=p.parse_args()
    if args.action in ['confirm','restore','status']:
        cmd=([ 'sudo'] if args.action!='status' else [])+['/usr/bin/python3',str(HERE/'root_helper.py'),args.action]
        raise SystemExit(subprocess.call(cmd))
    if os.geteuid()==0:raise RuntimeError('check/apply 请以桌面普通用户运行，不要对 deploy.py 使用 sudo；需要时会自行请求单次 sudo。')
    info,data=evaluate()
    print(json.dumps(info,ensure_ascii=False,indent=2),flush=True)
    if info['already_deployed']:
        print('已部署相同 EDID，配置匹配；未重复改动或重启桌面。');return
    if args.action=='check':print('预检查通过；尚未修改系统。');return
    print('将固定 DFP-1 和本机刚读取的真实 EDID，重启 GDM。保存工作并保留 SSH。',flush=True)
    if input('输入 APPLY 开始180秒保护测试，其他输入取消：').strip()!='APPLY':
        print('已取消。');return
    target=HERE/'deployment-data';target.mkdir(exist_ok=True)
    path=target/('DFP-1-'+info['edid_sha256']+'.bin');path.write_bytes(data)
    (target/'machine-check.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    print('测试后必须验证物理显示、拔线远程交互、插回显示；全部通过再执行 python3 deploy.py confirm。',flush=True)
    raise SystemExit(subprocess.call(['sudo','/usr/bin/python3',str(HERE/'root_helper.py'),'test-edid','--edid',str(path)]))


if __name__=='__main__':
    try:main()
    except Exception as e:print('停止部署：'+str(e),file=sys.stderr);sys.exit(1)
