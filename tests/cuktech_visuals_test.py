# SPDX-License-Identifier: GPL-3.0-or-later
"""CUKTECH 观感组件二件套自测：离屏运行，喂假数据断言渲染与信号。

用法: .venv\\Scripts\\python.exe tests/cuktech_visuals_test.py
（也支持 .venv\\Scripts\\python.exe -m tests.cuktech_visuals_test）

仿 tests/cuktech_panel_test.py 的离屏 + grab() 模式：
- SceneButtonRow：初始选中态图标路径、set_scene_ui 迁移、点击发
  scene_selected、retheme 后图标路径随主题切换；
- MultiMetricCurve：set_chart_data 后 grab 非空白、时间档位发
  range_changed(hours, interval)、W/Σ/A 指标切换重绘不崩、空态、
  双主题 retheme。

组件零网络：不发请求，只收数据发信号。
"""

import atexit
import copy
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# offscreen 平台不自带字体（Qt 6 不再内置 fonts 目录），必须指向系统
# 字体目录让 CJK 文字（「暂无数据」「充电器模式」等）真正渲染出像素
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# 允许直接以文件方式运行（python tests/cuktech_visuals_test.py）时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.ui import si_theme
from app.ui.theme_service import apply_theme

# ---------------------------------------------------------------------------
# 假数据：结构与 cuktech-api.md §7 /api/chart 契约一致
# ---------------------------------------------------------------------------

N = 60  # 60 个桶：hours=1, interval=30（面板现役档位， curves 兼容旧数据）

FAKE_CHART = {
    "ok": True,
    "labels": [f"{i // 4:02d}:{(i % 4) * 15:02d}" for i in range(N)],
    "datasets": {
        "power": [
            {"label": "C1", "data": [65.0 if i % 5 else 0.0 for i in range(N)]},
            {"label": "C2", "data": [30.0 if i % 7 else 0.0 for i in range(N)]},
            {"label": "C3", "data": [9.0] * N},
            {"label": "A", "data": [1.5] * N},
            {"label": "Total", "data": [65.0 + i % 7 * 9.0 for i in range(N)]},
        ],
        "voltage": [
            {"label": "C1", "data": [20.0] * N},
            {"label": "C2", "data": [9.0] * N},
            {"label": "C3", "data": [5.0] * N},
            {"label": "A", "data": [5.0] * N},
        ],
        "current": [
            {"label": "C1", "data": [3.25] * N},
            {"label": "C2", "data": [3.0] * N},
            {"label": "C3", "data": [1.0] * N},
            {"label": "A", "data": [0.3] * N},
        ],
    },
}

# 尾部带 3 个全零桶：验证上游 updateChart 式裁剪
TRAILING_ZERO_CHART = copy.deepcopy(FAKE_CHART)
for ds in TRAILING_ZERO_CHART["datasets"]["power"]:
    ds["data"][-3:] = [0.0] * 3
for key in ("voltage", "current"):
    for ds in TRAILING_ZERO_CHART["datasets"][key]:
        ds["data"][-3:] = [0.0] * 3

ALL_ZERO_CHART = {
    "ok": True,
    "labels": [f"{i:02d}:00" for i in range(10)],
    "datasets": {
        "power": [{"label": "C1", "data": [0.0] * 10}] * 5,
        "voltage": [{"label": "C1", "data": [0.0] * 10}] * 4,
        "current": [{"label": "C1", "data": [0.0] * 10}] * 4,
    },
}


def _image_is_blank(image) -> bool:
    """空白判定：全图非背景像素 <= 16 视为空白（同 cuktech_panel_test）。"""
    w, h = image.width(), image.height()
    counts: dict[str, int] = {}
    for x in range(w):
        for y in range(h):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    non_bg = w * h - max(counts.values())
    return non_bg <= 16


from app.ui.cuktech_visuals import (  # noqa: E402
    MultiMetricCurve,
    SceneButtonRow,
    _centered_icon_pixmap,
    _fmt_tick,
    _nice_scale,
    _scene_icon_path,
    _Y_AXIS_WIDTH,
)

# ---------- 1. SceneButtonRow：选中态 / 点击信号 / retheme ----------


