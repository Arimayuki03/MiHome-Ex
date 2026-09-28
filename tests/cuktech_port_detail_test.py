# SPDX-License-Identifier: GPL-3.0-or-later
"""CUKTECH 端口详情弹窗自测：离屏运行，假门面 + 假数据断言渲染与写路径。

用法: .venv\\Scripts\\python.exe tests/cuktech_port_detail_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_port_detail_test）

仿 tests/cuktech_protocols_test.py 的离屏 + grab() 模式：构造
PortDetailDialog（OverlayDialog 壳 + 假 status 单口快照 + 假
protocol_switches），断言：
- 构造渲染：端口名/四读数初值来自 status_port_data，协议开关初态
  来自 protocol_switches；
- push_port_sample 喂 5 个点：环形缓冲 5 点、曲线 grab 非空白、
  功率读数更新、非本端口数据被忽略；
- 120 点环形：喂 130 点断言缓冲 len==120（读内部 _samples）；
- 数据未更新兜底：无新数据超 10s 后曲线置 stale 灰字标注，新数据
  到达自动解除；
- 协议开关行：切换调用参数（port/protocol/on）、busy 禁用、busy 期间
  注入不覆盖、成功不乐观回写、失败回滚、门面缺失禁用+tooltip；
- apply_protocol_event 更新开关态（非 protocol 事件与畸形结构容错）；
- 暗/亮双主题 grab 非空白 + retheme 不抛异常。
service 用假门面（只实现弹窗用到的签名），不发起任何网络请求。
"""

import atexit
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体，Qt 字体回退引擎会解析到无 CJK 字形的图标
# 字体导致中文零像素（见 cuktech_panel_test.py 同款注释）。指向系统
# 字体目录让离屏渲染与实际 GUI 一致。
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_port_detail_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

# 测试禁写运行数据：仿 theme_test，防假设备经任何回写路径泄漏
from app.core import cache as _device_cache
_device_cache.save = lambda *a, **k: None

from app.ui import si_theme
from app.ui.theme_service import apply_theme

# ---------------------------------------------------------------------------
# 假数据：结构与 cuktech-api.md 契约一致（经 service 门面归一化后）
# ---------------------------------------------------------------------------

# 打开瞬间的单口快照（/api/status 的 ports["1"] 条目）
PORT_DATA_A = {"voltage": 20.0, "current": 3.25, "power": 65.0,
               "active": True, "protocol": "PD", "enabled": True}
PORT_DATA_B = {"voltage": 9.0, "current": 2.0, "power": 18.0,
               "active": True, "protocol": "QC", "enabled": True}

# 协议开关帧（四口全量，弹窗取 c1）：c1 pd=开 pps=关 ufcs=开
PROTOCOL_SWITCHES = {
    "c1": {"pd": True, "pps": False, "ufcs": True},
    "c2": {"pd": False, "pps": False, "ufcs": False},
    "c3": {"ufcs": False, "scp": False},
    "a": {"ufcs": False, "scp": False},
}

EVENT_B = {"type": "protocol",
           "switches": {"c1": {"pd": False, "pps": True, "ufcs": False}}}


class FakeService:
    """假门面：只实现弹窗用到的签名，零网络。

    mode:
    - "ok"      正常成功
    - "fail"    抛异常（走 on_error 回滚路径）
    - "missing" 不提供 cuktech_set_protocol_switch（门面未落地防御路径）
    """

    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.calls: list[tuple[int, str, bool]] = []  # (port, protocol, on)

    def cuktech_set_protocol_switch(self, port: int, protocol: str, on: bool):
        self.calls.append((port, protocol, on))
        if self.mode == "fail":
            raise RuntimeError("充电器离线：蓝牙未连接，命令未下发")
        return None


class MissingFacade:
    """没有 cuktech_set_protocol_switch 的门面（防御路径用）。"""


def _image_is_blank(image) -> bool:
    """空白判定：全图非背景像素 <= 16 视为空白（与 protocols 测试同标准）。"""
    w, h = image.width(), image.height()
    counts: dict[str, int] = {}
    for x in range(w):
        for y in range(h):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    non_bg = w * h - max(counts.values())
    return non_bg <= 16


def _has_theme_pixels_in_band(image, x0: int, x1: int) -> bool:
    """竖直带 [x0,x1) 内是否存在主题色像素（曲线/填充着色判定）。"""
    x0 = max(0, x0)
    x1 = min(image.width(), x1)
    for x in range(x0, x1):
        for y in range(image.height()):
            c = image.pixelColor(x, y)
            if abs(c.red() - 0x3D) < 40 and abs(c.green() - 0xBB) < 40 \
                    and abs(c.blue() - 0xA4) < 40:
                return True
    return False


