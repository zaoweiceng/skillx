# Reusing workflow results without repeating checks

DSH discovers workflow and SkillX capabilities through its native skills provider.
The initial model catalog contains names and descriptions; selecting a skill loads
the public input/output contract, not the workflow graph or dependency source.
Workflow execution can contain SkillX, LLM/agent nodes and nested workflows.
Published dependencies are version locked; nested graphs are expanded and validated
by the workflow compiler rather than loaded into the outer model's context.

A successful invocation can return `executionReceipt` alongside the unchanged
business `output`. The receipt is constructed from persisted execution records:
invocation and run identity, published source version/digest, completion time,
input/output digests, successful node records and exact output-field provenance.
Coverage is emitted only when a final output expression directly references an
actual successful node output with the same value. Node names such as `verify`
do not confer any special trust, and LLM-produced output is not relabeled as an
independent deterministic check.

The caller can reuse covered facts observed by this invocation instead of running
the same checks again merely to obtain those facts. A clean-worktree boolean with
runtime provenance can report the observed status; it does not fabricate command
stdout, exit codes, branch history or content hashes. Missing coverage, failures,
later external state changes or an explicit request for independent verification
still require appropriate checks. The receipt records past execution, not a
guarantee that mutable external state remains unchanged.

Receipts are returned by individual invocation creation/read endpoints, including
completed idempotent reuse. Bulk task/history views remain small. DSH projects only
the receipt fields needed to explain output provenance and preserves its existing
approval, cancellation and unknown-outcome behavior.
