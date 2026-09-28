# SPDX-License-Identifier: GPL-3.0-or-later
"""端口开关 pending 防回跳遮蔽测试（pytest 版，提交 d0b73ac 的回归防线）。

背景：端口开关写路径上，设备快照（SSE port_update 约 1s 一条 / 5s 轮询
整帧）滞后于用户操作——写命令提交到刷新数据落地之间到达的旧帧携带旧
enabled，会把开关视觉回跳回操作前状态，用户误以为失败再点一次造成反向
切换。修复分两层：

- PortCardGrid（app/ui/cuktech_stage.py）：begin_toggle(port) 后进入
  pending 遮蔽，_render_port 对 pending 口不同步开关与图标；pending 口
  被点击（disabled 尚未生效的窄竞态）时 _on_switch 立即回弹不发信号；
  end_toggle(port) 解除后恢复正常同步。
- CuktechPanel（app/ui/cuktech_panel.py）：done 回调不立即 end_toggle，
  而是记入 _pending_release[port]=(目标态, 提交时刻)；_render_ports_view
  在整帧渲染确认 enabled==目标态后统一释放；超时
  （_PENDING_RELEASE_TIMEOUT_S）兜底强制释放防卡死。

覆盖三层语义：
1. stage 层遮蔽/释放/点击回跳/非 pending 正常发信号；
2. stage 层整帧释放落定（遮蔽解除后旧帧能落回真实值）；
3. panel 层待释放账本：记入不立即释放、确认帧释放、不匹配帧不释放、
   超时兜底释放、失败立即释放回弹。

构造手法仿 tests/cuktech_panel_test.py：FakeService 只实现面板用到的
cuktech_* 签名，零网络；JobExecutor 用完 shutdown（atexit 兜底防
QThread 析构告警）。panel 构造成本可控（无主窗口依赖），故三层全测。
"""

import atexit
import copy
import os
import sys
import time
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体，指向系统字体目录让中文文本真实渲染
# （cuktech_panel_test.py 头注有完整原因说明）
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_stage_pending_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

# 测试禁写运行数据：仿 cuktech_panel_test，防假设备经任何回写路径泄漏
from app.core import cache as _device_cache

_device_cache.save = lambda *a, **k: None

from app.core.jobs import JobExecutor
from app.core.models import DeviceInfo

# ---------------------------------------------------------------------------
# 假数据：结构与 cuktech-api.md 契约一致（经 service 门面归一化后）
# ---------------------------------------------------------------------------

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
    "settings": {"5": 1, "16": 15},
    "device_model": "CUKTECH-10U",
    "firmware_version": "1.2.3",
}

FAKE_LIMITS = {"ok": True, "limits": {
    i: {"wh": 0.0, "mode": "once", "fired": False,
        "session_wh": 0.0, "is_charging": False} for i in (1, 2, 3, 4)}}

FAKE_CHART = {"ok": True, "labels": [], "datasets": {"power": [], "voltage": [],
                                                     "current": []}}


def _make_device() -> DeviceInfo:
    return DeviceInfo(did="cuk-1", name="CUKTECH 充电器",
                      model="cuktech.cu10u.charger", home_name="我的家庭",
                      room_name="书房", online=True, source="local")


class FakeService:
    """假门面：只实现面板用到的 cuktech_* 签名，零网络。"""

    def __init__(self):
        self.set_port_calls: list[tuple[int, bool]] = []

    def cuktech_status(self):
        return copy.deepcopy(FAKE_STATUS)

    def cuktech_charge_limits(self):
        return copy.deepcopy(FAKE_LIMITS)

    def cuktech_chart(self, hours: float = 1.0, interval: int = 30):
        return copy.deepcopy(FAKE_CHART)

    def cuktech_set_port(self, port: int, on: bool):
        self.set_port_calls.append((port, on))
        return {"ok": True, "value": 15 if on else 0}

    # 面板构造期不触发，但 refresh_data（含 SSE 后补拉）会拉曲线/限额；
    # 其余 cuktech_* 仅在对应 Tab 写路径被调，本文件不触达，缺省即可。


