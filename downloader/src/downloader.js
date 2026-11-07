'use strict';

const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const crypto = require('crypto');
const { spawn, spawnSync } = require('child_process');

const { fetchWithRetry, assertSafeUrl, combineSignals } = require('./net');
const { isHlsPlaylist, parsePlaylist, resolveIv, selectBestVariant } = require('./hls');
const { HAS_YTDLP, downloadWithYtDlp } = require('./ytdlp');

const MAX_PLAYLIST_DEPTH = 4;
const PROBE_BYTES = 2048;
const WINDOWS_RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;

/**
 * Sanitizes a title string so it can be safely used as a directory or file name.
 * @param {string} title
 * @returns {string}
 */
function sanitizeTitle(title) {
  if (!title || typeof title !== 'string') return 'Drama_Series';

  let clean = title
    .replace(/[\\/:*?"<>|\x00-\x1f\x7f]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\.+|\.+$/g, '')
    .trim();

  // Keep well under the 255-byte limit most filesystems enforce.
  if (Buffer.byteLength(clean, 'utf8') > 180) {
    while (Buffer.byteLength(clean, 'utf8') > 180) clean = clean.slice(0, -1);
    clean = clean.trim();
  }

  if (!clean || WINDOWS_RESERVED.test(clean)) return 'Drama_Series';
  return clean;
}

/**
 * Checks if a CLI tool is available on PATH, without invoking a shell.
 * @param {string} cmd
 * @returns {boolean}
 */
function isCommandAvailable(cmd) {
  const probe = process.platform === 'win32' ? 'where' : 'which';
  const result = spawnSync(probe, [cmd], { stdio: 'ignore' });
  return result.status === 0;
}

const HAS_FFMPEG = isCommandAvailable('ffmpeg');

/**
 * Runs ffmpeg asynchronously. The previous implementation used execSync, which
 * blocked the event loop and serialised every "parallel" episode download.
 * @param {string[]} args
 * @param {AbortSignal} [signal]
 * @returns {Promise<void>}
 */
function runFfmpeg(args, signal) {
  return new Promise((resolve, reject) => {
    const child = spawn('ffmpeg', args, { stdio: ['ignore', 'ignore', 'pipe'] });
    let stderr = '';

    const onAbort = () => child.kill('SIGKILL');
    signal?.addEventListener('abort', onAbort, { once: true });

    child.stderr.on('data', chunk => {
      stderr = (stderr + chunk.toString()).slice(-2000);
    });
    child.on('error', err => {
      signal?.removeEventListener('abort', onAbort);
      reject(err);
    });
    child.on('close', code => {
      signal?.removeEventListener('abort', onAbort);
      if (code === 0) resolve();
      else reject(new Error(`ffmpeg exited with code ${code}: ${stderr.trim()}`));
    });
  });
}

/**
 * Opens a request and peeks at the first bytes so we can tell a playlist from a
 * media file without buffering (or corrupting) the whole body.
 * @param {string} uri
 * @param {object} options
 * @returns {Promise<{response: Response, reader: ReadableStreamDefaultReader, head: Buffer, exhausted: boolean, contentType: string}>}
 */
async function openWithPeek(uri, options) {
  const response = await fetchWithRetry(uri, options);
  const contentType = response.headers.get('content-type') || '';

  if (!response.body) {
    const buffer = Buffer.from(await response.arrayBuffer());
    return { response, reader: null, head: buffer, exhausted: true, contentType };
  }

  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  let exhausted = false;

  while (size < PROBE_BYTES) {
    const { done, value } = await reader.read();
    if (done) {
      exhausted = true;
      break;
    }
    chunks.push(Buffer.from(value));
    size += value.length;
  }

  return { response, reader, head: Buffer.concat(chunks), exhausted, contentType };
}

/**
 * Reads whatever is left of a peeked response.
 * @param {ReadableStreamDefaultReader|null} reader
 * @param {Buffer} head
 * @param {boolean} exhausted
 * @returns {Promise<Buffer>}
 */
async function readRemaining(reader, head, exhausted) {
  if (!reader || exhausted) return head;

  const chunks = [head];
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks);
}

/**
 * Fetches and parses a playlist, following master playlists to the best variant.
 * @param {string} uri
 * @param {object} options
 * @param {number} [depth]
 * @returns {Promise<{playlist: object, url: string}>}
 */
async function loadMediaPlaylist(uri, options, depth = 0) {
  if (depth >= MAX_PLAYLIST_DEPTH) {
    throw new Error('Playlist nesting too deep — refusing to follow further variants.');
  }

  const response = await fetchWithRetry(uri, options);
  const text = await response.text();
  const finalUrl = response.url || uri;
  const playlist = parsePlaylist(text, finalUrl);

  if (playlist.type === 'master') {
    const best = selectBestVariant(playlist.variants);
    if (!best) throw new Error('Master playlist contains no playable variants.');
    return loadMediaPlaylist(best.uri, options, depth + 1);
  }

  return { playlist, url: finalUrl };
}

/**
 * Decrypts an AES-128-CBC segment. Some encoders omit PKCS#7 padding on the
 * final block, so we retry without padding validation before giving up.
 * @param {Buffer} data
 * @param {Buffer} key
 * @param {Buffer} iv
 * @returns {Buffer}
 */
function decryptSegment(data, key, iv) {
  const attempt = autoPad => {
    const decipher = crypto.createDecipheriv('aes-128-cbc', key, iv);
    decipher.setAutoPadding(autoPad);
    return Buffer.concat([decipher.update(data), decipher.final()]);
  };

  try {
    return attempt(true);
  } catch (err) {
    return attempt(false);
  }
}

/**
 * Fetches (and caches) an AES key.
 * @param {string} uri
 * @param {Map<string, Buffer>} cache
 * @param {object} options
 * @returns {Promise<Buffer>}
 */
async function loadKey(uri, cache, options) {
  if (cache.has(uri)) return cache.get(uri);

  const response = await fetchWithRetry(uri, options);
  const key = Buffer.from(await response.arrayBuffer());
  if (key.length !== 16) {
    throw new Error(`Unexpected AES key length ${key.length} (expected 16 bytes).`);
  }

  cache.set(uri, key);
  return key;
}

/**
 * Concatenates part files into one container file, streaming so memory stays flat.
 * @param {string[]} partPaths
 * @param {string} destination
 * @returns {Promise<number>} Total bytes written
 */
async function concatParts(partPaths, destination) {
  const output = fs.createWriteStream(destination);
  let bytes = 0;

  try {
    for (const part of partPaths) {
      await new Promise((resolve, reject) => {
        const input = fs.createReadStream(part);
        input.on('data', chunk => {
          bytes += chunk.length;
        });
        input.on('error', reject);
        input.on('end', resolve);
        input.pipe(output, { end: false });
      });
    }
  } finally {
    await new Promise((resolve, reject) => {
      output.on('finish', resolve);
      output.on('error', reject);
      output.end();
    });
  }

  return bytes;
}

/**
 * Downloads every segment of a media playlist into a temporary parts directory.
 * @param {object} playlist
 * @param {string} partsDir
 * @param {object} options
 * @returns {Promise<{partPaths: string[], bytes: number}>}
 */
async function downloadSegments(playlist, partsDir, options) {
  const { context, signal, segmentConcurrency, onEpisodeProgress } = options;

  const segments = playlist.initSegment
    ? [playlist.initSegment, ...playlist.segments]
    : playlist.segments;

  if (segments.length === 0) throw new Error('No video segments found in playlist.');

  await fsp.mkdir(partsDir, { recursive: true });

  const keyCache = new Map();
  const partPaths = segments.map((_, index) => path.join(partsDir, `${String(index).padStart(6, '0')}.part`));

  let completed = 0;
  let bytes = 0;
  let cursor = 0;

  const reportProgress = () => {
    onEpisodeProgress({
      downloadedSegments: completed,
      totalSegments: segments.length,
      percentage: Math.round((completed / segments.length) * 100),
      bytes
    });
  };

  async function worker() {
    while (cursor < segments.length) {
      if (signal?.aborted) throw new Error('Cancelled.');

      const index = cursor++;
      const segment = segments[index];
      const partPath = partPaths[index];

      // Resume: a part file only exists under its final name once fully written.
      try {
        const stat = await fsp.stat(partPath);
        if (stat.size > 0) {
          completed++;
          bytes += stat.size;
          reportProgress();
          continue;
        }
      } catch (err) {
        /* not downloaded yet */
      }

      const headers = segment.byteRange
        ? { Range: `bytes=${segment.byteRange.offset}-${segment.byteRange.offset + segment.byteRange.length - 1}` }
        : {};

      const response = await fetchWithRetry(segment.uri, { context, signal, headers, retries: 4 });
      let data = Buffer.from(await response.arrayBuffer());

      if (segment.key) {
        if (segment.key.method !== 'AES-128') {
          throw new Error(`Unsupported HLS encryption method "${segment.key.method}".`);
        }
        if (!segment.key.uri) throw new Error('Encrypted stream is missing its key URI.');

        const key = await loadKey(segment.key.uri, keyCache, { context, signal });
        data = decryptSegment(data, key, resolveIv(segment.key.iv, segment.sequence));
      }

      const stagingPath = `${partPath}.tmp`;
      await fsp.writeFile(stagingPath, data);
      await fsp.rename(stagingPath, partPath);

      completed++;
      bytes += data.length;
      reportProgress();
    }
  }

  const workers = [];
  const poolSize = Math.max(1, Math.min(segmentConcurrency, segments.length));
  for (let i = 0; i < poolSize; i++) workers.push(worker());
  await Promise.all(workers);

  return { partPaths, bytes };
}

/**
 * Streams a non-HLS response straight to disk, appending when resuming.
 * @param {object} peeked
 * @param {string} destination
 * @param {object} options - { onEpisodeProgress, append, alreadyOnDisk, onData }
 * @returns {Promise<number>} total bytes on disk afterwards
 */
async function streamToFile(peeked, destination, options = {}) {
  const { onEpisodeProgress = () => {}, append = false, alreadyOnDisk = 0, onData = () => {} } = options;
  const { response, reader, head, exhausted } = peeked;

  const contentLength = Number(response.headers.get('content-length')) || 0;
  const totalBytes = contentLength ? contentLength + alreadyOnDisk : 0;

  const output = fs.createWriteStream(destination, { flags: append ? 'a' : 'w' });
  let bytes = alreadyOnDisk;

  const write = chunk =>
    new Promise((resolve, reject) => {
      output.write(chunk, err => (err ? reject(err) : resolve()));
    });

  try {
    if (head.length > 0) {
      await write(head);
      bytes += head.length;
      onData();
    }

    if (reader && !exhausted) {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        // Tell the idle watchdog we are still making progress.
        onData();
        await write(Buffer.from(value));
        bytes += value.length;
        onEpisodeProgress({
          downloadedSegments: 0,
          totalSegments: 0,
          bytes,
          totalBytes,
          percentage: totalBytes ? Math.round((bytes / totalBytes) * 100) : 0
        });
      }
    }
  } finally {
    await new Promise((resolve, reject) => {
      output.on('finish', resolve);
      output.on('error', reject);
      output.end();
    });
  }

  return bytes;
}

