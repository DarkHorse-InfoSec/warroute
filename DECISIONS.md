# DECISIONS.md

Architectural questions that emerged during build, and how they were resolved.
Append-only. Newest at top.

---

## 2026-05-14 - Tester access via multi-user Caddy basic_auth (no app changes)

**Question:** WarRoute is deployed at `https://warroute.darkhorseinfosec.com` behind Caddy basic_auth with a single user (`domenic`). To validate the app before public release, we need to give a small number of testers their own credentials. PLAN.md §9 explicitly forbids login forms, JWTs, and session management ("Single-tenant. No login, no JWT, no session management. HTTP basic auth at the Caddy layer. Don't add a user model."). Does adding testers violate that constraint?

**Resolution:** No, as long as auth stays at the edge. Add additional basic_auth lines to the Caddyfile, one per tester. The app itself remains auth-unaware. The "single-tenant" constraint is about app state (no per-user data partitioning, no profile pages, no role checks in code) — not about how many people can authenticate to the reverse proxy.

**v0 contract:**
- Caddyfile `basic_auth {}` block holds N lines, one per identity.
- Tester onboarding via `infra/add-tester.sh <username>` on the box: generates a 24-char `secrets.token_urlsafe(18)` password, bcrypts via `caddy hash-password`, appends to `/etc/caddy/Caddyfile`, validates, reloads Caddy. Plaintext saved to `/etc/warroute/tester-passwords/<username>.txt` (mode 600, root-only) for retrieval.
- Revocation: delete the user's line from `/etc/caddy/Caddyfile` + `systemctl reload caddy`. Plaintext file at `/etc/warroute/tester-passwords/<username>.txt` is `rm`'d at the same time.
- Tester URL, username, and password delivered out-of-band (Signal/Slack DM); never in chat or commit.

**Why this and not the alternatives:**
- **Cloudflare Access (Zero Trust):** Considered. Free tier handles up to 50 users with Google/email SSO at the edge. Cleaner UX for testers (no shared-password ergonomics, real per-user audit logs). But requires turning on the Cloudflare orange-cloud proxy (changes TLS architecture; CF terminates TLS and re-issues to origin), setting up the CF Access app, and re-running our Let's Encrypt cert dance. Defer until tester count exceeds ~5 or per-user audit becomes a real requirement.
- **Tailscale:** Zero public exposure, magic-DNS hostnames, no app changes. But testers must install Tailscale and accept an invite — friction we don't want for casual testers, and incompatible with the "tester opens the URL on their phone" workflow.
- **Token-in-URL signed links:** Heaviest. Requires app code (verify, log, expire). Out of spec with "no JWT, no session." Rejected.

**Limits this approach hits before we need to upgrade:**
- No per-user audit: Caddy logs username on each request, but if a password leaks we can't tell *which user's session* that was without further correlation (multiple devices per tester, etc.). Acceptable for ~5 testers and a beta phase; not acceptable at scale.
- Password rotation is manual (delete + re-add).
- One leaked password = one revoke (single user), not a fleet-wide rotation. That's fine.

