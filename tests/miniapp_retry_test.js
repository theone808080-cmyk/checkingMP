const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('web/miniapp.html', 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const elements = {};
function el(id) {
  if (!elements[id]) elements[id] = {
    value: 'all', hidden: false, textContent: '', children: [], handlers: {},
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; },
    addEventListener(event, cb) { this.handlers[event] = cb; },
    setAttribute() {}
  };
  return elements[id];
}
const requests = [];
async function fetch(url) {
  requests.push(url);
  if (requests.length === 2) throw new Error('network unavailable');
  return {ok:true, async json() { return {materials:[], total:2, next_cursor:requests.length === 1 ? 'next-page' : null}; }};
}
const document = {getElementById:el, createElement:()=>el(Symbol()), documentElement:{style:{setProperty(){}}}};
vm.runInNewContext(script, {window:{}, document, fetch, URL, URLSearchParams, Intl, Date, console});
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await tick();
  assert.strictEqual(el('more').hidden, false, 'first page should offer more');
  el('more').handlers.click();
  await tick();
  assert.strictEqual(el('more').hidden, false, 'failed page must remain retryable');
  el('more').handlers.click();
  await tick();
  assert.strictEqual(requests.length, 3);
  assert.ok(requests[1].includes('cursor=next-page'));
  assert.strictEqual(requests[1], requests[2], 'retry must preserve cursor');
})().catch(error => { console.error(error); process.exitCode = 1; });
