# DSH host permission grants

Workflow effect confirmation accepts two distinct sources: a one-time human
approval, or an existing DSH host permission policy. The latter is recorded as
`decided_by=dsh-host-policy` with an `approval.resolved.authorization` event.
It is never recorded as a human clicking approve.

The plugin reads the host's sandbox policy for the current session before asking
for an approval. `danger-full-access` already permits local shell/process
execution. The host approval policy `never` does not mean that the user rejected
this operation. A rejected native approval is never converted into a grant.

The dedicated loopback endpoint binds host evidence to the invocation's session
key, pending approval snapshot, and recomputed frozen run plan. It validates the
policy digest and server-generated authorization digest. A static run plan grants
only its explicitly listed operations. Pure passthrough input boundaries can be
resolved before execution; capability outputs cannot be invented or pre-executed.
Future dynamic steps receive no permission from this subset grant.

When an input depends on real upstream results, a separate `skillx.invoke`
snapshot freezes that one node's resolved arguments, node attempt, successful
upstream output digests, source definition, dependency locks and runtime. The
endpoint recomputes this context from the database before accepting the host
policy. It grants only the current node attempt. Both paths require pinned,
direct SkillX operations and local declared effects. Unknown, explicitly external,
model-driven and nested capability effects require a separate approval. Published
nested workflows are expanded into ordinary nodes with their version locks.
Cancellation, stale inputs, changed dependency locks or changed upstream outputs
invalidate the corresponding snapshot. No global permission setting changes.

`local.command` has the same trust boundary as the host's native shell. A Python
program or Git hook can have effects beyond local file changes. These grants do
not enforce process network isolation, inspect all command behavior, or prove
that a manifest is truthful. Published package manifests and their pinned code
remain the existing trust boundary. Stronger enforcement requires a separate
sandbox implementation.

Host evidence is a declaration by the existing trusted loopback plugin caller,
not a remotely verifiable signed attestation. The model-facing tool schema does
not accept this evidence. The API's existing local-caller authentication boundary
is unchanged. Events retain session, call, invocation, policy and snapshot digests
so the permission source and its exact scope can be inspected afterwards.
