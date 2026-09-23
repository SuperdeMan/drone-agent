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

M1 completed on 2026-09-20 for PX4 SITL / Gazebo: executive/guardian processes, durable command reconciliation, five skills, authenticated local IPC, actual camera evidence and an independent judge. Revision `eefe76e` passed 400 tests and all 66 seeded flight/fault cases, with zero false success reports and matching offline replay. See the [qualified release evidence and scope](docs/m1-readiness.md).

M2 (constrained agent) closed on 2026-09-23: revision `f362b9e` passed the release gate, whose last criterion, the live admission-rate baseline with MiniMax-M3, admitted all 20 plannable requests on the first attempt, refused all 8 that should be refused, and admitted none of the 4 that must be blocked. Natural-language requests are planned by MiniMax-M3 by default (D029) into a narrow draft, compiled and admitted fail-closed, approved against the exact package hash, signed with Ed25519 and pulled over mTLS by an onboard uplink; the executive and guardian verify the signature again before flying the new two-phase inspection skill, and a mission service rechecks the evidence and writes a three-column report. On revision `f362b9e` 18/18 seeded end-to-end cases (nominal requests in three phrasings, a degraded-image retry, a service outage, a refusal and a fooled planner) passed with zero false success reports and matching replay, the full M1 matrix still passes (66/66, run in quiet-window batches on the shared server), and both adversarial corpora block every case. Those runs used labelled scripted planner answers; the baseline is the recorded live run. See the [M2 record](docs/m2-readiness.md).

## Development

Linux builds and integration runs now default to the cloud workspace. Use the [cloud development guide](docs/cloud-development.md) and the existing SSH connection settings:

```bash
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py verify
uv run python scripts/dev_stack.py test
```

The fixed simulation console can run in the cloud behind Tailscale Serve, with no separate application login. Run `uv run python scripts/dev_stack.py console-cloud --status` to get its private HTTPS URL; see the [Tailnet console guide](docs/tailnet-console.md) for deployment and access boundaries. The original local bridge remains available through `uv run python scripts/dev_stack.py console` at <http://127.0.0.1:8768>. Both entries show telemetry and camera frames and send pause/resume/cancel through the existing executive channel. This is the M1 human entry. The M2 mission desk (hri.v0 console and A2A gateway) runs locally with `uv run python -m drone_agent.console.mission --local` at <http://127.0.0.1:8769>: submit, plan, admit, approve and sign in process, while flights stay in the cloud (D023). It can also run resident in the cloud behind its own Tailscale Serve port (D035): `uv run python scripts/dev_stack.py desk-cloud --status` returns the private URL, approved missions fly in the cloud simulator under a supervisor that holds the project lock, and every finished mission is judged independently against simulator truth. See the [mission desk guide](docs/tailnet-desk.md). On revision `b9cf00c` it was accepted over real Tailnet HTTPS with labelled scripted planning: a nominal inspection, an in-flight restart of the page and the supervisor (adopted, not flown again, finished after a policy-approved retry), an operator cancel (no automatic retry), a refusal and an out-of-scope request, with zero false success reports and matching replay; see the [desk validation record](docs/tailnet-desk-readiness.md). On `74984f9`, with the model key in place and the mission service reaching the model only through an allowlisted egress proxy (D036), MiniMax-M3 planned free-form Chinese and English requests in about 3.4 s each (both flown and judged completed) and refused a privacy-intrusive request.

The [version-qualified live entry validation](docs/live-console-readiness.md) records the deployed runtime, local UI version, 8 live HTTP runs and the 18-case M1 regression subset. The original 66-case M1 baseline remains tied to `eefe76e`.

The cloud-resident entry has its own [Tailnet validation record](docs/tailnet-console-readiness.md), including the private HTTPS boundary, service restart behavior and exact deployed revision.

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
