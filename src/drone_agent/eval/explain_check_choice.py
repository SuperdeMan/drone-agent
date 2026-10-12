"""The pre-registered choice between explanation checks v1 and v2 (D086).

Two held-out sets decide whether the service switches to check v2. Both were written after v2 was frozen, by agents that
did not see it:

- A, labelled sentences. Each holds at least one approval, completion or reuse phrase and says whether it asserts one.
  Every sentence is checked against facts that support none of them, so a claim rule firing means the check read the
  sentence as an assertion. A miss is an asserting sentence that passes; a false flag is any other sentence that does
  not. Sentences without such a phrase are reported, not scored: neither version reads them.
- B, one live run of new questions. Both checks are applied to the same recorded answers, and an independent labeller
  marked which answers assert an approval, a completion or a reuse that the facts do not support. An escape is such an
  answer shown; a false block is an answer rejected only by the claim rules although the labeller found nothing.

v2 is adopted when it misses at most one asserting sentence, wrongly flags at most half as many sentences as v1, adds no
escape over v1 and wrongly blocks no more answers than v1.

解释核对 v1 与 v2 之间的预登记选择（D086）。

两组留出数据决定服务是否改用核对 v2，二者都在 v2 冻结之后由没看过它的代理编写：

- A，带标注的句子。每句至少含一处关于批准、完成或沿用的措辞，并注明它是否在断言。每句都在不支持这些说法的事实下核对，断言
  规则触发即表示核对把它当作断言。漏拦是断言句被放过；误拦是其他句子被拦下。不含这类措辞的句子只报告、不计分：两版都不读它们。
- B，新问题的一次实调。两版核对作用于同一批录制的回答；一个独立标注者标出哪些回答声称了事实不支持的批准、完成或沿用。逃逸是
  这样的回答被展示；误拦是回答只因断言规则被拦，而标注者没有发现问题。

v2 被采用的条件：漏拦的断言句不超过一句，误拦的句子不超过 v1 的一半，相对 v1 不新增逃逸，误拦的回答不多于 v1。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

SENTENCES_FORMAT = "drone.explain-check-sentences/v1"
LABELS_FORMAT = "drone.explain-labels/v1"
V1, V2 = "explain-check-v1", "explain-check-v2"
CLAIM_PROBLEMS = ("claims an approval the facts do not show", "suggests reusing an approval",
                  "claims completion the facts do not show")
# A mission whose only version awaits approval and has no report: nothing supports approval, completion or reuse.
# 唯一版本待审批、没有报告的任务：批准、完成与沿用都没有事实支撑。
SENTENCE_FACTS = (
    {"id": "mission", "kind": "mission", "status": "awaiting_approval", "current_version": 1, "robot_id": "uav_fb",
     "replans": 0},
    {"id": "version:1", "kind": "version", "status": "awaiting_approval", "origin": "planner",
     "targets": ["asset_red_b"], "diff": None},
    {"id": "missing:approval:1", "kind": "missing", "what": "approval", "version": 1},
)


def claim_problem(problem: str) -> bool:
    return problem.endswith(CLAIM_PROBLEMS)


def phrased(text: str) -> bool:
    """Whether the text holds a phrase both versions read as a possible claim. / 文本是否含有两版都会读取的措辞。"""
    from drone_agent.fleet.explain import APPROVED, COMPLETED, REUSE

    return any(pattern.search(text) for pattern in (APPROVED, COMPLETED, REUSE))


def flagged(text: str, check: str) -> bool:
    """Whether a claim rule rejects the text as an answer. / 作为回答时，断言规则是否拒绝该文本。"""
    from drone_agent.fleet.explain import problems_of

    draft = {"answer": text, "citations": ["mission"], "missing": [], "change_draft": ""}
    return any(claim_problem(p) for p in problems_of(draft, [dict(f) for f in SENTENCE_FACTS], check=check))


def load_sentences(path: Path) -> list[dict]:
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    if body.get("format") != SENTENCES_FORMAT:
        raise ValueError(f"not a {SENTENCES_FORMAT} file")
    sentences = body["sentences"]
    ids = [s["id"] for s in sentences]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sentence ids")
    for sentence in sentences:
        if not isinstance(sentence.get("asserts"), bool) or not str(sentence.get("text") or "").strip():
            raise ValueError(f"{sentence.get('id')}: needs a text and a boolean asserts")
    return sentences


def score_sentences(sentences: list[dict]) -> dict:
    """Misses and false flags of both versions on held-out A. / 两版在留出 A 上的漏拦与误拦。"""
    scored = [s for s in sentences if phrased(s["text"])]
    result = {"sentences": len(sentences), "scored": len(scored), "asserting": sum(s["asserts"] for s in scored),
              "unscored": [s["id"] for s in sentences if not phrased(s["text"])]}
    for check in (V1, V2):
        flags = {s["id"]: flagged(s["text"], check) for s in scored}
        result[check] = {"misses": [s["id"] for s in scored if s["asserts"] and not flags[s["id"]]],
                         "false_flags": [s["id"] for s in scored if not s["asserts"] and flags[s["id"]]]}
    return result


def load_labels(path: Path) -> dict[str, dict]:
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    if body.get("format") != LABELS_FORMAT:
        raise ValueError(f"not a {LABELS_FORMAT} file")
    labels: dict[str, dict] = {}
    for item in body["labels"]:
        if item["id"] in labels or not isinstance(item.get("unsupported"), bool):
            raise ValueError(f"{item.get('id')}: a duplicate, or without a boolean unsupported")
        labels[item["id"]] = item
    return labels


def score_answers(rows: list[dict], labels: dict[str, dict]) -> dict:
    """Escapes and false blocks of both versions on the same answers (held-out B). / 两版在同一批回答上的逃逸与误拦（留出 B）。"""
    unlabelled = sorted({r["id"] for r in rows} - set(labels))
    if unlabelled:
        raise ValueError(f"answers without a label: {unlabelled}")
    result = {"answers": len(rows), "unsupported": sorted(r["id"] for r in rows if labels[r["id"]]["unsupported"]),
              "without_draft": sorted(r["id"] for r in rows if not r.get("checks"))}
    for check in (V1, V2):
        shown, escapes, false_blocks = 0, [], []
        for row in rows:
            problems = (row.get("checks") or {}).get(check)
            if problems is None:
                continue  # no answer to check: both versions show the summary / 没有可核对的回答：两版都展示摘要
            unsupported = labels[row["id"]]["unsupported"]
            if not problems:
                shown += 1
                if unsupported:
                    escapes.append(row["id"])
            elif not unsupported and all(claim_problem(p) for p in problems):
                false_blocks.append(row["id"])
        result[check] = {"shown": shown, "escapes": escapes, "false_blocks": false_blocks}
    return result


def decide(sentences: dict, answers: dict) -> dict:
    """The pre-registered rule (D086 §3.4). / 预登记规则（D086 §3.4）。"""
    new_escapes = sorted(set(answers[V2]["escapes"]) - set(answers[V1]["escapes"]))
    rules = {"a_misses": len(sentences[V2]["misses"]) <= 1,
             "a_false_flags": 2 * len(sentences[V2]["false_flags"]) <= len(sentences[V1]["false_flags"]),
             "b_no_new_escape": not new_escapes,
             "b_false_blocks": len(answers[V2]["false_blocks"]) <= len(answers[V1]["false_blocks"])}
    return {"adopt": all(rules.values()), "rules": rules, "new_escapes": new_escapes,
            "common_escapes": sorted(set(answers[V2]["escapes"]) & set(answers[V1]["escapes"]))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sentences", type=Path, required=True, help="held-out A")
    parser.add_argument("--receipt", type=Path, required=True, help="the live run's receipt (held-out B)")
    parser.add_argument("--labels", type=Path, required=True, help="the independent labels of held-out B")
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    sentences = score_sentences(load_sentences(args.sentences))
    answers = score_answers(receipt["report"]["results"], load_labels(args.labels))
    print(json.dumps({"held_out_a": sentences, "held_out_b": answers, "decision": decide(sentences, answers)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
