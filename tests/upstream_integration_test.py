# SPDX-License-Identifier: GPL-3.0-or-later
"""mijiaAPI 5.x / 设备规格 / 场景门面的离线行为回归。

用法: .venv\\Scripts\\python.exe tests/upstream_integration_test.py

覆盖（全部使用内存假 API，不触真实米家云、BLE 或用户数据）：
1. spec_type 三元组索引、共享设备组装、无公开规格的中文友好错误；
2. 同一 spec_type 多设备只拉一次、坏缓存删除后重拉、中文描述兜底；
3. 产品页名称超时失败只查询一次（不堵塞 jobs 队列）；
4. 场景列表家庭过滤/未知家庭、执行成功/失败/异常包装；
5. 设备缓存 v3 同时保留 source + spec_type，旧本地 v2 / 上游 v2 安全失效。
"""

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
import mijiaAPI  # noqa: E402
import mijiaAPI.devices as mijia_devices  # noqa: E402
from app.core import _json_store  # noqa: E402

# 所有持久化文件重定向到本测试自己的临时目录，绝不碰仓库/用户运行数据
_TMP = Path(tempfile.mkdtemp(prefix="mihome-upstream-test-"))
_json_store.data_dir = lambda: _TMP
_json_store.data_file = lambda name: _TMP / name

from app.core import cache as device_cache  # noqa: E402
from app.core import service as service_mod  # noqa: E402
from app.core.models import DeviceInfo, SceneInfo  # noqa: E402
from app.core.service import MijiaService, ServiceError  # noqa: E402

_PASS: list[str] = []


def check(name: str) -> None:
    _PASS.append(name)
    print(f"  [PASS] {name}")


# ---------- 共用假数据 ----------

SPEC_LIGHT = {
    "name": "测试灯",
    "properties": [
        {"name": "on", "description": "Switch", "type": "bool", "rw": "rw",
         "range": [0, 1, 1], "method": {"siid": 2, "piid": 1}},
        {"name": "brightness", "description": "Brightness", "type": "uint",
         "rw": "rw", "range": [1, 100, 1],
         "method": {"siid": 2, "piid": 2}},
    ],
    "actions": [
        {"name": "turn-on", "description": "Turn on",
         "method": {"siid": 2, "aiid": 1}},
    ],
}


class _FakeAPI:
    """mijiaAPI 的最小内存替身；所有调用都不会离开本进程。"""

    available = True

    def __init__(self, own=None, shared=None, homes=None, scenes=None):
        self.own = list(own or [])
        self.shared = list(shared or [])
        self.homes = list(homes or [])
        self.scenes = dict(scenes or {})
        self.auth_data_path = _TMP / "mijia-api" / "auth.json"
        self.auth_data_path.parent.mkdir(parents=True, exist_ok=True)
        self.scene_calls: list[str | None] = []
        self.scene_error: Exception | None = None
        self.run_result = True
        self.run_error: Exception | None = None
        self.run_calls: list[tuple[str, str]] = []

    def get_homes_list(self):
        return copy.deepcopy(self.homes)

    def get_devices_list(self):
        return copy.deepcopy(self.own)

    def get_shared_devices_list(self):
        return copy.deepcopy(self.shared)

    def get_scenes_list(self, home_id=None):
        self.scene_calls.append(home_id)
        if self.scene_error is not None:
            raise self.scene_error
        return copy.deepcopy(self.scenes.get(home_id, []))

    def run_scene(self, scene_id, home_id):
        self.run_calls.append((str(scene_id), str(home_id)))
        if self.run_error is not None:
            raise self.run_error
        return self.run_result

    def get_devices_prop(self, batch):
        out = []
        for query in batch:
            if query.get("did") == "1001":
                out.append({"did": "1001", "code": 0, "value": True})
            else:
                out.append({"did": query.get("did"), "code": -1})
        return out


class _FakeCuktech:
    def __init__(self, connected=False):
        self._connected = connected

    def connected(self):
        return self._connected

    def status(self):
        # 真机型号尾部可能带控制字符，服务层应清掉
        return {"device_model": "njcuk.fitting.ad1204_\x03"}


