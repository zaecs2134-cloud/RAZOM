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

async function razomLoadGeography() {
  const info = $('geoInfo');
  if (!info) return;
  try {
    const data = await api('/admin/geography');
    if (me?.role !== 'admin') return;
    info.textContent = data.note + (data.unmapped.length
      ? ` Не нанесено на карту: ${data.unmapped.map(p=>`${p.city} (${p.requests} заявок)`).join(', ')}.`
      : '');
    if (!data.points.length) { $('geoMap').textContent = 'Немає заявок у відомих містах для відображення на карті.'; return; }
    const L = await razomLoadLeaflet();
    if (me?.role !== 'admin') return;
    razomClearMap();
    razomMap = L.map('geoMap', {scrollWheelZoom: false});
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
      maxZoom: 18,
    }).addTo(razomMap);
    const coords = [];
    data.points.forEach(point => {
      const pin = L.circleMarker([point.lat, point.lng], {
        radius: Math.min(21, 6 + Math.sqrt(point.requests)*3), color: '#28684c',
        fillColor: '#b9dfb9', fillOpacity: .78, weight: 2,
      }).addTo(razomMap);
      pin.bindPopup(`<b>${safe(point.city)}</b><p>Відкритих заявок: ${point.requests}<br>Позицій ресурсів: ${point.resources}</p>`);
      coords.push([point.lat, point.lng]);
    });
    if (coords.length === 1) razomMap.setView(coords[0], 9);
    else razomMap.fitBounds(coords, {padding: [25, 25], maxZoom: 9});
    razomMapBounds = coords;
    // Routes are only candidate transfers between city centres, not actual road paths.
    try {
      const logistics = await api('/logistics/matches');
      if (me?.role === 'admin' && razomMap) {
        logistics.matches.filter(x => !x.same_city && x.from_lat != null && x.to_lat != null)
          .slice(0, 12).forEach(x => {
            const line = L.polyline([[x.from_lat, x.from_lng], [x.to_lat, x.to_lng]], {
              color: '#bd7b22', weight: 2, opacity: .65, dashArray: '7,7',
            }).addTo(razomMap);
            line.bindPopup(`Можливе перевезення: ${safe(x.resource_city)} → ${safe(x.request_city)}. ${x.distance_km_straight} км по прямій.`);
          });
      }
    } catch { /* Map still useful if logistic endpoint is unavailable. */ }
    setTimeout(() => razomMap?.invalidateSize(), 200);
  } catch (e) { info.textContent = e.message; }
}

async function razomLoadAdminFeatures() {
  if (me?.role !== 'admin') return;
  await Promise.all([razomLoadDuplicates(), razomLoadForecast(), razomLoadGeography()]);
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
  razomClearMap();
};
const razomOriginalTab = tab;
tab = function(name) {
  razomOriginalTab(name);
  if (name === 'analyticsTab') setTimeout(() => {
    if (!razomMap) return;
    razomMap.invalidateSize();
    if (razomMapBounds?.length > 1) razomMap.fitBounds(razomMapBounds, {padding: [25,25], maxZoom: 9});
  }, 200);
};
