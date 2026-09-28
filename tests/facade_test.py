# SPDX-License-Identifier: GPL-3.0-or-later
"""MijiaService 组合门面自测：聚合云端 + 本地 CUKTECH 设备源。

用法: .venv\\Scripts\\python.exe tests/facade_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.facade_test）

云端部分用假 api 对象（子类覆写 _init_api，不打米家网络）；本地部分用
http.server 起假 BLE 服务（仿 cuktech_client_test.py）。断言：
list_devices 聚合出 local 设备、网关不可达时只有云端设备、各 cuktech_*
方法正常转发与 ServiceError 透传。绝不 import PySide6 / app.ui ——
本 venv 缺 PySide6，只测 core 层。
"""

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 允许直接以文件方式运行（python tests/facade_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cuktech_client import CuktechClient  # noqa: E402
from app.core.models import DeviceInfo  # noqa: E402
from app.core.service import MijiaService, ServiceError  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# 假 BLE 服务：按 (method, path) 注册 (status, json_body)，可运行期改写；
# last_request 记录最近一次请求供断言转发 body
# ---------------------------------------------------------------------------

_STATUS_OK: dict[str, Any] = {
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
              "protocol": "idle", "enabled": True},
    },
    "settings": {"5": 1, "6": 2, "16": 15},
    "protocol_extend": 8,
    "device_model": "CUKTECH-XXX",
    "firmware_version": "1.2.3",
}

_FAKE_ROUTES: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {
    ("GET", "/api/status"): (200, _STATUS_OK),
    ("GET", "/api/charge-limits"): (200, {
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
    }),
    ("GET", "/api/chart"): (200, {
        "ok": True,
        "labels": ["14:00", "14:01"],
        "datasets": {
            "power": [{"label": "Total", "data": [0.0, 65.0]}],
        },
    }),
    # CSV 导出（str 特例回原始文本，见 _FakeHandler._respond）
    ("GET", "/api/sessions/42/export"): (200, "timestamp,voltage,current,power\n"),
    ("POST", "/api/port"): (200, {"ok": True, "value": 14}),
    ("POST", "/api/charge-limits"): (200, {"ok": True}),
    ("POST", "/api/set"): (200, {"ok": True}),
}


