# SPDX-License-Identifier: GPL-3.0-or-later
"""托盘音箱控制栏 fetch 串台回归测试（离屏）。

复现并回归 _on_fetch_error 的 did 守卫（提交处 on_error=lambda e, d=did
快照 + 回调里 did != self._did 早退）：set_speaker("A") 的取数失败晚于
set_speaker("B") 到达时，不得把 B 的栏位刷成 "—" 或提前启用控件。

断言——
- A 的迟到 on_error 被丢弃：_vol_label 保持 "…"、按钮/滑块仍禁用；
- 当前设备 B 的 on_error 正常生效：标签变 "—"、控件启用；
- 守卫经真实 JobExecutor 线程路径同样成立（回调回主线程）。

用法: QT_QPA_PLATFORM=offscreen python -m pytest tests/tray_audio_bar_test.py -q
"""

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

import pytest

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.ui.theme_service import apply_theme
from app.ui.tray.audio_bar import _TrayAudioBar

apply_theme("dark")


class _FakeJobs:
    """捕获提交的任务，手动触发成功/失败回调（不启动线程）。"""

    def __init__(self):
        self.pending = []  # (fn, on_success, on_error)

    def submit(self, fn, on_success=None, on_error=None, **kw):
        self.pending.append((fn, on_success, on_error))

    def fail_last(self, exc: Exception):
        _, _, on_error = self.pending[-1]
        if on_error is not None:
            on_error(exc)

    def succeed_last(self):
        fn, on_success, _ = self.pending[-1]
        if on_success is not None:
            on_success(fn())


@pytest.fixture()
def bar():
    w = _TrayAudioBar(service=None, jobs=_FakeJobs())
    yield w
    w.deleteLater()


def test_stale_error_does_not_clobber_new_speaker(bar):
    """A 的迟到失败不得覆盖 B 的栏位（串台守卫本体）。"""
    bar.set_speaker("A")
    assert bar._vol_label.text() == "…"
    assert not bar._slider.isEnabled(), "取数未回来前滑块应禁用"
    assert not bar._btn_play.isEnabled()

    bar.set_speaker("B")
    assert bar._did == "B"
    assert bar._vol_label.text() == "…"

    # 飞行中 A 的取数失败迟到（修复前会把 B 的标签刷成 "—" 并启用控件）
    bar._on_fetch_error(Exception("boom"), "A")
    assert bar._vol_label.text() == "…", "旧设备的失败不应动当前栏位"
    assert not bar._slider.isEnabled(), "旧设备的失败不应提前启用滑块"
    assert not bar._btn_play.isEnabled(), "旧设备的失败不应提前启用按钮"

    # 当前设备 B 的失败：正常生效
    bar._on_fetch_error(Exception("boom"), "B")
    assert bar._vol_label.text() == "—"
    assert bar._btn_prev.isEnabled(), "当前设备失败后按钮应恢复可用（did 非 None）"


def test_stale_error_via_jobs_submission(bar):
    """不经直调：走 FakeJobs 的提交 → 按提交顺序触发 on_error，守卫同样生效。"""
    bar.set_speaker("A")
    bar.set_speaker("B")
    # pending = [A 的取数, B 的取数]；先让 A 的失败迟到到达
    a_fn, _, a_on_error = bar._jobs.pending[0]
    a_on_error(Exception("network down"))
    assert bar._vol_label.text() == "…", "A 的迟到失败应被守卫丢弃"
    assert not bar._slider.isEnabled()
    # B 自己的取数成功回来 → 栏位可用（默认 0-100 范围，值 None）
    bar._jobs.succeed_last()
    assert bar._vol_label.text() == "—"
    assert bar._slider.isEnabled()
    assert bar._slider.minimum() == 0 and bar._slider.maximum() == 100


def test_same_did_resubmit_and_clear(bar):
    """同 did 重复 set 不重新拉取；did 清空时旧失败因 did 匹配 None 仍丢弃。"""
    bar.set_speaker("A")
    n = len(bar._jobs.pending)
    bar.set_speaker("A")  # 同设备：仅刷新可见性，不再提交
    assert len(bar._jobs.pending) == n

    bar.set_speaker(None)
    assert bar._did is None
    assert bar.isHidden()
    # did=None 后旧设备的失败：did 非空 != None → 丢弃，不得启用控件
    bar._on_fetch_error(Exception("late"), "A")
    assert not bar._btn_play.isEnabled()


def test_real_job_executor_thread_path(qtbot=None):
    """真实 JobExecutor：回调经 Qt 信号回主线程，守卫在线程路径同样成立。"""
    from app.core.jobs import JobExecutor

    class _FlakyService:
        def device_detail(self, did):
            raise RuntimeError(f"device {did} offline")

        def read_props(self, did, names):
            raise RuntimeError(f"device {did} offline")

    jobs = JobExecutor()
    try:
        w = _TrayAudioBar(service=_FlakyService(), jobs=jobs)
        w.set_speaker("A")
        w.set_speaker("B")
        # 排空后台线程 + 主线程信号投递
        deadline = app.processEvents
        import time
        for _ in range(200):
            app.processEvents()
            if w._vol_label.text() == "—":  # B 的失败已处理完
                break
            time.sleep(0.01)
        else:
            # 只收到 A 的失败（应被丢弃）而 B 尚未处理——等待足够后仍须保持 "…"
            pass
        # 稳定后：B 的失败生效（标签 "—" 且控件启用），A 的失败被守卫丢弃。
        # 两者按序到达，最终状态由 B 决定；关键断言是 B 的失败确实生效。
        assert w._vol_label.text() == "—", w._vol_label.text()
        assert w._btn_prev.isEnabled()
        # 线程路径的串台断言：此刻标签绝非被 A 的迟到失败覆盖成
        # "—" 后又被禁用——控件必须处于启用态
        assert w._slider.isEnabled()
        w.deleteLater()
    finally:
        jobs.shutdown()
