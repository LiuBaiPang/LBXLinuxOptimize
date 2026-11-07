import datetime
import glob
import os
import re
from pathlib import Path
from core import command, VERSION

ID = 'display'
TITLE = '显示诊断与修复'
CONFIG = Path('/etc/X11/xorg.conf.d/99-nvidia-tegra.conf')


def xenv():
    env = {}
    # Infer only the caller's local Xorg session; do not assume :0 on other machines.
    if not os.environ.get('DISPLAY'):
        uid = os.getuid()
        for f in glob.glob('/proc/[0-9]*/environ'):
            try:
                p = Path(f)
                if p.stat().st_uid != uid:
                    continue
                values = dict(x.split(b'=', 1) for x in p.read_bytes().split(b'\0') if b'=' in x)
                d = values.get(b'DISPLAY', b'').decode()
                auth = values.get(b'XAUTHORITY', b'').decode()
                if re.fullmatch(r':\d+(\.\d+)?', d) and auth and Path(auth).is_file():
                    env = {'DISPLAY': d, 'XAUTHORITY': auth}
                    break
            except (OSError, ValueError, UnicodeError):
                pass
    return env


def outputs(text):
    result = {}
    current = None
    for line in text.splitlines():
        m = re.match(r'^(\S+) connected\b', line)
        if m:
            current = m[1]
            result[current] = []
        elif line and not line[0].isspace():
            current = None
        elif current and re.match(r'^\s+\d+x\d+', line):
            mode = line.split()[0]
            if mode not in result[current]:
                result[current].append(mode)
    return result


def valid_edid(data):
    return (len(data)>=128 and len(data)==128*(1+data[126])
            and data[:8]==b'\x00\xff\xff\xff\xff\xff\xff\x00'
            and all(sum(data[i:i+128])%256==0 for i in range(0,len(data),128)))


def edids(text):
    """Read actual EDID properties; never synthesize display timings."""
    found={};port=None;reading=False;chunks=[]
    def finish():
        if port and chunks:
            data=bytes.fromhex(''.join(chunks))
            if valid_edid(data):found[port]=data
    for line in text.splitlines()+['END']:
        if reading:
            if re.fullmatch(r'\s+[0-9a-fA-F]{32}\s*',line):
                chunks.append(line.strip());continue
            finish();reading=False;chunks=[]
        if line and not line[0].isspace():
            m=re.match(r'^(\S+) connected\b',line);port=m[1] if m else None
        elif port and line.strip()=='EDID:':reading=True
    return found


def diagnose():
    env = xenv()
    cmds = [['uname', '-a'], ['cat', '/etc/nv_tegra_release'],
            ['systemctl', 'status', 'gdm', '--no-pager'],
            ['systemctl', 'cat', 'gdm', '--no-pager'], ['xrandr', '--verbose'],
            ['xset', 'q'], ['ps', '-eo', 'pid,user,args'],
            ['journalctl', '-b', '-k', '-n', '250', '--no-pager'],
            ['systemctl', 'list-timers', '--all', '--no-pager']]
    results = [command(c, env=env) for c in cmds]
    files = {}
    paths = [str(CONFIG), '/etc/X11/xorg.conf', '/var/log/Xorg.0.log', '/var/log/Xorg.0.log.old',
             '/var/lib/linux-repair-display/state.json', '/etc/xdg/autostart/nvgpusetupexec.sh']
    paths += glob.glob(str(Path.home()/'.local/share/xorg/Xorg.*.log'))
    for f in paths:
        try:
            text = Path(f).read_text(errors='replace')
            files[f] = text[:24000] + ('\n...TAIL...\n'+text[-12000:] if len(text)>24000 else '')
        except OSError as e:
            files[f] = str(e)
    nx = []
    for f in ['/usr/NX/etc/server.cfg', '/usr/NX/etc/node.cfg']:
        try:
            for s in Path(f).read_text(errors='replace').splitlines():
                if re.match(r'^\s*(CreateDisplay|DisplayOwner|PhysicalDisplays|EnableScreenBlanking|EnableScreenBlankingEffect)\s', s):
                    nx.append(f+': '+s)
        except OSError as e:
            nx.append(str(e))
    drm = []
    for f in glob.glob('/sys/class/drm/card*-*/status'):
        try:
            edid = Path(f).with_name('edid').read_bytes()
            valid = len(edid)>=128 and len(edid)%128==0 and edid[:8]==b'\x00\xff\xff\xff\xff\xff\xff\x00' and all(sum(edid[i:i+128])%256==0 for i in range(0,len(edid),128))
            drm.append(dict(path=f, status=Path(f).read_text().strip(), edid_bytes=len(edid), checksum_valid=valid))
        except OSError as e:
            drm.append(dict(path=f, error=str(e)))
    findings = []
    cfg = files.get(str(CONFIG), '')
    if re.search(r'^\s*Option\s+"ConnectedMonitor"', cfg, re.M|re.I):
        findings.append('存在强制连接设置；xrandr connected 不能独立证明物理显示器已识别。')
    if any(re.search(r'EnableScreenBlanking\s+1\b', s) for s in nx):
        findings.append('NoMachine 开启了连接时物理屏黑屏；到其服务器安全设置检查。')
    if 'Unable to read EDID' in '\n'.join(r['text'] for r in results):
        findings.append('内核日志含 EDID 读取失败；需要核查线缆、转接器、载板 BSP 和显示驱动。')
    if not drm:
        findings.append('没有 DRM connector 节点；此驱动下不能据此认定没有显示器，应结合 Xorg 日志。')
    try:
        transaction=__import__('json').loads(files.get('/var/lib/linux-repair-display/state.json','{}'))
        if transaction.get('status')=='restored':
            findings.append('上一轮测试已恢复原配置；若测试时亮屏而现在黑屏，应对比 Xorg 新旧日志，而不是立即安装驱动。')
    except ValueError:pass
    if Path('/etc/xdg/autostart/nvgpusetupexec.sh').is_file():
        findings.append('系统含 NVIDIA 工厂 GPU 安装提示脚本。弹窗不是显卡硬件检测结论；普通 Jetson 不应据此安装 dGPU 驱动。')
    findings.append('热插拔连续操控尚需人工拔插和 NoMachine 操作验收，工具不会自动标记成功。')
    return dict(version=VERSION, module=ID, time=datetime.datetime.now().astimezone().isoformat(),
                display=os.environ.get('DISPLAY', env.get('DISPLAY', '')), results=results,
                files=files, nomachine=nx, drm=drm, findings=findings)


def format_report(r):
    text = 'Linux Repair '+r['version']+' | '+r['time']+'\n\n'
    text += '\n'.join('• '+s for s in r['findings'])+'\n\n'
    text += 'NoMachine 有效显示设置：\n'+'\n'.join(r['nomachine'])+'\n\n'
    text += 'DRM：'+str(r['drm'])+'\n\n'
    for item in r['results']:
        text += '$ '+' '.join(item['command'])+'\n返回码 '+str(item['code'])+'\n'+item['text']+'\n'
    for f,s in r['files'].items():
        text += '\n=== '+f+' ===\n'+s+'\n'
    return text
