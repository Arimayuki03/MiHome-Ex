# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 MiHome-Ex contributors
<#
.SYNOPSIS
    MiHome-Ex 一键构建脚本(基于 MiHome-Windows)
.DESCRIPTION
    自动完成：venv 创建 → 依赖安装 → Nuitka 编译
    需要：Python 3.10+, VS Build Tools 2022
.EXAMPLE
    .\build.ps1
    .\build.ps1 -Clean
    .\build.ps1 -ClientOnly   # 客户端版：不含内置 BLE 服务端组件（分离式部署用）
#>

param(
    [switch]$Clean,
    # 客户端版：跳过 BLE 服务端扩展组件构建，dist\ 不含 ble-server\。
    # 面向「后端跑在旁路由/NAS」的分离式部署用户：安装体积更小，
    # 设置里填充电器远程后端地址即可使用充电器面板
    [switch]$ClientOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

# ============================================================
# 0. 发布安全辅助函数
# ============================================================
# 纯参数化、无副作用；tests/release_safety_test.py 会用 PowerShell AST
# 抽取这些函数单独调用，验证版本提取与模板隔离行为（不执行构建）。
function Get-AppVersion {
    param([Parameter(Mandatory = $true)][string]$Path)
    $hit = @(Select-String -LiteralPath $Path -Pattern '^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*(?:#.*)?$')
    if ($hit.Count -ne 1) {
        throw "无法从 $Path 读取唯一的 __version__ = `"x.y.z`"（匹配数：$($hit.Count)）"
    }
    return $hit[0].Matches[0].Groups[1].Value
}

function Get-ServerVersion {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "BLE 服务端 pyproject.toml 不存在：$Path"
    }
    $section = ""
    $versions = @()
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($raw -match '^\[([^\]]+)\]\s*(?:#.*)?$') {
            $section = $Matches[1].Trim()
            continue
        }
        if ($section -ne "project") { continue }
        $line = $raw.Trim()
        if ($line -match '^version\s*=\s*["'']([0-9]+\.[0-9]+\.[0-9]+)["'']\s*(?:#.*)?$') {
            $versions += $Matches[1]
        }
    }
    if ($versions.Count -ne 1) {
        throw "无法从 $Path 的 [project] 段读取唯一的 version = `"x.y.z`"（匹配数：$($versions.Count)）"
    }
    return $versions[0]
}

function Get-ServerConfigTemplate {
    param([Parameter(Mandatory = $true)][string]$ServerRoot)
    # 只认受版本控制的示例模板；绝不回退到本机运行配置（含真实凭据）。
    $template = Join-Path $ServerRoot "config.yaml.example"
    if (-not (Test-Path -LiteralPath $template -PathType Leaf)) {
        throw "BLE 服务端缺少受版本控制的 config.yaml.example 模板：$template（拒绝回退到本地运行配置）"
    }
    return $template
}

function Assert-ServerConfigTemplate {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "BLE 默认配置模板不存在：$Path"
    }

    # 只解析本模板用到的两级标量结构；格式异常会明确失败，便于评审。
    $sections = @{}
    $current = $null
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        if ($raw -match '^([A-Za-z0-9_]+):\s*(?:#.*)?$') {
            $current = $Matches[1]
            if (-not $sections.ContainsKey($current)) { $sections[$current] = @{} }
            continue
        }
        if ($null -eq $current) { continue }
        if ($line -match '^([A-Za-z0-9_]+):\s*(.*)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim()
            if ($value.StartsWith('"')) {
                $end = $value.IndexOf('"', 1)
                if ($end -lt 0) { throw "配置模板 $Path 的 $current.$key 引号未闭合" }
                $value = $value.Substring(1, $end - 1)
            } elseif ($value.StartsWith("'")) {
                $end = $value.IndexOf("'", 1)
                if ($end -lt 0) { throw "配置模板 $Path 的 $current.$key 引号未闭合" }
                $value = $value.Substring(1, $end - 1)
            } else {
                $hash = $value.IndexOf("#")
                if ($hash -ge 0) { $value = $value.Substring(0, $hash).Trim() }
            }
            $sectionTable = $sections[$current]
            $sectionTable[$key] = $value
        }
    }

    $required = @(
        @("ble", "mac"), @("ble", "token"), @("ble", "ble_key"),
        @("mqtt", "username"), @("mqtt", "password"), @("bemfa", "uid")
    )
    $errors = @()
    foreach ($pair in $required) {
        $sectionName = $pair[0]
        $keyName = $pair[1]
        if (-not $sections.ContainsKey($sectionName) -or -not ($sections[$sectionName].ContainsKey($keyName))) {
            $errors += "缺少 $sectionName.$keyName"
        }
    }
    if ($errors.Count -eq 0) {
        if ($sections["ble"]["mac"] -notmatch '^(?:[Xx]{2}:){5}[Xx]{2}$') {
            $errors += "ble.mac 必须为 XX:XX:XX:XX:XX:XX 占位值"
        }
        foreach ($pair in @(@("ble", "token"), @("ble", "ble_key"), @("mqtt", "username"), @("mqtt", "password"), @("bemfa", "uid"))) {
            $sectionName = $pair[0]
            $keyName = $pair[1]
            if ($sections[$sectionName][$keyName] -ne "") {
                # 不回显值，避免把疑似真实凭据写进构建日志
                $errors += "$sectionName.$keyName 必须为空"
            }
        }
    }
    if ($errors.Count -gt 0) {
        throw ("BLE 默认模板未通过发布安全检查：`n - " + ($errors -join "`n - "))
    }
}

