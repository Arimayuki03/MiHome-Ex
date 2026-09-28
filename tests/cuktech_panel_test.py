# SPDX-License-Identifier: GPL-3.0-or-later
"""CUKTECH 专用 UI 三件套自测：离屏运行，喂假数据断言渲染。

用法: .venv\\Scripts\\python.exe tests/cuktech_panel_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_panel_test）

仿 tests/theme_test.py 的离屏 + grab() 模式：构造 CuktechDeviceCard 与
CuktechPanel、灌假 status/charge-limits/chart 数据，断言 grab() 非全
空白像素；暗/亮两种主题各来一遍。service 用假门面（只实现面板用到
的 cuktech_* 签名），不发起任何网络请求。

「实时」页整合断言（新 UI 组件链路）：舞台/端口卡类型断言、
update_state 后舞台渲染充电中、port_toggle → cuktech_set_port、
limit_commit（带 mode）→ cuktech_set_charge_limit、scene_selected →
cuktech_set_scene（含乐观 3s 保护）、延时快捷卡 → cuktech_set_delay_off、
SSE push_port_status 增量喂到舞台与端口卡、双主题 retheme 全链。

接线断言（端口详情弹窗 + quality + session_end）：port_clicked 打开
PortDetailDialog（类型断言）+ push_port_status/push_status/
push_protocol 转发喂数 + 弹窗关闭后引用清空；push_quality 整帧喂
设置 Tab 的 ConnectionQualityCard（文案断言）；on_session_end 在历史/
统计页曾加载过时触发其 refresh（monkeypatch 计数）且 5s 节流。
"""

import atexit
import copy
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体（Qt 6 不再内置 fonts 目录）。首次 qta.icon()
# 调用经 QFontDatabase.addApplicationFont 注册 codicon 图标字体后，
# offscreen 的字体回退引擎把所有字体族（含 "Microsoft YaHei UI"）都解析
# 到唯一存在的 codicon——它没有 CJK 字形，drawText 画中文即零像素，
# PowerCurveWidget 空态「暂无数据」渲染成整幅背景色（真 bug 的测试侧
# 表现）。指向系统字体目录让字体数据库有真实字体可回退，离屏渲染与
# 实际 GUI 一致。
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_panel_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

# 测试禁写运行数据：仿 theme_test，防假设备经任何回写路径泄漏
from app.core import cache as _device_cache
_device_cache.save = lambda *a, **k: None

from app.core.models import DeviceInfo
from app.ui import si_theme
from app.ui.theme_service import apply_theme

# ---------------------------------------------------------------------------
# 假数据：结构与 cuktech-api.md 契约一致（经 service 门面归一化后）
# ---------------------------------------------------------------------------

# 协议开关位图（PIID 21 解析视图）：c1 pd=False pps=True ufcs=True，其余全关
FAKE_PROTOCOL_SWITCHES = {
    "c1": {"pd": False, "pps": True, "ufcs": True},
    "c2": {"pd": False, "pps": False, "ufcs": False},
    "c3": {"ufcs": False, "scp": False},
    "a": {"ufcs": False, "scp": False},
}

FAKE_STATUS = {
    "connected": True,
    "authenticated": True,
    "ports": {
        "1": {"voltage": 20.0, "current": 3.25, "power": 65.0,
              "active": True, "protocol": "PD", "enabled": True},
        "2": {"voltage": 0.0, "current": 0.0, "power": 0.0,
              "active": False, "protocol": "idle", "enabled": False},
        "3": {"voltage": 9.0, "current": 1.0, "power": 9.0,
              "active": True, "protocol": "QC", "enabled": True},
        "4": {"voltage": 5.0, "current": 0.3, "power": 1.5,
              "active": True, "protocol": "5V", "enabled": False},
    },
    "settings": {"16": 15, "5": 1, "6": 2, "8": 0, "9": 0, "10": 0,
                 "11": 30, "12": 0, "13": 1, "15": 0, "19": 1, "20": 1,
                 "21": 0},
    # PIID 21 位图的解析视图（服务端 /api/status 总是下发该键），
    # c1 pps/ufcs 开，其余关——与 FAKE_PROTOCOL_SWITCHES 一致
    "protocol_switches": FAKE_PROTOCOL_SWITCHES,
    "device_model": "CUKTECH-10U",
    "firmware_version": "1.2.3",
}

FAKE_LIMITS = {
    "ok": True,
    "limits": {
        1: {"wh": 100.0, "mode": "once", "fired": False,
            "session_wh": 37.5, "is_charging": True},
        2: {"wh": 0.0, "mode": "once", "fired": False,
            "session_wh": 0.0, "is_charging": False},
        3: {"wh": 60.0, "mode": "always", "fired": True,
            "session_wh": 60.0, "is_charging": False},
        4: {"wh": 0.0, "mode": "once", "fired": False,
            "session_wh": 0.0, "is_charging": False},
    },
}

