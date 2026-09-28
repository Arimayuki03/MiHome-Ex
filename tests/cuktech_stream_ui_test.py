# SPDX-License-Identifier: GPL-3.0-or-later
"""M4 接线自测：MainWindow + CuktechEventStream 推送优先/轮询兜底链路。

用法: .venv\\Scripts\\python.exe tests/cuktech_stream_ui_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_stream_ui_test）

离屏运行（QT_QPA_PLATFORM=offscreen）。用 http.server 起假 SSE 端点
（仿 tests/cuktech_events_test.py）+ 假米家 api（仿 tests/facade_test.py
的 _init_api 覆写），把真实 CuktechEventStream 接进真实 MainWindow：
1. 出现 CUKTECH 卡片时 SSE 流懒启动；
2. port_update 推送 → 卡片功率文案即时更新、轮询计时器被重置；
3. connected_changed(False) → 5s 消抖（覆盖服务端 120s 硬超时的常态
   重连抖动，窗口内不闪断）；到期才离线灰置；init/True 取消消抖并恢复；
4. settings 推送 → 打开中的面板端口开关位跟随更新；
5. 窗口 force_quit 关闭 → SSE 线程与 jobs 线程 join 干净退出
   （atexit 兜底 join，解释器关闭无 "QThread: Destroyed" 警告）。

推送路径断言用直接 emit 信号做确定性校验（不经网络时序），
懒启动与收尾用真实假服务端到端走一遍。
"""

import atexit
import copy
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体（详见 tests/cuktech_panel_test.py 顶部注释）
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_stream_ui_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

# 测试禁写运行数据：仿 theme_test，防假设备经任何回写路径泄漏
from app.core import cache as _device_cache  # noqa: E402
_device_cache.save = lambda *a, **k: None

from app.core import cuktech_events as ce  # noqa: E402

# 加速测试的重连退避（模块方法体运行期读模块属性，直接改写）
ce.INITIAL_BACKOFF = 0.2
ce.MAX_BACKOFF = 1.0

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# 假 SSE 服务：/api/events + /api/status + /api/charge-limits + /api/chart
# ---------------------------------------------------------------------------


