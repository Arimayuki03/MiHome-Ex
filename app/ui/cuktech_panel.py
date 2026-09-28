# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电器专用 UI 三件套：设备卡片、功率曲线控件、详情面板。

数据获取分两层：SSE 推送（CuktechEventStream，经主窗口路由到
push_* 注入方法）优先实时更新功率/端口/限额展示；卡片与面板各自的
5s QTimer 轮询（经 service 门面的 cuktech_* 方法 + JobExecutor）保留
作兜底，推送到达时重置对应计时器。曲线数据量大不走推送，仍由面板
轮询拉取（只拉当前档位）。网络调用走 JobExecutor 后台线程（SSE 线程
绝不进 jobs 队列），回调先以 shiboken6.isValid 判活再操作控件。取色
一律 SiColors 动态代理、图标走 qtawesome(mdi.*)、字号沿用 8/9/10/11/15pt
档位。

模块分节：
- 常量与端口元数据
- PowerCurveWidget —— QPainter 自绘单线功率曲线（简单场景复用件；
  面板「实时」页曲线已升级为 cuktech_visuals.MultiMetricCurve）
- CuktechDeviceCard —— 216×92 专用卡片（状态点/总功率/总开关），自轮询状态
- CuktechPanel —— 详情面板（分区 Tab：实时/历史/统计/设备设置），
  由 DeviceDetailDialog 经 panel_factory 装载，接口对齐 WorkbenchPanel；
  Tab 导航沿用主窗口房间筛选的 themed_tab_button 自绘选中态 +
  QStackedWidget 换页。「实时」页为 web 主视觉布局：顶部状态条 +
  左设备渲染舞台（cuktech_stage.DeviceStageWidget，随容器宽自适应、
  最小 320 逻辑宽）+ 右 2×2 端口卡（PortCardGrid，点卡片开端口详情
  弹窗 cuktech_port_detail.PortDetailDialog，SSE 数据由面板转发喂数）；
  容器宽不足时双栏自动上下堆叠（resizeEvent 重排）。往下：四色功率
  占比条（MiniPhoneShareBar）+ 多指标曲线（MultiMetricCurve，档位切换经
  jobs 拉 cuktech_chart(hours, interval)）+ 可折叠限额卡组
  （cuktech_limits.ChargeLimitStack，默认收起只留摘要行）；「设备
  设置」页场景区为 SceneButtonRow（乐观更新 3s 保护，仿上游
  markLocal）、延时关闭区为 DelayOffQuickCard、底部为连接质量卡
  （cuktech_quality.ConnectionQualityCard，SSE quality 直喂）。历史/
  统计/设置页包 QScrollArea，整面板高度与 DeviceDetailDialog 的浮层
  尺寸约定（CUKTECH 大壳 920×760，通用详情保持 900×640）对齐
