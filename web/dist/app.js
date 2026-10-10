'use strict';

/* Signalpost console.
 *
 * Three rendering modes, tried in order:
 *   1. live  — the local API served by src/serve.py, so the console shows the
 *              output of an actual run.
 *   2. file  — the bundled snapshot under web/dist/data, so the page works when
 *              opened straight from disk with no server.
 *   3. demo  — a small inline fixture, so the page is never blank.
 *
 * The UI is organised around one claim: every value shown has a receipt. The
 * cascade diagram at the top is the cost argument, and the fact drawer is the
 * evidence argument. Nothing is displayed without its proof type.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const E = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const state = {
  profiles: [],        // index rows
  details: new Map(),  // orgnr -> full profile
  runs: [],
  score: null,
  selected: null,
  filter: 'all',
  query: '',
  mode: 'demo',
  tab: 'evidence',
};

/* ── formatting ──────────────────────────────────────────────────────────── */

const display = (n) => {
  const d = String(n || '').replace(/\D/g, '');
  return d.length === 9 ? `${d.slice(0, 3)} ${d.slice(3, 6)} ${d.slice(6)}` : String(n || '');
};

const NUM_FIELDS = new Set([
  'employees', 'revenue_latest', 'operating_result', 'net_result',
  'total_assets', 'equity', 'debt', 'accounting_year', 'latest_accounts_year',
]);

