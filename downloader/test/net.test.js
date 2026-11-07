'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { assertSafeUrl, buildRequestHeaders, cookieHeaderFor, isPrivateHost } = require('../src/net');

test('recognises loopback, RFC1918 and link-local hosts', () => {
  for (const host of ['localhost', '127.0.0.1', '10.1.2.3', '192.168.0.5', '172.16.9.9', '169.254.1.1', '::1', 'fe80::1', '[::ffff:127.0.0.1]', '[::ffff:7f00:1]', '[::ffff:192.168.1.1]', 'printer.local']) {
    assert.equal(isPrivateHost(host), true, `${host} should be private`);
  }

  for (const host of ['cdn.example.com', '8.8.8.8', '172.32.0.1', '[::ffff:8.8.8.8]', '[::ffff:808:808]', 'fcbarcelona.com', 'feed.example.org']) {
    assert.equal(isPrivateHost(host), false, `${host} should be public`);
  }
});

test('assertSafeUrl rejects non-HTTP schemes and private targets', () => {
  assert.throws(() => assertSafeUrl('file:///etc/passwd'), /non-HTTP/);
  assert.throws(() => assertSafeUrl('not a url'), /valid URL/);
  assert.throws(() => assertSafeUrl('http://127.0.0.1:3000/admin'), /private\/loopback/);

  assert.equal(assertSafeUrl('https://cdn.example.com/a.m3u8').host, 'cdn.example.com');
  assert.equal(assertSafeUrl('http://127.0.0.1/x', { allowPrivate: true }).hostname, '127.0.0.1');
});

test('cookies are scoped by domain, path and the secure flag', () => {
  const cookies = [
    { name: 'session', value: 'abc', domain: '.example.com', path: '/', secure: true },
    { name: 'admin', value: 'nope', domain: 'admin.example.com', path: '/', secure: false },
    { name: 'scoped', value: 'yes', domain: 'example.com', path: '/media', secure: false },
    { name: 'other', value: 'no', domain: 'elsewhere.net', path: '/', secure: false }
  ];

  assert.equal(cookieHeaderFor('https://cdn.example.com/media/a.ts', cookies), 'session=abc; scoped=yes');
  assert.equal(cookieHeaderFor('https://cdn.example.com/other/a.ts', cookies), 'session=abc', 'path-scoped cookie is withheld');
  assert.equal(cookieHeaderFor('http://cdn.example.com/media/a.ts', cookies), 'scoped=yes', 'secure cookie is withheld over http');
  assert.equal(cookieHeaderFor('https://elsewhere.net/a.ts', cookies), 'other=no');
  assert.equal(cookieHeaderFor('https://cdn.example.com/a.ts', []), '');
});

test('request headers carry the browser session identity', () => {
  const context = {
    userAgent: 'TestAgent/1.0',
    referer: 'https://goodshort.example.org/index.php?page=watch',
    origin: 'https://goodshort.example.org',
    cookies: [{ name: 'cf_clearance', value: 'token', domain: '.example.org', path: '/', secure: true }]
  };

  const headers = buildRequestHeaders('https://cdn.example.org/seg1.ts', context);

  assert.equal(headers['User-Agent'], 'TestAgent/1.0');
  assert.equal(headers['Referer'], context.referer);
  assert.equal(headers['Cookie'], 'cf_clearance=token');
  assert.equal(headers['Origin'], undefined, 'browsers do not send Origin on plain media GETs');
});

test('explicit headers override the derived ones', () => {
  const headers = buildRequestHeaders('https://cdn.example.com/a.ts', {}, { Range: 'bytes=0-99' });
  assert.equal(headers.Range, 'bytes=0-99');
});
