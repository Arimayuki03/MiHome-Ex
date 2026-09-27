# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 连接质量卡（BLE/MQTT/Bemfa 三链路评分，web 头部徽标浮层的桌面版）。

上游 web 端把链路质量做成页头三枚徽标 + hover 浮层（app.js
renderQuality/renderBleQuality/renderMqttQuality/renderBemfaQuality，
每约 5s 一条 SSE quality 事件）；桌面端做成一张可直接放进面板任意
Tab 的卡片，三行分组同屏展示，不需要浮层交互。

数据契约（cuktech-api.md §SSE 事件表 quality 行）：
{"type":"quality","ble":{score,decrypt,notify,reconnect_score,
reconnect_count_5m,...,uptime,last_push_age,next_reconnect_delay},
"mqtt":{score,uptime,disconnects,publish_failures},
"bemfa":{score,uptime,ping_lost,reconnect_count}}

组件纪律：
- 纯展示、零网络零轮询：SSE quality 事件经 :meth:`apply_quality`
  整帧直喂，约 5s 一条、字段量少，直接全量重刷；
- 字段语义与文案逐行对齐上游（「未连接」「Ns前」「Ns后」「N次」照抄
  locales/zh-CN.js quality.*；formatDuration 逐行照抄 app.js）；
- MQTT/Bemfa 未启用（服务端未启用/未连接时 score=0，或 payload 缺
  字段）整组灰字「未启用」；BLE 是主链路，永远渲染（缺字段按缺省值）；
- 各组独立容错：单组 payload 畸形只影响该组（按缺省值渲染，不抛异常），
  apply_quality 顶层再兜一层 try，一组坏不影响其他组；
- 取色 SiColors 动态代理：评分三档 ≥80 绿/≥50 黄/否则红（上游
  scoreColor；SiColors 无 SUCCESS 语义键，绿档用现有约定中的正向色
  THEME，黄=WARN_TEXT、红=ERROR_TEXT，均为双主题调色板键）；retheme()
  重求值内联样式并按最近一帧数据重渲（值色随数据走）。
