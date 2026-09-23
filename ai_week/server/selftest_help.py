#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第3周 · 按键求助事件（三层状态）自测

本周的题眼是「本地确认 / VPS 接收 / 查看者回应」三种状态必须能区分。
所以自测的重点不是"功能能不能用"，而是**三个来源的事实有没有被混在一起**：

  1. 正常闭环：板端发起 → 服务端接收 → 查看者回应 → 板端轮询到回应
  2. 三层状态各自独立，且各自带自己的时间戳（不合并）
  3. ★ 服务端绝不采信板端报的时间：板端故意报一个假时刻，received_at 仍是服务端的钟
  4. 板端取消：server_state=CANCELLED 且 cancelled_by=device
  5. ★ 已回应的求助不能再取消（回应是既成事实）
  6. ★ 已取消的求助不能再回应
  7. 重复取消是幂等的（返回 200 而不是报错）
  8. 空回应被拒（空回应等于没回应）
  9. ★ 超时只改 server_state，device_state / viewer_state 一动不动
 10. 板端轮询能拿到回应人与回应内容

运行：python selftest_help.py
（自起一个隔离实例，PORT=8013、DATA_DIR=server/tmpdata_help_<时间戳>，跑完自删）
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8013
BASE = "http://127.0.0.1:%d" % PORT
TMP = os.path.join(HERE, "tmpdata_help_%d" % int(time.time()))
PY = sys.executable
TZ = timezone(timedelta(hours=8))
DEV = "selftest-help-dev"
# 本机若开着 HTTP 代理（Clash 之类），127.0.0.1 的请求也会被劫走 → 必须绕开
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

fails = []


