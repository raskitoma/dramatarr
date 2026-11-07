'use strict';

/**
 * End-to-end exercise of the download pipeline against a local origin that
 * behaves like the real CDNs: it serves a master playlist, an AES-128 encrypted
 * media playlist, and it refuses any request that arrives without the browser's
 * Referer and cookies.
 */

// Must be set before the downloader validates the loopback test origin.
process.env.DRAMADOWN_ALLOW_PRIVATE_HOSTS = '1';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const { downloadSingleVideo, downloadDramaEpisodes, HAS_FFMPEG } = require('../src/downloader');

const AES_KEY = crypto.randomBytes(16);
const SEGMENT_COUNT = 4;
const EXPECTED_COOKIE = 'cf_clearance=solved-token';
const EXPECTED_REFERER = 'https://goodshort.example.org/index.php?page=watch&id=42';

const REQUEST_CONTEXT = {
  userAgent: 'DramaDownTest/1.0',
  referer: EXPECTED_REFERER,
  cookies: [{ name: 'cf_clearance', value: 'solved-token', domain: '127.0.0.1', path: '/', secure: false }]
};

/**
 * Encrypts a segment the way an HLS packager would.
 * @param {Buffer} plaintext
 * @param {number} sequence
 * @returns {Buffer}
 */
function encryptSegment(plaintext, sequence) {
  const iv = Buffer.alloc(16);
  iv.writeUInt32BE(sequence, 12);
  const cipher = crypto.createCipheriv('aes-128-cbc', AES_KEY, iv);
  return Buffer.concat([cipher.update(plaintext), cipher.final()]);
}

/**
 * Starts a fake CDN. Every media request must carry the browser identity.
 * @param {Buffer[]} plaintextSegments
 * @returns {Promise<{origin: string, close: function, log: object}>}
 */