function fmtValue(field, v) {
  if (v == null || v === '') return '—';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (NUM_FIELDS.has(field) && typeof v === 'number') {
    return new Intl.NumberFormat('nb-NO').format(v);
  }
  if (field === 'legal_name' || field === 'city' || field === 'municipality' || field === 'industry') {
    return String(v);
  }
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

function isUrl(v) {
  return typeof v === 'string' && /^https?:\/\//.test(v);
}

function shortUrl(u) {
  try {
    const p = new URL(u);
    return p.host.replace(/^www\./, '') + (p.pathname === '/' ? '' : p.pathname);
  } catch { return u; }
}

const PROOF_LABEL = {
  registry: 'register',
  orgnr_on_page: 'org number on page',
  name_postcode: 'name + address',
  identifier_join: 'identifier join',
  name_only: 'name only',
};

const LAYER_LABEL = {
  registry: 'registry layer',
  identifier: 'identifier layer',
  web_rules: 'web · rules',
  web_llm: 'web · model',
  derived: 'derived',
};

const FIELD_GROUP = {
  legal_name: 'Identity', legal_form: 'Identity', founded: 'Identity', registered: 'Identity',
  employees: 'Identity', industry: 'Identity', nace_code: 'Identity', sector: 'Identity',
  municipality: 'Location', postcode: 'Location', city: 'Location', address: 'Location',
  website: 'Web presence', official_website: 'Web presence', description: 'Web presence',
  email: 'Contact', phone: 'Contact',
  facebook: 'Social profiles', linkedin: 'Social profiles', instagram: 'Social profiles',
  twitter: 'Social profiles', youtube: 'Social profiles', tiktok: 'Social profiles',
  github: 'Social profiles',
  lei: 'Identifiers', parent_lei: 'Identifiers', wikidata: 'Identifiers',
  parent_org_number: 'Entity graph', latest_accounts_year: 'Accounts', accounting_year: 'Accounts',
  revenue_latest: 'Accounts', operating_result: 'Accounts', net_result: 'Accounts',
  total_assets: 'Accounts', equity: 'Accounts', debt: 'Accounts',
  purpose: 'Charter', language_form: 'Register flags', winding_up: 'Register flags', bankrupt: 'Register flags',
  website_orgnr_proof: 'Proof of ownership',
};

const groupOf = (f) => FIELD_GROUP[f] || 'Other';

/* ── data loading ────────────────────────────────────────────────────────── */

async function boot() {
  const sources = [
    { mode: 'live', load: loadLive },
    { mode: 'file', load: loadFile },
    { mode: 'demo', load: loadDemo },
  ];
  for (const s of sources) {
    try {
      const ok = await s.load();
      if (ok) {
        state.mode = s.mode;
        render();
        return;
      }
    } catch (err) {
      console.info(`[signalpost] ${s.mode} source unavailable:`, err.message);
    }
  }
  render();
}

async function loadLive() {
  const health = await getJSON('/api/health', 1200);
  if (!health || !health.ok) return false;
  const [index, runs, score] = await Promise.all([
    getJSON('/api/profiles?limit=400'),
    getJSON('/api/runs'),
    getJSON('/api/score').catch(() => null),
  ]);
  if (!Array.isArray(index) || !index.length) return false;
  state.profiles = index;
  state.runs = Array.isArray(runs) ? runs : [];
  state.score = score && !score.error ? score : null;
  $('#livestate').textContent = 'live';
  return true;
}

async function loadFile() {
  const index = await fetchLocal('data/index.json');
  if (!Array.isArray(index) || !index.length) return false;
  state.profiles = index;
  state.runs = (await fetchLocal('data/runs.json')) || [];
  state.score = await fetchLocal('data/score.json');
  $('#livestate').textContent = 'snapshot';
  return true;
}

function loadDemo() {
  if (typeof SIGNALPOST_DEMO === 'undefined') return false;
  state.profiles = SIGNALPOST_DEMO.profiles;
  state.runs = SIGNALPOST_DEMO.runs || [];
  state.score = SIGNALPOST_DEMO.score || null;
  SIGNALPOST_DEMO.details.forEach((p) => state.details.set(p.org_number, p));
  $('#livestate').textContent = 'demo';
  return true;
}

async function getJSON(url, timeout = 4000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const res = await fetch(url, { signal: ctrl.signal, headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function fetchLocal(rel) {
  try {
    const res = await fetch(rel, { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch { return null; }
}

async function detail(orgnr) {
  if (state.details.has(orgnr)) return state.details.get(orgnr);
  let full = null;
  if (state.mode === 'live') {
    full = await getJSON(`/api/profile/${orgnr}`).catch(() => null);
  } else if (state.mode === 'file') {
    full = await fetchLocal(`data/profiles/${orgnr}.json`);
  }
  if (full) state.details.set(orgnr, full);
  return full;
}

/* ── list ────────────────────────────────────────────────────────────────── */

function visible() {
  const q = state.query.trim().toLowerCase();
  return state.profiles.filter((p) => {
    if (state.filter === 'proven' && !p.proves_ownership) return false;
    if (state.filter === 'weak' && !(p.weak_matches > 0)) return false;
    if (state.filter === 'changed' && !(p.change_count > 0)) return false;
    if (!q) return true;
    return String(p.legal_name || '').toLowerCase().includes(q)
      || String(p.org_number || '').includes(q.replace(/\s/g, ''));
  });
}

function renderList() {
  const rows = visible();
  const list = $('#list');
  const totalFacts = rows.reduce((n, p) => n + (p.fact_count || 0), 0);
  $('#listmeta').textContent = `${rows.length} of ${state.profiles.length} companies`;

  if (!rows.length) {
    list.innerHTML = `<div style="padding:22px 14px;text-align:center;color:var(--ink-faint);font-size:12px">
      No company matches that filter.</div>`;
    return;
  }

  const maxFacts = Math.max(1, ...rows.map((p) => p.fact_count || 0));

  list.innerHTML = rows.map((p, i) => {
    const dotClass = p.proves_ownership ? 'dot--strong' : (p.weak_matches > 0 ? 'dot--weak' : 'dot--none');
    const pct = Math.round(((p.fact_count || 0) / maxFacts) * 100);
    const selected = state.selected === p.org_number;
    return `
      <button class="row" role="option" aria-selected="${selected}" aria-current="${selected}"
              data-orgnr="${E(p.org_number)}" style="animation-delay:${Math.min(i * 14, 320)}ms">
        <div class="row__top">
          <span class="row__name">${E(p.legal_name || '(unnamed)')}</span>
          <span class="row__num">${E(display(p.org_number))}</span>
        </div>
        <div class="row__meta">
          <span class="dot ${dotClass}" aria-hidden="true"></span>
          <span class="tiny">${p.fact_count || 0} facts</span>
          ${p.change_count ? `<span class="tiny" style="color:var(--llm)">· ${p.change_count} changed</span>` : ''}
          ${p.empty_count ? `<span class="tiny">· ${p.empty_count} empty</span>` : ''}
        </div>
        <div class="bar" aria-hidden="true"><div class="bar__fill" style="width:${pct}%"></div></div>
      </button>`;
  }).join('');
}

/* ── main view ───────────────────────────────────────────────────────────── */

function render() {
  renderList();
  const rows = visible();
  if (!state.selected && rows.length) {
    select(rows[0].org_number);
    return;
  }
  if (!state.selected) {
    renderPlaceholder();
    return;
  }
  renderProfile(state.selected);
}

function renderPlaceholder() {
  const advanced = state.mode === 'live' || state.mode === 'file';
  $('#main').innerHTML = `
    <div class="placeholder"><div class="placeholder__inner">
      <div class="placeholder__mark" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5">
          <rect x="3" y="2.5" width="14" height="15" rx="2"/><path d="M6.5 7h7M6.5 10.5h7M6.5 14h4"/>
        </svg>
      </div>
      <h2>No profiles to show</h2>
      <p>${advanced
        ? 'The engine has not written any profiles yet. Run <code>make run ORGNR_FILE=orgnrs.txt</code> and reload.'
        : 'Open this page through the local server to browse real output: <code>make web</code>.'}</p>
    </div></div>`;
}

async function select(orgnr) {
  state.selected = orgnr;
  state.tab = state.tab || 'evidence';
  renderList();
  $('#main').innerHTML = `<div class="panel">
    <div class="skeleton" style="height:96px"></div>
    <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton" style="height:44px"></div>
  </div>`;
  const full = await detail(orgnr);
  if (state.selected !== orgnr) return;
  if (!full) { renderPlaceholder(); return; }
  renderProfile(orgnr);
}

function renderProfile(orgnr) {
  const meta = state.profiles.find((p) => p.org_number === orgnr) || { org_number: orgnr };
  const full = state.details.get(orgnr);
  const facts = (full && full.facts) || [];
  const empties = (full && full.empty_fields) || [];
  const changes = (full && full.changes) || [];

  const proven = facts.filter((f) => f.match_proof === 'orgnr_on_page').length;
  const fromWeb = facts.filter((f) => f.layer === 'web_rules' || f.layer === 'web_llm').length;
  const meanConf = facts.length
    ? facts.reduce((s, f) => s + (f.confidence || 0), 0) / facts.length : 0;
  const site = (facts.find((f) => f.field === 'website') || {}).value;

  $('#main').innerHTML = `
    ${hero(meta, full, facts, { proven, fromWeb, meanConf, site })}
    ${tabs(facts, empties, changes)}
    <section class="panel" id="tab-evidence" ${state.tab !== 'evidence' ? 'hidden' : ''}>
      ${evidencePanel(facts)}
    </section>
    <section class="panel" id="tab-abstain" ${state.tab !== 'abstain' ? 'hidden' : ''}>
      ${abstainPanel(empties)}
    </section>
    <section class="panel" id="tab-changes" ${state.tab !== 'changes' ? 'hidden' : ''}>
      ${changesPanel(changes)}
    </section>
    <section class="panel" id="tab-json" ${state.tab !== 'json' ? 'hidden' : ''}>
      ${jsonPanel(full)}
    </section>`;

  wire(orgnr);
}

function hero(meta, full, facts, agg) {
  const name = meta.legal_name || (full && full.legal_name) || '(unnamed)';
  const layers = ['registry', 'identifier', 'web_rules'].map((layer) => {
    const hits = facts.filter((f) => f.layer === layer).length;
    return { layer, hits };
  });

  const stages = [
    {
      n: '1', name: 'Number validation',
      desc: 'Nine digits and a modulus-11 check digit, weights 3 2 7 6 5 4 3 2. An invalid number is rejected before any request is made.',
      cost: 'free', ran: true,
    },
    {
      n: '2', name: 'Deterministic layer',
      desc: 'The local Enhetsregisteret mirror, plus GLEIF and Wikidata keyed on register identifiers. No name guessing, so no collision risk.',
      cost: '$0', ran: layers[0].hits + layers[1].hits > 0,
      detail: `${layers[0].hits} registry · ${layers[1].hits} identifier facts`,
    },
    {
      n: '3', name: 'Web layer · ownership gate',
      desc: 'A page is accepted only if a checksum-valid organisation number appears on it. Otherwise name plus postcode plus city, at lower confidence.',
      cost: 'metered', ran: agg.proven > 0 || facts.some((f) => f.match_proof === 'name_postcode'),
      detail: agg.proven > 0 ? `${agg.proven} facts with the number on the page` : 'gate did not pass',
    },
    {
      n: '4', name: 'Web layer · rule extraction',
      desc: 'JSON-LD, OpenGraph, mailto and tel links, social profiles and address blocks. Costs no tokens, and runs before any model call.',
      cost: 'free', ran: agg.fromWeb > 0,
      detail: agg.fromWeb ? `${agg.fromWeb} facts extracted` : 'not reached',
    },
    {
      n: '5', name: 'LLM gap filling and verification',
      desc: 'Only unresolved fields reach the model, and only with a strict schema. A quote that is not on the page is discarded, and a conflict that cannot be settled drops the field.',
      cost: 'per token',
      ran: facts.some((f) => f.layer === 'web_llm'),
      detail: facts.some((f) => f.layer === 'web_llm') ? 'model filled unresolved fields' : 'no gaps needed filling',
    },
  ];

  return `
  <header class="hero">
    <div class="hero__grid" aria-hidden="true"></div>
    <div class="hero__inner">
      <div class="eyebrow" style="margin-bottom:9px">
        ${E(display(meta.org_number || ''))} · ${E((full && full.run_date) || meta.run_date || '')}
        ${(full && full.run_id) ? `· run ${E(full.run_id)}` : ''}
      </div>
      <h1 class="hero__title">${E(name)}</h1>
      <p class="hero__lede">
        ${facts.length} facts carry a source, a retrieval date and a confidence score.
        ${agg.proven > 0
          ? `Ownership is proven: ${agg.proven} values rest on the company's own organisation number printed on its own page.`
          : 'No page carrying the company\'s organisation number was found, so web values rest on a weaker name and address match.'}
        ${(full && full.empty_fields && full.empty_fields.length)
          ? ` ${full.empty_fields.length} fields were left empty rather than guessed.`
          : ''}
      </p>

      <div class="rule-line">
        <b>registry</b> <span class="sep">→</span>
        <b>web</b> <span class="sep">→</span>
        <b>LLM</b>
        <span class="sep">·</span> each layer runs only when the cheaper one above it failed
      </div>

      <div class="stats">
        <div class="stat">
          <div class="stat__value">${facts.length}</div>
          <div class="stat__label">facts published</div>
        </div>
        <div class="stat ${agg.proven > 0 ? 'stat--good' : 'stat--warn'}">
          <div class="stat__value">${agg.proven}</div>
          <div class="stat__label">proven on page</div>
        </div>
        <div class="stat">
          <div class="stat__value">${(agg.meanConf * 100).toFixed(0)}%</div>
          <div class="stat__label">mean confidence</div>
        </div>
        <div class="stat ${empties(full) === 0 ? '' : 'stat--warn'}">
          <div class="stat__value">${empties(full)}</div>
          <div class="stat__label">left empty</div>
        </div>
        <div class="stat">
          <div class="stat__value">${(full && full.changes && full.changes.length) || 0}</div>
          <div class="stat__label">changes logged</div>
        </div>
      </div>

      <div class="cascade">
        ${stages.map(stageCard).join('')}
      </div>
    </div>
  </header>`;
}

const empties = (full) => ((full && full.empty_fields) || []).length;

function stageCard(s) {
  return `
    <div class="stage" data-state="${s.ran ? 'ran' : 'skipped'}">
      <div class="stage__rail" aria-hidden="true">
        <div class="stage__node">${s.n}</div>
        <div class="stage__wire"></div>
      </div>
      <div class="stage__body">
        <div class="stage__name">${E(s.name)}</div>
        <div class="stage__desc">${E(s.desc)}</div>
        ${s.detail ? `<div class="tiny" style="margin-top:4px">${E(s.detail)}</div>` : ''}
      </div>
      <div class="stage__cost">${E(s.cost)}</div>
    </div>`;
}

function tabs(facts, emptyList, changes) {
  const t = (id, label, count) => `
    <button class="tab" role="tab" data-tab="${id}" aria-selected="${state.tab === id}">
      ${label}${count != null ? `<span class="count">${count}</span>` : ''}
    </button>`;
  return `
    <div class="tabs" role="tablist">
      ${t('evidence', 'Evidence', facts.length)}
      ${t('abstain', 'Left empty', emptyList.length)}
      ${t('changes', 'Change log', changes.length)}
      ${t('json', 'Profile JSON', null)}
    </div>`;
}

/* ── evidence panel ──────────────────────────────────────────────────────── */

function evidencePanel(facts) {
  if (!facts.length) {
    return `<div class="panelhead"><h2>No facts yet</h2>
      <p>The engine published nothing for this company. Every field it could not evidence is listed under Left empty.</p></div>`;
  }

  const groups = new Map();
  facts.forEach((f) => {
    const g = groupOf(f.field);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(f);
  });

  const order = ['Identity', 'Proof of ownership', 'Web presence', 'Contact', 'Location',
    'Identifiers', 'Entity graph', 'Social profiles', 'Accounts', 'Charter',
    'Register flags', 'Other'];

  return Array.from(groups.entries())
    .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
    .map(([group, items]) => `
      <div class="factgroup">
        <div class="factgroup__head">
          <h3 class="factgroup__title">${E(group)}</h3>
          <span class="factgroup__count">${items.length}</span>
        </div>
        <div class="facts">${items.map((f, i) => factCard(f, i)).join('')}</div>
      </div>`)
    .join('');
}

function factCard(f, i) {
  const conf = typeof f.confidence === 'number' ? f.confidence : 0;
  const pct = Math.round(conf * 100);
  const proofClass = `tag--${f.match_proof || 'registry'}`;
  const layerClass = `tag--${f.layer || 'registry'}`;
  const value = fmtValue(f.field, f.value);
  const valueHtml = isUrl(f.value)
    ? `<a href="${E(f.value)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${E(value)}</a>`
    : E(value);

  return `
    <article class="fact" data-open="false" style="animation-delay:${Math.min(i * 22, 300)}ms">
      <button class="fact__head" aria-expanded="false" data-field="${E(f.field)}">
        <span class="fact__field"><span>${E(f.field)}</span></span>
        <span class="fact__value">${valueHtml}</span>
        <span class="fact__right">
          <span class="tag ${proofClass}">${E(PROOF_LABEL[f.match_proof] || f.match_proof || '')}</span>
          <span class="meter" aria-hidden="true">
            <span class="meter__fill" style="width:${pct}%;background:${confColor(conf)}"></span>
          </span>
          <span class="conf">${pct}%</span>
          <svg class="chev" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
            <path d="M4.5 2.5 8 6l-3.5 3.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
      </button>
      <div class="fact__body"><div><div class="fact__inner">
        ${f.evidence ? `<div class="quote"><span>${E(f.evidence)}</span></div>` : ''}
        ${f.explanation ? `<p class="explain">${E(f.explanation)}</p>` : ''}
        <div class="kv">
          <div class="kv__item"><div class="kv__k">proof type</div>
            <div class="kv__v"><span class="tag ${proofClass}">${E(PROOF_LABEL[f.match_proof] || f.match_proof || '—')}</span></div></div>
          <div class="kv__item"><div class="kv__k">layer</div>
            <div class="kv__v"><span class="tag ${layerClass}">${E(LAYER_LABEL[f.layer] || f.layer || '—')}</span></div></div>
          <div class="kv__item"><div class="kv__k">confidence</div>
            <div class="kv__v">${conf.toFixed(3)}</div></div>
          <div class="kv__item"><div class="kv__k">retrieved</div>
            <div class="kv__v">${E((f.retrieved_at || '').slice(0, 10) || '—')}</div></div>
          ${f.method ? `<div class="kv__item"><div class="kv__k">method</div>
            <div class="kv__v">${E(f.method)}</div></div>` : ''}
          <div class="kv__item"><div class="kv__k">source</div>
            <div class="kv__v">${f.source_url
              ? `<a href="${E(f.source_url)}" target="_blank" rel="noopener noreferrer">${E(shortUrl(f.source_url))}</a>`
              : '—'}</div></div>
        </div>
        ${f.changed_from != null ? `<div class="changed">
          changed from ${E(fmtValue(f.field, f.changed_from))}</div>` : ''}
        ${(f.sources && f.sources.length) ? `
          <div>
            <div class="kv__k" style="margin-bottom:6px">corroborating sources</div>
            <div class="thread">${f.sources.map((s) => `
              <div class="thread__item">
                <span class="tag tag--${E(s.match_proof || 'registry')}">${E(PROOF_LABEL[s.match_proof] || s.match_proof || '')}</span>
                <div>
                  <a href="${E(s.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--registry)">${E(shortUrl(s.url))}</a>
                  <code>${E((s.evidence || '').slice(0, 150))}</code>
                </div>
              </div>`).join('')}</div>
          </div>` : ''}
      </div></div></div>
    </article>`;
}

function confColor(conf) {
  if (conf >= 0.95) return 'var(--rules)';
  if (conf >= 0.85) return 'var(--registry)';
  if (conf >= 0.70) return 'var(--warning)';
  return 'var(--abstain)';
}

/* ── abstention panel ────────────────────────────────────────────────────── */

function abstainPanel(list) {
  if (!list.length) {
    return `<div class="panelhead"><h2>Nothing was left empty</h2>
      <p>Every field the engine considered was evidenced above the confidence threshold.</p></div>`;
  }
  return `
    <div class="panelhead">
      <h2>Left empty on purpose</h2>
      <p>Abstention is the design, not a failure. A field below the confidence threshold is dropped
      and the reason is recorded, because a plausible guess published as a fact is worse than a gap.</p>
    </div>
    <div class="empty-list">
      ${list.map((s) => `
        <div class="empty-item">
          <div class="empty-item__field">${E(s.field)}</div>
          <div class="empty-item__reason">${E(s.reason)}</div>
        </div>`).join('')}
    </div>`;
}

/* ── change panel ────────────────────────────────────────────────────────── */

function changesPanel(list) {
  if (!list.length) {
    return `
      <div class="panelhead"><h2>No changes detected</h2>
        <p>This is the first run for this company, so there is nothing to compare against. On a later
        run the registry change stream is applied and each page is rechecked with a conditional request
        and a content hash; only differences are re-extracted, and each one is logged here with its old
        value, new value and detection date.</p></div>
      <div class="banner banner--info">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
          <circle cx="8" cy="8" r="6.2"/><path d="M8 7.2v4M8 5.1v.1" stroke-linecap="round"/>
        </svg>
        <span>Re-run <code>make run</code> after the first pass to populate this log.</span>
      </div>`;
  }
  return `
    <div class="panelhead">
      <h2>Changes since the previous run</h2>
      <p>Registry patches come from the official change-event stream; web values come from a content
      hash comparison after a conditional request. Each entry records the field, the old value, the new
      value and when it was noticed.</p>
    </div>
    <table class="table">
      <thead><tr><th>Field</th><th>Was</th><th>Now</th><th>Kind</th><th>Detected</th><th>Source</th></tr></thead>
      <tbody>
        ${list.map((c) => `
          <tr>
            <td class="mono">${E(c.field)}</td>
            <td>${E(fmtValue(c.field, c.old))}</td>
            <td><strong>${E(fmtValue(c.field, c.new))}</strong></td>
            <td><span class="tag tag--${c.kind === 'web' ? 'web_rules' : 'registry'}">${E(c.kind)}</span></td>
            <td class="mono">${E((c.detected_at || '').slice(0, 10))}</td>
            <td class="mono">${c.source_url
              ? `<a href="${E(c.source_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--registry)">${E(shortUrl(c.source_url))}</a>`
              : '—'}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

/* ── json panel ──────────────────────────────────────────────────────────── */

function jsonPanel(full) {
  if (!full) return '';
  return `
    <div class="panelhead">
      <h2>Profile document</h2>
      <p>The stored artefact, exactly as written to <code>profiles/${E(full.org_number)}.json</code>.
      This is the object every other tab is a view onto.</p>
    </div>
    <pre class="json">${highlight(JSON.stringify(full, null, 2))}</pre>`;
}

function highlight(json) {
  return E(json)
    .replace(/"([^"\\]*(\\.[^"\\]*)*)":/g, '<span class="k">"$1"</span>:')
    .replace(/: "([^"\\]*(\\.[^"\\]*)*)"/g, ': <span class="s">"$1"</span>')
    .replace(/: (true|false|null)/g, ': <span class="b">$1</span>')
    .replace(/: (-?\d+\.?\d*)/g, ': <span class="n">$1</span>');
}

/* ── wiring ──────────────────────────────────────────────────────────────── */

function wire(orgnr) {
  $$('.fact').forEach((card) => {
    const head = $('.fact__head', card);
    head.addEventListener('click', () => {
      const open = card.getAttribute('data-open') === 'true';
      card.setAttribute('data-open', String(!open));
      head.setAttribute('aria-expanded', String(!open));
    });
  });

  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      state.tab = tab.dataset.tab;
      $$('.tab').forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
      ['evidence', 'abstain', 'changes', 'json'].forEach((id) => {
        const panel = $(`#tab-${id}`);
        if (panel) panel.hidden = id !== state.tab;
      });
    });
  });

  const card = $('.fact');
  if (card && new URLSearchParams(location.search).get('open') === 'first') {
    card.setAttribute('data-open', 'true');
    $('.fact__head', card).setAttribute('aria-expanded', 'true');
  }
}

$('#list').addEventListener('click', (ev) => {
  const row = ev.target.closest('.row');
  if (row) select(row.dataset.orgnr);
});

$('#q').addEventListener('input', (ev) => {
  state.query = ev.target.value;
  renderList();
});

$$('.chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    state.filter = chip.dataset.filter;
    $$('.chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
    renderList();
  });
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === '/' && document.activeElement !== $('#q')) {
    ev.preventDefault();
    $('#q').focus();
  }
});

boot();
