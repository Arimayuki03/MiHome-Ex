# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""关于对话框：应用信息、上游依赖与开源许可。

版式按信息层级分区：标题区给出应用身份与版本，正文说明产品定位，
「上游依赖」「开源许可」各自收进独立卡片并整卡可点跳转，底部只留
「检查更新」与「GitHub」两个操作入口。
"""

from PySide6.QtCore import QSize, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

import qtawesome as qta

from app import __version__, resource_path
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import (
    PRIMARY_TEXT,
    SiColors,
    primary_button_qss,
    secondary_button_qss,
)
from app.ui.toast import Toast

GITHUB_URL = "https://github.com/Arimayuki03/MiHome-Ex"
MIJIA_API_URL = "https://github.com/Do1e/mijia-api"
LICENSE_URL = "https://github.com/huanyuejue/MiHome-Windows/blob/main/LICENSE"

# 与 pyproject.toml 的 project.license 保持一致
LICENSE_ID = "GPL-3.0-or-later"

_PANEL_W = 560
_APP_ICON_SIZE = 64
_BADGE_SIZE = 52


def _tint(color: str, alpha: float) -> str:
    """把 #RRGGBB 转成带透明度的 rgba()，用于图标徽章的淡色底。"""
    value = color.lstrip("#")
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha})"


