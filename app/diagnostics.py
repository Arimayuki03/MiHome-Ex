# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""源码模式下的运行时诊断输出。

崩溃时控制台窗口随进程一起消失，事后既看不到 Python 的 traceback，
也看不到 C++ 层的致命错误——段错误这类故障根本不经过 logging，
只有 faulthandler 能在进程死掉前留下调用栈。这里把标准输出与错误
双写进文件，同时接管未捕获异常，方便事后定位问题。
"""

import faulthandler
import os
import sys
import threading
import traceback
from pathlib import Path

# 保持文件句柄与原始流的引用：函数返回后被回收，faulthandler 会失效
_state: dict = {"file": None, "streams": None}


class _ConsoleWriter:
    """按原始文件描述符写控制台，绕过被重定向的 1/2 号描述符。"""

    def __init__(self, fd: int):
        self._fd = fd

    def write(self, text):
        try:
            # 控制台已关闭时写入会失败，静默忽略；日志文件那一侧仍然完整
            os.write(self._fd, text.encode("utf-8", "replace"))
        except OSError:
            pass
        return len(text)

    def flush(self):
        return None

    def isatty(self):
        try:
            return os.isatty(self._fd)
        except OSError:
            return False


class _Tee:
    """把写入同时投递到控制台与文件，任一侧失败都不影响另一侧。"""

    def __init__(self, console, handle):
        self._console = console
        self._handle = handle

    def write(self, text):
        if self._console is not None:
            try:
                self._console.write(text)
            except Exception:
                pass
        try:
            self._handle.write(text)
            self._handle.flush()
        except Exception:
            pass
        return len(text)

    def flush(self):
        for target in (self._console, self._handle):
            try:
                if target is not None:
                    target.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self._console and self._console.isatty())
        except Exception:
            return False

    def fileno(self):
        # 某些库会拿 fileno 判断是否终端；没有控制台时如实报错
        if self._console is None:
            raise OSError("no console available")
        return self._console.fileno()


def setup_console_log(path: Path) -> None:
    """把控制台输出与崩溃堆栈写入 path；重复调用只生效一次。"""
    if _state["file"] is not None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 行缓冲：崩溃时最多丢一行，不会整块留在缓冲区里
        handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return
    _state["file"] = handle
    # 先把原始描述符复制一份给控制台用，再把 1/2 号指向日志文件：
    # Qt 的 C++ 层直接写描述符，不经过 Python 的 sys.stderr，
    # 只有在描述符层重定向才能把它的警告也收进日志
    try:
        console_out = _ConsoleWriter(os.dup(1))
        console_err = _ConsoleWriter(os.dup(2))
        log_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
    except OSError:
        console_out = sys.stdout
        console_err = sys.stderr
    _state["streams"] = (console_out, console_err)
    # 每次启动写一条分隔线：日志是追加的，没有标记很难判断是哪次运行
    import time

    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    handle.flush()

    # faulthandler 直接写裸文件对象：进程被信号终止时 Python 层已经不可用
    try:
        faulthandler.enable(file=handle, all_threads=True)
    except Exception:
        pass

    sys.stdout = _Tee(_state["streams"][0], handle)
    sys.stderr = _Tee(_state["streams"][1], handle)

    original_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            handle.write("\n--- 未捕获异常 ---\n")
            traceback.print_exception(exc_type, exc, tb, file=handle)
            handle.flush()
        except Exception:
            pass
        if original_hook is not None:
            original_hook(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thread_hook(args):
        name = args.thread.name if args.thread is not None else "unknown"
        try:
            handle.write(f"\n--- 线程异常 {name} ---\n")
            traceback.print_exception(
                args.exc_type, args.exc_value, args.exc_traceback, file=handle)
            handle.flush()
        except Exception:
            pass

    threading.excepthook = _thread_hook
