# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""内置 BLE 服务端（cuktech-ble-server）的子进程生命周期管理。

服务端以扩展组件形式随主应用分发（打包态 dist/ble-server/ 下的
CuktechBleServer.exe；开发态回退服务端仓库 venv + ha_server.py），
主应用启动时拉起、真正退出时停止，充电器卡片数据由此获得本地数据源。

设计要点：
- 幂等：8199 已有服务应答（外部自跑或上次托盘残留）则不重复拉起，
  且该外部进程退出时不被清理。
- 数据与程序分离：config.yaml / port_history.db 经环境变量指到
  %LOCALAPPDATA%/MiHome-Ex/ble-server/，Program Files 安装目录
- 停止策略：先温和后强杀。服务端 sqlite 开 WAL（history.py），强杀
  不损坏数据库，最多丢约 1 秒批量缓冲，可安全兜底。
"""

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, QProcessEnvironment, Signal

from app import is_packaged, resource_path
# 服务端默认端口与就绪探测参数（服务端 config.py 默认 server.port=8199）
_SERVER_PORT = 8199
_READY_POLL_MS = 500
_READY_POLLS_MAX = 20  # 500ms × 20 = 10s：cold start 含 winrt 初始化的余量
_STOP_WAIT_MS = 3000
# 配置写入后服务端自重启（win32 干净退出交由本管理器拉起）的延迟；
# 以及非零退出的自动重启退避上限（防崩溃循环占满 CPU）
_RESTART_DELAY_MS = 2000
_RESTART_MAX_ATTEMPTS = 3


def _default_url() -> str:
    return f"http://127.0.0.1:{_SERVER_PORT}"


def _data_dir() -> Path:
    """服务端数据目录：%LOCALAPPDATA%\\MiHome-Ex\\ble-server\\。

    旧项目名 MiHome-Windows\\ble-server\\ 的已有数据（config.yaml、
    port_history.db）首次调用时整体搬迁，避免充电历史归零。
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    data_dir = Path(base) / "MiHome-Ex" / "ble-server"
    legacy_dir = Path(base) / "MiHome-Windows" / "ble-server"
    if legacy_dir.is_dir() and not data_dir.exists():
        try:
            data_dir.parent.mkdir(parents=True, exist_ok=True)
            # rename 同盘原子；失败（旧目录被占用等）回退整体拷贝。
            # 都失败则按全新目录走：服务端有全默认值兜底，不影响启动
            try:
                legacy_dir.rename(data_dir)
            except OSError:
                shutil.copytree(legacy_dir, data_dir, dirs_exist_ok=True)
        except OSError:
            pass
    return data_dir


def locate_server_command() -> tuple[str, list[str], Path] | None:
    """定位服务端启动命令，返回 (程序, 参数, 工作目录)；不可用返回 None。

    打包态：dist/ble-server/CuktechBleServer.exe（Nuitka，数据文件同级）。
    开发态：服务端仓库 venv 的 python + ha_server.py——仅为本地联调
    提供便利，venv 不存在（如他人 clone 主仓库）时返回 None，管理器
    整体静默不工作。
    """
    if is_packaged():
        exe = Path(resource_path("ble-server/CuktechBleServer.exe"))
        return (str(exe), [], exe.parent) if exe.is_file() else None
    repo_root = Path(__file__).resolve().parents[3]
    py = repo_root / "cuktech-ble-server" / ".venv" / "Scripts" / "python.exe"
    entry = repo_root / "cuktech-ble-server" / "ha_server.py"
    if py.is_file() and entry.is_file():
        return (str(py), [str(entry)], entry.parent)
    return None


def _prepare_environment(work_dir: Path) -> dict[str, str]:
    """确保数据目录与初始配置就绪，返回注入子进程的环境变量。"""
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.yaml"
    if not config_path.exists():
        template = work_dir / "config.default.yaml"
        if template.is_file():
            try:
                shutil.copy2(template, config_path)
            except OSError:
                pass  # 拷贝失败不阻断：服务端有全默认值兜底
    return {
        "CUKTECH_CONFIG_PATH": str(config_path),
        "CUKTECH_HISTORY_DB_PATH": str(data_dir / "port_history.db"),
    }


