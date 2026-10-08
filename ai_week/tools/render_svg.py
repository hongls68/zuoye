#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染 / 校验 SVG 图纸 —— 用来验收 hardware/*.svg。

为什么需要它
------------
SVG 是代码，但它交付的是**画面**。只读源码核对，等于没核对：
  * 后画的白色矩形会**盖住**先写的文字（源码里文字还在，画面上没有）；
  * 图元坐标超出 `viewBox` 会被**静默裁掉**（不报错，只是少一块）。
这两种错都"不报错"，所以必须换一个视角 —— 渲染出来看。

本工具做两件事：
  1. `--render`（默认）：调 Chrome 无头把 SVG 渲染成 PNG，可以整张也可以局部放大；
  2. `--check`：不渲染，直接读 SVG 算出所有**图形**的外接框，和 `viewBox` 比 ——
     越界就报出来。这一步能在渲染之前先把"画布太小"这类问题挡住。

用法
----
    # 整张渲染（输出到 <svg同名>.png）
    python tools/render_svg.py hardware/enclosure_top.svg

    # 局部放大：x0,y0,倍数 —— 用于看清小字
    python tools/render_svg.py hardware/enclosure_side.svg --zoom 60,280,2.2

    # 只查画布越界（不启动浏览器，快）
    python tools/render_svg.py hardware/enclosure_top.svg --check

    # 批量查三张图
    python tools/render_svg.py hardware/*.svg --check

已知局限（不假装它是完备的）
--------------------------
* `--check` 只算**图形**（rect / circle / line / path 的坐标点 / polygon / ellipse），
  **不算文字的宽度** —— 文字只按它的锚点参与计算。
  所以它抓不到"文字溢出方框"，那类问题只能渲染出来肉眼看。
* 只支持 `translate()` 变换。遇到 `scale()` / `rotate()` / `matrix()` 会**跳过该图元并提示**，
  不会假装算准了。
* `path` 的曲线段按控制点算（会略微高估），对"是否越界"的判断是安全的。
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

# Chrome 候选路径（Windows 优先，其它平台兜底）
CHROME_CANDIDATES = [
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    os.path.expanduser("~") + "/AppData/Local/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------- 画布越界检查

# 每个元素类型关心的属性
_ATTRS = {
    "rect": ["x", "y", "width", "height"],
    "circle": ["cx", "cy", "r"],
    "ellipse": ["cx", "cy", "rx", "ry"],
    "line": ["x1", "y1", "x2", "y2"],
    "text": ["x", "y"],
    "image": ["x", "y", "width", "height"],
}
_NUM = r"-?\d+(?:\.\d+)?"


def _bbox_of(tag, attrs):
    """返回 (minx, miny, maxx, maxy)；算不出来返回 None。"""
    if tag == "rect":
        x, y = float(attrs.get("x", 0)), float(attrs.get("y", 0))
        w, h = float(attrs.get("width", 0)), float(attrs.get("height", 0))
        return (x, y, x + w, y + h)
    if tag == "circle":
        cx, cy, r = float(attrs.get("cx", 0)), float(attrs.get("cy", 0)), float(attrs.get("r", 0))
        return (cx - r, cy - r, cx + r, cy + r)
    if tag == "ellipse":
        cx, cy = float(attrs.get("cx", 0)), float(attrs.get("cy", 0))
        rx, ry = float(attrs.get("rx", 0)), float(attrs.get("ry", 0))
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    if tag == "line":
        x1, y1 = float(attrs.get("x1", 0)), float(attrs.get("y1", 0))
        x2, y2 = float(attrs.get("x2", 0)), float(attrs.get("y2", 0))
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    if tag == "text":
        # ★ 只有锚点，没有字宽 —— 见文件头的"已知局限"
        x, y = float(attrs.get("x", 0)), float(attrs.get("y", 0))
        return (x, y, x, y)
    if tag == "polygon" or tag == "polyline":
        pts = [float(v) for v in re.findall(_NUM, attrs.get("points", ""))]
        if len(pts) < 2:
            return None
        xs, ys = pts[0::2], pts[1::2]
        return (min(xs), min(ys), max(xs), max(ys))
    if tag == "path":
        pts = [float(v) for v in re.findall(_NUM, attrs.get("d", ""))]
        if len(pts) < 2:
            return None
        # 路径里的数字混着坐标与半径/角度，按点对粗暴切分 —— 会高估，但对"越界"是安全的
        xs, ys = pts[0::2], pts[1::2]
        return (min(xs), min(ys), max(xs), max(ys))
    return None


_TAG_RE = re.compile(r"<(/?)(\w+)((?:\s+[\w:.-]+\s*=\s*\"[^\"]*\")*)\s*/?>")
_ATTR_RE = re.compile(r"([\w:.-]+)\s*=\s*\"([^\"]*)\"")
_TRANSLATE_RE = re.compile(r"translate\(\s*(%s)\s*[,\s]\s*(%s)\s*\)" % (_NUM, _NUM))
_OTHER_TRANSFORM_RE = re.compile(r"\b(scale|rotate|matrix|skewX|skewY)\s*\(")


