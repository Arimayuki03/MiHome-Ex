# SPDX-License-Identifier: GPL-3.0-or-later
"""CUKTECH 协议开关面板自测：离屏运行，假门面 + 假数据断言渲染与写路径。

用法: .venv\\Scripts\\python.exe tests/cuktech_protocols_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_protocols_test）

仿 tests/cuktech_panel_test.py 的离屏 + grab() 模式：构造
ProtocolSwitchPanel，灌假 protocol_switches（位图解析自 PIID 21 编码，
见 cuktech-ble-server/state.py PROTOCOL_SWITCH_BITS），断言：
- 4 行端口共 10 个开关初态渲染；
- refresh 位图解析（含外层 status dict 解包容错）；
- 切换调用参数正确（port=1, protocol="pd"），成功后不乐观更新；
- 失败回滚 + 门面方法缺失回滚；
- apply_protocol_event 位图更新；
- 暗/亮双主题 grab 非空白 + retheme 不抛异常。
service 用假门面（只实现面板用到的签名），不发起任何网络请求。
"""

import atexit
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体，Qt 字体回退引擎会解析到无 CJK 字形的图标
# 字体导致中文零像素（见 cuktech_panel_test.py 同款注释）。指向系统
# 字体目录让离屏渲染与实际 GUI 一致。
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_protocols_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication([])

# 测试禁写运行数据：仿 theme_test，防假设备经任何回写路径泄漏
from app.core import cache as _device_cache
_device_cache.save = lambda *a, **k: None

from app.ui import si_theme
from app.ui.theme_service import apply_theme

# ---------------------------------------------------------------------------
# 假数据：PIID 21 位图 -> protocol_switches 解析视图
# 编码: aFlags<<24 | c3Flags<<16 | c2Flags<<8 | c1Flags
# c1/c2Flags: bit0=PD bit1=PPS bit2=UFCS bit3=保留(固定1)；c3/aFlags: bit0=UFCS bit1=SCP
# ---------------------------------------------------------------------------


def decode_switches(value: int) -> dict:
    """与服务端 ChargerState.protocol_switches 相同的位图解析。"""
    return {
        "c1": {
            "pd": bool(value & (1 << 0)),
            "pps": bool(value & (1 << 1)),
            "ufcs": bool(value & (1 << 2)),
        },
        "c2": {
            "pd": bool(value & (1 << 8)),
            "pps": bool(value & (1 << 9)),
            "ufcs": bool(value & (1 << 10)),
        },
        "c3": {
            "ufcs": bool(value & (1 << 16)),
            "scp": bool(value & (1 << 17)),
        },
        "a": {
            "ufcs": bool(value & (1 << 24)),
            "scp": bool(value & (1 << 25)),
        },
    }


# c1 pd=False pps=True ufcs=True；其余全关（保留位照常置位不进解析视图）
BITMAP_A = 0x06
# c3 ufcs=True scp=True（bit16|bit17）；c1 保留位固定 1
BITMAP_B = (1 << 16) | (1 << 17) | 0x08

FULL_STATUS = {
    "connected": True,
    "protocol_extend": BITMAP_A,
    "protocol_switches": decode_switches(BITMAP_A),
}

EVENT_A = {"type": "protocol",
           "switches": decode_switches(BITMAP_A),
           "protocol_extend": BITMAP_A}
EVENT_B = {"type": "protocol",
           "switches": decode_switches(BITMAP_B),
           "protocol_extend": BITMAP_B}


