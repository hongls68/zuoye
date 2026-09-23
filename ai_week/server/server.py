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
  GET  /api/frames?device_id=X&limit=N  照片画廊元数据（含 sha256 / 尺寸 / 来源 / 是否已清理）
  GET  /api/frames/image?id=N           按帧 id 取原图（已清理的帧回 404 + 哈希）

  数据保留（计划书 4.5）：**元数据（含 SHA-256）永久保留，原图按 RETENTION_DAYS 清理**。
  清掉的是文件，不是证据 —— 画廊里仍能看到这一条的哈希与拍摄时间。

【第3周】按键求助事件（方向反过来：板子主动发起，人来回应）：
  POST /api/help                  板子发起 / 取消一次教学求助测试消息
  GET  /api/help/poll?device_id=X 板子轮询：查看者回应了吗？被取消了吗？
  GET  /api/help?device_id=X&limit=N  网页列出求助事件
  POST /api/help/answer           查看者回应（携带 answered_by / answer_text）
  POST /api/help/cancel           查看者取消

  ★ 核心：一次求助里同时存在**三个来源不同、谁也替不了谁**的事实，分三列存：
      device_state  ① 本地确认  —— 板子自报（板子的钟）
      server_state  ② VPS 接收  —— 服务端自判（服务端的钟，received_at）
      viewer_state  ③ 查看者回应 —— 网页前的人（人的动作时刻）
    压成一个 state 就会重演第2周"旧值冒充"那类错误：
    拿一个来源的事实去冒充另一个来源的事实。

  另外两条刻意定下的规则：
    · 已回应的求助**不能被取消** —— 回应是既成事实，不能让发起方单方面抹掉；
    · 超时只改 server_state，**不动 device_state / viewer_state** ——
      「没人回应」是服务端的判断，不等于「板子没发出来」，也不等于「人拒绝了」。

  状态集合：PENDING → RECEIVED → EXECUTING → UPLOADED → COMPLETED
            分支：EXPIRED（TTL 内无人取）/ TIMEOUT（取了没回传）/ FAILED（设备报错或证据不足）

【第4周】自然语言查询与请求采集（把上面的接口封装成受限工具，交给运行时模型调用）：
  POST /api/ask                   一句话进，一个带证据的回答出（含工具调用链与守卫标记）
  GET  /api/ask/health            运行时语言服务（Ollama）在不在、模型有没有

  ★ 两个角色必须分清：
      开发助手（写这份代码的 AI）—— 开发期参与写代码，用户看不到它；
      产品运行时模型（Ollama）    —— 运行期被 /api/ask 调用，只看得见受限工具。
    模型不知道的事，只能靠**工具返回的结构化结果**告诉它，所以工具返回什么，
    决定了它有没有可能说实话。

  ★ 四条硬约束（都在 nl_agent.py 里，且有自测守着）：
      1. 受限工具：只能调白名单里的 7 个工具，没有自由写 SQL 的能力；
         只读守卫拦 INSERT/UPDATE/DELETE/DROP，行数上限由服务端定不由模型定。
      2. 防假成功：request_capture 下完指令只会返回 PENDING，
         `success_claim_allowed=False`；**没有 COMPLETED 证据时不许说"已采集成功"**。
         模型硬说也没用 —— guard_answer() 会在最终文案上再拦一道。
      3. 歧义不猜：设备不唯一 → 返回 needs_clarification + 候选清单，要求反问用户。
      4. 必须报来源/时间/状态：每个工具结果都带 source / time / state 三要素。

  ★ 核心原则：UPLOADED ≠ COMPLETED。
    收到一张图不代表它就是这次要的那张，必须通过三条证据校验才算完成：
      E1 请求贯穿   request_id 同时出现在 ①指令记录 ②设备回执 ③观测记录
      E2 时序合理   观测的 capture_ts 必须晚于指令的 dispatched_at
      E3 新鲜度单调 同一次开机（boot_id）内 seq 必须严格递增

【第5周】传感器示波器：三轴波形 + 姿态孪生（对标参考产品的「传感器示波器」页）：
  POST /api/waveform              板端**批量**上传一批连续采样（默认 20Hz × 100 点）
  GET  /api/waveform?device_id=X&batches=N  取最近 N 批，拼成一条连续波形
  GET  /api/attitude?device_id=X  只取最新姿态（3D 孪生面板高频轮询用，比拉波形轻）

  ★ 为什么是批量上传，不是参考产品那样的 ~20Hz 实时推送：
    板↔电脑这条 Wi-Fi 链路实测延迟 3ms~770ms 剧烈抖动（第 1 周就确认了），
    在这条链路上维持长连接（WebSocket / MQTT）是给自己添堵。
    批量上传对丢包是**天然容错**的：丢一批只少一段波形，不会把连接搞死。

  ★ 姿态只用三轴加速度计就能判，因为加速度计测的是"重力指向设备的哪一面"：
      能测：平放 / 竖立 / 侧立 / 自由倾斜（凡由倾斜决定的状态）
      测不到：绕重力轴自转的**航向 Yaw** —— 自转不改变重力方向
    所以 yaw 这一列**永远为 NULL**，并带一句 yaw_note 说明原因。
    「测不到」要跟着数据一起出去，否则看的人分不清是"没采到"还是"测不了"。

  ★ 数据保留策略在这里分岔，别搅在一起：
      原图   = 证据 → 元数据（含 SHA-256）永久留，只清原图（默认 7 天）
      波形   = 过程 → 留够复现窗口就行（默认每设备最近 720 批 ≈ 1 小时）