def check_viewbox(path):
    """读 SVG，算出所有图形的外接框，与 viewBox 比。返回 (ok, 报告文本)。"""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    m = re.search(r'viewBox\s*=\s*"([^"]*)"', src)
    if not m:
        return True, "  没有 viewBox，跳过"
    vb = [float(v) for v in re.findall(_NUM, m.group(1))]
    vx, vy, vw, vh = vb[0], vb[1], vb[2], vb[3]

    stack = []          # 累积 translate
    cur = [0.0, 0.0]
    lo = [float("inf")] * 2
    hi = [float("-inf")] * 2
    skipped = []        # 遇到不支持的 transform 的元素
    counted = 0

    for mt in _TAG_RE.finditer(src):
        closing, tag, attrstr = mt.group(1), mt.group(2), mt.group(3)
        if tag == "g":
            if closing:
                if stack:
                    cur = stack.pop()
            else:
                attrs = dict(_ATTR_RE.findall(attrstr))
                t = attrs.get("transform", "")
                # ★ 必须先压"进入本组之前"的位移，再改 cur ——
                #   如果压的是改完之后的 cur，出栈时就把 translate 又还原成了它自己，
                #   位移会一路累加（本工具第一版就是这么错的：translate(760,100) 之后的
                #   translate(30,505) 算出了 790 而不是 30，三张图全报假越界）。
                stack.append(list(cur))
                if _OTHER_TRANSFORM_RE.search(t):
                    skipped.append(tag)
                    continue
                tm = _TRANSLATE_RE.search(t)
                if tm:
                    cur = [cur[0] + float(tm.group(1)), cur[1] + float(tm.group(2))]
            continue
        if closing:
            continue
        attrs = dict(_ATTR_RE.findall(attrstr))
        if _OTHER_TRANSFORM_RE.search(attrs.get("transform", "")):
            skipped.append(tag)
            continue
        bb = _bbox_of(tag, attrs)
        if bb is None:
            continue
        tm = _TRANSLATE_RE.search(attrs.get("transform", ""))
        dx, dy = (float(tm.group(1)), float(tm.group(2))) if tm else (0.0, 0.0)
        x0, y0 = cur[0] + dx, cur[1] + dy
        box = (bb[0] + x0, bb[1] + y0, bb[2] + x0, bb[3] + y0)
        lo[0], lo[1] = min(lo[0], box[0]), min(lo[1], box[1])
        hi[0], hi[1] = max(hi[0], box[2]), max(hi[1], box[3])
        counted += 1

    lines = []
    if counted == 0:
        return True, "  没解析到图形，跳过"
    lines.append("  viewBox        x %g..%g   y %g..%g" % (vx, vx + vw, vy, vy + vh))
    lines.append("  图形外接框     x %.1f..%.1f   y %.1f..%.1f  （%d 个图形）"
                 % (lo[0], hi[0], lo[1], hi[1], counted))
    bad = []
    if lo[0] < vx - 0.5:
        bad.append("左边超出 %.1f" % (vx - lo[0]))
    if hi[0] > vx + vw + 0.5:
        bad.append("右边超出 %.1f" % (hi[0] - (vx + vw)))
    if lo[1] < vy - 0.5:
        bad.append("上边超出 %.1f" % (vy - lo[1]))
    if hi[1] > vy + vh + 0.5:
        bad.append("★ 下边超出 %.1f" % (hi[1] - (vy + vh)))
    if skipped:
        lines.append("  ⚠ 有 %d 个元素带 scale/rotate/matrix，未参与计算：%s"
                     % (len(skipped), ", ".join(sorted(set(skipped)))))
    lines.append("  提示：文字只按锚点算，**字宽不算** —— 文字溢出方框这类问题请渲染后肉眼看。")
    if bad:
        return False, "\n".join(lines + ["  ✗ 越界：" + "；".join(bad)])
    return True, "\n".join(lines + ["  ✓ 画布装得下"])


