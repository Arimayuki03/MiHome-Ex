# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电器本地服务 HTTP 客户端——全项目唯一感知该 BLE 网关服务的模块。

桌面端通过局域网 HTTP 服务（cuktech-ble-server，默认 http://127.0.0.1:8199）
控制 CUKTECH 10 Ultra 充电器；其余代码一律经 app/core/service.py 门面访问，
不允许在别处出现该服务的任何端点知识。接口契约见
docs/dev-notes/cuktech-api.md（侦察代理逐行核对服务端源码产出）。

设计约定：

- 纯同步、零新依赖：HTTP 用标准库 urllib.request。本类只被 jobs.py 的
  串行队列工作线程调用，不做内部加锁，实例仅持有不可变配置。
- 对外端口标识统一为 int 1-4：1=C1（Type-C1）、2=C2、3=C3、4=A（USB-A）。
  服务端有三套命名——/api/status 的 ports 键是字符串 "1"-"4"、命令类端点
  （/api/port、/api/charge-limits、/api/protocol）用 "c1"/"c2"/"c3"/"a"、
  /api/sessions 返回的 port 字段是 int 1-4——全部在本类内部完成转换，
  调用方永远只见 int。
- 错误统一抛 :class:`ServiceError`（消息为用户可直接阅读的中文；
  service.py 门面捕获后原样透传给界面层）。特别注意：BLE 命令类端点
  （/api/set、/api/port、/api/charge-limits、/api/protocol）在设备离线时
  仍返回 HTTP 200，错误在 body 的 {"ok": false, "error": ...} 里，
  必须解析 body，不能只看状态码。
- 不实现 SSE（/api/events），实时通道由后续单独任务负责。
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# /api/port 等命令端点使用的端口名（服务端大小写不敏感，统一小写发送）
_PORT_KEYS: tuple[str, ...] = ("c1", "c2", "c3", "a")

# 充电器模式（PIID 5）取值 -> 内部名：1=AI、2=数码生态、3=单口、4=均衡
SCENE_MODES: dict[int, str] = {1: "ai", 2: "eco", 3: "single", 4: "balanced"}

# 延时关闭（PIID 8-12）分钟数上限（0=取消）；设备实际支持 1-240 分钟
_DELAY_OFF_MAX_MINUTES = 240
# 对外 int 端口 1-4 -> 单口延时关闭 PIID（1=C1、2=C2、3=C3、4=USB-A）；
# PIID 8 是总口（作用于全部端口），不映射端口，单独用 set_delay_off_all 暴露
_DELAY_OFF_PIIDS: tuple[int, ...] = (9, 10, 11, 12)


def _port_to_key(port: int) -> str:
    """对外 int 端口号（1-4）转换为命令端点的端口名（c1/c2/c3/a）。"""
    if 1 <= port <= len(_PORT_KEYS):
        return _PORT_KEYS[port - 1]
    raise ServiceError(f"端口号无效：{port}（有效范围 1-4，1=C1、2=C2、3=C3、4=USB-A）")


def _port_to_index(port: int) -> int:
    """对外 int 端口号（1-4）校验后原样返回（/api/status 的 ports 键即 "1"-"4"）。"""
    if not 1 <= port <= 4:
        raise ServiceError(f"端口号无效：{port}（有效范围 1-4，1=C1、2=C2、3=C3、4=USB-A）")
    return port


def _key_to_port(key: str) -> int:
    """服务端端口名（c1/c2/c3/a，大小写不敏感）转换为对外 int 端口号（1-4）。

    未知键名说明服务端契约变化，按协议错误处理。
    """
    normalized = str(key).strip().lower()
    if normalized in ("c1", "c2", "c3"):
        return int(normalized[1])
    if normalized == "a":
        return 4
    raise ServiceError(f"服务端返回未知端口名：{key}")


