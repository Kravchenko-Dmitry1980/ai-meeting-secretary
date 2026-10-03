# Secretary UI

React 19 + TypeScript 5.9 + Vite 8. Production files in `dist/` are served by the Secretary backend at `http://127.0.0.1:8765`. The normal application starts with the root `scripts/start.ps1`.

Optional frontend development, with Secretary backend already running:

```powershell
Set-Location 'D:\AI\Projects\Active\Secretary\frontend'
npm.cmd ci --ignore-scripts
npm.cmd run dev
```

Vite binds only `127.0.0.1:5173` and proxies `/api` to backend `127.0.0.1:8765`. Node >=22.12 is required; verified here with Node 24.19.0 / npm 11.17.0. Dependencies are local to `frontend/node_modules`; npm cache is `frontend/.cache/npm`.

Checks:

```powershell
npm.cmd run api:types
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test:processing
npm.cmd run build
```

`api:types` reads `../docs/openapi.json` exported from the actual backend and regenerates `src/generated/api.d.ts`. Domain, configuration and main response entities in `src/types/api.ts` use these generated schemas. Re-export OpenAPI and regenerate after backend contract changes; commit/store the generated declaration with source for reproducibility.

The HTTP client uses relative same-origin URLs. CSRF token comes from `/api/v1/session` and exists in memory only; modifying requests use `X-Secretary-Token`. Polza credentials have no browser input or storage and are configured in root `.env`. The UI shows `waiting_config` and empty/partial/error states without demonstration responses.

Meeting content remains text: React text rendering, no `dangerouslySetInnerHTML`, tools, or external actions. Transcript pages are bounded to 50 segments; source links request one exact segment and can switch the player to its actual audio channel. Null timing remains unknown; segment and chunk precision is displayed. Speakers are scoped to their source chunk. Cloud quality, actual cost, timestamps and diarization require live provider tests.

SSE listens for real snapshots, suppresses unchanged work signatures and refreshes artifacts only after a meaningful change. Every selection reloads saved artifacts, including selection of the same meeting; obsolete selection responses are ignored. A 15-second poll runs only while event delivery is disconnected. Transcription continuation sets an explicit retry only for an incomplete meeting with unprocessed chunks; historical failures cannot request a new full transcript version. Export uses the backend's direct download URL and Content-Disposition, without buffering a full export in frontend JavaScript.

Selective owner-authorized donor presentation reuse is recorded in [REUSE.md](REUSE.md). Donor repository has no declared final public license; no open-source license has been invented for its own code.