# ============================================================
# 1. 自动创建 venv 并安装依赖
# ============================================================
$Pip = Join-Path $Root ".venv\Scripts\pip.exe"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Pip)) {
    Write-Host "[1/3] Creating virtual environment..." -ForegroundColor Cyan

    # 尝试 py launcher，失败则用 python
    $PyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($PyCmd) {
        & py -3 -m venv (Join-Path $Root ".venv")
    } else {
        & python -m venv (Join-Path $Root ".venv")
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] 创建 venv 失败，请确认 Python 3 已安装并加入 PATH" -ForegroundColor Red
        exit 1
    }

    Write-Host "[2/3] Installing dependencies... (showing pip progress; a 'new version of pip' notice is informational only)" -ForegroundColor Cyan
    # 不用 --quiet：依赖下载可达数百 MB，静默会让人误以为卡死；
    # pip 的升级提示/进度条直接透传
    & $Python -m pip install -e $Root
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] 安装依赖失败" -ForegroundColor Red
        exit 1
    }

    Write-Host "[3/3] 初始化完成`n" -ForegroundColor Green
} else {
    Write-Host "[OK] Virtual environment ready" -ForegroundColor Green
}

# ============================================================
# 2. 激活 MSVC 编译环境
# ============================================================
# 本机默认装 Build Tools；CI（ilammy/msvc-dev-cmd）已配好环境变量，
# 无需再跑 vcvars——检测到 PATH 里有 cl.exe 即跳过探测。
$VcvarsCandidates = @(
    "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
)
$Vcvars = $VcvarsCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$ClInPath = Get-Command cl.exe -ErrorAction SilentlyContinue
if (-not $Vcvars -and -not $ClInPath) {
    Write-Host "[ERROR] VS Build Tools 2022 not found" -ForegroundColor Red
    Write-Host "Download: https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022" -ForegroundColor Yellow
    exit 1
}

