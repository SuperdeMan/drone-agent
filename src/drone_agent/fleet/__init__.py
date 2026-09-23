"""Mission service side of M2: business ledger, robot catalog, fleet transport, evidence verifier and reports.

The package is imported piecemeal: the onboard uplink uses only `events`, `transport` and `pki`, so this
module stays empty and never pulls the planner or model providers into an aircraft process.

M2 的任务服务侧：业务账本、机器人目录、车队传输、证据验证与报告。本包按模块导入：机载 uplink 只用
`events`、`transport` 与 `pki`，因此本模块保持为空，绝不把规划器或模型 provider 带进机载进程。
"""