FAKE_CHART = {
    "ok": True,
    "labels": [f"{i // 60:02d}:{i % 60:02d}" for i in range(0, 120, 2)],
    "datasets": {
        "power": [
            {"label": "C1", "data": [0.0, 30.0, 60.0, 65.0, 65.0, 60.0,
                                     40.0, 20.0, 0.0, 0.0, 65.0, 65.0,
                                     64.0, 65.5, 65.0, 60.0, 30.0, 0.0,
                                     0.0, 10.0, 50.0, 65.0, 65.0, 0.0,
                                     0.0, 0.0, 20.0, 60.0, 65.0, 65.0,
                                     33.0, 0.0, 0.0, 0.0, 12.0, 44.0,
                                     65.0, 65.0, 65.0, 30.0, 0.0, 0.0,
                                     0.0, 0.0, 8.0, 25.0, 60.0, 65.0,
                                     65.0, 65.0, 40.0, 0.0, 0.0, 0.0,
                                     0.0, 15.0, 45.0, 65.0, 65.0, 20.0]},
            {"label": "C2", "data": [0.0] * 60},
            {"label": "C3", "data": [9.0] * 60},
            {"label": "A", "data": [1.5] * 60},
            {"label": "Total", "data": [10.5 + (i % 7) * 9.0 for i in range(60)]},
        ],
        "voltage": [], "current": [],
    },
}

# 空数据：服务不可达/无历史时的渲染路径
EMPTY_STATUS = {"connected": False, "ports": {}, "settings": {}}
EMPTY_LIMITS = {"ok": True, "limits": {
    i: {"wh": 0.0, "mode": "once", "fired": False,
        "session_wh": 0.0, "is_charging": False} for i in (1, 2, 3, 4)}}


FAKE_SESSIONS = {
    "sessions": [
        {"id": 7, "port": 1, "start_time": 1758860000.0, "end_time": 1758863725.0,
         "total_wh": 32.5, "avg_power_w": 60.1, "peak_power_w": 65.0,
         "avg_voltage": 20.0, "avg_current": 3.0, "duration_sec": 3725,
         "protocol": "PD", "is_active": False},
    ],
    "total": 1, "page": 1, "limit": 50, "pages": 1,
}

FAKE_POINTS = {
    "session_id": 7,
    "points": [
        {"timestamp": 1758860000.0 + i * 60, "voltage": 20.0, "current": 3.0,
         "power": 60.0} for i in range(60)
    ],
}


