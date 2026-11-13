# SPDX-License-Identifier: GPL-3.0-or-later
"""托盘音响控制栏回归测试。

覆盖：音箱下拉只列传入的在线音箱、程序设置不回发信号、用户切换发一次信号、
切歌/音量状态渲染（滑块范围、数值徽标、音量图标）、无音箱时整栏隐藏。

不联网：状态渲染直接调 `_apply_state`，服务层只做占位。
"""

import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from tests._isolate import isolate

isolate()

from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.core.jobs import JobExecutor
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.theme_service import apply_theme
from app.ui.tray.audio_bar import _TrayAudioBar

apply_theme("dark")

svc = MijiaService()
jobs = JobExecutor()

speakers = [
    DeviceInfo(did="s1", name="小爱音箱Pro", model="xiaomi.wifispeaker.lx06",
               home_name="家", room_name="卧室", online=True),
    DeviceInfo(did="s2", name="小爱触屏音箱", model="xiaomi.wifispeaker.lx06",
               home_name="家", room_name="客厅", online=True),
]

bar = _TrayAudioBar(svc, jobs, None)
bar.resize(320, bar.sizeHint().height())
events: list[str] = []
bar.speaker_changed.connect(events.append)

# ---------- 1. 下拉内容 ----------
bar.set_speakers(speakers)
assert bar._speaker_combo.count() == 2
assert bar._speaker_combo.itemText(0) == "小爱音箱Pro · 卧室"
assert bar._speaker_combo.itemData(0) == "s1"
print("1. 下拉列出传入的音箱 OK")

# ---------- 2. 程序切换不发信号，用户切换发一次 ----------
bar.set_speaker("s1")
app.processEvents()
assert bar._speaker_combo.currentData() == "s1", "程序设置未同步下拉选中项"
assert bar.isVisible(), "选定音箱后整栏应显示"
assert events == [], f"程序设置不应回发信号: {events}"

# 重复设置同一台：状态不重置（音量不闪回「…」）
bar._vol_label.setText("42")
bar.set_speaker("s1")
app.processEvents()
assert bar._vol_label.text() == "42", "同设备重复设置不应清空音量显示"

# 模拟用户在下拉里换成另一台
bar._speaker_combo.setCurrentIndex(1)
app.processEvents()
assert events == ["s2"], f"用户切换应只发一次信号: {events}"
print("2. 程序设置静默 / 用户切换发信号 OK")

# ---------- 3. 状态渲染：范围、徽标、音量图标 ----------
bar._apply_state({
    "vol_range": (0, 60, 1),
    "vol_name": "volume",
    "mute_name": "mute-2",
    "vals": {"volume": 42, "mute-2": False},
})
app.processEvents()
assert bar._slider.minimum() == 0 and bar._slider.maximum() == 60, \
    f"滑块范围未按 spec 设置: {bar._slider.minimum()}~{bar._slider.maximum()}"
assert bar._slider.value() == 42
assert bar._vol_label.text() == "42", f"数值徽标未更新: {bar._vol_label.text()}"
assert bar._btn_play.isEnabled() and bar._slider.isEnabled()
assert bar._btn_play.toolTip() == "暂停", "未静音时按钮应是「暂停」"

# 静音后按钮变「播放」，音量图标打叉
bar._apply_state({
    "vol_range": (0, 60, 1),
    "vol_name": "volume",
    "mute_name": "mute-2",
    "vals": {"volume": 0, "mute-2": True},
})
assert bar._btn_play.toolTip() == "播放", "静音后按钮应是「播放」"
icon = bar._vol_icon.pixmap()
assert icon is not None and not icon.isNull(), "音量图标缺失"
print("3. 音量范围 / 徽标 / 静音状态渲染 OK")

# ---------- 3b. 主控圆钮尺寸 + 音量拇指是「主题色环 + 小白芯」 ----------
assert bar._btn_play.width() == bar._btn_play.height() == 36, \
    f"播放键圆形尺寸不符: {bar._btn_play.width()}px"
assert bar._btn_play.width() > bar._btn_prev.width(), "播放键应比切歌键更突出"
# 边框是 QSS 画的，sizeHint 必须补上 2px，否则圆钮会被上边框裁掉
assert bar.sizeHint().height() >= bar._btn_play.height() + bar.layout().contentsMargins().top() \
    + bar.layout().contentsMargins().bottom(), "音响栏高度不足，播放键会被裁切"
_slider_qss = bar._slider.styleSheet()
assert "border: 4px solid" in _slider_qss, "音量拇指白芯未收小（描边未加粗）"
print("3b. 播放键尺寸 + 音量拇指白芯 OK")

# ---------- 4. 无音箱时隐藏 ----------
bar.set_speaker(None)
app.processEvents()
assert bar.isHidden(), "没有在线音箱时应整栏隐藏"
print("4. 无音箱隐藏 OK")

# ---------- 5. 音响栏的偏好与语音指令的音箱互相独立 ----------
from app.core import settings_store
from app.ui.tray.quick_window import TrayQuickWindow

settings_store.set_default_speaker_did("")
settings_store.set_tray_audio_speaker_did("")
qw = TrayQuickWindow(svc, jobs, None)
qw.set_devices(speakers, {})
assert qw._pick_audio_speaker() is not None
assert qw._pick_audio_speaker().did == "s1", "未设置时应落到第一台在线音箱"

# 语音指令选了 s2，音响栏没选过 → 跟随语音（合理默认）
settings_store.set_default_speaker_did("s2")
assert qw._pick_audio_speaker().did == "s2"

# 音响栏自己选 s1 → 与语音彻底分开
settings_store.set_tray_audio_speaker_did("s1")
assert qw._pick_audio_speaker().did == "s1"
assert settings_store.get_default_speaker_did() == "s2", \
    "音响栏的选择不应改写语音指令的音箱"

# 用户在音响栏切换：只写音响栏偏好
qw._on_audio_speaker_changed("s2")
assert settings_store.get_tray_audio_speaker_did() == "s2"
assert settings_store.get_default_speaker_did() == "s2"

# 所选音箱离线/被移除：只在本次回退，偏好保留（回来还能自动选中）
settings_store.set_tray_audio_speaker_did("s_gone")
settings_store.set_default_speaker_did("s2")
assert qw._pick_audio_speaker().did == "s2", "离线偏好应回退到语音默认音箱"
assert settings_store.get_tray_audio_speaker_did() == "s_gone", \
    "回退不应清掉音响栏偏好"
qw.deleteLater()
app.processEvents()
print("5. 音响栏偏好与语音音箱独立 OK")

jobs.shutdown()
print("AUDIO REGRESSION ALL PASS")
