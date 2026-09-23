#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trace_closed_loop.py —— 把一次远程采集的「请求 → 设备回执 → 新观测」三处记录并排打出来

为什么要这个脚本
----------------
第 2 周的交付物之一是「**请求—设备回执—新观测记录**」——
不是三个孤立的接口，而是**同一条 `request_id` 在三处留下的痕迹**。
课堂上老师会问：这三处凭什么说是同一次采集？

三处分别在：
    ① 指令记录   commands 表       —— 谁下的、什么时候下的、现在到哪一步
    ② 设备回执   commands 表的
                 boot_id/seq/ack_at —— 哪一次开机、第几帧、什么时候回的话
    ③ 观测记录   frames 表          —— 图什么时候采的、内容指纹是什么

本脚本把三处并排打出来，并逐条核对 E1/E2/E3，
最后给一句结论：**这张图能不能算「本次新采集」**。

用法
----
    # 1) 跑一次完整闭环（虚拟设备，不碰真板子），然后立刻追踪它
    python server/trace_closed_loop.py

    # 2) 只追踪已经存在的一条指令（真机跑完之后用这个）
    python server/trace_closed_loop.py --request-id req-20260923-abc123

    # 3) 服务端不在默认端口
    python server/trace_closed_loop.py --base http://127.0.0.1:8010

    # 4) 真机上跑完闭环后，直接追最近一条
    python server/trace_closed_loop.py --latest --device s3eye-group07

说明
----
* 只用标准库，不需要开发板。
* `--request-id` 模式是**只读**的：不新建指令、不改任何状态，可以放心对生产库用。
* 默认模式会用一个 `trace-demo-<时间>-<pid>` 的虚拟设备号，不干扰真板子的记录。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

# 本机若开着 HTTP 代理（Clash 之类），连 127.0.0.1 的请求也会被劫走 → 502 / IncompleteRead。
# 本脚本只打本机，显式绕开代理。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 服务端只做「FF D8 开头 / FF D9 结尾」的最简 JPEG 校验，
# 本脚本关心的是追踪链路而不是图像解码，用最小合成帧即可。
FAKE_JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"

_SEQ = [0]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def req(base: str, method: str, path: str, data=None, headers=None, raw=None):
    """发一个请求；返回 (状态码, 解析后的 JSON 或原始字节)。"""
    body = None
    hdrs = dict(headers or {})
    if raw is not None:
        body = raw
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=body, headers=hdrs, method=method)
    try:
        with OPENER.open(r, timeout=15) as resp:
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
    except urllib.error.URLError as e:
        print("[错误] 连不上服务端 %s —— %s" % (base, e))
        print("       请先运行: cd server && python server.py")
        sys.exit(2)


# ---------------- 造一次完整闭环（仅默认模式） ----------------

def run_closed_loop(base: str, device: str) -> str:
    """模拟板子走完 下发 → 取走 → 回执 → 传帧，返回 request_id。"""
    boot_id = "boot-trace-%s" % device.rsplit("-", 1)[-1]

    st, r = req(base, "POST", "/api/command",
                {"device_id": device, "action": "capture", "ttl_s": 60})
    rid = r["command"]["request_id"]
    print("  下发指令      -> %s  state=%s" % (rid, r["command"]["state"]))

    st, r = req(base, "GET", "/api/command/poll?device_id=" + device)
    # 板端载荷刻意只有"执行什么"，不含 state/timeline（省 MCU 内存），
    # 所以这里只能核对取走的是不是刚才那条。
    got = (r.get("command") or {}).get("request_id")
    print("  设备取走      -> %s  （取走即置 RECEIVED）"
          % ("同一条指令" if got == rid else "取到 %s" % got))

    _SEQ[0] += 1
    st, r = req(base, "POST", "/api/command/ack",
                {"request_id": rid, "device_id": device, "device_ts": now_iso(),
                 "boot_id": boot_id, "seq": _SEQ[0]})
    print("  设备回执      -> state=%s  boot_id=%s seq=%s"
          % (r["command"]["state"], r["command"]["boot_id"], r["command"]["seq"]))

    time.sleep(0.05)                       # 让 capture_ts 严格晚于 dispatched_at
    _SEQ[0] += 1
    st, r = req(base, "POST", "/api/frame", raw=FAKE_JPEG, headers={
        "Content-Type": "image/jpeg",
        "X-Device-Id": device,
        "X-Ts-Device": now_iso(),
        "X-Capture-Ts": now_iso(),
        "X-Boot-Id": boot_id,
        "X-Seq": str(_SEQ[0]),
        "X-Request-Id": rid,
    })
    ev = (r or {}).get("evidence") or {}
    print("  上传新观测    -> HTTP %s  evidence_ok=%s" % (st, ev.get("ok")))
    return rid


