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
#>

param([switch]$Clean)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

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
    & $Python -m pip install nuitka ordered-set zstandard
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
    $AppVersion = (Select-String -Path "app\__init__.py" -Pattern '^__version__\s*=\s*"(.+?)"').Matches[0].Groups[1].Value
    Write-Host "  Version: $AppVersion" -ForegroundColor Gray
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
# 独立 Nuitka standalone，输出 dist\ble-server\。可选降级：编译失败
# 仅警告不阻塞主应用（BleServerManager 按组件缺失静默不工作）。
# 开发态在同级目录 ..\cuktech-ble-server；CI 的 checkout 落在工作区内部
# <workspace>\cuktech-ble-server（ci.yml 的 path:），两个位置都要认，
# 否则扩展组件被当作"源码缺失"跳过、安装包不带 ble-server
$ServerRoot = Join-Path $Root "..\cuktech-ble-server"
if (-not (Test-Path (Join-Path $ServerRoot "ha_server.py"))) {
    $ServerRoot = Join-Path $Root "cuktech-ble-server"
}
# CI 上服务端仓库是 fresh checkout，没有 .venv——此时退回主 venv 的
# python（依赖已由 CI 步骤装好），否则扩展组件会被整段跳过、产出的
# 安装包不带 ble-server（实测 v0.4.4 即如此）
$ServerVenvPy = Join-Path $ServerRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $ServerVenvPy)) { $ServerVenvPy = $Python }
$ServerExe = "dist\ble-server\CuktechBleServer.exe"
if (-not (Test-Path $ServerExe)) {
    if (-not (Test-Path (Join-Path $ServerRoot "ha_server.py"))) {
        Write-Host "`n[WARN] 未找到 BLE 服务端源码（$ServerRoot），跳过扩展组件构建。" -ForegroundColor Yellow
    } else {
    Write-Host "`nBuilding BLE server extension (cuktech-ble-server)..." -ForegroundColor Cyan
    # 不用 2>&1 重定向：脚本级 $ErrorActionPreference=Stop 会把
    # stderr 文本升级为终止错误（NativeCommandError 假警报）
    & $ServerVenvPy -m pip install --quiet nuitka ordered-set zstandard
    $TempServerBuild = Join-Path $env:TEMP "mihome-server-build"
    if (Test-Path $TempServerBuild) { Remove-Item $TempServerBuild -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $TempServerBuild | Out-Null
    # CI 上服务端仓库是 fresh checkout，config.yaml 被 .gitignore
    # 排除（含真实凭据，不入仓库）——缺失时回退模板，保证安装包
    # 始终带一份可用的 config.default.yaml
    $ServerCfg = Join-Path $ServerRoot "config.yaml"
    if (-not (Test-Path $ServerCfg)) { $ServerCfg = Join-Path $ServerRoot "config.yaml.example" }

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
        "--include-data-files=$ServerCfg=config.default.yaml"
        "--nofollow-import-to=tkinter,unittest,pytest,docker,systemd"
        "--jobs=4"
        "--assume-yes-for-downloads"
        "--output-dir=$TempServerBuild"
        "--output-filename=CuktechBleServer.exe"
        "--product-name=CuktechBleServer"
        # 服务端版本与 bleed 服务端仓库 pyproject 同步（当前 1.1.4）
        "--product-version=1.1.4"
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
        # 缺了它 BLE 扫描回调全炸、永远发现不了充电器）。编译后按 venv
        # 清单把 winrt 根级 pyd 与 windows/ 树整体补齐。
        $WinrtVenv = Join-Path $ServerRoot ".venv\Lib\site-packages\winrt"
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
        Write-Host "[WARN] BLE 服务端扩展编译失败，安装包将不含该组件（主应用功能不受影响，充电器卡片按组件缺失语义缺席）。" -ForegroundColor Yellow
        # nuitka 的输出重定向到了日志文件，不回显的话 CI 上只能看到
        # "编译失败" 四个字、无从诊断——回显尾部若干行
        if (Test-Path $ServerLog) {
            Write-Host "---- nuitka 日志尾部 ----" -ForegroundColor DarkGray
            Get-Content $ServerLog -Tail 25
            Write-Host "------------------------" -ForegroundColor DarkGray
        }
    }
    }
} else {
    Write-Host "[OK] BLE server extension already built" -ForegroundColor Green
}

$Exe = "dist\MiHome-Ex.exe"
if (Test-Path $Exe) {
    $Size = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
    Write-Host "`n构建成功! $Exe ($Size MB)" -ForegroundColor Green
} else {
    Write-Host "[ERROR] 找不到输出文件" -ForegroundColor Red
}