"""

import math

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.si_theme import SiColors

# ----------------------------------------------------------------------------
# 常量与文案（照抄上游 locales/zh-CN.js quality.*）
# ----------------------------------------------------------------------------

# 明细行 key -> 中文标签（zh-CN.js quality.* 原文）
_ROW_LABELS: dict[str, str] = {
    "connectionDuration": "连接时长",
    "lastPush": "最后推送",
    "nextReconnect": "下次重连",
    "decryptSuccess": "解密成功",
    "notifyResponse": "通知响应",
    "connectionStable": "连接稳定",
    "reconnect5m": "5min重连",
    "runtime": "运行时长",
    "disconnects": "断连次数",
    "publishFailures": "发送失败",
    "pingLost": "Ping丢包",
    "reconnectCount": "重连次数",
}

# 各链路明细行（顺序即展示顺序；「下次重连」仅重连等待期渲染）
_BLE_ROWS = ("connectionDuration", "lastPush", "nextReconnect",
             "decryptSuccess", "notifyResponse", "connectionStable",
             "reconnect5m")
_MQTT_ROWS = ("runtime", "disconnects", "publishFailures")
_BEMFA_ROWS = ("runtime", "pingLost", "reconnectCount")

_SCORE_MAX = 100


def _js_round(value) -> int | None:
    """宽松数值 + JS Math.round 语义（floor(x+0.5)，与 Python 银行家舍入
    不同）；None/脏值（含 NaN/inf）-> None。上游对 next_reconnect_delay
    取整用 Math.round。"""
    try:
        num = float(value)
        return int(math.floor(num + 0.5))
    except (TypeError, ValueError, OverflowError):
        return None


def _as_int(value, default: int = 0) -> int:
    """宽松转 int（Math.round 语义）：None/脏值回缺省，渲染路径用不抛异常。"""
    rounded = _js_round(value)
    return default if rounded is None else rounded


def _format_duration(sec) -> str:
    """时长文案：逐行照抄上游 app.js formatDuration（h/m/s 取整拼接）。"""
    sec = _as_int(sec)
    if sec <= 0:  # 上游 if (!sec) -> '0s'
        return "0s"
    hours, rem = divmod(sec, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h{minutes}m"
    if minutes > 0:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def _score_color(score: int) -> str:
    """评分分档色：≥80 绿 / ≥50 黄 / 否则红（上游 scoreColor）。

    SiColors 无 SUCCESS 语义键：绿档用 THEME（米家青绿，代码库既定的
    正向/在线色），黄=WARN_TEXT、红=ERROR_TEXT（双主题调色板均有定义）。
    """
    if score >= 80:
        return SiColors.THEME
    if score >= 50:
        return SiColors.WARN_TEXT
    return SiColors.ERROR_TEXT


def _section_enabled(section) -> bool:
    """MQTT/Bemfa 启用判定（读上游语义）：服务端未启用/未连接时回
    score=0，payload 缺字段同样按未启用——score 可解析且非 0 才算启用。"""
    if not isinstance(section, dict) or not section:
        return False
    score = _js_round(section.get("score"))
    return score is not None and score != 0


# ----------------------------------------------------------------------------
# _ScoreBar —— 自绘评分进度条
# ----------------------------------------------------------------------------


class _ScoreBar(QWidget):
    """圆角轨道 + 评分填充的进度条；填充色按分档取色（与评分文字同色）。

    取色在 paintEvent 内经 SiColors 动态代理读取，主题切换后 update()
    即以新调色板重绘，无需重建控件。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._score = 0
        self.setFixedHeight(6)

    def set_score(self, score: int) -> None:
        """整值替换评分（0-100 钳制）。"""
        self._score = max(0, min(int(score), _SCORE_MAX))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            rect = self.rect()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(SiColors.WINDOW_BG))
            painter.drawRoundedRect(rect, 3, 3)
            if self._score > 0:
                # 填充至少一个圆头宽，score>0 时视觉可见
                width = max(int(rect.width() * self._score / _SCORE_MAX),
                            rect.height())
                painter.setBrush(QColor(_score_color(self._score)))
                painter.drawRoundedRect(
                    QRect(rect.left(), rect.top(), width, rect.height()), 3, 3)
        finally:
            painter.end()


# ----------------------------------------------------------------------------
# _SectionGroup —— 单链路分组（标题行 + 评分条 + 明细行）
# ----------------------------------------------------------------------------