class _FakeHandler(BaseHTTPRequestHandler):
    """按路由表回 JSON；记录最近一次请求供断言 body/query。"""

    server_version = "FakeCUKTECH/1.0"
    last_request: dict[str, Any] = {}

    def _respond(self, method: str) -> None:
        path = self.path
        base = path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = (json.loads(self.rfile.read(length).decode("utf-8"))
                    if length > 0 else None)
        except Exception:
            body = None
        _FakeHandler.last_request = {
            "method": method,
            "path": base,
            "query": path.split("?")[1] if "?" in path else "",
            "body": body,
        }
        route = (method, base)
        if route not in _FAKE_ROUTES:
            self._send(404, {"ok": False, "error": "no such route"})
            return
        status, payload = _FAKE_ROUTES[route]
        self._send(status, payload)

    def _send(self, status: int, payload: Any) -> None:
        if isinstance(payload, str):
            # CSV 导出特例：原始文本 + text/csv（export 端点行为）
            self.send_response(status)
            self.send_header("Content-Type", "text/csv")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_GET(self) -> None:
        self._respond("GET")

    def do_POST(self) -> None:
        self._respond("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # 静默默认的每请求 stderr 日志


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# 云端假 api：MijiaService._init_api 的返回值替身，不打米家网络
# ---------------------------------------------------------------------------

_CLOUD_DEVICES = [
    {"did": "1001", "name": "卧室音箱", "model": "xiaomi.wifispeaker.x08c",
     "isOnline": True},
    {"did": "1002", "name": "客厅灯", "model": "yeelink.light.lamp1",
     "isOnline": False},
]
_HOMES = [{"name": "我的家",
           "roomlist": [{"name": "客厅", "dids": ["1001", "1002"]}]}]


class _FakeMijiaAPI:
    """只实现 list_devices 用到的三个方法；available 恒 True。"""

    available = True

    def get_homes_list(self) -> list[dict]:
        return _HOMES

    def get_devices_list(self) -> list[dict]:
        return list(_CLOUD_DEVICES)

    def get_shared_devices_list(self) -> list[dict]:
        return []


class _FacadeService(MijiaService):
    """云端会话替换为假 api，本地客户端由测试注入。"""

    def _init_api(self):
        return _FakeMijiaAPI()


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


def expect_service_error(name: str, fn, *fragments: str) -> None:
    """断言 fn() 抛 service 层 ServiceError 且消息包含全部片段。"""
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


def main() -> int:
    server, url = _start_server()
    try:
        service = _FacadeService(
            cuktech=CuktechClient(base_url=url, timeout=2.0))

        # ---------- 构造与注入 ----------
        assert isinstance(service.cuktech, CuktechClient)
        check("cuktech 属性暴露注入的客户端")

        injected = CuktechClient(base_url="http://127.0.0.1:1", timeout=0.3)
        expect_equal("cuktech_base_url 参数生效",
                     MijiaService(cuktech_base_url=url).cuktech._base_url, url)
        expect_equal("cuktech 注入优先于 base_url 参数",
                     MijiaService(cuktech=injected, cuktech_base_url=url).cuktech,
                     injected)

        # ---------- list_devices 聚合：本地在线 ----------
        devices = service.list_devices()
        expect_equal("聚合后设备总数 = 云端 2 + 本地 1", len(devices), 3)
        cloud = [d for d in devices if d.source == "cloud"]
        local = [d for d in devices if d.source == "local"]
        expect_equal("云端设备 2 台", len(cloud), 2)
        expect_equal("本地设备 1 台", len(local), 1)
        lv = local[0]
        assert isinstance(lv, DeviceInfo)
        expect_equal("本地 did 为合成 ID cuktech-local", lv.did, "cuktech-local")
        expect_equal("本地 model 来自 device_model", lv.model, "CUKTECH-XXX")
        # 真机型号是内部编码不可读，显示名固定为「CUKTECH 充电器」（2026-09-26 规则变更）
        expect_equal("本地名称固定为「CUKTECH 充电器」",
                     lv.name, "CUKTECH 充电器")
        expect_equal("本地 online 恒 True", lv.online, True)
        assert lv.is_cuktech, "本地设备 is_cuktech 应为 True"
        check("本地设备 is_cuktech 为 True")
        expect_equal("云端排序不受聚合影响（按家庭/房间/名称码点序）",
                     [(d.home_name, d.room_name, d.name) for d in cloud],
                     [("我的家", "客厅", "卧室音箱"), ("我的家", "客厅", "客厅灯")])

        # ---------- list_devices 聚合：蓝牙断开（HTTP 通但 connected=false） ----------
        # 门面聚合开关是 cuktech.connected()（服务可达 且 蓝牙在线）。
        # 本实例已成功组装过一次本地设备，瞬时断开沿用上次条目兜底
        # （卡片不应因一次探测抖动消失；在线与否由卡片自轮询呈现）
        _FAKE_ROUTES[("GET", "/api/status")] = (
            200, {**_STATUS_OK, "connected": False})
        devices = service.list_devices()
        expect_equal("蓝牙断开（connected=false）时沿用上次本地条目", len(devices), 3)
        local_after = [d for d in devices if d.source == "local"]
        expect_equal("兜底条目仍是 cuktech-local", len(local_after), 1)
        check("蓝牙断开时兜底保留本地条目")
        _FAKE_ROUTES[("GET", "/api/status")] = (200, _STATUS_OK)

        # ---------- list_devices 聚合：网关不可达（全新实例，从未成功过） ----------
        # 从未成功组装过本地设备的实例不兜底：只有云端设备
        offline = _FacadeService(
            cuktech=CuktechClient(base_url="http://127.0.0.1:1", timeout=0.3))
        devices = offline.list_devices()
        expect_equal("网关不可达时只有云端设备", len(devices), 2)
        assert all(d.source == "cloud" for d in devices)
        check("网关不可达时全部为 cloud 来源")

        # ---------- list_devices 聚合：device_model 为空视为无效 ----------
        # 组装失败（model 空）不更新 _last_cuktech_device，但本次调用
        # HTTP 是通的（connected=true 只是 model 无效）→ 组装返回 None，
        # 兜底沿用上次成功条目，仍是 3 台
        _FAKE_ROUTES[("GET", "/api/status")] = (
            200, {**_STATUS_OK, "device_model": ""})
        devices = service.list_devices()
        expect_equal("device_model 为空时沿用上次本地条目", len(devices), 3)
        _FAKE_ROUTES[("GET", "/api/status")] = (200, _STATUS_OK)

        # ---------- cuktech_* 转发 ----------
        status = service.cuktech_status()
        assert status["connected"] is True
        assert status["ports"]["1"]["power"] == 65.0
        check("cuktech_status() 转发原始快照")

        limits = service.cuktech_charge_limits()
        assert set(limits["limits"].keys()) == {1, 2, 3, 4}
        assert limits["limits"][1]["session_wh"] == 37.5
        check("cuktech_charge_limits() 键规整为 int 1-4")

        chart = service.cuktech_chart(2.5)
        assert chart["ok"] is True and "labels" in chart
        assert "hours=2.5" in _FakeHandler.last_request["query"]
        check("cuktech_chart() 转发 hours 参数")

        # ---------- CSV 导出门面转发 ----------
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "s42.csv"
            out = service.cuktech_export_session_csv(42, target)
            assert out == target, out
            assert target.exists()
            assert "timestamp,voltage,current,power" in target.read_text(
                encoding="utf-8")
            check("cuktech_export_session_csv() 转发并落盘 CSV")

        service.cuktech_toggle_total(True)
        assert _FakeHandler.last_request["body"] == {"port": "all", "action": "on"}
        check("cuktech_toggle_total(True) -> all/on")

        service.cuktech_toggle_total(False)
        assert _FakeHandler.last_request["body"] == {"port": "all", "action": "off"}
        check("cuktech_toggle_total(False) -> all/off")

        service.cuktech_set_port(4, True)
        assert _FakeHandler.last_request["body"] == {"port": "a", "action": "on"}
        check("cuktech_set_port(4, True) -> a/on")

        service.cuktech_set_port(1, False)
        assert _FakeHandler.last_request["body"] == {"port": "c1", "action": "off"}
        check("cuktech_set_port(1, False) -> c1/off")

        service.cuktech_set_charge_limit(2, 80.0)
        assert _FakeHandler.last_request["body"] == {"port": "c2", "wh": 80.0,
                                                     "mode": "once"}
        check("cuktech_set_charge_limit() body 正确")

        # ---------- 设备设置门面转发（/api/set） ----------
        service.cuktech_set_scene(2)
        assert _FakeHandler.last_request["body"] == {"piid": 5, "value": 2}
        check("cuktech_set_scene(2) -> piid=5/value=2")

        service.cuktech_set_screen_timeout(3)
        assert _FakeHandler.last_request["body"] == {"piid": 6, "value": 3}
        check("cuktech_set_screen_timeout(3) -> piid=6/value=3")

        for port, piid in ((1, 9), (2, 10), (3, 11), (4, 12)):
            service.cuktech_set_delay_off(port, 60)
            assert _FakeHandler.last_request["body"] == {"piid": piid, "value": 60}
        check("cuktech_set_delay_off() 端口 1-4 映射 PIID 9/10/11/12")

        service.cuktech_set_delay_off_all(120)
        assert _FakeHandler.last_request["body"] == {"piid": 8, "value": 120}
        check("cuktech_set_delay_off_all(120) -> piid=8/value=120")

        service.cuktech_set_device_language(True)
        assert _FakeHandler.last_request["body"] == {"piid": 13, "value": 1}
        service.cuktech_set_device_language(False)
        assert _FakeHandler.last_request["body"] == {"piid": 13, "value": 0}
        check("cuktech_set_device_language() 布尔转 0/1")

        service.cuktech_set_usb_a_trickle(True)
        assert _FakeHandler.last_request["body"] == {"piid": 15, "value": 1}
        service.cuktech_set_usb_a_trickle(False)
        assert _FakeHandler.last_request["body"] == {"piid": 15, "value": 0}
        check("cuktech_set_usb_a_trickle() 布尔转 0/1")

        service.cuktech_set_idle_screen_off(True)
        assert _FakeHandler.last_request["body"] == {"piid": 19, "value": 1}
        check("cuktech_set_idle_screen_off(True) -> piid=19/value=1")

        service.cuktech_set_screen_lock(False)
        assert _FakeHandler.last_request["body"] == {"piid": 20, "value": 0}
        check("cuktech_set_screen_lock(False) -> piid=20/value=0")

        # 客户端本地校验的门面透传：非法充电器模式 / 分钟越界（不经网络）
        expect_service_error("cuktech_set_scene 非法值透传中文",
                             lambda: service.cuktech_set_scene(9), "充电器模式无效")
        expect_service_error("cuktech_set_delay_off 分钟越界 241 透传",
                             lambda: service.cuktech_set_delay_off(1, 241),
                             "延时关闭分钟数无效")
        expect_service_error("cuktech_set_screen_timeout 越界透传",
                             lambda: service.cuktech_set_screen_timeout(6),
                             "息屏时间无效")

        # ---------- 错误透传：service.ServiceError（非 cuktech 同名类） ----------
        import app.core.cuktech_client as cc_module

        _FAKE_ROUTES[("POST", "/api/port")] = (200, {"ok": False,
                                                     "error": "not connected"})
        try:
            service.cuktech_set_port(1, True)
        except ServiceError as exc:
            assert not isinstance(exc, cc_module.ServiceError) or type(exc) is ServiceError, (
                "应抛 service 层 ServiceError")
            # 两个同名类非同一对象，严格断言类型
            assert type(exc) is ServiceError, f"类型应为 service.ServiceError: {type(exc)}"
            assert "充电器离线" in str(exc), str(exc)
            check(f"cuktech 错误转为 service.ServiceError 透传中文: {exc}")
        else:
            raise AssertionError("cuktech_set_port 离线场景未抛 ServiceError")

        _FAKE_ROUTES[("POST", "/api/charge-limits")] = (
            400, {"ok": False, "error": "port c2: wh must be finite and within 0-1000"})
        expect_service_error("cuktech_set_charge_limit 400 透传",
                             lambda: service.cuktech_set_charge_limit(2, 9999.0),
                             "充电器拒绝命令", "wh must be finite")

        _FAKE_ROUTES[("GET", "/api/status")] = (500, {"ok": False, "error": "boom"})
        expect_service_error("cuktech_status HTTP 500 透传",
                             service.cuktech_status, "充电器服务错误")

        # 非法端口号在客户端本地校验即抛（不经网络），同样透传
        expect_service_error("cuktech_set_port 端口越界透传",
                             lambda: service.cuktech_set_port(5, True), "端口号无效")

        # _wrap_error 兜底：让客户端抛非 ServiceError 的意外异常
        class _BoomClient(CuktechClient):
            def set_port_enabled(self, port: int, on: bool):
                raise ValueError("unexpected boom")

        boom = _FacadeService(cuktech=_BoomClient(base_url=url))
        expect_service_error("意外异常经 _wrap_error 包装",
                             lambda: boom.cuktech_set_port(1, True),
                             "充电器命令执行失败", "unexpected boom")

        _FAKE_ROUTES[("POST", "/api/port")] = (200, {"ok": True, "value": 14})
        _FAKE_ROUTES[("POST", "/api/charge-limits")] = (200, {"ok": True})
        _FAKE_ROUTES[("GET", "/api/status")] = (200, _STATUS_OK)

        # ---------- 云端错误路径不受影响 ----------
        class _BrokenAPI(_FakeMijiaAPI):
            def get_devices_list(self):
                raise RuntimeError("cloud down")

        class _BrokenService(MijiaService):
            def _init_api(self):
                return _BrokenAPI()

        expect_service_error("云端列表失败仍抛 _wrap_error 中文",
                             lambda: _BrokenService(
                                 cuktech=CuktechClient(base_url=url)).list_devices(),
                             "获取设备列表失败", "cloud down")
    finally:
        _stop_server(server)

    print(f"\n全部通过：{_PASS_COUNT} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