def _drain_jobs(rounds: int = 30) -> None:
    """等待串行队列清空并让回调经信号在主线程执行。"""
    for _ in range(rounds):
        time.sleep(0.02)
        app.processEvents()
    app.processEvents()


# ---------------------------------------------------------------------------
# 1. stage 层：pending 遮蔽期间旧帧不回跳开关与图标
# ---------------------------------------------------------------------------


def _drain_ani(switch) -> None:
    """跑空 progress_ani 动画队列（toggled 起跑的指数动画 tick 几轮落定）。

    offscreen 平台动画时钟驱动 QAbstractAnimation 统一计时，processEvents
    足够 tick；动画落定后 progress 与 checked 一致，断言不被在途动画噪声
    干扰（_sync_switch 本身会 stop 动画，pending 路径无需此函数）。
    """
    for _ in range(30):
        app.processEvents()
        if not switch.progress_ani.state():
            break


def test_stage_pending_holds_switch_and_icon(theme) -> None:
    """begin_toggle 后喂旧 enabled 帧：开关与图标不得回跳。

    场景：C2 设备当前关（enabled=False），用户点击打开 -> begin_toggle(2)
    进入 pending；此刻 SSE 旧帧仍报 enabled=False（快照滞后）——遮蔽生效
    时该帧不得回跳开关视觉（这是本修复的核心竞态）；对照组：不遮蔽的口
    同帧旧值正常落到开关。
    """
    from PySide6.QtTest import QSignalSpy

    from app.ui.cuktech_stage import PortCardGrid, _sync_switch

    grid = PortCardGrid()
    grid.show()
    app.processEvents()
    # 初态：C2 开关 on（设备真实态假设已打开，模拟面板整帧同步后）
    grid.update_port(2, {"power": 0.0, "enabled": True, "protocol": "idle"})
    assert grid._cards[2]["switch"].isChecked() is True
    icon_on = grid._cards[2]["icon"].pixmap()

    begin_spy = QSignalSpy(grid.port_toggle)
    grid.begin_toggle(2)  # 面板提交写命令前调用
    assert grid._pending_toggles == {2}

    # 旧帧到达（SSE 快照滞后：enabled=False）
    grid.update_port(2, {"power": 0.0, "enabled": False, "protocol": "idle"})
    app.processEvents()
    card = grid._cards[2]
    assert card["switch"].isChecked() is True, \
        "pending 口旧帧把开关回跳回操作前状态（防回跳失效）"
    assert card["switch"].progress == 1.0, \
        "pending 口旧帧把进度动画值回跳（_sync_switch 之外的同步路径漏堵）"
    assert card["icon"].pixmap().cacheKey() == icon_on.cacheKey(), \
        "pending 口旧帧把图标换成 off 版"
    assert begin_spy.count() == 0, "喂帧不应触发 port_toggle"

    # 对照组：未遮蔽的口同一旧帧语义正常同步
    grid.update_port(3, {"power": 9.0, "enabled": False, "protocol": "QC"})
    assert grid._cards[3]["switch"].isChecked() is False

    # 功率/协议等纯展示字段不受遮蔽影响（遮蔽只挡开关与图标）
    grid.update_port(2, {"power": 18.0, "enabled": False, "protocol": "QC"})
    assert grid._cards[2]["power"].text() == "18.0W"
    assert grid._cards[2]["proto"].text() == "QC"

    grid.hide()
    grid.deleteLater()


