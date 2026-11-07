'use strict';

/**
 * A small, dependency-free HLS (RFC 8216) playlist parser.
 *
 * Deliberately pure so it can be unit tested without network access. It covers
 * the parts these drama CDNs actually use: master playlists with variants,
 * media playlists, AES-128 encryption, fMP4 init segments and byte ranges.
 */

/**
 * Parses an HLS attribute list (`KEY=VALUE,KEY="quoted,value"`).
 * @param {string} input
 * @returns {Record<string, string>}
 */
function parseAttributes(input) {
  const attributes = {};
  const pattern = /([A-Za-z0-9_-]+)=("[^"]*"|[^,]*)/g;
  let match;

  while ((match = pattern.exec(input)) !== null) {
    let value = match[2].trim();
    if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
    attributes[match[1].toUpperCase()] = value;
  }

  return attributes;
}

/**
 * Detects whether a payload is an HLS playlist rather than a media file.
 * @param {string} text - The first bytes of the response, decoded as UTF-8
 * @param {string} [contentType]
 * @param {string|URL} [url]
 * @returns {boolean}
 */
function isHlsPlaylist(text, contentType = '', url = '') {
  if (typeof text === 'string' && text.trimStart().startsWith('#EXTM3U')) return true;
  if (/mpegurl|x-mpegurl/i.test(contentType)) return true;
  try {
    const pathname = (url instanceof URL ? url : new URL(String(url))).pathname;
    return /\.m3u8?$/i.test(pathname);
  } catch (err) {
    return false;
  }
}

/**
 * Resolves a possibly relative playlist URI against the playlist's own URL.
 * @param {string} uri
 * @param {string|URL} baseUrl
 * @returns {string}
 */
function resolveUri(uri, baseUrl) {
  try {
    return new URL(uri, baseUrl).href;
  } catch (err) {
    return uri;
  }
}

/**
 * `#EXT-X-BYTERANGE:<length>[@<offset>]`
 * @param {string} value
 * @param {number} previousEnd - Byte after the previous sub-range of the same resource
 * @returns {{offset: number, length: number}|null}
 */
function parseByteRange(value, previousEnd = 0) {
  if (!value) return null;
  const [lengthPart, offsetPart] = String(value).split('@');
  const length = Number(lengthPart);
  if (!Number.isFinite(length) || length <= 0) return null;
  const offset = offsetPart !== undefined ? Number(offsetPart) : previousEnd;
  if (!Number.isFinite(offset) || offset < 0) return null;
  return { offset, length };
}

/**
 * Builds the 16-byte AES initialisation vector for a segment. An explicit
 * `IV=0x...` wins; otherwise the media sequence number is used, per spec.
 * @param {string|undefined} explicitIv
 * @param {number} sequence
 * @returns {Buffer}
 */
function resolveIv(explicitIv, sequence) {
  if (explicitIv) {
    const hex = explicitIv.replace(/^0[xX]/, '').padStart(32, '0');
    const buffer = Buffer.from(hex, 'hex');
    if (buffer.length === 16) return buffer;
  }

  const iv = Buffer.alloc(16);
  iv.writeUInt32BE(Math.floor(sequence / 2 ** 96) >>> 0, 0);
  iv.writeUInt32BE(Math.floor(sequence / 2 ** 64) >>> 0, 4);
  iv.writeUInt32BE(Math.floor(sequence / 2 ** 32) >>> 0, 8);
  iv.writeUInt32BE(sequence >>> 0, 12);
  return iv;
}

/**
 * Parses a master or media playlist.
 * @param {string} text
 * @param {string|URL} baseUrl - Absolute URL the playlist was fetched from
 * @returns {{type: 'master'|'media', variants: Array, segments: Array, initSegment: object|null, mediaSequence: number, targetDuration: number, isLive: boolean}}
 */
