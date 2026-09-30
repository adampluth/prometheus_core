# Tasks — Rewire `frontend/` onto the backend contract

The **follow-up** anticipated by [`tasks/README.md`](../README.md): migrate the React app
(`frontend/`) off `@chainlit/react-client` + `recoil` and onto the standalone backend's JSON/SSE
`/api` contract ([`backend/CONTRACT.md`](../../backend/CONTRACT.md), machine def
`backend/app/contract.py`).

> **Note (branch `old-frontend-removal`):** the destructive part happened **out of order** — the
> old Chainlit `frontend/` app and the `chainlit-frontend` service are already **removed**, and
> `frontend-react/` was renamed to `frontend/`. What remains is the *code-level* Chainlit/recoil
> cleanup inside the React app (Step 09 below). The runtime already talks to `/api` over SSE; the
> Chainlit client just hasn't been fully excised yet.

## Progress (2026-08-19)

**Steps 00–09 all done.** The app authenticates, bootstraps, streams SSE, chats against
`/api`, uploads files, routes artifacts to viewers, and lists/resumes conversation history —
with **no Chainlit anything in the tree**: not in the data path, not in `package.json`, not in
the Vite proxy. Gates: `tsc --noEmit` + `eslint` + `vite build` clean. **Not yet validated live**
(`make up` needs Docker/VPN) — verified by typecheck + eslint + build + the Python suites only.

**Done — 00–04 (text-chat vertical slice):**

- `src/lib/api/{types,client,events}.ts` — contract mirror, typed `/api` fetch wrappers,
  SSE `EventSource` wrapper (`withCredentials`).
- `src/lib/store/chat.ts` — `useSyncExternalStore` store (`ChatItem` model): reduces
  `ChatMessageResponse` + SSE events into items; owns bootstrap/connect/sendTurn/
  submitFollowup/decidePlan/reset/stop; auto-approve auto-executes.
- `src/components/chat/FollowupForm.tsx` — `needs_info` form from `FormField[]`.
- `App.tsx` (auth via `/api/auth/me|logout`, bootstrap + connect), `Login.tsx` (JSON
  login, Okta hidden), `Chat.tsx` (store-driven; renders user/assistant/tool/needs_info/
  plan/error; uploads via `/api/uploads`), `ProjectView.tsx` (new chat via `chatStore.reset`),
  `vite.config.ts` (added `/api` proxy → `VITE_BACKEND_URL`, default `:8600`).

**Done — 05 (artifact adapter):**

- `src/lib/chainlit/artifactAdapter.ts` — `artifactToElement(ArtifactMeta) → AnyElement`
  keyed on `render_hint`; `Chat.tsx` feeds `artifactsToElements(item.artifacts)` to
  `ElementList` (replaced the placeholder `ArtifactLinks`). Every element is stamped with
  `props.artifact_id` so `dedupeElements` keys on the unique id (custom viewers otherwise
  collapse by component name).
- `src/lib/chainlit/elements.ts` — **`AnyElement` decoupled from Chainlit's `IElement`**
  (standalone interface; type-only import dropped early); added `report`/`reference`
  `ElementKind`s + classifier cases + registry entries; added `image/gif`+`image/webp` to
  the image set (note A-8).
- `src/viewers/ReportElement.tsx` (new — inline markdown, fetches body from the content
  proxy when not inline), `src/viewers/ReferenceElement.tsx` (new — citation card from
  `ArtifactMeta.citation`).
- `src/viewers/ImageViewerElement.tsx` + `PdfViewerElement.tsx` — repointed to
  `element.url` (the content proxy); **dropped `ChainlitContext`/`sessionState`/
  `getElementUrl`**.
- `src/components/Markdown.tsx` (new — extracted the shared GFM renderer so `Chat.tsx`
  and `ReportElement` render markdown identically).

  Decisions worth noting: **mesh** degrades to a download unless `json_body` carries a VTP
  manifest — the backend has no VTK→VTP pipeline (restoring interactive mesh is a
  backend follow-up); **mermaid** is best-effort from `json_body` (no agent emits it; the
  diagram text isn't a wire field). **ifc** works (ThatOpen loads the raw `.ifc` from the
  content proxy).

