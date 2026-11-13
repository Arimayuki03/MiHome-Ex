# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""新版本提示对话框：版本对比 + GitHub Release 更新日志（Markdown 渲染）。

与关于页/设置页同一套扁平化观感：无边框圆角面板、遮罩、淡入淡出。
更新日志直接取 release 正文，用 QTextDocument 的 Markdown 渲染器展示，
标题/列表/粗体/行内代码/链接这些常用语法都能正常呈现。
"""

from PySide6.QtCore import QSize, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

import qtawesome as qta

from app import __version__
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import (
    PRIMARY_TEXT,
    SiColors,
    primary_button_qss,
    secondary_button_qss,
)

_PANEL_W = 480
_PANEL_H = 520


class UpdateDialog(OverlayDialog):
    """发现新版本：展示更新日志，一键前往下载。"""

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self._info = info
        self.setWindowTitle("发现新版本")

        panel = self._panel
        panel.setFixedSize(_PANEL_W, _PANEL_H)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(14)

        # ---- 标题栏：图标 + 品牌名 + 标题 ----
        head = QHBoxLayout()
        head.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(30, 30)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self._icon)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        # 品牌名放在标题左侧：只写「发现新版本」看不出是哪个软件在更新
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._brand = QLabel("米家 - Windows")
        self._brand.setFont(QFont("Microsoft YaHei UI", 13, QFont.Weight.DemiBold))
        title_row.addWidget(self._brand)
        self._title = QLabel("发现新版本")
        self._title.setFont(QFont("Microsoft YaHei UI", 13, QFont.Weight.DemiBold))
        title_row.addWidget(self._title)
        title_row.addStretch(1)
        title_col.addLayout(title_row)
        self._version_line = QLabel(
            f"当前 v{__version__}　→　最新 {info.tag}")
        title_col.addWidget(self._version_line)
        head.addLayout(title_col, stretch=1)
        lay.addLayout(head)

        # ---- 元信息：版本名 / 发布日期（两者都缺时整行收起）----
        self._meta = QLabel(self._meta_text())
        self._meta.setWordWrap(True)
        self._meta.setVisible(bool(self._meta.text()))
        lay.addWidget(self._meta)

        # ---- 更新日志：Markdown 渲染 ----
        self._notes = QTextBrowser()
        self._notes.setOpenExternalLinks(True)
        self._notes.setFrameShape(QFrame.Shape.NoFrame)
        self._notes.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._notes.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(self._notes, stretch=1)
        self._render_notes()

        # ---- 底部按钮 ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch(1)
        self._later_btn = QPushButton("稍后提醒")
        self._later_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._later_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._later_btn)
        self._download_btn = QPushButton(" 前往下载")
        self._download_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_btn.setIconSize(QSize(18, 18))
        self._download_btn.clicked.connect(self._open_download)
        btn_row.addWidget(self._download_btn)
        lay.addLayout(btn_row)

        self._apply_styles()

    # ---------- 内容 ----------

    def _meta_text(self) -> str:
        parts = []
        if self._info.name and self._info.name != self._info.tag:
            parts.append(self._info.name)
        date = str(self._info.published_at or "")[:10]
        if date:
            parts.append(f"发布于 {date}")
        return "　·　".join(parts)

    def _render_notes(self) -> None:
        """把 release 正文按 Markdown 渲染；无正文时给一句说明。"""
        body = (self._info.body or "").strip()
        if not body:
            body = "本次发布未提供更新说明，可前往 GitHub 查看完整发布页。"
        # 文档默认样式在 setMarkdown 时求值：先挂样式再渲染，
        # 直接 setMarkdown 比"先转 HTML 再 setHtml"少一层转换
        self._notes.document().setDefaultStyleSheet(self._notes_qss())
        self._notes.setMarkdown(body)

    def _notes_qss(self) -> str:
        return f"""
            body {{ color: {SiColors.TEXT_PRIMARY}; }}
            p, li {{ font-size: 10pt; }}
            h1 {{ font-size: 15pt; }}
            h2 {{ font-size: 13pt; }}
            h3 {{ font-size: 11pt; }}
            a {{ color: {SiColors.THEME}; text-decoration: none; }}
            /* 代码块用窗口底色：与卡片色差更大，深色主题下也能一眼区分 */
            code {{ background-color: {SiColors.WINDOW_BG}; font-size: 9pt; }}
            pre {{ background-color: {SiColors.WINDOW_BG}; font-size: 9pt; }}
        """

    def _open_download(self) -> None:
        QDesktopServices.openUrl(QUrl(self._info.url))
        self.accept()

    # ---------- 样式 ----------

    def _apply_styles(self) -> None:
        """主题相关内联样式：构造与 retheme 共用。"""
        self._icon.setPixmap(
            qta.icon("mdi.rocket-launch", color=SiColors.THEME).pixmap(26, 26))
        self._icon.setStyleSheet("background: transparent;")
        self._brand.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._version_line.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        self._meta.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;"
            " font-size: 9pt;")
        self._notes.setStyleSheet(
            f"QTextBrowser {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 10px;"
            f" padding: 10px 12px; color: {SiColors.TEXT_PRIMARY}; }}"
            "QScrollBar:vertical { background: transparent; width: 6px;"
            " margin: 0 2px 0 0; }"
            f"QScrollBar::handle:vertical {{ background: {SiColors.SCROLLBAR};"
            " border-radius: 3px; min-height: 30px; }"
            f"QScrollBar::handle:vertical:hover {{ background: {SiColors.SCROLLBAR_HOVER}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            " { background: none; }")
        self._later_btn.setStyleSheet(secondary_button_qss("8px 20px"))
        self._download_btn.setStyleSheet(primary_button_qss("8px 20px"))
        # 主按钮文字固定白色，图标也要同色；ON_THEME_TEXT 在浅色强调色下是深色
        self._download_btn.setIcon(
            qta.icon("mdi.download", color=PRIMARY_TEXT))

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()
        # 文档默认样式在 setMarkdown 时求值，主题切换要整篇重渲染
        self._render_notes()

    # ---------- 定位 ----------

    def resizeEvent(self, event) -> None:  # noqa: N802
        # 遮罩铺满与淡入由 OverlayDialog 处理，这里只负责面板居中；
        # 重复实现 showEvent 会让 _fade_in 跑两次，动画互相打架
        super().resizeEvent(event)
        x = (self.width() - self._panel.width()) // 2
        y = (self.height() - self._panel.height()) // 2
        self._panel.move(x, y)
