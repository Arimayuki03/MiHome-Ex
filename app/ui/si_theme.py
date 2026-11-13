# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""主题中枢：深/浅双调色板、动态 SiColors 代理、全局 QSS 生成。

视觉语言：深黑底立体卡片 + 米家青绿主题色。主题色（THEME 系列）
与绿底上的文字在两种主题下完全一致；其余语义色按当前调色板取值。

用法约定：
- 界面代码一律 `from app.ui.si_theme import SiColors` 后取
  `SiColors.CARD` 等语义色——SiColors 经元类动态代理当前调色板，
  主题切换后新构建的控件自动取到新值；
- 内联样式是构造时求值的 f-string，已显示的界面在切换主题后由
  调用方重建（卡片网格/托盘行/工作台均为可重建结构）；
- 全局 QSS 由 build_qss() 生成，切换时整块替换。
"""

from string import Template
from typing import TYPE_CHECKING

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLineEdit, QSizePolicy

from app.siui.components.button import SiSwitchRefactor, SiToggleButtonRefactor

if TYPE_CHECKING:
    from PySide6.QtWidgets import QComboBox

    from app.siui.components.label import SiLabelRefactor

# ----------------------------------------------------------------------------
# 主题色：默认米家青绿，可在 设置 → 主题界面 → 主题色 里换成任意颜色
# ----------------------------------------------------------------------------
DEFAULT_ACCENT = "#3dbba4"
SWITCH_THUMB = "#1C191F"       # 深色滑块/暗扣（历史保留；开关拇指已统一白色）
SWITCH_THUMB_LIGHT = "#ffffff"  # 开关拇指：深浅主题统一用白
DANGER = "#c0392b"             # 危险红（关闭按钮 hover 等）
DANGER_TEXT = "#e57373"
WHITE = "#ffffff"

# 由当前主题色派生的一组语义色，set_accent() 重算。
# SiColors.THEME 等经元类从这里取值，所以换色后新建控件自动生效。
_ACCENT: dict[str, str] = {}
_accent_hex: str = DEFAULT_ACCENT


def _luminance(color: QColor) -> float:
    """相对亮度（WCAG 口径），用来决定主题色上该配深字还是白字。"""
    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return (0.2126 * channel(color.red()) + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue()))


def _mix_hex(base: QColor, other: QColor, t: float) -> str:
    """按比例把 base 往 other 混，返回 #rrggbb。"""
    t = min(max(t, 0.0), 1.0)
    return QColor(
        int(round(base.red() + (other.red() - base.red()) * t)),
        int(round(base.green() + (other.green() - base.green()) * t)),
        int(round(base.blue() + (other.blue() - base.blue()) * t)),
    ).name()


def _derive_accent(hex_color: str) -> dict[str, str]:
    """由主题色派生 hover / dim / 选中悬停 / 其上的文字色。

    派生规则只有一句：**hover 提亮、dim 压暗、选中悬停介于两者之间**，
    深浅主题共用同一套（主题色本身不随明暗变化，与原来的米家绿一致）。

    其上文字色的阈值取 0.36：米家绿(0.39)/黄(0.51) 这类偏亮的强调色配深字，
    蓝(0.33)/粉(0.33)/藏青 这类配白字。之前阈值 0.18 把中蓝也判成"亮色"，
    结果蓝底按钮上是近黑字（用户截图反馈"看不清、像没跟随主题色"）。
    """
    base = QColor(hex_color)
    white, black = QColor(255, 255, 255), QColor(0, 0, 0)
    on_text = "#0b0b0e" if _luminance(base) >= 0.36 else "#ffffff"
    return {
        "THEME": base.name(),
        "THEME_HOVER": _mix_hex(base, white, 0.18),
        "THEME_DIM": _mix_hex(base, black, 0.26),
        "THEME_CHECKED_HOVER": _mix_hex(base, white, 0.12),
        "ON_THEME_TEXT": on_text,
    }