class _LinkCard(QFrame):
    """整卡可点的信息卡片，右侧箭头提示可跳转。"""

    clicked = Signal()

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(name)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False

    def refresh_style(self) -> None:
        """悬停时边框换成主题色，明确提示整卡可点。"""
        border = SiColors.THEME if self._hovered else SiColors.LINE
        self.setStyleSheet(
            f"QFrame#{self.objectName()} {{ background: {SiColors.CARD};"
            f" border: 1px solid {border}; border-radius: 12px; }}")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.refresh_style()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class AboutDialog(OverlayDialog):
    """关于：遮罩 + 居中面板，与设置页同款观感。

    api_version 是上游 mijiaAPI 的实际安装版本（由适配层实时提供，
    见 MijiaService.api_version），与本地版本号同行展示便于排查兼容性。
    """

    def __init__(self, parent=None, api_version: str = ""):
        super().__init__(parent)
        self.setWindowTitle("关于")

        panel = self._panel
        panel.setFixedWidth(_PANEL_W)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(16)

        # ---- 标题区：应用图标 + 名称 + 版本 + 关闭 ----
        head = QHBoxLayout()
        head.setSpacing(16)

        self._app_icon = QLabel()
        self._app_icon.setFixedSize(_APP_ICON_SIZE, _APP_ICON_SIZE)
        self._app_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self._app_icon, 0, Qt.AlignmentFlag.AlignVCenter)

        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        self._title_label = QLabel("米家 - MiHome for Windows")
        title_col.addWidget(self._title_label)
        version_text = f"v{__version__}"
        if api_version:
            version_text += f"　·　mijiaAPI {api_version}"
        self._version_label = QLabel(version_text)
        title_col.addWidget(self._version_label)
        head.addLayout(title_col, stretch=1)
        head.addWidget(self._make_close_button(),
                       alignment=Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        # ---- 产品定位 ----
        self._intro_label = QLabel(
            "米家设备的 Windows 桌面控制端。扫码登录后在本地窗口查看与控制"
            "全部米家设备：设备卡片、详情工作台、系统托盘快捷控制、"
            "小爱语音指令与深浅色主题。")
        self._intro_label.setWordWrap(True)
        lay.addWidget(self._intro_label)

        # ---- 上游依赖卡片 ----
        self._dep_card = _LinkCard("depCard")
        self._dep_card.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(MIJIA_API_URL)))
        dep_lay = QHBoxLayout(self._dep_card)
        dep_lay.setContentsMargins(18, 16, 18, 16)
        dep_lay.setSpacing(16)
        self._dep_badge, self._dep_badge_icon = self._make_badge()
        dep_lay.addWidget(self._dep_badge, 0, Qt.AlignmentFlag.AlignTop)

        dep_text = QVBoxLayout()
        dep_text.setSpacing(8)
        self._dep_title = QLabel("上游依赖")
        dep_text.addWidget(self._dep_title)
        self._dep_label = QLabel(
            f'本软件是 <a href="{MIJIA_API_URL}">mijiaAPI</a> 的图形界面前端'
            "——米家 API 的 Python 封装。扫码登录、设备列表、属性读写与动作"
            "执行均由 mijiaAPI 完成，本项目仅负责界面与交互。")
        self._dep_label.setWordWrap(True)
        self._dep_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        self._dep_label.setOpenExternalLinks(True)
        dep_text.addWidget(self._dep_label)
        dep_lay.addLayout(dep_text, stretch=1)
        self._dep_arrow = QLabel()
        dep_lay.addWidget(self._dep_arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._dep_card)

        # ---- 开源许可卡片 ----
        self._lic_card = _LinkCard("licCard")
        self._lic_card.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(LICENSE_URL)))
        lic_lay = QHBoxLayout(self._lic_card)
        lic_lay.setContentsMargins(18, 16, 18, 16)
        lic_lay.setSpacing(16)
        self._lic_badge, self._lic_badge_icon = self._make_badge()
        lic_lay.addWidget(self._lic_badge, 0, Qt.AlignmentFlag.AlignTop)

        lic_text = QVBoxLayout()
        lic_text.setSpacing(8)
        lic_head = QHBoxLayout()
        lic_head.setSpacing(8)
        self._lic_title = QLabel("开源许可")
        lic_head.addWidget(self._lic_title)
        lic_head.addStretch(1)
        self._lic_badge_label = QLabel(LICENSE_ID)
        lic_head.addWidget(self._lic_badge_label)
        lic_text.addLayout(lic_head)
        self._lic_label = QLabel(
            "Copyright (C) 2026 MiHome-Windows contributors。本项目以 "
            f'<a href="{LICENSE_URL}">GNU General Public License v3.0</a>'
            " 或更高版本发布，可自由使用、修改与再分发；分发衍生作品时"
            "需保持同一许可并附上许可证全文。")
        self._lic_label.setWordWrap(True)
        self._lic_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        self._lic_label.setOpenExternalLinks(True)
        lic_text.addWidget(self._lic_label)
        lic_lay.addLayout(lic_text, stretch=1)
        self._lic_arrow = QLabel()
        lic_lay.addWidget(self._lic_arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._lic_card)

        lay.addStretch(1)

        # ---- 操作入口：右对齐，只留检查更新与仓库 ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch(1)
        self._update_btn = QPushButton(" 检测更新")
        self._update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_btn.setIconSize(QSize(16, 16))
        self._update_btn.clicked.connect(self._check_update)
        self._checking = False
        btn_row.addWidget(self._update_btn)
        self._github_btn = QPushButton(" GitHub")
        self._github_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._github_btn.setIconSize(QSize(16, 16))
        self._github_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))
        btn_row.addWidget(self._github_btn)
        lay.addLayout(btn_row)

        self._apply_styles()
        self._panel.adjustSize()

    # ---------- 布局辅助 ----------

    @staticmethod
    def _make_badge() -> tuple[QFrame, QLabel]:
        """卡片左侧的方形图标徽章：淡色底 + 居中图标。"""
        badge = QFrame()
        badge.setFixedSize(_BADGE_SIZE, _BADGE_SIZE)
        badge.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        badge_lay = QVBoxLayout(badge)
        badge_lay.setContentsMargins(0, 0, 0, 0)
        icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge_lay.addWidget(icon)
        return badge, icon

    # ---------- 交互 ----------

    def _check_update(self) -> None:
        # 请求在途时忽略重复点击，避免叠发多个网络请求
        if self._checking:
            return
        self._checking = True
        self._update_btn.setEnabled(False)
        self._update_btn.setText(" 检查中…")

        def _restore() -> None:
            self._checking = False
            if self._update_btn is not None:
                self._update_btn.setEnabled(True)
                self._update_btn.setText(" 检测更新")
                self._update_btn.setIcon(
                    qta.icon("mdi.update", color=PRIMARY_TEXT))

        def _finish(info, error) -> None:
            checker.deleteLater()
            _restore()
            if error is not None:
                Toast.info(self, f"检查更新失败：{error}", 4000)
                return
            from app import __version__
            from app.core.update_checker import is_newer
            if info is None or not is_newer(info.tag, __version__):
                Toast.info(self, "当前已是最新版本", 2500)
                return
            from app.ui.update_flow import prompt_new_version
            prompt_new_version(self, info)

        from app.core.update_checker import UpdateChecker
        checker = UpdateChecker(self)
        checker.check_finished.connect(_finish)
        checker.check()

    # ---------- 样式 ----------

    def _apply_styles(self) -> None:
        """主题相关内联样式：构造与 retheme 共用。"""
        pixmap = QPixmap(str(resource_path("app/ui/icon.png")))
        if not pixmap.isNull():
            self._app_icon.setPixmap(pixmap.scaled(
                _APP_ICON_SIZE, _APP_ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        self._app_icon.setStyleSheet("background: transparent;")

        # 字号写进样式表：全局 QSS 的 * 规则会覆盖 setFont 的设置
        self._title_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 16pt; font-weight: 600;")
        self._version_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9.5pt;")
        self._intro_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 10pt;")

        for card in (self._dep_card, self._lic_card):
            card.refresh_style()

        badge_bg = _tint(SiColors.THEME, 0.16)
        self._dep_badge.setStyleSheet(
            f"QFrame {{ background: {badge_bg}; border: none;"
            " border-radius: 12px; }")
        self._dep_badge_icon.setPixmap(
            qta.icon("mdi.code-tags", color=SiColors.THEME).pixmap(26, 26))
        self._dep_badge_icon.setStyleSheet("background: transparent;")

        self._dep_title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 11pt; font-weight: 600;")
        self._dep_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9.5pt;")

        lic_bg = _tint("#7c5cd6", 0.18)
        self._lic_badge.setStyleSheet(
            f"QFrame {{ background: {lic_bg}; border: none;"
            " border-radius: 12px; }")
        self._lic_badge_icon.setPixmap(
            qta.icon("mdi.license", color="#9b7fe8").pixmap(26, 26))
        self._lic_badge_icon.setStyleSheet("background: transparent;")

        self._lic_title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 11pt; font-weight: 600;")
        self._lic_badge_label.setStyleSheet(
            f"color: {SiColors.THEME}; background: {SiColors.SURFACE};"
            " border-radius: 8px; padding: 2px 10px; font-size: 8.5pt;")
        self._lic_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9.5pt;")

        for arrow in (self._dep_arrow, self._lic_arrow):
            arrow.setPixmap(
                qta.icon("mdi.chevron-right", color=SiColors.TEXT_MUTED).pixmap(20, 20))
            arrow.setStyleSheet("background: transparent;")

        self._update_btn.setStyleSheet(primary_button_qss("8px 20px"))
        self._update_btn.setIcon(qta.icon("mdi.update", color=PRIMARY_TEXT))
        self._github_btn.setStyleSheet(secondary_button_qss("8px 20px"))
        self._github_btn.setIcon(
            qta.icon("mdi.github", color=SiColors.TEXT_PRIMARY))

    def retheme(self) -> None:
        """主题切换：重设面板底色与全部内联样式。"""
        super().retheme()
        self._apply_styles()

    # ---------- 定位 ----------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        x = (self.width() - self._panel.width()) // 2
        y = (self.height() - self._panel.height()) // 2
        self._panel.move(x, y)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        # 无可见父窗口（托盘路径）：铺满可用屏幕
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())
        self.raise_()
        self._fade_in()
