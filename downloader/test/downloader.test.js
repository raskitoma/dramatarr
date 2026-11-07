'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const { concatParts, decryptSegment, downloadDramaEpisodes, sanitizeTitle } = require('../src/downloader');

test('sanitizeTitle strips path separators and dangerous names', () => {
  assert.equal(sanitizeTitle('Found A Homeless Genius'), 'Found A Homeless Genius');
  assert.equal(sanitizeTitle('a/b\\c:d*e?f"g<h>i|j'), 'a_b_c_d_e_f_g_h_i_j');
  assert.equal(sanitizeTitle('  spaced   out  '), 'spaced out');
  assert.equal(sanitizeTitle('Drama' + String.fromCharCode(0) + 'Title'), 'Drama_Title', 'control characters are replaced');
  assert.equal(sanitizeTitle('..'), 'Drama_Series');
  assert.equal(sanitizeTitle('../../etc/passwd'), '_.._etc_passwd', 'leading dots are stripped');
  assert.equal(sanitizeTitle(''), 'Drama_Series');
  assert.equal(sanitizeTitle(null), 'Drama_Series');
  assert.equal(sanitizeTitle('CON'), 'Drama_Series');
});

test('sanitizeTitle never produces a name that escapes its parent directory', () => {
  for (const evil of ['../../etc', '..\\..\\windows', '/etc/passwd', '....//....//x']) {
    const clean = sanitizeTitle(evil);
    const resolved = path.resolve('/downloads', clean);
    assert.ok(resolved.startsWith(`${path.resolve('/downloads')}${path.sep}`), `${evil} -> ${clean} escaped`);
  }
});

test('sanitizeTitle keeps names within filesystem limits', () => {
  const clean = sanitizeTitle('あ'.repeat(400));
  assert.ok(Buffer.byteLength(clean, 'utf8') <= 180);
  assert.ok(clean.length > 0);
});

test('decryptSegment round-trips AES-128-CBC', () => {
  const key = crypto.randomBytes(16);
  const iv = crypto.randomBytes(16);
  const plaintext = Buffer.from('G'.repeat(1000), 'utf8');

  const cipher = crypto.createCipheriv('aes-128-cbc', key, iv);
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()]);

  assert.deepEqual(decryptSegment(encrypted, key, iv), plaintext);
});

test('decryptSegment tolerates segments without PKCS#7 padding', () => {
  const key = crypto.randomBytes(16);
  const iv = crypto.randomBytes(16);
  const plaintext = crypto.randomBytes(64); // exact block multiple, encoder omits padding

  const cipher = crypto.createCipheriv('aes-128-cbc', key, iv);
  cipher.setAutoPadding(false);
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()]);

  const decrypted = decryptSegment(encrypted, key, iv);
  assert.ok(decrypted.length >= 48, 'decryption produced output rather than throwing');
});

test('concatParts joins part files in order', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-'));
  try {
    const parts = [];
    for (let i = 0; i < 5; i++) {
      const partPath = path.join(dir, `${i}.part`);
      await fsp.writeFile(partPath, `chunk${i}|`);
      parts.push(partPath);
    }

    const destination = path.join(dir, 'joined.ts');
    const bytes = await concatParts(parts, destination);

    assert.equal(fs.readFileSync(destination, 'utf8'), 'chunk0|chunk1|chunk2|chunk3|chunk4|');
    assert.equal(bytes, fs.statSync(destination).size);
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});

test('downloadDramaEpisodes handles a drama with no usable URIs', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-'));
  try {
    const summary = await downloadDramaEpisodes(
      { dramaTitle: 'Empty Show', episodes: [{ episode: 1, uri: null, error: 'Video not available' }] },
      { baseDir: dir }
    );

    assert.equal(summary.total, 0);
    assert.equal(summary.failedCount, 0);
    assert.equal(fs.existsSync(path.join(dir, 'Empty Show')), true);
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});

test('downloadDramaEpisodes refuses private-host URIs instead of fetching them', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-'));
  try {
    const summary = await downloadDramaEpisodes(
      { dramaTitle: 'SSRF Probe', episodes: [{ episode: 1, uri: 'http://127.0.0.1:22/' }] },
      { baseDir: dir }
    );

    assert.equal(summary.failedCount, 1);
    assert.match(summary.errors[0].error, /private\/loopback/);
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});

test('downloadDramaEpisodes skips episodes whose file already exists', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-'));
  try {
    const showDir = path.join(dir, 'Resume Show');
    await fsp.mkdir(showDir, { recursive: true });
    // A pre-existing file means the URI is never fetched — which is what makes
    // this assertion safe to run offline.
    await fsp.writeFile(path.join(showDir, 'Episode_01.mp4'), 'already downloaded');

    const summary = await downloadDramaEpisodes(
      { dramaTitle: 'Resume Show', episodes: [{ episode: 1, uri: 'https://cdn.example.com/ep1.m3u8' }] },
      { baseDir: dir }
    );

    assert.equal(summary.skippedCount, 1);
    assert.equal(summary.failedCount, 0);
    assert.equal(summary.downloadedCount, 1);
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});

