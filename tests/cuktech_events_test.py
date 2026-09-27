# SPDX-License-Identifier: GPL-3.0-or-later
"""CuktechEventStream 自测：用 http.server 起假 SSE 端点，不依赖真实服务。

用法: .venv\\Scripts\\python.exe tests/cuktech_events_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_events_test）

覆盖：SSE 多行 data 聚合与注释行忽略、坏 JSON 容错、type 分发到对应
信号（init→generic、status→status_event 等）、服务中途断开→指数退避
自动重连→继续收事件（假服务计数连接次数）、stop() 后线程干净退出、
stop 后可再次 start。需要 PySide6（被测模块 import Qt 信号）；本 venv
的 PySide6 缺失时本脚本无法运行，语法层验证用：
    .venv\\Scripts\\python.exe -m py_compile app/core/cuktech_events.py
"""

import atexit
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 允许直接以文件方式运行（python tests/cuktech_events_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.core import cuktech_events as ce  # noqa: E402
from app.core.cuktech_events import CuktechEventStream  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
# 让模块的 debug 重连日志可见，便于排查
logging.getLogger("app.core.cuktech_events").setLevel(logging.DEBUG)

# 加速测试的退避参数（模块方法体运行期读模块属性，直接改写）
ce.INITIAL_BACKOFF = 0.2
ce.MAX_BACKOFF = 1.0

# ---------------------------------------------------------------------------
# 假 SSE 服务：/api/events 端点，连接计数 + 待推事件队列 + 一次性断开开关
# ---------------------------------------------------------------------------


def _encode_event(event: dict[str, Any], keepalive: bool = True) -> str:
    """事件 dict → SSE 文本块（data: 行 + 可选 keepalive 注释 + 空行）。"""
    body = f"data: {json.dumps(event, ensure_ascii=False)}\r\n"
    if keepalive:
        body += ": keepalive\r\n"
    return body + "\r\n"