if ($Vcvars) {
    Write-Host "Activating MSVC environment..." -ForegroundColor Cyan

    $MsvcEnv = cmd /c "`"$Vcvars`" >nul 2>&1 && set"
    foreach ($Line in $MsvcEnv) {
        if ($Line -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
        }
    }
}
# vcvars 在部分机器上不导出该变量，缺失时兜底；已有值则尊重本机 SDK
if (-not $Env:WindowsSDKVersion -or $Env:WindowsSDKVersion -eq "0.0.0.0") {
    $Env:WindowsSDKVersion = "10.0.26100.0"
}
Write-Host "MSVC ready" -ForegroundColor Green

# ============================================================
# 3. 前置检查（Nuitka 缺失即补装，兼容先跑过 start.bat 的旧 venv）
# ============================================================
# 注意：探测必须经 cmd /c 在 cmd 层丢弃 stderr。脚本开头
# $ErrorActionPreference = "Stop" 时，PowerShell 会把带 2>$null 的
# 原生命令 stderr 转成终止性错误——全新 venv 没装 Nuitka，探测
# 必然失败，脚本会在「Installing Nuitka」分支之前直接死掉。

function Get-NuitkaVersion {
    $code = "from nuitka.Version import getNuitkaVersion; print(getNuitkaVersion())"
    $out = cmd /c "`"$Python`" -c `"$code`" 2>nul"
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    return ($out | Out-String).Trim()
}

$NuitkaVer = Get-NuitkaVersion
if (-not $NuitkaVer) {
    Write-Host "Installing Nuitka... (downloading compiler toolchain, may take a minute)" -ForegroundColor Cyan
    # 上限钉 4.2：4.3 起把 PySide6 的 Windows PATH 注入从 exe 目录
    # （__nuitka_binary_dir）改成 python_runtime_dir，引入启动期随机
    # segfault（pyside6.abi3.dll 0xc0000005，约 1/4 概率必崩，v0.4.6~
    # v0.5.1 的 CI 产物全部中招；4.2.2 同源码重建 10/10 稳定）。
    # 适配 4.3 后再人工放行
    & $Python -m pip install "nuitka>=4,<4.3" ordered-set zstandard
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] 安装 Nuitka 失败" -ForegroundColor Red
        exit 1
    }
    $NuitkaVer = Get-NuitkaVersion
    if (-not $NuitkaVer) {
        Write-Host "[ERROR] Nuitka 未安装" -ForegroundColor Red
        exit 1
    }
}
Write-Host "Nuitka $NuitkaVer" -ForegroundColor Green

# ============================================================
# 4. 清理
# ============================================================
if ($Clean -and (Test-Path "dist")) {
    Remove-Item "dist" -Recurse -Force
}

# ============================================================
# 5. 构建
# ============================================================

try {
    $AppVersion = Get-AppVersion -Path "app\__init__.py"
} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
Write-Host "  Version: $AppVersion" -ForegroundColor Gray

$NuitkaArgs = @(
    "run.py"
    "--standalone"
    "--enable-plugin=pyside6"
    "--windows-console-mode=disable"
    "--windows-icon-from-ico=app\ui\icon.ico"
    "--include-package=app.siui"
    "--include-package=qtawesome"
    "--include-package-data=qtawesome"
    "--include-data-files=app\ui\icon.png=app/ui/icon.png"
    "--include-data-files=app\ui\tray_icon.png=app/ui/tray_icon.png"
    "--include-data-files=app\ui\tray_icon_light.png=app/ui/tray_icon_light.png"
    # CUKTECH 充电器素材（舞台底图/端口条/模式图标，20+ 张 PNG）：
    # 漏打包会让面板舞台与设置页模式图标全部空白
    "--include-data-dir=app\ui\assets=app/ui/assets"
    "--nofollow-import-to=tkinter,unittest,pytest"
    "--noinclude-dlls=qt6datavisualization.dll"
    "--noinclude-dlls=qt6pdf.dll"
    # shiboken6 wheel 自带一份 MSVC 运行库 DLL（msvcp140 系等），
    # PySide6 目录里已有同款；360 实时防护对"新写入的运行库 DLL"是
    # 确定性锁定（连续 5 次构建 PermissionError 均落在这批文件）。
    # 排除后运行期由 Windows System32 提供系统级运行库（装了
    # VC++ Redistributable 的机器必有），应用行为不变
    "--noinclude-dlls=msvcp140*.dll"
    "--noinclude-dlls=concrt140.dll"
    "--noinclude-dlls=vcamp140.dll"
    "--noinclude-dlls=vccorlib140.dll"
    "--noinclude-dlls=vcomp140.dll"
    "--noinclude-dlls=vcruntime140*.dll"
    "--noinclude-dlls=msvcp140_codecvt_ids.dll"
    "--jobs=4"
    "--assume-yes-for-downloads"
    "--output-dir=build.dist"
    "--output-filename=MiHome-Ex.exe"
    # 版本号自动从 app/__init__.py 的 __version__ 读取，单一信源
    "--product-name=MiHome-Ex"
    "--product-version=$AppVersion"
    "--file-version=$AppVersion"
    # 公司名与文件描述必须补全：空 CompanyName / 空 FileDescription 的
    # 未签名 exe 是 360 QVM 等机器学习引擎的高权重误报特征
    # （HEUR/QVM....Malware.Gen），signed 之前先把可自报的信息补齐
    "--company-name=MiHome-Ex contributors"
    "--file-description=MiHome-Ex 米家设备 Windows 桌面控制端"
    "--copyright=Copyright (C) 2026 MiHome-Ex contributors"
)

