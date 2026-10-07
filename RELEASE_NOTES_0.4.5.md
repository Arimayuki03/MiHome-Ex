# 更新内容

### 修复（安装包不带/不更新内置 BLE 服务端，端口开关修复未能生效）

- **现象**：v0.4.4 装完后点击 C1/C2/C3/USB-A 端口开关仍报「切换失败：充电器拒绝命令：port write not confirmed」，机器上跑的 BLE 服务端还是 v1.1.1。
- **根因**：`build.ps1` 编译扩展组件的前置条件是 `Test-Path $ServerVenvPy`（服务端仓库的 `.venv`）。CI 上服务端仓库是 fresh checkout、**没有 `.venv`**，条件不成立 → 扩展组件构建被整段跳过，产出的安装包**根本不含 `ble-server\` 组件**（CI 日志里完全没有 "Building BLE server extension"）。
- **打包修复**：
  - 服务端无 `.venv` 时回退主 venv 的 python（依赖已由 CI 步骤装好），扩展组件不再被跳过
  - `config.yaml` 在服务端仓库被 `.gitignore` 排除（含真实凭据不入仓库），CI 上不存在 → 打进包的 `config.default.yaml` 回退为 `config.yaml.example`
  - Inno Setup 主文件条目带 `ignoreversion`，已存在同名文件会被直接跳过不覆盖；扩展组件改为单独条目（不带 `ignoreversion`）+ `[InstallDelete]` 安装前先清掉旧的 `ble-server\` 目录，保证每次升级拿到匹配版本
  - `InitializeSetup` 结束服务进程后补 1.5s 等待，避免文件句柄未释放导致删除失败
  - 顺带把内置服务端的 `product-version` 从过时的 1.1.1 同步到 1.1.4

### 缓解杀软误报（360 等）

用户反馈 360 报 `HEUR/QVM202.0.9349.Malware.Gen`。经与官方 Release 产物 SHA-256 逐字节比对确认是官方构建、非感染，属 QVM 机器学习引擎对未签名 Nuitka 编译程序的典型误报。已做的缓解：

- 主程序与服务端扩展的 exe 补全 `CompanyName` / `FileDescription`（空公司名 + 空描述的未签名 exe 是 QVM 高权重误报特征）
- README 新增「杀毒软件报毒（误报说明）」章节：`SHA256SUMS.txt` 自验方法、VirusTotal 判读要点、360 与 Windows Defender 信任区操作路径

> 彻底消除需代码签名证书（EV 证书可解决大部分信誉类误报），项目暂无该预算。

### 说明

- 本次内置 [cuktech-ble-server v1.1.4](https://github.com/Arimayuki03/cuktech-ble-server/releases/tag/v1.1.4)（含端口开关重试修复）。
- **升级后请确认**：安装目录下 `ble-server\CuktechBleServer.exe` 的版本应为 1.1.4（右键属性 → 详细信息）。若仍是旧版本，请先卸载再安装。
