'use strict';
const $ = s => document.querySelector(s);
const now = () => Date.now() / 1000;
let snap = null;

// Builds elements from text only (never HTML), so names can't inject markup.
function h(tag, attrs = {}, ...kids) {
  const e = ['svg', 'use'].includes(tag) ? document.createElementNS('http://www.w3.org/2000/svg', tag)
                                          : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'style') e.style.cssText = v;  // not as an attribute, which the page's CSP refuses
    else e.setAttribute(k, v);
  }
  e.append(...kids.flat().filter(k => k != null));
  return e;
}

function span(secs) {
  if (secs < 60) return Math.max(1, Math.round(secs)) + 's';
  const m = Math.ceil(secs / 60), hr = Math.floor(m / 60);
  if (hr >= 24) return `${Math.floor(hr / 24)}d ${hr % 24}h`;
  return hr ? `${hr}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`;
}
const until = ts => ts > now() ? 'in ' + span(ts - now()) : 'now';
const ago = ts => !ts ? 'never' : now() - ts < 60 ? 'just now' : span(now() - ts) + ' ago';
const clock = ts => ts ? new Date(ts * 1000).toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'}) : '';
const pct = v => Math.round(v * 100) + '%';
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const count = n => n.toLocaleString('en', {notation: 'compact', maximumFractionDigits: 1});
const money = v => v.toLocaleString('en', {style: 'currency', currency: 'USD', notation: 'compact',
                                         minimumFractionDigits: 2, maximumFractionDigits: 2});
const sum = xs => xs.reduce((x, y) => x + y, 0);
const tokens = t => sum(Object.values(t || {}));

// Tokens used and their estimated cost at API prices (the seats themselves are flat-rate).
function usageCell(x) {
  const t = x.tokens || {};
  return h('td', {class: 'num', 'data-label': 'Usage',
                  title: `${count(t.input_tokens || 0)} input, ${count(t.output_tokens || 0)} output, ` +
                         `${count(t.cache_read_input_tokens || 0)} cache reads, ` +
                         `${count(t.cache_creation_input_tokens || 0)} cache writes`},
           count(tokens(t)) + ' tokens',
           h('div', {class: 'sub'}, `${money(x.cost || 0)} · ${plural(x.requests || 0, 'request')}`));
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 6000);
}