/**
 * Aborts a transfer only when it genuinely stalls.
 *
 * A total timeout cannot tell a big file from a hung socket — a 30s cap aborts
 * any download that simply takes longer than 30s, mid-stream. This fires only
 * when no bytes have arrived for `ms`.
 *
 * @param {number} ms
 * @param {AbortSignal} [outerSignal]
 * @returns {{signal: AbortSignal, poke: function, stop: function, stalled: () => boolean}}
 */
function createStallGuard(ms, outerSignal) {
  const controller = new AbortController();
  let stalled = false;
  let timer = null;

  const trip = () => {
    stalled = true;
    controller.abort();
  };

  const poke = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(trip, ms);
    if (timer.unref) timer.unref();
  };

  const stop = () => {
    if (timer) clearTimeout(timer);
    timer = null;
  };

  poke();

  return {
    signal: combineSignals([outerSignal, controller.signal]),
    poke,
    stop,
    stalled: () => stalled
  };
}

/**
 * @param {string} filePath
 * @returns {Promise<number>} size in bytes, 0 if absent
 */
async function fileSize(filePath) {
  const stat = await fsp.stat(filePath).catch(() => null);
  return stat && stat.isFile() ? stat.size : 0;
}

/**
 * Downloads a video URI (HLS playlist or direct file) to outputPath.
 * @param {string} uri - Stream or file URI
 * @param {string} outputPath - Destination .mp4 file path
 * @param {object} [options] - { context, onEpisodeProgress, signal, segmentConcurrency, force }
 * @returns {Promise<{skipped: boolean, bytes: number, remuxed: boolean}>}
 */
