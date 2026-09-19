# M0 协议草案

这些文件定义传输边界，没有启动服务或接入飞控。目标命名空间是 v1；冻结条件仍是 M1 的双进程闭环通过。

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

M1 还需实现：身份认证、租约签发者校验、持久化对账、时间映射与超时预算、领域模型编解码、接收端拒绝未知枚举及错误主版本。`HandoffReply.accepted` 只表示愿意接收，不能当作所有权已转移。