**Done — 07 (uploads):** `src/lib/api/client.ts:94` `uploadFile` → `POST /api/uploads`
(`API_BASE="/api"`) returning `UploadDescriptor`; full attachment-chip UI in `Chat.tsx`
(Paperclip trigger, hidden file input, per-file spinner/error, send gated on in-flight uploads,
`readyDescriptors` echoed as `upload_ids`).

**Done — 06 (follow-ups + plan):** the JSON-contract UI is the only one left — inline
`FollowupForm` (`src/components/chat/FollowupForm.tsx`, reached from `Chat.tsx` →
`chatStore.submitFollowup`) and inline `PlanCard` (`Chat.tsx` → `chatStore.decidePlan`). The
legacy recoil/`askUserState`-driven `src/viewers/DynamicFormElement.tsx` was **deleted** and
`dynamic_form` unregistered from the viewer map (`src/lib/elements/elements.ts`): no producer
emitted it and `askUserState` could never populate, so there was nothing to preserve on either
side. `PlanViewerElement` stays registered — it renders from `ArtifactMeta`.

**Done — 08 (history):** `getRecentConversations` is wired into
`src/components/chat/ChatHistory.tsx:40`, with list / rename / soft-delete / resume.

**Done — 09 (destructive cutover):** `@chainlit/react-client` and `recoil` are out of
`frontend/package.json` (and out of its `description`); `npm ls` confirms neither resolves.
`src/main.tsx` renders `<App />` with no `ChainlitAPI` / `ChainlitContext` / `RecoilRoot`;
`src/lib/chainlit/chainlit.ts` is deleted and the directory renamed to `src/lib/elements/`;
`frontend/vite.config.ts` proxies only `/api`, with `CHAINLIT_TARGET` and all twelve legacy
routes gone; `VITE_CHAINLIT_SERVER` and the commented-out `chainlit-frontend` `depends_on` are
out of the root `docker-compose.yml`. Beyond the original scope, `CHAINLIT_AUTH_SECRET` was
retired as a fallback alias for the backend's own cookie-signing key — `BACKEND_JWT_SECRET` is
now the only name for it (`backend/app/config.py` `_jwt_secret`). **Already clean beforehand:**
`socket.io` was gone, nginx already proxied `→ backend:8000`, and there was no live
`ChainlitAPI(...).connect()`.

Gates after the cutover: `npm run typecheck` + `npm run lint` + `npm run build` clean, backend
pytest 361 passed, infra static suite 87 passed. Still **not validated live** (needs Docker/VPN).

## Why this is required (not optional)

The coupling is at the **transport/protocol** level, not the URL level:

- The app connects over **socket.io WebSocket** (`ChainlitAPI` in `main.tsx:7`,
  `connect({transports:["websocket"]})` in `App.tsx:30`) and reads a Chainlit `IStep`
  tree via `useChatMessages()` plus `elementState`/`actionState`/`askUserState` recoil
  atoms filled by Chainlit socket events (`Chat.tsx:252,299-301`).
- The backend has **no websocket**: `POST /api/chat/messages` returns one
  `ChatMessageResponse`; progress arrives one-way over **SSE** at `GET /api/events`.

So repointing a proxy cannot work — `connect()` has nothing to handshake with. The
transport + state layer and the auth calls must be rewritten.

## What's preserved vs. replaced

**Preserved (the bulk of the app):** all viewer components and the routing layer are
Chainlit **type-only** and stay behind a thin adapter:

- `components/ElementRenderer.tsx` (`classifyElement` + `VIEWER_REGISTRY` + `dedupeElements`)
- `lib/chainlit/elements.ts` (classifier/registry), `artifactPresentation.ts`,
  `elementDeduplication.ts` — logic unchanged, only the input type is retargeted
- Viewers with **no runtime** Chainlit dep: `JSONViewer`, `FollowUpSuggestionsElement`,
  `PlanViewerElement`, `ThermalAnalysisElement`, `MermaidElement`, `MeshViewerElement`,
  `IFCViewerElement`, `FileDownloadElement`
- All of `Chat.tsx`'s UI/scroll/markdown/upload-chip/attachment layer — only its **data
  source** (the 3 Chainlit hooks + 3 recoil atoms) swaps to the new store

