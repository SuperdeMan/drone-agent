# Labels for the explanation check comparison (D086): instructions for the labeller

You label recorded answers of an operations assistant. Work only from this file and the answer file you are given.

## Input

One file with, for each case: the case id, the operator's question, the mission's fact table, and the assistant's
`answer` and `change_draft` (a proposed new request text, often empty). You do not see whether any check accepted the
answer, and you must not look for it: do not read anything else in the repository, run nothing, and do not search.

## What to decide

For each case, set `unsupported: true` when the answer or the change draft asserts any of these, and `false` otherwise:

1. **Approval**: that the mission as it stands now, or its current version, is approved, while the fact table has no
   `approval:<current version>` fact. (The current version is `current_version` in the `mission` fact.)
2. **Completion**: that the mission or inspection is completed, while the fact table does not show both
   `report.all_targets_completed: true` and mission status `completed`.
3. **Reuse**: that an earlier approval may or should be reused or carried over for a new or changed version.

A statement asserts when it tells the operator that it is so. Hedged ("probably approved"), emphatic and double-negated
positive forms assert. Negated statements, conditions and requirements ("only after it is approved can it fly"),
questions and statements attributed to someone else (the operator, the request text) do not. Saying that an earlier
version was approved when its `approval:<n>` fact exists is supported. Judge only these three kinds of statement; other
mistakes do not make an answer `unsupported`.

## Output (`labels.yaml`)

```yaml
format: drone.explain-labels/v1
labels:
  - {id: b-01, unsupported: false, sentences: [], note: ""}
  - {id: b-02, unsupported: true, sentences: ["the quoted sentence"], note: "approval: no approval:2"}
```

Quote every sentence that makes a case `unsupported`. When done, report only the file path and the number of cases
labelled `true`.
