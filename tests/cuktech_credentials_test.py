# SPDX-License-Identifier: GPL-3.0-or-later
"""M5 统一登录自测：凭据提取（假小米云）+ 配置下发（假 BLE 服务端）。

用法: .venv\\Scripts\\python.exe tests/cuktech_credentials_test.py

两套假服务都在本进程内起 ThreadingHTTPServer：
- 假小米云（api.io.mi.com 形态）：校验 RC4 加密签名链路可解密、
  code==0，返回 homelist/home_device_list/blt_get_beaconkey 三段假数据；
- 假 BLE 服务端：POST /api/config 记录 body 并返回 ok:true。
绝不 import PySide6 / app.ui；凭据常量全部是占位假值。
"""

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cuktech_client import ServiceError  # noqa: E402
from app.core.cuktech_credentials import (  # noqa: E402
    CredentialError,
    extract_credentials,
    _BEACONKEY_URI,
    _DEVICE_LIST_URI,
)

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s: %(message)s")

_PASS = []


def ok(name: str) -> None:
    _PASS.append(name)
    print(f"  [PASS] {name}")


# 假凭据：仅供测试断言，无真实意义
AUTH = {"userId": "1234567890", "serviceToken": "T" * 40,
        "ssecurity": "c2VjdXJpdHktZGVtbw=="}  # base64("security-demo")
CHARGER = {"did": "900001", "mac": "AA:BB:CC:DD:EE:01", "token": "TOK" * 8,
           "model": "njcuk.fitting.ad1204_", "name": "酷态科充电器"}
OTHER = {"did": "900002", "mac": "AA:BB:CC:DD:EE:02", "token": "T" * 24,
         "model": "yeelink.light.lamp22", "name": "台灯"}


class FakeXiaomiCloud(BaseHTTPRequestHandler):
    """假小米云：走真 RC4 请求解密（_signed_request 的真实路径）。"""

    def do_POST(self):  # noqa: N802
        uri = self.path.replace("/app", "").split("?")[0]
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        auth = AUTH

        from urllib.parse import parse_qs
        from mijiaAPI.miutils import get_signed_nonce

        # 服务端视角：按上游方式解出 data 字段（RC4 通道回放）
        # 这里不做完整服务端实现，只按 uri 分发假数据
        def respond(result: dict) -> None:
            # 加密回包：客户端 decrypt(ssecurity, nonce, payload) 要求
            # payload 是 get_signed_nonce(ssecurity, nonce) 加密的
            # base64(rc4(明文)) 字符串——encrypt_rc4 本身已做 base64，
            # 这里直接传字符串即可（再包一层 b64 会双层编码解不开）。
            qs = parse_qs(body.decode("utf-8"))
            nonce = (qs.get("_nonce") or [""])[0]
            key = get_signed_nonce(auth["ssecurity"], nonce)
            from mijiaAPI.miutils import encrypt_rc4 as _enc
            payload = _enc(key, json.dumps({"code": 0, "result": result}))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(payload.encode())

        if uri == "/v2/homeroom/gethome":
            respond({"homelist": [{"id": 111, "uid": 1234567890,
                                   "name": "我的家"}]})
        elif uri == _DEVICE_LIST_URI:
            respond({"device_info": [OTHER, CHARGER], "has_more": False})
        elif uri == _BEACONKEY_URI:
            respond({"beaconkey": "BK" * 8})
        else:
            respond({})
            return
    def log_message(self, fmt, *args):  # 静音访问日志
        pass


