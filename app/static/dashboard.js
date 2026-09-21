// Dashboard layer: stat tiles, live call cards, and status badges.
// Loaded before app.js, which calls window.renderCalls / loadStats / renderLineBadge.
(() => {
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // One small icon set. Every badge = icon + label, so colour never carries meaning alone.
  const P = {
    check: '<path d="M20 6 9 17l-5-5"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
    ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
    missed: '<path d="m22 2-6 6M16 2l6 6"/><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
    phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
    ring: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/><path d="M14 2a8 8 0 0 1 8 8M14 6a4 4 0 0 1 4 4"/>',
    monitor: '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    minus: '<path d="M5 12h14"/>',
    chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z"/>',
    timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2M9 2h6"/>',
    rupee: '<path d="M6 3h12M6 8h12M6 13l8.5 8M6 13h3a5 5 0 0 0 0-10"/>',
    ear: '<path d="M6 8.5a6.5 6.5 0 1 1 13 0c0 6-6 6-6 10a3.5 3.5 0 0 1-7 0"/><path d="M15 8.5a2.5 2.5 0 0 0-5 0v1a2 2 0 0 0 2 2"/>',
  };
  const icon = (name) => `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || P.minus}</svg>`;
  const badge = (tone, ico, label, extra = '') => `<span class="badge ${tone}">${icon(ico)}${esc(label)}${extra}</span>`;

  const OUTCOME = {
    confirmed: ['good', 'check', 'Confirmed'],
    changed: ['warning', 'edit', 'Change requested'],
    cancelled: ['critical', 'x', 'Cancelled'],
    call_later: ['', 'clock', 'Call later'],
    wrong_number: ['serious', 'alert', 'Wrong number'],
    do_not_call: ['critical', 'ban', 'Do not call'],
    no_answer: ['', 'missed', 'No answer'],
    unclear: ['serious', 'help', 'Needs review'],
    error: ['critical', 'alert', 'Error'],
    none: ['', 'minus', 'No outcome'],
  };
  const outcomeBadge = (key) => { const o = OUTCOME[key] || OUTCOME.none; return badge(o[0], o[1], o[2]); };
  window.outcomeBadge = outcomeBadge;

  const mmss = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const talk = (s) => s >= 3600 ? `${(s / 3600).toFixed(1)} h` : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;

  // ---------------- bottom dock: sheets and toast ----------------
  const tabs = [...document.querySelectorAll('.dock [data-sheet]')];
  window.openSheet = (name) => {
    for (const t of tabs) t.setAttribute('aria-expanded', t.dataset.sheet === name);
    for (const s of document.querySelectorAll('.sheet')) s.classList.toggle('on', s.id === `sheet-${name}`);
    el('scrim').classList.toggle('on', !!name);
  };
  for (const t of tabs) t.onclick = () => window.openSheet(t.getAttribute('aria-expanded') === 'true' ? null : t.dataset.sheet);
  for (const b of document.querySelectorAll('[data-close]')) b.onclick = () => window.openSheet(null);
  el('scrim').onclick = () => window.openSheet(null);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') window.openSheet(null); });
  el('gohistory').onclick = () => { window.openSheet(null); el('history').scrollIntoView({ block: 'start' }); };

  let toastTimer = null;
  window.toast = (text) => {
    el('toast').textContent = text;
    el('toast').classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el('toast').classList.remove('on'), 3500);
  };

  // File picker: show the chosen name, and accept a dropped file.
  const drop = el('drop');
  el('file').onchange = () => { el('filename').textContent = el('file').files[0]?.name || 'Choose an Excel file'; };
  for (const ev of ['dragenter', 'dragover']) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); });
  for (const ev of ['dragleave', 'drop']) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); });
  drop.addEventListener('drop', (e) => {
    if (!e.dataTransfer.files.length) return;
    el('file').files = e.dataTransfer.files;
    el('file').onchange();
  });

  // ---------------- stat tiles ----------------
  let period = 'today';
  window.loadStats = async () => {
    const s = await fetch(`/api/stats?period=${period}`).then((r) => r.json());
    const tile = (tone, ico, label, value, note = '') =>
      `<div class="tile"><div class="label badge-like ${tone}" style="--c:var(--${tone || 'accent'})">${icon(ico)}${esc(label)}</div><div class="value">${value}</div><div class="note">${esc(note)}</div></div>`;
    el('tiles').innerHTML = [
      tile('', 'phone', 'Calls made', s.calls, s.phone_calls ? `${s.phone_calls} by phone, ${s.calls - s.phone_calls} browser tests` : (s.calls ? 'browser tests' : '')),
      tile('good', 'ring', 'Answered', s.answered, s.answer_rate == null ? '' : `${s.answer_rate}% of calls`),
      tile('good', 'check', 'Confirmed', s.confirmed, s.answered ? `${Math.round(100 * s.confirmed / s.answered)}% of answered` : ''),
      tile('warning', 'edit', 'Change requested', s.changed),
      tile('critical', 'x', 'Cancelled', s.cancelled),
      tile('', 'missed', 'No answer', s.no_answer),
      tile('serious', 'help', 'Needs review', s.needs_review, s.needs_review ? 'unclear, wrong number, call later' : ''),
      tile('', 'timer', 'Talk time', talk(s.talk_seconds), s.avg_seconds == null ? '' : `average ${mmss(s.avg_seconds)} per call`),
      tile('', 'rupee', 'Estimated cost', `Rs ${s.cost_rs.toFixed(2)}`, s.answered ? `about Rs ${(s.cost_rs / s.answered).toFixed(2)} per answered call` : ''),
    ].join('');
  };
  for (const [id, p] of [['p_today', 'today'], ['p_all', 'all']]) {
    el(id).onclick = () => {
      period = p;
      el('p_today').setAttribute('aria-pressed', p === 'today');
      el('p_all').setAttribute('aria-pressed', p === 'all');
      window.loadStats();
    };
  }

  // ---------------- phone line badge ----------------
  window.renderLineBadge = (st) => {
    el('linebadge').innerHTML = st.ready
      ? `<span class="badge good"><span class="dot"></span>Phone line ready · ${esc(st.from_number)}</span>`
      : `<span class="badge">${icon('monitor')}Browser testing only</span>`;
  };

  // ---------------- live calls ----------------
  const LIVE = {
    ringing: ['warning', 'ring', 'Ringing', true],
    agent_speaking: ['good', 'phone', 'In call · agent speaking', false],
    listening: ['good', 'ear', 'In call · listening', false],
    ending: ['', 'check', 'Wrapping up', false],
  };
  let hadLive = false;
  let liveRows = [];

  // Hang up a live call. Phone calls go through the server; a browser test ends itself.
  async function hangupLive(id) {
    const c = liveRows.find((r) => String(r.call_id) === String(id));
    if (!c) return;
    if (c.channel !== 'plivo') { if (window.hangupBrowserCall) window.hangupBrowserCall(); return; }
    const verb = c.status === 'ringing' ? 'Stop ringing' : 'Hang up on';
    if (!confirm(`${verb} ${c.customer_name || 'this customer'} (${c.to || ''})?`)) return;
    const r = await fetch(`/api/calls/${c.call_id}/hangup`, { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    window.toast(r.ok ? 'Call ended.' : (j.detail || 'Could not hang up.'));
    loadLive();
  }
  // Used by the dock button: the newest live call.
  window.hangupNewestLive = () => { if (liveRows.length) hangupLive(liveRows[liveRows.length - 1].call_id); };
  // The cards are redrawn every second, so listen on the container, and on pointerup: a click
  // needs press and release on the same element, which a redraw in between would break.
  el('live').addEventListener('pointerup', (e) => {
    const b = e.target.closest('[data-hang]');
    if (b) hangupLive(b.dataset.hang);
  });

  async function loadLive() {
    let rows = [];
    try { rows = await fetch('/api/live').then((r) => r.json()); } catch (e) { return; }
    liveRows = rows;
    el('hangup').disabled = rows.length === 0;
    el('livecount').innerHTML = rows.length
      ? `<span class="badge accent pulse"><span class="dot"></span>${rows.length} live ${rows.length === 1 ? 'call' : 'calls'}</span>` : '';
    if (!rows.length) {
      el('live').innerHTML = '<div class="empty">No calls in progress.</div>';
      if (hadLive && window.loadCalls) { window.loadCalls(); }   // a call just ended: refresh history and tiles
      hadLive = false;
      return;
    }
    hadLive = true;
    el('live').innerHTML = rows.map((c) => {
      const l = LIVE[c.status] || LIVE.listening;
      const bars = c.status === 'agent_speaking' ? '<span class="bars" aria-hidden="true"><i></i><i></i><i></i></span>' : '';
      const st = `<span class="badge ${l[0]} ${l[3] ? 'pulse' : ''}">${l[3] ? '<span class="dot"></span>' : icon(l[1])}${esc(l[2])}${bars}</span>`;
      const channel = c.channel === 'plivo' ? badge('', 'phone', 'Phone') : badge('', 'monitor', 'Browser test');
      const said = (c.lines || []).map((t) => `<div class="${t.role === 'agent' ? 'a' : ''}"><b>${t.role === 'agent' ? 'Agent' : 'Customer'}:</b> ${esc(t.text)}</div>`).join('');
      return `<div class="live">
        <div class="who"><b>${esc(c.customer_name || 'Unknown')}</b><span class="hint">${esc(c.to || '')}</span><span class="timer">${mmss(c.seconds)}</span></div>
        <div class="meta">${esc([c.order_id, c.product].filter(Boolean).join(' · '))}</div>
        <div class="row">${st}${channel}${c.outcome ? outcomeBadge(c.outcome) : ''}
          <button class="hang" data-hang="${c.call_id}">${icon('phone')}${c.status === 'ringing' ? 'Cancel' : 'Hang up'}</button></div>
        ${said ? `<div class="say">${said}</div>` : ''}
      </div>`;
    }).join('');
  }
  setInterval(loadLive, 1000);
  loadLive();

  // ---------------- call history ----------------
  const outcomeKey = (c) => c.outcome || (c.error ? 'error' : ((c.duration_sec || 0) > 0 ? 'none' : 'no_answer'));
  const detailText = (c) => Object.entries(c.outcome_details || {}).filter(([k]) => k !== 'outcome')
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).join(' · ');
  const whenText = (c) => c.started_at ? new Date(c.started_at).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
  // Older records still carry the model's leaked mood notes; never show them.
  const tidy = (s) => String(s || '').replace(/emotion_(?:user|model)\s*[a-zA-Z_]*\s*(?:<ctrl\d+>)?/g, '').replace(/<ctrl\d+>/g, '').trim();

  // Transcript as a chat: our agent on the right, the customer on the left.
  let callsById = {};
  function showTranscript(id) {
    const c = callsById[id];
    if (!c) return;
    el('chat-title').textContent = c.customer_name || 'Unknown customer';
    el('chat-sub').textContent = [c.order_id, whenText(c), mmss(c.duration_sec), `Rs ${(c.cost_rs ?? 0).toFixed(2)}`].filter(Boolean).join(' · ');
    el('chat-outcome').innerHTML = outcomeBadge(outcomeKey(c)) + (c.channel === 'plivo' ? badge('', 'phone', 'Phone') : badge('', 'monitor', 'Browser'));
    const bubbles = (c.transcript || []).map((t) => {
      const text = tidy(t.text);
      if (!text) return '';
      const spoken = text.replace(/<noise>/g, '').trim();
      if (!spoken) return '<div class="chat-note">background noise</div>';
      const who = t.role === 'agent' ? 'agent' : 'customer';
      return `<div class="msg ${who}"><span class="who">${who === 'agent' ? 'Agent' : esc(c.customer_name || 'Customer')}</span>${esc(spoken)}</div>`;
    }).join('');
    const details = detailText(c);
    el('chat-body').innerHTML = (bubbles || '<div class="chat-note">Nothing was said on this call.</div>')
      + (details ? `<div class="chat-note result">${icon('check')}${esc(details)}</div>` : '')
      + (c.error ? `<div class="chat-note error">${icon('alert')}${esc(c.error)}</div>` : '');
    window.openSheet('transcript');
    el('chat-body').scrollTop = 0;
  }
  el('calls').addEventListener('click', (e) => {
    const b = e.target.closest('[data-call]');
    if (b) showTranscript(b.dataset.call);
  });

  window.renderCalls = (rows) => {
    const tb = el('calls');
    callsById = Object.fromEntries(rows.map((c) => [c.id, c]));
    el('nocalls').hidden = rows.length > 0;
    tb.innerHTML = rows.map((c) => {
      const key = outcomeKey(c);
      const details = detailText(c);
      const when = whenText(c);
      const said = (c.transcript || []).length;
      return `<tr>
        <td class="num">${c.id}</td>
        <td>${c.channel === 'plivo' ? badge('', 'phone', 'Phone') : badge('', 'monitor', 'Browser')}</td>
        <td class="num">${esc(when)}</td>
        <td>${esc(c.customer_name || '')}<br><small>${esc(c.order_id || '')}</small></td>
        <td class="num">${mmss(c.duration_sec)}</td>
        <td>${outcomeBadge(key)}${details ? `<br><small>${esc(details)}</small>` : ''}</td>
        <td class="num">Rs ${(c.cost_rs ?? 0).toFixed(2)}</td>
        <td>${said || c.error ? `<button class="viewbtn" data-call="${c.id}">${icon('chat')}View</button>` : '<small>none</small>'}</td>
      </tr>`;
    }).join('');
  };
})();
