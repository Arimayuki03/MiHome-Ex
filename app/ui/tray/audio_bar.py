# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""托盘快捷窗口顶部的简易音箱控制栏。"""

import shiboken6

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

import qtawesome as qta

from app.core.jobs import JobExecutor
from app.core.service import MijiaService
from app.ui.si_theme import SiColors


class _TrayAudioBar(QFrame):
    """托盘顶部的简易音箱控制栏：上一首 / 静音(播放暂停) / 下一首 + 音量条。"""

    def __init__(self, service: MijiaService, jobs: JobExecutor, parent=None):
        super().__init__(parent)
        self._service = service
        self._jobs = jobs
        self._did: str | None = None
        self._muted: bool | None = None
        self._mute_name: str = "mute-2"
        self._vol_name: str = "volume"
        self._vol_min = 0
        self._vol_max = 100
        self.setObjectName("audioBar")
        self.setStyleSheet(
            f"QFrame#audioBar {{ background: {SiColors.WINDOW_BG}; border: 1px solid {SiColors.LINE}; border-radius: 14px; }}")
        self.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)

        # 标题：左上角加粗白字，与托盘设备一致
        tit = QHBoxLayout()
        tit_lab = QLabel("小爱音响控制")
        tit_lab.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        tit_lab.setStyleSheet(f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        tit.addWidget(tit_lab)
        tit.addStretch(1)
        lay.addLayout(tit)

        # 上排：⏮  ⏯(大)  ⏭  居中
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch(1)

        self._btn_prev = QPushButton()
        self._btn_prev.setIcon(qta.icon('mdi.skip-previous', color=SiColors.TEXT_PRIMARY))
        self._btn_prev.setIconSize(QSize(36, 36))
        self._btn_prev.setFixedSize(36, 36)
        self._btn_prev.setCursor(Qt.PointingHandCursor)
        self._btn_prev.setToolTip("上一首")
        self._btn_prev.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 8px; }"
            f"QPushButton:hover {{ background: {SiColors.SURFACE}; }}"
            f"QPushButton:pressed {{ background: {SiColors.BTN_PRESSED}; }}")
        self._btn_prev.clicked.connect(self._on_prev)
        btn_row.addWidget(self._btn_prev)

        self._btn_play = QPushButton()
        self._btn_play.setIcon(qta.icon('mdi.play', color=SiColors.TEXT_PRIMARY))
        self._btn_play.setIconSize(QSize(44, 44))
        self._btn_play.setFixedSize(44, 44)
        self._btn_play.setCursor(Qt.PointingHandCursor)
        self._btn_play.setToolTip("播放")
        self._btn_play.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 8px; }"
            f"QPushButton:hover {{ background: {SiColors.SURFACE}; }}"
            f"QPushButton:pressed {{ background: {SiColors.BTN_PRESSED}; }}")
        self._btn_play.clicked.connect(self._on_toggle_mute)
        btn_row.addWidget(self._btn_play)

        self._btn_next = QPushButton()
        self._btn_next.setIcon(qta.icon('mdi.skip-next', color=SiColors.TEXT_PRIMARY))
        self._btn_next.setIconSize(QSize(36, 36))
        self._btn_next.setFixedSize(36, 36)
        self._btn_next.setCursor(Qt.PointingHandCursor)
        self._btn_next.setToolTip("下一首")
        self._btn_next.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 8px; }"
            f"QPushButton:hover {{ background: {SiColors.SURFACE}; }}"
            f"QPushButton:pressed {{ background: {SiColors.BTN_PRESSED}; }}")
        self._btn_next.clicked.connect(self._on_next)
        btn_row.addWidget(self._btn_next)

        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        # 下排：音量条
        vol_row = QHBoxLayout()
        vol_row.setSpacing(8)
        vol_icon = QLabel()
        vol_icon.setPixmap(qta.icon('mdi.volume-high', color=f'{SiColors.ICON_MUTED}').pixmap(16, 16))
        vol_icon.setStyleSheet("background: transparent;")
        vol_row.addWidget(vol_icon)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 100)
        self._slider.setFixedHeight(20)
        self._slider.setCursor(Qt.PointingHandCursor)
        self._slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: {SiColors.SURFACE}; border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {SiColors.THEME}; border-radius: 2px; }}"
            f"QSlider::add-page:horizontal {{ background: {SiColors.SURFACE}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ width: 16px; height: 16px; background: {SiColors.THUMB}; border-radius: 8px; margin: -6px 0; }}"
            f"QSlider::handle:horizontal:disabled {{ background: {SiColors.OFFLINE_SUB}; }}")
        self._slider.valueChanged.connect(self._on_slider_preview)
        self._slider.sliderReleased.connect(self._on_slider_commit)
        vol_row.addWidget(self._slider, stretch=1)

        self._vol_label = QLabel("—")
        self._vol_label.setFixedWidth(32)
        self._vol_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._vol_label.setStyleSheet(f"color: {SiColors.TEXT_PRIMARY}; background: transparent; font-size: 9pt;")
        vol_row.addWidget(self._vol_label)

        lay.addLayout(vol_row)

        self._volume: int | None = None
        self._pending_volume: int | None = None

    # ---------- 对外：托盘刷新时调用 ----------

    def set_speaker(self, did: str | None) -> None:
        if did == self._did and did is not None:
            # 同一设备，仅刷新可见性
            self.setVisible(True)
            return
        self._did = did
        if not did:
            self.hide()
            return
        self.setVisible(True)
        self._set_enabled(False)
        self._vol_label.setText("…")
        self._slider.setEnabled(False)
        # 拉取音量范围与当前值（did 快照随响应回传：飞行中切到别的
        # 音箱时旧响应直接丢弃，不再把 A 台的音量渲染到 B 台上）
        self._jobs.submit(
            lambda d=did: self._fetch_state(d),
            on_success=lambda data, d=did: self._apply_state(data, d),
            on_error=lambda e, d=did: self._on_fetch_error(e, d),
        )

    def _fetch_state(self, did: str):
        # 后台线程：先拿 spec 范围，再批量读值（did 来自提交时快照）
        if not did:
            return None
        # 取 volume/mute 的范围与可读性；device_detail 会走 spec 缓存
        try:
            detail = self._service.device_detail(did)
        except Exception:
            # 拿不到 detail 仍尝试直接读值
            detail = None

        vol_range = (0, 100, 1)
        mute_name = "mute-2"
        vol_name = "volume"
        if detail is not None:
            # 不能在命中 mute-2 时 break：若它在 spec 里排在 volume
            # 前面，音量范围将永远读不到
            mute_2_found = False
            for p in detail.props:
                if p.name == "volume" and p.range:
                    vol_range = p.range
                    vol_name = p.name
                if p.name == "mute-2":
                    mute_2_found = True
            # 未命中 mute-2，找首个非 mute-4 的 mute（排除麦克风）
            if not mute_2_found:
                for p in detail.props:
                    if p.name.startswith("mute") and p.writable and p.name != "mute-4":
                        mute_name = p.name
                        break

        # 批量读当前值
        try:
            vals = self._service.read_props(did, [vol_name, mute_name])
        except Exception:
            vals = {vol_name: None, mute_name: None}

        return {
            "vol_range": vol_range,
            "vol_name": vol_name,
            "mute_name": mute_name,
            "vals": vals,
        }

    def _apply_state(self, data, did: str | None = None) -> None:
        if not shiboken6.isValid(self) or data is None:
            return
        # 飞行中已切到别的音箱：这份状态是旧设备的，丢弃
        if did is not None and did != self._did:
            return
        if self._did is None:
            return
        vol_range = data.get("vol_range", (0, 100, 1))
        self._vol_min, self._vol_max = int(vol_range[0]), int(vol_range[1])
        self._mute_name = data.get("mute_name", "mute-2")
        self._vol_name = data.get("vol_name", "volume")
        vals = data.get("vals", {})
        vol_name = self._vol_name
        mute_name = self._mute_name

        vol = vals.get(vol_name)
        mute = vals.get(mute_name)

        # 设滑块范围
        self._slider.blockSignals(True)
        try:
            self._slider.setRange(self._vol_min, self._vol_max)
            if isinstance(vol, (int, float)):
                self._slider.setValue(int(vol))
                self._vol_label.setText(str(int(vol)))
                self._volume = int(vol)
            else:
                self._vol_label.setText("—")
        finally:
            self._slider.blockSignals(False)

        if isinstance(mute, bool):
            self._muted = mute
        elif isinstance(mute, int):
            self._muted = bool(mute)
        else:
            self._muted = None

        self._refresh_play_style()
        self._set_enabled(True)

    def _on_fetch_error(self, err: Exception, did: str | None = None) -> None:
        if not shiboken6.isValid(self):
            return
        # 飞行中已切到别的音箱：旧设备的失败不该动当前栏位
        if did is not None and did != self._did:
            return
        self._vol_label.setText("—")
        self._set_enabled(self._did is not None)

    def _set_enabled(self, enabled: bool) -> None:
        self._btn_prev.setEnabled(enabled)
        self._btn_next.setEnabled(enabled)
        self._btn_play.setEnabled(enabled)
        self._slider.setEnabled(enabled)

    def _refresh_play_style(self) -> None:
        # 按钮点击切换静音：已静音显示「播放」（点击恢复），未静音
        # 显示「暂停」（点击静音），tooltip 与图标指向同一动作
        if self._muted:
            self._btn_play.setIcon(qta.icon('mdi.play', color=SiColors.TEXT_PRIMARY))
            self._btn_play.setToolTip("播放")
        else:
            self._btn_play.setIcon(qta.icon('mdi.pause', color=SiColors.TEXT_PRIMARY))
            self._btn_play.setToolTip("暂停")

    # ---------- 交互 ----------

    def _on_slider_preview(self, v: int) -> None:
        self._vol_label.setText(str(v))

    def _on_slider_commit(self) -> None:
        if not self._did:
            return
        v = int(self._slider.value())
        # 松手才下发，避免拖动连击；did 快照防飞行中切音箱后写错设备
        did = self._did
        self._pending_volume = v
        self._slider.setEnabled(False)
        self._jobs.submit(
            lambda v=v, d=did: self._service.write_prop(d, self._vol_name, v),
            on_success=lambda _, v=v, d=did: self._on_volume_written(v, d),
            on_error=lambda e, d=did: self._on_volume_error(e, d),
        )

    def _on_volume_written(self, v: int, did: str | None = None) -> None:
        if not shiboken6.isValid(self):
            return
        # 写入期间已切到别的音箱：本次结果与本台无关，仅当 did 匹配才生效
        if did is not None and did != self._did:
            return
        self._volume = v
        self._pending_volume = None
        self._slider.setEnabled(True)

    def _on_volume_error(self, err: Exception, did: str | None = None) -> None:
        if not shiboken6.isValid(self):
            return
        # 同上：飞行中切了音箱，旧一台的失败不该动当前滑块
        if did is not None and did != self._did:
            return
        self._slider.setEnabled(True)
        # 回滚为旧值
        if self._volume is not None:
            self._slider.blockSignals(True)
            try:
                self._slider.setValue(int(self._volume))
                self._vol_label.setText(str(int(self._volume)))
            finally:
                self._slider.blockSignals(False)
        from app.ui.toast import Toast
        try:
            Toast.info(self, f"音量设置失败：{err}", 2500)
        except Exception:
            pass

    def _on_toggle_mute(self) -> None:
        if not self._did:
            return
        # 若尚未读到状态，按切换为静音处理
        cur = self._muted
        new_val = not cur if isinstance(cur, bool) else True
        self._btn_play.setEnabled(False)
        did = self._did
        mn = self._mute_name
        self._jobs.submit(
            lambda did=did, mn=mn, nv=new_val: self._service.write_prop(did, mn, nv),
            on_success=lambda _, nv=new_val, d=did: self._on_mute_written(nv, d),
            on_error=lambda e, d=did: self._on_mute_error(e, d),
        )

    def _on_mute_written(self, new_val: bool, did: str | None = None) -> None:
        if not shiboken6.isValid(self):
            return
        if did is not None and did != self._did:
            return  # 飞行中已切到别的音箱
        self._muted = new_val
        self._refresh_play_style()
        self._btn_play.setEnabled(True)

    def _on_mute_error(self, err: Exception, did: str | None = None) -> None:
        if not shiboken6.isValid(self):
            return
        if did is not None and did != self._did:
            return
        self._btn_play.setEnabled(True)
        from app.ui.toast import Toast
        try:
            Toast.info(self, f"静音切换失败：{err}", 2500)
        except Exception:
            pass

    def _on_prev(self) -> None:
        if not self._did:
            return
        self._btn_prev.setEnabled(False)
        did = self._did
        self._jobs.submit(
            lambda did=did: self._service.run_action(did, "previous"),
            on_success=lambda _, b=self._btn_prev: self._on_action_ok(b),
            on_error=lambda e, b=self._btn_prev: self._on_action_error(e, b),
        )

    def _on_next(self) -> None:
        if not self._did:
            return
        self._btn_next.setEnabled(False)
        did = self._did
        self._jobs.submit(
            lambda did=did: self._service.run_action(did, "next"),
            on_success=lambda _, b=self._btn_next: self._on_action_ok(b),
            on_error=lambda e, b=self._btn_next: self._on_action_error(e, b),
        )

    def _on_action_ok(self, btn: QPushButton) -> None:
        if not shiboken6.isValid(self):
            return
        btn.setEnabled(True)

    def _on_action_error(self, err: Exception, btn: QPushButton) -> None:
        if not shiboken6.isValid(btn):
            return
        btn.setEnabled(True)
        from app.ui.toast import Toast
        try:
            Toast.info(self, f"操作失败：{err}", 2500)
        except Exception:
            pass




