#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第4周 · 用自然语言查询与请求采集（受限工具 + 结构化输出）

【这一周在做什么】
把前两周攒下来的查询接口和采集接口，封装成一组**受限工具**，
交给一个**运行时语言模型**去调用；用户说人话，模型决定调哪个工具、填什么参数。

【必须先分清的两个角色 —— 这是本周教学内容的第一个点】
  · 开发助手（写这份代码的那个 AI）：在**开发期**参与写代码、查资料、排错。
    它不参与产品运行，用户看不到它。
  · 产品运行时模型（本文件里的 Ollama 模型）：在**运行期**被 `ask()` 调用，
    负责把用户的一句话映射成工具调用。它只看得见这里给它的工具和规则，
    看不见仓库、看不见这份注释、也看不见开发期的对话。

  两者混为一谈，就会出现"以为模型什么都知道"的设计错误 ——
  它不知道的事，只能靠**工具返回的结构化结果**告诉它。所以工具返回什么，
  决定了它有没有可能说实话。

【第 5 周数据接入 —— 这里踩过一个自己给自己挖的坑】
  第 5 周新加了姿态与波形数据（wave_batches 表），但**同一个页面上的自然语言问答
  一开始看不见它**：工具清单里没有相关工具，设备白名单也没扫这张表。
  结果是"网页上画得出来、问答里问不出来" —— 同一份数据两套视野。
  ★ 所以：**加一张表，就要同时问三件事** ——
      ① 白名单（known_devices）扫了吗？ → 不扫，只传波形的设备会被判成"没这台设备"
      ② 工具有吗？                     → 没有工具，模型只能干说"我查不到"
      ③ 提示词里的口径写了吗？          → 前两条决定"能不能查到"，这条决定"查到了会不会说错"
    第 ③ 条在姿态这块最典型：**航向在原理上不可测**（无陀螺仪/磁力计），
    不写进提示词，模型面对"板子朝哪边"只会留空、或者干脆编一个 0°。

【本文件的四条硬约束】
  1. 受限工具：模型只能调下面 TOOL_SPECS 里列出的工具，**没有自由写 SQL 的能力**。
     每个工具的参数都过白名单校验（设备范围、行数上限、时间格式），非法即拒。
  2. 只读为主：查询类工具全部走固定模板 SQL，且再过一道
     `_assert_readonly()` —— 出现 INSERT/UPDATE/DELETE/DROP/ALTER 一律拒。
     唯一的"写"操作是 request_capture，且它只写 commands 表（下指令），不写数据。
  3. 防"假成功"：`request_capture` 下完指令立刻回读真实状态，
     返回 `state` 与 `success_claim_allowed`。**没有 COMPLETED 证据时，
      `success_claim_allowed` 一律为 False**，模型不许说"已采集成功"。
     即使模型硬说，`_guard_answer()` 也会在最终文案上再拦一道。
  4. 歧义不猜：设备不唯一、时间范围不清、指标不明 → 返回 `needs_clarification`
     和候选清单，让模型反问用户，而不是自己挑一个。

运行：
  python nl_agent.py "板子最近一次的加速度是多少"
  python nl_agent.py --json "让板子现在拍一张，然后告诉我拍好了没"
  python nl_agent.py --tools      # 只打印工具清单，不调模型
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or BASE_DIR
DB_PATH = os.path.join(DATA_DIR, "data.db")

OLLAMA_URL = os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or "smtek/Qwen3.8-27B:Q2_K_XL-12gb"
TZ = timezone(timedelta(hours=8))

# 行数硬上限：模型说 limit=100000 也只能拿这么多。
# 这不是性能优化，是"受限"的一部分 —— 工具能吐多少数据必须由我们定，不能由模型定。
MAX_ROWS = 200
DEFAULT_ROWS = 20
MAX_TURNS = 6                      # 最多几轮工具调用，防止模型绕圈
OLLAMA_TIMEOUT = 180

# 测试遗留设备：自测脚本留下的记录，真实板子不在其中。
# 用于"歧义判断"——只有一台真实设备时不必反问，多台才反问。
# 注意要同时看前缀和包含关系：板子叫 s3eye-group01，而自测记录叫 s3eye-selftest，
# 只按前缀匹配会把自测记录当成真实板子（这个坑在实测里真的踩到了）。
TEST_DEVICE_PREFIXES = ("selftest", "probe", "nl-", "demo-")
TEST_DEVICE_MARKERS = ("selftest", "probe", "demo", "test", "-tmp")

# ---- 第 5 周：姿态与波形的口径常量 ----
#
# ★ 这几个值**刻意在 server.py 里也有一份**，不 import 过来。
#   原因：server.py 顶部 `import nl_agent`，nl_agent 再 import server 就成环。
#   重复的代价是"改一处忘另一处"，所以下面那组自测会拿真库跑一遍，
#   一旦两边口径不一致就会露出来。
POSTURE_LABEL = {
    "flat":      "平放",
    "upright":   "竖直正面",
    "side_edge": "侧边直立",
    "tilted":    "自由倾斜",
    "unknown":   "无法判定",
}

# ★ 本板测不到航向。这句话必须**跟着数据一起**交给模型 ——
#   只在文档里写一遍没用，模型看不见文档。
YAW_NOTE = ("本板无陀螺仪/磁力计：航向（绕重力轴自转）在原理上不可测，"
            "不是没采到，也不是 0°")

# 姿态是怎么算出来的 —— 模型回答时必须能说清依据，而不是把它当成"读出来的一个字段"
ATTITUDE_BASIS = ("姿态由**重力方向**判定（加速度计静止时测的是支撑力，读数指向天空），"
                  "所以凡由倾斜决定的状态都测得到；"
                  "唯一测不到的是绕重力轴自转的航向。")

# 波形是"过程数据"不是"证据" —— 和原图分岔的保留策略，模型得知道，
# 否则会把"老波形被清理了"说成"设备从来没上报过波形"。
WAVE_RETENTION_NOTE = ("波形属过程数据，服务端按每设备最近 N 批滚动清理（默认 720 批）；"
                       "清理掉老批不等于设备没上报过。"
                       "原图相反：元数据（含 SHA-256）永久保留，只清原图。")

# 只读守卫：这些词出现在 SQL 里就直接拒。
# 正常情况下工具用的是固定模板 SQL，根本不会碰到；留着是为了
# "将来有人加工具时忘了守规矩"这一天的。
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b",
    re.IGNORECASE)


# ============================================================
# 一、校验与守卫
# ============================================================

def _assert_readonly(sql: str) -> None:
    if _FORBIDDEN_SQL.search(sql or ""):
        raise PermissionError("受限工具只允许只读查询，检测到写操作：%s"
                              % _FORBIDDEN_SQL.search(sql).group(0))
    if "select *" in (sql or "").lower():
        raise PermissionError("禁止 SELECT *：字段必须显式列出（避免无意中把新列吐给模型）")


def _run_readonly(conn, sql, params=(), max_rows=MAX_ROWS):
    """执行一条只读查询，并强制套上行数上限。"""
    _assert_readonly(sql)
    rows = conn.execute(sql, params).fetchmany(max_rows)
    return [dict(r) for r in rows]


