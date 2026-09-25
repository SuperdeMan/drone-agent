# 本地运行时与车队协议

[项目首页](../README.zh-CN.md) · [架构总览](../docs/architecture/00-overview.md) · [契约语义](../docs/architecture/02-contracts.md)

这些文件定义传输边界。M1 guardian 本地服务和 M2 签名任务上行已实现并完成各自的仿真验收，见 [M1 记录](../docs/m1-readiness.md)与 [M2 记录](../docs/m2-readiness.md)。`drone.*.v1` 的字段号、枚举和 RPC 签名锁定于 [v1-wire-lock.json](v1-wire-lock.json)，破坏性变更必须使用新的 wire 主版本。

当前车队传输由独立 uplink 经 mTLS gRPC 拉取已签名任务包与操作者请求，上传事件、证据媒体、能力和状态。服务端不经此协议发送控制意图；多机器人交接消息已保留在契约中，但交接执行属于 M4-B。

| 文件 | 内容 |
|---|---|
| [contract-fields.json](contract-fields.json) | 从 Pydantic 导出的字段号、类型、必填性与 JSON Schema 约束 |
| [contracts.proto](drone/contracts/v1/contracts.proto) | 六类共享契约、恢复策略与技能描述 |
| [control.proto](drone/control/v1/control.proto) | guardian 本地意图、业务心跳、租约安装、命令状态对账 |
| [fleet.proto](drone/fleet/v1/fleet.proto) | M2 投递拉取 / 确认、签名任务包、操作者请求、事件、媒体与状态；后续交接消息 |
| [autonomy.proto](drone/autonomy/v1/autonomy.proto) | M3 同一架飞行器上进程之间的自主层 IPC（D039）：局部任务、候选轨迹片段、障碍集合、定位报告、授权设定值与撤销、出口节点状态、信念事实；只走私有 Unix 套接字，不进入车队协议；M3-SITL 通过后写入 wire 锁 |

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
uv run python scripts/generate_contract_fields.py --check
uv run python scripts/generate_proto.py
uv run python scripts/freeze_wire.py --check
uv run pytest tests/contracts/test_proto_contracts.py -q
```

生成 Python stub 与 `contracts.pb` 位于 `gen/`，不提交。开发依赖默认包含既有 rpc 组，因此测试不会因缺 grpcio-tools 而跳过。

修改模型后运行 `generate_contract_fields.py` 更新字段清单和共享 proto，再编译。新字段只能追加，脚本拒绝旧字段/枚举的删除、类型改变或重编号。将来需要删除字段时，应在新版本设计中显式保留旧字段号，不能绕过兼容检查。

标量使用显式存在性；空消息不提供授权，所有枚举零值为 `UNSPECIFIED`。自由 JSON 使用 UTF-8 bytes 保留整数精度，必须解码并经过领域校验。proto 自身不执行 Pydantic 的范围、DAG、审批或租约校验。

M1 本地服务使用私有 UDS + gRPC local credentials，并绑定启动时的可信任务包与 executive 身份；账本持久化代次/对账，wire 编解码保留存在性并重新校验领域模型。`HandoffReply.accepted` 只表示愿意接收，不能当作所有权已转移，也不代表已有交接执行器。M2 已实现 Ed25519 审批签名、机载独立验签与 mTLS 证书身份；M1 本地信任模式的哈希检查仍不等于数字签名。

wire 命名空间与领域载荷版本分开：本发行包的领域 `schema_version` 为 `0.1.0`，本地运行时拒绝缺版本或不支持的载荷版本；冻结的 v1 线路布局禁止重编号，允许兼容增字段。不可把 protobuf 能解码等同于任务获得授权。