def set_accent(hex_color: str | None) -> None:
    """设定主题色（空/非法值回落到默认米家绿）；只改取色，不刷新界面。"""
    global _accent_hex, _ACCENT
    text = (hex_color or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        text = DEFAULT_ACCENT
    _accent_hex = text.lower()
    _ACCENT = _derive_accent(_accent_hex)


def accent() -> str:
    """当前主题色（#rrggbb）。"""
    return _accent_hex


def on_accent_text(alpha: float = 1.0) -> str:
    """强调色实心控件上的文字色；alpha<1 用于说明文案（比正文弱一档）。"""
    color = QColor(_ACCENT["ON_THEME_TEXT"])
    if alpha >= 1.0:
        return color.name()
    return (f"rgba({color.red()}, {color.green()}, {color.blue()},"
            f" {int(round(min(max(alpha, 0.0), 1.0) * 255))})")


def on_accent_overlay(alpha: float = 0.18) -> str:
    """强调色实心区域上的半透明浮层（小按钮底），自动与文字色反色。"""
    text_is_white = _ACCENT["ON_THEME_TEXT"].lower() == "#ffffff"
    base = QColor(0, 0, 0) if text_is_white else QColor(255, 255, 255)
    return (f"rgba({base.red()}, {base.green()}, {base.blue()},"
            f" {int(round(min(max(alpha, 0.0), 1.0) * 255))})")


# 主操作按钮（保存/添加/完成）的文字固定白色：这类按钮文字是"内容"，
# 不跟着主题明暗翻转，蓝/绿/粉底上都一样。极亮的强调色（黄）白字会糊，
# 此时把底色压暗一档（见 primary_bg），文字仍然是白。
PRIMARY_TEXT = "#ffffff"


def primary_bg() -> str:
    """主操作按钮底色：强调色太亮（黄/浅绿）时压暗一档，保证白字可读。"""
    if _luminance(QColor(_ACCENT["THEME"])) >= 0.5:
        return _ACCENT["THEME_DIM"]
    return _ACCENT["THEME"]


def primary_hover() -> str:
    """主按钮 hover 底色：与 primary_bg 保持同一明暗档。

    之前固定用 THEME_HOVER：黄色强调色下底色被压暗成深黄，
    悬停却变回亮黄，白字瞬间看不清——hover 必须跟着底色走。
    """
    bg = primary_bg()
    if bg == _ACCENT["THEME"]:
        return _ACCENT["THEME_HOVER"]
    return _mix_hex(QColor(bg), QColor(255, 255, 255), 0.12)


def primary_button_qss(padding: str = "6px 16px", radius: int = 8,
                       font_size: str | None = None) -> str:
    """实心主按钮（保存/添加/完成）：强调色底 + 固定白字。"""
    size = f" font-size: {font_size};" if font_size else ""
    return (
        f"QPushButton {{ background: {primary_bg()}; border: none;"
        f" border-radius: {radius}px; padding: {padding};"
        f" color: {PRIMARY_TEXT}; font-weight: 600;{size} }}"
        f"QPushButton:hover {{ background: {primary_hover()}; }}"
        f"QPushButton:disabled {{ background: {SiColors.SURFACE};"
        f" color: {SiColors.TEXT_DISABLED}; }}")


def secondary_button_qss(padding: str = "6px 16px", radius: int = 8,
                         font_size: str | None = None) -> str:
    """次级按钮（取消/刷新/编辑）：控件底色 + 描边，浅色模式下也有边界。"""
    size = f" font-size: {font_size};" if font_size else ""
    return (
        f"QPushButton {{ background: {SiColors.CTRL_BG};"
        f" border: 1px solid {SiColors.CTRL_BORDER};"
        f" border-radius: {radius}px; padding: {padding};"
        f" color: {SiColors.TEXT_PRIMARY};{size} }}"
        f"QPushButton:hover {{ background: {SiColors.CTRL_BG_HOVER}; }}"
        f"QPushButton:disabled {{ color: {SiColors.TEXT_DISABLED}; }}")


def check_toggle_qss(diameter: int = 28, font_size: str | None = None) -> str:
    """圆形勾选按钮（设备/场景行的 ✓ / +）：选中态跟随主题色。"""
    size = f" font-size: {font_size};" if font_size else ""
    return (
        f"QPushButton {{ background: {SiColors.CTRL_BG};"
        f" border: 1px solid {SiColors.CTRL_BORDER};"
        f" border-radius: {diameter // 2}px;"
        f" color: {SiColors.TEXT_PRIMARY};{size} }}"
        f"QPushButton:checked {{ background: {SiColors.THEME};"
        f" border-color: {SiColors.THEME}; color: {SiColors.ON_THEME_TEXT}; }}"
        f"QPushButton:hover {{ background: {SiColors.CTRL_BG_HOVER}; }}"
        f"QPushButton:checked:hover {{ background: {SiColors.THEME_CHECKED_HOVER};"
        f" border-color: {SiColors.THEME_CHECKED_HOVER}; }}")


set_accent(None)


# ----------------------------------------------------------------------------
# 深/浅调色板：语义键 -> 色值。新增颜色一律进这里，不要在界面代码写死
# ----------------------------------------------------------------------------
PALETTES: dict[str, dict[str, str]] = {
    "dark": dict(
        WINDOW_BG="#1e1f24",
        TITLE_BAR_BG="#1a1b20",
        TITLE_BAR_BORDER="#252630",
        CARD="#2a2c32",
        CARD_HOVER="#333540",
        CARD_BORDER_HOVER="#404252",
        SURFACE="#2e2f36",          # 输入框/小按钮/Toast 底
        SURFACE_PRESSED="#1f1f23",  # 电源钮忙碌底
        # 次级控件（刷新/编辑/关闭这类小按钮、页签底）的底色与描边。
        # 浅色模式必须靠白底 + 描边把自己从灰面板上"抬起来"，
        # 只靠 SURFACE(#eef0f3) 压在 WINDOW_BG(#f2f3f5) 上几乎看不见。
        CTRL_BG="#2e2f36",
        CTRL_BG_HOVER="#3a3b44",
        CTRL_BORDER="#2e2f36",      # 深色下与底色同值 = 视觉无描边，保持原观感
        PRESSED="#17171a",          # 按下态
        BTN_PRESSED="#25262e",      # 次级按钮按下
        BTN_HOVER="#3a3b44",
        LINE="#35363f",
        SCROLLBAR="#35363f",
        SCROLLBAR_HOVER="#4a4b55",
        TEXT_PRIMARY="#e8e8ea",
        TEXT_SECONDARY="#8a8b93",
        TEXT_MUTED="#7c7d83",
        TEXT_SUBTLE="#9a9aa0",
        TEXT_DISABLED="#555558",
        TEXT_FAINT="#444448",
        OFFLINE_CARD="#1a1b20",
        OFFLINE_TEXT="#6e6e78",
        OFFLINE_SUB="#5a5b64",
        ICON_DIM="#7a7b83",
        ICON_MUTED="#a0a0aa",
        HOME_HOVER="#b9babf",
        HOME_PRESSED="#85868b",
        THUMB="#ffffff",               # 滑块拇指：深色模式白
        STATE_OFF="#40434f",
        STATE_UNKNOWN_BG="#222227",
        STATE_UNKNOWN_BORDER="#3a3a40",
        STATE_UNKNOWN_HOVER="#2a2a30",
        ERROR_TEXT="#e06060",
        DEL_TEXT="#9a6a6a",
        DEL_BORDER="#3a2f2f",
        DEL_BORDER_HOVER="#6b2f2f",
        WARN_BG="#3a2d12",
        WARN_BORDER="#5a4220",
        WARN_TEXT="#e8c87a",
    ),
    "light": dict(
        WINDOW_BG="#f2f3f5",
        TITLE_BAR_BG="#ececef",
        TITLE_BAR_BORDER="#dcdfe4",
        CARD="#ffffff",
        CARD_HOVER="#f4f5f7",
        CARD_BORDER_HOVER="#d0d3da",
        SURFACE="#eef0f3",
        SURFACE_PRESSED="#e4e6ea",
        # 次级控件：浅色下用白底 + 淡描边，避免"白字白底/灰字灰底"看不清
        CTRL_BG="#ffffff",
        CTRL_BG_HOVER="#f1f3f7",
        CTRL_BORDER="#d5d8df",
        PRESSED="#d9dade",
        BTN_PRESSED="#dfe1e6",
        BTN_HOVER="#e9eaee",
        LINE="#e3e4e8",
        SCROLLBAR="#c9cbd2",
        SCROLLBAR_HOVER="#b0b3bc",
        TEXT_PRIMARY="#17181c",
        TEXT_SECONDARY="#6b6d76",
        TEXT_MUTED="#85878e",
        TEXT_SUBTLE="#8a8b93",
        TEXT_DISABLED="#b4b6bc",
        TEXT_FAINT="#c6c8ce",
        OFFLINE_CARD="#e4e6eb",
        OFFLINE_TEXT="#8f929b",
        OFFLINE_SUB="#a8abb3",
        ICON_DIM="#85878e",
        ICON_MUTED="#9a9aa0",
        HOME_HOVER="#55565c",
        HOME_PRESSED="#75767c",
        THUMB="#ffffff",               # 滑块拇指：两套主题都白（胶囊观感统一）
        STATE_OFF="#c4c8d0",
        STATE_UNKNOWN_BG="#ececef",
        STATE_UNKNOWN_BORDER="#c9cbd2",
        STATE_UNKNOWN_HOVER="#e2e3e8",
        ERROR_TEXT="#c62828",
        DEL_TEXT="#b05a5a",
        DEL_BORDER="#e8c5c5",
        DEL_BORDER_HOVER="#e09b9b",
        WARN_BG="#fdf3d8",
        WARN_BORDER="#e8d49a",
        WARN_TEXT="#8a6d1f",
    ),
}

# 当前主题；启动时由 theme_service.set_theme 写定
_current: str = "dark"


def current_theme() -> str:
    """当前主题名："dark" | "light"。"""
    return _current


def is_dark() -> bool:
    return _current == "dark"


def set_theme(theme: str) -> None:
    """切换当前调色板（仅改取色；全局刷新由 theme_service 编排）。"""
    global _current
    if theme not in PALETTES:
        raise ValueError(f"未知主题: {theme}")
    _current = theme


def palette() -> dict[str, str]:
    return PALETTES[_current]


class _PaletteMeta(type):
    """让 SiColors.XXX 动态代理当前调色板；主题色由 set_accent 派生。"""

    def __getattr__(cls, name: str) -> str:
        if name in _ACCENT:
            return _ACCENT[name]
        try:
            return PALETTES[_current][name]
        except KeyError:
            raise AttributeError(
                f"SiColors 无此语义色: {name}，可用键见 PALETTES") from None


class SiColors(metaclass=_PaletteMeta):
    """语义色代理。THEME 系列随 set_accent 变化；固定色是硬编码真属性。"""

    SWITCH_THUMB = SWITCH_THUMB
    DANGER = DANGER
    DANGER_TEXT = DANGER_TEXT
    WHITE = WHITE


def build_palette():
    """按当前主题生成 QPalette。

    全局 QSS 只覆盖它列到的控件；下拉弹层、消息框按钮、工具提示、
    QLineEdit 选区这些"Qt 自己的绘制"用的是调色板。不设调色板时它们
    会沿用 Windows 的深浅色：应用切浅色而系统是深色时，就会出现
    "白底白字/灰字灰底"——用户反馈的保存按钮文字看不清正是这一类。
    """
    from PySide6.QtGui import QPalette

    p = QPalette()
    window = QColor(PALETTES[_current]["WINDOW_BG"])
    card = QColor(PALETTES[_current]["CARD"])
    text = QColor(PALETTES[_current]["TEXT_PRIMARY"])
    sub = QColor(PALETTES[_current]["TEXT_SECONDARY"])
    disabled = QColor(PALETTES[_current]["TEXT_DISABLED"])
    line = QColor(PALETTES[_current]["LINE"])
    accent = QColor(_ACCENT["THEME"])
    on_accent = QColor(_ACCENT["ON_THEME_TEXT"])

    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, card)
    p.setColor(QPalette.ColorRole.AlternateBase, window)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(PALETTES[_current]["CTRL_BG"]))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.ToolTipBase, card)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, sub)
    p.setColor(QPalette.ColorRole.Highlight, accent)
    p.setColor(QPalette.ColorRole.HighlightedText, on_accent)
    p.setColor(QPalette.ColorRole.Link, accent)
    p.setColor(QPalette.ColorRole.LinkVisited, accent)
    p.setColor(QPalette.ColorRole.Light, line)
    p.setColor(QPalette.ColorRole.Midlight, line)
    p.setColor(QPalette.ColorRole.Mid, line)
    p.setColor(QPalette.ColorRole.Dark, line)
    p.setColor(QPalette.ColorRole.Shadow, line)
    # 禁用态统一压成灰字，避免 Qt 用系统彩色（浅色底上常见"几乎看不见"）
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base,
               QColor(PALETTES[_current]["SURFACE"]))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button,
               QColor(PALETTES[_current]["SURFACE"]))
    return p


