#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
传感器示波器 · 三轴波形 + 姿态判定 自测

本周（对标参考产品的「传感器示波器」页）要证明的三件事：

  1. **批量上传这条路走得通**：板端攒一批发一次，服务端按批存、网页按批取，
     取回来的样本和发出去的一模一样（往返一致，不是"大致对得上"）。
  2. ★ **姿态只用三轴加速度计就能判**：平放 / 竖立 / 侧立 / 自由倾斜四档，
     喂已知的重力方向进去，看它判得对不对。
     这是第 2 周那句"六轴 Gyro 与 3D 姿态都做不了"被更正后的落地验证。
  3. ★ **测不到的东西要如实为空**：yaw 永远是 None，并且带一句说明为什么。
     "没采到"和"测不了"是两回事，不能都留个空白让人猜。
  4. ★ **时间轴要能在这块真板子上站住**：本板 SNTP 实测经常对不上时，板端时间戳
     是不可解析的 —— 所以除了"读板端钟"和"读服务端钟"之外，还必须有一条
     **完全不需要任何时钟**的路（批号连续 + 采样率已知 → 反推）。
     自测里会验证：反推出来的轴与"板子对时了"那条路**逐点相同**。

另外守着几条老规矩：
  · 服务端绝不采信板端报的时间（t_first 造假不影响 received_at）
  · 脏数据**拒收而不是猜**（样本不是三元组就 400，不补个 0 蒙混过去）
  · 波形是"过程数据"：留够复现窗口就删，和原图那种"证据"策略不同

运行：python selftest_wave.py
（自起一个隔离实例，PORT=8014、DATA_DIR=server/tmpdata_wave_<时间戳>，跑完自删）
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
PORT = 8014
BASE = "http://127.0.0.1:%d" % PORT
TMP = os.path.join(HERE, "tmpdata_wave_%d" % int(time.time()))
PY = sys.executable
TZ = timezone(timedelta(hours=8))
DEV = "selftest-wave-dev"
# 本机若开着 HTTP 代理（Clash 之类），127.0.0.1 的请求也会被劫走 → 必须绕开
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 与板端 app_config.h 一致：g = raw / (1024 × 0.8078)
LSB_PER_G = 1024.0
CALIB = 0.8078
SCALE = 1.0 / (LSB_PER_G * CALIB)

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
        with OPENER.open(r, timeout=15) as resp:
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
    print("\n---- 服务端日志尾部 (%s) ----" % os.path.basename(path))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        for ln in lines[-n:]:
            print("  | " + ln)
    except OSError as e:
        print("  | (读不到日志：%s)" % e)
    print("---- 日志尾部结束 ----")


def g_to_raw(g):
    """把物理量 g 换成原始 ADC 计数（板端就是这么反着算的）。"""
    return int(round(g / SCALE))


def make_batch(gx, gy, gz, n=100, **kw):
    """造一批样本：全部点都用同一个重力方向（静止姿态），便于断言分类结果。"""
    raw = [g_to_raw(gx), g_to_raw(gy), g_to_raw(gz)]
    body = {
        "device_id": DEV,
        "boot_id": "selftest-boot1",
        "batch_seq": 1,
        "hz": 20,
        "lsb_per_g": LSB_PER_G,
        "calib": CALIB,
        "t_first": "2026-09-23T09:00:00.000+08:00",
        "t_last": "2026-09-23T09:00:05.000+08:00",
        "samples": [raw[:] for _ in range(n)],
    }
    body.update(kw)
    return body


def upload(body):
    return req("POST", "/api/waveform", body)


