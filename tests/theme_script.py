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

# 配置写入临时目录：测试绝不能碰项目根下的真实配置
from tests._isolate import isolate

isolate()

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

# ---------- 1b. 自定义主题色：派生色 + QSS + 开关拇指 ----------
si_theme.set_accent("#3f8fd8")
assert si_theme.SiColors.THEME == "#3f8fd8"
assert si_theme.SiColors.THEME_HOVER != "#3f8fd8", "hover 未派生"
assert si_theme.SiColors.THEME_DIM != "#3f8fd8", "dim 未派生"
assert "#3f8fd8" in si_theme.build_qss(), "自定义主题色未进全局 QSS"
# 亮色底配深字、深色底配白字
si_theme.set_accent("#f0b429")
assert si_theme.SiColors.ON_THEME_TEXT == "#0b0b0e"
si_theme.set_accent("#1b2a5b")
assert si_theme.SiColors.ON_THEME_TEXT == "#ffffff", "深色主题色上应改用白字"
# 空/非法值回落到默认米家绿
si_theme.set_accent("not-a-color")
assert si_theme.SiColors.THEME == "#3dbba4"
si_theme.set_accent(None)

# 开关拇指：深浅两套主题都必须白色（深色下黑点像脏点，用户两次反馈）
si_theme.set_theme("light")
sw_light = si_theme.themed_switch()
assert sw_light.style_data.thumb_color_checked.name().lower() == "#ffffff", \
    f"浅色模式开关拇指应为白色: {sw_light.style_data.thumb_color_checked.name()}"
si_theme.set_theme("dark")
sw_dark = si_theme.themed_switch()
assert sw_dark.style_data.thumb_color_checked.name().lower() == "#ffffff", \
    f"深色模式开关拇指也应为白色: {sw_dark.style_data.thumb_color_checked.name()}"
print("1b. 自定义主题色派生 + 开关拇指恒白 OK")

# ---------- 1c. 蓝色主题色：强调色上的文字必须是白色 ----------
si_theme.set_accent("#39a6d8")
assert si_theme.SiColors.THEME == "#39a6d8"
assert si_theme.SiColors.ON_THEME_TEXT == "#ffffff", (
    "中蓝主题色上仍是近黑字：按钮/对勾会糊在蓝色里（用户截图反馈）")
# 主按钮：白字固定，底色跟随主题色（过亮时才压暗一档）
assert si_theme.PRIMARY_TEXT == "#ffffff"
assert si_theme.primary_bg() == "#39a6d8"
qss_primary = si_theme.primary_button_qss()
assert "#39a6d8" in qss_primary and "#ffffff" in qss_primary
si_theme.set_accent("#f0b429")   # 黄：白字会糊，底色自动压暗
assert si_theme.primary_bg() != "#f0b429", "极亮强调色下主按钮底色应压暗"
assert si_theme.PRIMARY_TEXT == "#ffffff", "主按钮文字应始终为白色"
# 米家绿仍然是深字（默认观感不变）
si_theme.set_accent(None)
assert si_theme.SiColors.ON_THEME_TEXT == "#0b0b0e"

# ---------- 1d. 调色板同步（Qt 自绘控件跟随主题） ----------
from PySide6.QtGui import QPalette
si_theme.set_theme("light")
lp = si_theme.build_palette()
assert lp.color(QPalette.ColorRole.WindowText).name() == "#17181c", "浅色调色板文字应为深色"
assert lp.color(QPalette.ColorRole.Base).name() == "#ffffff"
assert lp.color(QPalette.ColorRole.Window).name() == "#f2f3f5"
assert lp.color(QPalette.ColorGroup.Disabled,
                QPalette.ColorRole.ButtonText).name() == "#b4b6bc"
si_theme.set_theme("dark")
dp = si_theme.build_palette()
assert dp.color(QPalette.ColorRole.WindowText).name() == "#e8e8ea", "深色调色板文字应为浅色"
assert dp.color(QPalette.ColorRole.Window).name() == "#1e1f24"

# 次级按钮：浅色模式下必须是"白底 + 描边"，不能与灰面板同色（看不清）
si_theme.set_theme("light")
qss_secondary = si_theme.secondary_button_qss()
assert "background: #ffffff" in qss_secondary, "浅色次级按钮应为白底"
assert "border: 1px solid #d5d8df" in qss_secondary, "浅色次级按钮缺少描边"
_svc, _jobs = MijiaService(), JobExecutor()
from app.ui.workbench_panel import WorkbenchPanel
_wb = WorkbenchPanel(_svc, _jobs, None)
assert "background: #ffffff" in _wb._refresh_btn.styleSheet(), \
    "设备详情的「刷新/编辑功能」未用浅色可见的按钮底色"
assert "#17181c" in _wb._refresh_btn.styleSheet(), "头部按钮文字色未跟随主题"
_wb.deleteLater()
_jobs.shutdown()
si_theme.set_theme("dark")
print("1c. 强调色文字/主按钮白字 + 1d. 调色板与次级按钮随主题 OK")

# ---------- 1e. 详情页滑块：白色拇指在浅色卡片上必须能看见 ----------
from app.siui.components.slider_ import SiSlider
from app.ui.prop_widgets import _make_slider

