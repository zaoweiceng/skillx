# Security policy

## Supported versions

SkillX is pre-1.0 software. Security fixes target the current `main` branch; there
is no commitment to backport fixes to historical snapshots.

## Report a vulnerability

Use GitHub's **Security → Report a vulnerability** for this repository when private
vulnerability reporting is enabled. Include the affected commit, a minimal
reproduction, impact, and suggested remediation if available. Replace real keys
and private data with placeholders.

If private reporting is unavailable, open an issue asking the maintainer to enable
a private reporting channel, without publishing vulnerability details or an exploit.
Never post credentials, private logs, or customer data in a public issue. There is
no guaranteed response time or paid bounty program.

## Deployment and data boundaries

- The intended environment is trusted, local, and single-user. The API has no user
  authentication layer. Keep it bound to loopback; do not expose it directly to a network.
- SkillX packages and configured tools execute code with the worker's OS privileges.
  Virtual environments and executable allowlists are not OS security sandboxes.
- Provider keys entered in Settings live in restricted local files, not encrypted
  storage. API and worker must share a protected credential directory.
- Traces, conversations, images, browser captures, artifacts, and database backups
  can contain private information even when API keys are redacted. Review them before sharing.
- Side effects with unknown outcomes need human verification before retrying.

## Before publishing a fork or a release

Scan both the checkout and full Git history with Gitleaks or an equivalent tool.
Check author/committer metadata, tags, Git notes, private hostnames, and package
registry URLs separately: secret scanners do not cover every privacy risk.
Do not push local assistant notes, backups, or runtime data. Push the intended branch
explicitly; avoid `git push --mirror` from a working repository.

If a real credential has been committed, revoke or rotate it first, then remove it
from all affected history and coordinate with anyone who has a clone. Rewriting
history alone does not invalidate a credential or remove copies already shared.
