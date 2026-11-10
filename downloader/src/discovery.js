'use strict';

// These are provider namespaces published by Dramafren, not arbitrary user-supplied hosts.
const PROVIDERS = ['reelshort', 'dramabox', 'goodshort', 'dramawave', 'netshort', 'shortmax', 'flickreels', 'radreel', 'stardusttv', 'idrama', 'flextv', 'starshort', 'kalostv', 'dramabite', 'shotshort', 'dramapops', 'microdrama', 'shortwave', 'moboreels', 'reelfren'];
const HOSTS = new Set(['dramafren.org', 'www.dramafren.org', ...PROVIDERS.map(p => `${p}.dramafren.org`)]);
function validateWatchUrl(raw) {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || !HOSTS.has(url.hostname) || url.username || url.password || (url.port && url.port !== '443')) {
    throw new Error('Use an HTTPS Dramafren provider URL.');
  }
  return url;
}
function sourceProvider(raw) {
  try {
    const host = new URL(raw).hostname;
    if (host === 'mydramawave.com' || host.endsWith('.mydramawave.com')) return 'dramawave';
    return PROVIDERS.find(p => host === `${p}.dramafren.org` || host === `${p}.com` || host.endsWith(`.${p}.com`)) || null;
  } catch { return null; }
}
function resolveLanguage(requested, published) {
  const exact=published.find(p=>p.code.toLowerCase()===requested.toLowerCase());
  if (exact) return exact.code;
  if (!/^[a-z]{2,3}$/.test(requested)) return null;
  const variants=published.filter(p=>p.code.toLowerCase().split('-')[0]===requested.toLowerCase());
  return variants.length===1 ? variants[0].code : null;
}
function normalizeTitle(raw) {
  return String(raw || '').normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase().replace(/[^\p{L}\p{N}_]+/gu, ' ').trim();
}
function classifyCandidates(rows, query, sourceUrl) {
  const unique = new Map();
  let sourceId = null;
  try { const u = new URL(sourceUrl); sourceId = u.searchParams.get('id') || u.pathname.split('/').filter(Boolean).pop(); } catch {}
  const provider = sourceProvider(sourceUrl);
  for (const row of rows) {
    let url;
    try { url = validateWatchUrl(row.url); } catch { continue; }
    if (url.searchParams.get('page') !== 'watch' && !/\/watch(?:\/|$)/.test(url.pathname)) continue;
    const externalId = url.searchParams.get('id') || null;
    const item = { title: String(row.title || '').trim().slice(0,500), url: url.href,
      provider: sourceProvider(url.href), language: url.searchParams.get('lang') || row.language || 'und',
      externalId, episodes: Number(row.episodes) || null,
      idMatch: Boolean(sourceId && externalId === sourceId && provider && provider === sourceProvider(url.href)),
      titleMatch: Boolean(normalizeTitle(query) && normalizeTitle(row.title) === normalizeTitle(query)) };
    if (item.title) unique.set(item.url, item);
  }
  return [...unique.values()].sort((a,b) => Number(b.idMatch)-Number(a.idMatch) || Number(b.titleMatch)-Number(a.titleMatch));
}

const DISCOVERY_TIMEOUT = 90_000;
const RESULT_LINK = 'a[href*="page=detail"],a[href*="page=watch"],a[href*="/watch/"]';

async function navigateDiscovery(page, url, readySelector) {
  let response;
  const readiness = new AbortController();
  const remember = candidate => {
    if (candidate.request().isNavigationRequest() && candidate.frame() === page.mainFrame()) {
      response = candidate;
    }
  };
  page.on('response', remember);
  try {
    const navigation = page.goto(url.href, {waitUntil:'domcontentloaded', timeout:DISCOVERY_TIMEOUT});
    // Child frames can stall the lifecycle. Only accept early readiness after the requested
    // main document has received a successful response and contains the expected content.
    const content = page.waitForFunction(({href, selector}) =>
      location.href === href && Boolean(document.querySelector(selector)),
    {timeout:DISCOVERY_TIMEOUT, signal:readiness.signal}, {href:url.href, selector:readySelector})
      .then(handle => {
        void handle.dispose();
        if (response?.ok() && response.url() === url.href) return response;
        return navigation;
      }, () => navigation);
    return await Promise.race([navigation, content]);
  } catch (error) {
    if (error.name === 'TimeoutError' && response?.headers()['cf-mitigated'] === 'challenge') {
      throw new Error('Provider access challenge did not finish loading before the navigation deadline', {cause:error});
    }
    throw error;
  } finally {
    readiness.abort();
    page.off('response', remember);
  }
}

