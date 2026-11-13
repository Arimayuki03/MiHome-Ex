# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""桌面挂件 Widgets Board：常驻桌面的设备控制面板。

与托盘快捷窗口（TrayQuickWindow）是**并列的两个入口**，不是替代关系：
托盘是临时呼出、用完即走，Board 是常驻抬眼即用。两者共用 PowerButton
与 SiColors 主题色，但设备清单各自独立（board.json / tray.json）。

窗口行为层（Board 独有，托盘窗口没有的部分）：
    常驻置顶   无边框 + WindowStaysOnTopHint + Tool，不占任务栏
    自由摆放   顶部拖拽区拖动窗口，位置与尺寸落盘恢复
    透明度     0.30–1.00 无级调节，鼠标悬停时自动恢复不透明便于读取
    一键收起   折叠为一条 32px 细横条，每台设备一个状态点，点一下展开
    列数       单排最多 1–9 张卡片，改动即时重排

卡片只承载「电源开关」这一条最快路径（与托盘快捷行同款）：名称 +
房间/温湿度副标题 + 圆形电源钮；点击卡片本体打开完整详情面板。
"""

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import qtawesome as qta

from app.core import board_store
from app.core.jobs import PRIORITY_CONTROL, JobExecutor
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.power_button import PowerButton
from app.ui.si_theme import SiColors

# 卡片与窗口的尺寸基准
# 卡片最小宽度：太宽时整块面板像一排大方块（用户反馈"太宽"），
# 148 下三行文字（名称 + 房间/读数）仍放得下，单排 9 张也不至于铺满两屏
_CARD_MIN_WIDTH = 148
_CARD_HEIGHT = 56
_COLLAPSE_HEIGHT = 32
# 悬停恢复不透明 / 移开回落的过渡时长：太短像瞬变，太长显得拖沓
_OPACITY_FADE_MS = 180
_MARGIN = 10          # 根面板左右留白
# 底部留白单独给一个更小的值：卡片本身就带圆角与边框，底边再留 10px
# 会在单排时变成一条"空行"（用户实测截图反馈），6px 与行间距 6px 对齐
_BOTTOM_PAD = 6
_GRID_SPACING = 6
_DRAG_HANDLE_HEIGHT = 22
# 判定"拖动"而非"点击"的位移阈值（像素）：太小会误判抖动，太大拖不动
_DRAG_THRESHOLD = 5
# 面板上卡片拖拽排序的位移阈值（比窗口拖动大一点，避免点卡片时误排序）
_REORDER_THRESHOLD = 8

# 折叠横条里状态点的候选尺寸 (点径, 间距)：放不下就逐级缩小，再放不下改聚合条
_DOT_STEPS = ((9, 5), (7, 4), (5, 3))
# 折叠横条固定开销：左右边距 20 + 抓握纹 10 + 展开按钮 22
_BAR_FIXED_W = 20 + 10 + 22
# 横条里放得下标题的最小宽度（窄了先把标题让给状态区）
_BAR_TITLE_MIN_W = 300

# SetWindowPos 的 hWndInsertAfter 与标志位：挂件按置顶开关归位层级时用
_HWND_TOPMOST = -1
_HWND_BOTTOM = 1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010

# 挂件自称：桌面上只有这一块面板，标题要让人一眼知道它是谁家的
# 换产品名只改这两行即可
_APP_LABEL = "米家"
_BOARD_TITLE = f"{_APP_LABEL} · 桌面挂件"
_BAR_TITLE = f"{_APP_LABEL}挂件"


def _board_btn_qss(palette: "_BoardPalette | None" = None) -> str:
    """Board 顶部工具按钮统一观感（22×22 圆角小按钮）。"""
    surface = palette.surface if palette is not None else SiColors.SURFACE
    hover = palette.btn_hover if palette is not None else SiColors.BTN_HOVER
    return (
        f"QPushButton {{ background: {surface}; border: none;"
        f" border-radius: 11px; }}"
        f"QPushButton:hover {{ background: {hover}; }}")


def _rgba(color: QColor, alpha: float) -> str:
    """QColor → Qt 样式表可用的 rgba() 字符串（alpha 用 0–255）。"""
    return (f"rgba({color.red()}, {color.green()}, {color.blue()},"
            f" {int(round(alpha * 255))})")


def _solid(color: QColor, alpha: float, backdrop: QColor) -> str:
    """半透明色 → 预混在底色上的不透明 #rrggbb。

    **只用于边框、色块这类"结构性"颜色**：Qt 的样式表在画半透明圆角边框
    时会走另一条合成路径，在非整数缩放（如 100% 系统 + 125% 界面 = 156%）
    下，1px 圆角边框会出现明显锯齿，圆角处还会冒出亮斑。跟随主题时用的
    本来就是不透明 hex，所以只有"自定义底色"下才看得见——用户实测反馈。
    文字仍保留 rgba（文字抗锯齿混合半透明很正常，不会有这个问题）。
    """
    return _mix(backdrop, color, alpha).name()



def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """按比例混合两色（t=0 取 a，t=1 取 b）。"""
    t = min(max(t, 0.0), 1.0)
    return QColor(
        int(round(a.red() + (b.red() - a.red()) * t)),
        int(round(a.green() + (b.green() - a.green()) * t)),
        int(round(a.blue() + (b.blue() - a.blue()) * t)),
    )


class _BoardPalette:
    """挂件配色：跟随主题时直接取 SiColors，自定义底色时按明暗派生。

    派生规则只说一句话：**底色暗就把卡片/文字往白里提，底色亮就往黑里压**。
    这样用户选纯黑、墨蓝、米白都能正常读，不会出现黑底黑字。
    强调色（电源开态、选中态）始终沿用主题绿，两套底色下都够亮。

    边框/色块一律用 `_solid` 预混成不透明 hex，只有文字保留半透明：
    半透明圆角边框在非整数缩放下会被 Qt 画成两行渐变线并出现锯齿与
    角部亮斑（见 `_solid` 的说明）。
    """

    def __init__(self, bg: str | None):
        if bg is None:
            self.window_bg = SiColors.WINDOW_BG
            self.line = SiColors.LINE
            self.card = SiColors.CARD
            self.card_hover = SiColors.CARD_HOVER
            self.card_border_hover = SiColors.CARD_BORDER_HOVER
            self.surface = SiColors.SURFACE
            self.btn_hover = SiColors.BTN_HOVER
            self.text_primary = SiColors.TEXT_PRIMARY
            self.text_secondary = SiColors.TEXT_SECONDARY
            self.text_muted = SiColors.TEXT_MUTED
            self.scrollbar = SiColors.SCROLLBAR
            self.scrollbar_hover = SiColors.SCROLLBAR_HOVER
            self.offline_text = SiColors.OFFLINE_TEXT
            self.offline_sub = SiColors.OFFLINE_SUB
            self.state_unknown_border = SiColors.STATE_UNKNOWN_BORDER
            self.state_off = SiColors.STATE_OFF
            self.is_custom = False
            return

        base = QColor(bg)
        # lightnessF 比 HSV 的 V 更稳：纯黑 V=0 时 lighter() 提不亮，必须手动混色
        dark = base.lightnessF() < 0.5
        self.is_custom = True
        self.window_bg = base.name()
        if dark:
            white = QColor(255, 255, 255)
            self.card = _mix(base, white, 0.10).name()
            self.card_hover = _mix(base, white, 0.16).name()
            self.card_border_hover = _mix(base, white, 0.30).name()
            self.surface = _mix(base, white, 0.14).name()
            self.btn_hover = _mix(base, white, 0.24).name()
            self.line = _solid(white, 0.14, base)
            self.scrollbar = _solid(white, 0.22, base)
            self.scrollbar_hover = _solid(white, 0.38, base)
            self.text_primary = "#f2f3f5"
            self.text_secondary = _rgba(white, 0.66)
            self.text_muted = _rgba(white, 0.45)
            self.offline_text = _rgba(white, 0.42)
            self.offline_sub = _solid(white, 0.28, base)
            self.state_unknown_border = _solid(white, 0.35, base)
            self.state_off = _solid(white, 0.28, base)
        else:
            black = QColor(0, 0, 0)
            self.card = _mix(base, black, 0.05).name()
            self.card_hover = _mix(base, black, 0.09).name()
            self.card_border_hover = _mix(base, black, 0.20).name()
            self.surface = _mix(base, black, 0.07).name()
            self.btn_hover = _mix(base, black, 0.13).name()
            self.line = _solid(black, 0.12, base)
            self.scrollbar = _solid(black, 0.22, base)
            self.scrollbar_hover = _solid(black, 0.36, base)
            self.text_primary = "#1a1b1f"
            self.text_secondary = _rgba(black, 0.66)
            self.text_muted = _rgba(black, 0.48)
            self.offline_text = _rgba(black, 0.38)
            self.offline_sub = _solid(black, 0.26, base)
            self.state_unknown_border = _solid(black, 0.30, base)
            self.state_off = _solid(black, 0.26, base)


def _qcolor(spec: str) -> QColor:
    """把样式表颜色串转成 QColor（支持 #rrggbb 与 rgba(r,g,b,a)）。"""
    s = (spec or "").strip()
    if s.startswith(("rgba(", "rgb(")):
        inner = s[s.index("(") + 1:s.rindex(")")]
        parts = [p.strip() for p in inner.split(",")]
        try:
            r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
            a = int(round(float(parts[3]))) if len(parts) > 3 else 255
            return QColor(r, g, b, a)
        except (ValueError, IndexError):
            return QColor("gray")
    return QColor(s)


class _FloatingHint(QWidget):
    """自绘的小提示窗（tooltip / 拖拽浮标共用）。

    为什么不直接用 QLabel + 样式表：Qt 在「半透明顶层窗口」里对
    QLabel 的样式表背景与 QToolTip 的绘制都不可控（实测拿到 alpha=0 或
    落回系统深色样式，表现为黑底深字）。这里直接 paintEvent 画圆角矩形
    与文字，配色由调用方给，任何主题下都是确定的像素。
    """

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._text = ""
        self._bg = QColor(30, 31, 36)
        self._fg = QColor(232, 232, 234)
        self._bd = QColor(53, 54, 63)
        self._pad_x, self._pad_y, self._radius = 9, 5, 6

    def set_colors(self, bg: str, fg: str, border: str) -> None:
        self._bg = _qcolor(bg)
        self._fg = _qcolor(fg)
        self._bd = _qcolor(border)

    def setText(self, text: str) -> None:  # noqa: N802 (与 QLabel 同名，便于替换)
        self._text = text or ""
        self.adjustSize()
        self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self):  # noqa: N802
        fm = self.fontMetrics()
        width = fm.horizontalAdvance(self._text) + self._pad_x * 2 + 2
        height = fm.height() + self._pad_y * 2 + 2
        return QSize(max(20, width), max(16, height))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        painter.fillPath(path, self._bg)
        painter.setPen(self._bd)
        painter.drawPath(path)
        painter.setPen(self._fg)
        painter.drawText(
            self.rect().adjusted(self._pad_x, self._pad_y, -self._pad_x, -self._pad_y),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._text,
        )
        painter.end()


