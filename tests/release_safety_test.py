# SPDX-License-Identifier: GPL-3.0-or-later
"""发布安全回归：版本提取、BLE 默认模板隔离、公开产物隐私门槛。

两种运行方式：

1. 单测/回归（pytest 或直接运行）：
       python tests/release_safety_test.py
   用临时夹具验证 build.ps1 中可复用的纯函数（Get-AppVersion /
   Get-ServerVersion / Get-ServerConfigTemplate / Assert-ServerConfigTemplate）
   以及产物扫描器；不会执行 Nuitka、不会读取真实运行配置。

2. CI 打包前/后门槛：
       python tests/release_safety_test.py --artifact-dir dist \
           --template cuktech-ble-server/config.yaml.example
       python tests/release_safety_test.py --portable-zip installer/Output/xxx.zip \
           --release-output installer/Output \
           --template cuktech-ble-server/config.yaml.example
   对 dist / 便携 ZIP / Release 输出检查：三个发布必需文件、项目运行数据
   文件与 `.mimosa`/`.dsh`/`.git` 状态目录、备份/私钥、默认模板占位值与
   一致性、小文本资源和私网地址。ZIP 只读扫描且拒绝路径穿越，不落地解压。
   只扫描可枚举的小文本资源，不扫描大二进制随机字节（避免误报）。

设计边界：build.ps1 的模板守卫与这里的检查都只接受 config.yaml.example，
绝不回退到本机 config.yaml；真实配置的值不会出现在错误信息中。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "build.ps1"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

PLACEHOLDER_MAC_RE = re.compile(r"^(?:[Xx]{2}:){5}[Xx]{2}$")

# 这些字段出现在默认模板里时必须是空字符串（发布包不带任何本机凭据）。
EMPTY_TEMPLATE_KEYS = (
    ("ble", "token"),
    ("ble", "ble_key"),
    ("mqtt", "username"),
    ("mqtt", "password"),
    ("bemfa", "uid"),
)
REQUIRED_TEMPLATE_KEYS = (("ble", "mac"),) + EMPTY_TEMPLATE_KEYS

# 项目真实运行数据文件名（见 app/core/_json_store.py、icon_store.py）。
PROJECT_RUNTIME_NAMES = (
    "settings.json",
    "tray.json",
    "workbench.json",
    "devices_cache.json",
    "device_icons.json",
)
# 项目/工具状态目录：只按完整路径段匹配，避免误伤 .gitignore 等正常文件。
STATE_DIR_NAMES = {".mimosa", ".dsh", ".git"}

# 运行数据/隐私文件：只按文件名判定，避免读取大二进制内容。
FORBIDDEN_NAME_RE = re.compile(
    r"^(?:"
    r"config\.ya?ml"
    r"|config\.ya?ml\.(?:bak|old|local|private)"
    r"|(?:settings|tray|workbench|devices_cache|device_icons)\.json"
    r"|.*\.(?:db|sqlite|sqlite3|log)"
    r"|.*\.(?:bak|backup|old|orig|tmp|save|corrupt)"
    r"|.*~"
    r"|\.env(?:\..*)?"
    r"|(?:auth|credentials?|tokens?|cookies?|sessions?)[^/\\]*\.(?:json|ya?ml|txt)"
    r"|(?:id_rsa|id_ed25519|private_key[^/\\]*|.*_private\.pem)"
    r"|.*\.(?:pfx|p12|ppk|key)"
    r")$",
    re.IGNORECASE,
)

# 只检查小文本资源；不扫描 exe/dll/pyd 的随机字节。
TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".ini", ".conf", ".txt", ".html", ".js", ".css", ".svg", ".md", ".pem"}
MAX_TEXT_BYTES = 256 * 1024
PRIVATE_TEXT_PATTERNS = (
    ("Windows 用户目录", re.compile(r"[A-Za-z]:\\Users\\[^\\\r\n]+", re.IGNORECASE)),
    ("开发目录", re.compile(r"[A-Za-z]:\\Code\\[^\\\r\n]+", re.IGNORECASE)),
    # RFC1918 私网地址；不含 localhost/文档地址，避免误报正常资源。
    ("私网 IPv4", re.compile(
        r"(?<![0-9])(?:"
        r"10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}"
        r"|192\.168\.(?:[0-9]{1,3})\.(?:[0-9]{1,3})"
        r"|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:[0-9]{1,3})\.(?:[0-9]{1,3})"
        r")(?![0-9])"
    )),
    ("私钥内容", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
)

SAFE_TEMPLATE = """\
ble:
  mac: "XX:XX:XX:XX:XX:XX"
  token: ""
  ble_key: ""
mqtt:
  username: ""
  password: ""
bemfa:
  uid: ""
