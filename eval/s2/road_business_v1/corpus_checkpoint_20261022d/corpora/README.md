# 当前审查入口：先保留22负结果，再看来源hold后的19条局部副本

**原22条结果为negative，保持原样。** `actual-audit-v1/core-audit.json`：22观察、21主候选、1旧repeat、1真实旧pair；22条来源/原生解码/独立重放与标签绑定核验，pair问题0，但Santolan三条曝光未裁定观察各报2条 `exposed_image_outside_development`，共6条；全局另15条。report SHA256：`667a1603811f2716b710cd9c981da9df4e146905a709c6a3b2fecbdd6d57f2c4`。原manifest、labels、negative audit、初版README与输入账本均未改。

`filtered19-v1/` 是之后追加的新局部副本：仅按来源曝光准入hold精确移出 `oam-extract-004a50bff322d5`、`oam-extract-007e2160190a1e`、`oam-extract-1c19cb36a18f50`。没有按业务答案、类别配额或模型质量筛选；发生在正式数据/模型测试冻结前。19条sample和label字典均与原22条对应记录完全相同，旧pair和协议阈值不变，protocol/exposure文件字节相同。

**19条副本实际审核：19观察、18主候选、1旧repeat、1原pair；来源/原生解码/重放核验19，逐观察问题0、逐对问题0；整体仍15个全局问题，`missing` / `data_ready=false`。** 真实执行UTC 2026-10-22 12:15:44.751731至12:16:28.734481，使用冻结cdf四核心，7,806份绑定输入前后重哈希一致。没有上传或下载源缓存。

物理元数据审核精确支持6条，其中3条Santolan仍受曝光准入hold；来源hold后的19条包含旧16加新增2条CRASAR和1条Ambil。hold没有撤销原物理支持，也没有获授正式曝光裁定。OAM原来源站点/曝光声明与限制始终保留。19条只是当前可组合局部范围，不能称“22全绿”。正式资产准入、正式配对准入、正式数据冻结、正式模型调用均为0，D089和WP-P6-01未关闭。

审查文件：

- 原22：`README.md`、`postrun-verification.json`、`actual-audit-v1/`、`control-v2/`。
- 新19：`filtered19-v1/README.md`、`source-exposure-hold-selection.json`、`postrun-verification.json`、`actual-audit-v1/`、`control-v1/`，后五项均在filtered19目录内。
- 控制代码：原 `prepare_composed22_v1.py` / `run_cloud_audit_v2.py`；新 `filtered19-v1/prepare_and_run_filtered19_v1.py`；两份独立postrun verifier。
- 最终本地完整文件摘要：`SHA256SUMS-final-v2.txt`。新19核心report SHA：`c2802a83b35acb1b4ddd9c0e7905017162258803b8c26a2bbc336e39099d78e1`；manifest SHA：`63e51a05ac1752d209f5af0dd955999eea8dc07baf695573691c528efbe25fbd`。

所有预检失败保留。`control-v1/local-preflight-failure.json` 记录历史606份本机range HTTP JSON与原云端绑定摘要不同，修正版要求原云端精确SHA且未覆盖本机副本。初次收尾元数据同步也按“不同字节不覆盖”停止：只有 `control-v2/remote-preflight.json` 的本机副本多一个末尾换行，原远端回执保持，原因与双摘要见 `finalization-known-difference-v2.json`；它不是核心审核输入差异。最终同步只补入指定新文件，既有两份回执均保留。