async function startFakeCdn(plaintextSegments) {
  const log = { referers: [], cookies: [], unauthorized: 0, paths: [] };

  const server = http.createServer((req, res) => {
    log.paths.push(req.url);

    // Everything except the entry playlist is treated as protected media.
    const referer = req.headers.referer;
    const cookie = req.headers.cookie;
    log.referers.push(referer);
    log.cookies.push(cookie);

    if (referer !== EXPECTED_REFERER || cookie !== EXPECTED_COOKIE) {
      log.unauthorized++;
      res.writeHead(403, { 'Content-Type': 'text/plain' });
      res.end('Forbidden');
      return;
    }

    const url = new URL(req.url, 'http://127.0.0.1');

    if (url.pathname === '/master.m3u8') {
      res.writeHead(200, { 'Content-Type': 'application/vnd.apple.mpegurl' });
      res.end(
        [
          '#EXTM3U',
          '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=480x854',
          'v/480.m3u8',
          '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1080x1920',
          'v/1080.m3u8',
          ''
        ].join('\n')
      );
      return;
    }

    if (url.pathname === '/v/1080.m3u8') {
      const lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:4', '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-KEY:METHOD=AES-128,URI="../key.bin"'];
      for (let i = 0; i < SEGMENT_COUNT; i++) {
        lines.push('#EXTINF:4.0,', `seg${i}.ts`);
      }
      lines.push('#EXT-X-ENDLIST', '');
      res.writeHead(200, { 'Content-Type': 'application/vnd.apple.mpegurl' });
      res.end(lines.join('\n'));
      return;
    }

    if (url.pathname === '/v/480.m3u8') {
      res.writeHead(200, { 'Content-Type': 'application/vnd.apple.mpegurl' });
      res.end('#EXTM3U\n#EXTINF:4.0,\nlow.ts\n#EXT-X-ENDLIST\n');
      return;
    }

    if (url.pathname === '/key.bin') {
      res.writeHead(200, { 'Content-Type': 'application/octet-stream' });
      res.end(AES_KEY);
      return;
    }

    const segmentMatch = url.pathname.match(/^\/v\/seg(\d+)\.ts$/);
    if (segmentMatch) {
      const index = Number(segmentMatch[1]);
      res.writeHead(200, { 'Content-Type': 'video/mp2t' });
      res.end(encryptSegment(plaintextSegments[index], index));
      return;
    }

    if (url.pathname === '/direct.mp4') {
      const body = Buffer.concat(plaintextSegments);
      res.writeHead(200, { 'Content-Type': 'video/mp4', 'Content-Length': body.length });
      res.end(body);
      return;
    }

    res.writeHead(404).end('nope');
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();

  return {
    origin: `http://127.0.0.1:${port}`,
    log,
    close: () => new Promise(resolve => server.close(resolve))
  };
}

/**
 * Produces a real MPEG-TS clip so the ffmpeg remux step is genuinely exercised.
 * @param {string} dir
 * @returns {Buffer|null}
 */
function makeRealTransportStream(dir) {
  if (!HAS_FFMPEG) return null;
  const output = path.join(dir, 'source.ts');
  const result = spawnSync(
    'ffmpeg',
    [
      '-y', '-hide_banner', '-loglevel', 'error',
      '-f', 'lavfi', '-i', 'testsrc=duration=2:size=128x128:rate=15',
      '-c:v', 'mpeg2video', '-b:v', '400k',
      '-f', 'mpegts', output
    ],
    { stdio: 'ignore' }
  );
  if (result.status !== 0 || !fs.existsSync(output)) return null;
  return fs.readFileSync(output);
}

test('downloads an encrypted HLS stream end to end, carrying the browser session', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-e2e-'));
  const plaintext = Array.from({ length: SEGMENT_COUNT }, (_, i) =>
    Buffer.from(`SEGMENT-${i}-`.repeat(200), 'utf8')
  );
  const cdn = await startFakeCdn(plaintext);

  t.after(async () => {
    await cdn.close();
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const outputPath = path.join(dir, 'Episode_01.mp4');
  const progress = [];

  const result = await downloadSingleVideo(`${cdn.origin}/master.m3u8`, outputPath, {
    context: REQUEST_CONTEXT,
    onEpisodeProgress: update => progress.push(update)
  });

  assert.equal(cdn.log.unauthorized, 0, 'every request carried the Referer and cookies');
  assert.ok(cdn.log.paths.includes('/master.m3u8'), 'fetched the master playlist');
  assert.ok(cdn.log.paths.includes('/v/1080.m3u8'), 'followed the highest-resolution variant');
  assert.ok(cdn.log.paths.includes('/key.bin'), 'fetched the AES key');
  assert.equal(cdn.log.paths.filter(p => /seg\d\.ts/.test(p)).length, SEGMENT_COUNT);

  // ffmpeg cannot remux synthetic payloads, so the raw container is kept —
  // which lets us assert the decrypted bytes exactly.
  assert.deepEqual(fs.readFileSync(outputPath), Buffer.concat(plaintext), 'segments were decrypted and joined in order');
  assert.equal(result.skipped, false);

  assert.ok(progress.length > 0, 'segment progress was reported');
  assert.equal(progress.at(-1).percentage, 100);
  assert.equal(progress.at(-1).totalSegments, SEGMENT_COUNT);

  // Temporary artefacts are cleaned up.
  assert.equal(fs.existsSync(`${outputPath}.parts`), false);
  assert.equal(fs.existsSync(`${outputPath}.temp.ts`), false);
});

test('remuxes a real transport stream into a playable MP4', { skip: !HAS_FFMPEG ? 'ffmpeg not installed' : false }, async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-mux-'));
  t.after(async () => fsp.rm(dir, { recursive: true, force: true }));

  const source = makeRealTransportStream(dir);
  if (!source) return t.skip('ffmpeg could not produce a test clip');

  const chunkSize = Math.ceil(source.length / SEGMENT_COUNT);
  const plaintext = Array.from({ length: SEGMENT_COUNT }, (_, i) =>
    source.subarray(i * chunkSize, Math.min((i + 1) * chunkSize, source.length))
  );

  const cdn = await startFakeCdn(plaintext);
  t.after(async () => cdn.close());

  const outputPath = path.join(dir, 'Episode_01.mp4');
  const result = await downloadSingleVideo(`${cdn.origin}/master.m3u8`, outputPath, {
    context: REQUEST_CONTEXT
  });

  assert.equal(result.remuxed, true, 'ffmpeg produced an MP4');

  const header = fs.readFileSync(outputPath).subarray(4, 8).toString('latin1');
  assert.equal(header, 'ftyp', 'output is a real MP4 container');

  const probe = spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', outputPath], {
    encoding: 'utf8'
  });
  if (probe.status === 0) {
    assert.match(probe.stdout, /video/, 'the MP4 contains a video stream');
  }
});

