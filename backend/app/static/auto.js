// Automatic calling (its card is in the Store orders settings) and the "Automatic" box on one campaign.
// Loaded last: app.js fills the agent / voice / language lists, campaigns.js knows the open campaign.
(() => {
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const json = async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || `error ${r.status}`); return j; };
  const post = (url, body, method = 'POST') => fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }).then(json);
  const onCampaigns = () => el('page-campaigns').classList.contains('on') || el('page-store').classList.contains('on');

  // ---------------- automatic calling ----------------
  const FIELDS = { 'auto-model': 'model', 'auto-voice': 'voice', 'auto-lang': 'language', 'auto-start': 'start_hour', 'auto-end': 'end_hour',
    'auto-budget': 'budget_rs', 'auto-attempts': 'max_attempts', 'auto-retry': 'retry_minutes', 'auto-concurrent': 'max_concurrent' };
  let auto = null;
  let filled = false;

  function fillForm(s) {
    // The lists are filled by app.js from /api/config; wait for them, then show the saved settings once.
    if (filled || !el('auto-voice').options.length || !el('auto-model').options.length) return;
    filled = true;
    for (const [id, key] of Object.entries(FIELDS)) {
      const v = s[key];
      if (v === undefined || v === null || v === '') continue;
      if (key === 'budget_rs' && !v && !auto.enabled) continue;   // keep the suggested cap until it was saved once
      if (el(id).tagName === 'SELECT' && ![...el(id).options].some((o) => o.value === String(v))) continue;
      el(id).value = v;
    }
    if (window.syncVoices) window.syncVoices();
  }

  function renderAuto(a) {
    auto = a;
    fillForm(a.settings || {});
    const s = a.settings || {};
    let state;
    if (a.running && a.in_progress) state = `On · ${a.in_progress} call${a.in_progress > 1 ? 's' : ''} in progress`;
    else if (a.running) state = `On · ${a.reason || 'starting'}`;
    else if (a.enabled) state = 'On, but not running';
    else state = 'Off';
    el('auto-state').textContent = state;
    el('auto-reason').textContent = a.running && a.calls_made ? `${a.calls_made} call${a.calls_made > 1 ? 's' : ''} since switched on` : (!a.running ? a.reason || '' : '');
    const warn = [];
    if (a.dial_only_my_number) warn.push(`Test mode: DIAL_ONLY_MY_NUMBER is on, so every automatic call rings your own phone (${a.my_number}).`);
    if (!a.running && a.phone_missing && a.phone_missing.length) warn.push(`Phone line not ready (missing ${a.phone_missing.join(', ')}). Start the server with run_phone.bat.`);
    el('auto-warn').hidden = !warn.length;
    el('auto-warn').textContent = warn.join(' ');
    el('auto-camps').innerHTML = a.campaigns.length
      ? a.campaigns.map((c) => `<button data-open-camp="${c.id}" title="${esc(c.call_type_name)}">${esc(c.name)} <b>${c.to_call}</b></button>`).join('')
      : '<span class="hint">Nothing to call yet. Store orders appear here as they come in; a manual campaign joins when you tick "Automatic" under its Calling step.</span>';
    el('auto-on').hidden = a.running;
    el('auto-off').hidden = !a.running;
    for (const id of Object.keys(FIELDS)) el(id).disabled = a.running;
    el('auto-hint').textContent = a.running ? `Calling hours ${s.start_hour}:00 to ${s.end_hour}:00 IST (now ${a.now_ist})${s.budget_rs ? ` · Rs ${s.budget_rs} a day` : ''}` : '';
  }

  async function loadAuto() {
    try { renderAuto(await fetch('/api/auto').then(json)); } catch (e) { /* server away or not restarted */ }
  }

  el('auto-camps').addEventListener('click', (e) => {
    const b = e.target.closest('[data-open-camp]');
    const card = b && document.querySelector(`#camp-list [data-open="${b.dataset.openCamp}"]`);
    if (card) card.click();
  });

  el('auto-on').onclick = async () => {
    const body = { build: window.BUILD || '' };
    for (const [id, key] of Object.entries(FIELDS)) body[key] = el(id).value;
    const n = auto ? auto.campaigns.reduce((t, c) => t + c.to_call, 0) : 0;
    const where = auto && auto.dial_only_my_number ? `to YOUR phone ${auto.my_number} (test mode)` : 'to the customers\' numbers';
    const agent = el('auto-model').selectedOptions[0] ? el('auto-model').selectedOptions[0].textContent : '';
    if (!confirm(`Switch on automatic calling, ${where}?\n\nAgent: ${agent}\n${n} contact${n === 1 ? '' : 's'} waiting now, and every new one in an Automatic campaign will be called as it arrives, between ${body.start_hour}:00 and ${body.end_hour}:00, without anyone pressing Start.\n\nEvery call is a real, paid phone call. It stays on after a server restart until you switch it off.`)) return;
    try { renderAuto(await post('/api/auto/start', body)); window.toast('Automatic calling is on.'); } catch (e) { window.toast(e.message); }
  };
  el('auto-off').onclick = async () => {
    try { renderAuto(await post('/api/auto/stop')); window.toast('Automatic calling is off. A call in progress will finish.'); } catch (e) { window.toast(e.message); }
  };

  // ---------------- one campaign: the Automatic box ----------------
  let shownFor = null;
  function syncCampaignBox() {
    const c = window.currentCampaign && window.currentCampaign();
    if (!c) { shownFor = null; return; }
    if (shownFor === c.id) return;
    shownFor = c.id;
    el('camp-auto').checked = !!c.auto;
  }
  el('camp-auto').onchange = async () => {
    const c = window.currentCampaign && window.currentCampaign();
    if (!c) return;
    const on = el('camp-auto').checked;
    try {
      await post(`/api/campaigns/${c.id}/auto`, { auto: on });
      c.auto = on ? 1 : 0;
      window.toast(on ? 'Marked Automatic: called when Automatic calling is on.' : 'No longer Automatic.');
      if (window.loadCampaigns) window.loadCampaigns();
      loadAuto();
    } catch (e) { el('camp-auto').checked = !on; window.toast(e.message); }
  };

  loadAuto();
  setInterval(() => { if (onCampaigns()) { loadAuto(); syncCampaignBox(); } }, 3000);
})();
