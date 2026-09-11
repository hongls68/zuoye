#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI交互课 第1周 —— 最小传感器数据接收与存储服务
仅使用 Python 标准库，无第三方依赖。

功能：
  POST /api/ingest                开发板上传一条传感器记录（JSON）
  GET  /api/latest?device_id=X    查询某设备最新一条记录
  GET  /api/history?device_id=X&limit=N   查询某设备历史记录（新→旧）
  GET  /api/devices               列出所有上报过的设备及其最后上报时间
  GET  /api/health                服务自检
  GET  /                          Web 展示页面（index.html）

运行：python server.py   （默认监听 0.0.0.0:8000）
"""
import json
import os
import socket
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
HTML_PATH = os.path.join(BASE_DIR, "index.html")
SNAP_DIR = os.path.join(BASE_DIR, "snapshots")  # 摄像头帧存这里
PORT = 8000
TZ = timezone(timedelta(hours=8))  # 东八区，与板端时间口径一致
MAX_BODY = 64 * 1024               # /api/ingest 的 JSON 上限
MAX_FRAME = 1 * 1024 * 1024        # /api/frame 的单帧 JPEG 上限（1MB）

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
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class Handler(BaseHTTPRequestHandler):
    server_version = "IMUIngest/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

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
        """开发板 POST 一帧 JPEG（二进制 body，自定义头携带设备编号与板端时间）"""
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
                    "INSERT INTO frames(device_id, ts_server, filename, bytes) "
                    "VALUES(?,?,?,?)",
                    (device_id or None, ts, fname, len(data)),
                )
                conn.commit()
            finally:
                conn.close()
        self._send_json({"ok": True, "bytes": len(data),
                         "device_id": device_id, "ts_server": ts}, 201)

    def _handle_frame_image(self) -> None:
        """返回最新一帧 JPEG，供网页 <img> 实时刷新显示"""
        latest = os.path.join(SNAP_DIR, "latest.jpg")
        if not os.path.exists(latest):
            self._send_json({"error": "暂无图像，等待开发板上传"}, 404)
            return
        try:
            with open(latest, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "读取图像失败"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/frame":
            self._handle_frame()
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


def main() -> None:
    init_db()
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
    print("=" * 60, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
