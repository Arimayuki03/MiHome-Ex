# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""自绘复选框。

原生 QCheckBox 的 indicator 用 QSS 只能填色，选中后没有勾，看不出
"已勾选"；QSS 的 image 方案要落地图片文件、换主题色还得重新生成，
所以直接画。颜色在 paint 时从调色板取，主题切换自动跟随。
"""

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QCheckBox

from app.ui.si_theme import SiColors


class SiCheckBox(QCheckBox):
    """选中时主题色方块 + 白色对勾的复选框。"""

    _BOX = 15
    _GAP = 8

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # indicator 与文字都由 paintEvent 画，这里只保留透明背景
        self.setStyleSheet("QCheckBox { background: transparent; }")

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        return QSize(
            self._BOX + self._GAP + fm.horizontalAdvance(self.text()),
            max(22, fm.height() + 8))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        box = QRectF(0, (self.height() - self._BOX) / 2, self._BOX, self._BOX)
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(SiColors.THEME))
            painter.drawRoundedRect(box, 4, 4)
            # 对勾：两段折线，端点圆头，避免直角显得生硬
            pen = QPen(QColor(SiColors.ON_THEME_TEXT), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(box.x() + box.width() * 0.25,
                        box.y() + box.height() * 0.52)
            path.lineTo(box.x() + box.width() * 0.43,
                        box.y() + box.height() * 0.70)
            path.lineTo(box.x() + box.width() * 0.76,
                        box.y() + box.height() * 0.31)
            painter.drawPath(path)
        else:
            # 空框的边框比卡片描边亮一档：LINE 在卡片底上几乎看不见，
            # 未勾选时也要能一眼找到可点的位置
            border = SiColors.THEME if self.underMouse() else SiColors.TEXT_MUTED
            painter.setPen(QPen(QColor(border), 1))
            painter.setBrush(QColor(SiColors.SURFACE))
            painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

        painter.setPen(QColor(SiColors.TEXT_PRIMARY))
        text_rect = QRectF(self._BOX + self._GAP, 0,
                           self.width() - self._BOX - self._GAP, self.height())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self.text())
        # 不画键盘焦点框：勾选态本身已经足够说明当前选择，再套一圈
        # 虚线反而像选中了整行文字，与卡片式布局不搭
        painter.end()
