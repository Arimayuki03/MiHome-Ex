# SPDX-License-Identifier: GPL-3.0-or-later
"""浅色/深色主题回归测试（离屏运行）。"""
import faulthandler
import os
import sys
from pathlib import Path

# CI 上该脚本曾以退出码 1 静默死掉（stdout 全缓冲丢失、stderr 无
# Traceback、仅 3.12 复现）：faulthandler 把原生崩溃栈直接写 stderr
# （行缓冲不丢），步骤标记也走 stderr 保证死点可见
faulthandler.enable()
def _mark(msg: str) -> None:
    print(f"[theme] {msg}", file=sys.stderr, flush=True)

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体，qtawesome 注册图标字体后唯一字体无 CJK 字形
# 会导致 drawText 中文零像素（详见 tests/cuktech_panel_test.py 顶部注释）
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/theme_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

# 数据文件全部重定向到临时目录（对齐 tray_span_test 的做法）：本脚本
# 用的是真实 settings_store/tray_store，不隔离会写脏用户数据——本地是
# 仓库根的 settings.json，打包版是 %LOCALAPPDATA%\MiHome-Ex。CI runner
# 的用户目录跨次运行共享，残留状态会让测试依赖环境。
import tempfile
_TMP_DATA = tempfile.mkdtemp(prefix="mihome-theme-test-")
_mark(f"data dir -> {_TMP_DATA}")

from app.core import _json_store as _json_store_mod
_json_store_mod.data_file = lambda name: Path(_TMP_DATA) / name
import app.core.cache as _device_cache  # noqa: E402
_device_cache.save = lambda *a, **k: None

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

_mark("creating QApplication")
app = QApplication.instance() or QApplication([])

from app.core import settings_store, tray_store
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.core.jobs import JobExecutor
from app.ui import si_theme
from app.ui.theme_service import apply_theme, effective_theme

# ---------- 1. 调色板与 QSS 生成 ----------
_mark("1. palette/qss")
assert si_theme.build_qss()  # dark 默认
si_theme.set_theme("light")
assert si_theme.SiColors.CARD == "#ffffff"
assert si_theme.SiColors.THEME == "#3dbba4"  # 主题色不随主题变化
assert "{SiColors" not in si_theme.build_qss()  # 占位符全部填充
si_theme.set_theme("dark")
assert si_theme.SiColors.CARD == "#2a2c32"
print("1. 调色板动态取值 + QSS 生成 OK", flush=True)

# ---------- 2. 跟随系统解析 ----------
_mark("2. settings roundtrip")
settings_store.set_theme_mode("system")
assert effective_theme("system") in ("dark", "light")
assert effective_theme("light") == "light"
assert effective_theme("dark") == "dark"
assert settings_store.get_theme_mode() == "system"
settings_store.set_theme_mode("light")
assert settings_store.get_theme_mode() == "light"
settings_store.set_theme_mode("dark")
print("2. 设置存取 + 系统主题解析 OK", flush=True)

# ---------- 3. 启动浅色主题 + 主窗口渲染 ----------
_mark("3. main window light")
tray_store.save([])
apply_theme("light")
svc = MijiaService()
jobs = JobExecutor()
devs = [DeviceInfo(did=str(i), name=f"设备{i}", model="a.b.c", home_name="家",
                   room_name="客厅", online=(i % 2 == 0)) for i in range(4)]
sys.path.insert(0, ".")
from app.ui.main_window import MainWindow

win = MainWindow()
win._apply_devices(devs)
win.show()
app.processEvents()

img = win.grab().toImage()
bg = img.pixelColor(500, 18)
assert bg.name().upper() == "#ECECEF", f"浅色标题栏异常: {bg.name()}"
card = next(iter(win._cards.values()))
cimg = card.grab().toImage()
cc = cimg.pixelColor(5, 5)
assert cc.name().upper() == "#FFFFFF", f"浅色卡片底异常: {cc.name()}"
print("3. 浅色主题主窗/卡片渲染 OK", flush=True)

# ---------- 4. 切换深色：卡片网格重建为新调色板 ----------
_mark("4. main window dark")
win.apply_theme_mode("dark")
app.processEvents()
assert si_theme.current_theme() == "dark"
img = win.grab().toImage()
bg = img.pixelColor(500, 18)
assert bg.name().upper() == "#1A1B20", f"深色标题栏异常: {bg.name()}"
card = next(iter(win._cards.values()))
cimg = card.grab().toImage()
cc = cimg.pixelColor(5, 5)
assert cc.name().upper() == "#2A2C32", f"深色卡片底异常: {cc.name()}"
print("4. 切换深色重建 OK", flush=True)

