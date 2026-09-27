# SPDX-License-Identifier: GPL-3.0-or-later
"""CuktechClient 自测：用 http.server 起本地假 BLE 服务，不依赖真实设备。

用法: .venv\\Scripts\\python.exe tests/cuktech_client_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_client_test）

覆盖各方法解析、端口三套命名转换、错误转中文 ServiceError（含命令类
端点 HTTP 200 + body ok=false 的离线坑）、connected() 失败折叠为 False、
连接失败与超时路径。绝不 import PySide6 / app.ui —— 本 venv 缺 PySide6，
只测本模块。
"""

import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 允许直接以文件方式运行（python tests/cuktech_client_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cuktech_client import CuktechClient, ServiceError  # noqa: E402
from app.core.cuktech_client import _key_to_port, _port_to_key  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# 假服务：按 (method, path) 注册 (status, json_body)，可运行期改写以模拟
# 离线 / 校验失败等场景；last_request 记录最近一次请求供断言
# ---------------------------------------------------------------------------

_FAKE_ROUTES: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {
    ("GET", "/api/health"): (200, {"ok": True, "healthy": True,
                                   "components": {"ble": True, "mqtt": False,
                                                  "bemfa": False},
                                   "uptime_ms": 123456}),
    ("GET", "/api/status"): (200, {
        "connected": True,
        "authenticated": True,
        "ports": {
            "1": {"voltage": 20.0, "current": 3.25, "power": 65.0, "active": True,
                  "protocol": "PD", "enabled": True},
            "2": {"voltage": 0.0, "current": 0.0, "power": 0.0, "active": False,
                  "protocol": "idle", "enabled": True},
            "3": {"voltage": 0.0, "current": 0.0, "power": 0.0, "active": False,
                  "protocol": "idle", "enabled": True},
            "4": {"voltage": 0.0, "current": 0.0, "power": 0.0, "active": False,
                  "protocol": "idle", "enabled": False},
        },
        "settings": {"5": 1, "6": 2, "16": 15, "9": 0, "10": 0, "11": 0, "12": 0},
        "protocol_extend": 8,
        "protocol_switches": {
            "c1": {"pd": True, "pps": False, "ufcs": False},
            "c2": {"pd": False, "pps": False, "ufcs": False},
            "c3": {"scp": False, "ufcs": False},
            "a": {"scp": False, "ufcs": False},
        },
        "device_model": "CUKTECH-XXX",
        "firmware_version": "1.2.3",
        "mqtt_connected": False,
        "session_recording": True,
    }),
    ("GET", "/api/charge-limits"): (200, {
        "ok": True,
        "limits": {
            "c1": {"wh": 100.0, "mode": "once", "fired": False,
                   "session_wh": 37.5, "is_charging": True},
            "c2": {"wh": 0.0, "mode": "once", "fired": False,
                   "session_wh": 0.0, "is_charging": False},
            "c3": {"wh": 0.0, "mode": "always", "fired": False,
                   "session_wh": 0.0, "is_charging": False},
            "a": {"wh": 0.0, "mode": "once", "fired": False,
                  "session_wh": 0.0, "is_charging": False},
        },
    }),
    ("GET", "/api/chart"): (200, {
        "ok": True,
        "labels": ["14:00", "14:01", "14:02"],
        "datasets": {
            "power": [{"label": "C1", "data": [0.0, 65.0, 65.0]},
                      {"label": "Total", "data": [0.0, 65.0, 65.0]}],
        },
    }),
    ("GET", "/api/sessions"): (200, {
        "sessions": [
            {"id": 42, "port": 1, "start_time": 1727000000.123, "end_time": None,
             "total_wh": 65.2, "avg_power_w": 60.1, "peak_power_w": 65.0,
             "avg_voltage": 20.0, "avg_current": 3.0, "duration_sec": 3600,
             "protocol": "PD", "is_active": True},
        ],
        "total": 1, "page": 1, "limit": 10, "pages": 1,
    }),
    ("GET", "/api/sessions/42/points"): (200, {
        "points": [{"timestamp": 1727000000.123, "voltage": 20.0,
                    "current": 3.25, "power": 65.0, "protocol": "PD"}],
    }),
    ("GET", "/api/sessions/999/points"): (404, {"ok": False,
                                                "error": "session not found"}),
    # CSV 导出：原始文本响应（str 特例走 _send_raw，Content-Type text/csv）
    ("GET", "/api/sessions/42/export"): (200, "timestamp,voltage,current,power\n"
                                           "1727000000.1,20.0,3.25,65.0\n"),
    ("GET", "/api/energy/stats"): (200, {
        "period": "today", "total_wh": 65.2, "session_count": 1,
        "avg_power_w": 60.1, "peak_power_w": 65.0, "total_duration_sec": 3600,
        "by_port": {"1": {"wh": 65.2, "count": 1, "is_active": True}},
    }),
    ("GET", "/api/energy/protocols"): (200, {
        "period": "today",
        "protocols": [{"protocol": "PD", "wh": 65.2, "count": 1, "peak_w": 65.0}],
        "total_wh": 65.2, "session_count": 1,
    }),
    ("GET", "/api/statistics/1"): (200, {
        "ok": True,
        "data": {"port": 1, "hours": 24, "samples": 10,
                 "power": {"avg": 60.0, "max": 65.0, "total_wh": 65.2}},
    }),
    # 命令类端点成功场景
    ("POST", "/api/set"): (200, {"ok": True}),
    ("POST", "/api/port"): (200, {"ok": True, "value": 14}),
    ("POST", "/api/enable"): (200, {"ok": True, "enabled": True}),
    ("POST", "/api/charge-limits"): (200, {
        "ok": True,
        "limits": {"c1": {"wh": 100.0, "mode": "once", "fired": False,
                          "session_wh": 0.0, "is_charging": False}},
    }),
    ("POST", "/api/protocol"): (200, {"ok": True}),
}

