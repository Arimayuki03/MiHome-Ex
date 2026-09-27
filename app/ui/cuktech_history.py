# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电器「充电会话历史 + 能量统计」UI 模块（纯增量，独立模块）。

数据来源：BLE 网关的会话/能量端点（/api/sessions、/api/energy/stats、
/energy/protocols、/sessions/{id}/points），经注入的 service 门面
（cuktech_sessions / cuktech_session_points / cuktech_energy_stats /
cuktech_energy_protocols）走 JobExecutor 串行队列，一个组件一次刷新
只排一单。回调先以 shiboken6.isValid 判活再操作控件；取色一律
SiColors 动态代理、图标走 qtawesome(mdi.*)、字号沿用 8/9/10/11/15pt
档位；观感对齐 cuktech_panel.py（QFrame#propCard 圆角卡 + 区块标题 +
同风格自绘曲线），主题切换由各组件 retheme() 重设内联样式。

模块分节：
- 常量与格式化辅助（端口名 / Wh / 时长 / 时间戳文案 / 五指标积分）
- _SessionRow —— 单条会话行（整行可点击的 QFrame，发 selected 信号）
- SessionHistoryWidget —— 会话历史（筛选工具栏 + 会话行列表 + 翻页）
- _TabDeck / _MiniBar / _ShareBar / _HourlyBarChart —— 统计卡自绘件
- EnergySummaryWidget —— 能量统计（三子 Tab：按端口/每小时/快充协议）
- _SessionCurve / SessionCurveWidget —— 单会话点级曲线（协议切换虚线
  标注 + 五项指标 + 统计四联块 + 精度下拉 + CSV 导出）
"""

import math
import shiboken6
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import qtawesome as qta
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from app.ui.si_theme import (
    SiColors,
    apply_combo_qss,
    themed_combo,
    themed_tab_button,
)
from app.ui.toast import Toast

if TYPE_CHECKING:
    from app.core.jobs import JobExecutor

# ----------------------------------------------------------------------------
# 常量与格式化辅助
# ----------------------------------------------------------------------------

# 每页条数：/api/sessions 的 limit 上限即 50（超过会被服务端钳到 50）
_PAGE_LIMIT = 50

# 会话行固定高度与行间距（_build_session_row 的 setFixedHeight 同源）
_SESSION_ROW_HEIGHT = 56
_SESSION_ROW_SPACING = 8
# 列表与分页条之间的最小间距（面板统一间距口径 ≥12px）
_PAGER_GAP = 12

# 曲线点数上限（超出等距抽稀），与 cuktech_panel.PowerCurveWidget 同值
_CURVE_MAX_POINTS = 200
_CURVE_HEIGHT = 160
_CURVE_MARGIN_L = 10
_CURVE_MARGIN_R = 10
_CURVE_MARGIN_T = 20  # 顶部留给峰值标注
_CURVE_MARGIN_B = 18  # 底部留给时间轴刻度

# 每小时柱状图 Y 轴：左侧数值带宽度与刻度档数（对齐主曲线观感）
_HOURLY_AXIS_W = 42
_HOURLY_TICKS = 4  # 0 线 + 3 条参考线 = 4 档

# 查询周期 -> 中文标签；week/month 是滚动 7/30 天（非自然周/自然月）
_PERIOD_LABELS: list[tuple[str, str]] = [
    ("today", "今天"),
    ("yesterday", "昨天"),
    ("week", "近7天"),
    ("month", "近30天"),
]

# 端口筛选：None=全部端口（/api/sessions 的 port 查询参数省略）
_PORT_FILTERS: list[tuple[int | None, str]] = [
    (None, "全部端口"),
    (1, "C1"),
    (2, "C2"),
    (3, "C3"),
    (4, "USB-A"),
]

# 端口号(1-4) -> 展示名；/api/sessions 返回的 port 字段是 int 1-4
_PORT_LABELS: dict[int, str] = {1: "C1", 2: "C2", 3: "C3", 4: "USB-A"}

# 四口身份色：抄上游 index.css --port-c1/c2/c3/a（与 cuktech_stage 同源，
# 双主题同值；小字标签不用亮色，身份由圆点/色块承载）
_PORT_COLORS: dict[int, str] = {
    1: "#FF7A00",  # 橙
    2: "#46B4FF",  # 蓝
    3: "#89D8F3",  # 浅蓝
    4: "#FFD24B",  # 黄
}

# 每小时柱状图柱色：抄上游 app.js --chart-bar=#46B4FF
_HOURLY_BAR_COLOR = "#46B4FF"

# 会话详情精度档：下拉文案 -> downsample 点数（0=不降采样，全部点）
_PRECISION_OPTIONS: list[tuple[int, str]] = [
    (0, "全部"),
    (100, "100 点"),
    (200, "200 点"),
    (300, "300 点"),
    (600, "600 点"),
]


def _port_name(port: int) -> str:
    return _PORT_LABELS.get(port, f"P{port}")


def _fmt_wh(value: float) -> str:
    """Wh 文案：37.5 -> "37.5Wh"，100.0 -> "100Wh"（对齐 cuktech_panel）。"""
    return f"{float(value):g}Wh"


def _fmt_watts(value: float) -> str:
    return f"{float(value):.1f}W"


def _fmt_duration(seconds: float) -> str:
    """时长文案：3600 -> "1h 0m"，125 -> "2m"（总秒数直接折算）。"""
    total = max(int(seconds or 0), 0)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _fmt_start_time(ts: float) -> str:
    """开始时间：当天显示 HH:MM，跨天显示 MM-DD HH:MM（本地时区）。"""
    try:
        dt = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return "--"
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def _fmt_period(start_ts, end_ts) -> str:
    """充电时间区间文案「HH:MM → HH:MM」（仿上游 charge_history.js 的
    ``{start} → {end}`` 标题）。跨天会话两端的 %m-%d 前缀会自然带出；
    进行中会话（end 缺失/为 0）显示「HH:MM → 充电中」。"""
    start_text = _fmt_start_time(start_ts)
    try:
        end_f = float(end_ts or 0.0)
    except (TypeError, ValueError):
        end_f = 0.0
    if end_f <= 0:
        return f"{start_text} → 充电中"
    return f"{start_text} → {_fmt_start_time(end_f)}"


def _period_combo(options: list[tuple[str, str]]):
    """周期下拉：选项 + 滚动周期说明 tooltip（week/month 非自然周月）。"""
    combo = themed_combo([])
    for _value, label in options:
        combo.addItem(label)
    combo.setItemData(2, "滚动近 7 天（非自然周）", Qt.ItemDataRole.ToolTipRole)
    combo.setItemData(3, "滚动近 30 天（非自然月）", Qt.ItemDataRole.ToolTipRole)
    combo.setToolTip("近7天/近30天为滚动周期，非自然周/自然月")
    return combo


def _compute_session_metrics(points: list[dict]) -> dict[str, float]:
    """从点列积分/平均计算会话五项指标（仿上游 charge_history.js:138-150）。

    电量 Wh = 梯形积分（相邻点 Δh × 功率均值，点间隔不足视为 0）；
    峰功率 = max(power)；均功率 = 电量 ÷ 总时长（无时长时退化为
    算术平均）；均压/均流 = 功率加权平均（退化点算术平均）。
    无有效点返回全 0。
    """
    rows: list[tuple[float, float, float, float]] = []  # (ts, w, v, a)
    for point in points:
        if not isinstance(point, dict):
            continue
        try:
            ts = float(point.get("timestamp"))
        except (TypeError, ValueError):
            continue
        try:
            w = max(float(point.get("power") or 0.0), 0.0)
        except (TypeError, ValueError):
            w = 0.0
        try:
            v = float(point.get("voltage") or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        try:
            a = float(point.get("current") or 0.0)
        except (TypeError, ValueError):
            a = 0.0
        rows.append((ts, w, v, a))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return {"wh": 0.0, "avg_w": 0.0, "peak_w": 0.0,
                "avg_v": 0.0, "avg_a": 0.0}
    # 梯形积分：Wh = Σ (Δt 小时 × 两点功率均值)
    energy = 0.0
    weighted_v = 0.0
    weighted_a = 0.0
    for (t0, w0, v0, a0), (t1, w1, v1, a1) in zip(rows, rows[1:]):
        dt_hours = (t1 - t0) / 3600.0
        if dt_hours <= 0 or dt_hours > 0.5:
            # 时间倒退或采样断档（>30min 视为不连续）不计能量
            continue
        energy += dt_hours * (w0 + w1) / 2.0
        weighted_v += (v0 + v1) / 2.0 * dt_hours
        weighted_a += (a0 + a1) / 2.0 * dt_hours
    span_hours = (rows[-1][0] - rows[0][0]) / 3600.0
    if span_hours > 0:
        avg_w = energy / span_hours
        avg_v = weighted_v / span_hours if span_hours else 0.0
        avg_a = weighted_a / span_hours if span_hours else 0.0
    else:
        # 单点/同时刻：退化为算术平均
        avg_w = sum(r[1] for r in rows) / len(rows)
        avg_v = sum(r[2] for r in rows) / len(rows)
        avg_a = sum(r[3] for r in rows) / len(rows)
    return {"wh": energy, "avg_w": avg_w,
            "peak_w": max(r[1] for r in rows),
            "avg_v": avg_v, "avg_a": avg_a}


# ----------------------------------------------------------------------------
# _SessionRow —— 单条会话行（整行可点击）
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# _TabDeck —— 零请求换页容器（QStackedWidget 会隐藏非当前页，
# isVisibleTo 语义变化破坏测试；零几何换页保持页内控件可见性不变）
# ----------------------------------------------------------------------------


class _TabDeck(QWidget):
    """手工换页容器：当前页占满、其余页置零几何。

    不用 QStackedWidget 的原因：QStackedWidget 会 setVisible(False)
    非当前页，页内控件 isVisibleTo(祖先) 语义随之改变；零几何换页
    保持控件 visible 标志恒真，测试与呼吸动画都不受换页影响。

    最小尺寸上报当前页的 minimumSizeHint：deck 自身无 layout，Qt
    默认最小尺寸≈0，父布局（QScrollArea widgetResizable）会把
    deck 压到比页内控件最小高度还矮，超出 deck 矩形的柱体被裁剪
    （「每小时」图只剩顶端一截）。上报后空间不足由页面级滚动消化。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pages: list[QWidget] = []
        self._current = -1

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt 命名约定)
        current = self.current_page()
        if current is not None:
            hint = current.minimumSizeHint()
            if hint.isValid() and (hint.height() > 0 or hint.width() > 0):
                return hint
        return super().minimumSizeHint()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt 命名约定)
        """理想尺寸同取当前页：外层 QScrollArea widgetResizable 只把
        widget 拉到 max(viewport, minimumSizeHint)，不看 sizeHint；
        但父布局分空间按 sizeHint 比例，缺省时 deck 侧需求被低估，
        页面足够高时也按压缩比摊薄、行文字被裁（按端口页历史行为）。"""
        current = self.current_page()
        if current is not None:
            return current.sizeHint()
        return super().sizeHint()

    def add_page(self, page: QWidget) -> None:
        page.setParent(self)
        page.show()
        self._pages.append(page)
        if self._current < 0:
            self._current = 0

    @property
    def current_index(self) -> int:
        return self._current

    def set_current(self, index: int) -> None:
        if not 0 <= index < len(self._pages) or index == self._current:
            return
        self._current = index
        self._relayout()
        # 最小尺寸随当前页变化，通知父布局重新分配空间
        self.updateGeometry()

    def current_page(self) -> QWidget | None:
        if 0 <= self._current < len(self._pages):
            return self._pages[self._current]
        return None

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._relayout()
        super().resizeEvent(event)

    def _relayout(self) -> None:
        for i, page in enumerate(self._pages):
            page.setGeometry(self.rect() if i == self._current
                             else QRect(0, 0, 0, 0))
        current = self.current_page()
        if current is not None:
            current.raise_()


