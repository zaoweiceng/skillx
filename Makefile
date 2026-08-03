.PHONY: demo-check backend-check browser-install frontend-check migration-check

demo-check: backend-check migration-check frontend-check

backend-check: browser-install
	cd backend && uv run ruff check app tests migrations ../examples/local_weather_mcp.py
	cd backend && uv run pytest -q

browser-install:
	cd backend && uv run playwright install chromium

migration-check:
	cd backend && DB_PATH=$$(mktemp -u /tmp/skillx-migration.XXXXXX) && \
		SKILLX_DATABASE_URL=sqlite+aiosqlite:///$$DB_PATH uv run alembic upgrade head

frontend-check:
	cd frontend && npm run check
	cd frontend && npm run lint
	cd frontend && npm run test
	cd frontend && npm run build
