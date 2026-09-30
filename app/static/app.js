// Browser test client: mic -> WebSocket -> server -> Gemini Live -> WebSocket -> speaker.
const $ = (id) => document.getElementById(id);
const OUT_RATE = 24000;

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let playhead = 0;          // when the next audio chunk should start (ctx time)
let scheduled = [];        // sources currently scheduled, for barge-in cancel
let agentLine = null;
let customerLine = null;
let chunksSent = 0;

function sendTyped() {
  const t = $('typed').value.trim();
  if (!t || !ws || ws.readyState !== 1) return;
  stopPlayback();
  ws.send(JSON.stringify({ type: 'text', text: t }));
  log('Customer (typed): ' + t, 'customer');
  $('typed').value = '';
}

function log(text, cls = '') {
  const el = document.createElement('div');
  el.className = 'line ' + cls;
  el.textContent = text;
  $('transcript').appendChild(el);
  $('transcript').scrollTop = $('transcript').scrollHeight;
  return el;
}

function setStatus(text) { $('status').textContent = text; }
// For results of one-off actions: the status line sits inside the console sheet, so also show a toast.
function notify(text) { setStatus(text); if (window.toast) window.toast(text); }

let contactRows = [];
function syncDialTo() {
  const c = contactRows.find(r => String(r.id) === $('contact').value);
  $('dialto').value = c ? (c.phone || '') : '';
}

const campaignId = () => (window.currentCampaignId ? window.currentCampaignId() : null);
const CALLBACK = ['no_answer', 'busy', 'dropped', 'call_later'];

// Every contact of every campaign: the open campaign's list shows its own, the Single call page
// offers all of them, and the Call back count in the sidebar covers all campaigns.
async function loadContacts() {
  const rows = await fetch('/api/contacts').then(r => r.json());
  contactRows = rows;
  const cid = campaignId();
  if (cid) {
    const mine = rows.filter(r => r.campaign_id === cid);
    $('contactcount').textContent = mine.length ? `${mine.length} on the list` : 'none yet';
    if (window.renderContacts) window.renderContacts(mine);
    if (window.loadCampaign) window.loadCampaign();
  }
  const cb = rows.filter(r => CALLBACK.includes(r.status)).length;
  $('nav-callback').textContent = cb ? String(cb) : '';
  const sel = $('contact');
  const chosen = sel.value;   // the list refreshes every few seconds during a run; keep the pick
  sel.innerHTML = '';
  if (!rows.length) {
    sel.innerHTML = '<option value="">No contacts yet. Create a campaign and upload its sheet.</option>';
    return;
  }
  for (const r of rows) {
    const o = document.createElement('option');
    o.value = r.id;
    o.textContent = [r.campaign_name || 'no campaign', r.customer_name || '-', r.phone || 'no phone', r.product || r.order_id || '', r.status].filter(Boolean).join(' | ');
    if (String(r.id) === chosen) o.selected = true;
    sel.appendChild(o);
  }
  if (sel.value !== chosen) syncDialTo();
}
window.loadContacts = loadContacts;

async function loadCalls() {
  const camp = $('hist-camp').value;
  const rows = await fetch(camp ? `/api/calls?campaign_id=${camp}` : '/api/calls').then(r => r.json());
  if (window.renderCalls) window.renderCalls(rows);
  if (window.loadStats) window.loadStats();
}
window.loadCalls = loadCalls;

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

function playChunk(arrayBuffer) {
  const int16 = new Int16Array(arrayBuffer);
  const f32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 0x8000;
  const buf = audioCtx.createBuffer(1, f32.length, OUT_RATE);
  buf.getChannelData(0).set(f32);
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  const now = audioCtx.currentTime;
  if (playhead < now) playhead = now + 0.02;
  src.start(playhead);
  playhead += buf.duration;
  scheduled.push(src);
  src.onended = () => { scheduled = scheduled.filter(s => s !== src); };
}

function stopPlayback() {
  for (const s of scheduled) { try { s.stop(); } catch (e) { /* ignore */ } }
  scheduled = [];
  playhead = 0;
}