Write-Host "`nBuilding MiHome-Ex..." -ForegroundColor Cyan
Write-Host "(这是最耗时的一步，通常需要几分钟；期间会输出 Nuitka 各阶段进度，请勿关闭窗口)`n" -ForegroundColor Yellow
$BuildStart = Get-Date

# 360 等安全软件对"新写入 D 盘项目目录的运行库 DLL"存在确定性锁定
# （连续多次构建 PermissionError 落在 msvcp140/vcruntime140 等文件）。
# 输出到 %TEMP%（安全软件对用户临时目录拦截概率低很多），成功后回搬。
# $NuitkaArgs 里 --output-dir 占位为 build.dist，实际写入临时目录。
$TempBuild = Join-Path $env:TEMP "mihome-build"
if (Test-Path $TempBuild) { Remove-Item $TempBuild -Recurse -Force }
$NuitkaArgs = @($NuitkaArgs | ForEach-Object { $_ -replace '^--output-dir=build\.dist$', "--output-dir=$TempBuild" })

$BuildAttempt = 0
$MaxAttempts = 3
do {
    $BuildAttempt += 1
    & $Python -m nuitka @NuitkaArgs
    if ($LASTEXITCODE -eq 0) { break }
    if ($BuildAttempt -lt $MaxAttempts) {
        Write-Host "`n[WARN] 第 $BuildAttempt 次构建失败（常见原因：杀软锁 DLL）。5 秒后自动重试…`n" -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
} while ($BuildAttempt -lt $MaxAttempts)

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] 构建失败（已重试 $($MaxAttempts - 1) 次）。" -ForegroundColor Red
    Write-Host "若错误仍指向某个 DLL 被占用：把项目 dist 目录加入杀软（如 360）信任区后重试。" -ForegroundColor Yellow
    exit 1
}

# 回搬：standalone 产物 run.dist 摊平到项目 dist\（.iss 打包源）
Write-Host "`nMoving build output to dist\ ..." -ForegroundColor Cyan
if (Test-Path "dist") { Remove-Item "dist" -Recurse -Force }
Copy-Item (Join-Path $TempBuild "run.dist") "dist" -Recurse
Remove-Item $TempBuild -Recurse -Force -ErrorAction SilentlyContinue
$Elapsed = (Get-Date) - $BuildStart
Write-Host ("`n编译耗时 {0:00}:{1:00}" -f [int]$Elapsed.TotalMinutes, $Elapsed.Seconds) -ForegroundColor Gray

