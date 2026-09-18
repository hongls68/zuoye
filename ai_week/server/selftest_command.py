#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_command.py —— 第2周「远程采集指令」自测脚本
仅使用 Python 标准库，无需开发板在旁即可验证整套状态机。

用法（两个终端）：
    终端1:  python server.py                    # 先起服务
    终端2:  python server/selftest_command.py   # 默认打 http://127.0.0.1:8000

    端口不是 8000 时（例如 PORT=8010 python server.py）：
            python server/selftest_command.py http://127.0.0.1:8010

为什么要有这个脚本
------------------
第2周的题眼是「怎样证明开发板进行了新采集，而非页面重新显示了旧值」。
这个问题在真机上很难反复复现（要拔电源、要卡时间点），
所以先把状态机在本地可测服务上跑扎实，真机只用来做最后的验收。

覆盖五个场景：
    1) 正常闭环    下发 → 取走 → 回执 → 上报观测 → COMPLETED（三条证据齐全）
    2) 设备关机    指令停在 PENDING，TTL 到点转 EXPIRED，且不产生任何图像
    3) 重复点击    两次点击生成两个独立 request_id，各自独立追踪
    4) 旧值冒充    capture_ts 早于下发时刻 → E2 不通过 → FAILED（绝不置 COMPLETED）
    5) 重复上传    同一开机内 seq 不递增 → E3 不通过 → FAILED
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
TZ = timezone(timedelta(hours=8))

# 每次运行都用一套全新的虚拟设备号与开机标识。
# 否则同一台服务端上重跑第二次时，E3 会拿上一轮留下的 seq 做比较，
# 报一堆"seq 未递增"的假失败 —— 这个坑第一次就踩到了。
RUN_TAG = "%s-%04x" % (datetime.now(TZ).strftime("%H%M%S"), os.getpid() & 0xFFFF)
DEVICE = "selftest-device-" + RUN_TAG
BOOT_ID = "boot-selftest-" + RUN_TAG

# 服务端只做「FF D8 开头 / FF D9 结尾」的最简 JPEG 校验，
# 本脚本关心的是状态机而不是图像解码，所以用一个合成的最小帧即可。
FAKE_JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"

