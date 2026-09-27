# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电器端口详情弹窗——上游 web index 页 portModal 的桌面等价物。

对齐 cuktech-ble-server/web index.html:405-423 + app.js:878-952/997-1049：
点端口卡弹出的单口浮层 = 四大读数（电压/电流/功率/协议徽标，非 idle
高亮）+ 「⚡实时」曲线区 + 单口协议开关组；关闭方式（Esc/点遮罩/右上
角 ×）与淡入淡出复用 OverlayDialog 壳，装载方式仿 DeviceDetailDialog
（内容摆 self._panel，resizeEvent 居中放置浮层面板）。

数据通路（本模块零轮询，全部外部注入，与 CuktechPanel 的 SSE 优先纪律
一致）：

- 实时读数与曲线：面板收到 SSE port_update（约 1s 一条）后调
  :meth:`PortDetailDialog.push_port_sample` 增量喂入；组件内部维护该
  端口的环形缓冲（容量 _MAX_POINTS=120，时间戳用接收时刻）。上游的
  500ms 去抖是为 Chart.js 重绘节流，Qt 的 update() 本身合并重绘请求，
  无需等价物；上游 2s 定时刷新同理（推送到达即重绘）。
- 无 SSE 兜底：构造时用 /api/status 中该口当前数据填缓冲首点；此后
  超过 _STALE_AFTER_S 秒无新数据，曲线区以灰字标注「数据未更新」
  （信息提示而非错误，由 1s 巡检定时器置位，仅显示期间运行）。
- 协议开关：不复用 4 端口全量的 ProtocolSwitchPanel（它是整面板卡片），
  新写单口精简行（C1/C2: PD/PPS/UFCS；C3/A: UFCS/SCP）；SSE protocol
  事件由面板转喂 :meth:`PortDetailDialog.apply_protocol_event`（按本
  端口过滤）。写路径与 cuktech_protocols.py 同模式：service 门面的
  ``cuktech_set_protocol_switch(port, protocol, on)``（getattr 防御，
  方法未落地时开关禁用 + tooltip「门面方法未就绪」）+ JobExecutor 后台
  线程；busy 期间禁用交互且注入不覆盖其视觉，成功不做乐观校准（等
  下一帧事件以设备真实状态覆盖），失败回滚开关视觉并 Toast 中文报错。

