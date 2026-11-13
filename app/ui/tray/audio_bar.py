# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""托盘快捷窗口顶部的音箱控制栏。

构成：音箱选择（下拉）→ 上一首 / 播放暂停 / 下一首 → 音量条。
下拉只列**在线**音箱：离线音箱点了也控制不了，混在列表里只会让人困惑；
音响栏选的是**放歌的音箱**，与「语音指令发到哪台」是两个独立偏好。
"""

import shiboken6

from PySide6.QtCore import QSize, Qt, Signal
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
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.si_theme import PRIMARY_TEXT, SiColors, themed_combo


class _TrayAudioBar(QFrame):
    """托盘顶部的音箱控制栏：选音箱 + 上一首/静音/下一首 + 音量条。"""

    # 用户在下拉里换了要控制的音箱（由外层负责落盘偏好并刷新）
    speaker_changed = Signal(str)

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
        # 下拉内容签名：设备列表每 5 秒推一次，内容没变就不重建（否则会闪）
        self._speaker_sig: tuple = ()
        self.setObjectName("audioBar")
        # 上用 SURFACE、下用 WINDOW_BG 的极轻渐变：整块从"贴纸"变成一块
        # 有厚度的卡片，比纯平底色耐看（用户反馈"太扁平"）
        self.setStyleSheet(
            "QFrame#audioBar {"
            " background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {SiColors.CTRL_BG}, stop:1 {SiColors.WINDOW_BG});"
            f" border: 1px solid {SiColors.LINE}; border-radius: 14px; }}")
        self.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        lay.addLayout(self._build_head())
        lay.addLayout(self._build_transport())
        lay.addLayout(self._build_volume())

        self._volume: int | None = None
        self._pending_volume: int | None = None

    def sizeHint(self) -> QSize:  # noqa: N802
        """补上 QSS 圆角边框的 2px。

        边框是样式表画的，不算进 QFrame 的 frameWidth，布局按 sizeHint
        给的可用高度就少 2px；顶部那颗固定尺寸的播放圆钮会被上边框裁掉
        一条（用户截图反馈"顶被裁切"）。
        """
        hint = super().sizeHint()
        return QSize(hint.width(), hint.height() + 2)

    # ---------- 界面搭建 ----------

    def _build_head(self) -> QHBoxLayout:
        """顶部：音箱图标 + 下拉选择（取代原来写死的「小爱音响控制」标题）。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        icon = QLabel()
        # 必须固定尺寸：带 pixmap 的 QLabel 在高 DPI 下 sizeHint 会算成 48px，
        # 图标和下拉之间就空出一大块
        icon.setFixedSize(16, 16)
        icon.setScaledContents(True)
        icon.setPixmap(qta.icon("mdi.speaker-wireless",
                                color=SiColors.THEME).pixmap(16, 16))
        icon.setStyleSheet("background: transparent;")
        row.addWidget(icon)

        self._speaker_combo = themed_combo([], "")
        # themed_combo 给设置页的窄下拉定了 112px，这里要让音箱名放得下；
        # 上限 240：再宽就成了"输入框"，不像选择器了
        self._speaker_combo.setMinimumWidth(150)
        self._speaker_combo.setMaximumWidth(240)
        self._speaker_combo.setFixedHeight(30)
        self._speaker_combo.setToolTip("选择要控制的音箱")
        self._speaker_combo.currentIndexChanged.connect(self._on_combo_changed)
        row.addWidget(self._speaker_combo, stretch=1)
        return row

    def _build_transport(self) -> QHBoxLayout:
        """中间：上一首 / 播放暂停（强调色圆形）/ 下一首。"""
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addStretch(1)

        self._btn_prev = self._make_transport_button(
            "mdi.skip-previous", "上一首", self._on_prev, accent=False)
        row.addWidget(self._btn_prev)

        self._btn_play = self._make_transport_button(
            "mdi.play", "播放", self._on_toggle_mute, accent=True)
        row.addWidget(self._btn_play)

        self._btn_next = self._make_transport_button(
            "mdi.skip-next", "下一首", self._on_next, accent=False)
        row.addWidget(self._btn_next)

        row.addStretch(1)
        return row

    def _make_transport_button(self, icon: str, tip: str, slot,
                               *, accent: bool) -> QPushButton:
        """播放键用强调色实心圆（主操作），前后切歌是弱化的图标按钮。"""
        btn = QPushButton()
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tip)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if accent:
            # 36px：再大就把顶部圆圈顶到边框上（用户反馈"顶被裁切"），
            # 只比切歌键(34)大一圈，靠实心色区分主次
            size, icon_size = 36, 20
            btn.setStyleSheet(
                f"QPushButton {{ background: {SiColors.THEME}; border: none;"
                f" border-radius: {size // 2}px; }}"
                f"QPushButton:hover {{ background: {SiColors.THEME_HOVER}; }}"
                f"QPushButton:pressed {{ background: {SiColors.THEME_DIM}; }}"
                f"QPushButton:disabled {{ background: {SiColors.THEME_DIM}; }}")
        else:
            size, icon_size = 34, 24
            btn.setStyleSheet(
                "QPushButton { background: transparent; border: none;"
                f" border-radius: {size // 2}px; }}"
                f"QPushButton:hover {{ background: {SiColors.SURFACE}; }}"
                f"QPushButton:pressed {{ background: {SiColors.BTN_PRESSED}; }}")
        btn.setFixedSize(size, size)
        btn.setIconSize(QSize(icon_size, icon_size))
        # 强调色实心圆上的图标与主按钮文字同一套：固定白色
        btn.setIcon(qta.icon(icon, color=PRIMARY_TEXT if accent else SiColors.TEXT_PRIMARY))
        btn._icon_name = icon          # 供主题切换/换图标时复用
        btn._accent = accent
        btn.clicked.connect(slot)
        return btn

    def _build_volume(self) -> QHBoxLayout:
        """底部：音量图标 + 渐变音量条 + 数值徽标。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self._vol_icon = QLabel()
        self._vol_icon.setFixedSize(16, 16)
        self._vol_icon.setStyleSheet("background: transparent;")
        self._refresh_vol_icon(None)
        row.addWidget(self._vol_icon)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 100)
        self._slider.setFixedHeight(22)
        self._slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.setStyleSheet(
            # 已播放部分用主题绿渐变（左深右亮），未播放部分是低对比轨道；
            # 拇指是白芯 + 绿色描边，深色底上边缘清楚，不像纯白圆那样突兀
            f"QSlider::groove:horizontal {{ height: 6px; border-radius: 3px;"
            f" background: {SiColors.SCROLLBAR}; }}"
            f"QSlider::sub-page:horizontal {{ border-radius: 3px;"
            " background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            f" stop:0 {SiColors.THEME_DIM}, stop:1 {SiColors.THEME}); }}"
            f"QSlider::add-page:horizontal {{ border-radius: 3px;"
            f" background: {SiColors.SCROLLBAR}; }}"
            # 拇指是主题色圆环 + 白色内芯：描边加粗到 4px，白芯收小到约
            # 7px（外径 15px），不然 12px 白芯占满拇指，看着像颗白球
            "QSlider::handle:horizontal { width: 12px; height: 12px;"
            f" margin: -5px 0; border-radius: 8px; background: {SiColors.THUMB};"
            f" border: 4px solid {SiColors.THEME}; }}"
            "QSlider::handle:horizontal:hover {"
            f" border-color: {SiColors.THEME_HOVER}; }}"
            "QSlider::handle:horizontal:disabled {"
            f" background: {SiColors.OFFLINE_SUB};"
            f" border-color: {SiColors.SURFACE}; }}")
        self._slider.valueChanged.connect(self._on_slider_preview)
        self._slider.sliderReleased.connect(self._on_slider_commit)
        row.addWidget(self._slider, stretch=1)

        self._vol_label = QLabel("—")
        self._vol_label.setFixedSize(38, 22)
        self._vol_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vol_label.setStyleSheet(
            f"QLabel {{ background: {SiColors.SURFACE_PRESSED};"
            f" border-radius: 6px; color: {SiColors.TEXT_PRIMARY};"
            " font-size: 9pt; font-weight: 600; }")
        row.addWidget(self._vol_label)
        return row

    # ---------- 对外：托盘刷新时调用 ----------

    def set_speakers(self, speakers: list[DeviceInfo]) -> None:
        """填充下拉（调用方只传在线音箱，顺序即展示顺序）。"""
        sig = tuple((d.did, f"{d.name} · {d.room_name}") for d in speakers)
        if sig == self._speaker_sig:
            return
        self._speaker_sig = sig
        self._speaker_combo.blockSignals(True)
        try:
            self._speaker_combo.clear()
            for did, label in sig:
                self._speaker_combo.addItem(label, did)
        finally:
            self._speaker_combo.blockSignals(False)

    def set_speaker(self, did: str | None) -> None:
        if not did:
            self._did = None
            self.hide()
            return
        self._sync_combo(did)
        if did == self._did:
            # 同一设备，仅刷新可见性
            self.setVisible(True)
            return
        self._did = did
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

    def _sync_combo(self, did: str) -> None:
        idx = self._speaker_combo.findData(did)
        if idx < 0 or idx == self._speaker_combo.currentIndex():
            return
        self._speaker_combo.blockSignals(True)
        try:
            self._speaker_combo.setCurrentIndex(idx)
        finally:
            self._speaker_combo.blockSignals(False)

    def _on_combo_changed(self, index: int) -> None:
        did = self._speaker_combo.itemData(index)
        if not did or did == self._did:
            return
        self.speaker_changed.emit(str(did))

    # ---------- 状态 ----------

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

        self._refresh_vol_icon(self._volume)
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

    def _refresh_vol_icon(self, vol: int | None) -> None:
        """音量图标随音量档位变化（静音时打叉），比固定喇叭更直观。"""
        if self._muted:
            name, color = "mdi.volume-off", SiColors.ICON_MUTED
        elif vol is None:
            name, color = "mdi.volume-medium", SiColors.ICON_MUTED
        elif vol <= 0:
            name, color = "mdi.volume-mute", SiColors.ICON_MUTED
        elif vol < 34:
            name, color = "mdi.volume-low", SiColors.TEXT_SECONDARY
        elif vol < 67:
            name, color = "mdi.volume-medium", SiColors.TEXT_SECONDARY
        else:
            name, color = "mdi.volume-high", SiColors.TEXT_SECONDARY
        self._vol_icon.setPixmap(qta.icon(name, color=color).pixmap(16, 16))

    def _refresh_play_style(self) -> None:
        # 按钮点击切换静音：已静音显示「播放」（点击恢复），未静音
        # 显示「暂停」（点击静音），tooltip 与图标指向同一动作。
        # 强调色圆底配白色图标（与主按钮白字同一套语汇）。
        color = PRIMARY_TEXT if self._btn_play._accent else SiColors.TEXT_PRIMARY
        if self._muted:
            self._btn_play.setIcon(qta.icon("mdi.play", color=color))
            self._btn_play.setToolTip("播放")
        else:
            self._btn_play.setIcon(qta.icon("mdi.pause", color=color))
            self._btn_play.setToolTip("暂停")

    # ---------- 交互 ----------

    def _on_slider_preview(self, v: int) -> None:
        self._vol_label.setText(str(v))
        self._refresh_vol_icon(v)

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
        self._refresh_vol_icon(self._volume)
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
