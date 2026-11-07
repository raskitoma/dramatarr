'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { selectBestQuality } = require('../src/extractor');

test('decodes reelshort base64 quality entries', () => {
  // hash64 is how the reelshort template hides its playlist URLs.
  const encode = value => Buffer.from(value, 'utf8').toString('base64');
  const sd = 'https://v-mps.example.com/vod/abc-sd.m3u8';
  const hd = 'https://v-mps.example.com/vod/abc-hd.m3u8';

  assert.equal(
    selectBestQuality([
      { quality: '480p', hash64: encode(sd) },
      { quality: '1080p', hash64: encode(hd) }
    ]),
    hd
  );
});

test('picks the tallest of a dramabox quality list', () => {
  // Labels arrive as "Server 1 720p" from that template.
  const chosen = selectBestQuality([
    { quality: 'Server 1 720p', url: 'https://cdn.example.com/a.720p.mp4' },
    { quality: 'Server 1 1080p', url: 'https://cdn.example.com/a.1080p.mp4' },
    { quality: 'Server 1 540p', url: 'https://cdn.example.com/a.540p.mp4' }
  ]);

  assert.equal(chosen, 'https://cdn.example.com/a.1080p.mp4');
});

test('prefers a playlist when heights are equal or unlabelled', () => {
  const chosen = selectBestQuality([
    { quality: '0p', url: 'https://cdn.example.com/plain.mp4' },
    { quality: '0p', url: 'https://cdn.example.com/master.m3u8' }
  ]);

  assert.equal(chosen, 'https://cdn.example.com/master.m3u8');
});

test('ignores entries that are not usable URLs', () => {
  assert.equal(selectBestQuality([]), null);
  assert.equal(selectBestQuality(undefined), null);
  assert.equal(selectBestQuality([{ quality: '720p' }]), null, 'no url and no hash64');
  assert.equal(selectBestQuality([{ quality: '720p', hash64: 'not-valid-base64!!!' }]), null);
  assert.equal(
    selectBestQuality([{ quality: '720p', url: 'javascript:alert(1)' }]),
    null,
    'only http(s) is accepted'
  );
});

test('a single entry is returned as-is', () => {
  assert.equal(
    selectBestQuality([{ quality: '720p', url: 'https://cdn.example.com/only.m3u8' }]),
    'https://cdn.example.com/only.m3u8'
  );
});
