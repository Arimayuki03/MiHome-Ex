# SPDX-License-Identifier: GPL-3.0-or-later
"""CUKTECH 充电会话历史 + 能量统计 UI 自测：离屏运行，喂假数据断言渲染。

用法: .venv\\Scripts\\python.exe tests/cuktech_history_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_history_test）

仿 tests/cuktech_panel_test.py 的离屏 + grab() 模式：构造
SessionHistoryWidget / EnergySummaryWidget / SessionCurveWidget、灌假
sessions / energy-stats / energy-protocols / session-points 数据，断言
grab() 非全空白像素；暗/亮两种主题各来一遍 + retheme。service 用假门面
（只实现三个组件用到的 cuktech_* 签名），不发起任何网络请求。
"""

import atexit
import copy
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体（Qt 6 不再内置 fonts 目录）。qta.icon() 经
# QFontDatabase.addApplicationFont 注册 codicon 图标字体后，offscreen 的
# 字体回退引擎会把所有字体族解析到唯一存在的 codicon——它没有 CJK 字形，
# 中文 drawText 即零像素。指向系统字体目录让字体数据库有真实字体可回退。
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_history_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

# 测试禁写运行数据：仿 theme_test，防假数据经任何回写路径泄漏
from app.core import cache as _device_cache
_device_cache.save = lambda *a, **k: None

from app.ui import si_theme
from app.ui.theme_service import apply_theme

# ---------------------------------------------------------------------------
# 假数据：结构与 docs/dev-notes/cuktech-api.md §9 与「其他可用端点」一致
# ---------------------------------------------------------------------------

_NOW = datetime.now()
_TODAY_TS = _NOW.replace(hour=14, minute=30, second=0, microsecond=0).timestamp()
_YESTERDAY_TS = (_NOW - timedelta(days=1)).replace(
    hour=9, minute=5, second=0, microsecond=0).timestamp()

# 进行中的会话：end_time 为 null、is_active=true、时长为实时值
FAKE_SESSIONS_PAGE1 = {
    "sessions": [
        {"id": 42, "port": 1, "start_time": _TODAY_TS, "end_time": None,
         "total_wh": 32.5, "avg_power_w": 60.1, "peak_power_w": 65.0,
         "avg_voltage": 20.0, "avg_current": 3.0, "duration_sec": 3725,
         "protocol": "PD", "is_active": True},
        {"id": 41, "port": 4, "start_time": _TODAY_TS - 3600, "end_time": _TODAY_TS - 600,
         "total_wh": 4.2, "avg_power_w": 4.8, "peak_power_w": 5.1,
         "avg_voltage": 5.05, "avg_current": 0.95, "duration_sec": 3000,
         "protocol": "5V", "is_active": False},
    ],
    "total": 3, "page": 1, "limit": 50, "pages": 2,
}
FAKE_SESSIONS_PAGE2 = {
    "sessions": [
        # 昨天开始的会话：开始时间应显示 MM-DD HH:MM
        {"id": 40, "port": 2, "start_time": _YESTERDAY_TS,
         "end_time": _YESTERDAY_TS + 1800,
         "total_wh": 18.0, "avg_power_w": 36.0, "peak_power_w": 40.0,
         "avg_voltage": 9.0, "avg_current": 4.0, "duration_sec": 1800,
         "protocol": "QC", "is_active": False},
    ],
    "total": 3, "page": 2, "limit": 50, "pages": 2,
}

FAKE_ENERGY_STATS = {
    "period": "today",
    "total_wh": 54.7,
    "session_count": 3,
    "avg_power_w": 45.5,
    "peak_power_w": 65.0,
    "total_duration_sec": 8525,
    "by_port": {  # 服务端返回字符串键 "1"-"4"
        "1": {"wh": 32.5, "count": 1, "is_active": True},
        "2": {"wh": 18.0, "count": 1},
        "3": {"wh": 0.0, "count": 0},
        "4": {"wh": 4.2, "count": 1},
    },
}

FAKE_ENERGY_PROTOCOLS = {
    "period": "today",
    "protocols": [
        {"protocol": "PD", "wh": 32.5, "count": 1, "peak_w": 65.0,
         "is_active": True},
        {"protocol": "QC", "wh": 18.0, "count": 1, "peak_w": 40.0},
        {"protocol": "5V", "wh": 4.2, "count": 1, "peak_w": 5.1},
    ],
    "total_wh": 54.7,
    "session_count": 3,
}

# 单会话点级数据：60 个点，timestamp 为 Unix 秒浮点
FAKE_POINTS = {
    "points": [
        {"timestamp": _TODAY_TS - 3600 + i * 60, "voltage": 20.0,
         "current": 1.5 + (i % 10) / 10, "power": 30.0 + (i % 7) * 5.0,
         "protocol": "PD"}
        for i in range(60)
    ],
}