function parsePlaylist(text, baseUrl) {
  const lines = String(text)
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line.length > 0);

  if (lines.some(line => line.startsWith('#EXT-X-STREAM-INF'))) {
    return { type: 'master', variants: parseVariants(lines, baseUrl), segments: [], initSegment: null, mediaSequence: 0, targetDuration: 0, isLive: false };
  }

  let mediaSequence = 0;
  let targetDuration = 0;
  let hasEndList = false;
  let currentKey = null;
  let pendingByteRange = null;
  let previousRangeEnd = 0;
  let initSegment = null;
  let duration = 0;

  const segments = [];

  for (const line of lines) {
    if (line.startsWith('#')) {
      if (line.startsWith('#EXT-X-MEDIA-SEQUENCE:')) {
        mediaSequence = Number(line.slice(22)) || 0;
      } else if (line.startsWith('#EXT-X-TARGETDURATION:')) {
        targetDuration = Number(line.slice(22)) || 0;
      } else if (line.startsWith('#EXTINF:')) {
        duration = parseFloat(line.slice(8)) || 0;
      } else if (line.startsWith('#EXT-X-BYTERANGE:')) {
        pendingByteRange = parseByteRange(line.slice(17), previousRangeEnd);
      } else if (line.startsWith('#EXT-X-KEY:')) {
        const attributes = parseAttributes(line.slice(11));
        const method = (attributes.METHOD || 'NONE').toUpperCase();
        currentKey =
          method === 'NONE'
            ? null
            : { method, uri: attributes.URI ? resolveUri(attributes.URI, baseUrl) : null, iv: attributes.IV || null };
      } else if (line.startsWith('#EXT-X-MAP:')) {
        const attributes = parseAttributes(line.slice(11));
        if (attributes.URI) {
          initSegment = {
            uri: resolveUri(attributes.URI, baseUrl),
            byteRange: parseByteRange(attributes.BYTERANGE, 0),
            key: currentKey,
            sequence: mediaSequence,
            duration: 0,
            isInit: true
          };
        }
      } else if (line === '#EXT-X-ENDLIST') {
        hasEndList = true;
      }
      continue;
    }

    const uri = resolveUri(line, baseUrl);
    segments.push({
      uri,
      byteRange: pendingByteRange,
      key: currentKey,
      sequence: mediaSequence + segments.length,
      duration,
      isInit: false
    });

    previousRangeEnd = pendingByteRange ? pendingByteRange.offset + pendingByteRange.length : 0;
    pendingByteRange = null;
    duration = 0;
  }

  return {
    type: 'media',
    variants: [],
    segments,
    initSegment,
    mediaSequence,
    targetDuration,
    isLive: !hasEndList
  };
}

/**
 * @param {string[]} lines
 * @param {string|URL} baseUrl
 * @returns {Array<{uri: string, bandwidth: number, resolution: string|null, codecs: string|null, height: number}>}
 */
function parseVariants(lines, baseUrl) {
  const variants = [];

  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].startsWith('#EXT-X-STREAM-INF')) continue;

    const attributes = parseAttributes(lines[i].slice(lines[i].indexOf(':') + 1));

    // The variant URI is the next line that is not a tag.
    let uriLine = null;
    for (let j = i + 1; j < lines.length; j++) {
      if (!lines[j].startsWith('#')) {
        uriLine = lines[j];
        break;
      }
    }
    if (!uriLine) continue;

    const resolution = attributes.RESOLUTION || null;
    variants.push({
      uri: resolveUri(uriLine, baseUrl),
      bandwidth: Number(attributes['AVERAGE-BANDWIDTH'] || attributes.BANDWIDTH) || 0,
      resolution,
      codecs: attributes.CODECS || null,
      height: resolution ? Number(String(resolution).split('x')[1]) || 0 : 0
    });
  }

  return variants;
}

/**
 * Picks the highest-quality variant, preferring vertical resolution and falling
 * back to declared bandwidth.
 * @param {Array} variants
 * @returns {object|null}
 */
function selectBestVariant(variants) {
  if (!Array.isArray(variants) || variants.length === 0) return null;
  return variants
    .slice()
    .sort((a, b) => b.height - a.height || b.bandwidth - a.bandwidth)[0];
}

module.exports = {
  isHlsPlaylist,
  parseAttributes,
  parseByteRange,
  parsePlaylist,
  resolveIv,
  resolveUri,
  selectBestVariant
};
