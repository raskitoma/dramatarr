'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { connect } = require('puppeteer-real-browser');

const { assertSafeUrl, DEFAULT_USER_AGENT } = require('./net');

const MEDIA_URL_PATTERN = /\.(m3u8|mp4|ts)(\?|#|$)/i;

// Episodes are resolved in small batches driven from Node. One giant
// page.evaluate would trip puppeteer's protocol timeout the moment a single
// request stalls, and these origins do stall (Cloudflare 524).
const EPISODE_BATCH_SIZE = 6;
const EPISODE_FETCH_TIMEOUT_MS = 45000;

/**
 * Chromium-based browsers we can drive, in order of preference. Real Chrome
 * first (puppeteer-real-browser's Turnstile handling is tuned for it), then the
 * other Chromium builds, which work but may be less reliable against the
 * challenge.
 * @returns {string[]}
 */
function getBrowserCandidates() {
  const home = os.homedir();

  if (process.platform === 'darwin') {
    return [
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      `${home}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`,
      '/Applications/Chromium.app/Contents/MacOS/Chromium',
      '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
      `${home}/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge`,
      '/Applications/Vivaldi.app/Contents/MacOS/Vivaldi'
    ];
  }

  if (process.platform === 'linux') {
    return [
      '/usr/bin/google-chrome-stable',
      '/usr/bin/google-chrome',
      '/usr/bin/chromium',
      '/usr/bin/chromium-browser',
      '/usr/bin/brave-browser',
      '/usr/bin/microsoft-edge-stable',
      '/usr/bin/microsoft-edge',
      '/snap/bin/chromium'
    ];
  }

  if (process.platform === 'win32') {
    const programFiles = process.env['PROGRAMFILES'] || 'C:\\Program Files';
    const programFilesX86 = process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)';
    return [
      `${programFiles}\\Google\\Chrome\\Application\\chrome.exe`,
      `${programFilesX86}\\Google\\Chrome\\Application\\chrome.exe`,
      `${programFiles}\\Microsoft\\Edge\\Application\\msedge.exe`,
      `${programFilesX86}\\Microsoft\\Edge\\Application\\msedge.exe`
    ];
  }

  return [];
}

/**
 * Resolves the Chrome / Chromium executable path across macOS, Linux and Windows.
 * @returns {string|undefined}
 */
function getChromeExecutablePath() {
  if (process.env.PUPPETEER_EXECUTABLE_PATH) return process.env.PUPPETEER_EXECUTABLE_PATH;
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;

  return getBrowserCandidates().find(candidate => fs.existsSync(candidate));
}

/**
 * Chrome refuses to reuse a profile that still carries the singleton lock of a
 * previous run. The lock is a symlink naming the host that held it, so when a
 * container is killed uncleanly it never clears itself and every later launch
 * fails. Clearing it on startup is safe: we only ever run one browser at a time.
 * @param {string} userDataDir
 */
function clearProfileLocks(userDataDir) {
  for (const name of ['SingletonLock', 'SingletonSocket', 'SingletonCookie']) {
    try {
      fs.rmSync(path.join(userDataDir, name), { force: true, recursive: true });
    } catch (err) {
      /* nothing to clear */
    }
  }
}

/**
 * Builds a helpful error for a failed browser launch.
 * @param {Error} err
 * @param {string|undefined} chromePath
 * @returns {Error}
 */
function launchError(err, chromePath) {
  if (chromePath) {
    return new Error(`Could not launch the browser at "${chromePath}": ${err.message}`);
  }
  return new Error(
    'No Chromium-based browser found. Install Google Chrome, or point ' +
    'PUPPETEER_EXECUTABLE_PATH at an existing one. Searched:\n  ' +
    getBrowserCandidates().join('\n  ')
  );
}

/**
 * Captures the identity of the solved browser session so downloads can reuse it.
 * Segment CDNs commonly reject requests without a matching Referer or cookies,
 * which is why this has to travel with the URIs.
 * @param {import('puppeteer-core').Page} page
 * @param {import('puppeteer-core').Browser} browser
 * @returns {Promise<{userAgent: string, referer: string, origin: string, cookies: Array}>}
 */
async function captureRequestContext(page, browser) {
  let cookies = [];
  try {
    // browser.cookies() also returns cookies set for the CDN hosts, which
    // page.cookies() scopes away.
    cookies = typeof browser.cookies === 'function' ? await browser.cookies() : await page.cookies();
  } catch (err) {
    try {
      cookies = await page.cookies();
    } catch (innerErr) {
      cookies = [];
    }
  }

  const userAgent = await page.evaluate(() => navigator.userAgent).catch(() => DEFAULT_USER_AGENT);
  const acceptLanguage = await page.evaluate(() => navigator.language).catch(() => 'en-US');
  const referer = page.url();

  let origin = '';
  try {
    origin = new URL(referer).origin;
  } catch (err) {
    /* keep empty */
  }

  return {
    userAgent: userAgent || DEFAULT_USER_AGENT,
    acceptLanguage: acceptLanguage ? `${acceptLanguage},en;q=0.9` : 'en-US,en;q=0.9',
    referer,
    origin,
    cookies: cookies.map(cookie => ({
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain,
      path: cookie.path,
      secure: cookie.secure
    }))
  };
}