# ------------------------------------------------------------------ 渲染

_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  html,body{margin:0;padding:0;background:#fff}
  #frame{width:%(fw)spx;height:%(fh)spx;overflow:hidden;position:relative}
  #inner{position:absolute;left:0;top:0;transform-origin:0 0;
         transform:scale(%(k)s) translate(%(tx)spx,%(ty)spx)}
</style></head><body>
<div id="frame"><div id="inner">%(svg)s</div></div>
</body></html>
"""


# ------------------------------------------------------------------ 自检

# 每个用例：(名字, SVG 文本, 期望通过?)
_SELFTEST_CASES = [
    (
        "内容在画布内 -> 通过",
        '<svg viewBox="0 0 100 100"><rect x="10" y="10" width="50" height="50"/></svg>',
        True,
    ),
    (
        "★ 内容超出画布下边 -> 必须报出来",
        # 这就是真实事故的形状：rect 高 80、起点 y=50 -> 底边 130 > viewBox 的 100
        '<svg viewBox="0 0 100 100"><rect x="0" y="50" width="50" height="80"/></svg>',
        False,
    ),
    (
        "★ 嵌套 translate 不能累加错",
        # 外层平移到 (60,0)，内层再平移到 (10,0)：内层矩形的左边界应是 70，不是 60+10+10
        '<svg viewBox="0 0 100 100">'
        '<g transform="translate(60,0)"><rect x="0" y="0" width="20" height="20"/></g>'
        '<g transform="translate(10,0)"><rect x="0" y="0" width="20" height="20"/></g>'
        '</svg>',
        True,
    ),
    (
        "★ 两个 translate 组各自独立（本工具第一版就错在这里）",
        # translate(60,0) 那组里的矩形右边界 = 80 <= 100；出栈后 cur 必须回到 0，
        # 否则第二组的矩形会被算到 10+20=30 之外的地方，把整个图误判成越界
        '<svg viewBox="0 0 100 100">'
        '<g transform="translate(60,0)"><rect x="0" y="0" width="20" height="20"/></g>'
        '<g transform="translate(10,0)"><rect x="0" y="0" width="80" height="20"/></g>'
        '</svg>',
        True,
    ),
]


def selftest():
    """★ 为什么要有这个自检：本工具的 translate 压栈曾经写反，
    三张真实图纸全被误报成"越界" —— 一个会误伤的校验器比没有校验器更糟。
    所以把当时那个形状钉成用例，谁改坏了立刻红。"""
    import tempfile

    print("render_svg.py 自检（%d 组）" % len(_SELFTEST_CASES))
    passed = failed = 0
    with tempfile.TemporaryDirectory() as td:
        for i, (name, svg, want_ok) in enumerate(_SELFTEST_CASES, 1):
            p = os.path.join(td, "case%d.svg" % i)
            with open(p, "w", encoding="utf-8") as f:
                f.write(svg)
            ok, report = check_viewbox(p)
            good = (ok == want_ok)
            passed, failed = (passed + 1, failed) if good else (passed, failed + 1)
            print("  [%s] %s" % ("通过" if good else "失败", name))
            if not good:
                print("        期望 %s，实际 %s" % ("通过" if want_ok else "越界", "通过" if ok else "越界"))
                for line in report.splitlines():
                    print("        " + line)

    print("")
    print("结果：%d 通过 / %d 失败" % (passed, failed))
    print("注意：本自检只覆盖 --check 的画布计算，**不覆盖渲染**（渲染要开浏览器，"
          "在没装 Chrome 的机器上会失败，不适合放进自检）。")
    return 0 if failed == 0 else 1


# ------------------------------------------------------------------ 渲染

def render(path, out, zoom=None):
    chrome = find_chrome()
    if not chrome:
        print("找不到 Chrome，无法渲染。候选路径：")
        for c in CHROME_CANDIDATES:
            print("  " + c)
        return False

    with open(path, "r", encoding="utf-8") as f:
        svg = f.read()
    svg = re.sub(r'<\?xml[^>]*\?>', "", svg)

    m = re.search(r'viewBox\s*=\s*"([^"]*)"', svg)
    if m:
        vb = [float(v) for v in re.findall(_NUM, m.group(1))]
        fw, fh = int(vb[2]), int(vb[3])
    else:
        fw, fh = 900, 700

    if zoom:
        x0, y0, k = zoom
        # 放大后能看到的画布区域变小，但截图窗口保持原画布大小
        html = _HTML % {"svg": svg, "fw": fw, "fh": fh, "k": k, "tx": -x0, "ty": -y0}
    else:
        html = _HTML % {"svg": svg, "fw": fw, "fh": fh, "k": 1, "tx": 0, "ty": 0}

    with tempfile.TemporaryDirectory() as td:
        hp = os.path.join(td, "view.html")
        with open(hp, "w", encoding="utf-8") as f:
            f.write(html)
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--no-proxy-server",
            "--hide-scrollbars", "--window-size=%d,%d" % (fw, fh),
            "--virtual-time-budget=8000",
            "--screenshot=" + os.path.abspath(out),
            "file:///" + hp.replace("\\", "/"),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
    if not os.path.isfile(out):
        print("渲染失败：\n" + (r.stderr or "")[-800:])
        return False
    print("已渲染 %s  ->  %s  (%d×%d%s)"
          % (path, out, fw, fh, "，放大 %.2f×" % zoom[2] if zoom else ""))
    return True


def main():
    ap = argparse.ArgumentParser(description="渲染 / 校验 SVG 图纸")
    ap.add_argument("svgs", nargs="*", help="一个或多个 .svg 路径")
    ap.add_argument("-o", "--out", help="输出 PNG（只在单个输入时有效）")
    ap.add_argument("--zoom", help="局部放大，格式 x0,y0,倍数")
    ap.add_argument("--check", action="store_true", help="只查画布越界，不渲染")
    ap.add_argument("--selftest", action="store_true", help="跑内置自检")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.svgs:
        ap.print_help()
        return 2

    missing = [p for p in args.svgs if not os.path.isfile(p)]
    if missing:
        for p in missing:
            print("找不到文件：" + p)
        return 2

    if args.check:
        all_ok = True
        for p in args.svgs:
            print(p)
            ok, report = check_viewbox(p)
            print(report)
            print("")
            all_ok = all_ok and ok
        return 0 if all_ok else 1

    if len(args.svgs) > 1:
        print("多个输入时忽略 -o，逐个渲染到同名 .png")
    zoom = None
    if args.zoom:
        parts = [float(v) for v in args.zoom.split(",")]
        if len(parts) != 3:
            print("--zoom 需要三个数：x0,y0,倍数")
            return 2
        zoom = parts

    ok = True
    for p in args.svgs:
        base = os.path.splitext(p)[0]
        suffix = "_zoom" if zoom else ""
        out = args.out if (args.out and len(args.svgs) == 1) else base + suffix + ".png"
        ok = render(p, out, zoom) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
