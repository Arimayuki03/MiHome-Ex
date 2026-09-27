# SPDX-License-Identifier: GPL-3.0-or-later
"""托盘悬停数据弹窗自测：离屏渲染 + 假 status 喂数断言。

用法: .venv\\Scripts\\python.exe tests/tray_hover_test.py

仿 cuktech_panel_test 的离屏 + grab() 模式：构造 CuktechHoverPopup、
灌假 /api/status 快照与 port_update 增量，断言——
- 在线快照渲染四口读数与总功率（Σ 口径与 cuktech_panel 一致）；
- 未到过的端口显示 "-- W"；
- connected=false 快照整体灰置为 "-- W"（BLE 残留数据不展示）；
- push_port_update 增量合并 + connected 抬为在线；
- TrayController.push_cuktech_status/port 转发入口不炸（不可见时忽略）；
- 暗/亮两种主题 retheme 各渲染一遍 grab() 非空白。
"""

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.ui import si_theme
from app.ui.theme_service import apply_theme
from app.ui.tray.cuktech_hover import CuktechHoverPopup

# 假快照：结构与 cuktech-api.md §/api/status 一致（service 原样透传）
_STATUS_ONLINE = {
    "connected": True,
    "ports": {
        "1": {"power": 45.5, "voltage": 20.0, "current": 2.2, "active": True},
        "2": {"power": 30.0, "voltage": 20.0, "current": 1.5, "active": True},
        "3": {"power": 0.0, "voltage": 0.0, "current": 0.0, "active": False},
    },
}


def _grab_not_blank(widget) -> bool:
    img = widget.grab().toImage()
    w, h = img.width(), img.height()
    if w == 0 or h == 0:
        return False
    first = img.pixelColor(0, 0).rgba()
    return any(img.pixelColor(x, y).rgba() != first
               for x in range(0, w, max(1, w // 24))
               for y in range(0, h, max(1, h // 24)))


def test_online_snapshot():
    popup = CuktechHoverPopup()
    popup.set_status(_STATUS_ONLINE)
    popup.show()
    assert popup._port_rows[1][1].text() == "45.5W"
    assert popup._port_rows[2][1].text() == "30.0W"
    assert popup._port_rows[3][1].text() == "0.0W"
    assert popup._port_rows[4][1].text() == "-- W"  # 未上报过的口
    assert popup._total_val.text() == "75.5W"  # Σ 口径不滤 enabled
    assert _grab_not_blank(popup)
    popup.hide()
    return popup


def test_offline_snapshot(popup):
    offline = {"connected": False, "ports": _STATUS_ONLINE["ports"]}
    popup.set_status(offline)
    for port_id, (_n, val) in popup._port_rows.items():
        assert val.text() == "-- W", f"port {port_id}"
    assert popup._total_val.text() == "-- W"
    popup.set_status(None)  # 服务不可达同样灰置
    assert popup._total_val.text() == "-- W"


def test_port_update_merges(popup):
    popup.set_status(_STATUS_ONLINE)
    popup.push_port_update({"port_id": 4, "port": "a", "data": {"power": 8.26}})
    assert popup._port_rows[4][1].text() == "8.3W"
    # 推送即在线证据：offline 快照被单口推送抬起
    assert popup._total_val.text() == "83.8W"  # 45.5+30+0+8.26（0.3W 浮点尾差由 :.1f 抹平）
    # 坏载荷静默忽略
    popup.push_port_update({"port_id": "x"})
    popup.push_port_update({})
    assert popup._total_val.text() == "83.8W"


def test_controller_forward_gates(popup=None):
    """转发入口在弹窗不可见时忽略、可见时喂数（不炸即可）。"""
    from app.ui.tray.controller import TrayController

    class _FakeTray:
        def isVisible(self):
            return True

        def geometry(self):
            from PySide6.QtCore import QRect
            return QRect()

    class _FakeQuick:
        def isVisible(self):
            return False

        def is_explicitly_visible(self):
            return False

        def set_cuktech_status(self, payload):
            self.status = payload

        def push_cuktech_port(self, payload):
            self.port = payload

    class _FakeMain:
        pass

    class _FakeService:
        from app.core.cuktech_client import CuktechClient  # noqa: N814
        cuktech = None

    ctrl = TrayController.__new__(TrayController)
    # 最小手工装配：只验证转发门控，不启动悬停轮询线程
    popup = CuktechHoverPopup()
    ctrl._hover_popup = popup
    ctrl._quick = _FakeQuick()
    ctrl._tray = None
    assert ctrl.push_cuktech_status(_STATUS_ONLINE) is None
    assert ctrl.push_cuktech_port({"port_id": 4}) is None
    # 可见时真正喂数
    popup.show()
    ctrl._tray = _FakeTray()
    ctrl.push_cuktech_status(_STATUS_ONLINE)
    assert popup._port_rows[1][1].text() == "45.5W"
    ctrl.push_cuktech_port({"port_id": 4, "data": {"power": 1.0}})
    assert popup._port_rows[4][1].text() == "1.0W"
    popup.hide()
    del _FakeTray, _FakeQuick, _FakeMain, _FakeService


def test_retheme_both_themes():
    for theme in ("dark", "light"):
        apply_theme(theme)
        popup = CuktechHoverPopup()
        popup.set_status(_STATUS_ONLINE)
        popup.show()
        popup.retheme()
        assert _grab_not_blank(popup)
        popup.hide()


if __name__ == "__main__":
    p = test_online_snapshot()
    test_offline_snapshot(p)
    test_port_update_merges(p)
    test_controller_forward_gates()
    test_retheme_both_themes()
    print("tray_hover_test: all assertions passed")
