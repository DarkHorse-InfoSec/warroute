# DECISIONS.md

Architectural questions that emerged during build, and how they were resolved.
Append-only. Newest at top.

---

## 2026-07-05 (pentest-remediation) - Eng #36 external pentest: self-host libs + nonce CSP

**Status: BUILT + verified (336 tests, ruff + mypy clean; browser-verified with Playwright,
zero CSP violations across coverage/plan/settings + the htmx plan-submit swap).**

**Context:** An external penetration test rated WarRoute STRONG: 0 Critical, 0 High. Four
findings touched this repo (the MEDIUM Cloudflare-origin-bypass + a source-map LOW belong to
sibling apps behind Cloudflare, not WarRoute; the "no WAF on warroute" INFO is accepted by
design). Resolutions:

- **#3 LOW (third-party scripts without SRI) -> self-host, don't SRI.** htmx@1.9.12 and
  leaflet@1.9.4 (js + css + marker images) are now vendored under
  `warroute/web/static/vendor/`; `base.html` points at local paths. For a privacy app whose
  localStorage holds users' WiGLE/ORS/Mapbox keys, removing the third-party origin entirely
  beats pinning it with SRI: no supply-chain vector, no CDN-availability dependency, and the
  CSP can shed `unpkg`/`jsdelivr`. To bump either lib: replace the file under `vendor/` and
  the `base.html` reference (and the `htmx-<version>` filename).

- **#4 LOW (CSP `script-src 'unsafe-inline'`) -> nonce-based CSP, emitted by the app.** CSP
  moved from Caddy into a FastAPI middleware (`warroute/web/app.py` `SecurityHeadersMiddleware`)
  because a per-request nonce can't be minted at the Caddy layer. `script-src` is now
  `'self' 'nonce-<per-request>'` with NO `unsafe-inline` and NO CDN hosts. Every inline
  `<script>` carries `nonce="{{ request.state.csp_nonce }}"`; the 9 inline `on*=` handlers were
  refactored to `data-action` / `data-geocode-input` markers dispatched by delegated listeners
  in `app.js` (nonces do NOT authorize inline event-handler attributes, so those had to go, and
  delegation also survives htmx swaps + stop-row template clones). **htmx-swapped partial
  scripts** (plan_result, run) keep working because `base.html` sets
  `htmx.config.inlineScriptNonce` to the page nonce, so htmx stamps swapped inline scripts with
  a nonce that matches the document policy (verified: the plan-submit swap renders its Leaflet
  map with zero CSP errors). `style-src` deliberately keeps `'unsafe-inline'` (Leaflet + many
  template `style=""` attrs; style injection is far lower risk than script injection). The CSP
  line was removed from all three Caddyfiles (two CSP headers would intersect and break the
  nonce).

- **#6 INFO (missing Permissions-Policy) -> emitted by the same middleware.** Denies
  accelerometer/camera/geolocation/gyroscope/magnetometer/microphone/payment/usb (none are used).