def test_stage_end_toggle_resumes_sync(theme) -> None:
    """end_toggle 后再喂帧恢复正常同步（整帧落到设备真实值）。

    场景链路：pending 期间确认帧（enabled==目标态）到达 -> 面板端
    _render_ports_view 调 end_toggle -> 遮蔽解除，此后帧正常驱动开关。
    """
    from app.ui.cuktech_stage import PortCardGrid

    grid = PortCardGrid()
    grid.show()
    app.processEvents()
    grid.update_port(1, {"power": 65.0, "enabled": True, "protocol": "PD"})
    assert grid._cards[1]["switch"].isChecked() is True

    grid.begin_toggle(1)
    # 遮蔽期间旧帧（enabled=False）不回跳
    grid.update_port(1, {"power": 65.0, "enabled": False, "protocol": "PD"})
    assert grid._cards[1]["switch"].isChecked() is True
    assert grid._cards[1]["icon"].pixmap().isNull() is False

    grid.end_toggle(1)
    assert 1 not in grid._pending_toggles
    # 解除后第一帧即同步：先喂目标态帧（设备已确认打开）
    grid.update_port(1, {"power": 30.0, "enabled": True, "protocol": "PD"})
    assert grid._cards[1]["switch"].isChecked() is True
    assert grid._cards[1]["power"].text() == "30.0W"
    # 再喂真实翻转帧：正常回跳（此时不再是竞态，是设备真实态）
    grid.update_port(1, {"power": 0.0, "enabled": False, "protocol": "idle"})
    assert grid._cards[1]["switch"].isChecked() is False

    grid.hide()
    grid.deleteLater()


def test_stage_end_toggle_is_idempotent(theme) -> None:
    """end_toggle 对未知端口安全（discard 语义，failed 与 done 竞态双调不炸）。"""
    from app.ui.cuktech_stage import PortCardGrid

    grid = PortCardGrid()
    grid.end_toggle(4)  # 从未 begin
    grid.end_toggle(4)
    assert grid._pending_toggles == set()
    grid.deleteLater()


def test_stage_pending_switch_click_bounces(theme) -> None:
    """pending 口开关被点击：立即回弹到 not on 且不发 port_toggle。

    正常路径下 pending 口 setEnabled(False) 点不到；这里防的是拥塞窗口
    内 disabled 尚未生效的极窄竞态——用户在 pending 期间再次点击，必须
    回弹视觉（_sync_switch 含 progress_ani.stop + setCurrentValue，动画
    在途时仅 setCurrentValue 会被下一个 tick 冲掉）且不重复发信号。
    """
    from PySide6.QtCore import QAbstractAnimation
    from PySide6.QtTest import QSignalSpy

    from app.ui.cuktech_stage import PortCardGrid, _sync_switch

    grid = PortCardGrid()
    grid.show()
    app.processEvents()
    # C2 初态关；用户点击 -> 面板 begin_toggle(2)（目标态 on）
    grid.update_port(2, {"power": 0.0, "enabled": False, "protocol": "idle"})
    switch = grid._cards[2]["switch"]
    spy = QSignalSpy(grid.port_toggle)
    switch.setChecked(True)  # 用户点击路径（toggled -> _on_switch 发信号）
    assert spy.count() == 1 and spy.at(0)[0] == 2 and spy.at(0)[1] is True

    grid.begin_toggle(2)
    # 窄竞态：pending 期间用户再次点击（disabled 尚未生效）。
    # _sync_switch(switch, on=True) 先模拟动画在途的真实点击瞬间
    _sync_switch(switch, True)  # 站位:点击后 switch 处于 on + 动画在途
    switch.progress_ani.start()  # 强制动画在途（模拟 _onChecked 起跑窗口）
    spy2 = QSignalSpy(grid.port_toggle)
    switch.setChecked(False)  # toggled(False) -> _on_switch(2, False, s)
    app.processEvents()
    assert 2 in grid._pending_toggles
    assert spy2.count() == 0, "pending 口点击不得重复发 port_toggle"
    assert switch.isChecked() is True, "pending 口点击未回弹到 not on"
    assert switch.progress == 1.0, "回弹未同步进度条（动画未停会被 tick 冲掉）"
    assert switch.progress_ani.state() == \
        QAbstractAnimation.State.Stopped, \
        "回弹未停 progress_ani，下次 tick 会把 progress 冲回 0"

    # 解除后同一开关恢复正常发信号（回弹路径只作用于 pending 期间）
    grid.end_toggle(2)
    switch.setChecked(False)
    assert spy2.count() == 1 and spy2.at(0)[0] == 2 and spy2.at(0)[1] is False

    grid.hide()
    grid.deleteLater()


