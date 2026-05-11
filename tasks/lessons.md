# WarRoute - Lessons Learned

Updated whenever Domenic corrects an approach or confirms a non-obvious choice.

## Format

```
### YYYY-MM-DD - one-line summary

**Trigger:** what I did or proposed.
**Correction:** what Domenic said.
**Rule going forward:** what to do differently.
**Why:** the reasoning.
```

---

### 2026-05-10 - Routing engine must be worldwide, not regional

**Trigger:** Initial PLAN.md proposed self-hosted OSRM with VT + NH + QC OSM extract.
**Correction:** Domenic wants WarRoute usable anywhere in the world.
**Rule going forward:** Use OpenRouteService API as the routing backend; do not reintroduce self-hosted OSRM. Worldwide OSM extracts (~120 GB, ~32 GB RAM) won't fit on a CPX21 anyway.
**Why:** Single-user app, but the user travels. Worldwide coverage is a requirement, not a stretch goal.

### 2026-05-10 - Phase ordering is canonical, never skip

**Trigger:** Domenic said "After [commit] move onto Phase 2." I interpreted as "skip Phase 1." He had told me earlier (in a /btw I missed) NOT to skip phases.
**Correction:** Build PLAN.md phases strictly in numeric order.
**Rule going forward:** "Move onto Phase N" means continue the sequence, not skip ahead. If user names a phase, verify it's the next un-built phase; if not, ask before skipping.
**Why:** Skipping Phase 1 caused Phase 2 and Phase 3 PRs to land before Phase 1, creating awkward branch dependencies. The repair cost (rebuilding Phase 1 on top of Phase 2 retroactively) far exceeded the cost of asking for clarification up front.

### 2026-05-10 - Don't use `or` chains on JSON dict values when 0/False are valid

**Trigger:** WDGoWars `/api/me` parser: `payload.get("daily_quota_remaining") or payload.get("daily_remaining") or ...`. When the API returned `daily_quota_remaining: 0`, `or` treated 0 as falsy and fell through to defaults, masking the actual zero quota.
**Correction:** Use explicit key-presence checks (`_first_present(payload, *keys)`) when the value 0/False/'' is semantically distinct from "missing."
**Rule going forward:** `dict.get(...) or dict.get(...)` is fine for "first non-None string-ish value." For numeric or boolean fields where 0/False are valid, use `for k in keys: if k in payload: return payload[k]`.
**Why:** This bug silently bypassed the WDGoWars quota check in production-like scenarios, allowing uploads that should have been deferred. Caught only because the unit test set `daily_quota_remaining: 0` and asserted the skip path was taken.

### 2026-05-11 - Probe undocumented APIs empirically; don't guess auth scheme

**Trigger:** Assumed WDGoWars used `Authorization: Bearer <token>` based on PLAN.md prose. First real call 401'd. Spent a moment debugging the token before realizing the auth *style* might be wrong.
**Correction:** Wrote a one-off script that tried 7 common API auth styles in parallel and printed only the response status (no token in output). Found `X-API-Key`. Took ~30 seconds.
**Rule going forward:** When a real API call rejects auth, default to "is the auth scheme right?" before "is the token wrong?" Spend the 30 seconds to script-probe alternatives. Never log/echo the token while doing it. Delete the probe script after.
**Why:** Tokens almost never silently rotate. Auth scheme guesses, however, are a coin-flip across `Bearer`, raw, `X-API-Key`, `Token`, query-param, etc. Empirical probe is faster and more reliable than reading docs (which may not exist).

### 2026-05-11 - When in doubt about response shape, probe before mapping

**Trigger:** Built `WdgowarsClient.me()` projecting fields named `points`, `daily_quota_remaining`, `owned_cells` based on what PLAN.md *implied* the API returned. The real `/api/me` had none of them: it has `total`, `wifi`, `recent_today`, and only counts (not IDs) for territory.
**Correction:** Probe `/api/me` first; map fields against the real shape; preserve the full payload in `.raw` so future code can extend without re-probing.
**Rule going forward:** Add a `probe(path)` method to any client that talks to an undocumented service. Run it once against the real account before writing parser code. Save the response shape to a project memory entry so future sessions can refer back without re-probing.
**Why:** The fields we assumed didn't exist meant the dashboard would have shown zeros for everything; the quota check would have been moot. Caught early because we built `probe` first; would have been a painful surprise on first deploy otherwise.

### 2026-05-11 - Windows + Git Bash auto-converts Unix paths in CLI args

**Trigger:** Ran `warroute coverage probe-wdgowars /api/me`. Got a 404 for `/Program%20Files/Git/api/me`.
**Correction:** Set `MSYS_NO_PATHCONV=1` per command. Domenic's machines are all Windows + Git Bash, so this comes up.
**Rule going forward:** When a Unix-style path argument lands in the wrong place on Windows, suspect MSYS path conversion before suspecting the program. README + project CLAUDE.md both mention this. Global memory has a reference entry too.
**Why:** Wasted ~5 minutes the first time. The error message ("404 Not Found") was indistinguishable from a real wrong-endpoint case until inspecting the request URL.

### 2026-05-10 - Don't roll a custom scoring formula

**Trigger:** Initial PLAN.md proposed `score = 0.6 * new_to_you + 0.4 * new_territory_cell`.
**Correction:** Use WDGoWars and WiGLE native numbers; don't compete with their scoring.
**Rule going forward:** The scorer combines WDGoWars `capture_value` (or ownership-derived value) with WiGLE AP density. No hand-tuned weights. WarRoute is a thin orchestration layer over the existing services.
**Why:** WarRoute should reflect how the games already value territory, not a parallel value system that drifts from them.
