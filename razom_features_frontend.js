/* RAZOM 2.5 extras — inserted into web/index.html by apply_razom_upgrade.py */
let razomMap = null;
let razomMapLayers = [];
let razomMapBounds = null;

function razomClearMap() {
  if (razomMap) { razomMap.remove(); razomMap = null; }
  razomMapLayers = [];
  razomMapBounds = null;
  const el = $('geoMap');
  if (el) el.innerHTML = '';
}

async function razomLoadLeaflet() {
  if (window.L) return window.L;
  if (!window.razomLeafletPromise) {
    window.razomLeafletPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
      script.onload = () => resolve(window.L);
      script.onerror = () => reject(new Error('Не вдалося завантажити карту. Перевірте інтернет.'));
      document.head.appendChild(script);
    });
  }
  return window.razomLeafletPromise;
}

async function razomLoadDuplicates() {
  const box = $('duplicateMatches');
  if (!box) return;
  try {
    const data = await api('/admin/duplicates');
    if (me?.role !== 'admin') return;
    box.innerHTML = data.matches.length
      ? data.matches.map(p => `<article class="queue-item">
          <b>Можливий дубль: заявки №${p.request_a} і №${p.request_b}</b>
          <p class="queue-meta">${safe(p.city)} · ${safe(categories[p.category]||p.category)} · схожість описів ${p.similarity_percent}%</p>
          <p class="muted small">${safe(p.reason)}</p>
          <div class="actions"><button type="button" class="btn btn-outline" data-duplicate-detail="${p.request_a}">Перша заявка</button>
          <button type="button" class="btn btn-outline" data-duplicate-detail="${p.request_b}">Друга заявка</button></div></article>`).join('')
      : '<p class="muted">Потенційних дублікатів за останні 30 днів не знайдено.</p>';
    if (data.truncated) box.innerHTML += '<p class="muted small">Перевірено обмежену кількість найновіших заявок.</p>';
    box.querySelectorAll('[data-duplicate-detail]').forEach(btn => btn.onclick = async () => {
      tab('requestsTab');
      await showDetail(Number(btn.dataset.duplicateDetail));
    });
  } catch (e) { box.textContent = e.message; }
}

async function razomLoadForecast() {
  const box = $('forecastCards');
  if (!box) return;
  try {
    const data = await api('/admin/forecast');
    if (me?.role !== 'admin') return;
    box.innerHTML = `<div class="feature-cards">${data.categories.map(c => `<div class="feature-card">
      <b>${safe(categories[c.category]||c.category)}</b>
      <p>Останні 7 днів: <strong>${c.last_7d}</strong></p>
      <p>Попередні 7 днів: <strong>${c.previous_7d}</strong></p>
      <p>Наступний тиждень: <strong>${c.enough_data ? `≈ ${c.forecast_next_7d}` : 'Недостатньо даних'}</strong></p>
      <p class="muted small">${safe(c.trend)}</p></div>`).join('')}</div>
      <p class="muted small">${safe(data.note)}</p>`;
  } catch (e) { box.textContent = e.message; }
}

/* RAZOM 2.6: evacuation map and settlement-only geocoding */
const razomSettlementCache = new Map();
let razomGeocodeNextAt = 0;
let razomGeoRun = 0;

async function razomGeocodeSettlement(city) {
  // Never send the separate street-address field to a third-party service.
  const name = String(city || '').trim();
  if (!name || /(^|[\s,])(вул\.?|вулиця|буд\.?|будинок|кв\.?|квартира|проспект|провулок|street)(?=[\s,.0-9]|$)/i.test(name)) return null;
  const key = name.toLocaleLowerCase('uk');
  if (razomSettlementCache.has(key)) return razomSettlementCache.get(key);
  const now = Date.now();
  const slot = Math.max(now, razomGeocodeNextAt);
  razomGeocodeNextAt = slot + 1200; // Reserve a slot; max 1 request/s.
  if (slot > now) await new Promise(resolve => setTimeout(resolve, slot - now));
  const query = new URLSearchParams({q: `${name}, Україна`, format: 'jsonv2', limit: '1', countrycodes: 'ua'});
  try {
    const response = await fetch(`https://nominatim.openstreetmap.org/search?${query}`, {
      headers: {'Accept': 'application/json'},
    });
    if (!response.ok) return null;
    const results = await response.json();
    const result = Array.isArray(results) ? results[0] : null;
    const lat = Number(result?.lat), lng = Number(result?.lon);
    const coords = result && Number.isFinite(lat) && Number.isFinite(lng) ? {lat, lng} : null;
    razomSettlementCache.set(key, coords);
    return coords;
  } catch { return null; }
}

