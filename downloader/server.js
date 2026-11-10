'use strict';

const http = require('http');
const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const crypto = require('crypto');

const { extractDramaVideoUris } = require('./src/extractor');
const { downloadDramaEpisodes } = require('./src/downloader');
const { validateWatchUrl } = require('./src/discovery');
const { createProgressReporter } = require('./src/progress');

const PORT = Number(process.env.PORT) || 3000;
// Bind to loopback by default. Containers set HOST=0.0.0.0 explicitly, so a
// local `npm run web` is never exposed to the rest of the network by accident.
const HOST = process.env.HOST || '127.0.0.1';
const PUBLIC_DIR = path.join(__dirname, 'public');
const DOWNLOAD_DIR = process.env.DOWNLOAD_DIR
  ? path.resolve(process.env.DOWNLOAD_DIR)
  : path.join(__dirname, 'downloads');

const MAX_BODY_BYTES = 1024 * 1024;
const SESSION_TTL_MS = 30 * 60 * 1000;
const KEEPALIVE_MS = 15000;

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2'
};

/**
 * Extraction results live here, never in the browser. The download endpoint
 * works from a session id, so a page cannot ask the server to fetch a URL of
 * its choosing, and the solved session's cookies are never handed to the client.
 * @type {Map<string, {createdAt: number, dramaTitle: string, episodes: Array, requestContext: object}>}
 */
const sessions = new Map();

/**
 * @param {object} result
 * @returns {string} session id
 */
function storeSession(result) {
  sweepSessions();
  const id = crypto.randomUUID();
  sessions.set(id, {
    createdAt: Date.now(),
    dramaTitle: result.dramaTitle,
    sourceUrl: result.sourceUrl || null,
    strategy: result.strategy || null,
    totalEpisodes: result.totalEpisodes || 0,
    episodes: result.episodes,
    requestContext: result.requestContext
  });
  return id;
}

function sweepSessions() {
  const cutoff = Date.now() - SESSION_TTL_MS;
  for (const [id, session] of sessions) {
    if (session.createdAt < cutoff) sessions.delete(id);
  }
}

/**
 * Strips the solved session's credentials before anything is sent to the browser.
 * @param {object} result
 * @param {string} sessionId
 * @returns {object}
 */
function toClientPayload(result, sessionId) {
  const { requestContext, ...safe } = result;
  return { ...safe, sessionId };
}

/**
 * Rejects requests initiated by another site. Without this, any page the user
 * has open could drive this server, because it runs on a predictable localhost port.
 * @param {http.IncomingMessage} req
 * @returns {boolean}
 */
function isSameOriginRequest(req) {
  const fetchSite = req.headers['sec-fetch-site'];
  if (fetchSite && fetchSite !== 'same-origin' && fetchSite !== 'none') return false;

  const origin = req.headers.origin;
  if (!origin) return true;

  try {
    return new URL(origin).host === req.headers.host;
  } catch (err) {
    return false;
  }
}

/**
 * @param {http.ServerResponse} res
 * @param {number} status
 * @param {object} payload
 */
function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body)
  });
  res.end(body);
}

/**
 * Reads a JSON request body with a hard size cap.
 * @param {http.IncomingMessage} req
 * @returns {Promise<object>}
 */
function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];

    req.on('data', chunk => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('Request body too large.'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('error', reject);
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(new Error('Request body is not valid JSON.'));
      }
    });
  });
}

/**
 * Starts an SSE response and returns helpers for writing to it.
 * @param {http.IncomingMessage} req
 * @param {http.ServerResponse} res
 * @returns {{send: function, close: function, signal: AbortSignal}}
 */
function startEventStream(req, res) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no'
  });

  const controller = new AbortController();
  const keepalive = setInterval(() => {
    if (!res.writableEnded) res.write(': keepalive\n\n');
  }, KEEPALIVE_MS);

  // If the user closes the tab, stop the browser / downloads instead of
  // letting the work run on unattended.
  res.on('close', () => {
    if (!res.writableEnded) controller.abort();
    clearInterval(keepalive);
  });

  return {
    signal: controller.signal,
    send(event, data) {
      if (res.writableEnded) return;
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    },
    close() {
      clearInterval(keepalive);
      if (!res.writableEnded) res.end();
    }
  };
}

