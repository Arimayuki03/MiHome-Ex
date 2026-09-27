# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电器协议开关面板——PIID 21 快充协议协商开关的 UI 模块。

每个端口可单独关闭某些快充协议（该端口之后不再以此协议协商，改动
即时生效）。位定义（PIID 21 原始 u32，cuktech-ble-server/state.py
PROTOCOL_SWITCH_BITS 逐行核实，编码 aFlags<<24|c3Flags<<16|c2Flags<<8|c1Flags）：

- C1: pd=bit0, pps=bit1, ufcs=bit2（bit3 为保留位，固定 1）
- C2: pd=bit8, pps=bit9, ufcs=bit10（bit11 保留）
- C3: ufcs=bit16, scp=bit17
- A : ufcs=bit24, scp=bit25

本面板只渲染开关与发起写命令，**不自带轮询定时器**：真实状态由外部
（CuktechPanel 的轮询回调 / SSE 事件路由）注入——

- :meth:`ProtocolSwitchPanel.refresh` 接收 /api/status 的
  ``protocol_switches``（键 c1/c2/c3/a）整帧刷新；
- :meth:`ProtocolSwitchPanel.apply_protocol_event` 接收 SSE
  ``{"type":"protocol","switches":{...},"protocol_extend":int}`` 事件。

写路径经 service 门面的 ``cuktech_set_protocol_switch(port, protocol,
on)``（约定签名，见模块尾注释）+ JobExecutor 后台线程；门面方法尚未
落地时以 getattr 防御，Toast「门面方法未就绪」并回滚开关，保证模块
可独立先行接入与测试。切换期间开关置 busy（禁用交互），成功后不做
乐观校准（保持用户切换后的视觉），等待下一次 refresh / SSE protocol
事件以设备真实状态覆盖；失败回滚开关视觉并 Toast 中文报错。