def known_devices(conn) -> list:
    """设备白名单：所有在库里出现过数据的设备。

    ★ 白名单不是"模型想查谁就查谁"，而是**库里确实存在过的设备**。
    查一个从没上报过的 device_id，结果必然是空 ——
    那种空结果很容易被模型说成"设备没数据"，其实是"根本没这台设备"。
    """
    ids = set()
    # ★ 这张表清单必须跟着"有 device_id 的表"一起长。
    #   第 5 周加 wave_batches 时漏了这一步，后果很隐蔽：一台只上传波形、
    #   还没上报过 readings 的设备，会被判成"根本没这台设备" ——
    #   而它其实正在正常上传数据。所以下面加了一组自测盯着这件事。
    for table, col in (("readings", "device_id"), ("frames", "device_id"),
                       ("commands", "device_id"), ("help_events", "device_id"),
                       ("wave_batches", "device_id")):
        try:
            for r in conn.execute("SELECT DISTINCT %s AS d FROM %s" % (col, table)):
                if r["d"]:
                    ids.add(r["d"])
        except sqlite3.OperationalError:
            continue                      # 表还没建（比如首次运行）
    return sorted(ids)


def is_test_device(device_id: str) -> bool:
    d = (device_id or "").lower()
    if any(d.startswith(p) for p in TEST_DEVICE_PREFIXES):
        return True
    return any(m in d for m in TEST_DEVICE_MARKERS)


def real_devices(conn) -> list:
    return [d for d in known_devices(conn) if not is_test_device(d)]


def _check_device(conn, device_id) -> dict:
    """设备范围校验。返回 {'ok':..., 'error':..., 'candidates':[...]}"""
    known = known_devices(conn)
    if device_id in (None, ""):
        real = [d for d in known if not is_test_device(d)]
        if len(real) == 1:
            return {"ok": True, "device_id": real[0], "assumed": True}
        return {"ok": False,
                "error": "没有指定设备，且库里不止一台设备，不能替用户挑",
                "needs_clarification": True,
                "candidates": known,
                "hint": "请反问用户要查哪一台"}
    device_id = str(device_id).strip()
    if device_id not in known:
        # ★ 这里必须把"没这台设备"和"设备没数据"分开说。
        # 两者都表现为"查不到"，但排障方向完全相反：一个是设备编号写错了，
        # 一个是设备连上了但没上报。混成一句"没有数据"，会把用户带偏。
        return {"ok": False,
                "error": "设备 %r 根本没有这台设备（不在已知设备名单里，"
                         "没有任何一条上报记录）。"
                         "注意这与'设备存在但没有数据'是两回事。" % device_id,
                "candidates": known,
                "hint": "这不是'设备没数据'，是'根本没这台设备'，两者必须分开说"}
    return {"ok": True, "device_id": device_id, "assumed": False}


def _check_limit(limit) -> int:
    """行数参数校验 + 夹紧。模型给什么都不能突破 MAX_ROWS。"""
    if limit in (None, ""):
        return DEFAULT_ROWS
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是整数，收到 %r" % (limit,))
    if n <= 0:
        raise ValueError("limit 必须为正数")
    return min(n, MAX_ROWS)


_SINCE_RE = re.compile(r"^(\d+)\s*(s|m|h|d)$", re.IGNORECASE)