async function downloadSingleVideo(uri, outputPath, options = {}) {
  const {
    context = {},
    onEpisodeProgress = () => {},
    signal,
    segmentConcurrency = 6,
    force = false,
    backend = process.env.DRAMADOWN_BACKEND || 'auto',
    alternates = [],
    refreshEndpoint = null
  } = options;

  assertSafeUrl(uri);

  if (force) {
    // "From the start": drop the finished file *and* any partial progress, so
    // nothing from a previous attempt is reused.
    await fsp.rm(outputPath, { force: true }).catch(() => {});
    await fsp.rm(`${outputPath}.part`, { force: true }).catch(() => {});
    await fsp.rm(`${outputPath}.parts`, { recursive: true, force: true }).catch(() => {});
  } else {
    try {
      const stat = await fsp.stat(outputPath);
      if (stat.size > 0) return { skipped: true, bytes: stat.size, remuxed: false };
    } catch (err) {
      /* not downloaded yet — a leftover .part or .parts/ will be resumed */
    }
  }

  // The site usually offers the same episode at several qualities, sometimes on
  // different servers. Those links expire independently, so a dead one is worth
  // retrying elsewhere before declaring the episode failed.
  const candidates = [uri, ...alternates].filter(
    (value, index, all) => typeof value === 'string' && value && all.indexOf(value) === index
  );

  let lastError = null;

  for (let index = 0; index < candidates.length; index++) {
    const candidate = candidates[index];

    try {
      assertSafeUrl(candidate);
    } catch (err) {
      lastError = err;
      continue;
    }

    try {
      return await downloadFromSource(candidate, outputPath, {
        context,
        onEpisodeProgress,
        signal,
        segmentConcurrency,
        backend
      });
    } catch (err) {
      if (/cancelled/i.test(err.message)) throw err;
      lastError = err;

      if (index < candidates.length - 1) {
        onEpisodeProgress({
          stage: 'source_fallback',
          message: `Source ${index + 1} failed (${err.message}) — trying source ${index + 2} of ${candidates.length}...`
        });
        // Partial data belongs to the URL that produced it; a different source
        // would append incompatible bytes.
        await fsp.rm(`${outputPath}.part`, { force: true }).catch(() => {});
        await fsp.rm(`${outputPath}.parts`, { recursive: true, force: true }).catch(() => {});
      }
    }
  }

  // Everything we were handed at extraction time is dead. Go back to the site
  // and ask its other servers for fresh links before giving up.
  if (refreshEndpoint && !/cancelled/i.test(lastError?.message || '')) {
    onEpisodeProgress({
      stage: 'server_refresh',
      message: 'All known links failed — asking the site for another server...'
    });

    const fresh = await fetchServerAlternates(refreshEndpoint, {
      context,
      signal,
      exclude: new Set(candidates)
    }).catch(() => []);

    for (let index = 0; index < fresh.length; index++) {
      if (signal?.aborted) break;

      try {
        assertSafeUrl(fresh[index]);
      } catch (err) {
        continue;
      }

      onEpisodeProgress({
        stage: 'server_refresh',
        message: `Trying a link from another server (${index + 1}/${fresh.length})...`
      });

      await fsp.rm(`${outputPath}.part`, { force: true }).catch(() => {});
      await fsp.rm(`${outputPath}.parts`, { recursive: true, force: true }).catch(() => {});

      try {
        return await downloadFromSource(fresh[index], outputPath, {
          context,
          onEpisodeProgress,
          signal,
          segmentConcurrency,
          backend
        });
      } catch (err) {
        if (/cancelled/i.test(err.message)) throw err;
        lastError = err;
      }
    }
  }

  throw lastError || new Error('No usable source for this episode.');
}