class _FlexLabel(QLabel):
    """宽度可压缩的标签：布局按 sizeHint 分配，最小宽度允许压到 0。

    QLabel 默认的 minimumSizeHint 就是文字宽度，标题一长就把所在行
    （标题栏/折叠横条）的最小宽顶大，进而钳制窗口 resize——从多列
    切到单列后窗口缩不回去就是这个原因。
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())


class _AggregateBar(QWidget):
    """折叠横条的聚合状态条：每台设备一段，按开 / 关 / 未知 / 离线着色。

    设备多到状态点放不下时用它表达——宽度只跟着窗口走，**永远不会撑宽挂件**。
    逐台明细放在横条的 tooltip 里。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._states: list[tuple[bool | None, bool]] = []
        self._palette: _BoardPalette | None = None
        self.setMinimumHeight(10)
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_states(self, states: list[tuple[bool | None, bool]],
                   palette: "_BoardPalette") -> None:
        self._states = list(states)
        self._palette = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._states or self._palette is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        n = len(self._states)
        gap = 2.0 if n > 1 else 0.0
        seg = max(2.0, (self.width() - gap * (n - 1)) / n)
        height = min(8.0, max(4.0, self.height()))
        top = (self.height() - height) / 2
        x = 0.0
        painter.setPen(Qt.PenStyle.NoPen)
        for state, online in self._states:
            if not online:
                color = _qcolor(self._palette.offline_sub)
            elif state is True:
                color = QColor(SiColors.THEME)
            elif state is False:
                color = _qcolor(self._palette.state_off)
            else:
                color = _qcolor(self._palette.state_unknown_border)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, top, seg, height), 2, 2)
            x += seg + gap
        painter.end()


