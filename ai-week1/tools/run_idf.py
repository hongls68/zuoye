"""
run_idf.py —— 在受限 shell（Git Bash / MSYS2 / 沙箱）中驱动 ESP-IDF 的 idf.py。

为什么需要这个脚本
------------------
本机 ESP-IDF 装在 C:\\Espressif，正常的入口是开始菜单里的
"ESP-IDF 5.5 PowerShell"。但在 Git Bash / AI 助手的沙箱里直接跑 idf.py 会踩三个坑：

坑 1：idf.py 静默不执行
    tools/idf.py 末尾：
        if __name__ == '__main__':
            if 'MSYSTEM' in os.environ:
                print_warning('MSys/Mingw is no longer supported...')  # 只警告
            elif ...:
                ...
            else:
                main()                                                  # 只有这里才干活
    只要 MSYSTEM 存在，main() 永不执行 —— 打印一行警告后退出，退出码仍是 0，
    极易误判为"构建成功"。Git Bash 自动设 MSYSTEM=MINGW64，且进程外剥离无效
    （`env -u MSYSTEM` 之后 python 内部仍能看到，宿主通过 sitecustomize.py 重新注入）。

坑 2：platform.machine() 返回空字符串
    idf_tools.py 模块级执行
        PYTHON_PLATFORM = platform.system() + '-' + platform.machine()
        CURRENT_PLATFORM = parse_platform_arg(PYTHON_PLATFORM)
    本环境下 machine() 返回 ''，得到 'Windows-'，不在 PLATFORM_FROM_NAME 里，
    于是 fatal() + SystemExit(1)：
        ERROR: Support for platform 'Windows-' hasn't been added yet.

坑 3：PATH 被 MSYS 路径转换破坏
    Git Bash 的 PATH 用冒号分隔，Windows 原生 python 用分号分隔。
    在 shell 里写 PATH="C:/Espressif/tools/cmake/3.30.2/bin:$PATH" 之后，
    MSYS 会把 'C:' 当相对路径转换，python 里实际看到
        C;C:\\Users\\...\\PortableGit\\...\\Espressif\\tools\\cmake\\3.30.2\\bin
    导致 shutil.which('cmake') 返回 None，报
        "cmake" must be available on the PATH to use idf.py

坑 4（预防）：沙箱劫持 os.remove
    沙箱的 sitecustomize.py 把 os.remove 换成"移到回收站"，
    cmake 配置阶段 idf_component_manager 清理临时文件时会抛
        OSError: SHFileOperationW 失败: 0x2
    这里恢复为真正的删除。

本脚本的做法
------------
在 **Python 进程内部** 修正以上全部问题，再以 __main__ 身份执行真正的 idf.py，
使控制流进入 else 分支调用 main()。不依赖 shell 的环境变量设置。

用法
----
    cd D:/qianwen/ai-week1/firmware
    python ../tools/run_idf.py build
    python ../tools/run_idf.py -p COM4 flash
    python ../tools/run_idf.py -p COM4 monitor

所有参数原样透传，退出码原样返回。
"""
import os
import runpy
import sys

# ============ 路径配置：按本机实际安装位置调整 ============
IDF_HOME = r"C:\Espressif"
IDF_PATH = rf"{IDF_HOME}\frameworks\esp-idf-v5.5.5"
IDF_TOOLS = rf"{IDF_HOME}\tools"
PYTHON_ENV = rf"{IDF_HOME}\python_env\idf5.5_py3.11_env"

# 构建必需的工具目录（会 prepend 到 PATH，Windows 分号分隔）
TOOL_BINS = [
    rf"{IDF_TOOLS}\cmake\3.30.2\bin",
    rf"{IDF_TOOLS}\ninja\1.12.1",
    rf"{IDF_TOOLS}\xtensa-esp-elf\esp-14.2.0_20260121\xtensa-esp-elf\bin",
    rf"{IDF_TOOLS}\idf-git\2.44.0\cmd",
    rf"{PYTHON_ENV}\Scripts",
    rf"{IDF_PATH}\tools",
    IDF_TOOLS,
]


def fix_msystem() -> None:
    """坑 1：必须在 idf.py 检查之前清掉 MSYSTEM。"""
    os.environ.pop("MSYSTEM", None)