_seq = [0]
RESULTS = []


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def next_seq() -> int:
    _seq[0] += 1
    return _seq[0]


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok))
    print("    [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                             ("  —— " + detail) if detail else ""))
    return ok


def req(method: str, path: str, data=None, headers=None, raw=None):
    """发一个请求；返回 (状态码, 解析后的 JSON 或原始字节)。"""
    body = None
    hdrs = dict(headers or {})
    if raw is not None:
        body = raw
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            payload = resp.read()
            if "json" in resp.headers.get("Content-Type", ""):
                return resp.status, json.loads(payload.decode("utf-8"))
            return resp.status, payload
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return e.code, payload


def create_command(ttl_s: int = 60):
    return req("POST", "/api/command",
               {"device_id": DEVICE, "action": "capture", "ttl_s": ttl_s})


def poll_command():
    return req("GET", "/api/command/poll?device_id=" + DEVICE)


def ack_command(request_id: str, seq: int = None, state: str = None):
    body = {"request_id": request_id, "device_id": DEVICE,
            "device_ts": now_iso(), "boot_id": BOOT_ID,
            "seq": next_seq() if seq is None else seq}
    if state:
        body["state"] = state
    return req("POST", "/api/command/ack", body)


def post_frame(request_id: str, capture_ts: str, seq: int = None,
               boot_id: str = None):
    hdrs = {
        "Content-Type": "image/jpeg",
        "X-Device-Id": DEVICE,
        "X-Ts-Device": now_iso(),
        "X-Capture-Ts": capture_ts,
        "X-Boot-Id": boot_id or BOOT_ID,
        "X-Seq": str(next_seq() if seq is None else seq),
    }
    if request_id:
        hdrs["X-Request-Id"] = request_id
    return req("POST", "/api/frame", headers=hdrs, raw=FAKE_JPEG)


def status_of(request_id: str):
    st, r = req("GET", "/api/command/status?request_id=" + request_id)
    if st != 200 or not r.get("commands"):
        return None
    return r["commands"][0]


# ---------------- 场景 ----------------

def scene_1_happy_path():
    print("\n[场景1] 正常闭环：三条证据齐全，应当 COMPLETED")
    st, r = create_command()
    rid = r["command"]["request_id"]
    check("下发成功且状态为 PENDING",
          st == 201 and r["command"]["state"] == "PENDING", rid)

    st, r = poll_command()
    check("设备取走并转 RECEIVED",
          st == 200 and r["command"] and r["command"]["request_id"] == rid,
          "板端载荷字段: %s" % (sorted(r["command"].keys()) if r["command"] else "无指令"))
    cmd = status_of(rid)
    check("服务端状态确为 RECEIVED", cmd and cmd["state"] == "RECEIVED",
          cmd["state_label"] if cmd else "查不到")

    st, r = ack_command(rid)
    check("回执后转 EXECUTING",
          st == 200 and r["command"]["state"] == "EXECUTING",
          "boot_id=%s seq=%s" % (r["command"]["boot_id"], r["command"]["seq"]))

    time.sleep(0.05)                       # 保证 capture_ts 晚于 dispatched_at
    st, r = post_frame(rid, now_iso())
    ev = (r or {}).get("evidence") or {}
    check("上报观测后通过证据校验", st == 201 and ev.get("ok") is True,
          str(ev.get("reason") or "三条证据齐全"))

    cmd = status_of(rid)
    check("最终状态为 COMPLETED", cmd and cmd["state"] == "COMPLETED",
          cmd["state_label"] if cmd else "查不到")
    check("该请求有可展示图像（has_frame=True）",
          bool(cmd and cmd["has_frame"]))
    st, body = req("GET", "/api/command/frame?request_id=" + rid)
    check("按 request_id 能取到本次图像", st == 200 and body[:2] == b"\xff\xd8")
    check("状态时间线至少 5 个节点",
          bool(cmd and len(cmd["timeline"]) >= 5),
          "timeline=%d 项" % (len(cmd["timeline"]) if cmd else 0))
    return rid


def scene_2_device_off():
    print("\n[场景2] 设备关机：指令停在 PENDING，TTL 到点转 EXPIRED")
    st, r = create_command(ttl_s=5)
    rid = r["command"]["request_id"]
    cmd = status_of(rid)
    check("下发后状态为 PENDING", cmd and cmd["state"] == "PENDING")

    print("    等待 TTL（5 秒）到点…")
    time.sleep(7)
    cmd = status_of(rid)
    check("TTL 到点后转 EXPIRED", cmd and cmd["state"] == "EXPIRED",
          cmd["state_label"] if cmd else "查不到")

    st, body = req("GET", "/api/command/frame?request_id=" + rid)
    check("未完成的请求不返回任何图像（HTTP 404）", st == 404,
          "状态码 %d" % st)
    check("未完成时 has_frame 为 False", bool(cmd and not cmd["has_frame"]))


def scene_3_double_click():
    print("\n[场景3] 重复点击：两次点击 = 两个独立 request_id")
    st, a = create_command()
    st, b = create_command()
    rid_a, rid_b = a["command"]["request_id"], b["command"]["request_id"]
    check("两次点击生成不同 request_id", rid_a != rid_b,
          "%s / %s" % (rid_a, rid_b))
    ca, cb = status_of(rid_a), status_of(rid_b)
    check("两条记录各自独立追踪", ca and cb and ca["request_id"] != cb["request_id"])
    check("两条都还是 PENDING（互不影响）",
          ca["state"] == "PENDING" and cb["state"] == "PENDING")
    # 设备只取走最早的那一条
    st, r = poll_command()
    check("设备一次只取走一条（先进先出）",
          r["command"] and r["command"]["request_id"] == rid_a)
    ca, cb = status_of(rid_a), status_of(rid_b)
    check("取走 A 不影响 B 的状态",
          ca["state"] == "RECEIVED" and cb["state"] == "PENDING",
          "A=%s B=%s" % (ca["state"], cb["state"]))
    # 把 B 也清掉，避免影响后续场景
    st, r = poll_command()
    ack_command(r["command"]["request_id"])


def scene_4_stale_capture():
    print("\n[场景4] 旧值冒充：capture_ts 早于下发时刻 → E2 不通过")
    st, r = create_command()
    rid = r["command"]["request_id"]
    poll_command()
    ack_command(rid)
    # 故意把采集时刻写成「昨天」——模拟把库里的旧图重新提交一次
    old_ts = (datetime.now(TZ) - timedelta(days=1)).isoformat(
        timespec="milliseconds")
    st, r = post_frame(rid, old_ts)
    ev = (r or {}).get("evidence") or {}
    check("证据校验拒绝该观测", ev.get("ok") is False, str(ev.get("reason")))
    cmd = status_of(rid)
    check("状态为 FAILED 而不是 COMPLETED",
          cmd and cmd["state"] == "FAILED",
          cmd["state_label"] if cmd else "查不到")
    check("FAILED 记录里写清了不通过原因",
          bool(cmd and "E2" in (cmd["fail_reason"] or "")),
          (cmd["fail_reason"] or "")[:60] if cmd else "")
    st, body = req("GET", "/api/command/frame?request_id=" + rid)
    check("证据不足时也不返回图像（不给旧图冒充的机会）", st == 404)


def scene_5_replay():
    print("\n[场景5] 重复上传：同一开机内 seq 不递增 → E3 不通过")
    # 先用一帧「周期抓拍」（不带 request_id）把本开机的 seq 抬到 100。
    # 注意 E3 是按 (device_id, boot_id) 看全表的最大 seq，
    # 不区分是不是命令触发的帧 —— 这正是它能拦住重放的原因。
    st, r = post_frame(None, now_iso(), seq=100)
    check("周期帧正常入库且不参与命令闭环",
          st == 201 and (r or {}).get("request_id") is None)

    st, r = create_command()
    rid = r["command"]["request_id"]
    poll_command()
    ack_command(rid)
    time.sleep(0.05)
    # 再拿一个更小的 seq 上报，模拟"把上一次拍的那张图重传一遍冒充新拍"
    st, r = post_frame(rid, now_iso(), seq=3)
    ev = (r or {}).get("evidence") or {}
    check("重放的观测被 E3 拦下",
          ev.get("ok") is False and "E3" in (ev.get("reason") or ""),
          str(ev.get("reason")))
    cmd = status_of(rid)
    check("该请求落 FAILED 而不是 COMPLETED",
          cmd and cmd["state"] == "FAILED",
          cmd["state_label"] if cmd else "查不到")


def main() -> int:
    print("=" * 64)
    print("第2周 远程采集指令 · 状态机自测")
    print("目标服务: %s   虚拟设备: %s" % (BASE, DEVICE))
    print("=" * 64)

    st, r = req("GET", "/api/health")
    if st != 200:
        print("\n无法连接服务端，请先在另一个终端运行: python server.py")
        return 2
    print("服务端在线: %s" % r.get("server_now"))

    for scene in (scene_1_happy_path, scene_2_device_off,
                  scene_3_double_click, scene_4_stale_capture, scene_5_replay):
        try:
            scene()
        except Exception as e:            # 单个场景异常不影响其余场景
            check("%s 执行异常" % scene.__name__, False, repr(e))

    ok = sum(1 for _, v in RESULTS if v)
    bad = [(n, ) for n, v in RESULTS if not v]
    print("\n" + "=" * 64)
    print("结果：%d/%d 通过" % (ok, len(RESULTS)))
    if bad:
        print("未通过项：")
        for n, in bad:
            print("  - " + n)
    print("=" * 64)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
