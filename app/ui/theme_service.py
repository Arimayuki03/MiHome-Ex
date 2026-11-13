# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""主题编排：设置解析（跟随系统/浅色/深色）、siui 色组与全局 QSS 刷新。

切换采用「重建式刷新」：全局 QSS 整块替换 + siui 全局色组切换 +
回调各界面重建（卡片网格/托盘行/工作台都是可重建结构）；即用即建
的对话框与 Toast 重开自然获得新主题。
"""

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication

from app.core import settings_store
from app.ui import si_theme

logger = logging.getLogger(__name__)

# 设置里的可选值
MODE_SYSTEM = "system"
MODE_LIGHT = "light"
MODE_DARK = "dark"


def effective_theme(mode: str) -> str:
    """把设置值解析为实际主题；跟随系统时读 Qt 色彩方案，未知回退深色。"""
    if mode in si_theme.PALETTES:
        return mode
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        from PySide6.QtCore import Qt
        if scheme == Qt.ColorScheme.Light:
            return "light"
    except Exception:
        pass
    return "dark"


def apply_theme(mode: str, accent: str | None = None) -> str:
    """应用主题设置并刷新全局样式，返回实际生效的主题名。

    `accent` 为 None 时读设置里的主题色（空串=默认米家绿）；设置页
    预览时会直接传待选的色值，避免"预览即落盘"。
    """
    theme = effective_theme(mode)
    si_theme.set_theme(theme)
    si_theme.set_accent(settings_store.get_theme_accent() if accent is None else accent)
    si_theme.sync_siui_colors()
    try:
        app = QGuiApplication.instance()
        if app is not None:
            # 调色板先于 QSS：Qt 自绘控件（下拉弹层、消息框按钮、选区、
            # 工具提示）取调色板，不设就会沿用 Windows 的深浅色而与本应用主题错位
            palette = si_theme.build_palette()
            app.setPalette(palette)
            app.setStyleSheet(si_theme.build_qss())
            # QToolTip 是独立顶层窗口，部分环境下不跟随 app 调色板：
            # 系统深色模式时会出现"黑底深字"几乎看不清，这里显式同步一份
            from PySide6.QtWidgets import QToolTip

            QToolTip.setPalette(palette)
    except Exception:
        logger.exception("应用全局 QSS 失败")
    return theme


class ThemeController(QObject):
    """持有当前设置模式；跟随系统时监听系统配色变化并广播切换。

    系统配色变化与应用主题是两件事：即使设置为固定深/浅色，
    托盘图标仍需跟随系统配色（任务栏底色由系统决定）。
    """

    theme_changed = Signal(str)  # 实际生效的主题名
    system_scheme_changed = Signal(str)  # 系统配色："dark" / "light"

    def __init__(self):
        super().__init__()
        self._mode = MODE_SYSTEM
        self._hints = QGuiApplication.styleHints()
        self._hints.colorSchemeChanged.connect(self._on_system_scheme_changed)

    def mode(self) -> str:
        return self._mode

    def system_scheme(self) -> str:
        """当前系统配色（"dark"/"light"），与设置里的模式无关。"""
        from PySide6.QtCore import Qt

        scheme = self._hints.colorScheme()
        return "light" if scheme == Qt.ColorScheme.Light else "dark"

    def set_mode(self, mode: str, accent: str | None = None) -> str:
        """切换设置模式（可选同时换主题色）并立即应用；返回实际生效主题。"""
        self._mode = mode
        theme = apply_theme(mode, accent)
        self.theme_changed.emit(theme)
        return theme

    def _on_system_scheme_changed(self, scheme) -> None:
        from PySide6.QtCore import Qt

        name = "light" if scheme == Qt.ColorScheme.Light else "dark"
        self.system_scheme_changed.emit(name)
        if self._mode != MODE_SYSTEM:
            return
        theme = apply_theme(MODE_SYSTEM)
        self.theme_changed.emit(theme)
        logger.info("系统配色变化，跟随切换为 %s", theme)