"""

UNSAFE_TEMPLATE = """\
ble:
  mac: "AA:BB:CC:DD:EE:FF"
  token: "real-token"
  ble_key: "real-ble-key"
mqtt:
  username: "homeassistant"
  password: "hunter2"
bemfa:
  uid: "private-uid"
"""


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (Path.cwd() / p)


def extract_run_blocks(ci_text: str) -> list[tuple[str, str]]:
    """从 workflow 文本提取 step 级 run 块（跳过 defaults.run.shell 映射）。"""
    lines = ci_text.splitlines()
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        match = re.match(r"^(\s*)run:\s*(.*)$", lines[i])
        if not match:
            i += 1
            continue
        indent = len(match.group(1))
        value = match.group(2).strip()
        if not value:
            # `defaults: run: shell: pwsh` 的映射键不是命令。
            i += 1
            continue
        if value in ("|", "|-", ">", ">-"):
            j = i + 1
            raw: list[str] = []
            while j < len(lines):
                line = lines[j]
                if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                    break
                raw.append(line)
                j += 1
            nonempty = [len(line) - len(line.lstrip()) for line in raw if line.strip()]
            cut = min(nonempty) if nonempty else 0
            code = "\n".join(line[cut:] if len(line) >= cut else "" for line in raw).rstrip("\n")
            blocks.append((f"run@{i + 1}", code))
            i = j
        else:
            blocks.append((f"run@{i + 1}", value))
            i += 1
    return blocks


def parse_template_scalars(text: str) -> dict[str, dict[str, str]]:
    """解析配置模板用到的两级标量结构（只读、无 YAML 依赖）。"""
    data: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 顶层 section 必须顶格；缩进键不参与 section 识别。
        if raw == raw.lstrip() and re.match(r"^[A-Za-z0-9_]+:\s*(?:#.*)?$", raw):
            section = raw.split(":", 1)[0]
            data.setdefault(section, {})
            continue
        if section is None:
            continue
        match = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if value.startswith('"'):
            end = value.find('"', 1)
            value = value[1:end] if end > 0 else value[1:]
        elif value.startswith("'"):
            end = value.find("'", 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split("#", 1)[0].strip()
        data[section][key] = value
    return data


def template_violations(text: str) -> list[str]:
    """返回模板安全问题；错误信息不包含实际值，避免日志泄密。"""
    data = parse_template_scalars(text)
    errors: list[str] = []
    for section, key in REQUIRED_TEMPLATE_KEYS:
        if key not in data.get(section, {}):
            errors.append(f"缺少 {section}.{key}")
    if errors:
        return errors

    if not PLACEHOLDER_MAC_RE.match(data["ble"]["mac"]):
        errors.append("ble.mac 必须为 XX:XX:XX:XX:XX:XX 占位值")
    for section, key in EMPTY_TEMPLATE_KEYS:
        if data[section][key] != "":
            errors.append(f"{section}.{key} 必须为空")
    return errors


def path_violations(rel_path: str) -> list[str]:
    """按完整路径段检查项目/工具状态目录；不读取文件内容。"""
    rel = str(rel_path).replace("\\", "/")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if any(part.lower() in STATE_DIR_NAMES for part in parts):
        return [f"禁止的项目/工具状态目录：{rel}"]
    return []


def text_violations(rel_path: str, text: str) -> list[str]:
    """只报告相对路径与类别，绝不把命中内容写入 finding/日志。"""
    findings: list[str] = []
    for label, pattern in PRIVATE_TEXT_PATTERNS:
        if pattern.search(text):
            findings.append(f"文本资源 {rel_path} 含{label}")
    return findings


def _check_template_bytes(raw: bytes, rel: str, template: Path | None) -> list[str]:
    findings: list[str] = []
    if template is not None:
        template = Path(template)
        if not template.is_file():
            findings.append("受版本控制的模板不存在：config.yaml.example")
        elif raw != template.read_bytes():
            findings.append(f"{rel} 与 config.yaml.example 不一致")
    for error in template_violations(raw.decode("utf-8", errors="ignore")):
        findings.append("默认模板：" + error)
    return findings


def scan_artifact_dir(root: Path, template: Path | None = None) -> list[str]:
    """CI 打包前产物门槛：文件名/状态目录 + 默认模板 + 小文本检查。"""
    findings: list[str] = []
    root = Path(root)
    if not root.is_dir():
        return [f"产物目录不存在：{root.name}"]

    required = (
        "MiHome-Ex.exe",
        "ble-server/CuktechBleServer.exe",
        "ble-server/config.default.yaml",
    )
    for rel in required:
        if not (root / rel).is_file():
            findings.append(f"缺少发布必需文件：{rel}")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        state_issues = path_violations(rel)
        if state_issues:
            findings.extend(state_issues)
            continue
        if FORBIDDEN_NAME_RE.match(path.name):
            findings.append(f"禁止的运行数据/隐私文件：{rel}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(text_violations(rel, text))

    packaged = root / "ble-server" / "config.default.yaml"
    if packaged.is_file():
        findings.extend(_check_template_bytes(
            packaged.read_bytes(), "dist/ble-server/config.default.yaml", template))
    return findings


def scan_release_output(root: Path) -> list[str]:
    """Release 输出目录只读扫描：只检查文件名/路径，不读取安装器二进制。"""
    findings: list[str] = []
    root = Path(root)
    if not root.is_dir():
        return [f"Release 输出目录不存在：{root.name}"]
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        findings.extend(path_violations(rel))
        if FORBIDDEN_NAME_RE.match(path.name):
            findings.append(f"禁止的运行数据/隐私文件：{rel}")
    return findings


def _safe_zip_member(name: str) -> tuple[str, str | None]:
    """规范化 ZIP 条目名；返回 (相对名, 不安全原因)。"""
    if "\x00" in name:
        return "", "NUL 字符"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return "", "绝对路径"
    if re.match(r"^[A-Za-z]:", normalized):
        return "", "盘符路径"
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return "", "路径穿越"
    return "/".join(parts), None


def scan_zip_artifact(zip_path: Path, template: Path | None = None) -> list[str]:
    """只读扫描便携 ZIP，不落地解压；拒绝绝对路径/盘符/路径穿越。"""
    findings: list[str] = []
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return [f"便携 ZIP 不存在：{zip_path.name}"]

    required = {
        "MiHome-Ex.exe",
        "ble-server/CuktechBleServer.exe",
        "ble-server/config.default.yaml",
    }
    members: dict[str, zipfile.ZipInfo] = {}
    template_raw: bytes | None = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                rel, reason = _safe_zip_member(info.filename)
                if reason is not None:
                    # 不安全条目名本身可能含路径/秘密，只报告类别。
                    findings.append(f"ZIP 条目路径不安全（{reason}）")
                    continue
                if not rel or info.is_dir():
                    continue
                if rel in members:
                    findings.append(f"ZIP 包含重复条目：{rel}")
                    continue
                members[rel] = info
                state_issues = path_violations(rel)
                if state_issues:
                    findings.extend(state_issues)
                    continue
                if FORBIDDEN_NAME_RE.match(PurePosixPath(rel).name):
                    findings.append(f"禁止的运行数据/隐私文件：{rel}")
                    continue
                if PurePosixPath(rel).suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if info.file_size > MAX_TEXT_BYTES:
                    continue
                try:
                    with zf.open(info) as fh:
                        data = fh.read(MAX_TEXT_BYTES + 1)
                except (OSError, RuntimeError):
                    findings.append(f"ZIP 条目无法读取：{rel}")
                    continue
                if len(data) > MAX_TEXT_BYTES:
                    continue
                findings.extend(text_violations(rel, data.decode("utf-8", errors="ignore")))
                if rel == "ble-server/config.default.yaml":
                    template_raw = data
    except zipfile.BadZipFile:
        return [f"不是有效 ZIP：{zip_path}"]

    for rel in sorted(required):
        if rel not in members:
            findings.append(f"便携 ZIP 缺少发布必需文件：{rel}")
    if template_raw is None:
        if "ble-server/config.default.yaml" in members:
            findings.append("便携 ZIP 的默认模板无法读取")
    else:
        findings.extend(_check_template_bytes(
            template_raw, "便携 ZIP ble-server/config.default.yaml", template))
    return findings


def ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ps_wrapper(call: str) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
        f"$buildPath = {ps_quote(BUILD_SCRIPT)}\n"
        "$tokens = $null\n"
        "$parseErrors = $null\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile($buildPath, [ref]$tokens, [ref]$parseErrors)\n"
        "if ($parseErrors.Count -gt 0) {\n"
        "    Write-Output ('ERROR: build.ps1 解析失败: ' + $parseErrors[0].Message)\n"
        "    exit 3\n"
        "}\n"
        "$functions = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))\n"
        "$definitions = ($functions | ForEach-Object { $_.Extent.Text }) -join \"`n`n\"\n"
        ". ([scriptblock]::Create($definitions))\n"
        "try {\n"
        f"{call}\n"
        "} catch {\n"
        "    Write-Output ('ERROR: ' + $_.Exception.Message)\n"
        "    exit 2\n"
        "}\n"
    )


def run_ps_helper(call: str) -> subprocess.CompletedProcess[str]:
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        raise unittest.SkipTest("未找到 PowerShell，跳过 build.ps1 纯函数验证")
    with tempfile.TemporaryDirectory(prefix="mihome-r01-ps-") as td:
        script = Path(td) / "invoke.ps1"
        script.write_text(_ps_wrapper(call), encoding="utf-8")
        return subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )


def run_powershell_script(script_text: str) -> subprocess.CompletedProcess[str]:
    """在 pwsh 7 下运行临时脚本；原生失败传播测试依赖 pwsh 7.3+。"""
    ps = shutil.which("pwsh")
    if not ps:
        raise unittest.SkipTest("未找到 pwsh 7，跳过失败传播/CI AST 探针")
    with tempfile.TemporaryDirectory(prefix="mihome-r01-ps-") as td:
        script = Path(td) / "probe.ps1"
        script.write_text(script_text, encoding="utf-8")
        return subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )


def _result_call(expression: str) -> str:
    return (
        f"$result = {expression}\n"
        "if ($null -ne $result) { $result | ConvertTo-Json -Compress } else { Write-Output 'OK' }"
    )


def _ps_ok(call: str) -> str:
    proc = run_ps_helper(call)
    if proc.returncode != 0:
        raise AssertionError(
            f"PowerShell helper 失败（exit {proc.returncode}）：{proc.stdout.strip()} {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _ps_json(call: str) -> object:
    return json.loads(_ps_ok(call))


def _ps_fails(call: str) -> str:
    proc = run_ps_helper(call)
    if proc.returncode == 0:
        raise AssertionError("期望 PowerShell helper 失败，但成功：" + proc.stdout)
    return (proc.stdout + "\n" + proc.stderr).strip()


# ---------------------------------------------------------------------------
# 单测：build.ps1 纯函数
# ---------------------------------------------------------------------------


def test_server_version_reads_only_project_section() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-version-") as td:
        path = Path(td) / "pyproject.toml"
        path.write_text(
            '[build-system]\nrequires = ["setuptools"]\n\n'
            '[project]\nname = "x"\nversion = "1.2.0"  # pinned\n\n'
            '[tool.other]\nversion = "9.9.9"\n',
            encoding="utf-8",
        )
        value = _ps_json(_result_call(f"Get-ServerVersion -Path {ps_quote(path)}"))
        assert value == "1.2.0", f"应只读 [project] 段版本，实际 {value!r}"


def test_server_version_rejects_non_strict_or_duplicate() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-version-") as td:
        path = Path(td) / "pyproject.toml"
        for body in (
            '[project]\nversion = "1.2.0rc1"\n',
            '[project]\nversion = "1.2"\n',
            '[project]\nversion = "1.2.0"\nversion = "1.2.1"\n',
            '[tool.other]\nversion = "1.2.0"\n',
        ):
            path.write_text(body, encoding="utf-8")
            message = _ps_fails(_result_call(f"Get-ServerVersion -Path {ps_quote(path)}"))
            assert "version" in message, message


def test_app_version_strict_extraction() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-version-") as td:
        path = Path(td) / "__init__.py"
        path.write_text('__version__ = "0.6.0"\n', encoding="utf-8")
        assert _ps_json(_result_call(f"Get-AppVersion -Path {ps_quote(path)}")) == "0.6.0"

        # 允许行尾注释（I01 合并期在版本行标注了“稍后统一升版”）。
        path.write_text('__version__ = "0.6.0"  # release sync\n', encoding="utf-8")
        assert _ps_json(_result_call(f"Get-AppVersion -Path {ps_quote(path)}")) == "0.6.0"

        path.write_text('__version__ = "0.6"\n', encoding="utf-8")
        assert "version" in _ps_fails(_result_call(f"Get-AppVersion -Path {ps_quote(path)}"))


def test_server_template_never_falls_back_to_local_config() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-template-") as td:
        root = Path(td)
        # 只有本机运行配置时也必须失败，不得回退。
        (root / "config.yaml").write_text(UNSAFE_TEMPLATE, encoding="utf-8")
        message = _ps_fails(_result_call(f"Get-ServerConfigTemplate -ServerRoot {ps_quote(root)}"))
        assert "config.yaml.example" in message, message

        template = root / "config.yaml.example"
        template.write_text(SAFE_TEMPLATE, encoding="utf-8")
        value = _ps_json(_result_call(f"Get-ServerConfigTemplate -ServerRoot {ps_quote(root)}"))
        assert Path(str(value)).name == "config.yaml.example", value


def test_server_template_assertion_rejects_local_values() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-template-") as td:
        safe = Path(td) / "safe.yaml"
        safe.write_text(SAFE_TEMPLATE, encoding="utf-8")
        assert _ps_ok(_result_call(f"Assert-ServerConfigTemplate -Path {ps_quote(safe)}")) == "OK"

        unsafe = Path(td) / "unsafe.yaml"
        unsafe.write_text(UNSAFE_TEMPLATE, encoding="utf-8")
        message = _ps_fails(_result_call(f"Assert-ServerConfigTemplate -Path {ps_quote(unsafe)}"))
        assert "ble.mac" in message and "mqtt.password" in message and "bemfa.uid" in message, message
        assert "hunter2" not in message and "real-token" not in message, "错误信息不得回显值"


def test_template_parser_flags_secret_values() -> None:
    assert template_violations(SAFE_TEMPLATE) == []
    errors = template_violations(UNSAFE_TEMPLATE)
    assert any("ble.mac" in e for e in errors)
    assert any("ble.token" in e for e in errors)
    assert any("mqtt.password" in e for e in errors)
    assert any("bemfa.uid" in e for e in errors)
    assert all("hunter2" not in e and "real-token" not in e for e in errors)


# ---------------------------------------------------------------------------
# 单测：产物扫描器
# ---------------------------------------------------------------------------


def _make_safe_artifact(root: Path) -> None:
    (root / "ble-server").mkdir(parents=True, exist_ok=True)
    (root / "MiHome-Ex.exe").write_bytes(b"MZ")
    (root / "ble-server" / "CuktechBleServer.exe").write_bytes(b"MZ")
    (root / "ble-server" / "config.default.yaml").write_text(SAFE_TEMPLATE, encoding="utf-8")
    # 正常第三方依赖资源：公开 CA 证书不是私钥，不应被误报。
    (root / "certifi").mkdir()
    (root / "certifi" / "cacert.pem").write_text(
        "-----BEGIN CERTIFICATE-----\nMIIB-public-ca\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )


def test_artifact_scanner_accepts_safe_tree() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        template = root.parent / (root.name + "-template.yaml")
        template.write_text(SAFE_TEMPLATE, encoding="utf-8")
        try:
            assert scan_artifact_dir(root, template) == []
        finally:
            template.unlink(missing_ok=True)


def test_artifact_scanner_requires_publish_files() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        findings = scan_artifact_dir(Path(td))
        joined = "\n".join(findings)
        for rel in ("MiHome-Ex.exe", "ble-server/CuktechBleServer.exe", "ble-server/config.default.yaml"):
            assert rel in joined, joined


def test_artifact_scanner_flags_runtime_and_private_files() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        (root / "ble-server" / "config.yaml").write_text(UNSAFE_TEMPLATE, encoding="utf-8")
        (root / "ble-server" / "port_history.db").write_bytes(b"db")
        (root / ".env").write_text("TOKEN=x", encoding="utf-8")
        (root / "ble-server" / "web").mkdir()
        (root / "ble-server" / "web" / "app.js").write_text(
            "const p = 'D:\\Code\\MiHome-Ex\\private';", encoding="utf-8"
        )
        findings = scan_artifact_dir(root)
        joined = "\n".join(findings)
        assert "config.yaml" in joined, joined
        assert "port_history.db" in joined, joined
        assert ".env" in joined, joined
        assert "开发目录" in joined, joined
        # 隐私日志只报相对路径/类别，不得回显命中的本机路径。
        assert "MiHome-Ex" not in "".join(f for f in findings if "开发目录" in f)
        assert "D:\\Code" not in joined and "private" not in joined, joined


def test_artifact_scanner_flags_project_runtime_files() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        for name in PROJECT_RUNTIME_NAMES:
            (root / name).write_text("{}", encoding="utf-8")
        joined = "\n".join(scan_artifact_dir(root))
        for name in PROJECT_RUNTIME_NAMES:
            assert name in joined, f"{name} 未被拦截：{joined}"


def test_artifact_scanner_flags_state_dirs_and_backups() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        (root / ".mimosa" / "hook-state").mkdir(parents=True)
        (root / ".mimosa" / "hook-state" / "session.json").write_text("{}", encoding="utf-8")
        (root / ".dsh").mkdir()
        (root / ".dsh" / "log.txt").write_text("log", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("x", encoding="utf-8")
        (root / "settings.json.bak").write_text("{}", encoding="utf-8")
        joined = "\n".join(scan_artifact_dir(root))
        for marker in (".mimosa", ".dsh", ".git", "settings.json.bak"):
            assert marker in joined, f"{marker} 未被拦截：{joined}"


def test_artifact_scanner_flags_private_key_content_without_echo() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        (root / "config" / "server.pem").parent.mkdir()
        (root / "config" / "server.pem").write_text(
            "-----BEGIN PRIVATE KEY-----\nSECRET-PRIVATE-MATERIAL\n-----END PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        findings = scan_artifact_dir(root)
        joined = "\n".join(findings)
        assert "私钥内容" in joined, joined
        assert "SECRET-PRIVATE-MATERIAL" not in joined and "BEGIN PRIVATE KEY" not in joined


def test_artifact_scanner_flags_private_ipv4_without_echo() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        (root / "ble-server" / "web").mkdir()
        (root / "ble-server" / "web" / "settings.js").write_text(
            "const url = 'http://192.168.50.23:8080';", encoding="utf-8"
        )
        findings = scan_artifact_dir(root)
        joined = "\n".join(findings)
        assert "私网 IPv4" in joined, joined
        assert "192.168.50.23" not in joined


def test_artifact_scanner_rejects_unsafe_packaged_template() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        (root / "ble-server" / "config.default.yaml").write_text(UNSAFE_TEMPLATE, encoding="utf-8")
        joined = "\n".join(scan_artifact_dir(root))
        assert "默认模板" in joined and "ble.mac" in joined, joined


def test_artifact_scanner_ignores_binary_random_bytes() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        # 大二进制里出现类似路径的随机字节不应触发误报（不扫描非文本后缀）。
        (root / "vendor.dll").write_bytes(b"\x00\x01C:\\Users\\secret\x00" + b"\xff" * 4096)
        assert scan_artifact_dir(root) == []


def test_artifact_scanner_flags_template_drift() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-dist-") as td:
        root = Path(td)
        _make_safe_artifact(root)
        template = root.parent / (root.name + "-template.yaml")
        template.write_text(SAFE_TEMPLATE, encoding="utf-8")
        try:
            with (root / "ble-server" / "config.default.yaml").open("a", encoding="utf-8") as fh:
                fh.write("# drift\n")
            joined = "\n".join(scan_artifact_dir(root, template))
            assert "不一致" in joined, joined
        finally:
            template.unlink(missing_ok=True)


def _make_zip(path: Path, files: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def _safe_zip_files() -> dict[str, str | bytes]:
    return {
        "MiHome-Ex.exe": b"MZ",
        "ble-server/CuktechBleServer.exe": b"MZ",
        "ble-server/config.default.yaml": SAFE_TEMPLATE,
        "certifi/cacert.pem": "-----BEGIN CERTIFICATE-----\nMIIB-public-ca\n-----END CERTIFICATE-----\n",
    }


def test_zip_scanner_accepts_safe_zip() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-zip-") as td:
        root = Path(td)
        template = root / "config.yaml.example"
        # ZIP 写入 str 时统一为 LF；模板也用原始字节，避免 Windows 文本换行差异。
        template.write_bytes(SAFE_TEMPLATE.encode("utf-8"))
        zip_path = root / "portable.zip"
        _make_zip(zip_path, _safe_zip_files())
        assert scan_zip_artifact(zip_path, template) == []


def test_zip_scanner_flags_runtime_file() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-zip-") as td:
        root = Path(td)
        files = _safe_zip_files()
        files["settings.json"] = "{}"
        zip_path = root / "portable.zip"
        _make_zip(zip_path, files)
        joined = "\n".join(scan_zip_artifact(zip_path))
        assert "settings.json" in joined, joined


def test_zip_scanner_rejects_traversal_and_unsafe_template() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-zip-") as td:
        root = Path(td)
        template = root / "config.yaml.example"
        template.write_bytes(SAFE_TEMPLATE.encode("utf-8"))
        files = _safe_zip_files()
        files["../evil.txt"] = "x"
        files["ble-server/config.default.yaml"] = UNSAFE_TEMPLATE
        zip_path = root / "portable.zip"
        _make_zip(zip_path, files)
        findings = scan_zip_artifact(zip_path, template)
        joined = "\n".join(findings)
        assert "路径穿越" in joined, joined
        assert "ble.mac" in joined, joined


def test_zip_scanner_redacts_private_text() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-zip-") as td:
        root = Path(td)
        files = _safe_zip_files()
        files["ble-server/web/app.js"] = "const p = 'D:\\Code\\TopSecretUser\\private';"
        zip_path = root / "portable.zip"
        _make_zip(zip_path, files)
        joined = "\n".join(scan_zip_artifact(zip_path))
        assert "开发目录" in joined, joined
        assert "TopSecretUser" not in joined and "D:\\Code" not in joined


def test_release_output_scanner_flags_runtime_file() -> None:
    with tempfile.TemporaryDirectory(prefix="mihome-r01-output-") as td:
        root = Path(td)
        (root / "MiHome-Ex-setup-0.6.0.exe").write_bytes(b"MZ")
        (root / "SHA256SUMS.txt").write_text("", encoding="utf-8")
        assert scan_release_output(root) == []
        (root / "settings.json").write_text("{}", encoding="utf-8")
        (root / ".mimosa" / "hook-state").mkdir(parents=True)
        (root / ".mimosa" / "hook-state" / "session.json").write_text("{}", encoding="utf-8")
        joined = "\n".join(scan_release_output(root))
        assert "settings.json" in joined and ".mimosa" in joined, joined


# ---------------------------------------------------------------------------
# 配置契约：构建脚本与 CI 的发布门槛
# ---------------------------------------------------------------------------


def test_build_script_contract() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "Get-ServerConfigTemplate" in script
    assert "Get-ServerVersion" in script
    assert "config.yaml.example" in script
    assert "--include-data-files=$ServerTemplate=config.default.yaml" in script
    assert "1.1.5" not in script, "BLE 版本资源不得再硬编码 1.1.5"
    assert "90dc6bc983d833fd60df6db2da32cf779046a043" in script, \
        "build.ps1 必须校验公开 v1.2.0 固定提交"
    assert "$ServerCfg" not in script, "不得保留旧的本机配置回退变量"
    assert not re.search(r"config\.ya?ml(?!\.example)", script), "build.ps1 不得引用本机运行配置"


def test_ci_workflow_contract() -> None:
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "90dc6bc983d833fd60df6db2da32cf779046a043" in ci
    assert "0cb099386fb6fc0e8efad745b2b62ebea8d54766" not in ci, \
        "CI 不得继续固定旧 BLE 提交"
    assert not re.search(r"^\s*ref:\s*main\s*$", ci, re.MULTILINE), "BLE checkout 不得浮动 main"
    assert "测试包含脚本式 assert 与 pytest 套件" in ci, "头部测试说明必须与实际入口一致"
    assert "测试均为脚本式 assert" not in ci
    assert "已知失败" not in ci, "hide_no_func 的临时失败说明已过时"
    assert "if: always()" not in ci
    assert "upload-artifact" not in ci, "本轮移除可选上传；不得无门禁上传产物"
    assert ci.count("defaults:") == 2, "两个 job 都应显式 defaults.run.shell=pwsh"
    assert ci.count("shell: pwsh") >= 2, "缺少 pwsh shell 配置"
    assert "--artifact-dir dist" in ci
    assert "--portable-zip" in ci
    assert "--release-output" in ci
    # 隐私门槛必须早于 Inno 打包；ZIP/Release 输出扫描必须晚于便携包、早于 Release。
    assert ci.index("--artifact-dir dist") < ci.index("打 Inno Setup 安装包")
    assert ci.index("打包后完整校验") > ci.index("打便携版 zip")
    assert ci.index("--portable-zip") > ci.index("打便携版 zip")
    assert ci.index("--portable-zip") < ci.index("生成发布说明并创建 Release")
    assert ci.index("--release-output") < ci.index("生成发布说明并创建 Release")
    assert "'--repo', 'Arimayuki03/MiHome-Ex'" in ci
    assert "${env:ProgramFiles(x86)}" in ci
    assert "$env:ProgramFiles(x86)" not in ci, "ProgramFiles(x86) 必须用 ${env:...} 语法"
    for command in (
        "python -m pytest tests/ble_server_manager_test.py -q",
        "python -m pytest tests/cuktech_stage_pending_test.py -q",
        "python -m pytest tests/tray_audio_bar_test.py -q",
    ):
        assert command in ci, f"CI 缺少真实 pytest 入口：{command}"
    for direct in (
        "python tests/ble_server_manager_test.py",
        "python tests/cuktech_stage_pending_test.py",
        "python tests/tray_audio_bar_test.py",
    ):
        assert direct not in ci, f"不得直接 python 空跑 pytest 套件：{direct}"
    for name in (
        "cuktech_credentials_test.py",
        "cuktech_stage_pending_test.py",
        "hide_no_func_test.py",
        "tray_audio_bar_test.py",
        "cuktech_stage_script.py",
        "upstream_integration_test.py",
        "scene_tray_test.py",
        "release_safety_test.py",
    ):
        assert name in ci, f"CI 缺少测试脚本：{name}"


def test_ci_run_blocks_enable_native_failure_propagation() -> None:
    blocks = extract_run_blocks(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert len(blocks) >= 17, f"CI run 块数量异常：{len(blocks)}"
    for name, code in blocks:
        assert "$ErrorActionPreference = 'Stop'" in code, f"{name} 缺少 ErrorActionPreference=Stop"
        assert "$PSNativeCommandUseErrorActionPreference = $true" in code, (
            f"{name} 缺少原生命令失败传播设置"
        )


def test_ci_run_blocks_parse_as_powershell() -> None:
    blocks = extract_run_blocks(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert blocks, "未提取到 CI run 块"
    sanitized = [
        {"name": name, "code": re.sub(r"\$\{\{[^}]*\}\}", "DUMMY", code)}
        for name, code in blocks
    ]
    payload = base64.b64encode(
        json.dumps(sanitized, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    script = """