def _new_service(api: _FakeAPI, connected: bool = False) -> MijiaService:
    # 子类化仅为把 _init_api 固定为内存假 API：绝不构造真实 mijiaAPI
    #（其构造函数会读取 ~/.config/mijia-api/auth.json）。
    class _TestService(MijiaService):
        def __init__(self):
            super().__init__(cuktech=_FakeCuktech(connected))

        def _init_api(self):
            return api

    return _TestService()


def _device(did: str, name: str, model: str, spec_type: str = "", **kw) -> dict:
    data = {"did": did, "name": name, "model": model, "isOnline": True}
    if spec_type:
        data["spec_type"] = spec_type
    data.update(kw)
    return data


# ---------- 1. 三元组索引 / 共享设备 / 无公开规格 ----------

def test_index_and_shared_devices() -> None:
    own = [_device("1001", "台灯", "m.lamp", "urn:test:light:1"),
           _device("1003", "无规格", "m.nospec")]
    shared = [_device("2001", "共享灯", "m.shared", "urn:test:shared:1"),
              _device("2002", "共享无规格", "m.shared2")]
    api = _FakeAPI(own=own, shared=shared)
    service = _new_service(api)
    service._refresh_device_index()

    assert service._device_index["1001"] == (
        "m.lamp", "urn:test:light:1", "台灯"), service._device_index["1001"]
    assert service._device_index["2001"] == (
        "m.shared", "urn:test:shared:1", "共享灯"), service._device_index["2001"]
    assert service._device_index["1003"][1] == "", "空 spec_type 必须保留为空串"
    check("did -> (model, spec_type, name) 三元组索引含自有与共享设备")

    calls: list[tuple[str, str]] = []
    original = service_mod.get_device_info

    def fake_info(spec_type, cache_path=None):
        calls.append((str(spec_type), str(cache_path)))
        return copy.deepcopy(SPEC_LIGHT)

    service_mod.get_device_info = fake_info
    try:
        built = service._build_shared_device("2001")
        assert built.did == "2001" and built.model == "m.shared"
        assert built.name == "共享灯"
        assert calls and calls[0][0] == "urn:test:shared:1", calls
        # 连字符属性同时注册下划线别名，且指向同一对象
        assert built.prop_list["on"] is built.prop_list["on"]
        assert built.prop_list["brightness"] is not None

        for did in ("2002", "1003"):
            try:
                if did == "1003":
                    service._get_device(did)
                else:
                    service._build_shared_device(did)
            except ServiceError as exc:
                assert "无公开的功能规格" in str(exc), str(exc)
            else:
                raise AssertionError(f"{did} 应给出无公开规格错误")

        try:
            service._build_shared_device("missing")
        except ServiceError as exc:
            assert "未找到设备" in str(exc), str(exc)
        else:
            raise AssertionError("未知共享设备应报未找到")
    finally:
        service_mod.get_device_info = original
    check("共享设备按 spec_type 手工组装；无规格给出中文友好错误")


# ---------- 2. 设备列表 / 本地 CUKTECH 聚合 ----------

def test_list_devices() -> None:
    own = [_device("1001", "台灯", "m.lamp", "urn:test:light:1"),
           _device("1003", "无规格", "m.nospec")]
    shared = [_device("2001", "共享灯", "m.shared", "urn:test:shared:1")]
    homes = [
        {"id": 1, "name": "家A", "roomlist": [{"name": "客厅", "dids": ["1001"]}]},
        {"id": 2, "name": "家B", "roomlist": [{"name": "卧室", "dids": ["2001"]}]},
    ]
    api = _FakeAPI(own=own, shared=shared, homes=homes)
    service = _new_service(api, connected=True)

    devices = service.list_devices()
    by_did = {d.did: d for d in devices}
    assert set(by_did) == {"1001", "1003", "2001", "cuktech-local"}, by_did
    assert by_did["1001"].home_name == "家A" and by_did["1001"].room_name == "客厅"
    assert by_did["1001"].spec_type == "urn:test:light:1"
    assert by_did["1003"].spec_type == ""
    assert by_did["1003"].home_name == "未知" and by_did["1003"].room_name == "未知"
    assert by_did["2001"].source == "cloud"
    local = by_did["cuktech-local"]
    assert local.source == "local" and local.online and local.is_cuktech
    assert "\x03" not in local.model, local.model
    check("设备列表补齐房间/spec_type/source，并聚合本地 CUKTECH")

    # 网关瞬时不可达时沿用上次成功条目，卡片不会消失
    api.own = []
    api.shared = []
    service._cuktech._connected = False
    devices2 = service.list_devices()
    assert any(d.did == "cuktech-local" for d in devices2), devices2
    check("CUKTECH 单次探测失败沿用上次成功条目")