运行：python server.py   （默认监听 0.0.0.0:8000）
"""
import hashlib
import json
import math
import os
import socket
import sqlite3
import threading
import time
import traceback
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

# ---- 第4周：运行时语言服务（自然语言 → 受限工具 → 结构化回答）----
# 刻意用 try/except 包住：Ollama 没装、模型没拉，都不该让整个服务起不来。
# 前两周的功能与语言服务无关，谁都不能因为对方挂掉而不可用。
try:
    import nl_agent                       # noqa: E402
    NL_IMPORT_ERR = None
except Exception as _nl_err:              # noqa: BLE001
    nl_agent = None
    NL_IMPORT_ERR = "%s: %s" % (type(_nl_err).__name__, _nl_err)

# ---- 第2周：指令通道的时间参数 ----
DEFAULT_TTL_S = 120                # 指令有效期：这么久没人取走 -> EXPIRED
EXEC_TIMEOUT_S = 120               # 已取走但这么久没回传观测 -> TIMEOUT
SWEEP_INTERVAL_S = 5               # 后台过期扫描周期（秒）

# ---- 第2周：数据保留与配额（对应计划书 4.5）----
# 规则：**元数据（含哈希）永久保留，原图按配额清理**。
# 理由：哈希能证明"这张图当时确实是这个内容"，是可追溯性的根；
#       原图只占存储，开发期留 7 天足够复现与演示。
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS") or 7)
PURGE_INTERVAL_S = 300             # 后台清理扫描周期（秒）

# 状态集合（与网页状态徽标一一对应，改这里要同步改 index.html）
ST_PENDING   = "PENDING"    # 指令已创建，等待设备取走
ST_RECEIVED  = "RECEIVED"   # 设备已取走并回执
ST_EXECUTING = "EXECUTING"  # 设备正在采集/上传
ST_UPLOADED  = "UPLOADED"   # 观测已入库（尚未通过证据校验）
ST_COMPLETED = "COMPLETED"  # 三条证据校验通过
ST_EXPIRED   = "EXPIRED"    # TTL 内无人取走
ST_TIMEOUT   = "TIMEOUT"    # 已取走但超时未回传
ST_FAILED    = "FAILED"     # 设备显式报错，或证据校验不通过

# ---- 第3周：按键求助事件的三层状态 ----
# 三层分别由三个不同的主体产生，**任何一层都不能替另一层作证**：
#   DEV_*  板端自报（板子自己的钟）
#   SRV_*  服务端自己判定（服务端自己的钟）
#   VWR_*  查看者（网页前的人）的动作
HELP_DEV_LOCAL_ACKED = "LOCAL_ACKED"   # ① 板端：按键已受理
HELP_DEV_SENDING     = "SENDING"       # 板端：正在发给 VPS
HELP_DEV_ACCEPTED    = "ACCEPTED"      # 板端：已收到服务端回执
HELP_DEV_FAILED      = "FAILED"        # 板端：没发出去

HELP_SRV_RECEIVED    = "RECEIVED"      # ② 服务端：确认收到（时间戳由服务端自己打）
HELP_SRV_CANCELLED   = "CANCELLED"     # 服务端：已取消（不再接受回应）
HELP_SRV_EXPIRED     = "EXPIRED"       # 服务端：超时无人回应

HELP_VWR_PENDING     = "PENDING"       # ③ 查看者：还没回应
HELP_VWR_ANSWERED    = "ANSWERED"      # 查看者：已回应
HELP_VWR_IGNORED     = "IGNORED"       # 查看者：显式忽略

# 求助事件的有效期：超过这么久没人回应，服务端把它标成 EXPIRED。
# 注意这只改 server_state，**不动 device_state 和 viewer_state** ——
# "没人回应"是服务端的判断，不等于"板子没发出来"，也不等于"人拒绝了"。
HELP_TTL_S = 300

# 中文标签：网页直接用，避免前端再维护一份映射
HELP_LABEL = {
    HELP_DEV_LOCAL_ACKED: "① 本地已确认（板端按键受理）",
    HELP_DEV_SENDING:     "板端发送中",
    HELP_DEV_ACCEPTED:    "板端已收到回执",
    HELP_DEV_FAILED:      "板端发送失败",
    HELP_SRV_RECEIVED:    "② VPS 已接收",
    HELP_SRV_CANCELLED:   "已取消（不再接受回应）",
    HELP_SRV_EXPIRED:     "已过期（无人回应）",
    HELP_VWR_PENDING:     "③ 等待查看者回应",
    HELP_VWR_ANSWERED:    "③ 查看者已回应",
    HELP_VWR_IGNORED:     "查看者已忽略",
}

# ---- 传感器示波器：三轴波形 + 姿态（对标参考产品的「传感器示波器」页）----
#
# 【为什么是批量上传，而不是参考产品那样的 ~20Hz 实时推送】
#   板子↔电脑这条 Wi-Fi 链路实测延迟在 3ms~770ms 之间剧烈抖动（第 1 周就确认了），
#   在这条链路上维持一条长连接（WebSocket / MQTT）是给自己添堵。
#   批量上传对丢包是**天然容错**的：丢一批只少一段波形，不会把连接搞死。
#   所以板端本地按 20Hz 采样、攒够一批再 POST 一次，服务端按批存、网页画"最近 N 批"。
WAVE_MAX_BATCH = 600            # 单批样本数上限（20Hz × 30 秒）
# 每设备保留最近多少批（20Hz×100点@5s ≈ 1 小时）。可用环境变量覆盖 ——
# 自测就是靠把它压到个位数来验证清理逻辑的。
WAVE_KEEP_BATCHES = int(os.environ.get("WAVE_KEEP_BATCHES") or 720)
WAVE_PURGE_INTERVAL_S = 60      # 后台波形清理周期（秒）
WAVE_MAX_ABS_RAW = 32767        # 原始计数按 int16 收，超出就是脏数据
WAVE_MAX_HZ = 200               # 采样率上限，防呆

# 原始计数 → g 的换算。★ 这里差点埋坑，写清楚：
#   板端 qma7981.c 是  g = raw / (QMA7981_LSB_PER_G × QMA7981_CALIB_SCALE)
#                                  = raw / (1024 × 0.8078) ≈ raw / 827.19
#   两个因子**都要**跟着数据走，不能只发一个：
#     lsb_per_g  手册标称灵敏度（±8g 量程 = 1024 LSB/g）
#     calib      实测标定系数（板子水平静置时 |a| 应为 1.000 的那个修正）
#   只发一个融合后的系数，以后就没法回答"这个偏差是量程选错还是零点没标"。
WAVE_DEFAULT_LSB_PER_G = 1024.0
WAVE_DEFAULT_CALIB = 0.8078

# 姿态分类阈值：归一化后的重力投影超过它，才认为"这一面确实朝下/朝上"。
# 0.85 ≈ 与主轴夹角 31.8° 以内。取这个值的理由：太松会把"斜着拿"误判成平放，
# 太严则正常摆放（桌面未必绝对水平）都会掉进 Tilted。
POSTURE_MAJOR_AXIS = 0.85

# ★ 唯一一处「只能假设、无法自证」的约定，单独提出来放在最显眼的地方。
#
# 加速度计的读数里有一半是**可以百分之百确定**的：
#     静止时它测的是"支撑力"而不是"重力本身"（比力 f = a − g，a=0），
#     所以读数指向**天空** —— 这正是 |a| 恒为 1 g、与摆放姿态无关的原因。
#     于是 **az > 0 ⟺ 传感器的 +Z 轴朝上**，这一条与芯片怎么贴装无关。
#
# 另一半**只能假设**：+Z 轴对应板子的哪一面。这取决于芯片在板上的贴装方向，
# 乐鑫没有公布，我们手上也没有板子可以实测。所以提成一个常量：
#     数值照算，只有"哪一面朝上"这句文案跟着它变。
# 拿到板子后平放一次 —— 看串口 az 是正还是负、当时朝上的是哪一面；
# 若与下面的约定相反，把这行改成 True 即可，其余代码一行都不用动。
#
# 当前取 False（az>0 → 背面朝上）的依据：本板平放时实测 az ≈ +0.7，
# 而参考产品把自己的平放态标成 "Flat Down（背面朝上）"，两者一致。
Z_UP_IS_FRONT_FACE = False

# 姿态分类结果（与参考产品的四档对齐：Side Edge / Upright / Tilted / Flat Down）
POSTURE_LABEL = {
    "flat":      "平放",
    "upright":   "竖直正面",
    "side_edge": "侧边直立",
    "tilted":    "自由倾斜",
    "unknown":   "无法判定",
}
POSTURE_EN = {
    "flat":      "Flat",
    "upright":   "Upright",
    "side_edge": "Side Edge",
    "tilted":    "Tilted",
    "unknown":   "Unknown",
}

# ★ 本板测不到航向。这句话要跟着数据一起出去，而不是只在文档里写一遍 ——
#   否则网页上那个"航向"格子空着，看的人分不清是"没采到"还是"测不了"。
YAW_NOTE = "本板无陀螺仪/磁力计：航向（绕重力轴自转）在原理上不可测，不是没采到"

_db_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def get_db() -> sqlite3.Connection:
    # DATA_DIR 可能指向一个还不存在的目录（例如自测用 ./tmpdata），先补出来，
    # 否则 sqlite 直接抛 "unable to open database file"，排查起来很绕。
    os.makedirs(DATA_DIR, exist_ok=True)
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
                          ("ts_device", "TEXT"),
                          # 下面几列服务于「照片画廊 + 完整性检验 + 配额清理」
                          ("sha256", "TEXT"),       # 原图内容的 SHA-256
                          ("size_bytes", "INTEGER"),# 原图字节数
                          ("width", "INTEGER"),     # 从 JPEG 头解析出的宽
                          ("height", "INTEGER"),    # 从 JPEG 头解析出的高
                          ("source", "TEXT"),       # 来源：web_manual / periodic / unknown
                          ("purged_at", "TEXT")):   # 原图被清理的时刻（NULL=原图还在）
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

        # 第3周：按键求助事件。
        #
        # 【为什么把状态拆成三列，而不是一个 state 字段】
        #   这是本周的题眼：一次"按键求助"里同时存在三个**来源不同、谁也替不了谁**的事实：
        #
        #     device_state  ① 本地确认  —— 板子自己说按键被受理了。只有板子知道。
        #     server_state  ② VPS 接收  —— 服务端自己说收到了。只有服务端知道，
        #                                  且时间戳必须由服务端自己打（received_at），
        #                                  绝不采信板子报上来的时间。
        #     viewer_state  ③ 查看者回应 —— 网页前的人点了回应。只有人知道。
        #
        #   如果压成一个 state，就会出现"服务端收到就算完成"这种偷换 ——
        #   那正是第2周"旧值冒充"的同类错误：拿一个来源的事实去冒充另一个来源的事实。
        conn.execute(
            """CREATE TABLE IF NOT EXISTS help_events(
                event_id      TEXT PRIMARY KEY,
                device_id     TEXT NOT NULL,
                kind          TEXT,
                device_state  TEXT,      -- ① 板端自报
                server_state  TEXT,      -- ② 服务端自己判定
                viewer_state  TEXT,      -- ③ 查看者侧
                pressed_at    TEXT,      -- 板端时钟：按下时刻
                local_ack_at  TEXT,      -- 板端时钟：本地确认时刻
                sent_at       TEXT,      -- 板端时钟：发出时刻
                received_at   TEXT,      -- 服务端时钟：入库时刻（★ 服务端自己打）
                answered_at   TEXT,      -- 查看者动作时刻
                cancelled_at  TEXT,
                answered_by   TEXT,
                answer_text   TEXT,
                cancelled_by  TEXT,      -- device / viewer —— 谁取消的必须记清
                cancel_reason TEXT,
                boot_id       TEXT,
                seq           INTEGER,
                reason        TEXT,      -- 失败原因等
                updated_at    TEXT
            )"""
        )
        conn.execute(
            # 注意：help_events 用 event_id(TEXT) 做主键，没有自增 id 列，
            # 排序只能按 received_at（服务端时钟）。别照抄 commands 表的 (device_id, id)。
            "CREATE INDEX IF NOT EXISTS idx_help_dev "
            "ON help_events(device_id, received_at DESC)"
        )

        # 传感器示波器：一批 = 板端攒好的一小段连续采样（默认 20Hz × 5 秒 = 100 点）。
        #
        # 【为什么按批存，而不是一点一行】
        #   20Hz 一点一行的话，一小时就是 7.2 万行 —— 库里全是波形，查询和备份都被拖累。
        #   波形是**过程数据**，成批进来、成批用掉，没有"查第 31872 个点"这种需求。
        #   所以一批一行，样本序列压成紧凑字符串（"x,y,z;x,y,z;…"）存在 samples 里。
        #
        # 【样本存原始计数，不存换算后的 g】
        #   原始 ADC 计数是一手证据（第 1 周"三处对账"就是拿它对的），
        #   换算系数（scale）另存一列 —— 以后标定系数改了，历史数据仍可重算。
        conn.execute(
            """CREATE TABLE IF NOT EXISTS wave_batches(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id    TEXT NOT NULL,
                boot_id      TEXT,
                batch_seq    INTEGER,
                hz           INTEGER,
                n_samples    INTEGER NOT NULL,
                lsb_per_g    REAL,            -- 手册标称灵敏度（±8g 量程 = 1024）
                calib        REAL,            -- 实测标定系数
                scale        REAL,            -- 派生：g = raw × scale（= 1/(lsb_per_g×calib)）
                t_first      TEXT,            -- 板端时钟：批内首样本时刻（只存，不采信）
                t_last       TEXT,            -- 板端时钟：批内末样本时刻（只存，不采信）
                dropped      INTEGER,         -- 板端如实上报：攒批期间因环形缓冲溢出丢掉的样本数
                received_at  TEXT NOT NULL,   -- ★ 服务端时钟：入库时刻，判定一律用它
                samples      TEXT NOT NULL,   -- 紧凑 "x,y,z;x,y,z;…"（原始 int16 计数）
                ax_raw       INTEGER,         -- 批内末样本的原始值（便于快速取"当前值"）
                ay_raw       INTEGER,
                az_raw       INTEGER,
                ax           REAL,            -- 换算后的 g 值
                ay           REAL,
                az           REAL,
                acc_mag      REAL,            -- 合加速度 |a|（g），静止时 ≈ 1
                pitch        REAL,            -- 俯仰（度），由重力向量反算
                roll         REAL,            -- 横滚（度），由重力向量反算
                posture      TEXT,            -- 姿态分类 key（见 POSTURE_LABEL）
                posture_note TEXT,            -- 例如"正面朝上"
                yaw          REAL,            -- ★ 永远为 NULL：本板测不到航向
                yaw_note     TEXT             -- 为什么是 NULL，跟着数据一起走
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wave_dev "
            "ON wave_batches(device_id, id DESC)"
        )
        # 老库补列：CREATE TABLE IF NOT EXISTS 对已存在的表不会加列，
        # 所以新加的字段必须单独 ALTER 一次，否则旧 data.db 一查就报 no such column。
        wave_cols = {r[1] for r in conn.execute("PRAGMA table_info(wave_batches)")}
        for col, decl in (("dropped", "INTEGER"),):
            if col not in wave_cols:
                conn.execute(f"ALTER TABLE wave_batches ADD COLUMN {col} {decl}")
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ---------------- 第3周：求助事件的公共逻辑 ----------------