# 协议切换点列：前 30 点 PD、后 30 点 PPS（验证协议切换虚线标注）
FAKE_POINTS_PROTO_SWITCH = {
    "points": [
        {"timestamp": _TODAY_TS - 3600 + i * 60, "voltage": 20.0,
         "current": 1.5, "power": 40.0,
         "protocol": "PD" if i < 30 else "PPS"}
        for i in range(60)
    ],
}

# 五指标已知点列（人工可算）：功率 60W 恒定 2 小时（120 点、每点 1 分钟）
# 电量 = 60W × 2h = 120Wh；均功率 60W；峰功率 60W；均压 20V；均流 3A
_METRIC_BASE_TS = _TODAY_TS - 7200
FAKE_POINTS_KNOWN = {
    "points": [
        {"timestamp": _METRIC_BASE_TS + i * 60, "voltage": 20.0,
         "current": 3.0, "power": 60.0, "protocol": "PD"}
        for i in range(121)
    ],
}

# 每小时 24 桶：/api/chart?hours=24&interval=3600 的 Total 序列
FAKE_CHART_24H = {
    "ok": True,
    "labels": [f"{h:02d}:00" for h in range(24)],
    "datasets": {
        "power": [
            {"label": "C1", "data": [0.0] * 24},
            {"label": "Total", "data": [float(h * 10) for h in range(24)]},
        ],
    },
}


class FakeService:
    """假门面：只实现历史/统计/曲线组件用到的 cuktech_* 签名，零网络。

    记录 sessions 的调用参数（端口/周期/页码）用于断言筛选透传；
    fail=True 时所有方法抛异常（服务不可达路径）。
    """

    def __init__(self, fail=False):
        self.fail = fail
        self.session_calls: list[dict] = []
        self.points_calls: list[tuple[int, int]] = []
        self.stats_periods: list[str] = []
        self.protocol_periods: list[str] = []
        self.chart_calls: list[tuple[float, int]] = []
        self.export_calls: list[tuple[int, str]] = []
        # 每小时图数据可运行期替换（空图路径测试用）
        self.chart_payload: dict = copy.deepcopy(FAKE_CHART_24H)
        # 会话点列可运行期替换（协议切换/已知值测试用）
        self.points_payload: dict = copy.deepcopy(FAKE_POINTS)

    def cuktech_sessions(self, port=None, period="today", limit=10, page=1):
        self.session_calls.append(
            {"port": port, "period": period, "limit": limit, "page": page})
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        if page >= 2:
            return copy.deepcopy(FAKE_SESSIONS_PAGE2)
        return copy.deepcopy(FAKE_SESSIONS_PAGE1)

    def cuktech_session_points(self, session_id: int, downsample: int = 0):
        self.points_calls.append((session_id, downsample))
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        if session_id == 404:
            return None
        return copy.deepcopy(self.points_payload)

    def cuktech_energy_stats(self, period: str = "today"):
        self.stats_periods.append(period)
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        return copy.deepcopy(FAKE_ENERGY_STATS)

    def cuktech_energy_protocols(self, period: str = "today"):
        self.protocol_periods.append(period)
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        return copy.deepcopy(FAKE_ENERGY_PROTOCOLS)

    def cuktech_chart(self, hours: float = 1, interval: int = 30):
        self.chart_calls.append((hours, interval))
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        return copy.deepcopy(self.chart_payload)

    def cuktech_export_session_csv(self, session_id: int, save_path):
        self.export_calls.append((session_id, str(save_path)))
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        path = Path(save_path)
        path.write_text("timestamp,voltage,current,power\n", encoding="utf-8")
        return path


def _image_is_blank(image) -> bool:
    """空白判定：全图非背景像素 <= 16 视为空白（渲染失败/未绘制任何内容）。

    背景色取全图出现次数最多的颜色（众数，容忍圆角控件透明角像素）。
    必须全图统计——空态文案仅居中一行小字，步长抽样会漏采。
    """
    w, h = image.width(), image.height()
    counts: dict[str, int] = {}
    for x in range(w):
        for y in range(h):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    non_bg = w * h - max(counts.values())
    return non_bg <= 16


def _drain_jobs(rounds: int = 30) -> None:
    """等待串行队列清空并让回调经信号在主线程执行。"""
    for _ in range(rounds):
        time.sleep(0.02)
        app.processEvents()
    app.processEvents()


from app.core.jobs import JobExecutor
from app.ui.cuktech_history import (
    EnergySummaryWidget,
    SessionCurveWidget,
    SessionHistoryWidget,
    _compute_session_metrics,
)

