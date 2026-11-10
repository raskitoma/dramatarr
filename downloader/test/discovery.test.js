'use strict';
const test=require('node:test');const assert=require('node:assert/strict');
const {validateWatchUrl,sourceProvider,classifyCandidates}=require('../src/discovery');
const {createProgressReporter}=require('../src/progress');
for (const provider of ['reelshort','dramabox','goodshort','dramawave','netshort']) {
 test(`accepts the published ${provider} namespace`,()=>{
  assert.equal(validateWatchUrl(`https://${provider}.dramafren.org/index.php?page=watch&id=123&lang=es`).hostname,`${provider}.dramafren.org`);
 });
}
test('rejects lookalike hosts, arbitrary subdomains, credentials and unexpected ports',()=>{
 for(const url of ['https://reelshort.dramafren.org.evil.test/','https://evil.dramafren.org/','https://x:y@reelshort.dramafren.org/','http://reelshort.dramafren.org/','https://reelshort.dramafren.org:81/'])assert.throws(()=>validateWatchUrl(url));
 assert.equal(sourceProvider('https://www.reelshort.com/movie/123'),'reelshort');
 assert.equal(sourceProvider('https://evilreelshort.com/movie/123'),null);
});
test('localized titles keep their provider identifier and language, without fuzzy identity',()=>{
 const items=classifyCandidates([
  {title:'El monstruo que rescaté es el Rey Dragón',url:'https://reelshort.dramafren.org/index.php?page=watch&id=abc&lang=es'},
  {title:'The Monster I Rescued Is the Dragon King',url:'https://reelshort.dramafren.org/index.php?page=watch&id=other&lang=en'},
  {title:'再见',url:'https://reelshort.dramafren.org/index.php?page=watch&id=cjk&lang=zh'},
 ],'The Monster I Rescued Is the Dragon King','https://www.reelshort.com/movie/abc');
 assert.equal(items[0].idMatch,true);assert.equal(items[0].language,'es');
 assert.equal(items[1].idMatch,false);assert.equal(items[1].titleMatch,true);
 assert.equal(items[2].titleMatch,false);
});
test('progress retains completed episodes during chunk events and never invents an HLS total size',()=>{
 const events=[];const report=createProgressReporter(e=>events.push(e));
 report({stage:'start',total:3,completed:0});
 report({stage:'downloading',episode:1,total:3});
 report({stage:'episode_progress',episode:1,bytes:40,percentage:50,downloadedSegments:2,totalSegments:4});
 assert.equal(events.at(-1).currentEpisodeTotalBytes,null);
 report({stage:'episode_done',episode:1,total:3,completed:1,bytes:80});
 report({stage:'downloading',episode:2,total:3});
 report({stage:'episode_progress',episode:2,bytes:20,totalBytes:100,percentage:20});
 const progress=events.at(-1);
 assert.equal(progress.episodesCompleted,1);assert.equal(progress.episodesTotal,3);
 assert.equal(progress.downloadedBytes,100);assert.equal(progress.currentEpisodeBytes,20);
 assert.equal(progress.currentEpisodeTotalBytes,100);
 report({stage:'episode_error',episode:2,total:3});assert.equal(events.at(-1).episodesFailed,1);
});
test('maps a base language to the sole published region without inventing an ambiguous edition',()=>{
 const {resolveLanguage}=require('../src/discovery');
 assert.equal(resolveLanguage('es',[{code:'es-MX'}]),'es-MX');
 assert.equal(resolveLanguage('en',[{code:'en-US'}]),'en-US');
 assert.equal(resolveLanguage('zhHans',[{code:'zhHans'}]),'zhHans');
 assert.equal(resolveLanguage('es-ES',[{code:'es-MX'}]),null);
 assert.equal(resolveLanguage('zh',[{code:'zh-CN'},{code:'zh-TW'}]),null);
});

test('ready main content wins over stalled frame navigation', async () => {
  const {navigateDiscovery} = require('../src/discovery');
  const url = new URL('https://reelshort.dramafren.org/index.php?page=home&lang=es');
  let listener;
  const frame = {};
  const response = {
    request: () => ({isNavigationRequest: () => true}),
    frame: () => frame,
    headers: () => ({}),
    url: () => url.href,
    ok: () => true,
  };
  const page = {
    mainFrame: () => frame,
    on: (_, callback) => { listener = callback; },
    off: () => { listener = undefined; },
    goto: () => { listener(response); return new Promise(() => {}); },
    waitForFunction: async (_check, options, target) => {
      assert.equal(options.timeout, 90000);
      assert.deepEqual(target, {href:url.href, selector:'input[type="search"]'});
      return {dispose:async () => {}};
    },
  };
  assert.equal(await navigateDiscovery(page, url, 'input[type="search"]'), response);
  assert.equal(listener, undefined);
  const timeout = Object.assign(new Error('Navigation timeout'), {name:'TimeoutError'});
  response.ok = () => false;
  response.headers = () => ({'cf-mitigated':'challenge'});
  page.goto = async () => { listener(response); throw timeout; };
  await assert.rejects(navigateDiscovery(page, url, 'input[type="search"]'), /access challenge/);
});
