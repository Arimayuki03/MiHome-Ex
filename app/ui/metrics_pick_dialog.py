# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""卡片外显属性选择对话框。

默认只显示温度和湿度，插座这类设备更关心功率。这里把设备上所有可读
的数值属性列出来，让用户挑最多两项显示在卡片副标题上。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.spec_zh import lookup_zh, PROPERTY_ZH
from app.ui.check_box import SiCheckBox
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import (
    SiColors,
    primary_button_qss,
    secondary_button_qss,
)

_PANEL_W = 380
_MAX_PICK = 2
# 参与外显的属性类型：布尔与字符串没法当数值展示
_NUMERIC_TYPES = ("int", "uint", "float")


class MetricsPickDialog(OverlayDialog):
    """返回选中的属性名列表；取消时 result 为 Rejected。"""

    def __init__(self, props, selected: list[str] | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("卡片外显")
        self._boxes: dict[str, SiCheckBox] = {}
        self._prop_info = {p.name: p for p in props}
        # selected 传 None 表示这台设备从未设置过，打开时按默认温湿度
        # 预勾；传空列表表示用户已明确选择不外显，保持全空。
        # _user_changed 用来区分"没碰过"和"主动改过"，没碰过就不写配置。
        self._saved = None if selected is None else list(selected)
        self._user_changed = False
        self._resetting = False
        # 恢复默认按钮按下后：勾选退回温湿度，同时标记要清掉自定义记录
        self._reset_to_default = False

        panel = self._panel
        panel.setFixedWidth(_PANEL_W)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)

        self._title = QLabel("卡片外显")
        lay.addWidget(self._title)

        self._hint = QLabel(f"选择在设备卡片上显示的值，最多 {_MAX_PICK} 项。")
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setFixedHeight(220)
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        box_lay = QVBoxLayout(host)
        box_lay.setContentsMargins(2, 2, 2, 2)
        box_lay.setSpacing(4)
        numeric = [p for p in props if p.readable and p.type in _NUMERIC_TYPES]
        initial = (self._default_names(numeric) if self._saved is None
                   else self._saved)
        for prop in numeric:
            # 用与挂件设置同款的自绘复选框：整个软件的勾选控件观感统一
            box = SiCheckBox(self._label_of(prop))
            box.setChecked(prop.name in initial)
            box.toggled.connect(self._on_toggle)
            box_lay.addWidget(box)
            self._boxes[prop.name] = box
        box_lay.addStretch(1)
        scroll.setWidget(host)
        lay.addWidget(scroll)

        if not numeric:
            self._hint.setText("这台设备没有可在卡片上显示的数值属性。")

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

        self._scroll = scroll
        self._refresh_hint()
        self._apply_styles()
        panel.adjustSize()

    # ---------- 内容 ----------

    @staticmethod
    def _default_names(props) -> list[str]:
        """默认外显的两项：温度 + 湿度（湿度在规格里有两种叫法）。"""
        available = {p.name for p in props}
        names: list[str] = []
        if "temperature" in available:
            names.append("temperature")
        for humidity in ("relative-humidity", "humidity"):
            if humidity in available:
                names.append(humidity)
                break
        return names

    @staticmethod
    def _label_of(prop) -> str:
        """属性展示名：优先用规格描述里的中文，取不到就退回属性名。"""
        desc = (prop.desc or "").split(" / ")[-1].strip()
        if desc:
            return f"{desc}（{prop.name}）"
        zh = lookup_zh(prop.name, PROPERTY_ZH)
        return f"{zh}（{prop.name}）" if zh else prop.name

    def _checked(self) -> list[str]:
        # 按属性在规格里的顺序输出，展示顺序稳定
        return [name for name in self._prop_info
                if self._boxes.get(name) is not None
                and self._boxes[name].isChecked()]

    def _on_toggle(self, checked: bool) -> None:
        self._user_changed = True
        if self._resetting:
            # 恢复默认是批量改选中态，中途会短暂超过上限，不该被回退
            return
        if checked and len(self._checked()) > _MAX_PICK:
            # 超出上限时回退这次勾选，避免用户以为能选第三项
            sender = self.sender()
            if isinstance(sender, SiCheckBox):
                sender.setChecked(False)
            return
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        chosen = len(self._checked())
        self._hint.setText(
            f"选择在设备卡片上显示的值，最多 {_MAX_PICK} 项（已选 {chosen} 项）。")

    def _reset(self) -> None:
        """恢复默认：退回温度 + 湿度，并清掉已保存的自定义选择。"""
        self._user_changed = True
        self._reset_to_default = True
        self._resetting = True
        defaults = self._default_names(list(self._prop_info.values()))
        for name, box in self._boxes.items():
            box.setChecked(name in defaults)
        self._resetting = False
        self._refresh_hint()

    # ---------- 结果 ----------

    def result_props(self) -> list[str]:
        """用户最终选中的属性列表，空列表表示不外显。"""
        return self._checked()

    def untouched_default(self) -> bool:
        """从未设置且用户也没改动：调用方应保持默认而不写配置。"""
        return self._saved is None and not self._user_changed

    def restored_default(self) -> bool:
        """用户点过「恢复默认」：调用方应删掉记录，回到默认温湿度。"""
        return self._reset_to_default

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
            "QScrollBar:vertical { background: transparent; width: 6px; }"
            f"QScrollBar::handle:vertical {{ background: {SiColors.SCROLLBAR};"
            " border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            " { height: 0; }")
        for box in self._boxes.values():
            box.setStyleSheet("QCheckBox { background: transparent; }")
        for btn in (self._reset_btn, self._cancel_btn):
            btn.setStyleSheet(secondary_button_qss("6px 16px"))
        self._ok_btn.setStyleSheet(primary_button_qss("6px 18px"))

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()

    # ---------- 定位 ----------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
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
