# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电器云端凭据提取（M5 统一登录）。

从 mijiaAPI 的已登录会话直接提取充电器的云端凭据（did / MAC / token /
beaconkey），写入本地 BLE 服务端配置，消除"桌面端扫一次码、BLE 服务端
再扫一次码"的二次登录。

实现依据（ADR-009，2026-09-28 核查；2026-09-29 实证修订）：
- mijiaAPI auth.json 已含 userId / serviceToken / ssecurity 三要素，
  ``miutils`` 提供 RC4 签名加密原语，与小米云 API 的加密通道完全一致；
- **serviceToken 有服务域之分（2026-09-29 实证）**：mijiaAPI QR 登录走
  ``sid=mijia``（apis.py service_login_url），换得的 serviceToken 只对
  ``api.mijia.tech`` 有效；beaconkey/设备列表端点在 ``api.io.mi.com``，
  该域要求 ``sid=xiaomiio`` 域的 token，mijia 的 token 直调一律返回
  HTTP 401 {"code":2,"message":"auth error"}（本机真机复现，与请求
  编码风格/cookie 补全无关）。因此先拿 auth.json 的 passToken 走
  ``account.xiaomi.com/pass/serviceLogin?_json=true&sid=xiaomiio``
  换发 io.mi.com 域 token（与上游 xiaomi_cloud.py 登录路径同源），
  换发成功后用返回的 ssecurity + 新 serviceToken 签名请求；
- 服务端点 ``POST {io_host}/v2/device/blt_get_beaconkey``（data 为
  JSON 的 {"did": ..., "pdid": 1}），响应 result.beaconkey 即 BLE key；
- 云端设备列表（get_devices_list 返回的原始 dict）里含 did / mac /
  token / model 字段，按 model 含 njcuk/fitting 识别充电器（与 BLE
  服务端 ha_server.py:1408 的过滤口径一致）。

与上游 cuktech-ble-server xiaomi_cloud.py 的关键差异（勿"对齐"回去）：
- 加密签名对 URI 敏感：_generate_enc_signature 用 ``url.split("com")[1]
  .replace("/app/", "/")`` 归一化路径。mijiaAPI 的 ``gen_enc_signature``
  直接对传入 uri 计算，因此**必须传归一化路径**（"/v2/device/..."）而非
  完整 URL，否则签名不一致、服务端返回非法请求（本地实证两签名不同）；
- beaconkey 是长期 BLE 凭据，**严禁写日志或异常文本**（上游曾在 info
  级日志整体泄露，见代码审查 2026-09-28）——本模块只记录"是否存在"；
- passToken 换发响应含新 ssecurity/location，同样属登录凭据，**只记
  成败与状态码，不记内容**。