async function waitForSearchOutcome(page) {
  const handle = await page.waitForFunction(selector => {
    if (document.querySelector(selector)) return 'results';
    const text = document.body?.innerText || '';
    return /no results found|no dramas found|no search results|no se encontraron resultados|nenhum resultado encontrado/i.test(text)
      ? 'empty' : false;
  }, {timeout:DISCOVERY_TIMEOUT}, RESULT_LINK);
  try {
    return await handle.jsonValue();
  } finally {
    await handle.dispose();
  }
}

async function searchWithPage(page, options, waitForChallenge) {
  const { query, sourceUrl, languages = ['es','en'], signal, onProgress = () => {} } = options;
  const provider = sourceProvider(sourceUrl);
  const providers = provider ? [provider] : PROVIDERS;
  const rows = [], errors = [], availableLanguages = {};
  const languageList = [...new Set(languages)].slice(0,5);
  if (!query || String(query).length > 300 || !languageList.length || languageList.some(l => !/^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?$/.test(l))) throw new Error('Invalid search or language.');
  for (const p of providers) {
    for (const lang of languageList) {
      if (signal?.aborted) throw new Error('Cancelled.');
      onProgress({stage:'searching', provider:p, language:lang, message:`Searching ${p}: ${lang}`});
      let step = 'Opening provider home';
      try {
        const home = new URL(`https://${p}.dramafren.org/index.php`);
        home.search = new URLSearchParams({page:'home',lang}).toString();
        const searchSelector = 'input[name="q"],input[name="query"],input[name="search"],input[name="keyword"],input[type="search"],input[placeholder*="Search"],input[placeholder*="search"]';
        const response = await navigateDiscovery(page, home, searchSelector);
        if (response && response.status() >= 500) throw new Error(`HTTP ${response.status()}`);
        step = response?.headers()['cf-mitigated'] === 'challenge' ? 'Provider access challenge' : 'Checking provider access';
        await waitForChallenge(page,90000,onProgress);
        step = 'Reading provider search form';
        await page.waitForSelector(searchSelector,{timeout:DISCOVERY_TIMEOUT});
        // Read the site's real search form. Preserve hidden fields (provider/language/page),
        // use the site's declared query field and reject actions leaving the chosen origin.
        const search = await page.evaluate(() => {
          const input = [...document.querySelectorAll('input')].find(e => /search|query|keyword|\bq\b|\bs\b/i.test(`${e.name} ${e.id} ${e.placeholder}`) && !['hidden','checkbox'].includes(e.type));
          if (!input || !input.form) return null;
          return {action:input.form.action,method:input.form.method,name:input.name,
            fields:[...input.form.elements].filter(e=>e.name && e.type==='hidden').map(e=>[e.name,e.value]),
            languages:[...input.form.querySelectorAll('select[name=lang] option')].map(e=>({code:e.value,label:e.textContent})),
            direct:(() => {const f=[...document.forms].find(f=>f.querySelector('input[name=input_raw]'));return f?{action:f.action,method:f.method}:null;})()};
        });
        if (!search || !search.name || search.method.toLowerCase() !== 'get') throw new Error('Search form is not supported');
        availableLanguages[p]=search.languages;
        const actualLang=search.languages.length ? resolveLanguage(lang,search.languages) : lang;
        if (!actualLang) {errors.push({provider:p,language:lang,error:'Language not published or region is ambiguous'});continue;}
        const url = new URL(search.action,home);
        if (url.origin !== home.origin) throw new Error('Unexpected search origin');
        for (const [k,v] of search.fields) url.searchParams.set(k,v);
        url.searchParams.set(search.name,query);
        url.searchParams.set('lang',actualLang);
        step = 'Loading search results';
        await navigateDiscovery(page, url, RESULT_LINK);
        await waitForChallenge(page,90000,onProgress);
        step = 'Waiting for search results';
        const outcome = await waitForSearchOutcome(page).catch(error => {
          if (error.name !== 'TimeoutError') throw error;
          return 'unconfirmed';
        });
        const found = await page.evaluate(() => [...document.querySelectorAll('a[href]')].filter(a => /page=(?:watch|detail)|\/watch\//.test(a.href)).map(a => ({
          url:a.href, title:a.querySelector('[class*="title"],h2,h3,h4')?.textContent || a.querySelector('img')?.alt || a.title || a.innerText.replace(/\bWatch Now\b/gi,''),
          language:new URL(a.href).searchParams.get('lang') || document.documentElement.lang || 'und'
        })));
        if (search.direct && sourceProvider(sourceUrl) === p) {
          try {
            const direct=new URL(search.direct.action,home);
            if (direct.origin !== home.origin) throw new Error('Unexpected direct-lookup origin');
            if (search.direct.method.toLowerCase() === 'post') {
              await Promise.all([
                page.waitForNavigation({waitUntil:'domcontentloaded',timeout:60000}),
                page.evaluate(({action,sourceUrl,lang})=>{
                  const f=document.createElement('form');f.method='post';f.action=action;
                  for(const [name,value] of Object.entries({input_raw:sourceUrl,lang})) {
                    const i=document.createElement('input');i.name=name;i.value=value;f.appendChild(i);
                  }
                  document.body.appendChild(f);f.submit();
                },{action:direct.href,sourceUrl,lang:actualLang})
              ]);
            } else {
              direct.searchParams.set('input_raw',sourceUrl);direct.searchParams.set('lang',actualLang);
              await page.goto(direct.href,{waitUntil:'domcontentloaded',timeout:60000});
            }
            await waitForChallenge(page,90000,onProgress);
            await page.waitForSelector('a[href*="page=watch"]',{timeout:20000});
            const linked=await page.evaluate(() => ({title:document.querySelector('h1')?.textContent || document.title,
              watch:document.querySelector('a[href*="page=watch"]')?.href,
              episodes:new Set([...document.querySelectorAll('a[href*="page=watch"]')].map(a=>new URL(a.href).searchParams.get('ep'))).size}));
            if (linked.watch) found.unshift({url:linked.watch,title:linked.title,language:actualLang,episodes:linked.episodes});
          } catch (err) {
            if (signal?.aborted) throw err;
            errors.push({provider:p,language:actualLang,error:'Direct language lookup unavailable'});
          }
        }
        if (outcome === 'unconfirmed' && !found.length) {
          errors.push({provider:p,language:actualLang,error:'Search did not publish results or an explicit no-results message before the deadline'});
        }
        const seen=new Set();
        for (const item of found.slice(0,10)) {
          const candidateUrl=validateWatchUrl(item.url);
          if (candidateUrl.origin !== home.origin || seen.has(candidateUrl.href)) continue;
          seen.add(candidateUrl.href);
          if (candidateUrl.searchParams.get('page') === 'detail') {
            if (signal?.aborted) throw new Error('Cancelled.');
            step = 'Opening candidate details';
            await navigateDiscovery(page, candidateUrl, 'a[href*="page=watch"]');
            await waitForChallenge(page,90000,onProgress);
            await page.waitForSelector('a[href*="page=watch"]',{timeout:20000});
            const detail=await page.evaluate(() => {
              const links=[...document.querySelectorAll('a[href*="page=watch"]')];
              return {watch:links[0]?.href,title:document.querySelector('h1')?.textContent,
                episodes:new Set(links.map(a=>new URL(a.href).searchParams.get('ep'))).size || null};
            });
            if (detail.watch) rows.push({...item,url:detail.watch,title:detail.title || item.title,episodes:detail.episodes});
          } else rows.push(item);
        }
      } catch (err) {
        if (signal?.aborted) throw err;
        errors.push({provider:p,language:lang,error:`${step}: ${String(err.message)}`.slice(0,240)});
      }
    }
  }
  return {candidates:classifyCandidates(rows,query,sourceUrl),errors,provider,languages:languageList,availableLanguages,perLanguageLimit:10};
}
module.exports = { PROVIDERS, validateWatchUrl, sourceProvider, classifyCandidates, searchWithPage, resolveLanguage, navigateDiscovery };
