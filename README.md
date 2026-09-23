# drone-agent

**Natural-language missions. Deterministic execution. Verifiable outcomes.**

**English** | [中文](README.zh-CN.md)

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![M2 complete in simulation](https://img.shields.io/badge/Milestone-M2%20%7C%20simulation-0F766E)](docs/m2-readiness.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

A safety-constrained mission runtime for drones, designed to grow into air-ground robotics. It turns inspection requests into typed missions, checks their boundaries, binds approval to the exact task, and reports what the evidence supports.

> **Current scope:** M2 completed on **2026-09-23** for a single drone in **PX4 SITL + Gazebo** (software-in-the-loop simulation). Local autonomy, hardware flights and air-ground handoff are [planned milestones](docs/roadmap.md).

[Try locally](#try-locally) · [Design](#design) · [Validation](#validation) · [Documentation](#documentation) · [Roadmap](#roadmap)

## What it does

- **Plans inspection missions from natural language.** MiniMax-M3 produces a constrained draft; the planner builds its context from read-only tools and registered assets, routes and flight volumes.
- **Checks before execution.** Deterministic compilation and admission validate capabilities, parameters, space, time, energy, airspace and resources. Unknown or invalid inputs are rejected.
- **Binds approval to the task.** Ed25519 signatures cover the approved version and package hash. Packages travel over mTLS and are independently checked onboard.
- **Executes under local supervision.** The mission executive schedules skills; a separate `guardian` process owns the only flight-controller connection and applies recovery policies. Native failsafes and manual takeover remain available.
- **Reports evidence, including uncertainty.** Image and telemetry checks feed completed / not completed / uncertain reports. MCAP, ULog and event records support independent judging and offline replay.
- **Provides human and agent entry points.** A Web mission desk supports planning and approval; the A2A gateway accepts task submissions and status queries. A resident desk can run behind Tailscale.

The implemented skill set covers takeoff, registered routes, image capture, asset inspection, return-home and landing. M2 inspection uses predefined observation routes in the [simulation scene](configs/scenarios/m2_campus_v2.yaml).

## Try locally

Prerequisites: **Python 3.12+**, **uv** and **Git**. The local mission desk runs on Windows or Linux without PX4 or Gazebo.

```bash
git clone https://github.com/SuperdeMan/drone-agent.git
cd drone-agent
uv sync --group dev
uv run python -m drone_agent.console.mission --local
```

Open **<http://127.0.0.1:8769>**, select `campus_training` and `asset_red`, then submit this exact example:

> Inspect the red equipment marker east of the pad and bring back a photo.

Review the plan and admission checks, then approve the package. **Local mode plans, admits and signs; no robot is connected and nothing flies.**

Without `MINIMAX_API_KEY`, the desk uses labelled scripted answers for the exact requests in the [M2 scenario set](configs/scenarios/m2_suite.yaml). With a key in the process environment, it uses live MiniMax-M3 planning. The page identifies the planner; key setup is documented in the [cloud development guide](docs/cloud-development.md).

### Run the flight loop in simulation

Integration uses a configured Linux cloud workspace. After following the [setup guide](docs/cloud-development.md), inspect the target and retrieve the resident mission desk URL:

```bash
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py desk-cloud --status
```

In the resident desk, approved missions fly in PX4/Gazebo and produce evidence reports and independent judge results. See the [mission desk guide](docs/tailnet-desk.md) for deployment and access, or the [fixed M1 console](docs/live-simulation.md) for the earlier simulation entry.

PX4 SITL and Gazebo run on Linux; use an ASCII repository path for native components. The [simulation guide](sim/README.md) also documents the explicit local fallback.

## Design

**Request → constrained draft → compilation and admission → signed approval → local execution → evidence report.**

| Boundary | Responsibility |
|---|---|
| Ground / cloud planning | The planner proposes a draft; deterministic code builds `MissionSpec`. Models have no control access. |
| Admission and approval | Validate the mission, bind approval to `package_hash`, and deliver a signed `MissionPackage`. |
| Onboard execution | `uplink` receives packages; `executive` schedules skills; `guardian` checks authority, freshness and constraints before control writes. |
| Verification and replay | Recheck evidence and keep `execution_status`, `effect_verdict` and `safety_verdict` separate. `UNKNOWN` never counts as success. |

The cloud sends an authorized mission and its limits. Local execution and recovery do not depend on a continuous stream of cloud-generated control commands. See the [architecture overview](docs/architecture/00-overview.md), [contracts](docs/architecture/02-contracts.md) and [safety model](docs/architecture/03-safety.md).

## Validation

Recorded results below belong to their **exact revisions and scopes**; they are not test results for every later commit.

| Evidence | Revision | Recorded result |
|---|---|---|
| [M1 runtime](docs/m1-readiness.md) | `eefe76e` | 400 tests; 22 flight/fault scenarios × 3 seeds = 66/66 passed; zero false success reports; matching replay. |
| [M2 release gate](docs/m2-readiness.md) | `f362b9e` | 775 tests; 18/18 end-to-end cases; 66/66 M1 regression; 42 deterministic and 32 scripted adversarial cases blocked or refused; zero false success reports and matching flight replay. |
| [Live planner baseline](docs/verification/m2-baseline-2026-09-23.json) | `f362b9e` | MiniMax-M3: 20/20 plannable requests admitted on the first attempt, 8/8 expected refusals, 4/4 required blocks. |
| [Resident desk with live planning](docs/tailnet-desk-readiness.md) | `74984f9` | Chinese and English requests planned, approved, flown and independently verified; a privacy-intrusive request refused. |

The M2 end-to-end and natural-language adversarial runs used **labelled scripted planner answers** to test the execution chain and its boundaries. Live model behavior is documented separately in the baseline and resident-desk records. The [machine-readable release result](docs/verification/m2-2026-09-23-release.json) links the M2 evidence together.

## Development

```bash
uv run ruff check .
uv run pytest -q
uv run python scripts/generate_contract_fields.py --check
uv run python scripts/generate_proto.py
```

For dependency downloads through the project mirror, set `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple` for the current command or terminal session. Contract and planning checks run directly on Windows; Linux builds and integration use `scripts/dev_stack.py`.

Read [CLAUDE.md](CLAUDE.md) before contributing ([AGENTS.md](AGENTS.md) is the coding-agent entry). Architecture and execution changes start with the relevant design documents. Design documents are in Chinese; the READMEs and code comments are bilingual.

## Documentation

| Start here | Guide |
|---|---|
| System boundaries and document map | [Architecture overview](docs/architecture/00-overview.md) |
| Message semantics and runtime assurance | [Contracts](docs/architecture/02-contracts.md) · [Safety](docs/architecture/03-safety.md) · [Wire protocol](proto/README.md) |
| Cloud simulation and the mission desk | [Cloud development](docs/cloud-development.md) · [Mission desk](docs/tailnet-desk.md) |
| Review flight evidence and replay | [Evaluation](docs/architecture/08-evaluation.md) · [Fetch and view a run](docs/cloud-development.md#查看与人工核对结果) |
| Design rationale and reuse | [Decisions](docs/decisions.md) · [Sibling-project reuse](docs/reuse-from-embodied-agent.md) |

## Roadmap

| Stage | Scope | Status |
|---|---|---|
| M0–M2 | Contracts, single-drone runtime, constrained agent and evidence loop | Complete in simulation |
| M3 | Local autonomy, perception, localization and degradation handling | Next |
| M4 | Restricted hardware validation and UAV–rover joint simulation | Planned |
| M5–M6 | Real air-ground collaboration, more platforms and model plugins | Planned |

See the [full roadmap and exit criteria](docs/roadmap.md). ROS 2 / Offboard autonomy, cross-robot handoff and DJI / ArduPilot support are outside the current implementation. Real airspace admission remains a stub that rejects real-mode missions; energy estimates are for simulation only.

## License

[Apache License 2.0](LICENSE).
