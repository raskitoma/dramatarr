'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { after, before } = require('node:test');

const { server } = require('../server');

let origin;

before(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  origin = `http://127.0.0.1:${port}`;
});

after(async () => {
  await new Promise(resolve => server.close(resolve));
});

test('serves the dashboard', async () => {
  const response = await fetch(`${origin}/`);
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /text\/html/);
  assert.match(await response.text(), /DramaDown/);
});

test('exposes a health endpoint', async () => {
  const response = await fetch(`${origin}/api/health`);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).status, 'ok');
});

test('does not advertise wildcard CORS', async () => {
  const response = await fetch(`${origin}/api/health`);
  assert.equal(response.headers.get('access-control-allow-origin'), null);
});

test('rejects API calls initiated by another site', async () => {
  const extract = await fetch(`${origin}/api/extract-stream?url=https://example.com/watch`, {
    headers: { Origin: 'https://evil.example' }
  });
  assert.equal(extract.status, 403);

  const download = await fetch(`${origin}/api/download-stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: 'https://evil.example' },
    body: JSON.stringify({ sessionId: 'whatever' })
  });
  assert.equal(download.status, 403);
});

test('rejects cross-site requests flagged by Sec-Fetch-Site', async () => {
  const response = await fetch(`${origin}/api/health`, {
    headers: { 'Sec-Fetch-Site': 'cross-site' }
  });
  assert.equal(response.status, 403);
});

test('requires a url parameter for extraction', async () => {
  const response = await fetch(`${origin}/api/extract-stream`);
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /URL parameter/);
});

test('download endpoint only accepts a known session id', async () => {
  const response = await fetch(`${origin}/api/download-stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId: 'does-not-exist' })
  });

  assert.equal(response.status, 404);
  assert.match((await response.json()).error, /expired extraction session/);
});

test('download endpoint ignores client-supplied episode URIs', async () => {
  // The old API took `episodes` straight from the request body, which let any
  // page make the server fetch arbitrary URLs and write them to disk.
  const response = await fetch(`${origin}/api/download-stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      dramaTitle: 'Injected',
      episodes: [{ episode: 1, uri: 'http://169.254.169.254/latest/meta-data/' }]
    })
  });

  assert.equal(response.status, 404, 'no session id means nothing is fetched');
});

test('rejects malformed JSON bodies', async () => {
  const response = await fetch(`${origin}/api/extract`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{not json'
  });

  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /valid JSON/);
});

test('static serving cannot escape the public directory', async () => {
  for (const attempt of ['/../server.js', '/%2e%2e/server.js', '/..%2fserver.js', '/....//server.js']) {
    const response = await fetch(`${origin}${attempt}`, { redirect: 'manual' });
    assert.ok(
      response.status === 403 || response.status === 404,
      `${attempt} returned ${response.status}`
    );
    const body = await response.text();
    assert.doesNotMatch(body, /require\('\.\/src\/extractor'\)/, `${attempt} leaked server source`);
  }
});

test('static assets revalidate instead of being cached blind', async () => {
  // Without this a redeploy leaves browsers running the previous app.js against
  // the new index.html — which looks like missing UI rather than a stale cache.
  const response = await fetch(`${origin}/app.js`);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-cache');

  const etag = response.headers.get('etag');
  assert.ok(etag, 'a validator is sent');

  const revalidated = await fetch(`${origin}/app.js`, { headers: { 'If-None-Match': etag } });
  assert.equal(revalidated.status, 304, 'an unchanged asset is answered with 304');
});

test('a versioned asset URL still resolves', async () => {
  const response = await fetch(`${origin}/app.js?v=7`);
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /javascript/);
});

test('library lists the downloads directory', async () => {
  const response = await fetch(`${origin}/api/library`);
  assert.equal(response.status, 200);

  const body = await response.json();
  assert.ok(typeof body.folder === 'string');
  assert.ok(Array.isArray(body.series));
});

test('library file listing refuses to escape the downloads directory', async () => {
  for (const attempt of ['../', '..', '../../etc', 'a/b', 'a\\b', '']) {
    const response = await fetch(`${origin}/api/library/files?folder=${encodeURIComponent(attempt)}`);
    assert.ok(
      response.status === 400 || response.status === 404,
      `folder=${JSON.stringify(attempt)} returned ${response.status}`
    );
    const body = await response.json();
    assert.ok(body.error, 'an error is reported rather than a listing');
  }
});

test('selecting episodes still requires a known session', async () => {
  const response = await fetch(`${origin}/api/download-stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId: 'nope', episodes: [1, 2, 3] })
  });

  assert.equal(response.status, 404);
});

test('unknown API routes 404 rather than falling through to static', async () => {
  const response = await fetch(`${origin}/api/nope`);
  assert.equal(response.status, 404);
  assert.match(response.headers.get('content-type'), /application\/json/);
});