def _encode_event(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"


_STATUS_OK: dict[str, Any] = {
    "connected": True,
    "authenticated": True,
    "ports": {
        "1": {"voltage": 20.0, "current": 3.25, "power": 65.0,
              "active": True, "protocol": "PD", "enabled": True},
        "2": {"voltage": 0.0, "current": 0.0, "power": 0.0,
              "active": False, "protocol": "idle", "enabled": True},
        "3": {"voltage": 0.0, "current": 0.0, "power": 0.0,
              "active": False, "protocol": "idle", "enabled": True},
        "4": {"voltage": 5.0, "current": 0.3, "power": 1.5,
              "active": True, "protocol": "5V", "enabled": True},
    },
    "settings": {"16": 15},
    "device_model": "CUKTECH-10U",
    "firmware_version": "1.2.3",
}

_LIMITS_OK: dict[str, Any] = {
    "ok": True,
    "limits": {
        "c1": {"wh": 100.0, "mode": "once", "fired": False,
               "session_wh": 37.5, "is_charging": True},
        "c2": {"wh": 0.0, "mode": "once", "fired": False,
               "session_wh": 0.0, "is_charging": False},
        "c3": {"wh": 0.0, "mode": "once", "fired": False,
               "session_wh": 0.0, "is_charging": False},
        "a": {"wh": 0.0, "mode": "once", "fired": False,
              "session_wh": 0.0, "is_charging": False},
    },
}

_CHART_OK: dict[str, Any] = {
    "ok": True,
    "labels": ["14:00", "14:01"],
    "datasets": {"power": [{"label": "Total", "data": [0.0, 65.0]}]},
}


class _FakeHandler(BaseHTTPRequestHandler):
    """SSE 端点 + 轮询兜底端点（面板 refresh_data 仍会拉这些）。"""

    def do_GET(self) -> None:  # noqa: N802
        base = self.path.split("?")[0]
        if base == "/api/events":
            self._serve_events()
            return
        routes = {
            "/api/status": (200, _STATUS_OK),
            "/api/charge-limits": (200, _LIMITS_OK),
            "/api/chart": (200, _CHART_OK),
            "/api/health": (200, {"ok": True, "healthy": True}),
        }
        if base not in routes:
            self.send_response(404)
            self.end_headers()
            return
        status, payload = routes[base]
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self) -> None:
        fake = self.server.fake  # type: ignore[attr-defined]
        with fake.lock:
            fake.connection_count += 1
            backlog = fake.pending
            fake.pending = []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # 连接建立即推 init（契约：服务端第一条）
        try:
            self.wfile.write(
                _encode_event({"type": "init", **copy.deepcopy(_STATUS_OK),
                               "mqtt_connected": False}).encode("utf-8"))
            self.wfile.flush()
        except OSError:
            return
        fake.client_connected.release()
        try:
            while True:
                with fake.lock:
                    if fake.drop_once:
                        fake.drop_once = False
                        return
                    item = fake.pending.pop(0) if fake.pending else None
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
    """ThreadingHTTPServer 包装：事件队列 + 连接计数 + drop 开关。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connection_count = 0
        self.pending: list[str] = []
        self.drop_once = False
        self.client_connected = threading.Semaphore(0)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def queue_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.pending.append(_encode_event(event))

    def start(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
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
# 假米家 api + service：云端零网络，cuktech 客户端指向假服务
# ---------------------------------------------------------------------------

_CLOUD_DEVICES = [
    {"did": "1001", "name": "客厅灯", "model": "yeelink.light.lamp1",
     "isOnline": False},
]
_HOMES = [{"name": "我的家", "roomlist": [{"name": "客厅", "dids": ["1001"]}]}]


class _FakeMijiaAPI:
    available = True

    def get_homes_list(self):
        return _HOMES

    def get_devices_list(self):
        return list(_CLOUD_DEVICES)

    def get_shared_devices_list(self):
        return []


def _make_service(sse_url: str):
    from app.core.cuktech_client import CuktechClient
    from app.core.service import MijiaService

    class _FakeService(MijiaService):
        def _init_api(self):
            return _FakeMijiaAPI()

    # timeout 调小：面板兜底轮询打到假服务几乎零开销
    return _FakeService(cuktech=CuktechClient(base_url=sse_url, timeout=2.0))


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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{message}（{timeout}s 超时）")


def main() -> int:
    from app.core.models import DeviceInfo
    from app.core.jobs import JobExecutor
    from app.ui.cuktech_panel import CuktechDeviceCard, CuktechPanel
    from app.ui.main_window import MainWindow
    from app.ui import si_theme

    si_theme.set_theme("dark")
    fake = _FakeSSEServer()
    url = fake.start()
    service = _make_service(url)
    jobs = JobExecutor()
    # 收尾兜底：断言失败跳过文件尾 shutdown 时，工作线程还活着，
    # 解释器关闭阶段销毁 QThread 即报 "Destroyed while thread is running"
    atexit.register(jobs.shutdown)

    # MainWindow 构造期会创建自己的 MijiaService/JobExecutor 并启动
    # jobs 工作线程（构造即 start）；这里创建前先替换 service 的
    # cuktech 客户端指向假服务（SSE 流懒创建，_base_url 会取到假地址），
    # 原生 jobs 线程在 close 后单独停掉（见收尾处 original_jobs）。
    win = MainWindow()
    original_jobs = win._jobs
    win._service = service
    win._jobs = jobs
    atexit.register(original_jobs.shutdown)

    try:
        cuk_did = "cuktech-local"
        devs = [DeviceInfo(did="1001", name="客厅灯", model="yeelink.light.lamp1",
                           home_name="我的家", room_name="客厅", online=False),
                DeviceInfo(did=cuk_did, name="CUKTECH-10U",
                           model="CUKTECH-10U", home_name="本地", room_name="本地",
                           online=True, source="local")]
        win.show()
        app.processEvents()
        win._apply_devices(devs)
        app.processEvents()

        # ---------- 1. 懒启动：出现 CUKTECH 卡片即启动 SSE 流 ----------
        card = win._cards.get(cuk_did)
        assert isinstance(card, CuktechDeviceCard), type(card)
        expect_equal("出现 CUKTECH 卡片即创建单实例流",
                     isinstance(win._cuktech_stream, ce.CuktechEventStream), True)
        wait_until(lambda: win._cuktech_stream._thread.isRunning(),
                   message="SSE 流线程未启动")
        check("懒启动：流线程已运行")
        wait_until(lambda: fake.connection_count >= 1,
                   message="假服务未收到 SSE 连接")
        # 流地址与轮询客户端一致（同一网关服务）
        expect_equal("SSE 流地址跟随 service 注入的客户端",
                     win._cuktech_stream._base_url, url)
        # start 幂等：再次触发不报错不新建线程
        win._maybe_start_cuktech_stream()
        check("_maybe_start_cuktech_stream 幂等")

        # ---------- 2. port_update 推送 → 卡片功率即时更新 ----------
        stream = win._cuktech_stream
        # 卡片先有基线状态（走一次真实轮询，面板/卡片兜底路径同时验证）
        card._poll_once()
        wait_until(lambda: card._status is not None, message="卡片轮询未回填")
        app.processEvents()
        expect_equal("轮询基线：卡片总功率文案",
                     card._watts_label.text(), "66.5W")  # 65.0 + 1.5
        # 推送应重置轮询计时器（回到满周期）：先让剩余时间衰减，
        # 推送后剩余时间应回到 ≈5000ms
        assert card._poll_timer.isActive(), "卡片轮询计时器未运行"
        time.sleep(0.35)
        # sleep 期间 jobs 工作线程可能仍有迟到的轮询回调（如流 init 触发的
        # 兜底轮询）在队列里；processEvents 派发干净，避免它在推送之后
        # 才落回主线程、用旧状态整帧覆盖推送合并结果（时序竞态）
        app.processEvents()
        pre_reset = card._poll_timer.remainingTime()
        assert pre_reset < 4700, f"计时器未衰减: {pre_reset}"

        port_payload = {"type": "port_update", "port_id": 2, "port": "c2",
                        "data": {"voltage": 9.0, "current": 2.0, "power": 18.0,
                                 "active": True, "protocol": "QC", "enabled": True}}
        stream.port_event.emit(copy.deepcopy(port_payload))
        app.processEvents()
        expect_equal("port_update 推送后卡片总功率更新",
                     card._watts_label.text(), "84.5W")  # 66.5 + 18
        post_reset = card._poll_timer.remainingTime()
        expect_equal("推送重置了卡片轮询计时器（回到满周期）",
                     post_reset > pre_reset and post_reset > 4800, True)
        # 推送不回写 service 缓存：假服务的 /api/status 原样未动
        expect_equal("推送未篡改服务端状态（仅 UI 内存合并）",
                     _STATUS_OK["ports"]["2"]["power"], 0.0)

        # ---------- 3. connected_changed(False) → 5s 消抖，不立刻灰置 ----------
        # 服务端 120s 硬超时 → 客户端 2s 起步重连是常态抖动：断开只启动
        # 消抖定时器，消抖窗口内不灰置；真离线（定时器到期）才转灰；
        # 期间重连成功（True/init）则取消定时器恢复在线。
        # 卡片自身 5s 轮询兜底会把假服务的 connected=True 渲染回在线
        # （兜底路径本身正确），本节隔离验证 SSE 消抖机制，先停掉卡片
        # 轮询，结束再恢复。
        card._poll_timer.stop()
        stream.connected_changed.emit(False)
        app.processEvents()
        expect_equal("connected(False) 消抖窗口内卡片仍在线",
                     card._online, True)
        expect_equal("connected(False) 消抖窗口内功率保留",
                     card._watts_label.text(), "84.5W")
        # 消抖定时器确已启动
        expect_equal("connected(False) 启动了断开消抖定时器",
                     win._cuktech_stream_down_timer.isActive(), True)
        # 窗口内重连成功 → 定时器取消、在线恢复
        stream.connected_changed.emit(True)
        app.processEvents()
        expect_equal("窗口内 connected(True) 取消消抖定时器",
                     win._cuktech_stream_down_timer.isActive(), False)
        expect_equal("重连后卡片在线", card._online, True)

        # 真离线：消抖定时器到期 → 卡片离线灰置。先把真实流彻底 stop
        # （假服务一直活着，真流 2s 退避就重连成功并 emit True/init，
        # 会把在线拉回去——这恰是"快速重连不闪断"的生产行为），
        # 保证这段没有真实重连竞争，只验证消抖定时器路径。
        stream.stop()
        app.processEvents()
        stream.connected_changed.emit(False)
        wait_until(lambda: not win._cuktech_stream_down_timer.isActive(),
                   timeout=8.0, message="消抖定时器未到期")
        app.processEvents()
        expect_equal("消抖到期后卡片 _online 为 False",
                     card._online, False)
        expect_equal("消抖到期后卡片提示断开文案",
                     card._status_label.text(), "实时数据已断开")
        expect_equal("消抖到期后功率清空",
                     card._watts_label.text(), "-- W")
        stream.connected_changed.emit(True)
        app.processEvents()
        expect_equal("connected(True) 恢复在线",
                     card._online, True)
        # SSE 消抖隔离段结束，恢复卡片轮询（后续面板节依赖它回填）
        card._poll_timer.start()

        # init 事件（服务端每次连接建立即推的全量快照）：既恢复在线又
        # 整帧刷新状态，并取消消抖定时器（重连成功的最强证据）
        stream.connected_changed.emit(False)
        app.processEvents()
        assert win._cuktech_stream_down_timer.isActive()
        init_payload = dict(copy.deepcopy(_STATUS_OK), type="init")
        init_payload["ports"]["1"]["power"] = 40.0
        stream.generic_event.emit("init", copy.deepcopy(init_payload))
        app.processEvents()
        expect_equal("init 事件取消消抖定时器",
                     win._cuktech_stream_down_timer.isActive(), False)
        expect_equal("init 事件后卡片在线", card._online, True)
        expect_equal("init 事件整帧刷新卡片功率（40.0+1.5）",
                     card._watts_label.text(), "41.5W")

        # ---------- 4. 打开面板：settings 推送 → 开关位跟随 ----------
        panel = CuktechPanel(service, jobs, next(
            d for d in devs if d.did == cuk_did))
        panel.resize(860, 600)
        panel.show_device(cuk_did, online=True)
        win._open_cuktech_panels.append(panel)
        wait_until(lambda: panel._status is not None and panel._limits is not None,
                   message="面板兜底轮询未回填")
        app.processEvents()
        expect_equal("面板基线：总功率",
                     panel._watts_label.text(), "66.5W")
        # 旧 _port_rows 四行布局已由 PortCardGrid 2×2 端口卡承接，
        # 断言语义不变：路径改为 _port_grid._cards[port]["switch"]
        expect_equal("面板基线：端口开关位（settings['16']=15 全开）",
                     panel._port_grid._cards[3]["switch"].isChecked(), True)

        settings_payload = {"type": "settings", "settings": {"16": 0}}
        stream.settings_event.emit(copy.deepcopy(settings_payload))
        app.processEvents()
        # 位图 0 → 全部端口 disabled；端口卡开关同时被 connected 与 enabled 驱动
        expect_equal("settings 推送（位图 0）后端口行开关关闭",
                     panel._port_grid._cards[1]["switch"].isChecked(), False)
        expect_equal("位图 0 同步关闭其余端口行",
                     panel._port_grid._cards[4]["switch"].isChecked(), False)
        # 位图恢复 0x0F：端口行恢复开启
        stream.settings_event.emit({"type": "settings", "settings": {"16": 15}})
        app.processEvents()
        expect_equal("settings 推送（位图 15）恢复端口行开关",
                     panel._port_grid._cards[1]["switch"].isChecked(), True)
        # 限额区不随 settings 推送变化（limits 走轮询）：进度行是
        # ChargeLimitStack 的 bar/progress 组合，wh=100 时进度文字可见
        expect_equal("settings 推送不触碰限额显示",
                     panel._limit_stack._rows[1]["progress"].text(),
                     "已充 37.5 / 100 Wh")

        # status 推送：面板整帧替换渲染
        status_payload = dict(copy.deepcopy(_STATUS_OK), type="status")
        status_payload["ports"]["1"]["power"] = 30.0
        stream.status_event.emit(copy.deepcopy(status_payload))
        app.processEvents()
        expect_equal("status 推送后面板总功率整帧更新",
                     panel._watts_label.text(), "31.5W")  # 30.0 + 1.5
        expect_equal("status 推送同步到卡片（卡片同屏刷新）",
                     card._watts_label.text(), "31.5W")

        # ---------- 5. 面板关闭注销 + 窗口 force_quit 停流 ----------
        win._open_cuktech_panels.remove(panel)
        panel.hide()
        panel.deleteLater()
        app.processEvents()

        # 懒启动路径上卡片在网格重建后依旧有流可用：重建网格不崩
        win._rebuild_grid()
        app.processEvents()
        assert isinstance(win._cards.get(cuk_did), CuktechDeviceCard)
        check("网格重建后专用卡片仍存在且流未受影响")

        win._all_devices = []  # 关闭路径不保存假设备
        win._force_quit = True  # 对齐托盘菜单「退出」的真实退出分支
        win.close()
        app.processEvents()
        expect_equal("force_quit 关闭后 SSE 流已停并置空",
                     win._cuktech_stream, None)
        expect_equal("关闭后面板投递名单已清空",
                     win._open_cuktech_panels, [])
        expect_equal("closeEvent 停掉了窗口注入的 jobs 线程",
                     jobs._thread.isRunning(), False)
        check("窗口真实退出路径：SSE 线程 join 干净退出")
    finally:
        # 断言失败时的兜底清理（与 closeEvent 同一停流路径）
        if win._cuktech_stream is not None:
            win._cuktech_stream.stop()
            win._cuktech_stream = None
        jobs.shutdown()
        original_jobs.shutdown()
        fake.stop()

    print(f"\n全部通过：{_PASS_COUNT} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