from PySide6.QtCore import QEvent, QPointF, Qt as QtMod
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import QLabel, QScrollArea

service = FakeService()
jobs = JobExecutor()
# 收尾兜底：断言失败抛异常时跳过文件尾部的 jobs.shutdown()，JobExecutor
# 的工作 QThread 还在运行，解释器关闭阶段销毁它即报 "QThread: Destroyed
# while thread is still running"。atexit 先于 Qt 对象析构执行 join。
atexit.register(jobs.shutdown)


# ---------- 1. SessionHistoryWidget：列表渲染 / 筛选参数 / 翻页 ----------


def test_history(theme: str) -> None:
    apply_theme(theme)
    widget = SessionHistoryWidget()
    widget.resize(760, 480)
    widget.show()
    widget.set_service(service, jobs)
    _drain_jobs()

    # 列表渲染：第 1 页两行（含进行中会话）
    assert len(widget._session_rows) == 2, len(widget._session_rows)
    rows = widget._session_rows
    # 进行中会话：绿点可见 + 「充电中」标签
    active_tags = [l for l in rows[0].findChildren(QLabel)
                   if l.property("role") == "active_tag"]
    assert active_tags and active_tags[0].text() == "充电中"
    assert rows[0].findChildren(QLabel)[0].isVisibleTo(rows[0]), "绿点应可见"
    # 中文时长格式：3725 秒 -> "1h 2m"（第 1 行）；3000 秒 -> "50m"（第 2 行）
    texts = [l.text() for l in rows[0].findChildren(QLabel)]
    assert "1h 2m" in texts, texts
    assert "32.5Wh" in texts, texts
    assert "峰值 65.0W" in texts, texts
    assert "PD" in texts, texts
    # 进行中会话的区间文案「HH:MM → 充电中」与当天开始时间 HH:MM
    assert "14:30 → 充电中" in texts, texts
    assert "14:30" in texts[3], texts  # 区间以开始时间起头
    texts2 = [l.text() for l in rows[1].findChildren(QLabel)]
    assert "50m" in texts2, texts2
    app.processEvents()
    img = widget.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 会话列表渲染空白"

    # 筛选参数透传：首次 set_service 的调用
    call = service.session_calls[-1]
    assert call["port"] is None and call["period"] == "today", call
    assert call["limit"] == 50 and call["page"] == 1, call

    # 周期切换：改周期应重置页码并携带新参数
    widget._period_combo.setCurrentIndex(2)  # week
    _drain_jobs()
    call = service.session_calls[-1]
    assert call["period"] == "week" and call["page"] == 1, call
    # 端口筛选：C1 -> port=1
    widget._port_combo.setCurrentIndex(1)  # C1
    _drain_jobs()
    call = service.session_calls[-1]
    assert call["port"] == 1 and call["period"] == "week", call
    # 还原筛选
    widget._port_combo.setCurrentIndex(0)
    widget._period_combo.setCurrentIndex(0)
    _drain_jobs()

    # 翻页：第 1 页时上一页禁用、下一页可用；点下一页到第 2 页
    assert widget._prev_btn.isEnabled() is False
    assert widget._next_btn.isEnabled() is True
    assert widget._page_label.text() == "第 1/2 页"
    widget._next_btn.click()
    _drain_jobs()
    assert service.session_calls[-1]["page"] == 2
    assert widget._page_label.text() == "第 2/2 页"
    assert widget._next_btn.isEnabled() is False
    assert widget._prev_btn.isEnabled() is True
    # 跨天会话：开始时间显示 MM-DD HH:MM
    assert len(widget._session_rows) == 1
    day_labels = [l for l in widget._session_rows[0].findChildren(QLabel)
                  if l.property("role") == "time"]
    day_text = day_labels[0].text() if day_labels else ""
    assert day_text.startswith(
        (_NOW - timedelta(days=1)).strftime("%m-%d")), day_text
    # 第 2 页只剩一页，继续点下一页不发请求
    calls_before = len(service.session_calls)
    widget._next_btn.click()
    _drain_jobs()
    assert len(service.session_calls) == calls_before, "末页下一页不应再发请求"

    # 点击会话行 -> session_selected 信号带 session_id
    got: list[int] = []
    widget.session_selected.connect(got.append)
    release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(10, 10),
                          QtMod.MouseButton.LeftButton,
                          QtMod.MouseButton.LeftButton,
                          QtMod.KeyboardModifier.NoModifier)
    widget._session_rows[0].mouseReleaseEvent(release)
    assert got == [40], got

    # ---------- 布局回归：列表/分页条不重叠、间距 ≥12px、行高恒定 ----------
    # 列表容器不再内嵌 QScrollArea（嵌套滚动死锁回归护栏）
    assert not isinstance(widget._list_host, QScrollArea), \
        "会话列表不应再是 QScrollArea（整页滚动承载）"
    row_h, spacing = 56, 8
    for row in widget._session_rows:
        assert row.height() == row_h and row.width() > 0, \
            (row.height(), row.width())
    host = widget._list_host
    # 列表容器高 ≥ 内容高（行定高不压缩；多余空间留容器内底部）
    n_rows = len(widget._session_rows)
    content_h = n_rows * row_h + (n_rows - 1) * spacing
    assert host.height() >= content_h, (host.height(), content_h)
    # 每一行几何完整落在容器内（行不被横切）
    for row in widget._session_rows:
        assert host.geometry().contains(row.geometry().translated(host.pos())), \
            (host.geometry(), row.geometry())
    # 列表与分页条在父布局中 ≥12px 间距且几何不重叠
    gap = widget._pager_row.y() - (host.y() + host.height())
    assert gap >= 12, gap
    assert not host.geometry().intersects(widget._pager_row.geometry()), \
        (host.geometry(), widget._pager_row.geometry())
    # 行高固定常量与行布局间距同源
    import app.ui.cuktech_history as _hist
    assert _hist._SESSION_ROW_HEIGHT == 56
    assert widget._list_lay.spacing() == _hist._SESSION_ROW_SPACING

    # 空态：服务返回空列表时显示「暂无充电会话」
    service.fail = True
    widget.refresh()
    _drain_jobs()
    service.fail = False
    widget._on_sessions({"sessions": [], "total": 0, "page": 1,
                         "limit": 50, "pages": 1})
    app.processEvents()
    assert widget._empty_label.isVisibleTo(widget) or widget._empty_label.text() == "暂无充电会话"
    assert widget._page_label.text() == "第 1/1 页"
    assert widget._prev_btn.isEnabled() is False
    assert widget._next_btn.isEnabled() is False

    # retheme 不抛异常且行样式重设成功
    widget.retheme()
    app.processEvents()
    widget.hide()
    widget.deleteLater()
    print(f"1. SessionHistoryWidget 列表/筛选/翻页/信号/空态 OK [{theme}]")