async function razomLoadGeography() {
  const info = $('geoInfo');
  const summary = $('geoSummary');
  if (!info || !summary || me?.role !== 'admin') return;
  const run = ++razomGeoRun;
  const filter = $('mapCategory')?.value || 'evacuation';
  info.textContent = 'Завантаження заявок…';
  try {
    const data = await api('/admin/geography');
    if (run !== razomGeoRun || me?.role !== 'admin') return;
    const L = await razomLoadLeaflet();
    if (run !== razomGeoRun || me?.role !== 'admin') return;
    razomClearMap();
    razomMap = L.map('geoMap', {scrollWheelZoom: false});
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
      maxZoom: 18,
    }).addTo(razomMap);
    razomMap.setView([49.0, 31.0], 6);
    const allCities = [...data.points, ...data.unmapped];
    const total = allCities.reduce((sum, p) => sum + Number(p.requests || 0), 0);
    const evacTotal = allCities.reduce((sum, p) => sum + Number(p.evacuation_requests || 0), 0);
    summary.textContent = `Відкритих заявок: ${total}. З них на евакуацію: ${evacTotal}.`;
    const showPoint = p => filter === 'all' ? Number(p.requests || 0) > 0 : Number(p.evacuation_requests || 0) > 0;
    const coords = [];
    const addPoint = point => {
      if (run !== razomGeoRun || me?.role !== 'admin' || !razomMap || !showPoint(point)) return;
      const evac = Number(point.evacuation_requests || 0);
      const totalRequests = Number(point.requests || 0);
      const red = evac > 0;
      const pin = L.circleMarker([point.lat, point.lng], {
        radius: Math.min(25, 13 + Math.sqrt(red ? evac : totalRequests) * 2),
        color: red ? '#a41919' : '#236447',
        fillColor: red ? '#d52b2b' : '#31885a', fillOpacity: 0.92, weight: 2,
      }).addTo(razomMap);
      pin.bindTooltip(String(red ? evac : totalRequests), {
        permanent: true, direction: 'center', className: 'razom-map-count', opacity: 1,
      });
      const ids = (point.evacuation_request_ids || []).filter(id => Number.isInteger(id) && id > 0);
      const buttons = ids.length ? `<div class="razom-map-requests">${ids.slice(0, 15).map(id =>
        `<button type="button" class="btn btn-outline" data-map-request="${id}">Заявка №${id}</button>`).join('')}</div>` : '';
      pin.bindPopup(`<b>${safe(point.city)}</b><p>🚨 Евакуація: <b>${evac}</b><br>Усього відкритих заявок: ${totalRequests}</p>${buttons}<p class="muted small">Позначка показує центр населеного пункту, а не адресу.</p>`);
      pin.on('popupopen', event => {
        const popup = event.popup.getElement();
        popup?.querySelectorAll('[data-map-request]').forEach(button => {
          button.onclick = async () => {
            if (me?.role !== 'admin') return;
            const id = Number(button.dataset.mapRequest);
            if (!Number.isInteger(id)) return;
            razomMap?.closePopup();
            tab('requestsTab');
            await showDetail(id); // Private address is fetched only after authorization.
          };
        });
      });
      coords.push([point.lat, point.lng]);
    };
    const fitPoints = () => {
      if (run !== razomGeoRun || !razomMap) return;
      razomMapBounds = coords;
      if (coords.length === 1) razomMap.setView(coords[0], 9);
      else if (coords.length > 1) razomMap.fitBounds(coords, {padding: [30, 30], maxZoom: 9});
      setTimeout(() => { if (run === razomGeoRun) razomMap?.invalidateSize(); }, 120);
    };
    data.points.forEach(addPoint);
    fitPoints();
    const unresolved = [];
    const missingToResolve = data.unmapped.filter(showPoint);
    const maxLookup = 8; // Avoid hammering public geocoding services.
    if (missingToResolve.length) info.textContent = `Уточнюємо розташування ${Math.min(maxLookup, missingToResolve.length)} населених пунктів…`;
    for (const point of missingToResolve.slice(0, maxLookup)) {
      if (run !== razomGeoRun || me?.role !== 'admin') return;
      const position = await razomGeocodeSettlement(point.city);
      if (run !== razomGeoRun || me?.role !== 'admin') return;
      if (position) { addPoint({...point, ...position}); fitPoints(); }
      else unresolved.push(point.city);
    }
    unresolved.push(...missingToResolve.slice(maxLookup).map(p => p.city));
    if (run !== razomGeoRun || me?.role !== 'admin') return;
    info.textContent = data.note + (unresolved.length ? ` Не вдалося нанести: ${unresolved.join(', ')}. Уточніть область у полі «Населений пункт».` : '');
    if (!coords.length) info.textContent += filter === 'evacuation'
      ? ' Наразі немає відображених заявок на евакуацію. Перевірте також список ненанесених населених пунктів.'
      : ' Наразі немає відкритих заявок для відображення.';
    // Straight-line transport suggestions only, never actual evacuation routes.
    if (filter === 'all') {
      try {
        const logistics = await api('/logistics/matches');
        if (run === razomGeoRun && me?.role === 'admin' && razomMap) {
          logistics.matches.filter(x => !x.same_city && x.from_lat != null && x.to_lat != null)
            .slice(0, 12).forEach(x => {
              L.polyline([[x.from_lat, x.from_lng], [x.to_lat, x.to_lng]], {
                color: '#bd7b22', weight: 2, opacity: 0.65, dashArray: '7,7',
              }).addTo(razomMap).bindPopup(`Можливе перевезення: ${safe(x.resource_city)} → ${safe(x.request_city)}. ${x.distance_km_straight} км по прямій.`);
            });
        }
      } catch { /* Map still works without logistics. */ }
    }
    fitPoints();
  } catch (error) {
    if (run === razomGeoRun) info.textContent = `Не вдалося завантажити мапу: ${error.message}`;
  }
}