def test_stage_nonpending_switch_click_emits(theme) -> None:
    """非 pending 口点击正常发 port_toggle(2, True)，未被遮蔽误伤。"""
    from PySide6.QtTest import QSignalSpy

    from app.ui.cuktech_stage import PortCardGrid

    grid = PortCardGrid()
    grid.show()
    app.processEvents()
    grid.update_port(2, {"power": 0.0, "enabled": False, "protocol": "idle"})
    spy = QSignalSpy(grid.port_toggle)
    grid._cards[2]["switch"].setChecked(True)
    app.processEvents()
    assert spy.count() == 1, spy.count()
    assert spy.at(0)[0] == 2 and spy.at(0)[1] is True
    # 其它口 pending 不影响本口（遮蔽按端口粒度）
    grid.begin_toggle(1)
    grid._cards[2]["switch"].setChecked(False)
    app.processEvents()
    assert spy.count() == 2 and spy.at(1)[0] == 2 and spy.at(1)[1] is False
    grid.end_toggle(1)
    grid.hide()
    grid.deleteLater()


# ---------------------------------------------------------------------------
# 2. stage 层经整帧注入的端到端语义（update_state 全四口）
# ---------------------------------------------------------------------------


def test_stage_update_state_frame_during_pending(theme) -> None:
    """pending 口经整帧注入（update_state）同样被遮蔽（两条喂帧路径一致）。

    _render_ports_view 走 update_state 整帧、SSE port_update 走
    update_port 单口增量，两条路径都汇入 _render_port，遮蔽必须对两条
    路径同样生效。
    """
    from app.ui.cuktech_stage import PortCardGrid

    grid = PortCardGrid()
    grid.update_state(dict(FAKE_STATUS))
    grid.show()
    app.processEvents()
    assert grid._cards[4]["switch"].isChecked() is False

    # 用户把 A 口打开：点击路径（非 pending，正常发信号）让开关先行乐观
    # on——pending 遮蔽的语义是「冻结点击瞬间的视觉」，由点击路径先落 on。
    # 图标不经点击路径（只随 _render_port 同步），此刻仍为 off 版
    grid._cards[4]["switch"].setChecked(True)
    app.processEvents()
    assert grid._cards[4]["switch"].isChecked() is True
    icon_frozen = grid._cards[4]["icon"].pixmap()

    grid.begin_toggle(4)  # 写命令提交，进入遮蔽
    stale = dict(FAKE_STATUS)  # 旧整帧仍报 enabled=False
    grid.update_state(stale)
    app.processEvents()
    card = grid._cards[4]
    assert card["switch"].isChecked() is True, \
        "整帧路径 pending 口未遮蔽（点击的乐观 on 被旧帧回跳）"
    assert card["icon"].pixmap().cacheKey() == icon_frozen.cacheKey(), \
        "pending 口图标随旧帧切换（遮蔽未冻结图标）"

    # 非 pending 口同帧正常同步（C3 从 on 变 off 的真实翻转）
    frame = dict(FAKE_STATUS)
    frame["ports"] = dict(FAKE_STATUS["ports"])
    frame["ports"]["3"] = dict(FAKE_STATUS["ports"]["3"], enabled=False)
    grid.update_state(frame)
    assert grid._cards[3]["switch"].isChecked() is False
    # pending 口经整帧后功率字段正常更新（遮蔽只挡开关与图标）
    assert grid._cards[4]["power"].text() == "1.5W"

    grid.hide()
    grid.deleteLater()


