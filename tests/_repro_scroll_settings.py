# SPDX-License-Identifier: GPL-3.0-or-later
"""复现/验证：CuktechPanel 设备设置页「滑不到底」（矮壳裁切）。

DeviceDetailDialog 在主窗口矮于约 800 逻辑 px 时把壳收缩到
min(760, 窗口高-40)；面板若固定 660 高会被壳裁掉底部——页内
QScrollArea 能滚，但它滚到底对齐的是被裁掉的视口底边，最后一段
内容永远看不见（用户截图即「5min重连」行被拦腰截断）。

本脚本离屏装完整 DeviceDetailDialog（panel_factory 走 CuktechPanel），
分别在高约 533 逻辑 px 的矮窗口（模拟 1.5× 缩放下 1187×800 物理
窗口）与完整大壳两种场景下量化：
1) 面板底边相对壳的溢出（>0 即被壳裁切）；
2) 设置页滚到最大时内容底边与视口底边的 bottom_gap（>0 即滚不到底）。
"""
import copy
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication, QScrollArea

app = QApplication([])

from app.core import cache as _device_cache
_device_cache.save = lambda *a, **k: None

from app.core.jobs import JobExecutor
from app.core.models import DeviceInfo
from app.ui.theme_service import apply_theme
from app.ui.device_dialog import DeviceDetailDialog
from app.ui.cuktech_panel import CuktechPanel, _TAB_SETTINGS

FAKE_STATUS = {
    "connected": True, "authenticated": True,
    "ports": {
        "1": {"voltage": 20.0, "current": 3.25, "power": 65.0,
              "active": True, "protocol": "PD", "enabled": True},
        "2": {"voltage": 0.0, "current": 0.0, "power": 0.0,
              "active": False, "protocol": "idle", "enabled": False},
        "3": {"voltage": 9.0, "current": 1.0, "power": 9.0,
              "active": True, "protocol": "QC", "enabled": True},
        "4": {"voltage": 5.0, "current": 0.3, "power": 1.5,
              "active": True, "protocol": "5V", "enabled": False},
    },
    "settings": {"16": 15, "5": 1, "6": 2, "8": 0, "9": 0, "10": 0,
                 "11": 30, "12": 0, "13": 1, "15": 0, "19": 1, "20": 1,
                 "21": 0},
    "device_model": "CUKTECH-10U", "firmware_version": "1.2.3",
}
FAKE_LIMITS = {"ok": True, "limits": {
    1: {"wh": 100.0, "mode": "once", "fired": False,
        "session_wh": 37.5, "is_charging": True},
    2: {"wh": 0.0, "mode": "once", "fired": False,
        "session_wh": 0.0, "is_charging": False},
    3: {"wh": 60.0, "mode": "always", "fired": True,
        "session_wh": 60.0, "is_charging": False},
    4: {"wh": 0.0, "mode": "once", "fired": False,
        "session_wh": 0.0, "is_charging": False},
}}
FAKE_CHART = {"ok": True,
              "labels": [f"{i // 60:02d}:{i % 60:02d}" for i in range(0, 120, 2)],
              "datasets": {"power": [
                  {"label": "C1", "data": [0.0] * 60},
                  {"label": "C2", "data": [0.0] * 60},
                  {"label": "C3", "data": [9.0] * 60},
                  {"label": "A", "data": [1.5] * 60},
                  {"label": "Total", "data": [10.0 + i for i in range(60)]}],
                  "voltage": [], "current": []}}


class FakeService:
    def cuktech_status(self):
        return copy.deepcopy(FAKE_STATUS)

    def cuktech_charge_limits(self):
        return copy.deepcopy(FAKE_LIMITS)

    def cuktech_chart(self, hours=1.0, interval=30):
        return copy.deepcopy(FAKE_CHART)

    def cuktech_set_port(self, port, on):
        return {"ok": True}

    def cuktech_set_charge_limit(self, port, wh, mode="once"):
        return {"ok": True}

    def cuktech_toggle_total(self, on):
        return on


def _drain(rounds=30):
    for _ in range(rounds):
        time.sleep(0.02)
        app.processEvents()
    app.processEvents()


DEV = DeviceInfo(did="cuk-1", name="CUKTECH 充电器",
                 model="cuktech.cu10u.charger", home_name="我的家庭",
                 room_name="书房", online=True, source="local")

apply_theme("dark")
service = FakeService()
jobs = JobExecutor()


def _make_dialog() -> DeviceDetailDialog:
    def _factory(parent):
        return CuktechPanel(service, jobs, DEV, parent)

    dialog = DeviceDetailDialog(service, jobs, DEV, None, panel_factory=_factory)
    dialog.load()
    dialog.show()
    return dialog


def measure(tag: str, win_w: int, win_h: int,
            simulate_fixed_height: bool = False) -> bool:
    """装壳到 win_w×win_h，切设置页滚到底，量化裁切与滚动间隙。

    simulate_fixed_height=True 复刻旧 bug（面板 setFixedHeight(660)），
    用于确认本脚本能检出该回归；默认 False 测当前代码。
    """
    dialog = _make_dialog()
    if simulate_fixed_height:
        dialog.panel.setFixedHeight(660)
    dialog.resize(win_w, win_h)
    _drain()
    dialog.panel._select_tab(_TAB_SETTINGS)
    _drain()

    shell = dialog._panel
    panel = dialog.panel
    panel_geo = panel.geometry()  # 相对壳坐标
    overflow = panel_geo.y() + panel_geo.height() - shell.height()

    scroll = panel._pages.currentWidget()
    assert isinstance(scroll, QScrollArea), type(scroll)
    inner = scroll.widget()
    sb = scroll.verticalScrollBar()
    sb.setValue(sb.maximum())
    _drain()
    gap = (inner.height() + inner.y()) - scroll.viewport().height()

    print(f"[{tag}] win={win_w}x{win_h} shell_h={shell.height()} "
          f"panel_h={panel_geo.height()} panel_bottom_overflow={overflow} "
          f"viewport_h={scroll.viewport().height()} widget_h={inner.height()} "
          f"scrollbar={sb.value()}/{sb.maximum()} bottom_gap={gap}")
    ok = overflow <= 0 and gap <= 0
    dialog.deleteLater()
    _drain()
    return ok


# 用户截图场景：1187×800 物理像素 ÷ 1.5 缩放 ≈ 791×533 逻辑窗口
ok_short = measure("short-window", 791, 533)
# 完整大壳：窗口高 800 逻辑 px 以上，壳保持 920×760
ok_full = measure("full-large", 960, 800)
# 回归检出自检：复刻旧 setFixedHeight(660) 必须判 FAIL
bad = measure("old-bug-sim", 791, 533, simulate_fixed_height=True)
jobs.shutdown()
print("current code:", "PASS" if (ok_short and ok_full) else "FAIL")
print("old-bug detection:", "PASS (correctly FAIL)" if not bad else "FAIL (not detected)")