# ============================================================
# 6. 扩展组件：内置 BLE 服务端（cuktech-ble-server）
# ============================================================
# 独立 Nuitka standalone，输出 dist\ble-server\。完整版必须包含该组件：
# 源码/模板/编译/版本任一不满足即失败，不再静默降级，避免发布半成品。
# -ClientOnly 时整段跳过（客户端版不含该组件）。开发态在同级目录
# ..\cuktech-ble-server；CI 的 checkout 落在工作区内部
# <workspace>\cuktech-ble-server（ci.yml 的 path:），两个位置都要认。
#
# 数据模板只允许受版本控制的 config.yaml.example；绝不读取或打包本机
# 运行配置（含真实 MAC/token/ble_key/MQTT 密码，已被 .gitignore 排除）。
$ServerRoot = Join-Path $Root "..\cuktech-ble-server"
if (-not (Test-Path (Join-Path $ServerRoot "ha_server.py"))) {
    $ServerRoot = Join-Path $Root "cuktech-ble-server"
}
$ServerVenvPy = Join-Path $ServerRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $ServerVenvPy)) { $ServerVenvPy = $Python }
$ExpectedBleCommit = "90dc6bc983d833fd60df6db2da32cf779046a043"
$ServerExe = "dist\ble-server\CuktechBleServer.exe"
if ($ClientOnly) {
    # 客户端版：确保产物不含旧一轮构建残留的 ble-server\（安装包
    # iss 按 DirExists 条件打包，残留会让"客户端版"悄悄带上组件）
    if (Test-Path "dist\ble-server") {
        Remove-Item "dist\ble-server" -Recurse -Force
        Write-Host "[ClientOnly] 已移除残留的 dist\ble-server\" -ForegroundColor Yellow
    }
    Write-Host "`n[ClientOnly] 客户端版构建：跳过 BLE 服务端扩展组件。" -ForegroundColor Cyan
} else {
    if (-not (Test-Path (Join-Path $ServerRoot "ha_server.py"))) {
        Write-Host "[ERROR] 未找到 BLE 服务端源码（$ServerRoot）。完整版必须包含内置充电器服务；如只需客户端版请显式使用 -ClientOnly。" -ForegroundColor Red
        exit 1
    }

    # 完整版只接受可追溯的公开 v1.2.0 提交。这样本机残留的旧版本
    # dist、不可追溯镜像或浮动分支都不会被误当作发布输入。
    $BleHeadRaw = & git -C $ServerRoot rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $BleHeadRaw) {
        Write-Host "[ERROR] 无法从 BLE 服务端源码读取 git HEAD；完整版必须使用可追溯的公开源码。" -ForegroundColor Red
        exit 1
    }
    $BleHead = ([string]$BleHeadRaw).Trim()
    if ($BleHead -ne $ExpectedBleCommit) {
        Write-Host "[ERROR] BLE 服务端 HEAD=$BleHead，不是固定提交 $ExpectedBleCommit。" -ForegroundColor Red
        exit 1
    }

    try {
        $ServerTemplate = Get-ServerConfigTemplate -ServerRoot $ServerRoot
        Assert-ServerConfigTemplate -Path $ServerTemplate
        $ServerVersion = Get-ServerVersion -Path (Join-Path $ServerRoot "pyproject.toml")
    } catch {
        Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
    Write-Host "  BLE server version: $ServerVersion ($BleHead)" -ForegroundColor Gray

    # 模板必须已入库且无未提交修改，防止本机改动混入发布包。
    if (Test-Path (Join-Path $ServerRoot ".git")) {
        cmd /c "git -C `"$ServerRoot`" ls-files --error-unmatch -- config.yaml.example >nul 2>&1"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] config.yaml.example 未纳入 BLE 服务端 Git 版本控制" -ForegroundColor Red
            exit 1
        }
        cmd /c "git -C `"$ServerRoot`" diff --quiet -- config.yaml.example >nul 2>&1"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] config.yaml.example 有未提交修改，拒绝打包" -ForegroundColor Red
            exit 1
        }
        cmd /c "git -C `"$ServerRoot`" diff --cached --quiet -- config.yaml.example >nul 2>&1"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] config.yaml.example 有已暂存修改，拒绝打包" -ForegroundColor Red
            exit 1
        }
    }

    if (-not (Test-Path $ServerExe)) {
        Write-Host "`nBuilding BLE server extension (cuktech-ble-server)..." -ForegroundColor Cyan
        # 不用 2>&1 重定向：脚本级 $ErrorActionPreference=Stop 会把
        # stderr 文本升级为终止错误（NativeCommandError 假警报）
        & $ServerVenvPy -m pip install --quiet nuitka ordered-set zstandard
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] 安装 BLE 服务端构建依赖失败" -ForegroundColor Red
            exit 1
        }
        $TempServerBuild = Join-Path $env:TEMP "mihome-server-build"
        if (Test-Path $TempServerBuild) { Remove-Item $TempServerBuild -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $TempServerBuild | Out-Null

        $ServerNuitkaArgs = @(
        "ha_server.py"
        "--standalone"
        "--windows-console-mode=disable"
        "--include-package=cuktech_ble"
        # winrt 是 PEP420 命名空间包，winrt-windows-* 子轮各挂一片子命名
        # 空间；pywinrt 运行期按需 import 事件参数所在模块（如广播回调的
        # winrt.windows.foundation.collections）。必须逐模块点名让 Nuitka
        # 编进产物，否则运行期直接抛 ModuleNotFoundError（磁盘补拷无效，
        # 编译期 finder 已登记该模块不存在）。清单按 venv 实际子模块枚举。
        "--include-module=winrt.windows.devices.bluetooth"
        "--include-module=winrt.windows.devices.bluetooth.advertisement"
        "--include-module=winrt.windows.devices.bluetooth.genericattributeprofile"
        "--include-module=winrt.windows.devices.enumeration"
        "--include-module=winrt.windows.devices.radios"
        "--include-module=winrt.windows.foundation"
        "--include-module=winrt.windows.foundation.collections"
        "--include-module=winrt.windows.storage.streams"
        # 服务端 web 静态前端（内存预读 + FileResponse 兜底，必须随包）
        "--include-data-dir=web=web/"
        # 只打包受版本控制的示例模板；配置值全部为占位/空
        "--include-data-files=$ServerTemplate=config.default.yaml"
        "--nofollow-import-to=tkinter,unittest,pytest,docker,systemd"
        "--jobs=4"
        "--assume-yes-for-downloads"
        "--output-dir=$TempServerBuild"
        "--output-filename=CuktechBleServer.exe"
        "--product-name=CuktechBleServer"
        # 版本资源从服务端 pyproject.toml 的 [project] version 读取
        "--product-version=$ServerVersion"
        "--file-version=$ServerVersion"
        "--company-name=MiHome-Ex contributors"
        "--file-description=CUKTECH 充电器 BLE 服务端（MiHome-Ex 扩展组件）"
        "--copyright=MIT (C) kairui1108/cuktech-ble-server contributors"
    )
    Push-Location $ServerRoot
    try {
        $env:PYTHONPATH = Join-Path $ServerRoot "src"
        # 输出量大且含 stderr。PowerShell 5.1 下 *> / 2>&1 重定向都会被
        # $ErrorActionPreference=Stop 把 stderr 行升级成终止错误；
        # 借 cmd.exe 承接重定向，PowerShell 只接收 cmd 的退出码
        $ServerLog = "$TempServerBuild\build.log"
        $ArgLine = ($ServerNuitkaArgs | ForEach-Object {
            if ($_ -match ' ') { '"' + $_ + '"' } else { $_ }
        }) -join ' '
        cmd /c "`"$ServerVenvPy`" -m nuitka $ArgLine > `"$ServerLog`" 2>&1"
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $TempServerBuild "ha_server.dist"))) {
        # winrt 是 PEP420 命名空间包（无 __init__.py，winrt-windows-* 子轮
        # 各自往 winrt/windows/... 放纯 Python shim），Nuitka 对这类包的
        # 处理不完整：dist 里缺 windows/ 下的 py shim，也缺根级的
        # _winrt_windows_foundation_collections.pyd（广播包解析必需，
        # 缺了它 BLE 扫描回调全炸、永远发现不了充电器）。编译后按实际
        # 编译解释器（$ServerVenvPy——CI 上是主 venv，本地是服务端
        # .venv）的 site-packages 清单把 winrt 根级 pyd 与 windows/ 树补齐。
        $WinrtSitePackages = (& $ServerVenvPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
        $WinrtVenv = Join-Path $WinrtSitePackages "winrt"
        $WinrtDist = Join-Path $TempServerBuild "ha_server.dist\winrt"
        Get-ChildItem $WinrtVenv -Filter "_winrt*.pyd" | ForEach-Object {
            $dst = Join-Path $WinrtDist $_.Name
            if (-not (Test-Path $dst)) {
                Copy-Item $_.FullName $dst
            }
        }
        $WinrtShimSrc = Join-Path $WinrtVenv "windows"
        if (Test-Path $WinrtShimSrc) {
            Copy-Item $WinrtShimSrc (Join-Path $WinrtDist "windows") -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path "dist\ble-server" | Out-Null
        Copy-Item (Join-Path $TempServerBuild "ha_server.dist\*") "dist\ble-server" -Recurse -Force
        Remove-Item $TempServerBuild -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "BLE server extension built: $ServerExe" -ForegroundColor Green
        } else {
            Write-Host "[ERROR] BLE 服务端扩展编译失败。完整版发布必须包含该组件；如只需客户端版请使用 -ClientOnly。" -ForegroundColor Red
            # nuitka 的输出重定向到了日志文件，不回显的话 CI 上只能看到
            # "编译失败" 四个字、无从诊断——回显尾部若干行
            if (Test-Path $ServerLog) {
                Write-Host "---- nuitka 日志尾部 ----" -ForegroundColor DarkGray
                Get-Content $ServerLog -Tail 25
                Write-Host "------------------------" -ForegroundColor DarkGray
            }
            exit 1
        }
    } else {
        Write-Host "[OK] BLE server extension already built" -ForegroundColor Green
    }

    # 无论新编译还是复用旧产物，都必须验证模板与版本，防止旧 dist 混入。
    $PackagedTemplate = "dist\ble-server\config.default.yaml"
    try {
        Assert-ServerConfigTemplate -Path $PackagedTemplate
    } catch {
        Write-Host "[ERROR] 打包模板不安全：$($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
    $TemplateHash = (Get-FileHash -LiteralPath $PackagedTemplate -Algorithm SHA256).Hash
    $SourceHash = (Get-FileHash -LiteralPath $ServerTemplate -Algorithm SHA256).Hash
    if ($TemplateHash -ne $SourceHash) {
        Write-Host "[ERROR] dist\ble-server\config.default.yaml 与 config.yaml.example 不一致，拒绝发布" -ForegroundColor Red
        exit 1
    }
    $BleVersion = (Get-Item $ServerExe).VersionInfo.ProductVersion
    if ($BleVersion -notlike "$ServerVersion*") {
        Write-Host "[ERROR] BLE 产物版本 $BleVersion 与 pyproject.toml $ServerVersion 不一致；请用 -Clean 重建" -ForegroundColor Red
        exit 1
    }
    Write-Host "  BLE extension version: $BleVersion" -ForegroundColor Gray
}

$Exe = "dist\MiHome-Ex.exe"
if (Test-Path $Exe) {
    $ExeVersion = (Get-Item $Exe).VersionInfo.ProductVersion
    if ($ExeVersion -notlike "$AppVersion*") {
        Write-Host "[ERROR] 主程序产物版本 $ExeVersion 与 app/__init__.py $AppVersion 不一致" -ForegroundColor Red
        exit 1
    }
    $Size = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
    $Edition = if ($ClientOnly) { "客户端版" } else { "完整版" }
    Write-Host "`n构建成功! [$Edition] $Exe ($Size MB)" -ForegroundColor Green
    if ($ClientOnly) {
        Write-Host "客户端版不含内置充电器服务：使用前在 设置 → 充电器远程后端 填入后端地址。" -ForegroundColor Yellow
    }
    # 显式归零：最后一条原生命令的 $LASTEXITCODE 可能残留非零值，
    # GitHub Actions 的 pwsh 步骤按它当退出码会误报失败。
    exit 0
} else {
    Write-Host "[ERROR] 找不到输出文件" -ForegroundColor Red
    exit 1
}
