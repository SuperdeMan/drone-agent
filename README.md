# drone-agent

**Natural-language missions. Deterministic execution. Verifiable outcomes.**

**English** | [中文](README.zh-CN.md)

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![M2 complete in simulation](https://img.shields.io/badge/Milestone-M2%20%7C%20simulation-0F766E)](docs/m2-readiness.md)
[![M3 simulation line passed](https://img.shields.io/badge/M3--SITL-passed-0F766E)](docs/m3-readiness.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

A drone inspection operations platform in development, built on a safety-constrained mission runtime. The implemented simulation stack provides typed missions, admission and approval, local execution and evidence-based reports, plus project-scoped sites and virtual docks that gate every dispatch. Durable workflows, fleet scheduling and finding-to-reinspection workflows are the next product milestones.

> **Current scope:** M2 completed on **2026-09-23** for a single drone in **PX4 SITL + Gazebo** (software-in-the-loop simulation). On **2026-09-25** the simulation line of M3 (local autonomy through a guarded PX4 external mode) passed its release gate; M3 closes only after Jetson-in-the-loop validation. H1 carries the pending JIL work; the original combined M3 gate remains not passed. P0 runtime provenance and product gates passed on **2026-09-25** at `de597d0`; see [P0 validation](docs/p0-readiness.md). P1 projects, virtual docks and the dispatch claim gate passed on **2026-09-26** at `b49701b`: three sites with logical docks and logical aircraft (S0), and one PX4 SITL aircraft through a virtual dock (S1); see [P1 validation](docs/p1-readiness.md). P2 durable workflows passed on **2026-09-26** at `3bdbd50`: restartable inspection → labelled analysis → human review → simulated work order → reinspection runs, without duplicate dispatch or dispatch after a cancel, over three logical aircraft (S0) and on one PX4 SITL aircraft through a virtual dock (S1); see [P2 validation](docs/p2-readiness.md). P3 multi-site, multi-drone scheduling passed on **2026-09-27** at `73fcf23`: deterministic, replayable assignment with airspace-cell holds, withdrawal before a claim and task-level relay, over logical sites and a 10 / 30 / 100-node ladder (S0) and two PX4 SITL aircraft in one Gazebo world (S1); see [P3 validation](docs/p3-readiness.md). The P4 multimodal business loop is implemented at `8f41112` and holds in the logical world (S0), on PX4 SITL with real appearance changes (S1) and on the resident desk; its release gate did **not** pass, because the single live test of the frozen MiniMax-M3 analyzer on a VisA circuit-board subset reached precision 0.8835 against the required 0.90 (recall 0.758 against 0.70). P4 stays open on that criterion; see [P4 validation](docs/p4-readiness.md). P5 software operations can proceed independently of hardware; hardware and air-ground work follow the [H/X milestones](docs/roadmap.md).

[Try locally](#try-locally) · [Design](#design) · [Validation](#validation) · [Documentation](#documentation) · [Roadmap](#roadmap)

## What it does

- **Plans inspection missions from natural language.** MiniMax-M3 produces a constrained draft; the planner builds its context from read-only tools and registered assets, routes and flight volumes.
- **Checks before execution.** Deterministic compilation and admission validate capabilities, parameters, space, time, energy, airspace and resources. Unknown or invalid inputs are rejected.
- **Binds approval to the task.** Ed25519 signatures cover the approved version and package hash. Packages travel over mTLS and are independently checked onboard.
- **Executes under local supervision.** The mission executive schedules skills; a separate `guardian` process owns the only flight-controller connection and applies recovery policies. Native failsafes and manual takeover remain available.
- **Reports evidence, including uncertainty.** Image and telemetry checks feed completed / not completed / uncertain reports. MCAP, ULog and event records support independent judging and offline replay.
- **Flies local autonomy through a guarded second path (M3, simulation).** ROS 2 Jazzy nodes plan short segments from depth maps; the `guardian` filters every target through a control barrier function and authorizes it for a few hundred milliseconds; a PX4 external-mode node only forwards those authorizations and stops when they end. Onboard event detection produces candidate facts only.
- **Dispatches only what the site supports (P1, simulation).** Projects bind sites, docks and aircraft. A dock reports link, lid, aircraft presence, energy, environment and upkeep; one deterministic eligibility check with exclusive reservations gates the preview, the lid opening and the moment the aircraft fetches its package. Holds are released only on a terminal result plus fresh grounded evidence, never on a timeout. Docks are logical simulators, not vendor hardware.
- **Runs durable inspection workflows (P2, simulation).** Versioned templates of whitelisted activities start by hand, on a daily or interval schedule, or from a bound event. Each run pins its template and survives service restarts through a lease, fencing and an outbox; the same activity never dispatches twice, and nothing new starts after a cancel. Every flight still needs its own human approval. Analysis is a labelled scripted fixture or a deterministic colour signature, not recognition quality; a reviewer's confirmation creates a simulated work order, and repair feedback starts a reinspection run.
- **Schedules tasks across sites and aircraft (P3, simulation).** An operator submits an inspection task; a pure, replayable decision filters every candidate with named reasons and ranks the rest by arrival time, recent use and robot id. One transaction assigns the task, compiles its mission and holds the robot, the dock and the airspace cells of its route; a silent aircraft's envelope grows until it is reconciled. An unclaimed assignment blocked too long is withdrawn to another robot with a new epoch, and a definite flight failure relays the task. Every flight still needs its own human approval.
- **Closes the business loop on evidence (P4, simulation).** A deterministic quality layer and durable analysis jobs turn verified captures into candidate findings with labelled sources (live model, recording, scripted or deterministic). Only a project reviewer confirms a finding; each confirmed finding gets one work order, and repair feedback starts a reinspection that closes the order only with a new verified capture taken after the feedback, an unsuspected analysis from an allowed source and the reviewer's confirmation. Model answers are candidates only and never change a flight verdict.
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

To see every operations workspace locally (fleet and docks, workflows, scheduling, findings and work orders), run `uv run python scripts/desk_preview.py --seed` and open <http://127.0.0.1:8770>. It serves the desk over the logical S0 world with the resident desk's catalogs; flights are logical, labelled as such, and nothing in the preview is validation evidence.

### Run the flight loop in simulation

Integration uses a configured Linux cloud workspace. After following the [setup guide](docs/cloud-development.md), inspect the target and retrieve the resident mission desk URL:

```bash
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py desk-cloud --status
```

The unified cloud flight desk provides the **operations desk** (workspaces for missions and approvals, workflows, scheduling, fleet and docks, and findings and work orders; D068) and **M1 fixed inspections** at one origin. Approved M2 missions fly in PX4/Gazebo and produce evidence reports; M1 retains live tracks, camera frames, pause/resume and cancellation. The two modes fly sequentially; switching pages never cancels a flight. See the [flight desk guide](docs/tailnet-desk.md) and the [M2 review and validation record](docs/m2-review-2026-09-24.md).

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
| [M2 review and unified desk](docs/m2-review-2026-09-24.md) | `e8edf28` | 833 Linux checks and 18/18 M2 E2E cases passed; evidence handling, post-cancellation retries and judge coverage fixed. Live-model and interactive checks are listed separately. |
| [M3-SITL release gate](docs/m3-readiness.md) | `75382dc` | 983 tests; 16 local-autonomy and fault scenarios × 3 seeds = 48/48; M1 66/66, M2 18/18 and 6/6 over Zenoh; zero false success reports and matching replay; supervision period p99 ≤ 106.6 ms. Jetson-in-the-loop pending hardware. |
| [P0 release gate](docs/p0-readiness.md) | `de597d0` | 1000 Linux tests; 18/18 scripted M2 E2E cases and source audits; MiniMax-M3 live completion and cancellation probes; authoritative flight-version receipts verified. |
| [P1 release gate](docs/p1-readiness.md) | `b49701b` | 1102 Linux tests; 14 dock and dispatch faults × 3 seeds = 42/42 over three logical sites (S0); 5/5 PX4 SITL cases through a virtual dock (S1); M2 18/18; wrong / duplicate dispatch, wrong release, project escape and false success all zero; resident desk migrated with a verified backup. |
| [P3 release gate](docs/p3-readiness.md) | `73fcf23` | 1226 Linux tests; 15 scheduling faults × 3 seeds = 45/45 and a 10 / 30 / 100 logical-node ladder 3/3 (S0); 5/5 two-aircraft PX4 SITL cases with 9 flights in one world (S1) at a real-time factor ≥ 0.97; P1, P2 S0 and S1 and M2 18/18 regressions; double ownership, duplicate execution, lost tasks, conflicting holds, false success and stale-epoch effects all zero; resident desk migrated with a verified backup and a task scheduled, approved and flown through it. |
| [P4 release gate](docs/p4-readiness.md) | `8f41112` | **Not passed (S2 only).** 1258 Linux tests; 15 business-loop faults × 3 seeds = 45/45 (S0) and the P1 / P2 / P3 S0 matrices; 5/5 PX4 SITL cases with 12 flights and a Gazebo damage patch (S1); P2 S1, P3 S1 and M2 18/18 regressions; resident desk migrated with a verified backup and a watch run analysed by the scripted fixture and live MiniMax-M3; all safety counts zero. The single live S2 test on VisA boards: precision 0.8835 < 0.90, recall 0.758, coverage 0.996, false assurance 0.017. |
| [Operations desk workspaces (D068)](docs/desk-workspaces-readiness.md) | `a0d12ff` | 1263 Linux tests; every workspace rendered through the real Tailnet entry in Edge, light and dark, desktop and phone, with no console errors; live-planned missions submitted, approved and followed in the page to the independent judge; a watch run, a repair and its reinspection driven through the page. No gate status changes. |
| [P2 release gate](docs/p2-readiness.md) | `3bdbd50` | 1174 Linux tests; 15 workflow restart, deduplication and cancellation faults × 3 seeds = 45/45 (S0) plus the P1 S0 matrix 42/42; 5/5 PX4 SITL cases with 8 flights (S1); M2 18/18; duplicate dispatch, dispatch after cancel, false success, false work orders and lost runs all zero; resident desk migrated with a verified backup and a workflow run through to reinspection. |

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
| Operations design and next work | [Operations architecture](docs/architecture/09-operations.md) · [Implementation tasks](docs/operations-implementation.md) · [P4 validation](docs/p4-readiness.md) |
| Design rationale and reuse | [Decisions](docs/decisions.md) · [Sibling-project reuse](docs/reuse-from-embodied-agent.md) |

## Roadmap

| Stage | Scope | Status |
|---|---|---|
| M0–M2 | Contracts, single-drone runtime, constrained agent and evidence loop | Complete in simulation |
| M3 | Local autonomy, perception, localization and degradation handling | Simulation line passed; Jetson-in-the-loop pending |
| P0 | Capability inventory, runtime provenance and product gates | Complete for the software / SITL scope |
| P1 | Projects, sites, virtual docks and the dispatch claim gate | Complete for the software / SITL scope |
| P2 | Durable business workflows | Complete for the software / SITL scope |
| P3 | Multi-site, multi-drone scheduling | Complete for the software / SITL scope (two PX4 SITL aircraft in one world) |
| P4 | Multimodal business loop | Implemented; release gate not passed: S2 precision 0.8835 < 0.90, kept open |
| P5 | Platform v0.1 | Next after P4; includes 72 h system endurance |
| H1–H3 | JIL, bench / restricted flights and field operations | Separate hardware validation track |
| X1–X3 | Air-ground collaboration, vendor hardware and model / kernel research | Conditional extensions |

See the [full roadmap and exit criteria](docs/roadmap.md). Local autonomy is validated in simulation only; cross-robot handoff and DJI / ArduPilot support are outside the current implementation. Airspace admission has an interface definition with a recorded UOM backend and rejects real-mode missions without a filing; energy estimates are for simulation only.

## License

[Apache License 2.0](LICENSE).
