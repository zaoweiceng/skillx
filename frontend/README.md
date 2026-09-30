# SkillX web workspace

React 18, TypeScript, Vite, Ant Design, and React Flow power the chat workspace,
settings, execution traces, and visual workflow editor.

For a complete local stack, run `./dev.sh` from the repository root. See the
[root README](../README.md) for prerequisites and model setup.

To run the frontend separately:

```bash
npm ci
npm run dev
```

The backend must be running at the proxy target configured in `vite.config.ts`.
Set `VITE_API_BASE` to override it; see `.env.example`.

```bash
npm run check
npm run lint
npm run test
npm run build
```

API types are generated from FastAPI's OpenAPI schema. Run `make api-types` from
the repository root after changing backend contracts.