_sl = _make_slider()
assert _sl.style_data.background_color.name().lower() == si_theme.SiColors.SCROLLBAR.lower(), \
    "未播放轨道用了 SURFACE，浅色白卡上整条滑轨会消失"
assert type(_sl)._drawThumbRect is not SiSlider._drawThumbRect, \
    "滑块拇指缺少描边实现（白色拇指会与白卡融为一体）"
print("1e. 滑块拇指描边 + 滑轨对比度 OK")

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

# ---------- 2b. 主题色设置：启动读取 / 预览不落盘 ----------
settings_store.set_theme_accent("#e27aa8")
assert settings_store.get_theme_accent() == "#e27aa8"
apply_theme("dark")
assert si_theme.SiColors.THEME == "#e27aa8", "应用主题时未读取保存的主题色"
# 设置页预览会直接传色值，不能写回设置
apply_theme("dark", "#3f8fd8")
assert si_theme.SiColors.THEME == "#3f8fd8"
assert settings_store.get_theme_accent() == "#e27aa8", "预览不应写回设置"
# 存空串 = 默认米家绿
settings_store.set_theme_accent("")
apply_theme("dark")
assert si_theme.SiColors.THEME == "#3dbba4"
print("2b. 主题色设置读取 / 预览不落盘 OK")

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
# 标题栏与内容区同色（无分割线），取窗口底色即可
assert bg.name().upper() == "#F2F3F5", f"浅色标题栏异常: {bg.name()}"
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
# 标题栏与内容区同色（无分割线），取窗口底色即可
assert bg.name().upper() == "#1E1F24", f"深色标题栏异常: {bg.name()}"
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
# 标题栏跟随窗口底色，不再单独上色
assert img.pixelColor(500, 18).name().upper() == "#F2F3F5"
print("4b. 切回浅色 OK")

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
# 主题色：保存按钮文案、预设色块、预览不落盘
assert dlg._done_btn.text() == "保存", f"设置页按钮文案应为「保存」: {dlg._done_btn.text()}"
assert len(dlg._accent_chips) == 4, f"主题色预设数量不符: {len(dlg._accent_chips)}"
assert dlg._custom_accent_chip.text() == "自定义"
dlg._accent_chips["#39a6d8"].click()
app.processEvents()
assert dlg._pending_accent == "#39a6d8", f"蓝色预设应为 #39A6D8: {dlg._pending_accent}"
assert si_theme.SiColors.THEME == "#39a6d8", "主题色预览未生效"
assert settings_store.get_theme_accent() == "", "预览主题色不应落盘"
# 开关轨道色必须即时跟随：设置页不重建，siui 开关是构造期快照色
from app.siui.components.button import SiSwitchRefactor
_switches = dlg.findChildren(SiSwitchRefactor)
assert _switches, "设置页未找到开关控件"
assert _switches[0].style_data.background_color_starting.name().lower() == "#39a6d8", \
    "换主题色后开关仍停留在旧颜色（要保存重开才变）"
# 取消 → 还原为打开前（store 里是 dark，无变化）
dlg._pending_mode = "light"
dlg.done(QDialog.DialogCode.Rejected)
app.processEvents()
assert settings_store.get_theme_mode() == "dark"
print("5. 设置下拉预览/取消还原 OK", flush=True)
assert settings_store.get_theme_accent() == "", "取消后主题色不应落盘"
assert si_theme.SiColors.THEME == "#3dbba4", "取消后主题色未还原为默认"
print("5. 设置下拉预览/取消还原 + 主题色 OK")

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
# 任务线程已改为 Python 守护线程：shutdown(wait>0) 在任务边界 join
win._jobs.shutdown(wait=0.5)
assert not win._jobs._thread.is_alive(), "主窗口任务线程未随 close 退出"
jobs.shutdown(wait=0.5)

# ---------- 8. 自绘工具提示：底色跟随主题 ----------
# 原生 QToolTip 在系统深色模式下始终黑底，QSS 覆盖不稳定，已改为自绘。
# 这里只验证绘制结果，不真的弹出顶层窗口：offscreen 平台对 Qt.ToolTip
# 窗口的支持不稳定，show() 会偶发段错误
from app.ui import si_theme as _si_theme
from app.ui.tooltip import _TipWindow

# 只切调色板状态，不走全局 setStyleSheet：此时后台线程仍在跑网络调用，
# 主线程重刷样式表会撞出偶发访问越界
_si_theme.set_theme("light")
tip = _TipWindow()
tip.set_text("设置")
img = tip.grab().toImage()
bg = img.pixelColor(tip.width() // 2, 3).name().upper()
assert bg == "#FFFFFF", f"浅色提示窗底色错误: {bg}"
_si_theme.set_theme("dark")
img = tip.grab().toImage()
bg = img.pixelColor(tip.width() // 2, 3).name().upper()
assert bg == "#2A2C32", f"深色提示窗底色错误: {bg}"
tip.deleteLater()
app.processEvents()
print("8. 自绘工具提示跟随主题 OK")

settings_store.set_theme_mode("system")
tray_store.save([])
print("THEME REGRESSION ALL PASS", flush=True)
_mark("done")
