# Why the platform reloads itself, and what it takes to fix

**Status:** **All four tiers are implemented** (2026-09-22), along with a separately reported
artifact-restore bug (§3 cause 8) — see the per-item notes in §4. Tiers 1–3 are verified.
Tier 4 passes every offline gate (373 backend tests, ruff, mypy, 90 infra static tests,
`docker compose config`); its container-level checks are listed in §6 and still want a pass.
**Scope:** the dev-profile React frontend (`frontend/`, Vite on :5150) and the FastAPI
`backend/`. Nothing in the agent tier is involved.
**Merged with `fix/mesh-history-restore`** (2026-09-29), which replaced mesh-specific
restore with generic lazy previews. Where this document says `services/mesh/service.py`,
`enrich_mesh_artifact(s|_dicts)`, `PROMETHEUS_MESH_*`, or the `mesh-cache` volume, read:
`services/previews/` (built on demand per viewer, never eagerly on a turn or a resume),
`PROMETHEUS_PREVIEW_{WORKERS,TIMEOUT_SECONDS,CACHE_DIR}`, and the `preview-cache` volume at
`/var/cache/prometheus/previews`. The subprocess pool of §4.2 survives unchanged in
substance, but now lives in `previews/pool.py` and isolates *every* previewer; a failed
build surfaces as a viewer error with a retry rather than a silent download fallback. A.1's
eager enrichment on read (and its rebind-then-enrich ordering) is gone — there is nothing
slow left on the resume path.
**Originally written against** `planning-improvements`; **reassessed against** `chat_history`
(`fda6c5f`, which adds per-user chat history, resume/rename/delete, and the Figma dashboard
shell).

---

## 0. Reassessment against `chat_history`

The chat-history feature changed the picture substantially — but in the *second* half of the
problem, not the first. The reload itself is untouched and slightly worse; what a reload
*costs* you is much improved.

