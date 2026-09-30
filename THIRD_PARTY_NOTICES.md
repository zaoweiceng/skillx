# Third-party notices

The root MIT license applies to original SkillX code and documentation. It does not
replace the licenses of dependencies, downloaded browser binaries, model services,
or optional external agent tools.

Dependency versions are recorded in:

- `backend/uv.lock` — Python backend and development dependencies.
- `frontend/package-lock.json` — frontend and build dependencies.
- `bridges/pi/package-lock.json` — the Pi SDK bridge dependencies.

When redistributing packaged dependencies or generated application bundles,
include the copyright and license notices required by those dependencies. Inspect
the installed distributions' license files for the exact versions shipped; this
file is not a complete transitive dependency license inventory.

Playwright downloads browser binaries separately. Those binaries have their own
notices and distribution terms. TraeX is optional and is not distributed with this
repository. Model providers and external MCP services may have separate usage terms.
