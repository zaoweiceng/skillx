# Contributing to SkillX

Small, focused contributions are welcome. Issues and pull requests may be written
in English or Chinese. Discuss substantial behavior or architecture changes in an
issue before implementing them.

## Set up

Use Python 3.12–3.13, uv, Node.js 22.19+, and npm. From the repository root:

```bash
./dev.sh --setup-only
./dev.sh --skip-install
```

The setup command installs dependencies, Chromium, and local demo data. See the
[README](README.md) for model configuration. Do not put credentials in source files.

## Make and verify a change

Create a focused branch from `main`. Keep API contracts, migrations, documentation,
and relevant tests consistent. Preserve immutable published versions and existing
execution snapshots; changes to defaults must not silently alter queued work.

Run `make quality-check` before submitting. For targeted iteration:

```bash
cd backend
uv run ruff check app tests real_provider_tests migrations e2e
uv run pytest -q
cd ../frontend
npm run check
npm run lint
npm run test
npm run build
```

Run `make demo-check` for orchestration or integration changes and
`make browser-ui-check` for user-flow changes. Real-provider checks are optional and
must be reported separately from local fixture tests. If changing an API schema,
run `make api-types` and include the generated TypeScript update. Add a new Alembic
migration instead of editing a migration that users may already have applied.

Keep dependency locks in sync. Do not commit database
files, provider keys, `.env` files, generated artifacts, logs, or screenshots with
private data. Before submitting, inspect `git diff --cached` and, with Gitleaks installed:

```bash
gitleaks git . --redact --log-opts="--all"
```

## Pull requests

Explain the problem, the resulting behavior, and the checks actually run. Include
screenshots for visible UI changes when useful. Mention known limitations and any
data migration. Avoid unrelated formatting or refactoring. No particular commit
message prefix is required; describe the change clearly.

By contributing, you agree that your contribution is provided under the project's
[MIT License](LICENSE). You must have the right to contribute the material. Keep
third-party copyright and license notices intact.

Follow the [code of conduct](CODE_OF_CONDUCT.md). Report security vulnerabilities
privately through [SECURITY.md](SECURITY.md), not in a public issue.
