# SPDX-License-Identifier: GPL-3.0-or-later
"""桌面挂件（Widgets Board）离屏回归测试。

覆盖：board.json 存取与越界钳制、挂件渲染、列数重排、透明度实时
预览与悬停恢复、一键收起/展开、折叠横条状态点、管理对话框勾选与
列数/透明度预览信号、主窗口接线（数据推送与显示开关）。

与 theme_test 同样禁写运行数据：假设备不得泄漏进 devices_cache.json
或 board.json。
"""

import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"

# 配置写入临时目录：测试绝不能碰项目根下的真实配置
from tests._isolate import isolate

isolate()

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

app = QApplication([])

from app.core import cache as _device_cache
_device_cache.save = lambda *a, **k: None

from app.core import board_store

# 防回归：测试必须写在临时目录。隔离一旦失效，跑测试就会清空用户的
# 真实挂件设备（曾真实发生，用户表现为"每次启动设置都丢"）
from app.core import _json_store
assert "mihome-test-" in str(_json_store.data_dir()), \
    f"测试数据目录未隔离，会污染真实配置: {_json_store.data_dir()}"
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.core.jobs import JobExecutor
from app.ui import si_theme
from app.ui.si_theme import SiColors
from app.ui.theme_service import apply_theme
from app.ui.widgets_board import WidgetsBoard, _COLLAPSE_HEIGHT
from app.ui.board_manager import BoardManagerDialog

# 测试全程离线：挂件建卡后会补拉开关状态，假设备没有真实云端，
# 不桩掉会真的发网络请求并让任务线程迟迟不退出
WidgetsBoard.refresh_power = lambda self, dids: None

# ---------- 1. board.json 存取与越界钳制 ----------
board_store.save_devices(["d3", "d1", "d3", "d2"])
assert board_store.load_devices() == ["d3", "d1", "d2"], "去重保序失败"
board_store.set_columns(4)
assert board_store.get_columns() == 4
board_store.set_columns(99)
assert board_store.get_columns() == 9, "列数上限未钳制"
board_store.set_columns(0)
assert board_store.get_columns() == 1, "列数下限未钳制"
board_store.set_columns(3)
board_store.set_opacity(0.5)
assert abs(board_store.get_opacity() - 0.5) < 1e-6
board_store.set_opacity(2.0)
assert board_store.get_opacity() == 1.0, "透明度上限未钳制"
board_store.set_opacity(0.01)
assert board_store.get_opacity() == 0.30, "透明度下限未钳制"
board_store.set_geometry(120, 80, 500, 300)
assert board_store.get_geometry() == (120, 80, 500, 300)
board_store.set_geometry(1, 2, 0, 0)
assert board_store.get_geometry() is None, "非法几何未拒绝"
board_store.set_enabled(True)
assert board_store.get_enabled() is True
board_store.set_enabled(False)
assert board_store.get_enabled() is False
# 旧版两个字段（autostart / visible）必须折算成 enabled，否则升级后
# 用户的挂件要么凭空消失、要么莫名弹出
import json as _json

_legacy = _json_store.data_file("board.json")
_legacy.write_text(_json.dumps(
    {"version": 1, "devices": [], "autostart": True, "visible": False}),
    encoding="utf-8")
assert board_store.get_enabled() is True, "旧 autostart=True 未折算为 enabled"
_legacy.write_text(_json.dumps(
    {"version": 1, "devices": [], "autostart": False, "visible": True}),
    encoding="utf-8")
assert board_store.get_enabled() is True, "旧 visible=True 未折算为 enabled"
_legacy.write_text(_json.dumps(
    {"version": 1, "devices": [], "autostart": False, "visible": False}),
    encoding="utf-8")
assert board_store.get_enabled() is False, "旧版两项都关时不应自动开启"
_raw = board_store._read_raw()
assert "autostart" not in _raw and "visible" not in _raw, "迁移后应清掉旧键"
print("1. board.json 存取 + 越界钳制 + 旧开关迁移 OK")

