/* Marked Up browser side. Talks only to its own server (/api/...).
 *
 * Two roles live in this one page:
 *   Find        - type a plate (+ share code) and watch where the vehicle is.
 *   My vehicles - enrol a vehicle you own; the phone that rides in it shares
 *                 its position while this page stays open.
 * Owner keys and share codes are kept in this browser's localStorage only.
 */
(function () {
  'use strict';

  const STORE_KEY = 'markedup.mine.v1';
  const TAB_KEY = 'markedup.tab';
  const REFRESH_MS = 15000;      // how often Find re-asks the server
  const MIN_SEND_MS = 5000;      // sharing: never post more often than this...
  const MIN_SEND_METRES = 15;    // ...unless the vehicle moved at least this far

  const state = {
    types: [],
    mine: [],
    watches: {},      // vehicle id -> geolocation watch id
    lastSent: {},     // vehicle id -> {lat, lng, at}
    findTimer: null,
    lastFind: null,   // {plate, code}
    lastFound: null,  // last successful lookup payload
    wakeLock: null,
  };

  // ---- tiny helpers --------------------------------------------------------

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $all = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  function normalizePlate(raw) {
    const compact = String(raw || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
    return /^[A-Z0-9]{2,10}$/.test(compact) ? compact : null;
  }

  function displayPlate(plate) {
    const m = /^([A-Z]+)(\d+)$/.exec(plate) || /^(\d+)([A-Z]+)$/.exec(plate);
    return m ? m[1] + ' ' + m[2] : plate;
  }

  function ago(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 10) return 'just now';
    if (s < 60) return s + ' s ago';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  function typeLabel(id) {
    const found = state.types.find((t) => t.id === id);
    return found ? found.label : id;
  }

  function distanceMetres(a, b) {
    const R = 6371000;
    const toRad = (d) => (d * Math.PI) / 180;
    const dLat = toRad(b.lat - a.lat);
    const dLng = toRad(b.lng - a.lng);
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  let toastTimer = null;
  function toast(message, ms) {
    const el = $('#toast');
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, ms || 3500);
  }

  async function copyText(text, label) {
    try {
      await navigator.clipboard.writeText(text);
      toast(label + ' copied.');
    } catch (err) {
      window.prompt('Copy this ' + label.toLowerCase() + ':', text);
    }
  }

  // ---- server --------------------------------------------------------------

  async function api(method, path, body, token) {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = 'Bearer ' + token;
    let res;
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (err) {
      const e = new Error('Cannot reach the server. Check the connection.');
      e.status = 0;
      throw e;
    }
    let data = {};
    try { data = await res.json(); } catch (err) { /* empty body */ }
    if (!res.ok) {
      const e = new Error(data.message || ('Server error ' + res.status));
      e.status = res.status;
      e.code = data.error;
      throw e;
    }
    return data;
  }

  // An owner call that re-enrols the vehicle from the keys this phone holds if
  // the server lost it (free hosting resets its disk on every deploy).
  async function ownerCall(v, method, path, body) {
    try {
      return await api(method, path, body, v.token);
    } catch (err) {
      if (err.status !== 404 || err.code !== 'unknown_vehicle') throw err;
      await api('POST', '/api/vehicles/restore', {
        id: v.id, plate: v.plate, type: v.type, visibility: v.visibility,
        owner_token: v.token, share_code: v.code,
      });
      return api(method, path, body, v.token);
    }
  }

  // ---- storage -------------------------------------------------------------

  function loadMine() {
    try {
      const list = JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
      return Array.isArray(list) ? list.filter((v) => v && v.id && v.token) : [];
    } catch (err) {
      return [];
    }
  }

  function saveMine() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(state.mine.map((v) => ({
        id: v.id, plate: v.plate, display: v.display, type: v.type,
        visibility: v.visibility, token: v.token, code: v.code,
        wasSharing: !!(v.sharing || v.wasSharing),
      }))));
    } catch (err) { /* private mode: keys live for this page only */ }
  }

  // ---- tabs ----------------------------------------------------------------

  function showTab(name) {
    $all('.tab').forEach((b) => {
      const on = b.dataset.tab === name;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $all('.panel').forEach((p) => {
      const on = p.id === 'tab-' + name;
      p.classList.toggle('is-active', on);
      p.hidden = !on;
    });
    try { localStorage.setItem(TAB_KEY, name); } catch (err) { /* ignore */ }
    if (name !== 'find') stopFollowing();
    else if (state.lastFind && $('#find-follow').checked) scheduleFollow();
  }

  // ---- Find ----------------------------------------------------------------

  function mapSrc(loc) {
    const d = 0.004;
    const bbox = [loc.lng - d * 1.4, loc.lat - d, loc.lng + d * 1.4, loc.lat + d].join(',');
    return 'https://www.openstreetmap.org/export/embed.html?bbox=' + bbox
      + '&layer=mapnik&marker=' + loc.lat + ',' + loc.lng;
  }

  function stopFollowing() {
    clearTimeout(state.findTimer);
    state.findTimer = null;
  }

  function scheduleFollow() {
    stopFollowing();
    state.findTimer = setTimeout(() => {
      if (document.visibilityState === 'visible' && state.lastFind && $('#find-follow').checked) {
        runFind(state.lastFind.plate, state.lastFind.code, true);
      } else {
        scheduleFollow();
      }
    }, REFRESH_MS);
  }

  async function runFind(plate, code, quiet) {
    const box = $('#find-result');
    state.lastFind = { plate, code };
    if (!quiet) {
      box.hidden = false;
      box.innerHTML = '<div class="card"><p class="status muted">Looking up ' + esc(displayPlate(plate)) + '…</p></div>';
    }
    const q = new URLSearchParams({ plate });
    if (code) q.set('code', code);
    try {
      const v = await api('GET', '/api/lookup?' + q.toString());
      renderFound(v);
    } catch (err) {
      state.lastFound = null;
      if (err.status === 404) renderNotFound(plate, err.message);
      else renderFindError(err);
    }
    if ($('#find-follow').checked) scheduleFollow();
  }

  function renderFound(v) {
    const box = $('#find-result');
    box.hidden = false;
    const prev = state.lastFound;
    const loc = v.location;
    const samePlace = prev && prev.plate === v.plate && prev.location && loc
      && prev.location.lat === loc.lat && prev.location.lng === loc.lng;
    state.lastFound = v;

    let statusHtml;
    if (!v.sharing || !loc) {
      statusHtml = '<p class="status muted">Not sharing a location right now.</p>';
    } else {
      const when = loc.stale
        ? 'Last seen ' + ago(loc.age_seconds) + '. The phone may have stopped sharing.'
        : 'Updated ' + ago(loc.age_seconds) + '.';
      const acc = loc.accuracy ? ' Accurate to about ' + Math.round(loc.accuracy) + ' m.' : '';
      statusHtml = '<p class="status ' + (loc.stale ? 'warn' : 'ok') + '">' + esc(when + acc) + '</p>';
    }

    if (samePlace && $('.found', box)) {
      $('.status', box).outerHTML = statusHtml;
      return;
    }

    let placeHtml = '';
    if (v.sharing && loc) {
      placeHtml = '<iframe class="map" title="Map around the vehicle" loading="lazy" referrerpolicy="no-referrer" src="'
        + esc(mapSrc(loc)) + '"></iframe>'
        + '<p class="links"><a href="https://www.google.com/maps?q=' + loc.lat + ',' + loc.lng
        + '" target="_blank" rel="noopener">Open in Google Maps</a> · <span class="mono">'
        + loc.lat.toFixed(5) + ', ' + loc.lng.toFixed(5) + '</span></p>';
    }
    box.innerHTML = '<div class="card found">'
      + '<p class="plate">' + esc(v.display) + '</p>'
      + '<p class="type">' + esc(v.type_label) + (v.visibility === 'public' ? ' · public' : '') + '</p>'
      + statusHtml + placeHtml + '</div>';
  }

  function renderNotFound(plate, message) {
    const box = $('#find-result');
    box.hidden = false;
    const smsBody = encodeURIComponent('LTO VEHICLE ' + plate);
    box.innerHTML = '<div class="card">'
      + '<p class="status warn">' + esc(message) + '</p>'
      + '<details class="tip"><summary>Want to know the vehicle type anyway?</summary>'
      + '<p>The LTO text service answers with the make, model, year and colour registered to a plate, '
      + 'and nothing about the owner. It charges a small fee per text.</p>'
      + '<p><a class="button" href="sms:2600?body=' + smsBody + '">Text LTO VEHICLE ' + esc(plate) + ' to 2600</a></p>'
      + '</details></div>';
  }

  function renderFindError(err) {
    const box = $('#find-result');
    box.hidden = false;
    box.innerHTML = '<div class="card"><p class="status error">' + esc(err.message) + '</p></div>';
  }

  function bindFind() {
    $('#find-form').addEventListener('submit', (ev) => {
      ev.preventDefault();
      const plate = normalizePlate($('#find-plate').value);
      if (!plate) { toast('Enter the plate as letters and digits.'); return; }
      $('#find-plate').value = displayPlate(plate);
      const code = $('#find-code').value.trim().toUpperCase();
      runFind(plate, code, false);
    });
    $('#find-follow').addEventListener('change', () => {
      if ($('#find-follow').checked && state.lastFind) scheduleFollow();
      else stopFollowing();
    });
  }

  // ---- My vehicles ---------------------------------------------------------

  function finderLink(v) {
    const url = new URL(location.pathname, location.origin);
    url.searchParams.set('plate', v.plate);
    if (v.visibility !== 'public') url.searchParams.set('code', v.code);
    return url.toString();
  }

  function phoneLink(v) {
    const payload = { id: v.id, plate: v.plate, type: v.type, visibility: v.visibility, token: v.token, code: v.code };
    return location.origin + location.pathname + '#add=' + encodeURIComponent(JSON.stringify(payload));
  }

  function renderMine() {
    const list = $('#mine-list');
    if (!state.mine.length) {
      list.innerHTML = '<p class="muted">No vehicles yet. Enrol the one you own above.</p>';
      return;
    }
    list.innerHTML = state.mine.map((v) => {
      const who = v.visibility === 'public'
        ? 'anyone who knows the plate can find it'
        : 'share code <b class="mono">' + esc(v.code) + '</b>';
      return '<article class="card vehicle" data-id="' + esc(v.id) + '">'
        + '<p class="plate">' + esc(v.display) + '</p>'
        + '<p class="type">' + esc(typeLabel(v.type)) + ' · ' + who + '</p>'
        + '<p class="status muted" data-status></p>'
        + '<div class="row">'
        + '<button class="primary" data-act="share">' + (v.sharing ? 'Stop sharing' : 'Share location') + '</button>'
        + '<button data-act="finder">Copy finder link</button>'
        + '<button data-act="phone">Copy phone link</button>'
        + '</div>'
        + '<details class="more"><summary>More</summary><div class="row">'
        + '<button data-act="code"' + (v.visibility === 'public' ? ' disabled' : '') + '>New share code</button>'
        + '<button data-act="visibility">' + (v.visibility === 'public' ? 'Require a share code' : 'Make public') + '</button>'
        + '<button class="danger" data-act="remove">Remove</button>'
        + '</div></details>'
        + '</article>';
    }).join('');
    state.mine.forEach(updateStatus);
  }

  function updateStatus(v) {
    const el = document.querySelector('[data-id="' + v.id + '"] [data-status]');
    if (!el) return;
    let text;
    let cls = 'muted';
    if (v.lastError) {
      text = v.lastError;
      cls = 'warn';
    } else if (v.sharing) {
      text = v.lastSentAt
        ? 'Sharing · position sent ' + ago((Date.now() - v.lastSentAt) / 1000) + '.'
        : 'Sharing · waiting for the first GPS fix…';
      cls = 'ok';
    } else if (v.wasSharing) {
      text = 'Sharing stopped when this page was closed. Tap Share location to resume.';
      cls = 'warn';
    } else {
      text = 'Not sharing.';
    }
    el.textContent = text;
    el.className = 'status ' + cls;
  }

  async function keepAwake() {
    if (!('wakeLock' in navigator) || state.wakeLock) return;
    try {
      state.wakeLock = await navigator.wakeLock.request('screen');
      state.wakeLock.addEventListener('release', () => { state.wakeLock = null; });
    } catch (err) { /* not granted: the user keeps the screen on by hand */ }
  }

  function releaseAwake() {
    if (Object.keys(state.watches).length || !state.wakeLock) return;
    state.wakeLock.release().catch(() => {});
    state.wakeLock = null;
  }

  async function sendPosition(v, pos) {
    const point = { lat: pos.coords.latitude, lng: pos.coords.longitude, at: Date.now() };
    const last = state.lastSent[v.id];
    if (last && point.at - last.at < MIN_SEND_MS && distanceMetres(last, point) < MIN_SEND_METRES) return;
    state.lastSent[v.id] = point;
    try {
      await ownerCall(v, 'POST', '/api/vehicles/' + v.id + '/location', {
        lat: point.lat, lng: point.lng,
        accuracy: Number.isFinite(pos.coords.accuracy) ? pos.coords.accuracy : null,
      });
      v.lastSentAt = Date.now();
      v.lastError = null;
    } catch (err) {
      v.lastError = err.message;
      if (err.status === 401 || err.status === 409) stopSharing(v, { silent: true });
    }
    updateStatus(v);
  }

  async function startSharing(v) {
    if (!('geolocation' in navigator)) {
      v.lastError = 'This browser cannot access location.';
      return updateStatus(v);
    }
    if (!window.isSecureContext) {
      v.lastError = 'Location only works over HTTPS (or on localhost).';
      return updateStatus(v);
    }
    v.sharing = true;
    v.wasSharing = false;
    v.lastError = null;
    v.lastSentAt = null;
    delete state.lastSent[v.id];   // the first fix after a (re)start is always sent
    saveMine();
    renderMine();
    state.watches[v.id] = navigator.geolocation.watchPosition(
      (pos) => sendPosition(v, pos),
      (err) => { v.lastError = 'Location error: ' + err.message; updateStatus(v); },
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 },
    );
    keepAwake();
    try {
      await ownerCall(v, 'POST', '/api/vehicles/' + v.id + '/sharing', { sharing: true });
    } catch (err) {
      v.lastError = err.message;
      updateStatus(v);
    }
  }

  async function stopSharing(v, opts) {
    opts = opts || {};
    if (state.watches[v.id] != null) {
      navigator.geolocation.clearWatch(state.watches[v.id]);
      delete state.watches[v.id];
    }
    v.sharing = false;
    v.wasSharing = false;
    delete state.lastSent[v.id];
    saveMine();
    renderMine();
    releaseAwake();
    if (opts.silent) return;
    try {
      await ownerCall(v, 'POST', '/api/vehicles/' + v.id + '/sharing', { sharing: false });
      toast('Stopped. The server forgot the position.');
    } catch (err) {
      v.lastError = err.message;
      updateStatus(v);
    }
  }

  async function removeVehicle(v) {
    if (!window.confirm('Remove ' + v.display + ' from this server and this phone?')) return;
    await stopSharing(v, { silent: true });
    try {
      await api('DELETE', '/api/vehicles/' + v.id, undefined, v.token);
    } catch (err) {
      if (err.status !== 404) { toast(err.message); return; }
    }
    state.mine = state.mine.filter((x) => x.id !== v.id);
    saveMine();
    renderMine();
    toast(v.display + ' removed.');
  }

  async function rotateCode(v) {
    try {
      const data = await ownerCall(v, 'POST', '/api/vehicles/' + v.id + '/code', {});
      v.code = data.share_code;
      saveMine();
      renderMine();
      toast('New share code: ' + v.code);
    } catch (err) { toast(err.message); }
  }

  async function toggleVisibility(v) {
    const visibility = v.visibility === 'public' ? 'code' : 'public';
    try {
      const data = await ownerCall(v, 'PATCH', '/api/vehicles/' + v.id, { visibility });
      v.visibility = data.visibility;
      saveMine();
      renderMine();
      toast(visibility === 'public' ? 'Anyone who knows the plate can find it now.' : 'A share code is required again.');
    } catch (err) { toast(err.message); }
  }

  function bindMine() {
    $('#enrol-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const plate = normalizePlate($('#enrol-plate').value);
      if (!plate) { toast('Enter the plate as letters and digits.'); return; }
      const body = { plate, type: $('#enrol-type').value, visibility: $('#enrol-visibility').value };
      try {
        const data = await api('POST', '/api/vehicles', body);
        state.mine.push({
          id: data.id, plate: data.plate, display: data.display, type: data.type,
          visibility: data.visibility, token: data.owner_token, code: data.share_code, sharing: false,
        });
        saveMine();
        renderMine();
        $('#enrol-plate').value = '';
        toast(data.display + ' enrolled. Share code ' + data.share_code + '.', 6000);
      } catch (err) {
        toast(err.message, 6000);
      }
    });

    $('#mine-list').addEventListener('click', (ev) => {
      const button = ev.target.closest('button[data-act]');
      if (!button) return;
      const card = button.closest('[data-id]');
      const v = state.mine.find((x) => x.id === card.dataset.id);
      if (!v) return;
      switch (button.dataset.act) {
        case 'share': return v.sharing ? stopSharing(v) : startSharing(v);
        case 'finder': return copyText(finderLink(v), 'Finder link');
        case 'phone': return copyText(phoneLink(v), 'Phone link');
        case 'code': return rotateCode(v);
        case 'visibility': return toggleVisibility(v);
        case 'remove': return removeVehicle(v);
        default: return undefined;
      }
    });
  }

  async function importFromHash(encoded) {
    let payload;
    try { payload = JSON.parse(decodeURIComponent(encoded)); } catch (err) { return false; }
    if (!payload || !payload.id || !payload.token || !payload.plate) return false;
    history.replaceState(null, '', location.pathname + location.search);
    if (state.mine.some((v) => v.id === payload.id)) { showTab('mine'); return true; }
    const v = {
      id: payload.id, plate: payload.plate, display: displayPlate(payload.plate), type: payload.type || 'other',
      visibility: payload.visibility === 'public' ? 'public' : 'code', token: payload.token, code: payload.code || '',
      sharing: false,
    };
    try {
      const data = await ownerCall(v, 'GET', '/api/vehicles/' + v.id);
      v.type = data.type;
      v.visibility = data.visibility;
      v.display = data.display;
    } catch (err) {
      toast('Could not add that vehicle: ' + err.message, 6000);
      return false;
    }
    state.mine.push(v);
    saveMine();
    renderMine();
    showTab('mine');
    toast(v.display + ' added to this phone.');
    return true;
  }

  // ---- start ---------------------------------------------------------------

  async function loadTypes() {
    try {
      const data = await api('GET', '/api/types');
      state.types = data.types;
    } catch (err) {
      state.types = [{ id: 'other', label: 'Other' }];
      toast(err.message, 6000);
    }
    $('#enrol-type').innerHTML = state.types
      .map((t) => '<option value="' + esc(t.id) + '">' + esc(t.label) + '</option>').join('');
  }

  async function init() {
    $all('.tab').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
    bindFind();
    bindMine();
    await loadTypes();
    state.mine = loadMine();
    renderMine();

    const params = new URLSearchParams(location.search);
    let shown = false;
    if (location.hash.startsWith('#add=')) {
      shown = await importFromHash(location.hash.slice(5));
    }
    if (!shown && params.get('plate')) {
      const plate = normalizePlate(params.get('plate'));
      if (plate) {
        $('#find-plate').value = displayPlate(plate);
        $('#find-code').value = (params.get('code') || '').toUpperCase();
        showTab('find');
        runFind(plate, $('#find-code').value, false);
        shown = true;
      }
    }
    if (!shown) {
      let saved = 'find';
      try { saved = localStorage.getItem(TAB_KEY) || 'find'; } catch (err) { /* ignore */ }
      showTab(saved === 'mine' ? 'mine' : 'find');
    }

    setInterval(() => state.mine.forEach(updateStatus), 5000);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && Object.keys(state.watches).length) keepAwake();
    });
  }

  init();
})();
