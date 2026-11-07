'use strict';

/**
 * Shared networking helpers.
 *
 * Everything that leaves this process for a media host goes through here so that
 * requests carry the same identity the browser had when it solved the challenge
 * (User-Agent, Referer, cookies). Most CDNs serving these streams reject requests
 * that lack a matching Referer, which is why plain `fetch(uri)` 403s even though
 * extraction succeeded.
 */

const DEFAULT_USER_AGENT =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const RETRYABLE_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

const PRIVATE_IPV4_PATTERNS = [
  /^0\./,
  /^10\./,
  /^127\./,
  /^169\.254\./,
  /^192\.168\./,
  /^172\.(1[6-9]|2[0-9]|3[01])\./
];

/**
 * True when the hostname points at loopback, link-local or RFC1918 space.
 * @param {string} hostname
 * @returns {boolean}
 */
function isPrivateHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '');
  if (!host) return true;

  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  if (host.endsWith('.local') || host.endsWith('.internal') || host.endsWith('.home.arpa')) return true;

  // IPv6 literals: loopback, unspecified, link-local (fe80::/10), ULA (fc00::/7),
  // and IPv4-mapped IPv6 addresses (::ffff:a.b.c.d or ::ffff:xxxx:xxxx).
  if (host.includes(':')) {
    if (host === '::1' || host === '::') return true;
    if (/^fe[89ab]/.test(host)) return true;
    if (/^f[cd]/.test(host)) return true;
    if (host.startsWith('::ffff:')) {
      const tail = host.slice(7);
      if (tail.includes('.')) return PRIVATE_IPV4_PATTERNS.some(re => re.test(tail));
      const match = tail.match(/^([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
      if (match) {
        const hi = parseInt(match[1], 16);
        const lo = parseInt(match[2], 16);
        const ipv4 = `${(hi >> 8) & 0xff}.${hi & 0xff}.${(lo >> 8) & 0xff}.${lo & 0xff}`;
        return PRIVATE_IPV4_PATTERNS.some(re => re.test(ipv4));
      }
    }
    return false;
  }

  return PRIVATE_IPV4_PATTERNS.some(re => re.test(host));
}

/**
 * Parses and validates a URL we are about to fetch on the user's behalf.
 * @param {string} raw
 * @param {{allowPrivate?: boolean}} [options]
 * @returns {URL}
 */
function assertSafeUrl(raw, options = {}) {
  const allowPrivate =
    options.allowPrivate ?? process.env.DRAMADOWN_ALLOW_PRIVATE_HOSTS === '1';

  let parsed;
  try {
    parsed = new URL(String(raw));
  } catch (err) {
    throw new Error(`Not a valid URL: ${String(raw).slice(0, 120)}`);
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(`Refusing to fetch non-HTTP URL (${parsed.protocol}//).`);
  }

  if (!allowPrivate && isPrivateHost(parsed.hostname)) {
    throw new Error(
      `Refusing to fetch private/loopback host "${parsed.hostname}". ` +
      'Set DRAMADOWN_ALLOW_PRIVATE_HOSTS=1 if this is intentional.'
    );
  }

  return parsed;
}

/**
 * Builds a Cookie header containing only the cookies that actually apply to the
 * target URL, mirroring how the browser would scope them.
 * @param {string|URL} targetUrl
 * @param {Array<{name: string, value: string, domain?: string, path?: string, secure?: boolean}>} cookies
 * @returns {string}
 */
function cookieHeaderFor(targetUrl, cookies) {
  if (!Array.isArray(cookies) || cookies.length === 0) return '';

  let url;
  try {
    url = targetUrl instanceof URL ? targetUrl : new URL(String(targetUrl));
  } catch (err) {
    return '';
  }

  const host = url.hostname.toLowerCase();
  const isSecure = url.protocol === 'https:';
  const seen = new Set();
  const parts = [];

  for (const cookie of cookies) {
    if (!cookie || !cookie.name) continue;

    const domain = String(cookie.domain || '').toLowerCase().replace(/^\./, '');
    if (domain && host !== domain && !host.endsWith(`.${domain}`)) continue;

    const cookiePath = cookie.path || '/';
    if (cookiePath !== '/' && !url.pathname.startsWith(cookiePath)) continue;

    if (cookie.secure && !isSecure) continue;

    if (seen.has(cookie.name)) continue;
    seen.add(cookie.name);
    parts.push(`${cookie.name}=${cookie.value}`);
  }

  return parts.join('; ');
}

/**
 * Assembles request headers that make an outbound fetch look like the browser
 * session that produced the URI.
 * @param {string|URL} targetUrl
 * @param {object} [context] - Request context captured by the extractor
 * @param {object} [extra] - Additional headers, take precedence
 * @returns {Record<string, string>}
 */
function buildRequestHeaders(targetUrl, context = {}, extra = {}) {
  const headers = {
    'User-Agent': context.userAgent || DEFAULT_USER_AGENT,
    'Accept': '*/*',
    'Accept-Language': context.acceptLanguage || 'en-US,en;q=0.9'
  };

  // Referer is the header these CDNs actually check. Origin is deliberately not
  // sent: browsers omit it for plain media GETs and some edges reject it.
  if (context.referer) headers['Referer'] = context.referer;

  const cookie = cookieHeaderFor(targetUrl, context.cookies);
  if (cookie) headers['Cookie'] = cookie;

  return { ...headers, ...extra };
}

/**
 * Combines abort signals, tolerating runtimes without AbortSignal.any.
 * @param {Array<AbortSignal|undefined>} signals
 * @returns {AbortSignal|undefined}
 */
function combineSignals(signals) {
  const list = signals.filter(Boolean);
  if (list.length === 0) return undefined;
  if (list.length === 1) return list[0];
  if (typeof AbortSignal.any === 'function') return AbortSignal.any(list);

  const controller = new AbortController();
  for (const signal of list) {
    if (signal.aborted) {
      controller.abort(signal.reason);
      break;
    }
    signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true });
  }
  return controller.signal;
}