def _drain_jobs(rounds: int = 30) -> None:
    """等待串行队列清空并让回调经信号在主线程执行。"""
    for _ in range(rounds):
        time.sleep(0.02)
        app.processEvents()
    app.processEvents()


def _switch(dialog, proto: str):
    return dialog._protocol_card._switches[proto]


from app.core.jobs import JobExecutor
from app.ui.cuktech_port_detail import PortDetailDialog

jobs = JobExecutor()
# 收尾兜底：断言失败抛异常时跳过文件尾部的 jobs.shutdown()，JobExecutor
# 的工作 QThread 还在运行，解释器关闭阶段销毁它即报 "QThread: Destroyed
# while thread is still running"。atexit 先于 Qt 对象析构执行 join。
atexit.register(jobs.shutdown)

# 宿主窗口：弹窗遮罩铺父窗口客户区（OverlayDialog._fill_parent_window）
_HOST = None


def _make_dialog(service, port: int = 1,
                 port_data: dict | None = PORT_DATA_A,
                 switches: dict | None = PROTOCOL_SWITCHES) -> PortDetailDialog:
    global _HOST
    if _HOST is None:
        from PySide6.QtWidgets import QWidget
        _HOST = QWidget()
        _HOST.resize(720, 620)
        _HOST.show()
    dialog = PortDetailDialog(_HOST, service, jobs, port,
                              port_data or {}, switches or {})
    dialog.show()
    app.processEvents()
    return dialog