def help_row_out(row) -> dict:
    """把一行求助事件整理成网页与板子都能直接用的形状。

    关键点：**三层状态各自带标签，三层时间戳按来源分组**。
    网页上必须能一眼看出"这个时刻是谁打的表" ——
    这正是本周要求「本地 / VPS / 查看者三种状态可区分」的落点。
    """
    if row is None:
        return None
    d = dict(row)
    d["device_label"] = HELP_LABEL.get(row["device_state"],
                                       row["device_state"] or "-")
    d["server_label"] = HELP_LABEL.get(row["server_state"],
                                       row["server_state"] or "-")
    d["viewer_label"] = HELP_LABEL.get(row["viewer_state"],
                                       row["viewer_state"] or "-")
    # 三种时钟分开列，绝不合并成一个"时间" —— 合并了就没法判断谁在撒谎
    d["clock_sources"] = {
        "device": {"pressed_at": row["pressed_at"],
                   "local_ack_at": row["local_ack_at"]},
        "server": {"received_at": row["received_at"]},
        "viewer": {"answered_at": row["answered_at"],
                   "answered_by": row["answered_by"]},
    }
    d["answerable"] = (row["server_state"] == HELP_SRV_RECEIVED
                       and row["viewer_state"] == HELP_VWR_PENDING)
    if row["viewer_state"] == HELP_VWR_ANSWERED:
        d["stage"] = "已完成：查看者已回应"
    elif row["server_state"] == HELP_SRV_CANCELLED:
        d["stage"] = "已取消（由 %s 取消）" % (row["cancelled_by"] or "?")
    elif row["server_state"] == HELP_SRV_EXPIRED:
        d["stage"] = "已过期：服务端收到了，但一直没人回应"
    elif row["server_state"] == HELP_SRV_RECEIVED:
        d["stage"] = "等待查看者回应（VPS 已接收）"
    else:
        d["stage"] = "未知"
    return d


def sweep_help_events(conn) -> int:
    """把超时无人回应的求助标成 EXPIRED。

    只改 server_state，**不动 device_state / viewer_state**：
    「没人回应」是服务端的判断，既不等于「板子没发出来」，也不等于「人拒绝了」。
    三者压成一个字段的话，排障方向立刻就偏了 —— 这是第2周
    「EXPIRED 找人 / TIMEOUT 找活」那条归因原则在求助通道上的同一套逻辑。
    """
    cutoff = (datetime.now(TZ) - timedelta(seconds=HELP_TTL_S)).isoformat()
    cur = conn.execute(
        "UPDATE help_events SET server_state=?, updated_at=? "
        "WHERE server_state=? AND viewer_state=? AND received_at < ?",
        (HELP_SRV_EXPIRED, now_iso(), HELP_SRV_RECEIVED, HELP_VWR_PENDING, cutoff))
    if cur.rowcount:
        conn.commit()
    return cur.rowcount


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


def parse_iso(iso: str):
    """ISO8601 → datetime。解析不了就返回 None（不猜、不拿"现在"顶替）。

    波形接口用它算"每个样本距离现在多少秒"。
    返回 None 时调用方会把时间差按 0 处理 —— 时间轴可能不准，
    但**绝不会因为一个坏时间戳就把整条波形丢掉**。
    """
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


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


# ---------------- 传感器示波器：波形入库与姿态判定 ----------------
#
# 【姿态判定为什么只用三轴加速度计就够 —— 这是本文件里最值得说清的一件事】
#
#   加速度计测的是「重力方向指向设备的哪一面」。
#   凡是**由倾斜决定**的状态，它都能测：平放、竖立、侧立、斜着放。
#
#   它测不到的只有一种：**绕重力轴自转**（航向 Yaw）。
#   因为自转不改变重力方向 —— 无论怎么转，重力还是指着同一面。
#   要测它必须用陀螺仪（积分角速度）或磁力计（找地磁北）。
#
#   所以参考产品那个「特征：自由倾斜（Tilted） · 1.02G」里，
#   那个 1.02G 恰恰说明它也是**拿重力模长在判姿态** —— 静止时必然 ≈ 1 g。
#   换句话说：**姿态分类这块，我们的板子完全做得出来**；
#   做不出来的只有六轴里的 Gyro 三路波形和 3D 孪生的 Yaw。
#   （第 2 周的对账表原写「六轴 Gyro 与 3D 姿态都做不了」，范围划宽了，已更正。）