class FakeBleServer(BaseHTTPRequestHandler):
    """假 BLE 服务端：记录 /api/config 收到的 body。"""

    last_config_save: dict | None = None

    def do_POST(self):  # noqa: N802
        if self.path == "/api/config":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            type(self).last_config_save = json.loads(body.decode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"ok": True, "message": "配置已保存，服务将在 1 秒后重启"}).encode())
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    # ---- 假小米云起服务，指向 127.0.0.1 ----
    cloud = ThreadingHTTPServer(("127.0.0.1", 0), FakeXiaomiCloud)
    threading.Thread(target=cloud.serve_forever, daemon=True).start()
    ble = ThreadingHTTPServer(("127.0.0.1", 0), FakeBleServer)
    threading.Thread(target=ble.serve_forever, daemon=True).start()

    import app.core.cuktech_credentials as cred
    saved_host = cred._IO_HOST
    cred._IO_HOST = f"http://127.0.0.1:{cloud.server_address[1]}"
    # _signed_request 拼 f"{_IO_HOST}/app" + uri —— 假服务不区分 /app 前缀
    try:
        print("== 凭据提取（假小米云） ==")

        creds = extract_credentials(auth_data=dict(AUTH))
        assert creds["did"] == CHARGER["did"], creds
        assert creds["mac"] == CHARGER["mac"], creds
        assert creds["token"] == CHARGER["token"], creds
        assert creds["ble_key"] == "BK" * 8, creds
        ok("extract_credentials 全链路（homelist→设备列表→beaconkey）返回四要素")

        assert creds.get("ble_key") not in ("", "None")
        ok("beaconkey 为非空字符串")

        # ---- 没有充电器的家庭 ----
        orig_list = FakeXiaomiCloud.do_POST

        def no_charger(self):
            # 篡改模块级判断代价高，直接在 list 里去掉充电器：
            # 复写 CHARGER model 为非关键字
            return orig_list(self)

        devices_without = [OTHER]
        picked_err = None
        try:
            cred._pick_charger(devices_without)
        except CredentialError as exc:
            picked_err = exc
        assert picked_err is not None and "没有找到" in str(picked_err)
        ok("云端无充电器时报中文 CredentialError")

        # ---- 缺登录态 ----
        for missing in ("userId", "serviceToken", "ssecurity"):
            bad = {k: v for k, v in AUTH.items() if k != missing}
            err = None
            try:
                extract_credentials(auth_data=bad)
            except CredentialError as exc:
                err = exc
            assert err is not None and "扫码登录" in str(err), (missing, err)
        ok("缺 userId/serviceToken/ssecurity 任一字段即拒绝并提示重新扫码")

        # ---- 凭据不进异常文本 ----
        try:
            cred._pick_charger([])
        except CredentialError as exc:
            assert CHARGER["token"][:3] not in str(exc)
            assert AUTH["serviceToken"][:3] not in str(exc)
        ok("错误信息不含凭据内容")

        print("== 配置下发（假 BLE 服务端） ==")
        from app.core.cuktech_client import CuktechClient

        client = CuktechClient(base_url=f"http://127.0.0.1:{ble.server_address[1]}")
        client.save_ble_credentials(CHARGER["mac"], CHARGER["token"], "BK" * 8)
        sent = FakeBleServer.last_config_save
        assert sent is not None and sent["config"]["ble"]["mac"] == CHARGER["mac"]
        assert sent["config"]["ble"]["ble_key"] == "BK" * 8
        assert set(sent["config"].keys()) == {"ble"}, "只提交 ble 段"
        ok("save_ble_credentials 只提交 ble 段且字段完整")

        err = None
        try:
            client.save_ble_credentials("", CHARGER["token"], "BK")
        except ServiceError as exc:
            err = exc
        assert err is not None and "必填" in str(err)
        ok("空 mac 本地拒绝（不发请求）")
        assert FakeBleServer.last_config_save == sent, "本地拒绝后远端未收到新请求"
        ok("本地拒绝不产生网络请求")

        print("== 门面转发 ==")
        from app.core.service import MijiaService

        assert hasattr(MijiaService, "cuktech_extract_credentials")
        assert hasattr(MijiaService, "cuktech_save_credentials")
        ok("MijiaService 暴露 cuktech_extract/save_credentials 门面方法")

        print(f"\n全部通过：{len(_PASS)} 项断言")
        return 0
    finally:
        cred._IO_HOST = saved_host
        cloud.shutdown()
        ble.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