/**
 * Downloads one candidate URL, using the native downloader and optionally
 * falling back to yt-dlp.
 * @param {string} uri
 * @param {string} outputPath
 * @param {object} options
 * @returns {Promise<object>}
 */
async function downloadFromSource(uri, outputPath, options) {
  const { context, onEpisodeProgress, signal, segmentConcurrency, backend } = options;

  // 'ytdlp' forces the external backend; 'native' disables it; 'auto' (default)
  // uses the built-in downloader and falls back to yt-dlp if it fails.
  if (backend === 'ytdlp') {
    if (!HAS_YTDLP) throw new Error('DRAMADOWN_BACKEND=ytdlp but yt-dlp is not installed.');
    const result = await downloadWithYtDlp(uri, outputPath, { context, onEpisodeProgress, signal, concurrentFragments: segmentConcurrency });
    return { skipped: false, bytes: result.bytes, remuxed: true, backend: 'yt-dlp' };
  }

  try {
    return await downloadNatively(uri, outputPath, { context, onEpisodeProgress, signal, segmentConcurrency });
  } catch (err) {
    // An error page is the source being dead, not a downloader limitation —
    // yt-dlp would only fetch the same page again.
    if (backend === 'native' || !HAS_YTDLP || /cancelled|Not a video/i.test(err.message)) throw err;

    // Second opinion: yt-dlp knows CDN quirks this downloader does not.
    onEpisodeProgress({ stage: 'backend_fallback', message: `Native download failed (${err.message}); retrying with yt-dlp...` });
    await fsp.rm(outputPath, { force: true }).catch(() => {});

    try {
      const result = await downloadWithYtDlp(uri, outputPath, { context, onEpisodeProgress, signal, concurrentFragments: segmentConcurrency });
      return { skipped: false, bytes: result.bytes, remuxed: true, backend: 'yt-dlp' };
    } catch (ytErr) {
      throw new Error(`${err.message} (yt-dlp fallback also failed: ${ytErr.message})`);
    }
  }
}

