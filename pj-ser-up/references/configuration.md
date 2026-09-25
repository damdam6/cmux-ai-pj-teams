# Server configuration

Copy `_shared/data/servers.example.json` to `servers.local.json` on first setup, or pass an
external JSON file with `--config`. There is no automatic merge or fallback to the example.
Paths to this config and the package may contain spaces. Keep credentials in the repository's
existing environment mechanism; local config files are excluded from distribution.

| Setting | Required choice / behavior |
|---|---|
| `backend.directory`, `frontend.directory` | Relative Git checkout/worktree roots below the common parent. Defaults `backend` / `frontend`; path escape and duplicate roots are refused |
| `command` | Nonempty argv array for the repository's server command. Shell operators are literal; use a maintained repository script when needed |
| `install`, `installMarker` | Optional argv and relative marker. Install runs in the new server tab only when the marker (example `node_modules`) is absent |
| `env` | Explicit string values for app port/API/callback settings. The app must honor them; frontend command must refuse port fallback (Vite: `--strictPort`) |
| `preserveEnv` | Optional list of keys from `env` to preserve across synchronous CommonJS loads via the bundled Node preload. Default empty; not a general env lock |
| `healthPath` | Local HTTP path expected to return 200. Backend example `/health`, frontend `/`; change to the app's actual endpoint |
| `ports.backend`, `ports.frontend`, `ports.count` | Start of two non-overlapping consecutive ranges and count. Example pair N is `5000+N` / `3100+N`, N=0..9 |
| `requiredPorts` | Local service ports that must already listen, e.g. PostgreSQL/Redis. Default empty; this verifies listening only, not service identity |
| `database.mode` | Explicit `none` for a backend without a DB; `isolated` for a DB-backed backend. The example's `configure` value blocks startup |
| `database.prepare` | Required argv for `isolated`. Runs in backend cwd with its env, after installation, on every backend start |
| `timeout` | 1..3600 seconds for health and DB preparation; default 900 |

Command/env/DB argument strings support `{ROOT}`, `{BE_DIR}`, `{FE_DIR}`, `{BE_PORT}`,
`{FE_PORT}`, `{BE_URL}`, `{FE_URL}`. Values are passed as literal subprocess arguments and
environment strings; no shell evaluation. The app may need additional callback/CORS settings.

`database.prepare` must be an existing, trusted, idempotent adapter that verifies or creates
this branch's isolated DB **and configures the backend to use it**. It must fail on partial
state or wrong ownership, and must not delete DBs or switch to shared mode. Progress belongs
on stderr; stdout must be one JSON object, for example:

```json
{"mode": "isolated", "database": "branch_example"}
```

PJ validates this contract; it does not independently inspect database contents, migrations,
or routing. Use an adapter matching your DB tooling. There is no bundled PostgreSQL container
name, source DB, framework dependency, or project-specific setup command. Other infrastructure
isolation is outside this adapter's guarantee.

Requires Python 3.10+, Git, `lsof`, cmux for tabs, and the package manager/server tools named
in config. Node.js is needed only for a Node app or the optional CommonJS preload. `scan` and
`command` inspect without starting servers; `run` is the tab command and executes configured
install/DB/server commands. Port selection is a live snapshot, not a reservation: it is checked
again before launch, and the app must fail if another process wins the final bind race.