"""

import shiboken6
import time
from typing import TYPE_CHECKING

import qtawesome as qta
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.core.models import DeviceInfo
from app.siui.components.container import SiRowCard
from app.ui.cuktech_history import (
    EnergySummaryWidget,
    SessionCurveWidget,
    SessionHistoryWidget,
)
from app.ui.cuktech_limits import ChargeLimitStack, DelayOffQuickCard
from app.ui.cuktech_port_detail import PortDetailDialog
from app.ui.cuktech_quality import ConnectionQualityCard
from app.ui.cuktech_stage import DeviceStageWidget, MiniPhoneShareBar, PortCardGrid
from app.ui.cuktech_visuals import MultiMetricCurve, SceneButtonRow
from app.ui.power_button import PowerButton
from app.ui.si_theme import (
    SiColors,
    SiToggleButtonRefactor,
    apply_combo_qss,
    themed_combo,
    themed_switch,
    themed_tab_button,
)
from app.ui.toast import Toast

if TYPE_CHECKING:
    from app.core.jobs import JobExecutor
    from app.core.service import MijiaService

# ----------------------------------------------------------------------------
# 常量与端口元数据
# ----------------------------------------------------------------------------

# 卡片尺寸与通用 DeviceCard 完全一致：网格列宽计算假设等宽，无需改动
_CARD_FIXED_WIDTH = 216
_CARD_FIXED_HEIGHT = 92
_POWER_BTN_SIZE = 36

# 卡片与面板共用的状态轮询周期（BLE 网关 /api/status 变化才重建，轮询廉价）
_STATUS_POLL_MS = 5000

# 功率曲线（PowerCurveWidget 复用件）：点数上限与绘制边距
_CURVE_MAX_POINTS = 200
_CURVE_HEIGHT = 160
_CURVE_MARGIN_L = 10
_CURVE_MARGIN_R = 10
_CURVE_MARGIN_T = 20  # 顶部留给峰值标注
_CURVE_MARGIN_B = 8

# 端口号(1-4) -> 展示名；/api/status 的 ports 键是字符串 "1"-"4"
_PORT_LABELS: dict[int, str] = {1: "C1", 2: "C2", 3: "C3", 4: "USB-A"}

# 分区 Tab：文案与图标（qtawesome mdi），顺序即面板页序
# （协议开关不做独立 Tab：单口协议卡收敛进端口详情弹窗）
_TAB_DEFS: tuple[tuple[str, str], ...] = (
    ("实时", "mdi.view-dashboard"),
    ("历史", "mdi.history"),
    ("统计", "mdi.chart-bar"),
    ("设备设置", "mdi.tune"),
)
_TAB_REALTIME, _TAB_HISTORY, _TAB_STATS, _TAB_SETTINGS = range(4)

# 息屏时间（PIID 6）：值 -> 展示文案，下拉按值排序展示
_SCREEN_TIMEOUT_OPTIONS: list[tuple[int, str]] = [
    (5, "1 分钟"), (1, "5 分钟"), (2, "10 分钟"),
    (3, "30 分钟"), (4, "常亮"),
]

# 设备语言（PIID 13）：布尔 -> 文案
_LANGUAGE_OPTIONS: list[tuple[bool, str]] = [(True, "中文"), (False, "English")]

# 面板高度与最小宽度：DeviceDetailDialog 对 CUKTECH 面板用大壳
# 920×760（通用详情保持 900×640），扣除壳层 margins（16×2）与标题行/
# 关闭钮后可用高度约 660；面板取该值保持观感一致，切 Tab 高度不变，
# 各页内部滚动。壳按 CuktechPanel.PREFERS_LARGE_DIALOG 特性放大，
# 其他面板不声明该特性则维持 900×640。
# 高度不能 setFixedHeight：壳高是 min(760, 窗口高-40) 的运行时值，
# 主窗口矮于 800（逻辑 px）时壳缩到 760 以下，固定 660 的面板会被
# 壳裁掉底部——页内 QScrollArea 滚到底的最后一段正好落在被裁区域，
# 视觉即「滑不到底」。改为 maximum 660 + 最小 400：大壳下与旧观感
# 一致，矮壳下由布局压缩面板、页内滚动消化内容差。
_PANEL_HEIGHT = 660
_PANEL_MIN_HEIGHT = 400
_PANEL_MIN_WIDTH = 780

# 设备舞台渲染尺寸：360×481 逻辑坐标系按容器宽等比缩放（坐标纪律
# 见 cuktech_stage 模块文档）。舞台占主视觉行固定比例列，宽度随面板
# 伸缩；320 是等比缩放下限（cuktech_stage 的 0.5 兜底）之上的观感
# 下限，再窄进入上下堆叠模式保证两者都不挤
_STAGE_MIN_WIDTH = 320
# 主视觉双栏的堆叠阈值：可用行宽低于该值时端口卡移到舞台下方
_HERO_STACK_WIDTH = 760
# 端口卡网格最小宽：2×2 网格再窄会挤压读数与开关
_PORT_GRID_MIN_WIDTH = 380
# 主视觉行舞台列占比（stage:grid ≈ 2:3，与上游 index.html 双栏观感一致）
_HERO_STAGE_STRETCH = 2
_HERO_GRID_STRETCH = 3
# 堆叠模式下舞台的固定渲染宽（≈旧 _STAGE_WIDTH 档，高按 hfw 换算）：
# 舞台按宽度等比缩放，堆叠时若随行宽拉伸会把设备画到 600+ 高挤占
# 首屏，固定 400 居中显示（上游移动端上下堆叠观感）
_STAGE_STACK_WIDTH = 400

# 图表时间档 -> 桶宽秒（抄上游 chart-config.js HISTORY_INTERVALS 桌面
# 四档，与 MultiMetricCurve._RANGE_DEFS 同源）；默认 60 分档
_CHART_RANGE_INTERVALS: dict[float, int] = {0.5: 20, 1.0: 20, 2.0: 30, 24.0: 300}
_CHART_DEFAULT_HOURS = 1.0

# 场景乐观更新保护期：用户点击后 3s 内轮询/SSE settings 回报不覆盖
# 乐观选中值（仿上游 app.js markLocal/isRecent 协议）
_SCENE_LOCAL_GUARD_S = 3.0

# 充电器模式（PIID 5，cuktech_client.SCENE_MODES 对齐）-> 提交 Toast 文案
_SCENE_NAMES: dict[int, str] = {1: "AI", 2: "数码生态", 3: "单口", 4: "均衡"}

# session_end 触发历史/统计刷新的节流窗口（秒）：设备一次拔插可能连发
# 多口 session_end，连续刷新只会排队相同请求（SessionHistoryWidget/
# EnergySummaryWidget.refresh 自带 _in_flight 去重，节流免重复排队）
_SESSION_END_THROTTLE_S = 5.0


def _port_name(port: int) -> str:
    return _PORT_LABELS.get(port, f"P{port}")


def _fmt_wh(value: float) -> str:
    """Wh 文案：37.5 -> "37.5Wh"，100.0 -> "100Wh"。"""
    return f"{float(value):g}Wh"


def _fmt_watts(value: float) -> str:
    return f"{float(value):.1f}W"


# ----------------------------------------------------------------------------
# PowerCurveWidget —— 自绘功率曲线（简单场景复用件）
# ----------------------------------------------------------------------------


class PowerCurveWidget(QWidget):
    """QPainter 自绘单线功率曲线：网格线 + 主题色填充曲线 + 峰值标注
    + 悬浮数据点提示（QToolTip 显示该点功率值）。

    面板「实时」页曲线已升级为 MultiMetricCurve（多指标 + 档位切换），
    本控件保留作简单场景复用件（历史组件同风格参照）。

    取色在 paintEvent 内经 SiColors 动态代理读取，主题切换后只需
    update() 即以新调色板重绘，无需重建控件。空白状态显示「暂无数据」。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points: list[float] = []
        self._times: list[str] = []  # 与 points 对齐的时间文案（悬浮用）
        self._hover_index: int = -1
        self._max_w: float | None = None
        self.setMinimumHeight(_CURVE_HEIGHT)
        self.setMouseTracking(True)

    def set_series(self, points: list[float], max_w: float | None = None,
                   times: list[str] | None = None) -> None:
        """整组替换曲线数据；点数超出上限按等距抽稀，max_w 缺省取峰值。

        times 提供与 points 对齐的时间文案（抽稀同步裁剪），悬浮提示
        显示「时间 + 功率」；缺省只显示功率。
        """
        pts: list[float] = []
        for value in points:
            try:
                pts.append(float(value))
            except (TypeError, ValueError):
                continue
        padded = list(times or []) + [""] * len(pts)
        if len(pts) > _CURVE_MAX_POINTS:
            stride = len(pts) / _CURVE_MAX_POINTS
            idx = [int(i * stride) for i in range(_CURVE_MAX_POINTS)]
            pts = [pts[i] for i in idx]
            padded = [padded[i] for i in idx]
        self._points = pts
        self._times = padded[:len(pts)]
        self._hover_index = -1
        if max_w is None and pts:
            max_w = max(pts)
        self._max_w = max(float(max_w or 0.0), 1.0)  # 至少 1W，避免除零
        self.update()

    # ---------- 悬浮数据提示 ----------

    def _chart_rect(self) -> QRectF:
        """绘图区几何（绘制与命中测试共用一套口径）。"""
        return QRectF(self.rect()).adjusted(
            _CURVE_MARGIN_L, _CURVE_MARGIN_T,
            -_CURVE_MARGIN_R, -_CURVE_MARGIN_B)

    def _nearest_point(self, x: float) -> int:
        """x 坐标 -> 最近点索引；不在绘图区或无数据返回 -1。"""
        if not self._points:
            return -1
        rect = self._chart_rect()
        if x < rect.left() or x > rect.right():
            return -1
        n = len(self._points)
        frac = (x - rect.left()) / max(rect.width(), 1e-6)
        return max(0, min(round(frac * max(n - 1, 1)), n - 1))

    def _tooltip_text(self, index: int) -> str:
        if not 0 <= index < len(self._points):
            return ""
        time_text = self._times[index] if index < len(self._times) else ""
        value = f"{self._points[index]:.1f}W"
        return f"{time_text} · {value}" if time_text else value

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        idx = self._nearest_point(event.position().x())
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()
        if idx >= 0:
            QToolTip.showText(event.globalPosition().toPoint(),
                              self._tooltip_text(idx), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hover_index = -1
        self.update()
        QToolTip.hideText()
        super().leaveEvent(event)

    def hover_index(self) -> int:
        """当前悬浮点索引（无悬浮 -1）；测试断言用。"""
        return self._hover_index

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            if not self._points:
                self._paint_empty(painter)
            else:
                self._paint_curve(painter)
        finally:
            painter.end()

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无数据")

    def _paint_curve(self, painter: QPainter) -> None:
        rect = self.rect().adjusted(
            _CURVE_MARGIN_L, _CURVE_MARGIN_T, -_CURVE_MARGIN_R, -_CURVE_MARGIN_B)
        n = len(self._points)
        span = max(self._max_w or 0.0, max(self._points), 1.0)

        def _xy(i: int) -> tuple[float, float]:
            x = rect.left() + rect.width() * i / max(n - 1, 1)
            y = rect.bottom() - rect.height() * min(self._points[i], span) / span
            return float(x), float(y)

        # 网格：三条水平参考线
        painter.setPen(QPen(QColor(SiColors.LINE), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for k in (1, 2, 3):
            y = int(rect.top() + rect.height() * k / 4)
            painter.drawLine(rect.left(), y, rect.right(), y)

        # 曲线主体
        path = QPainterPath(QPointF(*_xy(0)))
        for i in range(1, n):
            path.lineTo(QPointF(*_xy(i)))
        fill = QPainterPath(path)
        fill.lineTo(rect.right(), rect.bottom())
        fill.lineTo(rect.left(), rect.bottom())
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

        # 基线
        painter.setPen(QPen(QColor(SiColors.LINE), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

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
        tx = max(float(rect.left()), min(tx, rect.right() - metrics.horizontalAdvance(label)))
        baseline = peak_y - 8
        if baseline - metrics.ascent() < rect.top():
            baseline = peak_y + metrics.ascent() + 4
        painter.drawText(QPointF(tx, baseline), label)

        # 悬浮指示：命中点画竖参考线 + 曲线交点圆点（上游 index 模式观感）
        if 0 <= self._hover_index < n:
            hx, hy = _xy(self._hover_index)
            guide_pen = QPen(QColor(SiColors.TEXT_MUTED), 1)
            guide_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(guide_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(hx, rect.top()),
                             QPointF(hx, rect.bottom()))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(SiColors.THEME))
            painter.drawEllipse(QPointF(hx, hy), 3.5, 3.5)


# ----------------------------------------------------------------------------
# CuktechDeviceCard —— 专用设备卡片
# ----------------------------------------------------------------------------


class CuktechDeviceCard(SiRowCard):
    """CUKTECH 充电器专用卡片：状态点 + 设备名 + 当前总功率 + 总开关。

    与通用 DeviceCard 同尺寸同圆角（网格列宽计算无需区分）。数据由
    卡片自带轮询经 service.cuktech_status 刷新，不走主窗口的云端轮询。
    电源钮为「总开关」（点击发 power_clicked 由主窗口统一处理），
    点击卡片空白区发 open_requested 打开详情面板。

    对主窗口暴露与 DeviceCard 同名的最小接口（set_power_state /
    set_metrics / set_icon / set_busy），其中读数与图标按本卡语义忽略。
    """

    power_clicked = Signal(str)  # did
    open_requested = Signal(str)  # did

    def __init__(self, device: DeviceInfo, service: "MijiaService",
                 jobs: "JobExecutor", parent=None):
        super().__init__(parent, self.LeftToRight)
        self.device = device
        self._service = service
        self._jobs = jobs
        self._busy = False
        self._hovered = False
        self._online = bool(device.online)
        self._status: dict | None = None
        self._poll_in_flight = False
        # 总开关状态（PIID 16 位图非 0 即视为开）；主窗口总开关切换前
        # 读它取反。None=尚未获取，电源钮显示未知态
        self._output_on: bool | None = None

        self.style_data.background_color = QColor(
            SiColors.OFFLINE_CARD if not self._online else SiColors.CARD)
        self.style_data.border_radius = 14.0
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedSize(_CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.muteStretchWidget()

        self._power_btn = PowerButton(_POWER_BTN_SIZE, icon_size=24)
        self._power_btn.clicked.connect(lambda: self.power_clicked.emit(device.did))
        self._power_btn.set_online(self._online)
        # 开关能力是已知的，直接可见；真实状态等首次轮询回填
        self._power_btn.show()

        self._dot_label = QLabel("●")
        self._dot_label.setFont(QFont("Microsoft YaHei UI", 8))

        self._name_label = QLabel(device.name or "CUKTECH 充电器")
        self._name_label.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold))

        self._watts_label = QLabel("-- W")
        self._watts_label.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))

        self._status_label = QLabel("正在获取状态…")
        self._status_label.setFont(QFont("Microsoft YaHei UI", 8))

        text_col = QWidget()
        text_col.setAttribute(Qt.WA_TranslucentBackground)
        text_lay = QVBoxLayout(text_col)
        text_lay.setContentsMargins(0, 0, 0, 0)
        text_lay.setSpacing(1)
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(5)
        top_row.addWidget(self._dot_label)
        top_row.addWidget(self._name_label)
        top_row.addStretch(1)
        text_lay.addLayout(top_row)
        text_lay.addWidget(self._watts_label)
        text_lay.addWidget(self._status_label)
        text_lay.addStretch(1)

        lay = self.layout()
        lay.setContentsMargins(14, 10, 14, 10)
        lay.addWidget(text_col)
        lay.addStretch(1)
        lay.addWidget(self._power_btn, alignment=Qt.AlignVCenter)

        self._apply_text_colors()

        # 状态轮询：仅卡片可见时运行（隐藏到托盘/网格重建间隙不打请求）
        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._poll_timer.setInterval(_STATUS_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_once)

    # ---------- 主题观感 ----------

    def _apply_text_colors(self) -> None:
        online = self._online
        self._dot_label.setStyleSheet(
            f"color: {SiColors.THEME if online else SiColors.TEXT_MUTED};"
            f" background: transparent;")
        self._name_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY if online else SiColors.OFFLINE_TEXT};"
            f" background: transparent;")
        self._watts_label.setStyleSheet(
            f"color: {SiColors.THEME if online else SiColors.OFFLINE_SUB};"
            f" background: transparent;")
        self._status_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY if online else SiColors.OFFLINE_SUB};"
            f" background: transparent;")

    def _apply_card_color(self) -> None:
        if not self._online:
            # 离线卡片整体灰置，hover 不提亮（无交互暗示）
            self.style_data.background_color = QColor(SiColors.OFFLINE_CARD)
        else:
            self.style_data.background_color = QColor(
                SiColors.CARD_HOVER if self._hovered else SiColors.CARD)
        self.update()

    # ---------- 状态轮询 ----------

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        if not self._poll_timer.isActive():
            self._poll_timer.start()
            self._poll_once()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._poll_timer.stop()
        super().hideEvent(event)

    def _poll_once(self) -> None:
        if self._poll_in_flight:
            return
        self._poll_in_flight = True
        self._jobs.submit(
            self._service.cuktech_status,
            on_success=self._on_status_loaded,
            on_error=self._on_status_failed,
        )

    def _on_status_loaded(self, status: dict) -> None:
        self._poll_in_flight = False
        if not shiboken6.isValid(self):
            return
        self._status = status
        self._render_status()

    def push_port_status(self, payload: dict) -> None:
        """SSE port_update 推送注入点：单口实时数据（约 1 秒一条）。

        payload 结构见 cuktech-api.md §8：{"type":"port_update",
        "port_id":int, "port":"c1", "data":{voltage/current/power/active/
        protocol/enabled}}。只更新展示状态，不回写 service 缓存；把
        data 合并进缓存的 _status.ports 后复用 _render_status 渲染路径，
        使推送与轮询的渲染结果天然一致。同时重置轮询计时器，避免推送
        刚到 5s 轮询又重复请求一次。
        """
        data = payload.get("data")
        port_id = payload.get("port_id")
        if not isinstance(data, dict) or not isinstance(port_id, int):
            return
        status = dict(self._status or {})
        ports = dict(status.get("ports") or {})
        ports[str(port_id)] = data
        status["ports"] = ports
        # 推送本身就是设备在线的证据（BLE 网关每秒推端口数据）
        status["connected"] = True
        self._status = status
        self._render_status()
        self._reset_poll_timer()

    def push_status(self, payload: dict) -> None:
        """SSE status 推送注入点：全量状态（BLE 连接/断开/认证变化）。

        payload 结构同 /api/status（外加 "type":"status"），直接整帧
        替换 _status 复用 _render_status；不做字段级合并，避免残缺
        增量覆盖全量快照。connected=false 时渲染路径自动离线灰置。
        """
        status = dict(payload)
        status.pop("type", None)
        self._status = status
        self._render_status()
        self._reset_poll_timer()

    def _reset_poll_timer(self) -> None:
        """推送到达后重置状态轮询计时器（推送活络时轮询近乎静默）。"""
        if self._poll_timer.isActive():
            self._poll_timer.start()

    def _on_status_failed(self, _error: Exception) -> None:
        self._poll_in_flight = False
        if not shiboken6.isValid(self):
            return
        # 服务不可达：按离线灰置展示，等待下一轮轮询自行恢复
        self._status = None
        self._online = False
        self._output_on = None
        self._apply_card_color()
        self._apply_text_colors()
        self._watts_label.setText("-- W")
        self._status_label.setText("充电器服务未连接")
        self._power_btn.set_online(False)
        self._power_btn.set_state(None)

    def set_stream_connected(self, connected: bool) -> None:
        """SSE connected_changed 注入点：事件流建立/断开时的在线点。

        只更新状态点与文案做即时反馈（断开时离线灰置）；功率与端口
        数据仍以 status/port_update 推送与轮询为准。注意 SSE 流断开
        只代表与 BLE 网关的通道断了（内部会自动重连），不代表设备
        蓝牙离线，故不清 _status、不改电源钮状态。
        """
        if connected:
            self._online = True
            self._apply_card_color()
            self._apply_text_colors()
            self._status_label.setText("已连接")
            return
        # 断开：卡片走离线灰置（等待重连或轮询恢复），数据字段清空
        self._online = False
        self._apply_card_color()
        self._apply_text_colors()
        self._watts_label.setText("-- W")
        self._status_label.setText("实时数据已断开")

    def _render_status(self) -> None:
        status = self._status or {}
        connected = status.get("connected") is True
        self._online = connected
        ports: dict = status.get("ports") or {}
        total = 0.0
        active = 0
        for entry in ports.values():
            if not isinstance(entry, dict):
                continue
            total += float(entry.get("power") or 0.0)
            if entry.get("active"):
                active += 1
        settings = status.get("settings") or {}
        if "16" in settings:
            # 位图键存在才更新总开关:首帧 status 未到时(port_update 先
            # 到的窗口)默认值 15 会把"未知"误判成"开",用户基于它点
            # 总开关可能误关全部输出口。缺键保持 None(未知态)。
            try:
                bitmap = int(settings["16"])
            except (TypeError, ValueError):
                bitmap = 15
            self._output_on = bitmap != 0
        elif self._output_on is None:
            self._output_on = False

        self._apply_card_color()
        self._apply_text_colors()
        self._watts_label.setText(_fmt_watts(total))
        if connected:
            self._status_label.setText(f"{active} 口充电中" if active else "待机中")
        else:
            # BLE 断开时服务端端口数据是残留旧值，只提示不展示功率
            self._status_label.setText("蓝牙未连接")
            self._watts_label.setText("-- W")
        self._power_btn.set_online(connected)
        self._power_btn.set_state(self._output_on)

    # ---------- 与通用卡片对齐的接口（主窗口统一调用面） ----------

    @property
    def output_on(self) -> bool:
        """总开关当前状态（来自 PIID 16 位图）；未获取时按关处理。"""
        return bool(self._output_on)

    def set_power_state(self, state: bool | None) -> None:
        """对齐通用卡片接口：主窗口总开关操作完成后回填状态。"""
        if state is None:
            return
        self._output_on = state
        self._power_btn.set_state(state)
        self._power_btn.show()

    def set_metrics(self, _text: str | None) -> None:
        """对齐通用卡片接口：充电器读数由状态轮询自行驱动，忽略温湿度。"""

    def set_icon(self, _path) -> None:
        """对齐通用卡片接口：本卡以状态点代替图标位，忽略图标回填。"""

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._power_btn.set_busy(busy)

    # ---------- hover 与点击 ----------

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hovered = True
        self._apply_card_color()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hovered = False
        self._apply_card_color()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        # 电源钮自己消费点击事件，能走到这里的都是卡片空白区
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self.device.did)
        super().mouseReleaseEvent(event)