def _parse_since(since, now=None):
    """把 '10m' / '2h' / '3d' / ISO 时间 解析成 ISO 时间串。None 表示不限。"""
    if since in (None, ""):
        return None
    now = now or datetime.now(TZ)
    s = str(since).strip()
    m = _SINCE_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
        return (now - timedelta(seconds=secs)).isoformat(timespec="milliseconds")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("时间参数无法识别：%r（支持 10m / 2h / 3d 或 ISO 时间）" % s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.isoformat(timespec="milliseconds")


def _parse_iso_safe(s):
    """能解析就返回 datetime，不能就返回 None（**不抛异常**）。

    ★ 这里必须容错，因为板端在未对时的时候会把时间戳写成
      `uptime+12.345s(time_not_synced)` —— 这种串 parse 不了。
      板子 SNTP 对不上时是**常态**（第 1 周就实测到了），
      所以"解析失败"是预期内的一种正常输入，不是异常。
    """
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _ok(source, data, time=None, state=None, **extra):
    """统一的成功返回形状。

    ★ 每个结果都必须带 source / time / state 三要素 ——
    这是本周"基于真实结果回复来源、时间与状态"的技术落点：
    模型手里没有这三样，就只能编。
    """
    out = {"ok": True, "source": source, "time": time, "state": state, "data": data}
    out.update(extra)
    return out


def _err(source, message, **extra):
    out = {"ok": False, "source": source, "error": message}
    out.update(extra)
    return out


# ============================================================
# 二、受限工具实现
# ============================================================

def t_list_devices(conn, **_):
    """列出所有上报过的设备，附带最后上报时间与数据量。"""
    rows = _run_readonly(
        conn,
        "SELECT r.device_id, COUNT(*) AS n, MAX(r.ts_server) AS last_seen "
        "FROM readings r GROUP BY r.device_id ORDER BY last_seen DESC",
        max_rows=MAX_ROWS)
    for r in rows:
        r["is_test_device"] = is_test_device(r["device_id"])
    return _ok("GET /api/devices", rows, state="ok",
               note="is_test_device=true 的是自测脚本留下的记录，不是真实板子")


def t_get_latest_reading(conn, device_id=None, **_):
    chk = _check_device(conn, device_id)
    if not chk["ok"]:
        return _err("GET /api/latest", chk["error"],
                    needs_clarification=chk.get("needs_clarification", False),
                    candidates=chk.get("candidates", []), hint=chk.get("hint"))
    did = chk["device_id"]
    rows = _run_readonly(
        conn,
        "SELECT id, device_id, sensor, unit, ax, ay, az, ax_raw, ay_raw, az_raw, "
        "ts_device, ts_server, time_synced FROM readings "
        "WHERE device_id=? ORDER BY id DESC LIMIT 1",
        (did,), max_rows=1)
    if not rows:
        return _err("GET /api/latest", "该设备还没有任何一条读数",
                    device_id=did, state="empty")
    rec = rows[0]
    return _ok("GET /api/latest", rec, time=rec["ts_server"], state="ok",
               device_id=did,
               time_semantics={"ts_server": "服务端入库时刻（判定用）",
                               "ts_device": "板端采集时刻（板子自己报的）",
                               "time_synced": "板端是否已 SNTP 对时；false 时 ts_device 不可当绝对时间用"},
               assumed_device=chk.get("assumed", False))


def t_query_readings(conn, device_id=None, limit=None, since=None, **_):
    try:
        n = _check_limit(limit)
        since_iso = _parse_since(since)
    except ValueError as e:
        return _err("GET /api/history", str(e))
    chk = _check_device(conn, device_id)
    if not chk["ok"]:
        return _err("GET /api/history", chk["error"],
                    needs_clarification=chk.get("needs_clarification", False),
                    candidates=chk.get("candidates", []), hint=chk.get("hint"))
    did = chk["device_id"]
    sql = ("SELECT id, device_id, ax, ay, az, unit, ts_device, ts_server "
           "FROM readings WHERE device_id=?")
    params = [did]
    if since_iso:
        sql += " AND ts_server >= ?"
        params.append(since_iso)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(n)
    rows = _run_readonly(conn, sql, tuple(params), max_rows=n)
    if not rows:
        return _err("GET /api/history",
                    "该条件下没有任何读数（注意：这是'查不到'，不是'设备没数据'）",
                    device_id=did, since=since_iso, state="empty")
    axs = [r["ax"] for r in rows if r["ax"] is not None]
    stats = {"count": len(rows),
             "newest_ts_server": rows[0]["ts_server"],
             "oldest_ts_server": rows[-1]["ts_server"]}
    if axs:
        stats.update({"ax_min": min(axs), "ax_max": max(axs),
                      "ax_avg": round(sum(axs) / len(axs), 4)})
    return _ok("GET /api/history", {"records": rows, "stats": stats},
               time=rows[0]["ts_server"], state="ok", device_id=did,
               limit_applied=n, since=since_iso,
               assumed_device=chk.get("assumed", False))


def t_query_frames(conn, device_id=None, limit=None, since=None, **_):
    try:
        n = _check_limit(limit)
        since_iso = _parse_since(since)
    except ValueError as e:
        return _err("GET /api/frames", str(e))
    chk = _check_device(conn, device_id)
    if not chk["ok"]:
        return _err("GET /api/frames", chk["error"],
                    needs_clarification=chk.get("needs_clarification", False),
                    candidates=chk.get("candidates", []), hint=chk.get("hint"))
    did = chk["device_id"]
    # ★ 加了新列就要同步到这里 —— 否则模型看不到"这帧是补传的"，
    #   会把一张十几分钟前断网时拍的图说成"刚刚拍的"。见 §1.1 那张清单。
    sql = ("SELECT id, device_id, ts_server, capture_ts, filename, size_bytes, "
           "width, height, source, request_id, boot_id, seq, sha256, purged_at, "
           "buffered_us, backlog_dropped "
           "FROM frames WHERE device_id=?")
    params = [did]
    if since_iso:
        sql += " AND ts_server >= ?"
        params.append(since_iso)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(n)
    rows = _run_readonly(conn, sql, tuple(params), max_rows=n)
    if not rows:
        return _err("GET /api/frames", "该条件下没有任何一帧图像",
                    device_id=did, since=since_iso, state="empty")
    for r in rows:
        # purged_at 有值 = 原图已被配额清理，但哈希还在 —— 元数据不随原图消失
        r["image_available"] = not r["purged_at"]
        # ★ 补传帧：采集时刻在**过去**。这个标记必须跟着数据走，
        #   否则模型会把断网期间攒下的图说成"刚刚拍的"。
        r["is_backlog"] = (r.get("source") == "backlog")
        r["buffered_s"] = (round(r["buffered_us"] / 1e6, 1)
                           if r.get("buffered_us") is not None else None)
    return _ok("GET /api/frames", {"frames": rows, "count": len(rows)},
               time=rows[0]["ts_server"], state="ok", device_id=did,
               limit_applied=n, since=since_iso,
               backlog_note=("is_backlog=true 的帧是**断网期间采集、恢复后补传**的："
                             "capture_ts 在过去，ts_server 才是服务端收到它的时刻。"
                             "回答时必须说清这一点，不能把它当成'刚拍的'。"),
               assumed_device=chk.get("assumed", False))


def t_get_command_status(conn, request_id=None, device_id=None, limit=None, **_):
    """查一次采集请求的状态 —— 这是判断"到底拍没拍成"的唯一依据。"""
    if request_id:
        rows = _run_readonly(
            conn,
            "SELECT request_id, device_id, state, created_at, dispatched_at, "
            "ack_at, frame_id, frame_name, evidence_ok, fail_reason, updated_at "
            "FROM commands WHERE request_id=? LIMIT 1",
            (str(request_id).strip(),), max_rows=1)
    else:
        chk = _check_device(conn, device_id)
        if not chk["ok"]:
            return _err("GET /api/command/status", chk["error"],
                        needs_clarification=chk.get("needs_clarification", False),
                        candidates=chk.get("candidates", []), hint=chk.get("hint"))
        try:
            n = _check_limit(limit)
        except ValueError as e:
            return _err("GET /api/command/status", str(e))
        rows = _run_readonly(
            conn,
            "SELECT request_id, device_id, state, created_at, dispatched_at, "
            "ack_at, frame_id, frame_name, evidence_ok, fail_reason, updated_at "
            "FROM commands WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (chk["device_id"], n), max_rows=n)
    if not rows:
        return _err("GET /api/command/status", "没有匹配的采集请求",
                    request_id=request_id, state="empty")
    for r in rows:
        r["has_frame"] = bool(r["frame_name"]) and r["state"] == "COMPLETED"
    out = rows[0] if request_id else {"commands": rows, "count": len(rows)}
    return _ok("GET /api/command/status", out,
               time=(rows[0]["updated_at"] or rows[0]["created_at"]),
               state=rows[0]["state"], request_id=rows[0]["request_id"])


def t_list_help_events(conn, device_id=None, limit=None, **_):
    """查教学求助事件 —— 三层状态分别返回，绝不合并。"""
    try:
        n = _check_limit(limit)
    except ValueError as e:
        return _err("GET /api/help", str(e))
    sql = ("SELECT event_id, device_id, device_state, server_state, viewer_state, "
           "pressed_at, received_at, answered_at, answered_by, answer_text, "
           "cancelled_by, boot_id, seq FROM help_events")
    params = []
    if device_id:
        chk = _check_device(conn, device_id)
        if not chk["ok"]:
            return _err("GET /api/help", chk["error"], candidates=chk.get("candidates", []))
        sql += " WHERE device_id=?"
        params.append(chk["device_id"])
    sql += " ORDER BY received_at DESC LIMIT ?"
    params.append(n)
    rows = _run_readonly(conn, sql, tuple(params), max_rows=n)
    return _ok("GET /api/help", {"helps": rows, "count": len(rows)},
               time=(rows[0]["received_at"] if rows else None), state="ok",
               note="device_state=板子说的；server_state=服务端说的；viewer_state=人说的，三者不可互推")


def _wave_axis_source(rows) -> tuple:
    """这批波形的时间轴该走哪一档？返回 (source, note)。

    ★ 判据与 server.py 的 `_wave_batches_contiguous()` **必须一致**，
      那边有详细的推理过程，这里只说结论：
        ① device   板端采样时刻 t_last 可解析且互不相同
                   —— 板子的钟可能没对时，但**钟差是个常数**，做差后自动抵消
        ② derived  批号逐批 +1 + 同一次开机 + 采样率一致 + **批间没丢样本**
                   → 时间轴完全由 (n_samples, hz) 反推，一个时钟都不用
        ③ server   兜底：用服务端接收时刻（含网络抖动）

    ★ 三档都**不违反**"服务端不采信板端时间"那条铁律：
      判定（入库、证据、超时归因）一律仍用 received_at；
      这里只决定**相对间隔**怎么摆，且用了哪一档会一起交给模型。
    """
    if not rows:
        return "none", "没有数据"
    dev_times = [_parse_iso_safe(r["t_last"]) for r in rows]
    if all(d is not None for d in dev_times) and len(set(dev_times)) > 1:
        return "device", ("用板端采样时刻摆相对间隔 —— 钟差是常数，做差后自动抵消"
                          "（判定仍用服务端 received_at）")

    hz = rows[-1]["hz"]
    # ★ 降级原因必须写准 —— "批号断档"和"只有一批"是两回事，
    #   笼统写一句"时间轴不可靠"会让人去查一个根本不存在的丢批问题。
    if len(rows) < 2:
        return "server", "只有一批数据，谈不上批间连续，退回服务端接收时刻"
    if any(r["dropped"] for r in rows):
        return "server", ("板端如实上报了丢样本（批间有洞），累加不成立，"
                          "退回服务端接收时刻 —— 网络抖动会让间隔看起来不匀")
    if len({r["boot_id"] for r in rows}) != 1:
        return "server", ("中间重启过（boot_id 变了），批号会归零，不能当连续，"
                          "退回服务端接收时刻")
    if len({r["hz"] for r in rows}) != 1 or not hz or hz < 1:
        return "server", "采样率缺失或各批不一致，无法按采样率反推，退回服务端接收时刻"
    for i, r in enumerate(rows):
        if r["batch_seq"] is None:
            return "server", "板端没报批号，无法判断批间是否连续，退回服务端接收时刻"
        if i > 0 and r["batch_seq"] != rows[i - 1]["batch_seq"] + 1:
            return "server", ("批号有断档（中间有批没送达），退回服务端接收时刻 —— "
                              "网络抖动会让间隔看起来不匀")
    return "derived", ("批号连续且批间无丢样本，按采样率反推时间轴"
                       "（比服务端接收时刻准，不受网络抖动影响）")


def t_get_attitude(conn, device_id=None, **_):
    """取某台设备**最新姿态** —— 口径与 `GET /api/attitude` 完全一致。

    ★ 这个工具存在的意义，一半是给数据，一半是给**边界**：
      返回值里恒定带 `yaw: null` 和 `yaw_note`。
      模型手里有了这两个字段，才可能说出"航向不可测"；
      没有的话，它面对"板子朝哪边"就只剩留空和编造两条路。
    """
    chk = _check_device(conn, device_id)
    if not chk["ok"]:
        return _err("GET /api/attitude", chk["error"],
                    needs_clarification=chk.get("needs_clarification", False),
                    candidates=chk.get("candidates", []), hint=chk.get("hint"))
    did = chk["device_id"]
    rows = _run_readonly(
        conn,
        "SELECT id, device_id, boot_id, batch_seq, hz, n_samples, received_at, "
        "t_first, t_last, ax, ay, az, acc_mag, pitch, roll, posture, posture_note "
        "FROM wave_batches WHERE device_id=? ORDER BY id DESC LIMIT 1",
        (did,), max_rows=1)
    if not rows:
        # ★ 这里有两种可能，必须都摆出来 —— 断言成"从没上传过"会把人带偏：
        #   波形是按批滚动清理的，有可能设备传过、但老批（含最新那批）已被清掉。
        return _err("GET /api/attitude",
                    "该设备当前没有波形数据，因此算不出姿态。"
                    "可能是①设备从未上传过波形（板端 WAVE_ENABLE 没开），"
                    "也可能是②波形已被滚动清理。这两种情况的处理方向不同，"
                    "不要断言成'设备从来没上报过'。",
                    device_id=did, state="empty",
                    wave_retention=WAVE_RETENTION_NOTE)
    rec = rows[0]
    data = dict(rec)
    data["posture_label"] = POSTURE_LABEL.get(rec.get("posture"), "未知")
    # ★ 恒为 None。不是"没采到"，是**原理上测不到** —— 这个区别必须写在数据里。
    data["yaw"] = None
    data["yaw_note"] = YAW_NOTE
    return _ok("GET /api/attitude", data, time=rec["received_at"], state="ok",
               device_id=did,
               yaw=None, yaw_note=YAW_NOTE, basis=ATTITUDE_BASIS,
               time_semantics={
                   "received_at": "服务端入库时刻（判定用）",
                   "t_last": "板端采样时刻（板子自己报的，只作参考，服务端不采信）"},
               assumed_device=chk.get("assumed", False))


def t_query_waveform(conn, device_id=None, limit=None, since=None, **_):
    """取最近 N 批波形的**摘要** —— 刻意**不吐原始样本**。

    为什么不像 query_readings 那样把样本给出去：
      · 一批 100 点 × 3 轴 = 300 个数，几批就上千，塞进上下文纯属浪费；
      · 模型对原始样本做不了任何有用的事 —— 它不会去算 FFT，
        反而容易"看着数字编趋势"。要趋势就该用摘要里的统计量。
      · 结论：**能吐多少数据由我们定，不由模型定** —— 和第 4 周的 MAX_ROWS 同一个原则。
    """
    try:
        n = _check_limit(limit)
        since_iso = _parse_since(since)
    except ValueError as e:
        return _err("GET /api/waveform", str(e))
    chk = _check_device(conn, device_id)
    if not chk["ok"]:
        return _err("GET /api/waveform", chk["error"],
                    needs_clarification=chk.get("needs_clarification", False),
                    candidates=chk.get("candidates", []), hint=chk.get("hint"))
    did = chk["device_id"]
    sql = ("SELECT id, device_id, boot_id, batch_seq, hz, n_samples, dropped, "
           "received_at, t_first, t_last, ax, ay, az, acc_mag, pitch, roll, "
           "posture, posture_note FROM wave_batches WHERE device_id=?")
    params = [did]
    if since_iso:
        sql += " AND received_at >= ?"
        params.append(since_iso)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(n)
    rows = _run_readonly(conn, sql, tuple(params), max_rows=n)
    if not rows:
        return _err("GET /api/waveform",
                    "该条件下没有波形批次。注意这与'设备不存在'是两回事；"
                    "波形按最近 N 批滚动清理，也可能只是被清理掉了。",
                    device_id=did, since=since_iso, state="empty",
                    wave_retention=WAVE_RETENTION_NOTE)

    rows = list(reversed(rows))          # 老的在前，与网页画波形的顺序一致
    source, note = _wave_axis_source(rows)
    postures = [r["posture"] for r in rows]
    dropped_total = sum(int(r["dropped"] or 0) for r in rows)
    latest = rows[-1]
    detail = [{
        "id": r["id"], "batch_seq": r["batch_seq"], "n_samples": r["n_samples"],
        "hz": r["hz"], "dropped": int(r["dropped"] or 0),
        "received_at": r["received_at"],
        "t_first": r["t_first"], "t_last": r["t_last"],
        "ax": r["ax"], "ay": r["ay"], "az": r["az"],
        "acc_mag": r["acc_mag"], "pitch": r["pitch"], "roll": r["roll"],
        "posture": r["posture"],
        "posture_label": POSTURE_LABEL.get(r["posture"], "未知"),
        "posture_note": r["posture_note"],
    } for r in rows]
    data = {
        "batches": detail,
        "summary": {
            "batch_count": len(rows),
            "sample_count": sum(r["n_samples"] for r in rows),
            "hz": latest["hz"],
            "posture_latest": latest["posture"],
            "posture_latest_label": POSTURE_LABEL.get(latest["posture"], "未知"),
            "posture_changed_in_window": len(set(postures)) > 1,
            "dropped_total": dropped_total,
            "acc_mag_latest": latest["acc_mag"],
        },
        "time_axis_source": source,
        "time_axis_note": note,
        "samples_omitted": True,
        "samples_omitted_reason": "原始样本不交给模型：几批就上千个数，"
                                  "模型用它算不出东西，反而容易编趋势。"
                                  "要看波形请用网页上的示波器面板。",
    }
    return _ok("GET /api/waveform", data, time=latest["received_at"], state="ok",
               device_id=did, limit_applied=n, since=since_iso,
               yaw=None, yaw_note=YAW_NOTE, basis=ATTITUDE_BASIS,
               dropped_note=("dropped_total 是板端**如实上报**的丢样本数（环形缓冲溢出所致）。"
                             "非 0 时批间有洞，时间轴不能按采样率反推 —— "
                             "这条比'一个点都没丢'和'丢了一些'混着说要紧得多。"),
               wave_retention=WAVE_RETENTION_NOTE,
               assumed_device=chk.get("assumed", False))


def t_request_capture(conn, device_id=None, reason=None, **_):
    """请求开发板**现在**拍一张 —— 唯一的"写"工具，而且它只下指令、不写数据。

    ★ 本工具绝不返回"已采集成功"。
    下完指令立刻回读真实状态：此刻一定是 PENDING（设备还没来取），
    于是 success_claim_allowed=False，模型只能如实说"指令已下发、设备尚未回传"。
    真正的完成证据（state=COMPLETED + 有帧 + 三条证据通过）只能靠
    t_get_command_status 后续查出来 —— 也就是说，**"拍好了没"必须二次确认**。
    """
    chk = _check_device(conn, device_id)
    if not chk["ok"]:
        return _err("POST /api/command", chk["error"],
                    needs_clarification=chk.get("needs_clarification", False),
                    candidates=chk.get("candidates", []), hint=chk.get("hint"),
                    note="设备不存在就不下发指令 —— 给不存在的设备下指令等于制造假记录")
    did = chk["device_id"]

    # 复用与 /api/command 完全一致的编号规则与初始状态
    now = datetime.now(TZ).isoformat(timespec="milliseconds")
    request_id = "req-%s-%04x" % (datetime.now(TZ).strftime("%Y%m%d-%H%M%S"),
                                  int.from_bytes(os.urandom(2), "big"))
    ttl = 120
    try:
        conn.execute(
            "INSERT INTO commands(request_id, device_id, action, sensor, state, "
            "ttl_s, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (request_id, did, "capture", "camera", "PENDING", ttl, now, now))
        conn.commit()
    except sqlite3.Error as e:
        return _err("POST /api/command", "指令落库失败：%s" % e, device_id=did)

    row = conn.execute(
        "SELECT request_id, device_id, state, created_at, dispatched_at, "
        "frame_name, evidence_ok FROM commands WHERE request_id=?",
        (request_id,)).fetchone()
    state = row["state"]
    return _ok("POST /api/command",
               {"request_id": row["request_id"], "device_id": row["device_id"],
                "state": state, "created_at": row["created_at"],
                "dispatched_at": row["dispatched_at"]},
               time=row["created_at"], state=state,
               success_claim_allowed=(state == "COMPLETED" and bool(row["frame_name"])),
               must_verify_with="t_get_command_status(request_id=...)",
               note="指令已下发。此刻没有采集完成的证据 —— "
                    "设备还没来取（PENDING）。要判断拍没拍成，必须再用 "
                    "t_get_command_status 查一次，看 state 是否变成 COMPLETED。",
               reason=reason, assumed_device=chk.get("assumed", False))


TOOLS = {
    "list_devices":        t_list_devices,
    "get_latest_reading":  t_get_latest_reading,
    "query_readings":      t_query_readings,
    "query_frames":        t_query_frames,
    "get_command_status":  t_get_command_status,
    "list_help_events":    t_list_help_events,
    # ↓ 第 5 周数据（姿态/波形）—— 全是只读，不新增任何"写"能力
    "get_attitude":        t_get_attitude,
    "query_waveform":      t_query_waveform,
    "request_capture":     t_request_capture,
}

# 允许写库的工具（只有这一个，且只写 commands 表）
WRITE_TOOLS = ("request_capture",)

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "list_devices",
        "description": "列出所有上报过的设备及其最后上报时间、数据条数。"
                       "当用户没有指明设备、而你需要先知道有哪些设备时用这个。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_latest_reading",
        "description": "读取某台设备**最近一条**加速度读数（只读已有记录，不触发新采集）。",
        "parameters": {"type": "object", "properties": {
            "device_id": {"type": "string", "description": "设备编号；不确定时先调 list_devices"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "query_readings",
        "description": "读取某台设备**已有的**多条历史读数（只读，不触发新采集）。",
        "parameters": {"type": "object", "properties": {
            "device_id": {"type": "string"},
            "limit": {"type": "integer", "description": "返回条数，上限 200"},
            "since": {"type": "string", "description": "时间范围，如 10m / 2h / 3d，或 ISO 时间"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "query_frames",
        "description": "读取某台设备**已有的**图像帧元数据（含 SHA-256、拍摄时刻、原图是否已清理）。"
                       "只读，不触发新拍摄。"
                       "★ 注意 is_backlog=true 的帧是**断网期间采集、恢复后补传**的，"
                       "它的 capture_ts 在过去 —— 不能当成'刚拍的'。",
        "parameters": {"type": "object", "properties": {
            "device_id": {"type": "string"},
            "limit": {"type": "integer"},
            "since": {"type": "string"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_command_status",
        "description": "查询一次采集请求的状态（PENDING/RECEIVED/EXECUTING/UPLOADED/COMPLETED/"
                       "EXPIRED/TIMEOUT/FAILED）。"
                       "★ 判断'拍好了没'**只能**用这个工具，看 state 是否 COMPLETED。",
        "parameters": {"type": "object", "properties": {
            "request_id": {"type": "string", "description": "request_capture 返回的编号"},
            "device_id": {"type": "string", "description": "不传 request_id 时按设备查最近几条"},
            "limit": {"type": "integer"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "list_help_events",
        "description": "查询教学求助事件。返回三层状态：device_state(板子说的)、"
                       "server_state(服务端说的)、viewer_state(人说的)，三者不可互相推断。",
        "parameters": {"type": "object", "properties": {
            "device_id": {"type": "string"},
            "limit": {"type": "integer"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_attitude",
        "description": "读取某台设备**最新姿态**：posture（flat/upright/side_edge/tilted）、"
                       "posture_label、acc_mag（合加速度）、pitch、roll。只读。"
                       "★ 返回值里 yaw 恒为 null 且带 yaw_note —— "
                       "本板无陀螺仪/磁力计，**航向在原理上不可测**，"
                       "被问到朝向时必须如实说'不可测'并给原因，"
                       "既不许留空装作没看见，也不许报 0° 或任何具体角度。",
        "parameters": {"type": "object", "properties": {
            "device_id": {"type": "string", "description": "设备编号；不确定时先调 list_devices"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "query_waveform",
        "description": "读取某台设备最近几批**波形摘要**（批号、样本数、采样率、"
                       "丢样本数 dropped、每批的姿态/合加速度/俯仰/横滚、"
                       "以及时间轴用的是哪一档）。只读。"
                       "★ 刻意**不返回原始样本** —— 要看波形本身请用网页示波器面板。",
        "parameters": {"type": "object", "properties": {
            "device_id": {"type": "string"},
            "limit": {"type": "integer", "description": "取最近几批，上限 200"},
            "since": {"type": "string", "description": "时间范围，如 10m / 2h / 3d"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "request_capture",
        "description": "请求开发板**现在**拍一张并回传（这是唯一会改变系统状态的工具）。"
                       "调用后只会得到 PENDING —— 设备尚未回传，"
                       "**此时绝不能对用户说'已采集成功'**；"
                       "必须再用 get_command_status 查 request_id 才能确认结果。",
        "parameters": {"type": "object", "properties": {
            "device_id": {"type": "string"},
            "reason": {"type": "string", "description": "为什么发起这次采集（用于留痕）"}},
            "required": ["device_id"]}}},
]


# ============================================================
# 三、执行工具（带异常兜底 + 调用留痕）
# ============================================================

def execute_tool(name, args, conn) -> dict:
    """执行一个工具调用，返回结构化结果。

    任何异常都转成 ok=False 的结构化结果返回给模型 ——
    工具炸了不能让整轮对话炸掉，也不能让模型以为"调用成功了但没数据"。
    """
    if name not in TOOLS:
        return _err("(none)", "没有这个工具：%r。可用工具：%s"
                    % (name, ", ".join(sorted(TOOLS))),
                    allowed_tools=sorted(TOOLS))
    if not isinstance(args, dict):
        return _err(name, "参数必须是 JSON 对象，收到 %r" % (args,))
    try:
        return TOOLS[name](conn, **args)
    except PermissionError as e:
        return _err(name, "被只读守卫拦下：%s" % e)
    except ValueError as e:
        return _err(name, "参数不合法：%s" % e)
    except TypeError as e:
        return _err(name, "参数不匹配：%s" % e)
    except sqlite3.Error as e:
        return _err(name, "数据库错误：%s" % e)


def _open_conn():
    # timeout 给足：这个连接可能和 Web 服务端并发写（下采集指令），
    # 撞上锁的时候宁可等一下，也不要直接抛 "database is locked"。
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# 四、系统提示词 —— 把"不许编"写成可执行规则
# ============================================================

SYSTEM_PROMPT = """你是一台开发板数据服务的**运行时问答助手**。用户用自然语言问你问题，
你必须通过调用工具去查真实数据来回答。你没有别的事实来源。

【铁律一：只读与请求，必须分清】
  · 用户问"现在/刚才/最近的数值是多少""有哪些数据""拍过哪些照片""板子什么姿态/怎么摆的"
    —— 这是**读取已有记录**，调 get_latest_reading / query_readings / query_frames /
    list_help_events / list_devices / get_attitude / query_waveform。
    读记录**不会**产生新采集。
  · 用户说"让板子现在拍一张""重新采集一次""再测一下" —— 这才是**请求一次新采集**，
    调 request_capture。不要用读取类工具去假装完成了采集。

【铁律二：没有证据就不许说成功】（最重要）
  · request_capture 调用后只会得到 state=PENDING —— 这只表示"指令已下发，设备还没来取"。
  · **在拿到 state=COMPLETED 之前，绝对不许说"已采集成功""拍好了""采集完成"。**
    只能说："指令已下发，设备尚未回传，目前没有采集完成的证据"，并给出 request_id 和当前 state。
  · 要确认结果，必须再调 get_command_status(request_id=...)，看 state 是否 COMPLETED
    且 has_frame 为 true。state 是别的值就如实说那个值，不要美化。
  · 即使设备长时间没反应，也只能说"尚未回传"，不许推测"应该拍到了"。

【铁律三：必须报来源、时间与状态】
  每个工具结果里都带 source（数据来自哪个接口）、time（数据时刻）、state（状态）。
  回答时要把这三样说出来，例如"来自 /api/latest，数据时刻 2026-09-21T16:00:01，状态正常"。
  工具没给你的数字，一个都不许编。

【铁律四：歧义要反问，不许猜】
  · 用户没说哪台设备、而库里不止一台 → 调 list_devices 拿到名单，然后**反问用户要哪一台**。
  · 时间范围不清（"最近"没说多久）→ 用默认条数查，并在回答里说明你按什么口径查的。
  · 指标不明（"数据怎么样"）→ 反问要哪个指标。
  · 宁可不回答，也不要挑一个然后让用户以为那就是他要的。

【铁律五：三种状态不可互相推断】
  求助事件有三层状态：device_state 是**板子**说的，server_state 是**服务端**说的，
  viewer_state 是**人**说的。三者分别独立，绝不能由一层推断另一层。
  "服务端没收到"不等于"板子没发出来"；"没人回应"也不等于"人拒绝了"。

【铁律六：姿态的能测与不能测，必须分开说】
  · 姿态是**由重力方向**判出来的（加速度计静止时测的是支撑力，读数指向天空），
    所以凡由倾斜决定的状态都测得到：平放 / 竖直 / 侧立 / 自由倾斜，
    以及合加速度 |a|（静止时 ≈ 1.000 g）、俯仰 pitch、横滚 roll。
  · ★ **航向（yaw / 朝向 / 东南西北）在原理上不可测** —— 本板没有陀螺仪和磁力计，
    绕重力轴自转不改变重力方向，所以这个自由度从数据里根本不存在。
    工具返回的 yaw 恒为 null，并带 yaw_note。
    被问到航向时，必须**明确说"不可测"并给出原因**：
      ✗ 不许留空、装没看见；
      ✗ 不许报 0°、正北、或任何具体角度；
      ✗ 不许用 pitch/roll 去凑一个"方向"糊弄过去；
      ✓ 正确说法："俯仰 x°、横滚 y° 可测；航向不可测 —— 本板无陀螺仪/磁力计。"
  · 姿态是**某一批数据的属性**，不是"设备永久的状态"：它随时会随摆放变化。
    回答时要说清这是**哪一批/哪个时刻**的姿态，别把旧姿态说成"现在"。
  · 波形属**过程数据**：服务端按最近 N 批滚动清理。
    "查不到老波形"不等于"设备没上报过"—— 别把清理说成没数据。

【铁律七：补传的帧不等于"刚拍的"】
  · `is_backlog=true` 的帧，是设备**断网期间采集、恢复联网后补传**上来的。
    它的 `capture_ts`（采集时刻）在**过去**，可能比 `ts_server`（服务端收到时刻）
    早很多；`buffered_s` 就是它在板端 Flash 队列里待了多久。
  · 回答时必须说清"这是补传的，采集于 X，服务端 Y 时刻才收到"，
    **不许**把它当成"刚拍的"来汇报。
  · 补传帧**永远不会**是某次远程采集请求的结果（服务端在入口就拒掉了
    "补传 + request_id"的组合）—— 所以看到 is_backlog 就不要去关联任何 request_id。
  · `backlog_dropped` 大于 0 表示：存这一帧的时候，队列已经因为满了而丢掉了
    更老的若干帧。这是"断网太久、Flash 装不下"的如实记录，要如实说出来，
    不要含糊成"数据完整"。

【表达要求】
  用中文回答，简短、直给。先说结论，再给来源/时间/状态。
  用户没问的东西不要长篇解释。"""


# ============================================================
# 五、模型调用
# ============================================================

def _http_json(url, payload, timeout=OLLAMA_TIMEOUT):
    """POST JSON。绕开本机 HTTP 代理 —— 代理会把 127.0.0.1 也劫走。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ollama_chat(messages, model=OLLAMA_MODEL, base_url=OLLAMA_URL,
                 think=False, timeout=OLLAMA_TIMEOUT):
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SPECS,
        "stream": False,
        "options": {"temperature": 0},   # 要的是"照着工具结果说"，不是发挥
    }
    if think is not None:
        payload["think"] = think
    try:
        res = _http_json(base_url + "/api/chat", payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if "think" in payload:
            # 该版本不认 think 参数就退回默认，别因为这个把整轮对话搞挂
            payload.pop("think")
            res = _http_json(base_url + "/api/chat", payload, timeout=timeout)
        else:
            raise RuntimeError("模型服务返回 %s：%s" % (e.code, body))
    return res.get("message") or {}


def model_available(base_url=OLLAMA_URL, model=OLLAMA_MODEL) -> dict:
    """探测运行时语言服务是否可用（用于页面与自测给出人话提示）。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base_url + "/api/tags", timeout=5) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name") for m in tags.get("models", [])]
        return {"ok": model in names, "models": names, "want": model,
                "base_url": base_url}
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "base_url": base_url, "want": model, "models": []}


# ============================================================
# 六、结果守卫 —— 模型硬说成功时，在最终文案上再拦一道
# ============================================================

# 这些词一出现，就意味着模型在声称"事情做完了"
_SUCCESS_WORDS = ("已采集成功", "采集成功", "已成功采集", "拍好了", "已经拍好",
                  "已完成采集", "采集完成", "采集已完成", "拍照完成", "拍摄完成",
                  "拍摄已完成", "已经拍完")

# 句子里出现这些标记，说明它不是在"陈述已完成"，而是在反问 / 否定 / 讲条件 / 复述用户的话。
# 只做子串匹配会误伤 —— 实测踩过：用户问"告诉我拍好了没"，
# 模型把这句复述回来（"...查结果告诉你拍好了没"），子串里就出现了"拍好了"，
# 于是一句反问被误判成"假成功"。所以判定必须**按句子看语气**，不能只看有没有那几个字。
_NEG_HINTS = ("不", "没有", "没", "未", "尚未", "无法", "不能",
              "是否", "吗", "告诉", "确认", "如果", "假设", "待")


def _claims_success(text):
    """判断文案里有没有"声称已经采集完成"。返回 (是否声称, 命中的句子)。"""
    for sent in re.split(r"[。！\n]", text or ""):
        s = sent.strip()
        if not s or "？" in s or "?" in s:
            continue                                   # 疑问句不算声称
        hit = next((w for w in _SUCCESS_WORDS if w in s), None)
        if not hit:
            continue
        tail = s.split(hit, 1)[1][:3]
        if tail.startswith(("没", "吗", "否")):
            continue                                   # "拍好了没" 这种疑问式引用
        if any(n in s for n in _NEG_HINTS):
            continue                                   # 整句是否定/条件/待办语气
        return True, s
    return False, ""


# ---- 第 5 周：航向守卫 ----
#
# ★ 为什么"航向"要单独守一道，而不能只靠提示词：
#   航向是**原理上测不到**的自由度（本板无陀螺仪/磁力计），不是"这次没采到"。
#   模型面对"板子朝哪边"，最常见的两种糊弄在**数字上都不假**，却会让用户
#   以为"方向是测得到的"：
#     · 报 0° —— 看着最"中性"，可 0° 就是正北，等于凭空造了个方向；
#     · 拿 pitch/roll 的数字顶上 —— 那两个角是相对重力的倾角，跟朝哪儿无关。
#   所以这道守卫要拦的不是"说错数"，而是"把一个不可测的量说成测到了"。
_YAW_WORDS = ("航向", "朝向", "yaw", "指北", "罗盘", "方位", "朝哪", "哪个方向")
_YAW_DIRWORDS = ("正北", "正南", "正东", "正西", "朝北", "朝南", "朝东", "朝西",
                 "向北", "向南", "向东", "向西", "北偏", "南偏",
                 "东北方向", "西北方向", "东南方向", "西南方向")
_YAW_DENY = ("不可测", "测不到", "测不出", "无法测", "不能测", "没法测",
             "没有陀螺仪", "无陀螺仪", "没有磁力计", "无磁力计",
             "无法确定", "不可知", "不适用", "未知", "null", "none")
# 角度写法：`12°` / `12度` / `12 deg` / `-1.2°`
_ANGLE_RE = re.compile(r"-?\d+(?:\.\d+)?\s*(?:°|度|deg\b)")


def _asked_yaw(question: str) -> bool:
    q = (question or "").lower()
    return any(w in q for w in _YAW_WORDS) or any(w in q for w in _YAW_DIRWORDS)


def _yaw_violation(text, question):
    """有没有把"航向"当成一个能报出数的量。返回 (是否越界, 命中的句子)。"""
    def denied(s):
        low = s.lower()
        return any(d in low for d in _YAW_DENY)

    for sent in re.split(r"[。！？\n]", text or ""):
        s = sent.strip()
        if not s or denied(s):
            continue                        # "航向不可测" 这种正确说法，放过
        has_yaw = any(w in s.lower() for w in _YAW_WORDS)
        has_dir = bool(_ANGLE_RE.search(s)) or any(w in s for w in _YAW_DIRWORDS)
        if has_yaw and has_dir:
            return True, s

    # 用户问的**就是**航向，而整段回答里既没声明"不可测"、又在报角度或方向
    # （典型：拿俯仰/横滚的数字顶上，让人以为那就是朝向）
    if _asked_yaw(question) and not denied(text or ""):
        for sent in re.split(r"[。！？\n]", text or ""):
            s = sent.strip()
            if _ANGLE_RE.search(s) or any(w in s for w in _YAW_DIRWORDS):
                return True, s
    return False, ""


def _has_completion_evidence(trace) -> bool:
    """整轮对话里，有没有出现"采集确实完成"的硬证据。"""
    for t in trace:
        res = t.get("result") or {}
        if res.get("success_claim_allowed") is True:
            return True
        data = res.get("data") or {}
        if isinstance(data, dict):
            if data.get("state") == "COMPLETED" and data.get("has_frame"):
                return True
            if data.get("has_frame") is True and res.get("state") == "COMPLETED":
                return True
    return False


def _asked_for_capture(question: str) -> bool:
    return any(w in (question or "") for w in
               ("拍", "采集", "拍照", "拍一张", "抓拍", "重新测", "再测"))


def guard_answer(answer: str, trace, question: str) -> dict:
    """检查模型最终文案有没有越界。返回 {'text':..., 'guardrails':[...]}。

    这是**最后一道**闸。系统提示词是"要求"，这里是"强制" ——
    模型没照着要求做的时候，我们不能把它的原话直接交给用户。
    """
    flags = []
    text = (answer or "").strip()

    # ① 没有完成证据却说成功
    claims, hit_sent = _claims_success(text)
    if claims and not _has_completion_evidence(trace):
        states = ["%s=%s" % (t.get("tool"), t.get("state"))
                  for t in trace if t.get("state")]
        flags.append("success_without_evidence")
        text = ("【更正】本次**没有**采集完成的证据，不能说「已采集成功」。\n"
                "各次工具调用拿到的状态：%s\n"
                "准确说法是：指令已下发，设备尚未回传观测，"
                "需要再查一次状态才能确认结果。\n\n"
                "（模型原话里这句话被拦下了：%s）"
                % ("、".join(states) if states else "无任何状态记录", hit_sent))

    # ② 一个工具都没调，却在报数字 —— 那这些数字从哪来
    # （只在"答案里确实出现了数据"时才标记，纯寒暄不该被误伤）
    if not trace and re.search(r"\d", text):
        flags.append("no_tool_used")
        text = ("【提醒】这次回答没有调用任何工具，因此其中的数字**没有数据来源**，"
                "不能作为依据。\n\n" + text)

    # ③ 工具已经明确要求反问，模型却没反问
    need = [t for t in trace
            if (t.get("result") or {}).get("needs_clarification")]
    if need and not any(w in text for w in ("哪一台", "哪台", "请问", "哪一台设备",
                                            "要查哪", "具体是")):
        flags.append("clarification_skipped")
        cands = []
        for t in need:
            cands += (t.get("result") or {}).get("candidates") or []
        text += ("\n\n（系统提醒：设备不唯一，需要你确认要查哪一台。"
                 "候选：%s）" % ("、".join(sorted(set(cands))) or "无"))

    # ④ 把一个**原理上不可测**的量（航向）说成测到了
    bad_yaw, yaw_sent = _yaw_violation(text, question)
    if bad_yaw:
        flags.append("yaw_fabricated")
        text = ("【更正】航向不可测，不能给出具体方向或角度。\n"
                "本板无陀螺仪/磁力计 —— 绕重力轴自转不改变重力方向，"
                "这个自由度从数据里根本不存在，不是这次没采到。\n"
                "能测的只有俯仰、横滚和合加速度；用它们回答时要**明说**航向不可测。\n\n"
                "（模型原话里这句话被拦下了：%s）" % yaw_sent) + "\n\n" + text

    return {"text": text, "guardrails": flags}


# ============================================================
# 七、主流程
# ============================================================

def ask(question, model=OLLAMA_MODEL, base_url=OLLAMA_URL, conn=None,
        max_turns=MAX_TURNS, think=False, verbose=False) -> dict:
    """把一句自然语言变成工具调用，再把工具结果变成带证据的回答。"""
    own_conn = conn is None
    if own_conn:
        conn = _open_conn()
    trace = []
    answer = ""
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": question}]
        for turn in range(max_turns):
            msg = _ollama_chat(messages, model=model, base_url=base_url,
                               think=think)
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = (msg.get("content") or "").strip()
                if not answer and msg.get("thinking"):
                    # 有的模型把正文全放 thinking 里了；别把空字符串当答案交出去
                    answer = (msg.get("thinking") or "").strip()
                break
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": calls})
            for c in calls:
                fn = (c or {}).get("function") or {}
                name = fn.get("name")
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                args = args if isinstance(args, dict) else {}
                res = execute_tool(name, args, conn)
                trace.append({"turn": turn + 1, "tool": name, "args": args,
                              "ok": res.get("ok"), "source": res.get("source"),
                              "time": res.get("time"), "state": res.get("state"),
                              "result": res})
                if verbose:
                    print("  → 调用 %s(%s) => ok=%s state=%s"
                          % (name, json.dumps(args, ensure_ascii=False),
                             res.get("ok"), res.get("state")), flush=True)
                messages.append({
                    "role": "tool", "tool_name": name,
                    "content": json.dumps(res, ensure_ascii=False, default=str)})
        else:
            answer = "（达到最大工具调用轮数 %d，已停止，未能给出结论）" % max_turns
    finally:
        if own_conn:
            conn.close()

    guarded = guard_answer(answer, trace, question)
    return {
        "question": question,
        "answer": guarded["text"],
        "raw_answer": answer,
        "guardrails": guarded["guardrails"],
        "tool_calls": [{"tool": t["tool"], "args": t["args"], "ok": t["ok"],
                        "source": t["source"], "time": t["time"],
                        "state": t["state"]} for t in trace],
        "completion_evidence": _has_completion_evidence(trace),
        "model": model,
        "runtime_model": True,
        "note": "本回答由**产品运行时模型**（Ollama）生成，"
                "与写代码的开发助手是两个角色。",
    }


def _main() -> int:
    ap = argparse.ArgumentParser(description="第4周 · 自然语言查询与请求采集")
    ap.add_argument("question", nargs="*", help="用自然语言提问")
    ap.add_argument("--json", action="store_true", help="输出完整结构化结果")
    ap.add_argument("--tools", action="store_true", help="只打印工具清单")
    ap.add_argument("--verbose", action="store_true", help="打印每次工具调用")
    ap.add_argument("--model", default=OLLAMA_MODEL)
    ap.add_argument("--think", action="store_true", help="让模型输出思考过程")
    args = ap.parse_args()

    if args.tools:
        print("受限工具清单（共 %d 个，其中会改状态的只有 %s）："
              % (len(TOOL_SPECS), ", ".join(WRITE_TOOLS)))
        for s in TOOL_SPECS:
            f = s["function"]
            props = ", ".join(f["parameters"]["properties"]) or "无参数"
            print("  · %-20s %s" % (f["name"], props))
        return 0

    question = " ".join(args.question).strip()
    if not question:
        ap.error("请给一句话，例如：板子最近一次的加速度是多少")

    avail = model_available(model=args.model)
    if not avail["ok"]:
        print("运行时语言服务不可用：%s" % json.dumps(avail, ensure_ascii=False))
        return 2

    res = ask(question, model=args.model, verbose=args.verbose, think=args.think)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(res["answer"])
        print("\n--- 证据链 ---")
        for t in res["tool_calls"]:
            print("  %s(%s) → source=%s time=%s state=%s"
                  % (t["tool"], json.dumps(t["args"], ensure_ascii=False),
                     t["source"], t["time"], t["state"]))
        if res["guardrails"]:
            print("  ⚠ 守卫触发：%s" % "、".join(res["guardrails"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

