# SPDX-License-Identifier: GPL-3.0-or-later
"""CUKTECH 充电限额堆叠卡 + 延时关闭快捷卡自测：离屏运行，零网络。

用法: .venv\\Scripts\\python.exe tests/cuktech_limits_test.py

仿 tests/cuktech_panel_test.py 的离屏 + grab() 模式：直接实例化两个
组件、灌假 limits/settings 数据，断言状态文案三态（含 fired）、进度
百分比、快捷档/设置/关闭发出的信号参数、空白输入拦截、输入焦点保护、
commit_result 失败恢复、pending 防重复；暗/亮双主题各来一遍。组件
只发信号不发请求，无需假门面。
"""

import atexit
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体（Qt 6 不再内置 fonts 目录）。首次 qta.icon()
# 调用经 QFontDatabase.addApplicationFont 注册图标字体后，offscreen 的
# 字体回退引擎把所有字体族（含 "Microsoft YaHei UI"）都解析到唯一存在
# 的图标字体——它没有 CJK 字形，drawText 画中文即零像素。指向系统字体
# 目录让字体数据库有真实字体可回退，离屏渲染与实际 GUI 一致。
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_limits_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.ui import si_theme
from app.ui.theme_service import apply_theme

from app.ui.cuktech_limits import ChargeLimitStack, DelayOffQuickCard

# --------------------------------------------------------------------------
# 假数据：结构与门面 cuktech_charge_limits / status["settings"] 一致
# --------------------------------------------------------------------------

FAKE_LIMITS = {
    "ok": True,
    "limits": {
        1: {"wh": 65.0, "mode": "once", "fired": False,
            "session_wh": 12.3, "is_charging": True},
        2: {"wh": 0.0, "mode": "once", "fired": False,
            "session_wh": 0.0, "is_charging": False},
        3: {"wh": 60.0, "mode": "always", "fired": True,
            "session_wh": 60.0, "is_charging": False},
        4: {"wh": 40.0, "mode": "once", "fired": False,
            "session_wh": 10.0, "is_charging": True},
    },
}

FAKE_SETTINGS = {
    "5": 1, "8": 0,
    "9": 30,    # C1 已设 30 分
    "10": 0,    # C2 未设
    "11": 90,   # C3 已设 90 分
    # 12 缺失：USB-A 未上报 -> 显示「—」
}


# --------------------------------------------------------------------------
# 1. ChargeLimitStack
# --------------------------------------------------------------------------


class SignalSpy:
    """记录信号发射参数的简单探针（calls 为参数元组列表）。

    绑定到实例的信号上（未实例化的 SignalDescriptor 无 connect）。
    """

    def __init__(self, owner, signal_name: str):
        self.calls: list[tuple] = []
        getattr(owner, signal_name).connect(lambda *a: self.calls.append(a))

    def clear(self) -> None:
        self.calls.clear()

    @property
    def last(self) -> tuple | None:
        return self.calls[-1] if self.calls else None


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


# --------------------------------------------------------------------------
# 1. ChargeLimitStack
# --------------------------------------------------------------------------