class BleServerManager(QObject):
    """拉起/停止内置 BLE 服务端子进程，并向外广播状态。"""

    state_changed = Signal(str)  # stopped / starting / running / failed

    def __init__(self, parent: QObject | None = None,
                 base_url: str | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url or _default_url()
        self._process: QProcess | None = None
        self._state = "stopped"
        self._external = False  # 8199 被外部服务占用（非本管理器拉起）
        self._ready_timer: QTimer | None = None
        self._ready_polls = 0
        # 测试可注入的探测函数：url -> 服务是否应答（默认 HTTP 探活）
        self._probe = self._probe_http
        self._stop_requested = False
        # 非零退出连续计数（成功重启后归零），驱动退避重启
        self._restart_attempts = 0

    # ---------- 状态 ----------

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_external(self) -> bool:
        """当前服务是否由外部启动（非本管理器拉起，退出时不清理）。"""
        return self._external

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    # ---------- 启动 ----------

    def start(self) -> None:
        """按设置拉起服务端（幂等）。异步：就绪与否经信号通知。"""
        if self._state in ("starting", "running"):
            return
        self._stop_requested = False
        cmd = locate_server_command()
        if cmd is None:
            # 组件缺失（未打包服务端/无服务端 venv）：静默降级为无本地源，
            # 主界面充电器卡片按既有语义缺席，不报错打扰
            self._set_state("failed")
            return
        program, args, work_dir = cmd
        # 已有服务应答：视为外部实例，不重复拉起（幂等）
        if self._probe(self._base_url):
            self._external = True
            self._set_state("running")
            return
        env_vars = _prepare_environment(work_dir)
        self._external = False
        self._set_state("starting")

        process = QProcess(self)
        process.setProgram(program)
        process.setArguments(args)
        process.setWorkingDirectory(str(work_dir))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        env = QProcessEnvironment.systemEnvironment()
        for key, value in env_vars.items():
            env.insert(key, value)
        process.setProcessEnvironment(env)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error_occurred)
        self._process = process
        process.start()
        self._begin_ready_polling()

    def _begin_ready_polling(self) -> None:
        self._ready_polls = 0
        self._ready_timer = QTimer(self)
        self._ready_timer.setInterval(_READY_POLL_MS)
        self._ready_timer.timeout.connect(self._poll_ready)
        self._ready_timer.start()

    def _poll_ready(self) -> None:
        self._ready_polls += 1
        if self._probe(self._base_url):
            self._stop_ready_polling()
            self._set_state("running")
            return
        # 进程已退出仍探不到 → 启动失败（端口被占/立即崩溃等）
        if (self._process is None or self._process.state() == QProcess.ProcessState.NotRunning
                or self._ready_polls >= _READY_POLLS_MAX):
            self._stop_ready_polling()
            self._set_state("failed")

    def _stop_ready_polling(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.stop()
            self._ready_timer.deleteLater()
            self._ready_timer = None

    def _probe_http(self, base_url: str) -> bool:
        """HTTP 探活：/api/status 可达即视为服务就绪。

        同步阻塞调用，只用于 QProcess 异步流程中的短超时探测（服务端
        在本机回环，正常 <50ms）。requests 不可用时退化为 TCP 连接探测。
        """
        try:
            from app.core.cuktech_client import CuktechClient
            client = CuktechClient(base_url=base_url, timeout=1.0)
            client.status()
            return True
        except Exception:
            return False

    # ---------- 停止 ----------

    def stop(self, wait_ms: int = _STOP_WAIT_MS) -> None:
        """停止服务端（若为本管理器拉起）。外部实例不清理。

        主窗口 closeEvent 在 UI 线程同步调用：温和终止后最多等 wait_ms，
        超时强杀再短暂等待。服务端 WAL 保证强杀不损坏数据库。
        """
        self._stop_requested = True
        self._stop_ready_polling()
        if self._external or self._process is None:
            self._process = None
            self._set_state("stopped")
            return
        process, self._process = self._process, None
        if process.state() == QProcess.ProcessState.NotRunning:
            self._set_state("stopped")
            return
        process.terminate()
        if not process.waitForFinished(wait_ms):
            process.kill()
            process.waitForFinished(1000)
        self._set_state("stopped")

    # ---------- 子进程事件 ----------

    def _on_finished(self, exit_code: int, status) -> None:  # noqa: ANN001
        # 非主动停止的提前退出（崩溃/端口冲突）如实反映为 stopped；
        # starting 状态下的退出由 _poll_ready 兜底报 failed
        if self._stop_requested:
            return
        self._process = None
        if self._state == "running":
            self._set_state("stopped")
        # 服务端配置写入后的自重启约定：win32 下服务端 _restart() 干净
        # 退出（os._exit(0)，非 execv——WinRT 状态无法跨 exec 重建），
        # 由本管理器负责重新拉起。带退避防崩溃循环；外部实例（非本进程
        # 拉起）不代管，其生命周期归外部启动方。
        if self._external:
            return
        if exit_code == 0:
            self._restart_attempts = 0
            QTimer.singleShot(_RESTART_DELAY_MS, self._restart_if_needed)
        else:
            self._restart_attempts += 1
            if self._restart_attempts <= _RESTART_MAX_ATTEMPTS:
                delay = _RESTART_DELAY_MS * self._restart_attempts
                QTimer.singleShot(delay, self._restart_if_needed)

    def _restart_if_needed(self) -> None:
        """延迟重启回调：功能开关仍开启且无在途 stop 才拉起。"""
        from app.core.settings_store import get_ble_server_enabled
        if self._stop_requested or self._state in ("starting", "running"):
            return
        if get_ble_server_enabled():
            self.start()

    def _on_error_occurred(self, error) -> None:  # noqa: ANN001
        # FailedToStart 最常见：exe 缺失/被杀软隔离；探测循环会兜底报 failed
        pass