**Replaced:** the transport + state layer (`main.tsx`, `App.tsx`, the `useChat*` hooks,
recoil session/element/action/ask atoms), auth calls (`App.tsx`, `Login.tsx`), the
new-chat reset (`ProjectView.tsx`), and 3 viewers with runtime coupling
(`DynamicFormElement`, `ImageViewerElement`, `PdfViewerElement`).

## Ground rules

- **Contract is frozen; frontend conforms.** Code against `backend/app/contract.py`.
  Do not change backend shapes to suit the frontend without updating the contract + its
  tests. Raw `mime_type`/`json_type`/`tags` ride alongside `render_hint` for overrides.
  - ⚠️ **The contract was corrected after this doc was written** (see
    `../remediation/01-contract-correctness.md`): the `render_hint` enum now includes
    **`report`** (render as **markdown**, not a download) and `ArtifactMeta` gained a typed
    **`citation`** field (`source`/`section`/`page`) for `reference` cards. Re-read
    `app/contract.py` — the enum list quoted in Step 05 below predates these additions.
- **Preserve viewers.** Retarget the element/artifact model via an adapter; do not rewrite
  working viewers. Net deletions should exceed net additions.
- **Additive until cutover.** The Chainlit `frontend/` service is already removed; keep only the
  remaining client-side Chainlit code (deps, `main.tsx` providers, dead `chainlit.ts`,
  `DynamicFormElement` recoil, vite proxies) in place until Step 09 excises it in one commit.
  Net deletions should still exceed net additions.
- Match existing frontend conventions (TS strict, Tailwind/daisyUI, lucide icons, the
  existing store/hook style once chosen in Step 00).

## Concept & endpoint mapping

| Chainlit (today) | backend (`/api`) | Notes |
|---|---|---|
| socket.io `connect()` | `EventSource("/api/events")` | one SSE stream per session; auth via cookie |
| `sendMessage(msg, fileRefs)` | `POST /api/chat/messages` → `ChatMessageResponse` | one response, discriminated by `status` |
| streamed `IStep.output` | `response_text` / `report` in the response | answer is whole-turn, not token-streamed (see Decisions) |
| tool `IStep` (`end:null`) | SSE `status` events (`message` + `history`) | drives the `ToolStep` progress UI |
| `askUserState` (form) | `status:"needs_info"` + `followup_questions: FormField[]` | submit via re-POST with `followup_answers` |
| `askUserState` (action) / approve buttons | `status:"plan_ready"` + `plan`, `auto_approve` | approve → re-POST `plan_approved`; reject → `plan_rejected` |
| `elementState` / `step.elements` | `artifacts: ArtifactMeta[]` (+ `render_hint`) | mapped to `AnyElement` by the adapter (Step 05) |
| `apiClient.getElementUrl(id, sid)` | `GET /api/artifacts/{id}/content` | image/pdf/download URL source |
| `uploadFile(file)` → `{id}` | `POST /api/uploads` → `UploadDescriptor` | echo descriptor in next request's `upload_ids` |
| `stopTask()` | (no endpoint yet) | see Decisions — client-side abort for now |
| `clear()` + reload | `POST /api/sessions` | mints new `conversation_id`; reset local store |
| `GET /user` | `GET /api/auth/me` | 200 → user, 401 → unauth |
| `POST /login` (form) | `POST /api/auth/login` (form or JSON) → `{user}` | sets httpOnly JWT cookie |
| `POST /logout` | `POST /api/auth/logout` | clears cookie |
| `GET /login/oauth/authorize` | `GET /api/auth/callback/{provider}` (501) | Okta not wired — see Decisions |
| static `src/config.ts` | `GET /api/config` + `POST /api/sessions` | intro, suggestions, `max_upload_bytes`, auto-approve |
| history (unused in React yet) | `GET /api/conversations/recent` | optional; wire into `LeftPanel` |

## New transport + state model

A single store owns conversation state and the network layer, replacing Chainlit's recoil
atoms + socket:

- **`lib/api/client.ts`** — typed fetch wrappers for every `/api` route, mirroring
  `contract.py`; all calls `credentials:"include"`.
- **`lib/api/events.ts`** — `EventSource("/api/events")` opened once after auth;
  `addEventListener("status" | "task_progress" | "task_done", …)`; ignores `: ping`
  comments; reconnects on drop.
