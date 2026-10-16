# T9 isolated Team UI browser probe

This is a disposable synthetic fixture, not a production launcher. It uses the
real Team database, authentication, public Gateway, command service and worker.
Vikunja is an exact `httpx.MockTransport` HTTP simulator; there is no provider,
MAX, Polza, microphone, owner database, or owner configuration access.

## Prepare and start

Build the Team entry using the normal frontend build first. The probe expects
`frontend/dist/team.html` and the relative assets produced by the Vite multi-entry
build. It never builds or rewrites production files itself.

The current production entry also includes the remote MAX SDK script. The
fixture deliberately rejects external asset references, so the default start
command is unsuitable for that entry. For an offline browser check, prepare a
new disposable copy that removes only that SDK tag and retains the local
JS/CSS bytes. Pass that copy explicitly:

```powershell
.venv\Scripts\python.exe -B scripts\team\probe_team_ui.py start --frontend-dist <prepared-dist>
```

The 2026-10-04 prepared copy is recorded in
`.runtime/team-rollout/t9-browser-acceptance-316454ceb8734eddb67dbafad7f7c3af/prepared-dist-evidence.json`.
Its scope is `OFFLINE_UI_WITHOUT_MAX_SDK`. A later frontend rebuild requires a
new prepared copy and asset comparison. This does not validate MAX WebView or
the SDK itself. An entry containing only local relative assets can use the
default `frontend/dist` argument.

The default certificate generator is the already installed
`C:\Program Files\Git\usr\bin\openssl.exe`. `--openssl` accepts an explicit local
executable; missing tooling is an error. Nothing is installed or added to PATH.
The launcher creates `.runtime/team-rollout/t9-ui-<uuid>` and starts one hidden
owned Python child. It returns JSON containing `run_root`, `fixture_url`,
`team_url`, PID, process creation time, and the certificate SHA-256 fingerprint.
No login code, cookie, auth secret or bot token is included in that JSON.

The server holds one prebound `127.0.0.1:0` socket, so the OS chooses a free
port without a pick-close-bind race. Its origin is
`https://secretary-t9.localhost:<port>`. The certificate SAN contains that host
and `localhost`. Readiness connects explicitly to loopback and verifies the
scratch certificate, TLS hostname, Host, and unique run ID. No hosts-file, DNS,
trust-store, firewall or proxy setting is modified.

Use an isolated browser session. A self-signed certificate may produce a browser
interstitial: proceed only for this known local fixture after checking the
reported host/fingerprint. Do not weaken browser verification globally. This
local TLS check does not qualify public HTTPS, mobile MAX or WebView cookies.

## Synthetic login and scenarios

Open `fixture_url` (`/fixture`) to see the explicit synthetic label and the three
owners' one-time desktop codes. Each code has the actual production five-minute
TTL and one-use policy. A fixture-only form issues a fresh code for a selected
synthetic owner with an exact Origin check. Copy a code into the unchanged Team
UI login form at `team_url` (`/team/`). Codes remain in process memory; the fixture
page intentionally displays only these disposable credentials.

The first two owners have both projects. The third owner has only the second
project, whose ID exceeds JavaScript's safe integer range. Sixty-five tasks cover
all seven states, overdue and same-day dates, important/urgent combinations,
unclassified/no-due tasks, long titles, inert HTML-like text and large task IDs.
The first project has more than fifty tasks to exercise pagination.

The real `TeamReadRepository`, schema 6 `TeamSyncRepository`, and
`TeamSyncService` are injected. Initial full scans of both projects genuinely
set successful sync metadata; unchanged unclassified tasks remain valid. The
cloud status is honestly `not_configured` because no budget/provider is wired.
The fixture page also offers three explicit simulator-only controls: change the
first task's title outside the UI, set an unconfirmed external due date, and
restore its confirmed date. Each runs the real sync service against the changed
HTTP facts. External history has a local observation timestamp and no invented
remote author or occurrence time. A foreign due date produces `degraded` and
preserves both the confirmed projection and the last successful sync timestamp.

Command acceptance is genuinely `queued`. The actual Team worker invokes the
actual Vikunja adapter against the synthetic HTTP server. The command service
verifies each remote step and final GET before saving `applied`. The fixture does
not insert a fabricated applied receipt or directly edit the canonical projection
to simulate a successful user command. This validates the UI/application seam;
it does not qualify a native Vikunja deployment.

Only `/fixture`, `/fixture/status`, `/team/`, `/team/team.html`, `/team/assets/*`,
and the production Gateway's allowed API paths are served. There is no fallback
to Secretary's entry or API, no directory listing, and no raw database or config
file route. Symlink/junction assets are rejected. The production secure cookie,
exact Host/Origin checks and CSRF behavior are unchanged.
The asset allowlist is the Team entry's ESM/CSS dependency closure, including
required shared chunks and excluding the unused Secretary entry bundle. Rebuilds
require stopping the fixture and starting a fresh run before browser checks.

## Status and stop

Use the exact `run_root` returned by start:

```powershell
.venv\Scripts\python.exe -B scripts\team\probe_team_ui.py status --run-root <run_root>
.venv\Scripts\python.exe -B scripts\team\probe_team_ui.py stop --run-root <run_root>
```

