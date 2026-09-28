; SPDX-License-Identifier: GPL-3.0-or-later
; Copyright (C) 2026 MiHome-Ex contributors
; MiHome-Ex Windows 安装包脚本（Inno Setup 6）
;
; 用法：先运行 build.ps1 生成 dist\MiHome-Ex.exe 及其运行时文件，
; 再用 ISCC 编译本脚本得到 installer\Output\MiHome-Ex-setup-<版本>.exe

#define MyAppName "MiHome-Ex"
#define MyAppVersion ReadIni(SourcePath + "\version.ini", "version", "value", "0.2.0")
#define MyAppPublisher "MiHome-Ex contributors"
#define MyAppExeName "MiHome-Ex.exe"

[Setup]
AppId={{7E5F8C2A-9B1D-4A6E-8C3F-2D4A5B6C7D8E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/huanyuejue/MiHome-Windows
AppSupportURL=https://github.com/huanyuejue/MiHome-Windows
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

[Files]
; standalone 目录的全部运行时文件（build.ps1 已把 run.dist 摊平到 dist\）。
; Excludes 排除 MSVC 运行库 DLL：360 等安全软件对"新写入的运行库 DLL"
; 确定性锁定导致安装失败（构建期同样问题见 build.ps1 注释）。运行期由
; Windows System32 提供同款（Win10/11 装 VC++ Redistributable 后必有，
; 多数系统开箱即有）。
Source: "..\dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "msvcp140.dll,msvcp140_1.dll,msvcp140_2.dll,concrt140.dll,vcamp140.dll,vccorlib140.dll,vcomp140.dll,vcruntime140.dll,vcruntime140_1.dll,msvcp140_codecvt_ids.dll"
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion; Check: FileExists(ExpandConstant('{src}\LICENSE'))

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F >nul 2>&1"; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
; Nuitka onefile/standalone 运行残留（如有）
Type: filesandordirs; Name: "{app}\*.tmp"

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  // 静默结束正在运行的实例（忽略失败：可能本就没在运行）
  Exec(ExpandConstant('{cmd}'), ExpandConstant('/C taskkill /IM {#MyAppExeName} /F >nul 2>&1'), '', SW_HIDE, True, ResultCode);
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 用户数据保留在 %LOCALAPPDATA%\MiHome-Windows\，此处不自动删除，
    // 如需彻底清理可手动删除该目录
  end;
end;
