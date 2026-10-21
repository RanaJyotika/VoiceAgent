// Store orders: the orders from the main database for the chosen days, where each call stands, and a side
// panel per order (details, calls, notes; call now, schedule). Settings holds which orders get a call and
// automatic calling (auto.js runs that card).
(() => {
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const json = async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || `error ${r.status}`); return j; };
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }).then(json);
  const onPage = () => el('page-store').classList.contains('on');
  const when = (iso) => iso ? new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
  const mmss = (s) => `${Math.floor((s || 0) / 60)}:${String((s || 0) % 60).padStart(2, '0')}`;
  const day = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const rupees = (a) => a ? `₹${Number(a).toLocaleString('en-IN')}` : '';
  const ago = (iso) => {
    if (!iso) return 'not yet';
    const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : when(iso);
  };
  const PHONE = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg>';

  // The cards: who is left to call, who was reached, who did not pick up or was busy, who is booked for later.
  const VIEWS = [['left', 'Left to call', 'warning'], ['called', 'Called', 'good'], ['no_pick', "Didn't pick", 'serious'],
    ['busy', 'Busy', 'serious'], ['scheduled', 'Scheduled', 'accent'], ['all', 'All orders', '']];
  // The small line under a card's number. q: the day's limit on new orders (store_orders.quota).
  const NOTE = {
    left: (c, q) => c.calling ? `${c.calling} on a call now`
      : q && q.limit ? (q.room ? `${q.room} more today · limit ${q.limit} a day` : `batch of ${q.limit} done`) : 'not called yet',
    called: (c) => [c.confirmed && `${c.confirmed} confirmed`, c.cancelled && `${c.cancelled} cancelled`, c.review && `${c.review} need a look`].filter(Boolean).join(' · ') || 'none yet',
    no_pick: () => 'tried again later', busy: () => 'tried again later', scheduled: () => 'call booked for later',
    all: (c) => { const n = (c.not_calling || 0) + (c.not_picked || 0) + (c.too_late || 0); return n ? `${n} not being called` : ''; },
  };
  const PICK_TEXT = { newest: 'newest first', oldest: 'oldest first', random: 'picked at random' };
  // On a row, only the two tags that matter most; the side panel shows them all.
  const TAG_ORDER = ['Return risk', 'High Risk', 'Bad Address', 'COD cancelled (store)', 'Has a question', 'VIP customer',
    'COD confirmed (store)', 'High value', 'Repeat customer', 'New customer'];

  const view = { range: 'today', from: '', to: '', tab: 'left', q: '', tag: '', page: 1 };
  let data = null;
  let open = null;
  let busy = false;

  function setRange(r) {
    const now = new Date();
    const back = (n) => { const d = new Date(now); d.setDate(d.getDate() - n); return day(d); };
    view.range = r;
    if (r === 'today') view.from = view.to = day(now);
    else if (r === 'yesterday') view.from = view.to = back(1);
    else { view.from = back(Number(r) - 1); view.to = day(now); }
  }
  el('so-range').addEventListener('change', () => { setRange(el('so-range').value); view.page = 1; load(); });

  // ---------------- the list ----------------
  const qs = () => new URLSearchParams({ day_from: view.from, day_to: view.to, tab: view.tab, q: view.q, tag: view.tag, page: view.page, per_page: 25 }).toString();

  async function load() {
    if (busy) return;
    busy = true;
    try { data = await fetch(`/api/orders?${qs()}`).then(json); render(); }
    catch (e) { el('so-rows').innerHTML = ''; el('so-empty').hidden = false; el('so-empty').textContent = e.message; }
    finally { busy = false; }
  }

  function render() {
    const c = data.counts;
    el('so-views').innerHTML = VIEWS.map(([k, label, tone]) =>
      `<button role="tab" data-view="${k}" aria-selected="${view.tab === k}" class="${tone}"><span class="lbl"><i></i>${esc(label)}</span><b>${(c[k] || 0).toLocaleString('en-IN')}</b><small>${esc(NOTE[k](c, data.quota))}</small></button>`).join('');
    const keep = el('so-tag').value;
    el('so-tag').innerHTML = '<option value="">All tags</option>' + data.tags.map((t) => `<option>${esc(t)}</option>`).join('');
    el('so-tag').value = data.tags.includes(keep) ? keep : '';
    el('nav-store').textContent = c.left ? String(c.left) : '';
    el('so-empty').hidden = data.rows.length > 0;
    el('so-empty').textContent = view.tab === 'left' ? 'Nobody left to call.' : 'No orders here.';
    el('so-rows').innerHTML = data.rows.map(rowHtml).join('');
    const first = (data.page - 1) * 25 + 1;
    el('so-showing').textContent = data.total ? `${first}–${first + data.rows.length - 1} of ${data.total.toLocaleString('en-IN')}` : '';
    el('so-pages').innerHTML = pager(data.page, data.pages);
    el('so-export').href = `/api/orders/export?${qs()}`;
  }

  function pager(page, pages) {
    if (pages <= 1) return '';
    const nums = [...new Set([1, page - 1, page, page + 1, pages])].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
    let out = `<button data-page="${page - 1}" ${page === 1 ? 'disabled' : ''} aria-label="Previous">‹</button>`;
    let last = 0;
    for (const n of nums) { if (n - last > 1) out += '<span class="hint">…</span>'; out += `<button data-page="${n}" ${n === page ? 'aria-current="page"' : ''}>${n}</button>`; last = n; }
    return out + `<button data-page="${page + 1}" ${page === pages ? 'disabled' : ''} aria-label="Next">›</button>`;
  }

  const tagHtml = (t) => `<span class="so-tag ${t.tone || ''}">${esc(t.label)}</span>`;
  const topTags = (tags) => tags.filter((t) => TAG_ORDER.includes(t.label))
    .sort((a, b) => TAG_ORDER.indexOf(a.label) - TAG_ORDER.indexOf(b.label)).slice(0, 2);
  const itemsHtml = (items) => `<div class="so-items">${items.map((i) => `<div class="so-item">${i.image
    ? `<img src="${esc(i.image + (i.image.includes('?') ? '&' : '?') + 'width=80')}" alt="" loading="lazy">` : '<span class="noimg"></span>'}<span><b>${esc(i.title)}</b><small>${i.variant ? esc(i.variant) + ' · ' : ''}Qty ${i.qty}</small></span></div>`).join('')}</div>`;

  function callLine(r) {
    if (r.state === 'scheduled' && r.next_try_at) return `at ${when(r.next_try_at)}`;
    if (['no_pick', 'busy'].includes(r.state) && r.next_try_at) return `tries again ${when(r.next_try_at)}`;
    if (r.last_call) return `${when(r.last_call.at)} · ${mmss(r.last_call.seconds)}`;
    return '';
  }
  const canCall = (r) => r.state !== 'calling';
  const callText = (r) => r.state === 'calling' ? 'Calling…' : r.last_call ? 'Call again' : 'Call';

  function rowHtml(r) {
    const line = callLine(r);
    return `<tr data-order="${esc(r.name)}">
      <td><b>${esc(r.name)}</b><br><small>${esc(when(r.created_at))}</small></td>
      <td>${esc(r.customer || '—')}<br><small>${esc(r.phone ? '+91 ' + r.phone : 'no phone')}</small><div class="so-tags">${topTags(r.tags).map(tagHtml).join('')}</div></td>
      <td>${itemsHtml(r.items)}</td>
      <td class="num">${rupees(r.amount)}<br><small>${esc(r.payment)}</small></td>
      <td><span class="badge ${r.state_tone} ${r.state === 'calling' ? 'pulse' : ''}"><span class="dot"></span>${esc(r.state_label)}</span>${line ? `<br><small>${esc(line)}</small>` : ''}</td>
      <td><button data-act="call" ${canCall(r) ? '' : 'disabled'}>${PHONE}${callText(r)}</button></td>
    </tr>`;
  }

  el('so-views').addEventListener('click', (e) => { const b = e.target.closest('[data-view]'); if (b) { view.tab = b.dataset.view; view.page = 1; load(); } });
  el('so-pages').addEventListener('click', (e) => { const b = e.target.closest('[data-page]'); if (b && !b.disabled) { view.page = Number(b.dataset.page); load(); } });
  el('so-tag').addEventListener('change', () => { view.tag = el('so-tag').value; view.page = 1; load(); });
  let typing;
  el('so-q').addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => { view.q = el('so-q').value.trim(); view.page = 1; load(); }, 350); });
  el('so-rows').addEventListener('click', async (e) => {
    const tr = e.target.closest('tr[data-order]');
    if (!tr) return;
    if (e.target.closest('[data-act="call"]')) await callNow(tr.dataset.order);
    else await openOrder(tr.dataset.order);
  });

  // ---------------- one order ----------------
  async function callNow(name) {
    try {
      const contact = await post('/api/order/contact', { name });
      const auto = await fetch('/api/auto').then(json);
      const to = auto.dial_only_my_number ? `YOUR phone ${auto.my_number} (test mode)` : (contact.phone || 'no phone number');
      if (!confirm(`Call ${contact.customer_name || 'the customer'} about order ${name} now?\n\nRings: ${to}\n\nThis is a real, paid phone call.`)) return;
      const p = new URLSearchParams({ voice: el('auto-voice').value, model: el('auto-model').value, language: el('auto-lang').value, build: window.BUILD || '' });
      await fetch(`/api/calls/dial/${contact.id}?${p}`, { method: 'POST' }).then(json);
      window.toast('Calling. It shows on Home under Live calls.');
      setTimeout(load, 1500);
      if (open && open.name === name) setTimeout(() => openOrder(name), 1500);
    } catch (e) { window.toast(e.message); }
  }

  async function openOrder(name) {
    try { const o = await fetch(`/api/order?${new URLSearchParams({ name })}`).then(json); open = o; drawer(); }
    catch (e) { window.toast(e.message); }
  }

  function drawer() {
    const o = open;
    const kv = (k, v) => v ? `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>` : '';
    const st = o.store_status || {};
    const calls = o.calls.map((c) => `<div class="so-note"><b>${esc(window.outcomeLabel ? window.outcomeLabel(c.outcome || (c.error ? 'error' : 'none')) : c.outcome)}</b> · ${mmss(c.seconds)}
      <small>${esc(when(c.at))}${c.channel === 'browser' ? ' · browser test' : ''}</small><button data-view="${c.id}">Transcript</button></div>`).join('');
    const notes = o.notes.map((n) => `<div class="so-note">${esc(n.text)}<small>${esc(when(n.created_at))}</small></div>`).join('');
    el('so-drawer').innerHTML = `<header><h3>Order ${esc(o.name)}<button id="so-close" aria-label="Close">×</button></h3>
        <div class="so-tags">${o.tags.map(tagHtml).join('')}</div></header>
      <div class="body">
        <h4>Call</h4>${kv('Status', o.state_label)}${kv('Tries', o.attempts ? String(o.attempts) : '')}${kv('Result', o.result)}${kv('Next call', o.next_try_at ? when(o.next_try_at) : '')}
        ${calls}
        <div class="row2"><input class="fld" type="datetime-local" id="so-when" aria-label="Call at"><button id="so-when-save">Schedule</button></div>
        <h4>Customer</h4><b>${esc(o.customer || '—')}</b>${kv('Phone', o.phone ? '+91 ' + o.phone : '')}${kv('Email', o.email)}${kv('Earlier orders', String(o.earlier_orders))}
        <div style="margin-top:4px">${esc(o.address || '')}</div>
        <h4>Order</h4>${itemsHtml(o.items)}
        <div style="margin-top:8px">${kv('Amount', rupees(o.amount))}${kv('Payment', o.payment)}${kv('Placed', when(o.created_at))}${kv('Delivery', st.delivery)}${kv('Courier', st.courier)}${kv('Tracking', st.tracking)}</div>
        <h4>Notes</h4>${notes}
        <div class="row2"><textarea class="fld" id="so-note-text" placeholder="A note for the team"></textarea><button id="so-note-save">Add</button></div>
      </div>
      <footer><button id="so-d-call" class="call" ${o.state === 'calling' ? 'disabled' : ''}>${PHONE}${o.state === 'calling' ? 'Calling…' : o.calls.length ? 'Call again' : 'Call now'}</button></footer>`;
    el('so-drawer').hidden = false;
  }

  const closeDrawer = () => { el('so-drawer').hidden = true; open = null; };
  el('so-drawer').addEventListener('click', async (e) => {
    const t = e.target.closest('button');
    if (!t || !open) return;
    const name = open.name;
    if (t.id === 'so-close') return closeDrawer();
    if (t.dataset.view) return window.showTranscript && window.showTranscript(t.dataset.view);
    if (t.id === 'so-d-call') return callNow(name);
    if (t.id === 'so-when-save') {
      if (!el('so-when').value) return window.toast('Pick a date and time first.');
      try { await post('/api/order/schedule', { name, at: el('so-when').value }); window.toast('Scheduled. Automatic calling rings it then.'); openOrder(name); load(); }
      catch (err) { window.toast(err.message); }
      return;
    }
    if (t.id === 'so-note-save') {
      try { await post('/api/order/note', { name, text: el('so-note-text').value }); openOrder(name); }
      catch (err) { window.toast(err.message); }
    }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && open) closeDrawer(); });

  // ---------------- automatic calling bar and settings ----------------
  el('so-settings-btn').onclick = () => {
    const show = el('so-settings').hidden;
    el('so-settings').hidden = !show;
    el('so-settings-btn').setAttribute('aria-expanded', String(show));
  };
  let source = null;   // /api/store: since when orders are read, the give-up hours, the day's limit
  const daysText = (q) => (q && q.days_label ? q.days_label : 'Any waiting order').toLowerCase();
  const limitText = (q) => q && q.limit ? `at most ${q.limit} a day, from ${daysText(q)}, ${PICK_TEXT[q.pick] || q.pick} (${q.picked_today} in this batch)` : '';
  const rules = () => {
    if (!source || !source.since) return '';
    const q = source.quota || {};
    const which = q.limit ? limitText(q) : `${daysText(q)}, ${PICK_TEXT[q.pick] || 'newest first'}`;
    return ` <span class="hint">Calls only orders placed after ${esc(when(source.since))}, ${esc(which)}. An order not called within ${source.settings.order_max_hours} h is dropped.</span>`;
  };
  async function loadBar() {
    try {
      const a = await fetch('/api/auto').then(json);
      const s = a.settings || {};
      const bar = el('so-autobar');
      bar.className = `so-autobar ${a.running ? 'on' : 'off'}`;
      bar.innerHTML = (a.running
        ? `<span class="dot"></span><span><b>Automatic calling is on</b> · ${s.start_hour}:00 to ${s.end_hour}:00${s.budget_rs ? ` · up to ₹${s.budget_rs} a day` : ''}${a.calls_made ? ` · ${a.calls_made} calls so far` : ''}${a.dial_only_my_number ? ' · test mode: rings your own phone' : ''}</span>`
        : `<span class="dot"></span><span><b>Automatic calling is off.</b> Orders under "Left to call" are not being rung. Call one by one, or switch it on in Settings.</span><button data-open-settings>Settings</button>`)
        .replace(/<\/span>(<button|$)/, `${rules()}</span>$1`);
    } catch (e) { /* server away */ }
  }
  el('so-autobar').addEventListener('click', (e) => {
    if (e.target.closest('[data-open-settings]')) { el('so-settings').hidden = false; el('so-settings-btn').setAttribute('aria-expanded', 'true'); el('auto-card').scrollIntoView({ block: 'start', behavior: 'smooth' }); }
  });

  let filled = false;
  async function loadSource() {
    try {
      const s = await fetch('/api/store').then(json);
      source = s;
      if (!filled) {
        filled = true;
        el('store-orders').value = s.settings.orders;
        el('store-wait').value = s.settings.order_wait_minutes;
        el('store-max').value = s.settings.order_max_hours;
        el('store-limit').value = s.settings.order_daily_limit ?? 0;
        el('store-pick').value = s.settings.order_pick || 'newest';
        el('store-days').value = s.settings.order_days || 'all';
      }
      el('store-badge').innerHTML = !s.enabled ? '<span class="badge warning"><span class="dot"></span>Not connected</span>'
        : s.error ? '<span class="badge critical"><span class="dot"></span>Cannot read</span>' : '<span class="badge good"><span class="dot"></span>Reading</span>';
      el('store-sub').textContent = s.enabled ? `checked ${ago(s.last_check)}` : 'Add MONGODB_URI to .env and restart.';
      const q = s.quota || {};
      el('store-counts').textContent = [s.since ? `New orders are added from ${when(s.since)}.` : '',
        q.limit ? `Automatic calling rings ${limitText(q)}; the rest are not called.`
          : `Automatic calling rings ${daysText(q)}, ${PICK_TEXT[q.pick] || 'newest first'}, with no daily limit.`].filter(Boolean).join(' ');
      el('store-warn').hidden = !s.error;
      el('store-warn').textContent = s.error ? `The database could not be read: ${s.error}` : '';
    } catch (e) { /* server away */ }
  }
  el('store-save').onclick = async () => {
    const r = await fetch('/api/shopify/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orders: el('store-orders').value, order_wait_minutes: el('store-wait').value, order_max_hours: el('store-max').value,
        order_daily_limit: el('store-limit').value, order_pick: el('store-pick').value, order_days: el('store-days').value }) });
    window.toast(r.ok ? 'Saved.' : 'Could not save.');
    loadSource().then(loadBar);
    load();
  };

  // "Don't call orders that are...": switches, any number on at once (owner, 19 Oct 2026). Each one takes effect at
  // once, and turning one off puts its orders back in the queue (store_orders.SKIP_RULES).
  const skipBox = document.createElement('div');
  skipBox.innerHTML = `<div class="hint" style="margin-bottom:6px"><b>Don't call orders that are</b> (tap to switch; any number at once)</div>
    <div class="chips" id="store-skip" role="group" aria-label="Don't call orders that are"></div>
    <div class="hint" id="store-skip-hint" style="margin:-4px 0 10px"></div>`;
  el('store-counts').before(skipBox);
  let skip = null;
  const CHECK = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
  function renderSkip() {
    el('store-skip').innerHTML = skip.rules.map((r) =>
      `<button type="button" data-skip="${esc(r.key)}" aria-pressed="${r.on}">${r.on ? CHECK : ''}${esc(r.label)}</button>`).join('');
    const on = skip.rules.filter((r) => r.on).length;
    el('store-skip-hint').textContent = on
      ? `${skip.not_calling.toLocaleString('en-IN')} waiting order${skip.not_calling === 1 ? '' : 's'} not being called because of these. They show as "No call" with the reason.`
      : 'All switches off: every order on the list gets a call.';
  }
  async function loadSkip() {
    try { skip = await fetch('/api/store/skip').then(json); renderSkip(); } catch (e) { /* server away */ }
  }
  el('store-skip').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-skip]');
    if (!b || !skip) return;
    const on = skip.rules.filter((r) => (r.key === b.dataset.skip ? !r.on : r.on)).map((r) => r.key);
    b.disabled = true;
    try {
      skip = await fetch('/api/store/skip', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }) }).then(json);
      renderSkip();
      const c = skip.changed;
      window.toast(c && (c.skipped || c.back) ? `Saved. ${c.skipped} order(s) taken off the queue, ${c.back} put back.` : 'Saved.');
      load();
    } catch (err) { window.toast(err.message); b.disabled = false; }
  });

  // ---------------- batches: each batch of automatic calling, what it reached and what it cost (owner, 19 Oct 2026) ----------------
  const rs = (n) => n == null ? '—' : `₹${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const batchDay = (b) => new Date(`${b.day}T00:00:00`).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
  async function loadBatches() {
    let d;
    try { d = await fetch('/api/store/batches').then(json); } catch (e) { return; }
    const c = d.current || {};
    el('so-batch-now').textContent = c.id
      ? `Now: batch ${c.id}, ${c.picked} called${c.limit ? ` of ${c.limit} (${c.room} left)` : ' (no daily limit)'}, since ${when(c.started_at)}`
      : 'The next batch starts with the next order automatic calling rings.';
    // A day can hold several batches; number them within the day for the team ("19 Oct · 2").
    const perDay = {};
    for (const b of [...d.batches].reverse()) b.n = (perDay[b.day] = (perDay[b.day] || 0) + 1);
    el('so-batch-empty').hidden = d.batches.length > 0;
    el('so-batch-rows').innerHTML = d.batches.map((b) => `<tr>
      <td><b>${esc(batchDay(b))}${b.n > 1 ? ` · ${b.n}` : ''}</b><br><small>${esc(when(b.started_at))}${b.size ? ` · limit ${b.size}` : ''}</small></td>
      <td class="num">${b.orders}<br><small>${b.calls} call${b.calls === 1 ? '' : 's'}</small></td>
      <td class="num">${b.answered}</td>
      <td class="num">${b.confirmed}</td>
      <td class="num">${b.cancelled}</td>
      <td class="num">${b.not_reached}</td>
      <td class="num" title="Picked up, then the call ended before an answer">${b.hung_up}</td>
      <td class="num" title="Call back later, still waiting, or needs a look">${b.call_later + b.waiting + b.other}</td>
      <td class="num">${b.minutes} min</td>
      <td class="num"><b>${rs(b.cost_rs)}</b></td>
      <td class="num">${rs(b.per_order_rs)}</td>
      <td class="num">${rs(b.per_confirmed_rs)}</td>
    </tr>`).join('');
  }
  el('so-batch-new').addEventListener('click', async () => {
    if (!confirm('Start a new batch now? The next orders automatic calling rings count as a new batch, and the "Calls a day" number counts afresh from now.')) return;
    try { await post('/api/store/batches/new'); window.toast('New batch started.'); loadSource().then(loadBar); loadBatches(); }
    catch (e) { window.toast(e.message); }
  });

  window.loadStore = () => { load(); loadSource().then(loadBar); loadSkip(); loadBatches(); };
  setRange('today');
  window.loadStore();
  setInterval(() => { if (onPage() && !document.querySelector('#so-drawer textarea:focus, #so-drawer input:focus')) window.loadStore(); }, 10000);
})();
