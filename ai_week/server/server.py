#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI交互课 —— 传感器数据接收与存储服务
仅使用 Python 标准库，无第三方依赖。

【第1周】数据上行链路：
  POST /api/ingest                开发板上传一条传感器记录（JSON）
  GET  /api/latest?device_id=X    查询某设备最新一条记录
  GET  /api/history?device_id=X&limit=N   查询某设备历史记录（新→旧）
  GET  /api/devices               列出所有上报过的设备及其最后上报时间
  POST /api/frame?device_id=X     开发板上传一帧 JPEG（原始字节流，单帧上限 1MB）
  GET  /api/frame/latest?device_id=X  取某设备最新一帧 JPEG（网页 <img> 直接引用）
  GET  /api/health                服务自检
  GET  /                          Web 展示页面（index.html）

【第2周】远程采集指令通道与请求状态机：
  POST /api/command               网页下发一条采集指令（生成 request_id）
  GET  /api/command/poll?device_id=X  开发板轮询取指令（取走即置 RECEIVED）
  POST /api/command/ack           开发板回执（携带 device_ts / boot_id / seq）
  GET  /api/command/status?request_id=X | ?device_id=X&limit=N   网页查询状态
  GET  /api/command/frame?request_id=X  取「本次请求」对应的那一帧（非最新帧）

  状态集合：PENDING → RECEIVED → EXECUTING → UPLOADED → COMPLETED
            分支：EXPIRED（TTL 内无人取）/ TIMEOUT（取了没回传）/ FAILED（设备报错或证据不足）

  ★ 核心原则：UPLOADED ≠ COMPLETED。
    收到一张图不代表它就是这次要的那张，必须通过三条证据校验才算完成：
      E1 请求贯穿   request_id 同时出现在 ①指令记录 ②设备回执 ③观测记录
      E2 时序合理   观测的 capture_ts 必须晚于指令的 dispatched_at
      E3 新鲜度单调 同一次开机（boot_id）内 seq 必须严格递增