对键的容错：refresh 同时接受外层含 ``protocol_switches`` 的全量
status dict（自动解包），便于接入方直接透传。
"""

import shiboken6
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.si_theme import SiColors, themed_switch
from app.ui.toast import Toast

if TYPE_CHECKING:
    from app.core.jobs import JobExecutor
    from app.core.service import MijiaService

# ----------------------------------------------------------------------------
# 常量与端口元数据
# ----------------------------------------------------------------------------

# 端口行定义：(protocol_switches 的键, 对外 int 端口号, 展示名, 协议项)。
# 协议项顺序即渲染顺序；C1/C2 有 PD/PPS/UFCS，C3/USB-A 只有 UFCS/SCP。
_PORT_ROWS: tuple[tuple[str, int, str, tuple[str, ...]], ...] = (
    ("c1", 1, "C1", ("pd", "pps", "ufcs")),
    ("c2", 2, "C2", ("pd", "pps", "ufcs")),
    ("c3", 3, "C3", ("ufcs", "scp")),
    ("a", 4, "USB-A", ("ufcs", "scp")),
)

# 协议项 -> 展示名
_PROTOCOL_LABELS: dict[str, str] = {
    "pd": "PD", "pps": "PPS", "ufcs": "UFCS", "scp": "SCP"}


def _port_display(key: str) -> str:
    """protocol_switches 键（c1/c2/c3/a）-> 展示名。"""
    for row_key, _port, label, _protos in _PORT_ROWS:
        if row_key == key:
            return label
    return key.upper()


# ----------------------------------------------------------------------------
# ProtocolSwitchPanel
# ----------------------------------------------------------------------------


class ProtocolSwitchPanel(QFrame):
    """CUKTECH 充电器协议开关面板：说明行 + 四行端口协议开关组。

    视觉与 CuktechPanel 的分区卡片同构（propCard QFrame、8pt 灰字说明、
    10pt 端口名与协议名、themed_switch 开关）。取色一律 SiColors 动态
    代理，retheme() 重求值内联样式；网络调用经注入的 service 门面 +
    jobs 后台线程，回调以 shiboken6.isValid 判活后操作控件。
    """

    def __init__(self, service: "MijiaService", jobs: "JobExecutor",
                 parent=None):
        super().__init__(parent)
        self._service = service
        self._jobs = jobs
        # 正在写命令中的开关（键为 (端口键, 协议名)）：busy 期间禁用交互，
        # 且 refresh 不覆盖其视觉（服务端状态尚未变化，覆盖会回跳闪烁）
        self._busy: set[tuple[str, str]] = set()
        self._port_rows: dict[str, dict] = {}
        self._proto_labels: dict[tuple[str, str], QLabel] = {}

        self.setObjectName("propCard")
        self.setAttribute(Qt.WA_StyledBackground, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(10)

        self._hint_label = QLabel("关闭某协议后，该端口不再以此协议协商；改动即时生效")
        self._hint_label.setFont(QFont("Microsoft YaHei UI", 8))
        self._hint_label.setWordWrap(True)
        lay.addWidget(self._hint_label)

        for key, _port, label, protos in _PORT_ROWS:
            row = QHBoxLayout()
            row.setSpacing(10)
            name = QLabel(label)
            name.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
            name.setMinimumWidth(44)
            row.addWidget(name)
            row.addStretch(1)
            switches: dict[str, QWidget] = {}
            for proto in protos:
                proto_label = QLabel(_PROTOCOL_LABELS.get(proto, proto))
                proto_label.setFont(QFont("Microsoft YaHei UI", 10))
                switch = themed_switch()
                switch.toggled.connect(
                    lambda checked, k=key, p=proto, s=switch:
                        self._on_switch_toggled(k, p, checked, s))
                row.addWidget(proto_label)
                row.addWidget(switch)
                switches[proto] = switch
                self._proto_labels[(key, proto)] = proto_label
            lay.addLayout(row)
            self._port_rows[key] = {"name": name, "switches": switches}

        self._apply_inline_styles()

    # ---------- 主题观感 ----------

    def _apply_inline_styles(self) -> None:
        """重设构造期求值的内联样式（主题切换时由 retheme 调用）。"""
        self._hint_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")
        for key, row in self._port_rows.items():
            row["name"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
            for proto in row["switches"]:
                self._proto_labels[(key, proto)].setStyleSheet(
                    f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")

    def retheme(self) -> None:
        """主题切换：重求值内联样式（开关取色在 themed_switch 构造期
        绑定主题色，双主题同色无需重设）。"""
        self._apply_inline_styles()

    # ---------- 数据注入（外部驱动，不发请求） ----------

    def refresh(self, protocol_switches: dict) -> None:
        """整帧刷新开关状态（/api/status 的 protocol_switches 或 SSE）。

        protocol_switches 键为 c1/c2/c3/a，值为 {"pd": bool, ...}；缺省
        的端口/协议按关处理（服务端总是下发全量）。正在写命令中的开关
        跳过不覆盖，等命令落定后的下一帧校准。容错：直接传入外层含
        ``protocol_switches`` 的全量 status dict 时自动解包。
        """
        if not shiboken6.isValid(self):
            return
        if not isinstance(protocol_switches, dict):
            return
        inner = protocol_switches.get("protocol_switches")
        if isinstance(inner, dict):
            protocol_switches = inner
        for key, row in self._port_rows.items():
            entry = protocol_switches.get(key)
            if not isinstance(entry, dict):
                entry = {}
            for proto, switch in row["switches"].items():
                if (key, proto) in self._busy:
                    continue
                self._sync_switch(switch, bool(entry.get(proto, False)))

    def apply_protocol_event(self, payload: dict) -> None:
        """SSE protocol 事件注入点：{"type":"protocol","switches":{...}}。

        只消费 switches 字段（protocol_extend 原始值仅服务端事实的另一种
        表达，位图解析以 switches 为准）；type 缺失按 protocol 宽容处理，
        非 protocol 事件（settings/status 等）静默忽略。
        """
        if not isinstance(payload, dict):
            return
        if payload.get("type", "protocol") != "protocol":
            return
        self.refresh(payload.get("switches"))

    # ---------- 开关切写 ----------

    @staticmethod
    def _sync_switch(switch, checked: bool) -> None:
        """程序化同步开关状态（setChecked 会发 toggled，必须屏蔽信号）。

        SiSwitchRefactor 的自绘进度与 checked 状态是两套存储，同步补齐
        （与 CuktechPanel._sync_switch 同实现）。
        """
        switch.blockSignals(True)
        try:
            switch.setChecked(checked)
            switch.progress = 1.0 if checked else 0.0
            try:
                switch.progress_ani.setCurrentValue(1.0 if checked else 0.0)
            except Exception:
                pass
        finally:
            switch.blockSignals(False)

    def _on_switch_toggled(self, key: str, proto: str, on: bool,
                           switch) -> None:
        """用户切换开关：置 busy 后经门面 + jobs 下发，成功不回写视觉。"""
        self._busy.add((key, proto))
        switch.setEnabled(False)
        setter = getattr(self._service, "cuktech_set_protocol_switch", None)
        if not callable(setter):
            # 门面方法未落地：立即回滚，不进队列，保证模块独立可测
            self._on_toggle_failed(key, proto, on, None, facade_missing=True)
            return
        port = next(p for k, p, _l, _ps in _PORT_ROWS if k == key)
        self._jobs.submit(
            lambda: setter(port, proto, on),
            on_success=lambda _, k=key, p=proto, s=switch, o=on:
                self._on_toggle_done(k, p, s, o),
            on_error=lambda e, k=key, p=proto, s=switch, o=on:
                self._on_toggle_failed(k, p, o, e),
        )

    def _on_toggle_done(self, key: str, proto: str, switch, on: bool) -> None:
        self._busy.discard((key, proto))
        if not shiboken6.isValid(self):
            return
        switch.setEnabled(True)
        # 不做乐观校准：设备真实状态等下一帧 refresh / SSE protocol 覆盖
        Toast.info(self,
                   f"已{'打开' if on else '关闭'} {_port_display(key)} "
                   f"{_PROTOCOL_LABELS.get(proto, proto)}", 2000)

    def _on_toggle_failed(self, key: str, proto: str, on: bool,
                          error: Exception | None,
                          facade_missing: bool = False) -> None:
        self._busy.discard((key, proto))
        if not shiboken6.isValid(self):
            return
        switch = self._port_rows[key]["switches"][proto]
        switch.setEnabled(True)
        # 失败回弹开关视觉状态，等下一次注入以真实值覆盖
        self._sync_switch(switch, not on)
        if facade_missing:
            Toast.info(self, "门面方法未就绪", 2500)
        else:
            Toast.info(self,
                       f"切换 {_port_display(key)} "
                       f"{_PROTOCOL_LABELS.get(proto, proto)} 失败：{error}",
                       3000)


# ----------------------------------------------------------------------------
# 门面约定（待 service.py 落地，本模块按此签名调用）
# ----------------------------------------------------------------------------
#
#     def cuktech_set_protocol_switch(self, port: int, protocol: str,
#                                     on: bool) -> None:
#         """设置单口协议开关。port 为对外 int 1-4（1=C1、2=C2、3=C3、
#         4=USB-A）；protocol 取 "pd"/"pps"/"ufcs"/"scp"（须与端口能力
#         匹配：C1/C2 无 scp，C3/A 无 pd/pps）；on 为 True=开启/False=
#         关闭。内部转 CuktechClient.set_protocol_switch(port, protocol,
#         action="on"/"off")，失败抛 ServiceError（中文消息直接可展示）。
#         """
#
# 注意 PIID 21 的 pd/pps 仅 C1/C2 有、scp 仅 C3/A 有；端口能力不匹配的
# 组合服务端 400 拒绝，UI 侧因按端口渲染协议项天然不会发起非法组合。