class _SectionGroup(QWidget):
    """单链路分组：标题行「BLE 87/100」+ 评分条 + 明细行（标签左值右）。

    未启用态：评分/进度条/明细整组隐藏，标题行以「未启用」灰字替代
    （上游徽标灰置语义的桌面化）。
    """

    def __init__(self, name: str, row_keys: tuple[str, ...], parent=None):
        super().__init__(parent)
        self._name = name
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # 标题行：BLE 87/100（评分按分档着色，"/100" 灰字）
        title = QHBoxLayout()
        title.setSpacing(6)
        self._name_label = QLabel(name)
        self._name_label.setFont(QFont("Microsoft YaHei UI", 10,
                                       QFont.Weight.DemiBold))
        self._score_label = QLabel()
        self._score_label.setFont(QFont("Microsoft YaHei UI", 10,
                                        QFont.Weight.DemiBold))
        self._score_suffix = QLabel(f"/{_SCORE_MAX}")
        self._score_suffix.setFont(QFont("Microsoft YaHei UI", 9))
        self._disabled_label = QLabel("未启用")
        self._disabled_label.setFont(QFont("Microsoft YaHei UI", 8))
        title.addWidget(self._name_label)
        title.addWidget(self._score_label)
        title.addWidget(self._score_suffix)
        title.addWidget(self._disabled_label)
        title.addStretch(1)
        lay.addLayout(title)

        # 评分进度条（自绘，与评分同色分档）
        self._bar = _ScoreBar()
        lay.addWidget(self._bar)

        # 明细行：标签左（灰）值右（主色/警示色），8pt
        self._row_labels: dict[str, QLabel] = {}
        self._value_labels: dict[str, QLabel] = {}
        self._row_frames: dict[str, QWidget] = {}
        for key in row_keys:
            frame = QWidget()
            row = QHBoxLayout(frame)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            label = QLabel(_ROW_LABELS[key])
            label.setFont(QFont("Microsoft YaHei UI", 8))
            value = QLabel()
            value.setFont(QFont("Microsoft YaHei UI", 8))
            value.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(label)
            row.addStretch(1)
            row.addWidget(value)
            self._row_labels[key] = label
            self._value_labels[key] = value
            self._row_frames[key] = frame
            lay.addWidget(frame)

        self._apply_styles()

    # ---------- 渲染 ----------

    def render(self, score: int, enabled: bool,
               rows: list[tuple[str, str, str | None]]) -> None:
        """整组重刷：score 评分、enabled 启用态、rows=[(key, 文案, 覆盖色)]。

        rows 未列出的明细行隐藏（如非重连等待期的「下次重连」）；
        enabled=False 时评分/进度条/明细全部隐藏，标题行显示「未启用」。
        覆盖色为 None 时值用主文本色；label setText 前无需判活（均为
        本组件自有控件）。
        """
        score = max(0, min(int(score), _SCORE_MAX))
        self._bar.set_score(score)
        self._score_label.setText(str(score))
        self._score_label.setStyleSheet(
            f"color: {_score_color(score)}; background: transparent;"
            f" font-size: 10pt;")
        self._score_label.setVisible(enabled)
        self._score_suffix.setVisible(enabled)
        self._disabled_label.setVisible(not enabled)
        self._bar.setVisible(enabled)
        for key, frame in self._row_frames.items():
            frame.setVisible(enabled and key in {row[0] for row in rows})
        for key, text, color in rows:
            self._value_labels[key].setText(text)
            self._value_labels[key].setStyleSheet(
                f"color: {color or SiColors.TEXT_PRIMARY};"
                f" background: transparent; font-size: 8pt;")

    # ---------- 主题观感 ----------

    def _apply_styles(self) -> None:
        """重设构造期求值的内联样式（retheme 时重刷；值色由 render 补）。"""
        self._name_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            f" font-size: 10pt;")
        self._score_suffix.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;"
            f" font-size: 9pt;")
        self._disabled_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;"
            f" font-size: 8pt;")
        for label in self._row_labels.values():
            label.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                f" font-size: 8pt;")


# ----------------------------------------------------------------------------
# 单链路渲染（对齐上游 renderBle/Mqtt/BemfaQuality，字段独立容错）
# ----------------------------------------------------------------------------


def _render_ble(group: _SectionGroup, section) -> None:
    """BLE 组：上游 renderBleQuality 对齐。BLE 为主链路永远渲染，
    残缺 payload 按缺省值渲染（uptime=0「未连接」、last_push 缺「无」）。"""
    ble = section if isinstance(section, dict) else {}
    score = _as_int(ble.get("score"))
    uptime = _as_int(ble.get("uptime"))
    rows: list[tuple[str, str, str | None]] = [
        ("connectionDuration",
         _format_duration(uptime) if uptime > 0 else "未连接", None),
    ]
    # 最后推送：null -> 「无」；>10s 黄色警示（上游 pushColor）
    last_push = _js_round(ble.get("last_push_age"))
    if last_push is not None:
        rows.append(("lastPush", f"{last_push}s前",
                     SiColors.WARN_TEXT if last_push > 10 else None))
    else:
        rows.append(("lastPush", "无", None))
    # 下次重连：仅重连等待期（字段存在）显示，黄色
    delay = _js_round(ble.get("next_reconnect_delay"))
    if delay is not None:
        rows.append(("nextReconnect", f"{delay}s后", SiColors.WARN_TEXT))
    rows += [
        ("decryptSuccess", f"{_as_int(ble.get('decrypt'))}%", None),
        ("notifyResponse", f"{_as_int(ble.get('notify'))}%", None),
        ("connectionStable", f"{_as_int(ble.get('reconnect_score'))}%", None),
        ("reconnect5m", f"{_as_int(ble.get('reconnect_count_5m'))}次", None),
    ]
    group.render(score, True, rows)


