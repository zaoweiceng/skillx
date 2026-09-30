# SkillX

**Turn agent exploration into reusable, durable workflows.**

[中文](README.zh-CN.md) · [Getting started](#quick-start) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT license](LICENSE)

SkillX is a local-first workspace for AI agents, reusable skills, and visual workflows.
Start with a conversation, let an agent explore a task, and turn repeatable work into
versioned capabilities. A separate worker executes workflows with persisted state,
explicit approvals, and execution traces.

SkillX is in active development. The current UI and most detailed design documents
are in Chinese. It is intended for a trusted, single-user local environment; it is
not a hardened public or multi-tenant service.

## What you can do

- **Work through chat:** manage conversations, attach images for compatible models,
  inspect model requests and responses, and track token usage.
- **Connect models and agents:** configure OpenAI-compatible model services in the
  UI. Pi is the default agent runtime; TraeX and managed adapters are optional.
- **Build visual workflows:** connect nodes, map structured inputs, validate graphs,
  and publish immutable versions with locked dependencies.
- **Reuse capabilities:** combine workflows, portable SkillX packages, LLM nodes,
  agents, and MCP tools behind a common invocation contract.
- **Run and recover:** use a durable SQLite queue, human interaction cards,
  checkpoints, bounded retries, and independent agent recovery.
- **Manage skills:** import and manage skill content and inspect its published versions.

Automatic conversion of an agent trace into a reusable capability is intentionally
limited: only supported cases can be validated and published automatically. Arbitrary
generated code and new side effects still require review. MCP Streamable HTTP support
also has limitations; see [capability status](docs/capability-status.md).

## Quick start

Requires **Python 3.12 or 3.13**, **[uv](https://docs.astral.sh/uv/getting-started/installation/)**,
**Node.js 22.19+**, **npm**, and **Git**. The launcher uses Bash and POSIX process/file
locking; use macOS, Linux, or WSL on Windows.

From your clone of this repository:

```bash
./dev.sh
```

The launcher installs locked dependencies and Chromium, runs database migrations,
loads demo capabilities, and starts the API, worker, and frontend.

- Workspace: <http://127.0.0.1:5173>
- API documentation: <http://127.0.0.1:8000/docs>
- Logs: `.dev/logs/`

Open **Settings → Model services and agents**, add your provider's base URL, API key,
and model identifier, and test the connection. Pi uses this configuration for chat.
Model calls use your provider account and may incur charges. Browsing the UI and
running fixture-based tests do not require a model API key.

Press `Ctrl+C` to stop all services. Restart the launcher after backend changes;
frontend changes use Vite hot reload.

```bash
./dev.sh --setup-only                     # Install and initialize, without starting services
./dev.sh --skip-install                   # Reuse dependencies already installed
API_PORT=8001 FRONTEND_PORT=5174 ./dev.sh  # Override local ports
```

Normal upgrades use incremental migrations and preserve data. Back up the local
database and runtime files before upgrading. `./dev.sh --reset-db` is a destructive
recovery option for incompatible legacy databases: it deletes historical data and
published versions. It is **not** a normal installation or upgrade step.

## Configuration and local data

Defaults work without an environment file. To customize them, copy
[`backend/.env.example`](backend/.env.example) to `backend/.env`.
API and worker must share the same database, artifact root, and credential directory.
The frontend uses `/api` by default; `dev.sh` sets its API target automatically.

| Setting | Purpose / default |
| --- | --- |
| `SKILLX_DATABASE_URL` | SQLite database, `sqlite+aiosqlite:///./skillx.db` |
| `SKILLX_ARTIFACT_ROOT` | Local artifacts, `./artifacts` |
| `SKILLX_CAPABILITY_ROOT` | Trusted SkillX packages, `../capabilities/skillx` |
| `SKILLX_RUNTIME_CREDENTIALS_PATH` | Local provider-key files, `./.runtime-credentials` |
| `SKILLX_AUTO_CREATE_SCHEMA` | Keep `false`; use Alembic migrations |

Relative backend paths are resolved from `backend/` when using the launcher.
Provider keys entered in Settings are stored as restricted local files; database
records retain references. These files are **not encrypted at rest**. Conversations,
images, artifacts, and execution traces can also contain sensitive data. Keep all
runtime data out of Git and inspect logs before sharing them.

Advanced agent, direct-LLM, and recovery configuration is documented in
[runtime adapters](docs/runtime-adapters.md). SkillX packages execute code and are
not sandboxed merely by using a Python virtual environment. Only install trusted
packages and configure trusted tools. The API has no application-level user
authentication; keep the default loopback binding.

## Architecture

```text
React workspace ── REST / SSE ── FastAPI
                                  │
                             SQLite state
                                  │
                           Independent worker
                                  │
                   SkillX / LLM / Agent / MCP / Browser
```

The host validates contracts, permissions, and execution budgets. Published workflow
versions and capability dependencies are immutable. Models participate through
explicit LLM and agent execution paths; approved external side effects remain under
host control.

```text
backend/       FastAPI, domain contracts, orchestration, workers, migrations, tests
frontend/      React, TypeScript, Ant Design, and the visual workflow editor
bridges/pi/    Project-local Pi SDK bridge
capabilities/  Example portable SkillX packages
examples/      Workflow and local integration examples
docs/          Specifications and operating guides
tools/         Development and documentation checks
```

## Development and checks

Initialize dependencies with `./dev.sh --setup-only`, then run from the repository root:

```bash
make quality-check      # Backend checks, migrations, frontend tests/build, documentation
make demo-check         # Adds API + worker black-box integration tests
make browser-ui-check   # Browser UI acceptance tests (requires Chromium)
```

`make browser-install` installs Chromium separately. Optional TraeX checks require an
installed and authenticated real provider: `make traex-real-check` and
`make traex-real-product-check`. They are not evidence of a working provider when
skipped. CI uses local fixtures and does not require your API keys.

See [CONTRIBUTING.md](CONTRIBUTING.md) for individual commands and contribution guidelines.

## Documentation

- [Capability status and implementation limits](docs/capability-status.md)
- [Task lifecycle and human interactions](docs/task-runtime.md)
- [Runtime adapters and recovery](docs/runtime-adapters.md)
- [SkillX package specification](docs/skillx.md)
- [Managed skills](docs/managed-skills.md)
- [Database schema](docs/schema.md)
- [Design](docs/workflow-design-doc.md) and [development guide](docs/workflow-dev-doc.md)
- [Demo acceptance scenarios](docs/demo-acceptance.md)

`STEP/`, `REPLAY/`, and `review/` retain historical design and verification records;
their results describe the versions and dates recorded there, not the current build.

## Contributing and license

Bug reports, documentation improvements, and focused pull requests are welcome.
Follow the [contribution guide](CONTRIBUTING.md) and [code of conduct](CODE_OF_CONDUCT.md).
For vulnerabilities, follow the private reporting instructions in [SECURITY.md](SECURITY.md).

Copyright © 2026 zaoweiceng. SkillX is licensed under the [MIT License](LICENSE).
Third-party dependencies retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