# ---------- 2. 挂件渲染 + 空态 ----------
apply_theme("dark")
svc = MijiaService()
jobs = JobExecutor()
devs = [
    DeviceInfo(did=f"d{i}", name=f"设备{i}", model="a.b.c", home_name="家",
               room_name="客厅" if i % 2 == 0 else "卧室", online=(i % 3 != 0))
    for i in range(7)
]
board_store.save_devices([d.did for d in devs])
board = WidgetsBoard(svc, jobs, None)
assert board.columns() == 3
power = {d.did: (True if i % 2 == 0 else False) for i, d in enumerate(devs)}
board.set_devices(devs, power)
board.show()
app.processEvents()
assert board.isVisible()
img = board._root.grab().toImage()
panel = img.pixelColor(img.width() - 8, img.height() // 2)
assert panel.name().upper() in ("#1E1F24", "#F2F3F5"), f"挂件面板底色异常: {panel.name()}"
card = board._cards[devs[0].did]
assert card._power_btn is not None, "已知开关能力的设备应显示电源钮"
# 7 台设备 / 3 列 = 3 行；高度应容得下 3 行卡片
assert board.height() >= 3 * 56, f"挂件高度未按行数计算: {board.height()}"

# 空态：清空清单后显示引导文案而非空白面板
board_store.save_devices([])
board.set_devices(devs, power)
app.processEvents()
assert board._empty.isVisible(), "无设备时应显示空态引导"
assert not board._empty.text().strip() == "", "空态缺引导文案"
print("2. 挂件渲染 + 空态 OK")

# ---------- 3. 列数重排 ----------
board_store.save_devices([d.did for d in devs])
board.set_columns(1)
app.processEvents()
w1 = board.width()
board.set_columns(5)
app.processEvents()
w5 = board.width()
assert w5 > w1, f"列数增加后宽度应变大: {w1} -> {w5}"
assert board_store.get_columns() == 5
grid = board._grid
assert grid.count() == len(devs), f"网格条目数不符: {grid.count()}"
# 9 列越界应被钳到 9
board.set_columns(99)
assert board.columns() == 9
board.set_columns(3)
print("3. 列数重排 OK")

# ---------- 4. 透明度实时预览 + 悬停恢复 ----------
# windowOpacity 由平台按 1/255 量化，断言用 _OPACITY_TOL 而非浮点精确值
_OPACITY_TOL = 1.0 / 255.0 + 1e-6


def _wait_opacity(widget, target: float, timeout: float = 2.0) -> None:
    """等透明度动画跑到目标值：悬停恢复/移开回落是 180ms 的淡入淡出。"""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        app.processEvents()
        if abs(widget.windowOpacity() - target) < _OPACITY_TOL:
            return
        _time.sleep(0.01)


board.set_opacity(0.45)
app.processEvents()
assert abs(board.windowOpacity() - 0.45) < _OPACITY_TOL, \
    f"透明度未生效: {board.windowOpacity()}"
assert abs(board_store.get_opacity() - 0.45) < 1e-6, "透明度未落盘"
board.set_opacity(5.0)
assert abs(board.opacity() - 1.0) < 1e-6, "透明度未钳制"
# 鼠标进入 → 恢复不透明
board.set_opacity(0.45)
enter = QMouseEvent(QEvent.Type.Enter, QPoint(5, 5), Qt.MouseButton.NoButton,
                   Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
app.sendEvent(board, enter)
_wait_opacity(board, 1.0)
assert abs(board.windowOpacity() - 1.0) < _OPACITY_TOL, "悬停未恢复不透明"
leave = QMouseEvent(QEvent.Type.Leave, QPoint(5, 5), Qt.MouseButton.NoButton,
                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
app.sendEvent(board, leave)
_wait_opacity(board, 0.45)
assert abs(board.windowOpacity() - 0.45) < _OPACITY_TOL, "移开后未恢复设定透明度"
# 预览期间（管理对话框打开）不强制不透明，否则预览值被顶回 1.0
board.setProperty("previewing", True)
app.sendEvent(board, enter)
_wait_opacity(board, 0.45)
assert abs(board.windowOpacity() - 0.45) < _OPACITY_TOL, "预览期间被悬停恢复顶回"
board.setProperty("previewing", False)
app.processEvents()
board.set_opacity(0.92)
print("4. 透明度预览 + 悬停恢复 OK")

# ---------- 5. 一键收起 / 展开 ----------
x0, y0 = board.x(), board.y()
board._collapse()
app.processEvents()
assert board.collapsed is True
assert board_store.get_collapsed() is True
assert board.height() == _COLLAPSE_HEIGHT, f"收起后高度应为 {_COLLAPSE_HEIGHT}: {board.height()}"
assert board._bar.isVisible() and not board._root.isVisible()
assert (board.x(), board.y()) == (x0, y0), "收起后位置应保持不变"
# 折叠横条每台设备一个状态点
dots = board._dot_lay.count() - 1  # 末尾有一个 stretch
assert dots == len(devs), f"状态点数不符: {dots} vs {len(devs)}"
assert board_store.get_collapsed() is True

board._expand()
app.processEvents()
assert board.collapsed is False
assert board_store.get_collapsed() is False
assert board._root.isVisible() and not board._bar.isVisible()
assert board.width() > 120, "展开后应恢复网格宽度"
# 托盘菜单入口走的 toggle：折叠↔展开
board.toggle_collapse()
assert board.collapsed is True
board.toggle_collapse()
assert board.collapsed is False
print("5. 一键收起/展开 OK")


# ---------- 5b. 折叠横条：单击展开 / 拖动移动且不展开 ----------
def _press(widget, global_pos):
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(4, 4),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    ev._gpos = global_pos  # 见下方 _send 的说明
    return ev


def _send_bar_mouse(kind, global_pos, buttons=Qt.MouseButton.LeftButton):
    """把鼠标事件直接送给折叠横条的事件过滤器（走真实 eventFilter 路径）。"""
    return _send(board._bar, kind, global_pos, buttons)


def _send(target, kind, gpos, buttons=Qt.MouseButton.LeftButton):
    """构造带 globalPosition 的伪鼠标事件交给 eventFilter。

    QMouseEvent 的构造函数无法注入 globalPosition，用包装对象补齐；
    eventFilter 只用到 type/button/buttons/globalPosition 四个成员。
    """
    ev = QMouseEvent(kind, QPoint(4, 4), Qt.MouseButton.LeftButton,
                     buttons, Qt.KeyboardModifier.NoModifier)

    class _Wrapped:
        def __init__(self, inner, pos):
            self._inner = inner
            self._gpos = pos

        def type(self):
            return self._inner.type()

        def button(self):
            return self._inner.button()

        def buttons(self):
            return self._inner.buttons()

        def globalPosition(self):
            return QPointF(self._gpos)

    return board.eventFilter(target, _Wrapped(ev, gpos))


board._collapse()
app.processEvents()
assert board.collapsed is True
origin = QPoint(board.x(), board.y())

# 单击（无位移）→ 展开
_send_bar_mouse(QEvent.Type.MouseButtonPress, origin + QPoint(20, 12))
_send_bar_mouse(QEvent.Type.MouseButtonRelease, origin + QPoint(20, 12),
                Qt.MouseButton.NoButton)
app.processEvents()
assert board.collapsed is False, "单击折叠横条应展开"

# 拖动（越过阈值）→ 窗口移动、留在折叠态、位置落盘
board._collapse()
app.processEvents()
assert board.collapsed is True
origin = QPoint(board.x(), board.y())
_send_bar_mouse(QEvent.Type.MouseButtonPress, origin + QPoint(20, 12))
_send_bar_mouse(QEvent.Type.MouseMove, origin + QPoint(90, 52))
app.processEvents()
assert board.collapsed is True, "拖动不应展开"
assert (board.x(), board.y()) == (origin.x() + 70, origin.y() + 40), \
    f"拖动未移动窗口: {board.geometry()}"
_send_bar_mouse(QEvent.Type.MouseButtonRelease, origin + QPoint(90, 52),
                Qt.MouseButton.NoButton)
app.processEvents()
assert board.collapsed is True, "拖动后松手不应展开"
# 拖动位置要落盘，展开后回到新位置
board._expand()
app.processEvents()
assert board.x() == origin.x() + 70, f"展开后未回到拖动后的位置: {board.x()}"
print("5b. 折叠横条单击展开 / 拖动移动 OK")

# 5c. 展开态：标题栏与面板空白处都能拖；单击标题栏收起
assert board.collapsed is False
base = QPoint(board.x(), board.y())
_send(board._handle, QEvent.Type.MouseButtonPress, base + QPoint(40, 8))
_send(board._handle, QEvent.Type.MouseMove, base + QPoint(10, 38))
app.processEvents()
moved = QPoint(board.x() - base.x(), board.y() - base.y())
assert moved != QPoint(0, 0), f"标题栏拖拽失效，窗口未移动: {board.geometry()}"
assert board.collapsed is False, "拖拽标题栏不应改变折叠态"
_send(board._handle, QEvent.Type.MouseButtonRelease, base + QPoint(10, 38),
      Qt.MouseButton.NoButton)
app.processEvents()
assert board.collapsed is False, "拖动后松手不应收起"
print(f"5c. 展开态标题栏拖拽 OK（位移 {moved.x()},{moved.y()}）")

# 5d. 单击标题行 → 收起（不拖动才算点击）
assert board.collapsed is False
click_at = QPoint(board.x(), board.y()) + QPoint(50, 8)
_send(board._handle, QEvent.Type.MouseButtonPress, click_at)
_send(board._handle, QEvent.Type.MouseButtonRelease, click_at,
      Qt.MouseButton.NoButton)
app.processEvents()
assert board.collapsed is True, "单击标题行应收起为细横条"
assert board_store.get_collapsed() is True, "收起状态未落盘"

# 再单击标题行不应有副作用（此时已折叠，标题行不可见）
# 展开回来，确认可以反复收放
board._expand()
app.processEvents()
assert board.collapsed is False
click_at = QPoint(board.x(), board.y()) + QPoint(50, 8)
_send(board._handle, QEvent.Type.MouseButtonPress, click_at)
_send(board._handle, QEvent.Type.MouseButtonRelease, click_at,
      Qt.MouseButton.NoButton)
app.processEvents()
assert board.collapsed is True, "第二次单击标题行未收起"
board._expand()
app.processEvents()
print("5d. 单击标题行收起 OK")

# ---------- 6. 折叠态仍跟随开关状态 ----------
board._collapse()
board._apply_power({devs[0].did: False, devs[1].did: True})
app.processEvents()
assert board._bar.isVisible(), "折叠态应只更新状态点，不展开网格"
board._expand()
print("6. 折叠态状态同步 OK")

# ---------- 7. 管理对话框：勾选 + 列数/透明度预览信号 ----------
board_store.save_devices([devs[0].did, devs[2].did])
dlg = BoardManagerDialog(devs, None)
assert dlg.selected_dids() == [devs[0].did, devs[2].did], "初始勾选未按存储顺序"
previews = {"opacity": [], "columns": []}
dlg.opacity_preview.connect(previews["opacity"].append)
dlg.columns_preview.connect(previews["columns"].append)
# 勾选一台新设备
target = devs[4].did
dlg._checks[target].setChecked(True)
assert target in dlg.selected_dids()
# 取消勾选一台已选设备
dlg._checks[devs[0].did].setChecked(False)
assert devs[0].did not in dlg.selected_dids()
# 列数按钮：1–9 共 9 个，点 4 应落盘并发出预览信号
assert len(dlg._col_buttons) == 9, f"列数按钮数量不符: {len(dlg._col_buttons)}"
assert [b.text() for b in dlg._col_buttons] == [str(n) for n in range(1, 10)]
for btn in dlg._col_buttons:
    if btn.text() == "4":
        btn.click()
assert board_store.get_columns() == 4
assert previews["columns"] and previews["columns"][-1] == 4
# 透明度滑块：拖到 40% 应落盘并发出预览信号
dlg._opacity_slider.setValue(40)
assert abs(dlg.opacity() - 0.40) < 1e-6
assert abs(board_store.get_opacity() - 0.40) < 1e-6
assert previews["opacity"] and abs(previews["opacity"][-1] - 0.40) < 1e-6
# 关闭不崩溃（淡出动画路径）
dlg.done(0)
app.processEvents()
print("7. 管理对话框勾选 + 预览信号 OK")

# ---------- 8. 主窗口接线 ----------
sys.path.insert(0, ".")
from app.ui.main_window import MainWindow

# 测试全程离线：主窗口的开关轮询/温湿度拉取都走真实网络，假设备没有
# 云端，桩掉才不会卡住任务线程（挂件的 refresh_power 已在前面桩掉）
MainWindow._refresh_power_states = lambda self, force: None
MainWindow._refresh_metrics = lambda self: None

board_store.set_columns(3)
board_store.set_opacity(0.92)
board_store.save_devices([d.did for d in devs])
win = MainWindow()
assert win._board is not None, "挂件创建失败"
win._apply_devices(devs)
app.processEvents()
# 顶栏状态胶囊：亮灯数/温湿度/耗材按当前范围刷新
win._known_power = {devs[0].did: True, devs[1].did: False}
win._metrics = {devs[0].did: "22.1°C 69%"}
# 温湿度胶囊读原始读数：副标题文案可能被用户改成别的属性，
# 不能从文案里解析，所以这里单独喂一份 _climate
win._climate = {devs[0].did: {"temperature": 22.1, "relative-humidity": 69.0}}
win._consumables = [{"did": devs[0].did, "name": "牙刷", "description": "电池",
                     "value": 22, "threshold": 25, "low": True, "updated": 0}]
win._update_count_label()
assert not win._light_pill.isHidden() and "开启 1" in win._light_pill.text(), \
    f"开启设备数胶囊异常: {win._light_pill.text()}"
assert not win._temp_pill.isHidden() and "22.1" in win._temp_pill.text(), \
    f"温度胶囊异常: {win._temp_pill.text()}"
assert not win._humidity_pill.isHidden() and "69" in win._humidity_pill.text(), \
    f"湿度胶囊异常: {win._humidity_pill.text()}"
assert not win._consumable_pill.isHidden(), "耗材不足胶囊未显示"
# 主窗口设备推送后挂件拿到全量清单
assert len(win._board._dids) == len(devs)
# 关闭主窗口房间筛选不应影响挂件（挂件用全量列表）
before = len(win._board._cards)
win._select_room("客厅")
app.processEvents()
assert len(win._board._cards) == before, "挂件不应随主窗口房间筛选变化"

# 显示开关
win.hide_board()
app.processEvents()
assert not win._board.isVisible()
win.show_board()
app.processEvents()
assert win._board.isVisible()

# 收起开关（托盘菜单同款入口）
win.toggle_board_collapse()
assert win._board.collapsed is True
win.toggle_board_collapse()
assert win._board.collapsed is False

# 桌面挂件开关：开启 → 显示；关闭 → 隐藏（开关替代旧的 autostart/visible）
board_store.set_enabled(True)
win._apply_board_enabled()
app.processEvents()
assert win._board.isVisible(), "开启「桌面挂件」开关后应显示"
board_store.set_enabled(False)
win._apply_board_enabled()
app.processEvents()
assert not win._board.isVisible(), "关闭开关后应隐藏"
board_store.set_enabled(True)
win._apply_board_enabled()
app.processEvents()

# 温湿度读数同步到挂件副标题
win.show_board()
app.processEvents()
win._metrics[devs[1].did] = "22.5℃ 45%"
win._push_tray_metrics()
app.processEvents()
card1 = win._board._cards.get(devs[1].did)
assert card1 is not None and "22.5℃" in card1._sub.text(), "读数未同步到挂件卡片"
# 隐藏期间推送的读数，显示时应补回（否则一直显示旧值）
win.hide_board()
app.processEvents()
win._metrics[devs[1].did] = "26.0℃ 50%"
win._push_tray_metrics()
win.show_board()
app.processEvents()
assert "26.0℃" in win._board._cards[devs[1].did]._sub.text(), "隐藏期间的读数未补回"

# 主题切换：挂件整窗重建样式
from PySide6.QtGui import QIcon  # noqa: F401  (确保 QtGui 已加载)
win.apply_theme_mode("light")
app.processEvents()
img = win._board._root.grab().toImage()
light = img.pixelColor(img.width() - 8, img.height() // 2)
assert light.name().upper() == "#F2F3F5", f"切浅色后面板底色未刷新: {light.name()}"
win.apply_theme_mode("dark")
app.processEvents()
img = win._board._root.grab().toImage()
dark = img.pixelColor(img.width() - 8, img.height() // 2)
assert dark.name().upper() == "#1E1F24", f"切深色后面板底色未刷新: {dark.name()}"
print("8. 主窗口接线 OK")

# ---------- 9. 「桌面挂件」开关是显示与否的唯一来源 ----------
# 旧版用 autostart + visible 两个字段表达"要不要开"，勾了关不掉、
# 关了又自己弹。现在只有一个 enabled：开着就显示，关着就隐藏。
board_store.set_enabled(False)
win._apply_board_enabled()
app.processEvents()
assert not win._board.isVisible(), "开关关闭 → 必须隐藏"

board_store.set_enabled(True)
win._apply_board_enabled()
app.processEvents()
assert win._board.isVisible(), "开关开启 → 必须显示"

# 点 × 只是本次收起，不动开关（重启后仍会回来）
win._board.hide_by_user()
app.processEvents()
assert not win._board.isVisible()
assert board_store.get_enabled() is True, "点 × 不应改开关状态"
win._apply_board_enabled()      # 模拟下次启动
app.processEvents()
assert win._board.isVisible(), "开关仍开着，重启应恢复显示"

# 关掉开关后再启动也不该出现
board_store.set_enabled(False)
win._apply_board_enabled()
app.processEvents()
assert not win._board.isVisible()
board_store.set_enabled(True)
win._apply_board_enabled()
app.processEvents()
print("9. 桌面挂件开关决定显示（点 × 不改开关）OK")

# ---------- 10. 背景色：预设 / 自定义 / 跟随主题 ----------
# 存储层：只认 #rrggbb，非法值不写盘（写坏会把挂件刷成全黑或透明）
board_store.set_bg("#000000")
assert board_store.get_bg() == "#000000", "背景色未落盘"
board_store.set_bg("黑色")
assert board_store.get_bg() == "#000000", "非法颜色不得覆盖已有值"
board_store.set_bg("#ABCDEF")
assert board_store.get_bg() == "#abcdef", "颜色应统一小写"

win.show_board()
app.processEvents()
# 1) 纯黑应立刻生效，且卡片/文字自动反色（黑底黑字是这次要防的核心问题）
win._on_board_background_preview("#000000")
app.processEvents()
assert win._board.bg() == "#000000"
img = win._board._root.grab().toImage()
corner = img.pixelColor(img.width() - 8, img.height() // 2)
assert corner.red() < 40 and corner.green() < 40 and corner.blue() < 40, \
    f"选纯黑后挂件底色不是黑色: {corner.name()}"
name_style = win._board._cards[devs[1].did]._name.styleSheet()
assert "#f2f3f5" in name_style.lower(), f"深色底上文字未转亮色: {name_style}"
# 离线设备的文字要比在线设备更暗（rgba 透明度更低），否则看不出离线
offline_style = win._board._cards[devs[0].did]._name.styleSheet()
assert "rgba(255, 255, 255" in offline_style, \
    f"深色底上离线设备文字未转半透明白: {offline_style}"

# 2) 浅色底（米白）文字要转深色，不能沿用亮字
win._on_board_background_preview("#f5f1e8")
app.processEvents()
name_style = win._board._cards[devs[1].did]._name.styleSheet()
assert "#1a1b1f" in name_style.lower(), f"浅色底上文字未转深色: {name_style}"

# 2b) 边框/色块这类"结构性"颜色必须是不透明色：
# 半透明边框在非整数缩放（系统 125% × 界面 125% = 156%）下会画成
# 2px 渐变线，相邻卡片的对齐位置不同，看起来就是锯齿 + 角上亮斑。
# 跟随主题用的本来就是 hex，所以这个坑只在自定义底色下出现。
for _bg in ("#000000", "#f5f1e8"):          # 深、浅两条派生分支都要查
    win._on_board_background_preview(_bg)
    app.processEvents()
    _pal = win._board.palette
    for _name in ("line", "scrollbar", "scrollbar_hover",
                  "state_unknown_border", "state_off", "offline_sub"):
        _value = getattr(_pal, _name)
        assert _value.startswith("#") and len(_value) == 7, \
            f"自定义底色 {_bg} 下 {_name} 不是不透明色: {_value}"

# 3) 折叠横条跟随同一底色（收起后不能突然变回主题灰）
win._board._collapse()
app.processEvents()
assert "#f5f1e8" in win._board._bar.styleSheet().lower(), \
    f"折叠横条未跟随背景色: {win._board._bar.styleSheet()}"
win._board._expand()
app.processEvents()

# 4) 跟随主题：恢复后与主题色一致，且主题切换能继续带动
win._on_board_background_preview(None)
app.processEvents()
assert win._board.bg() is None
img = win._board._root.grab().toImage()
corner = img.pixelColor(img.width() - 8, img.height() // 2)
assert corner.name().upper() == "#1E1F24", f"恢复跟随主题后底色异常: {corner.name()}"
print("10. 背景色（黑/浅色反色 + 折叠条同步 + 跟随主题）OK")

# ---------- 11. 窗口置顶开关（默认不置顶）----------
from PySide6.QtCore import Qt as _Qt

assert board_store.get_topmost() is False, "置顶应默认关闭"
assert win._board.is_topmost() is False
assert not (win._board.windowFlags() & _Qt.WindowType.WindowStaysOnTopHint), \
    "默认不置顶时窗口标志里不应出现 WindowStaysOnTopHint"
# 非置顶的桌面挂件不允许抢焦点：点一下只响应点击，不把用户正在用的窗口顶走
assert (win._board.windowFlags() & _Qt.WindowType.WindowDoesNotAcceptFocus), \
    "非置顶时应禁止挂件接受焦点"
# 层级归位走原生 SetWindowPos；离屏环境也不该抛异常
win._board.apply_z_order()

# 预览路径只改真实窗口、不落盘（落盘由管理对话框负责，与透明度同一约定）
win._on_board_topmost_preview(True)
app.processEvents()
assert win._board.is_topmost() is True
assert win._board.windowFlags() & _Qt.WindowType.WindowStaysOnTopHint, \
    "开启置顶后窗口标志未带上 WindowStaysOnTopHint"
assert not (win._board.windowFlags() & _Qt.WindowType.WindowDoesNotAcceptFocus), \
    "置顶时不需要禁止焦点"
assert board_store.get_topmost() is False, "预览路径不应落盘"

# 带落盘的路径（挂件自身 API / 对话框）才写 board.json
win._board.set_topmost(True)
assert board_store.get_topmost() is True, "置顶状态未落盘"
win._board.set_topmost(False)
app.processEvents()
assert board_store.get_topmost() is False
assert not (win._board.windowFlags() & _Qt.WindowType.WindowStaysOnTopHint), \
    "关闭置顶后窗口标志未清除"
assert (win._board.windowFlags() & _Qt.WindowType.WindowDoesNotAcceptFocus), \
    "关闭置顶后应恢复禁止焦点"
# 改窗口标志会重建原生窗口，几何必须还原（否则挂件跳回屏幕左上角）
geo = win._board.geometry()
assert geo.width() > 0 and geo.height() > 0, "切换置顶后窗口尺寸丢失"
assert win._board.isVisible(), "切换置顶后窗口被隐藏了（需重新 show）"

# 标题栏图钉：一键切换置顶，选中态用主题色底
assert hasattr(win._board, "_pin_toggle"), "标题栏缺少置顶图钉按钮"
win._board._pin_toggle.click()
app.processEvents()
assert win._board.is_topmost() is True, "点图钉未开启置顶"
assert SiColors.THEME in win._board._pin_toggle.styleSheet(), \
    f"置顶时图钉未用主题色底: {win._board._pin_toggle.styleSheet()}"
win._board._pin_toggle.click()
app.processEvents()
assert win._board.is_topmost() is False, "再点图钉未取消置顶"
assert SiColors.THEME not in win._board._pin_toggle.styleSheet(), \
    "取消置顶后图钉仍是主题色底"
print("11. 置顶开关（默认关 / 可开可关 / 几何与显示不丢 / 图钉切换）OK")

# ---------- 12. 管理对话框：背景色与置顶的预览信号 ----------
board_store.set_bg(None)
board_store.set_topmost(False)
dlg2 = BoardManagerDialog(devs, None)
# 右上角的 × 已去掉：底部「取消」承担关闭，避免重复入口
assert not hasattr(dlg2, "_close_btn"), "挂件管理窗不应再有关闭按钮"
bg_events: list = []
top_events: list = []
dlg2.background_preview.connect(bg_events.append)
dlg2.topmost_preview.connect(top_events.append)
# 「自动」= 跟随主题，初始应选中
assert dlg2._bg_chips[0].isChecked(), "初始应选中「自动」"
# 点第一个预设色块（纯黑）
dlg2._bg_chips[1].click()
assert board_store.get_bg() == "#000000", "色块未落盘"
assert bg_events and bg_events[-1] == "#000000", f"未发出背景色预览信号: {bg_events}"
# 自定义颜色路径：直接注入 picker 结果，避免测试弹真实取色器
from PySide6.QtGui import QColor as _QColor

orig_picker = dlg2._choose_custom_bg
dlg2._choose_bg(_QColor("#123456").name())
assert board_store.get_bg() == "#123456"
assert all(not c.isChecked() for c in dlg2._bg_chips[1:]), \
    "自定义颜色不在预设里时不应选中任何预设色块"
assert dlg2._bg_hint.text() == "#123456", "提示未显示自定义色号"
# 自动 → 恢复跟随主题
dlg2._bg_chips[0].click()
assert board_store.get_bg() is None
assert bg_events[-1] is None, "恢复跟随主题应发出 None"
# 两个勾选框用自绘样式：选中时画主题色方块 + 对勾，不只是填色
from app.ui.check_box import SiCheckBox

assert isinstance(dlg2._topmost_check, SiCheckBox), "置顶勾选框未用自绘样式"
assert isinstance(dlg2._lock_check, SiCheckBox), "锁定勾选框未用自绘样式"
assert dlg2._topmost_check.sizeHint().width() > 20, "勾选框宽度异常"
# 置顶勾选框
dlg2._topmost_check.setChecked(True)
assert board_store.get_topmost() is True
assert top_events and top_events[-1] is True, f"未发出置顶预览信号: {top_events}"
dlg2.done(0)
app.processEvents()
print("12. 管理对话框背景色 + 置顶预览信号 OK")

# ---------- 13. 搜索设备 + 离线设备（灰名仍可见） ----------
extra_offline = DeviceInfo(did="d_off", name="次卧无人灯", model="a.b.c",
                           home_name="家", room_name="次卧", online=False)
search_devs = devs + [extra_offline]
board_store.save_devices([devs[0].did])
dlg3 = BoardManagerDialog(search_devs, None)
# 离线设备也要能加入挂件（常驻面板常放不常开的灯），只是名字标灰
assert "d_off" in dlg3._rows, "未勾选的离线设备也应出现在候选列表"
off_row = dlg3._rows["d_off"]
off_name = off_row.findChild(QLabel)
assert off_name is not None and SiColors.OFFLINE_TEXT in off_name.styleSheet(), \
    "离线设备名未用灰色显示"
# 已选中的设备即使离线也要保留（否则保存时会被静默丢掉）
board_store.save_devices([devs[0].did, extra_offline.did])
dlg4 = BoardManagerDialog(search_devs, None)
assert extra_offline.did in dlg4._rows, "已选中的离线设备必须保留，否则保存会丢设备"
assert dlg4._checks[extra_offline.did].isChecked()
dlg4.done(0)
app.processEvents()

# 搜索：按设备名过滤（假数据里 设备1 唯一）
dlg3._search.setText("设备1")
app.processEvents()
visible = [d for d, r in dlg3._rows.items() if not r.isHidden()]
assert visible == [devs[1].did], f"按名称搜索未过滤: {visible}"
# 按房间搜索：卧室里的设备都该出现（含离线的 d3，现在也允许加入挂件）
dlg3._search.setText("卧室")
app.processEvents()
visible = [d for d, r in dlg3._rows.items() if not r.isHidden()]
assert set(visible) == {devs[1].did, devs[3].did, devs[5].did}, \
    f"按房间搜索未过滤: {visible}"
# 清空恢复全部
dlg3._search.setText("")
app.processEvents()
assert all(not r.isHidden() for r in dlg3._rows.values()), "清空搜索后应恢复全部行"
# 搜索结果为空时不崩
dlg3._search.setText("不存在的设备")
app.processEvents()
assert all(r.isHidden() for r in dlg3._rows.values())
dlg3.done(0)
app.processEvents()
print("13. 搜索设备 + 离线设备灰名可见 OK")

# ---------- 13b. 管理窗房间筛选下拉 ----------
# 候选列表是全量设备、跨家庭混排，选项文字必须带家庭名：两个家庭各有
# 一间「卧室」时只看房间名无从分辨是哪一间
room_dlg = BoardManagerDialog(search_devs, None)
room_labels = [room_dlg._room_combo.itemText(i)
               for i in range(room_dlg._room_combo.count())]
assert room_labels[0] == "全部房间", f"下拉首项应是全部房间: {room_labels}"
assert set(room_labels[1:]) == {"家 · 客厅", "家 · 卧室", "家 · 次卧"}, \
    f"房间选项不符: {room_labels}"
assert not room_dlg._room_combo.isHidden(), "有房间归属的设备时下拉应显示"

# 选「卧室」：只剩该房间的设备行，勾选状态一概不动
checked_before = list(room_dlg.selected_dids())
room_dlg._room_combo.setCurrentIndex(room_labels.index("家 · 卧室"))
app.processEvents()
in_bedroom = {devs[1].did, devs[3].did, devs[5].did}
visible = {d for d, r in room_dlg._rows.items() if not r.isHidden()}
assert visible == in_bedroom, f"按房间筛选未生效: {sorted(visible)}"
assert list(room_dlg.selected_dids()) == checked_before, \
    "房间筛选不该改动勾选状态"

# 关键字与房间取交集：卧室 + 「设备1」只剩一台
room_dlg._search.setText("设备1")
app.processEvents()
visible = {d for d, r in room_dlg._rows.items() if not r.isHidden()}
assert visible == {devs[1].did}, f"房间与关键字未取交集: {sorted(visible)}"

# 房间退回「全部房间」时关键字仍生效
room_dlg._room_combo.setCurrentIndex(0)
app.processEvents()
visible = {d for d, r in room_dlg._rows.items() if not r.isHidden()}
assert visible == {devs[1].did}, f"清房间后应回到纯关键字结果: {sorted(visible)}"
room_dlg._search.setText("")
app.processEvents()
assert all(not r.isHidden() for r in room_dlg._rows.values()), \
    "清空关键字后应恢复全部行"

# 宽度贴当前选中项：选「全部房间」时不预留最长房间名的空白
narrow = room_dlg._room_combo.width()
room_dlg._room_combo.setCurrentIndex(room_labels.index("家 · 卧室"))
app.processEvents()
wide = room_dlg._room_combo.width()
assert wide > narrow, f"下拉宽度未跟随选中项: {narrow} -> {wide}"
room_dlg._room_combo.setCurrentIndex(0)
app.processEvents()
assert room_dlg._room_combo.width() == narrow, "退回全部房间后宽度应恢复"

# 两个家庭各有一间「卧室」：必须是两个独立选项，筛选不能串到对方
cross = [
    DeviceInfo(did="c1", name="主卧灯", model="a.b.c", home_name="家庭1",
               room_name="卧室", online=True),
    DeviceInfo(did="c2", name="侧卧灯", model="a.b.c", home_name="家庭2",
               room_name="卧室", online=True),
]
cross_dlg = BoardManagerDialog(cross, None)
cross_labels = [cross_dlg._room_combo.itemText(i)
                for i in range(cross_dlg._room_combo.count())]
assert set(cross_labels[1:]) == {"家庭1 · 卧室", "家庭2 · 卧室"}, \
    f"同名房间被合并成一项: {cross_labels}"
cross_dlg._room_combo.setCurrentIndex(cross_labels.index("家庭2 · 卧室"))
app.processEvents()
visible = {d for d, r in cross_dlg._rows.items() if not r.isHidden()}
assert visible == {"c2"}, f"同名房间筛选串到了另一个家庭: {sorted(visible)}"
cross_dlg.done(0)

# 场景没有房间归属：房间筛选不能把场景页藏空
scene_only = [{"scene_id": "rs", "name": "回家", "home_id": "h", "home_name": "家"}]
scene_dlg = BoardManagerDialog(search_devs, None, scenes=scene_only)
scene_dlg._room_combo.setCurrentIndex(1)      # 任一具体房间
scene_dlg._apply_scene_filter("")
assert not scene_dlg._rows["scene:rs"].isHidden(), "房间筛选把场景行也藏掉了"
scene_dlg.done(0)

# 全部设备都没有房间归属时没有可选项，下拉整块收起
no_room = [DeviceInfo(did="nr", name="手表", model="a.b.c",
                      home_name="未知", room_name="未知", online=True)]
nr_dlg = BoardManagerDialog(no_room, None)
assert nr_dlg._room_combo.count() == 1 and nr_dlg._room_combo.isHidden(), \
    "没有房间可筛时下拉应隐藏"
nr_dlg.done(0)
room_dlg.done(0)
app.processEvents()

# 设备清单刷新后：已选房间还在就保留，房间消失则退回「全部房间」
from app.ui import room_filter as _room_filter

combo = _room_filter.make_room_combo()
_room_filter.refresh_room_combo(combo, search_devs)
_bedroom_idx = next(i for i in range(combo.count())
                    if combo.itemData(i) == ("家", "卧室"))
combo.setCurrentIndex(_bedroom_idx)
_room_filter.refresh_room_combo(combo, search_devs)
assert combo.currentData() == ("家", "卧室"), "刷新后应保留已选房间"
_room_filter.refresh_room_combo(combo, devs[::2])   # 只剩客厅设备
assert combo.currentData() is None and combo.count() == 2, \
    "房间消失后应退回全部房间"
print("13b. 管理窗房间筛选（选项 / 交集 / 场景不受影响 / 无房间隐藏 / 刷新保留）OK")

# ---------- 14. 连点「+」只能开一个管理窗 ----------
import app.ui.main_window as _mw

created: list = []
_RealDialog = _mw.BoardManagerDialog


class _SpyDialog(_RealDialog):
    def exec(self):
        created.append(1)
        if len(created) == 1:
            # 模拟模态窗打开期间又点了一次「+」
            win.show_board_manager()
        return 0


_mw.BoardManagerDialog = _SpyDialog
board_store.save_devices([d.did for d in devs])
win.show_board_manager()
_mw.BoardManagerDialog = _RealDialog
assert len(created) == 1, f"连点 + 开了多个管理窗: {len(created)} 个"
assert win._board_manager is None, "对话框关闭后未清空引用（会影响下次打开）"
print("14. 连点 + 只开一个管理窗 OK")

# ---------- 15. 折叠横条：宽度锚定展开宽度 + 设备多时自动降级 ----------
from app.ui.widgets_board import _AggregateBar as _AggBar


def _dot_widgets(b):
    out = []
    for i in range(b._dot_lay.count()):
        w = b._dot_lay.itemAt(i).widget()
        if w is not None:
            out.append(w)
    return out


def _has_aggregate(b):
    return any(isinstance(w, _AggBar) for w in _dot_widgets(b))


many = [DeviceInfo(did=f"m{i}", name=f"设备{i}", model="a.b.c", home_name="家",
                   room_name="客厅", online=(i % 5 != 0)) for i in range(20)]
many_power = {d.did: (i % 2 == 0) for i, d in enumerate(many)}

# 1 列 + 20 台：设备再多也不能把横条撑宽
board_store.set_columns(1)
board.set_columns(1)
board_store.save_devices([d.did for d in many])
board.set_devices(many, many_power)
board._collapse()
app.processEvents()
assert board.collapsed
one_col = board._board_width()
assert one_col < 200, f"1 列宽度异常（卡片变宽了？）: {one_col}"
# 折叠横条宽度锚定展开宽度，但不得低于横条自身的最小宽（离屏字体度量
# 偏大时聚合条最小宽会高于 one_col；真机字体正常时两者一致）
assert board.width() == max(one_col, board._bar.minimumSizeHint().width()), \
    f"折叠横条宽度未锚定展开宽度: {board.width()} vs {max(one_col, board._bar.minimumSizeHint().width())}"
assert _has_aggregate(board), f"20 台设备在 {one_col}px 里应降级为聚合状态条"
assert "设备0" in board._bar.toolTip(), "聚合模式应在 tooltip 里给出逐台明细"
assert board._bar_title.isHidden(), "窄横条下标题应隐藏，把位置让给状态"

# 1 列 + 8 台：体积够，回到逐台状态点
board_store.save_devices([d.did for d in many[:8]])
board.set_devices(many[:8], {k: v for k, v in many_power.items()
                            if k in {d.did for d in many[:8]}})
app.processEvents()
assert board.width() == max(one_col, board._bar.minimumSizeHint().width())
assert not _has_aggregate(board), f"8 台在 {one_col}px 里应仍用逐台状态点"
assert len(_dot_widgets(board)) >= 8, "状态点数量不足"

# 6 列：宽度跟着列数走，点也放得下
board_store.set_columns(6)
board.set_columns(6)
board_store.save_devices([d.did for d in many])
board.set_devices(many, many_power)
app.processEvents()
six_col = board._board_width()
assert six_col > one_col, f"列数增加宽度没跟着变大: {one_col} -> {six_col}"
assert board.width() == six_col, \
    f"改列数后折叠宽度未跟随: {board.width()} vs {six_col}"
assert not _has_aggregate(board), f"{six_col}px 宽度下 20 台应能用逐台状态点"

# 展开 → 宽度回到展开态（同一套锚点）
board._expand()
app.processEvents()
assert not board.collapsed and board.width() == six_col

# 逐台点模式也必须保留标题和计数：设备多时标题曾被挤掉，横条上
# 看不出这是谁家的挂件、也看不到台数
board_store.set_columns(2)
board.set_columns(2)
board.set_devices(many[:16], {k: v for k, v in many_power.items()
                             if k in {d.did for d in many[:16]}})
board._collapse()
app.processEvents()
assert not _has_aggregate(board), "2 列 16 台应仍是逐台状态点"
assert not board._bar_title.isHidden(), "逐台点模式把标题挤掉了"
# 横条上不再显示「n/m 开」计数
assert not hasattr(board, "_bar_count"), "折叠横条仍残留计数 label"
# 恢复展开态：后续用例按「展开 → 推送设备」的顺序继续
board._expand()
app.processEvents()

# 单列与多列卡宽一致：从多列切单列后窗口必须缩回去，旧布局的
# minimumSizeHint 会把它顶在旧宽度上（用户实测：要收起再展开才恢复）
board.set_columns(2)
app.processEvents()
two_col_card = next(iter(board._cards.values())).width()
board.set_columns(1)
app.processEvents()
one_col_card = next(iter(board._cards.values())).width()
assert board.width() == board._board_width(), \
    f"单列窗口被旧布局顶大: {board.width()} vs {board._board_width()}"
assert abs(one_col_card - two_col_card) <= 2, \
    f"单列卡片宽度与多列不一致: {one_col_card} vs {two_col_card}"
print("15. 折叠宽度锚定 + 智能降级（点→聚合→宽条恢复）+ 单列宽度 OK")

# ---------- 16. 启动时是收起态 → 展开必须重建卡片 ----------
# 事故：上次退出时挂件收起，构造期只建了空网格；启动后主窗口在折叠态
# 推送设备清单时只更新状态点（不建卡），于是展开后停在「还没有添加设备」，
# 而折叠条上明明有一排状态点、管理窗里也勾着一堆设备。
board_store.set_columns(3)
board_store.save_devices([d.did for d in devs])
board_store.set_collapsed(True)          # 模拟上次退出时是收起态
startup = WidgetsBoard(MijiaService(), jobs, None)
startup.show()
app.processEvents()
assert startup.collapsed, "构造期应恢复为收起态"
assert not getattr(startup, "_cards", {}), "收起态构造期不应建卡"

startup.set_devices(devs, power)         # 折叠期间推送设备清单
app.processEvents()
startup._expand()                        # 用户点展开
app.processEvents()
assert len(startup._cards) == len(devs), \
    f"展开后未按最新清单重建卡片: {len(startup._cards)} 张（应为 {len(devs)}）"
assert not startup._empty.isVisible(), "有设备时展开却显示了『还没有添加设备』空态"
startup.hide()
board_store.set_collapsed(False)
print("16. 收起态启动 → 展开重建卡片 OK")

# ---------- 17. 场景与设备混排 + 点按钮执行 ----------
import time as _time


def pump(seconds: float, until=None) -> None:
    deadline = _time.time() + seconds
    while _time.time() < deadline:
        app.processEvents()
        if until is not None and until():
            return
        _time.sleep(0.01)


scene = {"scene_id": "s_home", "name": "回家模式", "home_id": "h1", "home_name": "家"}
board_store.save_scenes([scene])
board_store.save_devices([devs[0].did, devs[1].did])
board_store.save_order([
    {"type": "scene", "id": "s_home"},
    {"type": "device", "id": devs[0].did},
    {"type": "device", "id": devs[1].did},
])
board_store.set_columns(3)
board.set_columns(3)
board.set_devices(devs, power)
app.processEvents()
items = board._content_items()
assert [k for k, _ in items] == ["scene", "device", "device"], \
    f"混排顺序未按 order 生效: {[k for k, _ in items]}"
assert "s_home" in board._scene_cards, "场景卡未创建"
assert len(board._cards) == 2, f"设备卡数量不对: {len(board._cards)}"

# 点「执行」按钮 → 走 service.run_scene（用桩记录调用）
run_calls: list = []
board._service.run_scene = lambda info: run_calls.append(info)
board._scene_cards["s_home"]._run_btn.click()
pump(3, until=lambda: bool(run_calls))
assert run_calls and run_calls[0].scene_id == "s_home", f"场景未被调用: {run_calls}"
assert run_calls[0].home_id == "h1", "执行场景需要带上 home_id"
pump(3, until=lambda: board._scene_cards["s_home"]._run_btn.isEnabled())
assert board._scene_cards["s_home"]._run_btn.isEnabled(), "执行完成后按钮应恢复可点"
print("17. 场景与设备混排 + 执行 OK")

# ---------- 18. 锁定：隐藏标题栏、禁止拖动/收起、三条解锁路径 ----------
board_store.set_locked(False)
board.set_locked(False)
app.processEvents()
h_unlocked = board.height()
assert not board._handle.isHidden(), "未锁定时标题栏应可见"

# 标题栏上的「锁定」按钮：锁定入口不再藏在右键/托盘里
assert hasattr(board, "_lock_toggle"), "标题栏缺少锁定按钮"
assert not board._lock_toggle.isHidden(), "未锁定时标题栏锁定按钮应可见"
board._lock_toggle.click()
app.processEvents()
assert board.is_locked() and board_store.get_locked(), "点标题栏锁定按钮未生效/未落盘"
assert board._handle.isHidden(), "锁定后标题栏应隐藏"
assert board.height() < h_unlocked, \
    f"锁定后应少掉标题栏高度: {board.height()} vs {h_unlocked}"

# 锁定态下点击折叠条不应展开（形态固定）
board._collapse()
app.processEvents()
assert board.collapsed
_send(board._bar, QEvent.Type.MouseButtonPress, QPointF(board.x() + 30, board.y() + 16))
_send(board._bar, QEvent.Type.MouseButtonRelease, QPointF(board.x() + 30, board.y() + 16))
app.processEvents()
assert board.collapsed, "锁定后点击折叠条不应展开"

# 解锁（浮锁/右键/托盘三条路最终都走 set_locked(False)）
board.set_locked(False)
app.processEvents()
assert not board_store.get_locked(), "解锁未落盘"
board._expand()
app.processEvents()
assert not board._handle.isHidden(), "解锁后标题栏应恢复"

# 管理窗里的锁定开关与主窗口预览接线
board_store.set_locked(False)
dlg5 = BoardManagerDialog(devs, None, scenes=[scene])
lock_events: list = []
dlg5.lock_preview.connect(lock_events.append)
dlg5._lock_check.setChecked(True)
assert board_store.get_locked() is True, "管理窗勾选锁定未落盘"
assert lock_events and lock_events[-1] is True, f"未发出锁定预览信号: {lock_events}"
dlg5.done(0)
app.processEvents()
board_store.set_locked(False)
print("18. 锁定（隐藏标题栏 / 禁止收起 / 解锁 / 管理窗接线）OK")

# ---------- 19. 面板上按住卡片拖动 = 排序 ----------
board_store.set_locked(False)
board_store.set_columns(1)          # 单列：上下拖，落点判定最直观
board.set_locked(False)
board.set_columns(1)
board_store.save_scenes([])
board_store.save_devices([d.did for d in devs[:3]])
board_store.save_order([{"type": "device", "id": d.did} for d in devs[:3]])
board.set_devices(devs, power)
board._expand()
app.processEvents()

def _card_center_global(card):
    return card.mapToGlobal(card.rect().center())

a = board._cards[devs[0].did]
b = board._cards[devs[1].did]
start = _card_center_global(a)
end = _card_center_global(b)

# 单击（无位移）不能被排序逻辑吃掉：按下与松手都必须不消费，
# 事件继续交给卡片自身（鼠标松手会打开设备详情）
assert _send(a, QEvent.Type.MouseButtonPress, start) is False, "press not swallowed"
assert _send(a, QEvent.Type.MouseButtonRelease, start,
             Qt.MouseButton.NoButton) is False, "release without move not swallowed"

# 按下 → 移动越过阈值 → 松手落在 b 上
_send(a, QEvent.Type.MouseButtonPress, start)
_send(a, QEvent.Type.MouseMove, start + QPoint(0, 12))
# 拖动中必须有可见反馈：源卡片被拿起、跟随光标的浮动标签出现、落点被高亮
assert a.graphicsEffect() is not None, "拖动时源卡片应压暗（拿起效果）"
ghost = getattr(board, "_drag_ghost", None)
assert ghost is not None and ghost.isVisible(), "拖动时应出现跟随光标的浮动标签"
_send(a, QEvent.Type.MouseMove, end)
app.processEvents()
assert board._drop_target_key == ("device", devs[1].did), \
    f"落点卡片未高亮: {getattr(board, '_drop_target_key', None)}"
_send(a, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)
app.processEvents()
assert not ghost.isVisible(), "松手后浮动标签应消失"
assert a.graphicsEffect() is None, "松手后源卡片应恢复不透明"

order_after = [e["id"] for e in board_store.load_order()]
assert order_after[0] == devs[1].did and order_after[1] == devs[0].did, \
    f"拖拽未调整顺序: {order_after}"
assert board._content_items()[0][1].did == devs[1].did, "重建后顺序未生效"

# 锁定状态下不允许拖拽排序（锁定=冻结布局）
board.set_locked(True)
app.processEvents()
c = board._cards[devs[0].did]
_send(c, QEvent.Type.MouseButtonPress, _card_center_global(c))
_send(c, QEvent.Type.MouseMove, _card_center_global(c) + QPoint(0, 14))
_send(c, QEvent.Type.MouseButtonRelease, _card_center_global(c) + QPoint(0, 14),
      Qt.MouseButton.NoButton)
app.processEvents()
assert [e["id"] for e in board_store.load_order()][:2] == [devs[1].did, devs[0].did], \
    "锁定后不应还能拖动排序"
board.set_locked(False)
board_store.set_columns(3)
board.set_columns(3)
print("19. 面板卡片拖拽排序（锁定后禁用）OK")

# ---------- 20. 自绘悬浮提示（替代原生 tooltip）----------
from PySide6.QtGui import QHelpEvent

board.set_bg(None)          # 跟随主题，配色由调色板决定
board._rebuild()
app.processEvents()
lab = board._cards[devs[0].did]._name
assert lab.toolTip(), "设备名应带 tooltip 文案"
ev = QHelpEvent(QEvent.Type.ToolTip, QPoint(2, 2),
                lab.mapToGlobal(lab.rect().center()))
app.sendEvent(lab, ev)
app.processEvents()
hint = getattr(board, "_hint", None)
assert hint is not None and hint.isVisible(), "悬浮提示未显示（自绘提示失效）"
assert hint.text() == lab.toolTip(), f"提示文案不符: {hint.text()}"
# 用 render() 取真实绘制像素（grab() 对半透明顶层窗口不可靠，恒为 alpha=0）
from PySide6.QtGui import QImage

img = QImage(hint.size(), QImage.Format.Format_ARGB32)
img.fill(0)
hint.render(img)
bg = img.pixelColor(4, img.height() // 2)
assert bg.alpha() == 255, f"提示背景不透明（会变成黑框）: {bg.name()}/a{bg.alpha()}"
assert bg.name().upper() != "#000000", f"提示背景是纯黑: {bg.name()}"
board._hide_hint()
app.processEvents()
assert not hint.isVisible(), "提示应在收起后隐藏"
print("20. 自绘悬浮提示（不透明 + 主题配色）OK")

# ---------- 21. 「管理 / 显隐桌面挂件」入口只在开关开启时出现 ----------
board_store.set_enabled(False)
win._apply_board_enabled()
app.processEvents()
assert not win._board_manage_action.isVisible(), "开关关闭时主菜单仍显示管理入口"
assert not win._board_toggle_action.isVisible(), "开关关闭时主菜单仍显示显隐入口"
if win._tray is not None:
    assert not win._tray._board_manage_action.isVisible(), \
        "开关关闭时托盘菜单仍显示管理入口"
    assert not win._tray._board_toggle_action.isVisible(), \
        "开关关闭时托盘菜单仍显示显隐入口"
board_store.set_enabled(True)
win._apply_board_enabled()
app.processEvents()
assert win._board_manage_action.isVisible(), "开关开启后主菜单应显示管理入口"
assert win._board_toggle_action.isVisible(), "开关开启后主菜单应显示显隐入口"
assert win._board_toggle_action.text() == "隐藏桌面挂件", \
    f"挂件可见时主菜单文案应为隐藏: {win._board_toggle_action.text()}"
# 主菜单：旧的「桌面挂件」随手开关已删除（精确同名项不应存在）
more_texts = [a.text() for a in win._more_menu.actions() if a.text()]
assert "桌面挂件" not in more_texts, f"主菜单仍残留「桌面挂件」: {more_texts}"
assert "管理桌面挂件" in more_texts
assert "隐藏桌面挂件" in more_texts
# 刷新与设置独立成顶栏图标按钮，不再占用菜单项
assert "刷新" not in more_texts and "设置" not in more_texts, \
    f"刷新/设置应独立成图标按钮: {more_texts}"
assert hasattr(win, "_refresh_btn") and hasattr(win, "_settings_btn"), \
    "顶栏缺少刷新/设置图标按钮"
if win._tray is not None:
    tray_texts = [a.text() for a in win._tray._tray.contextMenu().actions()
                  if a.text()]
    assert "锁定桌面挂件" not in tray_texts, f"托盘菜单仍残留锁定项: {tray_texts}"
    assert "管理桌面挂件" in tray_texts
    toggle_text = win._board_toggle_action.text()
    assert toggle_text in tray_texts, f"托盘菜单缺少显隐项: {tray_texts}"
    assert (tray_texts.index("管理桌面挂件") < tray_texts.index(toggle_text)
            < tray_texts.index("定时任务")), \
        f"显隐入口应紧跟管理项、排在定时任务之前: {tray_texts}"

# 点托盘显隐项：挂件隐藏，两处文案同步变为「显示桌面挂件」
win._tray._toggle_board_visible()
app.processEvents()
assert not win._board.isVisible(), "点「隐藏桌面挂件」后挂件仍可见"
assert win._board_toggle_action.text() == "显示桌面挂件", \
    f"隐藏后主菜单文案未更新: {win._board_toggle_action.text()}"
win._refresh_board_menu_items()
assert win._tray._board_toggle_action.text() == "显示桌面挂件", \
    f"隐藏后托盘菜单文案未更新: {win._tray._board_toggle_action.text()}"
# 再点一次恢复显示，且不改动「桌面挂件」开关
win._tray._toggle_board_visible()
app.processEvents()
assert win._board.isVisible(), "点「显示桌面挂件」后挂件未恢复"
assert board_store.get_enabled() is True, "显隐切换不应改动挂件开关"
assert win._board_toggle_action.text() == "隐藏桌面挂件"
print("21. 挂件管理/显隐入口随开关显隐 + 文案切换 OK")

# ---------- 22. 落盘位置在屏幕外 → 拉回可见区 ----------
screen = QApplication.primaryScreen().availableGeometry()
board_store.set_geometry(screen.right() + 5000, screen.bottom() + 5000, 420, 300)
offscreen = WidgetsBoard(svc, jobs, None)
geo = offscreen.geometry()
assert screen.contains(geo.topLeft()), (
    f"屏幕外的落盘位置未拉回：{geo.getRect()} / 屏幕 {screen.getRect()}")
# 部分越界（右侧只剩几像素）也要保证可抓得住
board_store.set_geometry(screen.right() - 8, screen.top() + 20, 420, 300)
edge = WidgetsBoard(svc, jobs, None)
assert edge.geometry().x() <= screen.right() - 59, \
    f"右边缘越界未钳制: x={edge.geometry().x()} 屏右={screen.right()}"
offscreen.deleteLater()
edge.deleteLater()
app.processEvents()
print("22. 屏幕外几何拉回可见区 OK")

# ---------- 23. 轮询清单必须覆盖挂件设备（不受房间筛选影响）----------
win._all_devices = [
    DeviceInfo(did="roomA", name="客厅灯", model="a.b.c", home_name="家",
               room_name="客厅", online=True),
    DeviceInfo(did="roomB", name="卧室灯", model="a.b.c", home_name="家",
               room_name="卧室", online=True),
]
win._current_home = "家"
win._current_room = "客厅"
win._known_power = {"roomA": True, "roomB": True}
assert [d.did for d in win._visible_devices()] == ["roomA"]
board_store.save_devices(["roomB"])
if win._board is not None:
    win._board.set_devices(win._all_devices, win._known_power)
    polled = win._poll_dids()
    assert "roomB" in polled, (
        "挂件上的设备未纳入轮询：主界面切到别的房间后，"
        f"挂件状态再也不刷新（本轮轮询 {polled}）")
    assert "roomA" in polled, "可见设备仍须轮询"
win._current_room = _mw._ALL_ROOMS
print("23. 轮询覆盖挂件设备 OK")

# ---------- 24. 管理窗：设备/场景分页 + 行铺满 + 不提供排序 ----------
_scene_a = {"scene_id": "sa", "name": "回家", "home_id": "h", "home_name": "家"}
_scene_b = {"scene_id": "sb", "name": "离家", "home_id": "h", "home_name": "家"}
board_store.save_devices(["d0", "d1"])
board_store.save_scenes([_scene_a])
board_store.save_order([{"type": "device", "id": "d0"},
                        {"type": "scene", "id": "sa"},
                        {"type": "device", "id": "d1"}])
mgr = BoardManagerDialog(devs, None, scenes=[_scene_a, _scene_b])
mgr.show()
app.processEvents()
assert [b.text() for b in mgr._tab_buttons] == ["设备", "场景", "外观"], \
    f"页签不符: {[b.text() for b in mgr._tab_buttons]}"
# 设备和场景分开两页：设备页只有设备行，场景页只有场景行
assert "sa" in mgr._scene_keys and "sa" not in mgr._device_keys
assert all(not k.startswith("scene:") for k in mgr._device_keys)
# 行与滚动条之间留一小段间距（不再空一大条，也不贴死）
gap = mgr._scroll.viewport().width() - mgr._rows["d0"].width()
assert 4 <= gap <= 12, f"行与滚动条间距异常: {gap}px"
# 排序功能已下线（顺序在挂件面板上拖）
assert not hasattr(mgr, "_apply_drop") and not hasattr(mgr, "_hit_test"), \
    "管理窗仍残留拖拽排序实现"
# 但保存时必须保留面板上的混排顺序，不能按"设备在前场景在后"重写
order = mgr.ordered_items()
assert order == [{"type": "device", "id": "d0"},
                 {"type": "scene", "id": "sa"},
                 {"type": "device", "id": "d1"}], f"混排顺序被改写: {order}"
# 新勾选的设备追加在末尾，已有顺序不动
mgr._checks["d2"].setChecked(True)
order2 = mgr.ordered_items()
assert order2[:3] == order and order2[3] == {"type": "device", "id": "d2"}, \
    f"新勾选未追加到末尾: {order2}"
# 主按钮文字固定白色；勾选按钮跟着主题色走（不能再写死米家绿）
_saved_accent = si_theme.accent()
si_theme.set_accent("#39a6d8")
mgr.retheme()
_save_btn = [b for b in mgr.findChildren(QPushButton) if b.text() == "保存"][0]
assert "color: #ffffff" in _save_btn.styleSheet(), \
    f"保存按钮文字应为固定白色: {_save_btn.styleSheet()}"
_chk_qss = mgr._checks["d0"].styleSheet()
assert "#39a6d8" in _chk_qss, f"勾选按钮未跟随主题色: {_chk_qss}"
assert "#3dbba4" not in _chk_qss, "勾选按钮仍写死米家绿"
si_theme.set_accent(_saved_accent)
mgr.done(0)
app.processEvents()
print("24. 管理窗分页 + 行铺满 + 无排序 + 按钮跟随主题色 OK")

# ---------- 25. 托盘管理窗：顶部搜索框过滤 ----------
from app.ui.tray.manager_dialog import TrayManagerDialog

tm = TrayManagerDialog(devs, None, scenes=[])
# 右上角的 × 已去掉：底部「取消」承担关闭
assert not hasattr(tm, "_close_btn"), "托盘管理窗不应再有关闭按钮"
tm.show()
app.processEvents()
# 搜索提示词按页切换：设备页不出现"场景"，场景页不出现"设备"
assert "设备" in tm._search.placeholderText()
assert "场景" not in tm._search.placeholderText()
tm._switch_tab(1)
app.processEvents()
assert "场景" in tm._search.placeholderText(), tm._search.placeholderText()
assert "设备" not in tm._search.placeholderText(), tm._search.placeholderText()
tm._switch_tab(0)
app.processEvents()


def _visible_rows(dialog):
    out = []
    for i in range(dialog._list_lay.count()):
        item = dialog._list_lay.itemAt(i)
        w = item.widget() if item else None
        if w is not None and getattr(w, "_did", None) and w.isVisible():
            out.append(w._did)
    return out


assert tm._search.isVisible(), "托盘管理窗缺少搜索框"
total = len(_visible_rows(tm))
tm._search.setText("卧室")
app.processEvents()
filtered = _visible_rows(tm)
assert 0 < len(filtered) < total, f"搜索未过滤: {len(filtered)}/{total}"
row_map = {}
for _i in range(tm._list_lay.count()):
    _w = tm._list_lay.itemAt(_i).widget()
    if _w is not None and getattr(_w, "_did", None):
        row_map[_w._did] = _w
assert all("卧室" in row_map[d]._search_text for d in filtered), \
    "过滤结果里混入了不匹配的行"
tm._search.setText("不存在的设备")
app.processEvents()
assert _visible_rows(tm) == []
tm._search.setText("")
app.processEvents()
assert len(_visible_rows(tm)) == total, "清空搜索后应恢复全部行"
tm.reject()
app.processEvents()
print("25. 托盘管理搜索框 OK")

# ---------- 25b. 托盘管理窗房间筛选 ----------
tm2 = TrayManagerDialog(search_devs, None, scenes=[])
tm2.show()
app.processEvents()
tray_labels = [tm2._room_combo.itemText(i)
               for i in range(tm2._room_combo.count())]
assert tray_labels[0] == "全部房间", f"下拉首项应是全部房间: {tray_labels}"
assert set(tray_labels[1:]) == {"家 · 客厅", "家 · 卧室", "家 · 次卧"}, \
    f"托盘房间选项不符: {tray_labels}"

tm2._room_combo.setCurrentIndex(tray_labels.index("家 · 卧室"))
app.processEvents()
assert set(_visible_rows(tm2)) == {devs[1].did, devs[3].did, devs[5].did}, \
    f"托盘按房间筛选未生效: {_visible_rows(tm2)}"

# 筛选只是视图：被房间藏起来的已勾选设备不能从保存结果里消失
tm2._checks[devs[0].did].setChecked(True)
saved_dids = [it["id"] for it in tm2.selected_order() if it["type"] == "device"]
assert devs[0].did in saved_dids, "被房间筛掉的设备不该从保存结果里消失"
tm2._checks[devs[0].did].setChecked(False)

# 房间下拉只属于设备页：场景页与排序页都收起
tm2._switch_tab(1)
assert tm2._room_combo.isHidden(), "场景页应收起房间下拉"
tm2._switch_tab(0)
assert not tm2._room_combo.isHidden(), "回到设备页下拉应恢复"
tm2._switch_view("sort")
assert tm2._filter_row.isHidden(), "排序页不应显示搜索行"
tm2._switch_view("list")
tm2.reject()
app.processEvents()
print("25b. 托盘管理窗房间筛选（选项 / 筛选 / 保存不受影响 / 场景页收起）OK")

# ---------- 26. 单排挂件：底部不留空行、不出滚动条 ----------
board_store.set_locked(True)
board_store.set_columns(3)
board_store.save_devices(["d0", "d1", "d2"])
one_row = WidgetsBoard(svc, jobs, None)
one_row.set_devices(devs[:3], {d.did: True for d in devs[:3]})
one_row.show()
app.processEvents()
bottom_gap = one_row.height() - one_row._scroll.geometry().bottom() - 1
assert bottom_gap <= 8, f"单排时底部空出一大条: {bottom_gap}px"
assert not one_row._scroll.verticalScrollBar().isVisible(), \
    "单排时不应出现垂直滚动条"
one_row.hide()
one_row.deleteLater()
board_store.set_locked(False)
app.processEvents()
print("26. 单排无底部空行 OK")

# ---------- 27. 锁定隐藏标题栏后，窗口高度必须跟着收缩 ----------
# 曾经的 bug：隐藏标题栏后内外两层布局的最小高度还没重算，resize 被夹回
# 原高度，顶部省下的 22px 全跑到卡片下方 —— 看起来就是一条空行。
board_store.set_locked(False)
board_store.set_columns(3)
board_store.save_devices(["d0", "d1", "d2"])
locker = WidgetsBoard(svc, jobs, None)
locker.set_devices(devs[:3], {d.did: True for d in devs[:3]})
locker.show()
app.processEvents()
tall = locker.height()
locker.set_locked(True)
app.processEvents()
assert locker.height() < tall, f"锁定后高度未收缩: {locker.height()} vs {tall}"
assert locker.height() == locker._board_height(), \
    f"锁定后高度不等于公式值: {locker.height()} vs {locker._board_height()}"
lock_gap = locker.height() - locker._scroll.geometry().bottom() - 1
assert lock_gap <= 8, f"锁定后底部仍空一条: {lock_gap}px"
locker.set_locked(False)
app.processEvents()
assert locker.height() == tall, f"解锁后高度未还原: {locker.height()} vs {tall}"
locker.hide()
locker.deleteLater()
app.processEvents()
print("27. 锁定后高度自适应收缩 OK")

# ---------- 收尾：清运行数据 ----------
win._all_devices = []
win._force_quit = True
win.close()
jobs.shutdown()
board_store.save_devices([])
board_store.set_columns(3)
board_store.set_opacity(0.92)
board_store.set_collapsed(False)
board_store.set_enabled(False)
board_store.set_bg(None)
board_store.set_topmost(False)
si_theme.set_theme("dark")
print("BOARD REGRESSION ALL PASS")