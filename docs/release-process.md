# 发布流程

## 版本信源

- **主版本信源**：`app/__init__.py` 的 `__version__`。构建脚本读取它作为 Nuitka 的 `--product-version` / `--file-version`，运行时关于页、更新检查和 User-Agent 也使用该值。
- **发布时同步**：
  - `installer/version.ini`：Inno Setup 的显示版本与安装包文件名；
  - `pyproject.toml`：Python 包元数据版本。
- 三处版本、Git tag（`v<版本>`）和 `RELEASE_NOTES_<版本>.md` 必须一致；CI 在构建前校验三处版本与 tag，打包后再次校验主程序与内置 BLE 服务端的二进制版本。

`app/__init__.py` 是唯一需要人工维护的主版本；`installer/version.ini` 和 `pyproject.toml` 是发布同步项，不是独立信源。

## 发布前检查

1. 版本三处一致，且高于最近一次 Release。
2. 根目录存在对应的 `RELEASE_NOTES_<版本>.md`，内容与本次实际变更一致。
3. 工作区干净；待提交内容不含账号凭据、设备缓存、运行日志、真实 `config.yaml`、私网地址、本机路径或构建产物。
4. 测试按 [testing.md](testing.md) 的实际入口执行并通过；不要把历史基线的通过数量当作本次结论。
5. 内置 BLE 服务端源码固定到已发布的 `v1.2.0` 提交，不使用浮动的 `main`。

## CI 构建与发布

推送 `v*` 标签后，GitHub Actions 在干净的 Windows runner 上执行：

1. Python 3.10 / 3.12 测试矩阵；
2. 检出固定版本的 `cuktech-ble-server` 源码；
3. Nuitka 编译主程序与内置 BLE 服务端；
4. 检查 `dist` 中不含运行数据、本机配置、日志、数据库和临时文件；内置 BLE 默认模板必须来自公开的 `config.yaml.example`，`mac`/`token`/`ble_key`/MQTT 密码等只能是占位值或空值；
5. Inno Setup 从同一已检查的 `dist` 和已审查的 `installer\MiHome-Ex.iss` 生成完整版安装包与客户端版安装包；
6. 将完整版 `dist` 打成便携 zip；
7. **上传 Release 前独立检查便携 zip**：安全解压到临时目录，拒绝绝对路径、`..` 等路径穿越条目，再对解压内容执行与 `dist` 门禁等价的运行数据/本机配置/日志/数据库/私钥/备份/`.mimosa` 等扫描（同一扫描器或专用只读 ZIP 扫描函数）；不扫描大型二进制随机字节。检查失败不得创建 Release。安装包最终由验收另行提取核对，不能以 ZIP 检查代替；
8. 生成 `SHA256SUMS.txt`，校验三个发布产物及主程序/BLE 版本后创建 GitHub Release。

发布资产固定为：

- `MiHome-Ex-setup-<版本>.exe`（完整版，含内置 BLE 服务端）
- `MiHome-Ex-setup-<版本>-client.exe`（客户端版，不含内置 BLE 服务端）
- `MiHome-Ex-<版本>-x64-portable.zip`（完整版便携包）
- `SHA256SUMS.txt`

历史 tag 构建的整条流水线参考耗时约 40 分钟（包含测试和约 35 分钟构建），实际时间随 runner、依赖下载和编译情况变化，不作保证。

## 构建输入与隐私门禁

- 构建只允许读取受版本控制的公开 `config.yaml.example` 作为 BLE 默认模板，**绝不读取或打包本机真实 `config.yaml`**。
- 便携包和安装包只从干净 CI runner 的 `dist` 生成；本机旧的 `dist/`、`installer/Output/` 不得直接上传。
- 构建和打包前后都要拒绝运行数据与隐私文件进入发布产物；`.gitignore` 只能减少误提交，不能代替发布包门禁。
- 发布说明、截图和文档不得包含真实账号、设备标识、设备缓存、私网地址或本机路径。

## 内置 BLE 服务端

0.6.0 的内置 BLE 服务端仍固定为 **v1.2.0**（提交 `0cb099386fb6fc0e8efad745b2b62ebea8d54766`），本次不引入该服务端的新后端功能。构建脚本从服务端仓库的 `pyproject.toml` 读取版本资源，避免二进制继续显示旧的硬编码版本。