# ----------------------------------------------------------------------------
# 全局 QSS 生成：模板占位符 $KEY，由当前调色板填充
# ----------------------------------------------------------------------------
_QSS_TEMPLATE = Template("""
* {
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 10pt;
    color: $TEXT_PRIMARY;
}

QMainWindow, QDialog, QMessageBox {
    background: $WINDOW_BG;
}

QWidget#contentHost, QWidget#gridHost {
    background: $WINDOW_BG;
}

/* ---------- 内嵌标题栏（无边框窗口） ---------- */

/* 标题栏与内容区同色、无分隔线：窗口本身已经没有系统边框，
   再给标题栏单独上色会切出一条明显的横线，做不到整窗一体 */
QFrame#titleBar {
    background: $WINDOW_BG;
    border: none;
}

QLabel#titleBarText {
    font-size: 8pt;
    font-weight: 600;
    color: $TEXT_SUBTLE;
    background: transparent;
}

QPushButton#titleBarBtn,
QPushButton#titleBarClose,
QPushButton#titleBarMax {
    background: transparent;
    border: none;
    border-radius: 4px;
    color: $TEXT_SUBTLE;
    font-family: "Segoe MDL2 Assets";
    font-size: 8pt;
}

QPushButton#titleBarBtn:hover {
    background: $SURFACE;
    color: $TEXT_PRIMARY;
}

QPushButton#titleBarMax:hover {
    background: $SURFACE;
    color: $TEXT_PRIMARY;
}

QPushButton#titleBarClose:hover {
    background: $DANGER;
    color: $WHITE;
}

QLabel#statusHint {
    color: $TEXT_SUBTLE;
    background: transparent;
    font-size: 9pt;
    padding: 4px 0;
}

/* ---------- 顶栏家庭切换器 ---------- */

QPushButton#homeSwitcher {
    background: transparent;
    border: none;
    padding: 2px 0;
    font-size: 17pt;
    font-weight: 600;
    color: $TEXT_PRIMARY;
    text-align: left;
}

QPushButton#homeSwitcher:hover {
    color: $HOME_HOVER;
}

QPushButton#homeSwitcher:pressed {
    color: $HOME_PRESSED;
}

/* ---------- 详情页功能卡片（电源行/滑块/模式/输入/只读） ---------- */

QFrame#propCard {
    background: $CARD;
    border: 1px solid $LINE;
    border-radius: 14px;
}
QFrame#propCard:hover {
    background: $CARD_HOVER;
    border-color: $CARD_BORDER_HOVER;
}

QPushButton#propCardToggle {
    background: transparent;
    border: none;
    color: $THEME;
    font-size: 9pt;
    padding: 2px 6px;
}

QPushButton#propCardToggle:hover {
    color: $THEME_HOVER;
}

/* ---------- 原生菜单（家庭切换/更多操作） ---------- */

QMenu#appMenu {
    background: $CARD;
    border: 1px solid $LINE;
    border-radius: 8px;
    padding: 4px;
}

QMenu#appMenu::item {
    padding: 5px 26px 5px 6px;
    border-radius: 6px;
    font-size: 10pt;
    text-align: left;
    color: $TEXT_PRIMARY;
}

QMenu#appMenu::item:selected {
    background: $BTN_HOVER;
}

QMenu#appMenu::item:checked {
    color: $THEME;
}

QMenu#appMenu::indicator {
    width: 12px;
    height: 12px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 6px;
}

/* ---------- 详情面板 ---------- */

QLabel#panelTitle {
    font-size: 15pt;
    font-weight: 600;
    color: $TEXT_PRIMARY;
}

QLabel#panelSubtitle {
    color: $TEXT_SECONDARY;
    font-size: 9pt;
}

QScrollArea {
    background: transparent;
}

QLabel {
    background: transparent;
}

/* ---------- 原生数值框（详情面板输入行） ---------- */

QSpinBox, QDoubleSpinBox {
    background: $CARD;
    border: 1px solid $LINE;
    border-radius: 8px;
    padding: 5px 10px;
    selection-background-color: $THEME;
}

QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: $THEME;
}

/* ---------- 滚动条 ---------- */

QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: $SCROLLBAR;
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background: $SCROLLBAR_HOVER;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    height: 0;
    background: none;
}

QScrollBar:horizontal {
    height: 0;
    background: transparent;
}

/* ---------- 通知浮层 ---------- */

QFrame#toastCard {
    background: $SURFACE;
    border: 1px solid $BTN_HOVER;
    border-radius: 10px;
}

/* ---------- 小爱语音悬浮球 ---------- */

QPushButton#voiceBall {
    background: $THEME;
    border: none;
    border-radius: 27px;
}
QPushButton#voiceBall:hover {
    background: $THEME_CHECKED_HOVER;
}
QPushButton#voiceBall:pressed {
    background: $THEME_DIM;
}

QFrame#voicePanel {
    background: $SURFACE;
    border: 1px solid $LINE;
    border-radius: 12px;
}

/* ---------- 悬浮提示 ---------- */

/* 之前没有 QToolTip 规则：提示框背景取系统调色板（深色模式下是浅黄底），
   而全局面板规则把文字刷成主题色 —— 两相叠加会出现「黑底深字」几乎看不清
   （挂件场景卡的「执行场景」就是这样）。这里显式定死背景/文字/描边。
   注意必须用 background 简写：QToolTip 上 background-color 不生效，
   Qt 仍会按系统深色主题画黑底，浅色模式下就是「黑底深字」。 */
QToolTip {
    background: $SURFACE;
    color: $TEXT_PRIMARY;
    border: 1px solid $LINE;
    border-radius: 6px;
    padding: 4px 8px;
}
""")