async function razomLoadAdminFeatures() {
  if (me?.role !== 'admin') return;
  await Promise.all([razomLoadDuplicates(), razomLoadForecast()]);
}

async function razomLoadLogistics() {
  const box = $('logisticsList');
  if (!box || !me || (me.role !== 'admin' && !(me.role === 'volunteer' && me.is_verified))) return;
  try {
    const data = await api('/logistics/matches');
    if (!me || me.role === 'applicant') return;
    const remote = data.matches.filter(m => !m.same_city);
    box.innerHTML = remote.length
      ? remote.slice(0, 20).map(m => `<article class="queue-item">
        <b>Заявка №${m.request_id} — ${safe(m.request_city)}</b>
        <p class="queue-meta">${safe(m.resource_name)} (${m.resource_quantity} ${safe(m.resource_unit)}) · з міста ${safe(m.resource_city)}</p>
        <p>Приблизно ${m.distance_km_straight} км по прямій · ${safe(m.note)}</p>
        <a class="btn btn-outline" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&amp;origin=${m.from_lat}%2C${m.from_lng}&amp;destination=${m.to_lat}%2C${m.to_lng}&amp;travelmode=driving">Маршрут у Google Maps</a>
        </article>`).join('')
      : '<p class="muted">Поки що немає можливих міжміських перевезень для наявних ресурсів.</p>';
  } catch (e) { box.textContent = e.message; }
}

const razomOriginalLoadSuggestions = loadSuggestions;
loadSuggestions = async function() {
  if (me?.role !== 'volunteer' || !me.is_verified) return;
  try {
    const data = await api('/smart-matches');
    const box = $('suggestionList');
    box.innerHTML = data.matches.length
      ? data.matches.slice(0, 25).map(m => `<article class="queue-item">
        <b>Заявка №${m.request_id}: ${safe(m.request_title)}</b>
        <p class="queue-meta">${safe(m.request_city)} · ${safe(tierLabels[m.priority_tier]||'Пріоритет')} · оцінка відповідності ${m.match_score}</p>
        <p>Підходить ресурс: ${safe(m.resource_name)} (${m.resource_quantity} ${safe(m.resource_unit)})</p>
        <button type="button" class="btn btn-outline" data-smart-claim="${m.request_id}">Взяти заявку</button>
        </article>`).join('')
      : '<p class="muted">Наразі немає доступних заявок, що відповідають вашим ресурсам.</p>';
    box.querySelectorAll('[data-smart-claim]').forEach(btn => btn.onclick = async () => {
      try {
        await api(`/requests/${btn.dataset.smartClaim}/claim`, 'POST');
        notify('Заявку взято в роботу');
        await loadRequests(); await loadSuggestions();
        await showDetail(btn.dataset.smartClaim);
      } catch (e) { notify(e.message, true); }
    });
  } catch { await razomOriginalLoadSuggestions(); }
};

const razomOriginalLoadAnalytics = loadAnalytics;
loadAnalytics = async function() {
  await razomOriginalLoadAnalytics();
  await razomLoadAdminFeatures();
};
const razomOriginalLoadResources = loadResources;
loadResources = async function() {
  await razomOriginalLoadResources();
  await razomLoadLogistics();
};
const razomOriginalClearSensitive = clearSensitive;
clearSensitive = function() {
  razomOriginalClearSensitive();
  for(const id of ['duplicateMatches','forecastCards','geoInfo','logisticsList']) {
    const el = $(id); if(el) el.textContent = '';
  }
  razomGeoRun++;
  razomClearMap();
};
$('refreshMap').onclick = () => { if (me?.role === 'admin') razomLoadGeography(); };
$('mapCategory').onchange = () => { if (me?.role === 'admin') razomLoadGeography(); };
const razomOriginalTab = tab;
tab = function(name) {
  razomOriginalTab(name);
  if (name === 'mapTab' && me?.role === 'admin') {
    razomLoadGeography();
  }
};