class FakeService:
    """假门面：只实现面板/卡片用到的 cuktech_* 签名，零网络。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.set_port_calls: list[tuple[int, bool]] = []
        self.set_limit_calls: list[tuple[int, float, str]] = []
        self.toggle_calls: list[bool] = []
        self.chart_calls: list[dict] = []
        # 分区 Tab 新链路：历史/统计/设置/协议的调用记录
        self.sessions_calls: list[dict] = []
        self.points_calls: list[tuple[int, int]] = []
        self.stats_periods: list[str] = []
        self.protocol_periods: list[str] = []
        self.scene_calls: list[int] = []
        self.delay_calls: list[tuple[int, int]] = []
        self.delay_all_calls: list[int] = []

    def cuktech_status(self):
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        return copy.deepcopy(FAKE_STATUS)

    def cuktech_charge_limits(self):
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        return copy.deepcopy(FAKE_LIMITS)

    def cuktech_chart(self, hours: float = 1.0, interval: int = 30):
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        self.chart_calls.append({"hours": hours, "interval": interval})
        return copy.deepcopy(FAKE_CHART)

    def cuktech_set_port(self, port: int, on: bool):
        self.set_port_calls.append((port, on))
        return {"ok": True, "value": 15 if on else 0}

    def cuktech_set_charge_limit(self, port: int, wh: float, mode: str = "once"):
        self.set_limit_calls.append((port, wh, mode))
        return {"ok": True}

    def cuktech_toggle_total(self, on: bool):
        self.toggle_calls.append(on)
        return on

    # ---------- 分区 Tab 新链路（历史/统计/设置/协议） ----------

    def cuktech_sessions(self, port=None, period="today", limit=10, page=1):
        self.sessions_calls.append(
            {"port": port, "period": period, "limit": limit, "page": page})
        return copy.deepcopy(FAKE_SESSIONS)

    def cuktech_session_points(self, session_id: int, downsample: int = 0):
        self.points_calls.append((session_id, downsample))
        if session_id == 404:
            return None
        return copy.deepcopy(FAKE_POINTS)

    def cuktech_energy_stats(self, period: str = "today"):
        self.stats_periods.append(period)
        if self.fail:
            raise RuntimeError("充电器服务连接失败")
        return {"total_wh": 32.5, "session_count": 1, "avg_power_w": 60.1,
                "peak_power_w": 65.0, "by_port": {"1": {"wh": 32.5, "count": 1}}}

    def cuktech_energy_protocols(self, period: str = "today"):
        self.protocol_periods.append(period)
        return {"protocols": [{"protocol": "PD", "wh": 32.5, "count": 1}]}

    def cuktech_set_scene(self, mode: int):
        self.scene_calls.append(mode)
        return {"ok": True}

    def cuktech_set_delay_off(self, port: int, minutes: int):
        self.delay_calls.append((port, minutes))
        return {"ok": True}

    def cuktech_set_delay_off_all(self, minutes: int):
        self.delay_all_calls.append(minutes)
        return {"ok": True}

    def cuktech_set_protocol_switch(self, port: int, protocol: str, on: bool):
        return None


def _image_is_blank(image) -> bool:
    """空白判定：全图非背景像素 <= 16 视为空白（渲染失败/未绘制任何内容）。

    判定标准：背景色取全图出现次数最多的颜色（众数，容忍圆角控件的
    透明角像素），逐像素统计与背景色不同的像素数，少于等于 16 判空。
    必须全图统计、不能步长抽样——曲线空态仅居中一行 9pt「暂无数据」，
    文字像素百余个、约占全图 0.2%，7 像素步长抽样恰好错过笔画时会
    全部漏采，曾把正常空态误判为空白（误报 "[dark] 曲线空态渲染
    空白"）。16 像素的绝对余量容忍孤立杂点，远低于一行文字的量级；
    正常渲染（文字/曲线/网格）的非背景像素远超该阈值。
    """
    w, h = image.width(), image.height()
    counts: dict[str, int] = {}
    for x in range(w):
        for y in range(h):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    non_bg = w * h - max(counts.values())
    return non_bg <= 16


def _make_device(online: bool = True) -> DeviceInfo:
    return DeviceInfo(did="cuk-1", name="CUKTECH 充电器", model="cuktech.cu10u.charger",
                      home_name="我的家庭", room_name="书房", online=online,
                      source="local")


from app.core.jobs import JobExecutor
from app.ui.cuktech_limits import ChargeLimitStack, DelayOffQuickCard
from app.ui.cuktech_panel import CuktechDeviceCard, CuktechPanel, PowerCurveWidget
from app.ui.cuktech_stage import DeviceStageWidget, MiniPhoneShareBar, PortCardGrid
from app.ui.cuktech_visuals import MultiMetricCurve, SceneButtonRow

service = FakeService()
jobs = JobExecutor()
# 收尾兜底：断言失败抛异常时跳过文件尾部的 jobs.shutdown()，JobExecutor
# 的工作 QThread 还在运行，解释器关闭阶段销毁它即报 "QThread: Destroyed
# while thread is still running"。atexit 先于 Qt 对象析构执行 join。
atexit.register(jobs.shutdown)

DEV = _make_device()

# ---------- 1. PowerCurveWidget：数据曲线与空态 ----------


def test_curve(theme: str) -> None:
    apply_theme(theme)
    widget = PowerCurveWidget()
    widget.resize(400, 160)
    widget.set_series(list(FAKE_CHART["datasets"]["power"][-1]["data"]))
    widget.show()
    app.processEvents()
    img = widget.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 曲线渲染空白"
    # 空态显示「暂无数据」也不该是空白图
    widget.set_series([])
    app.processEvents()
    empty_img = widget.grab().toImage()
    assert not _image_is_blank(empty_img), f"[{theme}] 曲线空态渲染空白"
    # 抽稀：500 点进 200 点上限
    widget.set_series([float(i) for i in range(500)])
    assert len(widget._points) <= 200, f"[{theme}] 抽稀失效"
    widget.hide()
    widget.deleteLater()
    print(f"1. PowerCurveWidget 渲染/空态/抽稀 OK [{theme}]")


# ---------- 2. CuktechDeviceCard：假状态灌入后渲染 ----------


def test_card(theme: str) -> None:
    apply_theme(theme)
    card = CuktechDeviceCard(DEV, service, jobs)
    card.resize(216, 92)
    card.show()
    app.processEvents()
    # 直接喂假状态（绕开轮询时序），断言渲染
    card._status = dict(FAKE_STATUS)
    card._render_status()
    app.processEvents()
    assert card._watts_label.text() == "75.5W", card._watts_label.text()
    assert card._status_label.text() == "3 口充电中"
    assert card._power_btn.state() is True
    img = card.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 卡片渲染空白"
    # 离线路径
    card._status = dict(EMPTY_STATUS)
    card._render_status()
    app.processEvents()
    assert card._online is False
    assert card._status_label.text() == "蓝牙未连接"
    assert card._watts_label.text() == "-- W"
    card.hide()
    card.deleteLater()
    print(f"2. CuktechDeviceCard 在线/离线渲染 OK [{theme}]")


# ---------- 3. CuktechPanel：假数据 + grab + 整合写路径 ----------


def test_panel(theme: str) -> None:
    apply_theme(theme)
    panel = CuktechPanel(service, jobs, DEV)
    panel.resize(860, 600)
    panel.show_device(DEV.did, online=True, device=DEV)
    _drain_jobs()

    # 头部指标（语义不变：Σ ports 功率 + 限额区会话累计）
    assert panel._watts_label.text() == "75.5W", panel._watts_label.text()
    assert panel._status_label.text() == "已连接"
    assert panel._session_label.text() == "本次会话已充 97.5Wh", \
        panel._session_label.text()

    # ---- Tab1 整合断言：舞台与端口卡类型断言 + 数据渲染 ----
    assert isinstance(panel._stage, DeviceStageWidget), type(panel._stage)
    assert isinstance(panel._port_grid, PortCardGrid), type(panel._port_grid)
    assert isinstance(panel._share_bar, MiniPhoneShareBar), \
        type(panel._share_bar)
    assert isinstance(panel._curve, MultiMetricCurve), type(panel._curve)
    assert isinstance(panel._limit_stack, ChargeLimitStack), \
        type(panel._limit_stack)
    # 轮询整帧喂入后：舞台渲染「充电中」底图路径（totalW>0）
    assert panel._stage._charging is True, "整帧喂入后舞台未进入充电中"
    # 端口卡：C1 功率 65.0W / 协议 PD / 负载条非空载
    card1 = panel._port_grid._cards[1]
    assert card1["power"].text() == "65.0W", card1["power"].text()
    assert card1["proto"].text() == "PD", card1["proto"].text()
    assert card1["load"]._idle is False, "C1 有输出但负载条仍空载"
    assert card4["power"].text() == "1.5W" if (card4 := panel._port_grid._cards[4]) else False
    # 占比条标签：C1 65.0W（分母=活跃口合计 65+9+1.5）
    assert "C1 65.0W" in panel._share_bar._labels[0].text(), \
        panel._share_bar._labels[0].text()
    # 曲线：整包喂入，labels 裁剪后 60 桶
    assert len(panel._curve._labels) == 60, len(panel._curve._labels)
    # 限额堆叠卡组：C1 进度行启用且进度文字正确（isHidden 而非
    # isVisible：面板顶层未 show，isVisible 恒 False）
    row1 = panel._limit_stack._rows[1]
    assert not row1["bar"].isHidden(), "C1 限额进度条未显示"
    assert row1["progress"].text() == "已充 37.5 / 100 Wh", \
        row1["progress"].text()

    app.processEvents()
    img = panel.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 面板渲染空白"

    # ---- 写路径 1：端口卡开关 port_toggle → cuktech_set_port ----
    card2 = panel._port_grid._cards[2]
    card2["switch"].setChecked(True)
    _drain_jobs()
    assert service.set_port_calls[-1] == (2, True), service.set_port_calls
    # 成功后立即重拉 status（refresh_data 记录曲线拉取也带档位）
    assert service.chart_calls, "端口开关成功后未重拉状态/曲线"

    # ---- 写路径 2：限额 limit_commit（带 mode）→ cuktech_set_charge_limit ----
    stack_row2 = panel._limit_stack._rows[2]
    stack_row2["edit"].setText("80")
    panel._limit_stack._on_set(2)
    _drain_jobs()
    assert service.set_limit_calls[-1] == (2, 80.0, "once"), \
        service.set_limit_calls
    # 长期有效模式透传
    stack_row2["combo"].setCurrentIndex(1)
    stack_row2["edit"].setText("50")
    panel._limit_stack._on_set(2)
    _drain_jobs()
    assert service.set_limit_calls[-1] == (2, 50.0, "always"), \
        service.set_limit_calls
    # 非法输入本地拦截（invalid_input → Toast，不发请求）
    before = len(service.set_limit_calls)
    stack_row2["edit"].setText("2000")
    panel._limit_stack._on_set(2)
    _drain_jobs()
    assert len(service.set_limit_calls) == before, "越界限额未被拦截"

    # ---- 写路径 3：曲线档位切换 → cuktech_chart(hours, interval) ----
    # （断言「存在该次拉取」而非「尾部一条」：5s 状态轮询若恰好在
    # _drain_jobs 等待窗口内到期，会拉一条当前档（1.0, 20）追加在
    # chart_calls 尾部，竞争窗口在 dark 段几乎不撞、light 段偶发撞上）
    panel._curve.range_changed.emit(2.0, 30)
    _drain_jobs()
    assert {"hours": 2.0, "interval": 30} in service.chart_calls, \
        service.chart_calls[-3:]
    assert panel._chart_range == 2.0

    # ---- 服务不可达路径 ----
    service.fail = True
    panel.refresh_data()
    _drain_jobs()
    assert panel._status_label.text() == "充电器服务连接失败"
    assert panel._watts_label.text() == "-- W"
    service.fail = False

    # ---- retheme 全链不抛异常（新组件级联下发） ----
    panel.retheme()
    app.processEvents()
    panel.hide()
    panel.deleteLater()
    print(f"3. CuktechPanel 数据渲染/整合写路径/离线/retheme OK [{theme}]")


def _drain_jobs(rounds: int = 30) -> None:
    """等待串行队列清空并让回调经信号在主线程执行。"""
    for _ in range(rounds):
        time.sleep(0.02)
        app.processEvents()
    app.processEvents()


# ---------- 3b. Tab 结构与历史页（lazy load + 会话曲线链路） ----------


def test_panel_history_tab(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    panel = CuktechPanel(service, jobs, DEV)
    panel.show_device(DEV.did, online=True, device=DEV)
    _drain_jobs()

    # Tab 结构：4 页、页序=实时/历史/统计/设置（独立协议 Tab 已移除，
    # 单口协议卡收敛进端口详情弹窗），默认停在实时
    assert panel._pages.count() == 4, panel._pages.count()
    assert panel._pages.currentIndex() == 0
    assert len(panel._tab_buttons) == 4
    assert not hasattr(panel, "_protocol_panel"), "独立协议 Tab 应已移除"

    # 未切入历史页前不拉取（lazy load 纪律）
    assert service.sessions_calls == [], "历史页未切入即拉取，违反 lazy load"

    # 切到历史页：set_service 注入触发首次拉取
    panel._select_tab(1)
    _drain_jobs()
    assert panel._history_widget._service is service, "历史组件未注入面板门面"
    assert panel._history_widget._jobs is jobs
    assert len(service.sessions_calls) == 1
    assert service.sessions_calls[-1]["period"] == "today"
    assert service.sessions_calls[-1]["limit"] == 50
    app.processEvents()
    img = panel.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 历史 Tab 渲染空白"

    # 再次切入不重复 set_service，只 refresh
    panel._select_tab(0)
    panel._select_tab(1)
    _drain_jobs()
    assert len(service.sessions_calls) == 2

    # 点击会话行：session_selected -> show_session（downsample=200）+
    # 摘要回填（avg_voltage/avg_current 来自 current_session_summary）
    panel._history_widget.session_selected.emit(7)
    _drain_jobs()
    assert service.points_calls[-1] == (7, 200), service.points_calls[-1]
    assert panel._session_curve._summary_label.text() == "20.0V · 3.00A", \
        panel._session_curve._summary_label.text()
    assert len(panel._session_curve._curve._points) == 60

    # 切走再切回历史页：曲线与摘要保持
    panel._select_tab(0)
    panel._select_tab(1)
    assert panel._session_curve._summary_label.text() == "20.0V · 3.00A"

    panel.hide()
    panel.deleteLater()
    print(f"3b. 历史 Tab lazy load/set_service 注入/会话曲线链路 OK [{theme}]")


# ---------- 3c. 设置 Tab：场景图标排 + 延时快捷卡 + 回填保护 ----------


def test_panel_settings_tab(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    panel = CuktechPanel(service, jobs, DEV)
    panel.show_device(DEV.did, online=True, device=DEV)
    _drain_jobs()

    # 组件类型断言：场景行/延时快捷卡已整合进设置页
    assert isinstance(panel._scene_row, SceneButtonRow), type(panel._scene_row)
    assert isinstance(panel._delay_card, DelayOffQuickCard), \
        type(panel._delay_card)

    # 轮询回填：settings["5"]=1 -> 场景行选中 AI（乐观值=1），
    # 6=2 -> 息屏下拉 10 分钟；延时快捷卡 PIID 11=30 -> C3 行「已设 30 分」
    assert panel._scene_row.current_scene() == 1, \
        f"场景行未按 PIID 5 回填: {panel._scene_row.current_scene()}"
    assert panel._timeout_combo.currentIndex() == 2, \
        f"息屏下拉未按 PIID 6 回填: {panel._timeout_combo.currentIndex()}"
    assert panel._idle_switch.isChecked() is True   # PIID 19 = 1
    assert panel._trickle_switch.isChecked() is False  # PIID 15 = 0
    assert panel._delay_card._rows[3]["value"].text() == "已设 30 分", \
        panel._delay_card._rows[3]["value"].text()

    # 切到设置 Tab 后，点击场景「单口」按钮（mode 3）：组件内乐观高亮
    # + 提交 cuktech_set_scene(3)，乐观保护期 3s 内轮询回报不覆盖乐观值
    panel._select_tab(3)
    app.processEvents()
    panel._scene_row._buttons[3].click()
    _drain_jobs()
    assert service.scene_calls == [3], service.scene_calls
    assert panel._scene_row.current_scene() == 3, "乐观更新未生效"
    # 保护期内轮询回报旧值 1：不覆盖乐观值
    panel._apply_settings({"5": 1})
    assert panel._scene_row.current_scene() == 3, "乐观保护期内回报回滚了选中值"
    # 保护期后回报：落定为设备真实值
    panel._scene_local_until = time.monotonic() - 1
    panel._apply_settings({"5": 2})
    assert panel._scene_row.current_scene() == 2, "保护期后回报未落定"

    # 延时关闭快捷档：端口 3 行点 60 -> cuktech_set_delay_off(3, 60)
    for btn in panel._delay_card._rows[3]["buttons"]:
        btn.setEnabled(True)  # 解除上一轮 pending（如有）
    panel._delay_card._pending.pop(3, None)
    panel._delay_card._on_quick(3, 60)
    _drain_jobs()
    assert service.delay_calls[-1] == (3, 60), service.delay_calls
    # 清除 = 提交 0
    panel._delay_card.commit_result(3, True)
    panel._delay_card._on_quick(3, 0)
    _drain_jobs()
    assert service.delay_calls[-1] == (3, 0), service.delay_calls
    panel._delay_card.commit_result(3, True)

    app.processEvents()
    img = panel.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 设置 Tab 渲染空白"
    panel.hide()
    panel.deleteLater()
    print(f"3c. 设置 Tab 场景乐观保护/延时快捷卡/回填保护 OK [{theme}]")


# ---------- 3d. 协议 Tab 已移除：无该 Tab + protocol 事件只走弹窗 ----------


def test_panel_protocol_tab(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    panel = CuktechPanel(service, jobs, DEV)
    panel.show_device(DEV.did, online=True, device=DEV)
    _drain_jobs()

    # 独立协议 Tab 已移除：4 个按钮、无第 5 页、无 ProtocolSwitchPanel 挂载
    assert panel._pages.count() == 4, panel._pages.count()
    assert len(panel._tab_buttons) == 4
    assert not hasattr(panel, "_protocol_panel"), "独立协议 Tab 应已移除"
    # 越界 Tab 选择安全（旧索引 4 现在无对应页）
    panel._select_tab(4)
    assert panel._pages.currentIndex() == 4 or panel._pages.currentIndex() < 4

    # protocol_switches 数据保留：SSE protocol 事件无弹窗时静默忽略（不炸）
    panel.push_protocol({"type": "protocol",
                         "switches": {"c1": {"pd": True, "pps": False,
                                             "ufcs": False}}})
    app.processEvents()
    # 非 protocol 事件同样静默忽略
    panel.push_protocol({"type": "settings", "settings": {"16": 15}})
    app.processEvents()

    # 打开端口详情弹窗后，push_protocol 只喂弹窗（单口协议卡按端口过滤）
    panel._port_grid.port_clicked.emit(1)
    app.processEvents()
    detail = panel._port_detail
    assert detail is not None, "port_clicked 后弹窗引用为空"
    assert detail._protocol_card._switches["pd"].isChecked() is False, \
        "弹窗协议卡初态来自 FAKE_STATUS（c1 pd 关）"
    panel.push_protocol({"type": "protocol",
                         "switches": {"c1": {"pd": True, "pps": False,
                                             "ufcs": False}}})
    app.processEvents()
    assert detail._protocol_card._switches["pd"].isChecked() is True, \
        "protocol 事件应转发给端口详情弹窗"
    # 弹窗收到协议事件后轮询整帧仍保留 protocol_switches（弹窗下次
    # 打开的初态来源）
    assert panel._status.get("protocol_switches") == FAKE_PROTOCOL_SWITCHES

    detail.close()
    app.processEvents()
    panel.hide()
    panel.deleteLater()
    print(f"3d. 协议 Tab 移除/push_protocol 无弹窗忽略+弹窗转发 OK [{theme}]")


# ---------- 3e. SSE 推送注入：增量喂到舞台与端口卡 ----------


def test_panel_sse_push(theme: str) -> None:
    apply_theme(theme)
    panel = CuktechPanel(service, jobs, DEV)
    panel.resize(860, 600)
    panel.show_device(DEV.did, online=True, device=DEV)
    _drain_jobs()

    # push_port_status 增量：port 2 从 0W 变 18W -> 舞台/端口卡/头部同步
    payload = {"type": "port_update", "port_id": 2, "port": "c2",
               "data": {"voltage": 9.0, "current": 2.0, "power": 18.0,
                        "active": True, "protocol": "QC", "enabled": True}}
    panel.push_port_status(copy.deepcopy(payload))
    app.processEvents()
    assert panel._port_grid._cards[2]["power"].text() == "18.0W", \
        panel._port_grid._cards[2]["power"].text()
    assert panel._stage._ports["2"]["power"] == 18.0, "舞台未收到增量"
    assert panel._watts_label.text() == "93.5W", panel._watts_label.text()
    # 占比条同步重算（C2 18.0W 参与占比）
    assert "C2 18.0W" in panel._share_bar._labels[1].text(), \
        panel._share_bar._labels[1].text()
    # 增量路径不该动限额堆叠（limits 不在 SSE 里，走轮询）
    assert panel._limit_stack._rows[1]["progress"].text() == "已充 37.5 / 100 Wh"

    # push_status 整帧：舞台/端口卡/占比条/场景行/延时快捷卡全部同步
    frame = copy.deepcopy(FAKE_STATUS)
    frame["type"] = "status"
    frame["ports"]["1"]["power"] = 30.0
    frame["settings"]["9"] = 45
    panel.push_status(frame)
    app.processEvents()
    assert panel._watts_label.text() == "40.5W", panel._watts_label.text()
    assert panel._port_grid._cards[1]["power"].text() == "30.0W"
    assert panel._stage._ports["1"]["power"] == 30.0
    assert panel._share_bar._labels[0].text().startswith("C1 30.0W"), \
        panel._share_bar._labels[0].text()
    # settings 整帧同步延时快捷卡（PIID 9=45 -> C1 行「已设 45 分」）
    assert panel._delay_card._rows[1]["value"].text() == "已设 45 分", \
        panel._delay_card._rows[1]["value"].text()

    # push_settings：位图 0 -> 端口卡开关全部关闭（enabled 位驱动）
    panel.push_settings({"type": "settings", "settings": {"16": 0}})
    app.processEvents()
    assert panel._port_grid._cards[1]["switch"].isChecked() is False
    assert panel._port_grid._cards[4]["switch"].isChecked() is False
    panel.push_settings({"type": "settings", "settings": {"16": 15}})
    app.processEvents()
    assert panel._port_grid._cards[1]["switch"].isChecked() is True

    panel.hide()
    panel.deleteLater()
    print(f"3e. SSE push_port_status/push_status/push_settings 整合注入 OK [{theme}]")


# ---------- 3f. 端口详情弹窗接线：port_clicked → 打开/喂数/关闭清理 ----------


def test_panel_port_detail_dialog(theme: str) -> None:
    apply_theme(theme)
    panel = CuktechPanel(service, jobs, DEV)
    panel.resize(860, 600)
    panel.show_device(DEV.did, online=True, device=DEV)
    _drain_jobs()
    assert panel._status is not None

    # ---- 打开：port_clicked(port) → _port_detail 为 PortDetailDialog ----
    panel._port_grid.port_clicked.emit(1)
    app.processEvents()
    detail = panel._port_detail
    assert detail is not None, "port_clicked 后弹窗引用为空"
    from app.ui.cuktech_port_detail import PortDetailDialog
    assert isinstance(detail, PortDetailDialog), type(detail)
    assert detail._port == 1, detail._port
    # 构造快照：本口数据来自面板当前 _status（C1 65W/PD）
    assert detail._w_value.text() == "65.0", detail._w_value.text()
    assert detail._p_value.text() == "PD", detail._p_value.text()
    assert len(detail._samples) == 1, "构造快照未填缓冲首点"
    # 协议开关初态来自面板整帧 protocol_switches（FAKE c1 pps=True）
    assert detail._protocol_card._switches["pps"].isChecked() is True

    # ---- 单口增量转发：push_port_status → push_port_sample（非本口忽略）----
    sample = {"voltage": 20.0, "current": 2.5, "power": 55.0,
              "active": True, "protocol": "PD", "enabled": True}
    panel.push_port_status({"type": "port_update", "port_id": 2, "port": "c2",
                            "data": {"voltage": 9.0, "current": 0.1,
                                     "power": 0.5, "protocol": "QC"}})
    panel.push_port_status({"type": "port_update", "port_id": 1, "port": "c1",
                            "data": dict(sample)})
    app.processEvents()
    assert len(detail._samples) == 2, \
        f"弹窗应只收到本口样本（1 快照 + 1 增量），实际 {len(detail._samples)}"
    assert detail._w_value.text() == "55.0", detail._w_value.text()

    # ---- 整帧兜底转发：push_status 的本口条目同样喂入 ----
    frame = copy.deepcopy(FAKE_STATUS)
    frame["type"] = "status"
    panel.push_status(frame)
    app.processEvents()
    assert len(detail._samples) == 3, \
        f"push_status 本口条目应喂入，实际 {len(detail._samples)}"

    # ---- 协议事件转发：push_protocol → apply_protocol_event ----
    panel.push_protocol({"type": "protocol",
                         "switches": {"c1": {"pd": True, "pps": False,
                                             "ufcs": False}}})
    app.processEvents()
    assert detail._protocol_card._switches["pps"].isChecked() is False, \
        "protocol 事件未转发到弹窗协议卡"

    # ---- 关闭：finished 回调清引用（shiboken6 判活）----
    from shiboken6 import isValid
    detail.close()
    app.processEvents()
    assert panel._port_detail is None, "弹窗关闭后面板引用未清空"
    assert not isValid(detail), "关闭后弹窗对象应进入删除流程"

    # 关闭后再推送不炸（引用已清，转发点静默跳过）
    panel.push_port_status({"type": "port_update", "port_id": 1, "port": "c1",
                            "data": dict(sample)})
    app.processEvents()
    panel.hide()
    panel.deleteLater()
    print(f"3f. port_clicked 开弹窗/三路转发喂数/关闭清引用 OK [{theme}]")


# ---------- 3g. quality 注入 + session_end 节流刷新历史/统计 ----------


def test_panel_quality_and_session_end(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    panel = CuktechPanel(service, jobs, DEV)
    panel.show_device(DEV.did, online=True, device=DEV)
    _drain_jobs()

    # ---- push_quality：喂设置 Tab 底部的 ConnectionQualityCard ----
    from app.ui.cuktech_quality import ConnectionQualityCard
    assert isinstance(panel._quality_card, ConnectionQualityCard), \
        type(panel._quality_card)
    payload = {"type": "quality",
               "ble": {"score": 87, "decrypt": 99, "notify": 82,
                       "reconnect_score": 100, "reconnect_count_5m": 0,
                       "uptime": 3725, "last_push_age": 3,
                       "next_reconnect_delay": None},
               "mqtt": {"score": 0, "uptime": 0, "disconnects": 0,
                        "publish_failures": 0},
               "bemfa": {}}
    panel.push_quality(payload)
    app.processEvents()
    groups = panel._quality_card._groups
    assert groups["ble"]._score_label.text() == "87", \
        groups["ble"]._score_label.text()
    assert groups["ble"]._value_labels["connectionDuration"].text() == "1h2m", \
        groups["ble"]._value_labels["connectionDuration"].text()
    # MQTT score=0 -> 未启用灰字
    assert groups["mqtt"]._disabled_label.text() == "未启用"
    # 切到设置 Tab 渲染不空白（懒加载无负担：构造期即建好）
    panel._select_tab(3)
    app.processEvents()
    assert not _image_is_blank(panel.grab().toImage()), \
        f"[{theme}] 设置 Tab（含连接质量卡）渲染空白"

    # ---- on_session_end：历史/统计曾加载过才 refresh，且 5s 节流 ----
    history_calls: list[tuple] = []
    energy_calls: list[tuple] = []
    panel._history_widget.refresh = \
        lambda manual=False: history_calls.append(("history", manual))
    panel._energy_widget.refresh = \
        lambda manual=False: energy_calls.append(("stats", manual))

    # 未加载过（lazy load 纪律）：两页都不刷
    panel._history_loaded = False
    panel._stats_loaded = False
    panel.on_session_end({"type": "session_end"})
    assert history_calls == [] and energy_calls == [], "未加载页不应刷新"

    # 曾加载过：两页都触发 refresh(manual=False)
    panel._history_loaded = True
    panel._stats_loaded = True
    panel.on_session_end({"type": "session_end"})
    assert history_calls == [("history", False)], history_calls
    assert energy_calls == [("stats", False)], energy_calls

    # 5s 节流：窗口内的连续 session_end 不再刷新
    panel.on_session_end({"type": "session_end"})
    panel.on_session_end({"type": "session_end"})
    assert len(history_calls) == 1, f"节流失效: {history_calls}"
    assert len(energy_calls) == 1, f"节流失效: {energy_calls}"

    # 节流窗口过后恢复刷新
    panel._last_session_end_refresh = time.monotonic() - 6.0
    panel.on_session_end({"type": "session_end"})
    assert len(history_calls) == 2, f"节流窗口后未恢复刷新: {history_calls}"

    panel.hide()
    panel.deleteLater()
    print(f"3g. push_quality 文案/on_session_end 条件刷新与 5s 节流 OK [{theme}]")


# ---------- 4. 暗色主题重复全部用例 ----------

test_curve("dark")
test_card("dark")
test_panel("dark")
test_panel_history_tab("dark")
test_panel_settings_tab("dark")
test_panel_protocol_tab("dark")
test_panel_sse_push("dark")
test_panel_port_detail_dialog("dark")
test_panel_quality_and_session_end("dark")

# ---------- 5. 亮色主题重复全部用例 ----------

test_curve("light")
test_card("light")
test_panel("light")
test_panel_history_tab("light")
test_panel_settings_tab("light")
test_panel_protocol_tab("light")
test_panel_sse_push("light")
test_panel_port_detail_dialog("light")
test_panel_quality_and_session_end("light")

# ---------- 6. 离线设备的卡片（device.online=False） ----------

offline_card = CuktechDeviceCard(_make_device(online=False), service, jobs)
offline_card.show()
app.processEvents()
assert not offline_card._online or offline_card._status is not None
offline_card.hide()
offline_card.deleteLater()
print("6. 离线注册卡片构造 OK")

# ---------- 7. 列宽一致性：专用卡与通用卡同尺寸 ----------

from app.ui.device_card import _CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT
assert (_CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT) == (216, 92)
from app.ui.cuktech_panel import _CARD_FIXED_WIDTH as _CUK_W, _CARD_FIXED_HEIGHT as _CUK_H
assert (_CUK_W, _CUK_H) == (_CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT), \
    "专用卡与通用卡尺寸不一致，需同步 _columns_for_width"
print("7. 卡片尺寸与通用卡一致（列宽计算无需改动）OK")

jobs.shutdown()
si_theme.set_theme("dark")
print("CUKTECH PANEL TEST ALL PASS")
