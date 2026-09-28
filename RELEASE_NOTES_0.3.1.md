## 更新内容

### 变更
- 应用身份统一为 MiHome-Ex：数据目录迁移至 `%LOCALAPPDATA%\MiHome-Ex\`（旧 `MiHome-Windows\` 数据首次启动自动搬迁，设置/托盘配置/充电历史不丢）
- 开机自启动注册表值、单实例锁、User-Agent、版权信息同步更名；旧版实例运行时二次启动只唤起旧实例

### 新增
- CI 流水线：push/PR 自动跑全部测试（Python 3.10/3.12 双矩阵）；打 `v*` 标签自动构建安装包 + 便携版 zip 并发布 Release

### 修复
- 修复 Python 3.10 下 `string.Template.get_identifiers` 不存在导致主题初始化崩溃（CI 3.10 矩阵发现，已加优雅回退）

**SHA-256 校验和**（附件 SHA256SUMS.txt 同步提供）

```
A03C1F27A948A6C73B4CD538FCF2DC5EE908C9D9A2F9EA036CD1544C77628038  MiHome-Ex-setup-0.3.1.exe
7389B846312D50BD63C669216EE3AE83AAAA9EB2509FDEE018E1966A9A72EA43  MiHome-Ex-0.3.1-x64-portable.zip
```