test('streams a direct (non-HLS) video file to disk', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-direct-'));
  const plaintext = [Buffer.from('DIRECT-VIDEO-BODY-'.repeat(500), 'utf8')];
  const cdn = await startFakeCdn(plaintext);

  t.after(async () => {
    await cdn.close();
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const outputPath = path.join(dir, 'Episode_01.mp4');
  await downloadSingleVideo(`${cdn.origin}/direct.mp4`, outputPath, { context: REQUEST_CONTEXT });

  assert.deepEqual(fs.readFileSync(outputPath), plaintext[0], 'body written verbatim, not through response.text()');
  assert.equal(fs.existsSync(`${outputPath}.part`), false);
});

test('a download without the browser session is rejected by the CDN', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-noctx-'));
  const cdn = await startFakeCdn([Buffer.from('x')]);

  t.after(async () => {
    await cdn.close();
    await fsp.rm(dir, { recursive: true, force: true });
  });

  // This is the failure the old code hit on every real download: correct URI,
  // but no Referer or cookies, so the edge returns 403.
  await assert.rejects(
    downloadSingleVideo(`${cdn.origin}/master.m3u8`, path.join(dir, 'a.mp4'), { context: {} }),
    /HTTP 403/
  );
});

test('a direct download that dies mid-transfer resumes from where it stopped', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-resume-'));
  const body = Buffer.from('RESUMABLE-VIDEO-BODY-'.repeat(4000), 'utf8');
  let attempt = 0;

  const server = http.createServer((req, res) => {
    attempt++;
    const range = req.headers.range;

    if (range) {
      // Second attempt: honour the range so the client can append.
      const start = Number(range.replace(/bytes=(\d+)-.*/, '$1'));
      const slice = body.subarray(start);
      res.writeHead(206, {
        'Content-Type': 'video/mp4',
        'Content-Length': slice.length,
        'Content-Range': `bytes ${start}-${body.length - 1}/${body.length}`
      });
      res.end(slice);
      return;
    }

    // First attempt: promise the whole file, then cut the connection halfway.
    res.writeHead(200, { 'Content-Type': 'video/mp4', 'Content-Length': body.length });
    res.write(body.subarray(0, Math.floor(body.length / 2)));
    setTimeout(() => res.destroy(), 30);
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const outputPath = path.join(dir, 'Episode_01.mp4');

  await assert.rejects(
    downloadSingleVideo(`${origin}/video.mp4`, outputPath, { context: {}, backend: 'native' }),
    'the truncated transfer fails'
  );

  const partial = await fsp.stat(`${outputPath}.part`);
  assert.ok(partial.size > 0, 'partial progress is kept, not discarded');
  assert.ok(partial.size < body.length, 'and it really is partial');

  // Second run resumes rather than starting over.
  const result = await downloadSingleVideo(`${origin}/video.mp4`, outputPath, {
    context: {},
    backend: 'native'
  });

  assert.equal(result.skipped, false);
  assert.deepEqual(fs.readFileSync(outputPath), body, 'the resumed file matches the original exactly');
  assert.equal(fs.existsSync(`${outputPath}.part`), false, 'the staging file is cleaned up');
  assert.equal(attempt, 2, 'exactly one retry, using a range request');
});

test('force discards partial progress and starts over', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-force-'));
  t.after(async () => fsp.rm(dir, { recursive: true, force: true }));

  const outputPath = path.join(dir, 'Episode_01.mp4');
  await fsp.writeFile(outputPath, 'a finished file');
  await fsp.writeFile(`${outputPath}.part`, 'half a download');
  await fsp.mkdir(`${outputPath}.parts`, { recursive: true });
  await fsp.writeFile(path.join(`${outputPath}.parts`, '000000.part'), 'a segment');

  // Points at a host that refuses connections: we only care that the partial
  // state was cleared before any transfer was attempted.
  await assert.rejects(
    downloadSingleVideo('http://127.0.0.1:1/none.mp4', outputPath, { context: {}, backend: 'native', force: true })
  );

  assert.equal(fs.existsSync(`${outputPath}.part`), false, 'stale .part removed');
  assert.equal(fs.existsSync(`${outputPath}.parts`), false, 'stale segment directory removed');
  assert.equal(fs.existsSync(outputPath), false, 'the previous output is gone');
});