- **`lib/store/chat.ts`** — holds `messages: ChatItem[]`, `pendingFollowup`, `pendingPlan`,
  `progress` (from `status.history`), `awaitingResponse`, `conversationId`. A reducer maps
  each `ChatMessageResponse.status` → item(s); SSE `status` updates the live progress item;
  `task_done` resolves a `backgrounded` turn.
- **`ChatItem`** is the app's own message type (user | assistant | tool-progress |
  needs-info | plan). `assistant` items carry `elements: AnyElement[]` produced by the
  artifact adapter, so `ElementRenderer` renders unchanged.

## Step order

| # | Step | Summary | Depends on | Verify |
|---|------|---------|------------|--------|
| 00 | Store + client scaffold | Add `lib/api/{client,events}.ts` + `lib/store/chat.ts` (empty impls, typed to `contract.py`). Store is a `useSyncExternalStore` slice (Decision A). | — | `tsc`/`vite build` clean; no behavior change |
| 01 | Auth swap | `App.tsx` probe `GET /api/auth/me`; `Login.tsx` → `POST /api/auth/login`; logout → `POST /api/auth/logout`. Remove socket connect/disconnect + session recoil resets. Okta button per Decision C. | 00 | login/logout/refresh cycle works against backend |
| 02 | Bootstrap | On auth, `GET /api/config` (intro/suggestions/`max_upload_bytes`/auto-approve) + `POST /api/sessions` (conversation_id). Replace static `config.ts` reads; keep `useChatType` localStorage toggle. | 00, 01 | welcome text + suggestions load from backend; upload cap from config |
| 03 | SSE stream | Open `EventSource("/api/events")`; route `status`/`task_progress`/`task_done` into the store. | 00, 01 | events observed in devtools; heartbeats ignored; reconnect works |
| 04 | Send turn + statuses | `POST /api/chat/messages`; map `answer`/`recommendation`/`completed`/`executing`/`error` → `ChatItem`s; live `status` history → `ToolStep` progress. Rewire `Chat.tsx` off `useChatMessages`/`useChatInteract`. | 00, 03 | round-trip Q&A renders; progress animates then settles |
| 05 | Artifact adapter | `artifactToElement(ArtifactMeta) → AnyElement` keyed on `render_hint`; feed `ElementRenderer`. Repoint `Image`/`Pdf` viewers to `GET /api/artifacts/{id}/content`; drop `getElementUrl`/`ChainlitContext`/`sessionState`. | 04 | each `render_hint` routes to the right viewer; image/pdf/download load |
| 06 | Follow-ups + plan | `needs_info` → `DynamicFormElement` from `FormField[]`, submit re-POSTs `followup_answers` (replace `askUserState.callback`); `plan_ready` → `PlanViewer` + approve/reject re-POST (`auto_approve` hides buttons). | 04, 05 | a needs_info turn and a plan_ready turn both complete |
| 07 | Uploads | `POST /api/uploads`; keep the attachment-chip UX; echo `UploadDescriptor` in `upload_ids`; cap from config. | 04 | attach → send → agent receives upload |
| 08 | New chat + history | `handleNewChat` → `POST /api/sessions` + store reset (drop `clear()`/reload). Optional: `GET /api/conversations/recent` into `LeftPanel`. | 02, 04 | new chat resets cleanly without full reload |
| 09 | Cutover | Drop the legacy Chainlit proxy routes + `VITE_CHAINLIT_SERVER` from `vite.config.ts` (the `/api`→`VITE_BACKEND_URL` proxy is already there); remove `VITE_CHAINLIT_SERVER` + the commented `chainlit-frontend` `depends_on` from root `docker-compose.yml`; remove `@chainlit/react-client` + `recoil` from `package.json` + fix the description; strip `ChainlitAPI`/`RecoilRoot` from `main.tsx`; delete dead `chainlit.ts`; drop recoil from `DynamicFormElement`. (nginx already `→ backend:8000`.) | all | `make up PROFILE=dev` yields a working app end-to-end; no `@chainlit/react-client`/`recoil`/`chainlit-frontend` refs |

## Step detail highlights