def main() -> int:
    os.environ["DATA_DIR"] = TMP
    # 把保留窗口压到 3 批，好让清理逻辑在几秒内就能被验证。
    # ★ 必须在这里（**导入 server 之前**）设好：WAVE_KEEP_BATCHES 是模块导入时读的环境变量，
    #   只在子进程 env 里设的话，自测进程自己 import 进来的那份仍是 720 —— 清理器就会空转。
    os.environ["WAVE_KEEP_BATCHES"] = "3"
    env = dict(os.environ, PORT=str(PORT), DATA_DIR=TMP, WAVE_KEEP_BATCHES="3")
    logpath = os.path.join(HERE, "tmpdata_wave_server.log")
    logf = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([PY, os.path.join(HERE, "server.py")], env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    try:
        if not wait_port():
            print("服务端未能启动，中止。")
            _dump_log(logpath)
            return 1

        # ---------------------------------------------------------------
        print("\n== 1. 批量上传一批（板端攒够 100 点发一次）==")
        st, res = upload(make_batch(0, 0, 1.0))      # 平放，正面朝上
        check("POST /api/waveform 返回 201", st == 201, st)
        check("入库样本数 = 上传样本数 100", res.get("n_samples") == 100,
              res.get("n_samples"))
        check("received_at 由服务端打上", bool(res.get("received_at")),
              res.get("received_at"))
        check("换算因子原样存下（lsb_per_g / calib 都在）",
              res.get("lsb_per_g") == LSB_PER_G and res.get("calib") == CALIB,
              "%s / %s" % (res.get("lsb_per_g"), res.get("calib")))

        # ---------------------------------------------------------------
        print("\n== 2. ★ 服务端不采信板端报的时间 ==")
        fake = (datetime.now(TZ) - timedelta(days=1)).isoformat(timespec="milliseconds")
        st, res2 = upload(make_batch(0, 0, 1.0, batch_seq=2,
                                     t_first=fake, t_last=fake))
        recv = datetime.fromisoformat(res2["received_at"])
        check("received_at 仍是服务端当前时间，没被带偏",
              abs((datetime.now(TZ) - recv).total_seconds()) < 60,
              res2["received_at"])
        st, g2 = req("GET", "/api/waveform?device_id=%s&batches=1" % DEV)
        check("板端那个假时刻被原样存着（服务端只存不改）",
              g2["latest"]["t_first"] == fake, g2["latest"]["t_first"])
        check("假时刻与服务端时刻确实分属两个字段（没被合并）",
              g2["latest"]["t_first"] != g2["latest"]["received_at"],
              "%s vs %s" % (g2["latest"]["t_first"], g2["latest"]["received_at"]))
        check("clock_sources 里明确写了「判定一律用服务端」",
              "服务端" in g2["latest"]["clock_sources"]["server"]
              and "不采信" in g2["latest"]["clock_sources"]["device"],
              g2["latest"]["clock_sources"])

        # ---------------------------------------------------------------
        print("\n== 3. ★ 姿态四档分类（喂已知重力方向，看它判得对不对）==")
        cases = [
            # ★ 方向别想当然：静止时加速度计测的是"支撑力"（读数指天空），
            #   所以 az>0 是 **+Z 轴朝上**；"+Z 对应板子哪一面"是另一回事，
            #   见 server.py 的 Z_UP_IS_FRONT_FACE 常量。
            ("平放·背面朝上（az>0，+Z 朝上）", (0, 0, 1.0), "flat", "背面朝上"),
            ("平放·正面朝上（az<0，+Z 朝下）", (0, 0, -1.0), "flat", "正面朝上"),
            ("竖直正面", (0, 1.0, 0), "upright", None),
            ("竖直正面（反向）", (0, -1.0, 0), "upright", None),
            ("侧边直立", (1.0, 0, 0), "side_edge", None),
            ("侧边直立（反向）", (-1.0, 0, 0), "side_edge", None),
            ("自由倾斜", (0.577, 0.577, 0.577), "tilted", None),
        ]
        for i, (label, g, want, note_kw) in enumerate(cases):
            st, r = upload(make_batch(*g, batch_seq=10 + i))
            ok = st == 201 and r.get("posture") == want
            check("%s → %s" % (label, want), ok,
                  "%s / %s" % (r.get("posture"), r.get("posture_label")))
            if note_kw:
                check("  %s 的说明里写明朝向（%s）" % (label, note_kw),
                      note_kw in (r.get("posture_note") or ""),
                      r.get("posture_note"))

        print("\n== 3b. 合加速度≈0 时如实判为「无法判定」，不硬猜一个姿态 ==")
        st, r = upload(make_batch(0, 0, 0, batch_seq=30))
        check("全零样本 → posture=unknown", r.get("posture") == "unknown",
              r.get("posture"))
        check("说明里点出原因（自由落体/未就绪）",
              "自由落体" in (r.get("posture_note") or ""), r.get("posture_note"))

        print("\n== 3c. ★「哪一面朝上」这个假设必须能一行翻转（而不是埋在逻辑里）==")
        # az>0 = +Z 轴朝上，这是物理上确定的；但"+Z 对应板子哪一面"取决于芯片贴装，
        # 乐鑫没公布、我们也没板子实测 —— 所以它是**假设**，就该摆在明面上，
        # 并且要能一行改掉。下面这条断言就是在验证那句注释没有吹牛。
        sys.path.insert(0, HERE)
        import server as srv          # noqa: E402
        base_note = srv.classify_posture(0, 0, 1.0)[3]
        check("默认约定下 az>0 → 背面朝上", "背面朝上" in base_note, base_note)
        try:
            srv.Z_UP_IS_FRONT_FACE = True
            flipped = srv.classify_posture(0, 0, 1.0)[3]
        finally:
            srv.Z_UP_IS_FRONT_FACE = False
        check("★ 把 Z_UP_IS_FRONT_FACE 改成 True，文案就整体翻转（真的只改一行）",
              "正面朝上" in flipped, flipped)
        check("翻转的只是文案，姿态分类 key 不变（数值不受影响）",
              srv.classify_posture(0, 0, 1.0)[0] == "flat")

        # ---------------------------------------------------------------
        print("\n== 4. 横滚/俯仰由重力反算；静止平放时应≈0 ==")
        st, r = upload(make_batch(0, 0, 1.0, batch_seq=40))
        check("平放时 pitch≈0", abs(r.get("pitch", 99)) < 1.0, r.get("pitch"))
        check("平放时 roll≈0", abs(r.get("roll", 99)) < 1.0, r.get("roll"))
        st, r = upload(make_batch(1.0, 0, 0, batch_seq=41))   # 侧立 90°
        check("侧立时 |pitch|≈90°", abs(abs(r.get("pitch", 0)) - 90) < 1.5,
              r.get("pitch"))

        # ---------------------------------------------------------------
        print("\n== 5. ★ 测不到的航向要如实为空，并且说明原因 ==")
        check("POST 返回里 yaw 为 None", r.get("yaw") is None, r.get("yaw"))
        check("POST 返回里带 yaw_note", "不可测" in (r.get("yaw_note") or ""),
              r.get("yaw_note"))
        st, g = req("GET", "/api/waveform?device_id=%s&batches=1" % DEV)
        check("GET /api/waveform 的 latest.yaw 也为 None",
              g["latest"].get("yaw") is None, g["latest"].get("yaw"))
        check("GET 的 latest 也带 yaw_note",
              "不可测" in (g["latest"].get("yaw_note") or ""),
              g["latest"].get("yaw_note"))
        st, a = req("GET", "/api/attitude?device_id=%s" % DEV)
        check("GET /api/attitude 同样 yaw=None + yaw_note",
              a["attitude"].get("yaw") is None
              and "不可测" in (a["attitude"].get("yaw_note") or ""),
              a["attitude"].get("yaw_note"))
        check("attitude 里三层时钟口径分开标注",
              set(a["attitude"].get("clock_sources", {}).keys()) == {"device", "server"},
              list(a["attitude"].get("clock_sources", {}).keys()))

        # ---------------------------------------------------------------
        print("\n== 6. ★ 往返一致：取回来的样本 = 发出去的样本 ==")
        n = 60
        ramp = [[g_to_raw(0.001 * i), g_to_raw(0.002 * i), g_to_raw(0.5)]
                for i in range(n)]
        st, r = upload({"device_id": DEV, "boot_id": "selftest-boot1",
                        "batch_seq": 99, "hz": 20,
                        "lsb_per_g": LSB_PER_G, "calib": CALIB,
                        "samples": ramp})
        check("带斜坡的批次上传成功", st == 201, st)
        st, g = req("GET", "/api/waveform?device_id=%s&batches=1" % DEV)
        ser = g["series"]
        check("取回的样本数一致", len(ser["ax"]) == n, len(ser["ax"]))
        # 逐个比对换算后的值（浮点比到 4 位，与服务端 round 的位数一致）
        ok_all, first_bad = True, ""
        for i in range(n):
            want = round(ramp[i][0] * SCALE, 4)
            if abs(ser["ax"][i] - want) > 1e-4:
                ok_all, first_bad = False, "第 %d 点 want=%s got=%s" % (
                    i, want, ser["ax"][i])
                break
        check("ax 序列逐个点都能对上（不是「大致对得上」）", ok_all, first_bad)
        check("最新值 = 斜坡最后一个点",
              abs(ser["ax"][-1] - round(ramp[-1][0] * SCALE, 4)) < 1e-4,
              ser["ax"][-1])

        # ---------------------------------------------------------------
        print("\n== 7. 时间轴：相对现在（负数往过去），单调递增，末端≈0 ==")
        t = ser["t"]
        check("t 单调递增", all(t[i] <= t[i + 1] for i in range(len(t) - 1)))
        check("末端 ≈ 0（贴着「现在」）", abs(t[-1]) < 1.0, t[-1])
        check("起点为负（往过去推）", t[0] < 0, t[0])
        check("采样间隔 ≈ 1/hz = 0.05s",
              all(abs((t[i + 1] - t[i]) - 0.05) < 1e-6 for i in range(len(t) - 1)),
              t[1] - t[0] if len(t) > 1 else None)

        # ---------------------------------------------------------------
        print("\n== 7b. ★ 时间轴三档：板端时刻 / 采样率反推 / 服务端接收时刻 ==")
        # 为什么要专门测这一组：
        #   上一版只做了两档 —— "优先板端采样时刻，否则退回服务端接收时刻"。
        #   但本板的 SNTP 实测经常对不上时，板端时间戳会写成
        #   "uptime+12.3s(time_not_synced)" 这种**不可解析**的串，
        #   也就是说"用板端时刻"这条最优路在这台设备上其实走不到。
        #   结果就是：我造的演示数据看着很漂亮，真板子反而退化成最差的那条路。
        #   所以补了中间一档：**批号连续 + 采样率已知 → 完全不需要任何时钟，直接反推**。
        AXIS_DEVS = ["%s-axis%d" % (DEV, i) for i in (1, 2, 3, 4)]
        AXIS_BASE = datetime(2026, 9, 23, 9, 0, 0, tzinfo=TZ)

        def axis_batches(dev, t_ok, seqs, dropped=0):
            """灌 n 批。t_ok=True 给可解析的板端时刻；False 给板子没对时时的写法。"""
            for k, sq in enumerate(seqs):
                body = {"device_id": dev, "boot_id": "axis-boot1",
                        "batch_seq": sq, "hz": 20, "dropped": dropped,
                        "lsb_per_g": LSB_PER_G, "calib": CALIB,
                        "samples": [[g_to_raw(0), g_to_raw(0), g_to_raw(1.0)]] * 100}
                end = AXIS_BASE + timedelta(seconds=5 * (k + 1))
                if t_ok:
                    body["t_first"] = (end - timedelta(seconds=4.95)).isoformat(
                        timespec="milliseconds")
                    body["t_last"] = end.isoformat(timespec="milliseconds")
                else:
                    body["t_first"] = "uptime+%.3fs(time_not_synced)" % (5 * k)
                    body["t_last"] = "uptime+%.3fs(time_not_synced)" % (5 * (k + 1))
                upload(body)

        def get_axis(dev):
            return req("GET", "/api/waveform?device_id=%s&batches=3" % dev)[1]

        # 档 1：板端时刻可解析
        axis_batches(AXIS_DEVS[0], True, [1, 2, 3])
        gA = get_axis(AXIS_DEVS[0])
        check("板端时刻可解析 → 用板端时刻摆轴",
              gA["time_axis_source"] == "device", gA["time_axis_source"])

        # 档 2：板端时刻不可解析（板子没对时），但批号连续 → 用采样率反推
        axis_batches(AXIS_DEVS[1], False, [1, 2, 3])
        gB = get_axis(AXIS_DEVS[1])
        check("板端时刻不可解析 + 批号连续 → 用采样率反推",
              gB["time_axis_source"] == "derived", gB["time_axis_source"])
        check("反推时也如实说明用的是什么（不是默默换了口径）",
              "采样率" in gB["time_axis_note"], gB["time_axis_note"])
        # ★ 这一条才是关键：两条**互相独立**的路（一条读板端钟，一条只用 n/hz）
        #   算出来的时间轴必须逐点相同。相同才说明"板子没对时时反推的轴"是可信的，
        #   而不是"看着挺顺、其实差一点"。
        check("★ 反推出来的时间轴与「板子对时了」那条路逐点相同",
              gA["series"]["t"] == gB["series"]["t"],
              "%s vs %s" % (gA["series"]["t"][:2], gB["series"]["t"][:2]))
        check("300 点 @20Hz 的窗口 = 14.95s（(300-1)×0.05）",
              abs(gB["window_s"] - 14.95) < 0.02, gB["window_s"])

        # 档 3：批号有断档 → 不敢反推，退回服务端接收时刻
        axis_batches(AXIS_DEVS[2], False, [1, 2, 5])
        gC = get_axis(AXIS_DEVS[2])
        check("批号断档（1,2,5）→ 不敢反推，退回服务端接收时刻",
              gC["time_axis_source"] == "server", gC["time_axis_source"])
        check("退回时点明代价（网络抖动会让间隔看起来不匀）",
              "网络抖动" in gC["time_axis_note"], gC["time_axis_note"])

        # 档 3b：批号连续，但板端如实报了丢样本 → 累加的前提不成立，同样不能反推
        axis_batches(AXIS_DEVS[3], False, [1, 2, 3], dropped=7)
        gD = get_axis(AXIS_DEVS[3])
        check("板端如实报了丢样本 → 不再反推（累加的前提不成立）",
              gD["time_axis_source"] == "server", gD["time_axis_source"])
        check("dropped 原样入库，没替板端抹平成 0",
              gD["batches_detail"][-1]["dropped"] == 7,
              gD["batches_detail"][-1]["dropped"])

        st, r = upload(make_batch(0, 0, 1.0, batch_seq=77, dropped=3))
        check("POST 回显 dropped（板端报的数不吞掉）",
              st == 201 and r.get("dropped") == 3, r.get("dropped"))
        st, r = upload(make_batch(0, 0, 1.0, batch_seq=78, dropped=-5))
        check("dropped 传负数 → 归 0（而不是写个负数进库）",
              st == 201 and r.get("dropped") == 0, r.get("dropped"))

        # 档 3c：只有一批 —— 降级原因不能说成"批号不连续"。
        # 那是误导：只有一批时根本谈不上"连续不连续"，
        # 笼统写一句会让人去查一个不存在的丢批问题。
        one_dev = DEV + "-one"
        upload(make_batch(0, 0, 1.0, batch_seq=1, device_id=one_dev,
                          t_last="uptime+5.000s(time_not_synced)"))
        gE = req("GET", "/api/waveform?device_id=%s&batches=3" % one_dev)[1]
        check("只有一批时也退回服务端时刻", gE["time_axis_source"] == "server",
              gE["time_axis_source"])
        check("★ 只有一批时的原因写「只有一批」，不是「批号断档」",
              "只有一批" in gE["time_axis_note"]
              and "断档" not in gE["time_axis_note"], gE["time_axis_note"])

        # ---------------------------------------------------------------
        print("\n== 8. ★ 脏数据拒收，不猜（补个 0 蒙混过去会把问题藏起来）==")
        bad = [
            ("samples 不是数组", make_batch(0, 0, 1, samples="abc")),
            ("samples 是空数组", make_batch(0, 0, 1, samples=[])),
            ("样本不是三元组", make_batch(0, 0, 1, samples=[[1, 2]])),
            ("样本含非数值", make_batch(0, 0, 1, samples=[[1, 2, "x"]])),
            ("计数超出 int16", make_batch(0, 0, 1, samples=[[1, 2, 99999]])),
            ("hz 越界", make_batch(0, 0, 1, hz=0)),
            ("单批超上限", make_batch(0, 0, 1,
                                  samples=[[1, 2, 3]] * 601)),
        ]
        for label, body in bad:
            st, r = req("POST", "/api/waveform", body)
            check("%s → 400" % label, st == 400, "%s %s" % (st, r.get("error")))
        st, r = req("POST", "/api/waveform", {"samples": [[1, 2, 3]]})
        check("缺 device_id → 400", st == 400, st)
        st, r = req("POST", "/api/waveform", make_batch(0, 0, 1, lsb_per_g=0,
                                                        calib=0))
        check("换算因子不合理时退回默认值并如实告知（warn 非空）",
              st == 201 and r.get("warn"), r.get("warn"))
        check("退回后 lsb_per_g 用的是默认值",
              r.get("lsb_per_g") == 1024.0, r.get("lsb_per_g"))

        # ---------------------------------------------------------------
        print("\n== 9. 没数据的设备：如实说没有，不是 500 ==")
        st, g = req("GET", "/api/waveform?device_id=never-seen-dev")
        check("返回 200 且 batches=0", st == 200 and g["batches"] == 0, st)
        check("带一句提示告诉人为什么没有", bool(g.get("hint")), g.get("hint"))
        st, g = req("GET", "/api/waveform")
        check("缺 device_id → 400", st == 400, st)

        # ---------------------------------------------------------------
        print("\n== 10. ★ 波形是「过程数据」：只留最近 N 批，留的是最新的 ==")
        # purge 是**按设备逐个**裁到 WAVE_KEEP_BATCHES 的，返回的是全局删除总数。
        # 所以先确认上面那几台测试设备都没超过保留窗口 —— 否则它们的删除数会混进
        # 下面「删掉的是最老的那些」这条断言，数字对不上还找不到原因。
        others = 0
        for d in AXIS_DEVS + [one_dev]:
            others += max(0, req("GET", "/api/waveform?device_id=%s&batches=120"
                                 % d)[1]["batches"] - 3)
        check("其它测试设备都没超保留窗口（免得污染下面的计数断言）",
              others == 0, others)
        # 前面已经灌了十几批，保留窗口是 3，触发一次清理看结果
        st, before = req("GET", "/api/waveform?device_id=%s&batches=120" % DEV)
        before_ids = [b["id"] for b in before["batches_detail"]]
        print("     清理前：%d 批（id %d…%d）"
              % (before["batches"], before_ids[0], before_ids[-1]))
        # 直接调服务端里的清理函数（导入同一份代码，不另起实例）
        sys.path.insert(0, HERE)
        import server as srv          # noqa: E402
        print("     自测进程里的保留窗口 = %d（应为 3，否则清理器会空转）"
              % srv.WAVE_KEEP_BATCHES)
        conn = srv.get_db()
        try:
            removed = srv.purge_wave_batches(conn)
        finally:
            conn.close()
        st, after = req("GET", "/api/waveform?device_id=%s&batches=120" % DEV)
        after_ids = [b["id"] for b in after["batches_detail"]]
        print("     清理后：%d 批（删掉 %d 批）" % (after["batches"], removed))
        check("保留窗口=3，清理后正好剩 3 批", after["batches"] == 3,
              after["batches"])
        check("确实删掉了东西（不是清理器空转）", removed > 0, removed)
        check("留下的是**最新**的 3 批（id 最大的那三条）",
              after_ids == before_ids[-3:], "%s vs %s" % (after_ids, before_ids[-3:]))
        check("删掉的是最老的那些", removed == len(before_ids) - 3,
              "%d vs %d" % (removed, len(before_ids) - 3))

        # ---------------------------------------------------------------
        print("\n== 11. 姿态在整条链路上前后一致（不是每处各算一遍各说各话）==")
        st, g = req("GET", "/api/waveform?device_id=%s&batches=3" % DEV)
        last_detail = g["batches_detail"][-1]
        check("latest.posture 与 batches_detail 末条一致",
              g["latest"]["posture"] == last_detail["posture"],
              "%s vs %s" % (g["latest"]["posture"], last_detail["posture"]))
        check("latest.acc_mag 与末条一致",
              abs(g["latest"]["acc_mag"] - last_detail["acc_mag"]) < 1e-6,
              "%s vs %s" % (g["latest"]["acc_mag"], last_detail["acc_mag"]))
        st, a = req("GET", "/api/attitude?device_id=%s" % DEV)
        check("/api/attitude 与 /api/waveform 的 latest 一致",
              a["attitude"]["posture"] == g["latest"]["posture"]
              and a["attitude"]["id"] == g["latest"]["id"],
              "%s/%s vs %s/%s" % (a["attitude"]["posture"], a["attitude"]["id"],
                                  g["latest"]["posture"], g["latest"]["id"]))

    except Exception as e:                                    # noqa: BLE001
        import traceback
        print("\n自测中断：%s" % type(e).__name__)
        traceback.print_exc()
        fails.append("自测中断：%s" % type(e).__name__)
        _dump_log(logpath)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()
        try:
            shutil.rmtree(TMP)
        except OSError as e:
            # 沙箱里删目录可能被拦（或"删掉了才报错"），不影响结论，如实说一声
            print("（临时目录未能删除：%s）" % e)

    print("\n" + "=" * 56)
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
