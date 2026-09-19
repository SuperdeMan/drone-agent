# 本地运行时与车队协议

这些文件定义传输边界，M1 已实现 guardian 本地服务。目标命名空间是 v1；冻结条件仍是 M1 的双进程及故障矩阵通过。车队协议保持 M2/M4 接入边界，不提供控制接口。

| 文件 | 内容 |
|---|---|
| [contract-fields.json](contract-fields.json) | 从 Pydantic 导出的字段号、类型、必填性与 JSON Schema 约束 |
| [contracts.proto](drone/contracts/v1/contracts.proto) | 六类共享契约、恢复策略与技能描述 |
| [control.proto](drone/control/v1/control.proto) | guardian 本地意图、业务心跳、租约安装、命令状态对账 |
| [fleet.proto](drone/fleet/v1/fleet.proto) | 任务包、事件、证据、事实、状态、能力与交接提议 |

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
uv run python scripts/generate_contract_fields.py --check
uv run python scripts/generate_proto.py
uv run pytest tests/contracts/test_proto_contracts.py -q
```

生成 Python stub 与 `contracts.pb` 位于 `gen/`，不提交。开发依赖默认包含既有 rpc 组，因此测试不会因缺 grpcio-tools 而跳过。

修改模型后运行 `generate_contract_fields.py` 更新字段清单和共享 proto，再编译。新字段只能追加，脚本拒绝旧字段/枚举的删除、类型改变或重编号。将来需要删除字段时，应在新版本设计中显式保留旧字段号，不能绕过兼容检查。

标量使用显式存在性；空消息不提供授权，所有枚举零值为 `UNSPECIFIED`。自由 JSON 使用 UTF-8 bytes 保留整数精度，必须解码并经过领域校验。proto 自身不执行 Pydantic 的范围、DAG、审批或租约校验。

M1 本地服务使用私有 UDS + gRPC local credentials，并绑定启动时的可信任务包与 executive 身份；账本持久化代次/对账，wire 编解码保留存在性并重新校验领域模型。`HandoffReply.accepted` 只表示愿意接收，不能当作所有权已转移。远程签名签发与证书管理属于 M2，不能把本地哈希检查当成数字签名。