def classify_posture(ax: float, ay: float, az: float) -> tuple:
    """只用重力方向判定姿态。返回 (key, 中文标签, 英文标签, 补充说明)。

    轴定义以板子自身坐标系为准（与 qma7981 读出的 x/y/z 一致）：
        x → 板面内一个方向      y → 板面内另一个方向      z → 垂直板面向外（摄像头那面）
    ★ 不同安装方向要改的就是这里：真机实测后按实际摆放调整轴的含义。
    """
    if ax is None or ay is None or az is None:
        return "unknown", POSTURE_LABEL["unknown"], POSTURE_EN["unknown"], "读数缺失"
    mag = math.sqrt(ax * ax + ay * ay + az * az)
    if mag < 1e-6:
        return ("unknown", POSTURE_LABEL["unknown"], POSTURE_EN["unknown"],
                "合加速度≈0：自由落体或传感器未就绪，方向无从谈起")

    ux, uy, uz = ax / mag, ay / mag, az / mag
    mx, my, mz = abs(ux), abs(uy), abs(uz)

    if max(mx, my, mz) < POSTURE_MAJOR_AXIS:
        return ("tilted", POSTURE_LABEL["tilted"], POSTURE_EN["tilted"],
                "没有哪一轴占绝对主导，设备处于斜放状态")
    if mz >= mx and mz >= my:
        # ★ 方向：这里必须把「能确定的」和「只能假设的」分开写，否则很容易写反。
        #
        # 【能 100% 确定的部分】
        #   静止时加速度计测的不是"重力本身"，而是**支撑力**（比力 f = a − g，a=0）
        #   —— 所以它的读数指向**天空**。这正是 |a| 恒为 1 g、与摆放姿态无关的原因。
        #   于是：**az > 0 ⟺ 传感器的 +Z 轴朝上（背离地面）**。
        #   这一条与芯片怎么贴装无关，任何时候都成立。
        #
        # 【只能假设的部分】
        #   "+Z 轴对应板子的哪一面"取决于芯片在板上的贴装方向。
        #   乐鑫**没有公布这个信息**，我们手上也没有板子可以实测。
        #   所以它被提成一个显式常量（Z_UP_IS_FRONT_FACE），数值照算，只有文案跟着变。
        #   拿到板子后平放一次：看串口 az 是正还是负、当时朝上的是哪一面，
        #   若与本约定相反，改那一行即可，其余代码一行都不用动。
        #
        # 【为什么这个错特别难发现】
        #   写反了的话**所有数字都对、只有文案错**。自测若只断言 posture 这个 key，
        #   永远抓不到 —— 所以自测里连"说明文字里写没写明哪一面"也一起断言了。
        if (uz > 0) == Z_UP_IS_FRONT_FACE:
            side = "正面朝上（摄像头那面向上）"
        else:
            side = "背面朝上（摄像头那面朝下）"
        return "flat", POSTURE_LABEL["flat"], POSTURE_EN["flat"], side
    if my >= mx:
        return ("upright", POSTURE_LABEL["upright"], POSTURE_EN["upright"],
                "竖立，重力落在板面内的长边方向")
    return ("side_edge", POSTURE_LABEL["side_edge"], POSTURE_EN["side_edge"],
            "竖立，重力落在板面内的短边方向")


def tilt_angles(ax: float, ay: float, az: float) -> tuple:
    """由重力向量反算 (俯仰 pitch, 横滚 roll)，单位度。

    这两个角是"设备相对于重力"的倾角，所以加速度计就能算。
    航向 Yaw 不在这里 —— 它相对于重力是无关量，见上面那段说明。
    """
    if ax is None or ay is None or az is None:
        return None, None
    pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    roll = math.degrees(math.atan2(ay, az))
    return round(pitch, 1), round(roll, 1)


def parse_samples(raw) -> list:
    """校验并规整 /api/waveform 的 samples 字段。

    这里刻意"宁可拒收也不猜"：
      - 样本不是三元组 → 拒（说明板端协议不对，猜一个轴补 0 会把问题藏起来）
      - 计数超出 int16 → 拒（脏数据，不是"量程大"）
    返回 (样本列表, 错误说明)；出错时样本列表为 None。
    """
    if not isinstance(raw, list):
        return None, "samples 必须是数组"
    if not raw:
        return None, "samples 不能为空"
    if len(raw) > WAVE_MAX_BATCH:
        return None, "单批样本数 %d 超过上限 %d" % (len(raw), WAVE_MAX_BATCH)
    out = []
    for i, s in enumerate(raw):
        if not isinstance(s, (list, tuple)) or len(s) != 3:
            return None, "第 %d 个样本不是 [x,y,z] 三元组" % i
        trip = []
        for v in s:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None, "第 %d 个样本含非数值" % i
            iv = int(v)
            if abs(iv) > WAVE_MAX_ABS_RAW:
                return None, "第 %d 个样本计数 %s 超出 ±%d" % (i, iv, WAVE_MAX_ABS_RAW)
            trip.append(iv)
        out.append(trip)
    return out, None


def samples_to_text(samples: list) -> str:
    """紧凑成 "x,y,z;x,y,z;…" —— 比 JSON 数组省掉方括号和引号，解析也更快。"""
    return ";".join("%d,%d,%d" % (s[0], s[1], s[2]) for s in samples)


def samples_from_text(text: str) -> list:
    out = []
    for part in (text or "").split(";"):
        if not part:
            continue
        try:
            x, y, z = part.split(",")
            out.append([int(x), int(y), int(z)])
        except ValueError:
            continue
    return out


def wave_row_out(row) -> dict:
    """把一行 wave_batches 变成网页能直接用的结构。

    ★ 时间口径：`received_at` 是**服务端自己的钟**，`t_first`/`t_last` 是板子报的。
      两个都给出，且明确标注 —— 沿用第 3 周的铁律：
      **服务端绝不拿板端报的时间当判定依据。**
    """
    d = row_to_dict(row)
    d.pop("samples", None)          # 单条输出不带整批样本，太重
    # 老库补列后旧行的 dropped 是 NULL。这里归一成 0 再输出 ——
    # 让前端拿到一个确定的数，而不是显示成 "None" 让人以为是坏数据。
    d["dropped"] = int(d.get("dropped") or 0)
    d["posture_label"] = POSTURE_LABEL.get(d.get("posture"), "未知")
    d["posture_en"] = POSTURE_EN.get(d.get("posture"), "Unknown")
    d["yaw"] = None                 # 恒为 None：本板测不到，见 YAW_NOTE
    d["yaw_note"] = YAW_NOTE
    d["clock_sources"] = {
        "device": "t_first / t_last（板端时钟，只作参考，服务端不采信）",
        "server": "received_at（服务端时钟，判定一律用它）",
    }
    return d


def _wave_batches_contiguous(rows) -> bool:
    """这几批是不是「同一开机 + 批号逐批 +1 + 采样率一致 + 批间没丢样本」。

    成立时，时间轴可以**完全由 (n_samples, hz) 反推出来**，一个时钟都不用 ——
    这正是本板 SNTP 对不上时（实测常态）唯一还准的摆法。
    任一条件不成立就返回 False：宁可退回更差、但不会说谎的那条路，
    也不要拿一个"看起来很顺"的时间轴去骗人。
    """
    if len(rows) < 2:
        return False
    hz = rows[-1]["hz"]
    if not hz or hz < 1:
        return False
    if len({r["boot_id"] for r in rows}) != 1:
        return False                    # 中间重启过：batch_seq 会归零，不能当连续
    for i, r in enumerate(rows):
        if r["hz"] != hz:
            return False
        if r["dropped"]:
            return False                # 板端如实报了丢样本 —— 累加就不成立了
        if r["batch_seq"] is None:
            return False
        if i > 0 and r["batch_seq"] != rows[i - 1]["batch_seq"] + 1:
            return False
    return True