// Only one extraction may run at a time: each launches a full browser, and they
// would fight over the shared Chrome profile that keeps the cf_clearance cookie.
let extractionInFlight = false;

/**
 * GET /api/extract-stream?url=...
 */
async function handleExtractStream(req, res, reqUrl) {
  const targetUrl = reqUrl.searchParams.get('url');
  if (!targetUrl) return sendJson(res, 400, { error: 'URL parameter is required.' });

  if (extractionInFlight) {
    return sendJson(res, 409, {
      error: 'An extraction is already running. Wait for it to finish before starting another.'
    });
  }
  extractionInFlight = true;

  const stream = startEventStream(req, res);
  stream.send('progress', { stage: 'init', message: 'Starting extraction process...' });

  try {
    const result = await extractDramaVideoUris(targetUrl, {
      signal: stream.signal,
      onProgress: progress => stream.send('progress', progress)
    });

    const sessionId = storeSession(result);
    stream.send('complete', toClientPayload(result, sessionId));
  } catch (err) {
    if (!stream.signal.aborted) {
      stream.send('extract_error', { message: err.message || 'An error occurred during extraction.' });
    }
  } finally {
    extractionInFlight = false;
    stream.close();
  }
}

/**
 * POST /api/download-stream  { sessionId, concurrency, force }
 */
async function handleDownloadStream(req, res) {
  let payload;
  try {
    payload = await readJsonBody(req);
  } catch (err) {
    return sendJson(res, 400, { error: err.message });
  }

  const session = payload.sessionId ? sessions.get(payload.sessionId) : null;
  if (!session) {
    return sendJson(res, 404, {
      error: 'Unknown or expired extraction session. Run the extraction again before downloading.'
    });
  }

  // The folder name may be corrected in the UI before downloading. It is still
  // run through sanitizeTitle downstream, so it cannot escape the base directory.
  const titleOverride = typeof payload.titleOverride === 'string' ? payload.titleOverride.trim() : '';

  // An explicit selection downloads only those episode numbers. The URIs still
  // come from the stored session, so the client chooses *which* to fetch, never
  // *what* to fetch.
  const wanted = Array.isArray(payload.episodes)
    ? new Set(payload.episodes.map(Number).filter(Number.isFinite))
    : null;

  const episodes = wanted && wanted.size > 0
    ? session.episodes.filter(episode => wanted.has(Number(episode.episode)))
    : session.episodes;

  if (episodes.length === 0) {
    return sendJson(res, 400, { error: 'None of the selected episodes are in this extraction.' });
  }

  const stream = startEventStream(req, res);

  try {
    const result = await downloadDramaEpisodes(
      {
        dramaTitle: titleOverride || session.dramaTitle,
        episodes,
        requestContext: session.requestContext
      },
      {
        baseDir: DOWNLOAD_DIR,
        // uris.txt should always list the whole series, even on a partial run.
        manifestEpisodes: session.episodes,
        sourceUrl: session.sourceUrl,
        strategy: session.strategy,
        totalEpisodes: session.totalEpisodes,
        concurrency: clampConcurrency(payload.concurrency),
        force: payload.force === true,
        signal: stream.signal,
        onProgress: progress => stream.send('download_progress', progress)
      }
    );

    stream.send('download_complete', result);
  } catch (err) {
    if (!stream.signal.aborted) {
      stream.send('download_error', { message: err.message || 'Download failed.' });
    }
  } finally {
    stream.close();
  }
}

function acquisitionFailure(error, phase) {
  const message = String(error?.message || '');
  const http = /\bHTTP ([45]\d{2})\b/.exec(message);
  if (http) return { reason: 'provider_http', httpStatus: Number(http[1]) };
  if (/challenge|block page/i.test(message)) return { reason: 'provider_access' };
  if (error?.name === 'TimeoutError' || /timed? ?out|timeout/i.test(message)) return { reason: 'timeout' };
  if (/episode list|episode information|no playable video/i.test(message)) return { reason: 'no_playable_episodes' };
  return { reason: phase === 'extracting' ? 'extraction_failed' : 'transfer_failed' };
}