class FakeService:
    """假门面：只实现面板用到的签名，零网络。

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
    """空白判定：全图非背景像素 <= 16 视为空白（与 panel 测试同标准）。"""
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
    import time
    for _ in range(rounds):
        time.sleep(0.02)
        app.processEvents()
    app.processEvents()


def _switch(panel, key: str, proto: str):
    return panel._port_rows[key]["switches"][proto]


from app.core.jobs import JobExecutor
from app.ui.cuktech_protocols import ProtocolSwitchPanel

jobs = JobExecutor()
# 收尾兜底：断言失败抛异常时跳过文件尾部的 jobs.shutdown()，JobExecutor
# 的工作 QThread 还在运行，解释器关闭阶段销毁它即报 "QThread: Destroyed
# while thread is still running"。atexit 先于 Qt 对象析构执行 join。
atexit.register(jobs.shutdown)


def test_render_and_refresh(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    panel = ProtocolSwitchPanel(service, jobs)
    panel.resize(760, 260)
    panel.show()
    panel.refresh(FULL_STATUS["protocol_switches"])
    app.processEvents()
    assert panel._hint_label.text() == "关闭某协议后，该端口不再以此协议协商；改动即时生效"

    # 行数与开关数：4 行，C1/C2 各 3 个、C3/A 各 2 个 = 10 个开关
    assert set(panel._port_rows) == {"c1", "c2", "c3", "a"}, panel._port_rows.keys()
    total = sum(len(row["switches"]) for row in panel._port_rows.values())
    assert total == 10, f"开关总数应为 10，实际 {total}"
    assert set(panel._port_rows["c1"]["switches"]) == {"pd", "pps", "ufcs"}
    assert set(panel._port_rows["c3"]["switches"]) == {"ufcs", "scp"}
    assert set(panel._port_rows["a"]["switches"]) == {"ufcs", "scp"}

    # BITMAP_A: c1 pd=False pps=True ufcs=True，其余全关
    assert _switch(panel, "c1", "pd").isChecked() is False
    assert _switch(panel, "c1", "pps").isChecked() is True
    assert _switch(panel, "c1", "ufcs").isChecked() is True
    for key in ("c2", "c3", "a"):
        for sw in panel._port_rows[key]["switches"].values():
            assert sw.isChecked() is False, f"{key} 应全关"

    # 外层全量 status dict 传入自动解包（容错路径）
    panel.refresh(FULL_STATUS)
    assert _switch(panel, "c1", "pps").isChecked() is True

    # 位图 B 刷新：c3 ufcs/scp 双开
    panel.refresh(decode_switches(BITMAP_B))
    assert _switch(panel, "c3", "ufcs").isChecked() is True
    assert _switch(panel, "c3", "scp").isChecked() is True
    assert _switch(panel, "c1", "pps").isChecked() is False

    img = panel.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 面板渲染空白"
    panel.hide()
    panel.deleteLater()
    print(f"1. 初态渲染 10 开关 + refresh 位图解析 OK [{theme}]")


def test_toggle_write_path(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    panel = ProtocolSwitchPanel(service, jobs)
    panel.resize(760, 260)
    panel.show()
    panel.refresh(FULL_STATUS["protocol_switches"])
    app.processEvents()

    # 用户切换 c1 pd -> on：调用参数 (port=1, protocol="pd", on=True)，
    # 成功后不乐观更新（等 refresh 校准）
    _switch(panel, "c1", "pd").setChecked(True)
    _drain_jobs()
    assert service.calls == [(1, "pd", True)], service.calls
    assert _switch(panel, "c1", "pd").isEnabled(), "成功后开关应解除 busy"
    assert _switch(panel, "c1", "pd").isChecked() is True, \
        "成功路径不做乐观回写，切换视觉保留到 refresh 校准"

    # refresh 校准（例如设备端实际写失败后的真实态回滚）
    panel.refresh(FULL_STATUS["protocol_switches"])
    assert _switch(panel, "c1", "pd").isChecked() is False

    # 再切一个 c3 scp -> on
    _switch(panel, "c3", "scp").setChecked(True)
    _drain_jobs()
    assert service.calls[-1] == (3, "scp", True), service.calls

    # 切换中（busy 期间）refresh 不覆盖该开关视觉
    _switch(panel, "c2", "pps").setChecked(True)
    assert (not _switch(panel, "c2", "pps").isEnabled()), "切换中应 busy 禁用"
    panel.refresh(FULL_STATUS["protocol_switches"])
    assert _switch(panel, "c2", "pps").isChecked() is True, \
        "busy 期间 refresh 不得覆盖切换中的开关"
    _drain_jobs()
    assert _switch(panel, "c2", "pps").isEnabled()

    # retheme 不抛异常且 grab 依旧非空白
    panel.retheme()
    app.processEvents()
    assert not _image_is_blank(panel.grab().toImage()), f"[{theme}] retheme 后渲染空白"
    panel.hide()
    panel.deleteLater()
    print(f"2. 切换调用参数/成功路径/busy 期间 refresh 跳过 OK [{theme}]")


def test_failure_rollback(theme: str) -> None:
    apply_theme(theme)
    service = FakeService(mode="fail")
    panel = ProtocolSwitchPanel(service, jobs)
    panel.resize(760, 260)
    panel.show()
    panel.refresh(FULL_STATUS["protocol_switches"])
    app.processEvents()

    # 切换失败：开关回滚 + 解除 busy
    _switch(panel, "c1", "pd").setChecked(True)
    _drain_jobs()
    assert service.calls == [(1, "pd", True)]
    assert _switch(panel, "c1", "pd").isEnabled(), "失败后应解除 busy"
    assert _switch(panel, "c1", "pd").isChecked() is False, "失败应回滚开关视觉"

    # 反向切换失败同样回滚
    panel.refresh(decode_switches(BITMAP_A))
    _switch(panel, "c1", "pps").setChecked(False)
    _drain_jobs()
    assert service.calls[-1] == (1, "pps", False)
    assert _switch(panel, "c1", "pps").isChecked() is True, "失败应回滚为开"
    panel.hide()
    panel.deleteLater()
    print(f"3. 失败回滚 + busy 解除 OK [{theme}]")


def test_facade_missing(theme: str) -> None:
    apply_theme(theme)
    panel = ProtocolSwitchPanel(MissingFacade(), jobs)
    panel.resize(760, 260)
    panel.show()
    panel.refresh(FULL_STATUS["protocol_switches"])
    app.processEvents()

    # 门面方法缺失：同步回滚，无请求进队列
    # （refresh 后 c1.ufcs 初态为开，切 OFF 验证回滚回开；
    #   c1.pd 初态为关，切 ON 验证回滚回关）
    _switch(panel, "c1", "ufcs").setChecked(False)
    _drain_jobs()
    assert _switch(panel, "c1", "ufcs").isEnabled()
    assert _switch(panel, "c1", "ufcs").isChecked() is True, \
        "门面方法缺失应回滚开关视觉"
    _switch(panel, "c1", "pd").setChecked(True)
    _drain_jobs()
    assert _switch(panel, "c1", "pd").isChecked() is False, \
        "门面方法缺失应回滚开关视觉"
    panel.hide()
    panel.deleteLater()
    print(f"4. 门面方法缺失防御回滚 OK [{theme}]")


def test_sse_event(theme: str) -> None:
    apply_theme(theme)
    service = FakeService()
    panel = ProtocolSwitchPanel(service, jobs)
    panel.resize(760, 260)
    panel.show()
    app.processEvents()

    # SSE protocol 事件：位图 A -> c1 pps/ufcs 开
    panel.apply_protocol_event(EVENT_A)
    assert _switch(panel, "c1", "pps").isChecked() is True
    assert _switch(panel, "c1", "ufcs").isChecked() is True
    assert _switch(panel, "c1", "pd").isChecked() is False

    # 位图 B -> c3 ufcs/scp 开、c1 全关
    panel.apply_protocol_event(EVENT_B)
    assert _switch(panel, "c3", "ufcs").isChecked() is True
    assert _switch(panel, "c3", "scp").isChecked() is True
    assert _switch(panel, "c1", "pps").isChecked() is False
    assert _switch(panel, "c1", "ufcs").isChecked() is False

    # 非 protocol 事件忽略；畸形结构不抛异常
    panel.apply_protocol_event({"type": "settings", "settings": {"16": 15}})
    assert _switch(panel, "c3", "scp").isChecked() is True, "非 protocol 事件不得改动开关"
    panel.apply_protocol_event({})
    panel.apply_protocol_event({"type": "protocol"})  # 无 switches
    panel.apply_protocol_event(None)
    assert _switch(panel, "c3", "scp").isChecked() is True
    panel.hide()
    panel.deleteLater()
    print(f"5. apply_protocol_event 位图更新/容错 OK [{theme}]")


# ---------- 暗/亮双主题重复全部用例 ----------

for _theme in ("dark", "light"):
    test_render_and_refresh(_theme)
    test_toggle_write_path(_theme)
    test_failure_rollback(_theme)
    test_facade_missing(_theme)
    test_sse_event(_theme)

jobs.shutdown()
si_theme.set_theme("dark")
print("CUKTECH PROTOCOLS TEST ALL PASS")