线程约定：纯同步、可重入性无保证，由调用方（service 门面）经 jobs
串行队列在后台线程调用。
"""

import json
import logging
from typing import Any

import requests

from app import __version__
from mijiaAPI.miutils import (
    decrypt_rc4,
    gen_nonce,
    generate_enc_params,
    get_signed_nonce,
)

logger = logging.getLogger(__name__)

# 小米 IoT 开放 API 主机（上游 xiaomi_cloud.SERVERS["cn"]）。beaconkey
# 是 BLE 设备专属端点，mijiaAPI 的 api_base_url（api.mijia.tech）不提供
_IO_HOST = "https://api.io.mi.com"
# 归一化路径：签名计算与请求路径共用（上游对完整 URL 做 split("com")
# 归一化后的等价物，已本地实证签名一致性问题，见模块 docstring）
_BEACONKEY_URI = "/v2/device/blt_get_beaconkey"
_DEVICE_LIST_URI = "/home/home_device_list"

# passToken 换发 io.mi.com 域 serviceToken 的账户端点（sid 必须是
# xiaomiio——mijia 域 token 对 io.mi.com 无效，实证见模块 docstring）。
# 返回 ssecurity 与一次性 location，GET location 后 cookie 里带新 token
_SERVICE_LOGIN_URL = ("https://account.xiaomi.com/pass/serviceLogin"
                      "?_json=true&sid=xiaomiio&_locale=zh_CN")

# 充电器型号识别口径：model 含 njcuk（真机 njcuk.fitting.ad1204_）或
# fitting（上游 ha_server 同款宽匹配），大小写不敏感
_MODEL_KEYWORDS = ("njcuk", "fitting")


class CredentialError(Exception):
    """凭据提取失败；message 为用户可读中文，不含任何凭据内容。"""


def _is_cuktech_model(model: str) -> bool:
    model = (model or "").lower()
    return any(k in model for k in _MODEL_KEYWORDS)


def _exchange_io_token(session: requests.Session,
                       auth: dict[str, Any]) -> tuple[str, str, str]:
    """用 passToken 换发 io.mi.com 域 serviceToken，返回 (userId, serviceToken, ssecurity)。

    mijiaAPI 登录的 serviceToken 是 mijia 域的，对 api.io.mi.com 一律
    401（实证见模块 docstring）；beaconkey 链路必须先经账户侧
    serviceLogin（sid=xiaomiio）换发。ssecurity 用换发响应新返回的
    （与 token 同域配套），签名才一致。换发响应/请求不带凭据落日志。
    """
    pass_token = str(auth.get("passToken") or "")
    if not pass_token:
        raise CredentialError(
            "登录凭据缺少 passToken，无法访问小米 IoT 服务，请重新扫码登录")
    try:
        resp = session.get(
            _SERVICE_LOGIN_URL,
            cookies={"userId": str(auth["userId"]), "passToken": pass_token},
            timeout=(5, 30))
    except requests.RequestException as exc:
        raise CredentialError(f"小米账户服务请求失败：{exc}") from exc
    text = resp.text.strip()
    if text.startswith("&&&START&&&"):
        text = text[len("&&&START&&&"):]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CredentialError(
            f"小米账户服务返回异常（HTTP {resp.status_code}）") from exc
    if not isinstance(data, dict) or data.get("code") != 0 or not data.get("location"):
        raise CredentialError(
            "小米账户登录态已失效（换发 IoT 凭据被拒），请重新扫码登录")
    # location 一次性：GET 后 cookie 才带上 io 域 serviceToken
    try:
        resp = session.get(str(data["location"]), timeout=(5, 30))
    except requests.RequestException as exc:
        raise CredentialError(f"小米账户服务请求失败：{exc}") from exc
    token = resp.cookies.get("serviceToken") or session.cookies.get("serviceToken")
    if not token:
        raise CredentialError(
            "小米账户未返回 IoT 服务凭据，请重新扫码登录")
    user_id = str(data.get("userId") or auth.get("userId") or "")
    ssecurity = str(data.get("ssecurity") or auth.get("ssecurity") or "")
    return user_id, token, ssecurity


def _signed_request(session: requests.Session, auth: dict[str, Any],
                    uri: str, data: dict[str, Any]) -> dict[str, Any]:
    """向小米云 IoT API 发一次 RC4 加密签名请求并返回解密后的 result。

    复用 mijiaAPI 的 miutils 原语与已登录会话 cookie；uri 传归一化
    路径（见模块 docstring 的签名敏感性说明）。响应解密失败/非 JSON/
    code != 0 统一抛 CredentialError，异常文本只含状态码不含凭据。
    """
    host = f"{_IO_HOST}/app"
    url = host + uri
    params = {"data": json.dumps(data, separators=(",", ":"))}
    nonce = gen_nonce()
    ssecurity = str(auth["ssecurity"])
    signed_nonce = get_signed_nonce(ssecurity, nonce)
    enc_params = generate_enc_params(uri, "POST", signed_nonce, nonce,
                                     params, ssecurity)
    cookies = {
        "userId": str(auth["userId"]),
        "serviceToken": str(auth["serviceToken"]),
        "yetAnotherServiceToken": str(auth["serviceToken"]),
    }
    headers = {
        "Accept-Encoding": "identity",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
        "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        # 上游用随机 agent；固定可读 agent 便于小米侧识别量级，
        # 也避免每次请求生成新 agent 造成指纹噪声
        "User-Agent": f"MiHome-Ex/{__version__}",
    }
    try:
        resp = session.post(url, data=enc_params, headers=headers,
                            cookies=cookies, timeout=(5, 30))
    except requests.RequestException as exc:
        # RequestException 文本可能含 URL（无凭据），可安全展示
        raise CredentialError(f"小米云请求失败：{exc}") from exc
    if resp.status_code != 200:
        raise CredentialError(f"小米云返回 HTTP {resp.status_code}")
    # RC4 解密。mijiaAPI 的 decrypt() 遇 UTF-8 解码失败会当 gzip 兜底，
    # 我们请求时已声明 Accept-Encoding: identity、响应恒为明文 JSON，
    # 因此直接用 RC4 原语解，避免 gzip 兜底把解密失败伪装成
    # BadGzipFile 而丢失真实原因
    try:
        raw = decrypt_rc4(get_signed_nonce(ssecurity, nonce), resp.text)
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CredentialError(
            f"小米云响应解密失败（登录态可能已失效，请重新扫码）：{type(exc).__name__}"
        ) from exc
    if not isinstance(result, dict) or result.get("code", -1) != 0:
        code = result.get("code") if isinstance(result, dict) else "?"
        raise CredentialError(f"小米云拒绝请求（code={code}）")
    return result


def _device_list(session: requests.Session, auth: dict[str, Any]) -> list[dict]:
    """拉取全部家庭的云端设备原始列表（含 token/mac 字段）。

    auth 为换发后的 io 域凭据（userId/serviceToken/ssecurity 三要素）。
    """
    home_ret = _signed_request(session, auth, "/v2/homeroom/gethome", {
        "fg": True, "fetch_share": True, "fetch_share_dev": True,
        "limit": 300, "app_ver": 7,
    })
    homes = (home_ret.get("result") or {}).get("homelist") or []
    devices: list[dict] = []
    for home in homes:
        home_id = home.get("id")
        owner_id = home.get("uid") or auth.get("userId")
        if home_id is None:
            continue
        ret = _signed_request(session, auth, _DEVICE_LIST_URI, {
            "home_owner": owner_id,
            "home_id": int(home_id),
            "limit": 200,
            "get_split_device": True,
            "support_smart_home": True,
        })
        result = ret.get("result") or {}
        dev_list = result.get("device_info") or result.get("list") or []
        devices.extend(d for d in dev_list if isinstance(d, dict))
    return devices


def _pick_charger(devices: list[dict]) -> dict:
    """从云端设备列表挑出第一台 CUKTECH 充电器；没有则报错。"""
    for d in devices:
        if _is_cuktech_model(str(d.get("model") or "")):
            return d
    raise CredentialError(
        "小米云端设备列表里没有找到 CUKTECH 充电器——"
        "请确认充电器已在米家 APP 中绑定")


def _beaconkey(session: requests.Session, auth: dict[str, Any],
               did: str) -> str:
    """拉取指定设备的 BLE beaconkey（长期凭据，严禁落日志）。"""
    ret = _signed_request(session, auth, _BEACONKEY_URI,
                          {"did": str(did), "pdid": 1})
    key = str(((ret.get("result") or {}).get("beaconkey")) or "")
    if not key:
        # 只报"未取到"，绝不把响应体放进异常/日志
        raise CredentialError(
            "小米云未返回该设备的蓝牙凭据（设备可能未开通 BLE 接入）")
    return key


def extract_credentials(auth_data: dict[str, Any] | None = None,
                        session: requests.Session | None = None,
                        ) -> dict[str, str]:
    """从已登录会话提取充电器凭据，返回 {did, mac, token, ble_key}。

    auth_data 缺省读 mijiaAPI 标准位置的 auth.json（只取字段值，文件
    其余内容不触碰）；session 可注入供测试。任何一步失败抛
    CredentialError（中文、无凭据内容）。

    流程：passToken 换发 io 域 token（_exchange_io_token）→ 设备列表
    找充电器 → 拉该设备 beaconkey。
    """
    if auth_data is None:
        from pathlib import Path

        auth_path = Path.home() / ".config" / "mijia-api" / "auth.json"
        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialError(
                "尚未登录米家账号，请先在主界面扫码登录") from exc
    for field in ("userId", "serviceToken", "ssecurity"):
        if not auth_data.get(field):
            raise CredentialError(
                "登录凭据不完整，请先在主界面重新扫码登录")

    own_session = session is None
    sess = requests.Session() if session is None else session
    try:
        user_id, io_token, io_ssecurity = _exchange_io_token(sess, auth_data)
        io_auth = {"userId": user_id, "serviceToken": io_token,
                   "ssecurity": io_ssecurity}
        devices = _device_list(sess, io_auth)
        charger = _pick_charger(devices)
        did = str(charger.get("did") or "")
        mac = str(charger.get("mac") or "")
        token = str(charger.get("token") or "")
        if not did or not token:
            raise CredentialError("云端充电器条目缺少 did/token，无法配置")
        ble_key = _beaconkey(sess, io_auth, did)
        return {"did": did, "mac": mac, "token": token, "ble_key": ble_key}
    finally:
        if own_session:
            sess.close()