- **Step 04 status mapping.** One `ChatMessageResponse` → item(s): `answer`/`recommendation`
  = assistant bubble from `response_text` (+ artifacts, recommended_materials);
  `completed` = assistant bubble from `report` (+ artifacts, follow_up_suggestions);
  `executing` = transient progress; `backgrounded` = register `task_id`, resolve on SSE
  `task_done`; `error` = error bubble. SSE `status.message`/`history` feed the existing
  `ToolStep` (progress lines), replacing the `end:null` step convention.
- **Step 05 adapter is the linchpin.** `render_hint` (`pdf|image|json|ifc|mesh|mermaid|`
  `thermal|reference|file|unknown`) maps 1:1 onto the existing `ElementKind`/`VIEWER_REGISTRY`.
  Build `AnyElement`-shaped props from `ArtifactMeta` (name, description, `content`/`url` via
  the artifacts content route) so viewers stay untouched. `dedupeElements` +
  `artifactFilePresentation` keep working on the adapted shape.
- **Step 09 is the only remaining destructive code change.** Everything before it is additive
  and testable against a running backend (host `:8600`). *(The Chainlit **service** and old app
  are already gone — Step 09 now only removes the leftover Chainlit **client code** from the React
  app.)*

## Open decisions (resolve before Step 00)

- **A. State approach. — DECIDED: small custom store, drop Recoil.** The new store
  (`lib/store/chat.ts`) is a minimal `useSyncExternalStore`-backed store; `@chainlit/react-client`
  **and** `recoil` are both removed in Step 09. Recoil was present only for Chainlit.
- **B. Assistant text streaming.** The contract delivers the answer whole-turn (only
  `status` progress streams); there is no token stream. This matches today's behavior
  (status renders in a step, the answer arrives as the response), so it's a low-risk change —
  confirm token-streaming is out of scope for this pass.
- **C. Okta button.** `GET /api/auth/callback/{provider}` returns 501 (no IdP wired). *Recommendation:
  hide the "Continue with Okta" button until the provider is implemented, rather than link to a 501.*
- **D. `stopTask` semantics.** No cancel endpoint exists on the contract. *Recommendation: for
  this pass, "stop" aborts the client fetch / closes the UI's awaiting state only; a real
  server-side cancel is a backend follow-up.*

## Presentation parity notes (owned by the frontend, from remediation step 02)

Rendering moved to the frontend, so a few old-app behaviors are now the client's job (see
`../remediation/02-chat-turn.md`). Handle these in the adapter / message components:

- **`needs_info` with empty `followup_questions`** — render the `message` as a normal
  assistant bubble; do **not** enter a "waiting for form" state (the form would render
  nothing and the turn would appear to hang). The backend intentionally still sends
  `status: "needs_info"` here.
- **`error` status** — style it as an error (the old app prefixed the text with
  `**Error:**`); the backend now sends the clean message only.
- **Per-step plan status** — if you want the old PlanViewer running/completed/failed step
  states, derive them from the SSE `status`/`history` lines; the backend no longer computes
  a per-step heuristic.
- **`report` render_hint** — render as **markdown** (not a download); `recommendation`/
  `answer` read `response_text`, `completed` reads `report` (each turn populates exactly one).
- **Artifact content fetch failure** (A-7) — `GET /api/artifacts/{id}/content` returns a clean
  502/404 JSON when a `deeplynx://`/remote URI can't be resolved. Still render the artifact
  **card** from its `ArtifactMeta` (name/description/citation) and show a "couldn't load"
  state in the viewer, rather than dropping the artifact — the old app kept the reference.
- **Image classifier `gif`/`webp`** (A-8) — the backend classifies `image/gif` and `image/webp`
  as `image`; make sure the frontend image viewer/`isImageFile` set includes them so a
  backend `image` hint isn't downgraded to a download in the adapter.

## Cutover & rollback

Steps 00–08 ship against the running backend, so the app is verifiable incrementally. Step 09
removes the leftover Chainlit client code + proxies in one commit. Rollback is **branch-level
now** (`git revert` the Step 09 commit, or check the pre-removal Chainlit `frontend/` out of git
history) — the old `chainlit-frontend` service no longer exists to re-point at. The intermediate
steps remain valid because the contract is frozen.