/**
 * Discards a response body so the underlying socket is released.
 * @param {Response} response
 */
async function drain(response) {
  try {
    if (response && response.body && !response.bodyUsed) {
      await response.body.cancel();
    }
  } catch (err) {
    /* the socket is going away regardless */
  }
}

/**
 * Fetches a URL with browser-identical headers, bounded timeout and retries.
 * Retries transport failures and transient status codes only; a 404 fails fast.
 * @param {string|URL} url
 * @param {object} [options]
 * @returns {Promise<Response>}
 */
async function fetchWithRetry(url, options = {}) {
  const {
    retries = 3,
    timeoutMs = 30000,
    context = {},
    headers = {},
    signal,
    method = 'GET',
    allowPrivate
  } = options;

  const target = assertSafeUrl(url, { allowPrivate });
  const attempts = Math.max(1, retries);
  let lastError;

  for (let attempt = 0; attempt < attempts; attempt++) {
    if (signal?.aborted) throw new Error('Cancelled.');

    const timeoutSignal =
      timeoutMs > 0 && typeof AbortSignal.timeout === 'function'
        ? AbortSignal.timeout(timeoutMs)
        : undefined;

    let response;
    try {
      response = await fetch(target, {
        method,
        redirect: 'follow',
        headers: buildRequestHeaders(target, context, headers),
        signal: combineSignals([signal, timeoutSignal])
      });
    } catch (err) {
      if (signal?.aborted) throw new Error('Cancelled.');
      lastError = new Error(`Request to ${target.host} failed: ${err.message}`);
      if (attempt === attempts - 1) throw lastError;
      await backoff(attempt, signal);
      continue;
    }

    if (response.ok) return response;

    const status = response.status;
    const retryAfterMs = parseRetryAfter(response.headers.get('retry-after'));
    await drain(response);

    lastError = new Error(`HTTP ${status} ${response.statusText || ''}`.trim() + ` for ${target.host}`);

    if (!RETRYABLE_STATUS.has(status) || attempt === attempts - 1) throw lastError;
    await backoff(attempt, signal, retryAfterMs);
  }

  throw lastError || new Error('Request failed.');
}

/**
 * @param {string|null} value
 * @returns {number|undefined} Delay in milliseconds
 */
function parseRetryAfter(value) {
  if (!value) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.min(seconds * 1000, 30000);
  const date = Date.parse(value);
  if (Number.isFinite(date)) return Math.min(Math.max(date - Date.now(), 0), 30000);
  return undefined;
}

/**
 * Exponential backoff with jitter, interruptible by an abort signal.
 * @param {number} attempt
 * @param {AbortSignal} [signal]
 * @param {number} [overrideMs]
 */
function backoff(attempt, signal, overrideMs) {
  const delay = overrideMs ?? Math.min(1000 * 2 ** attempt, 8000) + Math.random() * 250;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, delay);
    if (!signal) return;
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        reject(new Error('Cancelled.'));
      },
      { once: true }
    );
  });
}

module.exports = {
  DEFAULT_USER_AGENT,
  assertSafeUrl,
  buildRequestHeaders,
  cookieHeaderFor,
  combineSignals,
  fetchWithRetry,
  isPrivateHost
};
