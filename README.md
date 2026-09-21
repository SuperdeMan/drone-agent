# drone-agent

**English** | [中文](README.zh-CN.md)

A safety-constrained mission runtime for air-ground heterogeneous robots, drone-first.

It compiles natural-language goals into verifiable, typed missions (`MissionSpec`), executes them onboard within local constraints, confirms outcomes with real evidence, and hands tasks off between drones and ground robots. Large models only express goals and judge evidence; a deterministic runtime owns execution, recovery and safety; the flight controller's native failsafes and manual takeover are never bypassed.

## Where to start

| Question | Read |
|---|---|
| Project rules, current phase, red lines | [CLAUDE.md](CLAUDE.md) (entry for AI coding agents: [AGENTS.md](AGENTS.md)) |
| Architecture overview and document map | [docs/architecture/00-overview.md](docs/architecture/00-overview.md) |
| The six contracts and the three-way verdict | [docs/architecture/02-contracts.md](docs/architecture/02-contracts.md) |
| Safety (runtime assurance, recovery policy graph, stop semantics) | [docs/architecture/03-safety.md](docs/architecture/03-safety.md) |
| Air-ground collaboration | [docs/architecture/04-air-ground.md](docs/architecture/04-air-ground.md) |
| Roadmap M0–M6 | [docs/roadmap.md](docs/roadmap.md) |
| Decision log | [docs/decisions.md](docs/decisions.md) |
| Frontier survey and the GPT-6 Pro review | [docs/research/](docs/research/) |
| What is reused from the sibling projects | [docs/reuse-from-embodied-agent.md](docs/reuse-from-embodied-agent.md) |

Design documents are written in Chinese; code identifiers are English and code comments are bilingual (English first, then Chinese).

## Architecture in one picture

```text
L0 operator entry (console, voice via cockpit-agent over A2A, API)
L1 mission planning and coordination (LLM/VLM planner, read-only MCP tools, fleet coordinator)
L2 deterministic compilation and admission (capability, space, time, energy, airspace, resources; approval bound to version)
L3 onboard mission executive (task DAG, long-running skills, local authoritative state, evidence)
L4 local autonomy (perception, localization, local map, deterministic planner; learned policies as shadow-first plugins)
L5 safety supervisor and control egress (guardian process: lease and sequence, freshness, envelope, Simplex decision)
L6 platform adapters (PX4 via MAVSDK / px4_ros2, DJI Cloud API, ArduPilot, Nav2) -> flight controller failsafes, RC takeover
```

Five principles: models express goals and the runtime executes them; a single control egress; evidence before success (`UNKNOWN` is never success); contracts before modules; capability negotiation instead of fake uniform interfaces.

## Status

M1 completed on 2026-09-20 for PX4 SITL / Gazebo: executive/guardian processes, durable command reconciliation, five skills, authenticated local IPC, actual camera evidence and an independent judge. Revision `eefe76e` passed 400 tests and all 66 seeded flight/fault cases, with zero false success reports and matching offline replay. See the [qualified release evidence and scope](docs/m1-readiness.md). M2 adds constrained language planning and remote mission services.

## Development

Linux builds and integration runs now default to the cloud workspace. Use the [cloud development guide](docs/cloud-development.md) and the existing SSH connection settings:

```bash
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py verify
uv run python scripts/dev_stack.py test
```

To start and observe a fixed cloud inspection from your browser, run `uv run python scripts/dev_stack.py console` and open <http://127.0.0.1:8768>. The page shows live onboard telemetry and camera frames, with pause/resume/cancel through the existing executive channel. The simulation runs in the cloud over the existing SSH connection. See the [live simulation guide](docs/live-simulation.md) for setup, connection semantics and evidence boundaries; this is the M1 human entry, not the full M2 planning/approval console.

To review a recorded cloud run as a human, list the runs, fetch one (every file is checked against the remote digests and the judge receipt) and open the generated offline `viewer.html`:

```bash
uv run python scripts/dev_stack.py runs
uv run python scripts/dev_stack.py fetch --run m1-<run_id> --cases nominal-7 --apply --artifacts D:/drone-agent-cloud
uv run python -m drone_agent.eval.viewer <fetched run directory>
```

The viewer only displays and re-hashes; every verdict comes from the judge (see [evaluation](docs/architecture/08-evaluation.md) §7).

Local editing and quick deterministic checks remain available:

```bash
# Behind the Chinese firewall: use the mirror per command, never in global config
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --group dev
uv run ruff check .
uv run pytest -q
uv run python scripts/generate_contract_fields.py --check
uv run python scripts/generate_proto.py
```

PX4 SITL, Gazebo and ROS 2 run on Linux only (WSL2 or Docker) and the repository must live under an ASCII path there. The contract and planning layers are plain Python and test on Windows directly.

The [simulation guide](sim/README.md) retains the explicit local fallback recipe. The cloud tooling never silently falls back to a local stack. The [proto guide](proto/README.md) describes wire boundaries; the [skill catalog](docs/m1-skill-catalog.md) distinguishes draft manifests from implemented capabilities. Recovery scenario names denote planned coverage, not passed injection runs.

## Sibling projects

- `../embodied-agent`: tabletop manipulator; template for this repository and the main source of reused code.
- `../car-agent`: intelligent cockpit; reference for voice, permissions and the task ledger; can act as an authorized mission entry point over A2A.

## License

Apache-2.0
