# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电限额堆叠卡组 + 延时关闭快捷卡（web 端 §3.7/§3.8 桌面简化版）。

两个独立组件（供整合代理替换 cuktech_panel.py 内的旧限额区/延时区）：

- :class:`ChargeLimitStack` —— 充电限额 4 张端口卡纵排（上游 index 端是
  堆叠一沓 + 手势翻页，桌面浮层空间有限，简化为纵排、不做手势）。
  每卡：端口名 + 状态行（未启用/仅一次/长期有效，fired 追加「· 已触发」
  并转警示色）+ 自绘进度条（session_wh/wh，wh=0 整行隐藏）+ 进度文字 +
  Wh 输入框 + once/always 下拉 + 快捷档 5/10/15/20/25 Wh（点击=填入并
  立即提交）+「设置」「关闭」（关闭=提交 wh:0）。
- :class:`DelayOffQuickCard` —— 延时关闭 4 行端口：名称 + 当前值
  （「已设 30 分」/「未设」）+ 快捷档 15/30/60/90/120/240 分 +「清除」。

交互纪律（与面板解耦，便于独立测试与替换）：
- 组件**不发任何网络请求**，只发信号 ``limit_commit(port, wh, mode)`` /
  ``delay_commit(port, minutes)``（0=清除），由面板调 service 门面 + Toast；
  成功与否由面板回喂 :meth:`ChargeLimitStack.commit_result` /
  :meth:`DelayOffQuickCard.commit_result`，失败恢复原值。
- 数据由外部整帧喂入：``update_limits(limits)``（门面 cuktech_charge_limits
  返回体，limits 键已规整 int 1-4）、``update_settings(settings)``（PIID
  9/10/11/12，直接喂 status["settings"] 或含 settings 键的全量 dict 均可）。
- 回填焦点保护（仿上游 dataset.touched）：用户正在编辑的输入框/已改动的
  下拉不被轮询覆盖；内容与设备值一致时自动恢复同步。延时行 pending 期间
  旧帧不覆盖「设置中…」，设备确认后解除（另有 10s 超时兜底防卡死）。
- 客户端校验对齐上游 parseWhInput：空白/NaN/负数/>1000 拦在本地不发信号
  （空白不能被当 0 静默关闭限额），仅发 ``invalid_input`` 供面板 Toast。
- 取色一律 SiColors 动态代理；retheme() 重求值内联样式（进度条在
  paintEvent 内取色，主题切换后自动跟随）。
