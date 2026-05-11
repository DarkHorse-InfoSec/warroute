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

### 2026-05-10 - Don't roll a custom scoring formula

**Trigger:** Initial PLAN.md proposed `score = 0.6 * new_to_you + 0.4 * new_territory_cell`.
**Correction:** Use WDGoWars and WiGLE native numbers; don't compete with their scoring.
**Rule going forward:** The scorer combines WDGoWars `capture_value` (or ownership-derived value) with WiGLE AP density. No hand-tuned weights. WarRoute is a thin orchestration layer over the existing services.
**Why:** WarRoute should reflect how the games already value territory, not a parallel value system that drifts from them.