def test_limit_stack(theme: str) -> None:
    apply_theme(theme)
    stack = ChargeLimitStack()
    stack.resize(560, 420)
    stack.show()
    app.processEvents()
    limit_commits = SignalSpy(stack, "limit_commit")
    limit_invalid = SignalSpy(stack, "invalid_input")

    rows = stack._rows

    # --- 数据渲染：状态文案三态（含 fired 警示） ---
    stack.update_limits(FAKE_LIMITS)
    app.processEvents()
    assert rows[1]["status"].text() == "仅一次", rows[1]["status"].text()
    assert rows[2]["status"].text() == "未启用"
    assert rows[3]["status"].text() == "长期有效 · 已触发", \
        rows[3]["status"].text()
    assert rows[4]["status"].text() == "仅一次"

    # fired 行样式含警示色（当前主题的 WARN_TEXT）
    assert rows[3]["status"].styleSheet().find(si_theme.SiColors.WARN_TEXT) >= 0, \
        f"[{theme}] fired 状态未用警示色: {rows[3]['status'].styleSheet()}"

    # 裸 limits dict（无外层 ok/limits 包装）也接受
    bare = dict(FAKE_LIMITS["limits"])
    stack.update_limits(dict(bare))
    app.processEvents()
    assert rows[3]["status"].text() == "长期有效 · 已触发"
    stack.update_limits(FAKE_LIMITS)

    # --- 进度条 pct 计算：session/wh 钳到 1；wh=0 时隐藏 ---
    assert rows[1]["bar"]._ratio == 12.3 / 65.0, rows[1]["bar"]._ratio
    assert rows[3]["bar"]._ratio == 1.0, rows[3]["bar"]._ratio  # 60/60 钳满
    assert rows[4]["bar"]._ratio == 10.0 / 40.0
    assert rows[2]["bar"].isVisible() is False, "wh=0 不应显示进度条"
    assert rows[2]["progress"].isVisible() is False
    assert rows[1]["progress"].text() == "已充 12.3 / 65 Wh", \
        rows[1]["progress"].text()

    # --- 快捷档：点击 = 填入输入框 + 立即提交 limit_commit(port, wh, mode) ---
    limit_commits.clear()
    rows[1]["quick"][0].click()          # C1 快捷档 5Wh，模式 once
    assert limit_commits.last == (1, 5.0, "once"), limit_commits.calls
    assert rows[1]["edit"].text() == "5", rows[1]["edit"].text()
    # 模式切到 always 再点快捷档：mode 随下拉
    rows[1]["combo"].setCurrentIndex(1)  # 用户改动 -> mode_touched
    rows[1]["quick"][1].click()          # 10Wh
    assert limit_commits.last == (1, 10.0, "always"), limit_commits.calls
    limit_commits.clear()

    # --- 设置按钮：用输入框值 + 当前下拉 mode ---
    rows[2]["edit"].setText("80")
    rows[2]["set_btn"].click()
    assert limit_commits.last == (2, 80.0, "once"), limit_commits.calls
    rows[2]["combo"].setCurrentIndex(1)
    rows[2]["set_btn"].click()
    assert limit_commits.last == (2, 80.0, "always"), limit_commits.calls
    # 回车提交与设置按钮同路径
    rows[2]["edit"].setText("66.5")
    rows[2]["edit"].returnPressed.emit()
    assert limit_commits.last == (2, 66.5, "always"), limit_commits.calls
    limit_commits.clear()

    # --- 空白/NaN/负数/越界：拦在本地，不发信号 ---
    for bad in ("", "   ", "abc", "-3", "1500"):
        rows[2]["edit"].setText(bad)
        rows[2]["set_btn"].click()
    assert limit_commits.calls == [], f"非法输入未被拦截: {limit_commits.calls}"
    assert len(limit_invalid.calls) == 5, limit_invalid.calls
    limit_invalid.clear()

    # --- 关闭按钮：提交 wh:0 ---
    rows[4]["edit"].setText("25")
    rows[4]["off_btn"].click()
    assert limit_commits.last == (4, 0.0, "once"), limit_commits.calls
    assert rows[4]["edit"].text() == "", "关闭应清空输入框"
    limit_commits.clear()

    # --- 焦点保护：正在编辑的输入框不被回填 ---
    stack.update_limits(FAKE_LIMITS)     # 先恢复同步态（内容与设备一致）
    rows[2]["edit"].setText("99")        # setText 不触发 textEdited，
    rows[2]["edit"].setFocus()           # 焦点保护走 hasFocus 分支
    app.processEvents()
    stack.update_limits(FAKE_LIMITS)
    app.processEvents()
    assert rows[2]["edit"].text() == "99", \
        f"[{theme}] 焦点输入框被轮询覆盖: {rows[2]['edit'].text()}"
    # touched 保护（textEdited 路径）：失焦后用户改过仍未提交的值不覆盖
    rows[2]["edit"].clearFocus()
    rows[2]["edit"].setText("77")        # 模拟用户键入：文本变化 + textEdited
    rows[2]["edit"].textEdited.emit("77")
    stack.update_limits(FAKE_LIMITS)
    app.processEvents()
    assert rows[2]["edit"].text() == "77", \
        f"[{theme}] touched 输入框被轮询覆盖: {rows[2]['edit'].text()}"
    # 内容与设备值一致时自动恢复同步
    rows[2]["edit"].setText("0")         # 设备 wh=0 -> 同步文本为空
    stack.update_limits(FAKE_LIMITS)
    app.processEvents()
    assert rows[2]["edit"].text() == "", "内容与设备一致应恢复同步"
    # 模式下拉 touched 保护：用户改动后回填不覆盖
    rows[3]["combo"].setCurrentIndex(0)  # 设备 always -> 用户改 once
    stack.update_limits(FAKE_LIMITS)
    app.processEvents()
    assert rows[3]["combo"].currentIndex() == 0, "touched 下拉被覆盖"
    rows[3]["combo"].setCurrentIndex(1)  # 与设备一致 -> 恢复同步
    stack.update_limits(FAKE_LIMITS)
    app.processEvents()
    assert rows[3]["combo"].currentIndex() == 1

    # --- commit_result：成功恢复同步；失败恢复原值 ---
    rows[1]["edit"].setText("12")
    rows[1]["edit"].textEdited.emit("12")
    stack.update_limits(FAKE_LIMITS)
    assert rows[1]["edit"].text() == "12"
    stack.commit_result(1, True)
    stack.update_limits(FAKE_LIMITS)
    assert rows[1]["edit"].text() == "65", \
        f"[{theme}] 提交成功后未恢复自动同步: {rows[1]['edit'].text()}"
    # 失败：输入框/下拉回到最近一帧设备值
    rows[1]["edit"].setText("88")
    rows[1]["edit"].textEdited.emit("88")
    rows[1]["combo"].setCurrentIndex(1)  # 用户改 always（设备 once）
    stack.commit_result(1, False)
    app.processEvents()
    assert rows[1]["edit"].text() == "65", \
        f"[{theme}] 失败未恢复原值: {rows[1]['edit'].text()}"
    assert rows[1]["combo"].currentIndex() == 0, "失败后模式未恢复"
    assert rows[1]["edit_touched"] is False and rows[1]["mode_touched"] is False

    # --- 双主题渲染非空白 ---
    app.processEvents()
    img = stack.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 限额卡组渲染空白"

    stack.hide()
    stack.deleteLater()
    print(f"1. ChargeLimitStack 渲染三态/进度/快捷档/校验/焦点保护/失败恢复 OK [{theme}]")