def build_qss() -> str:
    """由当前调色板生成全局 QSS。"""
    values = dict(PALETTES[_current])
    # 模板里用到的主题色（随 set_accent 变化）与固定色
    values.update(_ACCENT)
    values.update(DANGER=DANGER, WHITE=WHITE)
    # 缺键早失败：模板新增占位符而某套调色板漏配时给出可读报错。
    # Template.get_identifiers 是 Python 3.11 新增；3.10 下退回
    # substitute 自身报错兜底（缺键信息稍弱但行为一致）
    if hasattr(_QSS_TEMPLATE, "get_identifiers"):
        missing = set(_QSS_TEMPLATE.get_identifiers()) - set(values)
        assert not missing, f"QSS 模板占位符缺少调色板键: {missing}"
    return _QSS_TEMPLATE.substitute(values)


# ----------------------------------------------------------------------------
# siui 全局色组联动：SiCapsuleComboBox 弹层、tooltip 等内部件取色自这里
# ----------------------------------------------------------------------------

def sync_siui_colors() -> None:
    """把 siui 全局色组切到当前主题，并覆盖主题 token 为当前主题色。

    siui 内置 Bright/Dark 两套色组；覆盖后 siui 内部件的青绿与
    应用主题色完全一致（内置 Bright 组的青绿偏亮，未必是用户选的色）。
    """
    from app.siui.core import SiColor, SiGlobal
    from app.siui.gui.color_group import BrightColorGroup, DarkColorGroup

    # 原地链到新色组而非重新绑定：siui 控件构造时缓存了色组引用，
    # 重新绑定会让未重建的旧控件继续用旧调色板
    group = SiGlobal.siui.colors
    reference = getattr(group, "reference", None)
    fresh = DarkColorGroup() if _current == "dark" else BrightColorGroup()
    fresh.assign(SiColor.THEME, _ACCENT["THEME"])
    fresh.assign(SiColor.THEME_TRANSITION_A, _ACCENT["THEME"])
    fresh.assign(SiColor.THEME_TRANSITION_B, _ACCENT["THEME_HOVER"])
    fresh.assign(SiColor.SVG_THEME, _ACCENT["THEME"])
    group.valid_state = False  # 本组命中即转 reference，绕过陈旧覆盖
    group.reference = fresh
    if reference is not None:
        fresh.reference = reference
    # 图标包默认色：浅色底上用深灰
    SiGlobal.siui.iconpack.setDefaultColor("#D1CBD4" if _current == "dark" else "#5f6368")