def test_construct_render(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    dialog = _make_dialog(service)
    app.processEvents()

    # 头部：端口名 + 端口色圆点
    assert dialog._name_label.text() == "C1"
    assert dialog._dot_label.styleSheet() != ""

    # 四大读数初值来自 status_port_data
    assert dialog._v_value.text() == "20.0"
    assert dialog._a_value.text() == "3.25"
    assert dialog._w_value.text() == "65.0"
    assert dialog._p_value.text() == "PD"
    # 协议徽标非 idle 高亮（样式含主题色边框）
    assert si_theme.SiColors.THEME in dialog._p_value.styleSheet()

    # 构造瞬间快照填了缓冲首点（无 SSE 兜底）
    assert len(dialog._samples) == 1

    # 协议开关初态来自 protocol_switches（c1 pd=开 pps=关 ufcs=开）
    assert _switch(dialog, "pd").isChecked() is True
    assert _switch(dialog, "pps").isChecked() is False
    assert _switch(dialog, "ufcs").isChecked() is True
    assert _switch(dialog, "pd").isEnabled(), "门面就绪时开关应可交互"

    # 整窗 grab 非空白
    img = dialog.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 弹窗渲染空白"

    dialog.hide()
    dialog.deleteLater()
    print(f"1. 构造渲染：端口名/四读数/协议开关初态 OK [{theme}]")


def test_push_samples_and_ring(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    dialog = _make_dialog(service, port_data=PORT_DATA_A)
    app.processEvents()

    # 喂 5 个点（其中 1 条为非本端口数据，应被忽略）
    dialog.push_port_sample(2, PORT_DATA_B)  # 非本端口，忽略
    for i in range(1, 6):
        dialog.push_port_sample(1, {"voltage": 20.0, "current": 2.5,
                                    "power": 50.0 + i, "protocol": "PD"})
    app.processEvents()

    # 曲线缓冲 5+1（构造首点）点；非本端口数据未混入
    assert len(dialog._samples) == 6, f"缓冲应为 6 点，实际 {len(dialog._samples)}"
    # 功率读数更新为最后一条
    assert dialog._w_value.text() == "55.0"
    assert dialog._v_value.text() == "20.0"
    # 曲线数据同步且 grab 非空白（曲线有内容，不再是空态）
    assert len(dialog._curve._points) == 6
    curve_img = dialog._curve.grab().toImage()
    assert not _image_is_blank(curve_img), f"[{theme}] 曲线渲染空白"

    # 曲线区间标注随点数更新
    assert "6/120" in dialog._range_label.text()

    # 8 点横向铺满断言：喂到 8 点后，曲线/填充（主题色像素）在绘图区
    # 左带与右带都出现（修复前 8 点被 120 点窗口映射压在最右 1/8）
    for i in range(2):
        dialog.push_port_sample(1, {"voltage": 20.0, "current": 2.0,
                                    "power": 30.0 + i, "protocol": "PD"})
    app.processEvents()
    assert len(dialog._curve._points) == 8
    curve_img = dialog._curve.grab().toImage()
    cw = curve_img.width()
    assert _has_theme_pixels_in_band(curve_img, 42, 42 + cw // 8), \
        "[{theme}] 8 点曲线应铺到绘图区左带（右缘对位 bug）"
    assert _has_theme_pixels_in_band(curve_img, cw - cw // 8, cw), \
        "[{theme}] 8 点曲线右带（最新点）也应有内容"

    # 120 点环形：喂满 130 点断言缓冲截到 120
    for i in range(130):
        dialog.push_port_sample(1, {"voltage": 20.0, "current": 2.0,
                                    "power": 40.0 + (i % 10),
                                    "protocol": "PD"})
    assert len(dialog._samples) == 120, \
        f"环形缓冲应恒为 120 点，实际 {len(dialog._samples)}"
    assert len(dialog._curve._points) == 120
    assert "120/120" in dialog._range_label.text()
    # 功率读数跟随最后一条
    assert dialog._w_value.text() == "49.0"

    # 畸形注入容错：不抛异常、缓冲不变
    dialog.push_port_sample("x", PORT_DATA_B)
    dialog.push_port_sample(1, None)
    dialog.push_port_sample(None, PORT_DATA_B)
    assert len(dialog._samples) == 120

    dialog.hide()
    dialog.deleteLater()
    print(f"2. push_port_sample 喂点/环形 120 截断/读数更新 OK [{theme}]")


def test_stale_marker(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    dialog = _make_dialog(service)
    app.processEvents()

    # 未超时：不置 stale
    dialog._check_stale()
    assert dialog._stale is False

    # 模拟无新数据超 10s：巡检置 stale，曲线标注「数据未更新」
    dialog._last_sample_mono = time.monotonic() - 11.0
    dialog._check_stale()
    assert dialog._stale is True
    assert dialog._curve._stale is True
    assert not _image_is_blank(dialog._curve.grab().toImage())

    # 新数据到达：stale 自动解除
    dialog.push_port_sample(1, PORT_DATA_B)
    assert dialog._stale is False
    assert dialog._curve._stale is False
    assert dialog._w_value.text() == "18.0"

    dialog.hide()
    dialog.deleteLater()
    print(f"3. 数据未更新灰字标注与新数据解除 OK [{theme}]")


def test_protocol_toggle(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    dialog = _make_dialog(service)
    app.processEvents()

    # 用户切换 c1 pps -> on：调用参数 (port=1, protocol="pps", on=True)，
    # busy 禁用；成功后解除 busy 且不做乐观校准（视觉保留）
    _switch(dialog, "pps").setChecked(True)
    assert not _switch(dialog, "pps").isEnabled(), "切换中应 busy 禁用"
    _drain_jobs()
    assert service.calls == [(1, "pps", True)], service.calls
    assert _switch(dialog, "pps").isEnabled(), "成功后应解除 busy"
    assert _switch(dialog, "pps").isChecked() is True, \
        "成功路径不做乐观回写，切换视觉保留到下一帧校准"

    # busy 期间 apply_protocol_event 不覆盖切换中的开关
    _switch(dialog, "pd").setChecked(False)
    assert not _switch(dialog, "pd").isEnabled()
    dialog.apply_protocol_event(PROTOCOL_SWITCHES_FRAME_EVENT)
    assert _switch(dialog, "pd").isChecked() is False, \
        "busy 期间注入不得覆盖切换中的开关"
    _drain_jobs()
    assert _switch(dialog, "pd").isEnabled()

    # 失败回滚：切 c1 ufcs -> off 失败，回滚为开 + 解除 busy
    service.mode = "fail"
    _switch(dialog, "ufcs").setChecked(False)
    _drain_jobs()
    assert service.calls[-1] == (1, "ufcs", False)
    assert _switch(dialog, "ufcs").isEnabled(), "失败后应解除 busy"
    assert _switch(dialog, "ufcs").isChecked() is True, "失败应回滚开关视觉"

    dialog.hide()
    dialog.deleteLater()
    print(f"4. 协议切换调用参数/busy/失败回滚 OK [{theme}]")


def test_facade_missing(theme: str) -> None:
    apply_theme(theme)
    dialog = _make_dialog(MissingFacade())
    app.processEvents()

    # 门面方法未落地：开关禁用 + tooltip 提示，切换被禁用拦截
    for proto in ("pd", "pps", "ufcs"):
        assert not _switch(dialog, proto).isEnabled(), \
            f"门面缺失时 {proto} 开关应禁用"
        assert _switch(dialog, proto).toolTip() == "门面方法未就绪"

    # 防御路径（双重保险）：程序化触发 toggled 同步回滚
    switch = _switch(dialog, "pd")
    switch.blockSignals(False)
    switch.setChecked(not switch.isChecked())
    _drain_jobs()
    assert switch.isChecked() is True, "门面缺失应回滚开关视觉"
    dialog.hide()
    dialog.deleteLater()
    print(f"5. 门面方法缺失：禁用 + tooltip + 回滚 OK [{theme}]")


def test_apply_protocol_event(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    dialog = _make_dialog(service)
    app.processEvents()

    # SSE protocol 事件：c1 -> pd=关 pps=开 ufcs=关
    dialog.apply_protocol_event(EVENT_B)
    assert _switch(dialog, "pd").isChecked() is False
    assert _switch(dialog, "pps").isChecked() is True
    assert _switch(dialog, "ufcs").isChecked() is False

    # 非 protocol 事件忽略；畸形结构不抛异常
    dialog.apply_protocol_event({"type": "settings", "settings": {"16": 15}})
    assert _switch(dialog, "pps").isChecked() is True, "非 protocol 事件不得改动开关"
    dialog.apply_protocol_event({})
    dialog.apply_protocol_event({"type": "protocol"})  # 无 switches
    dialog.apply_protocol_event({"type": "protocol", "switches": {}})  # 无本口
    dialog.apply_protocol_event(None)
    assert _switch(dialog, "pps").isChecked() is True

    # 外层全量 status dict 解包容错（refresh 语义同款）
    dialog.apply_protocol_event(
        {"type": "protocol", "switches": PROTOCOL_SWITCHES})
    assert _switch(dialog, "pd").isChecked() is True
    assert _switch(dialog, "pps").isChecked() is False

    dialog.hide()
    dialog.deleteLater()
    print(f"6. apply_protocol_event 更新/按端口过滤/容错 OK [{theme}]")


def test_retheme(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    dialog = _make_dialog(service)
    dialog.push_port_sample(1, PORT_DATA_B)
    app.processEvents()

    assert not _image_is_blank(dialog.grab().toImage()), f"[{theme}] 渲染空白"
    # retheme 不抛异常，且 grab 依旧非空白
    dialog.retheme()
    app.processEvents()
    assert not _image_is_blank(dialog.grab().toImage()), \
        f"[{theme}] retheme 后渲染空白"

    dialog.hide()
    dialog.deleteLater()
    print(f"7. 双主题 grab + retheme 不崩 OK [{theme}]")


PROTOCOL_SWITCHES_FRAME_EVENT = {
    "type": "protocol", "switches": PROTOCOL_SWITCHES}


def test_nice_axis() -> None:
    """Y 轴 nice 刻度纯函数：轴顶 ≥ 峰值、含 0、4~6 档、无除零。"""
    from app.ui.cuktech_port_detail import _nice_axis
    for peak, expect_top in ((6.6, 8.0), (65.0, 80.0), (100.0, 100.0),
                             (0.8, 1.0), (1.0, 1.0), (140.0, 150.0)):
        axis_max, ticks = _nice_axis(peak)
        assert axis_max >= peak, f"peak={peak}: 轴顶 {axis_max} 裁顶"
        assert ticks[0] == 0 and ticks[-1] == axis_max
        assert 4 <= len(ticks) <= 7, f"peak={peak}: 档数 {len(ticks)}"
        assert axis_max == expect_top, f"peak={peak}: 轴顶 {axis_max}"
    # 极小/零峰值不炸不除零
    _nice_axis(0.0)
    print("8. Y 轴 nice 刻度纯函数断言 OK")


for _theme in ("dark", "light"):
    test_construct_render(_theme)
    test_push_samples_and_ring(_theme)
    test_stale_marker(_theme)
    test_protocol_toggle(_theme)
    test_facade_missing(_theme)
    test_apply_protocol_event(_theme)
    test_retheme(_theme)

test_nice_axis()

jobs.shutdown()
si_theme.set_theme("dark")
print("CUKTECH PORT DETAIL TEST ALL PASS")
