// The original dashboard's contacts, sheet uploads and call history. The single call (phone or browser test)
// moved to the new dashboard's Make a call dialog (frontend/src/components/make-call).
const $ = (id) => document.getElementById(id);

function notify(text) { if (window.toast) window.toast(text); }

const campaignId = () => (window.currentCampaignId ? window.currentCampaignId() : null);
const CALLBACK = ['no_answer', 'busy', 'dropped', 'call_later'];

// Every contact of every campaign: the open campaign's list shows its own, and the Call back count in
// the sidebar covers all campaigns.
async function loadContacts() {
  const rows = await fetch('/api/contacts').then(r => r.json());
  const cid = campaignId();
  if (cid) {
    const mine = rows.filter(r => r.campaign_id === cid);
    $('contactcount').textContent = mine.length ? `${mine.length} on the list` : 'none yet';
    if (window.renderContacts) window.renderContacts(mine);
    if (window.loadCampaign) window.loadCampaign();
  }
  const cb = rows.filter(r => CALLBACK.includes(r.status)).length;
  $('nav-callback').textContent = cb ? String(cb) : '';
}
window.loadContacts = loadContacts;

// ---- history filters: campaign + dates ----
// Days are Indian dates (YYYY-MM-DD): the server counts days in Indian time, whatever this PC's clock says.
const istDay = (back = 0) => new Date(Date.now() + 330 * 60000 - back * 86400000).toISOString().slice(0, 10);
const fmtDate = (day, year = true) => new Date(day + 'T00:00:00Z').toLocaleDateString('en-IN',
  { timeZone: 'UTC', day: 'numeric', month: 'short', ...(year ? { year: 'numeric' } : {}) });
const addMonths = (ym, n) => new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1)).toISOString().slice(0, 7);
const PRESETS = { '': 'All dates', today: 'Today', yesterday: 'Yesterday', 7: 'Last 7 days', 30: 'Last 30 days', month: 'This month' };

// A preset is worked out afresh on every refresh, so "Today" moves on at midnight; 'custom' keeps its days.
let histDates = { preset: '', from: '', to: '' };
function presetRange(p) {
  const t = istDay();
  if (p === 'today') return [t, t];
  if (p === 'yesterday') return [istDay(1), istDay(1)];
  if (p === '7' || p === '30') return [istDay(Number(p) - 1), t];
  if (p === 'month') return [t.slice(0, 8) + '01', t];
  return ['', ''];
}
// [from, to], both days included; '' means all dates.
const historyDates = () => histDates.preset === 'custom' ? [histDates.from, histDates.to] : presetRange(histDates.preset);
const rangeText = (from, to) => !from ? 'All dates' : from === to ? fmtDate(from)
  : `${fmtDate(from, from.slice(0, 4) !== to.slice(0, 4))} – ${fmtDate(to)}`;

let callsSeq = 0;
async function loadCalls() {
  const seq = ++callsSeq;
  const q = new URLSearchParams();
  const camp = $('hist-camp').value;
  const [from, to] = historyDates();
  if (camp) q.set('campaign_id', camp);
  if (from) q.set('day_from', from);
  if (to) q.set('day_to', to);
  const rows = await fetch(`/api/calls?${q}`).then(r => r.json());
  if (seq !== callsSeq) return;   // the filter changed while this was loading; the newer answer wins
  if (window.renderCalls) window.renderCalls(rows);
  $('nocalls').textContent = camp || from ? 'No calls match these filters.' : 'No calls yet.';
  const cap = from ? 1000 : 100;
  $('hist-hint').textContent = rows.length >= cap ? `Showing the latest ${cap} calls.` : `${rows.length} call${rows.length === 1 ? '' : 's'}`;
  if (window.loadStats) window.loadStats();
}
window.loadCalls = loadCalls;

// The date picker: presets on the left, two months on the right (one on a phone).
// Click one day for that day, or a first and a last day for a range, then Apply.
const dp = { a: '', b: '', hover: '', view: '' };   // a, b: the days picked; view: the first month shown (YYYY-MM)
const monthsShown = () => matchMedia('(max-width: 760px)').matches ? 1 : 2;