# ---------- 2. EnergySummaryWidget：三块统计 + 分端口 + 协议分布 ----------


def test_summary(theme: str) -> None:
    apply_theme(theme)
    widget = EnergySummaryWidget()
    widget.resize(560, 520)
    widget.show()
    widget.set_service(service, jobs)
    _drain_jobs()

    # 三块统计数值
    assert widget._total_wh.text() == "54.7Wh", widget._total_wh.text()
    assert widget._session_count.text() == "3 次", widget._session_count.text()
    assert widget._power_range.text() == "45.5W / 65.0W", \
        widget._power_range.text()
    # 分端口明细（服务端字符串键 "1"-"4" 归一化）
    assert widget._port_rows[1]["wh"].text() == "32.5Wh"
    assert widget._port_rows[1]["count"].text() == "1 次"
    assert widget._port_rows[3]["wh"].text() == "0Wh"
    assert widget._port_rows[3]["count"].text() == "0 次"
    # 协议分布：按 Wh 降序（PD 32.5 -> QC 18 -> 5V 4.2），文案「PD · 65.2Wh · 3 次」样式
    protos = [row["proto"].text() for row in widget._protocol_rows]
    assert protos == ["PD", "QC", "5V"], protos
    details = [row["detail"].text() for row in widget._protocol_rows]
    assert details[0] == "32.5Wh · 1 次", details
    assert details[2] == "4.2Wh · 1 次", details
    app.processEvents()
    img = widget.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 能量统计渲染空白"

    # 周期切换参数透传（stats 与 protocols 同周期）
    widget._period_combo.setCurrentIndex(3)  # month
    _drain_jobs()
    assert service.stats_periods[-1] == "month"
    assert service.protocol_periods[-1] == "month"

    # 服务不可达：三块回落占位
    service.fail = True
    widget.refresh()
    _drain_jobs()
    service.fail = False
    assert widget._total_wh.text() == "—"
    assert widget._total_caption.text() == "总充电量"

    # 空协议分布：显示「暂无协议数据」
    widget._render_protocols({"protocols": []})
    app.processEvents()
    assert widget._protocols_empty.isVisibleTo(widget)

    # ---------- 三子 Tab：按端口/每小时/快充协议 ----------

    # 周期月切回今天再渲染一遍（供堆叠条断言有真实数据）
    widget._period_combo.setCurrentIndex(0)
    _drain_jobs()
    assert widget._deck.current_index == 0, "默认子 Tab 应为按端口"

    # 按端口 Tab：四色堆叠条渲染 + 行内占比/占比%/活跃口标签
    app.processEvents()
    img0 = widget._deck.grab().toImage()
    assert not _image_is_blank(img0), f"[{theme}] 按端口页渲染空白"
    # 占比：C1 32.5 / 54.7 = 59%（四舍五入）；USB-A 4.2 / 54.7 = 8%
    assert widget._port_rows[1]["pct"].text() == "59%", \
        widget._port_rows[1]["pct"].text()
    assert widget._port_rows[4]["pct"].text() == "8%", \
        widget._port_rows[4]["pct"].text()
    assert widget._port_rows[1]["live"].isVisibleTo(widget._port_rows[1]["frame"]), \
        "C1 是活跃口应显示「充电中」标签"
    assert widget._port_rows[2]["live"].isVisibleTo(widget._port_rows[2]["frame"]) \
        is False, "C2 非活跃口不显示标签"
    assert len(widget._share_bar._segments) == 4, "堆叠条应为四段"
    assert abs(widget._share_bar._segments[0][0] - 32.5 / 54.7) < 1e-6
    assert widget._share_bar._segments[0][1] == "#FF7A00", "C1 端口身份色"

    # 每小时 Tab：懒加载只拉一次、24 桶、柱图非空白
    chart_calls_before = len(service.chart_calls)
    assert chart_calls_before == 0 or theme == "light", \
        "未切入每小时 Tab 不应拉 chart（dark 首轮校验）"
    widget._tab_buttons[1].click()
    _drain_jobs()
    assert len(service.chart_calls) == chart_calls_before + 1, \
        f"切入每小时 Tab 应恰好拉一次：{service.chart_calls}"
    assert service.chart_calls[-1] == (24, 3600), service.chart_calls[-1]
    assert widget._hourly_chart.bucket_count() == 24, "应为 24 桶"
    assert not widget._hourly_empty.isVisibleTo(widget)
    img1 = widget._deck.grab().toImage()
    assert not _image_is_blank(img1), f"[{theme}] 每小时柱图渲染空白"
    # 柱图内容验证：24 桶填的值 = Total 序列（h*10）
    assert widget._hourly_chart._values[12] == 120.0, "第 12 桶应为 120Wh"
    assert widget._hourly_chart._labels[0] == "00:00", "首桶应保留完整 label"

    # ---------- Y 轴刻度回归：42px 数值带 + nice 刻度 + 0 线必标 ----------
    import app.ui.cuktech_history as _hist
    chart = widget._hourly_chart
    assert _hist._HOURLY_AXIS_W == 42
    # 跨度 = nice 步长 × 4 档：峰值 230×1.08=248.4 → 步长 nice 100 → 跨度 400
    peak = max(chart._values)
    ticks = chart.y_tick_values()
    assert ticks[0] == 0.0, ticks  # 0 线必标
    assert len(ticks) == 5, ticks  # 0 线 + 4 档参考线
    assert all(abs(t - round(t)) < 1e-9 for t in ticks), \
        f"刻度值应为整数 Wh：{ticks}"
    assert all(ticks[i] < ticks[i + 1] for i in range(len(ticks) - 1)), ticks
    assert ticks[-1] >= peak, "顶端跨度应罩住数据峰值"  # 400 ≥ 230
    assert ticks[-1] / 4 == chart._y_axis_max() / 4  # 步长=跨度/4（nice 档）
    # 像素验证：左数值带内应有 TEXT_MUTED 刻度文字（此前一根刻度都没有）
    img_axis = widget._deck.grab().toImage()
    band_colors: set[str] = set()
    for yy in range(0, img_axis.height(), 2):
        for xx in range(0, _hist._CURVE_MARGIN_L + _hist._HOURLY_AXIS_W):
            c = img_axis.pixelColor(xx, yy).name()
            band_colors.add(c)
    from app.ui.si_theme import SiColors as _SC
    assert QColor(_SC.TEXT_MUTED).name() in band_colors, \
        "左数值带应渲染出 Y 轴刻度文字"

    # ---------- 统计页间距回归：三块统计/子 Tab 行/堆叠条 ≥12px ----------
    root_spacing = widget.layout().spacing()
    assert root_spacing >= 12, root_spacing
    stats_block = widget._total_wh.parentWidget()
    gap_stats_tab = widget._tab_row.y() - (stats_block.y() + stats_block.height())
    assert gap_stats_tab >= 12, gap_stats_tab
    # 子 Tab 行与当前页（按端口页）不重叠：deck 页几何统一映射到 widget 坐标
    page0 = widget._deck.current_page()
    page0_top = page0.mapTo(widget, page0.rect().topLeft()).y()
    tab_row_bottom = widget._tab_row.mapTo(
        widget, widget._tab_row.rect().bottomLeft()).y()
    assert page0_top >= tab_row_bottom, \
        (page0_top, tab_row_bottom)
    # 四色堆叠条（页内顶部留白 12）与子 Tab 行间距 ≥12px
    share_top = widget._share_bar.mapTo(widget, widget._share_bar.rect().topLeft()).y()
    assert share_top - tab_row_bottom >= 12, \
        (share_top, tab_row_bottom)

    # ---------- 端口行不重叠回归：压矮 widget 行高仍 ≥ 文字需求 ----------
    # 历史缺陷：_TabDeck 无布局不上报尺寸需求，高度紧张时父布局把
    # deck 压到页面需求以下，行文字上下裁切、行间互相重叠（截图 bug）
    label_need = widget._port_rows[1]["name"].sizeHint().height()
    frame = widget._port_rows[1]["frame"]
    assert frame.minimumHeight() >= label_need, \
        (frame.minimumHeight(), label_need)
    assert widget._deck.minimumSizeHint().height() \
        >= widget._deck.current_page().minimumSizeHint().height(), \
        "deck 最小尺寸应上报当前页"
    assert widget._deck.sizeHint().height() \
        >= widget._deck.current_page().sizeHint().height(), \
        "deck 理想尺寸应上报当前页"
    for h in (widget.height(), 380, 300):
        widget.resize(widget.width(), h)
        app.processEvents()
        for port, row in widget._port_rows.items():
            fh = row["frame"].height()
            assert fh >= label_need, \
                f"高度{h}时行{port}高{fh} < 文字需求{label_need}"
    widget.resize(560, 520)
    app.processEvents()

    # 同周期重复切入不重拉（上游 hourly 图只在首次建图）
    widget._tab_buttons[2].click()
    _drain_jobs()
    widget._tab_buttons[1].click()
    _drain_jobs()
    assert len(service.chart_calls) == chart_calls_before + 1, \
        f"同周期切入不应重拉：{service.chart_calls}"

    # 周期变化清缓存：切周期后再进每小时 Tab 应重拉
    widget._tab_buttons[0].click()
    _drain_jobs()
    widget._period_combo.setCurrentIndex(2)  # week
    _drain_jobs()
    widget._tab_buttons[1].click()
    _drain_jobs()
    assert len(service.chart_calls) == chart_calls_before + 2, \
        "周期变化后切入应重拉每小时"
    assert service.chart_calls[-1] == (24, 3600), service.chart_calls[-1]
    assert service.stats_periods[-1] == "week", "周期变化 stats 同步换档"

    # 每小时空数据：占位标签可见、柱图隐藏
    service.chart_payload = {"ok": True, "labels": [],
                             "datasets": {"power": []}}
    widget._period_combo.setCurrentIndex(0)
    _drain_jobs()
    widget._tab_buttons[1].click()
    _drain_jobs()
    assert widget._hourly_empty.isVisibleTo(widget)
    assert widget._hourly_chart.isVisibleTo(widget) is False
    service.chart_payload = copy.deepcopy(FAKE_CHART_24H)

    # 快充协议 Tab：行内占比条（行结构 proto/bar/detail）
    widget._tab_buttons[2].click()
    _drain_jobs()
    assert widget._deck.current_index == 2
    assert len(widget._protocol_rows) == 3, widget._protocol_rows
    assert widget._protocol_rows[0]["bar"] is not None, "协议行应有占比条"
    # PD 32.5/54.7 ≈ 0.594
    assert abs(widget._protocol_rows[0]["bar"]._frac - 32.5 / 54.7) < 1e-6
    img2 = widget._deck.grab().toImage()
    assert not _image_is_blank(img2), f"[{theme}] 协议页渲染空白"

    # retheme 不抛异常（协议行样式重设路径）
    widget.retheme()
    app.processEvents()
    widget.hide()
    widget.deleteLater()
    print(f"2. EnergySummaryWidget 统计/三子Tab/堆叠条/每小时懒加载/协议占比 OK [{theme}]")


