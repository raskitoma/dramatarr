'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { before, after } = require('node:test');
let extracted = 0;
let downloaded = 0;
let extractError;
require.cache[require.resolve('../src/extractor')] = { exports: {
  extractDramaVideoUris: async (url, options) => {
    extracted++;
    if (extractError) throw extractError;
    assert.equal(url, 'https://dramafren.org/watch/test');
    assert.equal(options.signal.aborted, false);
    return { dramaTitle: 'A Show', episodes: [{ episode: 1, uri: 'private-signed-url' }], requestContext: { cookies: 'secret' } };
  }
}};
require.cache[require.resolve('../src/downloader')] = { exports: {
  downloadDramaEpisodes: async (result, options) => {
    downloaded++;
    assert.equal(options.force, false);
    assert.equal(options.concurrency, 3);
    assert.match(options.baseDir, /a{32}$/);
    assert.equal(result.requestContext.cookies, 'secret');
    return { cleanTitle: 'A Show', failedCount: 0, downloadedCount: 1 };
  }
}};
process.env.DRAMATARR_WORKER_KEY = 'test-key';
const { server } = require('../server');
let origin;
before(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
});
after(async () => { await new Promise(resolve => server.close(resolve)); });
const body = { url: 'https://dramafren.org/watch/test', jobId: 'a'.repeat(32) };
async function post(payload, key) {
  return fetch(`${origin}/api/acquire`, { method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(key ? { 'X-Worker-Key': key } : {}) },
    body: JSON.stringify(payload) });
}
test('internal acquisition requires a worker credential before extracting', async () => {
  assert.equal((await post(body)).status, 401);
  assert.equal((await post(body, 'wrong-key')).status, 401);
  assert.equal(extracted, 0);
});
test('rejects filesystem traversal and unexpected sources before extracting', async () => {
  assert.equal((await post({ ...body, jobId: '../escape' }, 'test-key')).status, 400);
  assert.equal((await post({ ...body, url: 'https://localhost/' }, 'test-key')).status, 400);
  assert.equal(extracted, 0);
});
test('health stays reachable but legacy API cannot bypass the worker credential', async () => {
  assert.equal((await fetch(`${origin}/api/health`)).status, 200);
  assert.equal((await fetch(`${origin}/api/library`)).status, 401);
});
test('extracts and downloads under a stable job folder without disclosing solved credentials', async () => {
  const response = await post(body, 'test-key');
  assert.equal(response.status, 200);
  const events = await response.text();
  assert.match(events, /"kind":"complete"/);
  assert.match(events, /"downloadedCount":1/);
  assert.doesNotMatch(events, /private-signed-url|cookies|secret/);
  assert.equal(extracted, 1);
  assert.equal(downloaded, 1);
});


test('reports provider HTTP failures without exposing signed links', async () => {
  extractError = new Error('Provider returned HTTP 521 https://example.com/?signature=private');
  try {
    const events = await (await post(body, 'test-key')).text();
    assert.match(events, /"phase":"extracting","reason":"provider_http","httpStatus":521/);
    assert.doesNotMatch(events, /signature|private/);
  } finally {
    extractError = undefined;
  }
});