运行：python server.py   （默认监听 0.0.0.0:8000）
"""
import json
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据目录：默认与脚本同目录；可用环境变量 DATA_DIR 指向别处，
# 这样 selftest_command.py 能在不污染真实 data.db / snapshots 的前提下跑自测。
DATA_DIR = os.environ.get("DATA_DIR") or BASE_DIR
DB_PATH = os.path.join(DATA_DIR, "data.db")
HTML_PATH = os.path.join(BASE_DIR, "index.html")  # 页面始终取脚本同目录的
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")    # 摄像头帧存这里
PORT = int(os.environ.get("PORT") or 8000)  # 可用环境变量 PORT 覆盖，便于本地自测
TZ = timezone(timedelta(hours=8))  # 东八区，与板端时间口径一致
MAX_BODY = 64 * 1024               # /api/ingest 的 JSON 上限
MAX_FRAME = 1 * 1024 * 1024        # /api/frame 的单帧 JPEG 上限（1MB）

# ---- 第2周：指令通道的时间参数 ----
DEFAULT_TTL_S = 120                # 指令有效期：这么久没人取走 -> EXPIRED
EXEC_TIMEOUT_S = 120               # 已取走但这么久没回传观测 -> TIMEOUT
SWEEP_INTERVAL_S = 5               # 后台过期扫描周期（秒）

# 状态集合（与网页状态徽标一一对应，改这里要同步改 index.html）
ST_PENDING   = "PENDING"    # 指令已创建，等待设备取走
ST_RECEIVED  = "RECEIVED"   # 设备已取走并回执
ST_EXECUTING = "EXECUTING"  # 设备正在采集/上传
ST_UPLOADED  = "UPLOADED"   # 观测已入库（尚未通过证据校验）
ST_COMPLETED = "COMPLETED"  # 三条证据校验通过
ST_EXPIRED   = "EXPIRED"    # TTL 内无人取走
ST_TIMEOUT   = "TIMEOUT"    # 已取走但超时未回传
ST_FAILED    = "FAILED"     # 设备显式报错，或证据校验不通过

_db_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS readings(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                sensor    TEXT NOT NULL,
                unit      TEXT NOT NULL,
                ax        REAL,
                ay        REAL,
                az        REAL,
                ax_raw    INTEGER,
                ay_raw    INTEGER,
                az_raw    INTEGER,
                is_new_sample INTEGER,
                time_synced   INTEGER,
                ts_device TEXT,
                ts_server TEXT NOT NULL
            )"""
        )
        # 兼容旧库：早期版本的表没有原始值/标志位字段，
        # CREATE TABLE IF NOT EXISTS 不会改动已存在的表，这里补列。
        existing = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
        for col, decl in (("ax_raw", "INTEGER"),
                          ("ay_raw", "INTEGER"),
                          ("az_raw", "INTEGER"),
                          ("is_new_sample", "INTEGER"),
                          ("time_synced", "INTEGER")):
            if col not in existing:
                conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {decl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_dev "
            "ON readings(device_id, id DESC)"
        )
        # 摄像头帧元数据表（JPEG 文件本身存 snapshots/，这里只记索引便于核对）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS frames(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                ts_server TEXT NOT NULL,
                filename  TEXT,
                bytes     INTEGER
            )"""
        )
        # 第2周扩展：把「这一帧属于哪次请求」以及证据字段一并记下来，
        # 否则无法回答本课题眼——「这张图到底是不是这次拍的那张」。
        frame_cols = {r[1] for r in conn.execute("PRAGMA table_info(frames)")}
        for col, decl in (("request_id", "TEXT"),   # E1 请求贯穿
                          ("capture_ts", "TEXT"),   # E2 时序校验
                          ("boot_id", "TEXT"),      # E3 新鲜度（开机标识）
                          ("seq", "INTEGER"),       # E3 新鲜度（开机内序号）
                          ("ts_device", "TEXT")):
            if col not in frame_cols:
                conn.execute(f"ALTER TABLE frames ADD COLUMN {col} {decl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_frames_req ON frames(request_id)"
        )

        # 第2周：远程采集指令表。一行 = 一次「请求」，全生命周期都在这一行上流转。
        conn.execute(
            """CREATE TABLE IF NOT EXISTS commands(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id    TEXT NOT NULL UNIQUE,
                device_id     TEXT NOT NULL,
                action        TEXT NOT NULL,
                sensor        TEXT,
                state         TEXT NOT NULL,
                ttl_s         INTEGER NOT NULL,
                created_at    TEXT NOT NULL,
                dispatched_at TEXT,
                ack_at        TEXT,
                device_ts     TEXT,
                boot_id       TEXT,
                seq           INTEGER,
                capture_ts    TEXT,
                frame_id      INTEGER,
                frame_name    TEXT,
                evidence_ok   INTEGER,
                fail_reason   TEXT,
                updated_at    TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_commands_dev "
            "ON commands(device_id, id)"
        )
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ---------------- 第2周：指令通道的公共逻辑 ----------------

# 状态 -> 中文标签，网页直接用，避免前端再维护一份映射
STATE_LABEL = {
    ST_PENDING:   "待设备取走",
    ST_RECEIVED:  "设备已接收",
    ST_EXECUTING: "设备执行中",
    ST_UPLOADED:  "观测已入库",
    ST_COMPLETED: "已完成",
    ST_EXPIRED:   "已过期（无设备取走）",
    ST_TIMEOUT:   "已超时（取走未回传）",
    ST_FAILED:    "失败",
}

# 终态：落到这些状态就不再被后台扫描改动
FINAL_STATES = (ST_COMPLETED, ST_EXPIRED, ST_TIMEOUT, ST_FAILED)


def new_request_id() -> str:
    """短、可读、人眼可核对的请求编号，例如 req-20260918-110912-3f7a。

    刻意做成短码：第2周要防的就是「页面拿旧图冒充本次结果」，
    所以这个编号必须能一眼抄下来核对，而不是一串 UUID。
    """
    return "req-%s-%s" % (datetime.now(TZ).strftime("%Y%m%d-%H%M%S"),
                          os.urandom(2).hex())