# ----------------------------------------------------------------------------
# siui 控件工厂（仅保留项目实际用到的）
# ----------------------------------------------------------------------------

def apply_switch_theme(switch) -> None:
    """把一枚已存在的 siui 开关刷成当前主题色。

    开关的轨道色是构造期快照：设置页预览主题色时不会重建对话框，
    不手动刷一次就会出现"开关还是旧颜色，点保存重开才变"。
    """
    switch.style_data.background_color_starting = QColor(SiColors.THEME)
    switch.style_data.background_color_ending = QColor(SiColors.THEME)
    switch.style_data.thumb_color_checked = QColor(SWITCH_THUMB_LIGHT)
    switch.update()


def themed_switch() -> SiSwitchRefactor:
    """bool 属性用的开关，开启态轨道为主题色。

    拇指恒为白色：深浅两套主题下都是"彩色轨道 + 白点"，与主按钮的
    白字同一套语汇。之前深色主题用近黑拇指，在彩色轨道上像一块脏点
    （用户两轮反馈后统一为白）。
    """
    switch = SiSwitchRefactor()
    # 焦点策略 NoFocus：开关是纯点按控件，不参与键盘焦点链——保留
    # StrongFocus 时，点击嵌入 QScrollArea 的开关会使其拿走焦点，随后
    # 服务确认回读触发的整帧重渲/布局微调会让 QScrollArea 为保焦点
    # 自动滚动到该控件（真机上表现为「点 USB-A 开关页面自动下移」）。
    switch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    apply_switch_theme(switch)
    return switch