# 切回浅色
_mark("4b. back to light")
win.apply_theme_mode("light")
app.processEvents()
img = win.grab().toImage()
assert img.pixelColor(500, 18).name().upper() == "#ECECEF"
print("4b. 切回浅色 OK", flush=True)

# ---------- 5. 设置页：下拉预览 + 取消还原 ----------
_mark("5. settings dialog preview")
from app.ui.settings_dialog import SettingsDialog

dlg = SettingsDialog(win, devices=devs)
assert dlg._theme_combo.currentText() == "深色模式"  # win 当前是 dark? 否——上面切回了 light
# 修正：上面的 win 在 4b 切回了 light，但对话框读取的是 store（dark）。
# store 与实际预览可能不同步是正常的：对话框以 store 为初始值。
dlg._theme_combo.setCurrentText("浅色模式")
assert dlg._pending_mode == "light"
assert si_theme.current_theme() == "light"
dlg._theme_combo.setCurrentText("深色模式")
assert si_theme.current_theme() == "dark"
# 取消 → 还原为打开前（store 里是 dark，无变化）
dlg._pending_mode = "light"
dlg.done(QDialog.DialogCode.Rejected)
app.processEvents()
assert settings_store.get_theme_mode() == "dark"
print("5. 设置下拉预览/取消还原 OK", flush=True)

# ---------- 6. 旧回归：对话框淡出关闭 ----------
_mark("6. dialog fade-out close")
def run_dialog(dlg, name):
    escape = {"used": False}
    esc_timer = QTimer()
    esc_timer.setSingleShot(True)
    esc_timer.timeout.connect(lambda: (escape.__setitem__("used", True), QDialog.done(dlg, 0)))
    closer = QTimer()
    closer.setSingleShot(True)
    closer.timeout.connect(dlg.reject)
    esc_timer.start(2500)
    closer.start(300)
    dlg.exec()
    esc_timer.stop()
    assert not dlg.isVisible() and not escape["used"], name

run_dialog(SettingsDialog(win, devices=devs), "SettingsDialog")
_mark("6a. SettingsDialog closed")
from app.ui.tray import TrayManagerDialog
run_dialog(TrayManagerDialog(devs), "TrayManagerDialog")
_mark("6b. TrayManagerDialog closed")
from app.ui.device_dialog import DeviceDetailDialog
run_dialog(DeviceDetailDialog(svc, jobs, devs[0], win), "DeviceDetailDialog")
_mark("6c. DeviceDetailDialog closed")
from app.ui.add_drawer import AddDrawer
a = AddDrawer(win)
a.set_modules([("on", "开关", "开")], set())
run_dialog(a, "AddDrawer")
_mark("6d. AddDrawer closed")
print("6. 对话框淡出关闭回归 OK", flush=True)

# ---------- 7. 托盘整窗重建（走 _on_theme_changed 真实路径） ----------
_mark("7. tray rebuild")
if win._tray is not None:
    win._tray.set_devices(devs, {})
    win._tray._quick.show_near_tray()
    app.processEvents()
    assert win._tray._quick.isVisible()
    old_quick = win._tray._quick
    win._on_theme_changed(si_theme.current_theme())
    app.processEvents()
    assert win._tray._quick is not old_quick, "retheme 未重建快捷窗口"
    assert win._tray._quick.isVisible(), "重建后未自动恢复显示"
    # 新窗口面板底色取当前调色板
    img = win._tray._quick._root.grab().toImage()
    assert img.pixelColor(150, img.height() // 3).name().upper() in (
        "#1E1F24", "#F2F3F5", "#2A2C32", "#FFFFFF")
    win._tray.hide_quick()
    app.processEvents()
print("7. 托盘整窗重建 + 自动恢复显示 OK", flush=True)

_mark("8. teardown")
win._all_devices = []  # 关闭路径不保存假设备
# 测试环境的 settings_store 残留可能是「最小化到托盘」（且托盘创建成功），
# win.close() 会走隐藏分支跳过 _jobs.shutdown()，其内部 QThread 活到
# 解释器关闭即报 "QThread: Destroyed while thread is still running"。
# 置 force_quit 走真实退出路径（对齐托盘菜单「退出」），closeEvent 内
# 会自行 shutdown 并 quit。
win._force_quit = True
win.close()
assert not win._jobs._thread.isRunning(), "主窗口任务线程未随 close 退出"
jobs.shutdown()
settings_store.set_theme_mode("system")
tray_store.save([])
print("THEME REGRESSION ALL PASS", flush=True)
_mark("done")