def test_scene_row(theme: str) -> None:
    apply_theme(theme)
    row = SceneButtonRow()
    row.resize(360, 150)
    row.show()
    app.processEvents()

    # 初始选中 1（AI）：图标路径含 dark_ai_on（或 light_ai_on），其余 _off
    assert row.current_scene() == 1
    assert "ai_on" in _scene_icon_path(1, True)
    icon1 = row._buttons[1].icon()
    assert not icon1.isNull(), f"[{theme}] 场景图标 1 未加载"
    # 其余三键未选中态图标可加载（素材齐全）
    for mode in (2, 3, 4):
        assert not row._buttons[mode].icon().isNull()

    # set_scene_ui(2)：选中态迁移（外部喂当前值的唯一入口）
    row.set_scene_ui(2)
    assert row.current_scene() == 2
    assert row._current.text() == "数码生态"
    assert row._desc.text() == "多口同时充电均衡分配功率"

    # 点击第 4 个按钮：发 scene_selected(4)，且组件自己不发请求
    got: list[int] = []
    row.scene_selected.connect(got.append)
    row._buttons[4].click()
    app.processEvents()
    assert got == [4], got
    assert row.current_scene() == 4
    assert row._current.text() == "均衡模式"

    # retheme：不抛异常、图标重刷成功（主题切换后路径含新主题前缀）
    row.retheme()
    app.processEvents()
    img = row.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 场景按钮排渲染空白"

    # 非法值不迁移
    row.set_scene_ui(9)
    assert row.current_scene() == 4

    row.hide()
    row.deleteLater()
    print(f"1. SceneButtonRow 选中态/信号/retheme OK [{theme}]")


# ---------- 2. MultiMetricCurve：渲染 / 档位 / 指标 / 空态 ----------


def _band_non_background(image, x0: int, x1: int) -> bool:
    """竖直像素带内是否存在明显区别于带内主色的像素（刻度文字判定）。"""
    w, h = image.width(), image.height()
    xa, xb = max(0, x0), min(w, x1)
    if xb <= xa:
        return False
    counts: dict[str, int] = {}
    for x in range(xa, xb):
        for y in range(h):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    return (xb - xa) * h - max(counts.values()) > 16


def test_curve(theme: str) -> None:
    apply_theme(theme)
    curve = MultiMetricCurve()
    curve.resize(560, 260)
    curve.show()
    app.processEvents()

    # 空态：显示「暂无数据」且非空白
    img = curve.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 曲线空态渲染空白"

    # 喂入一次完整响应：非空白渲染
    curve.set_chart_data(copy.deepcopy(FAKE_CHART))
    app.processEvents()
    img = curve.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 曲线渲染空白"
    assert len(curve._labels) == N

    # 时间档位：点击 24小时 -> range_changed(24.0, 300)；
    # 120分 -> (2.0, 30)；30分 -> (0.5, 20)（抄上游 chart-config.js）
    got: list[tuple[float, int]] = []
    curve.range_changed.connect(lambda h, i: got.append((h, i)))
    curve._range_buttons[24.0].click()
    app.processEvents()
    assert got[-1] == (24.0, 300), got
    curve._range_buttons[2.0].click()
    app.processEvents()
    assert got[-1] == (2.0, 30), got
    curve._range_buttons[0.5].click()
    app.processEvents()
    assert got[-1] == (0.5, 20), got
    assert len(got) == 3, f"重复点同一档不应重复发信号: {got}"

    # 指标切换 W -> Σ -> V -> A：零请求重绘不崩（数据已全存）；
    # 各指标左缘 Y 轴数值带都有刻度文字（带内像素非纯背景）
    for metric in ("total", "voltage", "current", "power"):
        curve._metric_buttons[metric].click()
        app.processEvents()
        img = curve.grab().toImage()
        assert not _image_is_blank(img), f"[{theme}] 指标 {metric} 渲染空白"
        assert _band_non_background(img, 0, int(_Y_AXIS_WIDTH) - 2), \
            f"[{theme}] 指标 {metric} Y 轴数值带无刻度文字"
    assert curve.current_metric() == "power"

    # 尾部全零桶裁掉（上游 updateChart 清洗）：60 桶裁到 57
    curve.set_chart_data(copy.deepcopy(TRAILING_ZERO_CHART))
    assert len(curve._labels) == N - 3, \
        f"尾部全零桶未裁掉: {len(curve._labels)}"
    for key in ("power", "voltage", "current"):
        for ds in curve._series[key]:
            assert len(ds) == N - 3, f"{key} 序列未同步裁剪"

    # 全零响应：裁剪后仍全零 -> 空态，不崩
    curve.set_chart_data(copy.deepcopy(ALL_ZERO_CHART))
    app.processEvents()
    img = curve.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] 全零数据空态渲染空白"

    # 坏载荷：非 dict / 缺 datasets -> 空态不崩
    curve.set_chart_data(None)  # type: ignore[arg-type]
    curve.set_chart_data({"labels": []})
    curve.set_chart_data({"labels": ["x"], "datasets": {}})

    # retheme：档位/图例重设 + 重绘不抛异常；retheme 后刻度色跟随新主题
    curve.set_chart_data(copy.deepcopy(FAKE_CHART))
    curve._metric_buttons["total"].click()
    curve.retheme()
    app.processEvents()
    img = curve.grab().toImage()
    assert not _image_is_blank(img), f"[{theme}] retheme 后渲染空白"
    assert _band_non_background(img, 0, int(_Y_AXIS_WIDTH) - 2), \
        f"[{theme}] retheme 后 Y 轴数值带无刻度文字"

    curve.hide()
    curve.deleteLater()
    print(f"2. MultiMetricCurve 渲染/档位/指标/裁剪/空态/retheme OK [{theme}]")