/**
 * Records every media URL the player loads, installed before any page script
 * runs and re-installed on each navigation.
 *
 * Players built on hls.js feed the video element a `blob:` URL through Media
 * Source Extensions, so the real playlist never appears on the element. Hooking
 * `Hls.prototype.loadSource` captures it regardless of how the page obtained or
 * decoded it — no need to reverse-engineer each site's obfuscation.
 *
 * @param {import('puppeteer-core').Page} page
 */
function installMediaHook(page) {
  return page.evaluateOnNewDocument(() => {
    window.__dramadownMedia = [];

    const record = (url, how) => {
      if (typeof url !== 'string' || !url || url.startsWith('blob:')) return;
      window.__dramadownMedia.push({ url, how });
    };

    // hls.js is assigned to window later; intercept the assignment.
    let hlsRef;
    try {
      Object.defineProperty(window, 'Hls', {
        configurable: true,
        get() {
          return hlsRef;
        },
        set(value) {
          hlsRef = value;
          try {
            const proto = value && value.prototype;
            if (proto && proto.loadSource && !proto.__dramadownHooked) {
              const original = proto.loadSource;
              proto.loadSource = function (url) {
                record(url, 'hls.loadSource');
                return original.apply(this, arguments);
              };
              proto.__dramadownHooked = true;
            }
          } catch (err) {
            /* leave hls.js alone if it resists */
          }
        }
      });
    } catch (err) {
      /* non-fatal */
    }

    // Native playback path.
    try {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
      if (descriptor && descriptor.set) {
        Object.defineProperty(HTMLMediaElement.prototype, 'src', {
          configurable: true,
          enumerable: descriptor.enumerable,
          get: descriptor.get,
          set(value) {
            record(value, 'video.src');
            return descriptor.set.call(this, value);
          }
        });
      }
    } catch (err) {
      /* non-fatal */
    }
  });
}

/**
 * Waits for Cloudflare's interstitial to clear.
 * @param {import('puppeteer-core').Page} page
 * @param {number} timeoutMs
 * @param {function} onProgress
 * @returns {Promise<void>}
 */
async function waitForChallenge(page, timeoutMs, onProgress) {
  const deadline = Date.now() + timeoutMs;
  let announced = false;

  while (Date.now() < deadline) {
    const state = await page
      .evaluate(() => ({
        title: document.title || '',
        hasChallenge: Boolean(
          document.querySelector('#challenge-running, #cf-challenge-running, #challenge-form, iframe[src*="challenges.cloudflare.com"]')
        ),
        blocked: /Access denied|You have been blocked|Error 1015/i.test(document.body?.innerText || ''),
        ready: typeof window.dramaId !== 'undefined' || Array.isArray(window.chapterIds)
      }))
      .catch(() => null);

    if (state) {
      if (state.blocked) {
        throw new Error('The site returned a block page (rate limited or IP banned). Try again later.');
      }

      const inChallenge = /just a moment|attention required|checking your browser/i.test(state.title) || state.hasChallenge;
      if (!inChallenge) return;

      if (!announced) {
        announced = true;
        onProgress({ stage: 'challenge', message: 'Cloudflare challenge detected — waiting for it to solve...' });
      }
    }

    await sleep(1000);
  }

  throw new Error(`Failed to resolve the Cloudflare challenge within ${Math.round(timeoutMs / 1000)}s.`);
}

/**
 * Player chrome that is not the name of any series. These pages very often
 * expose nothing but "GoodShort Player" as their title, which is how downloads
 * ended up in a folder named after the player instead of the drama.
 */
const GENERIC_TITLES = new Set([
  'goodshort player',
  'goodshort',
  'dramabox player',
  'dramabox',
  'shortmax player',
  'shortmax',
  'dramafren',
  'player',
  'video player',
  'video',
  'watch',
  'watch online',
  'watch free short dramas',
  'free short dramas',
  'short drama',
  'short dramas',
  'home',
  'index',
  'drama',
  'drama series',
  'untitled'
]);

/**
 * Removes site branding that gets appended to page titles.
 * @param {string} value
 * @returns {string}
 */
