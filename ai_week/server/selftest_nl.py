#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第4周 · 受限工具层自测（**不调模型**）

为什么要单独测工具层：模型是概率性的，工具层必须是确定性的。
"不许编数据"这件事**不能只靠提示词**，得靠工具只吐真数据 + 出口再拦一道。
所以这一套断言全部围绕四件事：

  1. 受限：模型只能调白名单里的工具，没有自由 SQL；只读守卫拦写操作
  2. 校验：设备范围、行数上限、时间格式，非法一律拒，且**拒得说人话**
  3. 歧义：设备不唯一/没说清 → 要求反问，不许替用户挑
  4. ★ 防假成功：没有 COMPLETED 证据时，任何"已采集成功"的说法都要被拦下

运行：python selftest_nl.py
（自建临时数据库，跑完自删；不依赖真实板子，也不调 Ollama）
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
TMP = os.path.join(tempfile.gettempdir(), "nl_selftest_%d" % int(time.time()))

fails = []


def check(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name
          + (" | " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    os.makedirs(TMP, exist_ok=True)
    os.environ["DATA_DIR"] = TMP
    sys.path.insert(0, HERE)

    import server                                  # noqa: E402
    import nl_agent as nl                          # noqa: E402

    server.DATA_DIR = TMP
    server.DB_PATH = os.path.join(TMP, "data.db")
    server.SNAP_DIR = os.path.join(TMP, "snapshots")
    os.makedirs(server.SNAP_DIR, exist_ok=True)
    server.init_db()

    db = os.path.join(TMP, "data.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # ---- 造数据：两台"真实"设备 + 一台自测遗留设备 ----
    now = datetime.now(TZ)
    def iso(mins_ago):
        return (now - timedelta(minutes=mins_ago)).isoformat(timespec="milliseconds")

    for i in range(300):        # 300 条，用来验行数上限
        conn.execute(
            "INSERT INTO readings(device_id, sensor, unit, ax, ay, az, ts_device, "
            "ts_server, ax_raw, ay_raw, az_raw, is_new_sample, time_synced) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("board-A", "qma7981", "g", 0.01 * i, 0.0, 1.0, iso(300 - i),
             iso(300 - i), i, 0, 1000, 1, 1))
    conn.execute(
        "INSERT INTO readings(device_id, sensor, unit, ax, ay, az, ts_device, "
        "ts_server, ax_raw, ay_raw, az_raw, is_new_sample, time_synced) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("board-B", "qma7981", "g", 0.5, 0.0, 1.0, iso(5), iso(5), 1, 0, 1000, 1, 1))
    conn.execute(
        "INSERT INTO readings(device_id, sensor, unit, ax, ay, az, ts_device, "
        "ts_server, ax_raw, ay_raw, az_raw, is_new_sample, time_synced) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("selftest-leftover", "qma7981", "g", 0.0, 0.0, 1.0, iso(1), iso(1), 0, 0, 1000, 1, 0))
    conn.execute(
        "INSERT INTO frames(device_id, ts_server, filename, bytes, capture_ts, "
        "boot_id, seq, sha256, size_bytes, width, height, source, purged_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("board-A", iso(10), "a.jpg", 1234, iso(10), "boot1", 7, "deadbeef",
         1234, 320, 240, "periodic", None))
    conn.execute(
        "INSERT INTO frames(device_id, ts_server, filename, bytes, capture_ts, "
        "boot_id, seq, sha256, size_bytes, width, height, source, purged_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("board-A", iso(20), "b.jpg", 999, iso(20), "boot1", 6, "cafebabe",
         999, 320, 240, "periodic", iso(1)))
    conn.execute(
        "INSERT INTO help_events(event_id, device_id, kind, device_state, "
        "server_state, viewer_state, pressed_at, received_at, boot_id, seq, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("help-1", "board-A", "teach_help_test", "LOCAL_ACKED", "RECEIVED",
         "PENDING", iso(3), iso(3), "boot1", 8, iso(3)))
    conn.commit()

    print("\n== 1. 受限：工具白名单 ==")
    r = nl.execute_tool("drop_database", {}, conn)
    check("未知工具被拒", r["ok"] is False, r.get("error", "")[:40])
    check("拒的时候把可用工具列出来", "list_devices" in (r.get("allowed_tools") or []),
          r.get("allowed_tools"))
    check("工具清单里只有 1 个会改状态",
          len(nl.WRITE_TOOLS) == 1 and nl.WRITE_TOOLS[0] == "request_capture",
          nl.WRITE_TOOLS)

    print("\n== 2. 受限：只读守卫拦写操作 ==")
    for sql, tag in (("DELETE FROM readings", "DELETE"),
                     ("UPDATE readings SET ax=0", "UPDATE"),
                     ("DROP TABLE readings", "DROP"),
                     ("INSERT INTO readings VALUES(1)", "INSERT"),
                     ("SELECT * FROM readings", "SELECT *")):
        try:
            nl._assert_readonly(sql)
            check("拦下 %s" % tag, False, "竟然放行了")
        except PermissionError as e:
            check("拦下 %s" % tag, True, str(e)[:34])
    try:
        nl._assert_readonly("SELECT id, ax FROM readings")
        check("放行正常只读查询", True)
    except PermissionError as e:
        check("放行正常只读查询", False, e)

    print("\n== 3. 校验：参数不合法时返回结构化错误而不是抛异常 ==")
    r = nl.execute_tool("query_readings", {"device_id": "board-A", "limit": "abc"}, conn)
    check("limit 非整数 → ok=False 且说明原因", r["ok"] is False and "limit" in r["error"],
          r.get("error"))
    r = nl.execute_tool("query_readings", {"device_id": "board-A", "since": "上周"}, conn)
    check("时间格式非法 → 被拒", r["ok"] is False and "无法识别" in r["error"],
          r.get("error"))
    r = nl.execute_tool("query_readings",
                        {"device_id": "board-A", "limit": 5, "没这个参数": 1}, conn)
    check("多余参数不会炸（吞掉即可）", r["ok"] is True, r.get("error"))
    r = nl.execute_tool("get_latest_reading", "不是字典", conn)
    check("参数不是对象 → 被拒", r["ok"] is False, r.get("error"))

    print("\n== 4. 校验：行数上限由我们定，不由模型定 ==")
    r = nl.execute_tool("query_readings", {"device_id": "board-A", "limit": 99999}, conn)
    check("limit=99999 被夹到上限 %d" % nl.MAX_ROWS,
          r["data"]["stats"]["count"] == nl.MAX_ROWS, r["data"]["stats"]["count"])
    check("结果里回显实际用了多少行", r["limit_applied"] == nl.MAX_ROWS,
          r.get("limit_applied"))
    r = nl.execute_tool("query_readings", {"device_id": "board-A"}, conn)
    check("不传 limit 用默认 %d 条" % nl.DEFAULT_ROWS,
          r["data"]["stats"]["count"] == nl.DEFAULT_ROWS)

    print("\n== 5. 校验：设备范围（区分「没这台设备」与「设备没数据」）==")
    r = nl.execute_tool("get_latest_reading", {"device_id": "board-ZZZ"}, conn)
    check("不存在的设备被拒", r["ok"] is False, r.get("error", "")[:46])
    check("错误里点明这是'没这台设备'而不是'没数据'",
          "根本没有这台设备" in r["error"], r.get("error"))
    check("同时给出候选设备名单",
          set(r["candidates"]) >= {"board-A", "board-B"}, r.get("candidates"))
    r = nl.execute_tool("get_latest_reading", {"device_id": "board-A"}, conn)
    check("存在的设备正常返回", r["ok"] is True and r["data"]["device_id"] == "board-A")

    print("\n== 6. ★ 歧义：设备不唯一时必须反问，不许替用户挑 ==")
    r = nl.execute_tool("get_latest_reading", {}, conn)
    check("未指定设备且有多台 → 被拒", r["ok"] is False)
    check("标记 needs_clarification", r.get("needs_clarification") is True)
    check("明确要求反问用户", "反问" in (r.get("hint") or ""), r.get("hint"))
    check("把候选设备交给模型", len(r.get("candidates") or []) >= 2, r.get("candidates"))

    # 只有一台真实设备时，可以自动补全（不必为这个再问一遍）
    solo = sqlite3.connect(os.path.join(TMP, "solo.db"))
    solo.row_factory = sqlite3.Row
    solo.execute("CREATE TABLE readings(id INTEGER PRIMARY KEY, device_id TEXT, ts_server TEXT)")
    solo.execute("INSERT INTO readings(device_id, ts_server) VALUES('board-only', 'x')")
    solo.commit()
    chk = nl._check_device(solo, None)
    check("只有一台真实设备时自动补全（assumed=True）",
          chk["ok"] and chk.get("assumed") is True and chk["device_id"] == "board-only", chk)
    solo.close()

    print("\n== 7. 时间范围解析 ==")
    check("'10m' 能解析", nl._parse_since("10m") is not None, nl._parse_since("10m"))
    check("'2h' 能解析", nl._parse_since("2h") is not None)
    check("'3d' 能解析", nl._parse_since("3d") is not None)
    check("不传表示不限", nl._parse_since(None) is None)
    try:
        nl._parse_since("昨天")
        check("'昨天'被拒（不支持就别硬猜）", False, "竟然放行了")
    except ValueError as e:
        check("'昨天'被拒（不支持就别硬猜）", True, str(e)[:40])

    print("\n== 8. 每个成功结果都带 source / time / state ==")
    for tool, args in (("list_devices", {}),
                       ("get_latest_reading", {"device_id": "board-A"}),
                       ("query_readings", {"device_id": "board-A"}),
                       ("query_frames", {"device_id": "board-A"}),
                       ("list_help_events", {})):
        r = nl.execute_tool(tool, args, conn)
        has = bool(r.get("source")) and r.get("state") is not None and "time" in r
        check("%s 三要素齐全" % tool, has,
              "source=%s time=%s state=%s" % (r.get("source"), r.get("time"), r.get("state")))
    r = nl.execute_tool("get_latest_reading", {"device_id": "board-A"}, conn)
    check("最新读数带 ts_server（服务端的钟）", bool(r["data"]["ts_server"]))
    check("并说明 ts_device 与 time_synced 的语义（防把板子的钟当绝对时间）",
          "time_synced" in r["time_semantics"], list(r["time_semantics"]))
    r = nl.execute_tool("query_frames", {"device_id": "board-A"}, conn)
    by_name = {f["filename"]: f for f in r["data"]["frames"]}
    check("已清理的帧标记 image_available=False（哈希仍在，图没了）",
          by_name["b.jpg"]["image_available"] is False
          and by_name["a.jpg"]["image_available"] is True,
          {k: v["image_available"] for k, v in by_name.items()})
    check("已清理的帧仍带 sha256（证据不随原图消失）",
          by_name["b.jpg"]["sha256"] == "cafebabe")

    print("\n== 9. ★ 请求采集：设备不存在就不下发指令 ==")
    before = conn.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"]
    r = nl.execute_tool("request_capture", {"device_id": "board-ZZZ"}, conn)
    after = conn.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"]
    check("不存在的设备被拒", r["ok"] is False)
    check("★ 库里没有多出一条指令（不给不存在的设备造记录）", before == after,
          "%d -> %d" % (before, after))
    check("说明理由：下指令等于制造假记录",
          "假记录" in (r.get("note") or ""), r.get("note"))

    print("\n== 10. ★★ 防假成功：request_capture 绝不返回'已采集成功' ==")
    r = nl.execute_tool("request_capture", {"device_id": "board-A",
                                            "reason": "自测"}, conn)
    check("下发成功（指令落库）", r["ok"] is True, r.get("error"))
    check("★ state 是 PENDING（设备还没来取）", r["state"] == "PENDING", r["state"])
    check("★ success_claim_allowed=False", r["success_claim_allowed"] is False,
          r["success_claim_allowed"])
    check("★ 返回体里没有任何'成功'字样",
          "成功" not in json.dumps(r, ensure_ascii=False))
    check("指明必须二次确认（用哪个工具查）",
          "t_get_command_status" in (r.get("must_verify_with") or ""),
          r.get("must_verify_with"))
    rid = r["data"]["request_id"]
    check("给了 request_id 供后续核对", bool(rid), rid)

    print("\n== 11. 立刻回查状态：仍然没有完成证据 ==")
    r = nl.execute_tool("get_command_status", {"request_id": rid}, conn)
    check("能查到这条请求", r["ok"] is True)
    check("state 仍是 PENDING", r["state"] == "PENDING", r["state"])
    check("has_frame=False（没有图像证据）", r["data"]["has_frame"] is False)
    r = nl.execute_tool("get_command_status", {"request_id": "req-不存在"}, conn)
    check("查不存在的请求 → 明确报'没有匹配的采集请求'",
          r["ok"] is False and "没有匹配" in r["error"], r.get("error"))

    print("\n== 12. ★★ 结果守卫：模型硬说成功也要被拦下 ==")
    trace = [{"tool": "request_capture", "state": "PENDING",
              "result": {"ok": True, "state": "PENDING",
                         "success_claim_allowed": False,
                         "data": {"request_id": rid}}}]
    g = nl.guard_answer("好的，已经拍好了，图像已采集成功。", trace, "让板子拍一张")
    check("拦下'已采集成功'", "success_without_evidence" in g["guardrails"], g["guardrails"])
    check("文案被替换成诚实的说法", "没有" in g["text"] and "更正" in g["text"],
          g["text"][:50])
    check("并把真实状态回显出来", "PENDING" in g["text"])

    good = [{"tool": "get_command_status", "state": "COMPLETED",
             "result": {"ok": True, "state": "COMPLETED",
                        "data": {"state": "COMPLETED", "has_frame": True}}}]
    g = nl.guard_answer("采集已完成，图像已入库。", good, "拍好了吗")
    check("有 COMPLETED 证据时正常放行", g["guardrails"] == [], g["guardrails"])

    # 回归：模型把**用户自己的疑问句**复述回来时，不能被误判成"声称成功"。
    # （实测真的踩到过：用户问"告诉我拍好了没"，模型答"…查结果告诉你拍好了没"，
    #   子串里出现"拍好了"就被拦下了，属于误伤。）
    echo = ("要我对 s3eye-group01 发起一次新采集吗？"
            "确认后我就下发指令，再用 get_command_status 查结果告诉你拍好了没。")
    g = nl.guard_answer(echo, trace, "让板子拍一张，然后告诉我拍好了没")
    check("★ 复述用户的疑问不算声称成功（不误伤）", g["guardrails"] == [],
          g["guardrails"])
    check("★ 且原文没被改写", g["text"] == echo, g["text"][:40])
    g = nl.guard_answer("目前还没有采集完成的证据，不能说已采集成功。", trace, "拍好了吗")
    check("★ 主动否定'成功'的话不算声称成功", g["guardrails"] == [], g["guardrails"])
    g = nl.guard_answer("好的，已经拍好了。", trace, "让板子拍一张")
    check("★ 真正的声称仍然被拦下", "success_without_evidence" in g["guardrails"],
          g["guardrails"])

    print("\n== 13. 结果守卫：其它两类越界 ==")
    g = nl.guard_answer("加速度是 0.123 g。", [], "加速度多少")
    check("一个工具都没调却报数字 → 标记 no_tool_used",
          "no_tool_used" in g["guardrails"], g["guardrails"])
    check("并提醒这些数字没有来源", "没有数据来源" in g["text"], g["text"][:40])
    g = nl.guard_answer("好的。", [], "你好")
    check("没调工具也没数字 → 不误报", g["guardrails"] == [], g["guardrails"])

    clar = [{"tool": "get_latest_reading",
             "result": {"ok": False, "needs_clarification": True,
                        "candidates": ["board-A", "board-B"]}}]
    g = nl.guard_answer("board-A 的加速度是 0.5 g。", clar, "加速度多少")
    check("工具要求反问而模型没反问 → 标记 clarification_skipped",
          "clarification_skipped" in g["guardrails"], g["guardrails"])
    check("并把候选设备补进文案", "board-B" in g["text"], g["text"][-60:])

    print("\n== 14. 澄清后能正常回答（不误伤）==")
    g = nl.guard_answer("你指的是哪一台？候选有 board-A、board-B。", clar, "加速度多少")
    check("模型反问了就不标记", "clarification_skipped" not in g["guardrails"],
          g["guardrails"])

    print("\n== 15. 求助事件三层状态分别返回，不合并 ==")
    r = nl.execute_tool("list_help_events", {}, conn)
    h = r["data"]["helps"][0]
    check("device_state / server_state / viewer_state 三列都在",
          all(k in h for k in ("device_state", "server_state", "viewer_state")),
          list(h.keys()))
    check("三个值彼此独立", (h["device_state"], h["server_state"], h["viewer_state"])
          == ("LOCAL_ACKED", "RECEIVED", "PENDING"),
          (h["device_state"], h["server_state"], h["viewer_state"]))
    check("结果里写明三者不可互推", "不可互推" in (r.get("note") or ""), r.get("note"))

    print("\n== 16. 运行时语言服务可用性探测（不通过也不算失败）==")
    avail = nl.model_available()
    print("  [INFO] Ollama %s：%s" % ("可用" if avail["ok"] else "不可用",
                                      json.dumps(avail, ensure_ascii=False)[:120]))

    print("\n== 17. ★★ 设备无响应：整条链路上每一环都必须如实（\"无响应记录\"）==")
    # 场景：用户说"让板子现在拍一张"，但板子离线 / 一直不来取指令。
    # 这是最容易被"假装成功"糊弄过去的场景，所以逐环断言。
    r = nl.execute_tool("request_capture", {"device_id": "board-A",
                                            "reason": "无响应场景自测"}, conn)
    rid2 = r["data"]["request_id"]
    check("① 指令下发成功（这一步确实成功了，不许含糊）", r["ok"] is True)
    check("① 但状态只能是 PENDING", r["state"] == "PENDING", r["state"])
    check("① 且明确不许声称成功", r["success_claim_allowed"] is False)

    # 模拟"设备一直没来取"：把创建时间推到很久以前，再跑一次超时扫描
    old = (datetime.now(TZ) - timedelta(hours=1)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE commands SET created_at=? WHERE request_id=?", (old, rid2))
    conn.commit()
    n = server.sweep_commands(conn)
    check("② 超时扫描把没人取的指令标成 EXPIRED（不是 FAILED）", n >= 1, n)
    row2 = conn.execute("SELECT state, fail_reason FROM commands WHERE request_id=?",
                        (rid2,)).fetchone()
    check("② 归因是 EXPIRED（找人）而不是 TIMEOUT（找活）",
          row2["state"] == server.ST_EXPIRED, row2["state"])
    check("② 也绝不写 FAILED —— 超时≠硬件故障",
          row2["state"] != server.ST_FAILED, row2["state"])

    r = nl.execute_tool("get_command_status", {"request_id": rid2}, conn)
    check("③ 回查时状态如实是 EXPIRED", r["state"] == "EXPIRED", r["state"])
    check("③ has_frame=False（自始至终没有任何图像证据）",
          r["data"]["has_frame"] is False)
    check("③ 返回体里没有\"成功\"字样",
          "成功" not in json.dumps(r, ensure_ascii=False))

    trace2 = [{"tool": "request_capture", "state": "PENDING",
               "result": {"ok": True, "state": "PENDING",
                          "success_claim_allowed": False,
                          "data": {"request_id": rid2}}},
              {"tool": "get_command_status", "state": "EXPIRED",
               "result": {"ok": True, "state": "EXPIRED",
                          "data": {"state": "EXPIRED", "has_frame": False}}}]
    g = nl.guard_answer("已经拍好了，图像已入库。", trace2, "让板子拍一张")
    check("④ 无响应场景下，声称成功被拦下",
          "success_without_evidence" in g["guardrails"], g["guardrails"])
    check("④ 拦下后回显的是真实状态 EXPIRED", "EXPIRED" in g["text"], g["text"][:80])
    g = nl.guard_answer("指令已下发，但设备一直没来取，已过期（EXPIRED），"
                        "目前没有采集完成的证据。", trace2, "让板子拍一张")
    check("④ 如实描述无响应则正常放行（不误伤）", g["guardrails"] == [],
          g["guardrails"])
    check("⑤ 整条链路任何一环都没有把\"没响应\"说成\"失败\"或\"成功\"",
          row2["state"] == "EXPIRED" and r["data"]["has_frame"] is False
          and r["ok"] is True)
    print("  [INFO] 这一组就是课程要求的『无响应记录』：")
    print("         request_id=%s → PENDING → EXPIRED（has_frame=False，"
          "success_claim_allowed=False）" % rid2)

    conn.close()

    print("\n" + "=" * 60)
    print("结果：%s" % ("全部通过" if not fails
                       else "失败 %d 项：%s" % (len(fails), fails)))
    print("=" * 60)
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