# ---------- 3. 双主题跑一遍 ----------

test_scene_row("dark")
test_curve("dark")
test_scene_row("light")
test_curve("light")

# ---------- 3.5 Y 轴 nice 刻度纯函数抽查 ----------

# 功率类：好读的 1/2/5×10^k 步长，轴上界盖住数据、分段 2~4
step, top, div = _nice_scale(70.2)
assert (step, top, div) == (20.0, 80.0, 4), (step, top, div)
step, top, div = _nice_scale(108.0)
assert (step, top, div) == (50.0, 150.0, 3), (step, top, div)
step, top, div = _nice_scale(79.92)  # FAKE_CHART W 档：max 74×1.08
assert (step, top, div) == (20.0, 80.0, 4), (step, top, div)
# 电压/电流：小数步长
step, top, div = _nice_scale(21.6)
assert (step, top, div) == (10.0, 30.0, 3), (step, top, div)
step, top, div = _nice_scale(3.51)
assert (step, top, div) == (1.0, 4.0, 4), (step, top, div)
step, top, div = _nice_scale(0.35)
assert (step, top, div) == (0.1, 0.4, 4), (step, top, div)
# 兜底与格式化
assert _nice_scale(0.0) == (1.0, 1.0, 1)
assert _fmt_tick(0.0, 0) == "0"
assert _fmt_tick(2.5, 2) == "2.50"
assert _fmt_tick(-0.0, 2) == "0.00"
print("3.5 Y 轴 nice 刻度纯函数抽查 OK")

# ---------- 4. 图标路径规则抽查：16 张素材随主题×选中态取对路径 ----------

si_theme.set_theme("dark")
for mode, img in ((1, "ai"), (2, "mac"), (3, "single"), (4, "balance")):
    path = _scene_icon_path(mode, True)
    assert f"main_charger_dark_{img}_on.png" in path, path
    path = _scene_icon_path(mode, False)
    assert f"main_charger_dark_{img}_off.png" in path, path
si_theme.set_theme("light")
for mode, img in ((1, "ai"), (2, "mac"), (3, "single"), (4, "balance")):
    path = _scene_icon_path(mode, True)
    assert f"main_charger_light_{img}_on.png" in path, path
si_theme.set_theme("dark")
for mode, img in ((1, "ai"), (2, "mac"), (3, "single"), (4, "balance")):
    for state in (True, False):
        asset = Path(_scene_icon_path(mode, state))
        assert asset.exists(), f"素材缺失: {asset}"
print("4. 场景图标 16 张路径规则与素材存在性 OK")

# ---------- 4.5 重居中裁剪：实心圆回到画布中心 ----------

# 上游素材实心圆偏 (-12,-18)px：裁剪后实心圆心必须落在画布几何中心
# 半像素内，且圆占画布比例明显提高（56px 按钮里不再缩成 ~27px）
from PySide6.QtCore import QRect  # noqa: E402

for mode in (1, 2, 3, 4):
    for state in (True, False):
        pm = _centered_icon_pixmap(_scene_icon_path(mode, state))
        assert not pm.isNull()
        img = pm.toImage().convertToFormat(__import__("PySide6.QtGui", fromlist=["QImage"]).QImage.Format.Format_ARGB32)
        w, h = img.width(), img.height()
        assert w == h, f"mode {mode}: 裁剪结果非正方形 {w}x{h}"
        alpha = bytes(bytearray(img.constBits()))[: w * h * 4]
        # ARGB32 小端：每像素 BGRA，取 A 通道
        alpha = alpha[3::4]
        min_x, max_x, min_y, max_y = w, 0, h, 0
        for y in range(h):
            row = alpha[y * w:(y + 1) * w]
            xs = [i for i, v in enumerate(row) if v >= 200]
            if not xs:
                continue
            min_x, max_x = min(min_x, xs[0]), max(max_x, xs[-1])
            min_y, max_y = min(min_y, y), max(max_y, y)
        cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
        assert abs(cx - w / 2) <= 0.5 and abs(cy - h / 2) <= 0.5, \
            f"mode {mode}: 圆心 ({cx},{cy}) 偏离中心 (w={w})"
        solid_d = max_x - min_x + 1
        assert solid_d >= w * 0.7, f"mode {mode}: 圆径 {solid_d}/{w} 占比过低"
print("4.5 图标重居中裁剪（圆心回中 + 圆径占比）OK")

si_theme.set_theme("dark")
print("CUKTECH VISUALS TEST ALL PASS")