# ---------------------------------------------------------------------------
# 3. panel 层：「待释放」账本（_pending_release）
# ---------------------------------------------------------------------------


@pytest.fixture()
def panel_env():
    """构造 CuktechPanel + FakeService + JobExecutor，测试完清理。"""
    service = FakeService()
    jobs = JobExecutor()
    atexit.register(jobs.shutdown)
    from app.ui.cuktech_panel import CuktechPanel

    panel = CuktechPanel(service, jobs, _make_device())
    panel.resize(860, 600)
    panel.show_device(_make_device().did, online=True, device=panel._device)
    _drain_jobs()
    assert panel._status is not None, "面板初始拉取失败"
    yield panel, service, jobs
    panel._poll_timer.stop()
    panel.hide()
    panel.deleteLater()
    _drain_jobs(3)


def test_panel_toggle_done_holds_release_until_confirmed(theme, panel_env) -> None:
    """done 回调记入 _pending_release 不立即释放；确认帧（enabled==目标态）
    到达后 _render_ports_view 释放（end_toggle + 账本清空）。"""
    panel, _service, _jobs = panel_env
    from app.ui.cuktech_panel import _PENDING_RELEASE_TIMEOUT_S

    assert _PENDING_RELEASE_TIMEOUT_S > 1.0, "超时常量被测试意外改写"
    # ---- 细粒度断言：手工重放 done -> 确认帧 -> 释放三步 ----
    # 步骤 1：直接调 _on_port_toggle_done（跳过 service），断言记入不释放。
    # A 口初态关（FAKE），用户点击打开 -> 开关乐观 on（真实链路在
    # _on_port_toggle 里由用户点击路径先行落位）+ begin_toggle
    card4 = panel._port_grid._cards[4]
    card4["switch"].setChecked(True)
    app.processEvents()
    panel._port_grid.begin_toggle(4)
    panel._on_port_toggle_done(4, True)
    assert 4 in panel._port_grid._pending_toggles, \
        "done 后遮蔽应保持（不能立即 end_toggle）"
    assert panel._pending_release.get(4) is not None, "done 未记入待释放账本"
    on, submitted = panel._pending_release[4]
    assert on is True, f"账本目标态错误: {(on, submitted)}"
    assert submitted <= time.monotonic(), "提交时刻应在当前之前"
    assert card4["switch"].isEnabled() is True, "done 后开关应重新启用"

    # 步骤 2：喂「enabled 仍=False」的旧帧（SSE 滞后）——不释放
    stale = copy.deepcopy(FAKE_STATUS)
    panel._status = stale
    panel._render_status()
    assert 4 in panel._pending_release, "旧帧（不匹配）不应释放待释放口"
    assert 4 in panel._port_grid._pending_toggles
    # 遮蔽语义：整帧把开关冻结在点击时的乐观 on（旧帧 False 不回跳）
    assert panel._port_grid._cards[4]["switch"].isChecked() is True, \
        "panel 级 pending 释放前旧帧回跳了开关（stage 遮蔽未接上）"

    # 步骤 3：喂确认帧（enabled=True==目标态）-> 同一帧渲染后释放。
    # 注意释放发生在 update_state 之后：本帧渲染时口仍在遮蔽中（开关
    # 视觉保持 on 恰好正确），释放作用于后续帧。
    confirmed = copy.deepcopy(FAKE_STATUS)
    confirmed["ports"]["4"] = dict(confirmed["ports"]["4"], enabled=True)
    panel._status = confirmed
    panel._render_status()
    assert 4 not in panel._pending_release, "确认帧后账本未清空"
    assert 4 not in panel._port_grid._pending_toggles, "确认帧后未 end_toggle"
    assert panel._port_grid._cards[4]["switch"].isChecked() is True
    # 释放后的下一帧再喂旧值才恢复正常同步（不再是竞态窗口）
    panel._render_status()  # 确认帧重放（enabled=True）一致
    assert panel._port_grid._cards[4]["switch"].isChecked() is True

    # ---- 反向目标（关闭）链路同样成立：C1 目标 off ----
    card1 = panel._port_grid._cards[1]
    card1["switch"].setChecked(False)  # 用户关闭 C1 -> 开关落 off
    panel._port_grid.begin_toggle(1)
    panel._pending_release[1] = (False, time.monotonic())
    panel._render_status()  # FAKE: port1 enabled=True != 目标 False -> 不释放
    assert 1 in panel._pending_release
    assert card1["switch"].isChecked() is False, \
        "关闭目标：pending 期间旧帧（on）不得回跳开关"
    off_frame = copy.deepcopy(FAKE_STATUS)
    off_frame["ports"]["1"] = dict(off_frame["ports"]["1"], enabled=False)
    panel._status = off_frame
    panel._render_status()
    assert 1 not in panel._pending_release and \
        1 not in panel._port_grid._pending_toggles, "确认帧未释放"


