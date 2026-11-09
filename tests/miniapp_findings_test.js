const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('web/miniapp.html', 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const elements = {};
function el(id) {
  if (!elements[id]) elements[id] = {
    value: 'all', hidden: false, textContent: '', children: [], handlers: {}, attributes: {},
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; },
    addEventListener(event, cb) { this.handlers[event] = cb; },
    setAttribute(name, value) { this.attributes[name] = value; }
  };
  return elements[id];
}
const requests = [];
async function fetch(url) {
  requests.push(url);
  if (url.startsWith('/api/findings')) {
    return {ok:true, async json() { return {
      findings:[{
        id:7, platform:'Ozon', topic:'logistics', importance:5, summary:'Changed delivery terms',
        recommended_action:'Review shipments', verification:'needs_check', effective_date:null,
        updated_at:'2026-11-09T09:00:00Z', specificity:1,
        sources:[{name:'Official Ozon',trust:'official',category:'ozon',url:'https://example.org/source',published_at:null,collected_at:'2026-11-09T09:00:00Z'}]
      }], total:1, next_cursor:requests.filter(value => value.startsWith('/api/findings')).length === 1 ? 'find-next' : null
    }; }};
  }
  return {ok:true, async json() { return {materials:[], total:0, next_cursor:null}; }};
}
const document = {getElementById:el, createElement:()=>el(Symbol()), documentElement:{style:{setProperty(){}}}};
vm.runInNewContext(script, {window:{}, document, fetch, URL, URLSearchParams, Intl, Date, console});
const tick = () => new Promise(resolve => setImmediate(resolve));
function collect(node) { return [node, ...(node.children || []).flatMap(collect)]; }
(async () => {
  await tick();
  await el('nav-findings').handlers.click();
  await tick();
  assert.ok(requests.some(url => url.startsWith('/api/findings?')));
  const visible = collect(el('findings-cards')).map(node => node.textContent).join(' ');
  assert.ok(visible.includes('Важность 5'), 'finding should show a numbered importance label');
  const nodes = collect(el('findings-cards'));
  assert.ok(nodes.some(node => node.className === 'importance-square importance-5'));
  assert.ok(nodes.some(node => node.href === 'https://example.org/source'));
  assert.strictEqual(el('findings').hidden, false);
  await el('findings-more').handlers.click();
  await tick();
  assert.ok(requests.some(url => url.includes('cursor=find-next')));
  el('finding-period').value = 'week';
  el('finding-platform').value = 'ozon';
  el('finding-importance').value = '3';
  el('finding-topic').value = 'logistics';
  el('finding-verification').value = 'needs_check';
  el('finding-source-type').value = 'official';
  await el('finding-importance').handlers.change();
  await tick();
  const filtered = requests.find(url => url.includes('/api/findings?') && url.includes('importance=3'));
  assert.ok(filtered && !filtered.includes('cursor='), 'filter change should reset pagination');
  for (const value of ['period=week','platform=ozon','topic=logistics','verification=needs_check','source_type=official']) assert.ok(filtered.includes(value));
  await el('nav-feed').handlers.click();
  assert.strictEqual(el('feed').hidden, false, 'source publications should remain accessible');
})().catch(error => { console.error(error); process.exitCode = 1; });