/**
 * Asks the site's own video endpoint for links from its other servers.
 *
 * The player exposes a server switcher (sv=1..5); each server hands out its own
 * signed URLs, which expire independently. When every link we already hold is
 * dead, a different server usually still has a live one — this is the headless
 * equivalent of picking another entry from the page's server sheet.
 *
 * @param {string} refreshEndpoint - Absolute endpoint URL carrying an sv= parameter
 * @param {object} options - { context, signal, exclude }
 * @returns {Promise<string[]>} Fresh URLs, tallest quality first, never already tried
 */
async function fetchServerAlternates(refreshEndpoint, options) {
  const { context, signal, exclude = new Set() } = options;
  const found = [];

  let currentServer = 1;
  try {
    currentServer = Number(new URL(refreshEndpoint).searchParams.get('sv')) || 1;
  } catch (err) {
    return found;
  }

  // Skip the server we already used; its links are the ones that just failed.
  const servers = [1, 2, 3, 4, 5].filter(sv => sv !== currentServer);

  for (const sv of servers) {
    if (signal?.aborted) break;

    const endpoint = refreshEndpoint.replace(/([?&]sv=)\d+/, `$1${sv}`);

    try {
      const response = await fetchWithRetry(endpoint, {
        context,
        signal,
        retries: 2,
        timeoutMs: 20000,
        headers: { Accept: 'application/json' }
      });

      const payload = await response.json();
      if (!payload || payload.ok === false) continue;

      const ranked = (Array.isArray(payload.qualities) ? payload.qualities : [])
        .map(q => ({
          url: q && q.url,
          height: Number(String((q && q.quality) || '').match(/(\d{3,4})\s*p/i)?.[1]) || 0
        }))
        .filter(q => q.url)
        .sort((a, b) => b.height - a.height)
        .map(q => q.url);

      if (payload.videoUrl) ranked.push(payload.videoUrl);

      for (const url of ranked) {
        if (!exclude.has(url) && !found.includes(url)) found.push(url);
      }
    } catch (err) {
      /* that server is unreachable too — try the next */
    }
  }

  return found;
}

/**
 * True when the body is an XML/HTML error page rather than media.
 *
 * These CDNs answer a dead or expired link with a document, and S3 in
 * particular reports a missing object as AccessDenied naming s3:ListBucket
 * (it masks 404 as 403 so bucket contents cannot be probed). Writing that to
 * disk would produce an .mp4 that is really an error message.
 *
 * @param {Buffer} head
 * @param {string} contentType
 * @returns {boolean}
 */
function looksLikeErrorDocument(head, contentType) {
  const start = head.subarray(0, 256).toString('utf8').trimStart().toLowerCase();
  if (start.startsWith('<?xml') || start.startsWith('<html') || start.startsWith('<!doctype html')) return true;
  return /application\/xml|text\/xml|text\/html/.test(contentType);
}

/**
 * Pulls a readable reason out of such a document.
 * @param {Buffer} head
 * @returns {string}
 */
function describeErrorDocument(head) {
  const text = head.subarray(0, 1024).toString('utf8');
  const code = text.match(/<Code>([^<]+)<\/Code>/i)?.[1];
  const message = text.match(/<Message>([^<]+)<\/Message>/i)?.[1];
  const title = text.match(/<title>([^<]+)<\/title>/i)?.[1];

  if (code === 'AccessDenied' && /ListBucket/i.test(message || '')) {
    return 'the source link has expired or the file was removed (S3 reports a missing object as AccessDenied)';
  }
  if (code || message) return `the host returned ${code || 'an error'}: ${(message || '').slice(0, 160)}`;
  if (title) return `the host returned a page titled "${title.slice(0, 80)}"`;
  return 'the host returned an error page instead of video';
}

/**
 * The built-in downloader: HLS assembly or a direct streamed file.
 * @param {string} uri
 * @param {string} outputPath
 * @param {object} options
 * @returns {Promise<{skipped: boolean, bytes: number, remuxed: boolean, backend: string}>}
 */