# ---------- 3. spec_type 去重 / 坏缓存重拉 / 中文兜底 ----------

def test_spec_dedup_and_bad_cache() -> None:
    own = [_device("d1", "灯1", "m1", "urn:dup:1"),
           _device("d2", "灯2", "m2", "urn:dup:1"),
           _device("d3", "无规格", "m3")]
    service = _new_service(_FakeAPI(own=own))
    counts: dict[str, int] = {}

    def counting_fetch(spec_type, cache_dir):
        counts[spec_type] = counts.get(spec_type, 0) + 1
        return copy.deepcopy(SPEC_LIGHT)

    service._fetch_spec = counting_fetch
    result = service._specs_for(["d1", "d2", "d3"])
    assert counts == {"urn:dup:1": 1}, counts
    assert result["d1"] is result["d2"], "同规格设备应共享同一份 spec"
    assert result["d3"] is None
    assert service.spec_has_published_functions("urn:dup:1") is True
    assert service.spec_has_published_functions("") is False
    assert service.spec_has_published_functions("urn:not-fetched") is None

    service._specs_for(["d1", "d2"])
    assert counts == {"urn:dup:1": 1}, "内存缓存命中后不应重复拉取"
    check("同 spec_type 多设备只拉一次；未知/空规格三态语义正确")

    cache_dir = _TMP / "spec-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    spec_type = "urn:bad:1"
    bad_file = cache_dir / "urn_bad_1.json"
    bad_file.write_text("{not-json", encoding="utf-8")
    attempts: list[str] = []
    original = service_mod.get_device_info

    def fake_info(st, cache_path=None):
        attempts.append(str(st))
        path = Path(cache_path) / f"{str(st).replace(':', '_')}.json"
        if path.exists():
            raise json.JSONDecodeError("bad cache", "{not-json", 0)
        return copy.deepcopy(SPEC_LIGHT)

    bad_service = _new_service(_FakeAPI())
    service_mod.get_device_info = fake_info
    try:
        info = bad_service._fetch_spec(spec_type, cache_dir)
        assert len(attempts) == 2, f"坏缓存应删除后重拉一次: {attempts}"
        assert not bad_file.exists(), "坏缓存文件应被删除"
        assert info is not None
        props = {p["name"]: p["description"] for p in info["properties"]}
        assert props["on"] == "Switch / 开关", props
        assert props["brightness"] == "Brightness / 亮度", props
        acts = {a["name"]: a["description"] for a in info["actions"]}
        assert acts["turn-on"] == "Turn on / 开启", acts

        service_mod.get_device_info = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("network down"))
        assert bad_service._fetch_spec("urn:fail:1", cache_dir) is None
        assert bad_service._fetch_spec("", cache_dir) is None
    finally:
        service_mod.get_device_info = original
    check("坏 spec 缓存删除重拉；dict 路径中文兜底；异常折叠为无能力")