def _render_mqtt(group: _SectionGroup, section) -> None:
    """MQTT 组：上游 renderMqttQuality 对齐；未启用整组灰字。"""
    if not _section_enabled(section):
        group.render(0, False, [])
        return
    mqtt = section
    rows = [
        ("runtime", _format_duration(mqtt.get("uptime")), None),
        ("disconnects", f"{_as_int(mqtt.get('disconnects'))}", None),
        ("publishFailures", f"{_as_int(mqtt.get('publish_failures'))}", None),
    ]
    group.render(_as_int(mqtt.get("score")), True, rows)


def _render_bemfa(group: _SectionGroup, section) -> None:
    """Bemfa 组：上游 renderBemfaQuality 对齐；未启用整组灰字。"""
    if not _section_enabled(section):
        group.render(0, False, [])
        return
    bemfa = section
    rows = [
        ("runtime", _format_duration(bemfa.get("uptime")), None),
        ("pingLost", f"{_as_int(bemfa.get('ping_lost'))}/3", None),
        ("reconnectCount", f"{_as_int(bemfa.get('reconnect_count'))}", None),
    ]
    group.render(_as_int(bemfa.get("score")), True, rows)


_RENDERERS = {
    "ble": _render_ble,
    "mqtt": _render_mqtt,
    "bemfa": _render_bemfa,
}


# ----------------------------------------------------------------------------
# ConnectionQualityCard —— 连接质量卡
# ----------------------------------------------------------------------------


class ConnectionQualityCard(QWidget):
    """CUKTECH 连接质量卡：BLE / MQTT / Bemfa 三链路评分与明细。

    纯展示组件：零网络零轮询，数据经 :meth:`apply_quality` 直喂
    （SSE quality 事件整帧，约 5s 一条，直接全量重刷）；retheme()
    响应主题切换。基类用 QWidget、objectName 走全局 QSS 的 propCard
    皮肤（与面板分区卡同观感；QSS 选择器是 QFrame#propCard，故皮肤
    生效需 QFrame——这里用内联样式等价复刻，便于任意容器嵌入）。

    对外接口：
    - apply_quality(payload: dict) -> None
    - retheme() -> None
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("propCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(280)
        self._sections: dict[str, object] = {}
        self._groups: dict[str, _SectionGroup] = {
            "ble": _SectionGroup("BLE", _BLE_ROWS),
            "mqtt": _SectionGroup("MQTT", _MQTT_ROWS),
            "bemfa": _SectionGroup("Bemfa", _BEMFA_ROWS),
        }
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(10)
        for group in self._groups.values():
            root.addWidget(group)
        root.addStretch(1)
        self._apply_styles()

    # ---------- 数据注入（外部驱动，不发请求） ----------

    def apply_quality(self, payload: dict) -> None:
        """SSE quality 事件注入点（约 5s 一条，全量重刷）。

        payload 结构见模块 docstring；非 dict 静默忽略。各组独立容错：
        组内逐字段宽松解析（脏值按缺省渲染），组间再兜一层 try——
        单组渲染异常不拖累其他组。
        """
        if not isinstance(payload, dict):
            return
        for key in ("ble", "mqtt", "bemfa"):
            section = payload.get(key)
            self._sections[key] = section
            try:
                _RENDERERS[key](self._groups[key], section)
            except Exception:
                # 渲染路径已逐字段容错，此处仅兜底保证组间隔离
                continue

    # ---------- 主题观感 ----------

    def _apply_styles(self) -> None:
        """卡片皮肤与各组静态样式（propCard 全局 QSS 的内联等价）。"""
        self.setStyleSheet(
            f"QWidget#propCard {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 14px; }}")
        for group in self._groups.values():
            group._apply_styles()

    def retheme(self) -> None:
        """主题切换：重求值内联样式 + 按最近一帧数据重渲（值色随数据走）。

        进度条取色在 paintEvent 内动态读取，update() 自动跟随调色板。
        """
        self._apply_styles()
        for key, group in self._groups.items():
            try:
                _RENDERERS[key](group, self._sections.get(key))
            except Exception:
                continue