"""

import math

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from app.ui.si_theme import SiColors, apply_combo_qss, themed_combo

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

# 端口号(1-4) -> 展示名（与 cuktech_panel._PORT_LABELS 同源语义）
_PORT_LABELS: dict[int, str] = {1: "C1", 2: "C2", 3: "C3", 4: "USB-A"}

# 充电限额快捷档（Wh，charge_limit.js QUICK_WH）与上限（服务端 MAX_LIMIT_WH）
_QUICK_WH: tuple[int, ...] = (5, 10, 15, 20, 25)
_MAX_LIMIT_WH = 1000.0

# 模式下拉：index -> mode 值（client set_charge_limit 的 mode 参数语义）
_MODE_LABELS: tuple[str, str] = ("仅一次", "长期有效")  # once / always

# 延时关闭快捷档（分钟，index 版 15/30/60/90/120/240）
_QUICK_MINUTES: tuple[int, ...] = (15, 30, 60, 90, 120, 240)

# 对外端口 1-4 -> 延时关闭 PIID（cuktech_client._DELAY_OFF_PIIDS 同款）
_DELAY_PIIDS: tuple[int, ...] = (9, 10, 11, 12)

# 延时关闭分钟数上下限（0=取消），越界视为设备未上报
_DELAY_MAX_MIN = 240

# 延时 pending 防卡死超时：设备确认/面板回喂均未到达时强制解除
_PENDING_TIMEOUT_MS = 10000


def _port_name(port: int) -> str:
    return _PORT_LABELS.get(port, f"P{port}")


def _as_float(value) -> float:
    """宽松转 float：None/脏值一律 0.0（渲染路径用，不拦截）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_wh(text: str) -> float | None:
    """parseWhInput 语义：空白/NaN/负数/越界 -> None（本地拦截）。

    空白必须返回 None 而不是 0——0 是「关闭限额」的合法提交，空白被
    Number() 当 0 会静默关掉限额（上游注释明示的坑）。显式 0 合法。
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        wh = float(stripped)
    except ValueError:
        return None
    if math.isnan(wh) or math.isinf(wh) or wh < 0 or wh > _MAX_LIMIT_WH:
        return None
    return wh


def _fmt_num(value: float) -> str:
    """37.5 -> "37.5"、100.0 -> "100"（:g 去尾零，与面板 _fmt_wh 同风格）。"""
    return f"{float(value):g}"


def _as_minutes(value) -> int | None:
    """PIID 原始值 -> 分钟数；非 int/越界返回 None（设备未上报语义）。"""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    if minutes < 0 or minutes > _DELAY_MAX_MIN:
        return None
    return minutes


# ----------------------------------------------------------------------------
# _LimitBar —— 自绘限额进度条
# ----------------------------------------------------------------------------


class _LimitBar(QWidget):
    """圆角轨道 + 主题色填充的进度条；fired 时填充转警示色。

    取色在 paintEvent 内经 SiColors 动态代理读取，主题切换后 update()
    即以新调色板重绘，无需重建控件。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ratio = 0.0
        self._fired = False
        self.setFixedHeight(6)

    def set_progress(self, ratio: float, fired: bool = False) -> None:
        """整值替换进度（0-1 自动钳制）与触发态。"""
        self._ratio = max(0.0, min(float(ratio), 1.0))
        self._fired = bool(fired)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            rect = self.rect()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(SiColors.WINDOW_BG))
            painter.drawRoundedRect(rect, 3, 3)
            if self._ratio > 0.0:
                # 填充至少一个圆头宽，ratio>0 时视觉可见
                width = max(int(rect.width() * self._ratio), rect.height())
                color = QColor(SiColors.WARN_TEXT if self._fired
                               else SiColors.THEME)
                painter.setBrush(color)
                painter.drawRoundedRect(
                    QRect(rect.left(), rect.top(), width, rect.height()), 3, 3)
        finally:
            painter.end()


# ----------------------------------------------------------------------------
# ChargeLimitStack —— 充电限额堆叠卡组（桌面简化版）
# ----------------------------------------------------------------------------


