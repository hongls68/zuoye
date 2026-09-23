#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
断网补传（backlog）链路自测 —— 对应计划书 4.2

【这个功能要证明什么】
  "断网期间捕获的帧自动驻留 Flash，恢复后补传" —— 但要小心：
  补传帧的**采集时刻在过去**，它和"刚拍的帧"长得一模一样（同样是一个 JPEG）。
  所以整条链路必须能回答："这一帧是现在拍的，还是几分钟前拍的？"
  第 2 周整周在防的"旧值冒充新采集"，在这里换了个形状又出现了一次。

  测试分两侧：

  【服务端侧】
  1. 补传帧正常入库，且三个时间口径**分开**落库
     （capture_ts = 过去的采集时刻 / buffered_us = 板端报的"待了多久" /
       ts_server = 服务端收到的时刻）
  2. ★ 补传帧带 X-Request-Id 必须**硬拒 400**，不是静默忽略
  3. 补传帧不推进任何一条命令的状态（天然不参与命令闭环）
  4. 不可信的数值一律归 None，不用一个错的值去算"待了多久"
  5. 未知 source 不拒收，但落库归成 unknown（别让新板端把链路卡死）
  6. 画廊把补传帧单独标注 + 单独计数

  【固件侧（主机镜像校验）】
  7. 队列文件名编码的**排序不变量**：字典序 == 先进先出。
     并且断言镜像里的格式串与 firmware/main/backlog.c 里的 snprintf 格式
     **逐字符一致** —— 谁改了 C 那边而忘了这里，本组会红。
  8. 淘汰策略：满了丢最老、如实计数、**先传成功再删**。

  ★ 第 7、8 组是"把固件里的约定写成可执行断言"，**不是在跑固件代码**。
    它们的价值在于：这两条不变量一旦破了，板子上是静默出错的
    （队列顺序乱掉不会报错，只会让补传按错的顺序发出去）。
    真正的固件行为要在板子上验，见「断网补传设计说明」的验收步骤。

与其它自测脚本的关系：
  selftest_command.py 连**已经跑着**的服务端；
  本脚本要造"采集时刻在过去"这种特殊时间关系的帧，所以**自起一个隔离实例**
  （PORT=8016、DATA_DIR=server/tmpdata_backlog_<时间戳>），跑完自动清掉。