def purge_wave_batches(conn: sqlite3.Connection) -> int:
    """每设备只保留最近 WAVE_KEEP_BATCHES 批。

    和第 2 周的原图配额是同一个思路，但**结论相反**：
      原图是"证据" → 元数据永久留，只清原图；
      波形是"过程" → 留够复现窗口就行，老批直接删。
    分清"什么是证据、什么是过程"，才不会一刀切地全留或全删。
    """
    total = 0
    devs = [r[0] for r in conn.execute(
        "SELECT DISTINCT device_id FROM wave_batches").fetchall()]
    for dev in devs:
        n = conn.execute(
            "SELECT COUNT(*) FROM wave_batches WHERE device_id=?", (dev,)
        ).fetchone()[0]
        if n <= WAVE_KEEP_BATCHES:
            continue
        conn.execute(
            "DELETE FROM wave_batches WHERE device_id=? AND id NOT IN "
            "(SELECT id FROM wave_batches WHERE device_id=? "
            " ORDER BY id DESC LIMIT ?)",
            (dev, dev, WAVE_KEEP_BATCHES),
        )
        total += n - WAVE_KEEP_BATCHES
    if total:
        conn.commit()
    return total


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

    # ---------- 异常兜底 ----------
    def _handle_unexpected(self, method: str) -> None:
        """任何 handler 抛异常都要留下痕迹。

        【为什么必须有这个】
        之前踩过：handler 里抛异常 → HTTP 层直接断连接，客户端只看到
        `RemoteDisconnected: Remote end closed connection without response`，
        服务端这边连一行日志都没有，只能靠猜。同类事故还有 `_send_json`
        漏写响应体（服务端打印 200，客户端 IncompleteRead）——
        共同点是「错误发生在响应写出之后/之外」，所以必须在分发层兜住。
        """
        tb = traceback.format_exc()
        print("[%s] !! %s 处理请求时未捕获异常：\n%s" % (now_iso(), method, tb),
              flush=True)
        try:
            self._send_json({"error": "服务器内部错误",
                             "detail": tb.strip().splitlines()[-1],
                             "hint": "详见服务端日志"}, 500)
        except Exception:      # 响应已经开始写了就救不回来了
            pass

    # ---------- GET ----------
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:                              # noqa: BLE001
            self._handle_unexpected("GET")

    def _route_get(self):
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
        elif path == "/api/frames":
            self._handle_gallery(q)
        elif path == "/api/frames/image":
            self._handle_frame_image_by_id(q)
        elif path == "/api/command/poll":
            self._handle_command_poll(q)
        elif path == "/api/command/status":
            self._handle_command_status(q)
        elif path == "/api/command/frame":
            self._handle_command_frame(q)
        elif path == "/api/help":
            self._handle_help_list(q)
        elif path == "/api/help/poll":
            self._handle_help_poll(q)
        elif path == "/api/ask/health":
            self._handle_ask_health()
        elif path == "/api/waveform":
            self._handle_waveform_get(q)
        elif path == "/api/attitude":
            self._handle_attitude(q)
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
        # 画廊要用的派生信息：哈希（永久留痕）、字节数、分辨率、来源。
        digest = sha256_hex(data)
        width, height = jpeg_size(data)
        source = (self.headers.get("X-Source") or "").strip()
        if not source:
            # 没显式声明就按有无 request_id 推断：带 request_id 一定是网页点出来的。
            source = "web_manual" if request_id else "periodic"
        with _db_lock:
            conn = get_db()
            try:
                cur = conn.execute(
                    "INSERT INTO frames(device_id, ts_server, filename, bytes, "
                    "request_id, capture_ts, boot_id, seq, ts_device, "
                    "sha256, size_bytes, width, height, source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id or None, ts, fname, len(data),
                     request_id or None, capture_ts or None,
                     boot_id or None, seq, ts_device or None,
                     digest, len(data), width, height, source),
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
                         "sha256": digest, "width": width, "height": height,
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

    # ---------- 第2周：照片画廊（元数据永久保留，原图按配额清理） ----------
    def _handle_gallery(self, q: dict) -> None:
        """画廊数据源：按时间倒序列出帧元数据（含哈希、是否已按配额清理）。

        对应计划书 4.5 —— 原图可能被清掉，但这一行永远在，
        所以画廊里「已清理」的卡片仍然带着 sha256 与拍摄时间，依旧可追溯。
        """
        device_id = (q.get("device_id") or [""])[0]
        try:
            limit = int((q.get("limit") or ["60"])[0])
        except ValueError:
            limit = 60
        limit = max(1, min(500, limit))
        conn = get_db()
        try:
            sql = ("SELECT f.*, c.state AS command_state FROM frames f "
                   "LEFT JOIN commands c ON c.request_id = f.request_id")
            args = []
            if device_id:
                sql += " WHERE f.device_id=?"
                args.append(device_id)
            sql += " ORDER BY f.id DESC LIMIT ?"
            args.append(limit)
            items = []
            for r in conn.execute(sql, args).fetchall():
                items.append({
                    "id": r["id"],
                    "device_id": r["device_id"],
                    "ts_server": r["ts_server"],
                    "capture_ts": r["capture_ts"],
                    "request_id": r["request_id"],
                    "command_state": r["command_state"],
                    "source": r["source"] or "unknown",
                    "bytes": r["size_bytes"] if r["size_bytes"] is not None else r["bytes"],
                    "width": r["width"] or 0,
                    "height": r["height"] or 0,
                    "sha256": r["sha256"],
                    "purged": bool(r["purged_at"]),
                    "purged_at": r["purged_at"],
                })
            stat = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN purged_at IS NULL THEN 1 ELSE 0 END) AS kept, "
                "SUM(CASE WHEN purged_at IS NULL "
                "         THEN COALESCE(size_bytes, bytes, 0) ELSE 0 END) AS kept_bytes "
                "FROM frames"
            ).fetchone()
            total = stat["total"] or 0
            kept = stat["kept"] or 0
            self._send_json({
                "frames": items,
                "storage": {"total": total, "kept": kept, "purged": total - kept,
                            "kept_bytes": stat["kept_bytes"] or 0,
                            "retention_days": RETENTION_DAYS},
                "server_now": now_iso(),
            })
        finally:
            conn.close()

    def _handle_frame_image_by_id(self, q: dict) -> None:
        """按帧 id 取原图。已被配额清理的帧明确回 404 + 哈希，而不是含糊的 500。"""
        try:
            fid = int((q.get("id") or [""])[0])
        except ValueError:
            self._send_json({"error": "id 必须是整数"}, 400)
            return
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM frames WHERE id=?", (fid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            self._send_json({"error": "没有这一帧"}, 404)
            return
        if row["purged_at"]:
            self._send_json({"error": "该帧原图已按配额清理",
                             "sha256": row["sha256"],
                             "purged_at": row["purged_at"]}, 404)
            return
        path = guard_snapshot_path(row["filename"])
        if not path or not os.path.exists(path):
            self._send_json({"error": "原图文件缺失", "sha256": row["sha256"]}, 404)
            return
        self._send_image_file(path)

    # ---------- 第3周：按键求助事件（三层状态） ----------
    def _handle_help_submit(self) -> None:
        """板子发起 / 取消一次教学求助测试消息。

        服务端在这里只做两件事：
          1) **如实记录**板端自报的 device_state 与板端时刻（不采信、不修改、不代填）；
          2) 打上**自己的** received_at —— 这是「VPS 已接收」唯一的证据来源。
        """
        data = self._read_json_body()
        if data is None:
            return
        device_id = str(data.get("device_id") or "").strip()
        event_id = str(data.get("event_id") or "").strip()
        action = str(data.get("action") or "request").strip()
        if not device_id or not event_id:
            self._send_json({"error": "device_id 与 event_id 均为必填"}, 400)
            return
        if action not in ("request", "cancel"):
            self._send_json({"error": "action 只能是 request 或 cancel"}, 400)
            return

        now = now_iso()      # ★ 服务端自己的钟，与板端上报的时间戳分开存
        with _db_lock:
            conn = get_db()
            try:
                row = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                   (event_id,)).fetchone()
                if action == "request":
                    if row is None:
                        conn.execute(
                            "INSERT INTO help_events(event_id, device_id, kind, "
                            "device_state, server_state, viewer_state, pressed_at, "
                            "local_ack_at, sent_at, received_at, boot_id, seq, "
                            "updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (event_id, device_id,
                             str(data.get("kind") or "teach_help_test"),
                             # 板端自报的状态：原样记录，服务端不替它编
                             str(data.get("device_state") or HELP_DEV_LOCAL_ACKED),
                             HELP_SRV_RECEIVED, HELP_VWR_PENDING,
                             str(data.get("pressed_at") or ""),
                             str(data.get("local_ack_at") or ""),
                             now, now,
                             str(data.get("boot_id") or ""), data.get("seq"), now))
                        conn.commit()
                    else:
                        # 同一条事件重复上报：只在未终结时刷新板端字段，不覆盖服务端判定
                        if row["server_state"] not in (HELP_SRV_CANCELLED,
                                                       HELP_SRV_EXPIRED):
                            conn.execute(
                                "UPDATE help_events SET device_state=?, pressed_at=?, "
                                "local_ack_at=?, updated_at=? WHERE event_id=?",
                                (str(data.get("device_state") or row["device_state"]),
                                 str(data.get("pressed_at") or row["pressed_at"]),
                                 str(data.get("local_ack_at") or row["local_ack_at"]),
                                 now, event_id))
                            conn.commit()
                    out = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                       (event_id,)).fetchone()
                    self._send_json({"ok": True, "help": help_row_out(out)}, 201)
                    return

                # ---- action == "cancel" ----
                if row is None:
                    self._send_json({"error": "没有这条求助事件，无法取消"}, 404)
                    return
                if row["viewer_state"] == HELP_VWR_ANSWERED:
                    # 已经有人回应过了。回应是既成事实，不能被撤销 ——
                    # 否则「查看者回应」这条证据就可以被发起方单方面抹掉。
                    self._send_json(
                        {"error": "该求助已被回应，不能再取消",
                         "help": help_row_out(row)}, 409)
                    return
                if row["server_state"] == HELP_SRV_CANCELLED:
                    # 幂等：重复取消返回当前状态，不算错误
                    self._send_json({"ok": True, "help": help_row_out(row),
                                     "note": "该求助此前已取消"}, 200)
                    return
                conn.execute(
                    "UPDATE help_events SET server_state=?, device_state=?, "
                    "cancelled_at=?, cancelled_by=?, cancel_reason=?, updated_at=? "
                    "WHERE event_id=?",
                    (HELP_SRV_CANCELLED,
                     str(data.get("device_state") or HELP_DEV_FAILED),
                     now, str(data.get("cancelled_by") or "device"),
                     str(data.get("reason") or ""), now, event_id))
                conn.commit()
                out = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                   (event_id,)).fetchone()
                self._send_json({"ok": True, "help": help_row_out(out)}, 200)
            finally:
                conn.close()

    def _handle_help_poll(self, q: dict) -> None:
        """板子轮询：查看者回应了吗？被别人取消了吗？

        板子只需要**看**，不需要猜 —— 回应与否完全由服务端这条记录说话。
        """
        device_id = (q.get("device_id") or [""])[0]
        event_id = (q.get("event_id") or [""])[0]
        if not device_id:
            self._send_json({"error": "device_id 为必填"}, 400)
            return
        conn = get_db()
        try:
            if event_id:
                row = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                   (event_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM help_events WHERE device_id=? "
                    "ORDER BY received_at DESC, event_id DESC LIMIT 1",
                    (device_id,)).fetchone()
            self._send_json({"help": help_row_out(row) if row else None,
                             "server_now": now_iso()})
        finally:
            conn.close()

    # ---------- 第4周：自然语言问答（受限工具 + 运行时模型）----------
    def _handle_ask_health(self) -> None:
        """网页用它判断"运行时语言服务到底在不在"，而不是让用户瞎等。"""
        if nl_agent is None:
            self._send_json({"ok": False, "error": NL_IMPORT_ERR,
                             "hint": "nl_agent.py 没能导入，自然语言功能不可用"}, 200)
            return
        info = nl_agent.model_available()
        info["hint"] = ("运行时模型已就绪" if info.get("ok")
                        else "Ollama 在跑但没找到指定模型，可先 ollama pull")
        self._send_json(info, 200)

    def _handle_ask(self) -> None:
        """一句话进来，一个带证据的回答出去。

        注意这里**不做任何"帮模型兜底"的事** —— 模型说什么、工具查到什么，
        原样返回（含 guardrails 标记）。服务端替模型编答案，就等于把
        "无证据不报成功"这条底线拆了。
        """
        data = self._read_json_body()
        if data is None:
            return
        question = str(data.get("question") or "").strip()
        if not question:
            self._send_json({"error": "question 为必填"}, 400)
            return
        if len(question) > 500:
            self._send_json({"error": "问题太长了（上限 500 字）"}, 400)
            return
        if nl_agent is None:
            self._send_json({"error": "运行时语言服务不可用",
                             "detail": NL_IMPORT_ERR}, 503)
            return
        # 让工具层用**本进程认准的**数据库，避免两边 DATA_DIR 不一致
        # 导致"网页查得到、问答查不到"这种莫名其妙的差异。
        nl_agent.DB_PATH = DB_PATH
        try:
            res = nl_agent.ask(question)
        except Exception as e:                              # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": "调用运行时模型失败：%s: %s"
                                      % (type(e).__name__, e)}, 502)
            return
        print("[%s] 自然语言问答：%r → 工具 %d 次，守卫 %s"
              % (now_iso(), question[:40], len(res.get("tool_calls") or []),
                 res.get("guardrails") or "未触发"), flush=True)
        self._send_json(res, 200)

    def _handle_help_list(self, q: dict) -> None:
        """网页：列出求助事件（新→旧）。"""
        device_id = (q.get("device_id") or [""])[0]
        try:
            limit = int((q.get("limit") or ["20"])[0])
        except ValueError:
            limit = 20
        limit = max(1, min(200, limit))
        conn = get_db()
        try:
            if device_id:
                rows = conn.execute(
                    "SELECT * FROM help_events WHERE device_id=? "
                    "ORDER BY received_at DESC, event_id DESC LIMIT ?",
                    (device_id, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM help_events "
                    "ORDER BY received_at DESC, event_id DESC LIMIT ?",
                    (limit,)).fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) c FROM help_events WHERE viewer_state=? "
                "AND server_state=?", (HELP_VWR_PENDING, HELP_SRV_RECEIVED)
            ).fetchone()["c"]
            self._send_json({
                "helps": [help_row_out(r) for r in rows],
                "pending_count": pending,
                "server_now": now_iso(),
            })
        finally:
            conn.close()

    def _handle_help_answer(self) -> None:
        """查看者（网页前的人）回应一条求助。

        这是「③ 查看者回应」唯一的产生方式 —— 服务端、板端都不能代替它发生。
        """
        data = self._read_json_body()
        if data is None:
            return
        event_id = str(data.get("event_id") or "").strip()
        if not event_id:
            self._send_json({"error": "event_id 为必填"}, 400)
            return
        answered_by = str(data.get("answered_by") or "viewer").strip() or "viewer"
        answer_text = str(data.get("answer_text") or "").strip()

        now = now_iso()
        with _db_lock:
            conn = get_db()
            try:
                row = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                   (event_id,)).fetchone()
                if row is None:
                    self._send_json({"error": "没有这条求助事件"}, 404)
                    return
                # 先确认事件存在，再校验回应内容。
                # 顺序反过来的话，"回应一个不存在的求助"会得到 400「内容为空」——
                # 把一个"资源不存在"报成了"参数不合法"，排查时会指错方向。
                if not answer_text:
                    self._send_json({"error": "回应内容不能为空（空回应等于没回应）"},
                                    400)
                    return
                if row["server_state"] == HELP_SRV_CANCELLED:
                    self._send_json({"error": "该求助已被取消，不能再回应",
                                     "help": help_row_out(row)}, 409)
                    return
                if row["server_state"] == HELP_SRV_EXPIRED:
                    self._send_json({"error": "该求助已过期，不能再回应",
                                     "help": help_row_out(row)}, 409)
                    return
                if row["viewer_state"] == HELP_VWR_ANSWERED:
                    self._send_json({"error": "该求助已经回应过了",
                                     "help": help_row_out(row)}, 409)
                    return
                conn.execute(
                    "UPDATE help_events SET viewer_state=?, answered_at=?, "
                    "answered_by=?, answer_text=?, updated_at=? WHERE event_id=?",
                    (HELP_VWR_ANSWERED, now, answered_by, answer_text, now, event_id))
                conn.commit()
                out = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                   (event_id,)).fetchone()
                self._send_json({"ok": True, "help": help_row_out(out)}, 200)
            finally:
                conn.close()

    def _handle_help_cancel_by_viewer(self) -> None:
        """查看者取消一条求助（与板端按键取消走同一张表，但 cancelled_by 不同）。"""
        data = self._read_json_body()
        if data is None:
            return
        event_id = str(data.get("event_id") or "").strip()
        if not event_id:
            self._send_json({"error": "event_id 为必填"}, 400)
            return
        now = now_iso()
        with _db_lock:
            conn = get_db()
            try:
                row = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                   (event_id,)).fetchone()
                if row is None:
                    self._send_json({"error": "没有这条求助事件"}, 404)
                    return
                if row["viewer_state"] == HELP_VWR_ANSWERED:
                    self._send_json({"error": "该求助已被回应，不能再取消",
                                     "help": help_row_out(row)}, 409)
                    return
                if row["server_state"] == HELP_SRV_CANCELLED:
                    self._send_json({"ok": True, "help": help_row_out(row),
                                     "note": "该求助此前已取消"}, 200)
                    return
                conn.execute(
                    "UPDATE help_events SET server_state=?, cancelled_at=?, "
                    "cancelled_by=?, cancel_reason=?, updated_at=? WHERE event_id=?",
                    (HELP_SRV_CANCELLED, now, "viewer",
                     str(data.get("reason") or "viewer_cancelled"), now, event_id))
                conn.commit()
                out = conn.execute("SELECT * FROM help_events WHERE event_id=?",
                                   (event_id,)).fetchone()
                self._send_json({"ok": True, "help": help_row_out(out)}, 200)
            finally:
                conn.close()

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

    # ---------- 传感器示波器：三轴波形 + 姿态 ----------
    def _handle_waveform_post(self) -> None:
        """板端批量上传一批采样。

        板端是**攒够一批再发**的（默认 20Hz × 100 点 = 5 秒一批），
        不是一点一发 —— 理由见文件顶部 WAVE_MAX_BATCH 那段注释。
        """
        data = self._read_json_body()
        if data is None:
            return
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            self._send_json({"error": "device_id 为必填字段"}, 400)
            return

        samples, err = parse_samples(data.get("samples"))
        if err:
            self._send_json({"error": err, "hint": "拒收而不是猜：协议不对就该报错，"
                                                  "猜一个值补上会把问题藏起来"}, 400)
            return

        # ★ 注意这几个 `_raw is None` 的写法，别顺手改成 `or 默认值`：
        #   `data.get("hz") or 20` 会把**合法的 0** 当成"没传"而悄悄替换掉，
        #   于是 hz=0 这种非法值一路畅通无阻地入库（自测就是抓到了这个）。
        #   同一个病根在板端也犯过一次（s_led_on 的三态 -1 被当布尔用）。
        #   **"没有值"和"值是 0"是两件事。**
        hz_raw = data.get("hz")
        try:
            hz = 20 if hz_raw is None or hz_raw == "" else int(hz_raw)
        except (TypeError, ValueError):
            hz = 0
        if hz < 1 or hz > WAVE_MAX_HZ:
            self._send_json({"error": "hz 必须在 1~%d 之间" % WAVE_MAX_HZ}, 400)
            return

        def num_or(raw_val, default):
            """只在"真的没传"时用默认值；传了 0 就老老实实拿 0 去校验。"""
            if raw_val is None or raw_val == "":
                return default
            try:
                return float(raw_val)
            except (TypeError, ValueError):
                return float("nan")

        lsb_per_g = num_or(data.get("lsb_per_g"), WAVE_DEFAULT_LSB_PER_G)
        calib = num_or(data.get("calib"), WAVE_DEFAULT_CALIB)
        # 因子不合理就退回默认值，并如实告知 —— 不静默用一个瞎猜的系数，
        # 否则网页上的 g 值会整体偏移，而没人知道为什么。
        warn = None
        if not (1.0 <= lsb_per_g <= 100000.0):
            warn = "lsb_per_g=%s 不合理，已按默认 %s 处理" % (lsb_per_g,
                                                             WAVE_DEFAULT_LSB_PER_G)
            lsb_per_g = WAVE_DEFAULT_LSB_PER_G
        if not (0.01 <= calib <= 100.0):
            warn = ((warn + "；") if warn else "") + \
                   "calib=%s 不合理，已按默认 %s 处理" % (calib, WAVE_DEFAULT_CALIB)
            calib = WAVE_DEFAULT_CALIB
        scale = 1.0 / (lsb_per_g * calib)          # g = raw × scale

        # 批内最后一个样本 = "当前值"，用它算姿态（姿态只看当下，不看历史）
        lx, ly, lz = samples[-1]
        ax, ay, az = lx * scale, ly * scale, lz * scale
        acc_mag = math.sqrt(ax * ax + ay * ay + az * az)
        posture, p_label, p_en, p_note = classify_posture(ax, ay, az)
        pitch, roll = tilt_angles(ax, ay, az)

        # ★ 服务端时钟：判定一律用它。板子报的 t_first/t_last 原样存着，但不参与判定。
        received_at = now_iso()

        # 板端如实上报"攒批期间丢了多少样本"。收到就原样存下来 ——
        # 这个数决定了服务端能不能用 batch_seq 反推时间轴（见 _handle_waveform_get）。
        try:
            dropped = int(data.get("dropped") or 0)
        except (TypeError, ValueError):
            dropped = 0
        if dropped < 0:
            dropped = 0

        with _db_lock:
            conn = get_db()
            try:
                cur = conn.execute(
                    "INSERT INTO wave_batches(device_id, boot_id, batch_seq, hz, "
                    "n_samples, lsb_per_g, calib, scale, t_first, t_last, dropped, "
                    "received_at, samples, "
                    "ax_raw, ay_raw, az_raw, ax, ay, az, acc_mag, pitch, roll, "
                    "posture, posture_note, yaw, yaw_note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id,
                     str(data.get("boot_id") or ""),
                     int(data.get("batch_seq") or 0),
                     hz, len(samples), lsb_per_g, calib, scale,
                     str(data.get("t_first") or ""),
                     str(data.get("t_last") or ""),
                     dropped,
                     received_at,
                     samples_to_text(samples),
                     lx, ly, lz, round(ax, 4), round(ay, 4), round(az, 4),
                     round(acc_mag, 4), pitch, roll,
                     posture, p_note,
                     None,                       # yaw 恒为 NULL：本板测不到
                     YAW_NOTE),
                )
                conn.commit()
                row_id = cur.lastrowid
            finally:
                conn.close()

        self._send_json({
            "ok": True, "id": row_id, "device_id": device_id,
            "n_samples": len(samples), "hz": hz, "dropped": dropped,
            "lsb_per_g": lsb_per_g, "calib": calib, "scale": scale,
            "received_at": received_at,
            "posture": posture, "posture_label": p_label, "posture_en": p_en,
            "posture_note": p_note,
            "acc_mag": round(acc_mag, 4), "pitch": pitch, "roll": roll,
            "yaw": None, "yaw_note": YAW_NOTE,
            "warn": warn,
        }, 201)

    def _handle_waveform_get(self, q: dict) -> None:
        """取最近 N 批，拼成一条连续波形给网页画。

        横轴用**相对现在的秒数**（负数往过去）—— 和参考产品那个 `-9s … 现` 一致。
        这样网页不用管绝对时间，时间轴永远贴着"现在"。
        """
        device_id = (q.get("device_id") or [""])[0]
        if not device_id:
            self._send_json({"error": "device_id 为必填参数"}, 400)
            return
        try:
            batches = int((q.get("batches") or ["6"])[0])
        except ValueError:
            batches = 6
        batches = max(1, min(batches, 120))

        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM wave_batches WHERE device_id=? "
                "ORDER BY id DESC LIMIT ?", (device_id, batches)
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            self._send_json({"ok": True, "device_id": device_id, "batches": 0,
                             "n_samples": 0, "series": {"t": [], "ax": [], "ay": [], "az": []},
                             "latest": None,
                             "hint": "这台设备还没有波形数据。"
                                     "板端要开着 WAVE_ENABLE 才会按批上传。"}, 200)
            return

        rows = list(reversed(rows))          # 老的在前，拼出来就是时间正序
        hz = rows[-1]["hz"] or 20
        step = 1.0 / hz if hz else 0.05

        # ---- 时间轴锚点怎么选（三条路径，按可信度从高到低）----
        #
        # 【为什么这件事值得单独写一段 —— 这是波形特有的需求】
        #   波形要的是"样本之间隔多久"，是**相对间隔**，不是绝对时刻。
        #   服务端接收时刻会被网络抖动污染：链路卡一下、3 批几乎同时到，
        #   按接收时刻摆就全挤在一起，看着像"数据乱了"，其实只是到得晚。
        #
        #   路径 ①（最优）板端采样时刻 t_last
        #     板子的钟可能没对时，但**钟差是个常数**，两条时刻做差之后自动抵消，
        #     相对间隔反而是准的。
        #   路径 ②（次优）批号连续 + 采样率已知 → 直接反推
        #     这块板子的 SNTP 实测经常对不上时，t_last 会写成
        #     "uptime+12.345s(time_not_synced)" 这种**不可解析**的串，
        #     所以路径 ① 在这台设备上其实是走不到的 —— 没有路径 ② 就等于
        #     "只有我造的演示数据好看，真板子反而退化成最差的那条路"。
        #     批号每批 +1 说明批间没断开，每批时长 = n/hz，从最新一批往回累加即可。
        #     **前提是批间没丢样本**：板端会如实上报 dropped，不为 0 就不能这么推。
        #   路径 ③（兜底）服务端接收时刻 received_at
        #
        # ★ 注意这三条都不违反"服务端不采信板端时间"那条铁律：
        #   判定（入库时刻、证据校验、超时归因）一律仍用 received_at；
        #   这里只是拿它摆**相对间隔**，而且用了哪条路、为什么，都在返回值里写明。
        dev_times = [parse_iso(r["t_last"]) for r in rows]
        ends = None                                   # 每批末样本相对"现在"的秒数
        if all(d is not None for d in dev_times) and len(set(dev_times)) > 1:
            axis_source = "device"
            axis_note = ("时间轴用板端采样时刻：钟差是常数，做差后自动抵消，相对间隔准"
                         "（判定仍用服务端 received_at）")
            # 老批离"现在"更远 → 更负。符号别写反：是 (该批 − 最新批)。
            ends = [(d - dev_times[-1]).total_seconds() for d in dev_times]
        elif _wave_batches_contiguous(rows):
            axis_source = "derived"
            axis_note = ("板端没打可解析的时间戳，但批号连续且批间无丢样本，"
                         "按采样率反推时间轴（比服务端接收时刻准，不受网络抖动影响）")
            ends = [0.0] * len(rows)
            acc = 0.0
            for k in range(len(rows) - 1, 0, -1):
                acc += rows[k]["n_samples"] * step
                ends[k - 1] = -acc
        else:
            axis_source = "server"
            # 降级原因要写准 —— "批号不连续"和"只有一批"是两回事，
            # 笼统写一句会让人去查一个根本不存在的丢批问题。
            if len(rows) < 2:
                reason = "只有一批数据，谈不上批间连续"
            elif any(r["dropped"] for r in rows):
                reason = "板端如实上报了丢样本（批间有洞），累加不成立"
            elif len({r["boot_id"] for r in rows}) != 1:
                reason = "中间重启过（boot_id 变了），批号会归零，不能当连续"
            else:
                reason = "批号有断档（中间有批没送达）"
            axis_note = ("%s，时间轴退回服务端接收时刻 —— 网络抖动会让间隔看起来不匀"
                         % reason)
            recv_times = [parse_iso(r["received_at"]) for r in rows]
            base = recv_times[-1]
            # 同上：老批 → 更负，所以是 (该批 − 最新批)，不是反过来。
            ends = [((d - base).total_seconds() if (base and d) else 0.0)
                    for d in recv_times]

        series_t, sx, sy, sz = [], [], [], []
        for row, end_at in zip(rows, ends):
            samples = samples_from_text(row["samples"])
            scale = row["scale"] or (1.0 / (WAVE_DEFAULT_LSB_PER_G * WAVE_DEFAULT_CALIB))
            n = len(samples)
            if n == 0:
                continue
            # 批内第 i 个样本：末样本落在 end_at，往前每隔 step 一个
            t0 = end_at - (n - 1) * step
            for i, (rx, ry, rz) in enumerate(samples):
                series_t.append(round(t0 + i * step, 3))
                sx.append(round(rx * scale, 4))
                sy.append(round(ry * scale, 4))
                sz.append(round(rz * scale, 4))

        detail = [{
            "id": r["id"], "batch_seq": r["batch_seq"], "n": r["n_samples"],
            "hz": r["hz"], "received_at": r["received_at"],
            "t_first": r["t_first"], "t_last": r["t_last"],
            "dropped": int(r["dropped"] or 0),
            "posture": r["posture"],
            "posture_label": POSTURE_LABEL.get(r["posture"], "未知"),
            "acc_mag": r["acc_mag"], "pitch": r["pitch"], "roll": r["roll"],
        } for r in rows]

        self._send_json({
            "ok": True, "device_id": device_id,
            "batches": len(rows), "hz": hz, "n_samples": len(series_t),
            "window_s": round((len(series_t) - 1) * step, 2),
            "server_now": now_iso(),
            "time_axis_source": axis_source,
            "time_axis_note": axis_note,
            "series": {"t": series_t, "ax": sx, "ay": sy, "az": sz},
            "latest": wave_row_out(rows[-1]),
            "batches_detail": detail,
        }, 200)

    def _handle_attitude(self, q: dict) -> None:
        """只要最新姿态 —— 3D 孪生面板高频轮询它，比拉整条波形轻得多。"""
        device_id = (q.get("device_id") or [""])[0]
        conn = get_db()
        try:
            if device_id:
                row = conn.execute(
                    "SELECT * FROM wave_batches WHERE device_id=? "
                    "ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM wave_batches ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row is None:
            self._send_json({"ok": True, "attitude": None,
                             "hint": "还没有波形数据，无法给出姿态"}, 200)
            return
        out = wave_row_out(row)
        out["ok"] = True
        out["server_now"] = now_iso()
        self._send_json({"ok": True, "attitude": out, "server_now": out["server_now"]}, 200)

    # ---------- POST ----------
    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:                              # noqa: BLE001
            self._handle_unexpected("POST")

    def _route_post(self):
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
        if path == "/api/help":
            self._handle_help_submit()
            return
        if path == "/api/help/answer":
            self._handle_help_answer()
            return
        if path == "/api/help/cancel":
            self._handle_help_cancel_by_viewer()
            return
        if path == "/api/ask":
            self._handle_ask()
            return
        if path == "/api/waveform":
            self._handle_waveform_post()
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


def sha256_hex(data: bytes) -> str:
    """整帧内容的 SHA-256。

    原图会被配额清掉，但哈希永久留在库里，用来回答「当时那张图到底是什么」。
    这是计划书 4.5 里点名的加分项：**图片可以过期，证据不可以过期**。
    """
    return hashlib.sha256(data).hexdigest()


def jpeg_size(data: bytes) -> tuple:
    """从 JPEG 字节流里解析出 (width, height)，解析不出就返回 (0, 0)。

    只认 SOF0~SOF3 / SOF5~SOF7 / SOF9~SOF11 这些帧头段，跳过 DHT/DQT/APPn 等无关段。
    画廊里拿它显示「分辨率」，解析失败显示成「—」，不影响入库。
    """
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return (0, 0)
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:   # 无载荷段
            i += 2
            continue
        if marker == 0xD9:                                     # EOI，后面没头了
            break
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if seg_len < 2:
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3,
                      0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB):
            h = (data[i + 5] << 8) | data[i + 6]    # 段内：精度1 + 高2 + 宽2
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h)
        i += 2 + seg_len
    return (0, 0)