test('a partial run keeps the full series in uris.txt', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-'));
  try {
    const all = Array.from({ length: 10 }, (_, i) => ({
      episode: i + 1,
      uri: `https://cdn.example.com/ep${i + 1}.m3u8`
    }));

    // Downloading only episode 5 must not shrink the manifest to one line.
    await downloadDramaEpisodes(
      { dramaTitle: 'Manifest Show', episodes: [all[4]] },
      { baseDir: dir, manifestEpisodes: all, signal: AbortSignal.abort() }
    );

    const lines = fs
      .readFileSync(path.join(dir, 'Manifest Show', 'uris.txt'), 'utf8')
      .trim()
      .split('\n');

    assert.equal(lines.length, 10, 'every episode is still listed');
    assert.equal(lines[9], 'https://cdn.example.com/ep10.m3u8');
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});

test('downloadDramaEpisodes writes uris.txt before any network work', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-'));
  try {
    const episodes = Array.from({ length: 120 }, (_, i) => ({
      episode: i + 1,
      uri: `https://cdn.example.com/ep${i + 1}.m3u8`
    }));

    await downloadDramaEpisodes(
      { dramaTitle: 'Padded Show', episodes },
      { baseDir: dir, concurrency: 1, signal: AbortSignal.abort() }
    );

    const uris = fs.readFileSync(path.join(dir, 'Padded Show', 'uris.txt'), 'utf8').trim().split('\n');
    assert.equal(uris.length, 120);
    assert.equal(uris[0], 'https://cdn.example.com/ep1.m3u8');
    assert.equal(uris[119], 'https://cdn.example.com/ep120.m3u8');
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});

test('records the source URL and completeness beside the downloads', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-meta-'));
  try {
    const showDir = path.join(dir, 'Tracked Show');
    await fsp.mkdir(showDir, { recursive: true });
    await fsp.writeFile(path.join(showDir, 'Episode_01.mp4'), 'one');
    await fsp.writeFile(path.join(showDir, 'Episode_02.mp4'), 'two');

    const episodes = Array.from({ length: 12 }, (_, i) => ({
      episode: i + 1,
      uri: `https://cdn.example.com/ep${i + 1}.m3u8`
    }));

    await downloadDramaEpisodes(
      {
        dramaTitle: 'Tracked Show',
        sourceUrl: 'https://dramabox.dramafren.org/index.php?page=watch&id=99&ep=1',
        totalEpisodes: 12,
        strategy: 'video-endpoint',
        episodes
      },
      { baseDir: dir, signal: AbortSignal.abort() }
    );

    const record = JSON.parse(fs.readFileSync(path.join(showDir, 'dramadown.json'), 'utf8'));

    assert.equal(record.sourceUrl, 'https://dramabox.dramafren.org/index.php?page=watch&id=99&ep=1');
    assert.equal(record.totalEpisodes, 12, 'the series total is what the site advertised');
    assert.equal(record.downloadedEpisodes, 2, 'counted from the files actually present');
    assert.equal(record.missingEpisodes, 10);
    assert.equal(record.strategy, 'video-endpoint');
    assert.ok(record.firstDownloadedAt && record.lastUpdatedAt);
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});

test('a later partial run does not erase the recorded source or total', async () => {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'dramadown-meta2-'));
  try {
    const showDir = path.join(dir, 'Tracked Show');
    await fsp.mkdir(showDir, { recursive: true });
    await fsp.writeFile(
      path.join(showDir, 'dramadown.json'),
      JSON.stringify({
        dramaTitle: 'Tracked Show',
        sourceUrl: 'https://dramabox.dramafren.org/original',
        totalEpisodes: 40,
        downloadedEpisodes: 39,
        firstDownloadedAt: '2020-01-01T00:00:00.000Z'
      })
    );
    await fsp.writeFile(path.join(showDir, 'Episode_07.mp4'), 'seven');

    // Downloading a single episode must not shrink the series facts.
    await downloadDramaEpisodes(
      { dramaTitle: 'Tracked Show', episodes: [{ episode: 7, uri: 'https://cdn.example.com/ep7.m3u8' }] },
      { baseDir: dir, signal: AbortSignal.abort() }
    );

    const record = JSON.parse(fs.readFileSync(path.join(showDir, 'dramadown.json'), 'utf8'));

    assert.equal(record.sourceUrl, 'https://dramabox.dramafren.org/original', 'source survives');
    assert.equal(record.totalEpisodes, 40, 'series total survives');
    assert.equal(record.firstDownloadedAt, '2020-01-01T00:00:00.000Z', 'first-seen timestamp survives');
    assert.equal(record.downloadedEpisodes, 1, 'but the on-disk count is recomputed');
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
});
