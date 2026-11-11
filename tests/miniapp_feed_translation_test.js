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
const materials = [
  {id:1, title:'New customs rule', text:'An English excerpt from the source.', title_ru:'Новое таможенное правило', text_ru:'Новый фрагмент по-русски', translation_status:'ready', url:'https://foreign.example/new'},
  {id:2, title:'Pending English headline', text:'Pending English text', title_ru:null, text_ru:null, translation_status:'pending', url:'https://foreign.example/pending'},
  {id:3, title:'Legacy English headline', text:'Legacy English text', title_ru:null, text_ru:null, translation_status:'legacy', url:'https://foreign.example/legacy'}
];
async function fetch(url) {
  if (url.startsWith('/api/feed')) return {ok:true, async json() { return {materials, total:materials.length, next_cursor:null}; }};
  return {ok:true, async json() { return {}; }};
}
const document = {getElementById:el, createElement:()=>el(Symbol()), documentElement:{style:{setProperty(){}}}};
vm.runInNewContext(script, {window:{}, document, fetch, URL, URLSearchParams, Intl, Date, console});
const tick = () => new Promise(resolve => setImmediate(resolve));
function collect(node) { return [node, ...(node.children || []).flatMap(collect)]; }
(async () => {
  await tick();
  const cards = el('cards').children;
  assert.strictEqual(cards.length, 3);
  const visible = cards.map(card => collect(card).map(node => node.textContent).join(' '));
  assert.ok(visible[0].includes('Новое таможенное правило'));
  assert.ok(visible[0].includes('Новый фрагмент по-русски'));
  assert.ok(!visible[0].includes('New customs rule'));
  assert.ok(visible[1].includes('Перевод обрабатывается'));
  assert.ok(!visible[1].includes('Pending English headline'));
  assert.ok(!visible[1].includes('Pending English text'));
  assert.ok(visible[2].includes('Legacy English headline'), 'legacy materials remain unchanged');
  const links = cards.flatMap(collect).filter(node => node.href);
  assert.ok(links.some(link => link.href === 'https://foreign.example/new'));
  assert.ok(links.some(link => link.href === 'https://foreign.example/pending'));
})().catch(error => { console.error(error); process.exitCode = 1; });