- **#5 INFO (/sync no per-IP rate/count limit) -> already implemented, no code change.**
  `warroute/web/routes/sync.py` + `config.py` already enforce a per-IP 20/min sliding window,
  a 16 KB blob cap, and a global 10k-row LRU eviction. The black-box scan couldn't observe the
  limiter (20/min isn't tripped by light manual testing; the row cap is invisible externally),
  or the scanned deploy predated the sync feature. Action is deploy-verification only.

**Rejected:** SRI-pinning the CDN libs (keeps the third-party origin + availability dependency);
`strict-dynamic` (would require nonces on the vendored `<script src>` tags too, for no real gain
since htmx/leaflet don't dynamically inject scripts); keeping CSP in Caddy (can't do per-request
nonces). Branch: `fix/eng36-pentest-remediation`.

---

## 2026-07-05 (security-pass) - Pre-public hardening: strip operator data from the public tier

**Status: BUILT + verified (331 tests, ruff + mypy clean, gates confirmed on a local server).**

**Question:** A static security review before open-sourcing found that the stateless
model still had vestigial operator-specific server state leaking to the public,
no-auth tier, plus a few code-level bugs. Root cause: in a "browser is the account"
design the server should hold and expose NO operator data, but pieces still did.

**Resolution (root-cause fixes, secure by default):**
- **Home never rendered server-side.** `config.home_lat/lon` default is now a neutral
  contiguous-US centroid (not a real address); web routes seat maps at
  `PUBLIC_MAP_DEFAULT_LAT/LON`, never `settings.home_lat` (which is now used only by
  operator CLI coverage-grid painting). `effective_home` no longer reads the spoofable
  `X-Forwarded-User` -> `user_prefs`; it returns the neutral fallback, and the browser
  supplies the real home. `Caddyfile.public` also strips inbound `X-Forwarded-User`.
  Closes: operator-home disclosure on `/plan` + `/coverage`, and the "spoof the header,
  read a saved home address" leak.
- **Run data gated OFF by default.** `/runs/{id}` and `/runs/{id}/observations.geojson`
  serve exact scanned-AP coordinates (home network included), so they 404 unless
  `expose_run_data=true` - which must ONLY be set on a trusted, auth-gated deployment,
  never `Caddyfile.public`. The dashboard's recent-runs list is likewise hidden unless
  exposed. The public planner + coarse coverage grid stay public.
- **DOM XSS fixed.** Leaflet popups build HTML by concatenation from attacker-controlled
  map data (SSIDs, gang names, geocoder labels). Added `WarRoute.escapeHtml` and applied
  it in `coverage.html` (both popups) and `plan_result.html`; `run.html` already escaped.
- **Abuse backstops.** Added a global daily cap for the shared-ORS GEOCODE key
  (`shared_geocode_usage`, `_v8`; routing already had one) and an LRU row cap on the
  sync blob store (`sync_max_rows`), bounding operator-quota drain and storage DoS even
  under distributed/spoofed-IP abuse.
- **Infra perms.** `add-tester.sh`/`remove-tester.sh` install the Caddyfile 640 root:caddy
  so tester bcrypt hashes are not world-readable.

**Verified clean by the review (no change needed):** SQL fully parameterized; sync is
zero-knowledge with an unguessable id (no IDOR); no WiGLE/WDGoWars server-key fallback;
`X-Real-IP` is authoritatively set by the deployed Caddyfiles (not client-spoofable);
no SSRF (all outbound hosts fixed); no secrets in tree or history; `/docs` disabled.

**Tradeoff:** on the public instance the operator can no longer review their own runs
(the post-drive map). That is intentional: run review exposes exact AP coordinates, so
it belongs on a trusted, gated deployment (self-host, or the basic_auth / Cloudflare
Access Caddyfile with `expose_run_data=true`), not the anonymous public tier.

---

## 2026-07-05 (run-map) - Post-drive review map of scanned APs (NOT live in-drive)

**Status: BUILT + verified (endpoint + render + XSS-escape tested; local visual blocked by a
browser-profile lock, but the Leaflet init is a direct mirror of the working coverage/plan maps).**

**Question:** User asked "can the APs scanned show up as they drive on their map?" That is a live
in-drive UI - the exact thing PLAN.md section 9 forbids as a Critical distracted-driving risk ("No live
in-drive UI in v1. Plan in driveway, drive with phone in pocket, review on return"). It is also not in the
data path: WarRoute is not the scanner. The phone's wardriving app captures APs -> CSV -> Syncthing -> box
-> watcher ingests -> only then do observations exist. There is no live feed to plot.

**Resolution (asked the user; chose the safe option):** build the "post-drive review" half that section 9
explicitly blesses. New `GET /runs/{id}/observations.geojson` returns the APs first seen on that run
(`observations.first_seen_session = id`, with a location), and `run.html` renders a Leaflet map of them,
colored by encryption (open=red, WEP=amber, WPA/WPA2=green, WPA3/SAE=purple). Mirrors the coverage page's
client-fetch-geojson pattern; reuses the CSP-allowed `tile.openstreetmap.org` tiles. Capped at 5000 markers
(most-recent first) with a truncation note. No new migration. Two safety/correctness notes baked in:
- The page copy says "post-drive review only - built after upload, never live while driving," so the
  feature can't be mistaken for an in-drive UI.
- SSIDs are attacker-controlled (a network can be named `<img onerror=...>`), so the popup builder escapes
  every field client-side, and the raw SSID is delivered only as JSON (never inlined into page HTML). A
  test asserts a malicious SSID never appears in the server-rendered HTML.

A live/near-live passenger view and a true in-drive map were offered and declined in favor of this.

---

## 2026-07-05 (oneway-budget) - Time budget is optional for oneway ("just get me there")

**Status: BUILT + verified (local + browser).** Fixes forcing a time budget on a trip that has none.

**Question:** The time budget was required for every plan. But a oneway trip often has no budget: a road
trip, or just driving to a fixed address (mom's house). Forcing a number there is meaningless, and picking
a default silently either over- or under-shoots. What should a blank budget do?

**Resolution:** the budget is now **optional for oneway, still required for loop** (a loop with no budget
has no defined size). Two cases for oneway:
- **Blank budget -> route the direct path** to the destination and skip the planner entirely. There's no
  cap to optimize AP-scanning detours against; the direct route IS the answer. Result page shows a "No time
  budget set - showing the direct route" notice inviting the user to add a budget for detours.
- **A budget -> current behavior:** the extra time over the direct drive becomes AP-scanning detour.

Mechanics: `duration_min` form field is now `str | None`; the handler parses `budget_min: int | None`.
Loop + blank -> clear error up front (never touches ORS). Oneway + blank -> after the direct-route
precheck, stamp `req.duration_min = ceil(direct_min)` for display/DB and call `persist_direct_route`
(the same 0-cell path the no-cells-fit fallback uses). The reachability + budget-vs-direct sanity checks
are skipped when there's no budget (nothing to compare against; the destination was explicitly tapped).
Frontend: the mode `<select>` toggles the field's required-ness/placeholder/hint; switching to oneway
blanks the field unless the user hand-typed a value (tracked by an input listener). "Be somewhere by a
certain time" is served by the existing `arrive_by` field, which works with or without a budget.

---

## 2026-07-05 (geocode) - US Census geocoder for house numbers + raw coordinate pins

**Status: BUILT + verified (live Census calls).** Fixes "it doesn't give the full address" for rural US.

**Question:** ORS geocoding is OpenStreetMap-based, which has roads everywhere but few rural US house
numbers, so "1414 Mead Hill Road" only resolved to the road. User wanted the exact house.

**Resolution:** two additions to the geocode endpoint (`/plan/geocode`), tried in order:
1. **Raw coordinates -> exact pin.** If the query parses as `lat,lon`, return a single pin hit at those
   coords (no geocoder). Lets a user long-press a spot in Google/Apple Maps and paste the coordinates for
   anywhere no geocoder has. (The placeholder already promised this; now it's true.)
2. **US Census geocoder first for numbered addresses.** If the query starts with a house number, try the
   free, no-key US Census onelineaddress API (`warroute/clients/census.py`) - TIGER/Line data covers rural
   US streets with house-number ranges. If it has a match, use it (precise); else fall back to ORS.
3. **ORS (worldwide)** for everything else, or Census miss.

Verified live: "1414 Mead Hill Rd, DERBY, VT" -> exact house (44.94324, -72.00880) via Census, where ORS
only gave "Mead Hill Road, Holland VT" (road, wrong town). Note the town matters - Census needs the right
one; the user's address is in Derby, not Holland.

**Home-state auto-append (same day):** Census needs at least a STATE to pin a house number, and users type
bare streets ("1414 Mead Hill Road"). So the browser sends the user's home as focus (`lat`/`lon`, for
nearest-first sorting) + its label (`near`, e.g. "..., DERBY, VT, 05829"); the endpoint pulls the state
from `near` and, when a bare numbered query misses Census, retries with ", <state>" appended. Census then
figures out the town on its own ("1414 Mead Hill Road, VT" -> the Derby house). So a bare street now
resolves to the exact house without the user typing a town, as long as they've set their home. Verified
live. Only fires when the query itself has no state; falls back to ORS otherwise.

**Why Census and not a paid geocoder:** free, no key, no quota headache, and it is specifically strong at
the rural US residential addresses OSM misses. US-only, which is fine since it is a fallback layer over
ORS (which stays the worldwide default). Best-effort: any Census error/timeout falls through to ORS.

---

## 2026-07-05 (enrich) - Live per-user WiGLE density + WDGoWars territory scoring

**Status: BUILT + browser/log-verified.** Realizes the scorer's original intent (WiGLE density x WDGoWars
value) using the REQUESTER's own keys at plan time, so plans rank by real data without anyone pre-running
`coverage refresh`.

**Question:** Scoring was `capture_value x WiGLE_density`, but density lived only in the shared cells table
(operator-refreshed) and per-cell WDGoWars ownership was never populated (the API doesn't expose it), so
capture_value was a constant. On the public stateless instance, un-refreshed areas produced geometric
spreads. User asked to wire in live per-user WiGLE density and "also do War Dogs Go" (WDGoWars).

**Resolution (warroute/router/enrich.py, wired into planner.plan + _plan_multistop):**
- **WiGLE density (persisted, user-independent):** at plan time, query WiGLE (with the user's key) for the
  AP count of the NEAREST unprobed candidate cells, cache in the shared cells table. Density is the same
  for everyone, so the shared cache is correct and compounds across users. **Bounded** by a cell cap
  (`LIVE_DENSITY_CELL_CAP`, default 8) + a wall-clock budget (`LIVE_DENSITY_BUDGET_S`, default 20s) because
  WiGLE is ~1 req/sec. Probed cells are skipped, so the persist-cache makes repeat plans (and the loop
  auto-bump retry) cheap.
- **WDGoWars ownership (per-request, user-specific):** the API has no per-cell ownership, but it exposes
  gang-territory HULLS (`/api/territories`) + the user's gang (`/api/me.gang_id`). So point-in-polygon each
  candidate cell against the hulls: in the user's gang hull -> "me" (low value), in a rival gang hull ->
  "rival", else uncaptured (top value). This is user-specific (me vs rival depends on your gang), so it is
  an in-memory map passed to scoring, NEVER persisted to the shared table.
- Both are **best-effort**: any error logs and the plan proceeds (geometric spread). Keys come from the
  stateless headers -> PlanRequest.wigle_name/wigle_token/wdgowars_token.

**Why synchronous-bounded, not background or unbounded:** unbounded live WiGLE is impractical (1 req/sec x
hundreds of cells). Background-refresh-then-next-plan was considered but the user wanted THIS plan ranked;
bounded-synchronous (nearest N cells, cap + time budget, cached) gives real ranking now for the cells most
likely to be routed, at a bounded UX cost, and the shared cache means the cost is one-time per area.

**Honest limits:** hull ownership is coarse (gang outer boundary, not exact cell capture). Multi-stop
enriches once around home's radius (distant-segment cells may be missed). The cap means only the nearest
~8 cells get live density on the first plan in a new area; the rest stay geometric until refreshed.

---

## 2026-07-04 (sync) - Opt-in end-to-end-encrypted sync via a user-held code (not email login)

**Status: BUILT + browser-verified. `feature/sync-code`.**

**Question:** The stateless model (below) keeps keys in localStorage. That breaks for a real chunk of
users: iOS Safari EVICTS localStorage after ~7 days of not visiting a site, and there is no cross-device
story. "Enter your keys once" silently becomes "enter them again after Safari wipes you." Too many iOS
users to accept that. Need durable + cross-device persistence WITHOUT reintroducing accounts/logins to
hand out or plaintext keys on the server.

**Resolution: opt-in, end-to-end-encrypted backup keyed by a user-held sync CODE, not an email login.**
The browser encrypts the config (keys + prefs) with a key derived from a code the user holds, then stores
only the ciphertext server-side under a SHA-256 of the code. Restore on any device (or after eviction) by
entering the code. WebCrypto: PBKDF2(200k, SHA-256) -> AES-GCM-256; `sync_id = SHA-256(code)` so the
stored id does not reveal the code. Server side (`/sync/{id}` PUT/GET/DELETE, `_v7.sql synced_configs`) is
a dumb opaque-blob store, rate-limited + size-capped + id-format-validated. **Zero-knowledge:** the server
never sees the code or the plaintext keys (browser-verified: the stored blob contains no plaintext, and a
wrong code cannot restore). Fully opt-in: no code -> pure stateless as before; self-host stays the
zero-server option.

**Why a sync code and NOT magic-link email (the version first floated):**
- **No email infrastructure.** Magic links need an SMTP server or a paid provider plus SPF/DKIM and
  spam-folder deliverability fights on a hobby project. A code needs none of it.
- **More private + more aligned.** No PII (no emails stored), and the sync blob is E2E encrypted, so the
  server cannot read keys even transiently-at-rest. Fits the not-for-profit, no-account ethos.
- **Less mobile friction.** An email round-trip (switch to Mail, tap link, switch back) is worse on a
  phone than pasting a code from a password manager.
- Trade-off accepted: the code is a bearer credential the user must save (password manager); lose it and
  you re-enter keys (no recovery). For this data (re-enterable API keys) that is fine. A generated
  160-bit code makes `sync_id` non-enumerable and the KDF brute-force-resistant.

**Why not just accept localStorage-only:** the Safari 7-day eviction is a silent "it forgot my keys"
failure for the exact mobile audience the tool targets; not acceptable for a product with your name on it.

**Why not server-side accounts + encrypted-at-rest (operator holds a key):** would let the operator (or a
DB leak with the server key) decrypt user keys, and needs a real user model + auth. The code-based
E2E design keeps the server unable to read anything, which is strictly stronger and simpler.

---

## 2026-07-04 (design) - Access model for public testing: stateless "browser is the account"

**Status: BUILT (2026-07-04) on branch `feature/stateless-access`.** Keys live in the browser
(localStorage), attached to each request as `X-Wigle-*` / `X-Wdgowars-*` / `X-Ors-Key` headers via a
global htmx hook + `WarRoute.fetch`; the server stores nothing. WiGLE/WDGoWars have no fallback; ORS uses
the guarded shared carve-out (per-IP rate limit + per-day cap in `_v6.sql`, resolved by
`warroute.web.routing_quota`). `/settings` is a client-side editor (auto-save + export/import); the
dashboard player card and coverage overlay load via header-carrying partials/fetch; the plan form submits
via htmx. Three interchangeable edge modes ship: `infra/Caddyfile.public` (no gate, the intended public
posture), `infra/Caddyfile` (basic_auth), `infra/Caddyfile.cloudflare` (Cloudflare Access). Verified
end-to-end in a real browser (Playwright): localStorage save, header attachment, dashboard/plan flows,
Leaflet init after swap, nav-app ordering. The design + rationale below is unchanged.

**Question:** How do people test the hosted instance before (and after) it goes public, given three
constraints the maintainer set: (1) he should NOT have to create every tester's credentials; (2) it must be
secure; (3) some people want to remain anonymous. A follow-on wrinkle sharpened it: most WarRoute users
already have a WiGLE and/or WDGoWars account (uploading to those sites is the whole point), and many
will use BOTH. So "sign in with your key" raises: which key is the identity? A WiGLE token only proves a
WiGLE identity; a WDGoWars token only a WDGoWars one; a WDGoWars-only user can't log into a
WiGLE-anchored identity.

**Resolution (proposed): two tiers, and the hosted tier is fully client-side / stateless.**

Offer exactly TWO access tiers, matched to real personas, and resist adding more (every extra auth mode
is attack surface, and the weakest option tends to become the default):

1. **Hosted, stateless ("browser is the account").** No login, no server-side accounts, no server-side
   identity. The browser (localStorage) holds every credential the user has (WiGLE name+token, WDGoWars
   token, ORS key) plus non-sensitive prefs (home, nav-app choice). Each request carries the credentials
   it needs; the server uses them transiently to proxy WiGLE/WDGoWars/ORS and stores NOTHING. Pseudonymity
   is moot because there is no server identity at all.
2. **Self-host (open-source).** For anyone who wants to be anonymous even to the operator: clone + run it
   with their own keys; nothing ever touches the maintainer's box. Already available; just document it as the
   privacy tier.

One-sentence story: "Use the hosted app and enter your keys (we store nothing), or run your own copy for
full privacy."

**Why the stateless model dissolves the multi-service problem:** none of the three keys is an identity;
they are independent capabilities the browser carries. A user with only WiGLE, only WDGoWars, or both,
all work identically: the app just uses whichever key each operation needs and skips what is absent.
There is no "primary account service" to pick, so there is nothing to be arbitrary about.

**Security posture (stated honestly):**
- Tokens are NOT persisted on the server (a real upgrade over today's plaintext-in-SQLite per-user creds,
  DECISIONS 2026-05-14). But the server DOES see each token transiently in memory while proxying the
  upstream call. Accurate framing is "seen, not stored," NOT "never seen." Truly-never-seen would require
  the browser to call WiGLE/WDGoWars directly, which CORS blocks. That is the ceiling for a hosted
  instance; above it you self-host (tier 2). This is WHY the two tiers are genuinely distinct.
- **CRITICAL: no system-key fallback for web requests.** Today web ops fall back to the system .env keys.
  In the public/stateless model that would let anonymous users drain the maintainer's WiGLE/ORS/WDGoWars quota.
  Web-facing operations MUST use ONLY the caller's client-supplied keys; a missing key returns "add your
  key," never the system key. System keys remain only for the maintainer's background watcher/admin, which are
  not web-reachable. Treat this as a hard requirement (same class as the Cloudflare-Access origin-lock).
- Credentials in localStorage are readable by any successful XSS on the page. Mitigation: keep the
  already-strict CSP (see infra/Caddyfile), no third-party scripts, no user-generated HTML. Document the
  tradeoff; it is the standard cost of client-held credentials.

**Reconciling with PLAN.md §9 ("no login, no session, no user model in the app"):** the stateless model
is actually the PUREST expression of §9 - it adds NO user model, NO accounts, NO server sessions. It does
move credential handling from "server-side per-user store keyed by Caddy's X-Forwarded-User" to
"client-supplied per request." The Caddy basic_auth edge gate is DROPPED for the public tier (optionally
replaced by Cloudflare Access purely as an invite-gate during closed beta - see below). §9's spirit is
preserved and arguably strengthened.

**Cloudflare Access is orthogonal, not a security tier.** It gates WHO can reach the instance (keep a
closed beta to a known email list), not token security, and it identifies by email so it fights anonymity.
Keep it available as an optional "close the beta" switch (the Caddyfile.cloudflare already exists), not as
a user-facing security level. It comes off for public.

**Why this and not the alternatives:**
- **Key-as-login with a server-side identity (WiGLE userid):** Considered and REJECTED, primarily because
  of the exact multi-service problem the maintainer raised: it forces one service to be the identity anchor and
  awkwardly relegates the other to a secondary credential, and it locks out single-service users of the
  non-anchor service. It also reintroduces server-side state (prefs keyed by userid) and a login/validate/
  signed-cookie mechanism, plus a WiGLE rate-limit concern (can't validate every request; needs a cookie
  to avoid it). More moving parts, weaker privacy story, no real benefit once prefs can live client-side.
- **Server-side token storage (extend today's model):** REJECTED as a public default. Strictly weaker than
  client-side with no UX gain; keeps plaintext tokens on the box; becomes the "weak option that becomes
  the default." Fine for the maintainer's own single-tenant/admin use, not for public testers.
- **Fully open on the operator's shared keys:** REJECTED. Exposes the maintainer's quota to bots; contradicts the
  not-for-profit, bring-your-own-keys ethos. (The stateless model is "fully open" for BROWSING but safe
  because operations require the caller's OWN keys - see the no-fallback requirement above.)
- **A third "more secure than tier 1" hosted option:** Does not exist (CORS blocks browser-direct calls),
  so tier 1 is the hosted ceiling and self-host is the step above it.

**Known tradeoffs / costs of the chosen model:**
- **Per-device, not per-user.** localStorage is per-browser. Switch device or clear site data and config
  is gone. Mitigation: a config export/import (copy a JSON blob / a code), still with zero server storage.
  Cross-device auto-sync is intentionally NOT offered (it would require server-side identity + storage,
  reintroducing everything above).
- **Frontend rework (the main cost).** A plain full-page navigation (e.g. GET /dashboard) cannot attach a
  localStorage credential as a header. So credentialed data loads must move to JS-fetch endpoints that
  attach headers from localStorage, with server-rendered pages becoming shells + client-fetched data.
  /coverage already works this way (JS fetches the geojson). /dashboard and /plan need this treatment.

**RESOLVED 2026-07-04 (routing keys):** chose (b) - a shared system ORS key for ROUTING ONLY, behind
strict per-IP rate-limiting + a daily-quota guard. Rationale: WiGLE + WDGoWars are keys the audience
already has; ORS is the one they lack, so requiring it would add a signup step for exactly the capability
that should "just work." The no-system-key-fallback hard rule therefore has ONE carve-out: routing may use
the shared ORS key. WiGLE + WDGoWars remain strictly client-supplied with NO fallback. Guard design:
per-IP rate limit on `/plan`, plus a per-day counter of shared-ORS routing calls persisted in SQLite; when
it nears the free-tier cap (2000/day, keep headroom) the shared key is cut off and the user is told to add
their own ORS key. This bounds operator quota exposure to routing only and degrades gracefully. Mapbox (c)
stays a wired-but-unused later fallback.

---

## 2026-07-04 - Public-readiness: multi-app nav, Cloudflare Access auth mode, gang overlay

Three forks resolved while preparing WarRoute to flip from private to public open-source.

**1. Per-user choice of navigation app (not just Google Maps).**
Question: the plan-result page only handed off to Google Maps + a GPX download. Testers wanted to use whatever map app is on their phone. Resolution: added `apple_maps_url()`, `waze_url()`, and `geo_uri()` (device-default via the Android `geo:` scheme) alongside the existing Google Maps + GPX hand-offs, plus a per-user `preferred_nav_app` column (`_v5.sql`) that selects the primary button; all options always render. Honest limitation surfaced in the UI: only Google Maps and GPX carry the full multi-stop loop; Apple Maps, Waze, and `geo:` route to the FIRST STOP only because their URL schemes cannot carry intermediate waypoints. Rejected: trying to fake multi-stop on Apple/Waze (no reliable scheme exists); routing single-dest apps to the trip END (useless for a loop, where end == home).

**2. Cloudflare Access as a second auth mode (keeping the app auth-unaware).**
Question: for a public flip, basic_auth is fine for a handful of testers but doesn't scale to frictionless signup. Resolution: support BOTH, chosen by which Caddyfile is deployed. `infra/Caddyfile` = basic_auth (default, zero deps). `infra/Caddyfile.cloudflare` = Cloudflare Access SSO, which authenticates at the edge and forwards `Cf-Access-Authenticated-User-Email`; Caddy maps that into `X-Forwarded-User`, so the app is unchanged. This preserves PLAN.md §9 (no login/JWT/session in the app). Two coupled requirements that made this correct rather than a footgun:
  - **Origin lock (hard requirement).** Caddy core cannot validate the Cloudflare Access JWT without a plugin, so the trust in the email header depends on the origin being reachable ONLY from Cloudflare (Hetzner Cloud Firewall allowlist to Cloudflare IP ranges). Documented as non-negotiable in the Caddyfile header, README, and infra/README. Without it, anyone hitting the origin directly can spoof the email header and impersonate any user.
  - **Username allowlist widened to accept emails.** The identity allowlist was `^[a-z0-9_.-]{1,32}$`, which rejects an email (`@`, length). Under Cloudflare Access every user would have failed validation and silently fallen back to env defaults, breaking per-user prefs. Widened to `^[a-z0-9_.+@-]{1,254}$` (still blocks spaces/quotes/semicolons/slashes). This is why both auth modes now share one identity path.

**3. Gang-territory overlay with an unverified hull coordinate order.**
Question: `/api/territories` returns 187 gang hull polygons (mapped 2026-05-11), but the coordinate ORDER of a hull point (lat,lon vs lon,lat) was never captured and is undocumented. Resolution: shipped the overlay (`/coverage/gangs.geojson` + Leaflet layer, our gang id 16 highlighted) with the order behind a single flippable constant `WDGOWARS_HULL_IS_LATLON = True` (assumes Leaflet's lat,lon convention, which the WDGoWars web map likely uses). This is honest engineering under external uncertainty, not a guess-and-ship: the assumption is documented at the constant, the client preserves API order without swapping, and a needs-live-verification item is tracked in tasks/todo.md. If the overlay renders mirrored/misplaced on first live load, flip the constant. Rejected: not shipping until verifiable (the feature would never land, and one live check settles it); auto-detecting order by magnitude (fails for Poland, where both lat ~52 and lon ~21 are < 90).

---

## 2026-05-14 (very late) - Per-user API credentials + admin/tester split on /settings

**Question:** The tester program needs to be production-ready. Two problems with /settings as deployed:
1. It shows every tester all of the deploy's internal config: DB path, spool dir, Hetzner IP, fingerprints of system API keys, etc. None of that is *secret* but it's noise at best and unnecessary attack-surface signal at worst.
2. Testers can't bring their own WiGLE / WDGoWars / ORS / ntfy credentials. Today every plan call hits the maintainer's ORS quota and every dashboard renders the maintainer's WDGoWars player state. Testers can't actually do tester things on their own accounts.

**Resolution:** Two coordinated changes, both small extensions of the per-user-prefs primitive shipped earlier today.

**Admin vs tester split.** New env var `ADMIN_USERS` (comma list of `X-Forwarded-User` values). The `/settings` page renders in two halves:
- Always-visible: each user's home location + their API credentials editor.
- Admin-only: the `.env` config table (paths, fingerprints, etc.).

The classification is purely about *what to render*; admins and testers run the same code path otherwise. No role flags propagate further.

**Per-user credentials.** Migration `_v4.sql` adds nullable columns to `user_prefs`: `wigle_name`, `wigle_token`, `wdgowars_name`, `wdgowars_token`, `ors_api_key`, `mapbox_api_key`, `ntfy_topic`. Each existing client (`WigleClient`, `WdgowarsClient`, `OrsClient`) already takes credentials via constructor args with a settings-based fallback, so wiring is just "if the user has saved this, pass it; otherwise let the client default kick in." Helper `effective_credentials(request)` returns the merged view (user values overriding env defaults).

**Storage: plaintext, same trust model as `.env`.** The system tokens live in `/etc/warroute/warroute.env` (mode 600, root-owned) in plaintext. The user tokens live in `/var/lib/warroute/warroute.db` (warroute user, mode 644 by default - SQLite doesn't restrict). Both files are filesystem-protected; an attacker with root has everything regardless. Encryption-at-rest with a Fernet key derived from a `SETTINGS_SECRET` env var was considered and rejected for v1: it only helps in the narrow case of a DB-file-only exfiltration (e.g. unencrypted backup leak), adds a new dependency (`cryptography`) and a new failure mode (key rotation), and the user-visible promise becomes weaker because *something else* still has to protect the SETTINGS_SECRET. For the current single-server tester deployment with at most a handful of users, plaintext + clear UI disclosure ("Stored in plaintext; use disposable tokens you can rotate") is honest and sufficient. The migration to encrypted-at-rest is one column-rewrite away if the threat model tightens.

**Scope of "per-user" today:**
- **UI-driven operations use per-user creds:** `/plan` + `/plan/geocode` use the user's ORS key; `/dashboard` uses the user's WDGoWars creds for the player card.
- **Background daemons stay system-scoped:** The CSV upload watcher (`SPOOL_DIR` -> orchestrator -> WiGLE + WDGoWars) keeps running on the maintainer's credentials. Per-tester CSV upload is a separate, larger piece of work (file upload form, per-user spool routing, orchestrator tagging) deferred to a future iteration. Today, testers wardrive in a car and can plan + check dashboard, but the maintainer still does the upload step. The UI should make that clear.

**Why this is still not the user-model slope §9 was guarding against:** We added one boolean (`is_admin`) and seven nullable columns. There's no `users` table, no roles, no role-based access checks in code paths (the only check is "show this section vs hide it"). Per-user creds are stored under the same primary key (`username`) that home prefs are. The architecture is "auth-at-edge + tiny key-value of per-user prefs," not "multi-tenant SaaS."

**Why this and not the alternatives:**
- **Single shared keys, no per-user override:** Status quo. Rejected because testers can't actually use their own accounts, which defeats the tester program.
- **Separate `user_credentials` table:** Cleaner separation than extending `user_prefs`. Rejected because it doubles the DAL surface area for a one-row-per-user feature that already lives in `user_prefs`. If we ever need encryption-at-rest, splitting is easy.
- **Encrypted-at-rest with Fernet now:** Considered. Rejected (see above) for v1 due to complexity and weak threat-model justification.
- **Read tokens from per-user `.env` files on disk:** Considered (extends Caddy's per-user pattern to credentials). Rejected because it punts UI work to "edit a file on the server" which isn't a tester-friendly workflow and adds ops burden the maintainer doesn't want.

---

## 2026-05-14 (late evening) - Per-user home prefs via Caddy X-Forwarded-User

**Question:** Tester program needs per-user persistent "home" (lat/lon or geocoded address). PLAN.md §9 forbids login forms, JWTs, sessions, and a user model; the multi-tester decision (2026-05-14) kept the app auth-unaware, with all authentication living at Caddy's basic_auth. With no notion of "current user" inside the app, persisting `{user → home}` is impossible. Either we (a) accept browser-only persistence (per-device, not per-user), or (b) tell the app who's logged in.

**Resolution:** (b). Caddy already authenticates each request at the edge; have it inject the authenticated username as an upstream header (`X-Forwarded-User: {http.auth.user.id}`). The app reads that header on each request and uses it as a key into a new `user_prefs` table. No login form, no JWT, no session in the app - Caddy continues to own auth, the app just learns the username after-the-fact for the narrow purpose of keying preferences.

**Scope (intentionally tight):**
- New table `user_prefs(username PK, home_lat, home_lon, home_label, updated_at)`. Username is the basic_auth identity, sanitized server-side to `[a-z0-9_.-]{1,32}`.
- One new helper `current_username(request)` that reads the header, sanitizes, returns None on missing/malformed.
- `/settings` becomes editable: form with address (geocoded type-ahead) OR raw lat/lon, saves a row.
- `/plan` GET pre-fills the start field from the user's saved home (falls back to `.env` defaults when no row exists, e.g. local dev without Caddy).
- No other per-user state. No "owned routes," no "my plans" view. Plans remain a single shared list across testers (matches the "we're all wardriving the maintainer's account anyway" reality).

**Why this is not the user-model slope §9 was guarding against:** §9 was preventing a SaaS-style architecture (login → session → JWT → role checks → multi-tenant data partitioning). Per-user preferences keyed off an edge-trusted header is two orders of magnitude smaller: one column, no auth code, no role logic, no session state. The app still trusts the connection (Caddy authenticated it); the only thing that changed is that the app now also reads *who* Caddy let in. If we ever need richer per-user state (saved templates, run history scoped per tester), that's a separate decision built on this primitive.

**Why this and not the alternatives:**
- **Browser localStorage:** Considered. Zero backend changes. Rejected because "persistent home" means "I set it once, my next device remembers" - localStorage is per-device. A tester setting home on their phone won't see it on their laptop, and clearing site data wipes it. Misses the actual ask.
- **URL with `?home_lat=X&home_lon=Y`:** Considered. Ultra-simple, no state anywhere. Rejected because every tester has to construct + bookmark their URL, and there's no "set my home" affordance in the UI - so they go through the friction every visit, on every device.
- **Cookie signed by the app:** Considered. Persists across devices if the user copies the cookie, but in practice cookies are per-browser too. Adds signing complexity without solving the cross-device case. Rejected.
- **Reading `Authorization: Basic ...` directly in the app:** Considered. Would let the app see the username without Caddy injecting a header. Rejected because the app currently has no auth code and we don't want it learning basic_auth parsing - the value of putting auth at the edge is that the app stays auth-naive. Adding a Caddy `header_up` directive is one line; parsing basic_auth in the app is dragging the rubicon in the wrong direction.

**Local-dev fallback:** When there's no Caddy in front (e.g. `warroute serve` directly), the X-Forwarded-User header is absent. `current_username(request)` returns None, the app falls back to `.env` defaults. Existing dev workflow is unchanged.

**Threat model note:** The app trusts the X-Forwarded-User header. If someone reaches `127.0.0.1:8000` directly (bypassing Caddy), they could spoof the header and read/write any tester's prefs. Mitigation: uvicorn binds to 127.0.0.1 by default (not 0.0.0.0), and Hetzner Cloud Firewall blocks ingress to port 8000 anyway. The systemd unit enforces the bind. This is the same trust model as every other reverse-proxy + auth-at-edge deployment.

---

## 2026-05-14 (evening) - Per-stop arrival deadlines (Phase 6b.3)

**Question:** PLAN.md §6b ships a single request-level `arrive_by` that implicitly targets the *trip end* (last stop for oneway; return-home for loop). User asked for per-stop deadlines: "Daily I need to pick my son up by 4PM" - a constraint on an intermediate stop, with other stops potentially after it. The §6b model can't express that: today's `arrive_by` is one number per plan, attached to the last waypoint.

**Resolution:** Add `arrive_by: datetime | None` to the `Stop` dataclass. The planner walks segment legs forward from a departure candidate, producing per-stop arrival times. Each constrained stop contributes a tuple `(cumulative_minutes_to_stop, arrive_by)`; the binding constraint is `min(arrive_by - cumulative_minutes)` across all such tuples. That value is the latest possible departure, which then flows into the existing `scheduled_departures` row and the existing ntfy alarm path unchanged.

The request-level `arrive_by` survives as a back-compat alias: when set without any per-stop constraint, it behaves exactly as before (trip-end deadline). When both are set, the tightest binding constraint wins. Net effect: existing single-deadline plans see no behavior change; new plans can put a hard deadline on any stop.

**Conflict policy: auto-drop cells, keep deadlines.** Deadlines are the hard constraint; wardriving cells are the soft one. When the binding departure would require leaving in the past (or within `MIN_LEAD_MIN=5` of now), the planner re-solves segments leading up to the binding stop with cells dropped (direct legs only). If even direct driving can't meet the deadline, raise `PlannerError` naming the stop and the minutes shortfall - that's a "you cannot physically get there in time" message, not "we couldn't fit a route." User explicitly picked this over hard-fail-on-conflict and over silent budget expansion; rationale is that "make my kid's pickup work" should never silently sacrifice timing for AP coverage.

**UX: HH:MM, implicit today (auto-tomorrow if past).** Per-stop input is a 24h time picker (`<input type="time">`), serialized as 4-digit `HHMM` in the stop payload. Server side: parse to today's date; if the resulting datetime is already past, roll to tomorrow. Matches the "daily 4PM pickup" use case directly without forcing a datetime input on the phone. Form-payload format extends from `lat,lon[:dwell[:overnight]]` to `lat,lon[:dwell[:overnight[:HHMM]]]`. Position-stable: empty-dwell-but-overnight-set is `lat,lon:0:overnight`; adding an arrive time uses `lat,lon:0:overnight:1600`. CLI `--stop` flag accepts the same extended format.

**Alarm scope: departure only.** Existing ntfy alarm fires at `departure - NTFY_DEPARTURE_LEAD_MIN`. Since departure is now derived from the tightest deadline across all stops, on-time departure implies on-time arrival at every constrained stop on the schedule. Per-deadline mid-trip alarms were considered and rejected: we don't have live position tracking (no in-drive UI is a deliberate safety constraint - PLAN.md §9), so a "running late for pickup" alarm would just be the original schedule's pre-known time - the user already knows that.

**Persistence:** `arrive_by` round-trips through `stops_json` as an ISO datetime string (or null). No schema migration required - `stops_json` is `TEXT/JSON`.

**Why this and not the alternatives:**
- **ORS optimization `time_windows`:** ORS exposes a `time_windows` field on jobs in `/optimization`. Considered passing per-stop constraints there so ORS itself does the scheduling. Rejected for v1 because (a) our segments are already split per-stop - the solver-per-segment doesn't see the full schedule; (b) ORS time-windows assume an absolute clock, which forces us to pass a candidate departure into every optimize call and re-call when it shifts; (c) the back-off-on-infeasibility logic we want ("drop cells, not deadlines") is orthogonal to ORS's TW handling and would still need to live in our code. We can revisit if multi-stop plans grow beyond 5-6 stops and our forward-walk becomes the bottleneck.
- **Hard-fail on conflict:** Rejected per the conflict-policy question above. Surfaces user errors but doesn't help recover; "Auto-drop cells, keep deadlines" matches the daily-routine use case where the user wants the planner to *just work*.
- **Auto-expand budget silently:** Rejected. Magical; the user said "30 min route" for a reason. Expansion belongs to a UI prompt ("we widened your budget by 8 min to make the pickup, OK?"), which is a Phase 6b.4 ask, not this one.

**What this enables next:**
- **Recurring schedules (Phase 7?).** A "daily 4PM pickup" deadline screams for a saved-template feature: store the constrained stop + arrive_by + dwell and replay the plan from a fresh departure each weekday. Out of scope here but a natural follow-on.
- **Trip-aware push.** Once each constrained stop has a derived ETA, we can also surface "running over schedule" warnings post-drive (comparing actual GPS-tagged WiGLE upload timestamps vs predicted ETA per stop). Cheap given the data we already capture.

---

## 2026-05-14 (PM) - Planner auto-paints grid + unit-density proxy when DB empty

**Question:** v1's `/plan` raises `PlannerError("No scored cells in reachable radius. Run `warroute coverage refresh` first.")` when the `cells` table has no rows in the request's reachable radius. The auto-bump fallback (added in `b41d482`) only bumps the *time budget*, not the candidate set - empty DB → bumped retry → same empty DB → hard fail. Hit live on prod (5.161.250.8) where the DB was migrated but `coverage refresh` has never been run.

Deeper issue: `coverage refresh` queries WiGLE per cell at ~1 req/s and per-memory stalls 60+ seconds on rural Vermont cells with sparse APs. So "just tell the user to run refresh" is not a real fix - it's slow and unreliable in the exact terrain WarRoute targets.

**Resolution:** Two coordinated changes:
1. **Auto-paint grid on demand.** When `_candidate_cells` returns empty, call `cells_in_radius(home, reachable_radius)` and `upsert_grid` the result. No WiGLE/WDGoWars calls - just inserts the geometry rows (id, center, bbox) with `estimated_total_aps=NULL`. Capped at `MAX_AUTO_PAINT_CELLS=2000` so an 8-hour-loop request doesn't silently write 50k+ rows (those should run `coverage refresh` deliberately).
2. **Two-tier scorer ranking.** Scorer treats `estimated_total_aps IS NULL` cells as unprobed with `UNPROBED_DENSITY_PROXY=1`. `rank_cells` sorts probed cells first (descending score) then unprobed cells (descending capture_value as tiebreaker). Probed cells always outrank unprobed regardless of nominal score - so once you've actually wardriven somewhere, that density data drives the next plan.

`PlanResult` gets two new flags: `synthetic_density: bool` (True when every chosen cell is unprobed) and `auto_painted_cells: int`. The web layer surfaces the synthetic notice on the result page: "No coverage data for this area yet - wardrive this loop and your next plan will be density-optimized."

**Why this and not the alternatives:**
- **Synthetic-waypoints (no DB writes):** considered. Generate N evenly-spaced waypoints in a circle around home, no cells touched. Smaller change. Rejected because painted cells become the seed for a future `coverage refresh` - the on-demand paint is doing useful permanent work, not just a one-off hack.
- **Auto-trigger WiGLE refresh in background:** considered. Kick off `coverage refresh` async on first plan, return synthetic now, populate density next plan. Rejected for v1 due to complexity (background job, status check, partial-completion handling). Worth revisiting when we have a job queue for Phase 6 arrival-time scheduled departures.
- **Default density=0 for None cells:** rejected. Would make all unprobed cells score 0, tied with cells where WiGLE actually said "zero APs here." Loses the signal: a probed-zero cell is genuinely empty; an unprobed cell is unknown.

**Conceptual clarification (worth restating):** "Scored" in WarRoute means "we have asked WiGLE about this cell," not "someone has wardriven it." WiGLE's underlying database covers most cells with at least a few public-registry APs. Empty-DB-locally just means we never queried WiGLE for that area. Most useful in remote regions where the user is the primary wardriver - those are the highest-yield targets, not exclusion zones.

**What this enables next:** Phase 6 (multi-leg planner) builds on top - multi-stop routes can use auto-paint for any segment whose corridor hasn't been refreshed. Roadtrip mode especially benefits, since long-distance corridors will almost always have un-queried cells along them.

---

## 2026-05-14 - Tester access via multi-user Caddy basic_auth (no app changes)

**Question:** WarRoute is deployed at `https://warroute.darkhorseinfosec.com` behind Caddy basic_auth with a single operator user (`admin`). To validate the app before public release, we need to give a small number of testers their own credentials. PLAN.md §9 explicitly forbids login forms, JWTs, and session management ("Single-tenant. No login, no JWT, no session management. HTTP basic auth at the Caddy layer. Don't add a user model."). Does adding testers violate that constraint?

**Resolution:** No, as long as auth stays at the edge. Add additional basic_auth lines to the Caddyfile, one per tester. The app itself remains auth-unaware. The "single-tenant" constraint is about app state (no per-user data partitioning, no profile pages, no role checks in code) - not about how many people can authenticate to the reverse proxy.

**v0 contract:**
- Caddyfile `basic_auth {}` block holds N lines, one per identity.
- Tester onboarding via `infra/add-tester.sh <username>` on the box: generates a 24-char `secrets.token_urlsafe(18)` password, bcrypts via `caddy hash-password`, appends to `/etc/caddy/Caddyfile`, validates, reloads Caddy. Plaintext saved to `/etc/warroute/tester-passwords/<username>.txt` (mode 600, root-only) for retrieval.
- Revocation: delete the user's line from `/etc/caddy/Caddyfile` + `systemctl reload caddy`. Plaintext file at `/etc/warroute/tester-passwords/<username>.txt` is `rm`'d at the same time.
- Tester URL, username, and password delivered out-of-band (Signal/Slack DM); never in chat or commit.

**Why this and not the alternatives:**
- **Cloudflare Access (Zero Trust):** Considered. Free tier handles up to 50 users with Google/email SSO at the edge. Cleaner UX for testers (no shared-password ergonomics, real per-user audit logs). But requires turning on the Cloudflare orange-cloud proxy (changes TLS architecture; CF terminates TLS and re-issues to origin), setting up the CF Access app, and re-running our Let's Encrypt cert dance. Defer until tester count exceeds ~5 or per-user audit becomes a real requirement.
- **Tailscale:** Zero public exposure, magic-DNS hostnames, no app changes. But testers must install Tailscale and accept an invite - friction we don't want for casual testers, and incompatible with the "tester opens the URL on their phone" workflow.
- **Token-in-URL signed links:** Heaviest. Requires app code (verify, log, expire). Out of spec with "no JWT, no session." Rejected.

**Limits this approach hits before we need to upgrade:**
- No per-user audit: Caddy logs username on each request, but if a password leaks we can't tell *which user's session* that was without further correlation (multiple devices per tester, etc.). Acceptable for ~5 testers and a beta phase; not acceptable at scale.
- Password rotation is manual (delete + re-add).
- One leaked password = one revoke (single user), not a fleet-wide rotation. That's fine.

**Promotion path:** When we move from beta to public release, this all gets ripped out. Public release likely means either (a) opening Caddy without basic_auth and accepting "anyone can see the planner" (since there's no per-user state to leak), or (b) Cloudflare Access if we want analytics on who's using it. That's a a future maintainer decision; not in scope here.

---

## 2026-05-11 (PM, MSI home) - WDGoWars 1.3.0 API surface mapped; per-cell ownership not exposed

**Question:** Find the WDGoWars territory-enumeration endpoint so `cells.wdgowars_owner` can be populated. Open from the 2026-05-11 morning DECISIONS entry; candidates queued were `/api/territory`, `/api/cells`, `/api/gang/{id}`, `/api/reinforce`.

**Resolution:** Probed ~40 candidate paths from a clean home network (cert chain pre-verified Let's Encrypt). WDGoWars 1.3.0 exposes no endpoint that returns per-cell ownership IDs to API-token auth. Definitive list now in `memory/reference_wdgowars_api.md`.

**What IS available (newly mapped):**
- `/api/territories` - list of 187 gangs, each with `{name, color, members, points, hull, rank}`. The `hull` is a 12-point polygon: gang outer territory boundary. Usable for coloring gang regions on the coverage map. Filter params (`?owner=me`, `?mine=1`) are ignored - server returns the full list every time.
- `/api/badges` - badge catalog (168 bytes), `{badges: ...}`.
- `/api/leaderboard` - multi-leaderboard with `today / week / all_time / gangs / hunters / limit` slices.
- `/api/stats` - server stats: `uptime, version (1.3.0), requests, bytes, status (HTTP codes), cache, shield, connections, php, memory_kb, top_domains`. Use for monitoring server availability.

**`/api/me` is richer than mapped:** 22 top-level fields, of which the client currently surfaces 6. Unsurfaced: `country, joined, is_superuser, trusted, gang, gang_id, gang_role, mesh, cracked, aircraft, recent_7d, reinforce (per-zoom counts), reinforce_total, credits {balance/bounties/lifetime}, badges, donation_popup_suppressed`.

**Confirmed-NOT-API (session-cookie only):** `/api/captures` 302-redirects to `/login/`. The WDGoWars web UI presumably has a per-capture history view, but it's not API-token accessible.

**Confirmed 404 (all the ones we tried):** `/api/territory`, `/api/territory/me`, `/api/cells*`, `/api/owned*`, `/api/my/cells`, `/api/my/territory`, `/api/wifi*`, `/api/aps`, `/api/networks*`, `/api/reinforce*`, `/api/reinforcements`, `/api/credits`, `/api/players`, `/api/gangs`, `/api/gang/{id}*`, `/api/gangs/{id}`, `/api/territories/{id}`.

**Decision:**
- Close the open follow-up. `cells.wdgowars_owner` stays NULL by design. Don't probe further; the surface is mapped.
- The DB schema and ownership column stay as-is - they are forward-compatible if WDGoWars later adds an enumeration endpoint, but we won't build code that depends on it landing.
- Post-v1 enhancements unlocked by this probe (queued, not in scope now):
  1. `coverage/` overlay using `/api/territories` hull polygons (color gang regions, highlight `gang_id==16` as "us").
  2. Extend `WdgowarsClient.me()` to surface the 16 unmapped fields. The web dashboard's player card can show gang affiliation, recent_7d, credits balance, badge count.
  3. `/health` check enhancement: poll `/api/stats.version` to detect WDGoWars upgrades (which might expose new endpoints).

**Why this is fine:** WarRoute's value prop never required per-cell ownership granularity from WDGoWars. The PLAN's coverage analyzer was always going to lean on WiGLE for density and treat WDGoWars ownership as a sparse signal. Now we know it's a zero signal at the cell level and a polygon signal at the gang level. The router scorer already weights WiGLE density 100% in absence of WDGoWars data per `router/scorer.py`.

**Probe hygiene:** No probe script committed. Token never echoed; only HTTP status, body length, top-level keys printed. Probe code ran inline via `uv run python -c "..."`. Self-cleaning by construction.

---

## 2026-05-11 - Phase 5 notifications: run-complete only in v1; plan + quota toggles wired but no-op

**Question:** PLAN.md §3.5 punts push notifications to v1.1, but the maintainer wants them landed now. Should we ship just the run-complete notification (per the PLAN body example) or also wire plan-complete and quota-warning notifications?

**Resolution:** Ship run-complete only in v1. Add `NTFY_NOTIFY_PLAN` (default false) and `NTFY_NOTIFY_QUOTA` (default false) settings to `config.py` as documented toggles, but do NOT wire emit paths for them. When a future PR adds plan-complete and quota-warning emit paths, the settings are already there; flipping them on takes no schema change.

**Why this is fine:**
- Plan-complete notification is low value when the maintainer is at the keyboard planning (which is the normal case). The toggle exists for the rare case where he kicks off a long plan from his phone, leaves, and wants to be pinged when the GPX is ready.
- Quota-warning notification needs a separate watcher / scheduled job to be useful (warns once per day when WiGLE or ORS drops below 10%). That's a distinct surface (background task, persistent state) not just an extra hook in the orchestrator. Best landed when the hetzner-deploy / systemd timer infrastructure is up.
- Wiring stub no-op paths now would be dead code; the cleaner pattern is settings-now, emit-later.

**v1 contract:**
- `NTFY_TOPIC` empty -> notifications disabled (silent skip in `NtfyClient.notify`).
- `NTFY_TOPIC` set + `NTFY_NOTIFY_RUN=true` (default) -> on every successful CSV ingest, POST to `{NTFY_BASE_URL}/{NTFY_TOPIC}` with body `"+N new APs of M total. WiGLE: ok|failed. WDGoWars: ok|failed|skipped."`, title `"WarRoute: Run #ID"`, tag `car`, priority 3, and click URL `{WEB_BASE_URL}/runs/ID` if `WEB_BASE_URL` is set.
- Notification failure (transport, 4xx/5xx, timeout) is logged at WARNING and swallowed -- never breaks the ingest.
- `NTFY_AUTH_TOKEN` enables `Authorization: Bearer` for self-hosted private ntfy servers.

**Open follow-up:** When deploying hetzner infra, decide whether `NTFY_BASE_URL` points at public `https://ntfy.sh` (default) or `https://ntfy.darkhorseinfosec.com` (self-hosted; better for the WDGoWars/WiGLE-leak threat surface since the notification body could carry session metadata). Tracking in tasks/todo.md as a follow-up; default ships pointing at public ntfy.sh.

---

## 2026-05-11 - School-network TLS interception blocks #1 live run + #4 territory probe (no-bypass)

**Question:** Mid-session probe of WDGoWars `/api/me` from a network with TLS interception failed with `[SSL: CERTIFICATE_VERIFY_FAILED]`. Was this a stale certifi bundle, an expired cert on the WDGoWars side, or something else?

**Resolution:** Ran `openssl s_client -showcerts` against `wdgwars.pl:443`. Cert chain terminates at:

```
issuer=C=US, ST=California, L=Sunnyvale, O=Fortinet, OU=Certificate Authority, CN=<device-serial-redacted>
```

That is a FortiGate firewall's TLS-inspection certificate. The network is performing TLS MITM on outbound HTTPS. Confirmed by `curl` (schannel: `SEC_E_UNTRUSTED_ROOT`) - same failure from a separate trust store, so it's the network path, not the local trust store.

**Decision:**
1. **No-bypass-verify, ever.** Setting `verify=False` (httpx) or `--insecure` (curl) would let WIGLE_TOKEN / WDGOWARS_TOKEN / ORS_API_KEY transit through the Fortinet device in plaintext. This is a literal Rule #1 violation (never let a secret reach output / a logging endpoint). The 2-line fix is exactly what the inspection device wants.
2. **Defer token-transmitting work to a clean network.** #4 (WDGoWars territory endpoint probe) and #1 (live precheck + live drive verification) cannot safely run from this machine on this network. To be run from MSI (home, clean) or via a VPN tunnel off the school net.
3. **Token leak status: zero on this run.** Python's default secure-by-default SSL behavior aborted the handshake before the HTTP request body went out; the token never crossed the wire. No rotation needed unless a real-API command was run from this network in a prior session (none recorded - Phase 1-4 work used respx mocks, only `coverage probe-wdgowars` and the planner against real APIs could have leaked, and per git log those were authored on MSI before this session).
4. **Continue offline-safe work on this machine.** Phase 5 (ntfy.sh) code + tests with mocks, Hetzner infra artifacts (no execution), precheck CLI scaffolding with respx-mocked tests, docs.

**Why this is fine:** Three of the four post-v1 items have substantial offline-safe code surface. The two blocked items (probe, live drive) require the maintainer to physically be on a different network anyway - the drive itself is from home, not the intercepted network. Parking those for a clean-network session costs zero schedule.

**Open follow-up:** When on a clean network, run `openssl s_client -showcerts -servername wdgwars.pl -connect wdgwars.pl:443` first. If the chain terminates at a real public CA (Let's Encrypt, DigiCert, etc.), proceed. If it's still Fortinet/any-vendor-CA, the VPN isn't routing this traffic out the tunnel - fix that before transmitting any token.

**Generalized lesson:** Saved to global memory as `feedback_verify_tls_chain_before_sending_tokens.md`. Future sessions on any non-home network must cert-chain-check before authenticated HTTPS.

---

## 2026-05-11 - WDGoWars auth is `X-API-Key`, not `Authorization: Bearer`

**Question:** First probe of `/api/me` against the real account 401'd. We had assumed Bearer auth based on PLAN.md.

**Resolution:** Empirical probe (`scripts/probe_wdgowars_auth.py`, since deleted) tried 7 auth styles. Only `X-API-Key: <token>` returned 200. Updated `clients/wdgowars.py` accordingly.

**`/api/me` real response shape (probed 2026-05-11):**
- `ok: true|false` (success indicator, not `success`)
- `username`, `country`, `joined`, `is_superuser`, `trusted`, `gang`, `gang_id`, `gang_role`
- `total` (all entities), `wifi`, `ble`, `mesh`, `cracked`, `aircraft`
- `recent_today`, `recent_7d`
- `reinforce: {zoom_level: count}` and `reinforce_total` (territory data, but as counts not cell IDs)
- `credits: {balance, bounties_completed, lifetime_earned}`
- `badges: [string]`
- **No `owned_cells` list** - `/api/me` does not enumerate territory cells. Need a different endpoint (TBD).
- **No `daily_quota_remaining`** - derive as `20000 - recent_today` (PLAN.md cap).

**Open follow-up:** Find the territory-enumeration endpoint. Candidates to probe next: `/api/territory`, `/api/cells`, `/api/gang/{id}`, `/api/reinforce`. **RESOLVED 2026-05-11 PM** - see top entry; WDGoWars 1.3.0 does not expose per-cell ownership to API-token auth.

---

## 2026-05-10 - WDGoWars API surface is undocumented; build to known endpoints + probe

**Question:** PLAN.md mentions `/api/upload-csv` and `/api/me` but doesn't list endpoints for territory ownership, per-cell capture value, or owned-cell enumeration. The wdgwars.pl site is auth-gated; no public docs reachable.

**Resolution:** Build the WDGoWars client around the two known endpoints. Add a `warroute coverage probe-wdgowars` CLI command that calls `/api/me` (and any other endpoint we want to inspect) with the real token, dumps the JSON response shape, and lets us extend the client based on what comes back.

**Open follow-up:** Once the maintainer runs `probe-wdgowars` against his real account, capture the response shape and add a memory entry / fixture so the client can map `/api/me.owned_cells[]` (or whatever the real field is) into our `cells` table.

**Why this is fine:** Phase 2's coverage-report MVP can ship using just the cells we paint via WiGLE density and a placeholder ownership=null for everything until WDGoWars endpoint discovery completes. The grid + WiGLE half of the analyzer is fully buildable today.

---

## 2026-05-10 - REVERSED: Phase ordering decision was wrong

**Question:** Original entry below stated Phase 1 was being skipped per the maintainer's direction.

**Resolution:** I misread the maintainer's "move onto Phase 2" as "skip Phase 1." He had previously told me to build phases in strict order. Phase 1 was back-built after Phases 2 and 3 already had PRs open, creating awkward branch dependencies. Lesson saved to `tasks/lessons.md` and to project memory.

**Implication for repo:** Phase 1's uploader code branched from Phase 2's branch (so it can use the WiGLE+WDGoWars clients that already lived there). Merge order: PR #1 (Phase 2) -> PR #3 (Phase 1, this) -> PR #2 (Phase 3). Going forward: build phases strictly in order; if uncertain, ask.

### 2026-05-10 - Phase ordering: skip Phase 1, jump to Phase 2 (HISTORICAL)

**Question:** PLAN.md sequenced uploader (Phase 1) before coverage (Phase 2).

**Resolution at the time:** the maintainer directed to skip Phase 1 for now and build Phase 2. Reasoning: dual-upload automation is a quality-of-life win, but the actual game-changer is route planning (Phase 3), which depends on Phase 2 (coverage) and not Phase 1 (uploader). Manual upload is a fine bandaid until the route planner ships.

**Implication at the time:** WiGLE and WDGoWars HTTP clients (originally scoped to Phase 1) were built under `warroute/clients/` (shared by Phase 1 and Phase 2) rather than `warroute/uploader/`. This part of the structure stayed correct even after the phase-skip was reversed.

---

## 2026-05-10 - SSH push failed; switched repo remote to HTTPS+gh-token

**Question:** First push to `github-darkhorse:DarkHorse-InfoSec/warroute.git` SSH alias failed with permission-denied + YubiKey-format error.

**Resolution:** Switched origin to `https://github.com/DarkHorse-InfoSec/warroute.git` and ran `gh auth setup-git` so gh's stored token handles credentials. SSH key path on Windows had ACL issues and the YubiKey-backed key wasn't present.

**Why this is fine:** HTTPS + gh credential helper is the gh-recommended Windows workflow. SSH can be reinstated later if the maintainer wants to standardize keys.