Status is read-only and never creates a database. Stop validates the generated
directory, manifest, PID, exact process creation time and owned command line,
then writes a bounded stop request only to that run. The child stops itself,
finishes its owned command worker, closes clients/socket, and records `stopped`.
Uvicorn's connection/request drain has a five-second timeout; unfinished request
tasks are cancelled. This timeout does not bound every application lifespan or
worker cleanup operation. `stop_requested` is an acknowledgement, not proof
that the process has exited: verify the terminal manifest and owned child exit.
No process is killed by PID, process name or port. A one-hour watchdog also stops
an abandoned fixture. Startup/readiness failures request cooperative shutdown.
Generated data and certificates remain in that unique ignored directory as
evidence; the script never recursively deletes them.

## Offline checks

```powershell
.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests\test_team_ui_probe.py -q --tb=short
```

These checks use disposable SQLite, ASGI TestClient and two real Uvicorn shutdown
drain regressions with controlled connection/request state. They do not start TLS,
OpenSSL, a native server or a browser. The actual local TLS lifecycle/browser
probe is separate and must report its own result. A passing synthetic browser
probe must not be reported as real MAX login, public TLS, real task-provider,
cloud-provider or owner-data validation.

## Recorded preparation evidence (2026-10-04)

- Owned fixture suite: **20 passed**, 29.43 seconds, through the offline runner.
  The single warning is Starlette's existing TestClient/httpx deprecation.
- Independent path/closure probes: **2 passed**, 0.71 seconds, in
  `.runtime/team-rollout/review-t9-fixture/test_closure_boundaries.py`.
- Actual TLS lifecycle/auth probe with deliberately tiny synthetic HTML:
  `t9-ui-862d05e60c9b4296bc7535bc3106e41d`. Verified the scratch certificate and
  hostname, genuine desktop-code login, Secure cookie, first task page of 50,
  static response, and cooperative stop. Final state: `stopped`.
- First actual built Team entry: `t9-ui-fce2f430051741c28e0de12563fe6abd` reached
  TLS readiness with three allowed Team/shared assets and genuine initial sync.
  It was cooperatively stopped before root's final frontend rebuild; manifest
  confirms `stopped`. No browser acceptance is claimed for this preparation run.
- After the final sync-service changes, the three affected fixture sync tests
  passed again (10.49 seconds, 17 deselected).
- The intermediate rebuilt run `t9-ui-e5c01800d69b45bbb7caf209ec7d8e8d` was also
  cooperatively stopped before browser navigation when the accepted frontend
  store fix required rebuilding.
- The accepted frontend build was handed to root for browser checks in fresh
  run `t9-ui-b20283a3fe5b42829465533552d6463c`, at
  `https://secretary-t9.localhost:59901/team/`. Its preparation manifest records
  actual TLS readiness, PID 4416 and process creation time 1791070690.9534252.
  Certificate SHA-256:
  `17b7bdef63b954b2d98d1894f35cea9dad47b135bf951ca39e60ebe121cc5c76`.
  Root owns subsequent browser evidence and cooperative shutdown of this run;
  consult its live manifest rather than treating this historical ready record
  as current process status.

At that preparation handoff, the earlier preparation runs were stopped and the
accepted-build fixture was left for root, subject to its watchdog. This is a
historical handoff record, not a current process inventory.

## Loopback HTTP fallback when the local certificate blocks Chrome

Use `scripts/team/probe_team_ui_http.py` only for the same synthetic UI fixture
when the self-signed HTTPS interstitial cannot be passed. Start it with:

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/team/probe_team_ui_http.py start --frontend-dist frontend/dist
```

The command prints a fresh `/fixture` URL, `/team/` URL, run root, and prepared
asset-copy evidence. It binds only to `127.0.0.1`, accepts only the exact
`secretary-t9.localhost:<port>` Host and Origin, rejects forwarded headers and
non-loopback peers, blocks `/hooks/max`, and stops itself after 30 minutes.
The preparation copy removes only the exact MAX SDK script tag; the source
`frontend/dist` is unchanged. The wrapper maps that exact local HTTP Origin to
the HTTPS Origin expected by the real Team Gateway. The issued cookie remains
`Secure; HttpOnly; SameSite=Lax`.

Open the printed `/fixture` URL and follow its link to Team. Use only the three
synthetic one-time codes displayed there. If Chrome does not retain/send the
Secure cookie on this localhost origin and login fails, stop the fixture; do
not retry with an insecure cookie. Stop it cooperatively using the exact `run_root`
printed by `start`:

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/team/probe_team_ui_http.py stop --run-root '<run_root from start output>'
```

This HTTP fallback is explicitly `HTTP_SYNTHETIC_UI_ONLY`: it does not qualify
TLS, public HTTPS, browser security behavior, MAX WebView, a real MAX account,
or mobile devices. It makes no Polza/MAX calls and uses only a local simulated
Vikunja transport and throwaway fixture database. The production Gateway and
cookie settings are not changed.

## Latest shutdown and browser result (2026-10-04)

The two drain regressions failed before the fix and passed after it. Final
affected suite: **22 passed, 70.16 seconds, exit 0**, one existing warning.
A new actual TLS fixture stopped cooperatively in **6.469 seconds** with a
verified TLS socket held open through its child exit; terminal manifest was
`stopped`. The previously hung owned child required separately reviewed forced
HANDLE cleanup; its original stale `ready` manifest was preserved.

Browser scenarios remain **NOT_RUN**: Chrome's certificate interstitial could
not be passed through the human handoff. Both own test servers are stopped;
their old URLs are not usable. No screenshot, MAX SDK/WebView, phone, public
TLS or production acceptance is claimed. See
[the current diagnostic report](TEAM_DIAGNOSTICS_2026-10-04.md).