class ChargeLimitStack(QWidget):
    """充电限额 4 张端口卡纵排（上游堆叠+手势的桌面简化，不做手势）。

    信号：
    - ``limit_commit(port: int, wh: float, mode: str)``：所有提交唯一出口，
      面板负责调 service + Toast；wh=0 即关闭限额。
    - ``invalid_input(message: str)``：客户端校验拦截（空白/NaN/负数/越界）
      时发出，供面板 Toast；不发 limit_commit。
    """

    limit_commit = Signal(int, float, str)
    invalid_input = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._syncing = False               # 程序化回填下拉时屏蔽 touched
        self._data: dict[int, dict] = {}    # 最近一帧设备值（失败恢复用）
        self._rows: dict[int, dict] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for port in (1, 2, 3, 4):
            lay.addWidget(self._build_card(port))
        self._apply_styles()

    # ---------- 构建 ----------

    def _build_card(self, port: int) -> QFrame:
        frame = QFrame()
        frame.setObjectName("limitPortCard")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        # 行 1：端口名 + 状态行
        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(_port_name(port))
        name.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        status = QLabel("未启用")
        status.setFont(QFont("Microsoft YaHei UI", 8))
        head.addWidget(name)
        head.addWidget(status)
        head.addStretch(1)
        lay.addLayout(head)

        # 行 2：进度条 + 进度文字（wh=0 时整行隐藏）
        bar_row = QHBoxLayout()
        bar_row.setSpacing(8)
        bar = _LimitBar()
        progress = QLabel("")
        progress.setFont(QFont("Microsoft YaHei UI", 8))
        progress.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar_row.addWidget(bar, 1, Qt.AlignmentFlag.AlignVCenter)
        bar_row.addWidget(progress)
        lay.addLayout(bar_row)

        # 行 3：输入框 + 模式下拉 + 快捷档 + 设置/关闭
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)
        edit = QLineEdit()
        edit.setPlaceholderText("Wh 0-1000")
        edit.setFixedHeight(28)
        edit.setFixedWidth(88)
        edit.returnPressed.connect(lambda p=port: self._on_set(p))
        edit.textEdited.connect(lambda _t, p=port: self._on_edit_touched(p))
        combo = themed_combo(list(_MODE_LABELS), _MODE_LABELS[0])
        combo.currentIndexChanged.connect(
            lambda _i, p=port: self._on_mode_changed(p))
        ctrl.addWidget(edit)
        ctrl.addWidget(combo)
        quick_btns: list[QPushButton] = []
        for wh in _QUICK_WH:
            btn = QPushButton(f"{wh}")
            btn.setFixedSize(34, 26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, p=port, w=wh: self._on_quick(p, w))
            ctrl.addWidget(btn)
            quick_btns.append(btn)
        ctrl.addStretch(1)
        set_btn = QPushButton("设置")
        set_btn.setFixedSize(52, 28)
        set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_btn.clicked.connect(lambda _, p=port: self._on_set(p))
        off_btn = QPushButton("关闭")
        off_btn.setFixedSize(52, 28)
        off_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        off_btn.clicked.connect(lambda _, p=port: self._on_off(p))
        ctrl.addWidget(set_btn)
        ctrl.addWidget(off_btn)
        lay.addLayout(ctrl)

        self._rows[port] = {
            "frame": frame, "name": name, "status": status,
            "bar": bar, "progress": progress,
            "edit": edit, "combo": combo,
            "quick": quick_btns,
            "set_btn": set_btn, "off_btn": off_btn,
            "edit_touched": False, "mode_touched": False,
        }
        return frame

    # ---------- 数据注入（外部驱动，不发请求） ----------

    def update_limits(self, limits: dict) -> None:
        """整帧刷新（门面 cuktech_charge_limits 返回体或裸 limits dict）。

        limits 键为 int 1-4（客户端已规整；str 数字键兼容），每项含
        wh/mode/fired/session_wh/is_charging；缺省端口按未启用渲染。
        正在编辑的输入框/已改动的下拉不覆盖（焦点保护），内容与设备
        值一致时自动恢复同步。
        """
        if not isinstance(limits, dict):
            return
        inner = limits.get("limits")
        mapping = inner if isinstance(inner, dict) else limits
        for port in self._rows:
            entry = mapping.get(port)
            if entry is None:
                entry = mapping.get(str(port))
            if not isinstance(entry, dict):
                entry = {}
            self._data[port] = dict(entry)
            self._render_port(port, entry)

    def commit_result(self, port: int, ok: bool) -> None:
        """面板回喂提交结果：成功清 touched 恢复自动同步；失败恢复原值。

        失败恢复依据是最近一帧设备值（后端整体拒绝，不存在部分成功）。
        """
        row = self._rows.get(port)
        if row is None:
            return
        row["edit_touched"] = False
        row["mode_touched"] = False
        if not ok:
            self._render_port(port, self._data.get(port) or {}, force=True)

    # ---------- 渲染 ----------

    def _render_port(self, port: int, entry: dict, force: bool = False) -> None:
        """单端口整卡渲染；force=True 时无视焦点保护强制回填（失败恢复）。"""
        row = self._rows[port]
        wh = _as_float(entry.get("wh"))
        mode = "always" if str(entry.get("mode") or "") == "always" else "once"
        fired = bool(entry.get("fired"))
        session = _as_float(entry.get("session_wh"))

        # 状态行三态：未启用 / 仅一次|长期有效（+「· 已触发」警示色）
        if wh > 0:
            text = _MODE_LABELS[0 if mode == "once" else 1]
            color = SiColors.TEXT_SECONDARY
            if fired:
                text += " · 已触发"
                color = SiColors.WARN_TEXT
        else:
            text = "未启用"
            color = SiColors.TEXT_MUTED
        row["status"].setText(text)
        row["status"].setStyleSheet(
            f"color: {color}; background: transparent;")

        # 进度条 + 进度文字（wh=0 不显示）
        if wh > 0:
            row["bar"].setVisible(True)
            row["bar"].set_progress(min(session / wh, 1.0), fired)
            row["progress"].setVisible(True)
            row["progress"].setText(f"已充 {_fmt_num(session)} / {_fmt_num(wh)} Wh")
        else:
            row["bar"].setVisible(False)
            row["progress"].setVisible(False)

        # 输入框回填：touched/焦点保护；内容与设备一致时恢复同步
        text = _fmt_num(wh) if wh > 0 else ""
        edit = row["edit"]
        if row["edit_touched"]:
            parsed = _parse_wh(edit.text())
            if parsed is not None and abs(parsed - wh) < 1e-6:
                row["edit_touched"] = False
        if force or not (row["edit_touched"] or edit.hasFocus()):
            if edit.text() != text:
                edit.setText(text)

        # 模式下拉回填：touched 保护（程序化设值屏蔽 currentIndexChanged）
        index = 0 if mode == "once" else 1
        if row["mode_touched"] and row["combo"].currentIndex() == index:
            row["mode_touched"] = False
        if force or not row["mode_touched"]:
            if row["combo"].currentIndex() != index:
                self._syncing = True
                try:
                    row["combo"].setCurrentIndex(index)
                finally:
                    self._syncing = False

    # ---------- 交互（只发信号，不发请求） ----------

    def _current_mode(self, port: int) -> str:
        return "always" if self._rows[port]["combo"].currentIndex() == 1 else "once"

    def _on_edit_touched(self, port: int) -> None:
        """用户键入（textEdited 仅用户编辑触发，setText 不触发）。"""
        row = self._rows.get(port)
        if row is not None:
            row["edit_touched"] = True

    def _on_mode_changed(self, port: int) -> None:
        """用户改动模式下拉（程序化回填经 _syncing 屏蔽）。"""
        if self._syncing:
            return
        row = self._rows.get(port)
        if row is not None:
            row["mode_touched"] = True

    def _on_quick(self, port: int, wh: int) -> None:
        """快捷档：填入输入框并立即提交（点击=填入+提交）。"""
        row = self._rows[port]
        row["edit"].setText(str(wh))  # 填入（不动 touched，提交后随轮询同步）
        self.limit_commit.emit(port, float(wh), self._current_mode(port))

    def _on_set(self, port: int) -> None:
        """设置：校验输入框（空白/NaN/负数/越界本地拦截）后提交。"""
        wh = _parse_wh(self._rows[port]["edit"].text())
        if wh is None:
            self.invalid_input.emit(
                f"请输入 0-{_MAX_LIMIT_WH:.0f} 之间的 Wh 数值")
            return
        self.limit_commit.emit(port, wh, self._current_mode(port))

    def _on_off(self, port: int) -> None:
        """关闭限额：清空输入框并提交 wh:0（mode 随当前下拉，服务端忽略）。"""
        row = self._rows[port]
        row["edit"].setText("")
        self.limit_commit.emit(port, 0.0, self._current_mode(port))

    # ---------- 主题观感 ----------

    def _apply_styles(self) -> None:
        """重设构造期求值的内联样式（retheme 时整卡重刷）。"""
        frame_qss = (f"QFrame#limitPortCard {{ background: {SiColors.SURFACE};"
                     f" border: 1px solid {SiColors.LINE}; border-radius: 10px; }}")
        edit_qss = (
            f"QLineEdit {{ background: {SiColors.WINDOW_BG};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 8px;"
            f" padding: 2px 8px; color: {SiColors.TEXT_PRIMARY};"
            f" selection-background-color: {SiColors.THEME}; font-size: 9pt; }}"
            f"QLineEdit:focus {{ border-color: {SiColors.THEME}; }}")
        quick_qss = (
            f"QPushButton {{ background: {SiColors.WINDOW_BG};"
            f" color: {SiColors.TEXT_PRIMARY}; border: 1px solid {SiColors.LINE};"
            f" border-radius: 8px; font-size: 9pt; }}"
            f"QPushButton:hover {{ border-color: {SiColors.THEME};"
            f" color: {SiColors.THEME}; }}"
            f"QPushButton:disabled {{ color: {SiColors.TEXT_DISABLED};"
            f" border-color: {SiColors.LINE}; }}")
        set_qss = (
            f"QPushButton {{ background: {SiColors.THEME};"
            f" color: {SiColors.ON_THEME_TEXT}; border: none; border-radius: 8px;"
            f" font-size: 9pt; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {SiColors.THEME_HOVER}; }}"
            f"QPushButton:disabled {{ background: {SiColors.SURFACE};"
            f" color: {SiColors.TEXT_DISABLED}; }}")
        off_qss = (
            f"QPushButton {{ background: {SiColors.WINDOW_BG};"
            f" color: {SiColors.TEXT_PRIMARY}; border: 1px solid {SiColors.LINE};"
            f" border-radius: 8px; font-size: 9pt; }}"
            f"QPushButton:hover {{ border-color: {SiColors.DANGER};"
            f" color: {SiColors.DANGER_TEXT}; }}")
        for port, row in self._rows.items():
            row["frame"].setStyleSheet(frame_qss)
            row["name"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
            row["progress"].setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
            row["edit"].setStyleSheet(edit_qss)
            row["combo"].setStyleSheet("")
            apply_combo_qss(row["combo"])
            row["combo"].set_arrow_color(SiColors.TEXT_SECONDARY)
            for btn in row["quick"]:
                btn.setStyleSheet(quick_qss)
            row["set_btn"].setStyleSheet(set_qss)
            row["off_btn"].setStyleSheet(off_qss)
            # 状态行颜色由 _render_port 按数据决定，重刷一遍
            self._render_port(port, self._data.get(port) or {})

    def retheme(self) -> None:
        """主题切换：重求值内联样式（进度条 paintEvent 自动跟随调色板）。"""
        self._apply_styles()


# ----------------------------------------------------------------------------
# DelayOffQuickCard —— 延时关闭快捷卡（§3.8 index 版桌面简化）
# ----------------------------------------------------------------------------


class DelayOffQuickCard(QWidget):
    """延时关闭 4 行端口：名称 + 当前值 + 快捷档按钮排 +「清除」。

    信号：``delay_commit(port: int, minutes: int)``（0=清除），面板负责
    调 service + Toast。行级 pending 防抖（上游乐观更新+pending 语义）：
    提交后该行显示「设置中…」并禁点，直到设备上报确认（update_settings
    值与提交值一致）、面板回喂 :meth:`commit_result`，或 10s 超时兜底；
    pending 期间旧帧不覆盖该行展示。
    """

    delay_commit = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings: dict[int, int | None] = {}
        self._pending: dict[int, int] = {}
        self._rows: dict[int, dict] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        for port in (1, 2, 3, 4):
            lay.addLayout(self._build_row(port))
        self._apply_styles()

    # ---------- 构建 ----------

    def _build_row(self, port: int) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        name = QLabel(_port_name(port))
        name.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        name.setMinimumWidth(44)
        value = QLabel("—")
        value.setFont(QFont("Microsoft YaHei UI", 9))
        value.setMinimumWidth(84)
        row.addWidget(name)
        row.addWidget(value)
        row.addStretch(1)
        buttons: list[QPushButton] = []
        for minutes in _QUICK_MINUTES:
            btn = QPushButton(f"{minutes}")
            btn.setFixedSize(40, 26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _, p=port, m=minutes: self._on_quick(p, m))
            row.addWidget(btn)
            buttons.append(btn)
        clear_btn = QPushButton("清除")
        clear_btn.setFixedSize(56, 26)
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(lambda _, p=port: self._on_clear(p))
        row.addWidget(clear_btn)
        buttons.append(clear_btn)
        self._rows[port] = {"name": name, "value": value,
                            "buttons": buttons, "timer": None}
        return row

    # ---------- 数据注入（外部驱动，不发请求） ----------

    def update_settings(self, settings: dict) -> None:
        """整帧刷新（/api/status 的 settings，PIID 9/10/11/12）。

        直接喂 settings dict 或含 settings 键的全量 status dict 均可；
        pending 中的行：旧帧不覆盖（保持「设置中…」），设备上报值与
        提交值一致即确认解除。
        """
        if not isinstance(settings, dict):
            return
        inner = settings.get("settings")
        inner = inner if isinstance(inner, dict) else settings
        for port, row in self._rows.items():
            piid = _DELAY_PIIDS[port - 1]
            raw = inner.get(str(piid), inner.get(piid))
            minutes = _as_minutes(raw)
            if port in self._pending:
                if minutes is not None and minutes == self._pending[port]:
                    self._clear_pending(port)   # 设备已确认
                    self._settings[port] = minutes
                    self._render_value(port, minutes)
                continue
            self._settings[port] = minutes
            self._render_value(port, minutes)

    def commit_result(self, port: int, ok: bool) -> None:
        """面板回喂提交结果：成功按乐观值落定；失败恢复上一帧设备值。"""
        if port not in self._pending:
            return
        optimistic = self._pending[port]
        self._clear_pending(port)
        if ok:
            self._settings[port] = optimistic
        self._render_value(port, self._settings.get(port))

    # ---------- 渲染 ----------

    def _render_value(self, port: int, minutes: int | None) -> None:
        row = self._rows[port]
        if isinstance(minutes, int) and minutes > 0:
            row["value"].setText(f"已设 {minutes} 分")
            color = SiColors.TEXT_SECONDARY
        elif minutes == 0:
            row["value"].setText("未设")
            color = SiColors.TEXT_MUTED
        else:
            row["value"].setText("—")   # 设备未上报该 PIID
            color = SiColors.TEXT_MUTED
        row["value"].setStyleSheet(
            f"color: {color}; background: transparent;")

    # ---------- 交互（只发信号，不发请求） ----------

    def _on_quick(self, port: int, minutes: int) -> None:
        """快捷档提交；pending 中的行忽略（防重复点击）。"""
        if port in self._pending:
            return
        row = self._rows[port]
        self._pending[port] = minutes
        row["value"].setText("设置中…")
        row["value"].setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        for btn in row["buttons"]:
            btn.setEnabled(False)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda p=port: self._on_pending_timeout(p))
        timer.start(_PENDING_TIMEOUT_MS)
        row["timer"] = timer
        self.delay_commit.emit(port, minutes)

    def _on_clear(self, port: int) -> None:
        """清除延时 = 提交 0（同样走 pending 防抖）。"""
        self._on_quick(port, 0)

    def _clear_pending(self, port: int) -> None:
        row = self._rows[port]
        self._pending.pop(port, None)
        timer = row.get("timer")
        if timer is not None:
            timer.stop()
            row["timer"] = None
        for btn in row["buttons"]:
            btn.setEnabled(True)

    def _on_pending_timeout(self, port: int) -> None:
        """兜底：确认/回喂都未到达时解除 pending，按上一帧设备值回显。"""
        if port not in self._pending:
            return
        self._clear_pending(port)
        self._render_value(port, self._settings.get(port))

    # ---------- 主题观感 ----------

    def _apply_styles(self) -> None:
        """重设构造期求值的内联样式（retheme 时整卡重刷）。"""
        quick_qss = (
            f"QPushButton {{ background: {SiColors.SURFACE};"
            f" color: {SiColors.TEXT_PRIMARY}; border: 1px solid {SiColors.LINE};"
            f" border-radius: 8px; font-size: 9pt; }}"
            f"QPushButton:hover {{ border-color: {SiColors.THEME};"
            f" color: {SiColors.THEME}; }}"
            f"QPushButton:disabled {{ color: {SiColors.TEXT_DISABLED};"
            f" border-color: {SiColors.LINE}; }}")
        clear_qss = (
            f"QPushButton {{ background: {SiColors.SURFACE};"
            f" color: {SiColors.TEXT_PRIMARY}; border: 1px solid {SiColors.LINE};"
            f" border-radius: 8px; font-size: 9pt; }}"
            f"QPushButton:hover {{ border-color: {SiColors.DANGER};"
            f" color: {SiColors.DANGER_TEXT}; }}"
            f"QPushButton:disabled {{ color: {SiColors.TEXT_DISABLED};"
            f" border-color: {SiColors.LINE}; }}")
        for port, row in self._rows.items():
            row["name"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
            for btn in row["buttons"][:-1]:
                btn.setStyleSheet(quick_qss)
            row["buttons"][-1].setStyleSheet(clear_qss)
            self._render_value(port, self._settings.get(port))

    def retheme(self) -> None:
        """主题切换：重求值内联样式。"""
        self._apply_styles()
