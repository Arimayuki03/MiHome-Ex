; SPDX-License-Identifier: GPL-3.0-or-later
; Copyright (C) 2026 MiHome-Ex contributors
; MiHome-Ex Windows 安装包脚本（Inno Setup 6）
;
; 用法：先运行 build.ps1 生成 dist\MiHome-Ex.exe 及其运行时文件，
; 再用 ISCC 编译本脚本得到 installer\Output\MiHome-Ex-setup-<版本>.exe

#define MyAppName "MiHome-Ex"
#define MyAppVersion ReadIni(SourcePath + "\version.ini", "version", "value", "0.4.5")
#define MyAppPublisher "MiHome-Ex contributors"
#define MyAppExeName "MiHome-Ex.exe"

[Setup]
AppId={{7E5F8C2A-9B1D-4A6E-8C3F-2D4A5B6C7D8E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/Arimayuki03/MiHome-Ex
AppSupportURL=https://github.com/Arimayuki03/MiHome-Ex
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename=MiHome-Ex-setup-{#MyAppVersion}
; 版本资源信息：资源管理器里显示版本号与图标
SetupIconFile=..\app\ui\icon.ico
; 压缩：standalone 目录约几百 MB，lzma2 高压缩显著减小安装包
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMADictionarySize=65536
; 允许普通用户安装到自身权限范围，不需要管理员
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
WizardStyle=modern
; 卸载时清理 Nuitka 临时解压目录等
CreateUninstallRegKey=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
; 覆盖语言文件里的默认描述（中文由 ChineseSimplified.isl 提供，
; 英文走 Default.isl 内建）
LaunchProgram=启动 %1

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked; OnlyBelowVersion: 6.1

; 扩展组件（内置 BLE 服务端）：上层主条目用 ignoreversion，已存在同名
; 文件会被直接跳过、不覆盖——实测升级安装后 ble-server\ 下仍是旧版 exe
; 与旧 config.default.yaml（v0.4.4 装完跑的还是 v1.1.1 服务端，端口开关
; 修复因此不生效）。这里在安装前先清掉整个旧组件目录（[InstallDelete]
; 早于 [Files] 执行），再由下面那条不带 ignoreversion 的条目重新写入。
[InstallDelete]
Type: filesandordirs; Name: "{app}\ble-server"

[Files]
; standalone 目录的全部运行时文件（build.ps1 已把 run.dist 摊平到 dist\）。
; Excludes 排除 MSVC 运行库 DLL：360 等安全软件对"新写入的运行库 DLL"
; 确定性锁定导致安装失败（构建期同样问题见 build.ps1 注释）。运行期由
; Windows System32 提供同款（Win10/11 装 VC++ Redistributable 后必有，
; 多数系统开箱即有）。
Source: "..\dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "msvcp140.dll,msvcp140_1.dll,msvcp140_2.dll,concrt140.dll,vcamp140.dll,vccorlib140.dll,vcomp140.dll,vcruntime140.dll,vcruntime140_1.dll,msvcp140_codecvt_ids.dll"
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion; Check: FileExists(ExpandConstant('{src}\LICENSE'))

; 扩展组件单独一条且不带 ignoreversion：按版本/时间戳规则覆盖，
; 配合上面的 [InstallDelete] 确保始终拿到与主程序匹配的版本。
; Check 守卫：扩展组件编译失败时 dist\ble-server\ 不存在，硬引用会让
; Inno 直接 "No files found" 编译中止、连主程序安装包都出不来——降级为
; 不带该组件（BleServerManager 按组件缺失静默不工作）。
Source: "..\dist\ble-server\*"; DestDir: "{app}\ble-server"; Flags: recursesubdirs createallsubdirs; Excludes: "msvcp140.dll,msvcp140_1.dll,msvcp140_2.dll,concrt140.dll,vcamp140.dll,vccorlib140.dll,vcomp140.dll,vcruntime140.dll,vcruntime140_1.dll,msvcp140_codecvt_ids.dll"; Check: DirExists(ExpandConstant('{src}\..\dist\ble-server'))

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F >nul 2>&1"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{cmd}"; Parameters: "/C taskkill /IM CuktechBleServer.exe /F >nul 2>&1"; Flags: runhidden; RunOnceId: "KillBleServer"

[UninstallDelete]
; Nuitka onefile/standalone 运行残留（如有）
Type: filesandordirs; Name: "{app}\*.tmp"

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  // 静默结束正在运行的实例（忽略失败：可能本就没在运行）；
  // 内置 BLE 服务端扩展组件同样先结束，避免覆盖安装文件占用
  Exec(ExpandConstant('{cmd}'), ExpandConstant('/C taskkill /IM {#MyAppExeName} /F >nul 2>&1'), '', SW_HIDE, True, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM CuktechBleServer.exe /F >nul 2>&1', '', SW_HIDE, True, ResultCode);
  // taskkill 返回后文件句柄可能尚未完全释放，给系统一点时间再让
  // [InstallDelete] 删除 ble-server\ 目录（删不掉会残留旧版服务端）
  Sleep(1500);
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 用户数据保留在 %LOCALAPPDATA%\MiHome-Ex\，此处不自动删除，
    // 如需彻底清理可手动删除该目录（旧版残留 %LOCALAPPDATA%\MiHome-Windows\ 同理）
  end;
end;