class WidgetsBoard(QWidget):
    """常驻桌面的设备挂件。"""

    manage_requested = Signal()
    open_device_requested = Signal(str)  # did
    # 挂件内直接开关成功后回传主窗口：让主界面卡片与 _known_power 保持一致，
    # 否则轮询会拿旧记忆覆盖刚点的结果
    power_changed = Signal(str, object)  # (did, bool)

    def __init__(self, service: MijiaService, jobs: JobExecutor, parent=None):
        super().__init__(parent)
        self._service = service
        self._jobs = jobs
        self._devices: list[DeviceInfo] = []
        self._known_power: dict[str, bool | None] = {}
        self._metrics: dict[str, str | None] = {}
        # did -> 本地操作版本号：丢弃"操作前发出、操作后回来"的旧轮询结果
        self._power_gen: dict[str, int] = {}
        self._dids: list[str] = board_store.load_devices()
        # 场景快照与混排顺序：与设备一起渲染在同一个网格里
        self._scenes: list[dict] = board_store.load_scenes()
        self._order: list[dict] = board_store.load_order()
        # 锁定：隐藏标题栏 + 禁止拖动 + 形态固定（解锁靠悬停浮锁/右键/托盘）
        self._locked: bool = board_store.get_locked()
        self._scene_cards: dict = {}
        self._columns: int = board_store.get_columns()
        self._opacity: float = board_store.get_opacity()
        # 背景色：None = 跟随主题；否则是用户挑的固定色
        self._bg: str | None = board_store.get_bg()
        # 置顶：默认关。常驻置顶会挡住别的窗口（用户明确反馈过）
        self._topmost: bool = board_store.get_topmost()
        self._collapsed: bool = False
        # 悬停恢复不透明：拖动滑块时鼠标在窗口内，若强制 1.0 会看不到预览
        self._force_opaque = False
        # 管理对话框打开期间为 True：暂停悬停恢复，让透明度预览可见
        self.setProperty("previewing", False)

        self.setWindowFlags(self._window_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("米家桌面挂件")

        # 展开态几何：折叠时横条贴原位，收起高度后仍能回到原位置尺寸
        self._expanded_geom: tuple[int, int, int, int] | None = None
        # 三块可拖区域的统一按下状态：窗口原点与"是否已越过拖拽阈值"
        self._press_origin: QPoint | None = None
        self._press_moved = False
        # 位置/尺寸落盘防抖：拖动过程中每帧都写会拖慢窗口跟随
        # （_rebuild 也会用，必须先于首次 _rebuild 建好）
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_geometry)
        # 显式最小尺寸：窗口宽高全部由 _board_width/_board_height 计算，
        # 没有(也不允许)手动调整大小。不设的话 Qt 用缓存的 minimumSizeHint
        # 钳制 resize——内部控件清空后缓存不失效，单排 1 张的 148px 会被
        # 之前宽状态留下的 165px 旧缓存顶回去（实测复现）
        self.setMinimumSize(0, 0)

        # 配色表：跟随主题或用户挑的固定色（见 _BoardPalette）
        self._palette = _BoardPalette(self._bg)
        self._build_ui()
        self._install_hint_filters()
        # 锁定状态在构造期就生效：标题栏直接不建出来
        self._handle.setVisible(not self._locked)
        self._restore_geometry()
        self._apply_opacity()
        self._rebuild()

    # ---------- 界面搭建 ----------

    def _build_ui(self) -> None:
        self._root = QFrame(self)
        self._root.setObjectName("boardPanel")
        self._root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._root.setStyleSheet(
            f"QFrame#boardPanel {{ background: {self._palette.window_bg};"
            f" border: 1px solid {self._palette.line}; border-radius: 14px; }}")
        root_lay = QVBoxLayout(self._root)
        root_lay.setContentsMargins(_MARGIN, 8, _MARGIN, _BOTTOM_PAD)
        root_lay.setSpacing(6)

        # 顶部：拖拽把手 + 设备数 + 三个小按钮
        handle = QFrame(self._root)
        handle.setObjectName("boardHandle")
        handle.setFixedHeight(_DRAG_HANDLE_HEIGHT)
        handle.setStyleSheet("QFrame#boardHandle { background: transparent; }")
        handle.setCursor(Qt.CursorShape.OpenHandCursor)
        self._handle = handle
        handle.installEventFilter(self)
        # 面板空白处也可拖：只有 22px 把手太细，用户容易"抓不住"
        self._root.installEventFilter(self)
        hl = QHBoxLayout(handle)
        hl.setContentsMargins(2, 0, 2, 0)
        hl.setSpacing(4)

        self._title = _FlexLabel(_BOARD_TITLE)
        self._title.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.DemiBold))
        # 宽度按省略后的文本走：Ignored 会被布局压成 0 宽，标题直接看不见；
        # 窄挂件（单排 1 张）放不下全名时由 _update_title 先省略
        self._title.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._title.setStyleSheet(
            f"color: {self._palette.text_secondary}; background: transparent;")
        self._title.setToolTip(f"{_BOARD_TITLE}（单击收起为细横条，拖动可移动）")
        hl.addWidget(self._title)
        hl.addStretch(1)

        self._tool_buttons: list[QPushButton] = []
        # 锁定：状态开关，单独成组放最左；右侧三条是窗口动作。
        # 分组靠 1px 竖线 + 间距，不额外加底，保持标题行干净。
        self._lock_toggle = self._make_tool_button(
            "mdi.lock", "锁定",
            lambda: self.set_locked(True))
        hl.addWidget(self._lock_toggle)
        # 置顶：图钉开关，选中时主题色底；与锁定并排成"状态开关"组
        self._pin_toggle = self._make_tool_button(
            "mdi.pin-outline", "置顶", self._toggle_topmost)
        hl.addWidget(self._pin_toggle)
        self._tool_divider = QFrame(self._handle)
        self._tool_divider.setFixedSize(1, 14)
        hl.addSpacing(2)
        hl.addWidget(self._tool_divider)
        hl.addSpacing(2)
        # 收起：向下箭头（折叠成横条）
        self._collapse_btn = self._make_tool_button(
            "mdi.chevron-down", "收起为细横条", self._collapse)
        hl.addWidget(self._collapse_btn)
        # 管理：加号
        add_btn = self._make_tool_button(
            "mdi.plus", "管理挂件设备", self.manage_requested.emit)
        hl.addWidget(add_btn)
        # 隐藏：叉号（收起窗口但保留状态，托盘菜单可再显示）
        hide_btn = self._make_tool_button("mdi.close", "隐藏挂件", self.hide_by_user)
        hl.addWidget(hide_btn)
        root_lay.addWidget(handle)

        # 卡片滚动区：设备多于一屏时纵向滚动
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0 2px 0 0; }"
            f"QScrollBar::handle:vertical {{ background: {self._palette.scrollbar};"
            " border-radius: 3px; min-height: 30px; }"
            f"QScrollBar::handle:vertical:hover {{ background: {self._palette.scrollbar_hover}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }")
        self._host = QWidget()
        self._host.setStyleSheet("background: transparent;")
        self._list_lay = QVBoxLayout(self._host)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(0)
        self._scroll.setWidget(self._host)
        root_lay.addWidget(self._scroll, stretch=1)

        self._empty = QLabel("还没有添加设备或场景\n点右上角 + 选择")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(
            f"color: {self._palette.text_secondary}; background: transparent; font-size: 9pt;")
        self._empty.hide()
        root_lay.addWidget(self._empty)

        # 折叠态内容：一条横条，每台设备一个状态点
        self._bar = QFrame(self)
        self._bar.setObjectName("boardBar")
        self._bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._bar.setStyleSheet(
            f"QFrame#boardBar {{ background: {self._palette.window_bg};"
            f" border: 1px solid {self._palette.line}; border-radius: 14px; }}")
        self._bar.setFixedHeight(_COLLAPSE_HEIGHT)
        # 折叠态整条可拖拽移动；OpenHand 提示"能抓起来"
        self._bar.setCursor(Qt.CursorShape.OpenHandCursor)
        # 折叠横条同时承担两件事：拖动移动窗口、单击展开。
        # 靠位移阈值区分（见 eventFilter），否则一按就展开、永远拖不动
        self._bar.installEventFilter(self)
        bar_lay = QHBoxLayout(self._bar)
        bar_lay.setContentsMargins(10, 0, 10, 0)
        bar_lay.setSpacing(6)
        # 左侧抓握纹，明确告诉用户这条东西可以拖
        self._bar_grip = QLabel("⠿")
        self._bar_grip.setFixedWidth(10)
        self._bar_grip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bar_grip.setStyleSheet(
            f"color: {self._palette.text_muted}; background: transparent; font-size: 9pt;")
        self._bar_grip.setToolTip("拖动可移动位置，单击展开")
        bar_lay.addWidget(self._bar_grip)
        # 折叠态标题：只剩一条细横条时，光有状态点看不出这是谁家的挂件。
        # 用 Preferred 而不是 Ignored：Ignored 会被布局压成 0 宽，即使
        # _dot_plan 算出来放得下也看不见；空间不够时由 _dot_plan 主动隐藏
        self._bar_title = _FlexLabel(_BAR_TITLE)
        self._bar_title.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._bar_title.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.DemiBold))
        self._bar_title.setStyleSheet(
            f"color: {self._palette.text_secondary}; background: transparent; font-size: 8pt;")
        self._bar_title.setToolTip(f"{_BOARD_TITLE}（拖动可移动，单击展开）")
        bar_lay.addWidget(self._bar_title)
        self._dot_host = QWidget()
        self._dot_host.setStyleSheet("background: transparent;")
        self._dot_lay = QHBoxLayout(self._dot_host)
        self._dot_lay.setContentsMargins(0, 0, 0, 0)
        self._dot_lay.setSpacing(5)
        self._dot_lay.addStretch(1)
        bar_lay.addWidget(self._dot_host, stretch=1)
        # 展开按钮：向上箭头
        expand = self._make_tool_button('mdi.chevron-up', "展开挂件", self._expand)
        bar_lay.addWidget(expand)
        self._bar.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._root)
        outer.addWidget(self._bar)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # 锁定后的解锁入口之一：悬停时浮在面板右上角的小锁。
        # 不放进面板布局，避免影响卡片网格与折叠条宽度计算。
        self._lock_btn = QPushButton(self)
        # 锁定后标题栏已隐藏，这是"可见的解锁入口"：做小、贴角，
        # 尽量不压住卡片右上角的电源钮（18px 贴 2px 边基本错开）
        self._lock_btn.setFixedSize(18, 18)
        self._lock_btn.setIconSize(QSize(12, 12))
        self._lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lock_btn.setToolTip("解锁")
        self._lock_btn.clicked.connect(lambda: self.set_locked(False))
        self._lock_btn.hide()

        # 上面各控件的初始样式用主题色兜底，这里统一按当前配色表覆盖一遍：
        # 用户若设过自定义背景色，构造完就是正确颜色，不会闪一下主题色
        self._apply_styles()

    def _make_tool_button(self, icon: str, tip: str, on_click) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(22, 22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tip)
        btn.setIcon(qta.icon(icon, color=self._palette.text_primary))
        btn.setIconSize(QSize(16, 16))
        btn.setStyleSheet(_board_btn_qss())
        btn.setAutoDefault(False)
        btn.setDefault(False)
        btn.clicked.connect(on_click)
        # 图标色随主题变化，记住图标名供 retheme 重画
        btn._icon_name = icon
        self._tool_buttons.append(btn)
        return btn

    # ---------- 几何与透明度 ----------

    def _restore_geometry(self) -> None:
        geom = board_store.get_geometry()
        if geom is None:
            self._expanded_geom = self._default_geometry()
        else:
            self._expanded_geom = self._sanitize_geometry(geom)
        self._apply_expanded_geometry()
        if board_store.get_collapsed():
            self._collapsed = True
            self._swap_to_collapsed()

    def _sanitize_geometry(self, geom: tuple[int, int, int, int]
                           ) -> tuple[int, int, int, int]:
        """把落盘的位置拉回可见屏幕范围内。

        落盘的位置可能已经不存在于当前桌面：拔掉副屏、改分辨率、缩小
        缩放比之后，原来 (x, y) 落在屏幕外。挂件是无边框 Tool 窗口，
        不占任务栏也没有标题栏，一旦整窗跑到屏幕外，用户既看不见也点
        不到——只能删配置文件。这里按"窗口左上有 40px 落在某块屏内"判定
        有效，否则贴回主屏右上角（与首次启动一致）。
        """
        x, y, w, h = geom
        screen = QGuiApplication.screenAt(QPoint(x + 40, y + 10))
        if screen is None:
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return geom
            geo = screen.availableGeometry()
            return (max(geo.left(), geo.right() - w - 24),
                    max(geo.top(), geo.top() + 24), w, h)
        geo = screen.availableGeometry()
        # 部分越界：只把位置钳回该屏可见范围（尺寸由 _board_height 重算）
        x = min(max(x, geo.left() - w + 60), geo.right() - 60)
        y = min(max(y, geo.top()), geo.bottom() - 20)
        return x, y, w, h

    def _default_geometry(self) -> tuple[int, int, int, int]:
        """首次启动：贴在主屏右上角（避开任务栏与桌面图标栏）。"""
        screen = QGuiApplication.primaryScreen()
        width = self._board_width()
        height = self._board_height()
        if screen is None:
            return 80, 80, width, height
        geo = screen.availableGeometry()
        return max(geo.left(), geo.right() - width - 24), \
            max(geo.top(), geo.top() + 24), width, height

    def _apply_expanded_geometry(self) -> None:
        if self._expanded_geom is not None:
            self.setGeometry(*self._expanded_geom)
        self.resize(self._board_width(), self._board_height())

    def _board_width(self) -> int:
        """按列数算卡片区总宽：卡片下限宽 + 间距 + 边距。

        9 列在窄屏上会宽出桌面（窗口跑出屏幕就再也点不到），这里按屏幕
        可用宽度收住——卡片本身是可压缩的，收窄后自动均分。
        """
        # 单列与多列统一卡宽：单列再收窄会让窗口忽宽忽窄，卡片内容
        # 的最小宽也会反过来把窗口顶大（见 _rebuild 里的 setMinimumSize）
        unit = _CARD_MIN_WIDTH
        inner = unit * self._columns + _GRID_SPACING * (self._columns - 1)
        width = inner + _MARGIN * 2
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            width = min(width, max(320, screen.availableGeometry().width() - 16))
        return width

    def _board_height(self) -> int:
        """按行数算窗口高度（与真实布局边距一致，否则会出现滚动条）。

        根面板：上边距 8 + 拖拽把手 22 + 把手与卡片区间距 6 + 下边距 6
        + 上下各 1px 边框；每行卡片 56，行间距 6（末行后无间距）。
        只留 1px 余量吸收舍入误差——留多了会在单排时变成底部一条空行
        （用户实测截图反馈）。设备多到超出屏幕可用高度时封顶，由滚动条接管。
        """
        rows = max(1, -(-len(self._content_items()) // max(1, self._columns)))
        body = rows * _CARD_HEIGHT + (rows - 1) * _GRID_SPACING
        # 上边距 8 + 标题栏（锁定后隐藏，22px + 间距 6）+ 下边距 6
        head = 8 + (0 if self._locked else _DRAG_HANDLE_HEIGHT + 6) + _BOTTOM_PAD
        height = head + body + 2 + 1
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            cap = screen.availableGeometry().height() - 80
            height = min(height, max(160, cap))
        return height

    def _save_geometry(self) -> None:
        if self._expanded_geom is not None:
            board_store.set_geometry(*self._expanded_geom)

    # ---------- 背景色 / 置顶 ----------

    @property
    def palette(self) -> "_BoardPalette":
        return self._palette

    def set_bg(self, color: str | None, *, persist: bool = True) -> None:
        """设置背景色：None = 跟随主题，'#rrggbb' = 固定色。

        persist=False 给管理对话框做实时预览用（对话框自己负责落盘）。
        """
        if color is not None and not isinstance(color, str):
            return
        self._bg = color
        if persist:
            board_store.set_bg(color)
        self._palette = _BoardPalette(self._bg)
        self._apply_styles()

    def bg(self) -> str | None:
        return self._bg

    def _window_flags(self):
        """窗口标志：无边框 + Tool（不占任务栏），置顶按开关决定。

        非置顶时额外禁止窗口接受焦点：挂件是贴在桌面上的常驻面板，
        点一下只该响应这次点击，不该把焦点从用户正在用的窗口抢走。
        真正把它压在其它窗口之下由 _apply_z_order 完成（Qt 的
        WindowStaysOnBottomHint 在 Windows 上不生效）。
        """
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._topmost:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
        return flags

    def set_topmost(self, topmost: bool, *, persist: bool = True) -> None:
        """切换置顶。默认关：与普通窗口一样会被别的窗口盖住。

        注意 Qt 在 Windows 上改窗口标志会**销毁并重建原生窗口**：窗口被隐藏、
        位置尺寸回到默认值。因此改完必须重新 show 并还原几何，否则挂件会
        跳到屏幕左上角。
        """
        topmost = bool(topmost)
        if topmost == self._topmost:
            if persist:
                board_store.set_topmost(topmost)
            self._refresh_pin_toggle()
            return
        self._topmost = topmost
        if persist:
            board_store.set_topmost(topmost)
        self._apply_window_flags()
        self._refresh_pin_toggle()

    def _toggle_topmost(self) -> None:
        """标题栏图钉：一键切换置顶。"""
        self.set_topmost(not self._topmost)

    def _refresh_pin_toggle(self) -> None:
        """图钉按钮跟随置顶状态：选中时主题色底 + 实心钉，取消后恢复普通按钮。"""
        btn = getattr(self, "_pin_toggle", None)
        if btn is None:
            return
        if self._topmost:
            btn.setStyleSheet(
                f"QPushButton {{ background: {SiColors.THEME}; border: none;"
                " border-radius: 11px; }"
                f"QPushButton:hover {{ background: {SiColors.THEME_CHECKED_HOVER}; }}")
            btn.setIcon(qta.icon("mdi.pin", color=SiColors.ON_THEME_TEXT))
            btn.setToolTip("取消置顶")
            btn._icon_name = "mdi.pin"
        else:
            btn.setStyleSheet(_board_btn_qss(self._palette))
            btn.setIcon(qta.icon("mdi.pin-outline",
                                 color=self._palette.text_primary))
            btn.setToolTip("置顶")
            btn._icon_name = "mdi.pin-outline"

    def _apply_window_flags(self) -> None:
        visible = self.isVisible()
        geo = self.geometry()
        self.setWindowFlags(self._window_flags())
        # setWindowFlags 后窗口不可见；即使原来可见也要重新 show
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if visible:
            self.show()
            self.setGeometry(geo)
            # 折叠态高度只有 32px，show 之后重新贴回原尺寸
            if not self._collapsed:
                self._apply_expanded_geometry()
            self._apply_opacity()
            # show 之后系统会把窗口摆到最前，等这一轮事件处理完再按
            # 置顶开关归位（非置顶要压回最底层）
            QTimer.singleShot(0, self._apply_z_order)

    def is_topmost(self) -> bool:
        return self._topmost

    def _apply_z_order(self) -> None:
        """按置顶开关把窗口放到最顶层或最底层。

        Qt 的 WindowStaysOnBottomHint 在 Windows 上不生效：非置顶的挂件
        一旦被点击或重新 show 就会浮到其它窗口之上，把用户正在用的程序
        盖住。这里直接调 SetWindowPos 控制 z-order，HWND_BOTTOM 表示压在
        所有普通窗口之下（仍在桌面之上），HWND_TOPMOST 表示置顶。
        """
        import shiboken6

        # 延时调用（showEvent 后的单次定时器）可能落在窗口已销毁之后
        if not shiboken6.isValid(self) or not self.isVisible():
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            # 64 位下 hWndInsertAfter 必须按指针传，-1/1 直接传会截断
            insert_after = wintypes.HWND(
                _HWND_TOPMOST if self._topmost else _HWND_BOTTOM)
            user32.SetWindowPos(
                wintypes.HWND(int(self.winId())), insert_after,
                0, 0, 0, 0,
                _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
        except Exception:
            # 非 Windows 或原生窗口尚未就绪：退化成普通窗口的层级行为
            pass

    def apply_z_order(self) -> None:
        """按当前置顶状态立即归位（主窗口显示挂件后调用）。"""
        self._apply_z_order()

    def _apply_styles(self) -> None:
        """按当前配色表重设所有内联样式（主题切换与改背景色共用）。"""
        p = self._palette
        self._root.setStyleSheet(
            f"QFrame#boardPanel {{ background: {p.window_bg};"
            f" border: 1px solid {p.line}; border-radius: 14px; }}")
        self._bar.setStyleSheet(
            f"QFrame#boardBar {{ background: {p.window_bg};"
            f" border: 1px solid {p.line}; border-radius: 14px; }}")
        self._title.setStyleSheet(
            f"color: {p.text_secondary}; background: transparent;")
        self._empty.setStyleSheet(
            f"color: {p.text_secondary}; background: transparent; font-size: 9pt;")
        grip = getattr(self, "_bar_grip", None)
        if grip is not None:
            grip.setStyleSheet(
                f"color: {p.text_muted}; background: transparent; font-size: 9pt;")
        bar_title = getattr(self, "_bar_title", None)
        if bar_title is not None:
            bar_title.setStyleSheet(
                f"color: {p.text_secondary}; background: transparent;"
                " font-size: 8pt;")
        scroll = getattr(self, "_scroll", None)
        if scroll is not None:
            scroll.setStyleSheet(
                "QScrollArea { background: transparent; border: none; }"
                "QScrollBar:vertical { background: transparent; width: 6px;"
                " margin: 0 2px 0 0; }"
                f"QScrollBar::handle:vertical {{ background: {p.scrollbar};"
                " border-radius: 3px; min-height: 30px; }"
                f"QScrollBar::handle:vertical:hover {{ background: {p.scrollbar_hover}; }}"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
                "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
                " { background: none; }")
        for btn in self._tool_buttons:
            btn.setStyleSheet(_board_btn_qss(p))
            btn.setIcon(qta.icon(btn._icon_name, color=p.text_primary))
        divider = getattr(self, "_tool_divider", None)
        if divider is not None:
            divider.setStyleSheet(
                f"QFrame {{ background: {p.line}; border: none; }}")
        lock_btn = getattr(self, "_lock_btn", None)
        if lock_btn is not None:
            lock_btn.setStyleSheet(
                f"QPushButton {{ background: {p.surface}; border: 1px solid {p.line};"
                f" border-radius: 9px; }}"
                f"QPushButton:hover {{ background: {p.btn_hover};"
                f" border-color: {SiColors.THEME}; }}")
            # 图标表达"点击后会发生什么"：锁定态下显示开锁（点它解锁）
            lock_btn.setIcon(qta.icon("mdi.lock-open-variant", color=SiColors.THEME))
        # 卡片与状态点
        grid = getattr(self, "_grid", None)
        if grid is not None:
            for i in range(grid.count()):
                card = grid.itemAt(i).widget()
                if card is not None:
                    card.retheme()
        if self._collapsed:
            self._sync_dots()
        # 图钉的选中态由置顶开关决定：统一按钮样式之后再单独覆盖
        self._refresh_pin_toggle()

    def _apply_opacity(self, *, animate: bool = False) -> None:
        """按「悬停恢复」状态设置窗口透明度。

        animate=True 只给鼠标进出用：从 30% 瞬间跳到 100% 太突兀，用一段
        短过渡；拖动透明度滑块、切换置顶重建窗口等场景必须即时，否则
        滑块跟手性会被动画拖慢。
        """
        # 管理对话框打开时预览透明度：暂停「悬停恢复不透明」，
        # 否则鼠标移到对话框上经过挂件会把预览值顶回 1.0
        previewing = self.property("previewing")
        opaque = self._force_opaque and not previewing
        target = 1.0 if opaque else self._opacity
        if not animate or not self.isVisible():
            self._stop_opacity_anim()
            self.setWindowOpacity(target)
            return
        self._animate_window_opacity(target)

    def _stop_opacity_anim(self) -> None:
        anim = getattr(self, "_opacity_anim", None)
        if anim is not None:
            anim.stop()

    def _animate_window_opacity(self, target: float) -> None:
        """悬停恢复/移开回落：从当前值短动画过渡到目标值。"""
        current = self.windowOpacity()
        if abs(current - target) < 0.005:
            self._stop_opacity_anim()
            return
        anim = getattr(self, "_opacity_anim", None)
        if anim is None:
            anim = QPropertyAnimation(self, b"windowOpacity", self)
            anim.setDuration(_OPACITY_FADE_MS)
            anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
            self._opacity_anim = anim
        anim.stop()
        anim.setStartValue(current)
        anim.setEndValue(target)
        anim.start()

    def set_opacity(self, value: float, *, persist: bool = True) -> None:
        """设定透明度；persist=True 时同步落盘（滑块拖动过程每步都落盘）。"""
        self._opacity = _clamp_opacity(value)
        if persist:
            board_store.set_opacity(self._opacity)
        self._apply_opacity()

    def opacity(self) -> float:
        return self._opacity

    def set_previewing(self, previewing: bool) -> None:
        """管理对话框打开/关闭时调用：预览期间暂停「悬停恢复不透明」。"""
        self.setProperty("previewing", bool(previewing))
        self._apply_opacity()

    def enterEvent(self, event) -> None:  # noqa: N802
        # 鼠标进入即恢复不透明：半透明时文字难读、看不清开关状态
        self._force_opaque = True
        self._apply_opacity(animate=True)
        self._refresh_lock_btn()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._force_opaque = False
        self._apply_opacity(animate=True)
        btn = getattr(self, "_lock_btn", None)
        if btn is not None:
            btn.hide()
        super().leaveEvent(event)

    # ---------- 折叠 / 展开 ----------

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def _collapse(self) -> None:
        if self._collapsed:
            return
        self._swap_to_collapsed()
        board_store.set_collapsed(True)

    def _expand(self) -> None:
        if not self._collapsed:
            return
        self._swap_to_expanded()
        board_store.set_collapsed(False)

    def toggle_collapse(self) -> None:
        """托盘菜单「收起挂件」用：折叠时展开，已折叠直接切换。"""
        if self._collapsed:
            self._expand()
        else:
            self._collapse()

    def _swap_to_collapsed(self) -> None:
        """收起为细横条：**宽度锁定为展开态宽度**。

        曾经这里按「设备台数」逐台加宽（每台 14px），1 列摆放时设备一多，
        横条会比展开态宽出好几倍，把桌面分区撑开（20 台时 497px vs 188px）。
        现在宽度只由列数决定，装不下的设备信息在横条内部降级表达
        （缩小状态点 → 聚合比例条），见 _sync_dots。
        """
        geo = self.geometry()
        self._expanded_geom = (geo.x(), geo.y(), self._board_width(), self._board_height())
        self._root.hide()
        self._bar.show()
        self._collapsed = True
        # 先摘掉上一轮的状态行再 resize：旧聚合条/计数 label 的
        # minimumWidth 会把 resize 顶大（单排 1 张时 148 被顶成 165），
        # 之后再按新宽度重建状态行
        self._clear_dots()
        # 布局最小宽会被 Qt 回写到 minimumSize() 且延迟生效：旧状态行
        # （聚合条+计数）的最小宽 165 会钳制这里的 resize，必须先重置
        self.setMinimumSize(0, 0)
        self.resize(self._board_width(), _COLLAPSE_HEIGHT)
        self._sync_dots()
        # 折叠/展开不淡变透明度：windowOpacity 是用户设定值，动画会
        # 在中途覆盖它并在结束时跳回，观感是一次闪烁
        self._apply_opacity()

    def _swap_to_expanded(self) -> None:
        self._bar.hide()
        self._root.show()
        self._collapsed = False
        self._apply_expanded_geometry()
        # 折叠期间主窗口推送设备清单时走的是 _sync_dots（不建卡），
        # 展开必须按最新清单重建一次，否则会停在构造期那份空网格上
        # ——用户看到的就是「收着有状态点、展开却说还没有添加设备」。
        self._rebuild()
        self._apply_opacity()

    # ---------- 自绘悬浮提示 ----------

    def _install_hint_filters(self) -> None:
        """给所有带 tooltip 的子控件挂上事件过滤器，改用自绘提示。

        原生 QToolTip 在挂件这种「半透明 + 多层自定义样式」的窗口里不可控：
        实测它会被渲染成全透明（alpha=0）或落到系统深色样式上，出现黑底深字。
        自绘一个 ToolTip 型小窗，配色完全由当前调色板决定，深浅主题都不会错。
        """
        for widget in self.findChildren(QWidget):
            if widget is not self and widget.toolTip():
                widget.installEventFilter(self)

    def _ensure_hint(self):
        hint = getattr(self, "_hint", None)
        if hint is None:
            hint = _FloatingHint(None)
            hint.hide()
            self._hint = hint
        return hint

    def _show_hint(self, text: str, global_pos) -> None:
        hint = self._ensure_hint()
        p = self._palette
        hint.set_colors(p.window_bg, p.text_primary, p.line)
        hint.setText(text)
        hint.adjustSize()
        hint.move(global_pos + QPoint(12, 18))
        hint.show()
        hint.raise_()

    def _hide_hint(self) -> None:
        hint = getattr(self, "_hint", None)
        if hint is not None:
            hint.hide()

    # ---------- 面板上拖拽排序 ----------

    def _card_drag_event(self, card, key: tuple[str, str], event) -> bool:
        """按住卡片横/纵向拖动 → 调整顺序；松手后落盘并重建。

        窗口拖动只发生在标题行、面板空白与折叠横条上，卡片区域本来就是空闲的，
        所以这里不会和"移动窗口"打架。锁定状态下禁用（锁定=冻结布局）。
        """
        if self._locked:
            return False
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            self._reorder_state = {
                "key": key,
                "origin": event.globalPosition().toPoint(),
                "moved": False,
            }
            return False      # 让卡片保留按下反馈
        if etype == QEvent.Type.MouseMove:
            state = getattr(self, "_reorder_state", None)
            if not state or state["key"] != key:
                return False
            pos = event.globalPosition().toPoint()
            if not state["moved"]:
                if (pos - state["origin"]).manhattanLength() < _REORDER_THRESHOLD:
                    return False
                state["moved"] = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                # 让"正在被拖"看得见：源卡片变半透明 + 一个跟随光标的浮动标签
                self._set_card_lifted(card, True)
                self._show_drag_ghost(self._card_title(key), pos)
            else:
                self._move_drag_ghost(pos)
            self._highlight_drop_target(pos, key)
            return True       # 拖动期间吞掉移动，避免卡片自身抖动
        if etype == QEvent.Type.MouseButtonRelease:
            state = getattr(self, "_reorder_state", None)
            self._reorder_state = None
            if not state or state["key"] != key:
                return False
            if not state["moved"]:
                return False  # 普通点击：交给卡片（打开详情）
            target = self._card_key_at(event.globalPosition().toPoint(), exclude=key)
            self._clear_drop_highlight()
            self._set_card_lifted(card, False)
            self._hide_drag_ghost()
            self.unsetCursor()
            if target is not None:
                self._move_item(key, target)
            return True       # 吞掉松手，避免又触发"打开详情"
        return False

    def _card_title(self, key: tuple[str, str]) -> str:
        kind, ident = key
        if kind == "device":
            card = getattr(self, "_cards", {}).get(ident)
            return card._name.text() if card is not None else "设备"
        for scene in self._scenes:
            if scene["scene_id"] == ident:
                return str(scene.get("name") or "场景")
        return "场景"

    def _set_card_lifted(self, card, lifted: bool) -> None:
        """拖动中的源卡片压暗，明确"它正被拿起"。"""
        if card is None:
            return
        if lifted:
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            eff = QGraphicsOpacityEffect(card)
            eff.setOpacity(0.45)
            card.setGraphicsEffect(eff)
        else:
            card.setGraphicsEffect(None)

    def _ensure_drag_ghost(self):
        ghost = getattr(self, "_drag_ghost", None)
        if ghost is None:
            ghost = _FloatingHint(None)
            ghost.hide()
            self._drag_ghost = ghost
        return ghost

    def _show_drag_ghost(self, text: str, global_pos) -> None:
        """跟随光标的"卡片影子"——拖动时唯一能立刻看见的反馈。"""
        ghost = self._ensure_drag_ghost()
        p = self._palette
        ghost.set_colors(p.window_bg, p.text_primary, SiColors.THEME)
        ghost.setText(text)
        ghost.adjustSize()
        ghost.move(global_pos + QPoint(14, 14))
        ghost.show()
        ghost.raise_()

    def _move_drag_ghost(self, global_pos) -> None:
        ghost = getattr(self, "_drag_ghost", None)
        if ghost is not None and ghost.isVisible():
            ghost.move(global_pos + QPoint(14, 14))

    def _hide_drag_ghost(self) -> None:
        ghost = getattr(self, "_drag_ghost", None)
        if ghost is not None:
            ghost.hide()

    def _card_key_at(self, global_pos, *, exclude=None):
        """落点处的卡片 key；用于计算拖拽目标。"""
        for key, card in self._all_cards().items():
            if key == exclude:
                continue
            local = card.mapFromGlobal(global_pos)
            if card.rect().contains(local):
                return key
        return None

    def _all_cards(self) -> dict:
        out = {("device", did): card for did, card in getattr(self, "_cards", {}).items()}
        out.update({("scene", sid): card
                    for sid, card in getattr(self, "_scene_cards", {}).items()})
        return out

    def _highlight_drop_target(self, global_pos, source_key) -> None:
        target = self._card_key_at(global_pos, exclude=source_key)
        if target == getattr(self, "_drop_target_key", None):
            return
        self._clear_drop_highlight()
        self._drop_target_key = target
        if target is None:
            return
        card = self._all_cards().get(target)
        if card is not None:
            # 1px 太弱（用户实测"看不出在拖"）：2px 描边 + 一层很淡的主题色底
            tint = _rgba(QColor(SiColors.THEME), 0.14)
            card.setStyleSheet(
                card.styleSheet()
                + f"QFrame#boardCard {{ border: 2px solid {SiColors.THEME};"
                + f" background: {tint}; }}")

    def _clear_drop_highlight(self) -> None:
        target = getattr(self, "_drop_target_key", None)
        self._drop_target_key = None
        if target is None:
            return
        card = self._all_cards().get(target)
        if card is not None and hasattr(card, "retheme"):
            card.retheme()

    def _move_item(self, source_key: tuple[str, str], target_key: tuple[str, str]) -> None:
        """把 source 挪到 target 的位置（其余顺次平移），落盘并重建。

        位置按**拖动前**的下标算：先记下 source/target 原始下标，移除 source 后
        再插回 target 的原下标。这样"往前拖"和"往后拖"都能得到直觉上的交换效果
        （直接用移除后的 target 下标会漏掉一处偏移，导致相邻两格拖不动）。
        """
        ordered = [(("device", obj.did) if kind == "device"
                    else ("scene", obj["scene_id"]))
                   for kind, obj in self._content_items()]
        if source_key not in ordered or target_key not in ordered:
            return
        src_idx = ordered.index(source_key)
        tgt_idx = ordered.index(target_key)
        ordered.pop(src_idx)
        ordered.insert(tgt_idx, source_key)
        new_order = [{"type": t, "id": i} for t, i in ordered]
        # 保留未渲染的条目（例如设备暂时不在列表里），附在末尾不丢
        known = {tuple((e["type"], e["id"])) for e in new_order}
        for entry in self._order:
            key = (entry.get("type"), entry.get("id"))
            if key not in known:
                new_order.append({"type": key[0], "id": key[1]})
                known.add(key)
        self._order = new_order
        board_store.save_order(new_order)
        self._rebuild()

    # ---------- 锁定 ----------

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool, *, persist: bool = True) -> None:
        """切换锁定：隐藏标题栏、禁止拖动、形态固定（不能收起/展开）。

        解锁必须留有独立通道（悬停浮锁 / 右键菜单 / 托盘菜单），
        否则一旦锁定、标题栏也被隐藏，用户就再也点不到任何入口。
        """
        locked = bool(locked)
        if locked == self._locked:
            if persist:
                board_store.set_locked(locked)
            return
        self._locked = locked
        if persist:
            board_store.set_locked(locked)
        self._apply_locked()

    def _apply_locked(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is not None:
            handle.setVisible(not self._locked)
            # 关键：隐藏标题栏后必须先把布局的最小高度重算一遍再 resize。
            # 否则内外两层布局都还按"标题栏在"缓存着最小高度（+28px），
            # resize 会被夹回去——锁定后窗口留在原高度，顶部省下的空间全
            # 跑到卡片下方，看起来就是"底部多了一条空行"（用户实测反馈）。
            self._activate_layout(self._root)
            self._activate_layout(self)
        if self._locked:
            # 悬停浮锁是"可发现"的那条解锁路，锁定后立刻给一次提示
            self._lock_btn.setToolTip("解锁")
        if not self._collapsed:
            self.resize(self._board_width(), self._board_height())
            self._remember_geometry()
        # 锁定后不允许停在"拖动中"的中间态
        self._press_origin = None
        self.unsetCursor()
        # 若光标此刻已在面板上（例如从管理窗勾选锁定），浮锁要立刻可见，
        # 不能等用户移出再移入才触发 enterEvent
        self._refresh_lock_btn()
        self._position_lock_btn()

    @staticmethod
    def _activate_layout(widget) -> None:
        """让布局的最小尺寸立刻反映控件显隐（不等下一轮事件循环）。"""
        lay = widget.layout() if widget is not None else None
        if lay is not None:
            lay.invalidate()
            lay.activate()

    def _position_lock_btn(self) -> None:
        """把浮锁按钮贴在当前可见面板的右上角。"""
        if not hasattr(self, "_lock_btn"):
            return
        host = self._bar if self._collapsed else self._root
        if host is None or not host.isVisible():
            return
        size = self._lock_btn.width()
        self._lock_btn.move(
            max(0, host.geometry().right() - size - 2),
            max(0, host.geometry().top() + 2))
        self._lock_btn.raise_()

    def _refresh_lock_btn(self) -> None:
        """只在锁定且鼠标悬停时显示浮锁（平时不干扰观感）。"""
        btn = getattr(self, "_lock_btn", None)
        if btn is None:
            return
        if not self._locked:
            btn.hide()
            return
        btn.setVisible(self.underMouse())
        if btn.isVisible():
            self._position_lock_btn()

    # ---------- 折叠横条的状态表达 ----------

    def _relayout_bar(self) -> None:
        """状态区内容变化后强制重排横条布局。

        Qt 在隐藏控件后不会自动重排已有项的几何：隐藏标题时若不手动
        activate，标题仍占着原位置，状态区就和它叠在一起（实测截图如此）。
        """
        self._dot_host.updateGeometry()
        lay = self._bar.layout()
        if lay is not None:
            lay.activate()

    def _dots_area_width(self, *, title: bool) -> int:
        """状态区可用宽度：总宽 - 固定开销 - 间距 -（标题宽）。"""
        spaces = 6 * (3 if title else 2)
        title_w = self._bar_title.sizeHint().width() if title else 0
        return max(0, self.width() - _BAR_FIXED_W - spaces - title_w)

    def _dot_plan(self, count: int):
        """挑一套排布：(点径, 间距, 是否保留标题)；None 表示该走聚合条。

        先把所有点径按"能保留标题"筛一遍取最大档，全都放不下才退到
        无标题方案。不能逐级"先试带标题再试不带"——第一档无标题刚好
        放得下就返回的话，更小点径 + 标题的方案永远轮不到，标题白白
        被牺牲（16 台设备时就是这样：9px 无标题塞得下，5px + 标题也塞得下）。
        """
        for dot, gap in _DOT_STEPS:
            need = count * (dot + gap) - gap
            if need <= self._dots_area_width(title=True):
                return dot, gap, True
        for dot, gap in _DOT_STEPS:
            need = count * (dot + gap) - gap
            if need <= self._dots_area_width(title=False):
                return dot, gap, False
        return None

    @staticmethod
    def _bar_tooltip(items: list) -> str:
        """折叠横条的明细 tooltip：聚合模式下这是唯一的逐台信息入口。"""
        lines = []
        for dev, state, online in items:
            if not online:
                text = "离线"
            elif state is True:
                text = "开"
            elif state is False:
                text = "关"
            else:
                text = "状态未知"
            lines.append(f"{dev.name}（{dev.room_name}）· {text}")
        if len(lines) > 20:
            lines = lines[:20] + [f"… 另有 {len(lines) - 20} 台"]
        return "\n".join(lines)

    def _collapsed_items(self) -> list:
        lookup = {d.did: d for d in self._devices}
        items = []
        for did in self._dids:
            dev = lookup.get(did)
            if dev is None:
                continue
            items.append((dev, self._known_power.get(did), dev.online))
        return items

    def _clear_dots(self) -> None:
        """摘掉折叠横条上一轮的状态行（状态点/聚合条/计数）。

        先摘掉父子关系再 deleteLater：只是移出布局的话，旧控件
        （几何还是旧宽度）会继续画在新内容上面，表现为「换成小圆点后
        屏幕上却是一排长条」。另外旧控件的 minimumWidth 会把之后的
        resize 顶大（单排 1 张时 148 被顶成 165），折叠/改列数前必须先清。
        """
        while self._dot_lay.count():
            item = self._dot_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _sync_dots(self) -> None:
        """重建折叠横条的状态表达：逐台状态点，太多则降级为聚合比例条。

        降级顺序（宽度固定，绝不撑宽窗口）：
            9px 点 → 7px → 5px（每级都先试着保留标题，再牺牲标题）
            → 仍放不下：一条「开/关/离线」比例段 + "n/m 开" 计数，
              逐台明细进 tooltip。
        """
        import shiboken6

        self._clear_dots()

        items = self._collapsed_items()
        self._bar.setToolTip(self._bar_tooltip(items))
        if not items:
            self._bar_title.setVisible(True)
            self._dot_lay.setSpacing(5)
            self._dot_lay.addStretch(1)
            self._relayout_bar()
            return

        plan = self._dot_plan(len(items))
        if plan is not None:
            dot, gap, show_title = plan
            self._bar_title.setVisible(show_title)
            self._dot_lay.setSpacing(gap)
            radius = max(2, dot // 2)
            for dev, state, online in items:
                point = QLabel()
                point.setFixedSize(dot, dot)
                point.setAlignment(Qt.AlignmentFlag.AlignCenter)
                if not online:
                    color, border = "transparent", self._palette.offline_sub
                elif state is True:
                    color, border = SiColors.THEME, SiColors.THEME
                elif state is False:
                    color, border = self._palette.state_off, self._palette.state_off
                else:
                    color, border = "transparent", self._palette.state_unknown_border
                point.setStyleSheet(
                    f"QLabel {{ background: {color}; border: 1px solid {border};"
                    f" border-radius: {radius}px; }}")
                self._dot_lay.addWidget(point)
            self._dot_lay.addStretch(1)
            self._relayout_bar()
            return

        # 聚合模式：比例段（逐台明细在 tooltip 里）
        self._bar_title.setVisible(self.width() >= _BAR_TITLE_MIN_W)
        self._dot_lay.setSpacing(6)
        bar = _AggregateBar(self._dot_host)
        bar.set_states([(state, online) for _dev, state, online in items],
                       self._palette)
        self._dot_lay.addWidget(bar, stretch=1)
        self._relayout_bar()

    # ---------- 数据 ----------

    def set_devices(self, devices: list[DeviceInfo],
                    known_power: dict[str, bool | None]) -> None:
        """由主窗口在设备列表刷新 / 开关状态变化后推送。"""
        self._devices = list(devices)
        self._known_power = dict(known_power)
        self._dids = board_store.load_devices()
        if self._collapsed:
            # 设备数量变化会改变状态行内容，横条宽度要重新锚到展开宽度
            # （先清旧状态行，否则其最小宽会把 resize 顶大）
            self._clear_dots()
            self.setMinimumSize(0, 0)
            self.resize(self._board_width(), _COLLAPSE_HEIGHT)
            self._sync_dots()
        else:
            self._rebuild()

    def set_metrics(self, metrics: dict[str, str | None]) -> None:
        """主窗口温湿度读数同步：就地刷新可见卡片副标题。

        读数始终记进内存（隐藏时也要记），下次显示由 showEvent 补回，
        否则挂件隐藏期间更新的读数会一直显示旧值。
        """
        import shiboken6

        if not shiboken6.isValid(self):
            return
        self._metrics = dict(metrics)
        if self._collapsed or not self.isVisible():
            return
        self._refresh_subtitles()

    def _refresh_subtitles(self) -> None:
        """按当前设备/读数重算所有卡片副标题（房间 | 温湿度）。"""
        lookup = {d.did: d for d in self._devices}
        for did, card in getattr(self, "_cards", {}).items():
            dev = lookup.get(did)
            if dev is None:
                continue
            metrics = self._metrics.get(did)
            card.set_subtitle(
                f"{dev.room_name} · 离线" if not dev.online
                else (f"{dev.room_name} | {metrics}" if metrics else dev.room_name))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 显示时把隐藏期间累积的读数补回卡片
        self._refresh_subtitles()
        # show 之后窗口默认落在最前，等这一轮事件处理完再按置顶开关归位
        QTimer.singleShot(0, self._apply_z_order)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        # 非置顶时窗口被激活（用户点了挂件）会被系统提到前面，立刻压回
        # 底层；置顶时 WindowStaysOnTopHint 已经保证层级，不需要处理
        if (event.type() == QEvent.Type.ActivationChange
                and self.isActiveWindow() and not self._topmost):
            QTimer.singleShot(0, self._apply_z_order)

    def tracked_dids(self) -> list[str]:
        """挂件上放着的设备 did（不含场景），供主窗口纳入轮询。"""
        return list(self._dids)

    def hide_by_user(self) -> None:
        """用户主动隐藏（点 ×）。

        只是本次收起：是否随应用启动显示由设置里的「桌面挂件」开关决定
        （board_store.enabled），这里不改任何持久化状态，所以重启后
        开关仍开着就还会出现。
        """
        self.hide()

    def refresh_power(self, dids: list[str]) -> None:
        """补拉尚未确认开关能力的设备状态（与托盘窗口同一套串行队列）。"""
        dids = [d for d in dids if d and self._known_power.get(d) is None]
        if not dids:
            return
        # 快照版本号：回来时若用户已经点过这台设备，丢弃这条旧结果
        snapshot = dict(self._power_gen)
        self._jobs.submit(
            lambda: self._service.power_states(dids),
            on_success=lambda states, snap=snapshot: self._apply_power(states, snap),
            on_error=lambda _: None,
        )

    def _apply_power(self, states: dict[str, bool | None],
                     snapshot: dict[str, int] | None = None) -> None:
        import shiboken6

        if not shiboken6.isValid(self):
            return
        states = {d: s for d, s in states.items()
                  if snapshot is None
                  or snapshot.get(d, 0) == self._power_gen.get(d, 0)}
        self._known_power.update(states)
        if self._collapsed:
            self._sync_dots()
            return
        for did, state in states.items():
            card = getattr(self, "_cards", {}).get(did)
            if card is not None and shiboken6.isValid(card):
                card.set_power_state(state)

    # ---------- 卡片网格 ----------

    def _content_items(self) -> list[tuple[str, object]]:
        """按混排顺序返回挂件内容：("device", DeviceInfo) / ("scene", dict)。

        order 是唯一顺序来源；order 里没登记、但确实选中的条目补到末尾
        （旧数据升级、order 写坏时不至于丢内容）。设备信息来自推送的
        设备列表，场景信息来自 board.json 的快照，所以断网也能显示。
        """
        dev_map = {d.did: d for d in self._devices}
        scene_map = {s["scene_id"]: s for s in self._scenes}
        items: list[tuple[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for entry in self._order:
            t, i = entry.get("type"), entry.get("id")
            key = (t, i)
            if key in seen:
                continue
            if t == "device" and i in dev_map:
                items.append(("device", dev_map[i]))
                seen.add(key)
            elif t == "scene" and i in scene_map:
                items.append(("scene", scene_map[i]))
                seen.add(key)
        for did in self._dids:
            if ("device", did) not in seen and did in dev_map:
                items.append(("device", dev_map[did]))
                seen.add(("device", did))
        for scene in self._scenes:
            sid = scene["scene_id"]
            if ("scene", sid) not in seen:
                items.append(("scene", scene))
                seen.add(("scene", sid))
        return items

    def _rebuild(self) -> None:
        self._destroy_grid()
        # 每次重建都读最新清单：管理对话框保存或改列数后，内存里的
        # _dids 可能是旧的，只有读盘才不会用过期清单排版
        self._dids = board_store.load_devices()
        self._scenes = board_store.load_scenes()
        self._order = board_store.load_order()
        items = self._content_items()
        # 标题带产品名：桌面上可能同时开着一堆挂件，得能认出这是谁家的；
        # 窄挂件放不下全名，按剩余宽度省略（完整名在 tooltip 里）
        self._update_title(len(items))
        if not items:
            self._empty.show()
            self._scroll.hide()
            return
        self._empty.hide()
        self._scroll.show()
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(_GRID_SPACING)
        grid.setVerticalSpacing(_GRID_SPACING)
        for c in range(self._columns):
            grid.setColumnStretch(c, 1)
        self._cards = {}
        self._scene_cards = {}
        for idx, (kind, obj) in enumerate(items):
            if kind == "device":
                card = _BoardCard(self, obj, self._known_power.get(obj.did))
                self._cards[obj.did] = card
                key = ("device", obj.did)
            else:
                card = _SceneCard(self, obj)
                self._scene_cards[obj["scene_id"]] = card
                key = ("scene", obj["scene_id"])
            # 面板上按住卡片拖动 = 排序（窗口拖动只发生在标题行/空白/折叠条，
            # 与卡片拖动不冲突），把 key 挂在控件上供 eventFilter 识别
            card._reorder_key = key
            card.installEventFilter(self)
            grid.addWidget(card, idx // self._columns, idx % self._columns)
        self._grid = grid
        self._list_lay.addLayout(grid)
        self._list_lay.addStretch(1)
        self._install_hint_filters()
        if not self._collapsed:
            # 列数/设备数变化后重算尺寸并记住展开态几何（拖动位置保持不变）
            pos = self.geometry().topLeft()
            self._expanded_geom = (pos.x(), pos.y(),
                                   self._board_width(), self._board_height())
            # 旧布局（宽卡片 + 多列）的 minimumSizeHint 会残留并钳制 resize：
            # 从多列切到单列时窗口缩不回去，必须先把最小尺寸清掉再重设
            self.setMinimumSize(0, 0)
            self._host.setMinimumSize(0, 0)
            self._host.updateGeometry()
            self.resize(self._board_width(), self._board_height())
            # 布局事件（滚动条出现、标题栏最小宽）可能在 resize 之后把
            # 窗口撑回旧宽度，下一轮事件循环再校一次
            QTimer.singleShot(0, self._enforce_target_width)
            self._save_timer.start()
        online = [d.did for d in self._devices
                  if d.online and self._known_power.get(d.did) is None]
        if online:
            self.refresh_power(online)

    def _enforce_target_width(self) -> None:
        """按目标宽度校正窗口：布局把窗口撑大时再收一次。

        滚动条出现、标题栏/卡片最小宽都可能让 resize 之后的布局事件
        把窗口顶回旧宽度；单列这种窄布局最明显，表现为「切列数后卡片
        还是旧宽度，收起展开才恢复」。宽度已经正确时不做任何事。
        """
        import shiboken6

        if (not shiboken6.isValid(self) or self._collapsed
                or not self.isVisible()):
            return
        target = self._board_width()
        if self.width() != target:
            self.resize(target, self._board_height())

    def _update_title(self, count: int) -> None:
        """标题 = 产品名 + 数量，按窗口宽度省略；窄到读不出字就整块收起。

        单列窗口（168px）里工具钮已经占掉大半，标题只剩十几个像素，
        强行省略只会留下一截残影，不如让位给按钮、保持标题行干净。
        """
        full = f"{_BOARD_TITLE} · {count}"
        # 各工具钮 22px + 分隔线/间距约 20 + 根面板边距，给标题留余量
        reserved = sum(b.width() for b in self._tool_buttons) + 34
        avail = self._board_width() - reserved
        if avail < 48:
            self._title.hide()
            return
        self._title.show()
        self._title.setText(
            self._title.fontMetrics().elidedText(full, Qt.ElideRight, avail))

    def _destroy_grid(self) -> None:
        grid = getattr(self, "_grid", None)
        if grid is None:
            return
        for i in range(self._list_lay.count()):
            item = self._list_lay.itemAt(i)
            if item and item.layout() is grid:
                self._list_lay.takeAt(i)
                break
        while grid.count():
            w = grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        grid.deleteLater()
        self._grid = None
        self._cards = {}
        # 移除多余 stretch，避免反复重建后列表底部空出间距
        for i in range(self._list_lay.count() - 1, -1, -1):
            item = self._list_lay.itemAt(i)
            if item and item.spacerItem():
                self._list_lay.takeAt(i)
                break

    def set_columns(self, value: int) -> None:
        """设定单排最多放几张卡片（1–9），越界值落库时钳制。"""
        self._columns = min(max(int(value), 1), 9)
        board_store.set_columns(self._columns)
        if self._collapsed:
            # 折叠态宽度锚定展开宽度：改列数时横条要跟着变，
            # 否则管理窗里调完列数、收起后宽度还对不上。
            # resize 前先清掉旧状态行（其 minimumWidth 会顶大 resize）
            self._clear_dots()
            self.resize(self._board_width(), _COLLAPSE_HEIGHT)
            self._sync_dots()
            return
        self._rebuild()

    def columns(self) -> int:
        return self._columns

    # ---------- 交互 ----------

    def _toggle_power(self, did: str, btn) -> None:
        """点开关：已知状态时直接写反值（1 次往返）+ 乐观翻转按钮。"""
        current = self._known_power.get(did)
        if current is None:
            # 状态未知（首次探测还没回来）：只能让服务端读一次再写。
            # 仍按用户操作优先级排队，避免排在轮询后面
            btn.set_busy(True)
            self._jobs.submit(
                lambda: self._service.toggle_power(did),
                on_success=lambda ns, d=did, b=btn: self._on_power_done(d, ns, b),
                on_error=lambda e, b=btn: b.set_busy(False),
                priority=PRIORITY_CONTROL,
            )
            return
        target = not current
        # 乐观 UI：立刻翻转，用户无需等网络；失败再回滚
        self._bump_power_gen(did)
        self._known_power[did] = target
        btn.set_state(target)
        btn.set_busy(True)
        self._jobs.submit(
            lambda: (self._service.set_power_state(did, target), target)[1],
            on_success=lambda ns, d=did, b=btn: self._on_power_done(d, ns, b),
            on_error=lambda e, d=did, b=btn, c=current: self._on_power_failed(d, c, b, e),
            priority=PRIORITY_CONTROL,
        )

    def _on_power_done(self, did: str, new_state: bool, btn) -> None:
        import shiboken6

        self._bump_power_gen(did)
        self._known_power[did] = new_state
        if shiboken6.isValid(btn):
            btn.set_state(new_state)
            btn.set_busy(False)
        self.power_changed.emit(did, new_state)
        if self._collapsed:
            self._sync_dots()

    def _on_power_failed(self, did: str, revert_to: bool, btn, error: Exception) -> None:
        """写入失败：把乐观翻转的按钮回滚，并提示一次原因。"""
        import shiboken6

        self._bump_power_gen(did)
        self._known_power[did] = revert_to
        if shiboken6.isValid(btn):
            btn.set_state(revert_to)
            btn.set_busy(False)
        self.power_changed.emit(did, revert_to)
        from app.ui.toast import Toast

        Toast.info(self, f"操作失败：{error}", 4000)

    # ---------- 场景执行 ----------

    def _run_scene(self, scene: dict, btn) -> None:
        """执行场景：点按钮即执行（与托盘快捷窗口一致），执行中按钮置灰。"""
        from app.core.models import SceneInfo

        info = SceneInfo(
            scene_id=str(scene.get("scene_id") or ""),
            name=str(scene.get("name") or ""),
            home_id=str(scene.get("home_id") or ""),
            home_name=str(scene.get("home_name") or ""),
        )
        btn.setEnabled(False)
        self._jobs.submit(
            lambda: self._service.run_scene(info),
            on_success=lambda _r, b=btn: self._on_scene_done(scene, b, None),
            on_error=lambda e, b=btn: self._on_scene_done(scene, b, e),
            priority=PRIORITY_CONTROL,
        )

    def _on_scene_done(self, scene: dict, btn, error: Exception | None) -> None:
        import shiboken6

        from app.ui.toast import Toast

        if shiboken6.isValid(btn):
            btn.setEnabled(True)
        # 成功不弹提示：挂件是常驻面板，点场景后一条 Toast 会糊在卡片上
        # 挡住设备名（用户实测反馈）。失败必须提示，否则用户以为执行了。
        if error is not None:
            Toast.info(self, f"场景执行失败：{error}", 4000)

    def _bump_power_gen(self, did: str) -> None:
        """本地操作过这台设备：让在途的旧轮询结果作废。"""
        self._power_gen[did] = self._power_gen.get(did, 0) + 1

    def apply_power_states(self, states: dict[str, bool | None]) -> None:
        """轮询读到新状态后就地更新卡片按钮，不整窗重建。"""
        import shiboken6

        if not shiboken6.isValid(self):
            return
        self._known_power.update(states)
        if self._collapsed:
            self._sync_dots()
            return
        for did, state in states.items():
            card = getattr(self, "_cards", {}).get(did)
            if card is not None and state is not None and shiboken6.isValid(card):
                card.set_power_state(state)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """标题行 / 面板空白 / 折叠横条的统一鼠标处理。

        三块区域都能拖动窗口，差别只在"没拖动的那一下"：

            标题行   → 收起为细横条
            面板空白 → 什么都不做
            折叠横条 → 展开

        拖动与点击共用左键，靠位移阈值区分：按下记原点，移动超过
        _DRAG_THRESHOLD 才算拖拽（此后松手不触发点击动作），否则松手
        按区域执行各自动作。按下时消费事件，保证后续 move/release
        还能送回这里；卡片与按钮是子控件，会自己消费点击不受影响。

        **必须容错**：installEventFilter 之后 Qt 会立刻投递事件（构造期
        就在跑），此时 _bar / _handle 可能还没建好。Python 异常若从
        Qt 虚函数里抛出，会污染后续 Qt 调用——表现为下一个
        QHBoxLayout(...) 莫名返回 NULL。
        """
        try:
            etype = event.type()
            handle = getattr(self, "_handle", None)
            bar = getattr(self, "_bar", None)
            root = getattr(self, "_root", None)
            is_handle = handle is not None and obj is handle
            is_bar = bar is not None and obj is bar
            is_root = root is not None and obj is root
            if event.type() == QEvent.Type.ToolTip:
                text = obj.toolTip() if hasattr(obj, "toolTip") else ""
                if text:
                    self._show_hint(text, event.globalPos())
                    return True
                return False
            if event.type() == QEvent.Type.Leave:
                self._hide_hint()
            card_key = getattr(obj, "_reorder_key", None)
            if card_key is not None:
                return self._card_drag_event(obj, card_key, event)
            if not (is_handle or is_bar or is_root):
                return super().eventFilter(obj, event)
            if self._locked:
                # 锁定：不拖、不收起、不展开（形态由锁定时固定），
                # 但设备/场景控件是子控件，点击不受影响
                return False

            if etype == QEvent.Type.MouseButtonPress:
                if event.button() != Qt.MouseButton.LeftButton:
                    return False
                self._press_origin = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft())
                self._press_moved = False
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                return True

            if etype == QEvent.Type.MouseMove:
                if (self._press_origin is not None
                        and event.buttons() & Qt.MouseButton.LeftButton):
                    target = event.globalPosition().toPoint() - self._press_origin
                    # 越过阈值才算拖拽：轻微抖动不应把"点击"变成"拖动"
                    if not self._press_moved:
                        if (target - self.pos()).manhattanLength() < _DRAG_THRESHOLD:
                            return True
                        self._press_moved = True
                    self.move(self._clamp_to_screen(target.x(), target.y()))
                    self._remember_geometry()
                elif self._press_origin is not None:
                    # 松手事件丢在窗口外（拖太快）时在这里收尾，避免光标卡住
                    self._press_origin = None
                    self.unsetCursor()
                return True

            if etype == QEvent.Type.MouseButtonRelease:
                if self._press_origin is None:
                    return True
                self._press_origin = None
                self.unsetCursor()
                if self._press_moved:
                    # 拖过：只落盘位置，不触发点击动作
                    self._press_moved = False
                    self._save_geometry()
                    return True
                self._press_moved = False
                # 没拖动 → 视为单击
                if is_bar:
                    self._expand()
                elif is_handle:
                    self._collapse()
                return True
        except Exception:
            # 事件过滤里的意外不得冒泡到 Qt（见上面的说明）
            return False
        return super().eventFilter(obj, event)

    def _clamp_to_screen(self, x: int, y: int) -> QPoint:
        """拖动时限制在工作区可见范围内，避免窗口被拖丢到屏幕外。"""
        screen = QGuiApplication.screenAt(QPoint(x + 40, y + 10)) \
            or QGuiApplication.primaryScreen()
        if screen is None:
            return QPoint(x, y)
        geo = screen.availableGeometry()
        x = min(max(x, geo.left() - self.width() + 60), geo.right() - 60)
        y = min(max(y, geo.top()), geo.bottom() - 20)
        return QPoint(x, y)

    def _remember_geometry(self) -> None:
        """记住展开态几何。

        折叠态拖动横条时只更新位置，宽高仍用展开态的——否则展开后
        会变成细横条的尺寸；这样"折叠着挪个位置再展开"也能落到新地方。
        """
        geo = self.geometry()
        if self._collapsed:
            self._expanded_geom = (geo.x(), geo.y(),
                                   self._board_width(), self._board_height())
            return
        self._expanded_geom = (geo.x(), geo.y(), geo.width(), geo.height())

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._remember_geometry()
        self._save_timer.start()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self._collapsed:
            self._remember_geometry()
        self._position_lock_btn()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._save_geometry()
        self._hide_drag_ghost()
        self._hide_hint()
        super().hideEvent(event)

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setObjectName("appMenu")
        menu.addAction("管理挂件设备…").triggered.connect(self.manage_requested.emit)
        collapse_text = "展开挂件" if self._collapsed else "收起为细横条"
        menu.addAction(collapse_text).triggered.connect(self.toggle_collapse)
        menu.addSeparator()
        menu.addAction("刷新设备").triggered.connect(
            lambda: self.refresh_power(list(self._dids)))
        menu.addSeparator()
        lock_text = "解锁" if self._locked else "锁定"
        menu.addAction(lock_text).triggered.connect(lambda: self.set_locked(not self._locked))
        menu.addAction("隐藏挂件").triggered.connect(self.hide)
        menu.exec(self.mapToGlobal(pos))

    def retheme(self) -> None:
        """主题切换。

        自定义背景色的会话里主题切换不影响挂件底色（用户已经选了固定色），
        但仍然重算一遍配色表：以后新增强调色时不会漏刷。
        """
        self._palette = _BoardPalette(self._bg)
        self._apply_styles()


class _BoardCard(QFrame):
    """挂件里的单台设备卡：名称 + 副标题 + 圆形电源钮。

    与托盘快捷行同款结构与配色，按钮复用 PowerButton，保证两处观感一致。
    """

    def __init__(self, board: WidgetsBoard, dev: DeviceInfo, power: bool | None,
                 parent=None):
        super().__init__(parent)
        self._did = dev.did
        self._device = dev
        self._board = board
        self._online = dev.online
        self.setObjectName("boardCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(_CARD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(8)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self._name = QLabel()
        self._name.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.DemiBold))
        # 水平 Ignored：布局可任意压缩标签，长名称不会把列撑宽
        self._name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name.setToolTip(f"{dev.name} · {dev.room_name}")
        self._sub = QLabel()
        self._sub.setFont(QFont("Microsoft YaHei UI", 8))
        self._sub.setStyleSheet(
            f"color: {self._board.palette.text_secondary}; background: transparent;"
            " font-size: 8pt;")
        self._sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._sub.setText(self._sub_text(dev, board._metrics.get(dev.did)))
        text_col.addWidget(self._name)
        text_col.addWidget(self._sub)
        lay.addLayout(text_col, stretch=1)

        self._update_elided_name()
        self.retheme()
        self._layout = lay
        self._power_btn = self._make_power_btn(power)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """宽度可压缩到 0：卡片宽度由窗口列宽决定，不该反过来钳制窗口。

        名称/副标题标签虽然是 Ignored 策略，但 QLabel 的 minimumSizeHint
        仍是文字宽度，长副标题（如「卧室 | 21.8°C 67%」）会把卡片最小宽
        撑大，单列时窗口就缩不回目标宽度。
        """
        return QSize(0, _CARD_HEIGHT)

    def _make_power_btn(self, power: bool | None):
        """电源钮在探测确认设备有可写开关属性后才创建并插入布局。

        与主窗口 DeviceCard / 托盘快捷行同一时序：列表接口不带开关
        信息，先探测后出现，避免无开关设备留一个永远点不动的按钮。
        """
        if power is None:
            return None
        btn = PowerButton(28, icon_size=22)
        btn.set_state(power)
        btn.set_online(self._online)
        btn.clicked.connect(
            lambda _checked=False, d=self._did, b=btn: self._board._toggle_power(d, b))
        self._layout.addWidget(btn)
        return btn

    def _update_elided_name(self) -> None:
        """按当前宽度省略设备名，完整名称留在 tooltip。

        挂件卡片宽度随列数变化，名称必须单行省略，否则会被硬裁掉。
        """
        # 取标签宽度与卡片剩余宽度的较小值：离屏渲染时标签可能报出
        # 超出卡片的宽度，直接用会把文字算成放得下
        room = self.width() - 10 - 8 - 28 - 8
        width = min(self._name.width(), room)
        if width <= 0:
            # 布局还没结算：先放完整名字，等 resize 再截
            self._name.setText(self._device.name)
            return
        self._name.setText(self._name.fontMetrics().elidedText(
            self._device.name, Qt.TextElideMode.ElideRight, width))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elided_name()

    @staticmethod
    def _sub_text(dev: DeviceInfo, metrics: str | None) -> str:
        if not dev.online:
            return f"{dev.room_name} · 离线"
        return f"{dev.room_name} | {metrics}" if metrics else dev.room_name

    def set_subtitle(self, text: str) -> None:
        self._sub.setText(text)

    def set_power_state(self, state: bool | None) -> None:
        if self._power_btn is None:
            if state is None:
                return
            self._power_btn = self._make_power_btn(state)
            return
        self._power_btn.set_state(state)

    def retheme(self) -> None:
        # 配色由挂件统一给出：跟随主题或用户挑的固定背景色
        p = self._board.palette
        self.setStyleSheet(
            f"QFrame#boardCard {{ background: {p.card};"
            f" border: 1px solid {p.line}; border-radius: 10px; }}"
            f"QFrame#boardCard:hover {{ background: {p.card_hover};"
            f" border-color: {p.card_border_hover}; }}")
        color = p.text_primary if self._online else p.offline_text
        self._name.setStyleSheet(f"color: {color}; background: transparent;")
        sub_color = p.text_secondary if self._online else p.offline_sub
        self._sub.setStyleSheet(
            f"color: {sub_color}; background: transparent; font-size: 8pt;")
        # 构造期在电源钮创建之前也会调 retheme，故用 getattr 兜底
        power_btn = getattr(self, "_power_btn", None)
        if power_btn is not None:
            power_btn._apply()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 电源钮自己消费点击，走到这里的是卡片空白区 → 打开完整详情
        if event.button() == Qt.MouseButton.LeftButton:
            self._board.open_device_requested.emit(self._did)
        super().mouseReleaseEvent(event)


class _SceneCard(QFrame):
    """挂件里的场景卡：名称 + 家庭副标题 + 「执行」圆钮。

    与设备卡同构（同尺寸、同配色），区别只在右侧是播放钮而不是电源钮：
    场景没有开/关状态，点击只意味着"执行一次"。
    """

    def __init__(self, board: "WidgetsBoard", scene: dict, parent=None):
        super().__init__(parent)
        self._sid = str(scene.get("scene_id") or "")
        self._scene = scene
        self._board = board
        self.setObjectName("boardCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(_CARD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(8)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self._name = QLabel(str(scene.get("name") or "场景"))
        self._name.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.DemiBold))
        self._name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._sub = QLabel(str(scene.get("home_name") or "手动场景"))
        self._sub.setFont(QFont("Microsoft YaHei UI", 8))
        self._sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text_col.addWidget(self._name)
        text_col.addWidget(self._sub)
        lay.addLayout(text_col, stretch=1)

        self._run_btn = QPushButton()
        self._run_btn.setObjectName("sceneRunBtn")
        self._run_btn.setFixedSize(28, 28)
        self._run_btn.setIconSize(QSize(18, 18))
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.setToolTip("执行场景")
        self._run_btn.clicked.connect(
            lambda _c=False, b=self._run_btn: self._board._run_scene(self._scene, b))
        lay.addWidget(self._run_btn)
        self.retheme()

    def retheme(self) -> None:
        p = self._board.palette
        self.setStyleSheet(
            f"QFrame#boardCard {{ background: {p.card};"
            f" border: 1px solid {p.line}; border-radius: 10px; }}"
            f"QFrame#boardCard:hover {{ background: {p.card_hover};"
            f" border-color: {p.card_border_hover}; }}")
        self._name.setStyleSheet(
            f"color: {p.text_primary}; background: transparent;")
        self._sub.setStyleSheet(
            f"color: {p.text_secondary}; background: transparent; font-size: 8pt;")
        btn = getattr(self, "_run_btn", None)
        if btn is not None:
            btn.setStyleSheet(
                f"QPushButton {{ background: {SiColors.THEME}; border: none;"
                " border-radius: 14px; }"
                f"QPushButton:hover {{ background: {SiColors.THEME_HOVER}; }}"
                f"QPushButton:disabled {{ background: {SiColors.THEME_DIM}; }}")
            btn.setIcon(qta.icon("mdi.play", color=SiColors.ON_THEME_TEXT))


def _clamp_opacity(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.92
    return round(min(max(v, 0.30), 1.00), 2)