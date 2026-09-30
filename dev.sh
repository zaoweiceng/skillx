#!/usr/bin/env bash
# Local API, Worker and Vite supervisor. Compatible with macOS Bash 3.2.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESET_DB=0
SKIP_INSTALL=0
SETUP_ONLY=0
for argument in "$@"; do
  case "$argument" in
    --reset-db) RESET_DB=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --setup-only) SETUP_ONLY=1 ;;
    -h|--help)
      echo '用法：./dev.sh [--reset-db] [--skip-install] [--setup-only]'
      echo '默认安装缺失依赖、迁移、seed，然后启动 API / Worker / 前端。Ctrl+C 同时停止。'
      echo '--reset-db 永久删除数据库历史数据；--setup-only 只初始化，不启动服务。'
      echo '可设置 API_PORT（8000）、FRONTEND_PORT（5173）。日志位于 .dev/logs。'
      exit 0 ;;
    *) echo "未知参数：$argument" >&2; exit 2 ;;
  esac
done
# Keep the kernel lock across exec and in service children. An orphaned service
# must also prevent a second worker from starting.
if [ "${WORKFLOW_DEV_LOCKED:-}" != 1 ]; then
  command -v python3 >/dev/null || { echo '缺少依赖：python3' >&2; exit 1; }
  exec python3 "$ROOT/tools/dev_lock.py" "$ROOT/.dev" bash "$ROOT/dev.sh" "$@"
fi
unset WORKFLOW_DEV_LOCKED
for executable in uv node npm curl; do
  command -v "$executable" >/dev/null || { echo "缺少依赖：$executable" >&2; exit 1; }
done
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 19)) { console.error("需要 Node.js >= 22.19（Pi SDK）"); process.exit(1) }'
API_PORT="${API_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
for port in "$API_PORT" "$FRONTEND_PORT"; do
  case "$port" in ''|*[!0-9]*) echo '端口必须为整数' >&2; exit 2 ;; esac
done
mkdir -p "$ROOT/.dev/logs"
LOCK="$ROOT/.dev/lock"
pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]:-}"; do
    [ -z "$pid" ] || kill -TERM "$pid" 2>/dev/null || true
  done
  for attempt in 1 2 3 4 5; do
    alive=0
    for pid in "${pids[@]:-}"; do
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    [ "$alive" = 1 ] || break
    sleep 1
  done
  for pid in "${pids[@]:-}"; do
    [ -z "$pid" ] || kill -KILL "$pid" 2>/dev/null || true
    [ -z "$pid" ] || wait "$pid" 2>/dev/null || true
  done
  rm -f "$LOCK/pid"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ "$SKIP_INSTALL" = 0 ]; then
  (cd "$ROOT/backend" && uv sync --frozen --extra dev)
  for package in frontend bridges/pi; do
    marker="$ROOT/.dev/$(echo "$package" | tr / -).lock"
    if [ ! -d "$ROOT/$package/node_modules" ] || ! cmp -s "$ROOT/$package/package-lock.json" "$marker"; then
      if [ "$package" = bridges/pi ]; then
        (cd "$ROOT/$package" && npm ci --ignore-scripts)
      else
        (cd "$ROOT/$package" && npm ci)
      fi
      cp "$ROOT/$package/package-lock.json" "$marker"
    fi
  done
  (cd "$ROOT/backend" && uv run playwright install chromium)
fi
(cd "$ROOT/bridges/pi" && node bridge.mjs --probe) >/dev/null || {
  echo 'Pi SDK 缺失或版本不匹配，请去掉 --skip-install 或运行 make pi-install' >&2
  exit 1
}
PYTHON="$ROOT/backend/.venv/bin/python"
[ -x "$PYTHON" ] || { echo '缺少后端依赖，请去掉 --skip-install' >&2; exit 1; }
[ -f "$ROOT/frontend/node_modules/vite/bin/vite.js" ] || { echo '缺少前端依赖，请去掉 --skip-install' >&2; exit 1; }
# Check ports before touching the database, including on a destructive reset.
"$PYTHON" - "$API_PORT" "$FRONTEND_PORT" <<'PY'
import socket, sys
ports = [int(value) for value in sys.argv[1:]]
if len(set(ports)) != len(ports):
    raise SystemExit('API 和前端端口不能相同')
for port in ports:
    if not 1 <= port <= 65535:
        raise SystemExit('端口必须在 1..65535 范围内')
    with socket.socket() as probe:
        # Match server bind semantics: TIME_WAIT is not a listening service.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'端口 {port} 已占用，请先停止原服务或更换端口')
PY
export SKILLX_AUTO_CREATE_SCHEMA=false
export SKILLX_CORS_ORIGINS="${SKILLX_CORS_ORIGINS:-[\"http://127.0.0.1:$FRONTEND_PORT\",\"http://localhost:$FRONTEND_PORT\"]}"
if [ "$RESET_DB" = 1 ]; then
  (cd "$ROOT/backend" && "$PYTHON" -m app.reset_database --confirm-reset)
else
  if ! (cd "$ROOT/backend" && "$PYTHON" -m alembic upgrade head); then
    echo '旧版数据库无法升级时，可使用 ./dev.sh --reset-db 清空重建。' >&2
    exit 1
  fi
fi
(cd "$ROOT/backend" && "$PYTHON" -m app.seed)
(cd "$ROOT/backend" && "$PYTHON" - <<'PY'
import asyncio
from app.infrastructure.database import SessionLocal
from app.application.runtime_profiles import initialize_profiles
async def initialize():
    async with SessionLocal() as session:
        await initialize_profiles(session)
asyncio.run(initialize())
PY
)
[ "$SETUP_ONLY" = 0 ] || { echo '初始化完成'; exit 0; }
(cd "$ROOT/backend" && exec "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT") > "$ROOT/.dev/logs/api.log" 2>&1 &
pids+=("$!")
(cd "$ROOT/backend" && exec "$PYTHON" -m app.worker --worker-id "worker-local-$$") > "$ROOT/.dev/logs/worker.log" 2>&1 &
pids+=("$!")
(cd "$ROOT/frontend" && export VITE_API_BASE="http://127.0.0.1:$API_PORT/api" && exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort) > "$ROOT/.dev/logs/frontend.log" 2>&1 &
pids+=("$!")
for attempt in {1..60}; do
  for pid in "${pids[@]}"; do
    kill -0 "$pid" 2>/dev/null || { echo '服务启动失败，请查看 .dev/logs' >&2; exit 1; }
  done
  if curl -fsS "http://127.0.0.1:$API_PORT/api/health/ready" >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$FRONTEND_PORT" >/dev/null 2>&1; then
    echo "已启动：http://127.0.0.1:$FRONTEND_PORT  API：http://127.0.0.1:$API_PORT/docs"
    echo 'Ctrl+C 停止全部服务；日志位于 .dev/logs'
    while :; do
      for pid in "${pids[@]}"; do
        kill -0 "$pid" 2>/dev/null || { echo '服务意外退出，请查看 .dev/logs' >&2; exit 1; }
      done
      sleep 1
    done
  fi
  sleep 1
done
echo '启动超时，请查看 .dev/logs' >&2
exit 1