曲线为自绘单线（功率），风格对齐 MultiMetricCurve：网格参考线 / 主题
色填充 / 峰值标注；纵轴为左侧 nice 取整数值带（3-5 档，0 线必标），
横轴为「-10分 / -5分 / 现在」三档相对时间刻度；点数不满 120 时曲线
均匀铺满整个绘图区（右端=最新点），满 120 后窗口滑动（空位不补零，
上游 padZeros 会画出一段假 0W 线，「没数据」不等于「0W」），区间标注
「最近 10 分钟 · N/120 点」。
"""

import math
import shiboken6
import time
from collections import deque
from typing import TYPE_CHECKING

import qtawesome as qta
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.cuktech_stage import PORT_COLORS
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import SiColors, themed_switch
from app.ui.toast import Toast

if TYPE_CHECKING:
    from app.core.jobs import JobExecutor
    from app.core.service import MijiaService

# ----------------------------------------------------------------------------
# 常量与端口元数据
# ----------------------------------------------------------------------------

# 浮层面板尺寸：DeviceDetailDialog 为 900×640，单口详情取小一号的
# 600×560（读数行 + 160 曲线 + 协议卡，窗口过窄时自适应收缩）
_PANEL_W = 600
_PANEL_H = 560

# 曲线几何（与 MultiMetricCurve 同款边距纪律；左侧留数值带）
_CURVE_HEIGHT = 160
_CURVE_MARGIN_L = 42   # 左侧 Y 轴数值带（W 取整刻度文字）
_CURVE_MARGIN_R = 10
_CURVE_MARGIN_T = 20  # 顶部留给峰值标注 / 数据未更新标注
_CURVE_MARGIN_B = 22  # 底部留给 X 轴「-10分/-5分/现在」刻度文字

_MAX_POINTS = 120      # 环形缓冲容量（上游 MAX_VISIBLE=120）
_WINDOW_MINUTES = 10   # 语义窗口（上游 REAL_TIME_WINDOW_MS=10min）
_STALE_AFTER_S = 10.0  # 超过该秒数无新数据 → 曲线标注「数据未更新」
_STALE_POLL_MS = 1000  # 数据未更新巡检周期

# 端口号(1-4) -> 展示名 / protocol_switches 键 / 该口可切协议（顺序即
# 渲染顺序；与 cuktech_protocols._PORT_ROWS 同源口径）
_PORT_NAMES: dict[int, str] = {1: "C1", 2: "C2", 3: "C3", 4: "USB-A"}
_PORT_KEYS: dict[int, str] = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
_PORT_PROTOCOLS: dict[int, tuple[str, ...]] = {
    1: ("pd", "pps", "ufcs"),
    2: ("pd", "pps", "ufcs"),
    3: ("ufcs", "scp"),
    4: ("ufcs", "scp"),
}
_PROTOCOL_LABELS: dict[str, str] = {
    "pd": "PD", "pps": "PPS", "ufcs": "UFCS", "scp": "SCP"}


def _num(value) -> float | None:
    """宽泛转 float（None/脏值 → None，渲染路径不炸）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nice_axis(peak: float) -> tuple[float, list[float]]:
    """Y 轴 nice 刻度：峰值向上取整到 1/2/2.5/5/10 步长，4~5 档含 0。

    返回 (轴顶值, 刻度列表升序)。例：peak=6.6 → (8.0, [0,2,4,6,8])；
    peak=65 → (80.0, [0,20,40,60,80])。常规功率量级下刻度为整数
    （%g 渲染无小数点），极小峰值自动降级为小数步长防除零/死循环，
    轴顶 ≥ 峰值保证曲线不被裁顶。
    """
    top = max(float(peak), 1.0)
    raw = top / 4  # 目标 4~5 档
    mag = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= mult * mag:
            step = mult * mag
            break
    else:  # pragma: no cover - 循环必命中（10*mag 上界）
        step = 10.0 * mag
    axis_max = math.ceil(top / step) * step
    count = int(round(axis_max / step))
    return axis_max, [k * step for k in range(count + 1)]


# ----------------------------------------------------------------------------
# _PortCurve —— 自绘单线实时曲线（PowerCurveWidget 同风格）
# ----------------------------------------------------------------------------


