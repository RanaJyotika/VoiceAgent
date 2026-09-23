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

async function loadContacts() {
  const rows = await fetch('/api/contacts').then(r => r.json());
  contactRows = rows;
  const cc = document.getElementById('contactcount');
  if (cc) cc.textContent = rows.length ? `${rows.length} loaded` : 'none loaded';
  if (window.renderContacts) window.renderContacts(rows);
  if (window.loadCampaign) window.loadCampaign();
  const sel = $('contact');
  const chosen = sel.value;   // the list refreshes every few seconds during a run; keep the pick
  sel.innerHTML = '';
  if (!rows.length) {
    sel.innerHTML = '<option value="">No contacts. Upload the Excel sheet first.</option>';
    return;
  }
  for (const r of rows) {
    const o = document.createElement('option');
    o.value = r.id;
    o.textContent = `${r.order_id || '-'} | ${r.customer_name || '-'} | ${r.phone || 'no phone'} | ${r.product || '-'} | Rs ${r.amount || '-'} | ${r.payment_mode || ''} | ${r.status}`;
    if (String(r.id) === chosen) o.selected = true;
    sel.appendChild(o);
  }
  if (sel.value !== chosen) syncDialTo();
}

async function loadCalls() {
  const rows = await fetch('/api/calls').then(r => r.json());
  if (window.renderCalls) window.renderCalls(rows);
  if (window.loadStats) window.loadStats();
}

// ---- upload: preview first, import on confirm ----
let previewFilter = 'all';
let previewData = null;
const ISSUE = {
  ok: ['good', 'check', 'OK'],
  no_phone: ['critical', 'alert', 'No valid phone'],
  duplicate: ['warning', 'alert', 'Repeated in sheet'],
  on_list: ['serious', 'clock', 'Already on list'],
};
const COL_LABEL = { order_id: 'Order ID', customer_name: 'Customer name', phone: 'Phone', product: 'Product', quantity: 'Qty', amount: 'Amount',
  payment_mode: 'Payment', address: 'Address', delivery_date: 'Delivery date', language: 'Language', notes: 'Notes' };

async function previewSheet() {
  const f = $('file').files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  $('pv-summary').textContent = 'Reading the file…';
  $('preview').hidden = false;
  const r = await fetch('/api/contacts/preview', { method: 'POST', body: fd });
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
  $('pv-columns').innerHTML = `<span class="hint">Columns understood:</span>` + j.columns.map((k) => window.outcomeBadge ? `<span class="badge good">${esc(COL_LABEL[k] || k)}</span>` : esc(k)).join('')
    + (j.ignored.length ? `<span class="hint" style="margin-left:6px">Ignored:</span>` + j.ignored.map((k) => `<span class="badge">${esc(k)}</span>`).join('') : '');
  const warn = [];
  if (j.missing.length) warn.push(`Missing column${j.missing.length > 1 ? 's' : ''}: ${j.missing.map((k) => COL_LABEL[k] || k).join(', ')}. The agent will have nothing to say for those details.`);
  if (c.no_phone) warn.push(`${c.no_phone} row${c.no_phone > 1 ? 's have' : ' has'} no valid 10-digit phone.`);
  if (c.duplicate) warn.push(`${c.duplicate} row${c.duplicate > 1 ? 's repeat' : ' repeats'} a number used earlier in the sheet.`);
  if (c.on_list) warn.push(`${c.on_list} number${c.on_list > 1 ? 's are' : ' is'} already on the call list.`);
  $('pv-warn').hidden = !warn.length;
  $('pv-warn').textContent = warn.join(' ');
  const chip = (key, label, tone, n) => `<button class="${tone}" data-pv="${key}" aria-pressed="${previewFilter === key}">${label} <b>${n}</b></button>`;
  $('pv-chips').innerHTML = chip('all', 'All rows', '', j.total) + Object.entries(ISSUE).filter(([k]) => c[k]).map(([k, v]) => chip(k, v[2], v[0], c[k])).join('');
  const rows = j.rows.filter((r) => previewFilter === 'all' || r.issue === previewFilter);
  $('pv-rows').innerHTML = rows.map((r) => {
    const v = ISSUE[r.issue] || ISSUE.ok;
    const badge = window.outcomeBadge ? `<span class="badge ${v[0]}">${v[2]}</span>` : v[2];
    return `<tr>
      <td class="num"><small>${r.row}</small></td><td>${badge}</td>
      <td><b>${esc(r.customer_name)}</b></td><td class="num">${esc(r.phone)}</td><td><small>${esc(r.order_id)}</small></td>
      <td>${esc(r.product)}</td><td class="num">${esc(r.quantity)}</td><td class="num">${esc(r.amount)}</td><td>${esc(r.payment_mode)}</td>
      <td><small>${esc(r.address)}</small></td><td><small>${esc(r.delivery_date)}</small></td><td><small>${esc(r.notes)}</small></td>
    </tr>`;
  }).join('') || '<tr><td colspan="12"><small>No rows in this group.</small></td></tr>';
  $('pv-more').textContent = j.total > j.rows.length ? `Showing the first ${j.rows.length} of ${j.total} rows; all of them will be imported.` : '';
}