# ---------- 3. SessionCurveWidget：点级曲线 / 摘要 / clear ----------


def test_curve(theme: str) -> None:
    apply_theme(theme)
    # 复位可运行期被替换的会话点列（上一轮协议切换用例可能残留）
    service.points_payload = copy.deepcopy(FAKE_POINTS)
    widget = SessionCurveWidget()
    widget.resize(560, 360)
    widget.show()
    app.processEvents()

    # 空态可渲染（非空白：居中一行提示文字）
    img = widget.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 曲线空态渲染空白"

    # 拉取会话 42 的点级数据（downsample=200 缺省档）并绘制
    widget.show_session(42, service, jobs)
    _drain_jobs()
    assert service.points_calls[-1] == (42, 200), service.points_calls[-1]
    assert len(widget._curve._points) == 60
    assert widget._curve._times[0] == "13:30", widget._curve._times[0]
    # set_series 渲染非空白
    img = widget.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 会话曲线渲染空白"

    # 统计四联块（首次 show_session 随拉一次 energy_stats）
    assert widget._stat_labels["total_wh"].text() == "54.7Wh", \
        widget._stat_labels["total_wh"].text()
    assert widget._stat_labels["session_count"].text() == "3 次"
    assert widget._stat_labels["avg_power_w"].text() == "45.5W"
    assert widget._stat_labels["peak_power_w"].text() == "65.0W"

    # 五项指标（由 FAKE_POINTS 点列前端积分计算，非 — 占位）
    metrics = widget._metrics
    assert metrics["peak_w"] == 60.0, metrics
    assert metrics["wh"] > 0 and metrics["avg_w"] > 0, metrics
    assert abs(metrics["avg_v"] - 20.0) < 1e-6, metrics
    assert metrics["avg_a"] > 0, metrics

    # 五指标行文案渲染（电量/均功率/峰功率/均压/均流）
    assert widget._metric_labels["peak_w"].text() == "60.0W", \
        widget._metric_labels["peak_w"].text()
    assert widget._metric_labels["avg_v"].text() == "20.0V", \
        widget._metric_labels["avg_v"].text()
    assert widget._metric_labels["wh"].text().endswith("Wh")

    # 协议切换虚线：喂前 30 点 PD、后 30 点 PPS 的点列，切换点在索引 30
    service.points_payload = copy.deepcopy(FAKE_POINTS_PROTO_SWITCH)
    widget.show_session(42, service, jobs)
    _drain_jobs()
    marks = widget._curve.protocol_marks()
    assert marks == [30], marks
    assert widget._curve._protocols[30] == "PPS"
    # 虚线渲染验证：无切换（FAKE_POINTS 全 PD）与有切换的 grab 像素不同
    service.points_payload = copy.deepcopy(FAKE_POINTS)
    widget.show_session(42, service, jobs)
    _drain_jobs()
    assert widget._curve.protocol_marks() == []
    img_plain = widget.grab().toImage()
    service.points_payload = copy.deepcopy(FAKE_POINTS_PROTO_SWITCH)
    widget.show_session(42, service, jobs)
    _drain_jobs()
    img_marked = widget.grab().toImage()
    assert img_plain != img_marked, "协议切换虚线应改变曲线绘制（像素级）"
    # paintEvent 被调路径：直接绘制含 marks 的曲线体不抛异常且非空白
    app.processEvents()
    assert not _image_is_blank(img_marked), f"[{theme}] 协议虚线渲染空白"

    # 电压/电流摘要（由调用方以会话数据的 avg_* 字段传入）
    widget.set_summary(20.0, 3.0)
    app.processEvents()
    assert widget._summary_label.text() == "20.0V · 3.00A", \
        widget._summary_label.text()

    # 404（会话不存在）路径：payload None 不崩溃，曲线回到空态
    widget.show_session(404, service, jobs)
    _drain_jobs()
    assert widget._curve._points == []

    # clear 回空态、摘要清空、五指标回落「—」
    widget.show_session(42, service, jobs)
    _drain_jobs()
    widget.set_summary(9.0, 4.0)
    widget.clear()
    app.processEvents()
    assert widget._curve._points == [] and widget._summary_label.text() == ""
    assert widget._metric_labels["wh"].text() == "—", \
        widget._metric_labels["wh"].text()

    # 服务不可达：Toast + 空态不崩溃
    service.fail = True
    widget.show_session(42, service, jobs)
    _drain_jobs()
    service.fail = False
    assert widget._curve._points == []

    # 抽稀：500 点进 200 点上限，时间标签同步抽稀
    widget._curve.set_series([float(i) for i in range(500)],
                             [f"{i:04d}" for i in range(500)])
    assert len(widget._curve._points) <= 200
    assert len(widget._curve._times) == len(widget._curve._points)

    # ---------- 精度切换：重拉对应 downsample 参数 ----------
    widget._precision_combo.setCurrentIndex(1)  # 100 点
    _drain_jobs()
    assert service.points_calls[-1] == (42, 100), service.points_calls[-1]
    assert widget._downsample == 100
    widget._precision_combo.setCurrentIndex(4)  # 600 点
    _drain_jobs()
    assert service.points_calls[-1] == (42, 600), service.points_calls[-1]
    widget._precision_combo.setCurrentIndex(0)  # 全部
    _drain_jobs()
    assert service.points_calls[-1] == (42, 0), service.points_calls[-1]
    widget._precision_combo.setCurrentIndex(2)  # 还原 200 点档
    _drain_jobs()

    # ---------- CSV 导出：假服务落盘 + Toast 回执（QFileDialog 打桩） ----------
    import app.ui.cuktech_history as _mod
    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "session_42.csv"
        _mod.QFileDialog.getSaveFileName = (
            staticmethod(lambda *a, **k: (str(target), "CSV 文件 (*.csv)")))
        toasts: list[str] = []
        _orig_toast = _mod.Toast.info

        def _capture_toast(parent, text, duration_ms=4000):
            toasts.append(text)

        _mod.Toast.info = staticmethod(_capture_toast)
        try:
            widget._export_btn.click()
            _drain_jobs()
        finally:
            _mod.Toast.info = _orig_toast
        assert service.export_calls[-1][0] == 42, service.export_calls
        assert target.exists(), "CSV 应已落盘"
        assert "timestamp,voltage,current,power" in target.read_text(
            encoding="utf-8"), "落盘内容应为服务返回的 CSV 文本"
        assert toasts and "session_42.csv" in toasts[-1], toasts
        assert target.name in toasts[-1], "成功 Toast 应带保存路径"

        # 导出失败：错误 Toast、不崩溃
        service.fail = True
        _mod.Toast.info = staticmethod(_capture_toast)
        try:
            widget._export_btn.click()
            _drain_jobs()
        finally:
            _mod.Toast.info = _orig_toast
            service.fail = False
        assert toasts and "CSV 导出失败" in toasts[-1], toasts

    # retheme 后曲线重绘不抛异常
    widget.retheme()
    app.processEvents()
    widget.hide()
    widget.deleteLater()
    print(f"3. SessionCurveWidget 曲线/四联块/五指标/协议虚线/精度/导出 OK [{theme}]")


