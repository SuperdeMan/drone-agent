# drone-agent

**Natural-language missions. Deterministic execution. Verifiable outcomes.**

**English** | [中文](README.zh-CN.md)

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![M2 complete in simulation](https://img.shields.io/badge/Milestone-M2%20%7C%20simulation-0F766E)](docs/m2-readiness.md)
[![M3 simulation line passed](https://img.shields.io/badge/M3--SITL-passed-0F766E)](docs/m3-readiness.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

A drone inspection operations platform built on a safety-constrained mission runtime: typed missions, admission and approval, local execution, durable workflows, fleet scheduling, and evidence-based review, work orders and reinspection.

> **Current scope:** P0–P5 have passed their software / SITL gates. M3 local autonomy has passed in simulation; Jetson and field validation remain on the H track. P6 work packages 02–07 have passed within their documented scopes; corridor reservations and explanation check v2 were not adopted. **WP-P6-01 remains open:** public UAV sources have expanded, and owner-authorized independent AI annotation and verification are in progress. Human review timing is deferred to operational validation. Independent asset coverage, verified repeat pairs and the formal evaluation remain pending. See the [P6 handoff](docs/p6-road-corpus-handoff-2026-10-19.md) and [roadmap](docs/roadmap.md) for current counts and exact evidence boundaries.

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

Each linked record owns the full gate results, failures and limitations for the revision shown. These historical results do not validate later commits. P6 public materials are [development evidence](docs/p6-materials-development.md), not a release gate.

| Evidence | Revision |
|---|---|
| [Operations explanation assistant (WP-P6-05, D083)](docs/explain-readiness.md) | `76e7403` |
| [Fine airspace reservations (WP-P6-06, D084)](docs/fine-reservations-readiness.md) | `6544a9a` |
| [Capacity observability and data retention (WP-P6-07, D085)](docs/capacity-readiness.md) | `a63f3ac` |
| [Explanation check v2 (D086)](docs/explain-check-v2-readiness.md) | `7f7b97b` |
| [Asset archive (WP-P6-04, D087)](docs/asset-archive-readiness.md) | `6249129` |
| [Cross-site reference check (D082)](docs/reference-check-readiness.md) | `4398938` |
| [Planner binding (D081)](docs/planner-binding-readiness.md) | `b4a17a3` |
| [Planner clarification (D080)](docs/planner-clarification-readiness.md) | `02694be` |
| [P6 adversarial recordings (D079)](docs/p6-adversarial-readiness.md) | `8d69ae1` |
| [P6 active recapture (D078)](docs/p6-recapture-readiness.md) | `ecee452` |
| [External review hardening (D075, D076)](docs/research/external-review-2026-10-10.md) | `45ba863` |
| [Operations desk visual v2 (D074)](docs/desk-visual-v2-readiness.md) | `0094238` |
| [M1 runtime](docs/m1-readiness.md) | `eefe76e` |
| [M2 release gate](docs/m2-readiness.md) | `f362b9e` |
| [Live planner baseline](docs/verification/m2-baseline-2026-09-23.json) | `f362b9e` |
| [Resident desk with live planning](docs/tailnet-desk-readiness.md) | `74984f9` |
| [M2 review and unified desk](docs/m2-review-2026-09-24.md) | `e8edf28` |
| [M3-SITL release gate](docs/m3-readiness.md) | `75382dc` |
| [P0 release gate](docs/p0-readiness.md) | `de597d0` |
| [P1 release gate](docs/p1-readiness.md) | `b49701b` |
| [P3 release gate](docs/p3-readiness.md) | `73fcf23` |
| [P4 release gate](docs/p4-readiness.md) | `8f41112` |
| [P4 S2 re-verification](docs/p4-readiness.md) | `f4b1b99` |
| [Operations desk workspaces (D068)](docs/desk-workspaces-readiness.md) | `a0d12ff` |
| [P2 release gate](docs/p2-readiness.md) | `3bdbd50` |

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
| Operations design and next work | [Operations architecture](docs/architecture/09-operations.md) · [Implementation tasks](docs/operations-implementation.md) · [P4 validation](docs/p4-readiness.md) · [P5 validation](docs/p5-readiness.md) · [P6 recapture validation](docs/p6-recapture-readiness.md) · [P6 adversarial validation](docs/p6-adversarial-readiness.md) · [P5 plan](docs/p5-implementation.md) · [Operations guide](docs/operations-guide.md) |
| P6 public materials and remaining work | [Handoff](docs/p6-road-corpus-handoff-2026-10-19.md) · [Sources](docs/p6-materials-review.md) · [Whole-road annotation](docs/p6-road-labeling-guide.md) · [Development diagnostic](docs/p6-materials-development.md) |
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
| P4 | Multimodal business loop | Complete for the software / SITL scope; S2 re-verified with MiniMax-M3.1-Flash-Preview (D076) |
| P5 | Platform v0.1 | Complete for the software / SITL scope (vendor docks only against a protocol simulator) |
| P6 | Evidence-driven operations | WP-P6-02–07 complete within their recorded scopes; WP-P6-01 public-data development in progress, formal dataset not frozen; P6 remains open |
| H1–H3 | JIL, bench / restricted flights and field operations | Separate hardware validation track |
| X1–X3 | Air-ground collaboration, vendor hardware and model / kernel research | Conditional extensions |

See the [full roadmap and exit criteria](docs/roadmap.md). Local autonomy is validated in simulation only; cross-robot handoff and DJI / ArduPilot support are outside the current implementation. Airspace admission has an interface definition with a recorded UOM backend and rejects real-mode missions without a filing; energy estimates are for simulation only.

## License

[Apache License 2.0](LICENSE).
