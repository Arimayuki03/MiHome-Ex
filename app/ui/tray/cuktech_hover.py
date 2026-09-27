# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""托盘悬停数据弹窗：CUKTECH 充电器四口功率 + 总功率。

仿上游 cuktech-ble-server web 前端的图例条（cuktech_visuals 同款端口
四色 --port-c1/c2/c3/a）做成的小型 tooltip 弹窗：托盘图标悬停约 0.5s
后出现在图标下方，逐口「C1: x.xW」+ 「总功率 (W): x.xW」。数据由
TrayController 喂入——SSE port_update/status 推送与 2s 轮询兜底并存，
推送活络时轮询空转。网络失败按离线展示（蓝牙未连接）。

样式走 SiColors 动态代理（构造期求值的内联样式在 retheme 重设）；
弹窗为 Qt.ToolTip 顶层窗口，不抢焦点、不进任务栏。
"""

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.cuktech_visuals import _PORT_COLORS, _TOTAL_LINE_COLOR
from app.ui.si_theme import SiColors, current_theme

# 端口号(1-4) -> 展示名与色序（cuktech_panel._PORT_LABELS / 上游 index.css
# --port-c1/c2/c3/a 同款；A 口用 _PORT_COLORS 第 4 色 USB 黄）
_PORT_LABELS: dict[int, str] = {1: "C1", 2: "C2", 3: "C3", 4: "A"}


class CuktechHoverPopup(QWidget):
    """托盘图标悬停时的充电器实时功率弹窗（Qt.ToolTip 顶层窗）。"""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._status: dict | None = None

        self._root = QWidget(self)
        self._root.setObjectName("cuktechHoverPanel")
        self._root.setStyleSheet(
            f"QWidget#cuktechHoverPanel {{ background: {SiColors.WINDOW_BG}; "
            f"border: 1px solid {SiColors.LINE}; border-radius: 10px; }}")
        lay = QVBoxLayout(self._root)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(5)

        # 四个端口行：圆点 + 名字（左）对数值（右）
        self._port_rows: dict[int, tuple[QLabel, QLabel]] = {}
        for port_id in (1, 2, 3, 4):
            dot = QLabel(self._root)
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(
                f"background: {_PORT_COLORS[port_id - 1]}; border-radius: 5px;")
            name = QLabel(f"{_PORT_LABELS[port_id]}:")
            name.setFont(QFont("Microsoft YaHei UI", 9))
            val = QLabel()
            val.setFont(QFont("Microsoft YaHei UI", 9))
            val.setAlignment(Qt.AlignmentFlag.AlignRight |
                             Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(self._make_row(dot, name, val))
            self._port_rows[port_id] = (name, val)

        # 分隔线 + 总功率行（加粗，对齐截图层次）
        sep = QLabel(self._root)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {SiColors.LINE}; border: none;")
        lay.addSpacing(3)
        lay.addWidget(sep)
        lay.addSpacing(3)
        self._total_dot = QLabel(self._root)
        self._total_dot.setFixedSize(10, 10)
        self._total_name = QLabel("总功率 (W):")
        self._total_name.setFont(
            QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
        self._total_val = QLabel()
        self._total_val.setFont(
            QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
        self._total_val.setAlignment(Qt.AlignmentFlag.AlignRight |
                                     Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._make_row(self._total_dot, self._total_name,
                                     self._total_val))

        self._apply_text_colors()
        self._render()  # 先按未知状态渲染，避免首次 show 空白
        self.resize(self._root.sizeHint())

    @staticmethod
    def _make_row(dot: QLabel, name: QLabel, val: QLabel) -> QWidget:
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(7)
        row_lay.addWidget(dot)
        row_lay.addWidget(name)
        row_lay.addStretch(1)
        row_lay.addWidget(val)
        return row

    # ---------- 数据入口（TrayController 喂入） ----------

    def set_status(self, status: dict | None) -> None:
        """喂入 /api/status 快照（SSE status 推送与轮询共用入口）。"""
        if not shiboken6.isValid(self):
            return
        self._status = dict(status) if isinstance(status, dict) else None
        self._render()

    def push_port_update(self, payload: dict) -> None:
        """SSE port_update 注入点：单口增量合并进快照（cuktech_panel 同款）。"""
        if not shiboken6.isValid(self):
            return
        data = payload.get("data")
        port_id = payload.get("port_id")
        if not isinstance(data, dict) or not isinstance(port_id, int):
            return
        status = dict(self._status or {})
        ports = dict(status.get("ports") or {})
        ports[str(port_id)] = data
        status["ports"] = ports
        status["connected"] = True  # 推送本身即在线证据（网关每秒推端口数据）
        self._status = status
        self._render()

    # ---------- 渲染 ----------

    def _render(self) -> None:
        self._render_ports()
        self._render_total()
        # 文本宽度变化会挤 layout，仅在 sizeHint 实际变化时重设尺寸，
        # 避免 resize 抖动（"0.0W"→"10.0W" 这类等宽变化不触发）
        hint = self._root.sizeHint()
        if hint != self._root.size():
            self._root.resize(hint)
            self.resize(hint)

    def _render_ports(self) -> None:
        status = self._status or {}
        connected = status.get("connected") is True
        ports = status.get("ports") or {}
        for port_id, (_name, val) in self._port_rows.items():
            entry = ports.get(str(port_id))
            # BLE 断开时服务端端口数据是残留旧值（cuktech_panel 同纪律），
            # 没拿到过该口数据也先按占位显示
            if not connected or not isinstance(entry, dict):
                val.setText("-- W")
                continue
            val.setText(f"{self._port_power(entry):.1f}W")

    def _render_total(self) -> None:
        status = self._status or {}
        connected = status.get("connected") is True
        total = 0.0
        if connected:
            # Σ ports[*].power 口径与 cuktech_panel 一致（不滤 enabled）
            for entry in (status.get("ports") or {}).values():
                if isinstance(entry, dict):
                    total += self._port_power(entry)
        self._total_val.setText(f"{total:.1f}W" if connected else "-- W")

    @staticmethod
    def _port_power(entry: dict) -> float:
        try:
            return float(entry.get("power") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ---------- 观感 ----------

    def _apply_text_colors(self) -> None:
        for name, val in self._port_rows.values():
            name.setStyleSheet(self._name_style())
            val.setStyleSheet(self._name_style())
        self._total_name.setStyleSheet(self._total_name_style())
        self._total_val.setStyleSheet(self._total_name_style())
        # Σ 圆点用总功率线色（深 #C084FC / 浅 #7C3AED，随主题）
        self._total_dot.setStyleSheet(
            f"background: {_TOTAL_LINE_COLOR[current_theme()]}; "
            "border-radius: 5px;")

    def _name_style(self) -> str:
        return f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"

    def _total_name_style(self) -> str:
        return f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"

    def retheme(self) -> None:
        """主题切换：文字色与 Σ 圆点色全部重设。"""
        self._apply_text_colors()