function cleanTitle(value) {
  return String(value || '')
    // "… | ReelShort Player", "… - Dramabox Player" — anything ending in Player.
    .replace(/\s*[-–|]\s*[\w\s]*\bPlayer\s*$/i, '')
    .replace(/\s*[-–|]\s*(Watch\s+)?Free\s+Short\s+Dramas.*$/i, '')
    .replace(/\s*[-–|]\s*Full\s+Episodes(\s+Free)?.*$/i, '')
    .replace(/^(GoodShort|DramaBox|ShortMax|ReelShort|DramaFren)\s*(Player)?\s*[-–|]\s*/i, '')
    .replace(/\s*[-–|]\s*(GoodShort|DramaBox|ShortMax|ReelShort|DramaFren).*$/i, '')
    .replace(/\s*[-–|]\s*(Episode|Ep\.?)\s*\d+\s*$/i, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * True when a candidate is player chrome rather than a series name.
 * @param {string} value
 * @returns {boolean}
 */
function isGenericTitle(value) {
  const normalised = String(value || '').toLowerCase().replace(/\s+/g, ' ').trim();
  if (normalised.length < 3) return true;
  if (/^\d+$/.test(normalised)) return true;
  if (GENERIC_TITLES.has(normalised)) return true;
  // Bare site branding such as "GoodShort" or "DramaBox HD".
  if (/^(goodshort|dramabox|shortmax|dramafren)\b/i.test(normalised) && normalised.split(' ').length <= 3) return true;
  return false;
}

/**
 * Turns a URL slug into a readable series name.
 * @param {string} slug
 * @returns {string}
 */
function titleFromSlug(slug) {
  return String(slug || '')
    .replace(/\.[a-z0-9]{1,5}$/i, '')
    .replace(/[._+]+/g, '-')
    .split('-')
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
    .trim();
}

/**
 * Picks the best available series name.
 *
 * The watch URL's `slug` is the site's own canonical name for the series, so it
 * is trusted ahead of anything the player page renders — those pages routinely
 * report only "GoodShort Player".
 *
 * @param {object} candidates - { override, globalTitle, ogTitle, headingTitle, documentTitle }
 * @param {string|URL} [url] - The watch URL, used for its slug
 * @returns {string}
 */
function resolveDramaTitle(candidates = {}, url) {
  const override = cleanTitle(candidates.override);
  if (override) return override;

  let slugTitle = '';
  try {
    const parsed = url instanceof URL ? url : new URL(String(url));
    const slug = parsed.searchParams.get('slug') || parsed.searchParams.get('name');
    slugTitle = titleFromSlug(slug);
    if (!slugTitle) {
      const lastSegment = parsed.pathname.split('/').filter(Boolean).pop();
      if (lastSegment && !/^index\.[a-z]+$/i.test(lastSegment)) slugTitle = titleFromSlug(lastSegment);
    }
  } catch (err) {
    /* no usable URL */
  }

  const ordered = [
    candidates.globalTitle,
    candidates.ogTitle,
    slugTitle,
    candidates.headingTitle,
    candidates.documentTitle
  ];

  for (const candidate of ordered) {
    const cleaned = cleanTitle(candidate);
    if (cleaned && !isGenericTitle(cleaned)) return cleaned;
  }

  return 'Drama Series';
}

/**
 * Reads the metadata the player keeps in page scope.
 *
 * Older templates (goodshort) assign these as `window` properties. Newer ones
 * (dramabox, reelshort) declare them with `let`/`const`, which land in the
 * global *lexical* environment — reachable by a bare identifier but invisible to
 * `window.x`. Reading only `window` is why those sites looked metadata-less.
 *
 * @param {import('puppeteer-core').Page} page
 * @returns {Promise<object>}
 */
function readPageMetadata(page) {
  return page.evaluate(() => {
    const read = name => {
      if (typeof window[name] !== 'undefined' && window[name] !== null && window[name] !== '') return window[name];
      try {
        // Direct eval resolves against the global lexical environment.
        // eslint-disable-next-line no-eval
        const value = eval(name);
        return value === '' ? undefined : value;
      } catch (err) {
        return undefined;
      }
    };

    const pick = (...names) => {
      for (const name of names) {
        const value = read(name);
        if (value !== undefined && value !== null) return value;
      }
      return undefined;
    };

    const isFn = name => {
      if (typeof window[name] === 'function') return true;
      try {
        // eslint-disable-next-line no-eval
        return typeof eval(name) === 'function';
      } catch (err) {
        return false;
      }
    };

    const asArray = value => (Array.isArray(value) ? value : []);

    const chapterIds = asArray(pick('chapterIds'));
    const episodeNames = asArray(pick('episodeNames'));
    const availableQualities = asArray(pick('availableQualities'));

    return {
      dramaId: pick('dramaId', 'drama_id') ?? null,
      dramaSlug: pick('dramaSlug', 'slug') ?? null,
      currentEp: Number(pick('currentEp')) || 1,
      currentServer: Number(pick('currentSv', 'currentServer', 'sv')) || 1,
      currentLang: pick('currentLang', 'lang') ?? 'en',
      // reelshort calls this `maxEp`; dramabox and goodshort use `totalEp`.
      totalEp: Number(pick('totalEp', 'maxEp', 'totalEpisodes', 'epCount')) || chapterIds.length,
      chapterIds,
      episodeNames,
      // dramabox: a JSON endpoint returning {ok, videoUrl, qualities[]}
      videoEndpointUrl: pick('videoEndpointUrl') ?? null,
      // reelshort: qualities already in scope, URLs base64-encoded in `hash64`
      availableQualities: availableQualities.map(item => ({
        quality: item && item.quality,
        url: item && item.url,
        hash64: item && item.hash64
      })),
      titleCandidates: {
        globalTitle: pick('dramaTitle', 'dramaName', 'seriesTitle', 'bookName') || '',
        ogTitle: document.querySelector('meta[property="og:title"]')?.content || '',
        headingTitle: document.querySelector('h1, .drama-title, .series-title')?.textContent || '',
        documentTitle: document.title || ''
      },
      hasSwitchEpisode: isFn('switchEpisode'),
      hasNextEpisode: isFn('goToNextEp'),
      hasLoadFromEndpoint: isFn('loadVideoFromEndpoint')
    };
  });
}

/**
 * True when the page gave us enough for at least one extraction strategy.
 * @param {object|null} meta
 * @returns {boolean}
 */
function hasUsableMetadata(meta) {
  if (!meta) return false;
  return (
    meta.chapterIds.length > 0 ||
    Boolean(meta.videoEndpointUrl) ||
    meta.availableQualities.length > 0 ||
    (Boolean(meta.dramaId) && meta.totalEp > 0) ||
    meta.hasSwitchEpisode ||
    meta.hasNextEpisode
  );
}

/**
 * Polls until the player has published enough for some strategy to run.
 * Different templates expose different things, so waiting on any single global
 * silently gave up on the others.
 * @param {import('puppeteer-core').Page} page
 * @param {function} abortIfCancelled
 * @returns {Promise<object|null>}
 */
async function settleForMetadata(page, abortIfCancelled) {
  const deadline = Date.now() + 15000;
  let meta = null;

  while (Date.now() < deadline) {
    abortIfCancelled();
    meta = await readPageMetadata(page).catch(() => null);

    if (hasUsableMetadata(meta)) {
      // The quality list lands a beat after the episode count; give it one more
      // read so the better strategy is picked over blind network sniffing.
      if (meta.availableQualities.length === 0 && !meta.videoEndpointUrl && meta.chapterIds.length === 0) {
        await sleep(1500);
        meta = (await readPageMetadata(page).catch(() => null)) || meta;
      }
      return meta;
    }

    await sleep(500);
  }

  return meta;
}

/**
 * Picks the highest-resolution entry from a site's quality list.
 * Labels look like "1080p", "Server 1 720p" or occasionally "0p".
 * @param {Array<{quality?: string, url?: string, hash64?: string}>} qualities
 * @returns {string|null} The chosen URL
 */
function selectBestQuality(qualities) {
  const decoded = (qualities || [])
    .map(item => {
      let url = item.url || null;
      if (!url && item.hash64) {
        try {
          url = Buffer.from(item.hash64, 'base64').toString('utf8');
        } catch (err) {
          url = null;
        }
      }
      const height = Number(String(item.quality || '').match(/(\d{3,4})\s*p/i)?.[1]) || 0;
      return { url, height };
    })
    .filter(item => item.url && /^https?:\/\//i.test(item.url));

  if (decoded.length === 0) return null;

  // Prefer the tallest; among equals prefer a playlist over a bare file, since
  // a master playlist can still carry higher variants.
  decoded.sort((a, b) => b.height - a.height || (/\.m3u8/i.test(b.url) ? 1 : 0) - (/\.m3u8/i.test(a.url) ? 1 : 0));
  return decoded[0].url;
}

/**
 * Primary strategy: ask the site's own endpoint for each chapter's stream URL,
 * from inside the page so the solved session and origin apply automatically.
 * @param {import('puppeteer-core').Page} page
 * @param {object} meta
 * @returns {Promise<Array>}
 */
async function extractViaChapterApi(page, meta, options = {}) {
  const { onProgress = () => {} } = options;
  const total = meta.chapterIds.length;
  const results = [];

  for (let start = 0; start < total; start += EPISODE_BATCH_SIZE) {
    const indexes = [];
    for (let i = start; i < Math.min(start + EPISODE_BATCH_SIZE, total); i++) indexes.push(i);

    const batch = await page.evaluate(
      async (metadata, wanted, timeoutMs) => {
        const { dramaId, currentServer, currentLang, chapterIds, episodeNames } = metadata;

        const fetchChapter = async index => {
          const chapterId = chapterIds[index];
          const name = episodeNames[index] || `Episode ${index + 1}`;
          const endpoint =
            `index.php?action=get_video_url&id=${encodeURIComponent(dramaId)}` +
            `&chap_id=${encodeURIComponent(chapterId)}` +
            `&sv=${encodeURIComponent(currentServer)}&lang=${encodeURIComponent(currentLang)}`;

          let lastError = 'Video not available';

          for (let attempt = 0; attempt < 3; attempt++) {
            try {
              const response = await fetch(endpoint, {
                credentials: 'include',
                headers: { Accept: 'application/json, */*' },
                signal: AbortSignal.timeout(timeoutMs)
              });
              if (!response.ok) throw new Error(`HTTP ${response.status}`);

              const data = await response.json();
              if (data && data.status === 'success' && data.video_url) {
                return {
                  episode: index + 1,
                  name,
                  chapterId,
                  uri: new URL(data.video_url, location.href).href,
                  qualities: Array.isArray(data.qualities) ? data.qualities : []
                };
              }
              lastError = (data && data.message) || 'Video not available';
            } catch (err) {
              lastError = err.name === 'TimeoutError' ? 'Timed out asking the site for this episode.' : err.message;
              await new Promise(resolve => setTimeout(resolve, 400 * (attempt + 1)));
            }
          }

          return { episode: index + 1, name, chapterId, uri: null, error: lastError };
        };

        return Promise.all(wanted.map(fetchChapter));
      },
      meta,
      indexes,
      EPISODE_FETCH_TIMEOUT_MS
    );

    results.push(...batch);
    onProgress({
      stage: 'episode_resolved',
      message: `Resolved ${results.length}/${total} episode sources...`,
      done: results.length,
      total
    });
  }

  return results;
}

/**
 * dramabox strategy: the player asks `index.php?action=get_video&ep=N` for a
 * JSON payload containing the (often direct-MP4) URL plus a quality list. The
 * episode is a plain number, so the whole series is reachable without navigating.
 *
 * @param {import('puppeteer-core').Page} page
 * @param {object} meta
 * @param {{limit: number}} options
 * @returns {Promise<Array>}
 */
async function extractViaVideoEndpoint(page, meta, options) {
  const { limit, onProgress = () => {} } = options;
  const count = Math.max(1, Math.min(meta.totalEp || 1, limit));
  const results = [];

  // Batched from Node rather than one long page.evaluate: these origins
  // intermittently hang (Cloudflare 524), and a single stalled request would
  // otherwise blow puppeteer's protocol timeout and abort the whole run.
  for (let start = 0; start < count; start += EPISODE_BATCH_SIZE) {
    const episodes = [];
    for (let i = start; i < Math.min(start + EPISODE_BATCH_SIZE, count); i++) episodes.push(i + 1);

    const batch = await page.evaluate(
      async (metadata, wanted, timeoutMs) => {
        const { dramaId, currentLang, currentServer, videoEndpointUrl } = metadata;

        // Derive each episode's endpoint from the one the page already holds so
        // any extra parameters it carries are preserved.
        const buildEndpoint = episode => {
          if (videoEndpointUrl && /[?&]ep=\d+/.test(videoEndpointUrl)) {
            return videoEndpointUrl.replace(/([?&]ep=)\d+/, `$1${episode}`);
          }
          return `index.php?action=get_video&id=${encodeURIComponent(dramaId)}&ep=${episode}` +
            `&lang=${encodeURIComponent(currentLang)}&sv=${encodeURIComponent(currentServer)}`;
        };

        // Every URL the payload offers, tallest first. The extras matter: these
        // links expire, and a dead one on a given server/quality is often still
        // live on another.
        const rankUrls = payload => {
          const scored = (Array.isArray(payload.qualities) ? payload.qualities : [])
            .map(q => ({
              url: q && q.url,
              height: Number(String((q && q.quality) || '').match(/(\d{3,4})\s*p/i)?.[1]) || 0
            }))
            .filter(q => q.url);
          scored.sort((a, b) => b.height - a.height);

          const urls = scored.map(q => q.url);
          if (payload.videoUrl && !urls.includes(payload.videoUrl)) urls.push(payload.videoUrl);
          return [...new Set(urls)];
        };

        const fetchEpisode = async episode => {
          let lastError = 'Video unavailable';

          for (let attempt = 0; attempt < 3; attempt++) {
            try {
              const response = await fetch(buildEndpoint(episode), {
                credentials: 'same-origin',
                headers: { Accept: 'application/json' },
                signal: AbortSignal.timeout(timeoutMs)
              });
              if (!response.ok) throw new Error(`HTTP ${response.status}`);

              const payload = await response.json();
              if (payload && payload.ok !== false) {
                const ranked = rankUrls(payload).map(u => new URL(u, location.href).href);
                if (ranked.length > 0) {
                  return {
                    episode,
                    name: `Episode ${episode}`,
                    uri: ranked[0],
                    alternates: ranked.slice(1),
                    // Absolute endpoint for this episode. The downloader varies
                    // its sv= to ask the other servers for fresh links when the
                    // ones above have expired.
                    refreshEndpoint: new URL(buildEndpoint(episode), location.href).href,
                    qualities: (payload.qualities || []).map(q => q && q.quality).filter(Boolean)
                  };
                }
              }
              lastError = (payload && payload.error) || 'Video unavailable';
            } catch (err) {
              lastError = err.name === 'TimeoutError' ? 'Timed out asking the site for this episode.' : err.message;
              await new Promise(resolve => setTimeout(resolve, 400 * (attempt + 1)));
            }
          }

          return { episode, name: `Episode ${episode}`, uri: null, error: lastError };
        };

        // Two at a time. This origin is slow and answers a burst of parallel
        // requests with Cloudflare 524s; the site's own player asks for one.
        const out = new Array(wanted.length);
        let cursor = 0;
        const worker = async () => {
          while (cursor < wanted.length) {
            const i = cursor++;
            out[i] = await fetchEpisode(wanted[i]);
          }
        };
        await Promise.all([worker(), worker()]);
        return out;
      },
      meta,
      episodes,
      EPISODE_FETCH_TIMEOUT_MS
    );

    results.push(...batch);
    onProgress({
      stage: 'episode_resolved',
      message: `Resolved ${results.length}/${count} episode sources...`,
      done: results.length,
      total: count
    });
  }

  return results;
}

/**
 * reelshort strategy: the page holds `availableQualities` for the *current*
 * episode with base64-encoded playlist URLs and offers no JSON endpoint, so each
 * episode is reached by navigating to `ep=N` and re-reading the bindings.
 *
 * @param {import('puppeteer-core').Page} page
 * @param {object} meta
 * @param {{limit: number, onProgress: function, watchUrl: URL, challengeTimeoutMs: number}} options
 * @returns {Promise<Array>}
 */
async function extractViaInPageQualities(page, meta, options) {
  const { limit, onProgress, watchUrl, challengeTimeoutMs } = options;
  const total = Math.max(1, Math.min(meta.totalEp || 1, limit || meta.totalEp || 1));
  const results = [];

  for (let episode = 1; episode <= total; episode++) {
    if (episode !== meta.currentEp) {
      const next = new URL(watchUrl.href);
      next.searchParams.set('ep', String(episode));
      await page.goto(next.href, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
      await waitForChallenge(page, challengeTimeoutMs, () => {}).catch(() => {});
    }

    // The qualities are populated by the page's own scripts; give them a moment.
    let uri = null;
    for (let attempt = 0; attempt < 12 && !uri; attempt++) {
      const fresh = await readPageMetadata(page).catch(() => null);
      uri = fresh ? selectBestQuality(fresh.availableQualities) : null;
      if (!uri) {
        // Anything the player already loaded is just as good a source.
        const sniffed = await page.evaluate(() => (window.__dramadownMedia || []).map(m => m.url)).catch(() => []);
        uri = sniffed.find(u => /\.m3u8(\?|#|$)/i.test(u)) || sniffed[sniffed.length - 1] || null;
      }
      if (!uri) await sleep(700);
    }

    results.push(
      uri
        ? { episode, name: `Episode ${episode}`, uri }
        : { episode, name: `Episode ${episode}`, uri: null, error: 'No playable source found for this episode.' }
    );

    onProgress({
      stage: 'episode_resolved',
      message: `Resolved ${episode}/${total} episode sources...`,
      done: episode,
      total
    });
  }

  return results;
}

/**
 * Fallback strategy: drive the player's own navigation and watch the network for
 * the media request each episode triggers.
 * @param {import('puppeteer-core').Page} page
 * @param {object} meta
 * @param {{onProgress: function, stepTimeoutMs: number}} options
 * @returns {Promise<Array>}
 */
async function extractViaPlayerNavigation(page, meta, options) {
  const { onProgress, stepTimeoutMs, limit = Infinity } = options;
  const total = Math.max(1, Math.min(Number(meta.totalEp) || 1, limit));
  const results = [];
  const seen = new Set();

  const sniffed = [];
  const onResponse = response => {
    const url = response.url();
    if (MEDIA_URL_PATTERN.test(url)) sniffed.push(url);
  };
  page.on('response', onResponse);

  try {
    for (let index = 0; index < total; index++) {
      const before = sniffed.length;
      const previousSrc = await currentVideoSrc(page);

      if (index > 0 || meta.hasSwitchEpisode) {
        await page
          .evaluate(
            async (targetIndex, useSwitch) => {
              if (useSwitch && typeof window.switchEpisode === 'function') {
                await window.switchEpisode(targetIndex);
              } else if (typeof window.goToNextEp === 'function') {
                await window.goToNextEp();
              }
            },
            index,
            meta.hasSwitchEpisode
          )
          .catch(() => {});
      }

      const uri = await waitForEpisodeSource(page, { sniffed, before, previousSrc, timeoutMs: stepTimeoutMs });

      if (uri && !seen.has(uri)) {
        seen.add(uri);
        results.push({ episode: index + 1, name: `Episode ${index + 1}`, uri });
      } else {
        results.push({
          episode: index + 1,
          name: `Episode ${index + 1}`,
          uri: null,
          error: uri ? 'Player returned a duplicate source.' : 'Timed out waiting for the player to load this episode.'
        });
      }

      onProgress({
        stage: 'episode_resolved',
        message: `Resolved episode ${index + 1}/${total}${uri ? '' : ' (no source found)'}`,
        done: index + 1,
        total
      });
    }
  } finally {
    page.off('response', onResponse);
  }

  return results;
}

/**
 * @param {import('puppeteer-core').Page} page
 * @returns {Promise<string|null>}
 */
function currentVideoSrc(page) {
  return page
    .evaluate(() => {
      const video = document.querySelector('video');
      return video?.currentSrc || video?.src || null;
    })
    .catch(() => null);
}

/**
 * Waits until the player exposes a new source, either on the <video> element or
 * as a fresh media request on the wire.
 * @param {import('puppeteer-core').Page} page
 * @param {{sniffed: string[], before: number, previousSrc: string|null, timeoutMs: number}} options
 * @returns {Promise<string|null>}
 */
async function waitForEpisodeSource(page, options) {
  const { sniffed, before, previousSrc, timeoutMs } = options;
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    if (sniffed.length > before) {
      const fresh = sniffed.slice(before).filter(url => url !== previousSrc);
      const playlist = fresh.find(url => /\.m3u8(\?|#|$)/i.test(url));
      if (playlist) return playlist;
      if (fresh.length > 0) return fresh[fresh.length - 1];
    }

    // The in-page hook sees sources that hls.js only ever passes to MSE.
    const hooked = await page.evaluate(() => (window.__dramadownMedia || []).map(m => m.url)).catch(() => []);
    const hookedFresh = hooked.filter(url => url !== previousSrc);
    const hookedPlaylist = hookedFresh.find(url => /\.m3u8(\?|#|$)/i.test(url));
    if (hookedPlaylist) return hookedPlaylist;

    const src = await currentVideoSrc(page);
    if (src && src !== previousSrc && !src.startsWith('blob:')) return src;

    await sleep(400);
  }

  return null;
}

/**
 * @param {number} ms
 * @returns {Promise<void>}
 */
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Extracts all video URIs from a given dramafren / goodshort watch URL.
 * @param {string} url - The initial watch URL
 * @param {object} [options] - { onProgress, headless, signal, challengeTimeoutMs, navigationTimeoutMs }
 * @returns {Promise<{dramaTitle: string, totalEpisodes: number, episodes: Array, uris: string[], requestContext: object}>}
 */
async function extractDramaVideoUris(url, options = {}) {
  const {
    onProgress = () => {},
    headless = process.env.HEADLESS === '1',
    signal,
    challengeTimeoutMs = Number(process.env.CHALLENGE_TIMEOUT_MS) || 60000,
    navigationTimeoutMs = Number(process.env.NAVIGATION_TIMEOUT_MS) || 60000,
    stepTimeoutMs = 15000,
    maxEpisodes = Number(process.env.MAX_EPISODES) || 0,
    title,
    userDataDir = process.env.CHROME_USER_DATA_DIR || undefined,
    challengeAttempts = Number(process.env.CHALLENGE_ATTEMPTS) || 3
  } = options;

  const target = assertSafeUrl(url);

  onProgress({ stage: 'browser_start', message: 'Launching browser and resolving Cloudflare security...' });

  const chromePath = getChromeExecutablePath();

  // On Linux, puppeteer-real-browser starts its own Xvfb unless a display
  // already exists. That is how the container runs "headless": a real headful
  // Chrome on a virtual display, which Turnstile accepts. True --headless is
  // opt-in because Cloudflare fails it far more often.
  const hasDisplay = Boolean(process.env.DISPLAY);
  const manageXvfb = process.platform === 'linux' && !hasDisplay;

  const connectOptions = {
    // The library maps anything truthy to `--headless=<value>`; 'new' is the
    // only value modern Chrome understands. ('auto' is silently coerced to
    // false upstream, so it would never have taken effect.)
    headless: headless ? 'new' : false,
    turnstile: true,
    disableXvfb: !manageXvfb,
    // chrome-launcher's defaults plus the library's own anti-automation flags
    // already cover --no-sandbox, --disable-dev-shm-usage and
    // disable-features=AutomationControlled, so only add what they miss.
    args: [
      '--disable-blink-features=AutomationControlled',
      '--window-size=1920,1080',
      '--mute-audio',
      ...(process.platform === 'linux' ? ['--disable-gpu'] : [])
    ],
    // customConfig is spread straight into chrome-launcher — this is the only
    // place a browser path or profile directory is actually honoured.
    customConfig: {}
  };

  if (chromePath) {
    connectOptions.customConfig.chromePath = chromePath;

    // Resolve symlinks so the container's /usr/local/bin/dramadown-browser
    // reports as "chromium" or "google-chrome-stable" rather than the alias.
    let resolvedPath = chromePath;
    try {
      resolvedPath = fs.realpathSync(chromePath);
    } catch (err) {
      /* keep the original */
    }
    const browserName = path.basename(resolvedPath).replace(/\.exe$/i, '');
    if (!/^(google[ -])?chrome([ -]stable)?$/i.test(browserName)) {
      onProgress({
        stage: 'browser_choice',
        message:
          `Google Chrome not found — using ${browserName} instead. ` +
          'It should work, but Chrome passes Cloudflare challenges more reliably.'
      });
    }
  }

  // A persistent profile keeps the cf_clearance cookie between runs, so most
  // extractions skip the challenge entirely.
  if (userDataDir) {
    fs.mkdirSync(userDataDir, { recursive: true });
    clearProfileLocks(userDataDir);
    connectOptions.customConfig.userDataDir = userDataDir;
  }

  if (headless) {
    onProgress({
      stage: 'headless_warning',
      message: 'Running Chrome in --headless=new. Cloudflare challenges fail more often this way; prefer Xvfb.'
    });
  }

  let browser;
  let page;

  try {
    ({ page, browser } = await connect(connectOptions));
  } catch (err) {
    // A corrupt or locked profile must never permanently break the service, so
    // fall back to a throwaway one. The only cost is re-solving the challenge.
    if (!connectOptions.customConfig.userDataDir) throw launchError(err, chromePath);

    onProgress({
      stage: 'profile_reset',
      message: `Chrome could not use the saved profile (${err.message}). Retrying with a fresh one...`
    });

    delete connectOptions.customConfig.userDataDir;
    try {
      ({ page, browser } = await connect(connectOptions));
    } catch (retryErr) {
      throw launchError(retryErr, chromePath);
    }
  }

  const abortIfCancelled = () => {
    if (signal?.aborted) throw new Error('Cancelled.');
  };

  try {
    page.setDefaultNavigationTimeout(navigationTimeoutMs);
    if (options.discovery) {
      return await require('./discovery').searchWithPage(page, { ...options.discovery, signal, onProgress }, waitForChallenge);
    }
    await installMediaHook(page);

    // Navigate, clear the challenge and read the player's metadata as one
    // retryable unit. These origins intermittently answer with a Cloudflare 5xx
    // (524 "A timeout occurred"), which clears the challenge check but carries
    // no player data — so a metadata miss has to re-navigate, not give up.
    let meta = null;
    let navigationError = null;

    for (let attempt = 1; attempt <= challengeAttempts; attempt++) {
      abortIfCancelled();
      onProgress({
        stage: 'navigating',
        message:
          attempt === 1
            ? `Navigating to: ${target.href}`
            : `Retrying navigation (attempt ${attempt}/${challengeAttempts})...`
      });

      try {
        const response = await page.goto(target.href, { waitUntil: 'domcontentloaded', timeout: navigationTimeoutMs });
        abortIfCancelled();

        const status = typeof response?.status === 'function' ? response.status() : 0;
        if (status >= 500) {
          throw new Error(`The site returned HTTP ${status} (origin error, not a block).`);
        }

        await waitForChallenge(page, challengeTimeoutMs, onProgress);
        abortIfCancelled();

        onProgress({ stage: 'loading_metadata', message: 'Page challenge solved. Extracting episode playlist metadata...' });

        meta = await settleForMetadata(page, abortIfCancelled);
        if (hasUsableMetadata(meta)) {
          navigationError = null;
          break;
        }

        throw new Error('The page loaded but published no episode information.');
      } catch (err) {
        if (signal?.aborted) throw err;
        // A block page means we are rate limited; hammering it makes it worse.
        if (/block page/i.test(err.message)) throw err;

        navigationError = err;
        meta = null;
        if (attempt < challengeAttempts) {
          onProgress({ stage: 'challenge_retry', message: `${err.message} Retrying...` });
          await sleep(3000 * attempt);
        }
      }
    }

    if (!meta) {
      meta = await readPageMetadata(page).catch(() => null);
      if (!hasUsableMetadata(meta)) {
        throw navigationError || new Error(
          'Could not find an episode list on this page. It may not be a supported watch URL, ' +
          'or the site changed its player.'
        );
      }
    }

    abortIfCancelled();

    meta.title = resolveDramaTitle({ ...meta.titleCandidates, override: title }, target);

    const episodeCount = meta.chapterIds.length || meta.totalEp || 0;
    onProgress({
      stage: 'meta_ready',
      message: `Found Drama: "${meta.title}" with ${episodeCount || 'an unknown number of'} episodes.`,
      dramaTitle: meta.title,
      totalEpisodes: episodeCount
    });

    // These player templates differ enough that one strategy cannot serve them
    // all; pick by what the page actually exposes.
    const limit = maxEpisodes > 0 ? maxEpisodes : Infinity;
    let episodes;
    let strategy;

    if (meta.chapterIds.length > 0 && meta.dramaId) {
      strategy = 'chapter-api';
      episodes = await extractViaChapterApi(page, meta, { onProgress });
      if (Number.isFinite(limit)) episodes = episodes.slice(0, limit);
    } else if (meta.dramaId && (meta.videoEndpointUrl || meta.hasLoadFromEndpoint) && meta.totalEp > 0) {
      strategy = 'video-endpoint';
      onProgress({ stage: 'strategy', message: `Using the player's get_video endpoint for ${meta.totalEp} episodes.` });
      episodes = await extractViaVideoEndpoint(page, meta, { limit, onProgress });
    } else if (meta.availableQualities.length > 0 || (meta.totalEp > 0 && meta.hasNextEpisode)) {
      strategy = 'in-page-qualities';
      onProgress({
        stage: 'strategy',
        message: `No episode endpoint — reading the player's encoded quality list across ${meta.totalEp || 1} episodes.`
      });
      episodes = await extractViaInPageQualities(page, meta, {
        limit,
        onProgress,
        watchUrl: target,
        challengeTimeoutMs
      });
    } else if (meta.hasSwitchEpisode || meta.hasNextEpisode) {
      strategy = 'player-navigation';
      onProgress({
        stage: 'fallback',
        message: 'No chapter list found — falling back to player navigation and network sniffing.'
      });
      episodes = await extractViaPlayerNavigation(page, meta, { onProgress, stepTimeoutMs, limit });
    } else {
      throw new Error(
        'Could not find an episode list on this page. It may not be a supported watch URL, ' +
        'or the site changed its player.'
      );
    }

    // A strategy can pick the right shape and still come back empty when the
    // origin misbehaves. Rather than fail the run, hand off to the sniffing
    // path, which only needs the player to actually play something.
    if (!episodes.some(item => item && item.uri) && strategy !== 'player-navigation' && (meta.hasNextEpisode || meta.hasSwitchEpisode)) {
      const firstError = episodes.find(item => item && item.error)?.error;
      onProgress({
        stage: 'fallback',
        message: `The ${strategy} strategy returned nothing${firstError ? ` (${firstError})` : ''} — falling back to network sniffing.`
      });

      // Reload so the player runs from a clean state with the hook in place.
      await page.goto(target.href, { waitUntil: 'domcontentloaded', timeout: navigationTimeoutMs }).catch(() => {});
      await waitForChallenge(page, challengeTimeoutMs, () => {}).catch(() => {});

      const sniffed = await extractViaPlayerNavigation(page, meta, { onProgress, stepTimeoutMs, limit });
      if (sniffed.some(item => item && item.uri)) {
        strategy = 'player-navigation';
        episodes = sniffed;
      }
    }

    abortIfCancelled();

    const requestContext = await captureRequestContext(page, browser);
    const usable = episodes.filter(episode => episode && episode.uri);

    if (usable.length === 0) {
      const firstError = episodes.find(episode => episode && episode.error)?.error;
      throw new Error(`No playable video URIs were found${firstError ? ` (${firstError})` : ''}.`);
    }

    onProgress({
      stage: 'completed',
      message: `Successfully extracted ${usable.length} video URIs!`
    });

    return {
      // The watch URL this came from, recorded alongside the downloads so a
      // series can be traced back (and re-extracted) later.
      sourceUrl: target.href,
      strategy,
      dramaTitle: meta.title,
      totalEpisodes: episodes.length,
      episodes,
      uris: usable.map(episode => episode.uri),
      requestContext
    };
  } finally {
    await browser.close().catch(() => {});
  }
}

module.exports = {
  extractDramaVideoUris,
  getBrowserCandidates,
  getChromeExecutablePath,
  cleanTitle,
  isGenericTitle,
  resolveDramaTitle,
  selectBestQuality,
  titleFromSlug
};