def themed_label(text: str = "", color: str | None = None) -> "SiLabelRefactor":
    """自绘文字标签，颜色不受全局 QSS 影响；缺省为主文字色。"""
    from app.siui.components.label import SiLabelRefactor

    label = SiLabelRefactor()
    label.setText(text)
    label.setTextColor(color or SiColors.TEXT_PRIMARY)
    # QFrame 子类卡片加 WA_StyledBackground 后，某些渲染路径会让
    # 无样式表的 SiLabel 背景按 palette 画成不透明色块，必须显式
    # 强制透明（widget 级样式表优先级最高，直接覆盖任何级联来源）
    label.setStyleSheet("background: transparent;")
    return label


def themed_tab_button(text: str) -> SiToggleButtonRefactor:
    """房间筛选 tab，选中态为主题青底深字。"""
    button = SiToggleButtonRefactor()
    button.setText(text)
    # siui 按钮的默认 size hint 偏大且水平可扩展，强制固定尺寸
    # 避免三个 tab 在整行里分散排布
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    button.style_data.button_color = QColor("#00" + SiColors.CARD[1:])
    button.style_data.text_color = QColor(SiColors.TEXT_SECONDARY)
    button.style_data.toggled_button_color = QColor(SiColors.THEME)
    button.style_data.toggled_text_color = QColor(SiColors.ON_THEME_TEXT)
    button.style_data.hover_color = QColor("#1a" + SiColors.THEME[1:])
    button.style_data.idle_color = QColor("#00" + SiColors.THEME[1:])
    button.style_data.click_color = QColor("#40" + SiColors.THEME[1:])
    button.reloadStyleData()
    return button


