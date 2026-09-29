# 更新内容

### 修复（充电器一键登录 401）

- **根因**：mijiaAPI 扫码登录的服务是 `sid=mijia`，换得的 serviceToken 只对 `api.mijia.tech` 有效；而充电器凭据提取（设备列表 / beaconkey）走的是 `api.io.mi.com`，该域名要求 `sid=xiaomiio` 域的 token。两域不通用，一键登录第一步就被小米云以 HTTP 401（`auth error`）拒绝——表现为「充电器一键登录提示失败，桌面端看不到充电器设备」。
- **修复**：一键登录前先用登录会话里的 passToken 向小米账户服务（`serviceLogin?sid=xiaomiio`）换发 io.mi.com 域 token，再执行设备列表与 beaconkey 提取（与上游 BLE 服务端登录路径同源）。真机端到端验证：换发 → 设备列表 → 充电器识别（njcuk.fitting.ad1204）→ beaconkey 获取全链路通过。
- **补强**：缺 passToken、换发被拒（登录态失效）时给出明确中文提示；换发响应含登录凭据，只记成败不记内容。
- **测试**：凭据提取套件新增假账户服务，覆盖换发链路 / 缺 passToken / 换发被拒三个新用例（12 项断言）；其余套件回归全绿。

### 说明

- 本次修复只影响桌面端云端凭据提取，BLE 服务端无需升级（保持 v1.1.2 配套）。
- 设计依据与实证结论已补记至 `docs/04-decisions.md` ADR-009（2026-09-29 修订）：serviceToken 按服务域隔离，原「auth.json 三要素即可直调」结论对 io.mi.com 不成立。
- 无需重新扫码登录，升级后直接点「充电器一键登录」即可。