async function api(method, path, body) {
  const r = await fetch('/_gateway/api/' + path, {
    method, headers: {'content-type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error?.message || `Request failed (HTTP ${r.status}).`);
  return data;
}

// Runs one management request (after confirming, if there's a question) with its button
// disabled; resolves to whether it worked.
async function act(button, question, work) {
  if (question && !confirm(question)) return false;
  button.disabled = true;
  try { await work(); return true; }
  catch (e) { toast(e.message); return false; }
  finally { button.disabled = false; }
}

// ---------------------------------------------------------------- rendering

// An account's or developer's color: its place in the list, in a fixed order (gray past the eighth).
const color = i => `var(--s${i + 1}, var(--mute))`;

// A usage bar: parts ({v} in 0-1, and a color when there are several) side by side.
const bar = (parts, label) => h('span', {class: 'track', role: 'progressbar', 'aria-label': label, 'aria-valuemin': 0,
                                         'aria-valuemax': 100, 'aria-valuenow': Math.round(sum(parts.map(p => p.v)) * 100)},
  parts.map(p => h('span', {style: `width:${Math.min(100, p.v * 100)}%;background:${p.color ?? 'var(--fill)'}`})));

// A stat tile. With parts ({v, color, label, name}), it has a bar split into one segment per part, a legend
// naming them, and hovering anywhere on it lists them all.
function tile(label, value, sub, parts) {
  const t = h('div', {class: 'tile'}, h('div', {class: 'label'}, label), h('div', {class: 'value'}, value),
    parts && bar(parts.filter(p => p.v > 0), label), h('div', {class: 'sub'}, sub),
    parts?.length > 1 ? h('div', {class: 'legend'}, parts.map(p => h('span', {}, h('i', {style: `background:${p.color}`}), p.label)))
                      : null);
  if (parts) t.title = parts.map(p => p.name).join('\n');
  return t;
}

const noUsage = w => !snap.accounts.some(a => a[w] != null);

// The pool's use of window w: the use of every seat with that limit (none heard yet counts as unused, and none
// counts past its own limit) over the number of them, its bar split into each seat's share of the pool's limit.
function poolUse(w, label) {
  // An API key has credits, not 5-hour and weekly limits, and some plans have only a weekly limit.
  const seats = snap.accounts.filter(a => !a.api && a.windows.includes(w));
  if (!seats.length) return tile(label, '–', 'no account has this limit');
  if (noUsage(w)) return tile(label, '–', 'no data yet');
  const parts = seats.map((a, i) => ({v: Math.min(1, a[w] || 0) / seats.length, color: color(i),
    label: a.label, name: `${a.label}: ${pct(Math.min(1, a[w] || 0))} of its limit used`}));
  const use = sum(parts.map(p => p.v));
  return tile(label, pct(use), pct(Math.max(0, 1 - use)) + ' left of usage limit total', parts);
}

// The same, split by developer: each one's estimated share of the pool's limit (see Pool.snapshot).
function devUse(w, label) {
  if (noUsage(w)) return tile(label, '–', 'no data yet');
  const parts = snap.developers.map((d, i) => ({v: d[w], color: color(i), label: d.name,
                                                name: `${d.name}: ${pct(d[w])} of the pool's limit`}));
  return tile(label, pct(sum(parts.map(p => p.v))), 'estimated from what their requests cost', parts);
}

function meterCell(a, w, label) {
  const v = a[w], reset = a[w + '_reset'];
  if (!a.api && !a.windows.includes(w)) return h('td', {class: 'sub', 'data-label': label}, `no ${label.toLowerCase()} limit`);
  if (v == null) return h('td', {class: 'sub', 'data-label': label}, a.api ? '–' : 'no data yet');
  return h('td', {'data-label': label},
    h('div', {class: 'meter'}, bar([{v}], `${a.label} ${label} use`), h('span', {class: 'pct'}, pct(v))),
    reset > now() ? h('div', {class: 'sub', title: clock(reset)}, 'resets ' + until(reset)) : null);
}

function stateCell(a) {
  if (!a.blocked_kind) return h('td', {'data-label': 'State'}, h('span', {class: 'ok'}, '● Ready'));
  const reason = a.blocked_reason[0].toUpperCase() + a.blocked_reason.slice(1);
  const auth = a.blocked_kind === 'auth';  // its time is when it was rejected, not when it's back
  return h('td', {'data-label': 'State'}, h('span', {class: 'bad'}, '● ' + reason),
           h('div', {class: 'sub', title: (auth ? 'since ' : '') + clock(a.blocked_until)},
             auth ? 'replace its token' : 'back ' + until(a.blocked_until)));
}

function table(id, cols, rows, none) {
  $(id).replaceChildren(
    h('thead', {}, h('tr', {}, cols.map(c => typeof c === 'string' ? h('th', {scope: 'col'}, c) : c))),
    h('tbody', {}, rows.length ? rows : h('tr', {}, h('td', {colspan: cols.length, class: 'sub'}, none))));
}

// Under an account's label: an API key's credits used this month. A seat's usage is only heard in the API's
// replies, so it's as old as its last request through hotpocket; use elsewhere (claude.ai, a direct login)
// doesn't show until then. Say so once it's stale.
function accountNote(a) {
  if (a.api) return h('div', {class: 'sub'}, `API credits: ${money(a.month_cost)} used this month`);
  if (!a.usage_at || now() - a.usage_at < 600) return null;
  return h('div', {class: 'sub', title: clock(a.usage_at)}, 'usage as of ' + ago(a.usage_at));
}

// A Lucide icon from the set in page.html, in the text's color.
const icon = name => h('svg', {class: 'icon', 'aria-hidden': 'true'}, h('use', {href: '#i-' + name}));

// A row's actions sit behind a "⋯" button, in a native popover: it closes on Esc or a click elsewhere.
let menus = 0;
function menu(name, ...items) {
  if (!(items = items.filter(Boolean)).length) return null;
  const id = 'menu-' + ++menus, pop = h('div', {id, class: 'menu', popover: '', style: `position-anchor:--${id}`}, items);
  pop.addEventListener('click', () => pop.hidePopover(), true);  // closes before the item acts (or asks)
  return [h('button', {class: 'more', popovertarget: id, style: `anchor-name:--${id}`, title: 'Actions',
                       'aria-label': 'Actions for ' + name}, icon('ellipsis')), pop];
}

function accountRow(a) {
  return h('tr', {},
    h('th', {scope: 'row'}, a.label, accountNote(a)), stateCell(a), meterCell(a, 'five_hour', '5-hour'), meterCell(a, 'seven_day', 'Weekly'),
    usageCell(a),
    h('td', {class: 'actions admin-only'}, menu(a.label,
      h('button', {onclick: () => replaceToken(a.label)}, icon('key-round'), 'New token'),
      h('button', {class: 'bad', onclick: e => removeAccount(e.currentTarget, a.label)}, icon('trash-2'), 'Remove'))));
}

function devRow(d) {
  return h('tr', {},
    h('th', {scope: 'row'}, d.name),
    usageCell(d),
    h('td', {title: clock(d.last_seen), 'data-label': 'Last seen'}, ago(d.last_seen)),
    h('td', {'data-label': 'Last account'}, d.last_account || '–'),
    // Your own row shows your key (to set up another device) or makes a new one; admins can make anyone's.
    h('td', {class: 'actions'}, menu(d.name,
      d.name === snap.viewer.login && h('button', {onclick: e => showMyKey(e.currentTarget)}, icon('eye'), 'Show key'),
      (d.name === snap.viewer.login || snap.viewer.admin) &&
        h('button', {onclick: e => makeKey(e.currentTarget, d.name)}, icon('key-round'), 'New key'),
      snap.viewer.admin && h('button', {class: 'bad', onclick: e => removeDev(e.currentTarget, d.name)}, icon('trash-2'), 'Remove'))));
}

// The developer (each one has a key) under this name, if any.
const keyOf = name => snap.developers.find(d => d.name === name);

function render() {
  const {accounts, developers, viewer} = snap;
  document.body.classList.toggle('is-admin', viewer.admin);
  $('#who').textContent = viewer.login ? viewer.login + (viewer.admin ? ' · admin' : '') : '';
  $('#access').hidden = Boolean(viewer.login);
  // "Get my key" for anyone signed in who has none yet; once they do, "My key" shows it again.
  $('#mine-button').hidden = !viewer.login;
  $('#mine-button').textContent = keyOf(viewer.login) ? 'My key' : 'Get my key';

  const cost = sum(accounts.map(a => a.cost || 0)), all = sum(accounts.map(a => tokens(a.tokens)));
  $('#tiles').replaceChildren(poolUse('five_hour', '5-hour'), poolUse('seven_day', 'Weekly'),
    tile('Est. API cost', money(cost), `${count(all)} tokens since ` +
         new Date(snap.since * 1000).toLocaleDateString([], {month: 'short', day: 'numeric'})));

  const usageHead = () => h('th', {class: 'num', title: 'Tokens, and what they would cost at API prices'}, 'Usage');
  table('#accounts', ['Account', 'State', '5-hour', 'Weekly', usageHead(), h('th', {class: 'admin-only'})],
        accounts.map(accountRow), 'No accounts in the pool yet.');
  $('#dev-tiles').replaceChildren(devUse('five_hour', '5-hour by developer'), devUse('seven_day', 'Weekly by developer'));
  table('#developers', ['Name', usageHead(), 'Last seen', 'Last account', h('th')],
        developers.map(devRow), 'No developers yet.');
}

// ---------------------------------------------------------------- actions

const path = (kind, name) => `${kind}/${encodeURIComponent(name)}`;

// "Add account" and "Add developer" open their form (a second click closes it); saving closes it too.
function toggle(id, open) {
  const f = $('#' + id);
  f.hidden = !(open ?? f.hidden);
  if (!f.hidden) f.querySelector('input').focus();
  return f;
}
document.querySelectorAll('[data-opens]').forEach(b => b.addEventListener('click', () => toggle(b.dataset.opens)));

function replaceToken(label) {
  const f = toggle('account-form', true);
  f.elements.label.value = label;
  f.elements.token.focus();
  f.scrollIntoView({block: 'center', behavior: 'smooth'});
}

const removeAccount = (button, label) =>
  act(button, `Remove the account “${label}”? Requests stop using it right away.`,
      async () => { await api('DELETE', path('accounts', label)); toast(`Removed ${label}.`); });

const removeDev = (button, name) =>
  act(button, `Remove ${name}? Their key stops working right away. Anyone on the tailnet can still make ` +
              'their own key, so to keep someone out, remove them from the tailnet.', async () => {
    await api('DELETE', path('keys', name));
    if ($('#reveal').dataset.name === name) hideReveal();  // don't leave a dead key on screen
    toast(`Removed ${name}.`);
  });

// A new key for yourself (anyone signed in), or for anyone (admins); asks first when it replaces one.
function makeKey(button, name, question) {
  const mine = name === snap.viewer.login;
  question ??= keyOf(name) && (mine ? 'Make a new key? Your old one stops working right away, on every device.'
                                    : `Make a new key for ${name}? Their current key stops working.`);
  return act(button, question,
             async () => reveal(name, (await api('POST', mine ? 'my-key' : path('keys', name))).key, mine));
}

// Your key again, to set up another device with it, or a first one. Asks the gateway, not this page's data,
// which can miss a key just made elsewhere; a key it didn't keep (an old one) can only be replaced.
function showMyKey(button) {
  const me = snap.viewer.login;
  return act(button, null, async () => {
    const shown = await api('POST', 'my-key/show');
    await (shown.key ? reveal(me, shown.key, true) : makeKey(button, me, 'key' in shown &&
      "Your key was made before hotpocket kept keys, so it can't be shown. Make a new one? The old one stops " +
      'working on every device set up with it.'));
  });
}
$('#mine-button').addEventListener('click', e => showMyKey(e.currentTarget));

// A prompt that has Claude Code send its requests through the gateway while staying logged in to
// claude.ai: the key goes in its own header, so the login (Artifacts, connectors) stays in charge.
const setupPrompt = key => `Set up Claude Code to use our team's Claude gateway, hotpocket, while staying \
logged in to my claude.ai account (so Artifacts and connectors keep working). In ~/.claude/settings.json (create it \
if it doesn't exist), add these to the top-level "env" object, keeping everything else in the file:
  "ANTHROPIC_BASE_URL": "${location.origin}",
  "ANTHROPIC_CUSTOM_HEADERS": "x-hotpocket-key: ${key}"
If that "env" also sets ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY, remove them: they would replace my login. Then \
check it works by running \`claude -p "Reply with OK"\` and tell me what it printed, and that I need to restart \
Claude Code to switch over.`;

// Shows a key under the developers table, as a setup prompt and a settings.json snippet, until closed.
function reveal(name, key, mine = false) {
  const panel = $('#reveal');
  const env = {ANTHROPIC_BASE_URL: location.origin, ANTHROPIC_AUTH_TOKEN: key};  // for no claude.ai login
  panel.dataset.name = name;
  $('#reveal-title').textContent = mine ? 'Your key' : `Key for ${name}`;
  $('#reveal-again').textContent = mine ? 'Open it again any time with My key.'
                                        : `Give it to ${name} privately.`;
  $('#reveal-prompt').textContent = setupPrompt(key);
  $('#reveal-snippet').textContent = JSON.stringify({env}, null, 2);
  panel.querySelectorAll('[data-copy]').forEach(b => { b.textContent = 'Copy'; });
  panel.hidden = false;
  panel.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

document.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', async () => {
  const pre = $('#' + b.dataset.copy);
  try {
    await navigator.clipboard.writeText(pre.textContent);
    b.textContent = 'Copied';
  } catch {  // the browser said no: select it instead, for the keyboard shortcut
    getSelection().selectAllChildren(pre);
    toast('Selected: press Ctrl+C (⌘C on a Mac) to copy.');
  }
}));
function hideReveal() {
  $('#reveal-prompt').textContent = $('#reveal-snippet').textContent = '';
  $('#reveal').hidden = true;
}
$('#reveal-done').addEventListener('click', hideReveal);

$('#account-form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target, label = f.elements.label.value.trim();
  const replacing = snap.accounts.some(a => a.label === label);
  if (await act(f.querySelector('button'), null, async () => {
    await api('PUT', path('accounts', label), {token: f.elements.token.value});
    toast(replacing ? `Replaced the token for ${label}.` : `Added ${label}.`);
  })) { f.reset(); f.hidden = true; }
});

$('#key-form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target, name = f.elements.dev.value.trim().toLowerCase();
  if (await makeKey(f.querySelector('button'), name)) { f.reset(); f.hidden = true; }
});

// The gateway sends the page's data when it opens, when it changes, and every 30 s. The browser reconnects
// by itself after a dropped connection, but gives up on an error reply (say a 502 from the proxy while the
// gateway restarts), so then this starts over.
(function listen() {
  const events = new EventSource('/_gateway/events');
  events.onmessage = e => {
    snap = JSON.parse(e.data);
    $('#offline').hidden = true;
    if (!$(':popover-open')) render();  // not under an open menu
  };
  events.onerror = () => {
    $('#offline').hidden = false;
    if (events.readyState === EventSource.CLOSED) setTimeout(listen, 5000);
  };
})();
// Data that came while a menu was open shows once it closes (toggle doesn't bubble, hence the capture).
document.addEventListener('toggle', e => { if (e.newState === 'closed' && snap && !$(':popover-open')) render(); }, true);