_STATUS_OK: dict[str, Any] = _FAKE_ROUTES[("GET", "/api/status")][1]


class _FakeHandler(BaseHTTPRequestHandler):
    """按路由表回 JSON；记录最近一次请求供断言 body/headers/query。

    特例：路由值是 str 时按原始文本返回（Content-Type: text/csv，
    模拟 /api/sessions/{id}/export 的附件下载响应）。
    """

    server_version = "FakeCUKTECH/1.0"
    last_request: dict[str, Any] = {}

    def _respond(self, method: str) -> None:
        path = self.path
        base = path.split("?")[0]
        _FakeHandler.last_request = {
            "method": method,
            "path": base,
            "query": path.split("?")[1] if "?" in path else "",
            "body": self._read_body(),
            "content_type": self.headers.get("Content-Type", ""),
        }
        route = (method, base)
        if route not in _FAKE_ROUTES:
            self._send(404, {"ok": False, "error": "no such route"})
            return
        status, payload = _FAKE_ROUTES[route]
        if isinstance(payload, str):
            self._send_raw(status, payload)
            return
        self._send(status, payload)

    def _read_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _send_raw(self, status: int, text: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition",
                         'attachment; filename="session.csv"')
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def do_GET(self) -> None:
        self._respond("GET")

    def do_POST(self) -> None:
        self._respond("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # 静默默认的每请求 stderr 日志


class _TimeoutHandler(BaseHTTPRequestHandler):
    """只接收请求、永不响应，用于测试客户端超时。"""

    def _hang(self) -> None:
        time.sleep(30)  # 远超客户端 timeout=0.3，客户端应先超时

    do_GET = _hang
    do_POST = _hang

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def _start_server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------

_PASS_COUNT = 0


def check(name: str) -> None:
    global _PASS_COUNT
    _PASS_COUNT += 1
    print(f"[PASS] {name}")


def expect_service_error(name: str, fn, *fragments: str) -> None:
    """断言 fn() 抛 ServiceError 且消息包含全部片段（中文校验）。"""
    try:
        fn()
    except ServiceError as exc:
        msg = str(exc)
        for frag in fragments:
            if frag not in msg:
                raise AssertionError(f"{name}: 消息缺片段 {frag!r}：{msg}") from exc
        check(f"{name} -> ServiceError: {msg}")
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{name}: 抛了错误类型 {type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"{name}: 未抛 ServiceError")


def expect_equal(name: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: 期望 {expected!r}，实际 {actual!r}")
    check(name)


def main() -> int:
    # ---------- 端口转换单元逻辑 ----------
    expect_equal("端口 int->key: 1/2/3/4",
                 [_port_to_key(i) for i in (1, 2, 3, 4)], ["c1", "c2", "c3", "a"])
    expect_equal("端口 key->int: c1/c2/c3/a（大小写不敏感）",
                 [_key_to_port(k) for k in ("c1", "C2", "c3", "a")], [1, 2, 3, 4])
    expect_service_error("端口越界 0 拒绝", lambda: _port_to_key(0), "端口号无效")
    expect_service_error("端口越界 5 拒绝", lambda: _port_to_key(5), "端口号无效")
    expect_service_error("未知端口名拒绝", lambda: _key_to_port("c9"), "未知端口名")

    # ---------- 服务不可达：连接失败与超时 ----------
    unreachable = CuktechClient(base_url="http://127.0.0.1:1", timeout=0.3)
    # Windows 防火墙对无监听端口常表现为丢包超时而非立即拒绝，两种
    # 中文消息都算正确（客户端语义：不可达=连接失败或超时）
    try:
        unreachable.status()
    except ServiceError as exc:
        msg = str(exc)
        assert "充电器服务连接失败" in msg or "充电器命令超时" in msg, msg
        check(f"不可达服务 -> 中文错误: {msg}")
    else:
        raise AssertionError("不可达服务未抛 ServiceError")
    assert unreachable.connected() is False
    check("connected() 连接失败折叠为 False")

    timeout_server, timeout_url = _start_server(_TimeoutHandler)
    try:
        slow = CuktechClient(base_url=timeout_url, timeout=0.3)
        expect_service_error("超时 -> 中文超时", slow.status, "充电器命令超时")
        assert slow.connected() is False
        check("connected() 超时折叠为 False")
    finally:
        _stop_server(timeout_server)

    # ---------- 正常服务 ----------
    server, url = _start_server(_FakeHandler)
    try:
        c = CuktechClient(base_url=url, timeout=2.0)

        status = c.status()
        assert status["connected"] is True
        assert status["ports"]["1"]["power"] == 65.0
        check("status() 解析全量快照")

        expect_equal("connected() 在线 -> True", c.connected(), True)

        health = c.health()
        assert health["ok"] is True and health["healthy"] is True
        check("health() 解析")

        limits = c.charge_limits()
        assert set(limits["limits"].keys()) == {1, 2, 3, 4}, limits["limits"].keys()
        c1 = limits["limits"][1]
        assert c1["wh"] == 100.0 and c1["session_wh"] == 37.5
        assert limits["limits"][4]["is_charging"] is False
        check("charge_limits() 键规整为 int 1-4 且字段保留")

        chart = c.chart(2.5, 15)
        assert chart["ok"] is True and "labels" in chart
        assert "hours=2.5" in _FakeHandler.last_request["query"]
        assert "interval=15" in _FakeHandler.last_request["query"]
        check("chart() 传 query 参数")

        page = c.sessions(port=2, period="week", limit=20, page=1)
        assert page["total"] == 1 and page["sessions"][0]["port"] == 1
        assert "port=c2" in _FakeHandler.last_request["query"]
        assert "period=week" in _FakeHandler.last_request["query"]
        check("sessions() 端口转 c2 并解析分页")

        pts = c.session_points(42)
        assert pts["points"][0]["voltage"] == 20.0
        check("session_points() 解析点列")

        expect_equal("session_points() 404 -> None", c.session_points(999), None)

        # ---------- CSV 导出（/api/sessions/{id}/export） ----------
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "s42.csv"
            out = c.export_session_csv(42, target)
            expect_equal("export_session_csv() 返回写入路径", out, target)
            assert target.exists(), "CSV 文件应已落盘"
            content = target.read_text(encoding="utf-8")
            assert "timestamp,voltage,current,power" in content, content
            assert "1727000000.1,20.0,3.25,65.0" in content, content
            check("export_session_csv() CSV 原样落盘")
            # save_path 传已存在目录：自动补 Content-Disposition 文件名
            out_dir = c.export_session_csv(42, Path(tmp_dir))
            assert out_dir.name == "session.csv", out_dir
            assert out_dir.exists()
            check("export_session_csv() 目录参数自动补附件文件名")

        _FAKE_ROUTES[("GET", "/api/sessions/42/export")] = (
            404, {"ok": False, "error": "session not found"})
        with tempfile.TemporaryDirectory() as tmp_dir:
            expect_service_error(
                "export_session_csv 404 -> 中文错误",
                lambda: c.export_session_csv(42, Path(tmp_dir) / "x.csv"),
                "接口不存在")
        check("export_session_csv() 错误转中文 ServiceError")
        _FAKE_ROUTES[("GET", "/api/sessions/42/export")] = (
            200, "timestamp,voltage,current,power\n")

        estats = c.energy_stats("today")
        assert estats["total_wh"] == 65.2
        check("energy_stats() 解析")

        eprot = c.energy_protocols("today")
        assert eprot["protocols"][0]["protocol"] == "PD"
        check("energy_protocols() 解析")

        stat = c.statistics(1, 24)
        assert stat["data"]["power"]["total_wh"] == 65.2
        check("statistics() 解析")

        # ---------- 命令类 body 组装 ----------
        c.set_setting(9, 30)
        assert _FakeHandler.last_request["body"] == {"piid": 9, "value": 30}
        assert _FakeHandler.last_request["content_type"] == "application/json"
        check("set_setting() body/Content-Type 正确")

        ret = c.set_port_enabled(4, True)  # 4 -> "a"
        assert ret == {"ok": True, "value": 14}
        assert _FakeHandler.last_request["body"] == {"port": "a", "action": "on"}
        check("set_port_enabled() int 4 -> 'a'")

        ret = c.set_port_enabled_all(False)
        assert _FakeHandler.last_request["body"] == {"port": "all", "action": "off"}
        check("set_port_enabled_all() -> all/off")

        c.enable_ble(False)
        assert _FakeHandler.last_request["body"] == {"enabled": False}
        check("enable_ble() body 正确")

        c.set_charge_limit(1, 100.0, "once")
        assert _FakeHandler.last_request["body"] == {"port": "c1", "wh": 100.0,
                                                     "mode": "once"}
        check("set_charge_limit() body 正确")

        c.set_charge_limits({1: {"wh": 100.0, "mode": "always"}, 2: {"wh": 0.0}})
        assert _FakeHandler.last_request["body"] == {
            "limits": {"c1": {"wh": 100.0, "mode": "always"}, "c2": {"wh": 0.0}}}
        check("set_charge_limits() 批量键转换")

        c.set_protocol_switch(1, "pd", "on")
        assert _FakeHandler.last_request["body"] == {"port": "c1", "protocol": "pd",
                                                     "action": "on"}
        check("set_protocol_switch() body 正确")

        c.set_protocol_switches({1: {"pd": True, "pps": False, "ufcs": False}})
        assert _FakeHandler.last_request["body"] == {
            "switches": {"c1": {"pd": True, "pps": False, "ufcs": False}}}
        check("set_protocol_switches() 批量键转换")

        c.set_protocol_raw(50532111)
        assert _FakeHandler.last_request["body"] == {"value": 50532111}
        check("set_protocol_raw() body 正确")

        # ---------- 设备设置便捷方法（PIID 5/6/8-13/15/19/20） ----------
        from app.core.cuktech_client import SCENE_MODES
        expect_equal("SCENE_MODES 值->名映射",
                     SCENE_MODES, {1: "ai", 2: "eco", 3: "single", 4: "balanced"})

        for mode in (1, 2, 3, 4):
            c.set_scene(mode)
            assert _FakeHandler.last_request["body"] == {"piid": 5, "value": mode}
        check("set_scene() 合法值 1-4 全部转发 piid=5")

        expect_service_error("set_scene 非法值 0 拒绝且不发请求",
                             lambda: c.set_scene(0), "充电器模式无效")
        expect_service_error("set_scene 非法值 5 拒绝",
                             lambda: c.set_scene(5), "充电器模式无效")
        assert _FakeHandler.last_request["body"] == {"piid": 5, "value": 4}, \
            "非法充电器模式不应下发网络请求"
        check("set_scene 非法值未产生网络请求")

        c.set_screen_timeout(4)
        assert _FakeHandler.last_request["body"] == {"piid": 6, "value": 4}
        check("set_screen_timeout() 转发 piid=6")

        for bad in (0, 6):
            expect_service_error(f"set_screen_timeout 越界 {bad} 拒绝",
                                 lambda bad=bad: c.set_screen_timeout(bad),
                                 "息屏时间无效")

        for port, piid in ((1, 9), (2, 10), (3, 11), (4, 12)):
            c.set_delay_off(port, 30)
            assert _FakeHandler.last_request["body"] == {"piid": piid, "value": 30}
        check("set_delay_off() 端口 1-4 映射 PIID 9/10/11/12")

        c.set_delay_off(2, 0)
        assert _FakeHandler.last_request["body"] == {"piid": 10, "value": 0}
        check("set_delay_off() minutes=0 取消延时照常转发")

        expect_service_error("set_delay_off 分钟越界 241 拒绝",
                             lambda: c.set_delay_off(1, 241), "延时关闭分钟数无效")
        expect_service_error("set_delay_off 负分钟拒绝",
                             lambda: c.set_delay_off(1, -1), "延时关闭分钟数无效")
        expect_service_error("set_delay_off 端口越界拒绝",
                             lambda: c.set_delay_off(5, 30), "端口号无效")

        c.set_delay_off_all(240)
        assert _FakeHandler.last_request["body"] == {"piid": 8, "value": 240}
        check("set_delay_off_all() 转发总口 PIID 8")

        expect_service_error("set_delay_off_all 分钟越界 241 拒绝",
                             lambda: c.set_delay_off_all(241), "延时关闭分钟数无效")

        c.set_device_language(True)
        assert _FakeHandler.last_request["body"] == {"piid": 13, "value": 1}
        c.set_device_language(False)
        assert _FakeHandler.last_request["body"] == {"piid": 13, "value": 0}
        check("set_device_language() 布尔转 0/1 (PIID 13)")

        c.set_usb_a_trickle(True)
        assert _FakeHandler.last_request["body"] == {"piid": 15, "value": 1}
        c.set_usb_a_trickle(False)
        assert _FakeHandler.last_request["body"] == {"piid": 15, "value": 0}
        check("set_usb_a_trickle() 布尔转 0/1 (PIID 15)")

        c.set_idle_screen_off(True)
        assert _FakeHandler.last_request["body"] == {"piid": 19, "value": 1}
        check("set_idle_screen_off() 转发 piid=19")

        c.set_screen_lock(False)
        assert _FakeHandler.last_request["body"] == {"piid": 20, "value": 0}
        check("set_screen_lock() 转发 piid=20")

        # ---------- 命令类端点 HTTP 200 + ok=false（离线坑） ----------
        _FAKE_ROUTES[("POST", "/api/port")] = (200, {"ok": False,
                                                     "error": "not connected"})
        expect_service_error("离线 200+ok:false -> 充电器离线",
                             lambda: c.set_port_enabled(1, True), "充电器离线")
        _FAKE_ROUTES[("POST", "/api/port")] = (200, {"ok": False,
                                                     "error": "command timeout"})
        expect_service_error("body command timeout -> 中文超时",
                             lambda: c.set_port_enabled(1, True), "充电器命令超时")
        _FAKE_ROUTES[("POST", "/api/port")] = (200, {"ok": False,
                                                     "error": "some weird failure"})
        expect_service_error("未知 ok:false 文本透传",
                             lambda: c.set_port_enabled(1, True),
                             "充电器拒绝命令：some weird failure")
        _FAKE_ROUTES[("POST", "/api/port")] = (200, {"ok": True, "value": 14})

        # ---------- HTTP 400 ----------
        _FAKE_ROUTES[("POST", "/api/set")] = (
            400, {"ok": False, "error": "value must be between 0 and 1440"})
        expect_service_error("400 -> 拒绝命令+服务端文本",
                             lambda: c.set_setting(9, 9999), "充电器拒绝命令",
                             "value must be between 0 and 1440")
        _FAKE_ROUTES[("POST", "/api/set")] = (200, {"ok": True})

        # ---------- 未知端点 404 ----------
        expect_service_error("未知端点 -> 404 中文",
                             lambda: c._request("GET", "/api/nothing"), "接口不存在")

        # ---------- connected() 的折叠语义 ----------
        _FAKE_ROUTES[("GET", "/api/status")] = (500, {"ok": False, "error": "boom"})
        expect_equal("connected() HTTP 500 -> False", c.connected(), False)
        _FAKE_ROUTES[("GET", "/api/status")] = (200, {"connected": False, "ports": {}})
        expect_equal("connected() connected=false -> False", c.connected(), False)
        _FAKE_ROUTES[("GET", "/api/status")] = (200, {"unexpected": "shape"})
        expect_equal("connected() 异形 body -> False", c.connected(), False)
        _FAKE_ROUTES[("GET", "/api/status")] = (200, _STATUS_OK)
        expect_equal("connected() 还原后仍在线", c.connected(), True)

        # ---------- 参数校验 ----------
        expect_service_error("set_port_enabled 端口越界",
                             lambda: c.set_port_enabled(5, True), "端口号无效")
        expect_service_error("set_charge_limits 空批量拒绝",
                             lambda: c.set_charge_limits({}), "至少要指定一个端口")
    finally:
        _stop_server(server)

    print(f"\n全部通过：{_PASS_COUNT} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
