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
    busy: ['', 'missed', 'Busy'],
    dropped: ['warning', 'missed', 'Call dropped'],
    unclear: ['serious', 'help', 'Needs review'],
    error: ['critical', 'alert', 'Error'],
    none: ['', 'minus', 'No outcome'],
    // contact statuses that are not call outcomes
    pending: ['', 'clock', 'To call'],
    calling: ['accent', 'ring', 'Calling now'],
  };
  const outcomeBadge = (key) => { const o = OUTCOME[key] || OUTCOME.none; return badge(o[0], o[1], o[2]); };
  window.outcomeBadge = outcomeBadge;
  window.outcomeLabel = (key) => (OUTCOME[key] || OUTCOME.none)[2];

  const mmss = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const talk = (s) => s >= 3600 ? `${(s / 3600).toFixed(1)} h` : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;

  // ---------------- sidebar pages, phone drawer, transcript modal ----------------
  const PAGES = ['overview', 'callback', 'call', 'upload', 'history'];
  const navButtons = [...document.querySelectorAll('.nav [data-page], .dock [data-page]')];
  window.showPage = (name) => {
    if (!PAGES.includes(name)) name = 'overview';
    for (const p of PAGES) el(`page-${p}`).classList.toggle('on', p === name);
    for (const b of navButtons) { if (b.dataset.page === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); }
    closeDrawer();
    window.scrollTo({ top: 0 });
    if (name === 'callback' && window.loadCallbacks) window.loadCallbacks();
    try { localStorage.setItem('page', name); } catch (e) { /* ignore */ }
  };
  const FOCUS = { console: 'sheet-console', board: 'board' };
  for (const b of document.querySelectorAll('[data-page]')) b.addEventListener('click', () => {
    window.showPage(b.dataset.page);
    const target = FOCUS[b.dataset.focus];
    if (target) setTimeout(() => el(target).scrollIntoView({ block: 'start', behavior: 'smooth' }), 30);
  });

  // The phone drawer: the sidebar slides in over the page on small screens.
  const side = el('side');
  const openDrawer = () => { side.classList.add('open'); el('burger').setAttribute('aria-expanded', 'true'); el('scrim').classList.add('on'); };
  const closeDrawer = () => { side.classList.remove('open'); el('burger').setAttribute('aria-expanded', 'false'); if (!el('sheet-transcript').classList.contains('on')) el('scrim').classList.remove('on'); };
  el('burger').onclick = () => (side.classList.contains('open') ? closeDrawer() : openDrawer());

  // openSheet is what the older code calls; sheets are now pages, except the transcript, which is a modal.
  const SHEET_TO_PAGE = { contacts: 'upload', call: 'call', console: 'call' };
  window.openSheet = (name) => {
    if (name === 'transcript') { el('sheet-transcript').classList.add('on'); el('scrim').classList.add('on'); return; }
    if (name === null) { el('sheet-transcript').classList.remove('on'); closeDrawer(); el('scrim').classList.remove('on'); return; }
    window.showPage(SHEET_TO_PAGE[name] || name);
    if (name === 'console') el('sheet-console').scrollIntoView({ block: 'nearest' });
  };
  for (const b of document.querySelectorAll('[data-close]')) b.onclick = () => window.openSheet(null);
  el('scrim').onclick = () => window.openSheet(null);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') window.openSheet(null); });
  try { window.showPage(localStorage.getItem('page') || 'overview'); } catch (e) { window.showPage('overview'); }

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
    el('file').dispatchEvent(new Event('change'));   // also starts the preview (app.js listens)
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
      tile('', 'missed', 'Call back', s.call_back, [s.no_answer ? `${s.no_answer} no answer` : '', s.dropped ? `${s.dropped} dropped` : '', s.call_later ? `${s.call_later} asked later` : ''].filter(Boolean).join(', ')),
      tile('serious', 'help', 'Needs review', s.needs_review, s.needs_review ? 'unclear, wrong number, do not call' : ''),
      tile('', 'timer', 'Talk time', talk(s.talk_seconds), s.avg_seconds == null ? '' : `average ${mmss(s.avg_seconds)} per call`),
      tile('', 'rupee', 'Cost', `Rs ${s.cost_rs.toFixed(2)}`, s.answered ? `about Rs ${(s.cost_rs / s.answered).toFixed(2)} per answered call` : ''),
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
      ? `<span class="badge good"><span class="dot"></span>Phone line ready</span><span class="num">from ${esc(st.from_number)}</span>`
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
    el('fab').classList.toggle('live', rows.length > 0);   // the dock's call button pulses while a call is on
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

  // "~" marks a per-minute guess; without it the figure comes from the tokens the model reported.
  const costText = (c) => `${c.cost_estimated ? '~' : ''}Rs ${(c.cost_rs ?? 0).toFixed(2)}`;

  // Transcript as a chat: our agent on the right, the customer on the left.
  let callsById = {};
  async function showTranscript(id) {
    let c = callsById[id];
    if (!c) {
      // The board can point at a call older than the history table shows.
      try { c = await fetch(`/api/calls/${id}`).then((r) => r.ok ? r.json() : null); } catch (e) { c = null; }
      if (!c) { window.toast('That call is not on record.'); return; }
      callsById[id] = c;
    }
    el('chat-title').textContent = c.customer_name || 'Unknown customer';
    const u = c.usage || {};
    const tokens = u.turns ? `${u.turns} turns, ${(u.audio_in || 0) + (u.text_in || 0)} in / ${(u.audio_out || 0) + (u.text_out || 0)} out tokens` : '';
    el('chat-sub').innerHTML = [c.order_id, whenText(c), mmss(c.duration_sec), costText(c), tokens].filter(Boolean).map(esc).join(' · ')
      + (c.recording ? ` · <a href="/api/calls/${c.id}/recording" target="_blank">Listen</a>` : '');
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
        <td class="num" title="${c.cost_estimated ? 'estimated from call length' : 'from the tokens the model reported'}">${esc(costText(c))}</td>
        <td>${said || c.error ? `<button class="viewbtn" data-call="${c.id}">${icon('chat')}View</button>` : '<small>none</small>'}</td>
      </tr>`;
    }).join('');
  };

  // ---------------- campaign: call the whole list ----------------
  const STATUS_ORDER = ['pending', 'calling', 'confirmed', 'changed', 'cancelled', 'call_later', 'no_answer', 'busy', 'unclear', 'wrong_number', 'do_not_call'];
  let camp = null;
  window.renderCampaign = (s) => {
    camp = s;
    const running = s.running;
    let state, reason = s.reason || '';
    if (running && s.in_progress) state = `Calling… ${s.in_progress} call${s.in_progress > 1 ? 's' : ''} in progress`;
    else if (running && !s.within_hours) state = `Waiting for calling hours (${s.settings.start_hour}:00 to ${s.settings.end_hour}:00, now ${s.now_ist})`;
    else if (running) state = 'Running · picking the next contact';
    else if (!s.total) state = 'No contacts yet · upload the Excel sheet first';
    else if (s.to_call) state = `Ready · ${s.to_call} to call`;
    else state = 'List done · nobody left to call';
    if (running && s.calls_made) reason = `${s.calls_made} call${s.calls_made > 1 ? 's' : ''} placed this run` + (s.retry_left ? ` · ${s.retry_left} retries queued` : '');
    el('camp-state').textContent = state;
    el('camp-reason').textContent = reason;
    const calling = s.by_status.calling || 0;
    const done = Math.max(0, s.total - s.to_call - calling);
    el('camp-progress').querySelector('.done').style.width = s.total ? `${100 * done / s.total}%` : '0';
    el('camp-progress').querySelector('.now').style.width = s.total ? `${100 * calling / s.total}%` : '0';
    el('camp-progress').title = `${done} done · ${calling} in progress · ${s.to_call} to call (of ${s.total})`;
    el('camp-counts').innerHTML = s.total
      ? `<span class="hint">${done} of ${s.total} done${s.retry_left ? ` · ${s.retry_left} waiting for a retry` : ''}${calling ? ` · ${calling} on a call now` : ''}</span>`
      : '<span class="hint">Nothing on the list.</span>';
    const warn = el('camp-warn');
    warn.hidden = !s.dial_only_my_number;
    if (s.dial_only_my_number) warn.textContent = `Test mode: DIAL_ONLY_MY_NUMBER is on, so EVERY call in the campaign will ring your own phone (${s.my_number}) instead of the customers. Use "Stop after" to keep test runs short.`;
    el('camp-concurrent').max = s.max_concurrent_allowed || 10;
    el('camp-concurrent').title = `Calls running at the same time (up to ${s.max_concurrent_allowed || 10}). Each needs its own Gemini session and Plivo call.`;
    el('camp-start-btn').hidden = running;
    el('camp-stop-btn').hidden = !running;
    el('camp-start-btn').disabled = !s.to_call;
    for (const id of ['camp-voice', 'camp-lang', 'camp-attempts', 'camp-retry', 'camp-start', 'camp-end', 'camp-concurrent', 'camp-limit', 'camp-budget']) el(id).disabled = running;
    el('camp-requeue').disabled = running || !((s.by_status.no_answer || 0) + (s.by_status.busy || 0));
  };
  let staleWarned = false;
  window.loadCampaign = async () => {
    try {
      const r = await fetch('/api/campaign');
      if (r.status === 404) {
        // The page is new but the Python behind it is not: the server was not restarted.
        el('camp-state').textContent = 'Server needs a restart';
        el('camp-reason').textContent = 'This page is newer than the running server. Close the run_phone.bat / run.bat window, start it again, then reload.';
        el('camp-start-btn').disabled = true;
        if (!staleWarned) { staleWarned = true; window.toast('Restart the server: it is running older code.'); }
        return;
      }
      window.renderCampaign(await r.json());
    } catch (e) { /* server away */ }
  };
  el('camp-start-btn').onclick = async () => {
    if (!camp) return;
    const body = {
      voice: el('camp-voice').value, language: el('camp-lang').value, max_attempts: el('camp-attempts').value, retry_minutes: el('camp-retry').value,
      start_hour: el('camp-start').value, end_hour: el('camp-end').value, max_concurrent: el('camp-concurrent').value,
      limit: el('camp-limit').value, budget_rs: el('camp-budget').value,
    };
    const n = body.limit > 0 ? Math.min(body.limit, camp.to_call) : camp.to_call;
    const where = camp.dial_only_my_number ? `ALL of them to YOUR phone ${camp.my_number} (test mode)` : 'to the customers\' numbers';
    if (!confirm(`Start calling ${n} contact${n > 1 ? 's' : ''} now, ${where}?\n\nEvery call is a real, paid phone call. You can stop at any time; the call in progress will finish first.`)) return;
    const r = await fetch('/api/campaign/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) { window.toast(j.detail || 'Could not start.'); return; }
    window.toast('Campaign started. Watch the Live calls panel.');
    window.renderCampaign(j);
  };
  el('camp-stop-btn').onclick = async () => {
    const j = await fetch('/api/campaign/stop', { method: 'POST' }).then((r) => r.json());
    window.toast('Stopping after the current call.');
    window.renderCampaign(j);
  };
  el('camp-requeue').onclick = async () => {
    if (!confirm('Put every no-answer and busy contact back on the list with a fresh count of tries?')) return;
    const j = await fetch('/api/contacts/requeue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ statuses: ['no_answer', 'busy', 'dropped'] }) }).then((r) => r.json());
    window.toast(`${j.requeued} contact${j.requeued === 1 ? '' : 's'} re-queued.`);
    window.loadCampaign();
    if (window.loadContacts) window.loadContacts();
  };
  setInterval(window.loadCampaign, 3000);
  window.loadCampaign();

  // ---------------- contacts board: the list, grouped by what happened ----------------
  const CALLBACK_STATUSES = ['no_answer', 'busy', 'dropped', 'call_later'];
  const PICKED_UP = ['confirmed', 'changed', 'cancelled', 'call_later', 'dropped', 'unclear', 'do_not_call'];
  // Row 1: where each contact is in the run. Row 2: the exact result. One filter active at a time.
  const STAGE_FILTERS = {
    all: ['All', '', 'minus', () => true],
    pending: ['To call', '', 'clock', (c) => c.status === 'pending'],
    scheduled: ['Scheduled', 'accent', 'timer', (c) => CALLBACK_STATUSES.includes(c.status)],
    called: ['Called', '', 'phone', (c) => (c.attempts || 0) > 0 || c.status === 'calling'],
    answered: ['Answered', 'good', 'ring', (c) => PICKED_UP.includes(c.status) && (c.last_duration || 0) > 0],
    not_answered: ['Not answered', '', 'missed', (c) => c.status === 'no_answer' || c.status === 'busy'],
  };
  const RESULT_FILTERS = ['confirmed', 'changed', 'cancelled', 'call_later', 'busy', 'dropped', 'wrong_number', 'do_not_call', 'unclear', 'calling'];
  let boardRows = [];
  let boardFilter = 'all';
  let boardQuery = '';
  const NOTE_KEYS = ['notes', 'mismatch', 'new_product', 'new_address', 'new_delivery_date', 'cancel_reason', 'callback_time'];
  const fmtShort = (iso) => iso ? new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';

  function boardTest(c) {
    if (STAGE_FILTERS[boardFilter]) return STAGE_FILTERS[boardFilter][3](c);
    return c.status === boardFilter;
  }
  function boardMatches(c) {
    if (!boardTest(c)) return false;
    if (!boardQuery) return true;
    const hay = [c.customer_name, c.phone, c.order_id, c.product, c.last_result].join(' ').toLowerCase();
    return hay.includes(boardQuery);
  }

  window.renderContacts = (rows) => {
    boardRows = rows;
    const counts = {};
    for (const c of rows) counts[c.status] = (counts[c.status] || 0) + 1;
    const chip = (key, label, tone, ico, n) =>
      `<button class="${tone}" data-filter="${key}" aria-pressed="${boardFilter === key}">${icon(ico)}${esc(label)} <b>${n}</b></button>`;
    el('board-chips').innerHTML = Object.entries(STAGE_FILTERS)
      .map(([k, f]) => chip(k, f[0], f[1], f[2], rows.filter(f[3]).length)).join('');
    el('board-chips2').innerHTML = RESULT_FILTERS.filter((k) => counts[k] || k === boardFilter)
      .map((k) => { const o = OUTCOME[k]; return chip(k, o[2], o[0], o[1], counts[k] || 0); }).join('');
    el('board-chips2').hidden = !el('board-chips2').innerHTML;
    const shown = rows.filter(boardMatches).sort((a, b) => a.id - b.id);
    el('board-count').textContent = rows.length ? `${shown.length} of ${rows.length} contacts` : '';
    el('nav-contacts').textContent = counts.pending ? String(counts.pending) : '';
    const cb = CALLBACK_STATUSES.reduce((n, k) => n + (counts[k] || 0), 0);
    el('nav-callback').textContent = cb ? String(cb) : '';
    const now = Date.now();
    el('board-rows').innerHTML = shown.map((c) => {
      const d = c.last_details || {};
      const notes = NOTE_KEYS.filter((k) => d[k]).map((k) => `${k.replace(/_/g, ' ')}: ${d[k]}`).join(' · ');
      const result = c.last_result && (!notes || !notes.includes(c.last_result)) ? c.last_result : '';
      const busy = c.status === 'calling';
      const queued = CALLBACK_STATUSES.includes(c.status);
      const next = queued ? (c.next_try_at ? (new Date(c.next_try_at).getTime() <= now ? '<span class="due">due now</span>' : esc(fmtShort(c.next_try_at))) : '<span class="due">next</span>') : '';
      const acts = [
        c.last_call_id && (c.last_duration > 0 || c.last_error) ? `<button class="tiny viewbtn" data-call="${c.last_call_id}">${icon('chat')}Transcript</button>` : '',
        c.last_recording ? `<a class="tiny" href="/api/calls/${c.last_call_id}/recording" target="_blank">${icon('ear')}Listen</a>` : '',
        busy ? '' : `<button class="tiny" data-dial="${c.id}" title="Place one real phone call to this contact now">${icon('phone')}${c.status === 'pending' ? 'Call now' : 'Call again'}</button>`,
        busy || c.status === 'pending' || queued ? '' : `<button class="tiny" data-requeue="${c.id}" title="Put back on the list for the campaign">${icon('clock')}Re-queue</button>`,
      ].filter(Boolean).join('');
      return `<tr>
        <td class="num">${c.id}</td>
        <td><b>${esc(c.customer_name || '')}</b><br><small>${esc(c.phone || 'no phone')}</small></td>
        <td><small>${esc([c.order_id, c.product, c.amount ? `Rs ${c.amount}` : '', c.payment_mode].filter(Boolean).join(' · '))}</small></td>
        <td>${outcomeBadge(c.status)}</td>
        <td class="num">${c.attempts || 0}</td>
        <td><small>${esc([result, notes].filter(Boolean).join(' · '))}</small></td>
        <td class="num"><small>${esc(fmtShort(c.last_call_at))}</small></td>
        <td class="num"><small>${next}</small></td>
        <td><div class="acts">${acts}</div></td>
      </tr>`;
    }).join('');
    const empty = el('board-empty');
    empty.hidden = shown.length > 0;
    if (!shown.length) empty.textContent = rows.length ? 'No contacts in this group.' : 'No contacts yet. Upload the Excel sheet to start.';
  };
  // ---------------- call back: the queue of people to ring again ----------------
  let cbFilter = 'all';
  let cbRows = [];
  const WHY = { no_answer: 'Did not answer', busy: 'Was busy', dropped: 'Call ended without a result', call_later: 'Asked to be called later' };
  // datetime-local wants local wall time without a zone; the browser is assumed to be on Indian time.
  const toLocalInput = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const fmtWhen = (iso) => iso ? new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';

  window.loadCallbacks = async () => {
    let j;
    try { j = await fetch('/api/callbacks').then((r) => r.ok ? r.json() : null); } catch (e) { return; }
    if (!j) return;
    cbRows = j.rows;
    const counts = {};
    for (const c of cbRows) counts[c.status] = (counts[c.status] || 0) + 1;
    const chip = (key, label, tone, ico, n) => `<button class="${tone}" data-cb="${key}" aria-pressed="${cbFilter === key}">${icon(ico)}${esc(label)} <b>${n}</b></button>`;
    el('cb-chips').innerHTML = chip('all', 'All', '', 'minus', cbRows.length)
      + CALLBACK_STATUSES.filter((k) => counts[k]).map((k) => { const o = OUTCOME[k]; return chip(k, o[2], o[0], o[1], counts[k]); }).join('');
    const now = Date.now();
    const shown = cbRows.filter((c) => cbFilter === 'all' || c.status === cbFilter);
    el('cb-rows').innerHTML = shown.map((c) => {
      const d = c.last_details || {};
      const why = WHY[c.status] || c.status;
      const extra = c.status === 'call_later' && d.callback_time ? `“${d.callback_time}”` : (c.last_result && !WHY[c.status].toLowerCase().includes(c.last_result.toLowerCase()) ? c.last_result : '');
      const due = !c.next_try_at || new Date(c.next_try_at).getTime() <= now;
      const exhausted = c.status !== 'call_later' && (c.attempts || 0) >= j.max_attempts;
      return `<tr>
        <td><b>${esc(c.customer_name || '')}</b><br><small>${esc(c.phone || '')}${c.order_id ? ' · ' + esc(c.order_id) : ''}</small></td>
        <td>${outcomeBadge(c.status)}<br><small>${esc(why)}${extra ? ' · ' + esc(extra) : ''}</small></td>
        <td class="num">${c.attempts || 0}${exhausted ? '<br><small>no more automatic tries</small>' : ''}</td>
        <td class="num"><small>${esc(fmtWhen(c.last_call_at))}</small></td>
        <td><div class="when">
          <input type="datetime-local" value="${toLocalInput(c.next_try_at)}" data-when="${c.id}">
          <button class="tiny" data-save="${c.id}">Set</button>
          ${due ? '<span class="due">due now</span>' : ''}
        </div></td>
        <td><div class="acts">
          <button class="tiny" data-dial="${c.id}">${icon('phone')}Call now</button>
          <button class="tiny" data-giveup="${c.id}" title="Take off this list; goes to Needs review">${icon('x')}Give up</button>
        </div></td>
      </tr>`;
    }).join('');
    el('cb-empty').hidden = shown.length > 0;
    el('cb-note').textContent = cbRows.length
      ? (j.running ? 'The campaign is running: each of these is rung when its time comes.' : 'Start the campaign (Overview) to ring these when their time comes, or press Call now on one.')
      : '';
  };
  el('cb-chips').addEventListener('click', (e) => { const b = e.target.closest('[data-cb]'); if (b) { cbFilter = b.dataset.cb; window.loadCallbacks(); } });
  el('cb-rows').addEventListener('click', async (e) => {
    const save = e.target.closest('[data-save]');
    if (save) {
      const v = el('cb-rows').querySelector(`[data-when="${save.dataset.save}"]`).value;
      const r = await fetch(`/api/contacts/${save.dataset.save}/schedule`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ at: v || null }) });
      window.toast(r.ok ? (v ? `Will call at ${fmtWhen(v)}.` : 'Will call as soon as possible.') : 'Could not set the time.');
      window.loadCallbacks();
      return;
    }
    const gu = e.target.closest('[data-giveup]');
    if (gu) {
      const c = cbRows.find((r) => String(r.id) === gu.dataset.giveup);
      if (!confirm(`Stop calling ${c ? c.customer_name : 'this contact'}? They move to Needs review.`)) return;
      await fetch(`/api/contacts/${gu.dataset.giveup}/giveup`, { method: 'POST' });
      window.loadCallbacks();
      if (window.loadContacts) window.loadContacts();
      return;
    }
    const dial = e.target.closest('[data-dial]');
    if (dial) {
      const c = cbRows.find((r) => String(r.id) === dial.dataset.dial);
      if (!c) return;
      const to = camp && camp.dial_only_my_number ? `YOUR phone ${camp.my_number} (test mode)` : (c.phone || 'no phone');
      if (!confirm(`Call ${c.customer_name || 'this contact'} now?\n\nNumber: ${to}\n\nThis is a real, paid phone call.`)) return;
      const qs = new URLSearchParams({ voice: el('camp-voice').value || '', language: el('camp-lang').value || '' });
      const r = await fetch(`/api/calls/dial/${c.id}?${qs}`, { method: 'POST' });
      const jj = await r.json().catch(() => ({}));
      window.toast(r.ok ? `Dialing ${jj.dialed}…` : (jj.detail || 'Could not dial.'));
      window.loadCallbacks();
    }
  });
  el('cb-now-all').onclick = async () => {
    if (!cbRows.length) return;
    if (!confirm(`Make all ${cbRows.length} of these due right now? The campaign will ring them next (after anyone still "To call").`)) return;
    await Promise.all(cbRows.map((c) => fetch(`/api/contacts/${c.id}/schedule`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ at: null }) })));
    window.toast('All due now.');
    window.loadCallbacks();
  };
  setInterval(() => { if (el('page-callback').classList.contains('on')) window.loadCallbacks(); }, 5000);
  window.loadCallbacks();

  for (const id of ['board-chips', 'board-chips2']) el(id).addEventListener('click', (e) => {
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    boardFilter = b.dataset.filter;
    window.renderContacts(boardRows);
  });
  el('board-search').addEventListener('input', (e) => { boardQuery = e.target.value.trim().toLowerCase(); window.renderContacts(boardRows); });
  el('board-rows').addEventListener('click', async (e) => {
    const view = e.target.closest('[data-call]');
    if (view) { showTranscript(view.dataset.call); return; }
    const rq = e.target.closest('[data-requeue]');
    if (rq) {
      await fetch('/api/contacts/requeue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: [rq.dataset.requeue] }) });
      window.toast('Back on the list.');
      if (window.loadContacts) window.loadContacts();
      return;
    }
    const dial = e.target.closest('[data-dial]');
    if (dial) {
      const c = boardRows.find((r) => String(r.id) === dial.dataset.dial);
      if (!c) return;
      const to = camp && camp.dial_only_my_number ? `YOUR phone ${camp.my_number} (test mode)` : (c.phone || 'no phone');
      if (!confirm(`Call ${c.customer_name || 'this contact'} now?\n\nNumber: ${to}\nOrder: ${c.order_id || ''}\n\nThis is a real, paid phone call.`)) return;
      const qs = new URLSearchParams({ voice: el('camp-voice').value || '', language: el('camp-lang').value || '' });
      const r = await fetch(`/api/calls/dial/${c.id}?${qs}`, { method: 'POST' });
      const j = await r.json().catch(() => ({}));
      window.toast(r.ok ? `Dialing ${j.dialed}…` : (j.detail || 'Could not dial.'));
      if (window.loadContacts) window.loadContacts();
    }
  });
})();
