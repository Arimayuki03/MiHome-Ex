# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""托盘设备管理对话框：勾选加入 + 拖拽排序。"""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core import tray_store
from app.core.models import DeviceInfo
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import SiColors
from app.ui.tray.draggable_row import _DraggableRow


class TrayManagerDialog(OverlayDialog):
    """托盘设备管理：拖拽排序 + 勾选加入托盘，默认空。"""

    def __init__(self, devices: list[DeviceInfo], parent=None):
        # 与设备详情/添加抽屉同款观感：无边框圆角面板；
        # 但面板铺满窗口并带可拖拽标题栏，保持可移动性，无遮罩
        super().__init__(parent, overlay=False)
        self.setWindowTitle("管理托盘设备")
        self._drag_did: str | None = None
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self._panel)
        lay.setContentsMargins(16, 12, 16, 16)
        lay.setSpacing(12)

        # 顶部标题栏：标题 + 关闭按钮，整个标题栏区域可拖动窗口
        title_bar = QFrame(self._panel)
        title_bar.setObjectName("trayTitleBar")
        title_bar.setAttribute(Qt.WA_StyledBackground, True)
        title_bar.setStyleSheet("QFrame#trayTitleBar { background: transparent; }")
        title_bar.setCursor(Qt.OpenHandCursor)
        self._title_bar = title_bar
        self._header_drag_pos = None
        # 标题栏整个区域可拖拽窗口：事件过滤器捕获，避免子控件吃掉事件
        title_bar.installEventFilter(self)
        header = QHBoxLayout(title_bar)
        header.setContentsMargins(4, 2, 4, 2)
        header.setSpacing(8)
        header_title = QLabel("管理托盘设备")
        header_title.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
        header_title.setStyleSheet(f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        header.addWidget(header_title)
        header.addStretch(1)
        header.addWidget(self._make_close_button())
        lay.addWidget(title_bar)

        subtitle = QLabel("勾选加入托盘快捷窗口，可长按拖拽排序\n在托盘中右键卡片可切换占半行/整行（充电器整行显示实时功率）")
        subtitle.setStyleSheet(f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9pt;")
        lay.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self._host = host
        self._list_lay = QVBoxLayout(host)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(8)
        scroll.setWidget(host)
        lay.addWidget(scroll, stretch=1)
        # 与工作台一致：容器与对话框均接受拖拽，确保事件不被 viewport 拦截
        host.setAcceptDrops(True)
        scroll.viewport().setAcceptDrops(True)

        self._checks: dict[str, QPushButton] = {}
        self._rows: dict[str, _DraggableRow] = {}
        self._scroll = scroll
        selected = list(tray_store.load())
        dev_map = {d.did: d for d in devices}
        # 已选设备按 tray_store 顺序排列，未选设备在线优先+按名排序追加
        ordered = [dev_map[d] for d in selected if d in dev_map]
        sel_set = set(selected)
        unordered = sorted(
            [d for d in devices if d.did not in sel_set],
            key=lambda d: (0 if d.online else 1, d.name))
        all_devs = ordered + unordered

        for dev in all_devs:
            row = _DraggableRow(dev.did)
            row.setObjectName("trayRow")
            row.setAttribute(Qt.WA_StyledBackground, True)
            row.setFixedHeight(48)
            row.setMaximumWidth(290)
            from PySide6.QtWidgets import QSizePolicy as _SP2
            row.setSizePolicy(_SP2.Preferred, _SP2.Fixed)
            row.setStyleSheet(
                f"QFrame#trayRow {{ background: {SiColors.CARD}; border: 1px solid {SiColors.LINE}; border-radius: 10px; }}"
                f"QFrame#trayRow:hover {{ background: {SiColors.CARD_HOVER}; }}")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 8, 16, 8)
            name = QLabel(f"{dev.name}  ·  {dev.room_name}")
            name.setStyleSheet(f"color: {SiColors.TEXT_PRIMARY if dev.online else f'{SiColors.OFFLINE_TEXT}'}; background: transparent;")
            rl.addWidget(name, stretch=1)
            btn = QPushButton("✓" if dev.did in selected else "+")
            btn.setFixedSize(28, 28)
            btn.setCheckable(True)
            btn.setChecked(dev.did in selected)
            # 文字必须随勾选态同步：QSS 只负责背景色，固定文字会导致
            # 绿底显示加号 / 暗底显示对勾的错位（与详情页 set_added 一致）
            btn.toggled.connect(lambda checked, b=btn: b.setText("✓" if checked else "+"))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: {SiColors.SURFACE}; border: none; border-radius: 14px; color: {SiColors.TEXT_PRIMARY}; }}"
                "QPushButton:checked { background: #3dbba4; color: #0b0b0e; }"
                f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}"
                "QPushButton:checked:hover { background: #4ccdb5; }")
            self._checks[dev.did] = btn
            rl.addWidget(btn)
            self._rows[dev.did] = row
            row.dragStarted.connect(self._on_drag_start)
            row.dragFinished.connect(self._on_drag_finish)
            self._list_lay.addWidget(row)
        self._list_lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        cancel.setStyleSheet(
            f"QPushButton {{ background: {SiColors.SURFACE}; border: none; border-radius: 8px; padding: 6px 16px; color: {SiColors.TEXT_PRIMARY}; }}"
            f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}")
        ok = QPushButton("保存")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setStyleSheet(
            f"QPushButton {{ background: {SiColors.THEME}; border: none; border-radius: 8px; padding: 6px 16px; color: #0b0b0e; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {SiColors.THEME_HOVER}; }}")
        ok.clicked.connect(self.accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        lay.addLayout(btn_row)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 事件已被标题栏过滤器处理，这里只处理空白面板（可忽略）
        self._header_drag_pos = None
        super().mousePressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # 标题栏拖拽：在 eventFilter 中捕获鼠标事件，子控件（标题/按钮）
        # 的事件也会先经过此处，统一处理窗口移动
        if obj is self._title_bar and event.type() in (
            QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease
        ):
            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._header_drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove:
                if self._header_drag_pos is not None and event.buttons() & Qt.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._header_drag_pos)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._header_drag_pos = None
                return False  # 放行，让按钮点击生效
        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._header_drag_pos = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._header_drag_pos = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 面板铺满整个窗口，保留四角圆角观感
        self._panel.setGeometry(self.rect())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None and parent.window() is not None and parent.window().isVisible():
            self._center_on_screen(340, 475)
            self.raise_()
            self._fade_in()
            return
        # 主窗口隐藏（托盘独立弹出）时稍高一些
        self._center_on_screen(340, 520)
        self.raise_()
        self._fade_in()

    def _on_drag_start(self, did: str) -> None:
        self._drag_did = did

    def _on_drag_finish(self) -> None:
        self._drag_did = None
        self._reset_row_styles()

    def _reset_row_styles(self) -> None:
        for r in self._rows.values():
            r.setStyleSheet(
                f"QFrame#trayRow {{ background: {SiColors.CARD}; border: 1px solid {SiColors.LINE}; border-radius: 10px; }}"
                f"QFrame#trayRow:hover {{ background: {SiColors.CARD_HOVER}; }}")

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasText() and e.mimeData().text() in self._rows:
            self._drag_did = e.mimeData().text()
            e.acceptProposedAction()
        elif self._drag_did:
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasText() and e.mimeData().text() in self._rows:
            self._drag_did = e.mimeData().text()
        if not self._drag_did:
            e.ignore()
            return
        # 与工作台一致：用 host 坐标系检测落点
        pos = self._host.mapFrom(self, e.position().toPoint())
        # 若鼠标在 viewport 外，尝试由 viewport 映射
        if not self._host.rect().contains(pos):
            pos = self._host.mapFrom(self._scroll.viewport(), self._scroll.viewport().mapFrom(self, e.position().toPoint()))
        hovered_did = self._hit_test(pos)
        for did, row in self._rows.items():
            if did == self._drag_did:
                continue
            if did == hovered_did:
                row.setStyleSheet(
                    f"QFrame#trayRow {{ background: {SiColors.CARD_HOVER}; border: 2px solid #3dbba4; border-radius: 10px; }}")
            else:
                row.setStyleSheet(
                    f"QFrame#trayRow {{ background: {SiColors.CARD}; border: 1px solid {SiColors.LINE}; border-radius: 10px; }}"
                    f"QFrame#trayRow:hover {{ background: {SiColors.CARD_HOVER}; }}")
        e.acceptProposedAction()

    def dragLeaveEvent(self, e) -> None:  # noqa: N802
        self._reset_row_styles()
        super().dragLeaveEvent(e)

    def dropEvent(self, e) -> None:  # noqa: N802
        self._reset_row_styles()
        did_text = e.mimeData().text() if e.mimeData().hasText() else self._drag_did
        if not did_text or did_text not in self._rows:
            e.ignore()
            return
        self._drag_did = did_text
        pos = self._host.mapFrom(self, e.position().toPoint())
        if not self._host.rect().contains(pos):
            pos = self._host.mapFrom(self._scroll.viewport(), self._scroll.viewport().mapFrom(self, e.position().toPoint()))
        self._apply_drop(did_text, pos)
        self._drag_did = None
        e.acceptProposedAction()

    def _apply_drop(self, did: str, pos) -> None:
        """按落点坐标应用排序。可独立调用以便离屏测试。"""
        src_row = self._rows[did]
        target_did = self._hit_test(pos)
        if target_did == did:
            # 原地放下：保持原位不动。此前 hit_test 跳过自身，原地
            # 放下会被误判为「拖到空白」而挪到列表末尾（视口外看似消失）
            return
        if not target_did:
            # 真正的空白处：移到末尾（最后一个 widget 之后、stretch 之前）
            self._list_lay.removeWidget(src_row)
            src_row.show()
            self._list_lay.insertWidget(max(0, self._list_lay.count() - 1), src_row)
            return
        dst_row = self._rows[target_did]
        # 先移除源行再取目标索引：removeWidget 会让后续条目前移，
        # 用移除前算的索引插入会偏一位
        self._list_lay.removeWidget(src_row)
        dst_idx = self._list_lay.indexOf(dst_row)
        if dst_idx < 0:
            return
        src_row.show()
        self._list_lay.insertWidget(dst_idx, src_row)

    def _hit_test(self, pos) -> str | None:
        """用 host 坐标检测鼠标下方的行（含被拖拽行自身，原地放下才可判定）。"""
        for did, row in self._rows.items():
            if row.geometry().contains(pos):
                return did
        return None

    def selected_dids(self) -> list[str]:
        """按当前布局顺序返回已勾选的 did。"""
        result = []
        for i in range(self._list_lay.count()):
            item = self._list_lay.itemAt(i)
            w = item.widget() if item else None
            if w is None:
                continue
            did = getattr(w, "_did", None)
            if did and self._checks.get(did, QPushButton()).isChecked():
                result.append(did)
        return result




