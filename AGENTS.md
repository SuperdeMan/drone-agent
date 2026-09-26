# AGENTS.md — drone-agent

> 本文件面向所有 AI 编码代理（Claude Code、Codex、Gemini CLI 等）。**项目规则的单一事实来源是 [CLAUDE.md](CLAUDE.md)**，本文件只做入口与摘要；两者冲突时以 CLAUDE.md 为准，改规则先改 CLAUDE.md。

## 必读顺序

1. [CLAUDE.md](CLAUDE.md) — 项目规则全文（阶段状态、目录约定、命名、验证、红线）
2. [docs/architecture/00-overview.md](docs/architecture/00-overview.md) — 架构入口与文档地图；架构级变更**先改文档再动代码**
3. [docs/architecture/02-contracts.md](docs/architecture/02-contracts.md) 与 [03-safety.md](docs/architecture/03-safety.md) — 契约与安全语义，代码以此为准
4. [docs/roadmap.md](docs/roadmap.md) — P/H/X 阶段、历史映射与退出标准；运营工作另读 [09-operations.md](docs/architecture/09-operations.md)、[实施任务](docs/operations-implementation.md) 与 [P2 验收记录](docs/p2-readiness.md)（工作流运行、outbox、取消代次、工单与复检的接手物；资源与领取闸门见 [P1 记录](docs/p1-readiness.md)）
5. [docs/decisions.md](docs/decisions.md) — 技术决策记录，只增不删
6. [docs/reuse-from-embodied-agent.md](docs/reuse-from-embodied-agent.md) — 移植任何姊妹项目代码前必读

## 最低纪律（摘要，全文见 CLAUDE.md）

- **验证**：任何代码改动后 `uv run ruff check .` + `uv run pytest -q` 必须绿；契约测试不许跳过或放宽。
- **联调**：默认使用云服务器，入口 `scripts/dev_stack.py`，见 `docs/cloud-development.md`；不自动启动本地真栈。真机控制链仍在设备侧。
- **语言**：文档中文，README 中英双版（`README.md` 英文、`README.zh-CN.md` 中文，同步更新）；标识符与 commit message 英文（Conventional Commits）；注释与 docstring 中英双语（英文在前，`# English / 中文`），改一处两种语言一起改。
- **安全红线**：LLM/VLM 只产出 `MissionSpec` / 后续 `WorkflowSpec` 类型化草案与业务判断，永不触达控制出口；只有 `guardian` 持有飞控连接；`UNKNOWN` 不是成功；不禁用飞控失效保护；不提供 kill 接口。
- **密钥**：API key 不进代码 / commit / 日志；严禁复制 car-agent 的 `.env`。
- **网络**：uv 拉包用每次命令的环境变量走镜像：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`，不写入全局配置。
- **路径**：仓库路径含中文，原生库加载前复制到 ASCII 路径；Linux 侧仓库放 ASCII 路径。
- **术语**：`qpos`/`qvel`/`gripper`/`ee_pose`/`joint_targets`/`cockpit`/`cabin`/`座舱` 不得出现在本仓库代码中（契约测试扫描）；`vehicle` 不禁，因 PX4 / MAVLink 用它指飞行器。
- **模块**：随里程碑创建，不预建空模块；新增顶层目录先改文档。