async function startCall() {
  const contactId = $('contact').value;
  if (!contactId) return alert('Pick a contact');
  $('start').disabled = true;
  $('hangup').disabled = false;
  if (window.openSheet) window.openSheet('console');
  $('transcript').innerHTML = '';
  agentLine = customerLine = null;

  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  await audioCtx.audioWorklet.addModule('/static/pcm-processor.js');
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
  } catch (err) {
    setStatus('Microphone blocked. Open this page in Chrome or Edge and click Allow when it asks for the microphone.');
    log('Microphone permission was denied, so the call was not started.', 'error');
    $('start').disabled = false;
    $('hangup').disabled = true;
    try { audioCtx.close(); } catch (e) { /* ignore */ }
    audioCtx = null;
    return;
  }
  const source = audioCtx.createMediaStreamSource(micStream);
  workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');
  source.connect(workletNode);
  // Keep the worklet in the audio graph (silent) so the browser always runs it.
  const mute = audioCtx.createGain();
  mute.gain.value = 0;
  workletNode.connect(mute).connect(audioCtx.destination);
  chunksSent = 0;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const qs = new URLSearchParams({ voice: $('voice').value, model: $('modelSel').value, language: $('lang').value, build: window.BUILD || '' });
  try { localStorage.setItem('voice', $('voice').value); } catch (e) { /* ignore */ }
  ws = new WebSocket(`${proto}://${location.host}/ws/call/${contactId}?${qs}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    setStatus('Connected. The agent will greet you in 1 to 3 seconds. Wait for it, then reply.');
    $('typed').disabled = false;
    $('send').disabled = false;
    workletNode.port.onmessage = (e) => {
      // Mic level meter so you can see your voice is reaching the agent.
      const s = new Int16Array(e.data);
      let sum = 0;
      for (let i = 0; i < s.length; i++) sum += s[i] * s[i];
      const rms = Math.sqrt(sum / s.length) / 32768;
      $('meter').style.width = Math.min(100, Math.round(rms * 600)) + '%';
      const muted = $('mute').checked;
      // When muted we still send audio, but silent, so the agent's turn-taking keeps working.
      const payload = muted ? new ArrayBuffer(e.data.byteLength) : e.data;
      if (ws && ws.readyState === 1) { ws.send(payload); chunksSent++; }
      $('micinfo').textContent = muted ? 'mic muted, type your reply below'
        : rms > 0.01 ? `hearing you (${chunksSent} chunks sent)` : `listening, quiet (${chunksSent} chunks sent)`;
    };
  };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) { playChunk(e.data); return; }
    const m = JSON.parse(e.data);
    switch (m.type) {
      case 'status': setStatus(m.text); break;
      case 'interrupted': stopPlayback(); log('[customer interrupted]', 'meta'); agentLine = null; break;
      case 'transcript':
        if (m.role === 'agent') {
          if (!agentLine) { agentLine = log('Agent: ', 'agent'); setStatus('Agent is speaking…'); }
          agentLine.textContent += m.text;
        } else {
          if (!customerLine) customerLine = log('Customer: ', 'customer');
          customerLine.textContent += m.text;
        }
        break;
      case 'turn_complete':
        agentLine = customerLine = null;
        setStatus('Agent finished speaking. Your turn: speak, or type a reply below.');
        break;
      case 'outcome': log(`[outcome: ${m.outcome}] ${JSON.stringify(m.details)}`, 'meta'); break;
      case 'error': log('ERROR: ' + m.text, 'error'); break;
      case 'ended': setStatus(`Call ended (${m.duration_sec}s, outcome: ${m.outcome || 'none'})`); cleanup(); break;
    }
  };
  ws.onclose = () => cleanup();
  ws.onerror = () => { log('WebSocket error', 'error'); cleanup(); };
}

function hangupBrowserCall() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'hangup' }));
  cleanup();
}
window.hangupBrowserCall = hangupBrowserCall;
// "End test" on the browser test card. Phone calls are ended from their card in Live calls.
function hangup() {
  if (ws) hangupBrowserCall();
}

function cleanup() {
  $('start').disabled = false;
  $('hangup').disabled = true;
  $('typed').disabled = true;
  $('send').disabled = true;
  $('meter').style.width = '0';
  $('micinfo').textContent = 'not started';
  stopPlayback();
  if (workletNode) { try { workletNode.disconnect(); } catch (e) { /* ignore */ } workletNode = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  if (ws) { try { ws.close(); } catch (e) { /* ignore */ } ws = null; }
  loadCalls();
  loadContacts();
}

$('upload').onclick = upload;
$('pv-cancel').onclick = closePreview;
$('file').addEventListener('change', () => { if ($('file').files[0]) previewSheet(); });
for (const id of ['pv-skipdup', 'pv-skipnophone']) $(id).addEventListener('change', renderPreview);
$('pv-chips').addEventListener('click', (e) => { const b = e.target.closest('[data-pv]'); if (b) { previewFilter = b.dataset.pv; renderPreview(); } });
$('clear').onclick = clearContacts;
$('start').onclick = startCall;
$('hangup').onclick = hangup;
$('clearcalls').onclick = async () => {
  if (!confirm('Delete the whole call history? Transcripts and outcomes will be lost. Contacts are kept.')) return;
  await fetch('/api/calls', { method: 'DELETE' });
  await loadCalls();
  await loadContacts();
};
// Phone calls end outside this page, so keep the history fresh automatically.
// Skips while a transcript is open so it does not collapse under you.
setInterval(() => { if (!document.querySelector('#calls details[open]')) { loadCalls(); loadContacts(); } }, 5000);
$('send').onclick = sendTyped;
$('typed').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendTyped(); });
fetch('/api/config').then(r => r.json()).then(c => {
  window.BUILD = c.build;   // sent with every call this page starts; a restarted server refuses an old page
  let savedVoice = c.voice;
  try { savedVoice = localStorage.getItem('voice') || c.voice; localStorage.removeItem('model'); } catch (e) { /* ignore */ }
  for (const [name, gender] of Object.entries(c.voices)) {
    for (const id of ['voice', 'camp-voice']) {
      const o = document.createElement('option');
      o.value = name; o.textContent = `${name} (${gender})`;
      if (name === savedVoice) o.selected = true;
      $(id).appendChild(o);
    }
  }
  // The agent: Gemini 2.5, Gemini 3.8 or ChatGPT. A single call always starts on the first (Gemini 2.5):
  // remembering the last choice put calls 103 and 104 on ChatGPT without anyone noticing. A campaign
  // shows the agent it last ran with (dashboard.js puts it in data-want).
  const labels = c.model_labels || {};
  for (const id of ['modelSel', 'camp-model']) {
    const want = id === 'modelSel' ? c.model : ($(id).dataset.want || c.model);
    for (const m of c.models) {
      const o = document.createElement('option');
      o.value = m; o.textContent = labels[m] || m;
      if (m === want) o.selected = true;
      $(id).appendChild(o);
    }
  }  // Language: the sheet has no language column, so the owner picks it here. Remembered, and the
  // two selects (campaign and single call) stay in step.
  let savedLang = 'hinglish';
  try { savedLang = localStorage.getItem('language') || savedLang; } catch (e) { /* ignore */ }
  const LANG_LABELS = { hinglish: 'Hinglish (Hindi + English)', hindi: 'Hindi', english: 'English' };
  for (const id of ['lang', 'camp-lang']) {
    for (const l of c.languages || ['hinglish', 'hindi', 'english']) {
      const o = document.createElement('option');
      o.value = l; o.textContent = LANG_LABELS[l] || l;
      if (l === savedLang) o.selected = true;
      $(id).appendChild(o);
    }
    $(id).addEventListener('change', () => {
      const v = $(id).value;
      for (const other of ['lang', 'camp-lang']) $(other).value = v;
      try { localStorage.setItem('language', v); } catch (e) { /* ignore */ }
    });
  }
});
loadContacts();
loadCalls();

// --- Real phone call through Plivo (paid). Enabled only when .env has the Plivo settings. ---
async function loadPlivo() {
  const st = await fetch('/api/plivo/status').then(r => r.json());
  $('dial').disabled = !st.ready;
  if (window.renderLineBadge) window.renderLineBadge(st);
  $('plivoinfo').textContent = st.ready
    ? `Phone line ready. Calls go out from ${st.from_number} to the number in the box below. Every call is paid.`
    : `Phone line not ready. Missing: ${st.missing.join(', ')}. Start the server with run_phone.bat for real calls.`;
}
$('contact').addEventListener('change', syncDialTo);
$('dial').onclick = async () => {
  const contactId = $('contact').value;
  if (!contactId) return alert('Pick a contact first. The agent uses that row for the order details.');
  const to = $('dialto').value.trim();
  const digits = to.replace(/\D/g, '');
  if (digits.length < 10) return alert('Type a valid 10-digit mobile number.');
  const who = contactRows.find(r => String(r.id) === contactId);
  if (!confirm(`Place a REAL phone call?

Number: ${to}
Agent: ${$('modelSel').selectedOptions[0] ? $('modelSel').selectedOptions[0].textContent : ''}
Script for: ${who ? who.customer_name : ''} (${who ? who.order_id : ''})
Cost: a few rupees per minute (Gemini bills every turn for the whole call so far).

Only call numbers of people who expect it.`)) return;
  const qs = new URLSearchParams({ voice: $('voice').value, model: $('modelSel').value, to, language: $('lang').value, build: window.BUILD || '' });
  const r = await fetch(`/api/calls/dial/${contactId}?${qs}`, { method: 'POST' });
  const j = await r.json();
  notify(r.ok ? `Dialing ${j.dialed}… Watch the Live calls panel.` : 'Could not dial: ' + (j.detail || r.status));
  if (r.ok && window.openSheet) window.openSheet(null);
};
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
