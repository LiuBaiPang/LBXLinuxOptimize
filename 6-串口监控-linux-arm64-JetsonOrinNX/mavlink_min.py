#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mavlink_min —— MAVLink v1/v2 最小实现(纯 Python 标准库,零第三方依赖)。

背景: 主程序(serial_monitor.py)的"飞控 MAVLink"页需要解析/构造 MAVLink 帧,
     但不想引入 pymavlink 依赖。本模块手写实现:
  - v1(帧头 0xFE) 与 v2(帧头 0xFD) 帧解析(流式字节缓冲,自动判版本)
  - v2 的"零填充截断 payload"与"签名帧跳过"(MAVLINK_IFLAG_SIGNED)
  - CRC-16/X.25 + 每消息 CRC_EXTRA 校验(与飞控逐字节一致才收)
  - 常用消息的编解码: 心跳/系统状态/姿态/四元数/位置/GPS/HUD/RC/命令/文本/参数/版本
  - ArduPilot 与 PX4 两套 custom_mode → 飞行模式名 映射(按 autopilot 字段自动选)

直接运行本文件可自测: python3 mavlink_min.py
"""
import struct
import time

MAVLINK_V1_STX = 0xFE
MAVLINK_V2_STX = 0xFD
MAVLINK_IFLAG_SIGNED = 0x01

# ---------------- 消息定义表: msgid -> (名称, CRC_EXTRA, [(字段名, 类型), ...]) ----------------
# 类型: u8 i8 u16 i16 u32 i32 u64 f32 char[N] u8[N]  (MAVLink 全部小端)
# 注意: v2 消息的"扩展字段"一律排在定义末尾,截断 payload 时按需少解即可。
MSG_DEFS = {
    0:   ("HEARTBEAT", 50, [
            ("custom_mode", "u32"), ("type", "u8"), ("autopilot", "u8"),
            ("base_mode", "u8"), ("system_status", "u8"), ("mavlink_version", "u8")]),
    1:   ("SYS_STATUS", 124, [
            ("onboard_control_sensors_present", "u16"), ("onboard_control_sensors_enabled", "u16"),
            ("onboard_control_sensors_health", "u16"), ("load", "u16"),
            ("voltage_battery", "u16"), ("current_battery", "i16"),
            ("drop_rate_comm", "u8"), ("errors_comm", "u8"),
            ("errors_count1", "u8"), ("errors_count2", "u8"), ("battery_remaining", "i8")]),
    2:   ("SYSTEM_TIME", 137, [("time_unix_usec", "u64"), ("time_boot_ms", "u32")]),
    4:   ("PING", 237, [
            ("time_usec", "u64"), ("seq", "u32"),
            ("target_system", "u8"), ("target_component", "u8")]),
    11:  ("SET_MODE", 89, [
            ("target_system", "u8"), ("base_mode", "u8"), ("custom_mode", "u32")]),
    20:  ("PARAM_REQUEST_READ", 214, [
            ("target_system", "u8"), ("target_component", "u8"),
            ("param_id", "char[16]"), ("param_index", "i16")]),
    22:  ("PARAM_VALUE", 220, [
            ("param_id", "char[16]"), ("param_value", "f32"), ("param_type", "u8"),
            ("param_count", "u16"), ("param_index", "u16")]),
    24:  ("GPS_RAW_INT", 24, [
            ("time_usec", "u64"), ("fix_type", "u8"), ("lat", "i32"), ("lon", "i32"),
            ("alt", "i32"), ("eph", "u16"), ("epv", "u16"), ("vel", "u16"), ("cog", "u16"),
            ("satellites_visible", "u8"),
            # --- 以下为 v2 扩展字段(截断 payload 时自动省略) ---
            ("alt_ellipsoid", "i32"), ("h_acc", "u32"), ("v_acc", "u32"),
            ("vel_c", "u16"), ("vn", "i16"), ("ve", "i16"), ("vd", "i16")]),
    30:  ("ATTITUDE", 39, [
            ("time_boot_ms", "u32"),
            ("roll", "f32"), ("pitch", "f32"), ("yaw", "f32"),
            ("rollspeed", "f32"), ("pitchspeed", "f32"), ("yawspeed", "f32")]),
    31:  ("ATTITUDE_QUATERNION", 246, [
            ("time_boot_ms", "u32"),
            ("q1", "f32"), ("q2", "f32"), ("q3", "f32"), ("q4", "f32"),
            ("rollspeed", "f32"), ("pitchspeed", "f32"), ("yawspeed", "f32")]),
    33:  ("GLOBAL_POSITION_INT", 104, [
            ("time_boot_ms", "u32"), ("lat", "i32"), ("lon", "i32"),
            ("alt", "i32"), ("relative_alt", "i32"),
            ("vx", "i16"), ("vy", "i16"), ("vz", "i16"), ("hdg", "u16")]),
    65:  ("RC_CHANNELS", 118, [
            ("time_boot_ms", "u32"), ("chancount", "u8"),
            ("chan1_raw", "u16"), ("chan2_raw", "u16"), ("chan3_raw", "u16"),
            ("chan4_raw", "u16"), ("chan5_raw", "u16"), ("chan6_raw", "u16"),
            ("chan7_raw", "u16"), ("chan8_raw", "u16"), ("chan9_raw", "u16"),
            ("chan10_raw", "u16"), ("chan11_raw", "u16"), ("chan12_raw", "u16"),
            ("chan13_raw", "u16"), ("chan14_raw", "u16"), ("chan15_raw", "u16"),
            ("chan16_raw", "u16"), ("chan17_raw", "u16"), ("chan18_raw", "u16"),
            ("rssi", "u8")]),
    66:  ("REQUEST_DATA_STREAM", 148, [
            ("target_system", "u8"), ("target_component", "u8"),
            ("req_stream_id", "u8"), ("req_message_rate", "u16"), ("start_stop", "u8")]),
    74:  ("VFR_HUD", 20, [
            ("airspeed", "f32"), ("groundspeed", "f32"), ("heading", "i16"),
            ("throttle", "u16"), ("alt", "f32"), ("climb", "f32")]),
    76:  ("COMMAND_LONG", 152, [
            ("param1", "f32"), ("param2", "f32"), ("param3", "f32"), ("param4", "f32"),
            ("param5", "f32"), ("param6", "f32"), ("param7", "f32"),
            ("command", "u16"), ("target_system", "u8"),
            ("target_component", "u8"), ("confirmation", "u8")]),
    77:  ("COMMAND_ACK", 143, [
            ("command", "u16"), ("result", "u8"),
            # --- v2 扩展 ---
            ("progress", "u8"), ("result_param2", "i32"),
            ("target_system", "u8"), ("target_component", "u8")]),
    147: ("BATTERY_STATUS", 154, [
            ("id", "u8"), ("battery_function", "u8"), ("type", "u8"),
            ("temperature", "i16"), ("voltages", "u16[10]"),
            ("current_battery", "i16"), ("current_consumed", "i32"),
            ("energy_consumed", "i32"), ("battery_remaining", "i8")]),
    148: ("AUTOPILOT_VERSION", 178, [
            ("capabilities", "u64"), ("flight_sw_version", "u32"),
            ("middleware_sw_version", "u32"), ("os_sw_version", "u32"),
            ("board_version", "u32"),
            ("flight_custom_version", "u8[8]"), ("middleware_custom_version", "u8[8]"),
            ("os_custom_version", "u8[8]"), ("vendor_id", "u16"), ("product_id", "u16"),
            ("uid", "u64")]),
    253: ("STATUSTEXT", 83, [
            ("severity", "u8"), ("text", "char[50]")]),
}

# 类型 -> struct 格式(标量部分)
_SCALAR = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
           "u32": "I", "i32": "i", "u64": "Q", "f32": "f"}


def type_size(t):
    """单字段在报文里的字节数。"""
    if "[" in t:                      # char[50] / u16[10] / u8[8]
        base, n = t[:-1].split("[")
        return _SCALAR.get(base, 1) and int(n) * struct.calcsize("<" + _SCALAR[base])
    return struct.calcsize("<" + _SCALAR[t])


def msg_size(msgid):
    """整条消息 payload 的完整长度(v1 视角,不含扩展截断)。"""
    return sum(type_size(t) for _, t in MSG_DEFS[msgid][2])


def crc_x25(data, crc=0xFFFF):
    """MAVLink 使用的 CRC-16/X.25(初值与末值异或均为 0xFFFF, 反射多项式 0x8408)。"""
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def frame_crc(header_after_stx, payload, crc_extra):
    """按 MAVLink 规则算校验: 头(不含 STX) + 线上 payload(截断态) + CRC_EXTRA。"""
    c = crc_x25(header_after_stx)
    return crc_x25(payload, c) ^ 0xFFFF ^ crc_x25(bytes([crc_extra])) ^ 0xFFFF


# ---------------- 帧解析器 ----------------

class Frame(object):
    __slots__ = ("is_v2", "seq", "sysid", "compid", "msgid", "name",
                 "fields", "wire_len", "ts", "bad_crc")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return "<%s sysid=%d compid=%d %s>" % (
            "v2" if self.is_v2 else "v1", self.sysid, self.compid, self.name)


class Parser(object):
    """流式解析器: feed(字节) 即可, 函数内部攒缓冲, 返回本轮解出的 Frame 列表。

    校验失败的帧直接丢弃(与 pymavlink 行为一致), 并计数到 self.bad_frames。
    """

    def __init__(self):
        self.buf = bytearray()
        self.bad_frames = 0
        self.frames_v1 = 0
        self.frames_v2 = 0
        # 状态: 0=等帧头 1=收完整帧中
        self._st = 0
        self._need = 0
        self._hdr = bytearray()

    def feed(self, data):
        out = []
        if data:
            self.buf += data
        while True:
            if self._st == 0:
                # 丢掉帧头前的杂散字节, 认出 v1/v2
                while self.buf:
                    b = self.buf[0]
                    if b == MAVLINK_V2_STX:
                        self._is_v2 = True
                        break
                    if b == MAVLINK_V1_STX:
                        self._is_v2 = False
                        break
                    del self.buf[0]
                if not self.buf:
                    return out
                self._st = 1
                self._hdr = bytearray()
                # v1 头: len seq sysid compid msgid        = 5 字节
                # v2 头: len iflag cflag seq sysid compid msgid[3] = 9 字节
                self._need = 9 if self._is_v2 else 5
            else:
                take = min(self._need - len(self._hdr), len(self.buf))
                self._hdr += self.buf[:take]
                del self.buf[:take]
                if len(self._hdr) < self._need:
                    return out
                f = self._parse_one()
                if f is not None:
                    out.append(f)
                self._st = 0

    def _parse_one(self):
        h = self._hdr
        if self._is_v2:
            plen, iflag = h[0], h[1]
            seq, sysid, compid = h[3], h[4], h[5]
            msgid = h[6] | (h[7] << 8) | (h[8] << 16)
            header_crc_part = h[0:9]          # len..msgid
        else:
            plen = h[0]
            seq, sysid, compid, msgid = h[1], h[2], h[3], h[4]
            header_crc_part = h[0:5]
        # 需要 payload + 2 字节校验
        total = plen + 2
        if len(self.buf) < total:
            # 不够, 把已收的头部缓存回 buf 前端, 状态保持等 payload
            self.buf[0:0] = h
            self._st = 0
            self._hdr = bytearray()
            # 重新进入等帧头状态会把刚才认掉的 STX 当杂散丢掉 —— 所以改用
            # 专门的"等 payload"状态: 这里直接回到状态 1 但 need=0 头已齐。
            # 简化处理: 把头放回 buf 后回到找帧头会死循环, 因此我们改用
            # 独立状态 2 (头已解好, 只等 payload)。
            self._st = 2
            self._ctx = (self._is_v2, plen, seq, sysid, compid, msgid,
                         bytes(header_crc_part))
            return None
        payload = bytes(self.buf[:plen])
        crc_wire = self.buf[plen] | (self.buf[plen + 1] << 8)
        del self.buf[:total]
        # 签名帧: 后面还有 13 字节, 跳过
        if self._is_v2 and (iflag & MAVLINK_IFLAG_SIGNED):
            if len(self.buf) < 13:
                # 签名不完整: 整帧放弃(罕见, 一般只在加了密钥的链路)
                self.buf.clear()
                self.bad_frames += 1
                return None
            del self.buf[:13]
        return self._finish(self._is_v2, plen, seq, sysid, compid, msgid,
                            header_crc_part, payload, crc_wire)

    def _finish(self, is_v2, plen, seq, sysid, compid, msgid,
                header_crc_part, payload, crc_wire):
        if is_v2:
            self.frames_v2 += 1
        else:
            self.frames_v1 += 1
        name = MSG_DEFS.get(msgid, (None,))[0]
        if name is None:
            return Frame(is_v2=is_v2, seq=seq, sysid=sysid, compid=compid,
                         msgid=msgid, name="UNK_%d" % msgid, fields={},
                         wire_len=plen + (9 if is_v2 else 5) + 2,
                         ts=time.time(), bad_crc=False)
        crc_extra = MSG_DEFS[msgid][1]
        calc = frame_crc(header_crc_part, payload, crc_extra)
        if calc != crc_wire:
            self.bad_frames += 1
            return None
        return Frame(is_v2=is_v2, seq=seq, sysid=sysid, compid=compid,
                     msgid=msgid, name=name,
                     fields=self._decode(msgid, payload),
                     wire_len=plen + (9 if is_v2 else 5) + 2,
                     ts=time.time(), bad_crc=False)

    @staticmethod
    def _decode(msgid, payload):
        _, _, spec = MSG_DEFS[msgid]
        fields = {}
        off = 0
        for name, t in spec:
            if off >= len(payload) and "[" not in t:
                break                       # v2 截断: 后面按 0 处理, 不再解
            if "[" in t:
                base, n = t[:-1].split("[")
                n = int(n)
                sz = struct.calcsize("<" + _SCALAR[base])
                chunk = payload[off:off + n * sz]
                off += n * sz
                if base == "char":
                    fields[name] = chunk.split(b"\x00")[0].decode(
                        "utf-8", "replace")
                else:
                    fmt = "<%d%s" % (n, _SCALAR[base])
                    vals = list(struct.unpack(fmt, chunk.ljust(n * sz, b"\x00")))
                    # BATTERY_STATUS voltages 特例: 0xFFFF = 无此电芯
                    if t == "u16[10]":
                        vals = [v for v in vals if v != 0xFFFF]
                    fields[name] = vals
            else:
                fmt = "<" + _SCALAR[t]
                sz = struct.calcsize(fmt)
                chunk = payload[off:off + sz]
                if len(chunk) < sz:
                    fields[name] = 0        # v2 截断尾
                    continue
                off += sz
                fields[name] = struct.unpack(fmt, chunk)[0]
        return fields


# ---------------- 帧构造 ----------------

def encode(msgid, fields, sysid=255, compid=190, seq=0, is_v2=True):
    """构造一帧 MAVLink。fields 为 {字段名: 值} 缺省补 0。
    sysid=255/compid=190 是地面站惯用值。返回 bytes。"""
    if msgid not in MSG_DEFS:
        raise ValueError("未支持的消息 id=%d" % msgid)
    name, crc_extra, spec = MSG_DEFS[msgid]
    payload = bytearray()
    for fname, t in spec:
        v = fields.get(fname)
        if "[" in t:
            base, n = t[:-1].split("[")
            n = int(n)
            if base == "char":
                s = ("" if v is None else str(v)).encode("utf-8")[:n]
                payload += s.ljust(n, b"\x00")
            else:
                if v is None:
                    v = []
                if t == "u16[10]":
                    v = list(v) + [0xFFFF] * (n - len(v))   # 无电芯填 0xFFFF
                else:
                    v = list(v) + [0] * (n - len(v))
                payload += struct.pack("<%d%s" % (n, _SCALAR[base]), *v[:n])
        else:
            if v is None:
                v = 0
            payload += struct.pack("<" + _SCALAR[t], v)
    plen = len(payload)
    if is_v2:
        # v2 允许且鼓励去掉尾部 0 的截断, 但为了简单稳定发送完整长度
        hdr = bytes([plen, 0, 0, seq & 0xFF, sysid & 0xFF, compid & 0xFF,
                     msgid & 0xFF, (msgid >> 8) & 0xFF, (msgid >> 16) & 0xFF])
        crc = frame_crc(hdr, bytes(payload), crc_extra)
        return bytes([MAVLINK_V2_STX]) + hdr + bytes(payload) + \
            bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    hdr = bytes([plen, seq & 0xFF, sysid & 0xFF, compid & 0xFF, msgid & 0xFF])
    crc = frame_crc(hdr, bytes(payload), crc_extra)
    return bytes([MAVLINK_V1_STX]) + hdr + bytes(payload) + \
        bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# ---------------- 便捷构造器 ----------------

def heartbeat(autopilot, mav_type, base_mode, custom_mode, sysid=255, compid=190,
              seq=0, status=0):
    return encode(0, dict(custom_mode=custom_mode, type=mav_type,
                          autopilot=autopilot, base_mode=base_mode,
                          system_status=status, mavlink_version=3),
                  sysid=sysid, compid=compid, seq=seq)


def command_long(command, p1=0, p2=0, p3=0, p4=0, p5=0, p6=0, p7=0,
                 target_system=1, target_component=1, sysid=255, compid=190, seq=0):
    return encode(76, dict(param1=p1, param2=p2, param3=p3, param4=p4,
                           param5=p5, param6=p6, param7=p7, command=command,
                           target_system=target_system,
                           target_component=target_component, confirmation=0),
                  sysid=sysid, compid=compid, seq=seq)


def request_data_stream(target_system=1, target_component=1, rate_hz=10,
                        sysid=255, compid=190, seq=0):
    # stream_id=0 表示"全部流", start_stop=1 启动
    return encode(66, dict(target_system=target_system,
                           target_component=target_component,
                           req_stream_id=0, req_message_rate=rate_hz,
                           start_stop=1),
                  sysid=sysid, compid=compid, seq=seq)


def set_message_interval(msgid, interval_us, target_system=1, target_component=1,
                         sysid=255, compid=190, seq=0):
    # MAV_CMD_SET_MESSAGE_INTERVAL (511): param1=msg_id param2=间隔us
    return command_long(511, p1=msgid, p2=interval_us,
                        target_system=target_system,
                        target_component=target_component,
                        sysid=sysid, compid=compid, seq=seq)


def request_message(msgid, target_system=1, target_component=1,
                    sysid=255, compid=190, seq=0):
    # MAV_CMD_REQUEST_MESSAGE (512)
    return command_long(512, p1=msgid, target_system=target_system,
                        target_component=target_component,
                        sysid=sysid, compid=compid, seq=seq)


def request_param_read(param_id, target_system=1, target_component=1,
                       sysid=255, compid=190, seq=0):
    return encode(20, dict(target_system=target_system,
                           target_component=target_component,
                           param_id=param_id, param_index=-1),
                  sysid=sysid, compid=compid, seq=seq)


def ping(time_usec, seqn, target_system=0, target_component=0,
         sysid=255, compid=190, seq=0):
    return encode(4, dict(time_usec=time_usec, seq=seqn,
                          target_system=target_system,
                          target_component=target_component),
                  sysid=sysid, compid=compid, seq=seq)


# ---------------- 枚举与模式表 ----------------

MAV_TYPE_NAMES = {
    0: "通用", 1: "固定翼", 2: "四旋翼", 3: "共轴", 4: "直升机", 5: "飞艇",
    6: "气球", 7: "火箭", 8: "地面车", 9: "水面船", 10: "潜水器",
    11: "地面站", 12: "增程站", 18: "六旋翼", 19: "半四旋翼", 20: "八旋翼",
    21: "三旋翼", 22: "四旋翼H", 23: "四旋翼X", 24: "垂直起降双桨", 25: "垂直起降四桨",
}

MAV_AUTOPILOT_NAMES = {
    0: "通用", 1: "Pixhawk(旧)", 3: "ArduPilot", 12: "PX4",
}

MAV_STATE_NAMES = {
    0: "未初始化", 1: "启动中", 2: "校准中", 3: "待机", 4: "活动",
    5: "临界", 6: "紧急", 7: "关机", 8: "飞行终止",
}

MAV_FIX_NAMES = {
    0: "无GPS", 1: "无定位", 2: "2D定位", 3: "3D定位",
    4: "3D+DGPS", 5: "3D+RTK浮动", 6: "3D+RTK固定",
}

CMD_RESULT_NAMES = {
    0: "接受", 1: "临时拒绝", 2: "拒绝", 3: "参数越界", 4: "已拒绝",
    5: "当前模式不允许", 6: "无系统", 7: "失败", 255: "无结果/超时",
}

STATUS_SEVERITY_NAMES = ["紧急", "警报", "严重", "错误", "警告",
                         "通知", "信息", "调试"]

MAV_CMD_NAMES = {
    16: "NAV_WAYPOINT", 22: "NAV_TAKEOFF", 21: "NAV_LAND", 31: "NAV_RETURN_TO_LAUNCH",
    85: "NAV_VTOL_TAKEOFF", 86: "NAV_VTOL_LAND", 176: "DO_SET_MODE",
    179: "DO_SET_HOME", 250: "PREFLIGHT_CALIBRATION", 241: "PREFLIGHT_REBOOT_SHUTDOWN",
    400: "COMPONENT_ARM_DISARM", 410: "CONTROL_HIGH_LATENCY",
    511: "SET_MESSAGE_INTERVAL", 512: "REQUEST_MESSAGE",
    519: "DO_SET_SERVO", 520: "DO_REPEAT_SERVO", 530: "DO_FLIGHTTERMINATION",
}

# --- ArduPilot 模式表(custom_mode 就是模式编号) ---
ARDUPILOT_MODES = {
    2: {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
        5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT",
        12: "SPORT", 13: "FLIP", 14: "AUTOTUNE", 15: "POSHOLD", 16: "BRAKE",
        17: "THROW", 19: "GUIDED_NOGPS", 20: "SMART_RTL", 21: "FLOWHOLD",
        22: "FOLLOW", 23: "ZIGZAG", 25: "AUTOROTATE", 26: "AUTO_RTL"},
    1: {0: "MANUAL", 1: "CIRCLE", 2: "STABILIZE", 3: "TRAINING", 4: "ACRO",
        5: "FBWA", 6: "FBWB", 7: "CRUISE", 8: "AUTOTUNE", 10: "AUTO",
        11: "RTL", 12: "LOITER", 13: "TAKEOFF", 15: "GUIDED",
        17: "QSTABILIZE", 18: "QHOVER", 19: "QLOITER", 20: "QLAND",
        21: "QRTL", 22: "QAUTOTUNE", 23: "QACRO"},
    8: {0: "MANUAL", 1: "ACRO", 3: "STEERING", 4: "HOLD", 5: "LOITER",
        10: "AUTO", 11: "RTL", 12: "SMART_RTL", 15: "GUIDED", 17: "INITIALISING"},
}

# --- PX4 模式表(custom_mode = main_mode<<16 | sub_mode<<8? 不: = main<<16|sub) ---
PX4_MAIN_MODES = {1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 4: "AUTO",
                  5: "ACRO", 6: "OFFBOARD", 7: "STABILIZED", 8: "RATTITUDE"}
PX4_AUTO_SUB_MODES = {1: "READY", 2: "TAKEOFF", 3: "HOLD(LOITER)", 4: "MISSION",
                      5: "RTL", 6: "LAND", 7: "FOLLOW", 8: "PRECISION_LAND",
                      9: "VTOL_TAKEOFF", 10: "VTOL_LAND"}
# GUI 下拉用: PX4 (main, sub) -> base_mode/custom_mode
PX4_MODE_CHOICES = [
    ("MANUAL", 1, 0), ("STABILIZED", 7, 0), ("ACRO", 5, 0),
    ("RATTITUDE", 8, 0), ("ALTCTL", 2, 0), ("POSCTL", 3, 0),
    ("AUTO READY", 4, 1), ("AUTO TAKEOFF", 4, 2), ("AUTO HOLD", 4, 3),
    ("AUTO MISSION", 4, 4), ("AUTO RTL", 4, 5), ("AUTO LAND", 4, 6),
    ("OFFBOARD", 6, 0),
]
ARDU_MODE_CHOICES = [
    ("STABILIZE", 0), ("ACRO", 1), ("ALT_HOLD", 2), ("AUTO", 3),
    ("GUIDED", 4), ("LOITER", 5), ("RTL", 6), ("CIRCLE", 7), ("LAND", 9),
    ("POSHOLD", 15), ("BRAKE", 16), ("SMART_RTL", 20), ("AUTO_RTL", 26),
]


def ardupilot_mode(mav_type, custom_mode):
    fam = 2 if mav_type in (2, 3, 4, 13, 14, 15, 18, 19, 20, 21, 22, 23) else \
          1 if mav_type in (1, 24, 25) else \
          8 if mav_type in (8, 9, 10) else 2
    return ARDUPILOT_MODES[fam].get(custom_mode, "未知(%d)" % custom_mode)


def px4_mode(custom_mode):
    main = (custom_mode >> 16) & 0xFF
    sub = (custom_mode >> 24) & 0xFF   # PX4: sub_mode 在高 8 位? 官方: main<<24|sub<<16
    # 实际 PX4 定义: custom_mode = (main_mode << 24) | (sub_mode << 16)
    main = (custom_mode >> 24) & 0xFF
    sub = (custom_mode >> 16) & 0xFF
    m = PX4_MAIN_MODES.get(main, "主模式%d" % main)
    if main == 4 and sub:
        m += " / " + PX4_AUTO_SUB_MODES.get(sub, "子模式%d" % sub)
    return m


def mode_name(autopilot, mav_type, base_mode, custom_mode):
    """按 autopilot 类型选映射, 返回模式名。"""
    if autopilot == 3:                       # MAV_AUTOPILOT_ARDUPILOTMEGA
        return ardupilot_mode(mav_type, custom_mode)
    if autopilot == 12:                      # MAV_AUTOPILOT_PX4
        return px4_mode(custom_mode)
    if base_mode & 1:                        # MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        return "custom=%d" % custom_mode
    return "base_mode=0x%02X" % base_mode


def fw_version_str(v):
    """flight_sw_version 的打包格式: major<<24 | minor<<16 | patch<<8 | type"""
    if not v:
        return "-"
    return "%d.%d.%d" % ((v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF)


def quat_to_rpy(q1, q2, q3, q4):
    """四元数 -> 欧拉角(弧度), 公式与 mavlink 官方一致。"""
    import math
    roll = math.atan2(2 * (q1 * q2 + q3 * q4), 1 - 2 * (q2 * q2 + q3 * q3))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (q1 * q3 - q2 * q4))))
    yaw = math.atan2(2 * (q1 * q4 + q2 * q3), 1 - 2 * (q3 * q3 + q4 * q4))
    return roll, pitch, yaw


# ---------------- 自测 ----------------

def _selftest():
    ok = 0

    def check(cond, desc):
        nonlocal ok
        assert cond, "自测失败: " + desc
        ok += 1
        print("  ✓ %s" % desc)

    # 1. encode -> parse 往返(v2)
    raw = encode(30, dict(time_boot_ms=1234, roll=0.1, pitch=-0.2, yaw=3.0,
                          rollspeed=0, pitchspeed=0, yawspeed=0))
    f = Parser().feed(raw)[0]
    check(f.name == "ATTITUDE" and abs(f.fields["roll"] - 0.1) < 1e-6
          and abs(f.fields["pitch"] + 0.2) < 1e-6, "v2 ATTITUDE 编解码往返")

    # 2. v1 往返
    raw = encode(0, dict(custom_mode=3, type=2, autopilot=3, base_mode=209,
                         system_status=4, mavlink_version=3), is_v2=False)
    f = Parser().feed(raw)[0]
    check(f.name == "HEARTBEAT" and not f.is_v2 and f.fields["custom_mode"] == 3
          and mode_name(3, 2, 209, 3) == "AUTO", "v1 HEARTBEAT 往返 + ArduPilot 模式")

    # 3. 篡改 1 个比特必须被 CRC 拦下
    p = Parser()
    raw = bytearray(encode(33, dict(time_boot_ms=1, lat=31000000, lon=121000000,
                                    alt=10000, relative_alt=5000,
                                    vx=0, vy=0, vz=0, hdg=65535)))
    raw[-2] ^= 0x01
    out = p.feed(bytes(raw))
    check(len(out) == 0 and p.bad_frames == 1, "CRC 校验拦截坏帧")

    # 4. 长消息 char[50] + 分片喂入(模拟串口粘包/断包)
    p = Parser()
    raw = encode(253, dict(severity=6, text="Hello 世界 from PX4"))
    got = []
    for i in range(0, len(raw), 3):         # 每 3 字节喂一次
        got += p.feed(raw[i:i + 3])
    check(len(got) == 1 and got[0].fields["text"] == "Hello 世界 from PX4",
          "STATUSTEXT 中文 + 分片粘包")

    # 5. v2 截断 payload(GPS_RAW_INT 只发 v1 部分)
    _, ce, spec = MSG_DEFS[24]
    full = bytearray(msg_size(24))
    struct.pack_into("<Q", full, 0, 123456789)
    full[8] = 3                              # fix_type
    struct.pack_into("<i", full, 9, 31000000)
    struct.pack_into("<i", full, 13, 121000000)
    hdr = bytes([10, 0, 0, 7, 1, 1, 24, 0, 0])
    crc = frame_crc(hdr, bytes(full[:10]), ce)
    raw = bytes([0xFD]) + hdr + bytes(full[:10]) + bytes([crc & 0xFF, crc >> 8])
    f = Parser().feed(raw)[0]
    check(f.name == "GPS_RAW_INT" and f.fields["fix_type"] == 3
          and f.fields["satellites_visible"] == 0,
          "v2 截断 payload 解析(扩展字段缺省为 0)")

    # 6. 杂散字节 + 两个连续帧
    p = Parser()
    a = encode(0, dict(custom_mode=0, type=2, autopilot=12, base_mode=29,
                       system_status=3, mavlink_version=3))
    b = encode(74, dict(airspeed=12.5, groundspeed=13.5, heading=180,
                        throttle=55, alt=100.5, climb=-1.5))
    f1, f2 = p.feed(b"\x00\xff\xab" + a + b)
    check(f1.name == "HEARTBEAT" and f2.name == "VFR_HUD"
          and mode_name(12, 2, 29, 0) == "MANUAL"
          and abs(f2.fields["alt"] - 100.5) < 1e-5, "杂散字节 + 连续两帧")

    # 7. PX4 模式解码: AUTO/MISSION => main=4 sub=4
    check(px4_mode((4 << 24) | (4 << 16)) == "AUTO / MISSION", "PX4 模式位解码")

    # 8. COMMAND_LONG 往返(解锁指令)
    raw = command_long(400, p1=1.0)
    f = Parser().feed(raw)[0]
    check(f.name == "COMMAND_LONG" and f.fields["command"] == 400
          and abs(f.fields["param1"] - 1.0) < 1e-6, "COMMAND_LONG 解锁帧")

    # 9. 四元数 -> 欧拉角
    import math
    r, pi_, y = quat_to_rpy(1.0, 0.0, 0.0, 0.0)
    check(abs(r) < 1e-9 and abs(pi_) < 1e-9 and abs(y) < 1e-9, "单位四元数转欧拉角")

    print("自测通过: %d 项" % ok)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
