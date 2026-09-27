# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH BLE 服务的 SSE 实时事件流（GET /api/events）接收模块。

桌面端实时数据通道：一条长连接接收 BLE 网关（cuktech-ble-server）推送的
init / status / port_update / settings / protocol / session_end / quality
事件，解析后经 Qt 信号分发给界面层，避免高频轮询 /api/status。接口契约
见 docs/dev-notes/cuktech-api.md §8（侦察代理逐行核对服务端源码产出）。

服务端契约要点（本模块的实现依据）：

- SSE 流**没有 event:/id: 行**，事件类型在 data JSON 内部的 "type" 字段；
  本模块解析 JSON 后按 type 分发，绝不依赖 SSE event 名。
- data 可能拆成多条 ``data:`` 行，按 SSE 规范处理：逐行聚合、空行派发、
  冒号开头为注释行（服务端每 15 秒一行 ``: keepalive``）。
- 服务端中间件对 /api/events 有 **120 秒硬超时**，连接必然每 ~2 分钟被
  掐断（EOF 或 HTTP 504）——这属于正常流程而非故障。本模块对一切非主动
  stop 的断开做指数退避重连（初始 2 秒、上限 30 秒、连成功后重置）；
  重连成功后服务端会重推 init 事件，消费者应以 init + status 全量刷新
  状态兜底（服务端事件队列上限 128 条、满了丢最旧，断流期间的事件不
  保证不丢）。

线程模型：独立 QThread 工作线程阻塞式读流（urllib.request 流式读，标准
库零新依赖），**不进 jobs.py 的串行队列**——SSE 是常驻长连接，排队会
饿死普通任务。信号从工作线程发射，Qt 跨线程自动排队投递，槽函数永远
在主线程执行，界面层无需自行加锁。

生命周期约定：构造后 :meth:`CuktechEventStream.start` 启动；销毁前必须
:meth:`CuktechEventStream.stop`（join 带超时，兜底强制终止，与 jobs.py
同一策略），否则带着运行中的线程销毁 QThread 会让解释器关闭阶段崩溃。
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)

# 指数退避参数（秒）。方法体运行期引用模块属性，测试可改写加速；
# 默认值即任务契约：初始 2 秒、上限 30 秒、连成功后重置。
INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 30.0

# socket 读超时秒数（urlopen 的 timeout，同时是 TCP 连接超时）。服务端
# keepalive 周期 15 秒，正常连接不会触发；触发后只检查停止标志与僵死
# 判定，不主动断开（也是 stop() 能在有限时间内唤醒阻塞读的机制）。
_READ_TIMEOUT = 5.0
# 连续无任何字节（含 keepalive）超过该秒数判定连接僵死，主动重连，
# 覆盖对端异常失联而 TCP 层未察觉的情形（阈值 = 4 个 keepalive 周期）。
_STALE_AFTER = 60.0


