---
name: pj-ser-up
description: Start paired local backend and frontend servers in PJ worktrees using configurable commands, free ports, health checks, and optional isolated database preparation. Use for pj 서버 띄워, 워크트리 서버 켜, or pj-ser-up.
---

# pj-ser-up

Start this pair of backend/frontend checkouts in cmux terminal tabs. Resolve this skill's
real path as `SKILL_DIR`. Read [configuration.md](references/configuration.md) on first setup
or when the configured commands do not match the repository. Use the user's language.

The package owns the helper and configuration; do not edit repository env files, Vite config,
package scripts, or Git flags to allocate ports. No external server-management skill is needed.

1. Use `_shared/data/servers.local.json` next to this skill's parent, or the user's explicit
   `--config` file. Preserve existing choices. On first setup, inspect repository scripts and
   agree on missing commands/DB behavior before filling a copy of `servers.example.json`.
   The example is intentionally inactive until database mode is configured. A DB-backed
   backend requires an idempotent repository-owned isolation command; never silently use a
   shared database or invent a database cloning/deletion command.
2. Confirm cmux is available in this session. Run:

   ```bash
   python3 "$SKILL_DIR/scripts/servers.py" --config "$CONFIG" scan --cwd "$PWD"
   ```

   Optional `--index N` selects a configured pair; otherwise reuse this worktree's pair or
   choose the first free one. Read the JSON root, ports, owners and occupied list. A blocker
   stops startup. Report any already-running sides. `--be-only`/`--fe-only` are workflow
   choices: start only the requested side; FE-only requires this pair's backend to be listening.
3. Existing servers are reused by default. For an explicitly requested restart, inspect the
   listed PIDs and recheck their cwd/port immediately before stopping only this worktree's
   servers with SIGINT. Do not stop an unrelated listener or close server tabs. If restart was
   not already authorized, explain which processes must stop and ask before doing so.
4. Obtain the caller pane from `cmux identify`. Generate each tab's command with:

   ```bash
   python3 "$SKILL_DIR/scripts/servers.py" --config "$CONFIG" command \
     --side backend --root "$ROOT" --be-port "$BE_PORT" --fe-port "$FE_PORT"
   ```

   Pass that exact output as **one quoted `--command` argument** to
   `cmux new-surface --type terminal --pane "$PANE" --working-directory "$BE_DIR"`.
   Retain the returned surface and rename it `BE :<port>`. For frontend, generate with
   `--side frontend --be-surface "$BE_SURFACE"` and use `FE_DIR`; rename it `FE :<port>`.
   If BE was already running, omit `--be-surface`. Create the FE tab immediately: install
   runs in the tab and its helper waits for BE health before starting the frontend.
5. The backend tab performs any required install and DB preparation before starting the
   server. Tell the user if first-time install or DB cloning may take minutes. A failed
   preparation or an invalid isolation report stops startup; never fall back to shared mode.
   The tool does not start infrastructure services or drop databases.
6. Wait for both configured health URLs using `servers.py --config "$CONFIG" wait --url URL
   --surface SURFACE`. Use a background tool process for long waits and keep the user updated.
   Require `"ready": true` for every requested side before reporting success. On failure,
   inspect `cmux read-screen --surface SURFACE --scrollback --lines 80`, report the concrete
   error, and stop; do not repeat the same failed launch. Repository/data fixes need their own scope.
7. Report ports, tab names, FE URL, its configured backend URL, the backend's DB preparation
   report (if used), and how to stop with Ctrl-C in each tab. Additional services such as
   queues/graphs remain shared unless the user's configuration isolates them too. Open a
   browser only when requested, using `cmux browser open` inside cmux.

The configured app must honor its port and API environment variables and refuse automatic
port fallback. The optional preload only handles synchronous CommonJS dotenv loading; use a
repository-supported configuration mechanism for other loaders. Do not claim FE→BE wiring or
database isolation merely because tabs were created: verify the selected settings and health.