class _PortCurve(QWidget):
    """QPainter 自绘单线功率曲线：Y 轴数值带 + 网格线 + 填充 + 峰值标注。

    与 MultiMetricCurve 的差异：数据为环形缓冲的最近 ≤120 点，X 向按
    实际点数均匀铺满绘图区（右端=最新点；不满 120 点时从左端起铺——
    时间均匀采样，满 120 后自然等效窗口滑动），横轴为「-10分 / -5分 /
    现在」相对时间刻度，纵轴为 nice 取整数值带；支持「数据未更新」灰
    字角标（stale 状态由对话框的巡检定时器置位）。取色在 paintEvent
    内经 SiColors 动态代理读取，retheme 后 update() 即以新调色板重绘。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points: list[float] = []
        self._stale = False
        self.setMinimumHeight(_CURVE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_points(self, points: list[float]) -> None:
        """整组替换曲线数据（调用方已保证 ≤_MAX_POINTS）。"""
        pts: list[float] = []
        for value in points:
            num = _num(value)
            if num is not None:
                pts.append(num)
        self._points = pts[-_MAX_POINTS:]
        self.update()

    def set_stale(self, stale: bool) -> None:
        """数据未更新角标开关（状态变化才重绘）。"""
        if self._stale != stale:
            self._stale = stale
            self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            if self._points:
                self._paint_curve(painter)
            else:
                self._paint_empty(painter)
            if self._stale:
                self._paint_stale(painter)
        finally:
            painter.end()

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无数据")

    def _paint_stale(self, painter: QPainter) -> None:
        """「数据未更新」灰字角标：右上角，信息提示而非错误。"""
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(
            self.rect().adjusted(0, 2, -4, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
            "数据未更新")

    def _paint_curve(self, painter: QPainter) -> None:
        rect = self.rect().adjusted(
            _CURVE_MARGIN_L, _CURVE_MARGIN_T, -_CURVE_MARGIN_R, -_CURVE_MARGIN_B)
        n = len(self._points)
        # Y 轴 nice 取整：峰值向上收整刻度，轴顶 ≥ 峰值不裁顶
        axis_max, ticks = _nice_axis(max(self._points))

        def _xy(i: int) -> tuple[float, float]:
            # N 个点均匀铺满整个绘图区（时间均匀采样，右端=最新点）；
            # N=120 时与 120 点窗口公式重合，满窗后即窗口滑动语义
            x = rect.left() + rect.width() * i / max(n - 1, 1)
            y = rect.bottom() - rect.height() * min(self._points[i], axis_max) / axis_max
            return float(x), float(y)

        # Y 轴数值带：刻度线 + 左侧 W 取整文字（0 线即基线必标）
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        axis_font_metrics = painter.fontMetrics()
        for tick in ticks:
            ty = rect.bottom() - rect.height() * tick / axis_max
            painter.setPen(QPen(QColor(SiColors.LINE), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if tick:
                painter.drawLine(QPointF(rect.left(), ty),
                                 QPointF(rect.right(), ty))
            painter.setPen(QColor(SiColors.TEXT_MUTED))
            text = f"{tick:g}"
            painter.drawText(
                QRectF(0.0, ty - axis_font_metrics.ascent(),
                       rect.left() - 4, axis_font_metrics.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                text)

        # 曲线主体 + 填充（跟随曲线）
        first_x, _ = _xy(0)
        path = QPainterPath(QPointF(*_xy(0)))
        for i in range(1, n):
            path.lineTo(QPointF(*_xy(i)))
        fill = QPainterPath(path)
        last_x, _ = _xy(n - 1)
        fill.lineTo(last_x, rect.bottom())
        fill.lineTo(first_x, rect.bottom())
        fill.closeSubpath()
        fill_color = QColor(SiColors.THEME)
        fill_color.setAlpha(40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill_color)
        painter.drawPath(fill)
        line_pen = QPen(QColor(SiColors.THEME), 2)
        line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(line_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # 基线（0 线，压在数值带之上）
        painter.setPen(QPen(QColor(SiColors.LINE), 1))
        painter.drawLine(QPointF(rect.left(), rect.bottom()),
                         QPointF(rect.right(), rect.bottom()))

        # X 轴相对时间刻度：「-10分 / -5分 / 现在」（实时窗口语义）
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        painter.drawText(
            QRectF(rect.left(), rect.bottom() + 2, 60, 14),
            Qt.AlignmentFlag.AlignLeft, "-10分")
        painter.drawText(
            QRectF(rect.center().x() - 30, rect.bottom() + 2, 60, 14),
            Qt.AlignmentFlag.AlignCenter, "-5分")
        painter.drawText(
            QRectF(rect.right() - 60, rect.bottom() + 2, 60, 14),
            Qt.AlignmentFlag.AlignRight, "现在")

        # 峰值标注：圆点 + 文案，越界时收进绘图区
        peak = max(self._points)
        peak_x, peak_y = _xy(self._points.index(peak))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(SiColors.THEME))
        painter.drawEllipse(QPointF(peak_x, peak_y), 3.0, 3.0)
        painter.setPen(QColor(SiColors.TEXT_PRIMARY))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        label = f"峰值 {peak:.1f}W"
        metrics = painter.fontMetrics()
        tx = peak_x - metrics.horizontalAdvance(label) / 2
        tx = max(float(rect.left()),
                 min(tx, rect.right() - metrics.horizontalAdvance(label)))
        baseline = peak_y - 8
        if baseline - metrics.ascent() < rect.top():
            baseline = peak_y + metrics.ascent() + 4
        painter.drawText(QPointF(tx, baseline), label)


# ----------------------------------------------------------------------------
# _PortProtocolCard —— 单口精简协议开关卡
# ----------------------------------------------------------------------------


class _PortProtocolCard(QFrame):
    """单端口协议开关卡：标题 + 灰字说明 + 该口协议开关行。

    ProtocolSwitchPanel 的单口精简版（语义一致：外部驱动不发请求、
    busy 跳过注入、失败回滚、门面缺失防御），只渲染本口可切的协议项。
    """

    def __init__(self, service: "MijiaService", jobs: "JobExecutor",
                 port: int, parent=None):
        super().__init__(parent)
        self._service = service
        self._jobs = jobs
        self._port = port
        self._key = _PORT_KEYS.get(port, f"p{port}")
        # 正在写命令中的协议名：busy 期间禁用交互，注入不覆盖其视觉
        self._busy: set[str] = set()
        self._switches: dict[str, QWidget] = {}
        self._proto_labels: dict[str, QLabel] = {}
        # 门面方法就绪态（构造期 getattr 防御，见 _apply_facade_state）
        self._facade_ready = True

        self.setObjectName("propCard")
        self.setAttribute(Qt.WA_StyledBackground, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(10)

        # 标题行：图标 + 「快充协议开关」（CuktechPanel 分区标题同款）
        title = QHBoxLayout()
        title.setSpacing(8)
        self._icon_label = QLabel()
        self._icon_label.setFixedSize(16, 16)
        title.addWidget(self._icon_label)
        self._title_label = QLabel("快充协议开关")
        self._title_label.setFont(
            QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold))
        title.addWidget(self._title_label)
        title.addStretch(1)
        lay.addLayout(title)

        self._hint_label = QLabel(
            "关闭某协议后，该端口不再以此协议协商；改动即时生效")
        self._hint_label.setFont(QFont("Microsoft YaHei UI", 8))
        self._hint_label.setWordWrap(True)
        lay.addWidget(self._hint_label)

        row = QHBoxLayout()
        row.setSpacing(10)
        for proto in _PORT_PROTOCOLS.get(port, ()):
            proto_label = QLabel(_PROTOCOL_LABELS.get(proto, proto))
            proto_label.setFont(QFont("Microsoft YaHei UI", 10))
            switch = themed_switch()
            switch.toggled.connect(
                lambda checked, p=proto, s=switch:
                    self._on_switch_toggled(p, checked, s))
            row.addWidget(proto_label)
            row.addWidget(switch)
            self._switches[proto] = switch
            self._proto_labels[proto] = proto_label
        row.addStretch(1)
        lay.addLayout(row)

        self._apply_facade_state()
        self._apply_inline_styles()

    # ---------- 主题观感 ----------

    def _apply_inline_styles(self) -> None:
        """重设构造期求值的内联样式（主题切换时由 retheme 调用）。"""
        self._icon_label.setPixmap(
            qta.icon("mdi.swap-horizontal", color=SiColors.THEME).pixmap(16, 16))
        self._title_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._hint_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        for proto, label in self._proto_labels.items():
            label.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")

    def retheme(self) -> None:
        """主题切换：重求值内联样式（开关取色构造期绑定，双主题同色）。"""
        self._apply_inline_styles()

    # ---------- 门面就绪态 ----------

    def _apply_facade_state(self) -> None:
        """门面方法未落地：开关禁用 + tooltip 提示（getattr 防御）。"""
        setter = getattr(self._service, "cuktech_set_protocol_switch", None)
        self._facade_ready = callable(setter)
        if not self._facade_ready:
            for switch in self._switches.values():
                switch.setEnabled(False)
                switch.setToolTip("门面方法未就绪")

    # ---------- 数据注入（外部驱动，不发请求） ----------

    def set_entry(self, entry: dict) -> None:
        """整帧同步本口开关状态（键为协议名，缺省按关处理）。

        正在写命令中的开关跳过不覆盖（服务端状态尚未变化，覆盖会回跳
        闪烁），等命令落定后的下一帧校准。
        """
        if not shiboken6.isValid(self):
            return
        if not isinstance(entry, dict):
            return
        for proto, switch in self._switches.items():
            if proto in self._busy:
                continue
            self._sync_switch(switch, bool(entry.get(proto, False)))

    def apply_event(self, payload: dict) -> None:
        """SSE protocol 事件注入：{"type":"protocol","switches":{...}}。

        只取本端口键的条目（按端口过滤），非 protocol 事件与畸形结构
        静默忽略。
        """
        if not isinstance(payload, dict):
            return
        if payload.get("type", "protocol") != "protocol":
            return
        switches = payload.get("switches")
        if not isinstance(switches, dict):
            return
        entry = switches.get(self._key)
        if isinstance(entry, dict):
            self.set_entry(entry)

    # ---------- 开关切写 ----------

    @staticmethod
    def _sync_switch(switch, checked: bool) -> None:
        """程序化同步开关状态（setChecked 会发 toggled，必须屏蔽信号）。

        SiSwitchRefactor 的自绘进度与 checked 状态是两套存储，同步补齐
        （cuktech_protocols.ProtocolSwitchPanel 同实现）。
        """
        switch.blockSignals(True)
        try:
            switch.setChecked(checked)
            switch.progress = 1.0 if checked else 0.0
            try:
                switch.progress_ani.setCurrentValue(1.0 if checked else 0.0)
            except Exception:
                pass
        finally:
            switch.blockSignals(False)

    def _on_switch_toggled(self, proto: str, on: bool, switch) -> None:
        """用户切换开关：置 busy 后经门面 + jobs 下发，成功不回写视觉。"""
        self._busy.add(proto)
        switch.setEnabled(False)
        setter = getattr(self._service, "cuktech_set_protocol_switch", None)
        if not callable(setter):
            # 门面方法未落地：同步回滚，不进队列（正常情况下构造期已
            # 禁用交互，此处为双重防御，保证模块独立可测）
            self._on_toggle_failed(proto, on, None, facade_missing=True)
            return
        self._jobs.submit(
            lambda: setter(self._port, proto, on),
            on_success=lambda _, p=proto, s=switch, o=on:
                self._on_toggle_done(p, s, o),
            on_error=lambda e, p=proto, s=switch, o=on:
                self._on_toggle_failed(p, o, e),
        )

    def _on_toggle_done(self, proto: str, switch, on: bool) -> None:
        self._busy.discard(proto)
        if not shiboken6.isValid(self):
            return
        switch.setEnabled(True)
        # 不做乐观校准：设备真实状态等下一帧注入覆盖
        Toast.info(self,
                   f"已{'打开' if on else '关闭'} "
                   f"{_PORT_NAMES.get(self._port, self._port)} "
                   f"{_PROTOCOL_LABELS.get(proto, proto)}", 2000)

    def _on_toggle_failed(self, proto: str, on: bool,
                          error: Exception | None,
                          facade_missing: bool = False) -> None:
        self._busy.discard(proto)
        if not shiboken6.isValid(self):
            return
        switch = self._switches.get(proto)
        if switch is None:
            return
        switch.setEnabled(True)
        # 失败回弹开关视觉状态，等下一次注入以真实值覆盖
        self._sync_switch(switch, not on)
        if facade_missing:
            Toast.info(self, "门面方法未就绪", 2500)
        else:
            Toast.info(self,
                       f"切换 {_PORT_NAMES.get(self._port, self._port)} "
                       f"{_PROTOCOL_LABELS.get(proto, proto)} 失败：{error}",
                       3000)


# ----------------------------------------------------------------------------
# PortDetailDialog —— 端口详情弹窗
# ----------------------------------------------------------------------------


class PortDetailDialog(OverlayDialog):
    """CUKTECH 充电器端口详情弹窗（上游 portModal 桌面等价物）。

    装载方式仿 DeviceDetailDialog：OverlayDialog 壳 + self._panel 上摆
    内容，resizeEvent 居中放置 600×560 浮层面板。构造参数带入打开瞬间
    的单口快照与协议开关帧（无 SSE 时的兜底首点/初态），此后由面板经
    push_port_sample / apply_protocol_event 增量喂入，本模块零轮询。
    """

    def __init__(self, parent, service: "MijiaService", jobs: "JobExecutor",
                 port: int, status_port_data: dict,
                 protocol_switches: dict):
        super().__init__(parent)
        self._service = service
        self._jobs = jobs
        self._port = int(port)
        self._key = _PORT_KEYS.get(self._port, f"p{self._port}")
        # 该端口实时环形缓冲（最近 ≤120 点，时间戳语义为接收时刻）
        self._samples: deque = deque(maxlen=_MAX_POINTS)
        self._last_sample_mono = time.monotonic()
        self._stale = False

        outer = QVBoxLayout(self._panel)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # 头部：端口色圆点 + 端口名 + 关闭按钮
        header = QHBoxLayout()
        header.setSpacing(8)
        self._dot_label = QLabel("●")
        self._dot_label.setFont(QFont("Microsoft YaHei UI", 12))
        self._name_label = QLabel(
            _PORT_NAMES.get(self._port, f"P{self._port}"))
        self._name_label.setFont(
            QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        header.addWidget(self._dot_label)
        header.addWidget(self._name_label)
        header.addStretch(1)
        header.addWidget(self._make_close_button())
        outer.addLayout(header)

        # 四大读数行：电压 V / 电流 A / 功率 W / 协议徽标
        outer.addWidget(self._build_stats())

        # ⚡实时曲线区
        outer.addWidget(self._build_curve_card(), stretch=1)

        # 单口协议开关区
        self._protocol_card = _PortProtocolCard(service, jobs, self._port)
        outer.addWidget(self._protocol_card)

        self._apply_inline_styles()

        # 初态：status 快照填读数 + 缓冲首点（无 SSE 兜底）
        if isinstance(status_port_data, dict) and status_port_data:
            self._ingest(status_port_data)
        else:
            self._set_readings({})
            self._update_range_label()
        self._apply_switches_frame(protocol_switches)

        # 数据未更新巡检（仅显示期间运行）
        self._stale_timer = QTimer(self)
        self._stale_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._stale_timer.setInterval(_STALE_POLL_MS)
        self._stale_timer.timeout.connect(self._check_stale)

    # ---------- 布局构建 ----------

    def _build_stats(self) -> QWidget:
        """四大读数行：电压 (V) / 电流 (A) / 功率 (W) / 实时充电协议。"""
        stats = QWidget()
        lay = QHBoxLayout(stats)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(8)
        self._v_value = QLabel("--")
        self._a_value = QLabel("--")
        self._w_value = QLabel("--")
        self._p_value = QLabel("idle")
        self._p_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for value, caption in (
                (self._v_value, "电压 (V)"),
                (self._a_value, "电流 (A)"),
                (self._w_value, "功率 (W)"),
                (self._p_value, "实时充电协议")):
            col = QVBoxLayout()
            col.setSpacing(2)
            value.setFont(
                QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption_label = QLabel(caption)
            caption_label.setFont(QFont("Microsoft YaHei UI", 8))
            caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(value)
            col.addWidget(caption_label)
            lay.addLayout(col, stretch=1)
        return stats

    def _build_curve_card(self) -> QFrame:
        """「⚡实时」曲线卡：分区标题 + 区间标注 + 自绘单线曲线。"""
        frame = QFrame()
        frame.setObjectName("propCard")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(8)

        title = QHBoxLayout()
        title.setSpacing(8)
        self._curve_icon_label = QLabel()
        self._curve_icon_label.setFixedSize(16, 16)
        title.addWidget(self._curve_icon_label)
        self._curve_title_label = QLabel("实时曲线")
        self._curve_title_label.setFont(
            QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold))
        title.addWidget(self._curve_title_label)
        title.addStretch(1)
        self._range_label = QLabel("")
        self._range_label.setFont(QFont("Microsoft YaHei UI", 8))
        title.addWidget(self._range_label)
        lay.addLayout(title)

        self._curve = _PortCurve()
        lay.addWidget(self._curve, stretch=1)
        return frame

    # ---------- 主题观感 ----------

    def _style_protocol_badge(self, active: bool) -> None:
        """协议徽标：非 idle 高亮（主题青胶囊），idle 灰置。"""
        if active:
            self._p_value.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;"
                f" border: 1px solid {SiColors.THEME}; border-radius: 9px;"
                f" padding: 1px 6px;")
        else:
            self._p_value.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                f" border: 1px solid {SiColors.LINE}; border-radius: 9px;"
                f" padding: 1px 6px;")

    def _apply_inline_styles(self) -> None:
        """重设构造期求值的内联样式（主题切换时由 retheme 调用）。"""
        self._dot_label.setStyleSheet(
            f"color: {PORT_COLORS.get(self._port, SiColors.THEME)};"
            f" background: transparent;")
        self._name_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._curve_icon_label.setPixmap(
            qta.icon("mdi.flash", color=SiColors.THEME).pixmap(16, 16))
        self._curve_title_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._range_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        for value_label in (self._v_value, self._a_value):
            value_label.setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._w_value.setStyleSheet(
            f"color: {SiColors.THEME}; background: transparent;")
        # 协议徽标按当前文字重取亮灭态
        self._style_protocol_badge(
            self._p_value.text() not in ("", "idle"))

    def retheme(self) -> None:
        """主题切换：壳层面板底色 + 本弹窗内联样式 + 子组件级联。"""
        super().retheme()
        self._apply_inline_styles()
        self._protocol_card.retheme()
        self._curve.update()

    # ---------- 渲染 ----------

    def _set_readings(self, data: dict) -> None:
        """刷新四大读数（缺键/脏值显示 --）。"""
        voltage = _num(data.get("voltage"))
        current = _num(data.get("current"))
        power = _num(data.get("power"))
        self._v_value.setText("--" if voltage is None else f"{voltage:.1f}")
        self._a_value.setText("--" if current is None else f"{current:.2f}")
        self._w_value.setText("--" if power is None else f"{power:.1f}")
        protocol = str(data.get("protocol") or "idle")
        self._p_value.setText(protocol)
        self._style_protocol_badge(protocol not in ("", "idle"))

    def _update_range_label(self) -> None:
        """曲线区间标注：最近 10 分钟 · N/120 点。"""
        self._range_label.setText(
            f"最近 {_WINDOW_MINUTES} 分钟 · "
            f"{len(self._samples)}/{_MAX_POINTS} 点")

    # ---------- 实时数据注入 ----------

    def _ingest(self, port_data: dict) -> None:
        """消化一条该口数据：入环形缓冲 + 刷读数 + 刷曲线。"""
        self._samples.append({
            "voltage": _num(port_data.get("voltage")),
            "current": _num(port_data.get("current")),
            "power": _num(port_data.get("power")),
            "protocol": str(port_data.get("protocol") or "idle"),
        })
        self._last_sample_mono = time.monotonic()
        if self._stale:
            self._stale = False
            self._curve.set_stale(False)
        self._set_readings(port_data)
        self._curve.set_points(
            [sample["power"] or 0.0 for sample in self._samples])
        self._update_range_label()

    def push_port_sample(self, port_id, port_data) -> None:
        """SSE port_update 注入点：面板每秒把该口数据增量喂入。

        port_id 为对外端口号 1-4，非本端口数据静默忽略；port_data 结构
        同 /api/status 的 ports 条目（voltage/current/power/protocol）。
        """
        if not shiboken6.isValid(self):
            return
        try:
            pid = int(port_id)
        except (TypeError, ValueError):
            return
        if pid != self._port or not isinstance(port_data, dict):
            return
        self._ingest(port_data)

    def _check_stale(self) -> None:
        """无新数据超过 _STALE_AFTER_S 秒：曲线标注「数据未更新」。"""
        if self._stale:
            return
        if time.monotonic() - self._last_sample_mono > _STALE_AFTER_S:
            self._stale = True
            self._curve.set_stale(True)

    # ---------- 协议开关注入 ----------

    def _apply_switches_frame(self, protocol_switches: dict) -> None:
        """从 protocol_switches 帧取本口条目喂协议卡（构造期初态）。

        容错：接受外层含 protocol_switches 的全量 status dict（自动解
        包）、四口全量帧（按键取本口）、或直接传本口条目。
        """
        switches = protocol_switches
        if isinstance(switches, dict):
            inner = switches.get("protocol_switches")
            if isinstance(inner, dict):
                switches = inner
            candidate = switches.get(self._key)
            entry = candidate if isinstance(candidate, dict) else switches
        else:
            entry = {}
        self._protocol_card.set_entry(entry or {})

    def apply_protocol_event(self, payload: dict) -> None:
        """SSE protocol 事件注入点（面板按端口过滤后整帧转喂）。

        协议卡内部只取本端口键的条目，非 protocol 事件与畸形结构静默
        忽略。
        """
        self._protocol_card.apply_event(payload)

    # ---------- 生命周期 ----------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        # 居中面板：600×560，窗口过小时自适应收缩
        pw = min(_PANEL_W, max(320, self.width() - 40))
        ph = min(_PANEL_H, max(320, self.height() - 40))
        x = (self.width() - pw) // 2
        y = (self.height() - ph) // 2
        self._panel.setGeometry(x, y, pw, ph)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._stale_timer.isActive():
            self._stale_timer.start()
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        # 独立弹出：铺满可用屏幕（遮罩盖住全屏），面板由 resizeEvent
        # 居中放置（DeviceDetailDialog 同款策略）
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())
        self.raise_()
        self._fade_in()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._stale_timer.stop()
        super().hideEvent(event)