/** Internal worker protocol. Job identity and progress live in Dramatarr's durable queue. */
async function handleAcquire(req, res) {
  let payload;
  try { payload = await readJsonBody(req); } catch (err) {
    return sendJson(res, 400, { error: 'Invalid request body.' });
  }
  if (!/^[a-f0-9]{32}$/.test(payload.jobId || '')) {
    return sendJson(res, 400, { error: 'Invalid job id.' });
  }
  try { validateWatchUrl(payload.url); } catch {
    return sendJson(res, 400, { error: 'Use an HTTPS Dramafren provider URL.' });
  }
  if (extractionInFlight) return sendJson(res, 409, { error: 'Worker busy.' });
  extractionInFlight = true;
  const stream = startEventStream(req, res);
  const report = createProgressReporter(data => stream.send('progress', data));
  let phase = 'extracting';
  try {
    const result = await extractDramaVideoUris(payload.url, {
      signal: stream.signal,
      onProgress: report
    });
    phase = 'downloading';
    const summary = await downloadDramaEpisodes(result, {
      baseDir: path.join(DOWNLOAD_DIR, payload.jobId),
      sourceUrl: payload.url,
      strategy: result.strategy,
      totalEpisodes: result.totalEpisodes,
      concurrency: clampConcurrency(undefined),
      force: false,
      signal: stream.signal,
      onProgress: report
    });
    if (!stream.signal.aborted) stream.send('complete', {
      kind: 'complete', cleanTitle: summary.cleanTitle,
      failedCount: summary.failedCount + (result.episodes || []).filter(e => !e.uri).length,
      downloadedCount: summary.downloadedCount, episodesTotal: result.totalEpisodes || summary.total
    });
  } catch (err) {
    if (!stream.signal.aborted) stream.send('error', { kind: 'error', phase, ...acquisitionFailure(err, phase) });
  } finally {
    extractionInFlight = false;
    stream.close();
  }
}

async function handleDiscover(req, res) {
  let payload;
  try { payload = await readJsonBody(req); } catch { return sendJson(res,400,{error:'Invalid body'}); }
  if (typeof payload.query !== 'string' || !payload.query.trim() || payload.query.length > 300) return sendJson(res,400,{error:'Invalid query'});
  if (extractionInFlight) return sendJson(res,409,{error:'Worker busy'});
  extractionInFlight=true;
  const stream=startEventStream(req,res);
  try {
    const result=await extractDramaVideoUris('https://dramafren.org/', {
      signal:stream.signal,
      onProgress: p => stream.send('progress',{kind:'progress',stage:'searching',provider:p.provider,language:p.language,message:p.message}),
      discovery:{query:payload.query,sourceUrl:payload.sourceUrl,languages:payload.languages || ['es','en']}
    });
    stream.send('complete',{kind:'complete',...result});
  } catch (err) {
    if (!stream.signal.aborted) stream.send('error',{kind:'error',message:'Search failed.'});
  } finally {extractionInFlight=false;stream.close();}
}

/**
 * POST /api/extract  { url }
 */
async function handleExtract(req, res) {
  let payload;
  try {
    payload = await readJsonBody(req);
  } catch (err) {
    return sendJson(res, 400, { error: err.message });
  }

  if (!payload.url) return sendJson(res, 400, { error: 'Missing url field in request body.' });

  try {
    const result = await extractDramaVideoUris(payload.url);
    sendJson(res, 200, toClientPayload(result, storeSession(result)));
  } catch (err) {
    sendJson(res, 500, { error: err.message });
  }
}

/**
 * Resolves a name inside the download directory, refusing anything that would
 * escape it.
 * @param {string} folderName
 * @returns {string|null}
 */
function resolveLibraryPath(folderName) {
  const name = String(folderName || '');
  if (!name || name.includes('/') || name.includes('\\') || name.includes('\0')) return null;

  const resolved = path.resolve(DOWNLOAD_DIR, name);
  if (resolved !== DOWNLOAD_DIR && !resolved.startsWith(DOWNLOAD_DIR + path.sep)) return null;
  return resolved;
}