# ----------------------------------------------------------------------------
# CuktechPanel —— 详情面板（装进 DeviceDetailDialog）
# ----------------------------------------------------------------------------


class CuktechPanel(QWidget):
    """CUKTECH 充电器详情面板：分区 Tab（实时/历史/统计/设备设置）。

    由 DeviceDetailDialog 经 panel_factory 装载，对外接口对齐
    WorkbenchPanel：show_device(did, online, device) 进入、retheme()
    响应主题切换、push_* 注入 SSE 推送。Tab 导航为主窗口房间筛选
    同款的 themed_tab_button 按钮组 + QStackedWidget（siui 无 Tab
    容器，自绘选中态观感一致）；各页包 QScrollArea 内部滚动。本面板
    声明 PREFERS_LARGE_DIALOG，DeviceDetailDialog 按该特性用大壳
    920×760 装载（通用详情保持 900×640）。

    「实时」页为 web 主视觉布局（对齐上游 index.html 双栏观感）：
    左设备渲染舞台 DeviceStageWidget + 右 PortCardGrid 2×2 端口卡 +
    MiniPhoneShareBar 占比条 + MultiMetricCurve 多指标曲线 +
    ChargeLimitStack 限额堆叠卡组。「设备设置」页场景区为
    SceneButtonRow（点击乐观更新 + 3s 回报保护）、延时关闭区为
    DelayOffQuickCard。

    轮询与推送纪律：5s 状态轮询拉 status + limits + 当前档位曲线
    （只拉当前档，不拉 24h 大图，除非当前档就是它）；历史/统计页走
    「切入该 Tab 时刷新（lazy load）+ 手动刷新按钮」，不进轮询。
    SSE 推送（status/port_update/settings/protocol/quality）由主窗口
    路由到 push_* 注入方法，面板内部把 payload 转喂新组件（stage/grid/
    share 增量或整帧、settings 回报喂场景行与延时快捷卡、quality 喂
    连接质量卡、port_update/status/protocol 同步转发给打开中的端口
    详情弹窗，无弹窗时 protocol 事件忽略——协议开关不做独立 Tab）；
    session_end 经 on_session_end 节流刷新历史/统计页；
    限额数据不在 SSE 里，保持 5s 轮询。所有网络调用经注入的 service
    门面 + jobs 后台线程，回调判活后操作控件。
    """

    # 与 WorkbenchPanel 对齐的信号：DeviceDetailDialog 统一连接用于
    # 温湿度回写卡片；充电器读数不走该通道，本面板从不发射
    metrics_updated = Signal(str, str)

    # 壳尺寸特性：本面板内容高（主视觉 + 曲线 + 限额），声明后
    # DeviceDetailDialog 用大壳 920×760 装载；通用详情面板不声明，
    # 维持 900×640
    PREFERS_LARGE_DIALOG = True

    def __init__(self, service: "MijiaService", jobs: "JobExecutor",
                 device: DeviceInfo, parent=None):
        super().__init__(parent)
        self._service = service
        self._jobs = jobs
        self._device = device
        self._current_did: str | None = device.did
        self._status: dict | None = None
        self._limits: dict | None = None
        self._in_flight = False
        self._manual_refresh = False
        # 供 retheme 重设的构造期内联样式件
        self._section_title_labels: list[QLabel] = []
        self._section_icons: list[tuple[QLabel, str]] = []
        self._hint_labels: list[QLabel] = []
        # 曲线当前档位（小时）；切档经 MultiMetricCurve.range_changed
        # 触发独立拉取，5s 轮询只刷当前档
        self._chart_range = _CHART_DEFAULT_HOURS
        # 场景乐观更新保护截止时刻（time.monotonic 秒）；0=无保护
        self._scene_local_until = 0.0
        # 设备设置页（Tab4）的样式件与提交保护标志
        self._settings_row_labels: list[tuple[QLabel, QLabel]] = []
        self._timeout_commit = False
        self._lang_commit = False
        # Tab 导航态：按钮组与堆叠页一一对应；历史/统计页首次切入
        # 才注入 service 拉取（lazy load），此后每次切入都刷新
        self._tab_buttons: list[SiToggleButtonRefactor] = []
        self._history_loaded = False
        self._stats_loaded = False
        # 主视觉双栏当前排布（True=上下堆叠）；resizeEvent 按宽度切换
        self._hero_stacked = False
        # 限额区折叠态（默认收起只留摘要行）
        self._limits_open = False
        # 打开中的端口详情弹窗（PortCardGrid.port_clicked 打开，关闭时
        # 经 done 回调置 None；SSE 转发点以 shiboken6.isValid 判活）
        self._port_detail: PortDetailDialog | None = None
        # session_end 刷新节流（on_session_end 用）：上次触发时刻
        self._last_session_end_refresh = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)
        root.addWidget(self._build_header())
        root.addWidget(self._build_status_strip())
        root.addWidget(self._build_tab_bar())
        root.addWidget(self._build_pages(), stretch=1)

        # 面板尺寸：DeviceDetailDialog 对 CUKTECH 面板用大壳 920×760
        # （外壳 margins 16×2 + 标题行 + 关闭钮后，可用高度约 660）。
        # 高度自适应封顶 660：完整大壳下与旧固定高观感一致；主窗口
        # 矮、壳收缩时面板随布局压缩（不低于 400），各 Tab 页内容差
        # 经页内 QScrollArea 消化，不会溢出壳外被裁（曾因此「滑不到底」）
        self.setMaximumHeight(_PANEL_HEIGHT)
        self.setMinimumSize(_PANEL_MIN_WIDTH, _PANEL_MIN_HEIGHT)

        self._select_tab(_TAB_REALTIME)
        self._apply_inline_styles()

        # 状态轮询：面板显示期间运行
        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._poll_timer.setInterval(_STATUS_POLL_MS)
        self._poll_timer.timeout.connect(self.refresh_data)

    # ---------- 布局构建 ----------

    def _build_header(self) -> QWidget:
        header = QWidget()
        lay = QHBoxLayout(header)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._dot_label = QLabel("●")
        self._dot_label.setFont(QFont("Microsoft YaHei UI", 10))
        self._status_label = QLabel("正在获取充电器状态…")
        self._status_label.setFont(QFont("Microsoft YaHei UI", 9))
        status_row.addWidget(self._dot_label)
        status_row.addWidget(self._status_label)
        status_row.addStretch(1)
        title_col.addLayout(status_row)

        self._name_label = QLabel(self._device.name or "CUKTECH 充电器")
        self._name_label.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        title_col.addWidget(self._name_label)

        self._fw_label = QLabel("")
        self._fw_label.setFont(QFont("Microsoft YaHei UI", 8))
        title_col.addWidget(self._fw_label)
        lay.addLayout(title_col)
        lay.addStretch(1)

        metric_col = QVBoxLayout()
        metric_col.setSpacing(2)
        align_right = (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._watts_label = QLabel("-- W")
        self._watts_label.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        self._watts_label.setAlignment(align_right)
        self._watts_caption = QLabel("当前总功率")
        self._watts_caption.setFont(QFont("Microsoft YaHei UI", 8))
        self._watts_caption.setAlignment(align_right)
        self._session_label = QLabel("本次会话已充 —")
        self._session_label.setFont(QFont("Microsoft YaHei UI", 9))
        self._session_label.setAlignment(align_right)
        metric_col.addWidget(self._watts_label)
        metric_col.addWidget(self._watts_caption)
        metric_col.addWidget(self._session_label)
        lay.addLayout(metric_col)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setFixedSize(64, 32)
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        self._refresh_btn.clicked.connect(lambda: self.refresh_data(manual=True))
        lay.addWidget(self._refresh_btn, alignment=Qt.AlignVCenter)
        return header

    def _build_status_strip(self) -> QFrame:
        """面板顶部统一状态条：在线绿点 / 离线灰点 + 一句当前数据来源。

        数据判定只读已有的 _status（push_status/push_port_status/轮询
        三条路径都已写入），渲染收敛在 _render_status_strip，不新增
        任何数据流。网关地址展示 service.cuktech 客户端的只读 base_url，
        取不到时整段省略。
        """
        strip = QFrame()
        strip.setObjectName("statusStrip")
        strip.setAttribute(Qt.WA_StyledBackground, True)
        self._status_strip = strip
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(14, 7, 14, 7)
        lay.setSpacing(8)

        self._strip_dot = QLabel("●")
        self._strip_dot.setFont(QFont("Microsoft YaHei UI", 8))
        lay.addWidget(self._strip_dot)

        self._strip_text = QLabel("正在获取充电器状态…")
        self._strip_text.setFont(QFont("Microsoft YaHei UI", 8))
        lay.addWidget(self._strip_text)
        lay.addStretch(1)

        self._strip_gw = QLabel("")
        self._strip_gw.setFont(QFont("Microsoft YaHei UI", 8))
        self._strip_gw.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._strip_gw)
        self._apply_status_strip_style()
        return strip

    def _apply_status_strip_style(self) -> None:
        """状态条观感（retheme 重求值）；点色由 _render_status_strip 按态设。"""
        self._strip_dot.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._strip_text.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
        self._strip_gw.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._status_strip.setStyleSheet(
            f"QFrame#statusStrip {{ background: {SiColors.SURFACE};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 8px; }}")

    def _render_status_strip(self, connected: bool, service_ok: bool) -> None:
        """状态条整帧渲染（_render_status/_render_offline 两条路径汇入）。

        在线：主题绿点 +「实时数据」+ 网关地址；离线（BLE 断开或服务
        不可达）：灰点 + 提示文案（展示最近一帧数据，与主视觉灰置口径
        一致）。网关地址只取一次值，失败路径也保留（离线时更需要知道
        该看哪个网关）。
        """
        gw = ""
        cuktech = getattr(self._service, "cuktech", None)
        if cuktech is not None:
            gw = str(getattr(cuktech, "_base_url", "") or "")
        self._strip_gw.setText(f" · 网关 {gw}" if gw else "")
        if service_ok and connected:
            self._strip_dot.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;")
            self._strip_text.setText("实时数据 · 在线")
        elif service_ok:
            self._strip_dot.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;")
            self._strip_text.setText("充电器离线 · 显示最近数据")
        else:
            self._strip_dot.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;")
            self._strip_text.setText("充电器服务未连接 · 显示最近数据")

    def _build_tab_bar(self) -> QWidget:
        """Tab 导航行：themed_tab_button 按钮组（主窗口房间筛选同款）。"""
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        for index, (text, _icon) in enumerate(_TAB_DEFS):
            btn = themed_tab_button(text)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _, i=index: self._select_tab(i))
            lay.addWidget(btn)
            self._tab_buttons.append(btn)
        lay.addStretch(1)
        return bar

    def _build_pages(self) -> QStackedWidget:
        """四个堆叠页，均包 QScrollArea 内部滚动。"""
        self._pages = QStackedWidget()

        # Tab1 实时：web 主视觉布局（舞台/端口卡/占比条/曲线/限额）
        self._pages.addWidget(self._wrap_scroll(self._build_realtime_page()))

        # Tab2 历史：单会话曲线 + 会话列表（上下布局，曲线在上）
        history_page = QWidget()
        history_lay = QVBoxLayout(history_page)
        history_lay.setContentsMargins(0, 0, 0, 0)
        history_lay.setSpacing(12)
        self._history_widget = SessionHistoryWidget()
        self._history_widget.session_selected.connect(self._on_session_selected)
        self._session_curve = SessionCurveWidget()
        history_lay.addWidget(self._session_curve, stretch=2)
        history_lay.addWidget(self._history_widget, stretch=3)
        self._pages.addWidget(self._wrap_scroll(history_page))

        # Tab3 统计：能量统计
        self._energy_widget = EnergySummaryWidget()
        self._pages.addWidget(self._wrap_scroll(self._energy_widget))

        # Tab4 设备设置
        self._pages.addWidget(self._wrap_scroll(self._build_settings_page()))
        return self._pages

    @staticmethod
    def _wrap_scroll(inner: QWidget) -> QScrollArea:
        """页面包 QScrollArea：整面板固定高度时各页内部滚动。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner.setStyleSheet("background: transparent;")
        scroll.setWidget(inner)
        return scroll

    def _build_realtime_page(self) -> QWidget:
        """Tab1：对齐上游 index.html 的主视觉布局。

        自上而下：舞台+端口卡双栏主视觉行 → 四色功率占比条 → 多指标
        曲线卡 → 充电限额折叠卡组（默认收起）。内容超高经页内
        QScrollArea 纵向滚动；双栏宽度随面板伸缩，行宽不足（<760）
        时 resizeEvent 自动改为上下堆叠，不出现横向滚动。
        """
        container = QWidget()
        content = QVBoxLayout(container)
        content.setContentsMargins(0, 0, 8, 0)
        content.setSpacing(14)

        # 主视觉行：QGridLayout 承载左舞台列与右端口卡列，宽度充足时
        # 双栏（stage:grid ≈ 2:3），不足 _HERO_STACK_WIDTH 时把端口卡
        # 挪到第二行上下堆叠（resizeEvent 里重排，仅几何调整零数据）
        self._hero_grid = QGridLayout()
        self._hero_grid.setContentsMargins(0, 0, 0, 0)
        self._hero_grid.setHorizontalSpacing(14)
        self._hero_grid.setVerticalSpacing(14)
        self._stage = DeviceStageWidget()
        self._stage.setMinimumWidth(_STAGE_MIN_WIDTH)
        # 端口卡网格：点卡片（开关除外）打开端口详情弹窗（_on_port_clicked，
        # 打开期间 SSE 数据由面板转发喂数据）；port_toggle 接端口开关写路径
        self._port_grid = PortCardGrid()
        self._port_grid.port_toggle.connect(self._on_port_toggle)
        self._port_grid.port_clicked.connect(self._on_port_clicked)
        self._port_grid.setMinimumWidth(_PORT_GRID_MIN_WIDTH)
        self._hero_grid.addWidget(self._stage, 0, 0)
        self._hero_grid.addWidget(self._port_grid, 0, 1)
        self._hero_grid.setColumnStretch(0, _HERO_STAGE_STRETCH)
        self._hero_grid.setColumnStretch(1, _HERO_GRID_STRETCH)
        content.addLayout(self._hero_grid)

        # 端口功率占比条：舞台与曲线之间（上游 cardPortShare 同位）
        self._share_bar = MiniPhoneShareBar()
        content.addWidget(self._share_bar)

        content.addWidget(self._build_chart_section())
        content.addWidget(self._build_limits_section())
        content.addStretch(1)
        return container

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        """主视觉双栏重排入口（宽度变化时按阈值切换双栏/堆叠）。"""
        super().resizeEvent(event)
        self._update_hero_layout()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        # 面板最小宽下 resize 可能是无操作（尺寸未变不发 resizeEvent），
        # 首次显示时补一次重排判定
        self._update_hero_layout()
        if not self._poll_timer.isActive():
            self._poll_timer.start()

    def _update_hero_layout(self) -> None:
        """按容器宽重排主视觉行：行宽不足时端口卡移到舞台下方（堆叠）。

        仅改 QGridLayout 单元格位置与列拉伸，不动数据流；两个容器都有
        minimumSizeHint，宽度恢复时自动切回双栏。
        """
        grid = getattr(self, "_hero_grid", None)
        if grid is None:
            return
        content_w = self.width() - 20 - 20 - 8  # root margins + 右侧滚动余量
        stacked = content_w < _HERO_STACK_WIDTH
        if stacked == self._hero_stacked:
            return
        self._hero_stacked = stacked
        if stacked:
            # 舞台固定 400 渲染宽（heightForWidth 换算高）居中，避免
            # 行宽拉伸把设备画到 600+ 高挤占首屏；端口卡占满整行
            self._stage.setFixedSize(
                _STAGE_STACK_WIDTH,
                self._stage.height_for_width(_STAGE_STACK_WIDTH))
            grid.addWidget(self._stage, 0, 0, 1, 2,
                           Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(self._port_grid, 1, 0, 1, 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 0)
        else:
            # 双栏恢复：解除堆叠时的固定尺寸（16777215 = QWIDGETSIZE_MAX，
            # PySide6 未导出该常量），舞台回到比例列随宽伸缩
            self._stage.setMinimumSize(0, 0)
            self._stage.setMaximumSize(16777215, 16777215)
            grid.addWidget(self._stage, 0, 0)
            grid.addWidget(self._port_grid, 0, 1)
            grid.setColumnStretch(0, _HERO_STAGE_STRETCH)
            grid.setColumnStretch(1, _HERO_GRID_STRETCH)

    def _build_chart_section(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("propCard")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(8)
        title = self._build_section_title("功率曲线", "mdi.chart-line")
        # 多指标曲线：时间/指标档位与图例由组件内建；切档发
        # range_changed(hours, interval)，面板经 jobs 拉
        # cuktech_chart(hours, interval) 后 set_chart_data 整包回喂。
        # 组件内建的档位行（时间四档 | 弹簧 | 指标四档，本就同一行）
        # 抽到卡标题行右侧，省一行高度（组件不暴露该行，结构变化时
        # 静默跳过退回组件内旧行为）
        self._curve = MultiMetricCurve()
        self._curve.range_changed.connect(self._on_chart_range_changed)
        self._hoist_curve_bar(title)
        lay.addLayout(title)
        lay.addWidget(self._curve)
        return frame

    def _hoist_curve_bar(self, title: QHBoxLayout) -> None:
        """把 MultiMetricCurve 内建的档位行（时间|指标钮，本就同行）
        抽到卡标题行右侧，曲线卡省一行高度。

        仅几何搬运：从曲线 layout 取第 0 项（QHBoxLayout）挂进标题行，
        不改任何信号接线；组件内部结构变化（第 0 项不是布局）时原样
        放回，观感退回旧行为不崩。
        """
        curve_lay = self._curve.layout()
        if curve_lay is None or curve_lay.count() < 1:
            return
        item = curve_lay.takeAt(0)
        bar = item.layout() if item is not None else None
        if bar is None:
            curve_lay.insertItem(0, item)  # 结构不符：放回原位
            return
        bar.setContentsMargins(0, 0, 0, 0)
        title.addLayout(bar)

    def _hoist_scene_head(self, title: QHBoxLayout) -> None:
        """把 SceneButtonRow 内建的头行（右对齐当前模式名）抽到充电器模式卡
        标题行右侧，卡内省一行且消除「充电器模式」重复文案。

        仅几何搬运：从组件 layout 取第 0 项（QHBoxLayout）挂进标题行，
        不改任何信号接线；组件内部结构变化（第 0 项不是布局）时原样
        放回，观感退回旧行为不崩。
        """
        row_lay = self._scene_row.layout()
        if row_lay is None or row_lay.count() < 1:
            return
        item = row_lay.takeAt(0)
        head = item.layout() if item is not None else None
        if head is None:
            row_lay.insertItem(0, item)  # 结构不符：放回原位
            return
        head.setContentsMargins(0, 0, 0, 0)
        title.addLayout(head)

    def _build_limits_section(self) -> QFrame:
        """充电限额卡：标题行带折叠钮，默认收起只留进度摘要。

        桌面等价于上游「堆叠一沓省空间」：收起时只显示展开钮 + 活跃
        限额摘要（_render_limits 更新）；展开后显示说明文字与
        ChargeLimitStack 四卡。折叠只切换可见性（零数据影响）。
        """
        frame = QFrame()
        frame.setObjectName("propCard")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(8)

        title = self._build_section_title("充电限额",
                                          "mdi.battery-charging-medium")
        self._limit_summary = QLabel("—")
        self._limit_summary.setFont(QFont("Microsoft YaHei UI", 8))
        self._limit_summary.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        title.addWidget(self._limit_summary)

        self._limits_toggle = QPushButton("展开 ▾")
        self._limits_toggle.setCursor(Qt.PointingHandCursor)
        self._limits_toggle.setFixedHeight(26)
        self._limits_toggle.clicked.connect(self._toggle_limits_section)
        title.addWidget(self._limits_toggle)
        lay.addLayout(title)

        self._limit_hint = QLabel(
            "设置端口充电量上限（Wh，充电器输出口径），到量自动关断该端口；"
            "0 为不限（「关闭」按钮）")
        self._limit_hint.setFont(QFont("Microsoft YaHei UI", 8))
        # 长说明行：面板最小宽下也允许换行，避免底部被裁半
        self._limit_hint.setWordWrap(True)
        self._hint_labels.append(self._limit_hint)
        lay.addWidget(self._limit_hint)
        # 限额堆叠卡组：组件只发 limit_commit(port, wh, mode) /
        # invalid_input(msg) 信号，写路径与 Toast 由面板承担
        self._limit_stack = ChargeLimitStack()
        self._limit_stack.limit_commit.connect(self._on_limit_commit)
        self._limit_stack.invalid_input.connect(self._on_limit_invalid)
        lay.addWidget(self._limit_stack)
        self._apply_limits_collapsed()
        return frame

    def _toggle_limits_section(self) -> None:
        """限额折叠钮点击：切换展开态并同步按钮文案。"""
        self._limits_open = not self._limits_open
        self._apply_limits_collapsed()

    def _apply_limits_collapsed(self) -> None:
        """按折叠态重设限额卡可见元素与按钮文案。"""
        self._limit_hint.setVisible(self._limits_open)
        self._limit_stack.setVisible(self._limits_open)
        self._limits_toggle.setText("收起 ▴" if self._limits_open else "展开 ▾")

    def _select_tab(self, index: int) -> None:
        """切换 Tab：同步按钮选中态与堆叠页；切入历史/统计页时刷新。"""
        if 0 <= index < len(self._tab_buttons):
            for i, btn in enumerate(self._tab_buttons):
                if btn.isChecked() != (i == index):
                    btn.setChecked(i == index)
        self._pages.setCurrentIndex(index)
        if index == _TAB_HISTORY:
            self._ensure_history_loaded()
        elif index == _TAB_STATS:
            self._ensure_stats_loaded()

    def _ensure_history_loaded(self) -> None:
        """历史页 lazy load：首次注入 service 即拉取，此后仅刷新。"""
        if not self._history_loaded:
            self._history_loaded = True
            self._history_widget.set_service(self._service, self._jobs)
        else:
            self._history_widget.refresh()

    def _ensure_stats_loaded(self) -> None:
        """统计页 lazy load：语义同历史页。"""
        if not self._stats_loaded:
            self._stats_loaded = True
            self._energy_widget.set_service(self._service, self._jobs)
        else:
            self._energy_widget.refresh()

    def _on_session_selected(self, session_id: int) -> None:
        """历史页会话行点击：加载单会话点级曲线并回填电压/电流摘要。"""
        summary = self._history_widget.current_session_summary(session_id)
        self._session_curve.set_summary(
            summary.get("avg_voltage"), summary.get("avg_current"))
        # 曲线标题旁标明充电时间区间（start → end，仿上游会话详情）
        self._session_curve.set_period(
            summary.get("start_time"), summary.get("end_time"))
        self._session_curve.show_session(session_id, self._service, self._jobs)

    # ---------- 设备设置页（Tab4） ----------

    def _build_settings_page(self) -> QWidget:
        """设备设置页：场景/息屏/延时关闭/开关组/语言（propCard 卡分区）。"""
        page = QWidget()
        content = QVBoxLayout(page)
        # 底部 16px：滚动到底时最后一张卡（连接质量）不贴视口下缘，
        # 圆角卡完整可见
        content.setContentsMargins(0, 0, 8, 16)
        content.setSpacing(14)

        # -- 充电器模式卡（PIID 5）：图标按钮排替代旧下拉 --
        scene_card = QFrame()
        scene_card.setObjectName("propCard")
        scene_card.setAttribute(Qt.WA_StyledBackground, True)
        scene_lay = QVBoxLayout(scene_card)
        scene_lay.setContentsMargins(20, 14, 20, 14)
        scene_lay.setSpacing(8)
        scene_title = self._build_section_title("充电器模式", "mdi.theme-light-dark")
        # 场景图标排：点击只发 scene_selected（组件内乐观高亮），提交
        # 与 Toast 由面板承担；回报回填经 set_scene_ui + 3s 乐观保护。
        # 组件内建头行（右对齐当前模式名）抽到标题行右侧，得到
        # 「⚡充电器模式 …… 当前模式名」单行结构（结构变化时静默退回）
        self._scene_row = SceneButtonRow()
        self._scene_row.scene_selected.connect(self._on_scene_selected)
        self._hoist_scene_head(scene_title)
        scene_lay.addLayout(scene_title)
        scene_lay.addWidget(self._scene_row)
        content.addWidget(scene_card)

        # -- 屏幕与语言卡（PIID 6/13/19/20）--
        screen_card = QFrame()
        screen_card.setObjectName("propCard")
        screen_card.setAttribute(Qt.WA_StyledBackground, True)
        screen_lay = QVBoxLayout(screen_card)
        screen_lay.setContentsMargins(20, 14, 20, 14)
        screen_lay.setSpacing(10)
        screen_lay.addLayout(self._build_section_title("屏幕与语言", "mdi.cellphone-text"))
        self._timeout_combo = themed_combo(
            [label for _v, label in _SCREEN_TIMEOUT_OPTIONS])
        self._timeout_combo.addItem("—")  # 占位项
        self._timeout_commit = False
        self._timeout_combo.currentIndexChanged.connect(self._on_timeout_changed)
        self._timeout_combo.setCurrentIndex(len(_SCREEN_TIMEOUT_OPTIONS))
        self._add_setting_row(screen_lay, "息屏时间",
                              "空闲一段时间后屏幕自动熄灭", self._timeout_combo)
        self._idle_switch = themed_switch()
        self._idle_switch.toggled.connect(
            lambda on, s=self._idle_switch: self._on_bool_setting(
                "idle_screen_off", on, s))
        self._add_setting_row(screen_lay, "空闲息屏",
                              "空闲时自动息屏（PIID 19）", self._idle_switch)
        self._lock_switch = themed_switch()
        self._lock_switch.toggled.connect(
            lambda on, s=self._lock_switch: self._on_bool_setting(
                "screen_lock", on, s))
        self._add_setting_row(screen_lay, "屏幕方向锁",
                              "锁定屏幕方向不随摆放旋转（PIID 20）", self._lock_switch)
        self._lang_combo = themed_combo([label for _v, label in _LANGUAGE_OPTIONS])
        self._lang_combo.addItem("—")  # 占位项
        self._lang_commit = False
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self._lang_combo.setCurrentIndex(len(_LANGUAGE_OPTIONS))
        self._add_setting_row(screen_lay, "设备语言",
                              "充电器屏幕显示语言（PIID 13）", self._lang_combo)
        content.addWidget(screen_card)

        # -- USB-A 涓流卡（PIID 15）--
        trickle_card = QFrame()
        trickle_card.setObjectName("propCard")
        trickle_card.setAttribute(Qt.WA_StyledBackground, True)
        trickle_lay = QVBoxLayout(trickle_card)
        trickle_lay.setContentsMargins(20, 14, 20, 14)
        trickle_lay.setSpacing(10)
        trickle_lay.addLayout(self._build_section_title("USB-A", "mdi.usb-port"))
        self._trickle_switch = themed_switch()
        self._trickle_switch.toggled.connect(
            lambda on, s=self._trickle_switch: self._on_bool_setting(
                "usb_a_trickle", on, s))
        self._add_setting_row(trickle_lay, "小电流（涓流）模式",
                              "为小电流设备（手环/耳机等）保持慢充（PIID 15）",
                              self._trickle_switch)
        content.addWidget(trickle_card)

        # -- 延时关闭卡（PIID 8-12）：快捷档卡片替代旧数字输入行 --
        delay_card = QFrame()
        delay_card.setObjectName("propCard")
        delay_card.setAttribute(Qt.WA_StyledBackground, True)
        delay_lay = QVBoxLayout(delay_card)
        delay_lay.setContentsMargins(20, 14, 20, 14)
        delay_lay.setSpacing(8)
        delay_lay.addLayout(self._build_section_title("延时关闭", "mdi.timer"))
        self._delay_hint = QLabel(
            "到设定时间后自动关闭该端口输出（点击分钟数立即下发，「清除」=取消）")
        self._delay_hint.setFont(QFont("Microsoft YaHei UI", 8))
        # 长说明行：面板最小宽下也允许换行，避免底部被裁半（真机截图
        # 见该行下半截被裁切）
        self._delay_hint.setWordWrap(True)
        self._hint_labels.append(self._delay_hint)
        delay_lay.addWidget(self._delay_hint)
        # 延时关闭快捷卡：组件只发 delay_commit(port, minutes)（0=清除），
        # pending 防抖由组件内建，提交与 Toast 由面板承担
        self._delay_card = DelayOffQuickCard()
        self._delay_card.delay_commit.connect(self._on_delay_commit)
        delay_lay.addWidget(self._delay_card)
        content.addWidget(delay_card)

        # -- 连接质量卡（SSE quality 事件直喂，纯展示零请求）--
        # 组件零请求无懒加载负担，构造期直接建好；数据经 push_quality
        # 注入保存在组件内，切入设置 Tab 时自然可见最近一帧
        quality_card_frame = QFrame()
        quality_card_frame.setObjectName("propCard")
        quality_card_frame.setAttribute(Qt.WA_StyledBackground, True)
        quality_lay = QVBoxLayout(quality_card_frame)
        quality_lay.setContentsMargins(20, 14, 20, 14)
        quality_lay.setSpacing(8)
        quality_lay.addLayout(
            self._build_section_title("连接质量", "mdi.access-point-network"))
        self._quality_card = ConnectionQualityCard()
        quality_lay.addWidget(self._quality_card)
        content.addWidget(quality_card_frame)
        content.addStretch(1)
        return page

    def _add_setting_row(self, lay: QVBoxLayout, title: str, hint: str,
                         editor: QWidget) -> None:
        """设置行：标题 + 灰字说明 + 右侧编辑器（下拉或开关）。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(title)
        name.setFont(QFont("Microsoft YaHei UI", 10))
        hint_label = QLabel(hint)
        hint_label.setFont(QFont("Microsoft YaHei UI", 8))
        # 说明行与卡顶长说明同款纪律：允许换行避免窄宽下被裁
        hint_label.setWordWrap(True)
        col.addWidget(name)
        col.addWidget(hint_label)
        row.addLayout(col)
        row.addStretch(1)
        row.addWidget(editor, alignment=Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(row)
        self._settings_row_labels.append((name, hint_label))

    def _combo_index_for(self, value, options: list[tuple[int, str]]) -> int:
        """选项值 -> 下拉 index；值未知（None/越界）返回占位项 index。"""
        for index, (option_value, _label) in enumerate(options):
            if value is not None and option_value == value:
                return index
        return len(options)  # 占位项「—」

    @staticmethod
    def _combo_bool_index(options: list[tuple[bool, str]], value) -> int:
        """布尔下拉：value 是 True/False 才定位，未知回占位项。"""
        for index, (option_value, _label) in enumerate(options):
            if value is True or value is False:
                if option_value == value:
                    return index
        return len(options)

    # ---------- 设置提交（jobs.submit，成功 Toast「已生效」） ----------

    def _submit_setting(self, label: str, call, on_done=None) -> None:
        """提交一项设置：经 jobs 后台执行 call()，成功/失败中文 Toast。"""
        self._jobs.submit(
            call,
            on_success=lambda _, l=label, cb=on_done: self._on_setting_done(l, cb),
            on_error=lambda e, l=label: self._on_setting_failed(l, e),
        )

    def _on_setting_done(self, label: str, callback=None) -> None:
        if not shiboken6.isValid(self):
            return
        Toast.info(self, f"{label}已生效", 2000)
        if callback is not None:
            callback()
        self.refresh_data()

    def _on_setting_failed(self, label: str, error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        Toast.info(self, f"设置{label}失败：{error}", 3000)

    def _on_scene_selected(self, mode: int) -> None:
        """场景图标排点击：乐观保护期内回报不覆盖 + 提交 cuktech_set_scene。

        仿上游 setScene 协议：点击即乐观高亮（组件内已完成）并进入
        3s 保护期（轮询/SSE settings 回报不回滚，舞台徽标同步乐观值）；
        成功后由轮询回报真实值自然落定。
        """
        self._scene_local_until = time.monotonic() + _SCENE_LOCAL_GUARD_S
        self._stage.set_scene(mode)  # 舞台徽标乐观跟随
        label = _SCENE_NAMES.get(mode, f"模式{mode}")
        self._submit_setting(
            f"充电器模式（{label}）", lambda m=mode: self._service.cuktech_set_scene(m))

    def _on_timeout_changed(self, index: int) -> None:
        """息屏时间下拉切换：提交 cuktech_set_screen_timeout(1-5)。"""
        if self._timeout_commit or not 0 <= index < len(_SCREEN_TIMEOUT_OPTIONS):
            return
        value = _SCREEN_TIMEOUT_OPTIONS[index][0]
        label = f"息屏时间（{_SCREEN_TIMEOUT_OPTIONS[index][1]}）"
        self._submit_setting(
            label, lambda v=value: self._service.cuktech_set_screen_timeout(v))

    def _on_language_changed(self, index: int) -> None:
        """设备语言下拉切换：提交 cuktech_set_device_language(中文=英文)。"""
        if self._lang_commit or not 0 <= index < len(_LANGUAGE_OPTIONS):
            return
        chinese = _LANGUAGE_OPTIONS[index][0]
        label = f"设备语言（{_LANGUAGE_OPTIONS[index][1]}）"
        self._submit_setting(
            label, lambda c=chinese: self._service.cuktech_set_device_language(c))

    def _on_bool_setting(self, kind: str, on: bool, switch) -> None:
        """开关组切换：USB-A 涓流/空闲息屏/屏幕方向锁，失败回滚开关。"""
        setters = {
            "usb_a_trickle": ("USB-A 小电流模式",
                              self._service.cuktech_set_usb_a_trickle),
            "idle_screen_off": ("空闲息屏",
                                self._service.cuktech_set_idle_screen_off),
            "screen_lock": ("屏幕方向锁",
                            self._service.cuktech_set_screen_lock),
        }
        entry = setters.get(kind)
        if entry is None:
            return
        label, setter = entry
        switch.setEnabled(False)
        self._jobs.submit(
            lambda: setter(on),
            on_success=lambda _, l=label, s=switch, o=on:
                self._on_bool_done(l, s, o),
            on_error=lambda e, l=label, s=switch, o=on:
                self._on_bool_failed(l, s, o, e),
        )

    def _on_bool_done(self, label: str, switch, on: bool) -> None:
        if not shiboken6.isValid(self):
            return
        switch.setEnabled(True)
        Toast.info(self, f"{label}已生效", 2000)
        self.refresh_data()

    def _on_bool_failed(self, label: str, switch, on: bool,
                        error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        switch.setEnabled(True)
        # 失败回弹开关视觉状态，等下一次轮询以真实值覆盖
        self._sync_switch(switch, not on)
        Toast.info(self, f"设置{label}失败：{error}", 3000)

    # ---------- 端口开关（PortCardGrid.port_toggle 写路径） ----------

    def _on_port_toggle(self, port: int, on: bool) -> None:
        """端口卡开关切换：提交 cuktech_set_port(port, on)，成功重拉状态。"""
        card = self._port_grid._cards.get(port)
        if card is not None:
            card["switch"].setEnabled(False)
        self._jobs.submit(
            lambda: self._service.cuktech_set_port(port, on),
            on_success=lambda _, p=port, o=on: self._on_port_toggle_done(p, o),
            on_error=lambda e, p=port, o=on: self._on_port_toggle_failed(p, o, e),
        )

    def _on_port_toggle_done(self, port: int, on: bool) -> None:
        if not shiboken6.isValid(self):
            return
        card = self._port_grid._cards.get(port)
        if card is not None:
            card["switch"].setEnabled(True)
        Toast.info(self, f"已{'打开' if on else '关闭'} {_port_name(port)}", 2000)
        self.refresh_data()

    def _on_port_toggle_failed(self, port: int, on: bool,
                               error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        card = self._port_grid._cards.get(port)
        if card is not None:
            card["switch"].setEnabled(True)
        # 失败回弹开关视觉状态（按最近一帧已知状态整帧重渲），等下一
        # 次轮询以真实值覆盖
        self._port_grid.update_state(self._status or {})
        Toast.info(self, f"切换 {_port_name(port)} 失败：{error}", 3000)

    # ---------- 充电限额（ChargeLimitStack.limit_commit 写路径） ----------

    def _on_limit_invalid(self, message: str) -> None:
        """客户端校验拦截（空白/NaN/负数/越界）：Toast 提示，不发请求。"""
        Toast.info(self, message, 2500)

    def _on_limit_commit(self, port: int, wh: float, mode: str) -> None:
        """限额提交：cuktech_set_charge_limit(port, wh, mode)，成功 Toast
        「已生效」+ 立即重拉 limits；失败由 commit_result 恢复原值。"""
        label = f"{_port_name(port)}充电限额"
        self._jobs.submit(
            lambda p=port, w=wh, m=mode: self._service.cuktech_set_charge_limit(
                p, w, m),
            on_success=lambda _, p=port, l=label: self._on_limit_done(p, l),
            on_error=lambda e, p=port, l=label: self._on_limit_failed(p, l, e),
        )

    def _on_limit_done(self, port: int, label: str) -> None:
        if not shiboken6.isValid(self):
            return
        self._limit_stack.commit_result(port, True)
        Toast.info(self, f"{label}已生效", 2500)
        self.refresh_data()

    def _on_limit_failed(self, port: int, label: str, error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        self._limit_stack.commit_result(port, False)
        Toast.info(self, f"设置{label}失败：{error}", 3000)

    # ---------- 延时关闭（DelayOffQuickCard.delay_commit 写路径） ----------

    def _on_delay_commit(self, port: int, minutes: int) -> None:
        """延时快捷档提交：cuktech_set_delay_off(port, minutes)（0=清除）。

        组件在提交时已进入行级 pending（「设置中…」+ 按钮禁点），成功
        /失败经 commit_result 解除；设备上报确认由 update_settings 路径
        自然落定，10s 超时由组件兜底。
        """
        label = f"{_port_name(port)}延时关闭"
        self._jobs.submit(
            lambda p=port, m=minutes: self._service.cuktech_set_delay_off(p, m),
            on_success=lambda _, p=port, l=label: self._on_delay_done(p, l),
            on_error=lambda e, p=port, l=label: self._on_delay_failed(p, l, e),
        )

    def _on_delay_done(self, port: int, label: str) -> None:
        if not shiboken6.isValid(self):
            return
        self._delay_card.commit_result(port, True)
        Toast.info(self, f"{label}已生效", 2000)
        self.refresh_data()

    def _on_delay_failed(self, port: int, label: str, error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        self._delay_card.commit_result(port, False)
        Toast.info(self, f"设置{label}失败：{error}", 3000)

    # ---------- 曲线档位切换（MultiMetricCurve.range_changed 写路径） ----------

    def _on_chart_range_changed(self, hours: float, interval: int) -> None:
        """切时间档：记录当前档并单独拉取该档曲线（整包回喂组件）。"""
        self._chart_range = hours
        self._jobs.submit(
            lambda: self._service.cuktech_chart(hours=hours, interval=interval),
            on_success=self._on_range_chart,
            on_error=self._on_range_chart_error,
        )

    def _on_range_chart(self, chart: dict) -> None:
        if not shiboken6.isValid(self):
            return
        if chart:
            self._curve.set_chart_data(chart)

    def _on_range_chart_error(self, error: Exception) -> None:
        if not shiboken6.isValid(self):
            return
        Toast.info(self, f"曲线数据拉取失败：{error}", 3000)

    # ---------- 设置值回填（轮询驱动，焦点保护） ----------

    def _apply_settings(self, settings: dict) -> None:
        """把 /api/status 的 settings（PIID -> int）分发到设置控件。

        回填纪律（对齐限额/延时组件内的焦点保护）：用户正在编辑的控件
        不覆盖——下拉在焦点/弹出列表时跳过，开关只在切换导致 busy（禁用）
        期间由 enabled 守卫；场景行在乐观保护期（点击后 3s）内不回填，
        保护期后以设备回报值落定。占位项「—」表示设备尚未上报该值。
        """
        if not isinstance(settings, dict):
            return

        def piid(key: str):
            try:
                return int(settings.get(key))
            except (TypeError, ValueError):
                return None

        # 充电器模式（5）：乐观保护期内不覆盖（上游 markLocal/isRecent 同款）
        value = piid("5")
        if value in (1, 2, 3, 4) and time.monotonic() >= self._scene_local_until:
            self._scene_row.set_scene_ui(value)
        # 息屏时间（6）
        value = piid("6")
        index = self._combo_index_for(value, _SCREEN_TIMEOUT_OPTIONS)
        if not self._timeout_combo.hasFocus():
            self._timeout_commit = True
            self._timeout_combo.setCurrentIndex(index)
            self._timeout_commit = False
        # 空闲息屏（19）/屏幕方向锁（20）/USB-A 涓流（15）：非 busy 才回填
        for key, piid_no, switch in (
                ("idle_screen_off", "19", self._idle_switch),
                ("screen_lock", "20", self._lock_switch),
                ("usb_a_trickle", "15", self._trickle_switch)):
            raw = piid(piid_no)
            if raw in (0, 1) and switch.isEnabled() and not switch.hasFocus():
                self._sync_switch(switch, raw == 1)
        # 设备语言（13）
        value = piid("13")
        if value in (0, 1):
            index = self._combo_bool_index(_LANGUAGE_OPTIONS, value == 1)
            if not self._lang_combo.hasFocus():
                self._lang_commit = True
                self._lang_combo.setCurrentIndex(index)
                self._lang_commit = False
        # 延时关闭（9-12）：整帧喂快捷卡（行级 pending 由组件内部跳过，
        # 设备上报值与提交值一致时自动解除）
        self._delay_card.update_settings(settings)

    def _build_section_title(self, text: str, icon_name: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        icon_label = QLabel()
        icon_label.setFixedSize(16, 16)
        icon_label.setPixmap(qta.icon(icon_name, color=SiColors.THEME).pixmap(16, 16))
        label = QLabel(text)
        label.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold))
        row.addWidget(icon_label)
        row.addWidget(label)
        row.addStretch(1)
        self._section_icons.append((icon_label, icon_name))
        self._section_title_labels.append(label)
        return row

    def _apply_inline_styles(self) -> None:
        """重设构造期求值的内联样式（主题切换时由 retheme 调用）。"""
        self._dot_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._status_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
        self._name_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._fw_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._watts_label.setStyleSheet(
            f"color: {SiColors.THEME}; background: transparent;")
        self._watts_caption.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._session_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
        self._refresh_btn.setStyleSheet(
            f"QPushButton {{ background: {SiColors.SURFACE}; border: none;"
            f" border-radius: 8px; color: {SiColors.TEXT_PRIMARY}; font-size: 9pt; }}"
            f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}")
        for label in self._section_title_labels:
            label.setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        for icon_label, icon_name in self._section_icons:
            icon_label.setPixmap(
                qta.icon(icon_name, color=SiColors.THEME).pixmap(16, 16))
        for hint in self._hint_labels:
            hint.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._limit_summary.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._limits_toggle.setStyleSheet(
            f"QPushButton {{ background: {SiColors.SURFACE}; border: none;"
            f" border-radius: 8px; color: {SiColors.TEXT_PRIMARY};"
            f" padding: 2px 10px; font-size: 8pt; }}"
            f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}")
        self._apply_status_strip_style()
        # 设备设置页：标题/说明行与下拉弹出列表随主题重设（场景行/
        # 限额堆叠/延时快捷卡各自实现 retheme，由面板 retheme 下发）
        for name, hint_label in self._settings_row_labels:
            name.setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
            hint_label.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        apply_combo_qss(self._timeout_combo)
        apply_combo_qss(self._lang_combo)

    # ---------- 生命周期 ----------

    def show_device(self, did: str, online: bool = True,
                    device: DeviceInfo | None = None) -> None:
        """进入面板（与 WorkbenchPanel.show_device 同签名，供对话框装载）。

        online 形参仅为签名对齐：充电器的真实连接状态以 status 的
        connected 字段为准，云端注册的 online 值不作为展示依据。
        """
        self._current_did = did
        if device is not None:
            self._device = device
            self._name_label.setText(device.name or "CUKTECH 充电器")
        if not self._poll_timer.isActive():
            self._poll_timer.start()
        self.refresh_data()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        # 面板隐藏（浮层关闭/容器重建）：端口详情弹窗随面板一起关，
        # 避免悬空弹窗残留在被销毁面板之上
        detail = self._port_detail
        if detail is not None and shiboken6.isValid(detail):
            detail.close()
        self._poll_timer.stop()
        super().hideEvent(event)

    def retheme(self) -> None:
        """主题切换：重设内联样式并级联全部新组件与子面板的 retheme。

        曲线/舞台取色在绘制时动态读取（retheme 补一次重绘）；历史/
        统计子面板各自实现 retheme，一并下发。
        """
        self._apply_inline_styles()
        self._stage.retheme()
        self._port_grid.retheme()
        self._share_bar.retheme()
        self._curve.retheme()
        self._limit_stack.retheme()
        self._delay_card.retheme()
        self._scene_row.retheme()
        self._history_widget.retheme()
        self._energy_widget.retheme()
        self._quality_card.retheme()
        # 端口详情弹窗打开中：弹窗自身 retheme 级联（判活防悬空引用）
        detail = self._port_detail
        if detail is not None and shiboken6.isValid(detail):
            detail.retheme()

    # ---------- 数据获取 ----------

    def refresh_data(self, manual: bool = False) -> None:
        """拉取状态 + 限额 + 当前档位曲线（合并为一次任务，串行队列只排一单）。"""
        if self._in_flight:
            return
        self._in_flight = True
        self._manual_refresh = manual
        self._jobs.submit(
            self._fetch_all,
            on_success=self._on_data,
            on_error=self._on_data_error,
        )

    def _fetch_all(self) -> tuple[dict, dict, dict | None, float]:
        """status + limits + 曲线一趟拉齐；曲线只拉当前档位。

        返回值附带发起拉取时的档位（requested），渲染前与当前档位对账：
        档位在队列等待期间被切换时丢弃旧档数据，避免旧图覆盖新档。
        """
        requested = self._chart_range
        status = self._service.cuktech_status()
        limits = self._service.cuktech_charge_limits()
        try:
            chart = self._service.cuktech_chart(
                hours=requested,
                interval=_CHART_RANGE_INTERVALS.get(requested, 20))
        except Exception:
            # 图表失败不拖累状态与限额展示（曲线区显示「暂无数据」）
            chart = None
        return status, limits, chart, requested

    def _on_data(self, data: tuple) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        status, limits, chart, requested = data
        self._status = status
        self._render_status()
        self._render_limits(limits)
        if chart is not None and requested == self._chart_range:
            self._curve.set_chart_data(chart)

    def _on_data_error(self, error: Exception) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        self._status = None
        self._limits = None
        self._render_offline()
        # 后台轮询失败不弹 Toast 打扰；仅用户主动刷新时提示
        if self._manual_refresh:
            self._manual_refresh = False
            Toast.info(self, f"刷新失败：{error}", 3000)

    # ---------- SSE 推送注入（推送优先，轮询兜底） ----------

    def push_port_status(self, payload: dict) -> None:
        """SSE port_update 注入：单口实时数据增量直喂舞台与端口卡。

        payload 结构见 cuktech-api.md §8：{"type":"port_update",
        "port_id":int, "port":"c1", "data":{...}}。只更新 UI 展示状态，
        不回写 service 缓存：data 合并进 _status 后经 update_port 只刷
        对应舞台功率条与端口卡（上游 updatePortDOM 思路），占比条按
        合并后的整帧重算（组件无单口增量接口）。同时重置状态轮询计时
        器，推送刚到时不再叠加一次 5s 轮询。曲线不走推送，保持轮询
        拉取。限额数据不在 SSE 里，仍走轮询。
        """
        data = payload.get("data")
        port_id = payload.get("port_id")
        if not isinstance(data, dict) or not isinstance(port_id, int):
            return
        status = dict(self._status or {})
        ports = dict(status.get("ports") or {})
        ports[str(port_id)] = data
        status["ports"] = ports
        # 推送本身即设备在线证据（BLE 网关每秒推端口数据）
        status["connected"] = True
        self._status = status
        self._stage.update_port(port_id, data)
        self._port_grid.update_port(port_id, data)
        self._share_bar.update_state(status)
        self._update_total_watts()
        # 头部状态点与整帧渲染同语义：推送到达即在线
        self._dot_label.setStyleSheet(
            f"color: {SiColors.THEME}; background: transparent;")
        self._status_label.setText("已连接")
        # 端口详情弹窗打开中：单口数据同步喂入（弹窗内部按端口过滤）
        detail = self._port_detail
        if detail is not None and shiboken6.isValid(detail):
            detail.push_port_sample(port_id, data)
        # 注意:此处不复位轮询计时器。port_update 充电中约 1s 一条,而
        # 限额/会话累计/曲线只由 5s 轮询拉取(不走 SSE),每条推送都
        # 重置计时器会让它永远到期不了——充电期间头部 Wh、限额进度、
        # 曲线全部冻结。push_status/push_settings(全量帧)才重置。

    def push_status(self, payload: dict) -> None:
        """SSE status 注入：全量状态整帧替换后复用 _render_status。

        不做字段级合并（增量可能残缺，以整帧快照覆盖最稳）；
        connected=false 时渲染路径自动灰置；整帧同步舞台/端口卡/
        占比条/场景行/延时快捷卡全部主视觉与设置件。
        """
        status = dict(payload)
        status.pop("type", None)
        self._status = status
        self._render_status()
        # 端口详情弹窗打开中：整帧里的本口数据兜底喂入（弹窗内部按
        # 端口过滤；status 推送是全量快照，本口条目等价一次单口样本）
        detail = self._port_detail
        if detail is not None and shiboken6.isValid(detail):
            entry = (status.get("ports") or {}).get(str(detail._port))
            if isinstance(entry, dict):
                detail.push_port_sample(detail._port, entry)
        self._reset_poll_timer()

    def push_settings(self, payload: dict) -> None:
        """SSE settings 注入：设置（PIID 位图等）变化，同步端口开关位。

        设置推送不含 ports：只把 settings 合并进 _status，渲染沿用
        已知的端口数据。端口卡的 enabled 开关以 PIID 16 位图为唯一
        事实来源（契约：bit0=C1…bit3=A；推送的 data.enabled 可能滞后
        于位图写入），位图非 0 位即视为该口启用。settings 同帧喂场景
        行（乐观保护期内跳过）与延时快捷卡；限额数据服务端不经
        settings 事件推送，仍走面板轮询。
        """
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            return
        status = dict(self._status or {})
        status["settings"] = settings
        try:
            bitmap = int(settings.get("16", 15))
        except (TypeError, ValueError):
            bitmap = 15
        ports = dict(status.get("ports") or {})
        for index in range(4):
            entry = dict(ports.get(str(index + 1)) or {})
            if entry:
                entry["enabled"] = bool(bitmap & (1 << index))
                ports[str(index + 1)] = entry
        status["ports"] = ports
        self._status = status
        self._render_status()
        self._reset_poll_timer()

    def push_protocol(self, payload: dict) -> None:
        """SSE protocol 事件注入（main_window generic_event 分支调用）。

        独立协议 Tab 已移除（单口协议卡收敛进端口详情弹窗）：事件只
        转发给打开中的端口详情弹窗（弹窗内单口协议卡 apply_protocol_event
        按端口过滤消费 switches 字段，非 protocol 事件静默忽略）；无
        弹窗时忽略，协议位图等弹窗下次打开时以 _status 整帧兜底。
        """
        detail = self._port_detail
        if detail is not None and shiboken6.isValid(detail):
            detail.apply_protocol_event(payload)

    def set_stream_connected(self, connected: bool) -> None:
        """SSE connected_changed 注入：事件流建立/断开的头部状态点。

        断开只代表桌面端与 BLE 网关的 SSE 通道中断（内部自动重连），
        不代表设备蓝牙离线，故不清数据；提示文案即时反馈，等重连或
        轮询自行恢复。
        """
        if connected:
            self._dot_label.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;")
            self._status_label.setText("已连接")
            return
        self._dot_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._status_label.setText("实时数据已断开")

    # ---------- 端口详情弹窗（PortCardGrid.port_clicked 打开） ----------

    def _on_port_clicked(self, port: int) -> None:
        """端口卡点击：打开端口详情弹窗（上游 portModal 桌面等价物）。

        构造参数取面板当前 _status 的对应口数据（无 SSE 时的兜底首点）
        与整帧 protocol_switches（弹窗按本口过滤）。弹窗零轮询：打开
        期间的 SSE 数据由面板的 push_port_status/push_status/
        push_protocol 转发喂数；父面板销毁时经 done 回调同步清理引用。
        """
        detail = self._port_detail
        if detail is not None and shiboken6.isValid(detail):
            # 已打开：同口重复点直接置顶，其他口复用单实例换口重开
            detail.close()
            detail.deleteLater()
            self._port_detail = None
        ports = (self._status or {}).get("ports") or {}
        port_data = ports.get(str(port))
        if not isinstance(port_data, dict):
            port_data = {}
        protocol_switches = (self._status or {}).get(
            "protocol_switches") or {}
        dialog = PortDetailDialog(
            self, self._service, self._jobs, port, port_data,
            protocol_switches)
        # QDialog 无 done 信号：done(result) 内部发 finished(result)（Esc/
        # 遮罩 reject 同样汇入），关闭回调统一挂 finished 清引用
        dialog.finished.connect(self._on_port_detail_closed)
        self._port_detail = dialog
        dialog.show()

    def _on_port_detail_closed(self, *_args) -> None:
        """弹窗关闭（finished：done/reject/Esc/×/遮罩全汇入）：清引用。

        面板持引用置 None 后 deleteLater 面板侧残留引用（弹窗退场淡出
        动画期间对象仍存活，动画完成即析构）。
        """
        detail = self._port_detail
        self._port_detail = None
        if detail is not None and shiboken6.isValid(detail):
            detail.deleteLater()

    # ---------- SSE quality / session_end 注入（main_window 分发） ----------

    def push_quality(self, payload: dict) -> None:
        """SSE quality 事件注入（约 5s 一条）：整帧喂连接质量卡。

        组件纯展示零请求，设置 Tab 未切入过也不影响（卡片构造期即建，
        数据保存在组件内，切入后自然可见最近一帧）。
        """
        self._quality_card.apply_quality(payload)

    def on_session_end(self, payload: dict) -> None:
        """SSE session_end 事件注入：刷新历史/统计页（5s 节流）。

        对齐上游（报告 §3.4）：一次拔插会连发多口 session_end，连续
        刷新只会重复排队相同请求，5s 节流窗口内只刷一次（节流时刻只
        在实际发生刷新时推进——未触发刷新的事件不占用窗口）。仅在对应
        页曾加载过（有会话/统计缓存）时触发——未加载页保持 lazy load
        纪律，切入时自然会拉最新。
        """
        now = time.monotonic()
        if now - self._last_session_end_refresh < _SESSION_END_THROTTLE_S:
            return
        refreshed = False
        if self._history_loaded:
            self._history_widget.refresh(manual=False)
            refreshed = True
        if self._stats_loaded:
            self._energy_widget.refresh(manual=False)
            refreshed = True
        if refreshed:
            self._last_session_end_refresh = now

    def _reset_poll_timer(self) -> None:
        """推送到达后重置状态轮询计时器（推送活络时轮询近乎静默）。"""
        if self._poll_timer.isActive():
            self._poll_timer.start()

    # ---------- 渲染 ----------

    def _render_status(self) -> None:
        status = self._status or {}
        connected = status.get("connected") is True
        if connected:
            self._dot_label.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;")
            self._status_label.setText("已连接")
        else:
            self._dot_label.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;")
            self._status_label.setText("蓝牙未连接，读数为最后残留值")
        self._render_status_strip(connected, service_ok=True)
        self._update_total_watts()
        model = str(status.get("device_model") or self._device.model or "")
        fw = str(status.get("firmware_version") or "")
        parts = [p for p in (model, f"固件 {fw}" if fw else "") if p]
        self._fw_label.setText(" · ".join(parts))
        # 主视觉件整帧同步：舞台（含场景徽标）→ 端口卡 → 占比条
        self._render_ports_view(status)
        # protocol_switches 存进 _status 供端口详情弹窗打开时取初态
        # （不再有独立协议 Tab 消费整帧）；设置值（Tab4）同步回填
        self._apply_settings(status.get("settings") or {})
        # 乐观保护期内舞台徽标跟随场景行乐观值（update_state 会按
        # settings["5"] 回写，此处覆盖回乐观值，保护期后自然落定）
        if time.monotonic() < self._scene_local_until:
            self._stage.set_scene(self._scene_row.current_scene())

    def _render_ports_view(self, status: dict) -> None:
        """主视觉件整帧同步（纯展示组件，全部走组件自身渲染路径）。"""
        self._stage.update_state(status)
        self._port_grid.update_state(status)
        self._share_bar.update_state(status)

    def _update_total_watts(self) -> None:
        """头部总功率：Σ ports[*].power（与旧四行布局口径一致，不滤 enabled）。

        BLE 断开时服务端端口数据是残留旧值，头部与专用卡片同口径
        清为「-- W」，避免离线展示假读数。
        """
        status = self._status or {}
        if status.get("connected") is not True:
            self._watts_label.setText("-- W")
            return
        ports = status.get("ports") or {}
        total = 0.0
        for entry in ports.values():
            if isinstance(entry, dict):
                total += float(entry.get("power") or 0.0)
        self._watts_label.setText(_fmt_watts(total))

    def _render_limits(self, limits: dict | None) -> None:
        """限额整帧喂堆叠卡组 + 头部会话累计 + 折叠摘要行。

        摘要行（折叠时的唯一限额信息）取「进行中最接近限额的端口」：
        is_charging 且 wh>0 的口按进度就近排序，进度条 fired 的口加
        「已触发」；无活跃限额显示已启用口数。
        """
        self._limits = limits
        self._limit_stack.update_limits(limits or {})
        entries = (limits or {}).get("limits") or {}
        session_total = 0.0
        active: list[tuple[float, str]] = []   # (进度 0-1, 摘要文案)
        enabled_count = 0
        for port, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            session_total += float(entry.get("session_wh") or 0.0)
            wh = float(entry.get("wh") or 0.0)
            if wh <= 0:
                continue
            enabled_count += 1
            if not entry.get("is_charging"):
                continue
            session = float(entry.get("session_wh") or 0.0)
            ratio = min(session / wh, 1.0)
            text = (f"{_port_name(int(port))} {session:.0f}/{wh:.0f}Wh")
            if entry.get("fired"):
                text += " · 已触发"
            active.append((ratio, text))
        self._session_label.setText(f"本次会话已充 {_fmt_wh(session_total)}")
        # 折叠摘要：进行中限额口 > 已启用口数 > 占位
        if active:
            active.sort(key=lambda item: item[0], reverse=True)
            self._limit_summary.setText(" · ".join(t for _r, t in active[:2])
                                        + (" 等" if len(active) > 2 else ""))
        elif enabled_count:
            self._limit_summary.setText(f"{enabled_count} 口已设限额")
        else:
            self._limit_summary.setText("未设限额")

    def _render_offline(self) -> None:
        self._dot_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._status_label.setText("充电器服务连接失败")
        self._render_status_strip(connected=False, service_ok=False)
        self._watts_label.setText("-- W")
        self._fw_label.setText("")
        self._session_label.setText("本次会话已充 —")
        # 主视觉件清零（空帧渲染为空载/0W/无占比），限额卡组回未启用
        self._render_ports_view({})
        self._limit_stack.update_limits({})

    # ---------- 程序化开关同步（设备设置页开关组回填用） ----------

    @staticmethod
    def _sync_switch(switch, checked: bool) -> None:
        """程序化同步开关状态（setChecked 会发 toggled，必须屏蔽信号）。

        SiSwitchRefactor 的自绘进度与 checked 状态是两套存储，同步补齐。
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