def guard_snapshot_path(name: str) -> str:
    """把客户端给的文件名收敛到 snapshots/ 之内，挡掉 ../ 之类的目录穿越。"""
    base = os.path.basename((name or "").strip())
    if not base or base.startswith("."):
        return ""
    return os.path.join(SNAP_DIR, base)


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


def purge_expired_frames(conn) -> int:
    """按配额清理过期原图：删文件、置 purged_at，**元数据与哈希原样保留**。

    对应计划书 4.5「元数据永久留、原图按天清」：
    清完之后 /api/frames 仍能列出这一条（标成已清理），sha256 永远在，
    因此仍然能证明「当时的图是什么内容」——只是不再占存储。
    """
    cutoff = (datetime.now(TZ) - timedelta(days=RETENTION_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id, filename FROM frames "
        "WHERE purged_at IS NULL AND ts_server < ?", (cutoff,)
    ).fetchall()
    n = 0
    for row in rows:
        path = guard_snapshot_path(row["filename"])
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                # 只在「文件确实还在」时才算失败：某些环境删完才抛错（如回收站不可用），
                # 那种情况下文件已经没了，不该拖住 purged_at 的标记。
                if os.path.exists(path):
                    print("[%s] 清理原图失败，下轮重试: %s (%s)"
                          % (now_iso(), row["filename"], e), flush=True)
                    continue
        conn.execute("UPDATE frames SET purged_at = ? WHERE id = ?",
                     (now_iso(), row["id"]))
        n += 1
    if n:
        conn.commit()
    return n


def frame_purger() -> None:
    """后台配额清理线程：每 PURGE_INTERVAL_S 扫一次，把超期原图收走。"""
    while True:
        time.sleep(PURGE_INTERVAL_S)
        try:
            with _db_lock:
                conn = get_db()
                try:
                    n = purge_expired_frames(conn)
                finally:
                    conn.close()
            if n:
                print("[%s] 配额清理：%d 张原图超期（>%d 天）已清，元数据与哈希保留"
                      % (now_iso(), n, RETENTION_DAYS), flush=True)
        except Exception as e:      # 后台线程绝不能因异常退出
            print("[%s] 配额清理异常: %s" % (now_iso(), e), flush=True)


def wave_purger() -> None:
    """后台波形清理线程：每设备只留最近 WAVE_KEEP_BATCHES 批。

    和第 2 周的原图清理刻意分开成两个线程、两个周期：
      原图是"证据" → 留得久（默认 7 天），元数据永久；
      波形是"过程" → 留够复现窗口就行（默认 1 小时），老批直接删。
    合成一个清理器的话，早晚有人把两条策略搅在一起。
    """
    while True:
        time.sleep(WAVE_PURGE_INTERVAL_S)
        try:
            with _db_lock:
                conn = get_db()
                try:
                    n = purge_wave_batches(conn)
                finally:
                    conn.close()
            if n:
                print("[%s] 波形清理：%d 批超出保留窗口（每设备留最近 %d 批）已删"
                      % (now_iso(), n, WAVE_KEEP_BATCHES), flush=True)
        except Exception as e:      # 后台线程绝不能因异常退出
            print("[%s] 波形清理异常: %s" % (now_iso(), e), flush=True)


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
                    m = sweep_help_events(conn)     # 第3周：超时无人回应的求助
                finally:
                    conn.close()
            if n:
                print("[%s] 后台扫描：%d 条指令超时（转 EXPIRED / TIMEOUT）"
                      % (now_iso(), n), flush=True)
            if m:
                print("[%s] 后台扫描：%d 条求助超时无人回应（转 EXPIRED，"
                      "板端与查看者状态不动）" % (now_iso(), m), flush=True)
        except Exception as e:      # 后台线程绝不能因异常退出
            print("[%s] 后台扫描异常: %s" % (now_iso(), e), flush=True)


def main() -> None:
    init_db()
    threading.Thread(target=command_sweeper, daemon=True).start()
    threading.Thread(target=frame_purger, daemon=True).start()
    threading.Thread(target=wave_purger, daemon=True).start()
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
    print("  按键求助:  POST /api/help(发起/取消) | GET /api/help/poll(板端轮询)"
          " | GET /api/help(网页列表) | POST /api/help/answer"
          " | POST /api/help/cancel", flush=True)
    print("  传感器示波器: POST /api/waveform(板端批量上传) | GET /api/waveform(取波形)"
          " | GET /api/attitude(取姿态)", flush=True)
    if nl_agent is None:
        print("  自然语言:  ✗ 不可用（nl_agent.py 导入失败：%s）" % NL_IMPORT_ERR,
              flush=True)
    else:
        info = nl_agent.model_available()
        print("  自然语言:  %s | POST /api/ask | 模型 %s"
              % ("✓ 运行时模型已就绪" if info.get("ok")
                 else "△ 语言服务未就绪（%s）" % (info.get("error")
                                              or "模型未找到"), nl_agent.OLLAMA_MODEL),
              flush=True)
    print("=" * 60, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
