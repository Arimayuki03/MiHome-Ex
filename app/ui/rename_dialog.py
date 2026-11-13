# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""修改设备名称的小对话框。

用应用统一的无边框圆角面板承载输入框，避免原生 QInputDialog 与
其余界面风格割裂；「恢复默认」直接清掉本地覆盖，不必手动清空输入。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import (
    SiColors,
    primary_button_qss,
    secondary_button_qss,
)

_PANEL_W = 420


class RenameDeviceDialog(OverlayDialog):
    """返回 (名称, 是否恢复默认)；取消时 result 为 Rejected。"""

    def __init__(self, current: str, cloud_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改设备名称")
        self._restore = False

        panel = self._panel
        panel.setFixedWidth(_PANEL_W)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        self._title = QLabel("修改设备名称")
        lay.addWidget(self._title)

        self._hint = QLabel(f"留空或点「恢复默认」可还原为「{cloud_name}」。")
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        self._edit = QLineEdit(current)
        self._edit.setFixedHeight(34)
        self._edit.selectAll()
        self._edit.returnPressed.connect(self.accept)
        lay.addWidget(self._edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._restore_btn = QPushButton("恢复默认名称")
        self._restore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._restore_btn.clicked.connect(self._on_restore)
        btn_row.addWidget(self._restore_btn)
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

    # ---------- 交互 ----------

    def _on_restore(self) -> None:
        # 恢复默认不需要用户再点确定：意图已经明确
        self._restore = True
        self.accept()

    def value(self) -> str:
        return self._edit.text().strip()

    def restoring(self) -> bool:
        return self._restore

    # ---------- 样式 ----------

    def _apply_styles(self) -> None:
        self._title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 13pt; font-weight: 600;")
        self._hint.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        self._edit.setStyleSheet(
            f"QLineEdit {{ background: {SiColors.CTRL_BG};"
            f" border: 1px solid {SiColors.CTRL_BORDER}; border-radius: 8px;"
            f" padding: 4px 10px; color: {SiColors.TEXT_PRIMARY};"
            " font-size: 10pt; }"
            f"QLineEdit:focus {{ border-color: {SiColors.THEME}; }}")
        self._restore_btn.setStyleSheet(secondary_button_qss("7px 14px"))
        self._cancel_btn.setStyleSheet(secondary_button_qss("7px 18px"))
        self._ok_btn.setStyleSheet(primary_button_qss("7px 18px"))

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()

    # ---------- 定位 ----------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 先把透明度按回 0：紧接着的几何重排会打断基类启动的淡入，
        # 等窗口铺满父窗口后再统一淡入
        self.setWindowOpacity(0.0)
        QTimer.singleShot(0, self._fill_and_place)

    def _fill_and_place(self) -> None:
        import shiboken6

        # 对话框可能在延迟回调到达前就开始关闭，此时再淡入会和
        # 淡出动画抢同一个透明度属性
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