/**
 * GET /api/library — the series folders already on disk.
 */
async function handleLibrary(res) {
  const entries = await fsp.readdir(DOWNLOAD_DIR, { withFileTypes: true }).catch(() => []);
  const folders = [];

  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue;

    const dir = path.join(DOWNLOAD_DIR, entry.name);
    const files = await fsp.readdir(dir).catch(() => []);

    let bytes = 0;
    let episodes = 0;
    let modified = 0;

    for (const file of files) {
      if (!/\.(mp4|ts|mkv|webm)$/i.test(file)) continue;
      const stat = await fsp.stat(path.join(dir, file)).catch(() => null);
      if (!stat) continue;
      episodes++;
      bytes += stat.size;
      modified = Math.max(modified, stat.mtimeMs);
    }

    let metadata = null;
    try {
      metadata = JSON.parse(await fsp.readFile(path.join(dir, 'dramadown.json'), 'utf8'));
    } catch (err) {
      /* series downloaded before metadata existed, or not written yet */
    }

    folders.push({
      name: entry.name,
      episodes,
      bytes,
      modified,
      sourceUrl: metadata?.sourceUrl || null,
      totalEpisodes: metadata?.totalEpisodes || 0,
      downloadedEpisodes: metadata?.downloadedEpisodes ?? episodes,
      missingEpisodes: metadata?.totalEpisodes ? Math.max(0, metadata.totalEpisodes - episodes) : 0
    });
  }

  folders.sort((a, b) => b.modified - a.modified || a.name.localeCompare(b.name));
  sendJson(res, 200, { folder: DOWNLOAD_DIR, series: folders });
}

/**
 * GET /api/library/files?folder=... — one series folder's contents.
 */
async function handleLibraryFiles(res, reqUrl) {
  const dir = resolveLibraryPath(reqUrl.searchParams.get('folder'));
  if (!dir) return sendJson(res, 400, { error: 'Invalid folder name.' });

  const names = await fsp.readdir(dir).catch(() => null);
  if (!names) return sendJson(res, 404, { error: 'That series folder does not exist.' });

  const files = [];
  for (const name of names) {
    const stat = await fsp.stat(path.join(dir, name)).catch(() => null);
    if (!stat || !stat.isFile()) continue;
    files.push({ name, bytes: stat.size, modified: stat.mtimeMs });
  }

  files.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  sendJson(res, 200, { folder: path.basename(dir), files });
}

/**
 * @param {unknown} value
 * @returns {number}
 */
function clampConcurrency(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 3;
  return Math.max(1, Math.min(8, Math.floor(parsed)));
}

/**
 * Serves a file from public/, refusing anything that escapes the directory.
 *
 * Sends a validator and `Cache-Control: no-cache` — which means "revalidate
 * before reusing", not "do not store". Without it browsers cache the dashboard
 * heuristically, so a redeploy leaves people staring at the previous UI.
 */
function serveStatic(req, res, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch (err) {
    res.writeHead(400, { 'Content-Type': 'text/plain' });
    res.end('400 Bad Request');
    return;
  }

  const relative = decoded === '/' ? 'index.html' : decoded.replace(/^\/+/, '');
  const filePath = path.resolve(PUBLIC_DIR, relative);

  if (filePath !== PUBLIC_DIR && !filePath.startsWith(PUBLIC_DIR + path.sep)) {
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    res.end('403 Access Denied');
    return;
  }

  fs.stat(filePath, (statErr, stat) => {
    if (statErr || !stat.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('404 Not Found');
      return;
    }

    const contentType = MIME_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
    const etag = `W/"${stat.size.toString(16)}-${Math.floor(stat.mtimeMs).toString(16)}"`;

    if (req.headers['if-none-match'] === etag) {
      res.writeHead(304, { ETag: etag, 'Cache-Control': 'no-cache' });
      res.end();
      return;
    }

    res.writeHead(200, {
      'Content-Type': contentType,
      'Content-Length': stat.size,
      'X-Content-Type-Options': 'nosniff',
      'Cache-Control': 'no-cache',
      ETag: etag,
      'Last-Modified': stat.mtime.toUTCString()
    });
    fs.createReadStream(filePath).on('error', () => res.end()).pipe(res);
  });
}

