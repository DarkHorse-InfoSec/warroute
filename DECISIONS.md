# DECISIONS.md

Architectural questions that emerged during build, and how they were resolved.
Append-only. Newest at top.

---

## 2026-05-10 - WDGoWars API surface is undocumented; build to known endpoints + probe

**Question:** PLAN.md mentions `/api/upload-csv` and `/api/me` but doesn't list endpoints for territory ownership, per-cell capture value, or owned-cell enumeration. The wdgwars.pl site is auth-gated; no public docs reachable.

**Resolution:** Build the WDGoWars client around the two known endpoints. Add a `warroute coverage probe-wdgowars` CLI command that calls `/api/me` (and any other endpoint we want to inspect) with the real token, dumps the JSON response shape, and lets us extend the client based on what comes back.

**Open follow-up:** Once Domenic runs `probe-wdgowars` against his real account, capture the response shape and add a memory entry / fixture so the client can map `/api/me.owned_cells[]` (or whatever the real field is) into our `cells` table.

**Why this is fine:** Phase 2's coverage-report MVP can ship using just the cells we paint via WiGLE density and a placeholder ownership=null for everything until WDGoWars endpoint discovery completes. The grid + WiGLE half of the analyzer is fully buildable today.

---

## 2026-05-10 - Phase ordering: skip Phase 1, jump to Phase 2

**Question:** PLAN.md sequenced uploader (Phase 1) before coverage (Phase 2).

**Resolution:** Domenic directed to skip Phase 1 for now and build Phase 2. Reasoning: dual-upload automation is a quality-of-life win, but the actual game-changer is route planning (Phase 3), which depends on Phase 2 (coverage) and not Phase 1 (uploader). Manual upload is a fine bandaid until the route planner ships.

**Implication:** WiGLE and WDGoWars HTTP clients (originally scoped to Phase 1) are built here as shared dependencies under `warroute/clients/`, not `warroute/uploader/`. Phase 1 will reuse them when revisited.

---

## 2026-05-10 - SSH push failed; switched repo remote to HTTPS+gh-token

**Question:** First push to `github-darkhorse:DarkHorse-InfoSec/warroute.git` SSH alias failed with permission-denied + YubiKey-format error.

**Resolution:** Switched origin to `https://github.com/DarkHorse-InfoSec/warroute.git` and ran `gh auth setup-git` so gh's stored token handles credentials. SSH key path on Windows had ACL issues and the YubiKey-backed key wasn't present.

**Why this is fine:** HTTPS + gh credential helper is the gh-recommended Windows workflow. SSH can be reinstated later if Domenic wants to standardize keys.
