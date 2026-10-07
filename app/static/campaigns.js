// Campaigns: the list, the new / edit form, and one campaign's page (contacts, calling, results).
// Loaded after dashboard.js (badges, renderCampaign, renderContacts) and before app.js (upload,
// preview, loadContacts), which read window.currentCampaignId / currentCallType from here.
(() => {
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let types = [];          // from /api/call-types
  let campaigns = [];      // from /api/campaigns
  let current = null;      // the open campaign
  let editing = null;      // the campaign being edited in the form, or null for a new one
  let chosenType = '';

  const typeOf = (key) => types.find((t) => t.key === key);
  window.currentCampaignId = () => (current ? current.id : null);
  window.currentCampaign = () => current;
  window.currentCallType = () => (current ? typeOf(current.call_type) : null);

  const view = (name) => { for (const v of ['list', 'new', 'detail']) el(`cv-${v}`).hidden = v !== name; window.scrollTo({ top: 0 }); };
  const remember = (id) => { try { localStorage.setItem('campaign', id ? String(id) : ''); } catch (e) { /* ignore */ } };

  // ---------------- the list ----------------
  const fmtDay = (iso) => iso ? new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }) : '';
  const FINAL = ['confirmed', 'changed', 'cancelled', 'not_ordered', 'wrong_number', 'do_not_call', 'unclear', 'interested', 'not_interested', 'already_booked', 'needs_help', 'wants_to_order', 'already_ordered'];

  function renderList() {
    const running = campaigns.find((c) => c.running);
    el('nav-campaigns').textContent = running ? 'calling' : (campaigns.length ? String(campaigns.length) : '');
    el('home-start').hidden = campaigns.length > 0;
    const hist = el('hist-camp');
    const keep = hist.value;
    hist.innerHTML = '<option value="">All campaigns</option>' + campaigns.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    hist.value = keep;
    if (!campaigns.length) {
      el('camp-list').innerHTML = `<div class="empty camp-empty">No campaigns yet. Press <b>New campaign</b> to make the first one.</div>`;
      return;
    }
    el('camp-list').innerHTML = campaigns.map((c) => {
      const done = FINAL.reduce((n, k) => n + (c.by_status[k] || 0), 0);
      const pct = c.total ? Math.round((100 * done) / c.total) : 0;
      const state = c.running ? '<span class="badge accent pulse"><span class="dot"></span>Calling</span>'
        : !c.total ? '<span class="badge warning">No contacts yet</span>'
        : done >= c.total ? '<span class="badge good">Done</span>' : '<span class="badge">Ready</span>';
      const t = typeOf(c.call_type);
      const facts = c.facts || {};
      const sub = [t ? t.name : c.call_type, facts.start_date ? `on ${facts.start_date}` : '', `created ${fmtDay(c.created_at)}`].filter(Boolean).join(' · ');
      return `<button class="camp-card" data-open="${c.id}">
        <div class="top"><b>${esc(c.name)}</b>${state}</div>
        <div class="meta">${esc(sub)}</div>
        <div class="progress" aria-hidden="true"><i class="done" style="width:${pct}%"></i></div>
        <div class="meta">${c.total ? `${done} of ${c.total} contacts done${c.pending ? ` · ${c.pending} not called yet` : ''}` : 'Upload a contact sheet to start'}${c.last_call_at ? ` · last call ${fmtDay(c.last_call_at)}` : ''}</div>
      </button>`;
    }).join('');
  }

  async function loadCampaigns() {
    try { campaigns = await fetch('/api/campaigns').then((r) => (r.ok ? r.json() : [])); } catch (e) { return; }
    renderList();
    if (current) {
      const fresh = campaigns.find((c) => c.id === current.id);
      if (fresh) current = fresh;
      else { current = null; remember(null); if (!el('cv-detail').hidden) showCampaignList(); }
    }
  }
  window.loadCampaigns = loadCampaigns;

  function showCampaignList() {
    current = null;
    remember(null);
    view('list');
    loadCampaigns();
  }
  window.showCampaignList = showCampaignList;

  // ---------------- the new / edit form ----------------
  function renderTypes() {
    el('cn-types').innerHTML = types.map((t) => `<button class="type-opt" role="radio" aria-checked="${t.key === chosenType}" data-type="${t.key}" ${editing ? 'disabled' : ''}>
        <b>${esc(t.name)}</b><span>${esc(t.description)}</span>
        <small>Sheet: ${esc(t.columns.map((c) => c.label + (c.required ? ' *' : '')).join(', '))}</small>
      </button>`).join('');
    el('cn-type-locked').hidden = !editing;
  }

  function renderFacts() {
    const t = typeOf(chosenType);
    const facts = (editing && editing.facts) || {};
    if (!t || !t.facts.length) {
      el('cn-facts').innerHTML = t ? '<p class="hint" style="margin:0">Nothing else to fill in: everything the agent needs comes from each row of the contact sheet.</p>' : '';
      return;
    }
    el('cn-facts').innerHTML = `<p class="hint" style="margin:4px 0 12px">What the agent may say about this campaign. It will say nothing that is not written here.</p>
      <div class="form-grid">${t.facts.map((f) => {
        const v = esc(facts[f.key] || '');
        const label = `<span>${esc(f.label.replace(' (optional)', ''))}${f.required ? '' : '<span class="opt"> · optional</span>'}</span>`;
        const input = f.long
          ? `<textarea data-fact="${f.key}" placeholder="${esc(f.hint)}">${v}</textarea>`
          : `<input class="fld" data-fact="${f.key}" ${f.date ? 'type="date"' : ''} value="${v}" placeholder="${esc(f.hint)}">`;
        return `<label class="f ${f.long ? 'wide' : ''}">${label}${input}</label>`;
      }).join('')}</div>`;
  }

  function showNewCampaign(camp = null) {
    editing = camp;
    chosenType = camp ? camp.call_type : '';
    el('cn-title').textContent = camp ? `Edit ${camp.name}` : 'New campaign';
    el('cn-save').textContent = camp ? 'Save changes' : 'Create campaign';
    el('cn-next').hidden = !!camp;
    el('cn-name').value = camp ? camp.name : '';
    el('cn-last').value = camp ? (camp.last_call_date || '') : '';
    renderTypes();
    renderFacts();
    view('new');
  }

  el('cn-types').addEventListener('click', (e) => {
    const b = e.target.closest('[data-type]');
    if (!b || editing) return;
    chosenType = b.dataset.type;
    renderTypes();
    renderFacts();
    if (!el('cn-name').value) el('cn-name').focus();
  });

  el('cn-save').onclick = async () => {
    if (!chosenType) { window.toast('First pick what the calls are for.'); return; }
    const facts = {};
    for (const f of el('cn-facts').querySelectorAll('[data-fact]')) facts[f.dataset.fact] = f.value.trim();
    const body = { name: el('cn-name').value.trim(), call_type: chosenType, facts, last_call_date: el('cn-last').value };
    const r = await fetch(editing ? `/api/campaigns/${editing.id}` : '/api/campaigns', {
      method: editing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { window.toast(j.detail || 'Could not save.'); return; }
    window.toast(editing ? 'Saved.' : 'Campaign created. Now upload its contact sheet.');
    await loadCampaigns();
    openCampaign(j.id);
  };

  // ---------------- one campaign ----------------
  function renderDetail() {
    const c = current;
    const t = typeOf(c.call_type) || { name: c.call_type, columns: [], facts: [], kind: 'service' };
    el('cd-name').textContent = c.name;
    el('cd-type').innerHTML = `<span class="badge ${t.kind === 'promotional' ? 'warning' : 'accent'}">${esc(t.name)}</span>`
      + (c.last_call_date ? ` <span class="badge">calls until ${esc(c.last_call_date)}</span>` : '');
    const facts = t.facts.filter((f) => (c.facts || {})[f.key]);
    el('cd-facts').hidden = !facts.length;
    el('cd-facts').innerHTML = facts.map((f) => `<div><dt>${esc(f.label.replace(/ \(optional\)/, ''))}</dt><dd>${esc(c.facts[f.key])}</dd></div>`).join('');
    el('cd-template').href = `/api/call-types/${c.call_type}/template`;
    el('cd-export').href = `/api/campaigns/${c.id}/export`;
    el('cd-cols').textContent = `Columns: ${t.columns.map((col) => col.label + (col.required ? ' *' : '')).join(', ')}. Any other column is kept too.`;
    el('m-fields').innerHTML = t.columns.map((col) => `<input class="fld" data-m="${col.key}" placeholder="${esc(col.label)}${col.required ? ' *' : ''}"${col.key === 'phone' ? ' inputmode="tel"' : ''}>`).join('')
      + `<select id="m_lang" style="flex:0 0 150px" title="Language"><option value="">Language: campaign's</option><option>hinglish</option><option>hindi</option><option>english</option></select>`;
  }

  async function openCampaign(id) {
    let c = campaigns.find((x) => x.id === Number(id));
    if (!c) {
      const r = await fetch(`/api/campaigns/${id}`);
      if (!r.ok) { showCampaignList(); return; }
      c = await r.json();
    }
    current = c;
    remember(c.id);
    if (window.showPage && !el('page-campaigns').classList.contains('on')) window.showPage('campaigns');
    renderDetail();
    view('detail');
    if (window.closePreview) window.closePreview();
    if (window.resetBoardFilter) window.resetBoardFilter();
    if (window.resetCampaignForm) window.resetCampaignForm();
    if (window.renderContacts) window.renderContacts([]);
    if (window.loadContacts) window.loadContacts();
    if (window.loadCampaign) window.loadCampaign();
  }
  window.openCampaign = openCampaign;

  el('cd-edit').onclick = () => { if (current) showNewCampaign(current); };
  el('cd-delete').onclick = async () => {
    if (!current) return;
    const n = current.total || 0;
    if (!confirm(`Delete "${current.name}"${n ? ` and its ${n} contact${n === 1 ? '' : 's'}` : ''}?\n\nCall history and spending keep their records.`)) return;
    const r = await fetch(`/api/campaigns/${current.id}`, { method: 'DELETE' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { window.toast(j.detail || 'Could not delete.'); return; }
    window.toast('Campaign deleted.');
    showCampaignList();
  };

  el('camp-list').addEventListener('click', (e) => { const b = e.target.closest('[data-open]'); if (b) openCampaign(b.dataset.open); });
  // "data-go" buttons: New campaign (from Home or the list) and the back arrows.
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-go]');
    if (!b) return;
    if (b.dataset.go === 'new-campaign') { window.showPage('campaigns'); showNewCampaign(null); }
    if (b.dataset.go === 'campaign-list') {
      if (editing && !el('cv-new').hidden) { const back = editing; editing = null; openCampaign(back.id); return; }
      showCampaignList();
    }
  });

  // ---------------- start ----------------
  (async () => {
    try { types = await fetch('/api/call-types').then((r) => (r.ok ? r.json() : [])); } catch (e) { types = []; }
    await loadCampaigns();
    // Back where the user was: the campaign that was open, if the Campaigns page was.
    let last = '';
    try { last = localStorage.getItem('campaign') || ''; } catch (e) { /* ignore */ }
    if (el('page-campaigns').classList.contains('on') && last && campaigns.some((c) => String(c.id) === last)) openCampaign(last);
    else view('list');
  })();
  setInterval(() => { if (el('page-campaigns').classList.contains('on') || el('page-overview').classList.contains('on')) loadCampaigns(); }, 5000);
  el('hist-camp').addEventListener('change', () => { if (window.loadCalls) window.loadCalls(); });
})();