const server = http.createServer(async (req, res) => {
  let reqUrl;
  try {
    reqUrl = new URL(req.url, `http://${req.headers.host || `${HOST}:${PORT}`}`);
  } catch (err) {
    return sendJson(res, 400, { error: 'Malformed request URL.' });
  }

  const pathname = reqUrl.pathname;

  if (pathname.startsWith('/api/') && !isSameOriginRequest(req)) {
    return sendJson(res, 403, { error: 'Cross-origin requests to the DramaDown API are not allowed.' });
  }

  try {
    if (pathname === '/api/health') {
      return sendJson(res, 200, { status: 'ok', downloadDir: DOWNLOAD_DIR, sessions: sessions.size });
    }
    // Health is public for Docker; all other API calls require the internal credential
    // whenever this engine is deployed as part of Dramatarr.
    const workerKey = process.env.DRAMATARR_WORKER_KEY;
    if (workerKey) {
      const supplied = Buffer.from(String(req.headers['x-worker-key'] || ''));
      const expected = Buffer.from(workerKey);
      if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) {
        return sendJson(res, 401, { error: 'Worker credential required.' });
      }
    }
    if (pathname === '/api/discover' && req.method === 'POST') {
      if (!workerKey) return sendJson(res,503,{error:'Worker credential is not configured.'});
      return await handleDiscover(req,res);
    }
    if (pathname === '/api/acquire' && req.method === 'POST') {
      if (!workerKey) return sendJson(res, 503, { error: 'Worker credential is not configured.' });
      return await handleAcquire(req, res);
    }
    if (pathname === '/api/extract-stream' && req.method === 'GET') {
      return await handleExtractStream(req, res, reqUrl);
    }
    if (pathname === '/api/download-stream' && req.method === 'POST') {
      return await handleDownloadStream(req, res);
    }
    if (pathname === '/api/extract' && req.method === 'POST') {
      return await handleExtract(req, res);
    }
    if (pathname === '/api/library' && req.method === 'GET') {
      return await handleLibrary(res);
    }
    if (pathname === '/api/library/files' && req.method === 'GET') {
      return await handleLibraryFiles(res, reqUrl);
    }
    if (pathname.startsWith('/api/')) {
      return sendJson(res, 404, { error: 'Unknown endpoint.' });
    }

    serveStatic(req, res, pathname);
  } catch (err) {
    if (!res.headersSent) sendJson(res, 500, { error: err.message });
    else res.end();
  }
});

/**
 * Starts listening. Only invoked when run directly, so tests can bind an
 * ephemeral port instead.
 */
function start() {
  // Fail loudly at boot rather than after a 20-minute extraction: a bind mount
  // that is read-only or owned by another uid is the most common deploy mistake.
  try {
    fs.mkdirSync(DOWNLOAD_DIR, { recursive: true });
    const probe = path.join(DOWNLOAD_DIR, `.write-test-${process.pid}`);
    fs.writeFileSync(probe, '');
    fs.unlinkSync(probe);
  } catch (err) {
    console.error(
      `\n\x1b[31m✖ Download directory is not writable:\x1b[0m ${DOWNLOAD_DIR}\n` +
      `  ${err.message}\n` +
      `  Check the bind mount and its ownership (see PUID/PGID in README.md).\n`
    );
    process.exit(1);
  }

  server.listen(PORT, HOST, () => {
    const shown = HOST === '0.0.0.0' || HOST === '::' ? 'localhost' : HOST;
    console.log(`\n\x1b[32m🚀 DramaDown Server running at:\x1b[0m \x1b[1m\x1b[36mhttp://${shown}:${PORT}\x1b[0m`);
    console.log(`\x1b[90m   Downloads:\x1b[0m ${DOWNLOAD_DIR}\n`);
  });
  return server;
}

if (require.main === module) start();

module.exports = { server, start, sessions };