**Promotion path:** When we move from beta to public release, this all gets ripped out. Public release likely means either (a) opening Caddy without basic_auth and accepting "anyone can see the planner" (since there's no per-user state to leak), or (b) Cloudflare Access if we want analytics on who's using it. That's a future-Domenic decision; not in scope here.

---

## 2026-05-11 (PM, MSI home) - WDGoWars 1.3.0 API surface mapped; per-cell ownership not exposed

**Question:** Find the WDGoWars territory-enumeration endpoint so `cells.wdgowars_owner` can be populated. Open from the 2026-05-11 morning DECISIONS entry; candidates queued were `/api/territory`, `/api/cells`, `/api/gang/{id}`, `/api/reinforce`.

**Resolution:** Probed ~40 candidate paths from a clean home network (cert chain pre-verified Let's Encrypt). WDGoWars 1.3.0 exposes no endpoint that returns per-cell ownership IDs to API-token auth. Definitive list now in `memory/reference_wdgowars_api.md`.

**What IS available (newly mapped):**
- `/api/territories` — list of 187 gangs, each with `{name, color, members, points, hull, rank}`. The `hull` is a 12-point polygon: gang outer territory boundary. Usable for coloring gang regions on the coverage map. Filter params (`?owner=me`, `?mine=1`) are ignored — server returns the full list every time.
- `/api/badges` — badge catalog (168 bytes), `{badges: ...}`.
- `/api/leaderboard` — multi-leaderboard with `today / week / all_time / gangs / hunters / limit` slices.
- `/api/stats` — server stats: `uptime, version (1.3.0), requests, bytes, status (HTTP codes), cache, shield, connections, php, memory_kb, top_domains`. Use for monitoring server availability.

**`/api/me` is richer than mapped:** 22 top-level fields, of which the client currently surfaces 6. Unsurfaced: `country, joined, is_superuser, trusted, gang, gang_id, gang_role, mesh, cracked, aircraft, recent_7d, reinforce (per-zoom counts), reinforce_total, credits {balance/bounties/lifetime}, badges, donation_popup_suppressed`.

**Confirmed-NOT-API (session-cookie only):** `/api/captures` 302-redirects to `/login/`. The WDGoWars web UI presumably has a per-capture history view, but it's not API-token accessible.

**Confirmed 404 (all the ones we tried):** `/api/territory`, `/api/territory/me`, `/api/cells*`, `/api/owned*`, `/api/my/cells`, `/api/my/territory`, `/api/wifi*`, `/api/aps`, `/api/networks*`, `/api/reinforce*`, `/api/reinforcements`, `/api/credits`, `/api/players`, `/api/gangs`, `/api/gang/{id}*`, `/api/gangs/{id}`, `/api/territories/{id}`.

**Decision:**
- Close the open follow-up. `cells.wdgowars_owner` stays NULL by design. Don't probe further; the surface is mapped.
- The DB schema and ownership column stay as-is — they are forward-compatible if WDGoWars later adds an enumeration endpoint, but we won't build code that depends on it landing.
- Post-v1 enhancements unlocked by this probe (queued, not in scope now):
  1. `coverage/` overlay using `/api/territories` hull polygons (color gang regions, highlight `gang_id==16` as "us").
  2. Extend `WdgowarsClient.me()` to surface the 16 unmapped fields. The web dashboard's player card can show gang affiliation, recent_7d, credits balance, badge count.
  3. `/health` check enhancement: poll `/api/stats.version` to detect WDGoWars upgrades (which might expose new endpoints).

**Why this is fine:** WarRoute's value prop never required per-cell ownership granularity from WDGoWars. The PLAN's coverage analyzer was always going to lean on WiGLE for density and treat WDGoWars ownership as a sparse signal. Now we know it's a zero signal at the cell level and a polygon signal at the gang level. The router scorer already weights WiGLE density 100% in absence of WDGoWars data per `router/scorer.py`.

**Probe hygiene:** No probe script committed. Token never echoed; only HTTP status, body length, top-level keys printed. Probe code ran inline via `uv run python -c "..."`. Self-cleaning by construction.

---

## 2026-05-11 - Phase 5 notifications: run-complete only in v1; plan + quota toggles wired but no-op

**Question:** PLAN.md §3.5 punts push notifications to v1.1, but Domenic wants them landed now. Should we ship just the run-complete notification (per the PLAN body example) or also wire plan-complete and quota-warning notifications?

**Resolution:** Ship run-complete only in v1. Add `NTFY_NOTIFY_PLAN` (default false) and `NTFY_NOTIFY_QUOTA` (default false) settings to `config.py` as documented toggles, but do NOT wire emit paths for them. When a future PR adds plan-complete and quota-warning emit paths, the settings are already there; flipping them on takes no schema change.

**Why this is fine:**
- Plan-complete notification is low value when Domenic is at the keyboard planning (which is the normal case). The toggle exists for the rare case where he kicks off a long plan from his phone, leaves, and wants to be pinged when the GPX is ready.
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

**Question:** Mid-session probe of WDGoWars `/api/me` from the school PC (`domenic.laurenzi` on NCSUVT network) failed with `[SSL: CERTIFICATE_VERIFY_FAILED]`. Was this a stale certifi bundle, an expired cert on the WDGoWars side, or something else?

**Resolution:** Ran `openssl s_client -showcerts` against `wdgwars.pl:443`. Cert chain terminates at:

```
issuer=C=US, ST=California, L=Sunnyvale, O=Fortinet, OU=Certificate Authority, CN=FG6H0FTB22903890
```

That is a FortiGate firewall's TLS-inspection certificate (`FG6H0...` is a Fortinet device serial). The NCSUVT network is performing TLS MITM on outbound HTTPS. Confirmed by `curl` (schannel: `SEC_E_UNTRUSTED_ROOT`) — same failure from a separate trust store, so it's the network path, not the local trust store.

**Decision:**
1. **No-bypass-verify, ever.** Setting `verify=False` (httpx) or `--insecure` (curl) would let WIGLE_TOKEN / WDGOWARS_TOKEN / ORS_API_KEY transit through the Fortinet device in plaintext. This is a literal Rule #1 violation (never let a secret reach output / a logging endpoint). The 2-line fix is exactly what the inspection device wants.
2. **Defer token-transmitting work to a clean network.** #4 (WDGoWars territory endpoint probe) and #1 (live precheck + live drive verification) cannot safely run from this machine on this network. To be run from MSI (home, clean) or via a VPN tunnel off the school net.
3. **Token leak status: zero on this run.** Python's default secure-by-default SSL behavior aborted the handshake before the HTTP request body went out; the token never crossed the wire. No rotation needed unless a real-API command was run from this network in a prior session (none recorded — Phase 1-4 work used respx mocks, only `coverage probe-wdgowars` and the planner against real APIs could have leaked, and per git log those were authored on MSI before this session).
4. **Continue offline-safe work on this machine.** Phase 5 (ntfy.sh) code + tests with mocks, Hetzner infra artifacts (no execution), precheck CLI scaffolding with respx-mocked tests, docs.

**Why this is fine:** Three of the four post-v1 items have substantial offline-safe code surface. The two blocked items (probe, live drive) require Domenic to physically be on a different network anyway — the drive itself is from home, not the school PC. Parking those for a clean-network session costs zero schedule.

**Open follow-up:** When on a clean network, run `openssl s_client -showcerts -servername wdgwars.pl -connect wdgwars.pl:443` first. If the chain terminates at a real public CA (Let's Encrypt, DigiCert, etc.), proceed. If it's still Fortinet/any-vendor-CA, the VPN isn't routing this traffic out the tunnel — fix that before transmitting any token.

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
- **No `owned_cells` list** — `/api/me` does not enumerate territory cells. Need a different endpoint (TBD).
- **No `daily_quota_remaining`** — derive as `20000 - recent_today` (PLAN.md cap).

**Open follow-up:** Find the territory-enumeration endpoint. Candidates to probe next: `/api/territory`, `/api/cells`, `/api/gang/{id}`, `/api/reinforce`. **RESOLVED 2026-05-11 PM** — see top entry; WDGoWars 1.3.0 does not expose per-cell ownership to API-token auth.

---

## 2026-05-10 - WDGoWars API surface is undocumented; build to known endpoints + probe

**Question:** PLAN.md mentions `/api/upload-csv` and `/api/me` but doesn't list endpoints for territory ownership, per-cell capture value, or owned-cell enumeration. The wdgwars.pl site is auth-gated; no public docs reachable.

**Resolution:** Build the WDGoWars client around the two known endpoints. Add a `warroute coverage probe-wdgowars` CLI command that calls `/api/me` (and any other endpoint we want to inspect) with the real token, dumps the JSON response shape, and lets us extend the client based on what comes back.

**Open follow-up:** Once Domenic runs `probe-wdgowars` against his real account, capture the response shape and add a memory entry / fixture so the client can map `/api/me.owned_cells[]` (or whatever the real field is) into our `cells` table.

**Why this is fine:** Phase 2's coverage-report MVP can ship using just the cells we paint via WiGLE density and a placeholder ownership=null for everything until WDGoWars endpoint discovery completes. The grid + WiGLE half of the analyzer is fully buildable today.

---

## 2026-05-10 - REVERSED: Phase ordering decision was wrong

**Question:** Original entry below stated Phase 1 was being skipped per Domenic's direction.

**Resolution:** I misread Domenic's "move onto Phase 2" as "skip Phase 1." He had previously told me to build phases in strict order. Phase 1 was back-built after Phases 2 and 3 already had PRs open, creating awkward branch dependencies. Lesson saved to `tasks/lessons.md` and to project memory.

**Implication for repo:** Phase 1's uploader code branched from Phase 2's branch (so it can use the WiGLE+WDGoWars clients that already lived there). Merge order: PR #1 (Phase 2) -> PR #3 (Phase 1, this) -> PR #2 (Phase 3). Going forward: build phases strictly in order; if uncertain, ask.

### 2026-05-10 - Phase ordering: skip Phase 1, jump to Phase 2 (HISTORICAL)

**Question:** PLAN.md sequenced uploader (Phase 1) before coverage (Phase 2).

**Resolution at the time:** Domenic directed to skip Phase 1 for now and build Phase 2. Reasoning: dual-upload automation is a quality-of-life win, but the actual game-changer is route planning (Phase 3), which depends on Phase 2 (coverage) and not Phase 1 (uploader). Manual upload is a fine bandaid until the route planner ships.

**Implication at the time:** WiGLE and WDGoWars HTTP clients (originally scoped to Phase 1) were built under `warroute/clients/` (shared by Phase 1 and Phase 2) rather than `warroute/uploader/`. This part of the structure stayed correct even after the phase-skip was reversed.

---

## 2026-05-10 - SSH push failed; switched repo remote to HTTPS+gh-token

**Question:** First push to `github-darkhorse:DarkHorse-InfoSec/warroute.git` SSH alias failed with permission-denied + YubiKey-format error.

**Resolution:** Switched origin to `https://github.com/DarkHorse-InfoSec/warroute.git` and ran `gh auth setup-git` so gh's stored token handles credentials. SSH key path on Windows had ACL issues and the YubiKey-backed key wasn't present.

**Why this is fine:** HTTPS + gh credential helper is the gh-recommended Windows workflow. SSH can be reinstated later if Domenic wants to standardize keys.
