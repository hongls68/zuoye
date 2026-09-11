#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三处对账核对.py —— 一次性验证「板端 / 服务器 / 网页数据源」三处数值一致

用途：老师课堂要检查"倾斜板子时三处数值必须一致"。本脚本直接查服务器数据库
和查询接口，把两边结果并排打印，你只需再和串口日志、网页肉眼对一次即可。

用法：
    python 三处对账核对.py                # 列出所有设备并核对最新一条
    python 三处对账核对.py s3eye-group07  # 只核对指定设备编号

前提：server.py 正在运行（本脚本会通过 HTTP 访问它的查询接口）。
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER = "http://127.0.0.1:8000"
DB_PATH = Path(__file__).parent / "server" / "data.db"
TZ = timezone(timedelta(hours=8))


def http_get(path: str):
    """访问服务器查询接口，失败返回 None 而不是抛异常中断脚本"""
    try:
        with urllib.request.urlopen(SERVER + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[错误] 访问接口 {path} 失败: {e}")
        print(f"       请确认服务端已启动：python server/server.py")
        return None


def query_db_latest(device_id: str = ""):
    """直接读数据库，绕开 HTTP 层，用于交叉验证接口没有篡改数据"""
    if not DB_PATH.exists():
        print(f"[错误] 数据库文件不存在: {DB_PATH}")
        print("       说明服务端从未成功启动过。")
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        if device_id:
            row = conn.execute(
                "SELECT * FROM readings WHERE device_id=? ORDER BY id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fmt_record(rec: dict) -> str:
    if not rec:
        return "（无数据）"
    return (
        f"  记录#{rec.get('id')}  设备={rec.get('device_id')}  传感器={rec.get('sensor')}\n"
        f"  ax={rec.get('ax'):+.3f} {rec.get('unit')}  "
        f"ay={rec.get('ay'):+.3f} {rec.get('unit')}  "
        f"az={rec.get('az'):+.3f} {rec.get('unit')}\n"
        f"  板端时间={rec.get('ts_device')}\n"
        f"  入库时间={rec.get('ts_server')}"
    )


def main() -> int:
    device_id = sys.argv[1] if len(sys.argv) > 1 else ""
    print("=" * 66)
    print("三处对账核对 · " + datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 66)

    # 0) 服务是否活着
    health = http_get("/api/health")
    if health is None:
        return 1
    print(f"[服务端] 在线，服务器当前时间 {health.get('server_now')}\n")

    # 1) 有哪些设备在上报
    devices = http_get("/api/devices")
    if devices and devices.get("devices"):
        print("[设备列表]（老师检查项：数据确实来自本组设备）")
        for d in devices["devices"]:
            print(f"  {d['device_id']:<24} 传感器={d['sensor']:<16} "
                  f"共 {d['n']} 条  最后上报 {d['last_seen']}")
        print()
    else:
        print("[设备列表] 还没有任何设备上报过数据。\n"
              "  请检查：固件是否烧录成功、Wi-Fi 是否连上、SERVER_URL 是否填的局域网 IP。\n")

    # 2) 接口查到的最新一条
    q = f"?device_id={device_id}" if device_id else ""
    latest = http_get(f"/api/latest{q}")
    api_rec = latest.get("record") if latest else None
    print(f"[来源A · HTTP 查询接口 /api/latest{q}]")
    print(fmt_record(api_rec))
    print()

    # 3) 直接查数据库
    db_rec = query_db_latest(device_id)
    print(f"[来源B · 直接读数据库 {DB_PATH.name}]")
    print(fmt_record(db_rec))
    print()

    # 4) 两路数据比对
    print("-" * 66)
    if api_rec is None or db_rec is None:
        print("[结论] 数据不足，无法比对。请先让板子成功上传至少一条记录。")
        return 1

    fields = ["id", "device_id", "sensor", "unit", "ax", "ay", "az",
              "ts_device", "ts_server"]
    diffs = [f for f in fields if api_rec.get(f) != db_rec.get(f)]
    if diffs:
        print(f"[结论] ✗ 不一致，差异字段: {', '.join(diffs)}")
        print("       接口层可能篡改了数据，需检查 server.py 的查询实现。")
        return 1

    print("[结论] ✓ 查询接口与数据库内容完全一致，服务器侧无篡改。")
    print()
    print("接下来还需你肉眼确认另外两处：")
    print(f"  · 板端：idf.py -p COM4 monitor 的最新一行 raw/g 数值")
    print(f"  · 网页：{SERVER}/ 页面上的三个大数字")
    print(f"  三者的 ax/ay/az 应一致（网页与接口均保留 3 位小数）。")

    # 5) 顺手提示数据新鲜度
    last = db_rec.get("ts_server")
    try:
        age = (datetime.now(TZ) - datetime.fromisoformat(last)).total_seconds()
        if age > 10:
            print(f"\n[提示] 最新一条已 {age:.0f} 秒未更新，"
                  f"若板子仍在运行请检查网络；若已拔电，这正是网页显示"
                  f"「数据未更新」的预期表现。")
        else:
            print(f"\n[提示] 数据新鲜，最新一条距今 {age:.0f} 秒。")
    except (TypeError, ValueError):
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