class _FakeSSEHandler(BaseHTTPRequestHandler):
    """SSE 端点：连接即计数并先推 init，再循环吐事件队列。

    断开控制：``drop_once`` 置位后，处理完当前写入即主动结束本连接
    （模拟服务端 120s 硬超时掐断），随后自动复位只影响一次。
    """

    drop_once = False

    def do_GET(self) -> None:
        fake = self.server.fake  # type: ignore[attr-defined]
        if self.path.split("?")[0] != "/api/events":
            self.send_response(404)
            self.end_headers()
            return
        with fake.lock:
            fake.connection_count += 1
            init = _encode_event({"type": "init", "connected": True,
                                  "ports": {}, "settings": {}})
            backlog = fake.pending
            fake.pending = []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b"retry: 1000\r\n\r\n")  # 客户端应忽略的 SSE 字段
        self.wfile.write(init.encode("utf-8"))
        self.wfile.flush()
        fake.client_connected.release()
        try:
            while True:
                if _FakeSSEHandler.drop_once:
                    _FakeSSEHandler.drop_once = False
                    return  # 主动断开（模拟服务端掐连接）
                item = None
                with fake.lock:
                    if fake.pending:
                        item = fake.pending.pop(0)
                if item is None:
                    time.sleep(0.02)
                    continue
                self.wfile.write(item.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


class _FakeSSEServer:
    """ThreadingHTTPServer 包装：共享连接计数与事件队列。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connection_count = 0
        self.pending: list[str] = []  # 待推送给下一个连接的事件队列
        # 有客户端连上后由 handler 置位，供测试同步“连接已建立”
        self.client_connected = threading.Semaphore(0)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def queue_event(self, event: dict[str, Any]) -> None:
        """向队列投递一条 SSE 格式化事件（下个连接建立后即推）。"""
        with self.lock:
            self.pending.append(_encode_event(event))

    def queue_raw(self, raw_lines: list[str]) -> None:
        """投递原始行（测试多行 data 聚合 / 坏 JSON 等底层格式）。"""
        with self.lock:
            self.pending.append("\r\n".join(raw_lines) + "\r\n\r\n")

    def drop_once(self) -> None:
        """让当前（或下一个）连接被服务端主动掐断一次。"""
        _FakeSSEHandler.drop_once = True

    def start(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSSEHandler)
        self.server.daemon_threads = True
        self.server.fake = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------

_PASS_COUNT = 0


def check(name: str) -> None:
    global _PASS_COUNT
    _PASS_COUNT += 1
    print(f"[PASS] {name}")


def expect_equal(name: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: 期望 {expected!r}，实际 {actual!r}")
    check(name)


def wait_until(predicate, timeout: float = 5.0, message: str = "条件未满足") -> None:
    """轮询等待条件成立（Qt 信号跨线程排队需主线程事件循环处理）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{message}（{timeout}s 超时）")


class _SignalRecorder:
    """把流的所有信号接到列表，记录 (信号名, 参数元组) 便于断言。"""

    def __init__(self, stream: CuktechEventStream) -> None:
        self.entries: list[tuple[str, tuple]] = []
        stream.connected_changed.connect(
            lambda v: self.entries.append(("connected", (v,))))
        stream.status_event.connect(
            lambda d: self.entries.append(("status", (d,))))
        stream.port_event.connect(
            lambda d: self.entries.append(("port_update", (d,))))
        stream.settings_event.connect(
            lambda d: self.entries.append(("settings", (d,))))
        stream.generic_event.connect(
            lambda t, d: self.entries.append(("generic:" + t, (t, d))))

    def of_type(self, name: str) -> list[tuple]:
        return [args for entry, args in self.entries if entry == name]

    def reset(self) -> None:
        self.entries.clear()


def main() -> int:
    QCoreApplication(sys.argv)
    fake = _FakeSSEServer()
    url = fake.start()
    stream = CuktechEventStream(base_url=url)
    rec = _SignalRecorder(stream)

    # 收尾兜底：断言失败或 finally 之后解释器关闭阶段，若 SSE 工作线程
    # 仍在运行，销毁 QThread 即报 "QThread: Destroyed while thread is
    # still running"。atexit 先于 Qt 对象析构执行 stop()（内部 join）。
    atexit.register(stream.stop)

    try:
        # ---------- 基本连接与事件分发 ----------
        stream.start()
        wait_until(lambda: rec.of_type("connected")
                   and rec.of_type("connected")[-1][0] is True,
                   message="未收到 connected(True)")
        wait_until(lambda: fake.connection_count >= 1, message="假服务未收到连接")
        check("start() 后连接建立并广播 connected(True)")

        # init 事件：服务端连接后立即推，应落到 generic_event("init", ...)
        wait_until(lambda: rec.of_type("generic:init"), message="未收到 init 事件")
        init_type, init_payload = rec.of_type("generic:init")[-1]
        expect_equal("init 事件 type 名为 'init'", init_type, "init")
        assert init_payload.get("connected") is True
        check("init 事件 payload 完整（走 generic_event）")

        # handler 连接时已发 retry: 行；客户端按规范忽略它，未崩未误派发
        check("SSE 其余字段（retry: 等）被忽略")

        # status 事件
        fake.queue_event({"type": "status", "connected": True,
                          "authenticated": True, "ports": {"1": {"power": 65.0}},
                          "settings": {}})
        wait_until(lambda: rec.of_type("status"), message="未收到 status 事件")
        st = rec.of_type("status")[-1][0]
        assert st["connected"] is True and st["ports"]["1"]["power"] == 65.0
        check("status 事件分发到 status_event 且 payload 正确")

        # port_update 事件（多行 data 聚合：同一事件 data 拆成两行）
        port_payload = {"type": "port_update", "port_id": 1, "port": "c1",
                        "data": {"voltage": 20.0, "current": 3.25, "power": 65.0,
                                 "active": True, "protocol": "PD", "enabled": True}}
        raw = json.dumps(port_payload, ensure_ascii=False)
        half = raw.index(", \"data\"")  # 顶层逗号处切分，两段各自合法
        fake.queue_raw(["data: " + raw[:half], "data: " + raw[half:]])
        wait_until(lambda: rec.of_type("port_update"), message="未收到 port_update 事件")
        pu = rec.of_type("port_update")[-1][0]
        assert pu == port_payload, pu
        check("多行 data: 聚合为一条 port_update 事件且 JSON 还原正确")

        # settings 事件
        fake.queue_event({"type": "settings", "settings": {"16": 15}})
        wait_until(lambda: rec.of_type("settings"), message="未收到 settings 事件")
        assert rec.of_type("settings")[-1][0]["settings"] == {"16": 15}
        check("settings 事件分发到 settings_event")

        # 其余已知类型走 generic_event：session_end
        fake.queue_event({"type": "session_end", "session_id": 7, "port": "c1",
                          "port_id": 1, "total_wh": 12.5, "peak_power_w": 65.0,
                          "duration_sec": 300})
        wait_until(lambda: rec.of_type("generic:session_end"),
                   message="未收到 session_end 事件")
        se_type, se_payload = rec.of_type("generic:session_end")[-1]
        assert se_type == "session_end" and se_payload["total_wh"] == 12.5
        check("session_end 走 generic_event(type, payload)")

        # 未知类型也走 generic_event
        fake.queue_event({"type": "future_thing", "x": 1})
        wait_until(lambda: rec.of_type("generic:future_thing"),
                   message="未收到未知类型事件")
        check("未知类型走 generic_event")

        # 坏 JSON：记警告跳过，不影响后续事件
        rec.reset()
        fake.queue_raw(["data: {not valid json!!"])
        fake.queue_event({"type": "quality", "ble": {"score": 100}})
        wait_until(lambda: rec.of_type("generic:quality"),
                   message="坏 JSON 后的事件未收到")
        assert not rec.of_type("status") and not rec.of_type("port_update")
        check("坏 JSON 被容错跳过，后续事件正常分发")

        # 所有事件都夹带 keepalive 注释行；若注释被误当事件派发，
        # 上面的计数早已错乱（空 data 行静默忽略）
        check("注释行(: keepalive)不产生事件")

        # ---------- 断开 → 自动重连 → 继续收事件 ----------
        rec.reset()
        before_count = fake.connection_count
        # 服务端主动掐断连接（模拟 120s 硬超时）
        fake.drop_once()
        wait_until(lambda: rec.of_type("connected")
                   and rec.of_type("connected")[-1][0] is False,
                   timeout=8.0, message="断开后未收到 connected(False)")
        check("服务端断开 → connected(False)")
        # 指数退避（测试加速到 0.2s 起步）后应自动重连成功
        wait_until(lambda: rec.of_type("connected")[-1][0] is True
                   and fake.connection_count > before_count,
                   timeout=8.0, message="未自动重连成功")
        check(f"自动重连成功（服务端累计连接 {fake.connection_count} 次）")

        # 重连后的连接照常收事件（走的是第二个连接）
        fake.queue_event({"type": "status", "connected": False, "ports": {},
                          "settings": {}})
        wait_until(lambda: rec.of_type("status"), message="重连后未收到新事件")
        assert rec.of_type("status")[-1][0]["connected"] is False
        check("重连后继续接收事件")

        # ---------- stop() 干净退出 ----------
        rec.reset()
        stream.stop()
        assert not stream._thread.isRunning()
        check("stop() 后工作线程已退出（join 成功）")
        # 工作线程退出前发出的 connected(False) 走跨线程排队投递，
        # 需主线程转一次事件循环才送达
        wait_until(lambda: rec.of_type("connected"),
                   message="stop() 未广播 connected(False)")
        expect_equal("stop() 广播 connected(False)",
                     rec.of_type("connected")[-1][0], False)
        expect_equal("is_connected() 复位为 False", stream.is_connected(), False)

        # stop 后再 start 可重启（回归保护：QThread 复用）
        stream.start()
        wait_until(lambda: rec.of_type("connected")
                   and rec.of_type("connected")[-1][0] is True,
                   timeout=8.0, message="stop 后 start 未重新连接")
        check("stop() 后可重新 start()")
        stream.stop()
        assert not stream._thread.isRunning()
        check("第二次 stop() 同样干净退出")
    finally:
        stream.stop()
        fake.stop()

    print(f"\n全部通过：{_PASS_COUNT} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
