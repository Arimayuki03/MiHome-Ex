# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""顶部标签排序对话框。

自己实现拖拽：QListWidget 的内部移动依赖数据模型，落点与移除时机都
不受控，出现过"拖完条目消失"。这里改用行级控件 + QDrag，顺序由对话框
自己维护，拖拽只负责告诉它起点和落点。
"""

import qtawesome as qta

from PySide6.QtCore import QEvent, QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QDrag
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core import settings_store
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import (
    SiColors,
    primary_button_qss,
    secondary_button_qss,
)

_PANEL_W = 360
_MAX_VISIBLE_ROWS = 10
_ROW_HEIGHT = 40
# 未设置默认标签时程序打开「全屋」，界面标记也以它为准
_ALL_ROOMS = "全屋"


class _RoomRow(QFrame):
    """一行标签：左侧拖拽手柄，整行可作为拖拽源。"""

    dragStarted = Signal(str)  # 房间名
    dragFinished = Signal()  # noqa: N815
    setDefault = Signal(str)  # noqa: N815 房间名

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setObjectName("roomRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(_ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(10)

        self._handle = QLabel()
        self._handle.setFixedSize(16, 16)
        self._handle.setPixmap(
            qta.icon("mdi.drag-horizontal-variant",
                     color=SiColors.TEXT_MUTED).pixmap(14, 14))
        lay.addWidget(self._handle)

        self._label = QLabel(name)
        lay.addWidget(self._label, stretch=1)

        self._default_btn = QPushButton("设为默认")
        self._default_btn.setFixedHeight(22)
        self._default_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._default_btn.clicked.connect(lambda: self.setDefault.emit(name))
        lay.addWidget(self._default_btn)

        self._drag_start: QPoint | None = None
        self._dragging = False

    def refresh_style(self, highlight: bool = False, dragging: bool = False) -> None:
        """高亮 = 拖拽落点；dragging = 自己正被拖动（淡化作占位）。

        被拖动时不能用 setEnabled(False)：禁用控件会停止接收鼠标事件，
        拖拽状态机会被打断，表现为松手后什么都不发生。这里改透明度。
        """
        if highlight:
            bg, border = SiColors.THEME, SiColors.THEME
            text = SiColors.ON_THEME_TEXT
        else:
            bg, border = SiColors.CARD, SiColors.LINE
            text = SiColors.TEXT_PRIMARY
        self.setStyleSheet(
            f"QFrame#roomRow {{ background: {bg}; border: 1px solid {border};"
            f" border-radius: 8px; }}")
        self._label.setStyleSheet(
            f"color: {text}; background: transparent; font-size: 10pt;")
        if dragging:
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(0.35)
            self.setGraphicsEffect(effect)
        else:
            self.setGraphicsEffect(None)

    def set_is_default(self, flag: bool) -> None:
        """当前默认项：按钮变成不可点的「默认」标记。"""
        self._default_btn.setText("默认" if flag else "设为默认")
        self._default_btn.setEnabled(not flag)
        # 未选中时用 LINE 描边而不是 CTRL_BORDER：后者与卡片底色几乎
        # 同值，按钮在深色主题下会"消失"成一个纯文字标签
        self._default_btn.setStyleSheet(
            f"QPushButton {{ background: {SiColors.THEME if flag else SiColors.SURFACE};"
            f" border: 1px solid {SiColors.THEME if flag else SiColors.LINE};"
            " border-radius: 6px; padding: 0 10px; font-size: 8.5pt;"
            f" color: {SiColors.ON_THEME_TEXT if flag else SiColors.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ background: {SiColors.CTRL_BG_HOVER};"
            f" border-color: {SiColors.THEME};"
            f" color: {SiColors.TEXT_PRIMARY}; }}")

    # ---------- 拖拽 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_start is None:
            return
        if not self._dragging:
            moved = (event.pos() - self._drag_start).manhattanLength()
            if moved < 10:
                return
            self._dragging = True
            self.dragStarted.emit(self.name)
            drag = QDrag(self)
            mime = QMimeData()
            mime.setText(self.name)
            drag.setMimeData(mime)
            pixmap = self.grab()
            drag.setPixmap(pixmap)
            drag.setHotSpot(event.pos())
            drag.exec(Qt.DropAction.MoveAction)
            self._dragging = False
            self._drag_start = None
            self.dragFinished.emit()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_start = None
        self._dragging = False
        super().mouseReleaseEvent(event)


class RoomOrderDialog(OverlayDialog):
    """返回排序后的标签列表；取消时 result 为 Rejected。"""

    def __init__(self, rooms: list[str], defaults: list[str] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("调整标签顺序")
        # defaults 是「没有自定义顺序」时的自然排法，恢复默认按它来
        self._defaults = list(defaults) if defaults else sorted(rooms)
        self._restored = False
        self._names = list(rooms)
        self._rows: dict[str, _RoomRow] = {}
        self._drag_name: str | None = None
        # 打开时的默认标签；点「设为默认」后改这里，确定时再落盘。
        # 没设置过时程序实际打开「全屋」，界面上也要把它标成默认，
        # 否则用户会以为「一个都没选」。
        self._saved_default = settings_store.get_default_tab()
        self._default_tab = self._saved_default or _ALL_ROOMS
        # 是否点过「设为默认」：区分"没碰过"与"主动选了全屋"
        self._user_picked = False

        panel = self._panel
        panel.setFixedWidth(_PANEL_W)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)

        self._title = QLabel("调整标签顺序")
        lay.addWidget(self._title)

        self._hint = QLabel("按住拖动调整先后，确定后立即生效。")
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        # 行级拖拽需要容器自己接收拖放事件，故选 QScrollArea 而不是
        # QListWidget：后者内部移动依赖模型，落点行为不受控
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.viewport().setAutoFillBackground(False)
        # 只让容器接收拖放，并装事件过滤器集中处理：行控件本身不接收，
        # 事件会冒泡到容器，避免"鼠标停在行上时拖拽被判无效"
        self.setAcceptDrops(True)
        visible = min(_MAX_VISIBLE_ROWS, max(1, len(rooms)))
        # 高度 = 容器上下边距 + 行高 + 行间距（每行之间 6px）
        self._scroll.setFixedHeight(
            12 + 12 + _ROW_HEIGHT * visible + 6 * (visible - 1))
        self._host = QWidget()
        self._host.setObjectName("roomHost")
        self._host.setAcceptDrops(True)
        self._host.installEventFilter(self)
        self._host_lay = QVBoxLayout(self._host)
        self._host_lay.setContentsMargins(6, 6, 6, 6)
        self._host_lay.setSpacing(6)
        self._scroll.setWidget(self._host)
        lay.addWidget(self._scroll)
        self._rebuild_rows()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._reset_btn = QPushButton("恢复默认")
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(self._reset_btn)
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)
        self._ok_btn = QPushButton("确定")
        self._ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._ok_btn)
        lay.addLayout(btn_row)

        self._apply_styles()
        panel.adjustSize()

    # ---------- 行管理 ----------

    def _rebuild_rows(self) -> None:
        while self._host_lay.count():
            item = self._host_lay.takeAt(0)
            if widget := item.widget():
                widget.hide()
                widget.deleteLater()
        self._rows.clear()
        for name in self._names:
            row = _RoomRow(name, self._host)
            row.dragStarted.connect(self._on_drag_started)
            row.dragFinished.connect(self._on_drag_finished)
            row.setDefault.connect(self._on_set_default)
            row.set_is_default(name == self._default_tab)
            # 行本身不接收拖放：让它冒泡到容器，由事件过滤器统一处理
            row.setAcceptDrops(False)
            row.installEventFilter(self)
            row.refresh_style()
            self._host_lay.addWidget(row)
            self._rows[name] = row
        self._host_lay.addStretch(1)

    # ---------- 拖拽 ----------

    def _on_drag_started(self, name: str) -> None:
        self._drag_name = name
        row = self._rows.get(name)
        if row is not None:
            row.refresh_style(dragging=True)

    def _on_drag_finished(self) -> None:
        self._drag_name = None
        self._reset_styles()

    def _on_set_default(self, name: str) -> None:
        """把某个标签设为启动默认，并刷新所有行的标记。"""
        self._user_picked = True
        self._default_tab = name
        for row_name, row in self._rows.items():
            row.set_is_default(row_name == name)

    def _reset_styles(self) -> None:
        for row in self._rows.values():
            row.refresh_style()

    def _hit_insert(self, pos: QPoint) -> tuple[str | None, bool]:
        """落点：返回 (目标名, 是否插到其后)；None 表示末尾。

        按行在容器里的实际 y 排序，不依赖字典顺序——拖拽后布局顺序
        与记录顺序会不同步。
        """
        rows = sorted(self._rows.items(), key=lambda kv: kv[1].y())
        for name, row in rows:
            geo = row.geometry()
            if pos.y() < geo.top():
                return name, False
            if geo.top() <= pos.y() < geo.bottom():
                return name, pos.y() > geo.center().y()
        return None, True

    def _apply_drop(self, name: str, pos: QPoint) -> None:
        """把被拖动的标签与落点标签互换位置。

        用交换而不是插入：插入会把中间项整体挤开，而用户只是想把这
        两个调换个先后。落在列表空白处时不动作，避免误拖。
        """
        target, _after = self._hit_insert(pos)
        if name not in self._names or target is None or target == name:
            return
        source_index = self._names.index(name)
        target_index = self._names.index(target)
        self._names[source_index], self._names[target_index] = (
            self._names[target_index], self._names[source_index])
        # 延迟重建：此刻正处在拖拽源的 mouseMoveEvent 里（QDrag.exec
        # 尚未返回），直接删行会让返回后的代码访问已销毁对象
        QTimer.singleShot(0, self._rebuild_rows)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """拖放事件统一在这里处理：容器或任意行收到都走同一套逻辑。"""
        if obj is not self._host and not isinstance(obj, _RoomRow):
            return super().eventFilter(obj, event)
        etype = event.type()
        if etype == QEvent.Type.DragEnter:
            if event.mimeData().hasText():
                event.acceptProposedAction()
                return True
        elif etype == QEvent.Type.DragMove:
            self._on_drag_move()
            event.acceptProposedAction()
            return True
        elif etype == QEvent.Type.Drop:
            self._on_drop()
            event.acceptProposedAction()
            return True
        elif etype == QEvent.Type.DragLeave:
            self._reset_styles()
        return super().eventFilter(obj, event)

    def _drag_pos(self) -> QPoint:
        """鼠标位置换算到容器坐标。

        不用事件自带的坐标：拖放事件的坐标系随接收控件变化，而
        QCursor 始终是屏幕坐标，换算一次就够。
        """
        return self._host.mapFromGlobal(QCursor.pos())

    def _on_drag_move(self) -> None:
        if not self._drag_name:
            return
        target, _after = self._hit_insert(self._drag_pos())
        for name, row in self._rows.items():
            if name == self._drag_name:
                row.refresh_style(dragging=True)
            else:
                row.refresh_style(highlight=name == target)

    def _on_drop(self) -> None:
        if not self._drag_name:
            return
        self._apply_drop(self._drag_name, self._drag_pos())
        self._drag_name = None
        self._reset_styles()

    # ---------- 其它 ----------

    def _reset(self) -> None:
        """恢复默认顺序：回到自然顺序，并清掉自定义记录。"""
        self._names = list(self._defaults)
        self._rebuild_rows()
        self._restored = True

    def result_order(self) -> list[str]:
        return list(self._names)

    def default_tab(self) -> str:
        """用户选定的启动默认标签；空串表示全屋。

        本来就没设置过、也没改过选择时返回空串：不要把「全屋」写成
        显式配置，否则默认逻辑将来变化时用户会被固定住。
        """
        if not self._user_picked and not self._saved_default:
            return ""
        return self._default_tab

    def restored_default(self) -> bool:
        """用户是否点过「恢复默认」。"""
        return self._restored

    # ---------- 样式 ----------

    def _apply_styles(self) -> None:
        self._title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 13pt; font-weight: 600;")
        self._hint.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px;"
            " margin: 0 2px 0 0; }"
            f"QScrollBar::handle:vertical {{ background: {SiColors.SCROLLBAR};"
            " border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            " { height: 0; }")
        self._host.setStyleSheet("background: transparent;")
        for btn in (self._reset_btn, self._cancel_btn):
            btn.setStyleSheet(secondary_button_qss("6px 16px"))
        self._ok_btn.setStyleSheet(primary_button_qss("6px 18px"))

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()
        self._reset_styles()

    # ---------- 定位 ----------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 基类的淡入会被随后的几何重排打断，等窗口铺满父窗口后再淡入
        self.setWindowOpacity(0.0)
        QTimer.singleShot(0, self._fill_and_place)

    def _fill_and_place(self) -> None:
        import shiboken6

        if not shiboken6.isValid(self) or self._closing:
            return
        self._fill_parent_window()
        self._place_overlay()
        self._fade_in()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        x = (self.width() - self._panel.width()) // 2
        y = (self.height() - self._panel.height()) // 2
        self._panel.move(x, y)
