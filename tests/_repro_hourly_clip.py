# SPDX-License-Identifier: GPL-3.0-or-later
"""复现/验证统计页「每小时」柱状图被 deck 裁剪：真实壳条件
（对话框大壳 920x760 → 面板 maxHeight 660）下切到统计→每小时子 Tab,
实测 _TabDeck 高度与 _HourlyBarChart 最小高度的关系。

修复判据：deck 高度 ≥ 柱状图 minimumSizeHint（200px），且
hourly_chart 在 deck 内的完整矩形未被父级裁剪。

用法: .venv\\Scripts\\python.exe tests/_repro_hourly_clip.py
"""
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QDialog, QVBoxLayout

# 测试模块 import 期自建 QApplication(离屏),复用之
from tests.cuktech_history_test import (  # noqa: E402
    FakeService, _drain_jobs, app)

from app.ui.cuktech_panel import CuktechPanel  # noqa: E402
from app.core.models import DeviceInfo  # noqa: E402
from app.core.jobs import JobExecutor  # noqa: E402

# 模拟 DeviceDetailDialog 大壳：920x760 面板壳（与用户截图一致）
dialog = QDialog()
dialog.setFixedSize(920, 760)
lay = QVBoxLayout(dialog)
lay.setContentsMargins(16, 16, 16, 16)

device = DeviceInfo(did="1", name="CUKTECH 充电器",
                    model="cuktech.fitting.ad1204",
                    home_name="家", room_name="房", online=True)
panel = CuktechPanel(service=FakeService(), jobs=JobExecutor(), device=device)
lay.addWidget(panel)
dialog.show()

# 切到统计 Tab,喂假数据,再切「每小时」子 Tab
panel._tab_buttons[2].click()
_drain_jobs()
app.processEvents()

w = panel._energy_widget
w._select_tab(1)
_drain_jobs()
app.processEvents()

deck = w._deck
chart = w._hourly_chart
chart_min = chart.minimumSizeHint().height()
chart_min_set = chart.minimumHeight()
print(f"deck size                     : {deck.width()}x{deck.height()}")
print(f"hourly page size              : "
      f"{deck.current_page().width()}x{deck.current_page().height()}")
print(f"hourly_chart size             : {chart.width()}x{chart.height()}")
print(f"hourly_chart minimumHeight    : {chart_min_set}")
print(f"hourly_chart minimumSizeHint  : {chart_min}")

# 柱状图在 deck 坐标系里的可视矩形：超出 deck 底沿即被裁剪
chart_top = chart.mapTo(deck, chart.rect().topLeft()).y()
chart_bottom_in_deck = chart_top + chart.height()
deck_h = deck.height()
clipped = chart_bottom_in_deck > deck_h + 1
print(f"chart bottom in deck          : {chart_bottom_in_deck} "
      f"(deck height {deck_h})")

img = deck.grab()
img.save(str(Path(__file__).parent / "_repro_hourly_deck.png"))

if clipped or deck.height() < chart_min_set:
    print(f"CLIPPED: deck {deck_h} < chart need {chart_min_set}")
    sys.exit(1)
print("layout OK: chart fully visible inside deck")
dialog.hide()