function closePreview() {
  $('preview').hidden = true;
  previewData = null;
  $('file').value = '';
  $('filename').textContent = 'Choose an Excel file';
}

async function upload() {
  const f = $('file').files[0];
  if (!f) return alert('Choose an .xlsx file first');
  const fd = new FormData();
  fd.append('file', f);
  const qs = new URLSearchParams({ skip_duplicates: $('pv-skipdup').checked, skip_no_phone: $('pv-skipnophone').checked });
  $('upload').disabled = true;
  const r = await fetch(`/api/contacts/upload?${qs}`, { method: 'POST', body: fd });
  const j = await r.json().catch(() => ({}));
  $('upload').disabled = false;
  if (!r.ok) return alert(j.detail || 'Upload failed');
  notify(`Added ${j.imported} contact${j.imported === 1 ? '' : 's'} to the call list` + (j.skipped ? ` · ${j.skipped} skipped` : ''));
  closePreview();
  await loadContacts();
  if (window.showPage) { window.showPage('overview'); setTimeout(() => document.getElementById('board').scrollIntoView({ block: 'start', behavior: 'smooth' }), 30); }
}

async function clearContacts() {
  if (!confirm('Delete all contacts?')) return;
  await fetch('/api/contacts', { method: 'DELETE' });
  await loadContacts();
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
  const qs = new URLSearchParams({ voice: $('voice').value, model: $('modelSel').value, language: $('lang').value });
  try { localStorage.setItem('voice', $('voice').value); localStorage.setItem('model', $('modelSel').value); } catch (e) { /* ignore */ }
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
// Dock button: ends the browser test if one is running, otherwise the newest live phone call.
function hangup() {
  if (ws) return hangupBrowserCall();
  if (window.hangupNewestLive) window.hangupNewestLive();
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
$('refresh').onclick = loadCalls;
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
  let savedVoice = c.voice, savedModel = c.model;
  try { savedVoice = localStorage.getItem('voice') || c.voice; savedModel = localStorage.getItem('model') || c.model; } catch (e) { /* ignore */ }
  for (const [name, gender] of Object.entries(c.voices)) {
    for (const id of ['voice', 'camp-voice']) {
      const o = document.createElement('option');
      o.value = name; o.textContent = `${name} (${gender})`;
      if (name === savedVoice) o.selected = true;
      $(id).appendChild(o);
    }
  }
  for (const m of c.models) {
    const o = document.createElement('option');
    o.value = m; o.textContent = m.replace('-preview-12-2025', '');
    if (m === savedModel) o.selected = true;
    $('modelSel').appendChild(o);
  }
  // Language: the sheet has no language column, so the owner picks it here. Remembered, and the
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
Script for: ${who ? who.customer_name : ''} (${who ? who.order_id : ''})
Cost: a few rupees per minute (Gemini bills every turn for the whole call so far).

Only call numbers of people who expect it.`)) return;
  const qs = new URLSearchParams({ voice: $('voice').value, model: $('modelSel').value, to, language: $('lang').value });
  const r = await fetch(`/api/calls/dial/${contactId}?${qs}`, { method: 'POST' });
  const j = await r.json();
  notify(r.ok ? `Dialing ${j.dialed}… Watch the Live calls panel.` : 'Could not dial: ' + (j.detail || r.status));
  if (r.ok && window.openSheet) window.openSheet(null);
};
$('m_add').onclick = async () => {
  const row = {
    customer_name: $('m_name').value, phone: $('m_phone').value, order_id: $('m_order').value,
    product: $('m_product').value, quantity: $('m_qty').value || '1', amount: $('m_amount').value,
    payment_mode: $('m_pay').value, language: $('m_lang').value,
    address: $('m_address').value, delivery_date: $('m_date').value,
  };
  const r = await fetch('/api/contacts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(row) });
  const j = await r.json();
  if (!r.ok) return alert(j.detail || 'Could not add the contact');
  for (const id of ['m_name', 'm_phone', 'm_order', 'm_product', 'm_qty', 'm_amount', 'm_address', 'm_date']) $(id).value = '';
  notify('Contact added.');
  await loadContacts();
};
loadPlivo();
