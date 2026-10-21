# SPDX-License-Identifier: GPL-3.0-or-later
"""托盘存储迁移 / 场景混排 / 场景 UI 的离线回归（Qt 离屏）。

用法: .venv\\Scripts\\python.exe tests/scene_tray_test.py

覆盖：
1. tray.json 旧 v1 str、v2 span（1-4）读取与写回保序去重；
2. 场景快照、统一 order 的持久化/混排/非法条目丢弃/缺省兜底；
3. 托盘快捷窗口 span 大卡与场景行同网格、CUKTECH 大卡与 hover 共存；
4. 场景卡忙态/失败恢复、主页场景开关、场景卡与网格布局关键点。
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QObject, QSize, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])

from app.core import _json_store  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="mihome-scene-tray-test-"))
_json_store.data_dir = lambda: _TMP
_json_store.data_file = lambda name: _TMP / name

from app.core import settings_store, tray_store  # noqa: E402
from app.core.models import DeviceInfo, SceneInfo  # noqa: E402
from app.ui import toast as toast_mod  # noqa: E402
from app.ui.device_card import _CARD_FIXED_HEIGHT, _CARD_FIXED_WIDTH  # noqa: E402
from app.ui.theme_service import apply_theme  # noqa: E402
from app.ui.tray.cuktech_hover import CuktechHoverPopup  # noqa: E402
from app.ui.tray.quick_window import TrayQuickWindow  # noqa: E402

apply_theme("dark")

_PASS: list[str] = []


def check(name: str) -> None:
    _PASS.append(name)
    print(f"  [PASS] {name}")


def _write_tray(payload: dict) -> None:
    (_TMP / "tray.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@contextmanager
def _capture_toasts():
    messages: list[str] = []
    original = toast_mod.Toast.__dict__["info"]

    def fake_info(parent, text, duration_ms=4000):
        messages.append(str(text))

    toast_mod.Toast.info = staticmethod(fake_info)
    try:
        yield messages
    finally:
        toast_mod.Toast.info = original


class _DeferredJobs:
    """记录任务，由测试显式执行；避免后台线程/网络把断言变成竞态。"""

    def __init__(self):
        self.tasks: list[tuple] = []

    def submit(self, fn, on_success=None, on_error=None):
        self.tasks.append((fn, on_success, on_error))
        return None

    def run_last(self):
        fn, on_success, on_error = self.tasks.pop()
        try:
            result = fn()
        except Exception as exc:  # 与 JobExecutor 的失败路径一致
            if on_error is not None:
                on_error(exc)
            else:
                raise
        else:
            if on_success is not None:
                on_success(result)

    def shutdown(self):
        self.tasks.clear()


class _FakeService:
    def __init__(self):
        self.scenes: list[SceneInfo] = []
        self.run_calls: list[str] = []
        self.run_error: Exception | None = None

    def read_metrics(self, dids):
        return {}

    def cuktech_status(self):
        return {"connected": True, "ports": {}}

    def scene_list(self, home_name=None):
        return list(self.scenes)

    def run_scene(self, scene):
        self.run_calls.append(scene.scene_id)
        if self.run_error is not None:
            raise self.run_error

    def icon_urls(self, models):
        return {}

    def localized_product_names(self, dids, names):
        return {}

    def cached_product_page_names(self, models):
        return {}

    def has_product_page_name(self, model):
        return True

    def spec_has_published_functions(self, spec_type):
        return True

    def power_states(self, dids):
        return {}


# ---------- 1. tray_store：v1/v2 迁移、span、场景与 order ----------

def test_tray_store_migration() -> None:
    _write_tray({"version": 1, "devices": ["dev-a", "dev-b"]})
    assert tray_store.load() == ["dev-a", "dev-b"]
    assert tray_store.load_entries() == [
        {"did": "dev-a", "span": 1}, {"did": "dev-b", "span": 1}]
    assert tray_store.span_of("dev-a") == 1
    check("旧 v1 str 列表迁移为带 span=1 的条目")

    _write_tray({
        "version": 2,
        "devices": [
            {"did": "dev-a", "span": 4},
            {"did": "dev-b", "span": 0},
            {"did": "dev-c", "span": 99},
            {"did": "dev-d", "span": "2"},
        ],
        "scenes": [],
        "order": [],
    })
    spans = {e["did"]: e["span"] for e in tray_store.load_entries()}
    assert spans == {"dev-a": 4, "dev-b": 1, "dev-c": 4, "dev-d": 2}, spans
    check("v2 span 保留并钳到实际范围 1-4（含字符串/0/越界）")

    tray_store.set_span("dev-a", 5)
    tray_store.set_span("dev-b", -3)
    assert tray_store.span_of("dev-a") == 4
    assert tray_store.span_of("dev-b") == 1
    tray_store.set_span("not-exist", 2)  # 未知设备静默忽略
    assert tray_store.span_of("not-exist") == 1
    check("set_span 钳位与未知设备容错")

    tray_store.save(["dev-b", "dev-a", "dev-a"])
    assert tray_store.load() == ["dev-b", "dev-a"], "save 应去重保序"
    assert tray_store.span_of("dev-a") == 4, "save 必须保留已有 span"
    assert tray_store.span_of("dev-b") == 1
    raw = json.loads((_TMP / "tray.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    check("save(dids) 去重保序且不丢 span")

    tray_store.save_scenes([
        {"scene_id": "s1", "name": "回家", "home_id": "1",
         "home_name": "家A", "junk": "ignored"},
        {"scene_id": "s1", "name": "重复", "home_id": "2"},
        {"name": "缺 id"},
        "bad",
        None,
    ])
    scenes = tray_store.load_scenes()
    assert len(scenes) == 1, scenes
    assert set(scenes[0]) == {"scene_id", "name", "home_id", "home_name"}
    assert scenes[0]["name"] == "回家" and scenes[0]["home_name"] == "家A"
    check("场景快照规整、去重、丢弃非法条目并只存四字段")

    tray_store.save_order([
        {"type": "scene", "id": "s1"},
        {"type": "device", "id": "dev-a"},
        {"type": "bogus", "id": "x"},
        {"type": "scene", "id": "s1"},
        {"type": "device", "id": ""},
        {"type": "device", "id": "dev-b"},
    ])
    assert tray_store.load_order() == [
        {"type": "scene", "id": "s1"},
        {"type": "device", "id": "dev-a"},
        {"type": "device", "id": "dev-b"},
    ], tray_store.load_order()
    check("统一 order 去重并丢弃非法 type/空 id")

    # 旧数据没有 order：按设备在前、场景在后的历史顺序兜底
    _write_tray({
        "version": 2,
        "devices": ["dev-a", "dev-b"],
        "scenes": [{"scene_id": "s1", "name": "回家",
                    "home_id": "1", "home_name": "家A"}],
    })
    assert tray_store.load_order() == [
        {"type": "device", "id": "dev-a"},
        {"type": "device", "id": "dev-b"},
        {"type": "scene", "id": "s1"},
    ]
    check("缺 order 时合成设备在前、场景在后的兜底顺序")

    # 单独保存任一集合不丢其他两类数据
    tray_store.save(["dev-a"])
    tray_store.save_scenes([{"scene_id": "s1", "name": "回家",
                             "home_id": "1", "home_name": "家A"}])
    tray_store.save_order([{"type": "scene", "id": "s1"},
                           {"type": "device", "id": "dev-a"}])
    tray_store.save(["dev-a"])
    assert tray_store.load_scenes()[0]["scene_id"] == "s1"
    assert tray_store.load_order() == [{"type": "scene", "id": "s1"},
                                       {"type": "device", "id": "dev-a"}]
    tray_store.save_scenes([{"scene_id": "s1", "name": "回家",
                             "home_id": "1", "home_name": "家A"}])
    assert tray_store.load() == ["dev-a"]
    check("devices/scenes/order 三处写入互不覆盖")


# ---------- 2. 托盘快捷窗口：span 大卡 + 场景行 ----------

def _positions(grid):
    result = []
    for i in range(grid.count()):
        widget = grid.itemAt(i).widget()
        if widget is not None:
            result.append((widget, grid.getItemPosition(i)))
    return result


def test_quick_window_mixed_grid() -> None:
    dev_local = DeviceInfo(
        did="cuktech-local", name="CUKTECH 充电器", model="x",
        home_name="本地", room_name="本地", online=True, source="local")
    dev_a = DeviceInfo(
        did="dev-a", name="台灯", model="m.lamp", home_name="家A",
        room_name="客厅", online=True, source="cloud")
    dev_b = DeviceInfo(
        did="dev-b", name="插座", model="m.plug", home_name="家A",
        room_name="客厅", online=True, source="cloud")

    tray_store.save(["cuktech-local", "dev-a", "dev-b"])
    tray_store.set_span("cuktech-local", 2)
    tray_store.save_scenes([
        {"scene_id": "s1", "name": "回家", "home_id": "1", "home_name": "家A"},
        {"scene_id": "s2", "name": "离家", "home_id": "1", "home_name": "家A"},
    ])
    tray_store.save_order([
        {"type": "scene", "id": "s1"},
        {"type": "device", "id": "cuktech-local"},
        {"type": "scene", "id": "s2"},
        {"type": "device", "id": "dev-a"},
        {"type": "device", "id": "dev-b"},
    ])

    jobs = _DeferredJobs()
    service = _FakeService()
    win = TrayQuickWindow(service, jobs, None)
    win._columns = 2
    win.set_devices([dev_local, dev_a, dev_b], {})
    win.show()
    app.processEvents()
    win._rebuild()
    app.processEvents()

    items = win._tray_items()
    assert [(kind, getattr(item, "did", getattr(item, "scene_id", ""))) for kind, item, _ in items] == [
        ("scene", "s1"), ("device", "cuktech-local"), ("scene", "s2"),
        ("device", "dev-a"), ("device", "dev-b")], items
    assert [span for _, _, span in items] == [1, 2, 1, 1, 1], items
    check("快捷窗口按统一 order 混排设备/场景，CUKTECH span=2")

    positions = _positions(win._grid)
    by_key = {}
    cells = set()
    for widget, (row, col, row_span, col_span) in positions:
        key = getattr(widget, "_scene_id", None) or getattr(widget, "_did", "?")
        by_key[key] = (row, col, row_span, col_span)
        for rr in range(row, row + row_span):
            for cc in range(col, col + col_span):
                assert (rr, cc) not in cells, f"网格重叠: {(rr, cc)}"
                cells.add((rr, cc))
    assert by_key["s1"] == (0, 0, 1, 1), by_key
    assert by_key["cuktech-local"] == (1, 0, 1, 2), by_key
    assert by_key["s2"] == (2, 0, 1, 1), by_key
    assert by_key["dev-a"] == (2, 1, 1, 1), by_key
    assert by_key["dev-b"] == (3, 0, 1, 1), by_key
    assert len(cells) == 6, cells
    check("span 大卡跨整行、场景/普通卡让位且无重叠")

    card = win._cuktech_cards["cuktech-local"]
    assert card.height() == 112 and card._total_label.text() == "-- W"
    win.set_cuktech_status({
        "connected": True,
        "ports": {"1": {"power": 45.5}, "2": {"power": 30.0}},
    })
    assert card._total_label.text() == "75.5W", card._total_label.text()
    assert card._port_labels[1].text() == "45.5W"
    win.push_cuktech_port({"port_id": 4, "data": {"power": 8.26}})
    assert card._port_labels[4].text() == "8.3W"
    assert card._total_label.text() == "83.8W"
    assert win._estimate_scroll_height() == 56 + 112 + 56 + 56 + 3 * 6
    check("CUKTECH 大卡状态/单口增量与场景行共存，滚动高度按混排估算")

    # 场景行执行忙态：失败恢复，然后成功恢复
    scene = next(s for s in (SceneInfo(**d) for d in tray_store.load_scenes())
                 if s.scene_id == "s1")
    row = next(w for w, _ in positions
               if getattr(w, "_scene_id", None) == "s1")
    btn = row.findChild(QPushButton, "sceneRunBtn")
    assert btn is not None
    with _capture_toasts() as messages:
        service.run_error = RuntimeError("cloud down")
        win._run_scene(scene, btn)
        assert not btn.isEnabled(), "执行中运行按钮应禁用"
        jobs.run_last()
        assert btn.isEnabled(), "失败后按钮应恢复"
        assert messages and "失败" in messages[-1], messages

        service.run_error = None
        win._run_scene(scene, btn)
        assert not btn.isEnabled()
        jobs.run_last()
        assert btn.isEnabled(), "成功后按钮应恢复"
        assert messages and "已执行场景" in messages[-1], messages
    assert service.run_calls == ["s1", "s1"], service.run_calls
    check("托盘场景行执行忙态/失败/成功恢复，提示不崩")

    win.hide()
    win.deleteLater()
    app.processEvents()


# ---------- 3. 脏 order / 重复条目 ----------

def test_dirty_order_duplicates_and_missing() -> None:
    """order 只排序、不授予成员资格；重复/缺项按设备与场景集合收敛。"""
    dev_a = DeviceInfo("dev-a", "台灯", "m.a", "家A", "客厅", True)
    dev_b = DeviceInfo("dev-b", "已移除", "m.b", "家A", "客厅", True)
    dev_c = DeviceInfo("dev-c", "后加设备", "m.c", "家A", "客厅", True)
    scene = {"scene_id": "s1", "name": "回家", "home_id": "1", "home_name": "家A"}

    # 原始文件模拟旧版脏数据：devices/scenes/order 都有重复；dev-b 只剩 order；
    # dev-c 在 devices 但不在 order（应追加到末尾）。
    _write_tray({
        "version": 2,
        "devices": [
            {"did": "dev-a", "span": 2},
            {"did": "dev-a", "span": 2},
            {"did": "dev-c", "span": 1},
        ],
        "scenes": [dict(scene), dict(scene)],
        "order": [
            {"type": "device", "id": "dev-a"},
            {"type": "device", "id": "dev-b"},
            {"type": "device", "id": "dev-a"},
            {"type": "scene", "id": "s1"},
            {"type": "scene", "id": "s1"},
        ],
    })

    service = _FakeService()
    jobs = _DeferredJobs()
    win = TrayQuickWindow(service, jobs, None)
    win._columns = 2
    win.set_devices([dev_a, dev_b, dev_c], {})
    win.show()
    app.processEvents()
    win._rebuild()
    app.processEvents()

    def keys():
        return [(kind, getattr(item, "did", getattr(item, "scene_id", "")))
                for kind, item, _ in win._tray_items()]

    assert keys() == [
        ("device", "dev-a"), ("scene", "s1"), ("device", "dev-c")], keys()
    widgets = [w for w, _ in _positions(win._grid)]
    rendered = [getattr(w, "_did", getattr(w, "_scene_id", ""))
                for w in widgets]
    assert sorted(rendered) == ["dev-a", "dev-c", "s1"], rendered
    assert len(rendered) == len(set(rendered)), "重复条目不应重复渲染"
    check("脏 order 引用已移除设备不渲染、重复项收敛、缺项追加")

    # 管理对话框 save() 后 order 残留旧 did：成员集合说了算
    tray_store.save(["dev-c"])
    win._tray_entries = tray_store.load_entries()
    win._tray_order = tray_store.load_order()
    win._rebuild()
    app.processEvents()
    assert keys() == [("scene", "s1"), ("device", "dev-c")], keys()
    check("devices 已删除但 order 残留的 did 不再出现")

    win.hide()
    win.deleteLater()
    app.processEvents()


# ---------- 4. 主页场景视图与场景开关 ----------

class _NoTray:
    def set_tray_visible(self, visible):
        pass

    def hide_quick(self):
        pass

    def set_devices(self, *args, **kwargs):
        pass


class _FakeSettingsDialog(QObject):
    ble_server_setting_changed = Signal(bool)

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent)
        self._scale_changed = False
        self._url_changed = False

    def exec(self):
        settings_store.set_show_scene_tab(False)
        return 1  # QDialog.Accepted

    def deleteLater(self):
        super().deleteLater()


def test_main_window_scenes() -> None:
    import app.ui.main_window as mw

    settings_store.set_show_scene_tab(True)
    settings_store.set_hide_no_func_devices(False)
    service = _FakeService()
    jobs = _DeferredJobs()

    mw.MijiaService = lambda **kwargs: service
    mw.TrayController = lambda *args, **kwargs: _NoTray()
    original_stream = mw.MainWindow._maybe_start_cuktech_stream
    mw.MainWindow._maybe_start_cuktech_stream = lambda self: None
    win = mw.MainWindow()
    original_jobs = win._jobs
    win._service = service
    win._jobs = jobs
    try:
        win._show_scene_tab = True
        win.show()
        app.processEvents()
        win._apply_devices([DeviceInfo(
            did="dev-a", name="台灯", model="m.lamp", home_name="家A",
            room_name="客厅", online=True, source="cloud")])
        app.processEvents()

        assert "场景" in win._tab_buttons, win._tab_buttons
        check("场景开关开启时主页出现「场景」选项卡")

        scenes = [
            SceneInfo("s1", "回家", "1", "家A"),
            SceneInfo("s2", "离家", "1", "家A"),
        ]
        service.scenes = list(scenes)
        win._on_scenes_loaded(scenes)
        win._select_room("场景")
        app.processEvents()
        assert set(win._scene_cards) == {"s1", "s2"}, win._scene_cards
        assert "共 2 个场景" in win._count_label.text(), win._count_label.text()
        for card in win._scene_cards.values():
            assert card.size() == QSize(_CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT), card.size()
        check("场景视图建卡、计数与设备卡同尺寸")

        positions = _positions(win._grid)
        assert len(positions) == 2
        assert len({pos for _, pos in positions}) == 2, positions
        assert win._grid.columnStretch(0) == 0, "列不参与剩余宽度均分"
        assert win._grid.rowStretch(1) == 1, "多余垂直空间推到末行后"
        check("场景网格无重叠、列左对齐、末行吸收剩余高度")

        card = win._scene_cards["s1"]
        with _capture_toasts() as messages:
            card._run_btn.click()
            assert not card._run_btn.isEnabled(), "执行中场景卡按钮应禁用"
            jobs.run_last()
            assert card._run_btn.isEnabled()
            assert messages and "已执行场景" in messages[-1], messages

            service.run_error = RuntimeError("scene down")
            card._run_btn.click()
            assert not card._run_btn.isEnabled()
            jobs.run_last()
            assert card._run_btn.isEnabled(), "失败后应恢复可点击"
            assert messages and "失败" in messages[-1], messages

            service.run_error = None
            card._run_btn.click()
            jobs.run_last()
            assert card._run_btn.isEnabled(), "恢复后再次成功"
        check("主页场景卡忙态/错误提示/恢复重试")

        assert win._columns_for_width(win._default_window_width() - 48) == 5
        assert win._columns_for_width(0) == 1
        assert win._columns_for_width(426) == 2
        assert win._columns_for_width(640) == 3
        check("默认窗口宽度与 5 列公式一致，缩窄时列数单调回落")

        original_dialog = mw.SettingsDialog
        mw.SettingsDialog = _FakeSettingsDialog
        try:
            win._current_room = "场景"
            win._show_scene_tab = True
            win.show_settings()
        finally:
            mw.SettingsDialog = original_dialog
        assert win._show_scene_tab is False
        assert "场景" not in win._tab_buttons, win._tab_buttons
        assert win._current_room == "全屋", win._current_room
        assert "台设备" in win._count_label.text()
        check("关闭场景开关后入口消失且当前场景页回退「全屋」")
    finally:
        win._force_quit = True
        win._tray = None
        win.close()
        jobs.shutdown()
        original_jobs.shutdown()
        mw.MainWindow._maybe_start_cuktech_stream = original_stream


# ---------- 4. 托盘控制器：CUKTECH hover 与场景卡共存 ----------

def test_controller_hover_with_scenes() -> None:
    from app.ui.tray.controller import TrayController

    tray_store.save(["cuktech-local"])
    tray_store.set_span("cuktech-local", 2)
    tray_store.save_scenes([
        {"scene_id": "s1", "name": "回家", "home_id": "1", "home_name": "家A"}])
    tray_store.save_order([
        {"type": "scene", "id": "s1"},
        {"type": "device", "id": "cuktech-local"},
    ])

    dev = DeviceInfo(
        did="cuktech-local", name="CUKTECH 充电器", model="x",
        home_name="本地", room_name="本地", online=True, source="local")
    service = _FakeService()
    jobs = _DeferredJobs()
    host = QWidget()
    ctrl = TrayController(service, jobs, host)
    try:
        ctrl._quick._columns = 2
        ctrl._quick.set_devices([dev], {})
        ctrl._quick.show()
        app.processEvents()
        ctrl._quick._rebuild()
        app.processEvents()
        ctrl._hover_popup.show()
        app.processEvents()

        ctrl.push_cuktech_status({
            "connected": True,
            "ports": {"1": {"power": 45.5}, "2": {"power": 30.0}},
        })
        quick_card = ctrl._quick._cuktech_cards["cuktech-local"]
        assert quick_card._total_label.text() == "75.5W"
        assert ctrl._hover_popup._total_val.text() == "75.5W"
        ctrl.push_cuktech_port({"port_id": 4, "data": {"power": 8.26}})
        assert quick_card._port_labels[4].text() == "8.3W"
        assert ctrl._hover_popup._port_rows[4][1].text() == "8.3W"
        assert ctrl._hover_popup._total_val.text() == "83.8W"
        scene_rows = [w for w, _ in _positions(ctrl._quick._grid)
                      if getattr(w, "_scene_id", None) == "s1"]
        assert scene_rows, "场景行应仍在快捷窗口中"
        check("CUKTECH 大卡与 hover 弹窗同时更新，场景行共存")
    finally:
        ctrl._hover_popup.hide()
        ctrl._quick.hide()
        ctrl._tray.hide()
        host.close()
        host.deleteLater()
        app.processEvents()


def main() -> int:
    print("== scene_tray_test ==")
    test_tray_store_migration()
    test_quick_window_mixed_grid()
    test_dirty_order_duplicates_and_missing()
    test_main_window_scenes()
    test_controller_hover_with_scenes()
    print(f"\n全部通过：{len(_PASS)} 组行为断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
