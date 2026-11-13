# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""状态胶囊的明细面板：温湿度 / 灯 / 耗材分组列表。

设备类明细直接复用主页的 DeviceCard（图标 + 名称/副标题 + 电源钮），
可以在面板里就地开关；耗材不是开关设备，用同尺寸的自绘行卡片，
右侧展示电量圆环。
"""

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core import icon_store
from app.core.models import DeviceInfo
from app.ui.device_card import DeviceCard
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import SiColors, secondary_button_qss

# 两列设备卡片（200×2 + 间距 10）加左右边距，刚好不留大片空白
_PANEL_W = 460
_PANEL_H = 560
_ICON_SIZE = 36
_CARD_COLUMNS = 2


def _level_color(percent: float) -> str:
    """电量分档色：50 以上跟随主题绿，20-50 黄，20 以下红。

    黄/红用固定值而不是调色板里的 WARN 系列：后者是文字色，
    深色模式下偏暗、画成圆环不够醒目。
    """
    if percent >= 50:
        return SiColors.THEME
    if percent >= 20:
        return "#e0a43a"
    return "#e0574b"


class _RingBadge(QWidget):
    """电量环：灰底圆环 + 按电量分档着色的进度 + 中心百分比。"""

    def __init__(self, percent: float, parent=None):
        super().__init__(parent)
        self._percent = max(0.0, min(100.0, float(percent)))
        self.setFixedSize(46, 46)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(5, 5, self.width() - 10, self.height() - 10)

        base_pen = QPen(QColor(SiColors.LINE), 4)
        base_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(base_pen)
        painter.drawArc(rect, 0, 360 * 16)

        progress_pen = QPen(QColor(_level_color(self._percent)), 4)
        progress_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(progress_pen)
        painter.drawArc(rect, 90 * 16, -int(self._percent * 3.6 * 16))

        painter.setPen(QColor(SiColors.TEXT_PRIMARY))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                         f"{int(round(self._percent))}%")
        painter.end()


class StatusPanelDialog(OverlayDialog):
    """分组列表面板：设备卡片可直接开关，耗材条目展示电量环。"""

    power_toggled = Signal(str)  # did，主窗口据此真正下发开关
    open_requested = Signal(str)  # did

    def __init__(self, title: str, subtitle: str, groups, parent=None):
        super().__init__(parent)
        self._title_text = title
        self._subtitle_text = subtitle
        self._groups = groups
        self.setWindowTitle(title)
        # did -> 卡片：面板内点击开关后同步按钮状态
        self._cards: dict[str, DeviceCard] = {}

        panel = self._panel
        panel.setFixedSize(_PANEL_W, _PANEL_H)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        self._title = QLabel(title)
        self._title.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        lay.addWidget(self._title)

        self._subtitle = QLabel(subtitle)
        lay.addWidget(self._subtitle)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # viewport 不设透明会露出默认黑底：面板左上那块黑块就是它
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._host = QWidget()
        self._host.setObjectName("statusHost")
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(10)
        self._scroll.setWidget(self._host)
        lay.addWidget(self._scroll, stretch=1)
        self._build_rows()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._close_btn = QPushButton("关闭")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._close_btn)
        lay.addLayout(btn_row)

        self._apply_styles()

    # ---------- 内容 ----------

    def _clear_rows(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            layout = item.layout()
            if layout is not None:
                # 卡片网格是 QLayout 不是 QWidget：子项清掉后还要把布局本身
                # 释放，否则每次重建（主题切换）都会漏一层
                while layout.count():
                    sub = layout.takeAt(0)
                    sub_widget = sub.widget()
                    if sub_widget is not None:
                        sub_widget.deleteLater()
                layout.deleteLater()
        self._cards.clear()

    def _build_rows(self) -> None:
        self._clear_rows()
        for group_title, rows in self._groups:
            if group_title:
                label = QLabel(group_title)
                label.setStyleSheet(
                    f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
                    " font-size: 9pt; padding-top: 4px;")
                self._list.addWidget(label)
            if not rows:
                empty = QLabel("暂无数据")
                empty.setStyleSheet(
                    f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                    " font-size: 9pt;")
                self._list.addWidget(empty)
                continue
            device_rows = [r for r in rows if r.get("device") is not None]
            other_rows = [r for r in rows if r.get("device") is None]
            if device_rows:
                self._list.addLayout(self._make_card_grid(device_rows))
            for row in other_rows:
                self._list.addWidget(self._make_row(row))
        self._list.addStretch(1)

    def _make_card_grid(self, rows: list[dict]) -> QGridLayout:
        """设备卡片网格：与主页同款卡片，2 列排布。"""
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        for index, row in enumerate(rows):
            device: DeviceInfo = row["device"]
            card = DeviceCard(device)
            state = row.get("state")
            if state is not None:
                card.set_power_state(state)
            if row.get("metrics"):
                card.set_metrics(row["metrics"])
            path = row.get("icon")
            if path is not None and path.is_file():
                card.set_icon(path)
            card.power_clicked.connect(
                lambda did, c=card, s=state: self._toggle(c, did, s))
            card.open_requested.connect(self.open_requested.emit)
            self._cards[device.did] = card
            grid.addWidget(card, index // _CARD_COLUMNS, index % _CARD_COLUMNS)
        # 在卡片列之后补一列拉伸：卡片不足一行时网格整体靠左，
        # 而不是被布局均分到中间
        grid.setColumnStretch(_CARD_COLUMNS, 1)
        return grid

    def _toggle(self, card: DeviceCard, did: str, state) -> None:
        """面板内点开关：乐观翻转按钮，真实下发交给主窗口。"""
        next_state = not bool(state) if state is not None else None
        if next_state is not None:
            card.set_power_state(next_state)
        self.power_toggled.emit(did)

    def update_power(self, did: str, state: bool) -> None:
        """主窗口回填真实开关结果。"""
        card = self._cards.get(did)
        if card is not None:
            card.set_power_state(state)

    def _make_row(self, row: dict) -> QWidget:
        """耗材等非开关条目：图标 + 名称/副标题 + 右侧电量环。"""
        box = QFrame()
        box.setObjectName("statusRow")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 10, 14, 10)
        lay.setSpacing(12)

        icon = QLabel()
        icon.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        path = row.get("icon")
        if path is not None:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                icon.setPixmap(pixmap.scaled(
                    32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name = QLabel(row.get("name") or "")
        name.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 10pt; font-weight: 600;")
        text_col.addWidget(name)
        sub = QLabel(row.get("sub") or "")
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        text_col.addWidget(sub)
        lay.addLayout(text_col, stretch=1)

        if row.get("percent") is not None:
            lay.addWidget(_RingBadge(row["percent"]), 0,
                          Qt.AlignmentFlag.AlignVCenter)
        elif row.get("value"):
            value = QLabel(row["value"])
            value.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;"
                " font-size: 11pt; font-weight: 600;")
            lay.addWidget(value, 0, Qt.AlignmentFlag.AlignVCenter)
        return box

    def _apply_styles(self) -> None:
        # 与设置页/详情页同款面板底色，保持全应用视觉一致
        self._panel.setStyleSheet(
            f"QFrame#overlayPanel {{ background: {SiColors.WINDOW_BG};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 16px; }}")
        self._title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        self._close_btn.setStyleSheet(secondary_button_qss("8px 20px"))
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px;"
            " margin: 0 2px 0 0; }"
            f"QScrollBar::handle:vertical {{ background: {SiColors.SCROLLBAR};"
            " border-radius: 3px; min-height: 30px; }"
            f"QScrollBar::handle:vertical:hover {{ background: {SiColors.SCROLLBAR_HOVER}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            " { background: none; }")
        # 用显式选择器给 host 刷透明：裸声明和后面的规则直接拼在一起会
        # 解析失败，行卡片的底色就整条画不出来
        self._host.setStyleSheet(
            "QWidget#statusHost { background: transparent; }"
            "QFrame#statusRow {"
            f" background: {SiColors.CARD}; border: 1px solid {SiColors.LINE};"
            " border-radius: 12px; }")

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()
        self._build_rows()

    # ---------- 定位 ----------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 基类的淡入会被紧接着的 setGeometry 打断（窗口还没铺满就重排），
        # 这里先把透明度按回 0，等几何稳定后再统一淡入
        self.setWindowOpacity(0.0)
        QTimer.singleShot(0, self._fill_and_place)

    def _fill_and_place(self) -> None:
        # 延迟回调可能在面板已经关闭后才到，先确认底层对象还活着；
        # 已经在关闭流程里也不要再淡入，否则会和淡出抢透明度
        import shiboken6

        if not shiboken6.isValid(self) or self._closing:
            return
        self._fill_parent_window()
        self._place_overlay()
        self._fade_in()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        x = (self.width() - self._panel.width()) // 2
        y = (self.height() - self._panel.height()) // 2
        self._panel.move(x, y)