def check(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name
          + (" | " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


def req(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers,
                               method=method)
    try:
        with OPENER.open(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return e.code, payload


def wait_port(deadline=15.0):
    end = time.time() + deadline
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", PORT), 0.4):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _dump_log(path, n=40):
    """把服务端日志的最后 n 行打出来（排查"接口抛异常但只看到断连接"用）。"""
    print("\n---- 服务端日志尾部 (%s) ----" % os.path.basename(path))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        for ln in lines[-n:]:
            print("  | " + ln)
    except OSError as e:
        print("  | (读不到日志：%s)" % e)
    print("---- 日志尾部结束 ----")


def submit(action, event_id, **kw):
    body = {"device_id": DEV, "event_id": event_id, "action": action,
            "kind": "teach_help_test",
            "pressed_at": "2026-09-21T16:00:00.000+08:00",
            "local_ack_at": "2026-09-21T16:00:00.100+08:00",
            "boot_id": "selftest-boot1", "seq": 1}
    body.update(kw)
    return req("POST", "/api/help", body)


def main() -> int:
    os.environ["DATA_DIR"] = TMP
    env = dict(os.environ, PORT=str(PORT), DATA_DIR=TMP)
    # 服务端日志落盘：接口里抛异常时 HTTP 层只会断连接、不留痕迹，
    # 不抓日志就只能看到"Remote end closed connection"，白猜半天。
    logpath = os.path.join(HERE, "tmpdata_help_server.log")
    logf = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([PY, os.path.join(HERE, "server.py")], env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    try:
        if not wait_port():
            print("服务端未能启动，中止。")
            _dump_log(logpath)
            return 1

        print("\n== 1. 正常闭环：板端发起 → 服务端接收 ==")
        st, res = submit("request", "help-t1", device_state="LOCAL_ACKED")
        check("POST /api/help 返回 201", st == 201, st)
        h = res["help"]
        check("device_state = LOCAL_ACKED（板端自报）",
              h["device_state"] == "LOCAL_ACKED", h["device_state"])
        check("server_state = RECEIVED（服务端自判）",
              h["server_state"] == "RECEIVED", h["server_state"])
        check("viewer_state = PENDING（还没人回应）",
              h["viewer_state"] == "PENDING", h["viewer_state"])
        check("received_at 由服务端打上", bool(h["received_at"]), h["received_at"])
        check("answerable 为真", h["answerable"] is True)

        print("\n== 2. 三层状态各自带自己的时间戳（不合并）==")
        cs = h["clock_sources"]
        check("clock_sources 分三组 device/server/viewer",
              set(cs.keys()) == {"device", "server", "viewer"}, list(cs.keys()))
        check("device 组带 pressed_at 与 local_ack_at",
              cs["device"]["pressed_at"] and cs["device"]["local_ack_at"])
        check("server 组带 received_at", bool(cs["server"]["received_at"]))
        check("viewer 组此刻为空（还没回应）",
              cs["viewer"]["answered_at"] is None, cs["viewer"]["answered_at"])
        check("三层标签都生成了",
              all(h[k] for k in ("device_label", "server_label", "viewer_label")))

        print("\n== 3. ★ 服务端不采信板端报的时间 ==")
        # 板端故意报一个"昨天"的假时刻
        fake = (datetime.now(TZ) - timedelta(days=1)).isoformat(timespec="milliseconds")
        st, res = submit("request", "help-t2", device_state="LOCAL_ACKED",
                         pressed_at=fake, local_ack_at=fake)
        h2 = res["help"]
        check("板端假时刻被原样记录（服务端不修改它）",
              h2["pressed_at"] == fake, h2["pressed_at"])
        real = datetime.fromisoformat(h2["received_at"])
        check("received_at 仍是服务端当前时间，没被板端带偏",
              abs((datetime.now(TZ) - real).total_seconds()) < 30,
              h2["received_at"])
        check("两者确实不同（说明没被合并成一个字段）",
              h2["pressed_at"] != h2["received_at"])

        print("\n== 4. 查看者回应 ==")
        st, res = req("POST", "/api/help/answer",
                      {"event_id": "help-t1", "answered_by": "同学B",
                       "answer_text": "我看到板子的灯常亮了，问题已处理"})
        check("回应返回 200", st == 200, st)
        h = res["help"]
        check("viewer_state = ANSWERED", h["viewer_state"] == "ANSWERED")
        check("server_state 保持 RECEIVED（没被回应改写）",
              h["server_state"] == "RECEIVED", h["server_state"])
        check("answered_by 记录为 同学B", h["answered_by"] == "同学B")
        check("answerable 变为假", h["answerable"] is False)

        print("\n== 5. 板端轮询能拿到回应 ==")
        st, res = req("GET", "/api/help/poll?device_id=%s&event_id=help-t1" % DEV)
        check("轮询返回 200", st == 200, st)
        h = res["help"]
        check("板端能看到 viewer_state=ANSWERED",
              h["viewer_state"] == "ANSWERED")
        check("板端能拿到回应内容",
              "问题已处理" in (h["answer_text"] or ""), h["answer_text"])
        check("板端能拿到回应人", h["answered_by"] == "同学B")

        print("\n== 6. ★ 已回应的求助不能再取消 ==")
        st, res = req("POST", "/api/help/cancel",
                      {"event_id": "help-t1", "reason": "试试看"})
        check("返回 409 而不是成功", st == 409, st)
        check("给出明确理由", "回应" in (res.get("error") or ""), res.get("error"))

        print("\n== 7. 板端取消：cancelled_by 必须是 device ==")
        st, res = submit("request", "help-t3", device_state="LOCAL_ACKED")
        check("先发起成功", st == 201, st)
        st, res = submit("cancel", "help-t3", device_state="CANCELLED",
                         reason="user_cancelled_on_device")
        check("取消返回 200", st == 200, st)
        h = res["help"]
        check("server_state = CANCELLED", h["server_state"] == "CANCELLED")
        check("cancelled_by = device", h["cancelled_by"] == "device", h["cancelled_by"])
        check("cancelled_at 有值", bool(h["cancelled_at"]))

        print("\n== 8. ★ 已取消的求助不能再回应 ==")
        st, res = req("POST", "/api/help/answer",
                      {"event_id": "help-t3", "answer_text": "迟到很久的回应"})
        check("返回 409", st == 409, st)
        check("理由说明是取消导致", "取消" in (res.get("error") or ""), res.get("error"))

        print("\n== 9. 重复取消是幂等的 ==")
        st, res = submit("cancel", "help-t3", device_state="CANCELLED")
        check("再取消一次返回 200（不是错误）", st == 200, st)
        check("带说明 note", bool(res.get("note")), res.get("note"))

        print("\n== 10. 查看者取消：cancelled_by 必须是 viewer ==")
        st, _ = submit("request", "help-t4", device_state="LOCAL_ACKED")
        check("先发起成功", st == 201, st)
        st, res = req("POST", "/api/help/cancel",
                      {"event_id": "help-t4", "reason": "viewer_cancelled"})
        check("取消返回 200", st == 200, st)
        check("cancelled_by = viewer", res["help"]["cancelled_by"] == "viewer",
              res["help"]["cancelled_by"])

        print("\n== 11. 空回应被拒 ==")
        st, _ = submit("request", "help-t5", device_state="LOCAL_ACKED")
        st, res = req("POST", "/api/help/answer",
                      {"event_id": "help-t5", "answer_text": "   "})
        check("空回应返回 400", st == 400, st)
        check("理由说清是空内容", "空" in (res.get("error") or ""), res.get("error"))

        print("\n== 12. ★ 超时只改 server_state，另两层不动 ==")
        st, _ = submit("request", "help-t6", device_state="LOCAL_ACKED")
        check("先发起成功", st == 201, st)
        # 直接把 received_at 改成很久以前，然后调清理函数（不引入生产用的 TTL 开关）
        sys.path.insert(0, HERE)
        import server as mod
        conn = mod.get_db()
        old = (datetime.now(TZ) - timedelta(seconds=mod.HELP_TTL_S + 60)
               ).isoformat(timespec="milliseconds")
        conn.execute("UPDATE help_events SET received_at=? WHERE event_id=?",
                     (old, "help-t6"))
        conn.commit()
        n = mod.sweep_help_events(conn)
        row = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                           ("help-t6",)).fetchone()
        conn.close()
        check("清理了 1 条", n == 1, n)
        check("server_state 变为 EXPIRED",
              row["server_state"] == mod.HELP_SRV_EXPIRED, row["server_state"])
        check("device_state 仍是 LOCAL_ACKED（没被动过）",
              row["device_state"] == mod.HELP_DEV_LOCAL_ACKED, row["device_state"])
        check("viewer_state 仍是 PENDING（没被动过）",
              row["viewer_state"] == mod.HELP_VWR_PENDING, row["viewer_state"])
        st, res = req("POST", "/api/help/answer",
                      {"event_id": "help-t6", "answer_text": "太晚了"})
        check("过期后不能再回应（409）", st == 409, st)

        print("\n== 13. 网页列表与待回应计数 ==")
        st, res = req("GET", "/api/help?limit=50")
        check("列表返回 200", st == 200, st)
        ids = [x["event_id"] for x in res["helps"]]
        check("六条事件都在列表里",
              set(ids) >= {"help-t1", "help-t2", "help-t3", "help-t4",
                           "help-t5", "help-t6"}, ids)
        check("pending_count 只数『已接收且无人回应』的",
              res["pending_count"] == 2, res["pending_count"])   # t2 与 t5
        check("每条都有 stage 摘要",
              all(x.get("stage") for x in res["helps"]))

        print("\n== 14. 参数校验 ==")
        st, res = req("POST", "/api/help", {"device_id": DEV})   # 缺 event_id
        check("缺 event_id 返回 400", st == 400, st)
        st, res = req("POST", "/api/help",
                      {"device_id": DEV, "event_id": "help-x", "action": "乱写"})
        check("非法 action 返回 400", st == 400, st)
        st, res = req("POST", "/api/help/answer", {"event_id": "不存在的"})
        check("未知 event_id 返回 404", st == 404, st)

        print("\n== 15. 老接口无回归 ==")
        st, res = req("GET", "/api/health")
        check("/api/health 正常", st == 200 and res.get("ok") is True)
        st, res = req("GET", "/api/frames")
        check("/api/frames 仍正常", st == 200 and "frames" in res)
    except Exception as e:                      # noqa: BLE001
        # 中途崩了：把服务端 traceback 打出来，别让失败原因烂在日志文件里
        print("\n[自测中断] %s: %s" % (type(e).__name__, e))
        fails.append("自测中断：%s" % type(e).__name__)
        _dump_log(logpath)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()
        shutil.rmtree(TMP, ignore_errors=True)
        if os.path.isdir(TMP):
            # ★ 别只写 ignore_errors=True 就算完：Windows 上服务端子进程可能还没
            #   放开 data.db 的文件句柄，rmtree 会**静默失败** —— 于是"跑完自删"
            #   变成"跑完留下一堆 tmpdata_* 而没人知道"。
            #   自测可以删不掉临时目录，但不该把这件事瞒下来。
            print("（临时目录未能删除，可能仍有进程占着文件：%s）" % TMP)

    print("\n" + "=" * 60)
    print("结果：%s" % ("全部通过" if not fails
                       else "失败 %d 项：%s" % (len(fails), fails)))
    print("=" * 60)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