function paintRange() {
  const [lo, hi] = dp.a && !dp.b && dp.hover ? [dp.a, dp.hover].sort() : [dp.a, dp.b || dp.a];
  for (const b of $('dp-months').querySelectorAll('[data-day]')) {
    const d = b.dataset.day, on = !!lo && d >= lo && d <= hi;
    b.classList.toggle('start', on && d === lo);
    b.classList.toggle('end', on && d === hi);
    b.classList.toggle('mid', on && d > lo && d < hi);
    b.classList.toggle('range', on && lo !== hi);
    b.setAttribute('aria-pressed', String(on));
  }
  const n = lo ? Math.round((Date.parse(hi) - Date.parse(lo)) / 86400000) + 1 : 0;
  $('dp-pick').textContent = !dp.a ? 'Click a day, or a first and a last day.'
    : !dp.b ? `${fmtDate(dp.a)}. Click another day for a range, or Apply.`
    : `${rangeText(lo, hi)}${n > 1 ? ` (${n} days)` : ''}`;
  $('dp-apply').disabled = !dp.a;
}

function renderPicker() {
  const today = istDay();
  let html = '';
  for (let i = 0; i < 2; i++) {
    const ym = addMonths(dp.view, i);
    const first = new Date(ym + '-01T00:00:00Z');
    const lead = (first.getUTCDay() + 6) % 7;   // weeks start on Monday
    const days = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
    let cells = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].map((d) => `<span class="dow">${d}</span>`).join('') + '<span></span>'.repeat(lead);
    for (let n = 1; n <= days; n++) {
      const day = `${ym}-${String(n).padStart(2, '0')}`;
      cells += `<button type="button" data-day="${day}" class="${day === today ? 'today' : ''}" aria-label="${fmtDate(day)}"${day > today ? ' disabled' : ''}>${n}</button>`;
    }
    html += `<div class="dp-month"><h4>${first.toLocaleDateString('en-IN', { timeZone: 'UTC', month: 'long', year: 'numeric' })}</h4><div class="dp-grid">${cells}</div></div>`;
  }
  $('dp-months').innerHTML = html;
  $('dp-next').disabled = addMonths(dp.view, monthsShown() - 1) >= today.slice(0, 7);
  for (const b of $('dp-presets').children) b.setAttribute('aria-pressed', String(histDates.preset !== 'custom' && b.dataset.preset === histDates.preset));
  paintRange();
}

function openPicker() {
  [dp.a, dp.b] = historyDates();
  dp.hover = '';
  dp.view = addMonths((dp.b || istDay()).slice(0, 7), 1 - monthsShown());   // the chosen (or current) month on the right
  $('dp-pop').hidden = false;
  $('dp-btn').setAttribute('aria-expanded', 'true');
  renderPicker();
}
function closePicker() {
  $('dp-pop').hidden = true;
  $('dp-btn').setAttribute('aria-expanded', 'false');
}
function setHistDates(next) {
  histDates = next;
  const [from, to] = historyDates();
  $('dp-label').textContent = next.preset === 'custom' ? rangeText(from, to) : PRESETS[next.preset];
  $('dp-btn').classList.toggle('on', !!from);
  closePicker();
  loadCalls();
}

$('dp-btn').onclick = () => ($('dp-pop').hidden ? openPicker() : closePicker());
$('dp-prev').onclick = () => { dp.view = addMonths(dp.view, -1); renderPicker(); };
$('dp-next').onclick = () => { dp.view = addMonths(dp.view, 1); renderPicker(); };
$('dp-cancel').onclick = closePicker;
$('dp-apply').onclick = () => setHistDates({ preset: 'custom', from: dp.a, to: dp.b || dp.a });
$('dp-presets').addEventListener('click', (e) => {
  const b = e.target.closest('[data-preset]');
  if (b) setHistDates({ preset: b.dataset.preset, from: '', to: '' });
});
$('dp-months').addEventListener('click', (e) => {
  const day = e.target.closest('[data-day]')?.dataset.day;
  if (!day) return;
  if (!dp.a || dp.b) { dp.a = day; dp.b = ''; }        // start a new pick
  else if (day < dp.a) { dp.b = dp.a; dp.a = day; }   // picked backwards: turn it round
  else dp.b = day;
  paintRange();
});
$('dp-months').addEventListener('mouseover', (e) => {
  const day = e.target.closest('[data-day]')?.dataset.day;
  if (day && day !== dp.hover && dp.a && !dp.b) { dp.hover = day; paintRange(); }
});
$('dp-months').addEventListener('dblclick', (e) => {   // double-click: just that day, straight away
  const day = e.target.closest('[data-day]')?.dataset.day;
  if (day) setHistDates({ preset: 'custom', from: day, to: day });
});
document.addEventListener('mousedown', (e) => { if (!$('dp-pop').hidden && !e.target.closest('.datepick')) closePicker(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('dp-pop').hidden) { closePicker(); $('dp-btn').focus(); } });