class _SelectAllLineEdit(QLineEdit):
    """editable 下拉的输入框：聚焦/点击时自动全选。

    用户点进输入框直接输入新数字即整体替换（否则光标在末尾，
    输入会追加到 "100" 后变 "1001"）。
    """

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.selectAll()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        self.selectAll()


def themed_combo(options: list[str], current: str = "",
                 editable: bool = False) -> "QComboBox":
    """设置页等处的下拉选择器：原生 QComboBox + 主题化样式。

    曾用过 siui 的 SiCapsuleComboBox，但其胶囊风格与设置页卡片
    不搭且依赖图标包资源，改回原生控件按调色板着色。

    editable=True 时允许直接键入任意文本（如缩放百分比），下拉仍
    提供预设档位；输入不会插入列表（保持档位干净）。
    """
    import qtawesome as qta
    from PySide6.QtWidgets import QLabel, QComboBox

    class _NoWheelCombo(QComboBox):
        """禁用滚轮 + 自绘主题色下拉三角的原生下拉框。

        QSS 的 border 三角技巧在 down-arrow 上会渲染成色块长条，
        改为子控件摆一个 qtawesome 三角图标，颜色随调色板。
        """

        def __init__(self):
            super().__init__()
            self._arrow = QLabel(self)
            self._arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        def set_arrow_color(self, color: str) -> None:
            self._arrow.setPixmap(qta.icon('mdi.chevron-down', color=color).pixmap(14, 14))

        def resizeEvent(self, event) -> None:  # noqa: N802
            super().resizeEvent(event)
            self._arrow.move(self.width() - 20, (self.height() - 14) // 2)

        def wheelEvent(self, event) -> None:  # noqa: N802
            # 禁用滚轮切换：设置页滚动浏览时容易误改选项
            event.ignore()

    combo = _NoWheelCombo()
    combo.addItems(options)
    if editable:
        combo.setEditable(True)
        # 键入文本不进列表，档位保持预设干净；回车/失焦提交由调用方处理
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        # 替换输入框为自动全选版：聚焦/点击即全选，直接输入整体替换
        combo.setLineEdit(_SelectAllLineEdit(combo))
        # 关键：移除 completer 关闭自动补全——否则输入 "12" 会被自动匹配
        # 替换成列表里的 "125%" 之类（用户反馈「输 2 变 75」即此）。
        # 必须在 setLineEdit 之后：setLineEdit 会重置 completer。
        combo.setCompleter(None)
        combo.setCurrentText(current)
    elif current and current in options:
        combo.setCurrentText(current)
    combo.setCursor(Qt.CursorShape.PointingHandCursor)
    combo.setFixedHeight(32)
    # 选项最多四个字，固定窄宽即可，避免被布局撑得过宽；
    # 可编辑下拉同样保持此宽度，与设置页其他下拉（如主题配色）一致。
    # 弹出列表异常（压缩成几像素）的原因是 drop-down 区被隐藏，
    # 由 apply_combo_qss(editable=True) 保留 24px 下拉区解决，与宽度无关。
    combo.setFixedWidth(112)
    apply_combo_qss(combo, editable=editable)
    combo.set_arrow_color(SiColors.TEXT_SECONDARY)
    return combo


def apply_combo_qss(combo, editable: bool = False) -> None:
    """下拉框与弹出列表按当前调色板着色（主题切换时重设）。

    editable=True 时保留 ::drop-down 区域宽度：可编辑下拉需要点
    下拉区才弹出列表（点正文是输入）；隐藏该区域会导致点不开。
    """
    combo.setStyleSheet(f"""
        QComboBox {{
            background: {SiColors.SURFACE};
            border: 1px solid {SiColors.LINE};
            border-radius: 8px;
            padding: 4px {8 if editable else 28}px 4px 12px;
            color: {SiColors.TEXT_PRIMARY};
            font-size: 9pt;
        }}
        QComboBox:hover {{ border-color: {SiColors.THEME}; }}
        QComboBox::drop-down {{ border: none; width: {24 if editable else 0}px; }}
        QComboBox::down-arrow {{ width: 0px; height: 0px; }}
        QComboBox QAbstractItemView {{
            background: {SiColors.CARD};
            border: 1px solid {SiColors.LINE};
            border-radius: 6px;
            color: {SiColors.TEXT_PRIMARY};
            selection-background-color: {SiColors.BTN_HOVER};
            selection-color: {SiColors.TEXT_PRIMARY};
            outline: none;
        }}
        QComboBox QLineEdit {{
            background: transparent;
            border: none;
            color: {SiColors.TEXT_PRIMARY};
        }}
    """)
