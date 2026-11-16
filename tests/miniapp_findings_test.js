const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('web/miniapp.html', 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
assert.ok(html.includes('<fieldset'), 'finding filters should be exposed as named groups');
assert.ok(html.includes('legend'), 'filter groups should have accessible names');
assert.ok(html.includes('input:focus-visible'), 'keyboard focus must remain visible for topic search');
assert.ok(html.includes('@media (max-width:520px)'), 'narrow layout should stack filter groups');
assert.ok(html.includes('id="finding-seller-impact"'), 'seller impact should be an additional, compact finding filter');
assert.ok(html.includes('id="finding-summary-audit"'), 'summary audit should be an additional, compact finding filter');
assert.ok(html.includes('id="finding-recommendation-audit"'), 'recommendation audit should be an additional, compact finding filter');
assert.ok(html.indexOf('id="finding-period"') < html.indexOf('<details class="extra-filters">'), 'period should stay visible before additional filters');
assert.ok(html.indexOf('id="finding-platform"') < html.indexOf('<details class="extra-filters">'), 'platform should stay visible before additional filters');
const elements = {};
function el(id) {
  if (!elements[id]) elements[id] = {
    value: 'all', hidden: false, textContent: '', children: [], handlers: {}, attributes: {},
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; },
    addEventListener(event, cb) { this.handlers[event] = cb; },
    dispatch(event) { return this.handlers[event] ? this.handlers[event]({target:this}) : undefined; },
    setAttribute(name, value) { this.attributes[name] = value; }
  };
  return elements[id];
}
const requests = [];
const requestOptions = [];
const draftConsequence = 'Changed delivery tariff\nРекомендуемое действие: Review shipments\nСвязь и последствия нужно проверить.';
async function fetch(url, options) {
  requests.push(url);
  requestOptions.push({url, options:options || {}});
  if (url === '/api/findings/7/read') {
    return {ok:true, async json() { return {id:7, read:true}; }};
  }
  if (url === '/api/findings/7/impacts' && options && options.method === 'POST') {
    return {ok:true, async json() { return {id:71, platform:'Ozon', object_type:'marketplace', object_name:null,
      change_type:null, effective_date:null, processes:[], consequence:draftConsequence, source_url:'https://example.org/source',
      status:'needs_review', decided_by:null, decided_at:null}; }};
  }
  if (url === '/api/findings/7/impacts') {
    return {ok:true, async json() { return {impacts:[]}; }};
  }
  if (url === '/api/findings/8/impacts') {
    return {ok:true, async json() { return {impacts:[{
      id:72, platform:'Ozon', object_type:'seller_warehouse', object_name:'Склад продавца',
      change_type:'acceptance', effective_date:'2026-12-01', processes:['acceptance'],
      consequence:'Сверить график приёмки.', source_url:'https://example.org/impact-source-2',
      status:'needs_review', decided_by:null, decided_at:null
    }]}; }};
  }
  if (url === '/api/finding-impacts/72/decision') {
    return {ok:true, async json() { return {id:72, platform:'Ozon', object_type:'seller_warehouse', object_name:'Склад продавца',
      change_type:'acceptance', effective_date:'2026-12-01', processes:['acceptance'], consequence:'Сверить график приёмки.',
      source_url:'https://example.org/impact-source-2', status:'confirmed', decided_by:'owner-42', decided_at:'2026-11-16T12:00:00Z', updated_at:'2026-11-16T12:05:00Z'}; }};
  }
  if (url.startsWith('/api/findings/counts')) {
    return {ok:true, async json() { return {
      period:{all:21,today:21,week:21}, platform:{all:21,ozon:21,wildberries:0,yandex:0,chestny_znak:0,industry:0},
      importance:{all:21,1:0,2:0,3:0,4:0,5:21}, source_type:{all:21,official:21,regulator:0,industry:0},
      seller_impact:{all:21,positive:0,negative:21,mixed:0,neutral:0,unclear:0},
      summary_audit:{all:21,ok:1,review:0,issue:0,not_checked:20},
      recommendation_audit:{all:21,ok:0,review:1,issue:0,not_checked:20},
      topic_status:{topics:[{value:'logistics',count:21}],verification:{all:21,confirmed:0,needs_check:21}}
    }; }};
  }
  if (url.startsWith('/api/findings')) {
    return {ok:true, async json() { return {
      findings:[{
        id:7, platform:'Ozon', topic:'logistics', importance:5, seller_impact:'negative', summary:'Changed delivery tariff',
        recommended_action:'Review shipments', verification:'needs_check', effective_date:null,
        updated_at:'2026-11-09T09:00:00Z', specificity:1, classification:'seller_action_or_risk', relevance:'relevant',
        summary_audit:'ok', recommendation_audit:'review',
        sources:[{name:'Official Ozon',trust:'official',category:'ozon',url:'https://example.org/source',published_at:null,collected_at:'2026-11-09T09:00:00Z'}]
      }, {
        id:8, platform:'Ozon', topic:'rules', importance:4, seller_impact:'unclear', summary:'Changed acceptance schedule',
        recommended_action:'Read the source', verification:'needs_check', effective_date:null,
        updated_at:'2026-11-09T08:00:00Z', specificity:1, classification:'not_assessed', relevance:'not_assessed',
        summary_audit:'not_checked', recommendation_audit:'not_checked', sources:[]
      }], total:1, next_cursor:requests.filter(value => value.startsWith('/api/findings?')).length === 1 ? 'find-next' : null
    }; }};
  }
  return {ok:true, async json() { return {materials:[], total:0, next_cursor:null}; }};
}
const document = {getElementById:el, createElement:(tag)=>{ const node=el(Symbol()); node.tagName=tag; return node; }, documentElement:{style:{setProperty(){}}}};
vm.runInNewContext(script, {window:{}, document, fetch, URL, URLSearchParams, Intl, Date, console});
const tick = () => new Promise(resolve => setImmediate(resolve));
function collect(node) { return [node, ...(node.children || []).flatMap(collect)]; }
(async () => {
  await tick();
  await tick();
  await tick();
  assert.ok(requests.some(url => url.startsWith('/api/findings?')));
  const findingsRequest = requests.find(url => url.startsWith('/api/findings?'));
  assert.ok(findingsRequest.includes('period=fresh') && findingsRequest.includes('read=unread') && findingsRequest.includes('platform=all'),
    'default finding request should explicitly select fresh unread findings');
  const countsRequest = requests.find(url => url.startsWith('/api/findings/counts?'));
  assert.ok(countsRequest, 'finding filters should request full-result counts');
  assert.ok(countsRequest.includes('period=fresh') && countsRequest.includes('read=unread') && countsRequest.includes('platform=all'));
  const visible = collect(el('findings-cards')).map(node => node.textContent).join(' ');
  assert.ok(visible.includes('Важность 5'), 'finding should show a numbered importance label');
  assert.ok(/риск|отрицател|негатив|ухудшен/i.test(visible), 'finding should show negative seller impact in Russian');
  const nodes = collect(el('findings-cards'));
  const checks = nodes.filter(node => node.className === 'finding-checks');
  assert.strictEqual(checks.length, 2, 'each finding should create its own collapsed Checks disclosure');
  for (const disclosure of checks) {
    assert.strictEqual(disclosure.tagName, 'details');
    assert.strictEqual(disclosure.children[0].tagName, 'summary');
    assert.strictEqual(disclosure.children[0].textContent, 'Проверки');
  }
  const firstChecks = collect(checks[0]).map(node => node.textContent).join(' ');
  const secondChecks = collect(checks[1]).map(node => node.textContent).join(' ');
  for (const label of ['Классификатор: действие или риск для продавца', 'Релевантность: релевантно продавцу',
    'Проверка находки: нужно проверить', 'Аудит сводки: без замечаний', 'Аудит рекомендации: требуется разбор']) {
    assert.ok(firstChecks.includes(label), `${label} should be nested inside the first Checks disclosure`);
  }
  for (const label of ['Классификатор: нет оценки', 'Релевантность: нет оценки',
    'Проверка находки: нужно проверить', 'Аудит сводки: не проверено', 'Аудит рекомендации: не проверено']) {
    assert.ok(secondChecks.includes(label), `${label} should be nested inside the second Checks disclosure`);
  }
  const attention = nodes.filter(node => node.className === 'finding-attention');
  assert.strictEqual(attention.length, 1, 'only stored review or issue results should create a visible attention marker');
  assert.strictEqual(attention[0].textContent, 'Есть замечание');
  assert.ok(nodes.some(node => node.className === 'importance-square importance-5'));
  assert.ok(nodes.some(node => node.href === 'https://example.org/source'));
  assert.strictEqual(el('findings').hidden, false);
  const findingDetails = nodes.filter(node => node.className === 'finding-details');
  const firstFindingDetails = findingDetails[0];
  await firstFindingDetails.dispatch('toggle');
  await tick();
  const impactText = collect(firstFindingDetails).map(node => node.textContent).join(' ');
  const saveDraftButton = collect(firstFindingDetails).find(node => node.tagName === 'button' && node.textContent === 'Сохранить карточку');
  assert.ok(saveDraftButton, 'local draft should let the owner save it without inventing missing details');
  assert.ok(requests.includes('/api/findings/7/impacts'), 'opening a finding should load its impact cards');
  assert.ok(impactText.includes('Ozon') && impactText.includes('logistics') && impactText.includes('Changed delivery tariff') && impactText.includes('Review shipments'),
    'local draft should reuse the already loaded finding fields');
  assert.ok(impactText.includes('Нужно проверить'), 'a local draft must never appear as confirmed');
  assert.ok(impactText.includes('Не указано'), 'missing sorting center, process, tariff, and date should remain unspecified');
  assert.ok(collect(firstFindingDetails).some(node => node.href === 'https://example.org/source'), 'local draft should preserve the loaded finding source link');
  assert.ok(!requests.some(url => /openrouter|model|infer/i.test(url)), 'local draft should not call a model endpoint');
  await saveDraftButton.dispatch('click');
  await tick();
  const saveRequest = requestOptions.find(request => request.url === '/api/findings/7/impacts' && request.options.method === 'POST');
  assert.ok(saveRequest, 'saving a local draft should use the finding impact endpoint');
  assert.deepStrictEqual(JSON.parse(saveRequest.options.body), {
    platform:'Ozon', object_type:'marketplace', object_name:null, change_type:null, effective_date:null,
    processes:[], consequence:draftConsequence, source_url:'https://example.org/source'
  });
  assert.ok(collect(firstFindingDetails).map(node => node.textContent).join(' ').includes('Нужно проверить'),
    'saved draft should remain needs_review until the owner makes a separate decision');
  const secondFindingDetails = findingDetails[1];
  await secondFindingDetails.dispatch('toggle');
  await tick();
  const confirmButton = collect(secondFindingDetails).find(node => node.tagName === 'button' && node.textContent === 'Подтвердить связь');
  const rejectButton = collect(secondFindingDetails).find(node => node.tagName === 'button' && node.textContent === 'Отклонить');
  assert.ok(confirmButton && rejectButton, 'saved impact card should offer owner confirmation and rejection');
  await confirmButton.dispatch('click');
  await tick();
  const decisionRequest = requestOptions.find(request => request.url === '/api/finding-impacts/72/decision');
  assert.strictEqual(decisionRequest.options.method, 'POST');
  assert.deepStrictEqual(JSON.parse(decisionRequest.options.body), {decision:'confirmed'});
  assert.ok(collect(secondFindingDetails).map(node => node.textContent).join(' ').includes('Подтверждена'),
    'owner decision response should replace the visible card status');
  const decidedText = collect(secondFindingDetails).map(node => node.textContent).join(' ');
  assert.ok(decidedText.includes('owner-42'), 'saved impact should show which owner account made the decision');
  assert.ok(decidedText.includes('15:00') && decidedText.includes('15:05'), 'decision and last edit should show Moscow local times');
  await el('findings-more').handlers.click();
  await tick();
  assert.ok(requests.some(url => url.includes('cursor=find-next')));
  const archiveButton = collect(firstFindingDetails).find(node => node.tagName === 'button' && node.textContent === 'В архив');
  assert.ok(archiveButton, 'unread finding should offer an archive action that marks it as read');
  await archiveButton.dispatch('click');
  await tick();
  const archiveRequest = requestOptions.find(request => request.url === '/api/findings/7/read');
  assert.ok(archiveRequest, 'archive action should persist read state');
  assert.strictEqual(archiveRequest.options.method, 'POST');
  assert.deepStrictEqual(JSON.parse(archiveRequest.options.body), {read:true});
  const topicButtons = collect(el('finding-topic-options')).filter(node => String(node.tagName).toLowerCase() === 'button');
  assert.strictEqual(topicButtons.length, 1, 'available topics should be rendered as one button per topic');
  const topicButton = topicButtons[0];
  assert.ok(topicButton.textContent.includes('logistics') && topicButton.textContent.includes('21'));
  assert.strictEqual(topicButton.attributes['aria-pressed'], 'false');
  await topicButton.handlers.click();
  await tick();
  assert.strictEqual(el('finding-topic').value, 'logistics');
  const selectedTopicButton = collect(el('finding-topic-options')).find(node =>
    String(node.tagName).toLowerCase() === 'button' && node.textContent.includes('logistics'));
  assert.strictEqual(selectedTopicButton.attributes['aria-pressed'], 'true');
  const topicRequest = requests.filter(url => url.startsWith('/api/findings?')).at(-1);
  assert.ok(topicRequest.includes('topic=logistics') && !topicRequest.includes('cursor='), 'topic selection should reset pagination');
  el('finding-verification').value = 'confirmed';
  await el('finding-verification').handlers.change();
  await tick();
  assert.strictEqual(el('finding-topic').value, '', 'status selection should clear the topic in its shared filter group');
  const statusRequest = requests.filter(url => url.startsWith('/api/findings?')).at(-1);
  assert.ok(statusRequest.includes('verification=confirmed') && !statusRequest.includes('topic=') && !statusRequest.includes('cursor='),
    'status selection should reset pagination without retaining the topic');
  el('finding-period').value = 'week';
  el('finding-platform').value = 'ozon';
  el('finding-importance').value = '3';
  el('finding-topic').value = 'logistics';
  el('finding-verification').value = 'needs_check';
  el('finding-source-type').value = 'official';
  el('finding-seller-impact').value = 'negative';
  el('finding-summary-audit').value = 'ok';
  el('finding-recommendation-audit').value = 'review';
  await el('finding-importance').handlers.change();
  await tick();
  const filtered = requests.find(url => url.includes('/api/findings?') && url.includes('importance=3'));
  assert.ok(filtered && !filtered.includes('cursor='), 'filter change should reset pagination');
  for (const value of ['period=week','platform=ozon','topic=logistics','verification=needs_check','source_type=official','seller_impact=negative','summary_audit=ok','recommendation_audit=review']) assert.ok(filtered.includes(value));
  const filteredCounts = requests.find(url => url.includes('/api/findings/counts?') && url.includes('importance=3'));
  assert.ok(filteredCounts && !filteredCounts.includes('cursor='), 'counts should refresh with the same reset filters');
  await el('nav-feed').handlers.click();
  assert.strictEqual(el('feed').hidden, false, 'source publications should remain accessible');
})().catch(error => { console.error(error); process.exitCode = 1; });
