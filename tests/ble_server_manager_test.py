# SPDX-License-Identifier: GPL-3.0-or-later
"""BleServerManager 单元测试：生命周期状态机与幂等/外部实例语义。

绝不真连 BLE、不真拉起服务端：探活函数 _probe 全部注入桩，
QProcess 行为经由状态断言间接覆盖（真实拉起由全链路构建验证承担）。
用法: .venv\\Scripts\\python.exe -m pytest tests/ble_server_manager_test.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.core import settings_store  # noqa: E402
from app.core.ble_server_manager import (  # noqa: E402
    BleServerManager,
    _data_dir,
    locate_server_command,
)

app = QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path, monkeypatch):
    """所有设置写入临时目录，测试不再触碰开发工作树的 settings.json。"""
    monkeypatch.setattr(
        settings_store._json_store, "data_file", lambda name: tmp_path / name)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))


# ---------- locate_server_command ----------


def test_locate_in_dev_returns_venv_command(tmp_path, monkeypatch) -> None:
    """开发态：临时构造同级服务端 venv，返回 (python, [ha_server.py], 目录)。

    不依赖本机 D:\\Code\\MiHome-Ex\\cuktech-ble-server 是否 checkout：
    用 monkeypatch 伪造模块 __file__，把仓库根定位到 tmp_path 下的假仓库。
    """
    import app.core.ble_server_manager as mod

    # parents[3] 对应仓库根的父目录；module 放在 tmp/repo/app/core/ 下
    module = tmp_path / "repo" / "app" / "core" / "ble_server_manager.py"
    module.parent.mkdir(parents=True)
    module.write_text("# fake module path\n", encoding="utf-8")
    backend = tmp_path / "cuktech-ble-server"
    (backend / ".venv" / "Scripts").mkdir(parents=True)
    python = backend / ".venv" / "Scripts" / "python.exe"
    python.write_text("fake python\n", encoding="utf-8")
    entry = backend / "ha_server.py"
    entry.write_text("# fake entry\n", encoding="utf-8")

    monkeypatch.setattr(mod, "__file__", str(module))
    monkeypatch.setattr(mod, "is_packaged", lambda: False)
    cmd = locate_server_command()
    assert cmd is not None, "临时 venv + ha_server.py 应被识别为可用"
    program, args, work_dir = cmd
    assert program == str(python)
    assert args == [str(entry)]
    assert work_dir == backend
    assert (work_dir / "ha_server.py").is_file()


def test_locate_dev_missing_venv_returns_none(tmp_path, monkeypatch) -> None:
    """开发态没有服务端 venv 时返回 None，不依赖本机同级仓库。"""
    import app.core.ble_server_manager as mod

    module = tmp_path / "repo" / "app" / "core" / "ble_server_manager.py"
    module.parent.mkdir(parents=True)
    module.write_text("# fake module path\n", encoding="utf-8")
    monkeypatch.setattr(mod, "__file__", str(module))
    monkeypatch.setattr(mod, "is_packaged", lambda: False)
    assert locate_server_command() is None


def test_locate_packaged_present(tmp_path, monkeypatch) -> None:
    """打包态：dist/ble-server/CuktechBleServer.exe 存在时返回 exe 命令。"""
    import app.core.ble_server_manager as mod

    exe = tmp_path / "dist" / "ble-server" / "CuktechBleServer.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake exe")
    monkeypatch.setattr(mod, "is_packaged", lambda: True)
    monkeypatch.setattr(mod, "resource_path", lambda rel: tmp_path / "dist" / rel)
    cmd = locate_server_command()
    assert cmd is not None
    program, args, work_dir = cmd
    assert program == str(exe)
    assert args == []
    assert work_dir == exe.parent


def test_locate_packaged_missing_component(tmp_path, monkeypatch) -> None:
    """打包态组件缺失时返回 None（管理器据此静默降级）。"""
    import app.core.ble_server_manager as mod

    monkeypatch.setattr(mod, "is_packaged", lambda: True)
    monkeypatch.setattr(
        mod, "resource_path", lambda rel: tmp_path / "missing" / rel)
    assert locate_server_command() is None


# ---------- 数据目录 ----------


def test_data_dir_under_localappdata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    assert _data_dir() == tmp_path / "LocalAppData" / "MiHome-Ex" / "ble-server"


# ---------- 设置存取 ----------


def test_setting_roundtrip_and_default() -> None:
    """默认 True；写 False 读回 False；还原。"""
    original = settings_store.get_ble_server_enabled()
    try:
        settings_store.set_ble_server_enabled(False)
        assert settings_store.get_ble_server_enabled() is False
    finally:
        settings_store.set_ble_server_enabled(original)
    assert settings_store.get_ble_server_enabled() == original


# ---------- 状态机与幂等 ----------


def _make_manager(probe_returns: bool) -> BleServerManager:
    m = BleServerManager()
    m._probe = lambda url: probe_returns
    return m


def test_start_idempotent_when_probe_succeeds(monkeypatch) -> None:
    """8199 已有应答 → 标记外部实例、置 running、不创建 QProcess。

    定位函数注入假的可用命令，避免本机是否 checkout 同级服务端影响
    状态机断言；探活仍为内存桩，不真连 8199/BLE。
    """
    import app.core.ble_server_manager as mod

    monkeypatch.setattr(
        mod, "locate_server_command",
        lambda: (r"C:\fake\python.exe", [r"C:\fake\ha_server.py"], Path(r"C:\fake")))
    m = _make_manager(probe_returns=True)
    states: list[str] = []
    m.state_changed.connect(states.append)
    m.start()
    assert m.state == "running"
    assert m.is_external is True
    assert m._process is None
    # 幂等：重复 start 不变
    m.start()
    assert m.state == "running" and m.is_external
    # 外部实例 stop 只复位本地状态，不动任何进程
    m.stop()
    assert m.state == "stopped" and m._process is None


def test_start_missing_component_marks_failed() -> None:
    """组件缺失（locate 返回 None）→ failed，不抛错。"""
    import app.core.ble_server_manager as mod

    m = _make_manager(probe_returns=False)
    real_locate = mod.locate_server_command
    try:
        mod.locate_server_command = lambda: None
        m.start()
    finally:
        mod.locate_server_command = real_locate
    assert m.state == "failed"
    assert m._process is None
    # failed 后 stop 是安全空操作
    m.stop()
    assert m.state == "stopped"


def test_stop_without_start_is_safe() -> None:
    """未 start 直接 stop：复位状态、无异常。"""
    m = BleServerManager()
    m.stop()
    assert m.state == "stopped"


def test_state_signal_emitted_on_change() -> None:
    """状态真正变化才发信号。"""
    m = BleServerManager()
    states: list[str] = []
    m.state_changed.connect(states.append)
    m._set_state("starting")
    m._set_state("starting")  # 重复置同一状态不发
    assert states == ["starting"]


# ---------- 意外退出自动重启（服务端自重启约定） ----------


def _flush_timers() -> None:
    """跑完当前排队的 QTimer.singleShot 回调（无 pytest-qt，手动转事件循环）。

    """
    from PySide6.QtCore import QEventLoop

    loop = QCoreApplication.instance() or QCoreApplication([])
    for _ in range(50):  # 上限防死循环
        loop.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)


def test_exit_zero_schedules_supervised_restart(monkeypatch) -> None:
    """退出码 0（服务端配置写入后的约定自重启）→ 延迟后自动拉起。

    monkeypatch start() 捕获调用而非真拉进程；settings 开关开。
    """
    import app.core.ble_server_manager as mod

    m = _make_manager(probe_returns=False)
    m._external = False
    m._state = "running"
    calls: list[int] = []
    original_start = mod.BleServerManager.start
    monkeypatch.setattr(mod, "_RESTART_DELAY_MS", 0)
    try:
        mod.BleServerManager.start = lambda self: calls.append(1)
        m._on_finished(0, None)
        assert m.state == "stopped" and m._restart_attempts == 0
        _flush_timers()
        assert len(calls) == 1, "退出码 0 应延迟重启一次"
    finally:
        mod.BleServerManager.start = original_start


def test_exit_nonzero_backoff_and_gives_up(monkeypatch) -> None:
    """非零退出（崩溃）→ 退避重启，超过上限不再拉起。"""
    import app.core.ble_server_manager as mod

    m = _make_manager(probe_returns=False)
    m._external = False
    m._state = "running"
    calls: list[int] = []
    original_start = mod.BleServerManager.start
    monkeypatch.setattr(mod, "_RESTART_DELAY_MS", 0)
    try:
        mod.BleServerManager.start = lambda self: calls.append(1)
        # 连续崩溃：1、2 次安排重启，第 3 次到上限不再安排
        for expected_attempt in (1, 2, 3):
            m._on_finished(1, None)
            assert m._restart_attempts == expected_attempt
            _flush_timers()
            scheduled = len(calls)
            assert scheduled == min(expected_attempt, mod._RESTART_MAX_ATTEMPTS)
        assert len(calls) == mod._RESTART_MAX_ATTEMPTS
    finally:
        mod.BleServerManager.start = original_start


def test_restart_skipped_when_stop_requested_or_disabled(monkeypatch) -> None:
    """stop 请求在途 / 设置开关关闭时，退出不触发重启。"""
    import app.core.ble_server_manager as mod

    original_start = mod.BleServerManager.start
    calls: list[int] = []
    monkeypatch.setattr(mod, "_RESTART_DELAY_MS", 0)
    try:
        mod.BleServerManager.start = lambda self: calls.append(1)

        m = _make_manager(probe_returns=False)
        m._state = "running"
        m._stop_requested = True  # 用户主动停止
        m._on_finished(0, None)
        _flush_timers()
        assert calls == []

        original = settings_store.get_ble_server_enabled()
        try:
            settings_store.set_ble_server_enabled(False)
            m2 = _make_manager(probe_returns=False)
            m2._state = "running"
            m2._on_finished(0, None)
            _flush_timers()
            assert calls == []
        finally:
            settings_store.set_ble_server_enabled(original)
    finally:
        mod.BleServerManager.start = original_start


def test_restart_skipped_for_external_instance(monkeypatch) -> None:
    """外部实例（非本管理器拉起）退出时不代管重启。"""
    import app.core.ble_server_manager as mod

    m = _make_manager(probe_returns=False)
    m._external = True
    m._state = "running"
    calls: list[int] = []
    original_start = mod.BleServerManager.start
    monkeypatch.setattr(mod, "_RESTART_DELAY_MS", 0)
    try:
        mod.BleServerManager.start = lambda self: calls.append(1)
        m._on_finished(0, None)
        _flush_timers()
        assert calls == []
    finally:
        mod.BleServerManager.start = original_start
