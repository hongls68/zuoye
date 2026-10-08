#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台清理 vs 在线请求：加锁范围回归自测

【这个自测在防什么】
2026-10-08 真机上出现过一次：开发板插在 COM4、Wi-Fi 正常、ping 0% 丢包，
但**每一条上报都在 8 秒后超时**（串口上是 esp-tls: select() timeout），
服务端日志里一条请求都没有。先怀疑板子、Wi-Fi、防火墙、代理 —— 都不是。
真正的原因是服务端**被自己的配额清理线程锁死了约 9 分半**：

  frame_purger 用 `with _db_lock:` 把整个 purge_expired_frames() 包住，
  而那个函数一次要处理 35125 行（积压 17 天的帧），循环里既有 os.remove()
  又有每行一条 UPDATE，commit 只在最后做一次。
  于是所有 HTTP 线程都停在 `with _db_lock` 那一行，socket 堆成 CLOSE_WAIT。

【为什么必须有这个自测】
这个 bug **不报错**：日志干净、没有异常、进程活着，连 /api/health 都还能秒回
（它不碰数据库）。变的只有一件事 —— 「别的请求还能不能进来」。
破了不变量却不留痕迹，按本项目的老规矩就得钉成断言。

【断言什么】
  ① ★ 主断言（确定性）：`_db_lock` **绝不能被持有着跨过文件系统操作**。
     做法：把 _db_lock 换成替身锁（记下当前持锁的线程 id），再替换
     os.remove / os.path.exists，检查「调用它们的线程是不是正持着锁」。
     旧写法 100% 命中，跟机器快慢无关。
  ② 后果级检查：清理进行中，另一个线程能不能在合理时间内拿到锁做一次写。
     会随机器浮动，所以阈值给得松，只用来兜住「整段独占」那种写法。

【不覆盖什么】
  不检查清理策略本身（保留几天、哪些该清）—— 那是 selftest_gallery.py 的事。
  这里只管一件事：**清理的时候，服务端还活着吗。**