def find_latest(base: str, device: str) -> str:
    """按 device_id 找最近一条指令，用于 --latest。"""
    st, r = req(base, "GET", "/api/command/status?device_id=" + device)
    rows = (r or {}).get("commands") or []
    if not rows:
        print("[错误] 设备 %s 名下没有任何指令记录。" % device)
        sys.exit(1)
    return rows[0]["request_id"]


# ---------------- 追踪 ----------------

def rule(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def kv(label: str, value, note: str = "") -> None:
    v = "—" if value in (None, "") else str(value)
    print("  %-14s %s%s" % (label, v, ("    # " + note) if note else ""))


def trace(base: str, rid: str) -> int:
    st, body = req(base, "GET", "/api/command/status?request_id=" + rid)
    rows = (body or {}).get("commands") or []
    if st != 200 or not rows:
        print("[错误] 查不到 request_id=%s 的指令记录（HTTP %s）" % (rid, st))
        return 1
    cmd = rows[0]
    device = cmd["device_id"]

    rule("一次远程采集的三处记录 · request_id = %s" % rid)

    # ---- ① 指令记录 ----
    print("\n① 指令记录（commands 表）—— 谁下的、下到哪一步了")
    kv("device_id", cmd["device_id"])
    kv("action", cmd.get("action"))
    kv("created_at", cmd.get("created_at"), "网页点下按钮的时刻")
    kv("dispatched_at", cmd.get("dispatched_at"), "板子取走的时刻（不是创建时刻）")
    kv("ttl_s", cmd.get("ttl_s"))
    kv("state", cmd.get("state"), cmd.get("state_label") or "")

    # ---- ② 设备回执 ----
    print("\n② 设备回执（板子报上来的，服务端原样存着）")
    kv("boot_id", cmd.get("boot_id"), "哪一次开机 —— E3 的比较范围")
    kv("seq", cmd.get("seq"), "这次开机内第几帧 —— E3 的单调性依据")
    kv("device_ts", cmd.get("device_ts"), "板子的钟，只作参考")
    kv("ack_at", cmd.get("ack_at"), "服务端的钟，收到回执的时刻")
    kv("capture_ts", cmd.get("capture_ts"), "板子说这一帧是什么时候采的")

    # ---- ③ 观测记录 ----
    print("\n③ 观测记录（frames 表）—— 图本身的元数据")
    st2, gal = req(base, "GET",
                   "/api/frames?device_id=%s&limit=200" % urllib.parse.quote(device))
    mine = [f for f in ((gal or {}).get("frames") or [])
            if f.get("request_id") == rid]
    if not mine:
        print("  （没有找到属于本次 request_id 的帧）")
    for f in mine:
        kv("frames.id", f["id"])
        kv("source", f.get("source"), "web_manual=命令触发 / periodic=周期 / backlog=补传")
        kv("capture_ts", f.get("capture_ts"))
        kv("ts_server", f.get("ts_server"), "服务端收到的时刻 —— 判定用它")
        kv("bytes", f.get("bytes"))
        kv("尺寸", "%sx%s" % (f.get("width"), f.get("height")))
        kv("sha256", (f.get("sha256") or "")[:32] + "…",
           "原图内容指纹，防「换图」")
        kv("command_state", f.get("command_state"), "这一帧所属指令当前的状态")

    # ---- 三条证据 ----
    # ★ 这里必须分清「脚本能自己判的」和「脚本判不了的」。
    #
    #   E1 / E2 只看本次这一行就能判，脚本独立重算是有意义的交叉验证。
    #   E3 不行：它要跟**同一个 boot_id 的上一条** seq 比，
    #           而"上一条"不在本行的任何字段里。硬判会得到一个假结论。
    #
    #   第一版这里就是错的：FAILED 案例里服务端判 E3 不通过，
    #   脚本却打了"E3 通过"，输出看起来像服务端有 bug。
    #   —— 一个判据不完整的"独立校验"，比不做校验更糟。
    #   所以 E3 明确标成"单行判不了"，结论一律以服务端的 evidence_ok 为准。
    print("\n三条证据（脚本能自己判的才判；判不了的标出来，不硬判）")
    e1 = len(mine) > 0
    e2 = bool(cmd.get("capture_ts") and cmd.get("dispatched_at")
              and cmd["capture_ts"] > cmd["dispatched_at"])
    print("  E1 请求贯穿     %s  找到 %d 帧带着本 request_id"
          % ("通过" if e1 else "不通过", len(mine)))
    print("  E2 时序合理     %s  capture_ts %s dispatched_at"
          % ("通过" if e2 else "不通过",
             "晚于" if e2 else "不晚于/缺值"))
    if cmd.get("boot_id") and cmd.get("seq") is not None:
        print("  E3 新鲜度单调   单行判不了  本行 boot_id=%s seq=%s；"
              % (cmd["boot_id"], cmd["seq"]))
        print("                 └ 要比**同一个 boot_id 的上一条** seq，"
              "那个值不在这行里 —— 以服务端结论为准")
    else:
        print("  E3 新鲜度单调   不通过  本行没有 boot_id / seq")

    print("\n服务端的判定（这是唯一权威结论）")
    if cmd.get("evidence_ok") is None:
        kv("evidence_ok", "—", "还没到校验这一步（指令尚未上传观测）")
    else:
        kv("evidence_ok", cmd["evidence_ok"],
           "三条证据齐全" if cmd["evidence_ok"] else "被拦下")
    if cmd.get("fail_reason"):
        kv("fail_reason", cmd["fail_reason"])
    # 脚本重算与服务端结论矛盾时，多半是脚本那一项判不了 —— 提醒一句，
    # 免得读的人以为是服务端错了。
    if e1 is False and cmd.get("evidence_ok") == 1:
        print("  [注意] 脚本没找到带本 request_id 的帧，但服务端判为齐全 ——")
        print("         可能是 /api/frames 的分页窗口（limit=200）没覆盖到，"
              "以服务端为准。")

    # ---- 时间线 ----
    tl = cmd.get("timeline") or []
    if tl:
        print("\n时间线（看出卡在哪一步）")
        for i, node in enumerate(tl, 1):
            print("  %2d. %-12s %s  %s"
                  % (i, node.get("state", ""), node.get("at", ""),
                     node.get("note", "") or ""))

    # ---- 结论 ----
    rule("结论")
    state = cmd.get("state")
    has_frame = bool(cmd.get("has_frame"))
    if state == "COMPLETED" and has_frame:
        print("  这张图**可以**算本次新采集：request_id 贯穿三处，")
        print("  capture_ts 晚于下发时刻，seq 在同一开机内递增，证据齐全。")
        code = 0
    elif state == "EXPIRED":
        print("  **没有新采集**，而且问题不在板子：指令 TTL 内**没人来取**。")
        print("  → 先查板子是否在线（掉电/没连上 Wi-Fi），别去查摄像头。")
        code = 1
    elif state == "TIMEOUT":
        print("  **没有新采集**：板子已接手但没干完。")
        print("  → 先查板子侧（拍失败？传失败？看串口），网络差也会走这条。")
        code = 1
    elif state == "FAILED":
        print("  **不能算新采集**：证据校验没通过，已记 FAILED。")
        print("  → 看上面的 fail_reason，它直接写清了是哪一条证据不过。")
        code = 1
    elif state in ("PENDING", "RECEIVED", "EXECUTING", "UPLOADED"):
        print("  还在过程中（%s）：**现在还不能说采集成功了**。" % state)
        print("  → UPLOADED 只代表「收到图了」，还要过证据校验才算 COMPLETED。")
        code = 1
    else:
        print("  未知状态：%s" % state)
        code = 1
    return code


def main() -> int:
    ap = argparse.ArgumentParser(
        description="追踪一次远程采集的请求 → 设备回执 → 新观测三处记录")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="服务端地址")
    ap.add_argument("--request-id", help="只追踪这条指令（只读，不改任何状态）")
    ap.add_argument("--latest", action="store_true",
                    help="追踪指定设备最近的一条指令")
    ap.add_argument("--device", default="", help="配合 --latest 使用")
    args = ap.parse_args()

    base = args.base.rstrip("/")

    st, _ = req(base, "GET", "/api/health")
    if st != 200:
        print("[错误] /api/health 返回 %s" % st)
        return 2

    if args.request_id:
        rid = args.request_id
        print("只读追踪已有指令，不新建任何记录。")
    elif args.latest:
        if not args.device:
            print("[错误] --latest 需要同时给 --device")
            return 2
        rid = find_latest(base, args.device)
    else:
        tag = "%s-%04x" % (datetime.now(TZ).strftime("%H%M%S"), os.getpid() & 0xFFFF)
        device = "trace-demo-" + tag
        rule("先跑一次完整闭环（虚拟设备 %s，不碰真板子）" % device)
        rid = run_closed_loop(base, device)

    return trace(base, rid)


if __name__ == "__main__":
    sys.exit(main())
