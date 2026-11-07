'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { cleanTitle, isGenericTitle, resolveDramaTitle, titleFromSlug } = require('../src/extractor');

const WATCH_URL =
  'https://goodshort.dramafren.org/index.php?page=watch&id=31001349156&ep=0&lang=en' +
  '&slug=found-a-homeless-genius-to-save-my-company&sv=1';

test('the real page title from GoodShort is rejected as generic', () => {
  // This is exactly what the site returned: the folder was being named after
  // the player instead of the series.
  assert.equal(isGenericTitle('GoodShort Player'), true);
  assert.equal(isGenericTitle('GoodShort'), true);
  assert.equal(isGenericTitle('Player'), true);
  assert.equal(isGenericTitle(''), true);
  assert.equal(isGenericTitle('001'), true);

  assert.equal(isGenericTitle('Found A Homeless Genius To Save My Company'), false);
  assert.equal(isGenericTitle('Drama Queen'), false, 'a real series is not rejected for containing "Drama"');
});

test('falls back to the URL slug when the page only offers player chrome', () => {
  const title = resolveDramaTitle(
    {
      globalTitle: '',
      ogTitle: '',
      headingTitle: 'GoodShort Player',
      documentTitle: 'GoodShort Player'
    },
    WATCH_URL
  );

  assert.equal(title, 'Found A Homeless Genius To Save My Company');
});

test('a genuine page title wins over the slug', () => {
  const title = resolveDramaTitle(
    { globalTitle: "The CEO's Secret Baby", documentTitle: 'GoodShort Player' },
    WATCH_URL
  );

  assert.equal(title, "The CEO's Secret Baby", 'real punctuation is preserved');
});

test('an explicit override beats everything', () => {
  const title = resolveDramaTitle(
    { override: 'My Own Folder Name', globalTitle: 'Something Else' },
    WATCH_URL
  );

  assert.equal(title, 'My Own Folder Name');
});

test('strips the reelshort and dramabox player suffixes', () => {
  // These are the literal og:title values those two sites serve.
  assert.equal(
    cleanTitle('The Double Life of My Billionaire Husband - Full Episodes Free | ReelShort Player'),
    'The Double Life of My Billionaire Husband'
  );
  assert.equal(
    cleanTitle("No Escape as the Dragon King's Mate - Full Episodes Free | Dramabox Player"),
    "No Escape as the Dragon King's Mate"
  );
});

test('site branding is stripped from page titles', () => {
  assert.equal(cleanTitle('Some Drama - Watch Free Short Dramas'), 'Some Drama');
  assert.equal(cleanTitle('GoodShort Player - Some Drama'), 'Some Drama');
  assert.equal(cleanTitle('Some Drama | GoodShort'), 'Some Drama');
  assert.equal(cleanTitle('Some Drama - Episode 12'), 'Some Drama');
  assert.equal(cleanTitle('  Spaced    Out  '), 'Spaced Out');
});

test('slugs become readable titles', () => {
  assert.equal(titleFromSlug('found-a-homeless-genius-to-save-my-company'), 'Found A Homeless Genius To Save My Company');
  assert.equal(titleFromSlug('the_ceo.returns'), 'The Ceo Returns');
  assert.equal(titleFromSlug(''), '');
  assert.equal(titleFromSlug(null), '');
});

test('falls back to the last path segment when there is no slug parameter', () => {
  const title = resolveDramaTitle(
    { documentTitle: 'Player' },
    'https://example.com/watch/my-secret-billionaire-husband'
  );

  assert.equal(title, 'My Secret Billionaire Husband');
});

test('gives up gracefully when nothing usable exists', () => {
  assert.equal(resolveDramaTitle({ documentTitle: 'Player' }, 'https://example.com/index.php'), 'Drama Series');
  assert.equal(resolveDramaTitle({}, undefined), 'Drama Series');
});