$ErrorActionPreference = 'Stop'
$json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__PAYLOAD__'))
$items = @($json | ConvertFrom-Json)
$bad = @()
foreach ($item in $items) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseInput([string]$item.code, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) {
        $bad += ("$($item.name): " + $errors[0].Message)
    }
}
if ($bad.Count -gt 0) {
    $bad -join "`n"
    exit 1
}
Write-Output ("PS-AST-OK: " + $items.Count + " blocks")
""".replace("__PAYLOAD__", payload)
    proc = run_powershell_script(script)
    assert proc.returncode == 0, f"run 块 PowerShell 解析失败：{proc.stdout} {proc.stderr}"
    assert "PS-AST-OK" in proc.stdout, proc.stdout


def test_native_command_failure_propagation_probe() -> None:
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$PSNativeCommandUseErrorActionPreference = $true\n"
        "cmd /c \"exit 9\"\n"
        "Write-Output 'AFTER-FIRST'\n"
        "cmd /c \"exit 0\"\n"
        "Write-Output 'AFTER-SECOND'\n"
    )
    proc = run_powershell_script(script)
    assert proc.returncode != 0, "前命令 exit 9 后命令 exit 0 必须让脚本失败"
    assert "AFTER-FIRST" not in proc.stdout, "失败原生命令后不应继续执行"


def test_repo_template_if_checked_out() -> None:
    candidates = (
        ROOT / "cuktech-ble-server" / "config.yaml.example",
        ROOT.parent / "cuktech-ble-server" / "config.yaml.example",
    )
    for candidate in candidates:
        if candidate.is_file():
            errors = template_violations(candidate.read_text(encoding="utf-8"))
            assert errors == [], f"{candidate} 不安全：{errors}"
            return
    raise unittest.SkipTest("BLE 服务端模板未 checkout，跳过真实模板检查（CI build job 会强制检查）")


TESTS = (
    test_server_version_reads_only_project_section,
    test_server_version_rejects_non_strict_or_duplicate,
    test_app_version_strict_extraction,
    test_server_template_never_falls_back_to_local_config,
    test_server_template_assertion_rejects_local_values,
    test_template_parser_flags_secret_values,
    test_artifact_scanner_accepts_safe_tree,
    test_artifact_scanner_requires_publish_files,
    test_artifact_scanner_flags_runtime_and_private_files,
    test_artifact_scanner_flags_project_runtime_files,
    test_artifact_scanner_flags_state_dirs_and_backups,
    test_artifact_scanner_flags_private_key_content_without_echo,
    test_artifact_scanner_flags_private_ipv4_without_echo,
    test_artifact_scanner_rejects_unsafe_packaged_template,
    test_artifact_scanner_ignores_binary_random_bytes,
    test_artifact_scanner_flags_template_drift,
    test_zip_scanner_accepts_safe_zip,
    test_zip_scanner_flags_runtime_file,
    test_zip_scanner_rejects_traversal_and_unsafe_template,
    test_zip_scanner_redacts_private_text,
    test_release_output_scanner_flags_runtime_file,
    test_build_script_contract,
    test_ci_workflow_contract,
    test_ci_run_blocks_enable_native_failure_propagation,
    test_ci_run_blocks_parse_as_powershell,
    test_native_command_failure_propagation_probe,
    test_repo_template_if_checked_out,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiHome-Ex 发布安全测试/产物门槛")
    parser.add_argument("--artifact-dir", help="CI 打包前检查的 dist 目录")
    parser.add_argument("--portable-zip", help="CI 发布前只读扫描的便携 ZIP")
    parser.add_argument("--release-output", help="CI 发布前只读检查的 installer/Output")
    parser.add_argument("--template", help="受版本控制的 config.yaml.example")
    args = parser.parse_args(argv)

    modes = [args.artifact_dir, args.portable_zip, args.release_output]
    if any(modes):
        template = _resolve(args.template) if args.template else None
        findings: list[str] = []
        if args.artifact_dir:
            findings.extend(scan_artifact_dir(_resolve(args.artifact_dir), template))
        if args.portable_zip:
            findings.extend(scan_zip_artifact(_resolve(args.portable_zip), template))
        if args.release_output:
            findings.extend(scan_release_output(_resolve(args.release_output)))
        if findings:
            print("发布安全门槛失败：")
            for finding in findings:
                print(f"  [FAIL] {finding}")
            return 1
        print("发布安全门槛通过：产物无运行数据/隐私文件，默认模板为占位值")
        return 0

    passed = skipped = failures = 0
    for test in TESTS:
        try:
            test()
        except unittest.SkipTest as exc:
            skipped += 1
            print(f"[SKIP] {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 测试脚本需要汇总全部失败
            failures += 1
            print(f"[FAIL] {test.__name__}: {exc}")
        else:
            passed += 1
            print(f"[PASS] {test.__name__}")
    print(f"\n{passed} 项通过，{skipped} 项跳过，{failures} 项失败")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