def test_panel_mismatched_frame_never_releases(theme, panel_env) -> None:
    """enabled 与目标态不一致的帧不释放待释放口（继续等确认帧）。"""
    panel, _service, _jobs = panel_env
    # 手工构造待释放态：port 1 目标 on=False（用户刚关闭 C1）
    panel._port_grid.begin_toggle(1)
    panel._pending_release[1] = (False, time.monotonic())
    # 旧帧 enabled=True（不匹配目标 False）-> 不释放
    panel._status = copy.deepcopy(FAKE_STATUS)
    panel._render_status()
    assert panel._pending_release == {1: panel._pending_release[1]}, \
        "不匹配帧不应释放"
    assert 1 in panel._port_grid._pending_toggles

    # 多次不匹配帧后仍不释放（SSE 1s 一条，确认帧可能迟到多帧）
    for _ in range(3):
        panel._render_status()
    assert 1 in panel._pending_release, "多次不匹配帧后不应误释放"

    # 帧里缺该口条目（ports 键缺失）：enabled=None 也不释放
    partial = {"connected": True, "ports": {"2": FAKE_STATUS["ports"]["2"]}}
    panel._render_ports_view(partial)
    assert 1 in panel._pending_release, "缺口条目帧不应释放"

    # 确认帧到达（enabled=False==目标）-> 释放（释放作用于本帧渲染之后，
    # 开关视觉在下一帧才落定）
    off_frame = copy.deepcopy(FAKE_STATUS)
    off_frame["ports"]["1"] = dict(off_frame["ports"]["1"], enabled=False)
    panel._status = off_frame
    panel._render_status()
    assert 1 not in panel._pending_release, "确认帧未释放"
    assert 1 not in panel._port_grid._pending_toggles
    panel._render_status()  # 下一帧：遮蔽已解除，off 正常落位
    assert panel._port_grid._cards[1]["switch"].isChecked() is False


def test_panel_timeout_forces_release(theme, panel_env, monkeypatch) -> None:
    """超时兜底：_PENDING_RELEASE_TIMEOUT_S 调小后，不匹配帧也强制释放。"""
    panel, _service, _jobs = panel_env
    import app.ui.cuktech_panel as cp

    monkeypatch.setattr(cp, "_PENDING_RELEASE_TIMEOUT_S", 0.05)
    panel._port_grid.begin_toggle(1)
    panel._pending_release[1] = (False, time.monotonic())
    # 提交时刻已过超时（等待 > 0.05s），帧仍不匹配（enabled=True != False）
    time.sleep(0.08)
    panel._status = copy.deepcopy(FAKE_STATUS)
    panel._render_status()
    assert 1 not in panel._pending_release, "超时后未强制释放"
    assert 1 not in panel._port_grid._pending_toggles, "超时后未 end_toggle"
    # 超时释放是「防卡死兜底」：开关视觉允许短暂显示旧值，由后续真实帧
    # 落定——此处不断言开关位置，只断言遮蔽解除。


