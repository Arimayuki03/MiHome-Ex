# SPDX-License-Identifier: GPL-3.0-or-later
"""CUKTECH 观感件三件套自测：离屏运行，喂假数据断言渲染。

用法: .venv\\Scripts\\python.exe tests/cuktech_stage_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_stage_test）

仿 tests/cuktech_panel_test.py 的离屏 + grab() 模式：构造
DeviceStageWidget / PortCardGrid / MiniPhoneShareBar、灌假
status/port_update 数据，断言素材加载非空、功率条可见（grab 非全
空白）、totalW 计算、增量更新只动对应卡、暗/亮双主题渲染与 retheme
不崩。零网络请求（组件本身纯展示，数据由测试直接喂）。
"""

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体，指向系统字体目录让中文文本真实渲染
# （cuktech_panel_test.py 头注有完整原因说明）
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_stage_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

__test__ = False  # 脚本式测试：python 直接执行，pytest 收集会误报

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.ui import si_theme
from app.ui.si_theme import SiColors
from app.ui.theme_service import apply_theme

# ---------------------------------------------------------------------------
# 假数据：结构与 cuktech-api.md 契约一致（ports 键 "1"-"4"）
# ---------------------------------------------------------------------------

FAKE_STATUS = {
    "connected": True,
    "ports": {
        "1": {"voltage": 20.0, "current": 3.25, "power": 65.0,
              "active": True, "protocol": "PD", "enabled": True},
        "2": {"voltage": 0.0, "current": 0.0, "power": 0.0,
              "active": False, "protocol": "idle", "enabled": False},
        "3": {"voltage": 9.0, "current": 1.0, "power": 9.0,
              "active": True, "protocol": "QC", "enabled": True},
        "4": {"voltage": 5.0, "current": 0.3, "power": 1.5,
              "active": True, "protocol": "5V", "enabled": True},
    },
    "settings": {"5": 1, "16": 15},
}

EMPTY_STATUS = {"connected": False, "ports": {}, "settings": {}}


def _image_is_blank(image) -> bool:
    """空白判定：全图非背景像素 <= 16 视为空白（cuktech_panel_test 同款）。"""
    w, h = image.width(), image.height()
    counts: dict[str, int] = {}
    for x in range(w):
        for y in range(h):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    non_bg = w * h - max(counts.values())
    return non_bg <= 16


# ---------- 1. 素材加载：资源定位正确、QPixmap 非空 ----------

def test_assets() -> None:
    from app.ui import cuktech_stage as cs

    names = [
        "main_card_ad1204u_unconnected.png",
        "main_charger_dark_ad1204_all.png",
        "main_card_usb_c_rectangle.png",
        "main_card_usb_a_rectangle.png",
        "main_card_scene_icon_ai.png",
        "main_card_scene_icon_apple.png",
        "main_card_scene_icon_single.png",
        "main_card_scene_icon_balance.png",
        "main_card_port_c1_on.png", "main_card_port_c1_off.png",
        "main_card_port_c2_on.png", "main_card_port_c2_off.png",
        "main_card_port_c3_on.png", "main_card_port_c3_off.png",
        "main_card_port_a_on.png", "main_card_port_a_off.png",
    ]
    for name in names:
        pm = cs._asset_pixmap(name)
        assert not pm.isNull(), f"素材加载失败: {cs._asset(name)}"
    # resource_path 定位到真实文件
    path = Path(cs._asset("main_card_port_c1_on.png"))
    assert path.exists(), f"资源定位错误: {path}"
    print("1. 素材加载/资源定位 OK（16 张全部非空）")


# ---------- 2. DeviceStageWidget：充电中渲染 / totalW / 功率条可见 ----------