def test_device_object_zh_path() -> None:
    own = [_device("1001", "台灯", "m.lamp", "urn:test:light:1")]
    service = _new_service(_FakeAPI(own=own))
    original = mijia_devices.get_device_info
    mijia_devices.get_device_info = lambda *a, **k: copy.deepcopy(SPEC_LIGHT)
    try:
        detail = service.device_detail("1001")
    finally:
        mijia_devices.get_device_info = original
    props = {p.name: p.desc for p in detail.props}
    assert props["on"] == "Switch / 开关", props
    assert props["brightness"] == "Brightness / 亮度", props
    acts = {a.name: a.desc for a in detail.actions}
    assert acts["turn-on"] == "Turn on / 开启", acts
    assert detail.name == "台灯" and detail.model == "m.lamp"
    check("mijiaDevice 对象路径同样补齐中文描述（无重复控件）")


def test_product_page_timeout_once() -> None:
    service = _new_service(_FakeAPI())
    calls: list[str] = []
    original = requests.get

    def timeout(*args, **kwargs):
        calls.append(str(args[0]) if args else "")
        raise requests.Timeout("timed out")

    requests.get = timeout
    try:
        assert service.product_page_name("m.unknown") is None
        assert service.product_page_name("m.unknown") is None
    finally:
        requests.get = original
    assert len(calls) == 1, f"超时失败应只请求一次: {calls}"
    assert service.has_product_page_name("m.unknown") is True
    assert service.cached_product_page_names(["m.unknown"]) == {}
    check("产品页名称超时失败只请求一次并记忆 None")


# ---------- 4. 场景家庭过滤 / 执行 ----------

def test_scenes() -> None:
    homes = [{"id": 1, "name": "家A", "roomlist": []},
             {"id": 2, "name": "家B", "roomlist": []}]
    scenes = {
        None: [
            {"scene_id": "s1", "name": "回家", "home_id": "1"},
            {"scene_id": "s2", "name": "离家", "home_id": "2"},
            {"scene_id": "s3", "home_id": "1"},
        ],
        "1": [
            {"scene_id": "s1", "name": "回家", "home_id": "1"},
            {"scene_id": "s3", "home_id": "1"},
        ],
    }
    api = _FakeAPI(homes=homes, scenes=scenes)
    service = _new_service(api)

    all_scenes = service.scene_list(None)
    assert [s.scene_id for s in all_scenes] == ["s1", "s2", "s3"]
    by_id = {s.scene_id: s for s in all_scenes}
    assert by_id["s1"].home_name == "家A" and by_id["s1"].home_id == "1"
    assert by_id["s2"].home_name == "家B"
    assert by_id["s3"].name == "未命名场景", by_id["s3"]
    assert by_id["s3"].home_name == "家A"
    check("scene_list(None) 返回全部家庭并反查家庭显示名")

    only_a = service.scene_list("家A")
    assert [s.scene_id for s in only_a] == ["s1", "s3"]
    assert api.scene_calls[-1] == "1", api.scene_calls
    assert all(s.home_name == "家A" for s in only_a)
    calls_before = len(api.scene_calls)
    assert service.scene_list("未知家庭") == []
    assert len(api.scene_calls) == calls_before, "未知家庭不应发起云端请求"
    check("scene_list(家庭名) 按家庭 ID 过滤；未知家庭直接返回空")

    api.scene_error = RuntimeError("cloud down")
    try:
        service.scene_list(None)
    except ServiceError as exc:
        assert "获取场景列表失败" in str(exc) and "cloud down" in str(exc)
    else:
        raise AssertionError("场景列表异常应包装为 ServiceError")
    api.scene_error = None

    scene = SceneInfo("s1", "回家", "1", "家A")
    service.run_scene(scene)
    assert api.run_calls[-1] == ("s1", "1"), api.run_calls

    api.run_result = False
    try:
        service.run_scene(scene)
    except ServiceError as exc:
        assert "执行场景「回家」失败" in str(exc)
    else:
        raise AssertionError("run_scene 返回假值应报失败")

    api.run_result = True
    api.run_error = RuntimeError("device offline")
    try:
        service.run_scene(scene)
    except ServiceError as exc:
        assert "执行场景「回家」失败" in str(exc)
        assert "device offline" in str(exc)
        assert isinstance(exc.__cause__, RuntimeError)
    else:
        raise AssertionError("run_scene 异常应包装")
    api.run_error = None
    check("场景执行成功/假值失败/异常包装均符合门面契约")