运行：python selftest_purge_lock.py
"""
import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 每轮用新目录名，避免上一轮残留的 data.db 影响判定
TMP = os.path.join(HERE, "tmpdata_purgelock_%d" % int(time.time()))
N_ROWS = 2000          # 造这么多条过期帧：够让「整段独占」的写法明显卡住
MAX_WAIT_S = 1.0       # 后果级检查的阈值（松，只为兜住整段独占）

fails = []


def check(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name
          + (" | " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


class SpyLock:
    """替身锁：真的互斥，另外记下「现在哪个线程持有它」。

    ★ 只为了能问出这句话：「调用 os.remove 的线程，是不是正拿着 _db_lock？」
      threading.Lock 本身不暴露持有者，所以必须自己记。
      ★ 必须是「持有者」而不是「有没有被持有」：清理进行中，别的请求线程
        可能正合法地拿着锁做它自己的事，那时清理线程碰文件系统不算违规。
    """

    def __init__(self, real):
        self._real = real
        self.owner = None

    def __enter__(self):
        self._real.acquire()
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self.owner = None
        self._real.release()
        return False

    def locked(self):
        return self._real.locked()


def main() -> int:
    # 必须在 import server 之前设好，否则会连到真实的 data.db
    os.environ["DATA_DIR"] = TMP
    os.environ["RETENTION_DAYS"] = "0"     # 强制所有帧都过期
    sys.path.insert(0, HERE)
    import server as mod                  # noqa: E402

    mod.init_db()
    os.makedirs(mod.SNAP_DIR, exist_ok=True)

    print("== 0. 造 %d 条过期帧（真建文件，才测得到文件系统操作）==" % N_ROWS)
    conn = mod.get_db()
    try:
        for i in range(N_ROWS):
            fname = "seed-%05d.jpg" % i
            with open(os.path.join(mod.SNAP_DIR, fname), "wb") as f:
                f.write(b"\xff\xd8seed\xff\xd9")
            conn.execute(
                "INSERT INTO frames(device_id, ts_server, filename, bytes, sha256)"
                " VALUES(?,?,?,?,?)",
                ("seed-dev", "2020-01-01T00:00:00.000+08:00", fname, 12,
                 "h%05d" % i))
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    finally:
        conn.close()
    files_before = len(os.listdir(mod.SNAP_DIR))
    check("造数据完成", before == N_ROWS and files_before == N_ROWS,
          "行=%d 文件=%d" % (before, files_before))

    print("\n== 1. 装上探针 ==")
    spy = SpyLock(mod._db_lock)
    mod._db_lock = spy
    violations = []
    real_remove, real_exists = os.remove, os.path.exists

    def spy_remove(path, *a, **k):
        if spy.owner == threading.get_ident():
            violations.append(("os.remove", str(path)))
        return real_remove(path, *a, **k)

    def spy_exists(path, *a, **k):
        if spy.owner == threading.get_ident():
            violations.append(("os.path.exists", str(path)))
        return real_exists(path, *a, **k)

    os.remove, os.path.exists = spy_remove, spy_exists
    check("替身锁与探针已就位", mod._db_lock is spy)

    print("\n== 2. 探针自检：故意复现旧写法，探针必须报出来 ==")

    def old_style_purge():
        """旧写法：持着 _db_lock 跨过文件系统操作。

        ★ 为什么要故意写一遍坏的：如果探针本身不灵，后面那条"0 次违规"
          就是**假绿** —— 一个抓不到问题的校验器比没有校验器更糟
          （本项目已经在 render_svg.py 上踩过一次：它自己算错，把三张好图
          全报成越界）。所以先证明"它能抓到坏的"。
        """
        with mod._db_lock:
            victim = os.path.join(mod.SNAP_DIR, "probe-victim.jpg")
            with open(victim, "wb") as f:
                f.write(b"x")
            os.remove(victim)

    old_style_purge()
    caught = len(violations)
    check("探针能抓出旧写法（否则本自测是假绿）", caught > 0,
          "抓到 %d 次" % caught)
    violations.clear()

    print("\n== 3. 清理进行中，别的线程还能不能进数据库 ==")
    stop = threading.Event()
    waits, rounds = [], []

    def other_request():
        """模拟一条在线请求：拿锁 -> 写一次 -> 放锁。"""
        while not stop.is_set():
            t0 = time.time()
            with mod._db_lock:
                c = mod.get_db()
                try:
                    c.execute("UPDATE frames SET bytes=bytes WHERE id=1")
                    c.commit()
                finally:
                    c.close()
            waits.append(time.time() - t0)
            rounds.append(1)
            time.sleep(0.001)

    th = threading.Thread(target=other_request, daemon=True)
    th.start()
    time.sleep(0.05)               # 让它先转起来，确保和清理真的重叠

    t0 = time.time()
    n = mod.purge_expired_frames()
    purge_s = time.time() - t0

    stop.set()
    th.join(timeout=3)

    check("清理确实清掉了全部 %d 条" % N_ROWS, n == N_ROWS, "n=%d" % n)
    check("清理期间在线请求一直在成功（跑了 %d 轮）" % len(rounds), len(rounds) > 0)
    worst = max(waits) if waits else 0.0
    check("在线请求最长等待 < %.1fs（实测 %.3fs；清理总耗时 %.2fs）"
          % (MAX_WAIT_S, worst, purge_s), worst < MAX_WAIT_S)

    os.remove, os.path.exists = real_remove, real_exists

    print("\n== 4. ★ 主断言：持锁时不许碰文件系统 ==")
    check("_db_lock 没有被持有着跨过 os.remove / os.path.exists",
          not violations, violations[:5] if violations else "0 次违规")

    print("\n== 5. 语义没被改坏（元数据留、原图清）==")
    conn = mod.get_db()
    try:
        left = conn.execute("SELECT COUNT(*) FROM frames "
                            "WHERE purged_at IS NULL").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        hashes = conn.execute("SELECT COUNT(*) FROM frames "
                              "WHERE sha256 IS NOT NULL").fetchone()[0]
    finally:
        conn.close()
    files_after = len(os.listdir(mod.SNAP_DIR))
    check("没有未标记的帧了", left == 0, left)
    check("元数据一行不少", total == N_ROWS, total)
    check("哈希一条不丢", hashes == N_ROWS, hashes)
    check("原图文件已清空", files_after == 0, files_after)

    # ★ 不用 ignore_errors=True：删不掉就说出来，而不是假装干净。
    try:
        shutil.rmtree(TMP)
        check("临时目录已清理", not os.path.exists(TMP))
    except OSError as e:
        check("临时目录已清理", False, "残留 %s（%s）" % (TMP, e))

    print()
    if fails:
        print("FAILED: %d 项未通过" % len(fails))
        for f in fails:
            print("   -", f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
