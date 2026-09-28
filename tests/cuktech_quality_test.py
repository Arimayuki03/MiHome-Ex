# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 连接质量卡自测：离屏运行，零网络。

用法: .venv\\Scripts\\python.exe tests/cuktech_quality_test.py

仿 tests/cuktech_limits_test.py 的离屏 + grab() 模式：直接实例化
ConnectionQualityCard、灌 SSE quality 事件 payload（结构见
cuktech-api.md §SSE 事件表 quality 行），断言三组评分/颜色分档
（80+/50+/低）、明细文案（formatDuration 1h2m 格式、最后推送秒数、
>10s 黄色、Ns后重连行）、MQTT/Bemfa 未启用灰字、BLE 残缺容错、
畸形 payload 组间隔离；暗/亮双主题各来一遍（grab 非空白 + retheme
不崩）。组件纯展示，无需假门面。
"""

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体（Qt 6 不再内置 fonts 目录）。指向系统字体
# 目录让字体数据库有真实字体可回退，离屏渲染与实际 GUI 一致（详见
# cuktech_limits_test.py 同款注释）。
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_quality_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.ui import si_theme
from app.ui.si_theme import SiColors
from app.ui.theme_service import apply_theme

from app.ui.cuktech_quality import ConnectionQualityCard

# --------------------------------------------------------------------------
# 假数据：结构与 SSE quality 事件 payload 一致（cuktech-api.md §事件表）
# --------------------------------------------------------------------------

FULL_PAYLOAD = {
    "type": "quality",
    "ble": {
        "score": 87, "decrypt": 99, "notify": 82, "reconnect_score": 100,
        "reconnect_count_5m": 0, "keepalive": 100, "total_frames": 1200,
        "decrypt_failures": 1, "uptime": 3725,        # 1h2m5s -> "1h2m"
        "last_push_age": 3, "next_reconnect_delay": None,
    },
    "mqtt": {"score": 92, "uptime": 7325, "disconnects": 1,
             "publish_failures": 3},
    "bemfa": {"score": 65, "uptime": 95, "ping_lost": 1, "reconnect_count": 2},
}

# 空对象 -> 服务端「未启用/未连接」语义（score=0）
DISABLED_PAYLOAD = {
    "type": "quality",
    "ble": FULL_PAYLOAD["ble"],
    "mqtt": {"score": 0, "uptime": 0, "disconnects": 0, "publish_failures": 0},
    "bemfa": {},
}

# 评分三档 + 重连等待期（颜色取 SiColors 动态代理，须在当前主题下
# 调用时求值——模块级常量会在 import 期固定为当时主题的色值）
def _tiers() -> list[tuple[int, str]]:
    return [
        (95, SiColors.THEME),
        (80, SiColors.THEME),
        (79, SiColors.WARN_TEXT),
        (50, SiColors.WARN_TEXT),
        (49, SiColors.ERROR_TEXT),
        (0, SiColors.ERROR_TEXT),
    ]

RECONNECTING_BLE = {
    "score": 42, "decrypt": 96, "notify": 20, "reconnect_score": 25,
    "reconnect_count_5m": 3, "uptime": 0, "last_push_age": None,
    "next_reconnect_delay": 7.6,
}

STALE_PUSH_BLE = {**FULL_PAYLOAD["ble"], "last_push_age": 12}

BROKEN_BLE = {"score": "abc", "uptime": None, "decrypt": {}, "notify": [],
              "reconnect_score": "x", "reconnect_count_5m": None,
              "last_push_age": "NaN", "next_reconnect_delay": object()}


def _image_is_blank(image) -> bool:
    """空白判定：全图非背景像素 <= 16 视为空白（与面板测试同标准）。"""
    w, h = image.width(), image.height()
    counts: dict[str, int] = {}
    for x in range(w):
        for y in range(h):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    non_bg = w * h - max(counts.values())
    return non_bg <= 16


def _value_style(card, group: str, key: str) -> str:
    """取组内明细行值的 styleSheet（颜色断言用）。"""
    return card._groups[group]._value_labels[key].styleSheet()


def _score_style(card, group: str) -> str:
    return card._groups[group]._score_label.styleSheet()


def test_full_payload(theme: str) -> None:
    """全量 payload：三组评分/颜色分档/明细文案/警示色。"""
    apply_theme(theme)
    card = ConnectionQualityCard()
    card.resize(360, 420)
    card.show()
    app.processEvents()

    card.apply_quality(FULL_PAYLOAD)
    app.processEvents()

    groups = card._groups

    # --- BLE：87 绿档 + 明细文案（formatDuration 1h2m5s -> "1h2m"） ---
    assert groups["ble"]._score_label.text() == "87"
    assert SiColors.THEME in _score_style(card, "ble"), \
        f"[{theme}] 87 分未用绿档: {_score_style(card, 'ble')}"
    assert groups["ble"]._value_labels["connectionDuration"].text() == "1h2m"
    assert groups["ble"]._value_labels["lastPush"].text() == "3s前"
    assert SiColors.WARN_TEXT not in _value_style(card, "ble", "lastPush"), \
        "3s 前不应黄色警示"
    assert groups["ble"]._value_labels["decryptSuccess"].text() == "99%"
    assert groups["ble"]._value_labels["notifyResponse"].text() == "82%"
    assert groups["ble"]._value_labels["connectionStable"].text() == "100%"
    assert groups["ble"]._value_labels["reconnect5m"].text() == "0次"
    # 无 next_reconnect_delay：该行不显示
    assert not groups["ble"]._row_frames["nextReconnect"].isVisible(), \
        "无重连等待时不应显示「下次重连」行"

    # --- MQTT：92 绿档 + 明细（7325s = 2h2m5s -> "2h2m"） ---
    assert groups["mqtt"]._score_label.text() == "92"
    assert SiColors.THEME in _score_style(card, "mqtt")
    assert groups["mqtt"]._value_labels["runtime"].text() == "2h2m"
    assert groups["mqtt"]._value_labels["disconnects"].text() == "1"
    assert groups["mqtt"]._value_labels["publishFailures"].text() == "3"

    # --- Bemfa：65 黄档 + 明细（95s = 1m35s -> "1m35s"；Ping 1/3） ---
    assert groups["bemfa"]._score_label.text() == "65"
    assert SiColors.WARN_TEXT in _score_style(card, "bemfa"), \
        f"[{theme}] 65 分未用黄档: {_score_style(card, 'bemfa')}"
    assert groups["bemfa"]._value_labels["runtime"].text() == "1m35s"
    assert groups["bemfa"]._value_labels["pingLost"].text() == "1/3"
    assert groups["bemfa"]._value_labels["reconnectCount"].text() == "2"

    # --- 评分三档：逐值喂 BLE 组断言分档色（80 绿 / 79 黄 / 49 红） ---
    for score, color in _tiers():
        card.apply_quality({"ble": {**FULL_PAYLOAD["ble"], "score": score},
                            "mqtt": DISABLED_PAYLOAD["mqtt"],
                            "bemfa": {}})
        app.processEvents()
        style = _score_style(card, "ble")
        assert color in style, \
            f"[{theme}] score={score} 分档色错误: {style}（期望 {color}）"

    # --- 最后推送 >10s 黄色警示 ---
    card.apply_quality({"ble": STALE_PUSH_BLE})
    app.processEvents()
    assert groups["ble"]._value_labels["lastPush"].text() == "12s前"
    assert SiColors.WARN_TEXT in _value_style(card, "ble", "lastPush"), \
        f"[{theme}] 12s 前未黄色警示"

    # --- 重连等待期：「下次重连」行出现 + 黄色 + Math.round 取整 ---
    card.apply_quality({"ble": RECONNECTING_BLE})
    app.processEvents()
    assert groups["ble"]._row_frames["nextReconnect"].isVisible(), \
        "重连等待期应显示「下次重连」行"
    assert groups["ble"]._value_labels["nextReconnect"].text() == "8s后", \
        groups["ble"]._value_labels["nextReconnect"].text()
    assert SiColors.WARN_TEXT in _value_style(card, "ble", "nextReconnect")
    # uptime=0 -> 「未连接」；last_push 缺 -> 「无」
    assert groups["ble"]._value_labels["connectionDuration"].text() == "未连接"
    assert groups["ble"]._value_labels["lastPush"].text() == "无"

    print(f"1. 全量 payload 评分分档/明细文案/警示色/重连行 OK [{theme}]")


def test_disabled_and_tolerance(theme: str) -> None:
    """未启用灰字 + BLE 残缺容错 + 组间隔离。"""
    apply_theme(theme)
    card = ConnectionQualityCard()
    card.resize(360, 420)
    card.show()
    app.processEvents()
    groups = card._groups

    # --- MQTT 空对象 / Bemfa 缺失 -> 「未启用」灰字，评分/明细隐藏 ---
    card.apply_quality(DISABLED_PAYLOAD)
    app.processEvents()
    for group, rows in (("mqtt", ("runtime", "disconnects", "publishFailures")),
                        ("bemfa", ("runtime", "pingLost", "reconnectCount"))):
        assert groups[group]._disabled_label.text() == "未启用"
        assert groups[group]._disabled_label.isVisible(), \
            f"[{theme}] {group} 未启用时未显示灰字"
        assert not groups[group]._score_label.isVisible()
        assert not groups[group]._bar.isVisible()
        for key in rows:
            assert not groups[group]._row_frames[key].isVisible()
        assert SiColors.TEXT_MUTED in groups[group]._disabled_label.styleSheet()
    # BLE 正常渲染不受影响
    assert groups["ble"]._score_label.text() == "87"

    # --- BLE 残缺 payload：宽松缺省渲染，不崩 ---
    card.apply_quality({"ble": BROKEN_BLE})
    app.processEvents()
    assert groups["ble"]._score_label.text() == "0"
    assert groups["ble"]._value_labels["connectionDuration"].text() == "未连接"
    assert groups["ble"]._value_labels["lastPush"].text() == "无"
    assert groups["ble"]._value_labels["decryptSuccess"].text() == "0%"
    assert groups["ble"]._value_labels["reconnect5m"].text() == "0次"
    # score 脏值 -> 0 -> 红档
    assert SiColors.ERROR_TEXT in _score_style(card, "ble")

    # --- 非法输入：非 dict / None / 畸形顶层，静默忽略不崩 ---
    card.apply_quality(None)  # type: ignore[arg-type]
    card.apply_quality("quality")  # type: ignore[arg-type]
    card.apply_quality({"ble": 3, "mqtt": ["x"], "bemfa": True})
    app.processEvents()
    # 残值 payload 后再喂正常帧，完整恢复
    card.apply_quality(FULL_PAYLOAD)
    app.processEvents()
    assert groups["ble"]._score_label.text() == "87"
    assert groups["mqtt"]._score_label.text() == "92"
    assert groups["bemfa"]._score_label.text() == "65"

    print(f"2. 未启用灰字/BLE 残缺容错/非法输入隔离 OK [{theme}]")


def test_render_and_retheme(theme: str) -> None:
    """双主题 grab 非空白 + retheme 不崩 + 静态方法单测。"""
    apply_theme(theme)
    card = ConnectionQualityCard()
    card.resize(360, 420)
    card.show()
    card.apply_quality(FULL_PAYLOAD)
    app.processEvents()

    img = card.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 连接质量卡渲染空白"

    # retheme 不抛异常，数据保留
    card.retheme()
    app.processEvents()
    assert card._groups["ble"]._score_label.text() == "87"

    # formatDuration 照抄上游语义的边界（直接测渲染路径）
    card.apply_quality({"ble": {**FULL_PAYLOAD["ble"], "uptime": 0},
                        "mqtt": {"score": 51, "uptime": 59,
                                 "disconnects": 0, "publish_failures": 0},
                        "bemfa": {"score": 30, "uptime": 61,
                                  "ping_lost": 0, "reconnect_count": 0}})
    app.processEvents()
    assert card._groups["ble"]._value_labels["connectionDuration"].text() \
        == "未连接"
    assert card._groups["mqtt"]._value_labels["runtime"].text() == "59s"
    assert card._groups["bemfa"]._value_labels["runtime"].text() == "1m1s"
    # MQTT 51 黄档 / Bemfa 30 红档
    assert SiColors.WARN_TEXT in _score_style(card, "mqtt")
    assert SiColors.ERROR_TEXT in _score_style(card, "bemfa")

    card.hide()
    card.deleteLater()
    print(f"3. grab 非空白/retheme/formatDuration 边界 OK [{theme}]")


# --------------------------------------------------------------------------
# 主流程：暗/亮双主题各来一遍
# --------------------------------------------------------------------------

for theme in ("dark", "light"):
    test_full_payload(theme)
    test_disabled_and_tolerance(theme)
    test_render_and_retheme(theme)

si_theme.set_theme("dark")
print("CUKTECH QUALITY TEST ALL PASS")
