# DECISIONS.md

Architectural questions that emerged during build, and how they were resolved.
Append-only. Newest at top.

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

**Open follow-up:** Find the territory-enumeration endpoint. Candidates to probe next: `/api/territory`, `/api/cells`, `/api/gang/{id}`, `/api/reinforce`.

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