def add_seconds(iso: str, seconds: int) -> str:
    """ISO8601 字符串 + N 秒，解析失败时原样返回（不编造时间）。"""
    try:
        return (datetime.fromisoformat(iso)
                + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    except (ValueError, TypeError):
        return iso


def command_timeline(row: sqlite3.Row) -> list:
    """把一行指令摊成「状态时间线」，页面据此显示卡在哪一步。"""
    marks = (
        ("created_at",    ST_PENDING,   "网页下发指令"),
        ("dispatched_at", ST_RECEIVED,  "设备取走并回执"),
        ("ack_at",        ST_EXECUTING, "设备开始采集"),
        ("capture_ts",    ST_UPLOADED,  "观测入库"),
    )
    line = []
    for col, state, note in marks:
        if row[col]:
            line.append({"state": state, "label": STATE_LABEL[state],
                         "at": row[col], "note": note})
    if row["state"] in FINAL_STATES:
        line.append({"state": row["state"], "label": STATE_LABEL[row["state"]],
                     "at": row["updated_at"], "note": row["fail_reason"] or "闭环结束"})
    return line


def command_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["state_label"] = STATE_LABEL.get(row["state"], row["state"])
    d["expires_at"] = add_seconds(row["created_at"], row["ttl_s"])
    d["is_final"] = row["state"] in FINAL_STATES
    # 只有「证据齐全」才允许页面展示图像；否则页面必须显示"未完成"，
    # 绝不能回落到 latest.jpg —— 那正是本课题眼要排除的"旧值冒充"。
    d["has_frame"] = bool(row["frame_name"]) and row["state"] == ST_COMPLETED
    d["timeline"] = command_timeline(row)
    return d


def command_for_device(row: sqlite3.Row) -> dict:
    """给开发板看的精简载荷。

    刻意不带 state / timeline —— 板子只需要"执行什么"，
    返回体越小，MCU 侧解析用的内存就越小（ESP32 上这是实打实的约束）。
    """
    return {
        "request_id": row["request_id"],
        "device_id": row["device_id"],
        "action": row["action"],
        "sensor": row["sensor"],
        "ttl_s": row["ttl_s"],
        "created_at": row["created_at"],
        "expires_at": add_seconds(row["created_at"], row["ttl_s"]),
    }


def verify_evidence(conn: sqlite3.Connection, cmd: sqlite3.Row,
                    frame_row: sqlite3.Row) -> tuple:
    """三条证据校验。返回 (是否通过, 不通过原因)。

    这是整个第2周的核心：UPLOADED 不等于 COMPLETED，
    收到一张图不代表它就是这次要的那张，必须过这里才允许置 COMPLETED。
    """
    # --- E1 请求贯穿：观测必须带着本次的 request_id ---
    if (frame_row["request_id"] or "") != cmd["request_id"]:
        return False, "E1 不通过：观测未携带本次 request_id，无法关联到该请求"

    # --- E2 时序合理：采集时刻必须晚于指令下发时刻 ---
    cap = frame_row["capture_ts"] or ""
    if cmd["dispatched_at"] and cap and not cap.startswith("uptime+"):
        if cap <= cmd["dispatched_at"]:
            return False, ("E2 不通过：capture_ts(%s) 早于指令下发时刻(%s)，"
                           "疑似把库里的旧图重新提交" % (cap, cmd["dispatched_at"]))
    # 注：设备未对时时 capture_ts 形如 "uptime+12.345s"，无法与服务器时间比较，
    # 此时 E2 自动跳过，由 E3 兜底 —— 这正是 boot_id + seq 存在的意义。

    # --- E3 新鲜度单调：同一次开机内 seq 必须严格递增 ---
    boot = frame_row["boot_id"] or ""
    seq = frame_row["seq"]
    if boot and seq is not None:
        prev = conn.execute(
            "SELECT MAX(seq) AS m FROM frames "
            "WHERE device_id=? AND boot_id=? AND id<?",
            (cmd["device_id"], boot, frame_row["id"]),
        ).fetchone()
        prev_seq = prev["m"] if prev else None
        if prev_seq is not None and seq <= prev_seq:
            return False, ("E3 不通过：seq=%d 未超过同一开机的上一条 seq=%d，"
                           "疑似把同一帧重复上传冒充新拍" % (seq, prev_seq))
    return True, ""


def sweep_commands(conn: sqlite3.Connection) -> int:
    """把卡住的指令推进到 EXPIRED / TIMEOUT。返回本次改动的条数。

    PENDING 超时 -> EXPIRED（没人取）；RECEIVED/EXECUTING 超时 -> TIMEOUT（取了没回传）。
    注意：超时只说明「这次没拿到结果」，不等于硬件故障 —— 所以绝不写 FAILED。
    """
    now = datetime.now(TZ)
    changed = 0
    rows = conn.execute(
        "SELECT * FROM commands WHERE state IN (?,?,?)",
        (ST_PENDING, ST_RECEIVED, ST_EXECUTING),
    ).fetchall()
    for r in rows:
        if r["state"] == ST_PENDING:
            base, limit, nxt = r["created_at"], r["ttl_s"], ST_EXPIRED
        else:
            base = r["dispatched_at"] or r["created_at"]
            limit, nxt = EXEC_TIMEOUT_S, ST_TIMEOUT
        try:
            age = (now - datetime.fromisoformat(base)).total_seconds()
        except (ValueError, TypeError):
            continue
        if age > limit:
            conn.execute(
                "UPDATE commands SET state=?, updated_at=?, fail_reason=? WHERE id=?",
                (nxt, now_iso(), "%s：等待超过 %d 秒" % (STATE_LABEL[nxt], limit),
                 r["id"]),
            )
            changed += 1
    if changed:
        conn.commit()
    return changed


class Handler(BaseHTTPRequestHandler):
    server_version = "IMUIngest/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def _end_headers_close(self) -> None:
        """收尾响应头，并明确告诉对方「这条连接用完就关」。

        本服务端跑在 HTTP/1.1 上，而 HTTP/1.1 默认是**持久连接**。
        设备端固件用的却是短连接语义：它读完响应头就直接 close socket，
        从不发 Connection: close。两边语义不一致会留下半开连接 ——
        服务端仍认为连接有效、继续等下一个请求，而板子早已离开，
        服务端随后读到 RST，日志里刷 ConnectionResetError；
        板子那边也会在后续请求上撞到 errno=Connection already in progress。
        显式回 Connection: close 把语义对齐，两边都干净。
        """
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._end_headers_close()

    def _send_html(self) -> None:
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "index.html 不存在"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._end_headers_close()
        self.wfile.write(body)

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _read_json_body(self):
        """读并解析 JSON 请求体。任何一步失败都自行回包并返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json({"error": "请求体长度非法"}, 400)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "JSON 解析失败"}, 400)
            return None
        if not isinstance(data, dict):
            self._send_json({"error": "请求体必须是 JSON 对象"}, 400)
            return None
        return data

    def _send_image_file(self, path: str) -> None:
        """把磁盘上的一张 JPEG 原样回给浏览器（<img> 直接引用）。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "读取图像失败"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        # 请求级图像内容不会变，但仍禁用缓存，避免课堂上"换了图没变"的误会
        self.send_header("Cache-Control", "no-store")
        self._end_headers_close()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 精简日志
        print("[%s] %s" % (now_iso(), fmt % args), flush=True)

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        q = self._query()
        if path == "/":
            self._send_html()
        elif path == "/api/health":
            self._send_json({"ok": True, "server_now": now_iso()})
        elif path == "/api/latest":
            self._handle_latest(q)
        elif path == "/api/history":
            self._handle_history(q)
        elif path == "/api/devices":
            self._handle_devices()
        elif path == "/api/frame/latest":
            self._handle_frame_image()
        elif path == "/api/command/poll":
            self._handle_command_poll(q)
        elif path == "/api/command/status":
            self._handle_command_status(q)
        elif path == "/api/command/frame":
            self._handle_command_frame(q)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_latest(self, q: dict) -> None:
        device_id = (q.get("device_id") or [""])[0]
        conn = get_db()
        try:
            if device_id:
                row = conn.execute(
                    "SELECT * FROM readings WHERE device_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (device_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self._send_json(
                {"record": row_to_dict(row) if row else None,
                 "server_now": now_iso()}
            )
        finally:
            conn.close()

    def _handle_history(self, q: dict) -> None:
        device_id = (q.get("device_id") or [""])[0]
        try:
            limit = max(1, min(200, int((q.get("limit") or ["20"])[0])))
        except ValueError:
            limit = 20
        conn = get_db()
        try:
            if device_id:
                rows = conn.execute(
                    "SELECT * FROM readings WHERE device_id=? "
                    "ORDER BY id DESC LIMIT ?",
                    (device_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM readings ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            self._send_json(
                {"records": [row_to_dict(r) for r in rows],
                 "server_now": now_iso()}
            )
        finally:
            conn.close()

    def _handle_devices(self) -> None:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT device_id, sensor, COUNT(*) AS n, "
                "MAX(ts_server) AS last_seen "
                "FROM readings GROUP BY device_id ORDER BY last_seen DESC"
            ).fetchall()
            self._send_json(
                {"devices": [row_to_dict(r) for r in rows],
                 "server_now": now_iso()}
            )
        finally:
            conn.close()

    # ---------- 摄像头帧 ----------
    def _handle_frame(self) -> None:
        """开发板 POST 一帧 JPEG（二进制 body，自定义头携带设备编号与板端时间）

        第2周新增四个请求头，用于把这一帧和「某一次请求」绑起来：
          X-Request-Id  本次所属请求（命令触发时必填，E1 请求贯穿）
          X-Capture-Ts  采集时刻（E2 时序校验）
          X-Boot-Id     本次开机标识（E3 新鲜度校验）
          X-Seq         开机内递增序号（E3 新鲜度校验）
        周期性抓拍不带 X-Request-Id，只入库、不参与命令闭环。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_FRAME:
            self._send_json({"error": "帧长度非法（0 或超 1MB）"}, 400)
            return
        data = self.rfile.read(length)
        # 最简 JPEG 校验：以 FF D8 开头、FF D9 结尾
        if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
            self._send_json({"error": "收到的不是 JPEG 数据"}, 400)
            return
        device_id = (self.headers.get("X-Device-Id") or "").strip()
        request_id = (self.headers.get("X-Request-Id") or "").strip()
        capture_ts = (self.headers.get("X-Capture-Ts") or "").strip()
        boot_id = (self.headers.get("X-Boot-Id") or "").strip()
        ts_device = (self.headers.get("X-Ts-Device") or "").strip()
        try:
            seq = int(self.headers.get("X-Seq") or "")
        except ValueError:
            seq = None
        ts = now_iso()
        try:
            fname = save_frame(data, ts)
        except OSError as e:
            self._send_json({"error": "保存图像失败: %s" % e}, 500)
            return
        with _db_lock:
            conn = get_db()
            try:
                cur = conn.execute(
                    "INSERT INTO frames(device_id, ts_server, filename, bytes, "
                    "request_id, capture_ts, boot_id, seq, ts_device) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (device_id or None, ts, fname, len(data),
                     request_id or None, capture_ts or None,
                     boot_id or None, seq, ts_device or None),
                )
                conn.commit()
                frame_id = cur.lastrowid
            finally:
                conn.close()

        # 命令触发的帧：立刻走证据校验，决定是 UPLOADED 还是 COMPLETED / FAILED
        verdict = None
        if request_id:
            verdict = self._link_command_frame(request_id, frame_id)

        self._send_json({"ok": True, "bytes": len(data),
                         "device_id": device_id, "ts_server": ts,
                         "request_id": request_id or None,
                         "evidence": verdict}, 201)

    def _link_command_frame(self, request_id: str, frame_id: int):
        """把刚入库的这一帧关联到指令上，并做三条证据校验。

        返回校验结论，随 /api/frame 的响应一起回给开发板 ——
        这样串口日志里能直接看到「这次到底算不算成功、不成功是差哪条证据」。
        """
        ts = now_iso()
        with _db_lock:
            conn = get_db()
            try:
                cmd = conn.execute("SELECT * FROM commands WHERE request_id=?",
                                   (request_id,)).fetchone()
                if cmd is None:
                    return {"ok": False, "reason": "未知的 request_id"}
                if cmd["state"] in FINAL_STATES:
                    return {"ok": False, "state": cmd["state"],
                            "reason": "指令已处于终态，本次观测仅入库"}
                frame = conn.execute("SELECT * FROM frames WHERE id=?",
                                     (frame_id,)).fetchone()
                ok, reason = verify_evidence(conn, cmd, frame)
                new_state = ST_COMPLETED if ok else ST_FAILED
                conn.execute(
                    "UPDATE commands SET state=?, frame_id=?, frame_name=?, "
                    "capture_ts=?, evidence_ok=?, fail_reason=?, updated_at=? "
                    "WHERE id=?",
                    (new_state, frame_id, frame["filename"],
                     frame["capture_ts"] or ts, 1 if ok else 0,
                     None if ok else reason, ts, cmd["id"]),
                )
                conn.commit()
            finally:
                conn.close()
        print("[%s] 观测入库并校验 %s -> %s%s"
              % (ts, request_id, new_state, "" if ok else "（%s）" % reason),
              flush=True)
        return {"ok": ok, "state": new_state, "reason": None if ok else reason}

    def _handle_frame_image(self) -> None:
        """返回最新一帧 JPEG，供网页 <img> 实时刷新显示"""
        latest = os.path.join(SNAP_DIR, "latest.jpg")
        if not os.path.exists(latest):
            self._send_json({"error": "暂无图像，等待开发板上传"}, 404)
            return
        self._send_image_file(latest)

    # ---------- 第2周：远程采集指令 ----------
    def _handle_command_create(self) -> None:
        """网页下发一条采集指令。

        每次点击都生成独立的 request_id —— 重复点击不会被合并成一条，
        否则「两次点击共用一条记录」会让状态互相覆盖，追踪就失效了。
        """
        data = self._read_json_body()
        if data is None:
            return
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            self._send_json({"error": "device_id 为必填字段"}, 400)
            return
        action = str(data.get("action") or "capture").strip()
        sensor = str(data.get("sensor") or "camera").strip()
        try:
            ttl = int(data.get("ttl_s") or DEFAULT_TTL_S)
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL_S
        ttl = max(5, min(3600, ttl))

        request_id = new_request_id()
        ts = now_iso()
        with _db_lock:
            conn = get_db()
            try:
                cur = conn.execute(
                    "INSERT INTO commands(request_id, device_id, action, sensor, "
                    "state, ttl_s, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (request_id, device_id, action, sensor,
                     ST_PENDING, ttl, ts, ts),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM commands WHERE id=?",
                                   (cur.lastrowid,)).fetchone()
            finally:
                conn.close()
        print("[%s] 已下发指令 %s -> %s（TTL %d 秒）"
              % (ts, request_id, device_id, ttl), flush=True)
        self._send_json({"ok": True, "command": command_to_dict(row)}, 201)

    def _handle_command_poll(self, q: dict) -> None:
        """开发板轮询取指令。

        取走即置 RECEIVED 并记下 dispatched_at —— 这是「设备已接收」
        这个状态唯一的证据来源，没有它就只能说"已下发，不知道设备收没收到"。
        """
        device_id = (q.get("device_id") or [""])[0].strip()
        if not device_id:
            self._send_json({"error": "device_id 为必填参数"}, 400)
            return
        with _db_lock:
            conn = get_db()
            try:
                sweep_commands(conn)
                row = conn.execute(
                    "SELECT * FROM commands WHERE device_id=? AND state=? "
                    "ORDER BY id ASC LIMIT 1",
                    (device_id, ST_PENDING),
                ).fetchone()
                if row is None:
                    self._send_json({"command": None, "server_now": now_iso()})
                    return
                ts = now_iso()
                conn.execute(
                    "UPDATE commands SET state=?, dispatched_at=?, updated_at=? "
                    "WHERE id=?",
                    (ST_RECEIVED, ts, ts, row["id"]),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM commands WHERE id=?",
                                   (row["id"],)).fetchone()
            finally:
                conn.close()
        print("[%s] 指令 %s 已被 %s 取走"
              % (ts, row["request_id"], device_id), flush=True)
        self._send_json({"command": command_for_device(row),
                         "server_now": now_iso()})

    def _handle_command_ack(self) -> None:
        """开发板回执。携带板端时间、开机标识与序号，供 E2 / E3 校验。"""
        data = self._read_json_body()
        if data is None:
            return
        request_id = str(data.get("request_id") or "").strip()
        if not request_id:
            self._send_json({"error": "request_id 为必填字段"}, 400)
            return
        state = str(data.get("state") or ST_EXECUTING).strip()
        if state not in (ST_EXECUTING, ST_FAILED):
            state = ST_EXECUTING
        seq = data.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            seq = None

        with _db_lock:
            conn = get_db()
            try:
                row = conn.execute("SELECT * FROM commands WHERE request_id=?",
                                   (request_id,)).fetchone()
                if row is None:
                    self._send_json({"error": "未知的 request_id"}, 404)
                    return
                # 幂等：终态指令被重复回执时不改状态，只回报当前状态
                if row["state"] in FINAL_STATES:
                    self._send_json({"ok": True, "idempotent": True,
                                     "command": command_to_dict(row)})
                    return
                ts = now_iso()
                conn.execute(
                    "UPDATE commands SET state=?, ack_at=?, device_ts=?, "
                    "boot_id=?, seq=?, fail_reason=?, updated_at=? WHERE id=?",
                    (state, ts,
                     str(data.get("device_ts") or ""),
                     str(data.get("boot_id") or ""),
                     seq,
                     (str(data.get("reason") or "") or "设备显式报错")
                     if state == ST_FAILED else None,
                     ts, row["id"]),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM commands WHERE id=?",
                                   (row["id"],)).fetchone()
            finally:
                conn.close()
        print("[%s] 收到回执 %s（state=%s, boot_id=%s, seq=%s）"
              % (ts, request_id, row["state"], row["boot_id"], row["seq"]),
              flush=True)
        self._send_json({"ok": True, "command": command_to_dict(row)})

    def _handle_command_status(self, q: dict) -> None:
        """网页查询指令状态。页面每秒轮询这里，状态时间线也来自这里。"""
        request_id = (q.get("request_id") or [""])[0].strip()
        device_id = (q.get("device_id") or [""])[0].strip()
        try:
            limit = max(1, min(50, int((q.get("limit") or ["10"])[0])))
        except ValueError:
            limit = 10
        with _db_lock:
            conn = get_db()
            try:
                sweep_commands(conn)
                if request_id:
                    rows = conn.execute(
                        "SELECT * FROM commands WHERE request_id=?",
                        (request_id,)).fetchall()
                elif device_id:
                    rows = conn.execute(
                        "SELECT * FROM commands WHERE device_id=? "
                        "ORDER BY id DESC LIMIT ?",
                        (device_id, limit)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM commands ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
            finally:
                conn.close()
        self._send_json({"commands": [command_to_dict(r) for r in rows],
                         "server_now": now_iso()})

    def _handle_command_frame(self, q: dict) -> None:
        """取「本次请求」对应的那一帧，而不是"最新一帧"。

        ★ 刻意不做「取不到就回落到 latest.jpg」——
        那正是本课题眼要排除的旧值冒充。请求没完成就返回 404，
        页面据此显示"未完成"，绝不允许拿库里的旧图顶上。
        """
        request_id = (q.get("request_id") or [""])[0].strip()
        if not request_id:
            self._send_json({"error": "request_id 为必填参数"}, 400)
            return
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM commands WHERE request_id=?",
                               (request_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            self._send_json({"error": "未知的 request_id"}, 404)
            return
        if row["state"] != ST_COMPLETED or not row["frame_name"]:
            self._send_json(
                {"error": "本次请求尚未完成，无可用图像",
                 "state": row["state"],
                 "state_label": STATE_LABEL.get(row["state"], row["state"])},
                404)
            return
        self._send_image_file(os.path.join(SNAP_DIR, row["frame_name"]))

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/frame":
            self._handle_frame()
            return
        if path == "/api/command":
            self._handle_command_create()
            return
        if path == "/api/command/ack":
            self._handle_command_ack()
            return
        if path != "/api/ingest":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json({"error": "请求体长度非法"}, 400)
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "JSON 解析失败"}, 400)
            return
        if not isinstance(data, dict):
            self._send_json({"error": "请求体必须是 JSON 对象"}, 400)
            return
        device_id = str(data.get("device_id") or "").strip()
        sensor = str(data.get("sensor") or "").strip()
        if not device_id or not sensor:
            self._send_json(
                {"error": "device_id 与 sensor 为必填字段"}, 400)
            return

        def num(key):
            v = data.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            return None

        def intnum(key):
            """原始 ADC 计数为整数。bool 不算数（会被当成 0/1 造成误读）"""
            v = data.get(key)
            if isinstance(v, bool):
                return None
            if isinstance(v, int):
                return v
            if isinstance(v, float):
                return int(v)
            return None

        record = {
            "device_id": device_id,
            "sensor": sensor,
            "unit": str(data.get("unit") or "g"),
            "ax": num("ax"), "ay": num("ay"), "az": num("az"),
            # 原始 14 位 ADC 计数，用于与板端串口日志三处对账
            "ax_raw": intnum("ax_raw"),
            "ay_raw": intnum("ay_raw"),
            "az_raw": intnum("az_raw"),
            "is_new_sample": 1 if data.get("is_new_sample") else 0,
            "time_synced": 1 if data.get("time_synced") else 0,
            "ts_device": str(data.get("ts_device") or ""),
            "ts_server": now_iso(),
        }
        with _db_lock:
            conn = get_db()
            try:
                cur = conn.execute(
                    "INSERT INTO readings(device_id, sensor, unit, "
                    "ax, ay, az, ax_raw, ay_raw, az_raw, "
                    "is_new_sample, time_synced, ts_device, ts_server) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record["device_id"], record["sensor"], record["unit"],
                     record["ax"], record["ay"], record["az"],
                     record["ax_raw"], record["ay_raw"], record["az_raw"],
                     record["is_new_sample"], record["time_synced"],
                     record["ts_device"], record["ts_server"]),
                )
                conn.commit()
                record_id = cur.lastrowid
            finally:
                conn.close()
        self._send_json({"ok": True, "id": record_id,
                         "ts_server": record["ts_server"]}, 201)


def save_frame(data: bytes, ts: str) -> str:
    """把一帧 JPEG 存入 snapshots/：按时间戳命名归档，并覆盖 latest.jpg 供实时显示。"""
    os.makedirs(SNAP_DIR, exist_ok=True)
    # ts 形如 2026-09-11T10:00:00.123+08:00，去掉文件名非法字符
    safe = ts.replace(":", "-").replace("+", "_").replace(".", "-")
    fname = safe + ".jpg"
    full = os.path.join(SNAP_DIR, fname)
    with open(full, "wb") as f:
        f.write(data)
    latest = os.path.join(SNAP_DIR, "latest.jpg")
    with open(latest, "wb") as f:   # 覆盖式写入，网页固定读这一个文件
        f.write(data)
    return fname


def command_sweeper() -> None:
    """后台把卡住的指令推进到 EXPIRED / TIMEOUT。

    页面轮询时也会顺手扫一次，但后台线程保证「没人看页面」时状态机依然自洽 ——
    比如设备关机后下发、页面又关掉了，指令仍会在 TTL 到点后转 EXPIRED。
    """
    while True:
        time.sleep(SWEEP_INTERVAL_S)
        try:
            with _db_lock:
                conn = get_db()
                try:
                    n = sweep_commands(conn)
                finally:
                    conn.close()
            if n:
                print("[%s] 后台扫描：%d 条指令超时（转 EXPIRED / TIMEOUT）"
                      % (now_iso(), n), flush=True)
        except Exception as e:      # 后台线程绝不能因异常退出
            print("[%s] 后台扫描异常: %s" % (now_iso(), e), flush=True)


def main() -> None:
    init_db()
    threading.Thread(target=command_sweeper, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except OSError:
        lan = "127.0.0.1"
    print("=" * 60, flush=True)
    print("传感器接收服务已启动", flush=True)
    print("  本机访问:  http://127.0.0.1:%d/" % PORT, flush=True)
    print("  局域网访问: http://%s:%d/  ← 填到开发板固件里" % (lan, PORT),
          flush=True)
    print("  指令通道:  POST /api/command | GET /api/command/poll"
          " | POST /api/command/ack | GET /api/command/status", flush=True)
    print("=" * 60, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
