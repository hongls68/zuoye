#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第2周 · 照片画廊与配额清理自测

覆盖计划书 4.5「元数据永久保留、原图按配额清理」这条约定：

  1. 上传一帧后，SHA-256 / 分辨率 / 字节数 / 来源 是否正确落库
  2. /api/frames 能否按时间倒序列出这些元数据，并给出存储统计
  3. /api/frames/image?id=N 能否取到原图（且内容与上传字节完全一致）
  4. 触发配额清理后：原图文件消失、purged_at 落库，
     但**元数据行数不变、sha256 一条不少**
  5. 已清理的帧再取图，返回 404 且带上 sha256 作为替代证据
  6. 老接口（/api/health、/api/frame/latest）无回归

与 selftest_command.py 的区别：
  selftest_command.py 连接**已经跑着**的服务端（不污染真实 data.db 靠外部隔离）；
  本脚本因为要临时把 RETENTION_DAYS 压到 0 来强制过期，所以**自起一个隔离实例**
  （PORT=8012、DATA_DIR=server/tmpdata_gallery_<时间戳>），跑完自动清掉。

运行：python selftest_gallery.py
"""
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8012
BASE = "http://127.0.0.1:%d" % PORT
# 每轮用新目录名，避免上一轮残留的 data.db 影响判定
TMP = os.path.join(HERE, "tmpdata_gallery_%d" % int(time.time()))
PY = sys.executable
# 本机若开着 HTTP 代理（Clash 之类），127.0.0.1 的请求也会被劫走，导致自测全红。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method="POST")
    with OPENER.open(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


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


def main() -> int:
    # 本进程也要指向隔离目录，否则第 5 步 import server 会碰到真实的 data.db
    os.environ["DATA_DIR"] = TMP
    env = dict(os.environ, PORT=str(PORT), DATA_DIR=TMP)
    proc = subprocess.Popen([PY, os.path.join(HERE, "server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_port():
            print("服务端未能启动，中止。")
            return 1
        jpg = make_jpeg(320, 240)
        digest = hashlib.sha256(jpg).hexdigest()

        print("\n== 1. 上传一帧：哈希 / 尺寸 / 来源落库 ==")
        st, res = post("/api/frame", jpg, {
            "Content-Type": "image/jpeg", "X-Device-Id": "test-dev",
            "X-Capture-Ts": "2026-09-21T15:00:00+08:00",
            "X-Boot-Id": "boot-abc", "X-Seq": "7"})
        check("POST /api/frame 返回 201", st == 201, st)
        check("响应回带 sha256", res.get("sha256") == digest, res.get("sha256"))
        check("响应回带 320x240", (res.get("width"), res.get("height")) == (320, 240),
              (res.get("width"), res.get("height")))

        print("\n== 2. 画廊列表 ==")
        st, gal = get_json("/api/frames?limit=10")
        check("GET /api/frames 返回 200", st == 200, st)
        check("列表有 1 条", len(gal["frames"]) == 1, len(gal["frames"]))
        f = gal["frames"][0]
        check("条目 sha256 一致", f["sha256"] == digest)
        check("条目宽高正确", (f["width"], f["height"]) == (320, 240))
        check("来源推断为 periodic（无 request_id）", f["source"] == "periodic", f["source"])
        check("未清理 purged=False", f["purged"] is False)
        check("storage.kept=1", gal["storage"]["kept"] == 1, gal["storage"])
        check("storage.retention_days 有值", gal["storage"]["retention_days"] >= 0)

        print("\n== 3. 按 id 取原图 ==")
        with OPENER.open("%s/api/frames/image?id=%d" % (BASE, f["id"]), timeout=10) as r:
            body = r.read()
            check("取原图 200 且字节完全一致", r.status == 200 and body == jpg, r.status)
            check("Content-Type 是 image/jpeg",
                  r.headers.get("Content-Type") == "image/jpeg",
                  r.headers.get("Content-Type"))

        print("\n== 4. 再传一帧（供配额清理计数）==")
        st, _ = post("/api/frame", jpg, {"Content-Type": "image/jpeg",
                                         "X-Device-Id": "test-dev"})
        check("第二次上传 201", st == 201, st)

        print("\n== 5. 配额清理（RETENTION_DAYS=0 强制过期）==")
        sys.path.insert(0, HERE)
        os.environ["RETENTION_DAYS"] = "0"
        import server as mod
        conn = mod.get_db()
        try:
            n = mod.purge_expired_frames(conn)
            left = conn.execute("SELECT COUNT(*) c FROM frames "
                                "WHERE purged_at IS NULL").fetchone()["c"]
            total = conn.execute("SELECT COUNT(*) c FROM frames").fetchone()["c"]
            hashes = conn.execute("SELECT COUNT(*) c FROM frames "
                                  "WHERE sha256 IS NOT NULL").fetchone()["c"]
        finally:
            conn.close()
        check("清理了 2 张原图", n == 2, n)
        check("没有未清理的帧了", left == 0, left)
        check("元数据行数没变（仍为 2）", total == 2, total)
        check("哈希全部保留（2 条）", hashes == 2, hashes)
        check("snapshots 里只剩 latest.jpg",
              sorted(os.listdir(os.path.join(TMP, "snapshots"))) == ["latest.jpg"],
              os.listdir(os.path.join(TMP, "snapshots")))

        print("\n== 6. 已清理的帧取图：404 + 哈希 ==")
        try:
            OPENER.open("%s/api/frames/image?id=%d" % (BASE, f["id"]), timeout=10)
            check("应回 404", False, "居然成功了")
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode("utf-8"))
            check("回 404", e.code == 404, e.code)
            check("404 里带 sha256 作为替代证据", payload.get("sha256") == digest,
                  payload.get("sha256"))

        print("\n== 7. 老接口无回归 ==")
        st, res = get_json("/api/health")
        check("/api/health 正常", st == 200 and res.get("ok") is True)
        with OPENER.open(BASE + "/api/frame/latest", timeout=10) as r:
            check("/api/frame/latest 仍可读", r.status == 200)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 56)
    print("结果：%s" % ("全部通过" if not fails
                       else "失败 %d 项：%s" % (len(fails), fails)))
    print("=" * 56)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
