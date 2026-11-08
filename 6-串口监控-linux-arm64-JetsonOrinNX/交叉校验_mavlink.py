#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交叉校验_mavlink —— 把自研 mavlink_min 的每条消息定义与 pymavlink(官方生成器
产物)逐字段比对, 用官方实现兜底查线序/类型错误。

MAVLink 的坑: 官网文档展示的字段顺序 ≠ 线上字节顺序(XML 定义顺序才是),
历史教训: VFR_HUD 的线上顺序是 airspeed,groundspeed,alt,climb,heading,throttle。

依赖(仅本脚本需要, 主程序零依赖):  pip3 install --user pymavlink
用法:  python3 交叉校验_mavlink.py
"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mavlink_min as mav                      # noqa: E402
from pymavlink.dialects.v20 import common      # noqa: E402

CLASSNAME = {
    0: "heartbeat", 1: "sys_status", 2: "system_time", 4: "ping",
    11: "set_mode", 20: "param_request_read", 22: "param_value",
    24: "gps_raw_int", 30: "attitude", 31: "attitude_quaternion",
    33: "global_position_int", 65: "rc_channels", 66: "request_data_stream",
    74: "vfr_hud", 76: "command_long", 77: "command_ack",
    147: "battery_status", 148: "autopilot_version", 253: "statustext",
}
SIZE = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4, "u64": 8}


def testvals(i, t):
    """每字段一个可辨识的非零值, 错位必然被暴露。"""
    if t == "f32":
        return 1.0 + i * 0.5
    if t == "u64":
        return 0x1122334455667700 + i
    return (i * 7 + 1) % 30000 + 1


def main():
    mavp = common.MAVLink(None, use_native=False)
    all_ok = True
    for msgid, (name, ce, spec) in sorted(mav.MSG_DEFS.items()):
        cls = getattr(common, "MAVLink_%s_message" % CLASSNAME[msgid], None)
        if cls is None:
            print("⚠ pymavlink 无 %s(%d), 跳过" % (name, msgid))
            continue
        mine = [fn for fn, _t in spec]

        # ============ 方向 A: 我方编码 → 官方解码 ============
        vals, i = {}, 0
        for fn, t in spec:
            if "[" in t:
                base, n = t[:-1].split("[")
                n = int(n)
                vals[fn] = ("AB%d" % msgid) if base == "char" else \
                    [100 + k for k in range(n)]
            else:
                vals[fn] = testvals(i, t)
            i += 1
        raw = mav.encode(msgid, vals)
        got = mavp.parse_char(raw)
        if got is None:
            print("✗ %s(%d): pymavlink 拒收我方帧" % (name, msgid))
            all_ok = False
            continue
        if not isinstance(got, list):
            got = [got]
        g = got[0]
        bad = []
        for fn, t in spec:
            if "[" in t:
                base = t[:-1].split("[")[0]
                if base == "char" and \
                        str(getattr(g, fn, ""))[:6] != str(vals[fn]):
                    bad.append("%s: 我=%r 官=%r" % (fn, vals[fn],
                                                    getattr(g, fn, None)))
                continue
            mine_v, off_v = vals[fn], getattr(g, fn, None)
            if off_v is None:
                continue        # pymavlink 方言版本可能没有该扩展字段, 忽略
            if t == "f32":
                if abs(off_v - mine_v) > 0.01:
                    bad.append("%s: 我=%.3f 官=%.3f" % (fn, mine_v, off_v))
            else:
                mod = 1 << (8 * SIZE[t])
                if int(off_v) & (mod - 1) != int(mine_v) & (mod - 1):
                    bad.append("%s: 我=%s 官=%s" % (fn, mine_v, off_v))

        # ============ 方向 B: 官方打包 → 我方解码 ============
        # 用 pymavlink 构造同一条消息打包, 验证我方解析器读出的字段一致
        import inspect
        bad_b = []
        try:
            params = list(inspect.signature(cls.__init__).parameters)[1:]
            args = [vals.get(fn, 0) for fn in params]
            obj = cls(*args)
            raw_b = obj.pack(mavp)
            frames = mav.Parser().feed(raw_b)
            if not frames:
                bad_b.append("我方解析器拒收官方帧")
            else:
                f = frames[0]
                for fn, t in spec:
                    if "[" in t:
                        base = t[:-1].split("[")[0]
                        if base == "char":
                            if str(f.fields.get(fn, ""))[:6] != str(vals[fn]):
                                bad_b.append("%s: 官=%r 我=%r" %
                                             (fn, vals[fn], f.fields.get(fn)))
                        continue
                    if fn not in f.fields:
                        bad_b.append("%s(我方未解出)" % fn)
                        continue
                    mine_v, my_v = vals[fn], f.fields[fn]
                    if t == "f32":
                        if abs(my_v - mine_v) > 0.01:
                            bad_b.append("%s: 官=%.3f 我=%.3f" % (fn, mine_v, my_v))
                    else:
                        mod = 1 << (8 * SIZE[t])
                        if int(my_v) & (mod - 1) != int(mine_v) & (mod - 1):
                            bad_b.append("%s: 官=%s 我=%s" % (fn, mine_v, my_v))
        except Exception as e:
            bad_b.append("官方打包异常: %r" % e)

        ok = not bad and not bad_b
        all_ok &= ok
        print("%s %s(%d)%s" % ("✓" if ok else "✗", name, msgid,
              "" if ok else "\n     A(我编→官解): %s\n     B(官包→我解): %s"
              % (bad or "一致", bad_b or "一致")))

    print()
    print("结论:", "双向校验全部一致 ✓" if all_ok else "存在不一致, 请修正 MSG_DEFS ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
