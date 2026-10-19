// Coupons: the codes the abandoned-cart agent offers (once, to a customer who says no), the form that adds or
// changes one with a live preview of its message, and the outbox of coupon messages. Until a message service
// is set in .env, a person sends those by hand from here: Open in WhatsApp (or Copy text), then Mark as sent.
(() => {
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // An error keeps its HTTP status: a 404 on a list means the server is older than this page.
  const json = async (r) => {
    const j = await r.json().catch(() => ({}));
    if (r.ok) return j;
    const detail = typeof j.detail === 'string' ? j.detail : (Array.isArray(j.detail) && j.detail[0] && j.detail[0].msg) || `error ${r.status}`;
    throw Object.assign(new Error(detail), { status: r.status });
  };
  const post = (url, body, method = 'POST') => fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }).then(json);
  const onPage = () => el('page-coupons').classList.contains('on');
  // The server keeps UTC; shown in Indian time, whatever this PC's clock says.
  const when = (iso) => iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
  // Coupon dates are Indian days (YYYY-MM-DD); the year is shown only when it is not this one.
  const thisYear = () => new Date(Date.now() + 330 * 60000).toISOString().slice(0, 4);
  const fmtDay = (d) => d ? new Date(`${d}T00:00:00Z`).toLocaleDateString('en-IN',
    { timeZone: 'UTC', day: 'numeric', month: 'short', ...(d.slice(0, 4) !== thisYear() ? { year: 'numeric' } : {}) }) : '';
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const sentence = (s) => (/[.!?]$/.test(s) ? s : `${s}.`);

  // The icons this page uses, drawn as dashboard.js draws its own: every badge is an icon and words.
  const P = {
    check: '<path d="M20 6 9 17l-5-5"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    minus: '<path d="M5 12h14"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
    chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    trash: '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4Z"/>',
    retry: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  };
  const icon = (name) => `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || P.minus}</svg>`;
  const badge = (tone, ico, label) => `<span class="badge ${tone}">${icon(ico)}${esc(label)}</span>`;

  // A redraw replaces the buttons, so the focus goes back to the same one (or, failing that, to the
  // one the fallback names). A refresh that changed nothing does not redraw at all.
  function redraw(box, html, fallback) {
    const a = document.activeElement;
    const inside = !!a && a !== document.body && box.contains(a);
    const key = inside ? [...a.attributes].filter((x) => x.name.startsWith('data-')).map((x) => `[${x.name}="${CSS.escape(x.value)}"]`).join('') : '';
    const back = inside && fallback ? fallback(a) : '';
    box.innerHTML = html;
    const b = (key && box.querySelector(key)) || (back && box.querySelector(back));
    if (b) b.focus({ preventScroll: true });
  }

  // The page is newer than the Python behind it: the server was not restarted after the update.
  // Each list clears only its own mark, so one that answers does not hide the other's 404.
  let staleWarned = false;
  const staleFrom = new Set();
  function stale(from, on) {
    if (on) staleFrom.add(from); else staleFrom.delete(from);
    el('cp-stale').hidden = !staleFrom.size;
    if (on && !staleWarned) { staleWarned = true; window.toast('Restart the server: it is running older code.'); }
  }

  // WhatsApp shows *words* in bold and links as links; the preview and the outbox do the same.
  const waHtml = (text) => esc(text)
    .replace(/\*(\S(?:[^*\n]*\S)?)\*/g, '<b>$1</b>')
    .replace(/https?:\/\/[^\s<]+/g, (u) => {
      const [, link, tail] = u.match(/^(.*?)([.,!?)]*)$/);
      return `<a href="${link}" target="_blank" rel="noopener">${link}</a>${tail}`;
    });

  // ---------------- the coupon codes ----------------
  // A coupon's state, worked out by the server in Indian dates.
  const STATE = { live: ['good', 'check', 'Live'], off: ['', 'minus', 'Off'], expired: ['serious', 'alert', 'Expired'], not_started: ['warning', 'clock', 'Not started'] };
  const NOT_LIVE = { off: 'is switched off', expired: 'has expired', not_started: 'has not started yet' };
  const stateBadge = (c) => {
    const s = STATE[c.state] || STATE.off;
    return badge(s[0], s[1], c.state === 'not_started' && c.valid_from ? `Starts ${fmtDay(c.valid_from)}` : s[2]);
  };
  const validText = (c) => {
    const f = c.valid_from, u = c.valid_until;
    if (!f && !u) return 'Always';
    if (f && u) return f === u ? `Only on ${fmtDay(f)}` : `${fmtDay(f)} – ${fmtDay(u)}`;
    return f ? `From ${fmtDay(f)}` : `Until ${fmtDay(u)}`;
  };
  // On / Off: the coupon's own switch. Off = never offered on a call and never sent.
  const toggle = (c) => `<div class="seg cp-seg" role="group" aria-label="${esc(c.code)} on or off">`
    + `<button data-active="1" data-id="${esc(c.id)}" aria-pressed="${!!c.active}">On</button>`
    + `<button data-active="0" data-id="${esc(c.id)}" aria-pressed="${!c.active}">Off</button></div>`;

  function couponRow(c) {
    const m = c.messages || {};
    const count = [m.sent ? `${m.sent} sent` : '', m.waiting ? `${m.waiting} waiting` : ''].filter(Boolean).join(' · ');
    // The agent offers the one live coupon marked for calls; marked but not live, it offers none.
    const calls = !c.offer_on_calls
      ? `<button class="tiny" data-offer="${esc(c.id)}" aria-label="Offer on calls: ${esc(c.code)}">${icon('phone')}Offer on calls</button>`
      : c.state === 'live' ? badge('accent', 'phone', 'Offered on calls')
        : `${badge('', 'phone', 'Picked for calls')}<br><small>${esc(c.state === 'not_started' && c.valid_from
          ? `offered from ${fmtDay(c.valid_from)}` : `not offered: it ${NOT_LIVE[c.state] || 'is not live'}`)}</small>`;
    return `<tr>
      <td><b class="cp-code">${esc(c.code)}</b>${c.description ? `<br><small>${esc(c.description)}</small>` : ''}</td>
      <td>${esc(c.offer_text || `${c.discount || ''} off`)}</td>
      <td data-label="Valid">${esc(validText(c))}</td>
      <td>${calls}</td>
      <td><div class="cp-status">${stateBadge(c)}${toggle(c)}</div></td>
      <td data-label="Messages">${count ? esc(count) : '<small>none yet</small>'}</td>
      <td><div class="cp-acts">
        <button class="tiny" data-edit="${esc(c.id)}" aria-label="Edit ${esc(c.code)}">${icon('edit')}Edit</button>
        <button class="tiny danger" data-del="${esc(c.id)}" aria-label="Delete ${esc(c.code)}">${icon('trash')}Delete</button>
      </div></td>
    </tr>`;
  }

  let data = null;   // GET /api/coupons
  let couponSeq = 0;
  let couponsShown = '';

  function renderCoupons() {
    const list = data.coupons || [];
    const def = data.default_message || '';
    if (el('cp-message').placeholder !== def) el('cp-message').placeholder = def;
    renderPlaces(data.placeholders);
    el('cp-sub').innerHTML = data.call_coupon
      ? `The agent offers <b class="cp-code">${esc(data.call_coupon)}</b> on abandoned-cart calls: once, to a customer who says no.`
      : list.length ? 'No coupon is offered on calls right now. Press <b>Offer on calls</b> on a live one.' : '';
    el('cp-empty').hidden = list.length > 0;
    el('cp-empty').innerHTML = 'No coupons yet. Press <b>Add coupon</b> to make the first one.';
    redraw(el('cp-rows'), list.map(couponRow).join(''));
    if (!el('cp-form').hidden) syncForm();
  }

  async function loadCouponList(force = false) {
    const seq = ++couponSeq;
    try {
      const j = await fetch('/api/coupons').then(json);
      if (seq !== couponSeq) return;   // a newer load is on its way; it wins
      stale('coupons', false);
      data = j;
      const sig = JSON.stringify(j);
      if (force || sig !== couponsShown) { couponsShown = sig; renderCoupons(); }
    } catch (e) {
      if (e.status === 404) stale('coupons', true);
      else if (!data && e.status) { el('cp-empty').hidden = false; el('cp-empty').textContent = e.message; }
    }
  }

  const couponOf = (id) => ((data && data.coupons) || []).find((c) => String(c.id) === String(id));

  async function setActive(c, on) {
    if (!!c.active === on) return;
    const w = (c.messages || {}).waiting || 0;
    if (!on && w && !confirm(`Switch off ${c.code}?\n\n${plural(w, 'message')} with this code ${w === 1 ? 'is' : 'are'} still waiting. Switched off, the coupon is not sent to anyone.`)) return;
    try {
      const s = await post(`/api/coupons/${c.id}`, { active: on }, 'PUT');
      window.toast(on ? `${s.code} is on.${s.offer_on_calls && s.state === 'live' ? ' The agent offers it on abandoned-cart calls.' : ''}`
        : `${s.code} is off.${c.offer_on_calls ? ' The agent offers no coupon on calls now.' : ''}`);
    } catch (err) { window.toast(err.message); }
    loadCouponList(true);
    loadMessages(true);   // its waiting messages say whether the customer can still use the code
  }

  async function offerOnCalls(c) {
    const now = data && data.call_coupon;
    const q = c.state !== 'live'
      ? `${c.code} ${NOT_LIVE[c.state] || 'is not live'}. Until it is live, the agent offers no coupon on calls${now ? `, not even ${now}` : ''}.\n\nPick it for calls anyway?`
      : now && now !== c.code ? `Offer ${c.code} on abandoned-cart calls instead of ${now}?` : '';
    if (q && !confirm(q)) return;
    try {
      await post(`/api/coupons/${c.id}`, { offer_on_calls: true }, 'PUT');
      window.toast(c.state === 'live' ? `The agent now offers ${c.code} on abandoned-cart calls.` : `${c.code} is picked for calls. The agent offers it once it is live.`);
    } catch (err) { window.toast(err.message); }
    loadCouponList(true);
  }

  async function removeCoupon(c) {
    const m = c.messages || {};
    const lines = [`Delete coupon ${c.code}?`];
    if (c.offer_on_calls) lines.push('The agent stops offering it on abandoned-cart calls.');
    if (m.waiting) lines.push(`${plural(m.waiting, 'message')} with this code ${m.waiting === 1 ? 'is' : 'are'} still waiting; ${m.waiting === 1 ? 'it is' : 'they are'} not sent.`);
    if (m.sent) lines.push('Messages already sent stay on record.');
    if (!confirm(lines.join('\n\n'))) return;
    try {
      await fetch(`/api/coupons/${c.id}`, { method: 'DELETE' }).then(json);
      window.toast(`Coupon ${c.code} deleted.`);
      if (editing && editing.id === c.id) closeForm();
    } catch (err) { window.toast(err.message); }
    loadCouponList(true);
    loadMessages(true);
  }

  el('cp-rows').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    const d = b.dataset;
    const c = couponOf(d.edit || d.del || d.offer || d.id);
    if (!c) return;
    if (d.edit) openForm(c);
    else if (d.del) removeCoupon(c);
    else if (d.offer) offerOnCalls(c);
    else if (d.active) setActive(c, d.active === '1');
  });

  // ---------------- add or change a coupon ----------------
  let editing = null;   // the coupon in the form; null = a new one
  const FORM = ['cp-code', 'cp-type', 'cp-value', 'cp-min', 'cp-from', 'cp-until', 'cp-prepaid', 'cp-calls', 'cp-desc', 'cp-message'];
  // What each {word} in a message becomes. Which words there are comes from the server.
  const PLACE = {
    name: 'the customer\'s name, with "ji"', product: 'the product in the cart', amount: 'the cart value',
    offer: 'the offer in words', discount: '10% or ₹100', code: 'the coupon code', link: 'the cart link, or the product page',
    store: 'the store name', min_order: 'the minimum order, if there is one', pay: '"Online payment karein"',
  };
  let placesShown = '';
  function renderPlaces(keys) {
    const list = keys && keys.length ? keys : Object.keys(PLACE);
    if (list.join() === placesShown) return;
    placesShown = list.join();
    el('cp-places').innerHTML = list.map((k) => `<span><code>{${esc(k)}}</code> ${esc(PLACE[k] || '')}</span>`).join('');
  }

  const formError = (text) => { el('cp-error').hidden = !text; el('cp-error').textContent = text; };

  // The bits of the form that follow the rest: the unit, the date limits, which default-message button
  // shows, and what ticking "Offer this on abandoned-cart calls" does to the coupon offered now.
  function syncForm() {
    const pct = el('cp-type').value === 'percent';
    el('cp-unit').textContent = pct ? '%' : '₹';
    el('cp-value').placeholder = pct ? '10' : '100';
    if (pct) el('cp-value').max = '100'; else el('cp-value').removeAttribute('max');
    if (el('cp-from').value) el('cp-until').min = el('cp-from').value; else el('cp-until').removeAttribute('min');
    const custom = el('cp-message').value.trim() !== '';
    el('cp-default').hidden = !custom;
    el('cp-copy-default').hidden = custom;
    const marked = ((data && data.coupons) || []).find((x) => x.offer_on_calls);
    const mine = !!(editing && marked && marked.id === editing.id);
    el('cp-calls-note').textContent = el('cp-calls').checked
      ? (marked && !mine ? `Replaces ${marked.code}: one coupon is offered on calls at a time.` : '')
      : (mine ? 'Unticked, the agent offers no coupon on calls.' : '');
  }

  const num = (id) => { const v = el(id).value.trim(); return v === '' ? '' : Number(v); };
  function formBody() {
    const msg = el('cp-message').value.trim();
    return {
      code: el('cp-code').value.trim(), discount_type: el('cp-type').value, discount_value: num('cp-value'),
      prepaid_only: el('cp-prepaid').checked, min_order: num('cp-min') || 0,
      valid_from: el('cp-from').value, valid_until: el('cp-until').value, offer_on_calls: el('cp-calls').checked,
      description: el('cp-desc').value.trim(),
      // An unchanged copy of the default is the default: it then follows any later change to it.
      message: data && msg === (data.default_message || '').trim() ? '' : msg,
    };
  }

  function openForm(c) {
    editing = c || null;
    el('cp-form-title').textContent = c ? `Edit ${c.code}` : 'New coupon';
    el('cp-save').textContent = c ? 'Save changes' : 'Add coupon';
    el('cp-code').value = c ? c.code : '';
    el('cp-type').value = c && c.discount_type === 'amount' ? 'amount' : 'percent';
    el('cp-value').value = c ? c.discount_value : '';
    el('cp-min').value = c ? (c.min_order || 0) : 0;
    el('cp-from').value = (c && c.valid_from) || '';
    el('cp-until').value = (c && c.valid_until) || '';
    el('cp-prepaid').checked = c ? !!c.prepaid_only : true;
    el('cp-calls').checked = !!(c && c.offer_on_calls);
    el('cp-desc').value = (c && c.description) || '';
    el('cp-message').value = (c && c.message) || '';
    formError('');
    syncForm();
    el('cp-form').hidden = false;
    el('cp-form').scrollIntoView({ block: 'start', behavior: 'smooth' });
    el('cp-code').focus({ preventScroll: true });
    preview(true);
  }

  function closeForm() {
    editing = null;
    el('cp-form').hidden = true;
    clearTimeout(previewTimer);
    previewSeq++;   // a preview still on its way is dropped
  }

  el('cp-add').onclick = () => openForm(null);
  el('cp-cancel').onclick = () => { closeForm(); el('cp-add').focus(); };

  el('cp-save').onclick = async () => {
    const body = formBody();
    const pct = body.discount_type === 'percent';
    if (!body.code) { formError('Type the coupon code, as the customer types it at checkout.'); el('cp-code').focus(); return; }
    if (body.discount_value === '') { formError(pct ? 'Type the discount, e.g. 10 for 10% off.' : 'Type the rupees off, e.g. 100.'); el('cp-value').focus(); return; }
    const wasNew = !editing;
    if (wasNew) body.active = true;
    el('cp-save').disabled = true;
    try {
      const c = await post(wasNew ? '/api/coupons' : `/api/coupons/${editing.id}`, body, wasNew ? 'POST' : 'PUT');
      closeForm();
      window.toast(`${c.code} ${wasNew ? 'added' : 'saved'}.${!c.offer_on_calls ? ''
        : c.state === 'live' ? ' The agent offers it on abandoned-cart calls.' : ' The agent offers it on calls once it is live.'}`);
      el('cp-add').focus();
      loadCouponList(true);
    } catch (err) { formError(err.message); }
    finally { el('cp-save').disabled = false; }
  };

  // The code as the customer types it at checkout: capitals, no spaces (the server cleans it the same way).
  el('cp-code').addEventListener('input', () => {
    const f = el('cp-code');
    const clean = f.value.toUpperCase().replace(/\s+/g, '');
    if (clean === f.value) return;
    const at = f.value.slice(0, f.selectionStart ?? f.value.length).toUpperCase().replace(/\s+/g, '').length;
    f.value = clean;
    f.setSelectionRange(at, at);
  });

  el('cp-default').onclick = () => { el('cp-message').value = ''; syncForm(); preview(true); el('cp-message').focus(); };
  el('cp-copy-default').onclick = () => {
    const t = el('cp-message');
    t.value = (data && data.default_message) || '';
    syncForm();
    preview(true);
    t.focus();
    t.setSelectionRange(0, 0);
    t.scrollTop = 0;
  };

  // The message as the customer will get it, with a sample customer, redrawn a moment after typing stops.
  let previewTimer = null;
  let previewSeq = 0;
  function preview(now = false) {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const seq = ++previewSeq;
      try {
        const j = await post('/api/coupons/preview', formBody());
        if (seq !== previewSeq) return;   // typed on since; the newer one wins
        el('cp-preview').innerHTML = j.text ? waHtml(j.text) : '<span class="hint">Nothing to send: the message is empty.</span>';
        // A line whose word has nothing to fill it is left out; the SMS is one line, so it can come back empty.
        el('cp-sms').textContent = j.sms || 'Empty for now: one of its words has nothing to fill it yet, such as the code.';
        // {words} the server does not know go to the customer as typed: most likely a typo ({naam} for {name}).
        const odd = Array.isArray(j.unknown) ? j.unknown : [];
        el('cp-unknown').hidden = !odd.length;
        el('cp-unknown').textContent = odd.length ? `Not filled in, the customer gets these as typed: ${odd.map((k) => `{${k}}`).join(', ')}. Check the spelling against the list.` : '';
      } catch (err) {
        if (seq !== previewSeq) return;
        el('cp-unknown').hidden = true;
        el('cp-preview').innerHTML = `<span class="hint">${esc(err.status === 404 ? 'Restart the server to see the preview.' : `No preview: ${err.message}`)}</span>`;
        el('cp-sms').textContent = '';
      }
    }, now ? 0 : 400);
  }
  for (const id of FORM) for (const ev of ['input', 'change']) el(id).addEventListener(ev, () => { syncForm(); preview(); });

  // ---------------- the messages ----------------
  const MSG = {
    queued: ['accent', 'clock', 'Waiting'],
    sending: ['accent', 'send', 'Sending'],
    sent: ['good', 'check', 'Sent'],
    failed: ['critical', 'alert', 'Failed'],
    held: ['warning', 'user', 'Needs a person'],
    cancelled: ['', 'x', 'Cancelled'],
  };
  const STATUSES = Object.keys(MSG);
  const FILTERS = ['queued', 'held', 'sent', 'failed', 'cancelled', 'all'];   // the chips, in the order a person works through them
  const EMPTY = {
    all: 'No coupon messages yet. One is written when a customer says yes to the coupon on an abandoned-cart call.',
    queued: 'Nothing waiting to be sent.', held: 'Nothing needs a person.', sent: 'Nothing sent yet.', failed: 'Nothing failed.', cancelled: 'Nothing cancelled.',
  };
  const WHY = { held: 'Waits for a person: ', failed: 'Not sent: ', queued: 'Last try failed: ', cancelled: 'Cancelled: ' };
  const PROVIDER = { whatsapp: 'WhatsApp', plivo_sms: 'SMS', webhook: 'the webhook', manual: 'hand' };
  const LIMIT = 200;

  let msgs = null;   // GET /api/messages for the chosen chip
  let filter = 'all';
  let msgSeq = 0;
  let msgsShown = '';
  const openRows = new Set();   // messages shown in full; they stay open across refreshes

  const nameOf = (m) => m.customer_name || 'Customer';
  const phoneText = (p) => (/^\+91\d{10}$/.test(p || '') ? `+91 ${p.slice(3, 8)} ${p.slice(8)}` : (p || 'no number'));
  const paramsOf = (m) => {
    if (m.params && typeof m.params === 'object') return m.params;
    try { return JSON.parse(m.params || '{}') || {}; } catch (e) { return {}; }
  };
  const toSend = (m) => ['queued', 'held', 'failed'].includes(m.status);   // still to be sent, and nothing is sending it now
  const msgOf = (id) => ((msgs && msgs.messages) || []).find((m) => String(m.id) === String(id));

  // A message still to be sent whose coupon is off, expired or deleted: the customer could not use the code.
  function deadCoupon(m) {
    if (!toSend(m) || !data || !data.coupons) return '';
    const c = data.coupons.find((x) => String(x.code).toUpperCase() === String(m.coupon_code || '').toUpperCase());
    if (!c) return `The coupon ${m.coupon_code || ''} is no longer on this page: the customer cannot use it.`;
    return c.state === 'live' ? '' : `The coupon ${c.code} ${NOT_LIVE[c.state] || 'is not live'}: the customer cannot use it now.`;
  }

  function detailHtml(m, svc) {
    const todo = toSend(m);
    const wa = /^https:\/\//.test(m.wa_link || '') ? m.wa_link : '';
    const dead = deadCoupon(m);
    const why = (m.error && m.status !== 'sent' ? `<div class="camp-warn">${esc((WHY[m.status] || 'Last error: ') + m.error)}</div>` : '')
      + (dead ? `<div class="camp-warn">${esc(dead)}</div>` : '');
    const facts = [`written ${when(m.created_at)}`, m.attempts ? plural(m.attempts, 'try', 'tries') : '',
      m.sent_at ? `sent ${when(m.sent_at)}${m.provider ? ` by ${PROVIDER[m.provider] || m.provider}` : ''}` : ''].filter(Boolean).join(' · ');
    const acts = [
      todo && svc.ready ? `<button class="call" data-act="send" data-id="${esc(m.id)}">${icon('send')}Send now</button>` : '',
      todo && wa ? `<a class="${svc.ready ? '' : 'primary'}" href="${esc(wa)}" target="_blank" rel="noopener" data-wa="${esc(m.id)}">${icon('chat')}Open in WhatsApp</a>` : '',
      todo ? `<button data-act="mark-sent" data-id="${esc(m.id)}">${icon('check')}Mark as sent</button>` : '',
      `<button data-copy="${esc(m.id)}">${icon('copy')}Copy text</button>`,
      m.status === 'failed' || m.status === 'held' ? `<button data-act="retry" data-id="${esc(m.id)}">${icon('retry')}Retry</button>` : '',
      todo ? `<button class="danger" data-act="cancel" data-id="${esc(m.id)}">${icon('x')}Cancel</button>` : '',
      m.call_id ? `<button data-call="${esc(m.call_id)}">${icon('phone')}Call transcript</button>` : '',
    ].filter(Boolean).join('');
    return `<div class="cm-detail">
        <div class="chat-body cm-thread"><div class="msg customer cp-bubble">${waHtml(m.body)}</div></div>
        <div class="cm-side">${why}<div class="hint">${esc(facts)}</div><div class="cm-acts">${acts}</div></div>
      </div>`;
  }

  function msgRow(m, svc) {
    const s = MSG[m.status] || MSG.queued;
    const id = esc(m.id);
    const open = openRows.has(String(m.id));
    const p = paramsOf(m);
    const product = p.product && p.product !== 'aapka product' ? p.product : '';
    const line = m.status === 'sent' ? [when(m.sent_at), m.provider ? `by ${PROVIDER[m.provider] || m.provider}` : ''].filter(Boolean).join(' · ')
      : m.status === 'queued' && m.next_try_at ? `next try ${when(m.next_try_at)}`
        : m.status === 'held' || m.status === 'failed' ? (m.error || '') : '';
    const st = m.status === 'sending' ? `<span class="badge accent pulse"><span class="dot"></span>${esc(s[2])}</span>` : badge(s[0], s[1], s[2]);
    const test = m.test ? ' <span class="so-tag accent" title="From a browser test call or test mode: it does not count as the customer having the coupon">test</span>' : '';
    return `<tr class="cm-row${open ? ' open' : ''}" data-msg="${id}">
        <td class="num">${esc(when(m.created_at))}</td>
        <td><b>${esc(nameOf(m))}</b>${product ? `<br><small>${esc(product)}</small>` : ''}</td>
        <td class="num">${esc(phoneText(m.to_phone))}</td>
        <td><span class="cp-code">${esc(m.coupon_code || '')}</span></td>
        <td>${st}${test}${line ? `<small class="cm-line" title="${esc(line)}">${esc(line)}</small>` : ''}</td>
        <td><button class="viewbtn" data-more="${id}" aria-expanded="${open}" aria-controls="cm-more-${id}" aria-label="${open ? 'Hide' : 'View'} the message to ${esc(nameOf(m))}">${icon('chat')}${open ? 'Hide' : 'View'}</button></td>
      </tr>
      <tr class="cm-more" id="cm-more-${id}"${open ? '' : ' hidden'}><td colspan="6">${detailHtml(m, svc)}</td></tr>`;
  }

  // After a redraw the focus goes back to the row's View button when the button it was on is gone.
  const focusBack = (a) => {
    const tr = a.closest('tr');
    const id = tr && (tr.dataset.msg || tr.id.replace('cm-more-', ''));
    return id ? `[data-more="${CSS.escape(id)}"]` : '';
  };

  function renderMessages() {
    const counts = msgs.counts || {};
    const svc = msgs.service || {};
    const all = STATUSES.reduce((n, k) => n + (counts[k] || 0), 0);
    const bar = el('cm-service');
    bar.hidden = false;
    bar.className = `so-autobar ${svc.ready ? 'on' : 'off'}`;
    const missing = (svc.missing || []).length ? `Missing in .env: ${svc.missing.join(', ')}.` : '';
    bar.innerHTML = svc.ready
      ? `<span class="dot"></span><span><b>Sending by ${esc(svc.label)}</b>${svc.note ? ` · ${esc(svc.note)}` : ''}</span>`
      : `<span class="dot"></span><span><b>${esc(svc.label || 'Not set up')}:</b> ${esc([svc.note, missing].filter(Boolean).join(' '))}</span>`;
    redraw(el('cm-chips'), FILTERS.map((k) => {
      const [tone, ico, label] = k === 'all' ? ['', 'minus', 'All'] : MSG[k];
      return `<button class="${tone}" data-filter="${k}" aria-pressed="${filter === k}">${icon(ico)}${esc(label)} <b>${k === 'all' ? all : counts[k] || 0}</b></button>`;
    }).join(''));
    const rows = msgs.messages || [];
    el('cm-empty').hidden = rows.length > 0;
    el('cm-empty').textContent = EMPTY[filter] || EMPTY.all;
    el('cm-showing').textContent = rows.length >= LIMIT ? `Showing the newest ${LIMIT}.` : '';
    redraw(el('cm-rows'), rows.map((m) => msgRow(m, svc)).join(''), focusBack);
  }

  const waitingOf = (c) => ((c || {}).queued || 0) + ((c || {}).held || 0);
  const setNav = (n) => { el('nav-coupons').textContent = n ? String(n) : ''; };

  async function loadMessages(force = false) {
    const seq = ++msgSeq;
    try {
      const j = await fetch(`/api/messages?${new URLSearchParams({ status: filter, limit: LIMIT })}`).then(json);
      if (seq !== msgSeq) return;   // the chip changed meanwhile; the newer answer wins
      stale('messages', false);
      msgs = j;
      setNav(waitingOf(j.counts));
      const sig = filter + JSON.stringify(j);
      if (force || sig !== msgsShown) { msgsShown = sig; renderMessages(); }
    } catch (e) { if (e.status === 404) stale('messages', true); }
  }

  // The sidebar number: messages waiting to be sent, or for a person. Kept fresh on every page.
  async function loadNav() {
    try { const s = await fetch('/api/messages/status').then(json); setNav(s.waiting ?? waitingOf(s.counts)); }
    catch (e) { /* server away, or older than this page */ }
  }

  function toggleRow(id) {
    id = String(id);
    const open = !openRows.has(id);
    if (open) openRows.add(id); else openRows.delete(id);
    const row = el('cm-rows').querySelector(`tr.cm-row[data-msg="${CSS.escape(id)}"]`);
    const more = el(`cm-more-${id}`);
    const b = row && row.querySelector('[data-more]');
    if (row) row.classList.toggle('open', open);
    if (more) more.hidden = !open;
    if (b) {
      const m = msgOf(id);
      b.setAttribute('aria-expanded', String(open));
      b.setAttribute('aria-label', `${open ? 'Hide' : 'View'} the message to ${m ? nameOf(m) : 'Customer'}`);
      b.innerHTML = `${icon('chat')}${open ? 'Hide' : 'View'}`;
    }
  }

  async function copyText(id) {
    const m = msgOf(id);
    if (!m) return;
    const back = document.activeElement;
    let ok = false;
    try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(m.body || ''); ok = true; } } catch (e) { ok = false; }
    if (!ok) {
      // Over plain http on another address the browser offers no clipboard API; the old way works there.
      const t = document.createElement('textarea');
      t.value = m.body || '';
      t.setAttribute('readonly', '');
      t.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(t);
      t.select();
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      t.remove();
      if (back && back.focus) back.focus({ preventScroll: true });
    }
    window.toast(ok ? 'Copied. Paste it into WhatsApp or an SMS.' : 'Could not copy. Select the text and copy it by hand.');
  }

  // Opening WhatsApp is the person's own send; a test call's message may carry a real customer's number.
  function openWhatsApp(e, id) {
    const m = msgOf(id);
    if (m && m.test && !confirm(`This message came from a test call. Open WhatsApp to ${phoneText(m.to_phone)} anyway?`)) { e.preventDefault(); return; }
    window.toast('Once it is sent in WhatsApp, press Mark as sent.');
  }

  const sendResult = (r) => r.status === 'sent' ? 'Sent.'
    : r.status === 'queued' ? `Not sent yet: ${sentence(r.error || 'the service did not answer')} It is tried again by itself.`
      : `Not sent: ${sentence(r.error || r.status || 'unknown error')}`;

  const acting = new Set();   // messages with a press on its way: a second press does nothing
  async function act(what, id) {
    const m = msgOf(id);
    if (!m || acting.has(String(m.id))) return;
    const svc = (msgs && msgs.service) || {};
    const who = `${nameOf(m)} (${phoneText(m.to_phone)})`;
    const test = m.test ? '\n\nIt came from a test call: this number may be a real customer who never asked for it.' : '';
    const dead = deadCoupon(m);
    if ((what === 'send' || what === 'mark-sent') && dead && !confirm(`${dead}\n\n${what === 'send' ? 'Send' : 'Mark as sent'} anyway?`)) return;
    const stopped = m.error ? `\n\nIt stopped because: ${m.error}` : '';
    if (what === 'send' && !confirm(`Send the ${m.coupon_code} message to ${who} now, by ${svc.label}?${test}`)) return;
    if (what === 'retry' && svc.ready && !confirm(`Send the message to ${who} again? ${svc.label} sends it within a minute.${stopped}${test}`)) return;
    // Not ready yet, a retried message just waits; one from a test call may still carry a customer's number.
    if (what === 'retry' && !svc.ready && m.test && !confirm(`Put the message to ${who} back under Waiting?\n\nIt came from a test call: once a message service is set up, it is sent to that number.${stopped}`)) return;
    if (what === 'cancel' && !confirm(`Cancel the message to ${who}? It will not be sent.`)) return;
    acting.add(String(m.id));
    for (const b of el('cm-rows').querySelectorAll(`[data-act][data-id="${CSS.escape(String(m.id))}"]`)) b.disabled = true;
    try {
      const r = await post(`/api/messages/${m.id}/${what}`);
      window.toast(what === 'send' ? sendResult(r && r.message && typeof r.message === 'object' ? r.message : r)
        : what === 'mark-sent' ? 'Marked as sent.'
          : what === 'retry' ? (svc.ready ? 'Back in the queue: it is sent within a minute.' : 'Back under Waiting.')
            : 'Cancelled. It will not be sent.');
    } catch (err) { window.toast(err.message); }
    finally { acting.delete(String(m.id)); }
    loadMessages(true);
    loadCouponList(true);
  }

  el('cm-chips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    filter = b.dataset.filter;
    for (const x of el('cm-chips').querySelectorAll('[data-filter]')) x.setAttribute('aria-pressed', String(x.dataset.filter === filter));
    loadMessages(true);
  });

  el('cm-rows').addEventListener('click', (e) => {
    const t = e.target.closest('button, a');
    if (!t) {
      // A click elsewhere on a row opens it; selecting text in it does not.
      const tr = e.target.closest('tr.cm-row');
      if (tr && !String(window.getSelection ? window.getSelection() : '').trim()) toggleRow(tr.dataset.msg);
      return;
    }
    const d = t.dataset;
    if (d.more) toggleRow(d.more);
    else if (d.wa) openWhatsApp(e, d.wa);
    else if (d.copy) copyText(d.copy);
    else if (d.call) { if (window.showTranscript) window.showTranscript(d.call); }
    else if (d.act) act(d.act, d.id);
  });

  // ---------------- start ----------------
  window.loadCoupons = () => { loadCouponList(); loadMessages(); };
  renderPlaces();
  loadNav();
  if (onPage()) window.loadCoupons();   // dashboard.js showed this page before this file was loaded
  setInterval(() => { if (onPage() && !document.hidden) window.loadCoupons(); }, 10000);
  setInterval(loadNav, 30000);
})();