test('a failed HLS episode keeps its segments for the next attempt', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-hlspart-'));
  const good = Buffer.from('SEGMENT-OK-'.repeat(100), 'utf8');

  const server = http.createServer((req, res) => {
    if (req.url === '/v/index.m3u8') {
      res.writeHead(200, { 'Content-Type': 'application/vnd.apple.mpegurl' });
      res.end('#EXTM3U\n#EXTINF:4,\nseg0.ts\n#EXTINF:4,\nseg1.ts\n#EXT-X-ENDLIST\n');
      return;
    }
    if (req.url === '/v/seg0.ts') {
      res.writeHead(200).end(good);
      return;
    }
    // The second segment is permanently broken, so the episode fails.
    res.writeHead(500).end('nope');
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const outputPath = path.join(dir, 'Episode_01.mp4');
  await assert.rejects(
    downloadSingleVideo(`${origin}/v/index.m3u8`, outputPath, { context: {}, backend: 'native' })
  );

  const parts = fs.readdirSync(`${outputPath}.parts`);
  assert.ok(parts.includes('000000.part'), 'the segment that succeeded is still on disk');
  assert.equal(fs.existsSync(`${outputPath}.temp.ts`), false, 'the half-built container is discarded');
});

test('downloadDramaEpisodes writes a full series into downloads/<title>/', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-series-'));
  const plaintext = Array.from({ length: SEGMENT_COUNT }, (_, i) => Buffer.from(`S${i}`.repeat(100), 'utf8'));
  const cdn = await startFakeCdn(plaintext);

  t.after(async () => {
    await cdn.close();
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const episodes = Array.from({ length: 3 }, (_, i) => ({
    episode: i + 1,
    name: `Episode ${i + 1}`,
    uri: `${cdn.origin}/master.m3u8`
  }));

  const summary = await downloadDramaEpisodes(
    { dramaTitle: 'Found A Homeless Genius', episodes, requestContext: REQUEST_CONTEXT },
    { baseDir: dir, concurrency: 3 }
  );

  const folder = path.join(dir, 'Found A Homeless Genius');
  assert.equal(summary.failedCount, 0, JSON.stringify(summary.errors));
  assert.equal(summary.downloadedCount, 3);
  assert.deepEqual(
    fs.readdirSync(folder).sort(),
    ['Episode_01.mp4', 'Episode_02.mp4', 'Episode_03.mp4', 'dramadown.json', 'uris.txt']
  );

  // The series record travels with the downloads.
  const record = JSON.parse(fs.readFileSync(path.join(folder, 'dramadown.json'), 'utf8'));
  assert.equal(record.downloadedEpisodes, 3);
  assert.equal(record.dramaTitle, 'Found A Homeless Genius');

  // Re-running resumes rather than re-downloading.
  const second = await downloadDramaEpisodes(
    { dramaTitle: 'Found A Homeless Genius', episodes, requestContext: REQUEST_CONTEXT },
    { baseDir: dir, concurrency: 3 }
  );
  assert.equal(second.skippedCount, 3);
  assert.equal(second.freshCount, 0);
});

test('an S3 error page is never written as a video file', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-s3-'));

  // Exactly what these CDNs answer with when a link has expired: S3 masks a
  // missing object as AccessDenied naming s3:ListBucket.
  const errorBody =
    '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code>' +
    '<Message>User: arn:aws:iam::481617506592:user/s3_video_akm_cdn_ro is not authorized to perform: ' +
    's3:ListBucket on resource: "arn:aws:s3:::hwyc-dramabox-video-online" because no identity-based ' +
    'policy allows the s3:ListBucket action</Message></Error>';

  const server = http.createServer((req, res) => {
    // Served with 200 so it slips past a status check.
    res.writeHead(200, { 'Content-Type': 'application/xml', 'Content-Length': errorBody.length });
    res.end(errorBody);
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const outputPath = path.join(dir, 'Episode_01.mp4');

  await assert.rejects(
    downloadSingleVideo(`${origin}/gone.mp4`, outputPath, { context: {}, backend: 'native' }),
    /expired or the file was removed/
  );

  assert.equal(fs.existsSync(outputPath), false, 'no .mp4 full of XML is left behind');
});

test('a dead source falls back to the next quality URL', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-alt-'));
  const good = Buffer.from('WORKING-FALLBACK-SOURCE-'.repeat(500), 'utf8');
  const hits = [];

  const server = http.createServer((req, res) => {
    hits.push(req.url);
    if (req.url === '/1080p.mp4') {
      res.writeHead(200, { 'Content-Type': 'application/xml' });
      res.end('<?xml version="1.0"?><Error><Code>AccessDenied</Code><Message>s3:ListBucket</Message></Error>');
      return;
    }
    res.writeHead(200, { 'Content-Type': 'video/mp4', 'Content-Length': good.length });
    res.end(good);
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const outputPath = path.join(dir, 'Episode_01.mp4');
  const result = await downloadSingleVideo(`${origin}/1080p.mp4`, outputPath, {
    context: {},
    backend: 'native',
    alternates: [`${origin}/720p.mp4`, `${origin}/540p.mp4`]
  });

  assert.equal(result.skipped, false);
  assert.deepEqual(fs.readFileSync(outputPath), good, 'the working source supplied the file');
  assert.deepEqual(hits, ['/1080p.mp4', '/720p.mp4'], 'stopped at the first source that worked');
});

test('falls back to another server when every known link is dead', async t => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-server-'));
  const good = Buffer.from('SERVER-3-DELIVERED-'.repeat(400), 'utf8');
  const hits = [];

  const server = http.createServer((req, res) => {
    hits.push(req.url);
    const url = new URL(req.url, 'http://127.0.0.1');

    // The site's own endpoint: only server 3 still has a live link.
    if (url.pathname === '/index.php' && url.searchParams.get('action') === 'get_video') {
      const sv = url.searchParams.get('sv');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(
        sv === '3'
          ? { ok: true, videoUrl: `http://127.0.0.1:${server.address().port}/live.mp4`,
              qualities: [{ quality: 'Server 3 1080p', url: `http://127.0.0.1:${server.address().port}/live.mp4` }] }
          : { ok: false, error: 'Video unavailable' }
      ));
      return;
    }

    if (url.pathname === '/live.mp4') {
      res.writeHead(200, { 'Content-Type': 'video/mp4', 'Content-Length': good.length });
      res.end(good);
      return;
    }

    // Every originally-extracted link is expired.
    res.writeHead(200, { 'Content-Type': 'application/xml' });
    res.end('<?xml version="1.0"?><Error><Code>AccessDenied</Code><Message>s3:ListBucket</Message></Error>');
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await fsp.rm(dir, { recursive: true, force: true });
  });

  const outputPath = path.join(dir, 'Episode_01.mp4');
  const stages = [];

  const result = await downloadSingleVideo(`${origin}/dead-1080p.mp4`, outputPath, {
    context: {},
    backend: 'native',
    alternates: [`${origin}/dead-720p.mp4`],
    refreshEndpoint: `${origin}/index.php?action=get_video&id=42&ep=1&lang=en&sv=1`,
    onEpisodeProgress: p => p.stage && stages.push(p.stage)
  });

  assert.equal(result.skipped, false);
  assert.deepEqual(fs.readFileSync(outputPath), good, 'the live server supplied the episode');
  assert.ok(stages.includes('server_refresh'), 'it announced going back for another server');

  // sv=1 is skipped: those are the links that just failed.
  const asked = hits.filter(h => h.includes('action=get_video')).map(h => new URL(h, 'http://x').searchParams.get('sv'));
  assert.ok(!asked.includes('1'), 'did not re-ask the server whose links already failed');
  assert.ok(asked.includes('3'), 'reached the server that still had a link');
});