// ---- upload: preview first, import on confirm ----
let previewFilter = 'all';
let previewData = null;
const ISSUE = {
  ok: ['good', 'check', 'OK'],
  no_phone: ['critical', 'alert', 'No valid phone'],
  duplicate: ['warning', 'alert', 'Repeated in sheet'],
  on_list: ['serious', 'clock', 'Already in this campaign'],
  do_not_call: ['critical', 'alert', 'Asked not to be called'],
};
const COL_LABEL = { order_id: 'Order / booking ID', customer_name: 'Customer name', phone: 'Phone', product: 'Product / puja', quantity: 'Qty', amount: 'Amount',
  payment_mode: 'Payment', address: 'Address', delivery_date: 'Delivery date', language: 'Language', notes: 'Notes' };

async function previewSheet() {
  const f = $('file').files[0];
  if (!f) return;
  const cid = campaignId();
  if (!cid) { notify('Open a campaign first.'); return; }
  const fd = new FormData();
  fd.append('file', f);
  $('pv-summary').textContent = 'Reading the file…';
  $('preview').hidden = false;
  const r = await fetch(`/api/campaigns/${cid}/preview`, { method: 'POST', body: fd });
  const j = await r.json().catch(() => ({}));
  if (r.status === 404) { $('preview').hidden = true; notify('The server is running older code: close the run_phone.bat / run.bat window and start it again, then reload this page.'); return; }
  if (!r.ok) { $('preview').hidden = true; notify(j.detail || 'Could not read the file.'); return; }
  previewData = j;
  previewFilter = 'all';
  renderPreview();
  $('preview').scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function renderPreview() {
  const j = previewData;
  if (!j) return;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const c = j.counts;
  const willImport = c.ok + (!$('pv-skipdup').checked ? c.duplicate + c.on_list : 0) + (!$('pv-skipnophone').checked ? c.no_phone : 0);
  $('pv-summary').textContent = `${j.total} row${j.total === 1 ? '' : 's'} in the sheet · ${willImport} will be added`;
  $('upload').textContent = `Import ${willImport} contact${willImport === 1 ? '' : 's'}`;
  $('upload').disabled = willImport === 0;
  $('pv-columns').innerHTML = `<span class="hint">Columns read:</span>`
    + j.columns.map((k) => `<span class="badge good">${esc(COL_LABEL[k] || k)}</span>`).join('')
    + j.extras.map((x) => `<span class="badge good">${esc(x.label)}</span>`).join('');
  const warn = [];
  if (j.missing.length) warn.push(`Missing column${j.missing.length > 1 ? 's' : ''} this kind of call needs: ${j.missing.join(', ')}. The agent will have nothing to say for ${j.missing.length > 1 ? 'those' : 'that'}.`);
  if (c.no_phone) warn.push(`${c.no_phone} row${c.no_phone > 1 ? 's have' : ' has'} no valid 10-digit phone.`);
  if (c.duplicate) warn.push(`${c.duplicate} row${c.duplicate > 1 ? 's repeat' : ' repeats'} a number used earlier in the sheet.`);
  if (c.on_list) warn.push(`${c.on_list} number${c.on_list > 1 ? 's are' : ' is'} already in this campaign.`);
  if (c.do_not_call) warn.push(`${c.do_not_call} number${c.do_not_call > 1 ? 's' : ''} asked not to be called again and will be left out.`);
  $('pv-warn').hidden = !warn.length;
  $('pv-warn').textContent = warn.join(' ');
  const chip = (key, label, tone, n) => `<button class="${tone}" data-pv="${key}" aria-pressed="${previewFilter === key}">${label} <b>${n}</b></button>`;
  $('pv-chips').innerHTML = chip('all', 'All rows', '', j.total) + Object.entries(ISSUE).filter(([k]) => c[k]).map(([k, v]) => chip(k, v[2], v[0], c[k])).join('');
  const rows = j.rows.filter((r) => previewFilter === 'all' || r.issue === previewFilter);
  $('pv-rows').innerHTML = rows.map((r) => {
    const v = ISSUE[r.issue] || ISSUE.ok;
    const badge = window.outcomeBadge ? `<span class="badge ${v[0]}">${v[2]}</span>` : v[2];
    // Everything else on the row, labelled, so the user sees exactly what the agent will be told.
    const fixed = Object.entries(COL_LABEL).filter(([k]) => !['customer_name', 'phone'].includes(k) && r[k]).map(([k, l]) => `${l}: ${r[k]}`);
    const extra = Object.values(r.extra || {}).map((x) => `${x.label}: ${x.value}`);
    return `<tr>
      <td class="num"><small>${r.row}</small></td><td>${badge}</td>
      <td><b>${esc(r.customer_name)}</b></td><td class="num">${esc(r.phone)}</td>
      <td><small>${esc([...fixed, ...extra].join(' · '))}</small></td>
    </tr>`;
  }).join('') || '<tr><td colspan="5"><small>No rows in this group.</small></td></tr>';
  $('pv-more').textContent = j.total > j.rows.length ? `Showing the first ${j.rows.length} of ${j.total} rows; all of them will be imported.` : '';
}

function closePreview() {
  $('preview').hidden = true;
  previewData = null;
  $('file').value = '';
  $('filename').textContent = 'Upload the Excel sheet';
}
window.closePreview = closePreview;

async function upload() {
  const f = $('file').files[0];
  const cid = campaignId();
  if (!f || !cid) return alert('Choose an .xlsx file first');
  const fd = new FormData();
  fd.append('file', f);
  const qs = new URLSearchParams({ skip_duplicates: $('pv-skipdup').checked, skip_no_phone: $('pv-skipnophone').checked });
  $('upload').disabled = true;
  const r = await fetch(`/api/campaigns/${cid}/upload?${qs}`, { method: 'POST', body: fd });
  const j = await r.json().catch(() => ({}));
  $('upload').disabled = false;
  if (!r.ok) return alert(j.detail || 'Upload failed');
  notify(`Added ${j.imported} contact${j.imported === 1 ? '' : 's'}` + (j.skipped ? ` · ${j.skipped} skipped` : '') + '. Next: start calling (step 2).');
  closePreview();
  await loadContacts();
  if (window.loadCampaigns) window.loadCampaigns();
  $('campaign').scrollIntoView({ block: 'start', behavior: 'smooth' });
}

async function clearContacts() {
  const cid = campaignId();
  if (!cid || !confirm('Delete every contact in this campaign? Call history keeps its records.')) return;
  const r = await fetch(`/api/campaigns/${cid}/contacts`, { method: 'DELETE' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return notify(j.detail || 'Could not clear the list.');
  await loadContacts();
  if (window.loadCampaigns) window.loadCampaigns();
}

$('upload').onclick = upload;
$('pv-cancel').onclick = closePreview;
$('file').addEventListener('change', () => { if ($('file').files[0]) previewSheet(); });
for (const id of ['pv-skipdup', 'pv-skipnophone']) $(id).addEventListener('change', renderPreview);
$('pv-chips').addEventListener('click', (e) => { const b = e.target.closest('[data-pv]'); if (b) { previewFilter = b.dataset.pv; renderPreview(); } });
$('clear').onclick = clearContacts;
$('clearcalls').onclick = async () => {
  if (!confirm('Delete the whole call history? Transcripts and outcomes will be lost. Contacts are kept.')) return;
  await fetch('/api/calls', { method: 'DELETE' });
  await loadCalls();
  await loadContacts();
};
// Phone calls end outside this page, so keep the history fresh automatically.
// Skips while a transcript is open so it does not collapse under you.
setInterval(() => { if (!document.querySelector('#calls details[open]')) { loadCalls(); loadContacts(); } }, 5000);
fetch('/api/config').then(r => r.json()).then(c => {
  window.BUILD = c.build;   // sent with every call this page starts; a restarted server refuses an old page
  let savedVoice = c.voice;
  try { savedVoice = localStorage.getItem('voice') || c.voice; localStorage.removeItem('model'); } catch (e) { /* ignore */ }
  const sarvamVoices = c.sarvam_voices || {};
  for (const [name, gender] of Object.entries(c.voices)) {
    for (const id of ['camp-voice', 'auto-voice']) {
      const o = document.createElement('option');
      o.value = name; o.textContent = `${name} (${gender})`;
      o.dataset.gender = gender;
      o.dataset.sarvam = name in sarvamVoices ? '1' : '';
      if (name === savedVoice) o.selected = true;
      $(id).appendChild(o);
    }
  }
  // The agent: Sarvam, ChatGPT or Gemini. A campaign shows the agent it last ran with (dashboard.js puts it
  // in data-want), otherwise the first (config.AGENT_LABELS).
  const labels = c.model_labels || {};
  for (const id of ['camp-model', 'auto-model']) {
    const want = $(id).dataset.want || c.model;
    for (const m of c.models) {
      const o = document.createElement('option');
      o.value = m; o.textContent = labels[m] || m;
      if (m === want) o.selected = true;
      $(id).appendChild(o);
    }
  }
  // Each voice list shows the voices its agent speaks in: Sarvam its own (Priya, Varun), Gemini and ChatGPT
  // Gemini's list. A voice the agent cannot use moves to a shown one of the same gender. campaigns.js and
  // auto.js set saved settings without a change event, so they call syncVoices after.
  const VOICE_FOR_AGENT = { 'camp-voice': 'camp-model', 'auto-voice': 'auto-model' };
  window.syncVoices = () => {
    for (const [vid, mid] of Object.entries(VOICE_FOR_AGENT)) {
      const sel = $(vid), sarvam = ($(mid).value || '').startsWith('sarvam');
      for (const o of sel.options) o.hidden = (o.dataset.sarvam === '1') !== sarvam;
      const cur = sel.selectedOptions[0];
      if (cur && cur.hidden) {
        const shown = [...sel.options].filter((o) => !o.hidden);
        const same = shown.find((o) => o.dataset.gender === cur.dataset.gender) || shown[0];
        if (same) sel.value = same.value;
      }
    }
  };
  for (const [vid, mid] of Object.entries(VOICE_FOR_AGENT)) {
    $(mid).addEventListener('change', window.syncVoices);
    $(vid).addEventListener('focus', window.syncVoices);
  }
  window.syncVoices();
  // Language: the sheet has no language column, so the owner picks it here. Remembered, and the
  // two selects (campaign and single call) stay in step.
  let savedLang = 'hinglish';
  try { savedLang = localStorage.getItem('language') || savedLang; } catch (e) { /* ignore */ }
  const LANG_LABELS = { hinglish: 'Hinglish (Hindi + English)', hindi: 'Hindi', english: 'English' };
  for (const id of ['camp-lang', 'auto-lang']) {
    for (const l of c.languages || ['hinglish', 'hindi', 'english']) {
      const o = document.createElement('option');
      o.value = l; o.textContent = LANG_LABELS[l] || l;
      if (l === savedLang) o.selected = true;
      $(id).appendChild(o);
    }
    $(id).addEventListener('change', () => {
      const v = $(id).value;
      try { localStorage.setItem('language', v); } catch (e) { /* ignore */ }
    });
  }
});
loadContacts();
loadCalls();

// --- Phone line status, for the sidebar badge. ---
async function loadPlivo() {
  const st = await fetch('/api/plivo/status').then(r => r.json());
  if (window.renderLineBadge) window.renderLineBadge(st);
}
// Add one contact by hand: the fields are the open campaign's sheet columns (campaigns.js draws them).
$('m_add').onclick = async () => {
  const cid = campaignId();
  if (!cid) return;
  const row = {};
  for (const f of $('m-fields').querySelectorAll('[data-m]')) row[f.dataset.m] = f.value.trim();
  if ($('m_lang') && $('m_lang').value) row.language = $('m_lang').value;
  const r = await fetch(`/api/campaigns/${cid}/contacts`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(row) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return alert(j.detail || 'Could not add the contact');
  for (const f of $('m-fields').querySelectorAll('[data-m]')) f.value = '';
  notify('Contact added.');
  await loadContacts();
  if (window.loadCampaigns) window.loadCampaigns();
};
loadPlivo();
