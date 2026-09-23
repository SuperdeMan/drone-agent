"""Intake -> compile -> admit, recording the stage that blocked a mission (WP-M2-05/09).

Intake turns an untrusted mapping (a planner draft or a directly constructed spec) into a MissionSpec;
contract validation failures become controlled issues instead of exceptions. The outcome names the
stage that stopped the mission so the adversarial corpus can assert where each attack was caught.

接收 → 编译 → 准入，并记录拦下任务的阶段（WP-M2-05/09）。

接收阶段把不可信的映射（规划草案或直接构造的规格）变成 MissionSpec；契约校验失败变为受控问题而不是
异常。结果写明拦下任务的阶段，便于对抗语料断言每种攻击在哪一层被拦住。
"""

from __future__ import annotations

from pydantic import Field, ValidationError

from drone_agent.admission.admission import AdmissionContext, admit
from drone_agent.admission.compiler import CompileContext, compile_spec
from drone_agent.admission.models import AdmissionResult, CompileResult
from drone_agent.contracts import MissionPackage, MissionSpec
from drone_agent.contracts.common import ContractModel
from drone_agent.runtime.issues import Issue, Severity, issue

STAGES = ("planner", "compiler", "admission", "approval", "onboard")


class PipelineOutcome(ContractModel):
    """Where a mission stopped, why, and the admitted package if it did not stop.

    任务停在哪一阶段、为什么；若未被拦下则附带已准入的任务包。
    """

    blocked_at: str | None = Field(default=None, description="planner|compiler|admission|approval|onboard or None")
    issues: list[Issue] = Field(default_factory=list)
    spec: MissionSpec | None = None
    compile: CompileResult | None = None
    admission: AdmissionResult | None = None

    @property
    def codes(self) -> list[str]:
        return [i.code for i in self.issues]

    @property
    def package(self) -> MissionPackage | None:
        return None if self.blocked_at else (self.compile.package if self.compile else None)


def _classify(error: dict) -> str:
    message, location = str(error.get("msg", "")), [str(part) for part in error.get("loc", ())]
    if "control-level keys" in message:
        return "params.control_key"
    if any(word in message for word in ("cycle", "unknown task", "must be unique", "allow_unverified_from")):
        return "compile.invalid_dag"
    if location and location[0] in ("spatial_scope", "temporal_window", "energy_budget"):
        return "package.boundary_missing" if error.get("type") == "missing" else "compile.params_invalid"
    return "compile.params_invalid"


def intake(data: dict) -> tuple[MissionSpec | None, list[Issue]]:
    """Validate an untrusted mapping into a MissionSpec, mapping every failure to a controlled issue.

    把不可信的映射校验为 MissionSpec，每个失败都映射为受控问题。
    """
    try:
        return MissionSpec.model_validate(data), []
    except ValidationError as error:
        found, seen = [], set()
        for item in error.errors():
            code = _classify(item)
            path = ".".join(str(part) for part in item.get("loc", ()))
            if (code, path) in seen:
                continue
            seen.add((code, path))
            found.append(issue(code, f"{path}: {item.get('msg', '')}"[:300], affected=[path] if path else []))
        return None, found


def evaluate(spec: MissionSpec, compile_ctx: CompileContext, admission_ctx: AdmissionContext) -> PipelineOutcome:
    """Compile then admit; the first stage with an error-severity issue is the blocking stage.

    先编译后准入；第一个出现错误级问题的阶段就是拦截阶段。
    """
    compiled = compile_spec(spec, compile_ctx)
    if not compiled.ok:
        return PipelineOutcome(blocked_at="compiler", issues=compiled.issues, spec=spec, compile=compiled)
    admitted = admit(compiled.package, admission_ctx, spec=spec)
    if not admitted.accepted:
        return PipelineOutcome(blocked_at="admission", issues=admitted.issues, spec=spec, compile=compiled,
                               admission=admitted)
    warnings = [i for i in admitted.issues if i.severity is not Severity.ERROR]
    return PipelineOutcome(blocked_at=None, issues=warnings, spec=spec, compile=compiled, admission=admitted)


def evaluate_mapping(data: dict, compile_ctx: CompileContext, admission_ctx: AdmissionContext) -> PipelineOutcome:
    """Intake, compile and admit an untrusted mapping. / 对不可信映射执行接收、编译与准入。"""
    spec, found = intake(data)
    if spec is None:
        return PipelineOutcome(blocked_at="compiler", issues=found)
    return evaluate(spec, compile_ctx, admission_ctx)
