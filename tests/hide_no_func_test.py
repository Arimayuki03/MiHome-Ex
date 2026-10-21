# SPDX-License-Identifier: GPL-3.0-or-later
"""「隐藏无可控制功能的设备」失效修复回归测试。

用法: .venv\\Scripts\\python.exe tests/hide_no_func_test.py
（离屏运行 QT_QPA_PLATFORM=offscreen）

根因：隐藏过滤在网格构建时求值（_visible_devices → _device_has_functions
→ service._spec_cache），而 spec 缓存由开关/温湿度探测异步填充——
网格先建、spec 结论后到，探测回调从不触发网格重建，无功能设备永远
留在界面上。

本测试使用完全离线的假 API / 假本地充电器源，并把全部配置、托盘、
缓存、图标路径重定向到临时目录。覆盖 mijiaAPI 5.x 语义：
1. 无 spec_type 的云设备立即隐藏（官方确认无公开规格）；
2. 有 spec_type 但 spec 尚未拉取的设备保持显示（等待探测证实）；
3. 探测结论到达后无功能设备从网格消失、计数标签同步；
4. 温湿度/开关回调均不重复重建无变化的网格；
5. 本地 CUKTECH 充电器不受隐藏开关影响，始终可见；
6. 开关关闭时不走隐藏重建分支。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from app.core import _json_store  # noqa: E402

# 隔离用户数据：settings/tray/cache/icon 全部落在测试自己的临时目录
_TMP = Path(tempfile.mkdtemp(prefix="mihome-hide-test-"))
_json_store.data_dir = lambda: _TMP
_json_store.data_file = lambda name: _TMP / name

from app.core import cache as _device_cache  # noqa: E402
from app.core.models import DeviceInfo  # noqa: E402
from app.core.service import MijiaService  # noqa: E402
from app.ui import si_theme  # noqa: E402

si_theme.set_theme("dark")

_PASS: list[str] = []


def check(name: str) -> None:
    _PASS.append(name)
    print(f"  [PASS] {name}")


# spec 假数据：灯有 bool 可写 on 属性（有功能），插座 spec 为空（无功能）
SPEC_LAMP = {"properties": [{"name": "on", "type": "bool", "rw": ["w"],
                             "method": {"siid": 2, "piid": 1}}]}
SPEC_PLUG_EMPTY = {}

LAMP_SPEC = "urn:test:device:lamp:1"
PLUG_SPEC = "urn:test:device:plug:1"
UNKNOWN_SPEC = "urn:test:device:unknown:1"

_DEVICES = [
    {"did": "1001", "name": "台灯", "model": "fake.light.lamp1",
     "isOnline": True, "spec_type": LAMP_SPEC},
    {"did": "1002", "name": "插座", "model": "fake.plug.empty",
     "isOnline": True, "spec_type": PLUG_SPEC},
    # 云端不下发 spec_type：官方确认无公开规格，应立即隐藏
    {"did": "1003", "name": "温湿度计", "model": "fake.sensor.ht",
     "isOnline": True},
    # 有 spec_type 但查询失败/尚未返回：先显示，等探测结论
    {"did": "1004", "name": "未知规格设备", "model": "fake.unknown",
     "isOnline": True, "spec_type": UNKNOWN_SPEC},
]


class _FakeAPI:
    """纯内存 mijiaAPI 替身，不读真实账号、不发网络请求。"""

    available = True
    auth_data_path = _TMP / "mijia-api" / "auth.json"

    def get_homes_list(self):
        return [{"id": 1, "name": "家", "uid": 1,
                 "roomlist": [{"name": "客厅",
                               "dids": ["1001", "1002", "1003", "1004"]}]}]

    def get_devices_list(self):
        return [dict(item) for item in _DEVICES]

    def get_shared_devices_list(self):
        return []

    def get_scenes_list(self, home_id=None):
        return []

    def get_devices_prop(self, batch):
        # 开关探测：只有灯响应，其余设备 code!=0 模拟无此属性
        out = []
        for query in batch:
            if query["did"] == "1001":
                out.append({"did": "1001", "code": 0, "value": True})
            else:
                out.append({"did": query["did"], "code": -1})
        return out

    def get_properties(self, batch):
        return [{"did": query["did"], "code": -1} for query in batch]


class _FakeCuktech:
    """明确的本地充电器假源，替代原测试对真实本机 CUKTECH 服务的依赖。"""

    def connected(self):
        return True

    def status(self):
        return {"device_model": "njcuk.fitting.ad1204_"}


class _FakeService(MijiaService):
    def __init__(self):
        super().__init__(cuktech=_FakeCuktech())

    def _init_api(self):
        return _FakeAPI()

    def _fetch_spec(self, spec_type, cache_dir):
        # 不打网络：按 mijiaAPI 5.x 的 spec_type 返回内存假 spec
        if spec_type == LAMP_SPEC:
            return dict(SPEC_LAMP)
        if spec_type == PLUG_SPEC:
            return dict(SPEC_PLUG_EMPTY)
        return None  # 未知规格/无公开规格

    def cuktech_status(self):
        return {"connected": True, "ports": {}}


class _DeferredJobs:
    """把探测任务排队，由测试显式触发，避免异步竞态影响断言。"""

    def __init__(self):
        self.tasks = []

    def submit(self, fn, on_success=None, on_error=None):
        self.tasks.append((fn, on_success, on_error))

    def run_last(self):
        fn, on_success, on_error = self.tasks.pop()
        try:
            result = fn()
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
        else:
            if on_success is not None:
                on_success(result)

    def shutdown(self):
        self.tasks.clear()


class _NoTray:
    def set_tray_visible(self, visible):
        pass

    def hide_quick(self):
        pass

    def set_devices(self, *args, **kwargs):
        pass


def _card_dids(win) -> set:
    return set(win._cards.keys())


def main() -> int:
    import app.ui.main_window as mw

    _device_cache.save = lambda *a, **k: None  # 不写运行缓存
    service = _FakeService()
    jobs = _DeferredJobs()

    # 不构造真实账号/托盘/SSE：MainWindow 直接复用本测试的假服务
    mw.MijiaService = lambda **kwargs: service
    mw.TrayController = lambda *args, **kwargs: _NoTray()
    original_stream = mw.MainWindow._maybe_start_cuktech_stream
    mw.MainWindow._maybe_start_cuktech_stream = lambda self: None

    win = mw.MainWindow()
    original_jobs = win._jobs
    win._service = service
    win._jobs = jobs
    win._hide_no_func = True

    try:
        devices = service.list_devices()
        assert len(devices) == 5, devices  # 4 台云端 + 本地 CUKTECH
        win.show()
        app.processEvents()
        win._apply_devices(devices)
        app.processEvents()

        # ---- 1. 无 spec_type 立即隐藏；有 spec_type 未知保持等待；
        #         CUKTECH 本地设备始终可见 ----
        assert "1003" not in _card_dids(win), "无 spec_type 设备应立即隐藏"
        assert "1004" in _card_dids(win), "有规格但未拉取时应保留等待"
        assert "cuktech-local" in _card_dids(win), "本地 CUKTECH 必须可见"
        assert _card_dids(win) == {"1001", "1002", "1004", "cuktech-local"}, \
            _card_dids(win)
        assert service.spec_has_published_functions(UNKNOWN_SPEC) is None
        assert service.spec_has_published_functions("") is False
        check("无 spec_type 立即隐藏、有规格未知等待、CUKTECH 总可见")

        # ---- 2. 开关探测回调：spec 到达，无功能设备（插座/未知规格）消失 ----
        states = service.power_states(["1001", "1002", "1004"])
        assert states["1001"] is True
        assert states["1002"] is None and states["1004"] is None
        win._apply_power_states(states)
        app.processEvents()
        assert "1002" not in _card_dids(win), f"无功能设备未被隐藏: {_card_dids(win)}"
        assert "1004" not in _card_dids(win), f"未知规格设备未被隐藏: {_card_dids(win)}"
        assert "1001" in _card_dids(win), "有功能设备被误隐藏"
        assert "cuktech-local" in _card_dids(win), "CUKTECH 被误隐藏"
        check("power_states 回调后无功能/未知规格设备从网格消失")

        assert "2 台设备" in win._count_label.text(), win._count_label.text()
        check("计数标签按隐藏后数量更新")

        # ---- 3. 温湿度路径不改变可见集合时不重建（防每轮轮询全量重建） ----
        metrics = service.read_metrics(["1001", "1002", "1003", "1004"])
        rebuilds = []
        original = win._rebuild_grid
        win._rebuild_grid = lambda: (rebuilds.append(1), original())[1]
        cards_before = _card_dids(win)
        win._apply_metrics(metrics)
        app.processEvents()
        assert rebuilds == [], f"可见集合无变化却重建了网格: {rebuilds}"
        assert _card_dids(win) == cards_before
        win._rebuild_grid = original
        check("温湿度回调无可见变化时跳过重建")

        # ---- 4. 开关关闭：不触发隐藏重建分支 ----
        win._hide_no_func = False
        rebuilds.clear()
        win._rebuild_grid = lambda: (rebuilds.append(1), original())[1]
        win._apply_power_states({"1001": False, "1002": None})
        app.processEvents()
        assert rebuilds == [], "开关关闭时不应走重建分支"
        win._rebuild_grid = original
        check("开关关闭时不走隐藏重建分支（恢复展示由设置保存路径负责）")

        print(f"\n全部通过：{len(_PASS)} 项断言")
        return 0
    finally:
        win._force_quit = True
        win._tray = None
        win.close()
        jobs.shutdown()          # 先停后台线程再关窗：避免 QThread 销毁竞态警告
        original_jobs.shutdown()
        mw.MainWindow._maybe_start_cuktech_stream = original_stream


if __name__ == "__main__":
    raise SystemExit(main())