| # | Cause | Status |
|---|-------|--------------------------|
| 1 | Watcher starvation → HMR socket drop → `location.reload()` | **Fixed.** On arrival at `chat_history` this was unchanged and measurably worse: `vite.config.ts` byte-identical (0-line diff), watched files 9,430 → **9,554**, idle CPU **63.61%** on a container 3 minutes old. Now 378 watched entries and **~0.6%** idle. See §4 Tier 1.1–1.2. |
| 2 | No `optimizeDeps` + nested `import("mermaid")` | **Fixed (it was latent, never firing).** The cache was warm throughout (286 deps, `mermaid` present), so this was never today's symptom; `optimizeDeps.include: ["mermaid"]` stops it recurring after the next cache rebuild. See §4 Tier 1.3. |
| 3 | Explicit HMR full reloads from `src/lib/api/types.ts` | **Fixed — but by a different mechanism than this document first claimed.** The original "HMR propagates up through non-component importers and dead-ends" explanation was wrong: nothing imports `types.ts` at runtime at all. See §3.3 for the real chain (Tailwind's `addWatchFile` → an untransformed module-graph node → `propagateUpdate` bails). The originally prescribed `import.meta.hot.accept()` was implemented, measured, and **found to be a no-op**; the working fix is a Tailwind `@source not` exclusion. |
| 4 | Fast Refresh state wipe with no reload | **Fixed.** `bootstrappedRef` deleted; the run-once guard moved into `chat.ts` as a module-level flag beside the `state` it guards, so a Fast Refresh resets both together. See §4 Tier 3.3. |
| 5 | Zero persistence + unconditional new `conversation_id` | **Fixed.** `chat_history` supplied the server side (see §3.5); the remaining gap — `bootstrap()` minting a fresh conversation on every load — is closed by remembering the open conversation per tab and resuming it. A reload now lands you back in the same conversation with no click at all. See §4 Tier 2.1. |
| 6 | Backend restart wipes the in-process session store | **Fixed at root (Tier 4, stream A).** Session state, logout markers and the SSE event bus all moved into Valkey, so a restart no longer forks the conversation, kills the task subscriber or wipes the artifact catalog. Tiers 2–3 had already made it visible and recoverable; this removes the loss. |
| 7 | Silent SSE death on 401; `Secure` cookie over `http://` | **Fixed, all three.** `chat.ts` passes an `onError` that tells a retrying stream from a dead one and probes `/api/auth/me` before prompting re-login; `App.tsx` retries a network/5xx probe failure instead of reading it as a logout; `settings.cookie_secure` is derived from the browser-facing scheme. See §4 Tier 3. |
| 8 | Resuming a conversation restores the text but **not** the artifact viewers | **New finding (reported 2026-09-22), now fixed.** Three separate defects in the `chat_history` resume path, all in §3 cause 8: persisted mesh artifacts carry `json_body: null` so they degraded to download cards; the client-agent writes each answer twice so every restored transcript showed doubled assistant turns; and the whole artifact catalog was attached to the last assistant turn. Fixed and verified live. |

**Two recommendations were wrong and are corrected below.** First, the prescribed
`import.meta.hot.accept()` on `types.ts` (originally Tier 1.4) does nothing — it was
implemented and measured, and the full reload still fired. The module is never loaded by the
browser, so Vite never transforms it and never sees the accept. §3.3 has the real mechanism and
Tier 1.4 has the fix that does work. Second, tightening
`react-refresh/only-export-components` (originally Tier 1.5) would **not** have caught this
bug. `npm run lint` passes clean on this branch with `--max-warnings 0`, because that rule only
fires on files exporting *both* components and non-components — a module with **zero** component
exports, like `types.ts`, is invisible to it. See §4, Tier 1.5.

Net, as of 2026-09-22: **all four tiers are done.** Tier 1 was the whole of the
reload problem and was entirely configuration — idle CPU fell from ~58–64% to **~0.6%**, a
15-minute soak logged zero reloads, and editing `types.ts` is now a no-op for HMR. Tier 2 makes a
reload land you back in the same conversation. Tier 3 turns three silent failures into visible,
recoverable ones. Tier 4 then removed the structural assumptions the first three tiers had worked
around rather than fixed: session state is in Valkey, VTK runs in a child process, and the
Chainlit scaffolding is gone from the tree.

---

## 1. Summary

*Written in the past tense where a cause is fixed. §0 has the per-cause status; the diagnosis in
§3 is kept as-is because it is the evidence the fixes rest on.*

The page reload was not the application's doing and not a server restart. It was the **Vite dev
client calling `location.reload()`** after its HMR websocket closed uncleanly. That socket kept
dropping because the dev server's file watcher was starving it: `frontend/vite.config.ts` polled
at `interval: 100` with **no `watch.ignored`**, over **9,554 files** in the bind-mounted tree —
**9,150 of which were a leftover `frontend/.venv`** from the deleted Chainlit frontend. That is
~95,000 `stat()` calls per second through Docker Desktop's gRPC-FUSE layer, and it cost **63.61%
CPU at complete idle**. Deleting the venv and supplying an ignore list took that to 378 watched
entries and ~0.6%.

A reload also **destroyed** the conversation, because the transcript lived only in a module-level
`let state` and every boot minted a new `conversation_id`. The `chat_history` branch fixed most of
that — conversations are listed, resumable, renameable and soft-deletable, all ownership-gated
server-side — and the last gap, `bootstrap()` starting a *new* conversation on every load, is
closed by remembering the open conversation per tab and resuming it.

So there were two independent defects, plus a third found while fixing them: a restored
conversation came back without its artifact viewers (§3 cause 8). All three are fixed.

---

## 2. How to confirm you are looking at this

```bash
# 1. The smoking gun: the watcher burning CPU with nobody touching anything.
docker stats --no-stream frontend-dev backend
#   frontend-dev   63.61%   132.9MiB / 7.746GiB      <-- observed, idle, 3 min after restart
#   backend         0.06%   172.2MiB / 7.746GiB
#   (memory climbs with uptime: the same container measured 1.122GiB after 4 days)

# 2. How many files the watcher is actually walking.
find frontend -path frontend/node_modules -prune -o -type f -print | wc -l   # 9554
find frontend/.venv -type f | wc -l                                          # 9150

# 3. Explicit HMR full reloads already in the log.
docker logs frontend-dev 2>&1 | grep -o 'page reload .*' | sort | uniq -c | sort -rn
```

These are the checks that identified the problem, and they are still the right ones for a
regression. For reference, the same commands now report ~0.6% CPU, 378 files and no `page reload`
lines.

**In the browser console**, a starvation reload is preceded by:

```
[vite] server connection lost. Polling for restart...
```

If you see that line, it is cause 1. If instead you see `[vite] hmr update ...` followed by a
reload, it is cause 3. If the transcript goes blank with **nothing** in the console, it is cause
4 — which is not a reload at all.

---

## 3. Root causes, ranked

Ranked by whether they are firing *now*, not by severity. Several are latent or symptom-free and
are easy to chase by mistake; the caveats are called out explicitly.

### Cause 1 — Watcher starvation drops the HMR socket. **Recurring. Needs no human action. This is the root cause.**

`frontend/node_modules/vite/dist/client/client.mjs:552-563` reloads the page on *any* non-clean
websocket close, as soon as the server answers a ping again:

```js
socket2.addEventListener("close", async ({ wasClean }) => {
    if (wasClean) return;
    ...
    if (hasDocument) {
      console.log(`[vite] server connection lost. Polling for restart...`);
      await waitForSuccessfulPing(protocol, hostAndPath);
      location.reload();
    }
});
```

This is unconditional and not configurable. Anything that kills the socket reloads the tab.

`frontend/vite.config.ts:25-28` is what kills it — unchanged on this branch:

```ts
watch: {
  usePolling: true,
  interval: 100,
},
```

There is no `watch.ignored`. Vite's defaults exclude only `.git`, `node_modules`,
`test-results`, the cache dir and the out dir — so everything else in the mount is polled ten
times a second. Measured contents of the watched tree (re-measured on `chat_history`):

| Path | Files | Still needed? |
|------|------:|---------------|
| `.venv` | **9,150** | No — leftover Python venv from the removed Chainlit frontend |
| `tests` | 76 | No — Python tests for the removed frontend |
| `src` | ~180 | **Yes** (grew with the dashboard + chat-history components) |
| `public` | 39 | **Yes** |
| `components` | 22 | No — Chainlit-era Python |
| `.ruff_cache` | 19 | No |
| `plugins` | 18 | No — Chainlit-era Python |
| `utils` | 10 | No |
| `__pycache__`, `.mypy_cache`, `.pytest_cache` | ~14 | No |

≈9,554 files × 10 polls/sec ≈ **95,000 `stat()` calls per second**, each one a round trip through
Docker Desktop's gRPC-FUSE bind mount. `CHOKIDAR_USEPOLLING` at `docker-compose.yml:241`
reinforces the same setting. Under that load the Node event loop stalls periodically; a stall
past the websocket's tolerance closes it, and cause 1 fires.

`.venv` and `.env` are gitignored, so they **survive every branch switch** — including the one
that produced this reassessment. This does not clean itself up.

**A second, independent reload path the `.venv` was feeding.** Vite full-reloads
unconditionally for any changed `.html` file, whether or not it is in the module graph
(`dep-BK3b2jBa.js:63726`, inside the `if (!hmrContext.modules.length)` branch — `.html` is the
one case that reloads with no matching module). The leftover `.venv` contained LiteLLM's bundled
admin UI, i.e. a tree of `.html` files under
`.venv/lib/python3.12/site-packages/litellm/proxy/_experimental/out/`. Deleting the venv proved
it live — the delete alone emitted:

```
page reload .venv/lib/python3.12/site-packages/litellm/proxy/_experimental/out/index.html
page reload .venv/lib/python3.12/site-packages/litellm/proxy/_experimental/out/chat/index.html
page reload .venv/lib/python3.12/site-packages/litellm/proxy/_experimental/out/logging-and-alerts/index.html
```

So any `uv sync`, package reinstall, or branch switch touching those files would have hard-reloaded
every open tab, with no CPU starvation required. Both the deletion and the `watch.ignored` entry
close this off.

### Cause 2 — Missing `optimizeDeps` + a nested `import("mermaid")`. **Latent. Fires once per cold dep cache, and lands mid-conversation.**

`frontend/vite.config.ts` still has no `optimizeDeps` block, and `mermaid` is still reachable
only through a dynamic import *inside* a lazily-loaded component:
`src/lib/chainlit/elements.ts:161` (`lazy(() => import("../../viewers/MermaidElement"))`) →
`src/viewers/MermaidElement.tsx:88` (`await import("mermaid")`). On a cold cache, the first
agent-rendered Mermaid diagram makes Vite discover new dependencies at request time and
full-reload unconditionally (`✨ optimized dependencies changed. reloading`). Because Mermaid
only appears when an agent emits a diagram, that reload lands *in the middle of a conversation* —
the worst possible moment.

**Caveat — do not chase this first.** Re-checked on this branch after the container restart:
`/app/node_modules/.vite/deps/` holds 286 entries and `_metadata.json` lists `mermaid`. The cache
is warm, so this is **not** what is firing today. It recurs after every cache rebuild:
`npm install`, a Vite upgrade, or a fresh anonymous volume. (The host's
`frontend/node_modules/.vite/` is empty and misleading — the cache lives in the container's
anonymous volume. Check it with `docker exec`.)

The codebase already knows about this class of bug: `elements.ts:129-139` and `:153-155` use a
DEV-only top-level `await import(...)` for the IFC and Mesh viewers for exactly this reason.
Mermaid was left out.

### Cause 3 — Explicit HMR full reloads. **Mechanism corrected; now fixed.**

Sixteen in the current dev log, up from twelve:

```
   7 page reload src/lib/api/types.ts
   5 page reload src/components/TabBar.tsx
   4 page reload src/components/dashboard/{SuggestionCard,StatusPanel,nav,HomeWelcome}.tsx
```

`frontend/src/lib/api/types.ts` is a **100% type-only module** — all 31 exports are `type` or
`interface` (was 29; this branch added `ConversationDetailResponse` and friends), and every
importer uses `import type`.

**The original explanation in this document was wrong.** It claimed HMR propagates upward through
the non-component modules that import `types.ts` and dead-ends there. It does not, because
**nothing imports `types.ts` at runtime**: every importer uses `import type`, SWC strips those
entirely, and the transformed output of `client.ts`, `events.ts`, `chat.ts`, `session.ts` and
`artifactAdapter.ts` contains **zero** references to it. Verified by crawling all 62 modules the
dev server actually serves — none mentions `api/types`. The browser never loads this file.

The real chain, verified step by step against Vite 5.4.21:

1. `@tailwindcss/vite` scans every source file for class names and calls Vite's
   `this.addWatchFile()` on each one
   (`node_modules/@tailwindcss/vite/dist/index.mjs`, `f.generate(r, R => this.addWatchFile(R), i)`).
2. Vite's `vite:import-analysis` turns each non-CSS watch-file dependency into a **real
   module-graph node** via `ensureEntryFromUrl`
   (`vite/dist/node/chunks/dep-BK3b2jBa.js:36629`). So a node for `types.ts` exists even though
   no module imports it.
3. Because the browser never requests it, Vite never transforms it, so that node's
   `isSelfAccepting` stays `undefined`.
4. On change, `propagateUpdate` hits its not-yet-analyzed guard and gives up
   (`dep-BK3b2jBa.js:63828`):

   ```js
   if (node.id && node.isSelfAccepting === void 0) {
     debugHmr?.(`[propagate update] stop propagation because not analyzed: ...`)
     return false;
   }
   ```

5. `updateModules` reads that `false` as `needFullReload` and logs
   `page reload src/lib/api/types.ts` (`dep-BK3b2jBa.js:63776`).

Two consequences worth recording. **`import.meta.hot.accept()` cannot fix this** — it was
implemented and measured, and the reload still fired, because the accept only takes effect once
Vite has transformed the module, which never happens. Forcing a transform by hand
(`curl http://localhost:5150/src/lib/api/types.ts`) and then touching the file does produce
`hmr update`, which confirms the mechanism but is not something a browser session ever does. And
the reload only fires **after the app has been loaded once** — on a freshly restarted dev server
with an empty module graph, touching `types.ts` logs nothing at all.

This also means the trigger is *any* source file Tailwind scans that nothing imports at runtime,
i.e. every pure type module. `types.ts` is currently the only one.

**The `dashboard/*` entries are not a boundary defect** — don't "fix" them. Each appears exactly
once, and the burst includes `HomeWelcome.tsx`, a file this branch deleted: it is `git checkout`
churn. I checked `dashboard/nav.tsx` specifically, since it exports a non-component function
(`navLabel`) alongside constants: both of its importers (`LeftPanel.tsx:4`,
`ProjectView.tsx:12`) *are* self-accepting components, so an edit there propagates to them and
stops. **A branch switch while the dev server is running reloads every open tab**, and that is
the whole explanation for those four lines.

Also guaranteed to full-reload or restart the server: any edit to `frontend/vite.config.ts`,
`frontend/.env` (server restart → socket close → cause 1), `tsconfig.json`,
`tsconfig.node.json`, `index.html`, or `src/main.tsx`.

### Cause 4 — A state wipe with **no** reload. **Unchanged. Looks stranger than a reload; produces no console output at all.**

`@vitejs/plugin-react-swc` only HMR-accepts a module if *every* export is a component
(`node_modules/@vitejs/plugin-react-swc/index.js:66-69`). Editing `src/lib/store/chat.ts` or
`src/lib/api/*.ts` invalidates upward to `App.tsx` / `Chat.tsx` / `ProjectView.tsx`, which *do*
accept — so there is no page reload, but `chat.ts` **re-executes** and `state` resets to
`initialState` (`chat.ts:111`).

Meanwhile `App.tsx:25` holds `bootstrappedRef`, which survives Fast Refresh as `true`, so
`bootstrap()` never re-runs and `ready` stays `false`. Result: blank transcript, wedged UI, empty
console, no network activity. The chat-history sidebar does **not** rescue this case — the UI
never reaches a state where you can click anything. If a report says "it lost my chat but didn't
reload," this is it.

### Cause 5 — The amplifier: **largely fixed on this branch.**

This was the reason a reload was fatal rather than annoying. `chat_history` closed most of it,
and closed it better than this document originally proposed — the adopt path went onto the
conversations router rather than into `POST /api/sessions`, which leaves session creation
untouched.

**What landed (backend, `backend/app/routers/conversations.py`):**

| Endpoint | Purpose |
|----------|---------|
| `GET /api/conversations/recent` | user-scoped list, soft-delete aware, `pending` flag from `agent:status:{id}` |
| `GET /api/conversations/{id}` | full transcript + artifact catalog |
| `POST /api/conversations/{id}/resume` | rebinds the session to that conversation **and** returns its transcript in one round trip |
| `PATCH /api/conversations/{id}` | rename |
| `DELETE /api/conversations/{id}` | soft delete |

Every per-conversation route is gated on `ConversationStateClient.owns(user_id, conv_id)`
(`backend/app/conversation_state.py:56-69`, backed by the `user:convs:{user_id}` index) and
returns **404, not 403**, so a non-owner cannot distinguish "doesn't exist" from "not yours".
It fails closed on a Redis error. `backend/tests/test_conversations.py` covers it (+313 lines).

**What landed (frontend):** `chatStore.loadConversation()` (`chat.ts:466`) calls
`api.resumeConversation()` and replays the transcript through a new `itemsFromTranscript()`
(`chat.ts:162`); `ChatHistory` / `ChatHistoryItem` render the sidebar list with rename, delete
and a pending spinner; `historyVersion` on `ChatState` triggers re-fetches. `post()` gained a
staleness guard (`chat.ts:367-377`) so a response landing after the user switched conversations
is not rendered into the wrong one, matching the backend's own `stale` handling in
`services/chat.py`. And `api.getRecentConversations` — flagged as dead code in the original
write-up — is now the sidebar's data source.

**What still does not work.** `bootstrap()` (`chat.ts:340-343`) is unchanged:

```ts
const config = await api.getConfig();
const session = await api.createSession(profile);
```

There is still no `sessionStorage`/`localStorage` persistence of `conversationId`, and
`backend/app/routers/sessions.py:64` still always mints `str(uuid.uuid4())`. So **every page load
starts a new, empty conversation.** After a reload the user sees a blank chat and must find the
previous conversation in the sidebar and click it. That is a large improvement over unrecoverable
loss, and it is still a bad experience: the work is *recoverable*, not *preserved*.

Secondary gap: `resume_conversation` deliberately sets `session.awaited_task_ids = set()`
(`conversations.py`), and `awaitedTaskIds` in `chat.ts:113` is module-level and cleared by both
`reset()` and `loadConversation()`. So a **backgrounded task started before the reload is not
re-subscribed**: `onTaskDone` still drops it (`chat.ts:323-324`). The `pending` badge plus the
8-second poll in `ChatHistory` give visibility that something is still running, and the result
does reach the stored transcript once the agent writes it — so it is recoverable on the next
resume, but it will not stream in live.

### Cause 6 — A backend restart looks identical, with no reload. **Unchanged.**

`backend/app/sessions.py:1-16` says the session store "lives only in this process's memory and is
**lost on restart**." The session cookie survives (static `CHAINLIT_AUTH_SECRET`), so after a
backend restart the user is still "logged in" but:

- `backend/app/routers/chat.py:101-102` silently forks a new conversation.
- The reconnected SSE stream's task subscriber returns immediately at
  `backend/app/services/events.py:301-303` (`if not conversation_id: return`), so
  `task_progress` / `task_done` never arrive again — **with nothing logged**.
- `session.artifacts` and `_revoked_sids` (`backend/app/security.py:293`) are wiped.

`restart: unless-stopped` with no healthcheck hides this completely. The most likely trigger is
OOM: Exodus→VTP conversion runs **in-process** at `backend/app/services/mesh/service.py:54-62`, a
`ThreadPoolExecutor` driving VTK decimation inside the web server.

For the original incident I ruled a restart out — all containers up 4 days with
`RestartCount=0`, still true — but it is the same user-visible symptom. The new resume endpoint
does now give the user a way back.

### Cause 7 — Silent death on session expiry, and a `Secure` cookie over `http://`. **Unchanged.**

`chat.ts:359` still passes no `onError`:

```ts
closeStream = openEventStream({ onStatus, onTaskProgress, onTaskDone });
```

The plumbing has been there all along — `src/lib/api/events.ts:21` declares `onError` and `:58`
registers it when given. `EventSource` auto-retries a transport drop but treats a non-2xx as
**fatal and permanent**, so a 401 from `/api/events` (cookie TTL 86400s,
`backend/app/config.py:120`, enforced at `backend/app/security.py:373-390`) closes the stream
forever. `App.tsx:31-45` probes `/api/auth/me` exactly once with `[]` deps, so the UI happily
stays mounted: events just stop, and the next POST 401s into a generic "Error communicating with
agent" bubble. **There is no 401 → re-login handling anywhere in the client.**

Separately, `App.tsx:39` still treats *any* `me()` failure as `unauthed` and drops to Login:

```ts
.catch(() => setBoot("unauthed"));
```

The dev log contains real blips (`http proxy error: /api/auth/me — getaddrinfo ENOTFOUND
backend`), each of which presents to the user as "it logged me out and lost my chat."

And `backend/app/security.py:362` still sets `secure=not settings.debug` with `debug: false`
(`backend/app/config.yml:28`) while `BACKEND_PUBLIC_URL` is `http://localhost:5150`. Chrome and
Firefox special-case `http://localhost`; **Safari does not**, and any access over
`http://<LAN-host>:5150` has the cookie silently dropped — every `/api` call 401s and the user is
bounced straight to Login.

### Cause 8 — Restoring a conversation restored the text but not the viewers. **New finding; now fixed.**

Reported separately: resuming a past conversation brought back the messages but every artifact
came back as a plain download card instead of the embedded viewer. Three independent defects, all
on the `chat_history` resume path.

**8a. The persisted catalog has no mesh manifest.** The frontend adapter only builds a
`MeshViewer` element when a mesh artifact's `json_body` carries the VTP manifest
(`baseUrl` + `timesteps`); with no manifest it falls through to the download branch. On a live
turn that manifest is produced by `enrich_mesh_artifacts` in `backend/app/services/chat.py:336` —
computed on the fly, **never written back to Redis**. So every mesh artifact in
`artifacts:conv:{id}:by_id` is stored with `json_body: null`, and the detail/resume endpoints
returned it unchanged. The conversion result itself was fine and already cached on disk under
`mesh_cache_root()`; nothing was re-reading it.

**8b. Every assistant turn was duplicated.** The client-agent appends each answer to
`conversation_history` twice — once as `agent_response_<tool>`, once as `final_report`. Live this
is invisible (only the final one is rendered), but `get_transcript` replayed both, so a restored
three-exchange conversation showed six assistant bubbles.

**8c. Every artifact landed on the last turn.** `itemsFromTranscript` in
`frontend/src/lib/store/chat.ts` attached the entire conversation-scoped catalog to the final
assistant item, so a conversation that produced a mesh in turn 1 and a report in turn 3 showed
both under turn 3 and nothing under turn 1.

Note none of the three is a *reload* cause — they were only ever visible once resume existed,
which is why they do not appear in the original write-up. They matter here because Tier 2 makes
resume automatic on every load, at which point a lossy restore would be the new user-visible
symptom.

### Ruled out

So that nobody re-investigates these:

- **Any server-side restart.** All containers up 4 days, `RestartCount=0`. The backend runs plain
  `uvicorn app.main:app` with **no `--reload`** and no source bind mount
  (`backend/Dockerfile:46`, `docker-compose.yml:174-220`) — so agents writing into `shared/`
  cannot restart it.
- **Application code.** The only `window.location.reload()` in the tree is
  `src/components/SetupWizard.tsx:42` (first-run wizard, on a 409 from `/api/setup/bootstrap`).
  No router navigation, no `<meta http-equiv="refresh">`, no service worker.
- **SSE idle timeouts.** Backend heartbeat is `_HEARTBEAT_SECONDS = 15.0`
  (`backend/app/routers/events.py:41`) against `proxy_read_timeout 3600s`
  (`frontend/nginx.conf:21-24`) and `timeout: 0` (`frontend/vite.config.ts:35-40`). Correct on
  both paths.
- **The `ChatHistory` pending poll.** It is an 8-second `setInterval` calling
  `getRecentConversations` (`ChatHistory.tsx:26,53-59`), active only while a row is pending. It
  is a fetch, not a navigation, and cannot reload the page.
- **`<StrictMode>` double-mount**, and codegen or editor hooks touching frontend files
  (`types.ts` is hand-maintained; repo hooks only run clang-format for C++).

---

## 4. Required modifications

Four tiers. Tier 1 alone is expected to remove the reported symptom and touches no application
logic.

### Tier 1 — Configuration only. **Implemented 2026-09-22.** *Low effort, high payoff; this was the whole of the reload problem.*

Measured result: idle `frontend-dev` CPU **58–64% → ~0.6%**, watched files **9,554 → 378**, dev
server cold start 267 ms with no re-optimize pass, a 15-minute soak with **zero** `page reload` /
`server connection lost` / `Pre-transform error` lines, editing `src/lib/api/types.ts` now a
no-op, and editing `src/components/Chat.tsx` still producing a normal `hmr update`.

**1.1 Delete the leftover Python tree from `frontend/`.** ✅ **Done.** This is 96% of the watcher load and is
dead weight from the removed Chainlit frontend. Still present on this branch.

```bash
cd frontend
rm -rf .venv __pycache__ .mypy_cache .ruff_cache .pytest_cache
```

This removed 10,567 of the ~10,700 watched entries in one step. All five paths were confirmed
untracked (`git status --porcelain` clean for them) and are regenerable, so nothing was lost.

`frontend/tests/` (78 entries), `frontend/components/` (26), `frontend/plugins/` (34) and
`frontend/utils/` (12) were **left on disk deliberately**. They are also untracked Chainlit-era
Python — `git ls-files` returns nothing for any of them — but they are small, deleting untracked
files is not reversible, and `watch.ignored` already removes their cost. They are the obvious next
cleanup; `frontend/` is a Node-only tree and none of them is referenced by it.

**1.2 Add `watch.ignored` and back off the interval** in `frontend/vite.config.ts`, so the
problem cannot return the moment someone recreates a venv. ✅ **Done** (`**/dist/**` added as well;
the list Vite receives is *appended* to its own defaults — `.git`, `node_modules`, `test-results`,
`cacheDir` — confirmed at `dep-BK3b2jBa.js:54422-54437`, so nothing is lost by supplying it):

```ts
watch: {
  usePolling: true,
  interval: 1000,
  ignored: [
    "**/.venv/**",
    "**/__pycache__/**",
    "**/.*_cache/**",
    "**/tests/**",
  ],
},
```

Polling was **kept** with `interval: 1000`. At 378 watched files the remaining cost is ~0.6% CPU,
so the incentive to drop it is small, and it is the safe default for host→container event
delivery on Docker Desktop. Still worth trying `usePolling: false` plus removing
`CHOKIDAR_USEPOLLING` (`docker-compose.yml:241`) on a quiet day: if HMR still picks up edits, both
can go and the idle cost reaches zero. Revert if edits stop being noticed.

**1.3 Pre-bundle the heavy viewer deps** so cause 2 cannot recur. ✅ **Done** — added to
`frontend/vite.config.ts`:

```ts
optimizeDeps: {
  include: ["mermaid"],
},
```

Only `mermaid` needed listing. Audited the rest against
`docker exec frontend-dev node -e '...require("/app/node_modules/.vite/deps/_metadata.json")'`:
`@thatopen/components`, `three`, `web-ifc` and the seven `@kitware/vtk.js/*` entry points are all
already discovered, because `elements.ts:129-139,153-155` top-level-awaits their dynamic imports
in DEV. That existing precedent covers them; `mermaid` is the one reached only through a nested
`await import()` inside a `lazy()` component, so it is the one that could be missed on a cold
cache.

**1.4 Exclude `src/lib/api/types.ts` from Tailwind's source scan.** ✅ **Done, but not the way
this document originally prescribed.** *(Corrected — see cause 3 above for the verified
mechanism.)* The fix is two lines in `frontend/src/index.css`, immediately after
`@import "tailwindcss";`:

```css
/* Tailwind scans every source file for class names and calls Vite's addWatchFile()
   on each, which registers a module-graph node for it (vite importAnalysis ->
   ensureEntryFromUrl). api/types.ts is 100% type declarations: it has no class
   names to find, and every importer uses `import type`, so the browser never
   loads it and Vite never transforms it. Its node therefore has
   isSelfAccepting === undefined, and Vite's propagateUpdate bails on a
   not-yet-analyzed module by forcing a full page reload -- which wipes the live
   conversation. Excluding it from the scan removes the node, so editing it is a
   no-op for HMR instead of a reload. See RELOAD.md. */
@source not "./lib/api/types.ts";
```

`@source not` requires Tailwind ≥ 4.1; this tree is on 4.2.2, so it is available. It is scoped to
the one path, and the generated stylesheet is **byte-identical** with and without it (169,635
bytes both ways, `diff` empty) — as expected for a file containing only `type` and `interface`
declarations and therefore no class names.

**What this document originally prescribed here was
`if (import.meta.hot) import.meta.hot.accept();` appended to `types.ts`, and that is a no-op.** It
was implemented and measured before being reverted. Vite only learns a module self-accepts by
*transforming* it and seeing the `import.meta.hot.accept` call; nothing ever imports `types.ts` at
runtime (every importer uses `import type`, which SWC strips — a crawl of all 62 modules the dev
server actually served found zero references to it), so it is never requested, never transformed,
and the accept is never seen. With the accept in place, `touch src/lib/api/types.ts` still logged
`page reload src/lib/api/types.ts`. Forcing a transform by hand first
(`curl http://localhost:5150/src/lib/api/types.ts`) and then touching it logged `hmr update`
instead — which confirms both that the accept would work if the module were ever loaded, and that
in the real app it never is.

The general rule this replaces the old one with: **a source file with no class names and no
runtime importers should not be in Tailwind's scan set**, because being scanned is the only reason
it has a module-graph node at all, and a node that is never transformed is a guaranteed full
reload. The same reasoning applies to any future type-only or constants-only module that other
non-component modules `import type` from.


**1.5 Do *not* rely on the lint rule for this.** *(Corrected from the original write-up.)* The
original recommendation was to raise `react-refresh/only-export-components` to `error` in
`frontend/.eslintrc.cjs:13-16`. That would not have prevented any of this: `npm run lint` already
passes clean on this branch under `--max-warnings 0`, because the rule only fires on files that
export **both** components and non-components. A module with **zero** component exports —
`types.ts`, or `dashboard/nav.tsx` — is invisible to it, and those are exactly the modules that
dead-end HMR. Tightening the rule is still mildly worthwhile hygiene, but it is not
the defence; 1.4 is. It was left unchanged.

### Artifact restore — fixed 2026-09-22. *Prerequisite for Tier 2: there is no point restoring a conversation automatically if restoring it loses the viewers.*

See §3 cause 8 for the diagnosis. Three changes, all shipped:

**A.1 Mesh-enrich the persisted catalog on read.** `backend/app/services/mesh/service.py` grew a
list-based core, `enrich_mesh_artifact_dicts(artifacts)`; the old response-shaped
`enrich_mesh_artifacts(response)` is now a thin wrapper over it (unioning `new_artifacts` +
`artifacts`). `backend/app/routers/conversations.py` splits the old synchronous
`_build_detail`/`_fetch_detail` into a blocking `_fetch_detail_parts` (ownership check + raw Redis
reads, still on a worker thread) and an `async _load_detail` that awaits
`enrich_mesh_artifact_dicts(raw_artifacts)` on the event loop *before* `shape_artifacts`. Both
`GET /api/conversations/{id}` and `POST /{id}/resume` go through it, and `resume` seeds
`session.artifacts` from that same enriched list rather than re-reading Redis — so the content
proxy resolves restored artifacts too. Enrichment is content-hash cached, so a conversation whose
meshes were already converted live costs a cache hit, not a re-conversion.

**A.2 Collapse the duplicated assistant turn.** `backend/app/conversation_state.py`'s
`get_transcript` drops an entry whose `role` *and* `content` both match the entry immediately
before it. The client-agent logs each answer twice — once as `agent_response_<tool>`, once as
`final_report` — which is harmless live (the frontend only renders the final one) but shows up as
a visible duplicate in a replayed transcript. Adjacent-only, so a user legitimately repeating
themselves later in the conversation is untouched.

**A.3 Attach each artifact to the turn that produced it.** New
`frontend/src/lib/store/transcriptArtifacts.ts` exports
`artifactsByAssistantTurn(messages, artifacts)`, which buckets artifacts to assistant turns by
comparing `created_at` against the message timestamps, falling back to the last turn for anything
unattributable. `chat.ts`'s `itemsFromTranscript` uses it instead of dumping the whole catalog on
the final assistant item. Covered by `transcriptArtifacts.test.ts` (5 tests, `node --test`).

### Tier 2 — Make a reload non-destructive. **Implemented 2026-09-22.**

Everything this tier needs on the server side shipped with `chat_history` (§3.5): ownership-gated
`GET /api/conversations/{id}`, `POST /api/conversations/{id}/resume`, and the transcript replay
helper `itemsFromTranscript()` on the client. The remaining work is to use them **automatically**
instead of only on a sidebar click.

**2.1 Persist and restore the open conversation.** ✅ **Done.** New leaf module
`frontend/src/lib/store/openConversation.ts` (`readOpenConversationId` /
`saveOpenConversationId`) wraps **`sessionStorage`** — per-tab, so a second tab still gets its own
conversation, where `localStorage` would make every tab fight over one. Every access is
try/caught: Safari in private browsing and any browser with site data blocked *throw* rather than
returning null, and losing the id must degrade to the old behaviour (a new chat), not break
bootstrap. Covered by `openConversation.test.ts` (5 tests, `node --test`), including the
throwing-storage and no-storage-object cases.

Wiring in `frontend/src/lib/store/chat.ts`:

- The mirror-to-storage call sits inside `set()`, keyed on `"conversationId" in patch`, rather
  than at each of the three call sites (`bootstrap()` / `reset()` / `loadConversation()`) — so a
  future fourth site cannot forget it, and `reset()`'s "New chat" id replaces the old one for
  free.
- `applyResume(conversationId)` was extracted from `loadConversation()`, and `bootstrap()` now
  calls it when a saved id exists. The two paths share one body, so a sidebar click and a reload
  cannot drift apart.
- A **404** from resume is definitive (soft-deleted, not ours, evicted from Valkey): drop the id
  and fall through to `createSession()`. Anything else — network, 5xx — is rethrown to the outer
  catch instead, which **keeps** the saved id for the next attempt. Falling through on a
  transient blip would silently fork a new conversation and abandon the one the user was in,
  which is the exact failure this tier exists to remove.
- Logout goes through the promoted `chatStore.clear()` (was `_clear`, marked "not used by the
  app"), which also forgets the id — so the next user in that tab cannot inherit the previous
  one's conversation.
- The `post()` staleness guard is untouched and still correct: it captures
  `state.conversationId` before its `await`, and `bootstrap()`'s resume only ever *sets* that
  value, so a turn started immediately on load is compared against whatever id was current when
  it was sent.

**2.2 What a reload does to a backgrounded task.** ✅ **Decided: accept it, and say so in the
code.** A task backgrounded before the reload keeps running, but its `task_progress` /
`task_done` will not stream into the resumed session — the ids to subscribe to lived only in the
old in-process `SessionState` and are persisted nowhere. The user is not left guessing: the
history sidebar marks the conversation pending while `agent:status:{id}` is live, and the result
is in the transcript once it lands. The comment recording this is on
`resume_conversation`'s `session.awaited_task_ids = set()`
(`backend/app/routers/conversations.py`).

Restoring live streaming was the alternative, and it is deliberately **not** done here: doing it
properly means `SessionState` in Valkey (Tier 4), not a second ad-hoc persistence path for one
field.

**2.3 Note for whoever does this:** the original write-up proposed adding an optional
`conversation_id` to `POST /api/sessions`. **Don't** — `chat_history` chose the better design by
putting adopt on `POST /api/conversations/{id}/resume`, leaving `sessions.py` as the one place
that mints a conversation. `backend/CONTRACT.md` is already updated for it.

### Tier 3 — Stop failing silently. **Implemented 2026-09-22.**

**3.1 Handle a dead event stream.** ✅ **Done.** `openEventStream`'s `onError` contract changed
from `(err: Event)` to `({ closed, event })`, where `closed` is `source.readyState ===
EventSource.CLOSED` read at dispatch time. That distinction is the whole point: `EventSource`
reports a transport blip it will retry and a fatal non-2xx it will *never* retry as the same
`error` event, and without telling them apart a permanently dead stream is indistinguishable
from an idle one.

`chat.ts` now tracks `connection: "idle" | "open" | "reconnecting" | "lost"` in `ChatState` and,
on `closed`, probes `/api/auth/me` before concluding anything. A 401/403 sets `authLost`; any
other failure appends a recoverable "the server is unreachable" error. Probing first matters —
bouncing someone to Login over a restarting backend is the same bug as 3.2, just from the other
direction.

`App.tsx` watches it through `useAuthLost()`, a deliberately narrow subscription rather than
`useChatState().authLost`: App renders the whole tree and the full state object changes on every
streamed status update. On auth loss it calls `chatStore.suspendForReauth()`, which closes the
stream and clears `ready` but **keeps** the remembered conversation id — so signing back in
resumes the same conversation rather than opening an empty one.

**3.2 Distinguish "logged out" from "network hiccup".** ✅ **Done.** `App.tsx`'s auth probe now
only treats `ApiError` with 401/403 as an answer about identity; anything else is retried on a
`[500, 1500, 3000]` ms schedule before giving up and showing Login. The effect cleans up through
a `cancelled` flag so a StrictMode double-mount or an unmount mid-backoff cannot call
`setBoot` on a dead component. This is what removes the `getaddrinfo ENOTFOUND backend` bounces.

**3.3 Fix the Fast-Refresh wedge (cause 4).** ✅ **Done, by deleting the ref.** The run-once
guard moved into `chat.ts` as a module-level `bootstrapping` flag plus a `state.ready` check.
That is the robust shape because guard and guarded state are now re-initialised *together*: a
Fast Refresh that resets `state` also resets the guard, where a component ref survived and left
`ready === false` forever. `App.tsx` dropped `bootstrappedRef` entirely — both `bootstrap()` and
`connect()` are self-guarding and idempotent, so the effect can just call them. Verified in
passing while doing this work: editing `chat.ts` now logs `hmr update` and the app re-bootstraps
instead of wedging.

**3.4 Gate the cookie's `secure` flag on the actual scheme.** ✅ **Done.** New
`config._cookie_secure(data)` resolves, in order: an explicit `BACKEND_COOKIE_SECURE` /
`cookie-secure` (needed when TLS is terminated by an ingress that doesn't set
`BACKEND_PUBLIC_URL`), then the scheme of `BACKEND_PUBLIC_URL` (`https` → `True`, `http` →
`False`), then the old `not debug` as a last resort when there is nothing to derive from. Exposed
as `settings.cookie_secure` and used by **both** cookie writers — `security.set_session_cookie`
and the OIDC flow cookie in `routers/auth.py` — and the misleading docstring at
`security.py:350-356` is rewritten. Verified in the running container:
`BACKEND_PUBLIC_URL=http://localhost:5150` now yields `cookie_secure = False` where
`not debug` yielded `True`.

Two tests were patching `settings.debug` purely to get a non-`Secure` cookie that `TestClient`
would replay (`tests/test_setup.py`, `tests/test_oidc_login.py`); they now patch
`settings.cookie_secure` directly, which is both what they meant and immune to this coupling
changing again. `tests/test_config.py` gained 11 cases over `_cookie_secure` itself.

### Tier 4 — Remove the single-process assumptions. **Implemented 2026-09-22.**

Tiers 1–3 stopped the reload and made one survivable. Tier 4 is the structural half: the three
places the backend still assumed there was exactly one process, holding exactly one copy of
everything. Three independent streams, implemented C → B → A.

**4.1 Chainlit excision (stream C).** ✅ **Done.** `frontend/vite.config.ts` lost `CHAINLIT_TARGET`
and all twelve proxy entries (only `/api` survives); `@chainlit/react-client` and `recoil` are out
of `package.json`, with the `ChainlitAPI` / `RecoilRoot` / `ChainlitContext` wrappers gone from
`main.tsx`; `lib/chainlit/` is now `lib/elements/`; `DynamicFormElement.tsx` was rewritten without
recoil (it was unreachable in both directions — no producer emits a `DynamicForm` element, and
`askUserState` could never populate because `useChatSession` is never called).

`CHAINLIT_AUTH_SECRET` is gone too, which was the point of doing this stream properly rather than
just deleting the proxy: it had stopped being a Chainlit secret and become a **fallback alias** for
the backend's own cookie-signing key (`config.py`'s `_jwt_secret()`), i.e. exactly the accumulating
cruft the stream exists to remove. `BACKEND_JWT_SECRET` is now the only name for it, chased through
the two fail-fast messages, compose, `.env.example`, the infra `REQUIRED_VARS`, and six docs. The
migration-task docs and `migration.md`'s table keep the old name deliberately — they are records of
what it *was*.

> **Operator step, once, before rebuilding:** the live secret is in `CHAINLIT_AUTH_SECRET` in the
> gitignored `.env`, with `BACKEND_JWT_SECRET` blank. **Copy the value across; do not generate a new
> one.** A new value is harmless but invalidates every outstanding session cookie, forcing one round
> of re-logins. A 401 storm right after the rebuild means the value changed rather than just the name.

**4.2 VTK conversion in a subprocess pool (stream B).** ✅ **Done.** `services/mesh/service.py`'s
2-thread `ThreadPoolExecutor` became a spawn-based `ProcessPoolExecutor`
(`max_tasks_per_child=1`, size from `PROMETHEUS_MESH_WORKERS`, default 2). `_convert_sync` was
already a module-level function with picklable arguments that imported VTK lazily, so the swap was
close to mechanical — and the parent process now never imports VTK at all. A segfault or OOM kill
inside VTK costs exactly one artifact, which degrades to a file download, instead of taking the
interpreter and every live conversation with it.

Three things the pool made necessary, all of which are the interesting part:

- **`BrokenProcessPool` handling.** One killed child poisons the pool for *every* subsequent
  submit, so `_enrich_one` catches it, logs, drops the module-level pool so the next call builds a
  fresh one, and returns `None` — which `enrich_mesh_artifact_dicts` already degraded on
  per-artifact.
- **A conversion timeout** (`PROMETHEUS_MESH_TIMEOUT_SECONDS`, default 120). It can only be enforced
  by tearing the pool down, which may also abort a concurrently running conversion; that collateral
  degrades down the same "no mesh data" path, and is the price of the no-queue design.
- **`shutdown_mesh_executor()`**, called from the lifespan. The old thread pool was created lazily
  and never shut down at all.

The cache moved onto a named `mesh-cache` volume at `/var/cache/prometheus-mesh`
(`PROMETHEUS_MESH_CACHE_DIR`), because container-local `/tmp` meant every restart re-ran
conversions it had already done. The `backend` service also gained `mem_limit`
(`BACKEND_MEM_LIMIT`, default 4g) and a `/healthz` healthcheck — the limit is what turns "a
pathological mesh ate the host" into one OOM-killed child, and the healthcheck is what stops
`restart: unless-stopped` from silently papering over a backend that boots but cannot serve.

**4.3 Session state, event bus and revocation in Valkey (stream A).** ✅ **Done.** This closes cause
6 at the root. The hard part was never serialisation — `SessionState` held only JSON-native fields
plus one `set`. The hard part was that **callers mutated the state object in place**
(`session.awaited_task_ids.add(...)`, `session.consecutive_needs_info += 1`), which a detached
snapshot cannot observe. Per project philosophy, the fix is the root cause rather than a
write-back shim:

- **`SessionState` is `@dataclass(frozen=True)` with `awaited_task_ids: frozenset`.** Every former
  mutation is now an explicit `SessionStore` coroutine, and any call site this migration missed
  raises `FrozenInstanceError`/`AttributeError` instead of silently dropping a write. Silently
  dropping one is the worst available failure here, so it was made impossible rather than unlikely.
- **Two keys per session**, both carrying the cookie TTL and refreshed on every write, so there is
  no reaper: `session:{sid}` (HASH: `conversation_id`, `profile`, `consecutive_needs_info`,
  `artifacts` JSON) and `session:{sid}:awaited` (SET). `get_or_create` writes *nothing* — Redis has
  no empty hash, so "registered but untouched" and "absent" are the same state.
- **`claim_task` is a single `SREM` whose return count is the claim.** The exactly-once guarantee
  for announcing a finished background task used to be a check-then-discard against an in-process
  set, safe only because there was one process. Now exactly one of the subscriber loop, the
  fast-finish guard and the router's reconcile gets `1`. T-5's dedup gets *stronger* under
  `--workers N`, not weaker.
- **`EventBus` is Valkey pub/sub on `sse:{sid}`.** `publish` must **not** also deliver locally or
  local subscribers see every event twice, so every event round-trips through Valkey. `register` is
  async and awaits `SUBSCRIBE`, and is called from the route rather than the stream generator:
  once a `StreamingResponse` is returned the status line is committed, so registering in the route
  turns a Valkey failure into a clean 503 instead of a stream that connects and then never delivers.
  The two listener counts are deliberately distinct — `has_listeners` is `PUBSUB NUMSUB`
  (cluster-wide, for `reconcile_backgrounded`), `local_listener_count` is this worker's queues only
  (per-worker relay and subscriber teardown).
- **Revocation is `revoked:sid:{sid}`** with TTL = the token's remaining lifetime, which deletes the
  manual prune loop outright, and makes `current_claims` an `async def` (FastAPI resolves async
  dependencies natively, so every `Depends(current_claims)` was untouched).
- **Failure directions, each chosen rather than defaulted:** `is_revoked` fails **open** (a Valkey
  blip must not 401 every logged-in user; a logged-out session staying usable until its token
  expires is exactly what the old dict did after any restart). `has_listeners` fails **open**.
  `publish` swallows and warns. Session *reads* do **not** degrade — sessions are core state, so an
  unreachable Valkey is a 503, not a confusingly empty session.

**Alembic had to move for `--workers` to be correct at all** (`docker-entrypoint.sh` is new).
`alembic upgrade head` ran inside the FastAPI lifespan, guarded only by a comment that a
single-replica backend has no migration race — precisely the assumption `--workers N` invalidates,
since uvicorn forks N processes that each import the app and run the lifespan. They would all read
the same `alembic_version`, all emit the same DDL, and the losers would fail with `relation already
exists`; a lifespan exception kills that worker, uvicorn restarts it, and it can lose again. The
entrypoint runs the upgrade once per container, before any worker exists, with `set -e` making it a
gate. `alembic/env.py` additionally takes `pg_advisory_xact_lock` **before**
`context.run_migrations()` — order matters, so a blocked second caller reads the *updated* revision
and correctly finds nothing to do — which covers two containers, or a developer's CLI, starting
together. `_xact_` releases with the transaction, so there is no unlock to leak.

**`BACKEND_WORKERS` stays 1.** Multi-worker is now *valid* and configurable; flipping the default is
a separate operational decision, and `PROMETHEUS_MESH_WORKERS` is per web worker, so raising it
multiplies the VTK child count.

**Tests.** `fakeredis` joined the backend dev dependencies, and `conftest.py` grew the fixtures the
~15 files that each hand-rolled `SessionStore()` should always have shared (`valkey_server`,
`fake_redis`, `session_store`, `event_bus`, and an autouse `valkey_state` that installs them on
`app.state`). Two shape changes rippled through the suite: the mapper takes `(store, sid)` instead
of a live session, so assertions **re-read** the session afterwards — which is the stronger check,
since it proves the write reached Valkey — and pub/sub delivery is asynchronous, so positive
assertions poll and negative ones ("nothing arrived") settle first, or they pass by racing the
relay rather than by the event being filtered out. New coverage: `claim_task` exactly-once under
three concurrent claimants, `EventBus` delivering one copy per queue with two relays attached, and
`tests/test_multi_worker.py`, which builds a second client over the same `fakeredis.FakeServer` to
prove a cross-worker `bump_needs_info`, a cross-worker conversation binding and a logout on one
worker 401-ing on the other. `tests/test_config.py`'s two dead lifespan-migration tests were
replaced by three guards on the new arrangement (nothing migration-related is reachable from the
lifespan; the entrypoint migrates before `exec uvicorn` and fails closed; `env.py` locks before
running).

**Residual gaps — the new known list.**

- **Mesh latency is untouched.** `enrich_mesh_artifact_dicts` still converts sequentially and the
  chat HTTP response still blocks on conversion. Stream B bought crash isolation, not speed; doing
  it properly is an async job with the frontend polling for readiness.
- **The mesh cache is never pruned.** Content-hash keyed and now on a persistent volume, so it grows
  without bound instead of being cleared by restarts. It needs a size cap.
- **`GET /api/artifacts/mesh/{dataset_id}/{filename}` has no per-user ownership check**
  (`routers/artifacts.py`). Any authenticated user who knows a `dataset_id` can fetch it; the id is
  a sha256 prefix and so unguessable, but this is a real gap worth a ticket.
- **`app/identity.py:124`** still has the one pre-existing mypy `no-any-return`.
- Docstring-only "Chainlit" mentions survive across ~20 backend files, and whether
  `frontend/AGENT-CONTRACT.md` and `frontend/public/elements/` should be deleted is unresolved.
- `fakeredis` was installed into `backend/.venv` out-of-band; the backend image needs a rebuild to
  pick up the new dev dependency group (see the note at the end of §6).

---

## 5. The prod-profile workaround (no longer needed, still useful)

This was the recommendation while Tier 1 was unimplemented. Tier 1 is now done, so it is no longer
a workaround for anything — but it remains the right profile for a demo or a long conversation you
cannot afford to have interrupted, so it is kept here rather than deleted.

Run the production profile:

```bash
PROFILE=prod make up     # nginx-served build on FRONTEND_PROD_PORT (8080)
```

There is **no HMR channel at all** in the prod build, so causes 1–4 are structurally impossible:
no watcher, no websocket, no dep-optimizer reload, no Fast Refresh. Code edits and branch switches
cannot touch a live session. The cost is losing hot reload, so it suits demos and long-running
conversations rather than active frontend development.

It never helped with causes 5, 6 or 7 — those are not HMR-related — but 5 and 7 are now fixed in
both profiles, and 6 is at least visible and recoverable.

---

## 6. Verification checklist

Acceptance tests, not a vibe check.

**After Tier 1 — all run on 2026-09-22; measured results recorded inline.**

1. `docker stats --no-stream frontend-dev` — idle CPU falls from ~64% toward single digits.
   **Result: 0.43–0.88% over five samples** (was 58.01–64.4%). Watched entries, counted the same
   way as the original measurement: **9,554 → 378**.
2. `touch frontend/src/lib/api/types.ts` → `docker logs -f frontend-dev` shows **no** `page
   reload`. **Result: pass — and no `hmr update` either**, which is the correct outcome and a
   change from what this checklist originally expected. The module is not in the graph at all any
   more (that is the whole of fix 1.4), so there is nothing to update; a type-only file has no
   runtime effect to propagate. Expect silence, not an `hmr update`.
3. `touch frontend/src/components/Chat.tsx` → still hot-reloads (confirms HMR is alive, not just
   silenced — important if you dropped `usePolling`). **Result: pass —
   `hmr update /src/components/Chat.tsx`.**
4. If `usePolling` was removed: edit a file in `src/`, confirm the browser updates. If it does
   not, restore polling with the ignore list and `interval: 1000`.
5. Warm-cache check:
   `docker exec frontend-dev cat /app/node_modules/.vite/deps/_metadata.json | grep mermaid`
   after a `docker compose down -v` + `up`, *before* opening any diagram — Mermaid must already be
   listed.
6. **One-hour idle soak** with a browser tab open:
   `docker logs frontend-dev 2>&1 | grep -cE 'page reload|server connection lost'` → `0`, and the
   browser console is clean. This is the test that actually proves the reported symptom is gone.
   **Result: a 15-minute soak returned zero matches** for `page reload`, `server connection lost`,
   `optimized dependencies changed`, `restarting`, `Pre-transform error` and `error`. Short of the
   prescribed hour — the full soak is still worth running once under normal use, but the reported
   symptom used to appear well inside 15 minutes at 58% CPU.

**After Tier 2 — implemented; items 7–11 are browser-level and still want a manual pass.**

The code paths are covered by `openConversation.test.ts` (5 tests) and the existing
`test_conversations.py` resume tests, and the resume endpoint was exercised live against a real
conversation (see item 12). What has *not* been driven by hand is the browser half: the list below
is the pass to make.

7. Start a conversation, exchange a few messages, hard-refresh: the transcript is restored
   automatically, the `conversation_id` is unchanged, and
   `docker exec agent-valkey valkey-cli -a "$REDIS_PASSWORD" keys 'conversation:*' | wc -l` shows
   **no new key**.
8. "New chat" still produces a fresh, empty conversation with a new id, and the sidebar list
   updates.
9. Open a second tab: it gets its own conversation, and the first tab is unaffected.
10. Soft-delete the conversation in tab A, then reload tab B which had it open: tab B falls back
    to a fresh session instead of erroring.
11. Per the 2.2 decision (accept): start a long task, refresh mid-flight, and confirm the
    `pending` badge shows it running and the result appears in the transcript once it lands —
    live `task_progress` streaming is *expected* not to resume.

**Artifact restore (fixed 2026-09-22, alongside Tier 1):**

12. Resume a conversation that produced a mesh artifact and confirm the interactive viewer mounts
    rather than a download card. Verified against a real conversation
    (`699e5aa7-…`, 4 artifacts / 1 mesh): `_load_detail` returns `found=True messages=6`, the mesh
    artifact comes back with `render_hint='mesh'`,
    `baseUrl=/api/artifacts/mesh/b95596560c5acfea`, `timesteps=1`, and the VTP cache holds
    `manifest.json`, `meta.json`, `surface_t0000.vtp`.
13. Transcript roles alternate `user`/`assistant` with no doubled answers (the client-agent writes
    each answer twice, as `agent_response_<tool>` then `final_report`; `get_transcript` collapses
    adjacent duplicates). Verified: `user/assistant × 3` for the conversation above.
14. Artifacts attach to the assistant turn that produced them, not all to the last one — covered
    by `frontend/src/lib/store/transcriptArtifacts.test.ts` (5 tests) and exercised end to end by
    the resume above.

**Already covered on `chat_history` — re-run rather than re-write:**

15. `backend/tests/test_conversations.py` covers resume/rename/delete plus cross-user 404. Extend
    it only if you touch `resume`'s `awaited_task_ids` handling (2.2).

**After Tier 3 — implemented; 16 and 17 are browser-level, 18 is partly verified.**

16. `docker stop backend`, interact, `docker start backend`: the UI shows a recoverable error and
    recovers — it does not bounce to Login. (Both halves are in place: the auth probe retries a
    non-401 failure, and a closed stream probes before concluding anything. Not driven by hand.)
17. Expire or delete the session cookie with a tab open: the UI detects the dead stream and
    prompts for re-login instead of going silent — and signing back in returns to the *same*
    conversation, not an empty one (`suspendForReauth` keeps the remembered id).
18. Load the UI in Safari over `http://localhost:5150` and over `http://<LAN-IP>:5150`: login
    persists in both. The server side is verified — the running container reports
    `cookie_secure = False` for `BACKEND_PUBLIC_URL=http://localhost:5150`, where the old
    `not debug` gave `True` — so the cookie is no longer marked `Secure` over plain http. The
    Safari and LAN-origin browser checks themselves are still worth doing.

**After Tier 4 — implemented; the offline gates pass, the container checks are the pass to make.**

Offline, all run 2026-09-22: `backend` **373 passed**, `ruff check app/ tests/` clean, `mypy app/`
down to the one pre-existing `app/identity.py:124`, infra static **90 passed**,
`docker compose config --quiet` exit 0. The rest needs the stack.

19. **Stream C acceptance:** `cd frontend && npm run lint && npm run typecheck && npm run build`,
    and `npm ls recoil @chainlit/react-client` → absent. Then `git grep -i chainlit` returns hits
    **only** under `docs/dev-docs/chainlit-migration-tasks/` and `docs/docs/dev-docs/migration.md`
    (the deliberately preserved history) plus docstring-only mentions. Copy the secret in `.env`
    *before* rebuilding (§4.1), then confirm login works — that is what proves the signing key
    resolved — plus chat and one of each viewer (Mermaid, mesh, IFC, PDF, JSON), per the migration
    README's "preserve viewers" rule.
20. **Stream B, cache:** upload an Exodus file, confirm the mesh renders, and
    `docker exec backend ls /var/cache/prometheus-mesh` shows the dataset. Restart the backend and
    re-open the artifact — it must hit the cache, not reconvert.
21. **Stream B, crash isolation:** `docker exec backend pkill -9 -f spawn_main` mid-conversion. The
    backend must survive, log the `BrokenProcessPool`, degrade that one artifact to a download, and
    convert the *next* one successfully. This is the check that the whole stream exists for.
22. **Stream A, single worker (the test that motivated this document):** start a conversation,
    background a task, `docker compose up -d --no-deps --force-recreate backend` mid-task, and
    confirm the conversation id survives, `task_done` still arrives, and the artifact list is
    intact. `docker exec agent-valkey valkey-cli -a "$REDIS_PASSWORD" keys 'session:*'` shows both
    the hash and the awaited set.
23. **Stream A, revocation crossing the store:** log out in one tab, confirm the other tab 401s.
24. **Stream A, multi-worker:** `BACKEND_WORKERS=2`, rebuild, re-run 22 and 23, plus: two tabs both
    receive status/`task_progress` events (proving pub/sub crosses workers), and a `needs_info`
    follow-up increments correctly when the two turns land on different workers.
25. **Migrations run exactly once:** `docker logs backend` shows one Alembic upgrade, from the
    entrypoint, with no `relation already exists` anywhere — at `BACKEND_WORKERS=2`.
26. **The advisory lock, independently:** with the stack up, run `docker exec backend alembic
    upgrade head` twice concurrently; both must exit 0.

**Build note (standing constraint):** do **not** use `make up` / `make restart` for backend-only
changes — they clear LiteLLM's Postgres volume and drop every agent registration. Use
`PROMETHEUS_API_BASE_IMAGE_HASH=<hash> docker compose build backend` then
`docker compose up -d --no-deps backend`, and never pass `--remove-orphans`. The new `mesh-cache`
volume is the one change that specifically needs the `up -d` on the backend service to attach the
mount. The rebuild is also what installs `fakeredis` into the image (§4.3).

**Always:**

27. `cd backend && uv run pytest && uv run ruff check && uv run mypy app/` — **373 passed** (361 before Tier 4),
    ruff clean, mypy down to the one pre-existing `app/identity.py:124` error (the
    `routers/conversations.py` one is gone: the detail response now builds typed
    `ConversationMessage` objects instead of dicts). Note plain `uv run` re-syncs and fails in
    this checkout; use `uv run --no-sync`. `ruff format --check` reports pre-existing drift in 13
    files, unrelated to this work (14 before it).
28. `cd frontend && npm run lint && npm run typecheck` — both clean, plus `npm run build`
    (16.9 s, only the pre-existing chunk-size warning) and `node --test src/lib/store/` (10/10).
29. `frontend/src/lib/store/chat.ts` still has **no** unit test of its own — it imports React and
    `@/`-aliased modules, so `node --test` cannot load it, and there is no `npm test` script or
    test runner configured. The testable logic was extracted into two dependency-free leaf
    modules instead (`transcriptArtifacts.ts`, `openConversation.ts`), which is where the 10 tests
    live. Giving the store real coverage means adding a runner (Vitest) — worth doing, and out of
    scope here.

**Note on running the suites in this checkout (Tier 4).** Neither `uv run pytest` nor
`make test-static` can resolve dependencies here: both trees pull
`prometheus-api @ git+https://github.inl.gov/genesis/prometheus_api.git@a2a_1.0`, and without
VPN/credentials `uv` fails with `could not read Username for 'https://github.inl.gov'`. Every
result recorded above was therefore produced by invoking the already-populated virtualenvs
directly, bypassing the resolver:

```bash
backend/.venv/bin/python -m pytest -q                            # 373 passed
cd tests && .venv/bin/python -m pytest -m "not integration" -q   # 90 passed
```

Run the infra suite **from `tests/`**, as its Makefile target does. From the repo root, pytest
also collects `agents/*/tests` and reports 7 unrelated `ModuleNotFoundError` collection errors.

Stream A's tests need `fakeredis`, which is now in the backend's dev dependency group but was
installed into the existing venv out-of-band (`uv pip install --python .venv/bin/python
fakeredis`, pulling `fakeredis==2.38.0` + `sortedcontainers==2.4.0`). **The backend image needs
one rebuild on VPN** to pick the group up; until then the tests run only against the local venv.
