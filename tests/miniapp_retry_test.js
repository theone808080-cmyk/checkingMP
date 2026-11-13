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
let feedRequests = 0;
async function fetch(url) {
  requests.push(url);
  if (url.startsWith('/api/findings/counts')) return {ok:true, async json() { return {
    period:{all:0,today:0,week:0}, platform:{all:0,ozon:0,wildberries:0,yandex:0,chestny_znak:0,industry:0},
    importance:{all:0,1:0,2:0,3:0,4:0,5:0}, topic_status:{topics:[],verification:{all:0,confirmed:0,needs_check:0}},
    source_type:{all:0,official:0,regulator:0,industry:0}
  }; }};
  if (url.startsWith('/api/findings')) return {ok:true, async json() { return {findings:[], total:0, next_cursor:null}; }};
  if (url.startsWith('/api/feed')) {
    feedRequests += 1;
    if (feedRequests === 2) throw new Error('network unavailable');
    return {ok:true, async json() { return {materials:[], total:2, next_cursor:feedRequests === 1 ? 'next-page' : null}; }};
  }
  throw new Error('unexpected request');
}
const document = {getElementById:el, createElement:()=>el(Symbol()), documentElement:{style:{setProperty(){}}}};
vm.runInNewContext(script, {window:{}, document, fetch, URL, URLSearchParams, Intl, Date, console});
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await tick();
  await tick();
  el('nav-feed').handlers.click();
  await tick();
  await tick();
  assert.strictEqual(el('more').hidden, false, 'first page should offer more');
  el('more').handlers.click();
  await tick();
  assert.strictEqual(el('more').hidden, false, 'failed page must remain retryable');
  el('more').handlers.click();
  await tick();
  const feedUrls = requests.filter(url => url.startsWith('/api/feed?'));
  assert.strictEqual(feedUrls.length, 3);
  assert.ok(feedUrls[1].includes('cursor=next-page'));
  assert.strictEqual(feedUrls[1], feedUrls[2], 'retry must preserve cursor');
})().catch(error => { console.error(error); process.exitCode = 1; });
