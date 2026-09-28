# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""轻量通知浮层：右下角滑现、停留后淡出。

用于"刷新完成，新增 X 台"这类无需用户确认的结果反馈，不打断
当前操作；同一时刻只保留最新一条，新消息直接替换旧消息。
"""

import shiboken6
from PySide6.QtCore import QPropertyAnimation, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QWidget

from app.ui.si_theme import SiColors


class Toast(QFrame):
    _current: "Toast | None" = None

    @classmethod
    def info(cls, parent: QWidget, text: str, duration_ms: int = 4000) -> None:
        if cls._current is not None and shiboken6.isValid(cls._current):
            cls._current.deleteLater()
        toast = cls(parent, text)
        cls._current = toast
        toast._popup(duration_ms)

    def __init__(self, parent: QWidget, text: str):
        super().__init__(parent)
        self.setObjectName("toastCard")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        label = QLabel(text)
        label.setFont(QFont("Microsoft YaHei UI", 10))
        label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        lay.addWidget(label)

    def _popup(self, duration_ms: int) -> None:
        self.adjustSize()
        host = self.parentWidget()
        x = max(16, host.width() - self.width() - 20)
        y = max(16, host.height() - self.height() - 16)
        # 宿主右下角有语音悬浮球时通知上移，间距与输入框保持一致
        fab = getattr(host, "_voice_fab", None)
        if fab is not None and shiboken6.isValid(fab) and fab.isVisible():
            y -= fab.height() + 12 + 10
        self.move(x, y)
        self.show()
        self.raise_()
        QTimer.singleShot(duration_ms, self._fade_out)

    def _fade_out(self) -> None:
        if not shiboken6.isValid(self):
            return
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(400)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.deleteLater)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

