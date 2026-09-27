# SPDX-License-Identifier: GPL-3.0-or-later
"""托盘 span 网格 + CUKTECH 大卡 + 曲线悬浮回归自测（离屏）。

用法: .venv\\Scripts\\python.exe tests/tray_span_test.py

断言——
- tray_store v2 读写/旧 v1 str 列表迁移/set_span/保留 span 的 save()；
- TrayQuickWindow span 占位：大卡跨 2 列、普通卡让位、无重叠；
- CUKTECH 大卡显示总功率与四口读数，SSE 喂数后刷新；
- 曲线 hover：MultiMetricCurve/PowerCurveWidget/_SessionCurve 的
  _nearest_point 命中与 _tooltip_text 文案。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtCore import QPointF, QEvent, Qt as QtMod
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.core import tray_store
from app.ui.cuktech_visuals import MultiMetricCurve
from app.ui.cuktech_panel import PowerCurveWidget
from app.ui.cuktech_history import _SessionCurve
from app.ui.theme_service import apply_theme
from app.ui.tray.quick_window import TrayQuickWindow
from app.core.models import DeviceInfo

tmpdir = tempfile.mkdtemp()

# ---------- 1. tray_store：v1 迁移 / v2 读写 / set_span ----------

v1_file = Path(tmpdir) / "tray_v1.json"
v1_file.write_text(json.dumps(
    {"version": 1, "devices": ["dev-a", "dev-b"]}), encoding="utf-8")
tray_store._json_store.data_file = (  # 重定向数据文件到临时目录
    lambda name: v1_file if name == "tray.json" else Path(tmpdir) / name)
assert tray_store.load() == ["dev-a", "dev-b"], "v1 迁移读取"
entries = tray_store.load_entries()
assert all(e["span"] == 1 for e in entries), entries
assert tray_store.span_of("dev-a") == 1

tray_store.set_span("dev-a", 2)
assert tray_store.span_of("dev-a") == 2
assert tray_store.span_of("dev-b") == 1
# save 只管勾选集合，保留已存 span
tray_store.save(["dev-b", "dev-a"])
assert tray_store.span_of("dev-a") == 2, "save 应保留 span"
assert tray_store.span_of("dev-b") == 1
raw = json.loads(v1_file.read_text(encoding="utf-8"))
assert raw["version"] == 2 and raw["devices"][1]["span"] == 2, raw
tray_store.set_span("dev-a", 99)  # 钳位到 4
assert tray_store.span_of("dev-a") == 4
print("1. tray_store v2 迁移/读写/set_span/save 保留 span OK")

# ---------- 2. TrayQuickWindow：span 占位 + CUKTECH 大卡 ----------

apply_theme("dark")

class _FakeService:
    def read_metrics(self, dids):
        return {}

class _FakeJobs:
    def submit(self, fn, on_success=None, on_error=None, **kw):
        return None

win = TrayQuickWindow(_FakeService(), _FakeJobs(), None)
win.resize(300, 380)
dev_local = DeviceInfo(
    did="cuktech-local", name="CUKTECH 充电器", model="x",
    home_name="h", room_name="本地", online=True, source="local")
dev_cloud = DeviceInfo(
    did="cloud-1", name="卧室灯", model="y", home_name="h",
    room_name="卧室", online=True, source="cloud")
win.set_devices([dev_local, dev_cloud], {"cloud-1": True})
tray_store.save(["cuktech-local", "cloud-1"])
tray_store.set_span("cuktech-local", 2)
win._tray_entries = tray_store.load_entries()
win.show()
app.processEvents()
win._rebuild()
app.processEvents()

grid = win._grid
positions = {}
for i in range(grid.count()):
    item = grid.itemAt(i)
    w = item.widget()
    if w is not None:
        positions[getattr(w, "_did", "?")] = grid.getItemPosition(i)
pos_local = positions["cuktech-local"]
pos_cloud = positions["cloud-1"]
# getItemPosition 返回 (row, col, rowSpan, colSpan)
assert pos_local == (0, 0, 1, 2), f"大卡应占第 0 行跨 2 列: {pos_local}"
assert pos_cloud == (1, 0, 1, 1), f"普通卡应让位到下一行首格: {pos_cloud}"
assert len(win._cuktech_cards) == 1
card = win._cuktech_cards["cuktech-local"]
assert card._total_label.text() == "-- W"
win.set_cuktech_status({
    "connected": True,
    "ports": {"1": {"power": 45.5}, "2": {"power": 30.0},
              "3": {"power": 0.0}},
})
assert card._total_label.text() == "75.5W", card._total_label.text()
assert card._port_labels[1].text() == "45.5W"
assert card._port_labels[4].text() == "--"
win.push_cuktech_port({"port_id": 4, "data": {"power": 8.26}})
assert card._port_labels[4].text() == "8.3W"
assert card._total_label.text() == "83.8W"
# 右键大卡 -> 占格 2 切回 1，CUKTECH 退回普通行、普通卡让位重排
win._toggle_span("cuktech-local")
assert tray_store.span_of("cuktech-local") == 1
assert not win._cuktech_cards, "退回 1 格后不应再有大卡"
win._toggle_span("cuktech-local")  # 再切回 2，恢复大卡
assert tray_store.span_of("cuktech-local") == 2
assert "cuktech-local" in win._cuktech_cards
img = win._root.grab().toImage()
assert img.width() > 0
win.hide()
win.deleteLater()
tray_store.save([])
print("2. 托盘 span 占位/大卡实时数据/单口增量/右键切换 OK")

# ---------- 3. 曲线悬浮命中与文案 ----------

FAKE = {
    "labels": [f"{i:02d}:00" for i in range(24)],
    "datasets": {
        "power": [
            {"label": "C1", "data": [float(i) for i in range(24)]},
            {"label": "C2", "data": [0.0] * 24},
            {"label": "C3", "data": [0.0] * 24},
            {"label": "A", "data": [0.0] * 24},
            {"label": "Total", "data": [float(i) for i in range(24)]},
        ],
        "voltage": [], "current": [],
    },
}
apply_theme("dark")

curve = MultiMetricCurve()
curve.resize(400, 260)
curve.show()
app.processEvents()
curve.set_chart_data(FAKE)
assert curve._nearest_point(-1) == -1, "绘图区外命中 -1"
# 期望值按绘图区几何推算（左缘有 Y 轴数值带，控件中点≠绘图区中点）
rect = curve._chart_rect()
mid_x = rect.left() + rect.width() / 2
assert curve._nearest_point(mid_x) == 11 or \
    curve._nearest_point(mid_x) == 12
# 绘图区右缘命中最后一桶（控件右缘超绘图区右边界，命中 -1 属预期）
assert curve._nearest_point(rect.right() - 0.5) == 23
assert curve._nearest_point(curve.width() + 1) == -1, "控件外命中 -1"
text = curve._tooltip_text(5)
assert text.startswith("05:00") and "C1: 5.0W" in text \
    and "总功率: 5.0W" in text, text
assert curve._tooltip_text(-1) == ""
# hover 事件路径（x 取绘图区中点）
mv = QMouseEvent(QEvent.Type.MouseMove, QPointF(mid_x, 100),
                 QtMod.MouseButton.NoButton,
                 QtMod.MouseButton.NoButton,
                 QtMod.KeyboardModifier.NoModifier)
curve.mouseMoveEvent(mv)
assert curve.hover_index() in (11, 12), curve.hover_index()
curve.leaveEvent(QEvent(QEvent.Type.Leave))
assert curve.hover_index() == -1
curve.deleteLater()

pw = PowerCurveWidget()
pw.resize(400, 200)
pw.show()
app.processEvents()
pw.set_series([1.0, 2.0, 3.0, 4.0], times=["a", "b", "c", "d"])
assert pw._nearest_point(-1) == -1
assert pw._tooltip_text(2) == "c · 3.0W"
assert pw.set_series([1.0], None) is None
pw.deleteLater()

sc = _SessionCurve()
sc.set_series([1.0, 5.0], ["10:00", "10:01"], protocols=["PD", "PD"])
assert sc._tooltip_text(1) == "10:01 · 5.0W\n协议: PD", sc._tooltip_text(1)
sc.set_series([1.0, 5.0], ["10:00", "10:01"], protocols=["PD", "QC"])
assert "协议: QC" in sc._tooltip_text(1)
assert sc._tooltip_text(9) == ""
sc.deleteLater()
print("3. MultiMetricCurve/PowerCurveWidget/_SessionCurve 悬浮命中与文案 OK")

print("TRAY SPAN + CURVE HOVER TEST ALL PASS")
