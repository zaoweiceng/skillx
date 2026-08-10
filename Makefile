.PHONY: demo-check quality-check backend-check browser-install browser-check frontend-check migration-check e2e-check browser-ui-check

UV_CACHE_DIR ?= /tmp/skillx-workflow-uv-cache
export UV_CACHE_DIR

# 完整验收：质量门禁 + docs/demo-acceptance.md 定义的真实黑盒 E2E。
demo-check: quality-check e2e-check

# 较快的静态/单元/进程内集成检查，不含黑盒子进程 E2E。
quality-check: backend-check migration-check frontend-check

backend-check:
	cd backend && uv run ruff check app tests migrations e2e ../examples/local_weather_mcp.py ../examples/traex_stub.py ../examples/openai_agent_sidecar.py ../examples/local_form_site.py
	cd backend && uv run pytest -q

browser-install:
	cd backend && uv run playwright install chromium

browser-check:
	@cd backend && location=$$(uv run playwright install --dry-run chromium | \
		sed -n 's/^  Install location: *//p' | head -1) && \
		test -n "$$location" && \
		find "$$location" -type f -perm -111 -print -quit | grep -q . || \
		(printf '%s\n' 'Chromium 缺失，请先运行 make browser-install' >&2; exit 1)

migration-check:
	cd backend && DB_PATH=$$(mktemp -u /tmp/skillx-migration.XXXXXX) && \
		SKILLX_DATABASE_URL=sqlite+aiosqlite:///$$DB_PATH uv run alembic upgrade head

# 真实 API + 独立 Worker 子进程黑盒 E2E。浏览器安装独立执行，避免每次门禁重复下载。
e2e-check: browser-check
	cd backend && uv run pytest e2e -q -p no:cacheprovider

browser-ui-check: browser-check
	cd backend && uv run ruff check browser_e2e
	cd backend && uv run pytest browser_e2e -q -p no:cacheprovider

frontend-check:
	cd frontend && npm run check
	cd frontend && npm run lint
	cd frontend && npm run test
	cd frontend && npm run build