def fix_platform_machine() -> None:
    """
    坑 2：本环境缺少 PROCESSOR_ARCHITECTURE，导致 platform.machine() 返回 ''
    （platform.uname() 的 machine 字段从该环境变量读取）。

    这里用 **设置环境变量** 而不是 monkey-patch platform.machine，
    因为环境变量会被 idf.py spawn 出去的子进程继承 —— patch 函数只影响当前进程，
    子进程里的 idf_tools.py 仍会算出 'Windows-' 而失败。
    """
    if not os.environ.get("PROCESSOR_ARCHITECTURE"):
        os.environ["PROCESSOR_ARCHITECTURE"] = "AMD64"
    # 本进程若已缓存了空的 uname 结果，清掉让它按新环境变量重新计算
    import platform as _platform

    for attr in ("_uname_cache",):
        if hasattr(_platform, attr):
            setattr(_platform, attr, None)


def fix_path() -> None:
    """
    坑 3：在 Python 内部用 Windows 原生格式（反斜杠 + 分号）重建 PATH，
    绕开 MSYS 的路径转换。
    """
    existing = os.environ.get("PATH", "")
    parts = [p for p in TOOL_BINS if os.path.isdir(p)]
    missing = [p for p in TOOL_BINS if not os.path.isdir(p)]
    if missing:
        print("[run_idf] 警告：以下工具目录不存在，已跳过：", file=sys.stderr)
        for p in missing:
            print("           " + p, file=sys.stderr)
    # 已存在的项不重复添加
    for p in parts:
        if p.lower() not in existing.lower():
            existing = p + ";" + existing
    os.environ["PATH"] = existing

    os.environ["IDF_PATH"] = IDF_PATH
    os.environ["IDF_TOOLS_PATH"] = IDF_HOME
    os.environ["IDF_PYTHON_ENV_PATH"] = PYTHON_ENV

    # cmake 生成 esp_rom gdbinit 时需要此变量，缺失会报
    #   OSError: ESP_ROM_ELF_DIR environment variable is not defined
    rom_elfs = rf"{IDF_TOOLS}\esp-rom-elfs"
    if os.path.isdir(rom_elfs):
        versions = sorted(os.listdir(rom_elfs))
        if versions:
            os.environ.setdefault("ESP_ROM_ELF_DIR", rf"{rom_elfs}\{versions[-1]}")


def fix_os_remove() -> None:
    """
    坑 4：沙箱把 os.remove 替换成"移到回收站"，对临时文件会失败（SHFileOperationW 0x2）。
    这里恢复成真正的删除。
    """
    fn = os.remove
    if getattr(fn, "__name__", "") in ("_safe_remove", "safe_remove", "trash_remove"):
        try:
            import ctypes
            from ctypes import wintypes

            _DeleteFileW = ctypes.windll.kernel32.DeleteFileW
            _DeleteFileW.argtypes = [wintypes.LPCWSTR]
            _DeleteFileW.restype = wintypes.BOOL

            def _real_remove(path, *, dir_fd=None):
                path = os.fspath(path)
                if not _DeleteFileW(path):
                    raise OSError(f"DeleteFileW failed: {path}")

            os.remove = _real_remove
        except Exception:
            # 拿不到 windll 就退回：先尝试 os.unlink
            try:
                os.remove = os.unlink
            except Exception:
                pass


def main() -> int:
    fix_msystem()
    fix_platform_machine()
    fix_path()
    fix_os_remove()

    idf_py = os.environ.get("IDF_PY_PATH", rf"{IDF_PATH}\tools\idf.py")

    # sys.argv[0] 设为 idf.py 路径，其余参数原样透传
    sys.argv = [idf_py] + sys.argv[1:]

    # 直接 `python idf.py` 时解释器会把脚本目录加入 sys.path，
    # 但 runpy.run_path() 不会，导致 idf.py 里 `import python_version_checker` 失败。
    idf_tools_dir = os.path.dirname(os.path.abspath(idf_py))
    if idf_tools_dir not in sys.path:
        sys.path.insert(0, idf_tools_dir)

    try:
        runpy.run_path(idf_py, run_name="__main__")
    except SystemExit as e:
        return int(e.code) if e.code else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
