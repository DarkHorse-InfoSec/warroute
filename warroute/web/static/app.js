// WarRoute stateless client (DECISIONS.md 2026-07-04).
//
// Your WiGLE / WDGoWars / ORS keys and prefs live in THIS browser (localStorage)
// and are attached to each request as headers. Nothing is stored on the server.
// A global htmx:configRequest hook covers every htmx request (geocode, dashboard
// player card, plan submit); WarRoute.fetch covers our own fetch() calls.
(function () {
  "use strict";

  var KEY = "warroute.config.v1";

  // Config field -> request header. Only non-empty values are sent.
  var HEADER_MAP = {
    wigle_name: "X-Wigle-Name",
    wigle_token: "X-Wigle-Token",
    wdgowars_name: "X-Wdgowars-Name",
    wdgowars_token: "X-Wdgowars-Token",
    ors_key: "X-Ors-Key",
    mapbox_key: "X-Mapbox-Key",
    ntfy_topic: "X-Ntfy-Topic",
    nav_app: "X-Nav-App"
  };

  function load() {
    try {
      return JSON.parse(localStorage.getItem(KEY)) || {};
    } catch (e) {
      return {};
    }
  }

  function save(cfg) {
    localStorage.setItem(KEY, JSON.stringify(cfg || {}));
  }

  function get(k) {
    var v = load()[k];
    return v == null ? "" : v;
  }

  // Build the request headers from stored config (skips blank values).
  function headers() {
    var c = load();
    var h = {};
    for (var field in HEADER_MAP) {
      if (c[field]) {
        h[HEADER_MAP[field]] = String(c[field]);
      }
    }
    return h;
  }

  // Address-search type-ahead. Driven explicitly (not htmx) to avoid htmx param
  // and attribute-inheritance pitfalls: sends the query as `q`, finds its own
  // results container (the nearest .geocode-results inside the .geocode-field),
  // and carries the credential headers. Works for the plan start, plan stops, and
  // the settings home field alike. The injected buttons call warrouteSelectGeocode
  // (defined per page). Debounced per input.
  var _geoTimers = new WeakMap();
  function geocodeInput(inputEl) {
    var field = inputEl.closest(".geocode-field");
    var hitsEl = field ? field.querySelector(".geocode-results") : null;
    if (!hitsEl) { return; }
    var q = (inputEl.value || "").trim();
    var prev = _geoTimers.get(inputEl);
    if (prev) { clearTimeout(prev); }
    if (q.length < 2) { hitsEl.innerHTML = ""; return; }
    _geoTimers.set(inputEl, setTimeout(function () {
      // Send the user's home as focus (nearest-first) + its label (so a bare
      // street query can be resolved to the exact house via the home state).
      var c = load();
      var url = "/plan/geocode?q=" + encodeURIComponent(q);
      if (c.home_lat && c.home_lon) {
        url += "&lat=" + encodeURIComponent(c.home_lat) + "&lon=" + encodeURIComponent(c.home_lon);
      }
      if (c.home_label) { url += "&near=" + encodeURIComponent(c.home_label); }
      warrouteFetch(url)
        .then(function (r) { return r.text(); })
        .then(function (html) { hitsEl.innerHTML = html; applyUnits(hitsEl); })
        .catch(function () { hitsEl.innerHTML = ""; });
    }, 300));
  }

  // ---- Units (metric default, or imperial) --------------------------------
  // Distances/speeds are rendered by the server in metric with a data-* attribute
  // carrying the source value; this rewrites them to the user's chosen unit on
  // load and after any content swap. No-JS users just see metric.
  function units() {
    return load().units === "imperial" ? "imperial" : "metric";
  }
  function fmtKm(kmRaw) {
    var km = parseFloat(kmRaw);
    if (isNaN(km)) { return ""; }
    if (units() === "imperial") {
      var mi = km * 0.621371;
      if (mi < 0.1) { return Math.round(mi * 5280) + " ft"; }
      return (mi < 10 ? mi.toFixed(1) : String(Math.round(mi))) + " mi";
    }
    if (km < 1) { return Math.round(km * 1000) + " m"; }
    return (km < 10 ? km.toFixed(1) : String(Math.round(km))) + " km";
  }
  function fmtSpeedKmh(kmhRaw) {
    var kmh = parseFloat(kmhRaw);
    if (isNaN(kmh)) { return ""; }
    return units() === "imperial"
      ? Math.round(kmh * 0.621371) + " mph"
      : Math.round(kmh) + " km/h";
  }
  function applyUnits(root) {
    root = root || document;
    root.querySelectorAll("[data-dist-km]").forEach(function (el) {
      el.textContent = fmtKm(el.getAttribute("data-dist-km"));
    });
    root.querySelectorAll("[data-speed-kmh]").forEach(function (el) {
      el.textContent = fmtSpeedKmh(el.getAttribute("data-speed-kmh"));
    });
  }
  document.addEventListener("DOMContentLoaded", function () { applyUnits(document); });
  document.addEventListener("htmx:afterSettle", function (evt) {
    applyUnits(evt && evt.detail && evt.detail.elt ? evt.detail.elt : document);
  });

  // fetch() that carries the credential headers. Use for our own AJAX.
  function warrouteFetch(url, opts) {
    opts = opts || {};
    var merged = {};
    var base = opts.headers || {};
    var k;
    for (k in base) {
      merged[k] = base[k];
    }
    var h = headers();
    for (k in h) {
      merged[k] = h[k];
    }
    opts.headers = merged;
    return fetch(url, opts);
  }

  // Attach credential headers to EVERY htmx request. Registered on document so it
  // works even though this script runs in <head> before <body> exists; htmx
  // events bubble up to document.
  document.addEventListener("htmx:configRequest", function (evt) {
    var h = headers();
    for (var k in h) {
      evt.detail.headers[k] = h[k];
    }
  });

  // ------------------------------------------------------------------
  // Opt-in end-to-end-encrypted sync (DECISIONS.md 2026-07-04 sync entry).
  // The config is encrypted IN THIS BROWSER with a key derived from a user-held
  // sync code, then stored server-side as an opaque blob. The server never sees
  // the code or the plaintext keys. Solves iOS Safari localStorage eviction +
  // cross-device. All crypto is WebCrypto (available on modern iOS Safari).
  // ------------------------------------------------------------------
  var SYNC_KEY = "warroute.sync.v1"; // { code, enabled }
  var B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"; // RFC4648, no padding

  function loadSync() {
    try {
      return JSON.parse(localStorage.getItem(SYNC_KEY)) || {};
    } catch (e) {
      return {};
    }
  }
  function saveSync(s) {
    localStorage.setItem(SYNC_KEY, JSON.stringify(s || {}));
  }

  function enc(str) {
    return new TextEncoder().encode(str);
  }
  function toHex(buf) {
    var b = new Uint8Array(buf), s = "";
    for (var i = 0; i < b.length; i++) {
      s += b[i].toString(16).padStart(2, "0");
    }
    return s;
  }
  function toB64(bytes) {
    var bin = "";
    for (var i = 0; i < bytes.length; i++) {
      bin += String.fromCharCode(bytes[i]);
    }
    return btoa(bin);
  }
  function fromB64(b64) {
    var bin = atob(b64), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) {
      out[i] = bin.charCodeAt(i);
    }
    return out;
  }

  // Generate a strong, human-copyable sync code: 20 random bytes -> base32,
  // grouped as XXXXX-XXXXX-... (32 chars, 160 bits of entropy).
  function genCode() {
    var raw = crypto.getRandomValues(new Uint8Array(20));
    var bits = 0, value = 0, out = "";
    for (var i = 0; i < raw.length; i++) {
      value = (value << 8) | raw[i];
      bits += 8;
      while (bits >= 5) {
        out += B32[(value >>> (bits - 5)) & 31];
        bits -= 5;
      }
    }
    if (bits > 0) {
      out += B32[(value << (5 - bits)) & 31];
    }
    return out.replace(/(.{5})(?=.)/g, "$1-");
  }

  // Normalize a code the user typed (strip spaces/hyphens, uppercase) so it
  // matches what was generated, regardless of how they pasted it.
  function normCode(code) {
    return (code || "").toUpperCase().replace(/[^A-Z2-7]/g, "");
  }

  async function deriveSyncId(code) {
    var digest = await crypto.subtle.digest("SHA-256", enc(normCode(code) + "|warroute-sync-id-v1"));
    return toHex(digest);
  }

  async function deriveKey(code) {
    var material = await crypto.subtle.importKey("raw", enc(normCode(code)), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey(
      { name: "PBKDF2", salt: enc("warroute-sync-key-v1"), iterations: 200000, hash: "SHA-256" },
      material,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"]
    );
  }

  async function encryptConfig(cfg, code) {
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var key = await deriveKey(code);
    var ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv }, key, enc(JSON.stringify(cfg)));
    var ctBytes = new Uint8Array(ct);
    var combined = new Uint8Array(iv.length + ctBytes.length);
    combined.set(iv, 0);
    combined.set(ctBytes, iv.length);
    return toB64(combined);
  }

  async function decryptConfig(blob, code) {
    var combined = fromB64(blob);
    var iv = combined.slice(0, 12);
    var ct = combined.slice(12);
    var key = await deriveKey(code);
    var pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: iv }, key, ct);
    return JSON.parse(new TextDecoder().decode(pt));
  }

  // Encrypt the current config and upload it under the derived sync id.
  async function syncPush() {
    var s = loadSync();
    if (!s.enabled || !s.code) { return; }
    var id = await deriveSyncId(s.code);
    var blob = await encryptConfig(load(), s.code);
    var r = await fetch("/sync/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ciphertext: blob })
    });
    if (!r.ok) { throw new Error("sync push failed (" + r.status + ")"); }
    return (await r.json()).updated_at;
  }

  // Turn on sync: mint a code, remember it, push the current config. Returns the code.
  async function enableSync() {
    var code = genCode();
    saveSync({ code: code, enabled: true });
    await syncPush();
    return code;
  }

  // Restore config from a code (another device / after eviction). Also enables
  // sync on THIS device so it stays backed up.
  async function syncPull(code) {
    var id = await deriveSyncId(code);
    var r = await fetch("/sync/" + id);
    if (r.status === 404) { throw new Error("No config found for that code."); }
    if (!r.ok) { throw new Error("sync fetch failed (" + r.status + ")"); }
    var data = await r.json();
    var cfg = await decryptConfig(data.ciphertext, code);
    save(cfg);
    saveSync({ code: code, enabled: true });
    return cfg;
  }

  // Stop syncing: delete the server copy and forget the code locally. Keys stay
  // in this browser.
  async function disableSync() {
    var s = loadSync();
    if (s.code) {
      try {
        var id = await deriveSyncId(s.code);
        await fetch("/sync/" + id, { method: "DELETE" });
      } catch (e) { /* best effort */ }
    }
    saveSync({});
  }

  // Debounced push used by the settings editor on every config change.
  var _pushTimer = null;
  function syncPushIfEnabled() {
    if (!loadSync().enabled) { return; }
    if (_pushTimer) { clearTimeout(_pushTimer); }
    _pushTimer = setTimeout(function () {
      syncPush().catch(function (e) { console.warn("[WarRoute] sync push failed", e); });
    }, 800);
  }

  // HTML-escape untrusted text before it goes into a Leaflet popup (which treats a
  // string as HTML). Map data - SSIDs, gang names, geocoder labels - is attacker
  // controlled (anyone can name a WiFi network or a gang "<img onerror=...>"), so
  // every popup that builds HTML by concatenation must run its fields through this.
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- Delegated event handlers (CSP: no inline on* attributes) -----------
  // The nonce-based CSP (Eng #36 finding #4) forbids inline event-handler
  // attributes, so templates mark elements with data-action / data-geocode-input
  // and we dispatch here. Delegation on document also survives htmx swaps and the
  // stop-row <template> clones (which the old inline handlers relied on being
  // re-parsed for). The per-page functions (warroute*) are globals defined in the
  // plan / settings inline scripts; call them if present.
  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target.closest("[data-action]") : null;
    if (!t) { return; }
    switch (t.getAttribute("data-action")) {
      case "geocode-select":
        if (window.warrouteSelectGeocode) { window.warrouteSelectGeocode(t); }
        break;
      case "geocode-clear":
        if (window.warrouteClearGeocode) { window.warrouteClearGeocode(t.getAttribute("data-field")); }
        break;
      case "stop-add":
        if (window.warrouteAddStop) { window.warrouteAddStop(); }
        break;
      case "stop-remove":
        if (window.warrouteRemoveStop) { window.warrouteRemoveStop(t); }
        break;
      case "stop-clear":
        if (window.warrouteClearStop) { window.warrouteClearStop(t); }
        break;
    }
  });
  document.addEventListener("input", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches("[data-geocode-input]")) { geocodeInput(el); }
  });

  window.WarRoute = {
    KEY: KEY,
    HEADER_MAP: HEADER_MAP,
    load: load,
    save: save,
    get: get,
    headers: headers,
    fetch: warrouteFetch,
    escapeHtml: escapeHtml,
    geocodeInput: geocodeInput,
    units: units,
    applyUnits: applyUnits,
    hasKeys: function () {
      var c = load();
      return !!(c.wigle_token || c.wdgowars_token || c.ors_key);
    },
    // sync
    loadSync: loadSync,
    syncEnabled: function () { return !!loadSync().enabled; },
    syncCode: function () { return loadSync().code || ""; },
    enableSync: enableSync,
    syncPull: syncPull,
    disableSync: disableSync,
    syncPush: syncPush,
    syncPushIfEnabled: syncPushIfEnabled
  };
})();