# ----------------------------------------------------------------------------
# _MiniBar —— 行内单值占比条（统计行右端的水平细条）
# ----------------------------------------------------------------------------


class _MiniBar(QWidget):
    """单值占比条：轨道 + 主题色圆角填充，宽度=值/总量。

    水平方向 Expanding：吃掉行内名称与数字之间的中间空档
    （与 Web 版 energy-track 同思路），垂直保持细条定高。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frac = 0.0
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(64)

    def set_fraction(self, frac: float) -> None:
        self._frac = max(0.0, min(float(frac or 0.0), 1.0))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(SiColors.LINE))
            painter.drawRoundedRect(rect, 3, 3)
            if self._frac > 0.0:
                w = max(int(rect.width() * self._frac), 6)  # 至少 6px 可见
                painter.setBrush(QColor(SiColors.THEME))
                painter.drawRoundedRect(
                    QRect(rect.left(), rect.top(), w, rect.height()), 3, 3)
        finally:
            painter.end()


# ----------------------------------------------------------------------------
# _ShareBar —— 四色堆叠占比条（统计「按端口」Tab 顶部一条）
# ----------------------------------------------------------------------------


class _ShareBar(QWidget):
    """四色堆叠条：各段宽度=该口 Wh 占比，颜色=端口身份色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._segments: list[tuple[float, str]] = []  # (占比 0-1, 颜色)
        self.setFixedHeight(8)

    def set_segments(self, segments: list[tuple[float, str]]) -> None:
        self._segments = list(segments)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(0.0, 0.0, self.width(), self.height())
            path = QPainterPath()
            path.addRoundedRect(rect, 4.0, 4.0)
            painter.setClipPath(path)
            # 轨道底色（空载时整条可见）
            painter.fillRect(rect, QColor(SiColors.LINE))
            x = 0.0
            for frac, color in self._segments:
                w = rect.width() * frac
                if w > 0.5:
                    painter.fillRect(QRectF(x, 0.0, w, rect.height()),
                                     QColor(color))
                x += w
        finally:
            painter.end()


# ----------------------------------------------------------------------------
# _HourlyBarChart —— 每小时 24 根柱状图（对齐 PowerCurveWidget 风格）
# ----------------------------------------------------------------------------


