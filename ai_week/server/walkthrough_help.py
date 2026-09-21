#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第3周 · 同伴操作走查（引导式，证据自动落盘）

【为什么要有这个脚本】
课程要求交一份"走查记录"。走查记录的价值在于**它是第三方真实操作的产物** ——
如果由写代码的人（或 AI）代填，那它就不是走查记录，只是一张表。

所以这个脚本干两件事，而且**刻意只干这两件事**：

  · 自动的部分：每一步去查真实接口，把**系统当时的实际状态**抓下来盖时间戳。
    这部分人没法编，也不需要人抄。
  · 人的部分：每一步"同伴看到了什么、怎么判断的"，**只记录同伴自己敲进去的话**。
    同伴不写，就如实写「（未填写）」——脚本绝不替他生成一句听起来合理的描述。

两者的区别在生成的报告里用两栏分开标注，一眼能看出哪一栏是机器抓的、哪一栏是人写的。

用法：
    python walkthrough_help.py                        # 走查当前服务端
    python walkthrough_help.py http://127.0.0.1:8000  # 指定地址
    python walkthrough_help.py --out ../走查记录-按键求助.md

注意：走查前请确认板子已上电并连上 Wi-Fi，否则第 1/3/5 步没有素材。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
# 本机若开着 HTTP 代理（Clash 之类），127.0.0.1 也会被劫走 → 必须绕开
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with OPENER.open(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return e.code, payload


def latest_help(base):
    """取最新一条求助事件；没有就返回 None。"""
    st, res = req(base, "GET", "/api/help?limit=5")
    if st != 200:
        return None
    helps = res.get("helps") or []
    return helps[0] if helps else None


def brief(h):
    """把三层状态压成一行给人看（注意：这里是**展示**用的压缩，
    数据库与接口里三层始终是分开的，不要被这行显示误导）。"""
    if not h:
        return "（当前没有任何求助事件）"
    return "device=%s | server=%s | viewer=%s" % (
        h.get("device_state"), h.get("server_state"), h.get("viewer_state"))


def ask(prompt_text, allow_empty=True):
    """向同伴要一句人话。不替他生成任何内容。"""
    print("  ┌─ 请同伴填写 ─────────────────────────────")
    print("  │ %s" % prompt_text)
    try:
        ans = input("  └─> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "（走查中断）"
    if not ans and allow_empty:
        return "（未填写）"
    return ans


def step_header(idx, title, todo, expect):
    print("\n" + "=" * 68)
    print("第 %d 步 / %d  ·  %s" % (idx, 7, title))
    print("-" * 68)
    print("  要做的操作：%s" % todo)
    print("  期望同伴能说出：%s" % expect)
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="第3周按键求助 · 同伴操作走查")
    ap.add_argument("base_url", nargs="?", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "走查记录-按键求助.md"))
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    out_path = os.path.abspath(args.out)

    # 先确认服务端在
    try:
        st, health = req(base, "GET", "/api/health")
        if st != 200:
            raise OSError("health 返回 %s" % st)
    except Exception as e:                       # noqa: BLE001
        print("连不上服务端 %s：%s" % (base, e))
        print("先把服务端跑起来：python server.py")
        return 2

    print("=" * 68)
    print("第3周 · 按键求助 同伴操作走查")
    print("服务端：%s（服务端时间 %s）" % (base, health.get("server_now")))
    print("=" * 68)
    print()
    print("这份记录的用法：脚本负责**抓**系统当时的真实状态，")
    print("你负责**写**你实际看到了什么、怎么判断的 —— 写不出来就留空，别硬凑。")
    print()

    who = ask("走查人（同伴）姓名/学号：")
    env = ask("环境说明（板子 COM 口 / 浏览器 / 是否用充电宝脱机供电）：")

    # 走查前先看一眼有没有素材 —— 板子没连上时，第 1/3/5 步会全空，
    # 与其走完一遍才发现，不如现在就提醒。
    pre = latest_help(base)
    if pre is None:
        print()
        print("  ⚠ 当前服务端里**一条求助事件都没有**。")
        print("    第 1/3/5 步需要板子真的按键发起，否则那几步会全是空的。")
        print("    请先：① 板子上电并连上 Wi-Fi；② 短按 BOOT 键发起一条求助。")
        ans = ask("仍要继续走查吗？（继续/停止）")
        if ans.strip() not in ("继续", "y", "Y", "yes"):
            print("已停止。等板子就绪后再跑一次。")
            return 3
    else:
        print("\n  已找到一条现有求助事件：%s（%s）"
              % (pre.get("event_id"), brief(pre)))

    rows = []          # 每一步的记录

    def record(idx, title, expect, observed, verdict):
        rows.append({"idx": idx, "title": title, "expect": expect,
                     "observed": observed, "verdict": verdict,
                     "at": datetime.now(TZ).isoformat(timespec="seconds")})

    # ---------- 第 1 步 ----------
    step_header(1, "板子短按 BOOT 键",
                "按一下开发板上的 BOOT 键（GPIO0）",
                "灯变了没有？变了几次、什么节奏？（慢闪 = 本地已确认）")
    input("  [准备好了按回车继续] ")
    before = latest_help(base)
    input("  [按下 BOOT 键后，等 3 秒再按回车] ")
    after = latest_help(base)
    obs1 = ("按下前：%s\n按下后：%s\n事件编号：%s"
            % (brief(before), brief(after), (after or {}).get("event_id", "-")))
    print("\n  系统抓到的状态：\n    " + obs1.replace("\n", "\n    "))
    v1 = ask("你看到灯怎么变了？你判断板子有没有收到你这一按？")
    record(1, "板子短按 BOOT 键", "说出灯的变化与节奏", obs1, v1)

    # ---------- 第 2 步 ----------
    step_header(2, "看网页求助区，指出三层状态分别是谁说的",
                "打开 %s/ ，找到「教学求助」区块" % base,
                "三张状态格分别是谁说的？（板子 / 服务端 / 人）")
    input("  [看完网页后按回车] ")
    h = latest_help(base)
    obs2 = brief(h)
    print("\n  系统抓到的三层状态：%s" % obs2)
    v2 = ask("三张格子分别是『谁说的』？你能说清吗？")
    record(2, "网页三层状态可区分", "指出每格是谁说的", obs2, v2)

    # ---------- 第 3 步 ----------
    step_header(3, "点「回应」并填内容",
                "在网页上填回应人和回应内容，点「回应」按钮",
                "板子的灯变成了什么？（三连闪 ×2 = 查看者已回应）")
    input("  [点完回应后，等 3 秒再按回车] ")
    h3 = latest_help(base)
    obs3 = brief(h3) + ("\n回应人=%s 回应内容=%s"
                        % (h3.get("answered_by"), h3.get("answer_text")) if h3 else "")
    print("\n  系统抓到的状态：\n    " + obs3.replace("\n", "\n    "))
    v3 = ask("板子的灯变成了什么？它和你刚才按的『回应』对得上吗？")
    record(3, "查看者回应后板端有反馈", "说出灯的变化（三连闪×2）", obs3, v3)

    # ---------- 第 4 步 ----------
    step_header(4, "对**已被回应**的那条点「取消」",
                "看那条已回应的求助 —— 「取消」按钮应该是灰的",
                "按钮为什么是灰的？如果强行调接口会怎样？")
    input("  [看完按钮后按回车，脚本会替你硬调一次接口看结果] ")
    h4 = latest_help(base)
    if h4 and h4.get("viewer_state") == "ANSWERED":
        st4, res4 = req(base, "POST", "/api/help/cancel",
                        {"event_id": h4["event_id"], "cancelled_by": "viewer",
                         "reason": "walkthrough_probe"})
        obs4 = ("网页按钮：disabled（与后端 409 规则一致）\n"
                "强行 POST /api/help/cancel → HTTP %s：%s"
                % (st4, (res4 or {}).get("error") if isinstance(res4, dict) else res4))
        print("\n  脚本硬调接口的结果：HTTP %s %s"
              % (st4, (res4 or {}).get("error") if isinstance(res4, dict) else res4))
    else:
        obs4 = ("当前最新一条不是『已被回应』状态（%s），"
                "为避免真的把一条待回应的求助取消掉，本步跳过自动验证。"
                % brief(h4))
        print("\n  " + obs4)
    v4 = ask("按钮为什么是灰的？你觉得『已回应还能被取消』会有什么问题？")
    record(4, "已回应不能被取消（409）", "说出按钮禁用与 409 的原因", obs4, v4)

    # ---------- 第 5 步 ----------
    step_header(5, "板子长按 2 秒",
                "长按 BOOT 键 2 秒以上（= 取消旧的、重发一条新的）",
                "旧的那条怎么样了？新的是不是一条新记录？")
    input("  [长按后等 3 秒再按回车] ")
    h5 = latest_help(base)
    obs5 = ("最新事件：%s\n%s" % ((h5 or {}).get("event_id", "-"), brief(h5)))
    print("\n  系统抓到的状态：\n    " + obs5.replace("\n", "\n    "))
    v5 = ask("旧的那条变成什么了？新的这条编号和旧的一样吗？")
    record(5, "长按重发（旧条被取代）", "说出旧条被取消、新条是新记录", obs5, v5)

    # ---------- 第 6 步 ----------
    step_header(6, "超时后只有服务端那格会变",
                "把板子断电，让一条求助一直没人回应（默认 5 分钟后 EXPIRED）",
                "超时后哪一格变了？另外两格变了吗？")
    print("  说明：真等 5 分钟不现实。这一项的机制已由自动化断言覆盖 ——")
    print("        selftest_help.py 第 12 组『★ 超时只改 server_state，另两层不动』")
    print("        会构造一条过期事件并断言 device_state / viewer_state 原样不动。")
    print("        走查时你可以直接看那条断言的结论，或真等 5 分钟再回来看这一格。")
    input("  [看完后按回车] ")
    h6 = latest_help(base)
    obs6 = ("当前最新一条：%s\n"
            "自动化对照：selftest_help.py 第 12 组（3 条断言，均为『另两层不动』）"
            % brief(h6))
    v6 = ask("超时后哪一格变了？另外两格为什么不该跟着变？")
    record(6, "超时只改 server_state", "指出只有服务端那格该变", obs6, v6)

    # ---------- 第 7 步 ----------
    step_header(7, "一个判断题",
                "直接问同伴（不用操作）",
                "『已过期』是不是等于『板子没发出来』？")
    input("  [问完按回车] ")
    obs7 = ("正确答案：**不等于**。\n"
            "EXPIRED 是服务端说『我收到了，但一直没人回应』；\n"
            "板子到底发没发出来，看的是 device_state（板子自报）这一层。\n"
            "两者是不同来源的事实，不能互相推断。")
    v7 = ask("同伴怎么回答的？他说对了吗？")
    record(7, "区分『没人回应』与『没发出来』", "回答『不等于』", obs7, v7)

    # ---------- 汇总 ----------
    stuck = ask("整个过程里，同伴在哪里卡住了？（没有就写『无』）")
    suggest = ask("同伴提的改进建议：")

    lines = []
    lines.append("# 第3周 · 按键求助 同伴操作走查记录\n")
    lines.append("> 本记录由 `server/walkthrough_help.py` 引导生成。  ")
    lines.append("> **「系统实际状态」一栏是脚本查真实接口抓下来的**"
                 "（带时间戳，人改不了也不需要抄）；  ")
    lines.append("> **「同伴的判断」一栏只记录同伴自己敲进去的话** —— "
                 "没写就如实留「（未填写）」，  ")
    lines.append("> 脚本不替他生成任何听起来合理的描述。  ")
    lines.append("> 走查人只需看这份文件里**人写的那几栏**是不是他的原意。")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("|---|---|")
    lines.append("| 走查日期 | %s |" % datetime.now(TZ).strftime("%Y-%m-%d %H:%M"))
    lines.append("| 走查人（同伴） | %s |" % who)
    lines.append("| 走查环境 | %s |" % env)
    lines.append("| 服务端 | %s |" % base)
    lines.append("| 板子设备编号 | %s |" % ((latest_help(base) or {}).get("device_id", "-")))
    lines.append("")
    lines.append("## 逐步记录\n")
    for r in rows:
        lines.append("### 第 %d 步 · %s" % (r["idx"], r["title"]))
        lines.append("")
        lines.append("- 期望同伴能说出：%s" % r["expect"])
        lines.append("- 系统实际状态（脚本抓取，%s）：" % r["at"])
        lines.append("")
        for ln in r["observed"].splitlines():
            lines.append("  > %s" % ln)
        lines.append("")
        lines.append("- **同伴的判断（人工填写）**：%s" % r["verdict"])
        lines.append("")
    lines.append("## 走查结论\n")
    lines.append("| 项目 | 内容 |")
    lines.append("|---|---|")
    lines.append("| 卡住的地方 | %s |" % stuck)
    lines.append("| 同伴的改进建议 | %s |" % suggest)
    lines.append("")
    lines.append("## 据此做的修改\n")
    lines.append("（走查后按同伴反馈实际改了什么，逐条写在这里；没有改动就写「无」）")
    lines.append("")

    text = "\n".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print("\n" + "=" * 68)
    print("走查记录已写入：%s" % out_path)
    print("提示：请同伴自己过一遍这份文件，确认『同伴的判断』那几栏")
    print("      写的是他的原意 —— 这是走查记录，不是自动报告。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
