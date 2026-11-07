#!/usr/bin/python3
"""Bounded root helper: one known Xorg option, transactional backup + rollback.
No shell, arbitrary paths, packages, device tree, networking or NX security edits.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TARGET = Path('/etc/X11/xorg.conf.d/99-nvidia-tegra.conf')
STORE = Path('/var/lib/linux-repair-display')
UNIT = 'linux-repair-display-rollback'
BASE_CONFIG = 'Section "Device"\n    Identifier "Tegra GPU"\n    Driver "nvidia"\n    Option "AllowEmptyInitialConfiguration" "true"\nEndSection\n'


def check_clean_layout():
    for f in [Path('/etc/X11/xorg.conf')]+list(Path('/etc/X11/xorg.conf.d').glob('*.conf'))+list(Path('/usr/share/X11/xorg.conf.d').glob('*.conf')):
        if f==TARGET or not f.is_file():continue
        if re.search(r'^\s*Section\s+"(Device|Screen|ServerLayout)"',f.read_text(errors='replace'),re.M|re.I):
            raise ValueError('另有显示布局 '+str(f)+'，拒绝自动创建新配置。')


def restore_snapshot(s):
    if s.get('existed',True):
        atomic(TARGET,Path(s['backup']).read_bytes(),s['mode']);os.chown(str(TARGET),s['uid'],s['gid'])
    elif TARGET.exists():
        TARGET.unlink()  # Remove only the exact file this transaction created.


def digest(b):
    return hashlib.sha256(b).hexdigest()


def preview(text):
    if len(re.findall(r'^\s*Section\s+"Device"', text, re.M|re.I)) != 1:
        raise ValueError('仅支持一个 Device 段；复杂配置需要人工检查。')
    if len(re.findall(r'^\s*Section\b', text, re.M|re.I)) != 1 or not re.search(r'^\s*Driver\s+"nvidia"', text, re.M|re.I):
        raise ValueError('配置不符合已验证的 NVIDIA Device 结构。')
    pattern = r'^([ \t]*Option[ \t]+"ConnectedMonitor"[ \t]+"[^"\n]+"[^\n]*)$'
    if len(re.findall(pattern, text, re.M|re.I)) != 1:
        raise ValueError('需要恰好一条有效 ConnectedMonitor；无此项时无需重复修改。')
    if re.search(r'^\s*Option\s+"(CustomEDID|UseDisplayDevice)"', text, re.M|re.I):
        raise ValueError('存在额外强制显示设置，需要人工检查。')
    return re.sub(pattern, r'# LinuxRepair auto-detect test: \1', text, flags=re.M|re.I)


def edid_preview(text, edid_path):
    # This profile is intentionally scoped to the DFP-1 mapping verified on this Jetson.
    if len(re.findall(r'^\s*Section\b',text,re.M|re.I))!=1 or not re.search(r'^\s*Section\s+"Device"',text,re.M|re.I) or not re.search(r'^\s*Driver\s+"nvidia"',text,re.M|re.I):
        raise ValueError('只支持已检查的单 NVIDIA Device 配置。')
    if re.search(r'^\s*Option\s+"(ConnectedMonitor|CustomEDID|UseDisplayDevice)"',text,re.M|re.I):
        raise ValueError('请先恢复自动检测并确认，再测试 DFP-1 EDID 固定。')
    if len(re.findall(r'^\s*EndSection\s*$',text,re.M|re.I))!=1:raise ValueError('配置段结束标记异常。')
    extra='    Option "ConnectedMonitor" "DFP-1"\n    Option "CustomEDID" "DFP-1:'+str(edid_path)+'"\n'
    return re.sub(r'^(\s*EndSection\s*)$',lambda m:extra+m[1],text,flags=re.M|re.I)


def valid_edid(data):
    return len(data)>=128 and len(data)==128*(1+data[126]) and data[:8]==b'\x00\xff\xff\xff\xff\xff\xff\x00' and all(sum(data[i:i+128])%256==0 for i in range(0,len(data),128))


def execute(args, check=True):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=40)
    if check and p.returncode:
        raise RuntimeError(' '.join(args)+'\n'+p.stdout)
    return p


def atomic(path, data, mode=0o600):
    temp = path.with_name(path.name+'.tmp')
    fd = os.open(str(temp), os.O_WRONLY|os.O_CREAT|os.O_EXCL, mode)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists(): temp.unlink()


def write_state(s):
    atomic(STORE/'state.json', json.dumps(s,ensure_ascii=False,indent=2).encode(),0o644)


def state():
    p = STORE/'state.json'
    return json.loads(p.read_text()) if p.exists() else {'status':'idle'}


def restart():
    # Async job belongs to systemd, not the remote desktop process being stopped.
    execute(['systemctl','--no-block','restart','gdm'])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action', choices=['preview','status','test-auto','test-edid','confirm','restore'])
    parser.add_argument('--edid',type=Path)
    args=parser.parse_args()
    if args.action=='preview':
        print(preview(TARGET.read_text()));return
    if args.action=='status':
        print(json.dumps(state(),ensure_ascii=False,indent=2));return
    if os.geteuid()!=0:
        raise RuntimeError('此操作需要 sudo 或 pkexec 授权。')
    if STORE.is_symlink(): raise RuntimeError('状态目录不得为符号链接。')
    STORE.mkdir(mode=0o755,exist_ok=True)
    if STORE.stat().st_uid!=0 or STORE.stat().st_mode & 0o022:
        raise RuntimeError('状态目录所有者或权限异常。')
    with (STORE/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        s=state()
        if args.action in ['test-auto','test-edid']:
            if s['status']=='pending': raise RuntimeError('已有待确认测试，先恢复或确认。')
            existed=TARGET.is_file()
            if TARGET.is_symlink() or TARGET.resolve()!=TARGET or (TARGET.exists() and not existed):
                raise RuntimeError('目标文件缺失或为符号链接。')
            if not existed:
                if args.action!='test-edid':raise ValueError('没有目标文件，无需移除旧强制设置。')
                model=Path('/proc/device-tree/model').read_text().rstrip('\0')
                release=Path('/etc/nv_tegra_release').read_text()
                if 'Orin NX' not in model or not re.search(r'# R35 \(release\), REVISION: 6\.4,',release):
                    raise ValueError('仅允许在已验证的 Orin NX R35.6.4 上新建配置。')
                check_clean_layout()
                if not TARGET.parent.is_dir():raise ValueError('Xorg 配置目录不存在。')
            old=TARGET.read_bytes() if existed else b''
            if args.action=='test-edid':
                if not args.edid or args.edid.stat().st_size>32768:raise ValueError('需要有效的真实 EDID 文件，大小不得超过32KiB。')
                data=args.edid.read_bytes()
                if not valid_edid(data):raise ValueError('EDID 文件头、长度或校验和无效。')
                xlog=Path('/var/log/Xorg.0.log').read_text(errors='replace')
                if not re.search(r'\([^\n]*DFP-1\): connected',xlog):raise ValueError('当前 Xorg 日志没有真实 DFP-1 显示器连接记录，拒绝猜测输出映射。')
                edid_path=STORE/('edid-'+digest(data)+'.bin')
                if edid_path.exists():
                    if edid_path.is_symlink() or edid_path.read_bytes()!=data:raise ValueError('EDID 保存路径异常。')
                else:atomic(edid_path,data,0o644)
                new=edid_preview(old.decode() if existed else BASE_CONFIG,edid_path).encode()
            else:new=preview(old.decode()).encode()
            st=TARGET.stat() if existed else None
            if st and st.st_uid!=0: raise RuntimeError('配置文件必须属于 root。')
            stamp=str(time.time_ns())
            backup=STORE/('backup-'+stamp+'.conf')
            atomic(backup,old)
            # Copy rollback executable to a root-owned directory so unzipping/moving
            # the GUI folder or ending its session cannot remove the rollback code.
            installed=STORE/('rollback-'+stamp+'.py')
            atomic(installed,Path(__file__).read_bytes(),0o700)
            s=dict(status='pending', profile=args.action, existed=existed, backup=str(backup), original=digest(old),
                   tested=digest(new), mode=(st.st_mode & 0o777) if st else 0o644, uid=st.st_uid if st else 0,gid=st.st_gid if st else 0,
                   started=time.time(),deadline=time.time()+180, helper=str(installed))
            write_state(s)
            execute(['systemctl','stop',UNIT+'.timer'],check=False)
            execute(['systemctl','reset-failed',UNIT+'.service'],check=False)
            try:
                execute(['systemd-run','--unit='+UNIT,'--on-active=180s',
                         '--timer-property=AccuracySec=1s','/usr/bin/python3',str(installed),'restore'])
                execute(['systemctl','is-active','--quiet',UNIT+'.timer'])
                atomic(TARGET,new,s['mode']);os.chown(str(TARGET),s['uid'],s['gid'])
            except Exception:
                # If writing failed after replacement, restore the snapshot before exiting.
                if (TARGET.read_bytes() if TARGET.exists() else b'')!=old:
                    restore_snapshot(s)
                s['status']='aborted';write_state(s)
                execute(['systemctl','stop',UNIT+'.timer'],check=False)
                raise
            print('测试已应用；180 秒后自动恢复。不要在待确认阶段重启整机。',flush=True)
            restart()
        elif args.action=='confirm':
            if s['status']!='pending': raise RuntimeError('没有待确认测试。')
            if time.time()>=s['deadline']-5: raise RuntimeError('已接近或超过恢复期限，不再确认；等待恢复。')
            if digest(TARGET.read_bytes())!=s['tested']: raise RuntimeError('配置已被其他程序修改，拒绝确认。')
            execute(['systemctl','stop',UNIT+'.timer'])
            s['status']='confirmed';write_state(s);print('已保留测试配置；备份仍可恢复。')
        elif args.action=='restore':
            if s['status'] not in ['pending','confirmed']:
                print('没有需要恢复的测试。');return
            if TARGET.is_symlink(): raise RuntimeError('目标变成符号链接，拒绝写入。')
            old=Path(s['backup']).read_bytes()
            if digest(old)!=s['original']: raise RuntimeError('备份校验失败。')
            if digest(TARGET.read_bytes() if TARGET.exists() else b'') not in [s['tested'],s['original']]:
                raise RuntimeError('配置已被其他程序修改；保留现场，拒绝覆盖。')
            restore_snapshot(s)
            s['status']='restored';write_state(s)
            execute(['systemctl','stop',UNIT+'.timer'],check=False)
            print('已恢复测试前配置。',flush=True);restart()


if __name__=='__main__':
    try: main()
    except Exception as e:
        print('ERROR: '+str(e),file=sys.stderr);sys.exit(1)