class _HourlyBarChart(QWidget):
    """24 桶每小时电量柱状图：Y 轴 nice-number 刻度 + 网格线 + #46B4FF
    圆角柱 + X 轴时间刻度 + 悬浮 tooltip（观感对齐 PowerCurveWidget）。

    数据来自 /api/chart?hours=24&interval=3600 的 Total 功率序列
    （1h 桶 AVG(power) 即该小时 Wh，上游 app.js 同款口径）。

    X 轴刻度按绘图区等距取点、文案取对应桶的完整 label：服务端
    label 是 %m-%d %H:%M 本地时间且 24h 滚动窗的桶不对齐整点/整日
    （如 23:00 起），按"hour%4==0"挑桶会导致大多数窗口一个刻度
    都画不出来。tooltip 悬停显示「时间 · 电量」并高亮柱体。
    """

    # X 轴刻度个数（含首末；桶数不足时按桶数取）
    _X_TICKS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values: list[float] = []
        self._labels: list[str] = []  # 每桶完整时间文案（刻度/tooltip 用）
        self._hover_index: int = -1
        self.setMinimumHeight(_CURVE_HEIGHT)
        self.setMouseTracking(True)

    def set_data(self, values: list[float], labels: list[str]) -> None:
        """整组替换数据；等长截断到 24 桶（服务端 24h 窗即 24-25 桶）。"""
        n = min(len(values), len(labels), 24)
        self._values = [float(v or 0.0) for v in values[:n]]
        self._labels = [str(label) for label in labels[:n]]
        self._hover_index = -1
        self.update()

    def bucket_count(self) -> int:
        return len(self._values)

    # ---------- 悬浮交互 ----------

    def _bar_geometry(self) -> tuple[QRectF, float]:
        """绘图区与单桶槽宽（绘制与命中测试共用一套口径）。"""
        rect = QRectF(self.rect()).adjusted(
            _CURVE_MARGIN_L + _HOURLY_AXIS_W, _CURVE_MARGIN_T,
            -_CURVE_MARGIN_R, -_CURVE_MARGIN_B)
        slot = rect.width() / max(len(self._values), 1)
        return rect, slot

    def _bucket_at(self, x: float) -> int:
        """x 坐标 -> 桶索引；不在绘图区内返回 -1。"""
        if not self._values:
            return -1
        rect, slot = self._bar_geometry()
        if x < rect.left() or x > rect.right():
            return -1
        return max(0, min(int((x - rect.left()) / slot),
                          len(self._values) - 1))

    def _tooltip_text(self, index: int) -> str:
        if not 0 <= index < len(self._values):
            return ""
        label = self._labels[index] if index < len(self._labels) else ""
        return f"{label or '—'} · {self._values[index]:.1f}Wh"

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        idx = self._bucket_at(event.position().x())
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
        """当前悬停桶索引（无悬停 -1）；测试断言用。"""
        return self._hover_index

    @staticmethod
    def _nice_ceil(value: float) -> float:
        """nice-number 上取整：1/2/2.5/5 × 10^k 档（Y 轴刻度步长用）。"""
        if value <= 0:
            return 1.0
        exp = math.floor(math.log10(value))
        base = value / (10.0 ** exp)
        for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
            if base <= factor:
                return factor * (10.0 ** exp)
        return 10.0 * (10.0 ** exp)

    def _y_axis_max(self) -> float:
        """Y 轴跨度：峰值 grace 8% 均分 4 档后按 nice 步长上取整再 ×4，
        保证 5 档刻度值都是整洁数字（0/50/100/150/200 这类）。"""
        peak = max(self._values) if self._values else 0.0
        step = max(self._nice_ceil(max(peak, 1.0) * 1.08 / _HOURLY_TICKS), 1.0)
        return step * _HOURLY_TICKS

    def y_tick_values(self) -> list[float]:
        """当前 Y 轴刻度值（底 0 线 → 顶跨度，自绘刻度与测试共用口径）。"""
        span = self._y_axis_max()
        return [span * k / _HOURLY_TICKS for k in range(_HOURLY_TICKS + 1)]

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            if not self._values:
                self._paint_empty(painter)
            else:
                self._paint_bars(painter)
        finally:
            painter.end()

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                         "暂无每小时数据")

    def _tick_label(self, value: float) -> str:
        """Wh 刻度文案：步长取整后恒为整洁数字（50/100/200，0 线必标）。"""
        return f"{round(value):d}"

    def _paint_bars(self, painter: QPainter) -> None:
        # 左侧 42px 数值带给 Y 轴刻度，其余边距与主曲线同源
        rect = self.rect().adjusted(
            _CURVE_MARGIN_L + _HOURLY_AXIS_W, _CURVE_MARGIN_T,
            -_CURVE_MARGIN_R, -_CURVE_MARGIN_B)
        n = len(self._values)
        span = self._y_axis_max()

        # 网格：三条水平参考线 + 左侧数值刻度（0 线必标）
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.setPen(QPen(QColor(SiColors.LINE), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(1, _HOURLY_TICKS):
            y = int(rect.top() + rect.height() * k / _HOURLY_TICKS)
            painter.drawLine(rect.left(), y, rect.right(), y)
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        metrics = painter.fontMetrics()
        axis_right = rect.left() - 6  # 数值与绘图区留 6px 间隙
        for k, value in enumerate(self.y_tick_values()):
            y = rect.bottom() - rect.height() * k / _HOURLY_TICKS
            text = self._tick_label(value)
            painter.drawText(
                QPointF(axis_right - metrics.horizontalAdvance(text),
                        y + metrics.ascent() / 2 - 1),
                text)

        # 柱体：等分宽度、内缩 25% 留缝，圆角 2px；悬停桶高亮。
        # 0Wh 小时不画（上游 Chart.js 0 值柱高度为 0 不可见，不画假痕迹）
        rect, slot = self._bar_geometry()
        n = len(self._values)
        bar_w = max(slot * 0.75, 1.0)
        bar_color = QColor(_HOURLY_BAR_COLOR)
        hover_color = QColor(_HOURLY_BAR_COLOR)
        hover_color.setAlpha(160)
        painter.setPen(Qt.PenStyle.NoPen)
        for i, value in enumerate(self._values):
            h = rect.height() * min(value, span) / span
            if h <= 0.5:
                continue
            x = rect.left() + i * slot + (slot - bar_w) / 2
            painter.setBrush(hover_color if i == self._hover_index
                             else bar_color)
            painter.drawRoundedRect(
                QRectF(x, rect.bottom() - h, bar_w, h), 2.0, 2.0)

        # 基线
        painter.setPen(QPen(QColor(SiColors.LINE), 1))
        painter.drawLine(rect.left(), rect.bottom(),
                         rect.right(), rect.bottom())

        # X 轴时间刻度：按绘图区等距取 _X_TICKS 个位置，文案取对应
        # 桶 label（滚动窗口桶不对齐整点，不能按 hour%4 挑桶）
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        metrics = painter.fontMetrics()
        count = len(self._values)
        ticks = min(self._X_TICKS, count)
        if ticks > 1:
            for k in range(ticks):
                i = round(k * (count - 1) / (ticks - 1))
                text = (self._labels[i] if i < len(self._labels) else "") \
                    .split(" ")[-1]  # 跨天 label %m-%d %H:%M 只留 HH:MM
                if not text:
                    continue
                tx = rect.left() + i * slot + slot / 2 \
                    - metrics.horizontalAdvance(text) / 2
                tx = max(float(rect.left()),
                         min(tx, rect.right() - metrics.horizontalAdvance(text)))
                painter.drawText(
                    QPointF(tx, rect.bottom() + metrics.ascent() + 4), text)


class _SessionRow(QFrame):
    """单条会话行：圆角卡片观感，左键点击发 selected(session_id)。

    行内容由 SessionHistoryWidget 填充；QFrame 子类承载信号与点击
    虚函数覆写（实例级 monkey-patch 不能可靠覆盖 C++ 虚派发）。
    """

    selected = Signal(int)  # session_id

    def __init__(self, session_id: int, parent=None):
        super().__init__(parent)
        self._session_id = int(session_id)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self._session_id)
        super().mouseReleaseEvent(event)


# ----------------------------------------------------------------------------
# SessionHistoryWidget —— 充电会话历史
# ----------------------------------------------------------------------------


class SessionHistoryWidget(QWidget):
    """充电会话历史：周期/端口筛选 + 会话行列表 + 翻页。

    对外接口：
    - set_service(service, jobs)：注入门面与任务队列，注入即首次拉取；
      service 需提供 cuktech_sessions(port, period, limit, page)。
    - session_selected = Signal(int)：点击会话行发射 session_id，
      由容器/面板负责加载点级曲线（SessionCurveWidget.show_session）。
    - refresh(manual)：手动刷新入口；retheme()：主题切换重设样式。
    """

    session_selected = Signal(int)  # session_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._service = None
        self._jobs = None
        self._period = "today"
        self._port: int | None = None
        self._page = 1
        self._pages = 1
        self._total = 0
        self._in_flight = False
        self._session_rows: list[_SessionRow] = []
        # 当前页会话原始 dict（id -> session）：供容器回查摘要字段
        self._sessions: dict[int, dict] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_list(), stretch=1)
        root.addWidget(self._build_pager())
        self._apply_inline_styles()

    # ---------- 布局构建 ----------

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._period_combo = _period_combo(_PERIOD_LABELS)
        self._period_combo.currentIndexChanged.connect(self._on_period_changed)

        self._port_combo = themed_combo([])
        for _port, label in _PORT_FILTERS:
            self._port_combo.addItem(label)
        self._port_combo.currentIndexChanged.connect(self._on_port_changed)

        self._refresh_btn = QPushButton()
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setToolTip("刷新会话列表")
        self._refresh_btn.clicked.connect(lambda: self.refresh(manual=True))

        lay.addWidget(self._period_combo)
        lay.addWidget(self._port_combo)
        lay.addStretch(1)
        lay.addWidget(self._refresh_btn)
        return bar

    def _build_list(self) -> QWidget:
        """会话列表容器：普通 QWidget（行定高 56 + 间距 8 布局）。

        不再内嵌 QScrollArea：面板历史页已包整页 QScrollArea，行数多时
        由页面级滚动承载，避免嵌套滚动死锁；容器 sizePolicy 恒为
        Preferred，高度=可见行数×行高随行数伸缩，不与分页条重叠。
        """
        self._list_host = QWidget()
        self._list_lay = QVBoxLayout(self._list_host)
        self._list_lay.setContentsMargins(0, 0, 4, 0)
        self._list_lay.setSpacing(_SESSION_ROW_SPACING)
        self._empty_label = QLabel("暂无充电会话")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setFont(QFont("Microsoft YaHei UI", 10))
        self._list_lay.addWidget(self._empty_label)
        self._list_lay.addStretch(1)
        return self._list_host

    def _build_pager(self) -> QWidget:
        pager = QWidget()
        self._pager_row = pager  # 显式引用：测试断言列表/分页条几何用
        lay = QHBoxLayout(pager)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._prev_btn = QPushButton("上一页")
        self._prev_btn.setFixedSize(64, 28)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(lambda: self._go_page(self._page - 1))
        self._page_label = QLabel("第 1/1 页")
        self._page_label.setFont(QFont("Microsoft YaHei UI", 9))
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next_btn = QPushButton("下一页")
        self._next_btn.setFixedSize(64, 28)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(lambda: self._go_page(self._page + 1))
        lay.addStretch(1)
        lay.addWidget(self._prev_btn)
        lay.addWidget(self._page_label)
        lay.addWidget(self._next_btn)
        lay.addStretch(1)
        return pager

    def _apply_inline_styles(self) -> None:
        """重设构造期求值的内联样式（主题切换时由 retheme 调用）。"""
        self._refresh_btn.setIcon(
            qta.icon("mdi.refresh", color=SiColors.TEXT_PRIMARY))
        self._refresh_btn.setStyleSheet(
            f"QPushButton {{ background: {SiColors.SURFACE}; border: none;"
            f" border-radius: 8px; }}"
            f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}")
        self._empty_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._page_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
        pager_qss = (
            f"QPushButton {{ background: {SiColors.SURFACE}; border: none;"
            f" border-radius: 8px; color: {SiColors.TEXT_PRIMARY};"
            f" font-size: 9pt; }}"
            f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}"
            f"QPushButton:disabled {{ background: {SiColors.SURFACE};"
            f" color: {SiColors.TEXT_DISABLED}; }}")
        self._prev_btn.setStyleSheet(pager_qss)
        self._next_btn.setStyleSheet(pager_qss)
        apply_combo_qss(self._period_combo)
        apply_combo_qss(self._port_combo)
        for row in self._session_rows:
            self._style_row(row)

    def _style_row(self, row: _SessionRow) -> None:
        """会话行圆角卡片与文字配色（构造与 retheme 共用）。"""
        row.setStyleSheet(
            f"QFrame {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 12px; }}"
            f"QFrame:hover {{ background: {SiColors.CARD_HOVER};"
            f" border-color: {SiColors.CARD_BORDER_HOVER}; }}")
        for label in row.findChildren(QLabel):
            prop = label.property("role")
            if prop == "active_dot":
                color = SiColors.THEME
            elif prop == "active_tag":
                color = SiColors.THEME
            elif prop == "proto":
                color = SiColors.THEME
            elif prop == "port" or prop == "wh":
                color = SiColors.TEXT_PRIMARY
            elif prop == "time" or prop == "watts":
                color = SiColors.TEXT_SECONDARY
            else:
                color = SiColors.TEXT_MUTED
            label.setStyleSheet(
                f"color: {color}; background: transparent; border: none;")

    # ---------- 数据获取 ----------

    def set_service(self, service, jobs: "JobExecutor") -> None:
        """注入门面与任务队列；注入即首次拉取。"""
        self._service = service
        self._jobs = jobs
        self.refresh()

    def refresh(self, manual: bool = False) -> None:
        """拉取会话列表（当前周期/端口/页码）；单次任务单请求。"""
        if self._service is None or self._jobs is None or self._in_flight:
            return
        self._in_flight = True
        self._jobs.submit(
            lambda: self._service.cuktech_sessions(
                port=self._port, period=self._period,
                limit=_PAGE_LIMIT, page=self._page),
            on_success=self._on_sessions,
            on_error=self._on_error,
        )

    def _on_period_changed(self, index: int) -> None:
        if 0 <= index < len(_PERIOD_LABELS):
            self._period = _PERIOD_LABELS[index][0]
            self._page = 1
            self.refresh()

    def _on_port_changed(self, index: int) -> None:
        if 0 <= index < len(_PORT_FILTERS):
            self._port = _PORT_FILTERS[index][0]
            self._page = 1
            self.refresh()

    def _go_page(self, page: int) -> None:
        page = max(1, min(page, max(self._pages, 1)))
        if page == self._page:
            return
        self._page = page
        self.refresh()

    def _on_sessions(self, payload: dict) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        sessions = payload.get("sessions") or []
        try:
            self._total = int(payload.get("total") or 0)
            self._pages = max(int(payload.get("pages") or 1), 1)
        except (TypeError, ValueError):
            self._total, self._pages = len(sessions), 1
        self._page = max(1, min(self._page, self._pages))
        self._render_sessions(sessions)

    def _on_error(self, error: Exception) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        # 历史模块无轮询兜底，任何一次加载都是用户动作触发，失败必须提示
        Toast.info(self, f"会话历史加载失败：{error}", 3000)
        if not self._session_rows:
            self._empty_label.setVisible(True)

    # ---------- 渲染 ----------

    def _render_sessions(self, sessions: list) -> None:
        # 清空布局旧行：行帧 deleteLater；空态标签保留复用，末尾 stretch 重建
        while self._list_lay.count():
            item = self._list_lay.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._empty_label:
                widget.deleteLater()
        self._session_rows.clear()
        # 当前页会话原始 dict：供 current_session_summary 回查摘要字段
        self._sessions = {
            int(s.get("id") or 0): s for s in sessions if isinstance(s, dict)}
        for session in sessions:
            if not isinstance(session, dict):
                continue
            row = self._build_session_row(session)
            self._session_rows.append(row)
            self._list_lay.addWidget(row)
        if not sessions:
            self._list_lay.addWidget(self._empty_label)
            self._empty_label.setVisible(True)
        else:
            self._empty_label.setVisible(False)
        self._list_lay.addStretch(1)
        # 翻页态：「第 X/Y 页」，两端到头禁用
        self._page_label.setText(f"第 {self._page}/{self._pages} 页")
        self._prev_btn.setEnabled(self._page > 1)
        self._next_btn.setEnabled(self._page < self._pages)

    def _build_session_row(self, session: dict) -> _SessionRow:
        """会话行布局：状态点 | 端口 时间 时长 [充电中] … 协议 Wh 峰值。"""
        is_active = bool(session.get("is_active"))
        session_id = int(session.get("id") or 0)
        row = _SessionRow(session_id)
        row.setFixedHeight(_SESSION_ROW_HEIGHT)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(8)

        active_dot = QLabel("●")
        active_dot.setProperty("role", "active_dot")
        active_dot.setFont(QFont("Microsoft YaHei UI", 8))

        port = QLabel(_port_name(int(session.get("port") or 0)))
        port.setProperty("role", "port")
        port.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        port.setMinimumWidth(38)

        time_label = QLabel(_fmt_period(
            session.get("start_time"), session.get("end_time")))
        time_label.setProperty("role", "time")
        time_label.setFont(QFont("Microsoft YaHei UI", 9))
        # 区间两端都可能跨天（最宽 "MM-DD HH:MM → MM-DD HH:MM"），
        # 只按开始端是否跨天估宽度会截断终点文案
        time_label.setMinimumWidth(
            150 if _is_cross_day(session.get("start_time"))
            or _is_cross_day(session.get("end_time")) else 96)

        duration = QLabel(_fmt_duration(session.get("duration_sec")))
        duration.setProperty("role", "duration")
        duration.setFont(QFont("Microsoft YaHei UI", 9))

        proto = QLabel(str(session.get("protocol") or ""))
        proto.setProperty("role", "proto")
        proto.setFont(QFont("Microsoft YaHei UI", 8))

        wh = QLabel(_fmt_wh(float(session.get("total_wh") or 0.0)))
        wh.setProperty("role", "wh")
        wh.setFont(QFont("Microsoft YaHei UI", 10))
        wh.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        watts = QLabel(f"峰值 {_fmt_watts(float(session.get('peak_power_w') or 0.0))}")
        watts.setProperty("role", "watts")
        watts.setFont(QFont("Microsoft YaHei UI", 9))
        watts.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        lay.addWidget(active_dot)
        # 显隐必须在 addWidget 之后设置：无父级控件 setVisible(True)
        # 会立即作为独立原生顶层窗口显示（带系统标题栏的白窗在屏幕
        # 上一闪而过），挂进布局后才只是行内普通子控件
        active_dot.setVisible(is_active)
        if is_active:
            active_tag = QLabel("充电中")
            active_tag.setProperty("role", "active_tag")
            active_tag.setFont(QFont("Microsoft YaHei UI", 8))
            active_dot.setToolTip("充电中")
            lay.addWidget(active_tag)
        lay.addWidget(port)
        lay.addWidget(time_label)
        lay.addWidget(duration)
        lay.addStretch(1)
        lay.addWidget(proto)
        lay.addSpacing(6)
        lay.addWidget(wh)
        lay.addWidget(watts)

        self._style_row(row)
        # 点击整行 -> 本组件 session_selected（容器负责接曲线加载）
        row.selected.connect(self.session_selected.emit)
        return row

    def current_session_summary(self, session_id: int) -> dict:
        """回查当前页某会话的原始数据（供容器喂 set_summary 等摘要位）。

        session_id 不在当前页（已翻页/尚未拉取）返回空 dict，调用方
        以 .get() 取字段即安全降级为「摘要 —」。
        """
        return self._sessions.get(int(session_id)) or {}

    def retheme(self) -> None:
        """主题切换：重设全部内联样式（含动态创建的会话行）。"""
        self._apply_inline_styles()