class CuktechEventStream(QObject):
    """CUKTECH BLE 服务 SSE 实时事件流客户端（自动解析 + 自动重连）。

    用法::

        stream = CuktechEventStream("http://127.0.0.1:18199")
        stream.status_event.connect(self._on_status)
        stream.port_event.connect(self._on_port)
        stream.generic_event.connect(self._on_generic)   # init 等其余类型
        stream.connected_changed.connect(self._on_connected)
        stream.start()
        ...
        stream.stop()   # 退出前必须调用，保证工作线程干净退出

    信号语义（均从工作线程发射，Qt 自动排队到主线程槽执行）：

    ==================  ============  ===================================
    信号                参数          语义
    ==================  ============  ===================================
    connected_changed   (bool,)       流建立成功 True / 断开 False。重连
                                      属常态（服务端 120s 硬超时），每次
                                      True 后服务端紧跟一条 init 事件
    status_event        (dict,)       type==status，结构同 /api/status；
                                      connected=false 是设备蓝牙离线
    port_event          (dict,)       type==port_update，单口实时数据，
                                      实时功率的主要来源（约 1 秒一条）
    settings_event      (dict,)       type==settings，设备设置变化
    generic_event       (str, dict)   其余类型（init/protocol/session_end/
                                      quality/未知），参数为 type 名
                                      （缺失时为空串）与原始 dict
    ==================  ============  ===================================
    """

    connected_changed = Signal(bool)
    status_event = Signal(dict)
    port_event = Signal(dict)
    settings_event = Signal(dict)
    generic_event = Signal(str, dict)

    def __init__(self, base_url: str = "http://127.0.0.1:8199", parent=None) -> None:
        """记录服务地址（默认本机 8199；Windows 服务端默认 18199）。"""
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._stop_event = threading.Event()
        self._connected = False
        self._backoff = INITIAL_BACKOFF
        # 与 jobs.py 相同的手法：循环体不长，直接改写 run 而不单独建
        # worker 类。线程随本对象销毁，stop() 必须先于析构调用。
        self._thread = QThread(self)
        self._thread.setObjectName("cuktech-sse")
        self._thread.run = self._run_loop

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动事件流（重复调用幂等；stop() 之后可再次 start）。"""
        if self._thread.isRunning():
            return
        self._stop_event.clear()
        self._thread.start()

    def stop(self, timeout_ms: int = 10000) -> None:
        """停止接收并退出工作线程（幂等，未 start 时调用同样安全）。

        停止标志让阻塞中的读循环至多 _READ_TIMEOUT 秒内醒来退出；
        join 等待 timeout_ms 毫秒，超时则强制终止（与 jobs.py 同一兜底：
        带着运行线程销毁 QThread 会让解释器关闭阶段崩溃）。退出后
        connected_changed(False) 恰好广播一次（若此前处于已连接态）。
        """
        self._stop_event.set()
        if self._thread.isRunning():
            if not self._thread.wait(timeout_ms):
                logger.warning("SSE 线程 %s ms 内未退出，强制终止", timeout_ms)
                self._thread.terminate()
                self._thread.wait(500)
        self._set_connected(False)

    def is_connected(self) -> bool:
        """当前流是否已建立（最近一次 connected_changed 的值）。"""
        return self._connected

    # ---------- 工作线程主体 ----------

    def _run_loop(self) -> None:
        """主循环：连接 → 读流 → 断开退避 → 重连，直至 stop()。"""
        logger.debug("SSE 工作线程启动")
        while not self._stop_event.is_set():
            response = None
            try:
                response = self._open_stream()
            except urllib.error.HTTPError as exc:
                # 服务端中间件对 /api/events 有 120 秒硬超时，超时以 504
                # 掐断连接——这是每 ~2 分钟一次的常态，不是故障
                logger.debug("SSE 连接 HTTP %s: %s", exc.code, exc.reason)
            except Exception as exc:  # noqa: BLE001 —— 连接失败一律退避重试
                logger.debug("SSE 连接失败: %s: %s", type(exc).__name__, exc)
            if self._stop_event.is_set():
                if response is not None:
                    response.close()
                break
            if response is None:
                if not self._sleep_backoff():
                    break  # 退避等待期间收到停止信号
                continue
            # 连接建立：广播状态并重置退避（"连成功后重置"）
            self._set_connected(True)
            self._backoff = INITIAL_BACKOFF
            try:
                self._read_events(response)
            except Exception as exc:  # noqa: BLE001 —— 读流异常统一视为断开
                logger.debug("SSE 流中断，准备重连: %s: %s",
                             type(exc).__name__, exc)
            finally:
                response.close()
                self._set_connected(False)
            if self._stop_event.is_set():
                break
            if not self._sleep_backoff():
                break
        logger.debug("SSE 工作线程退出")

    def _open_stream(self):
        """建立到 /api/events 的 SSE 长连接，返回流式响应对象。

        urllib 不主动声明 Accept-Encoding: gzip，按服务端契约（仅当请求
        带 gzip 才压缩）响应必然是未压缩文本，无需解压处理。
        """
        request = urllib.request.Request(
            self._base_url + "/api/events",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
        )
        return urllib.request.urlopen(request, timeout=_READ_TIMEOUT)

    def _read_events(self, response) -> None:
        """逐行读取并解析 SSE 流，直到 EOF/断开/僵死/主动停止。

        SSE 规范解析：``data:`` 行逐条聚合，空行即事件边界（派发），
        冒号开头为注释行忽略；event:/id:/retry: 等其余字段一律忽略
        （本服务不用它们，事件类型在 data JSON 的 "type" 字段里）。
        """
        data_lines: list[str] = []
        last_activity = time.monotonic()
        while not self._stop_event.is_set():
            try:
                raw = response.readline()
            except TimeoutError:
                # Python 3.10+ socket.timeout 即 TimeoutError。读超时不代表
                # 断开：窗口内还有 keepalive 就继续读；长时间无任何字节
                # 说明连接僵死，返回上层走重连
                if time.monotonic() - last_activity > _STALE_AFTER:
                    logger.debug("SSE 连接 %s 秒无数据，判定僵死并重连",
                                 _STALE_AFTER)
                    return
                continue
            if not raw:
                return  # EOF：服务端关闭连接（120s 超时/重启的常态断法）
            last_activity = time.monotonic()
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                # 空行 = 事件边界：派发聚合完毕的 data 行
                self._dispatch(data_lines)
                data_lines = []
                continue
            if line.startswith(":"):
                continue  # 注释行（服务端 15 秒一次的 ": keepalive"）
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]  # SSE 规范：去掉冒号后的单个空格
            if field == "data":
                data_lines.append(value)
            # 其余 SSE 字段（event:/id:/retry:/未知）按规范忽略
        # 走到这说明收到停止信号：丢弃未派发的半成品事件

    def _dispatch(self, data_lines: list[str]) -> None:
        """把一条事件的 data 行拼接为 JSON 并按 type 分发到信号。

        坏 JSON / 非对象 data 是服务端契约被破坏的信号：记警告后跳过，
        绝不让异常杀死读流线程（容错，后续事件继续正常分发）。
        """
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.warning("忽略无法解析的 SSE 事件: %s（data=%r）",
                           exc, payload[:200])
            return
        if not isinstance(event, dict):
            logger.warning("忽略非对象 SSE 事件: %r", payload[:200])
            return
        event_type = event.get("type")
        if event_type == "status":
            self.status_event.emit(event)
        elif event_type == "port_update":
            self.port_event.emit(event)
        elif event_type == "settings":
            self.settings_event.emit(event)
        else:
            # init / protocol / session_end / quality / 未知类型
            self.generic_event.emit(
                str(event_type) if event_type is not None else "", event)

    # ---------- 内部辅助 ----------

    def _set_connected(self, value: bool) -> None:
        """更新连接状态，仅在变化时广播（避免重复 True/False 刷屏）。"""
        if self._connected == value:
            return
        self._connected = value
        self.connected_changed.emit(value)

    def _sleep_backoff(self) -> bool:
        """指数退避等待，返回 False 表示等待期间收到停止信号。"""
        delay = self._backoff
        self._backoff = min(self._backoff * 2.0, MAX_BACKOFF)
        logger.debug("SSE %.1f 秒后重试连接", delay)
        # Event.wait 兼当可中断 sleep：stop() 置位即刻醒来
        return not self._stop_event.wait(delay)