async function downloadNatively(uri, outputPath, options) {
  const { context, onEpisodeProgress, signal, segmentConcurrency, stallTimeoutMs = 120000 } = options;

  const stagingPath = `${outputPath}.part`;
  // A leftover `.part` can only have come from a direct download — HLS keeps its
  // progress in a `.parts/` directory — so its presence identifies the resume path.
  const alreadyOnDisk = await fileSize(stagingPath);

  // No total timeout on media transfers. A fixed cap cannot tell a large file
  // from a hung socket, and a 30s one aborted every download that took longer
  // than 30 seconds, mid-stream. A stall guard watches for silence instead.
  const guard = createStallGuard(stallTimeoutMs, signal);
  const fetchOptions = { context, signal: guard.signal, timeoutMs: 0 };

  let peeked;
  let resuming = false;

  try {
    if (alreadyOnDisk > 0) {
      const ranged = await fetchWithRetry(uri, {
        ...fetchOptions,
        headers: { Range: `bytes=${alreadyOnDisk}-` }
      });

      // 206 means the server honoured the range; anything else restarts cleanly.
      resuming = ranged.status === 206;
      peeked = {
        response: ranged,
        reader: ranged.body ? ranged.body.getReader() : null,
        head: Buffer.alloc(0),
        exhausted: !ranged.body,
        contentType: ranged.headers.get('content-type') || ''
      };

      onEpisodeProgress({
        stage: resuming ? 'resumed' : 'restarted',
        message: resuming
          ? `Resuming from ${(alreadyOnDisk / 1048576).toFixed(1)} MB`
          : 'Server ignored the resume request — starting over.'
      });
    } else {
      peeked = await openWithPeek(uri, fetchOptions);
    }

    const looksLikeHls =
      !resuming && isHlsPlaylist(peeked.head.toString('utf8'), peeked.contentType, uri);

    if (!looksLikeHls && looksLikeErrorDocument(peeked.head, peeked.contentType)) {
      throw new Error(`Not a video — ${describeErrorDocument(peeked.head)}. Re-extract the series for fresh links.`);
    }

    if (!looksLikeHls) {
      const bytes = await streamToFile(peeked, stagingPath, {
        onEpisodeProgress,
        append: resuming,
        alreadyOnDisk: resuming ? alreadyOnDisk : 0,
        onData: guard.poke
      });

      if (bytes === 0) {
        await fsp.rm(stagingPath, { force: true });
        throw new Error('Downloaded video file is empty.');
      }

      await fsp.rename(stagingPath, outputPath);
      return { skipped: false, bytes, remuxed: false, backend: 'native' };
    }
  } catch (err) {
    if (guard.stalled()) {
      throw new Error(
        `Transfer stalled for ${Math.round(stallTimeoutMs / 1000)}s. ` +
        'Partial progress was kept — download this episode again to resume.'
      );
    }
    throw err;
  } finally {
    guard.stop();
  }

  // The peeked body is the playlist itself — finish reading it, then re-parse
  // from the final (post-redirect) URL so relative segment URIs resolve.
  const playlistText = (await readRemaining(peeked.reader, peeked.head, peeked.exhausted)).toString('utf8');
  const playlistUrl = peeked.response.url || uri;
  let playlist = parsePlaylist(playlistText, playlistUrl);

  if (playlist.type === 'master') {
    const best = selectBestVariant(playlist.variants);
    if (!best) throw new Error('Master playlist contains no playable variants.');
    ({ playlist } = await loadMediaPlaylist(best.uri, fetchOptions, 1));
  }

  const partsDir = `${outputPath}.parts`;
  const isFragmentedMp4 = Boolean(playlist.initSegment);
  const containerPath = `${outputPath}.${isFragmentedMp4 ? 'temp.mp4' : 'temp.ts'}`;

  try {
    const { partPaths } = await downloadSegments(playlist, partsDir, {
      context,
      signal,
      segmentConcurrency,
      onEpisodeProgress
    });

    const bytes = await concatParts(partPaths, containerPath);
    if (bytes === 0) throw new Error('Assembled video is empty.');

    let remuxed = false;
    if (HAS_FFMPEG) {
      const base = ['-y', '-hide_banner', '-loglevel', 'error', '-i', containerPath];
      const attempts = isFragmentedMp4
        ? [[...base, '-c', 'copy', '-movflags', '+faststart', outputPath]]
        : [
            [...base, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-movflags', '+faststart', outputPath],
            ['-y', '-hide_banner', '-loglevel', 'error', '-fflags', '+genpts', '-i', containerPath, '-c', 'copy', outputPath]
          ];

      for (const args of attempts) {
        try {
          await runFfmpeg(args, signal);
          remuxed = true;
          break;
        } catch (err) {
          await fsp.rm(outputPath, { force: true });
        }
      }
    }

    if (!remuxed) {
      // Without ffmpeg (or if remuxing failed) keep the raw container — it still
      // plays in VLC/mpv, it just is not a real MP4.
      await fsp.rename(containerPath, outputPath);
    }

    const finalStat = await fsp.stat(outputPath).catch(() => null);
    if (!finalStat || finalStat.size === 0) {
      throw new Error('Downloaded video file is empty or missing.');
    }

    // Only now is the working state safe to discard.
    await fsp.rm(partsDir, { recursive: true, force: true }).catch(() => {});
    await fsp.rm(containerPath, { force: true }).catch(() => {});

    return { skipped: false, bytes: finalStat.size, remuxed, backend: 'native' };
  } catch (err) {
    // Keep the downloaded segments so the next attempt continues where this one
    // stopped; only the half-assembled container is worthless.
    await fsp.rm(containerPath, { force: true }).catch(() => {});
    throw err;
  }
}

const METADATA_FILE = 'dramadown.json';
const EPISODE_FILE_PATTERN = /^Episode_\d+\.(mp4|ts|mkv|webm)$/i;

/**
 * Counts the episode files actually present in a series folder.
 * @param {string} targetDir
 * @returns {Promise<number>}
 */
async function countEpisodeFiles(targetDir) {
  const names = await fsp.readdir(targetDir).catch(() => []);
  return names.filter(name => EPISODE_FILE_PATTERN.test(name)).length;
}

/**
 * Records where a series came from and how complete it is, in
 * <series>/dramadown.json.
 *
 * Merged rather than replaced: a run that covers a handful of episodes must not
 * erase the source URL or the series total recorded by an earlier one.
 *
 * @param {string} targetDir
 * @param {object} facts - { sourceUrl, dramaTitle, totalEpisodes, playableEpisodes, strategy }
 * @returns {Promise<object>} the merged record
 */
async function writeSeriesMetadata(targetDir, facts = {}) {
  const file = path.join(targetDir, METADATA_FILE);

  let existing = {};
  try {
    existing = JSON.parse(await fsp.readFile(file, 'utf8')) || {};
  } catch (err) {
    /* first run for this series */
  }

  const now = new Date().toISOString();
  const downloadedEpisodes = await countEpisodeFiles(targetDir);

  // Precedence matters. A run covering three episodes knows how many *it* has,
  // not how long the series is, so its own count is only a last resort — an
  // already-recorded total outranks it.
  const knownTotal = Number(facts.totalEpisodes) || 0;
  const totalEpisodes = knownTotal || existing.totalEpisodes || Number(facts.fallbackCount) || 0;

  const record = {
    dramaTitle: facts.dramaTitle || existing.dramaTitle || path.basename(targetDir),
    sourceUrl: facts.sourceUrl || existing.sourceUrl || null,
    strategy: facts.strategy || existing.strategy || null,
    totalEpisodes,
    playableEpisodes: knownTotal
      ? Number(facts.playableEpisodes) || 0
      : existing.playableEpisodes || Number(facts.playableEpisodes) || 0,
    downloadedEpisodes,
    missingEpisodes: Math.max(0, totalEpisodes - downloadedEpisodes),
    firstDownloadedAt: existing.firstDownloadedAt || now,
    lastUpdatedAt: now
  };

  await fsp.writeFile(file, JSON.stringify(record, null, 2) + '\n', 'utf8').catch(() => {});
  return record;
}

/**
 * Downloads all drama episodes into downloads/<Title Of Drama>/
 * @param {object} dramaData - { dramaTitle, episodes, requestContext }
 * @param {object} [options] - { concurrency, onProgress, baseDir, signal, force, segmentConcurrency }
 * @returns {Promise<object>} Summary of the run
 */
async function downloadDramaEpisodes(dramaData, options = {}) {
  const {
    concurrency = 3,
    onProgress = () => {},
    baseDir = path.join(__dirname, '..', 'downloads'),
    signal,
    force = false,
    segmentConcurrency = 6
  } = options;

  const context = options.context || dramaData.requestContext || {};
  const cleanTitle = sanitizeTitle(dramaData.dramaTitle || 'Drama Series');
  const targetDir = path.join(baseDir, cleanTitle);

  await fsp.mkdir(targetDir, { recursive: true });

  const episodes = (dramaData.episodes || []).filter(episode => episode && episode.uri);
  const total = episodes.length;

  if (total === 0) {
    onProgress({ stage: 'all_done', folder: targetDir, cleanTitle, total: 0, completed: 0, failed: 0, skipped: 0, message: 'Nothing to download — no episodes had a usable URI.' });
    return { folder: targetDir, cleanTitle, total: 0, downloadedCount: 0, failedCount: 0, skippedCount: 0, errors: [] };
  }

  // uris.txt records the whole series, not just this run. Downloading a
  // selection used to overwrite it with those few lines, throwing the rest away.
  const manifest = (options.manifestEpisodes || episodes).filter(episode => episode && episode.uri);
  await fsp.writeFile(
    path.join(targetDir, 'uris.txt'),
    manifest.map(episode => episode.uri).join('\n') + '\n',
    'utf8'
  );

  const metadataFacts = {
    sourceUrl: options.sourceUrl || dramaData.sourceUrl || null,
    dramaTitle: dramaData.dramaTitle || cleanTitle,
    strategy: options.strategy || dramaData.strategy || null,
    totalEpisodes: Number(options.totalEpisodes) || Number(dramaData.totalEpisodes) || 0,
    playableEpisodes: manifest.length,
    // Used only when nothing better has ever been recorded.
    fallbackCount: manifest.length
  };

  // Written up front so a run that fails outright still records its origin.
  await writeSeriesMetadata(targetDir, metadataFacts);

  const highestEpisode = episodes.reduce((max, episode, index) => Math.max(max, Number(episode.episode) || index + 1), 0);
  const padLength = Math.max(2, String(highestEpisode).length);

  let completed = 0;
  let failed = 0;
  let skipped = 0;
  let cursor = 0;
  const errors = [];

  onProgress({
    stage: 'start',
    folder: targetDir,
    cleanTitle,
    total,
    completed: 0,
    message: `Created folder "downloads/${cleanTitle}". Starting download of ${total} episodes...`
  });

  async function worker() {
    while (cursor < episodes.length) {
      if (signal?.aborted) return;

      const index = cursor++;
      const episode = episodes[index];
      const episodeNumber = Number(episode.episode) || index + 1;
      const filename = `Episode_${String(episodeNumber).padStart(padLength, '0')}.mp4`;
      const filePath = path.join(targetDir, filename);

      onProgress({
        stage: 'downloading',
        episode: episodeNumber,
        total,
        filename,
        message: `[${index + 1}/${total}] Downloading ${filename}...`
      });

      try {
        const result = await downloadSingleVideo(episode.uri, filePath, {
          context,
          signal,
          force,
          segmentConcurrency,
          alternates: Array.isArray(episode.alternates) ? episode.alternates : [],
          refreshEndpoint: episode.refreshEndpoint || null,
          onEpisodeProgress: progress => {
            onProgress({
              stage: 'episode_progress',
              episode: episodeNumber,
              total,
              filename,
              ...progress
            });
          }
        });

        if (result.skipped) {
          skipped++;
          onProgress({
            stage: 'episode_skipped',
            bytes: result.bytes,
            episode: episodeNumber,
            total,
            completed: completed + skipped,
            filename,
            message: `[${index + 1}/${total}] ${filename} already exists — skipped.`
          });
        } else {
          completed++;
          onProgress({
            stage: 'episode_done',
            episode: episodeNumber,
            total,
            completed: completed + skipped,
            filename,
            bytes: result.bytes,
            message: `[${completed + skipped}/${total}] Finished ${filename}`
          });
        }
      } catch (err) {
        if (signal?.aborted) return;
        failed++;
        errors.push({ episode: episodeNumber, filename, error: err.message });
        onProgress({
          stage: 'episode_error',
          episode: episodeNumber,
          total,
          filename,
          error: err.message,
          message: `[${index + 1}/${total}] Failed ${filename}: ${err.message}`
        });
      }
    }
  }

  const poolSize = Math.max(1, Math.min(Number(concurrency) || 3, 8, total));
  await Promise.all(Array.from({ length: poolSize }, () => worker()));

  // Refresh the counts now that the run is over.
  const metadata = await writeSeriesMetadata(targetDir, metadataFacts);

  const summary = {
    folder: targetDir,
    cleanTitle,
    metadata,
    sourceUrl: metadata.sourceUrl,
    seriesTotal: metadata.totalEpisodes,
    onDisk: metadata.downloadedEpisodes,
    total,
    downloadedCount: completed + skipped,
    freshCount: completed,
    skippedCount: skipped,
    failedCount: failed,
    errors
  };

  onProgress({
    stage: 'all_done',
    ...summary,
    completed: completed + skipped,
    failed,
    message:
      `All done! ${completed + skipped}/${total} episodes in "downloads/${cleanTitle}"` +
      (skipped ? ` (${skipped} already present)` : '') +
      (failed ? ` — ${failed} failed.` : '.')
  });

  return summary;
}

module.exports = {
  HAS_FFMPEG,
  countEpisodeFiles,
  writeSeriesMetadata,
  METADATA_FILE,
  concatParts,
  decryptSegment,
  downloadDramaEpisodes,
  downloadSingleVideo,
  sanitizeTitle
};