运行：python selftest_backlog.py
"""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8016
BASE = "http://127.0.0.1:%d" % PORT
# 每轮用新目录名，避免上一轮残留的 data.db 影响判定
TMP = os.path.join(HERE, "tmpdata_backlog_%d" % int(time.time()))
PY = sys.executable
FIRMWARE = os.path.join(HERE, os.pardir, "firmware", "main")
# 本机若开着 HTTP 代理（Clash 之类），127.0.0.1 的请求也会被劫走，导致自测全红。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEV = "eye-backlog-test"

fails = []


def check(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name
          + (" | " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


def make_jpeg(w=320, h=240):
    """手工拼一张「结构合法」的 JPEG：FFD8 + SOF0(含宽高) + 少量数据 + FFD9。

    服务端只校验首尾标记，jpeg_size() 只解析 SOF0，所以这个最小样本够用，
    不必依赖 Pillow。
    """
    sof = struct.pack(">BHHB", 0x08, h, w, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    seg = b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    return b"\xff\xd8" + seg + b"\xff\xda\x00\x08" + b"\x00" * 64 + b"\xff\xd9"


def post(path, body, headers):
    """POST 并把 HTTPError 也当成正常返回（4xx 是我们要断言的对象）。"""
    req = urllib.request.Request(BASE + path, data=body, headers=headers,
                                 method="POST")
    try:
        with OPENER.open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def post_json(path, obj):
    return post(path, json.dumps(obj).encode("utf-8"),
                {"Content-Type": "application/json"})


def get_json(path):
    with OPENER.open(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def wait_port(deadline=15.0):
    end = time.time() + deadline
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", PORT), 0.4):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def snapshot_files():
    d = os.path.join(TMP, "snapshots")
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


# ============================================================================
# 固件侧镜像：队列文件名编码
# ============================================================================

# 与 firmware/main/backlog.c 的 backlog_make_name() 保持一致。
# 第 7 组会拿 backlog.c 里的实际格式串跟它逐字符对比，防止两边漂移。
NAME_FMT = "%06lu_%08lu_%08lu.jpg"

# 镜像 backlog.c 的 backlog_parse_uptime()：
# 三段定长数字 + ".jpg"，并且**必须刚好用完整个名字**。
# （C 那边靠 sscanf 的 %n 拿已消费长度，这里靠 re.fullmatch。）
NAME_RE = re.compile(r"(\d{1,6})_(\d{1,8})_(\d{1,8})\.jpg\Z")


def mirror_make_name(boot_cnt, seq, uptime_s):
    return NAME_FMT % (boot_cnt % 1000000, seq % 100000000, uptime_s % 100000000)


def mirror_parse_uptime(name):
    m = NAME_RE.match(name)
    return None if m is None else int(m.group(3))


def read_c_format():
    """从 backlog.c 里抠出文件名格式串（`snprintf(out, cap, "...")` 的第三个参数）。"""
    path = os.path.join(FIRMWARE, "backlog.c")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r'snprintf\(\s*out\s*,\s*cap\s*,\s*"([^"]+)"', src)
    return m.group(1) if m else None


def read_c_macro(name):
    """从 app_config.h 里读一个整数宏的值。"""
    path = os.path.join(FIRMWARE, "app_config.h")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"^#define\s+%s\s+(\d+)" % re.escape(name), src, re.M)
    return int(m.group(1)) if m else None


class MirrorQueue:
    """镜像 backlog.c 的淘汰策略。

    ★ 说明白：这**不是在跑固件代码**，而是把 backlog.h/.c 里写下的策略
      变成可执行断言。策略错了板子上是静默出错的，所以值得在主机上钉死。
    """

    def __init__(self, max_frames):
        self.max_frames = max_frames
        self.items = []          # 按入队顺序（等价于文件名排序）
        self.dropped = 0
        self.seq = 0

    def put(self, boot_cnt, uptime_s):
        self.seq += 1
        name = mirror_make_name(boot_cnt, self.seq, uptime_s)
        # 满了先丢最老：用户关心"刚才发生了什么"，所以丢老不丢新。
        while len(self.items) >= self.max_frames:
            self.items.pop(0)
            self.dropped += 1
        self.items.append(name)
        return name

    def peek_oldest(self):
        return self.items[0] if self.items else None

    def drop_oldest(self):
        if self.items:
            self.items.pop(0)


# ============================================================================

def main() -> int:
    os.environ["DATA_DIR"] = TMP
    env = dict(os.environ, PORT=str(PORT), DATA_DIR=TMP)
    proc = subprocess.Popen([PY, os.path.join(HERE, "server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_port():
            print("服务端未能启动，中止。")
            return 1
        jpg = make_jpeg(320, 240)

        # ---------------------------------------------------------------
        print("\n== 1. 补传帧入库：三个时间口径分开 ==")
        past = "2026-09-21T15:00:00+08:00"     # 明显在过去（补传帧就该是这样）
        st, res = post("/api/frame", jpg, {
            "Content-Type": "image/jpeg",
            "X-Device-Id": DEV,
            "X-Capture-Ts": past,
            "X-Boot-Id": "boot-bk-1",
            "X-Seq": "11",
            "X-Source": "backlog",
            "X-Buffered-Us": str(615 * 1000000),   # 在队列里待了 615 秒
            "X-Backlog-Dropped": "3",
        })
        check("补传帧入库返回 201", st == 201, st)
        check("source 落库为 backlog", res.get("source") == "backlog",
              res.get("source"))
        bl = res.get("backlog") or {}
        check("响应带 backlog 块", isinstance(bl, dict) and "buffered_us" in bl, bl)
        check("buffered_us 原样回带", bl.get("buffered_us") == 615 * 1000000,
              bl.get("buffered_us"))
        check("buffered_s 换算为 615.0", bl.get("buffered_s") == 615.0,
              bl.get("buffered_s"))
        check("dropped_before 原样回带（3）", bl.get("dropped_before") == 3,
              bl.get("dropped_before"))
        check("backlog.note 讲清 capture_ts 在过去",
              "过去" in (bl.get("note") or ""), bl.get("note"))
        check("补传帧没有 request_id", res.get("request_id") is None,
              res.get("request_id"))
        check("补传帧不做证据校验（evidence 为空）", res.get("evidence") is None,
              res.get("evidence"))

        st, gal = get_json("/api/frames?device_id=" + DEV + "&limit=10")
        f0 = gal["frames"][0]
        check("画廊里 capture_ts 是那个过去的时刻", f0["capture_ts"] == past,
              f0["capture_ts"])
        check("★ capture_ts ≠ ts_server（采集和收到不是同一时刻）",
              f0["capture_ts"] != f0["ts_server"],
              (f0["capture_ts"], f0["ts_server"]))
        check("ts_server 是今天（判定用的那个钟）",
              str(f0["ts_server"]).startswith(time.strftime("%Y-%m-%d")),
              f0["ts_server"])

        # ---------------------------------------------------------------
        print("\n== 2. ★ 硬拒：补传帧不许绑 request_id ==")
        st, cmd = post_json("/api/command",
                            {"device_id": DEV, "action": "capture", "ttl_s": 300})
        check("先建一条命令备用（201）", st == 201, st)
        # 响应形如 {"ok": true, "command": {...}}，request_id 在 command 里
        rid = (cmd.get("command") or {}).get("request_id")
        check("拿到 request_id", bool(rid), cmd)

        before = len(snapshot_files())
        st, res = post("/api/frame", jpg, {
            "Content-Type": "image/jpeg",
            "X-Device-Id": DEV,
            "X-Capture-Ts": past,
            "X-Boot-Id": "boot-bk-1",
            "X-Seq": "12",
            "X-Source": "backlog",          # ← 补传
            "X-Request-Id": rid,            # ← 却又说这是某次请求的观测
            "X-Buffered-Us": str(60 * 1000000),
        })
        check("补传帧带 request_id → 400", st == 400, st)
        check("错误说明点出「不能绑定」", "绑定" in (res.get("error") or ""),
              res.get("error"))
        check("错误说明点出「过去」这个理由", "过去" in (res.get("detail") or ""),
              res.get("detail"))
        check("提示里给出正确做法（只带 X-Source: backlog）",
              "backlog" in (res.get("hint") or ""), res.get("hint"))
        check("★ 被拒的帧没有落盘（不是「先存了再说」）",
              len(snapshot_files()) == before, (before, len(snapshot_files())))
        st, gal = get_json("/api/frames?device_id=" + DEV + "&limit=10")
        check("★ 被拒的帧没有入库", len(gal["frames"]) == 1, len(gal["frames"]))

        # ---------------------------------------------------------------
        print("\n== 3. 补传帧不推进命令状态 ==")
        # 注意 /api/command/status 回的是 {"commands": [...]}（列表），
        # 和创建接口的 {"command": {...}}（单个）形状不同 —— 别记混。
        def cmd_row():
            _, body = get_json("/api/command/status?request_id=" + rid)
            rows = body.get("commands") or []
            return rows[0] if rows else {}

        check("命令仍是 PENDING（没被那帧推进）",
              cmd_row().get("state") == "PENDING", cmd_row().get("state"))
        check("★ 命令的 has_frame 仍为 false（补传帧没被当成它的观测）",
              cmd_row().get("has_frame") is False, cmd_row().get("has_frame"))

        # 不带 request_id 的补传帧同样不该碰命令
        st, res = post("/api/frame", jpg, {
            "Content-Type": "image/jpeg", "X-Device-Id": DEV,
            "X-Capture-Ts": past, "X-Boot-Id": "boot-bk-1", "X-Seq": "13",
            "X-Source": "backlog", "X-Buffered-Us": str(120 * 1000000),
        })
        check("不带 request_id 的补传帧入库 201", st == 201, st)
        check("命令仍是 PENDING（补传帧与命令闭环解耦）",
              cmd_row().get("state") == "PENDING", cmd_row().get("state"))
        check("命令的 has_frame 仍为 false", cmd_row().get("has_frame") is False,
              cmd_row().get("has_frame"))

        # ---------------------------------------------------------------
        print("\n== 4. 不可信的值归 None（不拿错值去算「待了多久」）==")
        cases = [
            ("负数 buffered_us", {"X-Buffered-Us": "-1"}, None),
            ("超上限 buffered_us（31 天）", {"X-Buffered-Us": str(31 * 86400 * 1000000)},
             None),
            ("负数 backlog_dropped", {"X-Backlog-Dropped": "-5"}, None),
            ("缺失 buffered_us", {}, None),
            ("非数字 buffered_us", {"X-Buffered-Us": "abc"}, None),
        ]
        for label, extra, want in cases:
            h = {"Content-Type": "image/jpeg", "X-Device-Id": DEV,
                 "X-Source": "backlog", "X-Capture-Ts": past}
            h.update(extra)
            st, res = post("/api/frame", jpg, h)
            got = (res.get("backlog") or {}).get("buffered_us")
            check("%s → %s" % (label, want), st == 201 and got == want,
                  (st, got))
        # 边界内的 0 必须**保留**，不能被当成"没有值"
        st, res = post("/api/frame", jpg, {
            "Content-Type": "image/jpeg", "X-Device-Id": DEV,
            "X-Source": "backlog", "X-Capture-Ts": past,
            "X-Buffered-Us": "0", "X-Backlog-Dropped": "0"})
        check("★ buffered_us=0 是合法的（刚入队就传），不能归成 None",
              (res.get("backlog") or {}).get("buffered_us") == 0,
              (res.get("backlog") or {}).get("buffered_us"))

        # ---------------------------------------------------------------
        print("\n== 5. 未知 source：不拒收，但归成 unknown ==")
        st, res = post("/api/frame", jpg, {
            "Content-Type": "image/jpeg", "X-Device-Id": DEV,
            "X-Source": "some_future_source"})
        check("未知 source 仍入库 201", st == 201, st)
        check("落库归成 unknown", res.get("source") == "unknown",
              res.get("source"))

        # ---------------------------------------------------------------
        print("\n== 6. 画廊：补传帧单独标注 + 单独计数 ==")
        st, gal = get_json("/api/frames?device_id=" + DEV + "&limit=50")
        by_id = {f["id"]: f for f in gal["frames"]}
        bks = [f for f in gal["frames"] if f["is_backlog"]]
        others = [f for f in gal["frames"] if not f["is_backlog"]]
        check("画廊认出补传帧（≥5 条）", len(bks) >= 5, len(bks))
        check("非补传帧也在（未知 source 那条）", len(others) >= 1, len(others))
        check("每条都带 is_backlog 布尔", all(isinstance(f["is_backlog"], bool)
                                        for f in gal["frames"]))
        check("每条都带 buffered_s 字段",
              all("buffered_s" in f for f in gal["frames"]))
        check("每条都带 backlog_dropped 字段",
              all("backlog_dropped" in f for f in gal["frames"]))
        one = [f for f in bks if f["backlog_dropped"] == 3]
        check("有那条 dropped=3 的补传帧", len(one) == 1, len(one))
        check("它的 buffered_s=615.0", one and one[0]["buffered_s"] == 615.0,
              one and one[0]["buffered_s"])
        stor = gal["storage"]
        check("storage.backlog_frames 单独计数（=补传帧数）",
              stor["backlog_frames"] == len(bks),
              (stor["backlog_frames"], len(bks)))
        check("storage.backlog_dropped_total 累加（≥3）",
              stor["backlog_dropped_total"] >= 3, stor["backlog_dropped_total"])
        check("顶层有 backlog_note 说明口径", "backlog" in (gal.get("backlog_note") or ""),
              gal.get("backlog_note"))
        check("顶层有 server_now（服务端的钟）", bool(gal.get("server_now")),
              gal.get("server_now"))

        # ---------------------------------------------------------------
        print("\n== 7. 队列文件名：字典序 == 先进先出（镜像 + 与 C 源码对账）==")
        c_fmt = read_c_format()
        check("能从 backlog.c 抠出格式串", bool(c_fmt), c_fmt)
        check("★ 镜像格式串与 backlog.c 逐字符一致", c_fmt == NAME_FMT,
              (c_fmt, NAME_FMT))
        if c_fmt:
            widths = re.findall(r"%0(\d+)lu", c_fmt)
            check("格式串是三段定长补零（6/8/8）", widths == ["6", "8", "8"], widths)

        names = [mirror_make_name(1, s, 100 + s) for s in range(1, 13)]
        check("12 帧的名字按字典序排序 == 入队顺序", sorted(names) == names,
              sorted(names)[:3])
        check("首帧名字形如 000001_00000001_00000101.jpg",
              names[0] == "000001_00000001_00000101.jpg", names[0])

        bad = ["%lu_%lu.jpg" % (1, s) for s in (1, 2, 10)]
        check("★ 反例：不补零时 '10' 会排到 '2' 前面（所以必须补零）",
              sorted(bad) != bad, sorted(bad))

        check("解析：正常名字取回采集 uptime",
              mirror_parse_uptime("000001_00000001_00000101.jpg") == 101,
              mirror_parse_uptime("000001_00000001_00000101.jpg"))
        check("★ 解析：'…jpg_extra' 被拒（%n 卡「刚好用完」）",
              mirror_parse_uptime("000001_00000001_00000101.jpg_extra") is None,
              mirror_parse_uptime("000001_00000001_00000101.jpg_extra"))
        check("解析：大写扩展名被拒",
              mirror_parse_uptime("000001_00000001_00000101.JPG") is None)
        check("解析：少一段被拒",
              mirror_parse_uptime("000001_00000001.jpg") is None)
        check("解析：首段 7 位数字被拒",
              mirror_parse_uptime("1234567_00000001_00000101.jpg") is None)
        check("解析：'..' 与 'latest.jpg' 这类都不是我们的名字",
              mirror_parse_uptime("..") is None
              and mirror_parse_uptime("latest.jpg") is None)

        check("已知限制：开机计数超 6 位会回绕（见 backlog.h 注释）",
              mirror_make_name(1000000, 1, 1).startswith("000000_"),
              mirror_make_name(1000000, 1, 1))

        # ---------------------------------------------------------------
        print("\n== 8. 淘汰策略：满了丢最老、如实计数、先传成功再删 ==")
        cap = read_c_macro("BACKLOG_MAX_FRAMES")
        check("能从 app_config.h 读到 BACKLOG_MAX_FRAMES", cap is not None, cap)
        cap = cap or 64

        q = MirrorQueue(cap)
        for i in range(1, cap + 1):
            q.put(1, 1000 + i)
        check("刚好装满时不丢帧（dropped=0）", q.dropped == 0 and len(q.items) == cap,
              (q.dropped, len(q.items)))

        oldest_before = q.peek_oldest()
        q.put(1, 1000 + cap + 1)
        check("★ 再入一帧 → 丢最老一帧", len(q.items) == cap, len(q.items))
        check("★ dropped 如实加 1", q.dropped == 1, q.dropped)
        check("★ 丢掉的确实是最老那帧", oldest_before not in q.items,
              oldest_before)
        check("新帧还在队尾", q.items[-1] == mirror_make_name(1, cap + 1, 1000 + cap + 1),
              q.items[-1])
        check("淘汰后队列仍是有序的（FIFO 不变量没被破坏）",
              sorted(q.items) == q.items)

        # 先传成功再删：传失败时帧必须留着，且 dropped 不变
        n_before, d_before = len(q.items), q.dropped
        head = q.peek_oldest()
        # 模拟"上传失败"：什么都不做
        check("★ 补传失败时帧留在队列里（不删）",
              len(q.items) == n_before and q.peek_oldest() == head,
              (len(q.items), q.peek_oldest()))
        check("补传失败不会让 dropped 变动（丢帧与失败是两件事）",
              q.dropped == d_before, q.dropped)
        # 模拟"上传成功"：删
        q.drop_oldest()
        check("补传成功后才删掉队首", len(q.items) == n_before - 1,
              len(q.items))
        check("删除成功那一帧不会计入 dropped", q.dropped == d_before, q.dropped)
        check("补传后队首换成了下一帧", q.peek_oldest() != head, q.peek_oldest())

        # ---------------------------------------------------------------
        print("\n== 9. 老接口无回归 ==")
        st, res = get_json("/api/health")
        check("/api/health 正常", st == 200 and res.get("ok") is True)
        st, res = get_json("/api/frames?limit=5")
        check("/api/frames 不带 device_id 也能用", st == 200, st)
        st, res = get_json("/api/command/status?device_id=" + DEV)
        check("/api/command/status 正常", st == 200, st)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)
        if os.path.isdir(TMP):
            # ★ 别只写 ignore_errors=True 就算完：Windows 上服务端子进程可能还没
            #   放开 data.db 的文件句柄，rmtree 会**静默失败** —— 于是"跑完自删"
            #   变成"跑完留下一堆 tmpdata_* 而没人知道"。
            #   自测可以删不掉临时目录，但不该把这件事瞒下来。
            print("（临时目录未能删除，可能仍有进程占着文件：%s）" % TMP)

    print("\n" + "=" * 56)
    print("结果：%s" % ("全部通过" if not fails
                       else "失败 %d 项：%s" % (len(fails), fails)))
    print("=" * 56)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