def _is_cross_day(ts) -> bool:
    try:
        return datetime.fromtimestamp(float(ts)).date() != datetime.now().date()
    except (TypeError, ValueError, OSError, OverflowError):
        return False


# ----------------------------------------------------------------------------
# EnergySummaryWidget —— 能量统计
# ----------------------------------------------------------------------------


class EnergySummaryWidget(QWidget):
    """能量统计：三子 Tab（按端口/每小时/快充协议）+ 共用周期下拉。

    对齐上游 cardEnergy（web-frontend-features.md §3.4）：三个子 Tab
    共用一个周期下拉与标题行合计值；「按端口」= 统计三块 + 四色堆叠
    条 + 分端口明细（行内占比条/占比%/活跃口充电中标记）；「每小时」
    = 24 根柱状图（cuktech_chart 1h 桶，切入才拉）；「快充协议」=
    协议分布行（行内占比条）。

    对外接口：set_service(service, jobs)、refresh(manual)、retheme()。
    service 需提供 cuktech_energy_stats(period)、
    cuktech_energy_protocols(period)、cuktech_chart(hours, interval)。

    懒加载纪律：stats/protocols 注入即拉（合并一单）；每小时柱图
    仅在切入该 Tab 时首次拉取，此后每次切入刷新（周期变化清缓存，
    切回时重拉）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._service = None
        self._jobs = None
        self._period = "today"
        self._in_flight = False
        self._protocol_rows: list[dict] = []
        # 每小时 Tab 状态：数据未拉过/正在拉；周期变化即清缓存
        self._hourly_loaded = False
        self._hourly_in_flight = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_stats())
        root.addWidget(self._build_tab_row())
        self._deck = _TabDeck()
        self._deck.add_page(self._build_ports_page())
        self._deck.add_page(self._build_hourly_page())
        self._deck.add_page(self._build_protocols_page())
        # deck 之后不加 trailing stretch：deck 上报当前页最小尺寸，
        # stretch 会与其对半分剩余空间把 deck 压矮、柱状图被裁剪
        # （外层 QScrollArea widgetResizable 已消化多余空间）
        root.addWidget(self._deck, stretch=1)
        self._apply_inline_styles()
        self._apply_tab_styles()

    # ---------- 布局构建 ----------

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._period_combo = _period_combo(_PERIOD_LABELS)
        self._period_combo.currentIndexChanged.connect(self._on_period_changed)
        lay.addWidget(self._period_combo)
        lay.addStretch(1)
        return bar

    def _build_stats(self) -> QWidget:
        stats = QWidget()
        lay = QHBoxLayout(stats)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._total_wh = QLabel("—")
        self._total_caption = QLabel("总充电量")
        self._session_count = QLabel("—")
        self._count_caption = QLabel("充电会话")
        self._power_range = QLabel("—")
        self._power_caption = QLabel("平均 / 峰值功率")
        for value, caption in (
                (self._total_wh, self._total_caption),
                (self._session_count, self._count_caption),
                (self._power_range, self._power_caption)):
            col = QVBoxLayout()
            col.setSpacing(2)
            value.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption.setFont(QFont("Microsoft YaHei UI", 8))
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(value)
            col.addWidget(caption)
            lay.addLayout(col, stretch=1)
        return stats

    def _build_tab_row(self) -> QWidget:
        """三子 Tab 按钮行（themed_tab_button，主面板页签同款选中态）。"""
        row = QWidget()
        self._tab_row = row  # 显式引用：测试断言统计块/Tab 行几何用
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._tab_buttons: list[QPushButton] = []
        for text in ("按端口", "每小时", "快充协议"):
            btn = themed_tab_button(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _=False, i=len(self._tab_buttons): self._select_tab(i))
            self._tab_buttons.append(btn)
            lay.addWidget(btn)
        lay.addStretch(1)
        return row

    def _build_ports_page(self) -> QWidget:
        """「按端口」页：四色堆叠条 + 分端口明细行（行内占比条+占比%）。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)

        self._share_bar = _ShareBar()
        lay.addWidget(self._share_bar)

        self._port_rows: dict[int, dict] = {}
        for port in (1, 2, 3, 4):
            row_frame = QWidget()
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            dot = QLabel("●")
            dot.setFont(QFont("Microsoft YaHei UI", 8))
            name = QLabel(_port_name(port))
            name.setProperty("role", "port")
            name.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
            name.setMinimumWidth(44)
            live = QLabel("充电中")
            live.setProperty("role", "live_tag")
            live.setFont(QFont("Microsoft YaHei UI", 8))
            live.setVisible(False)
            bar = _MiniBar()
            bar.setToolTip("占该周期总电量比例")
            wh = QLabel("—")
            wh.setProperty("role", "wh")
            wh.setFont(QFont("Microsoft YaHei UI", 10))
            wh.setMinimumWidth(52)
            wh.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count = QLabel("0 次")
            count.setProperty("role", "count")
            count.setFont(QFont("Microsoft YaHei UI", 9))
            count.setMinimumWidth(40)
            count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            pct = QLabel("0%")
            pct.setProperty("role", "pct")
            pct.setFont(QFont("Microsoft YaHei UI", 9))
            pct.setMinimumWidth(34)
            pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(dot)
            row.addWidget(name)
            row.addWidget(live)
            row.addWidget(bar)
            row.addWidget(wh)
            row.addWidget(count)
            row.addWidget(pct)
            # 行高下限：QHBoxLayout 的 minimumSizeHint 取子控件最小值
            # 的最大者，但 QLabel 横排下不约束行高——高度紧张时行被压
            # 到字体高度以下，文字上下裁切、行间互相重叠（截图 bug）。
            # 22 ≈ 10pt DemiBold 行名 17px + 上下各 2px 呼吸空间
            row_frame.setMinimumHeight(22)
            lay.addWidget(row_frame)
            self._port_rows[port] = {"frame": row_frame, "dot": dot,
                                     "name": name, "live": live, "bar": bar,
                                     "wh": wh, "count": count, "pct": pct}
        return page

    def _build_hourly_page(self) -> QWidget:
        """「每小时」页：24 桶柱状图（数据切入才拉）。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)
        self._hourly_chart = _HourlyBarChart()
        self._hourly_chart.setMinimumHeight(_CURVE_HEIGHT + 40)
        lay.addWidget(self._hourly_chart)
        self._hourly_empty = QLabel("暂无每小时数据")
        self._hourly_empty.setProperty("role", "empty")
        self._hourly_empty.setFont(QFont("Microsoft YaHei UI", 9))
        self._hourly_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._hourly_empty)
        return page

    def _build_protocols_page(self) -> QWidget:
        """「快充协议」页：原协议分布内容挪入，行内加占比条。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)
        self._protocols_lay = QVBoxLayout()
        self._protocols_lay.setContentsMargins(0, 0, 0, 0)
        self._protocols_lay.setSpacing(6)
        lay.addLayout(self._protocols_lay)
        lay.addStretch(1)
        self._protocols_empty = QLabel("暂无协议数据")
        self._protocols_empty.setProperty("role", "empty")
        self._protocols_empty.setFont(QFont("Microsoft YaHei UI", 9))
        self._protocols_lay.addWidget(self._protocols_empty)
        return page

    def _apply_inline_styles(self) -> None:
        """重设构造期求值的内联样式（主题切换时由 retheme 调用）。"""
        for value in (self._total_wh, self._session_count, self._power_range):
            value.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;")
        for caption in (self._total_caption, self._count_caption,
                        self._power_caption):
            caption.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        apply_combo_qss(self._period_combo)
        self._hourly_empty.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._protocols_empty.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._apply_port_row_styles()
        for row in self._protocol_rows:
            row["proto"].setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;")
            row["detail"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
            row["bar"].update()
        self._share_bar.update()
        self._hourly_chart.update()

    def _apply_port_row_styles(self) -> None:
        """端口明细行样式：圆点=端口身份色（恒亮），空载口文字压暗。"""
        for port, row in self._port_rows.items():
            active = row["live"].isVisibleTo(row["frame"])
            row["dot"].setStyleSheet(
                f"color: {_PORT_COLORS[port]}; background: transparent;")
            row["name"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY if active else SiColors.TEXT_MUTED};"
                f" background: transparent;")
            row["live"].setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;")
            row["wh"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
            row["count"].setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
            row["pct"].setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
            row["bar"].update()

    def _apply_tab_styles(self) -> None:
        """子 Tab 选中态：选中钮重取样式（themed_tab_button 是自绘态）。"""
        for i, btn in enumerate(self._tab_buttons):
            btn.setChecked(i == self._deck.current_index)
            btn.update()

    # ---------- 数据获取 ----------

    def set_service(self, service, jobs: "JobExecutor") -> None:
        """注入门面与任务队列；注入即首次拉取。"""
        self._service = service
        self._jobs = jobs
        self.refresh()

    def refresh(self, manual: bool = False) -> None:
        """拉取 stats + protocols（合并为一次任务，串行队列只排一单）。

        当前 Tab 在「每小时」时同时刷新每小时数据；其他 Tab 切入时
        才拉（懒加载纪律）。
        """
        if self._service is None or self._jobs is None or self._in_flight:
            return
        self._in_flight = True
        self._jobs.submit(
            self._fetch_all,
            on_success=self._on_data,
            on_error=self._on_error,
        )
        if self._deck.current_index == 1:
            self._load_hourly()

    def _fetch_all(self) -> tuple[dict, dict | None]:
        stats = self._service.cuktech_energy_stats(self._period)
        try:
            protocols = self._service.cuktech_energy_protocols(self._period)
        except Exception:
            # 协议分布失败不拖累统计三块（显示「暂无协议数据」）
            protocols = None
        return stats, protocols

    def _load_hourly(self) -> None:
        """拉每小时数据：仅切到该 Tab 时调用；串行队列单请求。"""
        if self._service is None or self._jobs is None or self._hourly_in_flight:
            return
        self._hourly_in_flight = True
        self._jobs.submit(
            lambda: self._service.cuktech_chart(hours=24, interval=3600),
            on_success=self._on_hourly,
            on_error=self._on_hourly_error,
        )

    def _select_tab(self, index: int) -> None:
        """切子 Tab：换页 + 选中态；切入「每小时」按需拉数据。"""
        if index == self._deck.current_index:
            return
        self._deck.set_current(index)
        self._apply_tab_styles()
        if index == 1:
            # 首次切入或周期刚变（缓存已清）才拉；同一周期重复切入不重拉，
            # 上游 hourly 图也只在首次建图（canvas 隐藏时尺寸为 0）
            if not self._hourly_loaded:
                self._load_hourly()

    def _on_period_changed(self, index: int) -> None:
        if 0 <= index < len(_PERIOD_LABELS):
            self._period = _PERIOD_LABELS[index][0]
            # 周期变化三 Tab 共用状态：每小时缓存作废，切回时重拉
            self._hourly_loaded = False
            self.refresh()
            if self._deck.current_index == 1:
                self._load_hourly()

    def _on_data(self, data: tuple) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        stats, protocols = data
        self._render_stats(stats or {})
        self._render_protocols(protocols)

    def _on_error(self, error: Exception) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        # 统计回落占位（仿 cuktech_panel 的离线渲染路径），并提示原因
        self._total_wh.setText("—")
        self._session_count.setText("—")
        self._power_range.setText("—")
        Toast.info(self, f"能量统计加载失败：{error}", 3000)

    def _on_hourly(self, payload: dict | None) -> None:
        self._hourly_in_flight = False
        self._hourly_loaded = True
        if not shiboken6.isValid(self):
            return
        labels = (payload or {}).get("labels") or []
        datasets = (payload or {}).get("datasets") or {}
        power_sets = datasets.get("power") or []
        total: list[float] = []
        for ds in power_sets:
            if isinstance(ds, dict) and ds.get("label") == "Total":
                total = ds.get("data") or []
                break
        if not total and power_sets and isinstance(power_sets[0], dict):
            total = power_sets[0].get("data") or []
        # labels 原样下传（%m-%d %H:%M 本地时间文案）：X 轴刻度与
        # tooltip 都取完整文案，跨天窗口也能正确显示
        self._hourly_chart.set_data(total, [str(l) for l in labels])
        self._hourly_empty.setVisible(not total)
        self._hourly_chart.setVisible(bool(total))

    def _on_hourly_error(self, error: Exception) -> None:
        self._hourly_in_flight = False
        if not shiboken6.isValid(self):
            return
        Toast.info(self, f"每小时数据加载失败：{error}", 3000)

    # ---------- 渲染 ----------

    def _render_stats(self, stats: dict) -> None:
        self._total_wh.setText(_fmt_wh(float(stats.get("total_wh") or 0.0)))
        self._session_count.setText(f"{int(stats.get('session_count') or 0)} 次")
        avg = float(stats.get("avg_power_w") or 0.0)
        peak = float(stats.get("peak_power_w") or 0.0)
        self._power_range.setText(f"{_fmt_watts(avg)} / {_fmt_watts(peak)}")
        # by_port 键服务端是字符串 "1"-"4"（DB 查询与实时合并两处均为
        # str(port)），规整成 int 兼容门面/未来契约归一化为 int 的情况
        by_port: dict[int, dict] = {}
        for key, entry in (stats.get("by_port") or {}).items():
            try:
                by_port[int(key)] = entry if isinstance(entry, dict) else {}
            except (TypeError, ValueError):
                continue
        total_wh = sum(float((by_port.get(p) or {}).get("wh") or 0.0)
                       for p in (1, 2, 3, 4))
        segments: list[tuple[float, str]] = []
        for port in (1, 2, 3, 4):
            entry = by_port.get(port) or {}
            wh = float(entry.get("wh") or 0.0)
            frac = wh / total_wh if total_wh > 0 else 0.0
            segments.append((frac, _PORT_COLORS[port]))
            row = self._port_rows[port]
            row["wh"].setText(_fmt_wh(wh))
            row["count"].setText(f"{int(entry.get('count') or 0)} 次")
            row["pct"].setText(f"{round(frac * 100)}%")
            row["bar"].set_fraction(frac)
            # 活跃口：is_active=true（服务端已合并进行中会话），呼吸点
            # 简化为常亮圆点 + 「充电中」文字标签
            is_active = bool(entry.get("is_active"))
            row["live"].setVisible(is_active)
            row["live"].setToolTip("充电中")
        self._share_bar.set_segments(segments)
        self._apply_port_row_styles()

    def _render_protocols(self, payload: dict | None) -> None:
        # 清空旧行（空态标签保留复用）；行是 QWidget，deleteLater 即清
        while self._protocols_lay.count():
            item = self._protocols_lay.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._protocols_empty:
                widget.deleteLater()
        self._protocol_rows.clear()
        rows = [r for r in ((payload or {}).get("protocols") or [])
                if isinstance(r, dict)]
        rows.sort(key=lambda r: float(r.get("wh") or 0.0), reverse=True)
        total = sum(float(r.get("wh") or 0.0) for r in rows)
        if not rows:
            self._protocols_lay.addWidget(self._protocols_empty)
            self._protocols_empty.setVisible(True)
        else:
            self._protocols_empty.setVisible(False)
            for entry in rows:
                row = self._build_protocol_row(entry, total)
                self._protocol_rows.append(row)
                self._protocols_lay.addWidget(row["frame"])
                self._style_protocol_row(row)

    def _build_protocol_row(self, entry: dict, total_wh: float) -> dict:
        """协议行：「PD」[占比条]「65.2Wh · 3 次」（按 Wh 降序由调用方排序）。"""
        frame = QWidget()
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        proto = QLabel(str(entry.get("protocol") or "未知"))
        proto.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        proto.setMinimumWidth(44)
        bar = _MiniBar()
        bar.setToolTip("占该周期总电量比例")
        detail = QLabel(
            f"{_fmt_wh(float(entry.get('wh') or 0.0))}"
            f" · {int(entry.get('count') or 0)} 次")
        detail.setFont(QFont("Microsoft YaHei UI", 9))
        detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(proto)
        lay.addWidget(bar)
        lay.addWidget(detail)
        wh = float(entry.get("wh") or 0.0)
        bar.set_fraction(wh / total_wh if total_wh > 0 else 0.0)
        row = {"frame": frame, "proto": proto, "detail": detail, "bar": bar}
        return row

    def _style_protocol_row(self, row: dict) -> None:
        row["proto"].setStyleSheet(
            f"color: {SiColors.THEME}; background: transparent;")
        row["detail"].setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")

    def retheme(self) -> None:
        """主题切换：重设全部内联样式（含动态创建的协议行）。"""
        self._apply_inline_styles()
        self._apply_tab_styles()


# ----------------------------------------------------------------------------
# SessionCurveWidget —— 单会话点级曲线
# ----------------------------------------------------------------------------


class SessionCurveWidget(QWidget):
    """单会话详情：统计四联块 + 点级曲线（协议虚线标注）+ 五项指标
    + 精度下拉 + CSV 导出（PowerCurveWidget 同风格自绘）。

    对外接口：
    - show_session(session_id, service, jobs)：内部走
      cuktech_session_points(session_id, downsample) 拉点级数据并绘制；
      service 需提供 cuktech_session_points(session_id, downsample) 与
      cuktech_export_session_csv(session_id, save_path)。
    - set_summary(avg_voltage, avg_current)：电压/电流摘要文案，来自
      会话数据的 avg_voltage / avg_current 字段，由调用方传入。
    - clear()：清空曲线与摘要回到空态；retheme()：主题切换重绘。

    五项指标（电量 Wh/均功率/峰功率/均压/均流）由点列积分/平均计算
    （_compute_session_metrics，仿上游 charge_history.js:138-150）；
    精度切换重拉对应 downsample 参数；导出走门面的 CSV 下载（后台
    线程落盘，完成后 Toast 带保存路径）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._service = None
        self._jobs = None
        self._session_id: int | None = None
        self._in_flight = False
        self._export_in_flight = False
        self._downsample = 200  # 缺省与精度下拉「200 点」档一致
        self._summary: tuple[float | None, float | None] | None = None
        self._metrics: dict[str, float] = {}
        # 统计四联块：首次 show_session 拉一次后缓存复用
        self._stats_loaded = False
        self._stats_in_flight = False

        self._summary_label = QLabel("")
        self._summary_label.setFont(QFont("Microsoft YaHei UI", 9))
        self._curve = _SessionCurve()
        self._curve.setMinimumHeight(_CURVE_HEIGHT)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(self._build_header())
        lay.addWidget(self._build_stats_block())
        lay.addWidget(self._curve)
        lay.addWidget(self._build_metric_row())

    # ---------- 布局构建 ----------

    def _build_stats_block(self) -> QWidget:
        """统计四联块（数据 cuktech_energy_stats(period)，组件自查）：
        总电量 / 充电次数 / 平均功率 / 峰值功率。首次 show_session 拉
        一次并缓存，此后 show_session 都复用（数据是周期级概览）。"""
        block = QWidget()
        lay = QHBoxLayout(block)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._stat_labels: dict[str, QLabel] = {}
        for key, caption in (
                ("total_wh", "总充电量"),
                ("session_count", "充电次数"),
                ("avg_power_w", "平均功率"),
                ("peak_power_w", "峰值功率")):
            col = QVBoxLayout()
            col.setSpacing(1)
            value = QLabel("—")
            value.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold))
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap = QLabel(caption)
            cap.setFont(QFont("Microsoft YaHei UI", 8))
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap.setProperty("role", "metric_caption")
            col.addWidget(value)
            col.addWidget(cap)
            self._stat_labels[key] = value
            lay.addLayout(col, stretch=1)
        return block

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("会话功率曲线")
        title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold))
        header.addWidget(title)
        # 充电时间区间副文案（set_period 填充；默认隐藏）
        self._period_label = QLabel("")
        self._period_label.setProperty("role", "period")
        self._period_label.setFont(QFont("Microsoft YaHei UI", 9))
        self._period_label.setVisible(False)
        header.addWidget(self._period_label)
        header.addStretch(1)
        header.addWidget(self._summary_label)

        self._precision_combo = themed_combo([])
        for _value, label in _PRECISION_OPTIONS:
            self._precision_combo.addItem(label)
        # 缺省选中 200 点档（与 _downsample=200 缺省一致）
        self._precision_combo.setCurrentIndex(2)
        self._precision_combo.setToolTip("曲线抽稀精度（点数），切换后重拉数据")
        self._precision_combo.currentIndexChanged.connect(
            self._on_precision_changed)
        header.addWidget(self._precision_combo)

        self._export_btn = QPushButton("导出 CSV")
        self._export_btn.setFixedHeight(28)
        self._export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._export_btn.setToolTip("导出本会话 CSV 到本地文件")
        self._export_btn.clicked.connect(self._on_export)
        header.addWidget(self._export_btn)
        return header

    def _build_metric_row(self) -> QWidget:
        """五项指标行：电量/均功率/峰功率/均压/均流（点列计算）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._metric_labels: dict[str, QLabel] = {}
        for key, caption in (
                ("wh", "电量"),
                ("avg_w", "均功率"),
                ("peak_w", "峰功率"),
                ("avg_v", "均压"),
                ("avg_a", "均流")):
            col = QVBoxLayout()
            col.setSpacing(1)
            value = QLabel("—")
            value.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap = QLabel(caption)
            cap.setFont(QFont("Microsoft YaHei UI", 8))
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap.setProperty("role", "metric_caption")
            value.setProperty("role", f"metric_{key}")
            col.addWidget(value)
            col.addWidget(cap)
            self._metric_labels[key] = value
            lay.addLayout(col, stretch=1)
        return row

    # ---------- 公开接口 ----------

    def show_session(self, session_id: int, service, jobs: "JobExecutor") -> None:
        """拉取并绘制单会话点级曲线（downsample 由精度下拉决定）。

        统计四联块缺数据时随首次会话一并拉取（单次，失败静默回落
        「—」，不拖累曲线）。
        """
        self._service = service
        self._jobs = jobs
        self._session_id = int(session_id)
        if self._jobs is None:
            return
        if not self._stats_loaded and not self._stats_in_flight:
            self._stats_in_flight = True
            self._jobs.submit(
                lambda: self._service.cuktech_energy_stats("today"),
                on_success=self._on_stats,
                on_error=self._on_stats_error,
            )
        if self._in_flight:
            return
        self._in_flight = True
        downsample = self._downsample
        self._jobs.submit(
            lambda: self._service.cuktech_session_points(
                self._session_id, downsample),
            on_success=self._on_points,
            on_error=self._on_error,
        )

    def set_summary(self, avg_voltage: float | None,
                    avg_current: float | None) -> None:
        """设置电压/电流摘要（来自会话行数据的 avg_* 字段）。

        仅作初值：点列到达后 _on_points 会用点列计算的均值覆盖（旧
        数据会话行 avg_* 可能为 0，点列值才是真实均压/均流）。
        """
        self._summary = (avg_voltage, avg_current)
        self._render_summary()

    def set_period(self, start_ts, end_ts) -> None:
        """标明本会话充电时间区间「start → end」（曲线标题副文案）。

        对齐上游 charge_history.js 的 ``${start} → ${end}``；进行中
        会话终点显示「充电中」。
        """
        self._period_label.setText(_fmt_period(start_ts, end_ts))
        self._period_label.setVisible(True)

    def clear(self) -> None:
        """清空曲线与摘要回到空态。"""
        self._session_id = None
        self._summary = None
        self._metrics = {}
        self._curve.set_series([], [])
        self._render_summary()
        self._render_metrics()

    # ---------- 统计四联块 ----------

    def _on_stats(self, payload: dict | None) -> None:
        self._stats_in_flight = False
        self._stats_loaded = True
        if not shiboken6.isValid(self):
            return
        stats = payload or {}
        values = {
            "total_wh": _fmt_wh(float(stats.get("total_wh") or 0.0)),
            "session_count": f"{int(stats.get('session_count') or 0)} 次",
            "avg_power_w": _fmt_watts(float(stats.get("avg_power_w") or 0.0)),
            "peak_power_w": _fmt_watts(float(stats.get("peak_power_w") or 0.0)),
        }
        for key, text in values.items():
            self._stat_labels[key].setText(text)

    def _on_stats_error(self, error: Exception) -> None:
        # 统计块失败静默保留「—」占位：曲线主功能不受影响
        self._stats_in_flight = False

    def _render_stats_block_empty(self) -> None:
        for label in self._stat_labels.values():
            label.setText("—")

    # ---------- 渲染 ----------

    def _render_summary(self) -> None:
        if self._summary is None:
            self._summary_label.setText("")
            return
        avg_v, avg_a = self._summary
        if avg_v is None or avg_a is None:
            self._summary_label.setText("电压/电流 —")
        else:
            self._summary_label.setText(f"{avg_v:.1f}V · {avg_a:.2f}A")

    def _on_points(self, payload: dict | None) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        points = (payload or {}).get("points") or []
        powers: list[float] = []
        times: list[str] = []
        protocols: list[str] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                powers.append(float(point.get("power") or 0.0))
            except (TypeError, ValueError):
                continue
            try:
                times.append(datetime.fromtimestamp(
                    float(point.get("timestamp"))).strftime("%H:%M"))
            except (TypeError, ValueError, OSError, OverflowError):
                times.append("")
            protocols.append(str(point.get("protocol") or ""))
        self._curve.set_series(powers, times, protocols=protocols)
        self._metrics = _compute_session_metrics(
            points if isinstance(points, list) else [])
        # 摘要优先用点列计算的均压/均流：会话行 avg_* 是旧版瞬时值
        # （恒 0），点列值与五项指标同源且对旧数据同样正确。点列为空
        # （404/无采样）时保留调用方传入值，不落 0。
        if points:
            self._summary = (self._metrics["avg_v"], self._metrics["avg_a"])
        self._render_summary()
        self._render_metrics()

    def _render_metrics(self) -> None:
        """五项指标文案：无数据时回落「—」（clear 与空点列共用）。"""
        if not self._metrics:
            for label in self._metric_labels.values():
                label.setText("—")
            return
        self._metric_labels["wh"].setText(_fmt_wh(self._metrics["wh"]))
        self._metric_labels["avg_w"].setText(_fmt_watts(self._metrics["avg_w"]))
        self._metric_labels["peak_w"].setText(_fmt_watts(self._metrics["peak_w"]))
        self._metric_labels["avg_v"].setText(f"{self._metrics['avg_v']:.1f}V")
        self._metric_labels["avg_a"].setText(f"{self._metrics['avg_a']:.2f}A")

    def _on_precision_changed(self, index: int) -> None:
        """精度切换：更新 downsample 并重拉当前会话（参数透传）。"""
        if not 0 <= index < len(_PRECISION_OPTIONS):
            return
        self._downsample = _PRECISION_OPTIONS[index][0]
        if self._session_id is not None:
            self.show_session(self._session_id, self._service, self._jobs)

    def _on_export(self) -> None:
        """导出 CSV：QFileDialog 选保存路径，门面下载转 Toast 回执。"""
        if self._service is None or self._jobs is None \
                or self._session_id is None or self._export_in_flight:
            return
        suggested = f"session_{self._session_id}.csv"
        save_path, _selected = QFileDialog.getSaveFileName(
            self, "导出会话 CSV", suggested, "CSV 文件 (*.csv)")
        if not save_path:
            return
        self._export_in_flight = True
        session_id = self._session_id
        self._jobs.submit(
            lambda: self._service.cuktech_export_session_csv(
                session_id, save_path),
            on_success=self._on_exported,
            on_error=self._on_export_error,
        )

    def _on_exported(self, path: Path) -> None:
        self._export_in_flight = False
        if not shiboken6.isValid(self):
            return
        Toast.info(self, f"CSV 已导出：{path}", 4000)

    def _on_export_error(self, error: Exception) -> None:
        self._export_in_flight = False
        if not shiboken6.isValid(self):
            return
        Toast.info(self, f"CSV 导出失败：{error}", 4000)

    def _on_error(self, error: Exception) -> None:
        self._in_flight = False
        if not shiboken6.isValid(self):
            return
        Toast.info(self, f"会话曲线加载失败：{error}", 3000)

    def retheme(self) -> None:
        """主题切换：曲线取色在绘制时动态读取，补一次重绘即可。"""
        self._curve.update()
        self._apply_inline_styles()

    def _apply_inline_styles(self) -> None:
        """内联样式：五项指标值/统计四联块主题色、说明灰字、按钮/下拉主题化。"""
        for label in self._metric_labels.values():
            label.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;")
        for label in self._stat_labels.values():
            label.setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        for cap in self.findChildren(QLabel):
            if cap.property("role") == "metric_caption":
                cap.setStyleSheet(
                    f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        self._period_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
        if hasattr(self, "_export_btn"):
            self._export_btn.setStyleSheet(
                f"QPushButton {{ background: {SiColors.SURFACE}; border: none;"
                f" border-radius: 8px; color: {SiColors.TEXT_PRIMARY};"
                f" font-size: 9pt; padding: 0 12px; }}"
                f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}")
        if hasattr(self, "_precision_combo"):
            apply_combo_qss(self._precision_combo)


class _SessionCurve(QWidget):
    """单会话曲线自绘体：网格线/填充曲线/峰值标注与
    cuktech_panel.PowerCurveWidget 同风格，另加底部 HH:MM 时间刻度
    （首/中/末三点）、协议切换竖虚线标注（仿上游 charge_history.js
    afterDraw 插件：协议变化点画竖虚线 + 协议名文字）与悬浮数据点
    提示（QToolTip「时间 · 功率」，协议非空时附协议行，对齐上游
    label/afterLabel 双行 tooltip）。取色经 SiColors 动态代理，重绘
    即随主题。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points: list[float] = []
        self._times: list[str] = []
        self._protocols: list[str] = []  # 与 points 对齐的协议序列
        self._max_w: float | None = None
        self._hover_index: int = -1
        self.setMouseTracking(True)

    def set_series(self, points: list[float], times: list[str],
                   max_w: float | None = None,
                   protocols: list[str] | None = None) -> None:
        """整组替换曲线数据：无效点丢弃、时间标签同步对齐、超限抽稀。"""
        pts: list[float] = []
        ts: list[str] = []
        protos: list[str] = []
        padded = list(times) + [""] * len(points)
        padded_protos = list(protocols or []) + [""]
        for i, (value, label) in enumerate(zip(points, padded)):
            try:
                pts.append(float(value))
            except (TypeError, ValueError):
                continue
            ts.append(label)
            proto = padded_protos[i] if i < len(padded_protos) else ""
            protos.append(str(proto or ""))
        if len(pts) > _CURVE_MAX_POINTS:
            stride = len(pts) / _CURVE_MAX_POINTS
            idx = [int(i * stride) for i in range(_CURVE_MAX_POINTS)]
            pts = [pts[i] for i in idx]
            ts = [ts[i] for i in idx]
            protos = [protos[i] for i in idx]
        self._points = pts
        self._times = ts
        self._protocols = protos
        self._hover_index = -1
        if max_w is None and pts:
            max_w = max(pts)
        self._max_w = max(float(max_w or 0.0), 1.0)  # 至少 1W，避免除零
        self.update()

    def protocol_marks(self) -> list[int]:
        """协议切换点索引列表（首点不计——起点不是「切换」）。"""
        marks: list[int] = []
        for i in range(1, len(self._points)):
            if i < len(self._protocols) and self._protocols[i] \
                    and self._protocols[i] != self._protocols[i - 1]:
                marks.append(i)
        return marks

    # ---------- 悬浮数据提示（上游 label/afterLabel 双行 tooltip） ----------

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
        lines = [f"{time_text} · {self._points[index]:.1f}W"
                 if time_text else f"{self._points[index]:.1f}W"]
        proto = self._protocols[index] if index < len(self._protocols) else ""
        if proto:  # 上游 afterLabel：协议非空才追加
            lines.append(f"协议: {proto}")
        return "\n".join(lines)

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
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                         "选择会话查看功率曲线")

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

        # 曲线主体：主题色填充 + 描边
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
        tx = max(float(rect.left()),
                 min(tx, rect.right() - metrics.horizontalAdvance(label)))
        baseline = peak_y - 8
        if baseline - metrics.ascent() < rect.top():
            baseline = peak_y + metrics.ascent() + 4
        painter.drawText(QPointF(tx, baseline), label)

        # 协议切换竖虚线标注：协议变化点自基线到顶画虚线 + 协议名
        # 文字（仿上游 charge_history.js afterDraw 插件）
        marks = self.protocol_marks()
        if marks:
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            for idx in marks:
                mx, _my = _xy(idx)
                dash_pen = QPen(QColor(SiColors.TEXT_MUTED), 1)
                dash_pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(dash_pen)
                painter.drawLine(QPointF(mx, rect.top()),
                                 QPointF(mx, rect.bottom()))
                proto_name = (self._protocols[idx]
                              if idx < len(self._protocols) else "") or ""
                if not proto_name:
                    continue
                metrics = painter.fontMetrics()
                tx = mx + 4
                if tx + metrics.horizontalAdvance(proto_name) > rect.right():
                    tx = mx - 4 - metrics.horizontalAdvance(proto_name)
                painter.setPen(QColor(SiColors.TEXT_SECONDARY))
                painter.drawText(QPointF(tx, rect.top() + metrics.ascent()),
                                 proto_name)

        # 时间轴：首/中/末 HH:MM 刻度（画在基线下方）
        if self._times:
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.setPen(QColor(SiColors.TEXT_MUTED))
            marks: list[tuple[int, str]] = [(0, "left"), (n - 1, "right")]
            mid = (n - 1) // 2
            if 0 < mid < n - 1:
                marks.append((mid, "center"))
            for idx, align in marks:
                text = self._times[idx] if idx < len(self._times) else ""
                if not text:
                    continue
                if align == "left":
                    tx = float(rect.left())
                elif align == "right":
                    tx = float(rect.right() - metrics.horizontalAdvance(text))
                else:
                    tx = (rect.left() + rect.right()
                          - metrics.horizontalAdvance(text)) / 2
                painter.drawText(QPointF(tx, float(rect.bottom()
                                                      + metrics.ascent() + 4)),
                                 text)

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
