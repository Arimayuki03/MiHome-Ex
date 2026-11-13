# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""全局自绘工具提示。

原生 QToolTip 在 Windows 上按系统深色主题取调色板，应用切浅色后仍是
黑底，而全局 QSS 的 QToolTip 规则对已创建的提示窗口时灵时不灵
（background-color 尤其不生效）。这里直接接管所有 ToolTip 事件，
用 QPainter 自绘提示窗，颜色实时取主题调色板，绕开系统配色与 QSS。
"""

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QCursor, QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.ui.si_theme import SiColors


class _TipWindow(QWidget):
    """自绘提示窗：圆角卡片 + 单行/多行文字，颜色随主题实时取。"""

    _PAD_X = 10
    _PAD_Y = 6
    _RADIUS = 6

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # 鼠标穿透：提示窗压在光标附近时不抢主窗口的 Enter/Leave
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFont(QFont("Microsoft YaHei UI", 9))
        self._text = ""

    def set_text(self, text: str) -> None:
        self._text = text
        # 首次显示前必须先 polish：全局 QSS 的字号要等样式应用后才生效，
        # 否则按构造时的字体算宽度，会把长文本裁掉一截
        self.ensurePolished()
        metrics = self.fontMetrics()
        lines = text.split("\n") or [""]
        width = max(metrics.horizontalAdvance(line) for line in lines)
        height = metrics.height() * len(lines) + 2
        # 固定尺寸而不是 resize：避免首次 show 先按旧 sizeHint 渲染一帧
        self.setFixedSize(width + self._PAD_X * 2, height + self._PAD_Y * 2)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(SiColors.LINE), 1))
        painter.setBrush(QColor(SiColors.CARD))
        painter.drawRoundedRect(rect, self._RADIUS, self._RADIUS)
        painter.setPen(QColor(SiColors.TEXT_PRIMARY))
        painter.drawText(
            self.rect().adjusted(self._PAD_X, self._PAD_Y, -self._PAD_X, -self._PAD_Y),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._text)
        painter.end()


class TooltipController(QObject):
    """接管 QEvent.ToolTip，用自绘提示窗替换原生 QToolTip。"""

    # 鼠标离开控件后提示保留的时间，避免快速划过时闪一下
    _AUTO_HIDE_MS = 6000

    def __init__(self, app):
        super().__init__(app)
        self._tip: _TipWindow | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        app.installEventFilter(self)

    # ---------- 事件 ----------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        etype = event.type()
        if etype == QEvent.Type.ToolTip:
            text = obj.toolTip() if hasattr(obj, "toolTip") else ""
            if text and not self._owns_tooltip(obj):
                self.show_text(text)
                return True
        elif etype == QEvent.Type.Leave:
            # 鼠标移出被悬停控件即收起；提示窗自身是穿透的，不会收到 Leave
            if obj is not self._tip:
                self.hide()
        elif etype in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel,
                       QEvent.Type.KeyPress):
            self.hide()
        return False

    @staticmethod
    def _owns_tooltip(obj) -> bool:
        """挂件的提示窗是自绘的，交给它自己处理，避免两套提示抢事件。"""
        try:
            from app.ui.widgets_board import WidgetsBoard

            window = obj.window() if hasattr(obj, "window") else None
            return isinstance(window, WidgetsBoard)
        except Exception:
            return False

    # ---------- 提示窗 ----------

    def show_text(self, text: str) -> None:
        if self._tip is None:
            self._tip = _TipWindow()
        self._tip.set_text(text)

        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = pos.x() + 14
            y = pos.y() + 20
            if x + self._tip.width() > geo.right():
                x = pos.x() - self._tip.width() - 10
            if y + self._tip.height() > geo.bottom():
                y = pos.y() - self._tip.height() - 12
            self._tip.move(max(geo.left(), x), max(geo.top(), y))
        else:
            self._tip.move(pos.x() + 14, pos.y() + 20)
        self._tip.show()
        self._tip.raise_()
        self._timer.start(self._AUTO_HIDE_MS)

    def hide(self) -> None:
        self._timer.stop()
        if self._tip is not None:
            self._tip.hide()