# --------------------------------------------------------------------------
# 2. DelayOffQuickCard
# --------------------------------------------------------------------------


def test_delay_card(theme: str) -> None:
    apply_theme(theme)
    card = DelayOffQuickCard()
    card.resize(560, 220)
    card.show()
    app.processEvents()
    delay_commits = SignalSpy(card, "delay_commit")

    rows = card._rows

    # --- 数据渲染：当前值（PIID 9/10/11/12） ---
    card.update_settings(FAKE_SETTINGS)
    app.processEvents()
    assert rows[1]["value"].text() == "已设 30 分", rows[1]["value"].text()
    assert rows[2]["value"].text() == "未设"
    assert rows[3]["value"].text() == "已设 90 分"
    assert rows[4]["value"].text() == "—", rows[4]["value"].text()
    # 含 settings 键的全量 dict 也能解包
    card.update_settings({"settings": FAKE_SETTINGS})
    assert rows[1]["value"].text() == "已设 30 分"

    # --- 快捷档：提交 delay_commit(port, minutes) + 乐观「设置中…」 ---
    delay_commits.clear()
    rows[2]["buttons"][1].click()        # C2 快捷档 30 分
    assert delay_commits.last == (2, 30), delay_commits.calls
    assert rows[2]["value"].text() == "设置中…", rows[2]["value"].text()
    assert all(not b.isEnabled() for b in rows[2]["buttons"]), \
        "pending 行按钮应禁用"
    # pending 期间再点无效（防重复提交）
    rows[2]["buttons"][2].click()
    assert delay_commits.calls == [(2, 30)], delay_commits.calls
    # 旧帧（值未变）不覆盖「设置中…」
    card.update_settings(FAKE_SETTINGS)
    assert rows[2]["value"].text() == "设置中…"
    # 设备上报确认（PIID 10=30）-> 解除 pending
    card.update_settings({**FAKE_SETTINGS, "10": 30})
    assert rows[2]["value"].text() == "已设 30 分"
    assert all(b.isEnabled() for b in rows[2]["buttons"]), \
        "确认后按钮应恢复"
    assert 2 not in card._pending

    # --- 清除按钮：提交 (port, 0)，成功按乐观值落定「未设」 ---
    delay_commits.clear()
    rows[1]["buttons"][-1].click()       # 清除 C1
    assert delay_commits.last == (1, 0), delay_commits.calls
    assert rows[1]["value"].text() == "设置中…"
    card.commit_result(1, True)          # 面板回喂成功
    assert rows[1]["value"].text() == "未设", rows[1]["value"].text()
    assert rows[1]["buttons"][-1].isEnabled()

    # --- commit_result 失败：恢复上一帧设备值 ---
    rows[3]["buttons"][0].click()        # C3 快捷档 15 分（设备当前 90）
    assert delay_commits.last == (3, 15)
    card.commit_result(3, False)
    assert rows[3]["value"].text() == "已设 90 分", \
        f"[{theme}] 失败未恢复原值: {rows[3]['value'].text()}"
    assert all(b.isEnabled() for b in rows[3]["buttons"])

    # --- pending 超时兜底：不回喂也能解除（回显上一帧设备值） ---
    rows[4]["buttons"][0].click()        # USB-A 15 分（设备未上报）
    assert delay_commits.last == (4, 15)
    timer = rows[4]["timer"]
    assert timer is not None and timer.isActive()
    timer.stop()
    rows[4]["timer"] = None              # 模拟超时到期（不经真实等待）
    card._on_pending_timeout(4)
    assert rows[4]["value"].text() == "—", rows[4]["value"].text()
    assert all(b.isEnabled() for b in rows[4]["buttons"])
    delay_commits.clear()

    # --- 双主题渲染非空白 ---
    app.processEvents()
    img = card.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 延时快捷卡渲染空白"

    # --- retheme 不抛异常 ---
    card.retheme()
    app.processEvents()
    card.hide()
    card.deleteLater()
    print(f"2. DelayOffQuickCard 当前值/快捷档/清除/pending 防重复/失败恢复 OK [{theme}]")


# --------------------------------------------------------------------------
# 主流程：暗/亮双主题各来一遍
# --------------------------------------------------------------------------

for theme in ("dark", "light"):
    test_limit_stack(theme)
    test_delay_card(theme)

si_theme.set_theme("dark")
print("CUKTECH LIMITS TEST ALL PASS")