def test_stage_charging(theme: str) -> None:
    from app.ui.cuktech_stage import DeviceStageWidget

    apply_theme(theme)
    stage = DeviceStageWidget()
    stage.resize(360, 328)
    stage.update_state(dict(FAKE_STATUS))
    app.processEvents()
    # totalW = 65 + 9 + 1.5 = 75.5（enabled 且 power>0）
    assert abs(cs_total(stage) - 75.5) < 1e-6, cs_total(stage)
    assert stage._charging is True
    img = stage.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 充电中舞台渲染空白"
    # 充电中显示的是 all 底图路径（不随主题换）
    stage.hide()
    stage.deleteLater()
    print(f"2. 舞台充电中 totalW=75.5/渲染非空 OK [{theme}]")


def test_stage_no_clip() -> None:
    """窄宽度回归：整块等比缩放后四口条/文本/机身右缘都落在控件内。

    历史版只按宽度 fit，面板把舞台压窄（<360）时 360 逻辑宽直接溢出
    控件被裁（用户截图：机身只剩右半）。修复后按 min(w/360, h/328)
    缩放居中，任意尺寸下画布完整可见。

    底部/顶部不裁（用户截图第二轮：面板双栏把舞台行压到端口卡最小
    高 206，画布矮于 fit 值 → 内容贴顶、底部大片留白；且 unconnected/
    charging 底图丢了盒→画布 51.5px 下沉，机身顶出画布上沿）——
    断言两态下设备内容包围盒完整落在控件内（四边留白 > 2px），以及
    布局压矮场景（resizeEvent 钉 minHeight 后行高 ≥ hfw）不复发。
    """
    from app.ui.cuktech_stage import DeviceStageWidget, _PVAL_X, _PVAL_W

    stage = DeviceStageWidget()
    stage.update_state(dict(FAKE_STATUS))
    for w in (280, 327):
        stage.resize(w, stage.height_for_width(w))
        stage.show()
        app.processEvents()
        scale = min(stage.width() / 360.0, stage.height() / 328.0)
        # 画布縮放后完整落在控件内（左右居中、上下不溢出）
        assert scale < w / 360.0 + 1e-6
        ox = (stage.width() - 360.0 * scale) / 2
        assert ox >= -0.5, (w, ox)
        # 最右元素（功率文本右缘 344.5 盒内）映射到控件坐标仍在界内
        right = ox + (_PVAL_X + _PVAL_W) * scale
        assert right <= stage.width() + 0.5, (w, right, stage.width())
        img = stage.grab().toImage()
        assert not _image_is_blank(img), f"宽度 {w} 渲染空白"

    # 内容包围盒四边不贴控件边（充电中 FAKE_STATUS 与空载 EMPTY 各一遍）
    from app.ui.cuktech_stage import _CANVAS_H, _STAGE_W

    for status in (dict(FAKE_STATUS), dict(EMPTY_STATUS)):
        stage.update_state(status)
        app.processEvents()
        for w, h in ((280, 255), (327, 298), (360, 328)):
            stage.setFixedSize(w, h)
            stage.show()
            app.processEvents()
            img = stage.grab().toImage()
            assert not _image_is_blank(img), f"{w}x{h} 渲染空白"
            # 内容 = 显著偏离背景主色的像素（ΔRGB 通道差合计 > 60）——
            # 径向光晕的淡染(Δ≈20)不算内容，只有机身/条/文本/徽标算
            counts: dict[int, int] = {}
            for y in range(img.height()):
                for x in range(img.width()):
                    c = img.pixelColor(x, y)
                    key = (c.red() // 16, c.green() // 16, c.blue() // 16)
                    counts[key] = counts.get(key, 0) + 1
            bg_key = max(counts, key=lambda k: counts[k])
            bg_val = [k * 16 + 8 for k in bg_key]

            def _is_content(c) -> bool:
                return (abs(c.red() - bg_val[0]) + abs(c.green() - bg_val[1])
                        + abs(c.blue() - bg_val[2])) > 60

            top = next(y for y in range(img.height())
                       if any(_is_content(img.pixelColor(x, y))
                              for x in range(0, img.width(), 2)))
            bottom = next(y for y in range(img.height() - 1, -1, -1)
                          if any(_is_content(img.pixelColor(x, y))
                                 for x in range(0, img.width(), 2)))
            # 设备主体高 241/328 逻辑：fit 后内容四边至少留 2px（顶部
            # 徽标/闪电索可能贴近上缘，放宽到 2；历史裁切是 0 贴边）
            assert top >= 2, f"{w}x{h} 顶裁 top={top}"
            assert img.height() - 1 - bottom >= 2, (
                f"{w}x{h} 底裁 bottom={img.height() - 1 - bottom}")

    # 布局协作：resizeEvent 把 minHeight 钉在 hfw(当前宽) 上
    stage.setMinimumSize(0, 0)
    stage.setMaximumSize(16777215, 16777215)
    stage.resize(333, 10)
    app.processEvents()
    assert stage.minimumHeight() == stage.height_for_width(333), (
        stage.minimumHeight(), stage.height_for_width(333))
    stage.hide()
    stage.deleteLater()
    print("2b. 窄宽度（280/327）等比缩放无裁切 OK")
    print("2b+. 两态×三尺寸内容包围盒四边不贴边 + minHeight 钉 hfw OK")


def cs_total(stage) -> float:
    from app.ui.cuktech_stage import _total_watts
    return _total_watts(stage._ports)


# ---------- 3. 舞台功率条可见性：充电中 grab 像素级验证条区域 ----------

def test_stage_bar_pixels(theme: str) -> None:
    from app.ui.cuktech_stage import DeviceStageWidget, _BAR_Y, _PVAL_H, _PVAL_W, _PVAL_X

    apply_theme(theme)
    stage = DeviceStageWidget()
    stage.resize(360, 328)
    stage.update_state(dict(FAKE_STATUS))
    stage.show()
    app.processEvents()
    img = stage.grab().toImage()
    # C1 功率文本中心（盒内 (274.5+35, 34+4+11) → 画布 +51.5）：
    # 7.1W 时白色粗体文本应落在机身右缘外侧的该位置
    cx = int(_PVAL_X + _PVAL_W / 2)
    cy = int(51.5 + _BAR_Y[1] + 4.0 + _PVAL_H / 2)
    center = img.pixelColor(cx, cy)
    # 功率文本是白色粗体，背景是深色面板——亮度应显著偏高
    v = center.red() + center.green() + center.blue()
    assert v > 200, f"[{theme}] C1 功率文本区域疑似未绘制: {center.name()}"
    # 文本右缘不出条尾（盒内 371.5 → 画布 423）
    assert cx + _PVAL_W / 2 <= 423
    stage.hide()
    stage.deleteLater()
    print(f"3. 舞台 C1 功率文本位置/亮度像素验证 OK [{theme}]")


# ---------- 4. 舞台空载：unconnected 图、功率条隐藏 ----------

def test_stage_idle(theme: str) -> None:
    from app.ui.cuktech_stage import DeviceStageWidget, _BAR_Y, _PVAL_DY, _PVAL_H, _PVAL_W, _PVAL_X

    apply_theme(theme)
    stage = DeviceStageWidget()
    stage.resize(360, 328)
    stage.update_state(dict(FAKE_STATUS))
    stage.show()
    app.processEvents()
    assert stage._charging is True
    img = stage.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 舞台渲染空白"
    # 空载同一位置充电中是白色文本、空载后是机身像素——断言语义：
    # 文本框（70×22）内"近白"像素数充电态显著多于空载态。机身银色
    # 装饰环亮度可达 ~717，但不是白（RGB 通道均衡）；白色粗体文本
    # 会有大量 >=730 的近白像素，阈值取 720 分开两种材质。
    cx0 = int(_PVAL_X)
    cy0 = int(51.5 + _BAR_Y[1] + _PVAL_DY)
    pw, ph = int(_PVAL_W), int(_PVAL_H)

    def _near_white_count() -> int:
        im = stage.grab().toImage()
        n = 0
        for x in range(cx0, cx0 + pw):
            for y in range(cy0, cy0 + ph):
                c = im.pixelColor(x, y)
                if c.red() + c.green() + c.blue() >= 720:
                    n += 1
        return n

    v_charging = _near_white_count()
    stage.update_state(dict(EMPTY_STATUS))
    app.processEvents()
    assert stage._charging is False
    img = stage.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 空载舞台渲染空白"
    v_idle = _near_white_count()
    assert v_charging > 80 and v_charging - v_idle >= 40, (
        f"[{theme}] 功率文本未落在口右侧位置或空载未消失: "
        f"charging={v_charging} idle={v_idle}")
    # update_port 逐口归零（C1 65W 已在 FAKE_STATUS，C3/A 仍 9+1.5W）
    stage.update_state(dict(FAKE_STATUS))
    app.processEvents()
    assert stage._charging is True
    for port, data in (
            (1, {"power": 0.0, "enabled": True, "protocol": "idle"}),
            (3, {"power": 0.0, "enabled": True, "protocol": "idle"}),
            (4, {"power": 0.0, "enabled": True, "protocol": "idle"})):
        stage.update_port(port, data)
        app.processEvents()
    assert stage._charging is False, "全部口归零后未回空载态"
    stage.hide()
    stage.deleteLater()
    print(f"4. 舞台空载 unconnected/功率条隐藏/update_port 归零切换 OK [{theme}]")


# ---------- 5. 场景徽标：set_scene(2) 文案「数码生态」 ----------

def test_stage_scene(theme: str) -> None:
    from app.ui.cuktech_stage import SCENE_BADGES, DeviceStageWidget

    apply_theme(theme)
    assert SCENE_BADGES[2] == ("apple", "数码生态"), SCENE_BADGES[2]
    stage = DeviceStageWidget()
    stage.resize(360, 328)
    stage.set_scene(2)
    assert stage._scene == 2
    # update_state 带 settings["5"]=4 时徽标跟着整帧走
    status = dict(FAKE_STATUS)
    status["settings"] = {"5": 4}
    stage.update_state(status)
    assert stage._scene == 4
    # 非法模式忽略
    stage.set_scene(9)
    assert stage._scene == 4
    stage.deleteLater()
    print(f"5. 场景徽标 set_scene/整帧回填/非法模式忽略 OK [{theme}]")


# ---------- 6. PortCardGrid：整帧注入渲染断言 ----------

def test_grid_state(theme: str) -> None:
    from app.ui.cuktech_stage import PortCardGrid, _load_pct

    apply_theme(theme)
    grid = PortCardGrid()
    grid.resize(560, 300)
    grid.update_state(dict(FAKE_STATUS))
    app.processEvents()
    cards = grid._cards
    # 大读数文案
    assert cards[1]["power"].text() == "65.0W", cards[1]["power"].text()
    assert cards[4]["power"].text() == "1.5W"
    assert cards[2]["power"].text() == "0.0W"
    # V·A 副行（3.25 按 Python round-half-even 显示 3.2）
    assert cards[1]["va"].text() == "20.0V · 3.2A", cards[1]["va"].text()
    # 协议徽标：非 idle 高亮（样式含主题色）、idle 灰置
    assert SiColors.THEME in cards[1]["proto"].styleSheet()
    assert cards[1]["proto"].text() == "PD"
    assert SiColors.LINE in cards[2]["proto"].styleSheet()
    assert cards[2]["proto"].text() == "idle"
    # 负载条 pct：65/120 -> 54%，0 -> idle
    assert cards[1]["load"]._pct == _load_pct(65.0, 1)
    assert cards[1]["load"]._pct == 54, cards[1]["load"]._pct
    assert cards[1]["load"]._idle is False
    assert cards[2]["load"]._idle is True and cards[2]["load"]._pct == 0
    # 边界钳制：loadPct 语义
    assert _load_pct(0.0, 1) == 0
    assert _load_pct(120.0, 1) == 99
    assert _load_pct(-1.0, 1) == 0
    assert _load_pct(0.5, 3) == 2  # 有输出但极小 -> 下限 2%
    # 开关态：enabled=False 的 C2 开关应为关
    assert cards[2]["switch"].isChecked() is False
    assert cards[1]["switch"].isChecked() is True
    img = grid.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 端口卡网格渲染空白"
    grid.hide()
    grid.deleteLater()
    print(f"6. 端口卡网格读数/协议徽标/负载条/开关态 OK [{theme}]")


# ---------- 7. update_port 增量：只有对应卡变化 ----------

def test_grid_incremental(theme: str) -> None:
    from app.ui.cuktech_stage import PortCardGrid

    apply_theme(theme)
    grid = PortCardGrid()
    grid.resize(560, 300)
    grid.update_state(dict(FAKE_STATUS))
    app.processEvents()
    cards = grid._cards
    before = {p: (cards[p]["power"].text(), cards[p]["va"].text(),
                  cards[p]["load"]._pct, cards[p]["proto"].text())
              for p in (1, 2, 3, 4)}
    # 单口增量：C1 65W -> 30W
    grid.update_port(1, {"voltage": 20.0, "current": 1.5, "power": 30.0,
                         "active": True, "protocol": "PPS", "enabled": True})
    app.processEvents()
    assert cards[1]["power"].text() == "30.0W"
    assert cards[1]["va"].text() == "20.0V · 1.5A"
    assert cards[1]["proto"].text() == "PPS"
    assert cards[1]["load"]._pct == 25  # 30/120 -> 25%
    # 其它卡完全没动（增量不重建）
    for p in (2, 3, 4):
        after = (cards[p]["power"].text(), cards[p]["va"].text(),
                 cards[p]["load"]._pct, cards[p]["proto"].text())
        assert after == before[p], f"端口 {p} 被增量误更新: {before[p]} -> {after}"
    # 充电态描边切换：C2 从 0W -> 20W 应获得端口色混色描边
    # （上游 color-mix(port-c2 70%, 前景墨)；暗色 -> #77c4f9，浅色偏深）
    grid.update_port(2, {"voltage": 20.0, "current": 1.0, "power": 20.0,
                         "active": True, "protocol": "PD", "enabled": True})
    app.processEvents()
    ss = cards[2]["frame"].styleSheet().lower()
    assert "border: 1px solid #" in ss, ss
    # 描边色必须是 port-c2 蓝与墨的混色（含蓝主导分量），而非中性灰 LINE
    assert "#46b4ff" in ss or "#77c4f9" in ss or "#3885bb" in ss, ss
    grid.hide()
    grid.deleteLater()
    print(f"7. 端口卡增量更新只动对应卡/描边切换 OK [{theme}]")


# ---------- 8. 端口图标随开关切换 + 点卡片/点开关信号 ----------

def test_grid_signals(theme: str) -> None:
    from app.ui.cuktech_stage import PortCardGrid

    apply_theme(theme)
    grid = PortCardGrid()
    grid.resize(560, 300)
    grid.update_state(dict(FAKE_STATUS))
    grid.show()
    app.processEvents()

    clicked = QSignalSpy(grid.port_clicked)
    toggled = QSignalSpy(grid.port_toggle)

    # 点开关：程序化 setChecked(True) 发 toggled -> port_toggle(2, True)
    grid._cards[2]["switch"].setChecked(True)
    assert toggled.count() == 1, toggled.count()
    assert toggled.at(0)[0] == 2 and toggled.at(0)[1] is True

    # 点卡片空白区：直接对 frame 发左键 mouseReleaseEvent
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, QPoint
    frame = grid._cards[1]["frame"]
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(10, 10),
                     QPointF(10, 10), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    app.sendEvent(frame, ev)
    assert clicked.count() == 1, clicked.count()
    assert clicked.at(0)[0] == 1

    # 开关切换后图标应随 enabled 更新（update_port 喂 enabled=False）
    icon_on = grid._cards[1]["icon"].pixmap()
    grid.update_port(1, {"power": 0.0, "enabled": False, "protocol": "idle",
                         "voltage": 0.0, "current": 0.0})
    app.processEvents()
    icon_off = grid._cards[1]["icon"].pixmap()
    assert not icon_on.isNull() and not icon_off.isNull()
    assert icon_on.toImage() != icon_off.toImage(), "开关切换后端口图标未变化"
    # 同步关掉本地开关态
    assert grid._cards[1]["switch"].isChecked() is False

    grid.hide()
    grid.deleteLater()
    print(f"8. 端口卡 port_clicked/port_toggle 信号/图标随开关切换 OK [{theme}]")


# ---------- 9. MiniPhoneShareBar：占比计算与四色渲染 ----------

def test_share_bar(theme: str) -> None:
    from app.ui.cuktech_stage import PORT_COLORS, MiniPhoneShareBar

    apply_theme(theme)
    bar = MiniPhoneShareBar()
    bar.resize(400, 40)
    bar.update_state(dict(FAKE_STATUS))
    app.processEvents()
    # 占比：65+9+1.5=75.5 -> C1 86%、C3 12%、A 2%、C2 空载 0%
    labels = [lb.text() for lb in bar._labels]
    assert labels[0] == "C1 65.0W · 86%", labels[0]
    assert labels[2] == "C3 9.0W · 12%", labels[2]
    assert labels[3] == "USB-A 1.5W · 2%", labels[3]
    assert labels[1] == "C2 0.0W · 0%", labels[1]
    # 分段：四色、比例正确
    segs = bar._bar._segments
    assert len(segs) == 4
    assert [c for _f, c in segs] == [PORT_COLORS[p] for p in (1, 2, 3, 4)]
    assert abs(segs[0][0] - 65.0 / 75.5) < 1e-6, segs[0]
    assert segs[1][0] == 0.0
    # 全空载：占比 0%，不除零
    bar.update_state(dict(EMPTY_STATUS))
    assert all(f == 0.0 for f, _c in bar._bar._segments)
    img = bar.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 占比条渲染空白"
    bar.hide()
    bar.deleteLater()
    print(f"9. MiniPhoneShareBar 占比计算/四色分段/全空载 OK [{theme}]")


# ---------- 10. 双主题 grab + retheme 不崩 ----------

def test_retheme(theme: str) -> None:
    from app.ui.cuktech_stage import DeviceStageWidget, MiniPhoneShareBar, PortCardGrid

    apply_theme(theme)
    stage = DeviceStageWidget()
    stage.resize(360, 328)
    stage.update_state(dict(FAKE_STATUS))
    stage.show()
    grid = PortCardGrid()
    grid.resize(560, 300)
    grid.update_state(dict(FAKE_STATUS))
    grid.show()
    share = MiniPhoneShareBar()
    share.resize(400, 40)
    share.update_state(dict(FAKE_STATUS))
    share.show()
    app.processEvents()
    for name, w in (("舞台", stage), ("端口卡", grid), ("占比条", share)):
        img = w.grab().toImage()
        assert not _image_is_blank(img), f"[{theme}] {name} 渲染空白"
        w.retheme()  # 不抛异常即可
    app.processEvents()
    # retheme 后仍非空白（样式重设成功）
    for name, w in (("舞台", stage), ("端口卡", grid), ("占比条", share)):
        img = w.grab().toImage()
        assert not _image_is_blank(img), f"[{theme}] retheme 后 {name} 渲染空白"
    stage.hide(); stage.deleteLater()
    grid.hide(); grid.deleteLater()
    share.hide(); share.deleteLater()
    print(f"10. 双主题 grab/retheme OK [{theme}]")


# ---------- 执行：暗色一遍、亮色一遍 ----------

test_assets()

for theme in ("dark", "light"):
    test_stage_charging(theme)
    test_stage_no_clip()
    test_stage_bar_pixels(theme)
    test_stage_idle(theme)
    test_stage_scene(theme)
    test_grid_state(theme)
    test_grid_incremental(theme)
    test_grid_signals(theme)
    test_share_bar(theme)
    test_retheme(theme)

si_theme.set_theme("dark")
print("CUKTECH STAGE TEST ALL PASS")