def test_panel_timeout_does_not_release_fresh_pending(theme, panel_env,
                                                      monkeypatch) -> None:
    """超时兜底不误伤新提交的待释放口（提交时刻在窗口内仍等确认帧）。"""
    panel, _service, _jobs = panel_env
    import app.ui.cuktech_panel as cp

    monkeypatch.setattr(cp, "_PENDING_RELEASE_TIMEOUT_S", 60.0)
    panel._port_grid.begin_toggle(1)
    panel._pending_release[1] = (False, time.monotonic())
    panel._status = copy.deepcopy(FAKE_STATUS)  # 不匹配帧
    panel._render_status()
    assert 1 in panel._pending_release, "超时窗口内不应强制释放"


def test_panel_toggle_failed_releases_immediately(theme, panel_env) -> None:
    """失败回调立即 end_toggle + 按最近一帧已知状态整帧重渲回弹。"""
    panel, _service, _jobs = panel_env
    # 构造在途态：C2 用户打开中（目标 on=True），SSE 旧帧曾把开关顶住
    panel._port_grid.begin_toggle(2)
    panel._port_grid._cards[2]["switch"].setEnabled(False)
    panel._pending_release[2] = (True, time.monotonic())
    # 构造失败前的最近已知状态：C2 关（FAKE）
    panel._status = copy.deepcopy(FAKE_STATUS)
    panel._on_port_toggle_failed(2, True, RuntimeError("BLE 写失败"))
    assert 2 not in panel._port_grid._pending_toggles, "失败后未立即解除遮蔽"
    assert 2 not in panel._pending_release, "失败后账本未清"
    card2 = panel._port_grid._cards[2]
    assert card2["switch"].isEnabled() is True, "失败后开关未重新启用"
    assert card2["switch"].isChecked() is False, \
        "失败回弹未按最近一帧已知状态渲染（_status 整帧重渲）"
    # _status 为 None（服务不可达）时用空帧渲染也不炸（空帧缺省 enabled=True）
    panel._status = None
    panel._port_grid.begin_toggle(1)
    panel._on_port_toggle_failed(1, True, RuntimeError("连接失败"))
    assert 1 not in panel._port_grid._pending_toggles


def test_panel_full_toggle_chain_no_stale_leak(theme, panel_env) -> None:
    """全链（真实点击 -> FakeService 成功 -> force 重拉 -> 确认帧）无残留。

    端到端回归：点击 C2 打开 -> set_port 成功 -> done 记账 +
    refresh_data(force=True) 重拉落地。FakeService 的 cuktech_status 恒
    返回 FAKE_STATUS（C2 enabled=False != 目标 True），重拉帧不释放——
    这恰好验证「done 后到确认帧之间遮蔽不解除」的窗口语义；手工喂确认
    帧（设备快照已翻转）收尾，断言链路完整走通且无 pending 残留。
    """
    panel, service, _jobs = panel_env
    card2 = panel._port_grid._cards[2]
    card2["switch"].setChecked(True)  # 真实点击链路
    _drain_jobs()
    assert service.set_port_calls[-1] == (2, True), service.set_port_calls
    # done 已跑完：force 重拉已落地（FakeService 帧不匹配 -> 账本保留）
    assert 2 in panel._pending_release, \
        "done 后确认帧未到（FakeService 帧不匹配）账本应保留"
    assert 2 in panel._port_grid._pending_toggles, \
        "确认帧未到前遮蔽不应解除（这正是修复语义）"
    # 喂确认帧（设备快照已翻转）-> 释放
    confirmed = copy.deepcopy(FAKE_STATUS)
    confirmed["ports"]["2"] = dict(confirmed["ports"]["2"], enabled=True)
    panel._status = confirmed
    panel._render_status()
    assert 2 not in panel._pending_release
    assert 2 not in panel._port_grid._pending_toggles
    assert card2["switch"].isChecked() is True
    assert card2["switch"].isEnabled() is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