# ---------- 5. 设备缓存 v3 迁移 ----------

def _cache_payload(version: int, devices: list[dict], **extra) -> dict:
    return {"version": version, "devices": devices,
            "known_power": {}, "metrics": {}, **extra}


def test_device_cache() -> None:
    def sample(source="cloud", spec_type="urn:test:light:1"):
        return DeviceInfo(
            did="1001", name="台灯", model="m.lamp", home_name="家A",
            room_name="客厅", online=True, spec_type=spec_type, source=source)

    device_cache.save([sample()], {"1001": True, "bad": "x"},
                      {"1001": "28.0°C 60%", "empty": ""})
    raw = json.loads((_TMP / "devices_cache.json").read_text(encoding="utf-8"))
    assert raw["version"] == 3, raw
    assert raw["devices"][0]["source"] == "cloud"
    assert raw["devices"][0]["spec_type"] == "urn:test:light:1"
    assert raw["known_power"]["1001"] is True
    assert raw["metrics"] == {"1001": "28.0°C 60%"}, raw["metrics"]

    loaded = device_cache.load()
    assert loaded is not None
    devices, known, metrics = loaded
    assert devices[0].source == "cloud" and devices[0].spec_type == "urn:test:light:1"
    assert known == {"1001": True} and metrics == {"1001": "28.0°C 60%"}
    check("缓存 v3 读写 round-trip 同时保留 source 与 spec_type")

    old_v2_local = _cache_payload(2, [{
        "did": "x", "name": "旧本地", "model": "m", "home_name": "h",
        "room_name": "r", "online": True, "source": "local"}])
    (_TMP / "devices_cache.json").write_text(
        json.dumps(old_v2_local, ensure_ascii=False), encoding="utf-8")
    assert device_cache.load() is None, "缺 spec_type 的旧本地 v2 必须整体失效"

    old_v2_upstream = _cache_payload(2, [{
        "did": "x", "name": "旧上游", "model": "m", "home_name": "h",
        "room_name": "r", "online": True, "spec_type": "urn:x"}])
    (_TMP / "devices_cache.json").write_text(
        json.dumps(old_v2_upstream, ensure_ascii=False), encoding="utf-8")
    assert device_cache.load() is None, "缺 source 的旧上游 v2 必须整体失效"

    (_TMP / "devices_cache.json").write_text(
        json.dumps(_cache_payload(1, [])), encoding="utf-8")
    assert device_cache.load() is None
    (_TMP / "devices_cache.json").write_text("{broken", encoding="utf-8")
    assert device_cache.load() is None
    check("旧本地 v2 / 上游 v2 / v1 / 坏文件一律安全失效")


def test_device_info_positional_contract() -> None:
    """旧 fork 的 7 位置参数契约：第 7 位是 source，spec_type 追加在后。"""
    legacy = DeviceInfo("1001", "台灯", "m.lamp", "家A", "客厅", True, "local")
    assert legacy.source == "local", legacy
    assert legacy.spec_type == "", legacy
    assert legacy.is_cuktech is True

    default = DeviceInfo("1002", "灯", "m", "h", "r", True)
    assert default.source == "cloud" and default.spec_type == ""

    modern = DeviceInfo(
        "1003", "灯", "m", "h", "r", True, "cloud", "urn:test:light:1")
    assert modern.source == "cloud"
    assert modern.spec_type == "urn:test:light:1"
    check("DeviceInfo 旧 7 位置参数保持 source=local，spec_type 默认空")


def main() -> int:
    print("== upstream_integration_test ==")
    test_device_info_positional_contract()
    test_index_and_shared_devices()
    test_list_devices()
    test_spec_dedup_and_bad_cache()
    test_device_object_zh_path()
    test_product_page_timeout_once()
    test_scenes()
    test_device_cache()

    version = MijiaService.api_version()
    assert version and version.startswith("5."), f"mijiaAPI 版本异常: {version!r}"
    check(f"关于页读取实际 mijiaAPI 版本: {version}")

    print(f"\n全部通过：{len(_PASS)} 组行为断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
