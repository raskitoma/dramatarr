'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { isHlsPlaylist, parseAttributes, parseByteRange, parsePlaylist, resolveIv, selectBestVariant } = require('../src/hls');

const BASE = 'https://cdn.example.com/media/drama/index.m3u8';

test('detects playlists by body, content-type and extension', () => {
  assert.equal(isHlsPlaylist('#EXTM3U\n#EXT-X-VERSION:3', '', ''), true);
  assert.equal(isHlsPlaylist('binary junk', 'application/vnd.apple.mpegurl', ''), true);
  assert.equal(isHlsPlaylist('binary junk', 'video/mp4', 'https://cdn.example.com/a/b.m3u8'), true);
  assert.equal(isHlsPlaylist('binary junk', 'video/mp4', 'https://cdn.example.com/a/b.mp4'), false);
});

test('parses quoted and unquoted attribute lists', () => {
  const attributes = parseAttributes('METHOD=AES-128,URI="https://k.example/key?a=1,2",IV=0xABCD');
  assert.equal(attributes.METHOD, 'AES-128');
  assert.equal(attributes.URI, 'https://k.example/key?a=1,2');
  assert.equal(attributes.IV, '0xABCD');
});

test('parses byte ranges with and without an explicit offset', () => {
  assert.deepEqual(parseByteRange('1024@512'), { offset: 512, length: 1024 });
  assert.deepEqual(parseByteRange('1024', 4096), { offset: 4096, length: 1024 });
  assert.equal(parseByteRange(''), null);
});

test('selects the highest-resolution variant of a master playlist', () => {
  const master = [
    '#EXTM3U',
    '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=480x854',
    '480p/index.m3u8',
    '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1080x1920',
    '1080p/index.m3u8',
    '#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=720x1280',
    'https://other.example.com/720p/index.m3u8'
  ].join('\n');

  const playlist = parsePlaylist(master, BASE);
  assert.equal(playlist.type, 'master');
  assert.equal(playlist.variants.length, 3);

  const best = selectBestVariant(playlist.variants);
  assert.equal(best.uri, 'https://cdn.example.com/media/drama/1080p/index.m3u8');
  assert.equal(best.height, 1920);
});

test('resolves relative segment URIs against the playlist URL', () => {
  const media = ['#EXTM3U', '#EXT-X-TARGETDURATION:10', '#EXTINF:9.9,', 'seg0.ts', '#EXTINF:9.9,', '../shared/seg1.ts', '#EXT-X-ENDLIST'].join('\n');

  const playlist = parsePlaylist(media, BASE);
  assert.equal(playlist.type, 'media');
  assert.equal(playlist.isLive, false);
  assert.deepEqual(
    playlist.segments.map(segment => segment.uri),
    ['https://cdn.example.com/media/drama/seg0.ts', 'https://cdn.example.com/media/shared/seg1.ts']
  );
});

test('tracks rotating AES-128 keys and clears them on METHOD=NONE', () => {
  const media = [
    '#EXTM3U',
    '#EXT-X-MEDIA-SEQUENCE:10',
    '#EXT-X-KEY:METHOD=AES-128,URI="key1.bin"',
    '#EXTINF:4,',
    'a.ts',
    '#EXT-X-KEY:METHOD=AES-128,URI="key2.bin",IV=0x0123456789ABCDEF0123456789ABCDEF',
    '#EXTINF:4,',
    'b.ts',
    '#EXT-X-KEY:METHOD=NONE',
    '#EXTINF:4,',
    'c.ts',
    '#EXT-X-ENDLIST'
  ].join('\n');

  const { segments } = parsePlaylist(media, BASE);

  assert.equal(segments[0].key.uri, 'https://cdn.example.com/media/drama/key1.bin');
  assert.equal(segments[0].key.iv, null);
  assert.equal(segments[0].sequence, 10, 'sequence starts at EXT-X-MEDIA-SEQUENCE');

  assert.equal(segments[1].key.uri, 'https://cdn.example.com/media/drama/key2.bin');
  assert.equal(segments[1].key.iv, '0x0123456789ABCDEF0123456789ABCDEF');
  assert.equal(segments[1].sequence, 11);

  assert.equal(segments[2].key, null, 'METHOD=NONE clears encryption');
});

test('captures fMP4 init segments and chained byte ranges', () => {
  const media = [
    '#EXTM3U',
    '#EXT-X-MAP:URI="init.mp4"',
    '#EXTINF:4,',
    '#EXT-X-BYTERANGE:1000@0',
    'chunk.mp4',
    '#EXTINF:4,',
    '#EXT-X-BYTERANGE:500',
    'chunk.mp4',
    '#EXT-X-ENDLIST'
  ].join('\n');

  const { initSegment, segments } = parsePlaylist(media, BASE);

  assert.equal(initSegment.uri, 'https://cdn.example.com/media/drama/init.mp4');
  assert.equal(initSegment.isInit, true);
  assert.deepEqual(segments[0].byteRange, { offset: 0, length: 1000 });
  assert.deepEqual(segments[1].byteRange, { offset: 1000, length: 500 }, 'offset continues from the previous range');
});

test('derives the IV from the media sequence when none is declared', () => {
  assert.equal(resolveIv(null, 1).toString('hex'), '00000000000000000000000000000001');
  assert.equal(resolveIv(null, 258).toString('hex'), '00000000000000000000000000000102');
  assert.equal(resolveIv('0x000102030405060708090a0b0c0d0e0f', 99).toString('hex'), '000102030405060708090a0b0c0d0e0f');
});

test('flags a playlist with no EXT-X-ENDLIST as live', () => {
  const { isLive } = parsePlaylist('#EXTM3U\n#EXTINF:4,\na.ts', BASE);
  assert.equal(isLive, true);
});