# ---------- 3.5 _compute_session_metrics：五指标已知值断言 ----------


def test_metrics() -> None:
    # 恒定 60W × 2h（121 点、1 分钟间隔）：电量 120Wh、均功率 60W、
    # 峰功率 60W、均压 20V、均流 3A
    metrics = _compute_session_metrics(FAKE_POINTS_KNOWN["points"])
    assert abs(metrics["wh"] - 120.0) < 1e-6, metrics
    assert abs(metrics["avg_w"] - 60.0) < 1e-6, metrics
    assert abs(metrics["peak_w"] - 60.0) < 1e-6, metrics
    assert abs(metrics["avg_v"] - 20.0) < 1e-6, metrics
    assert abs(metrics["avg_a"] - 3.0) < 1e-6, metrics

    # 空点列/缺字段：全 0 不抛异常
    empty = _compute_session_metrics([])
    assert empty == {"wh": 0.0, "avg_w": 0.0, "peak_w": 0.0,
                     "avg_v": 0.0, "avg_a": 0.0}, empty
    junk = _compute_session_metrics([{"power": "x"}, None, {}])
    assert junk["wh"] == 0.0, junk

    # 阶梯功率（前 60 点 30W、后 60 点 60W，各 1h）：电量 45Wh
    stepped = [
        {"timestamp": 1000.0 + i * 60, "voltage": 10.0, "current": 3.0,
         "power": 30.0 if i < 60 else 60.0, "protocol": "PD"}
        for i in range(120)
    ]
    metrics = _compute_session_metrics(stepped)
    assert abs(metrics["wh"] - 89.25) < 1e-6, metrics
    assert abs(metrics["peak_w"] - 60.0) < 1e-6, metrics
    assert abs(metrics["avg_w"] - 45.0) < 1e-6, metrics
    # 采样断档（>30min）不计能量：两段各 30W × 2min（梯形对）
    gapped = [
        {"timestamp": 1000.0 + i * 60, "voltage": 10.0, "current": 3.0,
         "power": 30.0, "protocol": "PD"} for i in range(3)
    ] + [
        {"timestamp": 1000.0 + 3600 * 3 + i * 60, "voltage": 10.0,
         "current": 3.0, "power": 30.0, "protocol": "PD"} for i in range(3)
    ]
    metrics = _compute_session_metrics(gapped)
    # 每段 2 个连续分钟对 × 1min × 30W = 1Wh，跨档对（>30min）丢弃 → 2Wh
    assert abs(metrics["wh"] - 2.0) < 1e-6, f"断档能量只算连续段：{metrics}"
    assert metrics["peak_w"] == 30.0, metrics
    print("3.5 五指标积分计算（恒定/阶梯/断档/空列） OK")


# ---------- 4. 暗色主题重复全部用例 ----------
test_history("dark")
test_summary("dark")
test_curve("dark")
test_metrics()

# ---------- 5. 亮色主题重复全部用例 ----------

test_history("light")
test_summary("light")
test_curve("light")

jobs.shutdown()
si_theme.set_theme("dark")
print("CUKTECH HISTORY TEST ALL PASS")