class ServiceError(Exception):
    """携带用户可直接阅读的中文信息，界面层直接展示 message 即可。

    与 app/core/service.py 的同名异常语义一致；门面层捕获本异常后
    原样透传（或统一转成 service 层的 ServiceError）。
    """


class CuktechClient:
    """CUKTECH BLE 网关服务的同步 HTTP 客户端。

    所有方法纯同步、线程安全（仅不可变配置，无共享可变状态），供
    jobs.py 串行队列工作线程调用。除 :meth:`connected` 按约定把失败
    折叠成 False 外，所有网络/协议错误统一抛 :class:`ServiceError`。

    端口约定：对外一律 int 1-4（1=C1、2=C2、3=C3、4=USB-A），
    与服务端三套命名之间的转换全部在本类内部完成。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8199",
                 timeout: float = 4.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ---------- HTTP 基础层 ----------

    def _request(self, method: str, path: str, *,
                 body: dict[str, Any] | None = None,
                 query: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送一次 JSON 请求并解析 JSON 响应，返回原始 dict。

        HTTP 非 2xx 或 body 的 ok=false 统一转中文 ServiceError。
        query 值做 str() 转换与 URL 编码；body 序列化为 UTF-8 JSON。
        """
        url = self._base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: str(v) for k, v in query.items()})
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            # aiohttp 的 request.json() 依赖该头解析 body
            headers["Content-Type"] = "application/json"
        try:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
            payload = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 错误响应通常也是 JSON（{"ok": false, "error": ...}），尽力解析
            # 出服务端 error 文本拼进消息；解析失败退回 HTTP 状态短语
            try:
                err_body = json.loads(exc.read().decode("utf-8"))
                detail = str(err_body.get("error") or exc.reason)
            except Exception:
                detail = str(exc.reason)
            logger.debug("CUKTECH 服务 HTTP %s %s 失败: %s", method, path, detail)
            raise self._http_error(exc.code, detail) from exc
        except TimeoutError as exc:
            # Python 3.10+ socket.timeout 即 TimeoutError（读响应阶段也可能抛）
            logger.debug("CUKTECH 服务 %s %s 请求超时", method, path)
            raise ServiceError("充电器命令超时：设备可能离线") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            # timeout 也可能被包在 URLError 里（连接阶段）
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                logger.debug("CUKTECH 服务 %s %s 连接超时", method, path)
                raise ServiceError("充电器命令超时：设备可能离线") from exc
            logger.debug("CUKTECH 服务 %s %s 连接失败: %s", method, path, reason)
            raise ServiceError(f"充电器服务连接失败：{reason}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("CUKTECH 服务 %s %s 返回非 JSON: %s", method, path, exc)
            raise ServiceError(f"充电器服务返回了无法解析的响应：{exc}") from exc
        except ConnectionResetError as exc:
            # 连接被服务端中断（如服务进程重启/崩溃恢复）
            logger.debug("CUKTECH 服务 %s %s 连接被中断: %s", method, path, exc)
            raise ServiceError("充电器服务连接被中断：服务可能正在重启") from exc
        except OSError as exc:
            # URLError 之外的套接字层错误兜底
            logger.debug("CUKTECH 服务 %s %s 网络错误: %s", method, path, exc)
            raise ServiceError(f"充电器服务连接失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise ServiceError("充电器服务返回了非对象响应")
        # 命令类端点设备离线时仍返回 HTTP 200，错误藏在 body 的 ok=false：
        # 这里统一解析，调用方拿到的 dict 必然是成功结果
        if payload.get("ok") is False:
            detail = str(payload.get("error") or "未知错误")
            logger.debug("CUKTECH 服务 %s %s 命令被拒绝: %s", method, path, detail)
            if detail == "not connected":
                raise ServiceError("充电器离线：蓝牙未连接，命令未下发")
            if detail == "command timeout":
                raise ServiceError("充电器命令超时：设备可能离线")
            raise ServiceError(f"充电器拒绝命令：{detail}")
        return payload

    def _http_error(self, code: int, detail: str) -> ServiceError:
        """把 HTTP 状态码 + 服务端 error 文本组装为中文 ServiceError。"""
        if code == 400:
            return ServiceError(f"充电器拒绝命令：{detail}")
        if code == 404:
            return ServiceError("充电器服务接口不存在（服务版本不匹配？）")
        if code == 413:
            return ServiceError("充电器命令过大被服务端拒绝")
        if code == 504:
            return ServiceError("充电器命令超时：设备可能离线")
        return ServiceError(f"充电器服务错误（HTTP {code}）：{detail}")

    # ---------- 状态与健康 ----------

    def health(self) -> dict[str, Any]:
        """GET /api/health：服务进程健康检查（ok 恒 true）。

        注意响应的 ``healthy`` 字段恒为 true，不能当作设备在线判定；
        设备在线用 :meth:`connected` 或 :meth:`status` 的 connected。
        """
        return self._request("GET", "/api/health")

    def status(self) -> dict[str, Any]:
        """GET /api/status：全量状态快照（原始 dict，服务端原始命名）。

        ports 键是字符串 "1"-"4"（1=C1、2=C2、3=C3、4=A）；``connected``
        是设备蓝牙在线判定。注意：响应里没有"本会话已充 Wh"，充电进度
        要用 :meth:`charge_limits` 的 session_wh。
        """
        return self._request("GET", "/api/status")

    def connected(self) -> bool:
        """设备蓝牙是否在线（/api/status 的 connected 字段）。

        任何失败（服务未启动、超时、非 JSON）都按离线处理返回 False
        不抛异常，供界面轮询直接使用。
        """
        try:
            payload = self._request("GET", "/api/status")
        except ServiceError as exc:
            logger.debug("connected() 探测失败按离线处理: %s", exc)
            return False
        return payload.get("connected") is True

    # ---------- 设备设置（PIID） ----------

    def set_setting(self, piid: int, value: int) -> None:
        """POST /api/set：写设备设置（PIID + int 值）。

        可写 PIID：5 充电器模式 / 6 息屏时间 / 8-12 定时关断分钟数 /
        13 语言 / 15 USB-A 涓流 / 16 端口开关位图 / 19,20 屏幕相关 /
        21 协议扩展原始值。取值范围由服务端校验，越界 400 转 ServiceError。
        """
        self._request("POST", "/api/set", body={"piid": int(piid), "value": int(value)})

    def set_scene(self, mode: int) -> None:
        """写 PIID 5：切换充电器模式（1=AI、2=数码生态、3=单口、4=均衡）。

        mode 必须是 :data:`SCENE_MODES` 里的合法取值，本地先校验，
        非法值直接抛中文 ServiceError，不下发网络请求。
        """
        if mode not in SCENE_MODES:
            valid = "、".join(str(k) for k in sorted(SCENE_MODES))
            raise ServiceError(f"充电器模式无效：{mode}（有效取值 {valid}，"
                               "1=AI、2=数码生态、3=单口、4=均衡）")
        self.set_setting(5, mode)

    def set_screen_timeout(self, value: int) -> None:
        """写 PIID 6：息屏时间（1=5分钟、2=10分钟、3=30分钟、4=常亮、5=1分钟）。

        本地校验 1-5，越界直接抛中文 ServiceError，不下发网络请求。
        """
        if not 1 <= int(value) <= 5:
            raise ServiceError(f"息屏时间无效：{value}（有效取值 1-5，"
                               "5=1分钟、1=5分钟、2=10分钟、3=30分钟、4=常亮）")
        self.set_setting(6, int(value))

    def set_delay_off(self, port: int, minutes: int) -> None:
        """写 PIID 9-12：设置单端口延时关闭分钟数（0=取消延时）。

        port 为对外 int 1-4（1=C1、2=C2、3=C3、4=USB-A），映射到
        PIID 9/10/11/12；总口（PIID 8）用 :meth:`set_delay_off_all`。
        minutes 取 0-240，越界本地先拒绝不下发。
        """
        index = _port_to_index(port)
        minutes = int(minutes)
        if not 0 <= minutes <= _DELAY_OFF_MAX_MINUTES:
            raise ServiceError(
                f"延时关闭分钟数无效：{minutes}（有效范围 0-{_DELAY_OFF_MAX_MINUTES}，"
                "0=取消延时）")
        self.set_setting(_DELAY_OFF_PIIDS[index - 1], minutes)

    def set_delay_off_all(self, minutes: int) -> None:
        """写 PIID 8：设置全部端口的总延时关闭分钟数（0=取消延时）。

        minutes 取 0-240，越界本地先拒绝不下发。
        """
        minutes = int(minutes)
        if not 0 <= minutes <= _DELAY_OFF_MAX_MINUTES:
            raise ServiceError(
                f"延时关闭分钟数无效：{minutes}（有效范围 0-{_DELAY_OFF_MAX_MINUTES}，"
                "0=取消延时）")
        self.set_setting(8, minutes)

    def set_device_language(self, chinese: bool) -> None:
        """写 PIID 13：设备屏幕显示语言（True=中文，False=English）。"""
        self.set_setting(13, 1 if chinese else 0)

    def set_usb_a_trickle(self, enabled: bool) -> None:
        """写 PIID 15：USB-A 口小电流（涓流）模式开关。"""
        self.set_setting(15, 1 if enabled else 0)

    def set_idle_screen_off(self, enabled: bool) -> None:
        """写 PIID 19：空闲时自动息屏开关。"""
        self.set_setting(19, 1 if enabled else 0)

    def set_screen_lock(self, enabled: bool) -> None:
        """写 PIID 20：屏幕方向锁开关（锁定当前方向不随摆放旋转）。"""
        self.set_setting(20, 1 if enabled else 0)

    def set_port_enabled(self, port: int, on: bool) -> dict[str, Any]:
        """POST /api/port：开关单个端口（底层写 PIID 16 位图）。

        port 为对外 int 1-4。返回服务端响应体（含新的位图 ``value``）。
        """
        key = _port_to_key(_port_to_index(port))
        return self._request("POST", "/api/port",
                             body={"port": key, "action": "on" if on else "off"})

    def set_port_enabled_all(self, on: bool) -> dict[str, Any]:
        """POST /api/port：一键开关全部四个端口（服务端 "all"）。

        返回服务端响应体（含新的位图 ``value``，全开 0x0F/全关 0）。
        """
        return self._request("POST", "/api/port",
                             body={"port": "all", "action": "on" if on else "off"})

    def enable_ble(self, enabled: bool = True) -> dict[str, Any]:
        """POST /api/enable：启停服务的 BLE 连接任务。

        返回 ok 不代表蓝牙已连上（连接是异步的），需轮询 :meth:`status`。
        enabled=False 会断开设备并把四个端口状态清零。
        """
        return self._request("POST", "/api/enable", body={"enabled": bool(enabled)})

    # ---------- 充电量限额（到量自动关断） ----------

    def charge_limits(self) -> dict[str, Any]:
        """GET /api/charge-limits：读充电量限额与各口会话进度。

        返回体与原始响应一致，但 ``limits`` 的键已从 c1/c2/c3/a 规整为
        对外 int 端口 1-4。每项含 wh（限额，0=禁用）/mode（once/always）/
        fired（本会话是否已触发过关断）/session_wh（本会话已输出能量，
        可当充电进度用）/is_charging。
        """
        payload = self._request("GET", "/api/charge-limits")
        raw_limits = payload.get("limits")
        if isinstance(raw_limits, dict):
            payload["limits"] = {_key_to_port(k): v for k, v in raw_limits.items()}
        return payload

    def set_charge_limit(self, port: int, wh: float, mode: str = "once") -> None:
        """POST /api/charge-limits：设置单端口充电量限额。

        wh 为充电器输出能量口径（非设备实际充入），<=0 表示禁用；
        mode 为 "once"（达到即关断并清零）或 "always"（每次充电会话
        重新武装）。wh 越界（上限 1000）或 mode 非法由服务端 400 拒绝。
        """
        key = _port_to_key(_port_to_index(port))
        self._request("POST", "/api/charge-limits",
                      body={"port": key, "wh": wh, "mode": mode})

    def set_charge_limits(self, limits: dict[int, dict[str, Any]]) -> None:
        """POST /api/charge-limits：批量设置多口限额（服务端合并语义）。

        键为对外 int 端口，值是 {"wh": float, "mode": str}（字段可选，
        只更新提及的端口/字段）。任何非法项服务端都会拒绝整个请求，
        不存在静默部分成功。
        """
        if not limits:
            raise ServiceError("充电量限额批量设置至少要指定一个端口")
        converted = {_port_to_key(_port_to_index(port)): dict(spec)
                     for port, spec in limits.items()}
        self._request("POST", "/api/charge-limits", body={"limits": converted})

    # ---------- 图表与历史 ----------

    def chart(self, hours: float = 1.0, interval: int = 30) -> dict[str, Any]:
        """GET /api/chart：功率图表数据。

        hours 为时间窗（最大 720，越界服务端 400）；interval 为桶宽
        秒数（最小 5，更小会被服务端静默钳到 5）。返回体含 labels
        （本地时间字符串）与 datasets（power 固定 5 条含 Total，
        voltage/current 各 4 条），labels 与 data 数组一一对应。
        """
        return self._request("GET", "/api/chart",
                             query={"hours": hours, "interval": interval})

    def sessions(self, port: int | None = None, period: str = "today",
                 limit: int = 10, page: int = 1) -> dict[str, Any]:
        """GET /api/sessions：充电会话历史（分页）。

        port 为对外 int 1-4 或 None=全部端口；period 取 today /
        yesterday / week / month（week、month 是滚动 7/30 天），传错值
        服务端静默回退为全部历史。limit 每页条数（服务端钳到最大 50）。
        返回体无 ok 字段，含 sessions/total/page/limit/pages；每条会话
        的 port 字段是 int 1-4，start_time/end_time 是 Unix 秒浮点，
        is_active=true 表示会话进行中（end_time 为 null）。
        """
        query: dict[str, Any] = {"period": period, "limit": limit, "page": page}
        if port is not None:
            query["port"] = _port_to_key(_port_to_index(port))
        return self._request("GET", "/api/sessions", query=query)

    def session_points(self, session_id: int, downsample: int = 0) -> dict[str, Any] | None:
        """GET /api/sessions/{id}/points：单次会话的电压/电流/功率点列。

        downsample>0 时服务端降采样。会话不存在返回 None（404 按
        "查不到" 语义处理），其余错误照常抛 ServiceError。
        """
        try:
            return self._request(
                "GET", f"/api/sessions/{int(session_id)}/points",
                query={"downsample": downsample} if downsample > 0 else None)
        except ServiceError as exc:
            if "404" in str(exc) or "不存在" in str(exc):
                return None
            raise

    def export_session_csv(self, session_id: int,
                           save_path: str | Path) -> Path:
        """GET /api/sessions/{id}/export：下载单次会话 CSV 到本地文件。

        浏览器场景该端点回 text/csv 附件，桌面端直接把响应体落盘到
        save_path（Path 接受 str），返回最终写入路径（save_path 为
        目录时自动补服务端建议文件名 session_{id}.csv）。CSV 内容
        原样写入不做任何解析加工；HTTP 非 2xx 照常转中文 ServiceError
        （404 即会话不存在）。
        """
        path = Path(save_path)
        url = f"{self._base_url}/api/sessions/{int(session_id)}/export"
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/csv"},
                                         method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                disposition = str(resp.headers.get("Content-Disposition") or "")
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read().decode("utf-8"))
                detail = str(err_body.get("error") or exc.reason)
            except Exception:
                detail = str(exc.reason)
            logger.debug("CUKTECH 服务导出会话 %s CSV 失败: %s", session_id, detail)
            raise self._http_error(exc.code, detail) from exc
        except TimeoutError as exc:
            raise ServiceError("充电器命令超时：设备可能离线") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise ServiceError("充电器命令超时：设备可能离线") from exc
            raise ServiceError(f"充电器服务连接失败：{reason}") from exc
        except OSError as exc:
            raise ServiceError(f"充电器服务连接失败：{exc}") from exc
        # save_path 是目录时用 Content-Disposition 的文件名兜底
        if path.suffix == "" and path.exists() and path.is_dir():
            filename = "session.csv"
            for part in disposition.split(";"):
                part = part.strip()
                if part.lower().startswith("filename"):
                    _, _, value = part.partition("=")
                    filename = value.strip().strip('"').strip("'") or filename
                    break
            path = path / filename
        try:
            path.write_bytes(raw)
        except OSError as exc:
            raise ServiceError(f"CSV 写入失败：{exc}") from exc
        return path

    def energy_stats(self, period: str = "today") -> dict[str, Any]:
        """GET /api/energy/stats：分时段电量统计（服务端已合并进行中会话）。

        返回体含 total_wh/session_count/avg_power_w/peak_power_w/
        total_duration_sec/by_port（键为 int 1-4）。
        """
        return self._request("GET", "/api/energy/stats", query={"period": period})

    def energy_protocols(self, period: str = "today") -> dict[str, Any]:
        """GET /api/energy/protocols：按快充协议聚合的电量统计分布。"""
        return self._request("GET", "/api/energy/protocols", query={"period": period})

    def statistics(self, port: int, hours: float = 24.0) -> dict[str, Any]:
        """GET /api/statistics/{port}：单端口统计（均值/峰值/总电量/占比）。

        hours 上限 720。无数据时 data 只有 port/hours/samples=0。
        """
        index = _port_to_index(port)
        return self._request("GET", f"/api/statistics/{index}", query={"hours": hours})

    # ---------- 协议开关（PIID 21） ----------

    def set_protocol_switch(self, port: int, protocol: str,
                            action: str = "toggle") -> None:
        """POST /api/protocol：设置单口协议开关（pd/pps/ufcs/scp）。

        action 为 "toggle"/"on"/"off"；协议名与端口能力匹配由服务端
        校验（未知组合 400 → ServiceError）。
        """
        key = _port_to_key(_port_to_index(port))
        self._request("POST", "/api/protocol",
                      body={"port": key, "protocol": protocol, "action": action})

    def set_protocol_switches(self, switches: dict[int, dict[str, bool]]) -> None:
        """POST /api/protocol：批量设置协议开关（服务端整体编码为 PIID 21）。

        键为对外 int 端口，值如 {"pd": true, "pps": false, "ufcs": false}
        （C1/C2 口用 pd/pps/ufcs，C3/A 口用 ufcs/scp）。
        """
        converted = {_port_to_key(_port_to_index(port)): dict(spec)
                     for port, spec in switches.items()}
        self._request("POST", "/api/protocol", body={"switches": converted})

    def set_protocol_raw(self, value: int) -> None:
        """POST /api/protocol：直接写 PIID 21 原始 u32 值（0-0xFFFFFFFF）。

        仅从 :meth:`status` 的 protocol_extend 读到的值回写时使用；
        手工构造位模式极易踩错位，优先用上面两个结构化方法。
        """
        self._request("POST", "/api/protocol", body={"value": int(value)})
