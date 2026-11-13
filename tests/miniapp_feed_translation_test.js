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
const longSummary = 'Условия для продавцов изменились, проверьте влияние на расчёты и договоры. '.repeat(8).trim();
const findings = [{
  id: 11, importance: 5, platform: 'Ozon', topic: 'Тарифы', verification: 'needs_check',
  summary: longSummary, recommended_action: 'Проверьте расчёты.',
  effective_date: '2026-11-20T00:00:00Z',
  sources: [{name: 'Официальный источник', url: 'https://ozon.example/rules'}]
}];
const requests = [];
async function fetch(url) {
  requests.push(String(url));
  if (url.startsWith('/api/feed')) return {ok:true, async json() { return {materials, total:materials.length, next_cursor:null}; }};
  if (url.startsWith('/api/findings/counts')) return {ok:true, async json() { return {
    period:{all:1,today:1,week:1}, platform:{all:1,ozon:1,wildberries:0,yandex:0,chestny_znak:0,industry:0},
    importance:{all:1,1:0,2:0,3:0,4:0,5:1}, topic_status:{verification:{all:1,confirmed:0,needs_check:1},topics:[]},
    source_type:{all:1,official:1,regulator:0,industry:0}
  }; }};
  if (url.startsWith('/api/findings')) return {ok:true, async json() { return {findings, total:findings.length, next_cursor:null}; }};
  return {ok:true, async json() { return {}; }};
}
const document = {getElementById:el, createElement:(tag)=>{
  const node = el(Symbol()); node.tagName = String(tag).toUpperCase(); return node;
}, documentElement:{style:{setProperty(){}}}};
vm.runInNewContext(script, {window:{}, document, fetch, URL, URLSearchParams, Intl, Date, console});
const tick = () => new Promise(resolve => setImmediate(resolve));
function collect(node) { return [node, ...(node.children || []).flatMap(collect)]; }
(async () => {
  await tick();
  await tick();
  assert.strictEqual(el('nav-findings').attributes['aria-selected'], 'true', 'Находки открываются первой вкладкой');
  assert.strictEqual(el('findings').hidden, false);
  assert.strictEqual(el('feed').hidden, true);
  const findingNodes = collect(el('findings-cards'));
  const findingDetails = findingNodes.filter(node => node.className === 'finding-details');
  assert.strictEqual(findingDetails.length, 1, 'внешние подробности находки раскрываются семантическим элементом');
  const checksDisclosure = findingNodes.filter(node => node.className === 'finding-checks');
  assert.strictEqual(checksDisclosure.length, 1, 'вложенная проверка не должна считаться внешними подробностями находки');
  const disclosure = findingDetails[0].children.find(node => node.tagName === 'SUMMARY');
  const detailBody = findingDetails[0].children.find(node => node.tagName !== 'SUMMARY');
  const previewText = collect(disclosure).map(node => String(node.textContent)).join(' ');
  assert.ok(previewText.includes(longSummary.slice(0, 80)), 'компактный preview сводки виден до раскрытия');
  assert.ok(!previewText.includes(longSummary), 'полная длинная сводка не занимает компактную часть карточки');
  assert.ok(collect(detailBody).some(node => node.textContent === longSummary), 'полная сводка доступна в раскрываемых подробностях');
  assert.ok(collect(findingDetails[0]).some(node => String(node.textContent).includes('Проверьте расчёты.')));
  assert.ok(collect(findingDetails[0]).some(node => node.href === 'https://ozon.example/rules'));
  const secondaryFilters = html.match(/<details\b[^>]*>[\s\S]*?<summary[^>]*>\s*Ещё фильтры\s*<\/summary>([\s\S]*?)<\/details>/);
  assert.ok(secondaryFilters, 'вторичные фильтры находятся в раскрываемой панели');
  for (const id of ['finding-importance', 'finding-topic', 'finding-verification', 'finding-source-type']) {
    assert.ok(secondaryFilters[1].includes(`id="${id}"`), `${id} остаётся внутри раскрываемой панели`);
  }
  assert.match(html, /id="finding-active-filters"/, 'активные вторичные условия выводятся компактными метками');
  const activeFilterRule = html.match(/\.active-filter\s*\{([^}]*)\}/);
  assert.ok(activeFilterRule, 'для активной метки определено отдельное CSS-правило');
  assert.match(activeFilterRule[1], /max-width\s*:\s*100%/, 'длинная тема не расширяет ширину экрана');
  assert.match(activeFilterRule[1], /overflow-wrap\s*:\s*anywhere/, 'длинная тема переносится внутри метки');
  el('finding-importance').value = '5';
  el('finding-importance').handlers.change();
  await tick();
  await tick();
  assert.ok(requests.some(url => url.startsWith('/api/findings?') && url.includes('importance=5')));
  assert.ok(collect(el('finding-active-filters')).some(node => String(node.textContent).includes('5')));
  el('nav-feed').handlers.click();
  await tick();
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
  el('nav-findings').handlers.click();
  await tick();
  await tick();
  assert.strictEqual(el('finding-importance').value, '5', 'вторичный фильтр сохраняется после смены вкладки');
  assert.ok(requests.at(-2).includes('importance=5'), 'сохранённый фильтр участвует в следующей выдаче');
})().catch(error => { console.error(error); process.exitCode = 1; });
