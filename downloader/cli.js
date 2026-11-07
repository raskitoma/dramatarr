#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const { extractDramaVideoUris } = require('./src/extractor');
const { downloadDramaEpisodes } = require('./src/downloader');

const DEFAULT_URL =
  'https://goodshort.dramafren.org/index.php?page=watch&id=31001349156&ep=0&lang=en' +
  '&slug=found-a-homeless-genius-to-save-my-company&sv=1';

const colors = {
  reset: '\x1b[0m',
  bold: '\x1b[1m',
  dim: '\x1b[90m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m'
};

const USAGE = `
${colors.bold}DramaDown${colors.reset} — drama video URI extractor & downloader

${colors.bold}Usage${colors.reset}
  node cli.js "<WATCH_URL>" [options]

${colors.bold}Options${colors.reset}
  -r, --raw                    Print only the URIs (implies --no-download, writes no files)
      --no-download            Extract URIs but skip downloading video files
      --extract-only           Alias for --no-download
  -o, --output <file>          Write the URI list to <file> (default: ./uris.txt)
      --out-dir <dir>          Base download directory (default: ./downloads)
  -t, --title <name>           Override the series name used for the folder
  -n, --max-episodes <n>       Only take the first <n> episodes (0 = all)
      --backend <name>         Download engine: auto (default), native, or ytdlp
  -c, --concurrency <n>        Episodes downloaded in parallel, 1-8 (default: 3)
      --segment-concurrency <n> Segments fetched in parallel per episode (default: 6)
      --force                  Re-download episodes whose file already exists
      --headless               Run Chrome headless (less reliable against Turnstile)
  -h, --help                   Show this help

${colors.bold}Examples${colors.reset}
  node cli.js "https://goodshort.dramafren.org/index.php?page=watch&id=123&ep=0"
  node cli.js "<URL>" --no-download
  node cli.js "<URL>" --raw > uris.txt
  node cli.js "<URL>" -c 5 --out-dir /Volumes/Media/dramas
`;

/**
 * Parses argv into an options object.
 * @param {string[]} argv
 * @returns {object}
 */
function parseArgs(argv) {
  const options = {
    url: null,
    raw: false,
    download: true,
    output: null,
    outDir: null,
    title: null,
    maxEpisodes: 0,
    backend: 'auto',
    concurrency: 3,
    segmentConcurrency: 6,
    force: false,
    headless: false,
    help: false
  };

  // Supports "--flag value", "--flag=value" and the short forms.
  const takeValue = (arg, index) => {
    const equalsIndex = arg.indexOf('=');
    if (equalsIndex !== -1) return { value: arg.slice(equalsIndex + 1), nextIndex: index };
    return { value: argv[index + 1], nextIndex: index + 1 };
  };

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const name = arg.split('=')[0];

    switch (name) {
      case '-h':
      case '--help':
        options.help = true;
        break;
      case '-r':
      case '--raw':
        options.raw = true;
        options.download = false;
        break;
      case '--no-download':
      case '--extract-only':
        options.download = false;
        break;
      case '--force':
        options.force = true;
        break;
      case '--headless':
        options.headless = true;
        break;
      case '-o':
      case '--output': {
        const { value, nextIndex } = takeValue(arg, i);
        options.output = value;
        i = nextIndex;
        break;
      }
      case '--out-dir': {
        const { value, nextIndex } = takeValue(arg, i);
        options.outDir = value;
        i = nextIndex;
        break;
      }
      case '-t':
      case '--title': {
        const { value, nextIndex } = takeValue(arg, i);
        options.title = value;
        i = nextIndex;
        break;
      }
      case '-n':
      case '--max-episodes': {
        const { value, nextIndex } = takeValue(arg, i);
        options.maxEpisodes = Math.max(0, parseInt(value, 10) || 0);
        i = nextIndex;
        break;
      }
      case '--backend': {
        const { value, nextIndex } = takeValue(arg, i);
        options.backend = ['auto', 'native', 'ytdlp'].includes(value) ? value : 'auto';
        i = nextIndex;
        break;
      }
      case '-c':
      case '--concurrency': {
        const { value, nextIndex } = takeValue(arg, i);
        options.concurrency = clamp(parseInt(value, 10), 1, 8, 3);
        i = nextIndex;
        break;
      }
      case '--segment-concurrency': {
        const { value, nextIndex } = takeValue(arg, i);
        options.segmentConcurrency = clamp(parseInt(value, 10), 1, 16, 6);
        i = nextIndex;
        break;
      }
      default:
        if (!options.url && /^https?:\/\//i.test(arg)) options.url = arg;
        break;
    }
  }

  return options;
}

/**
 * @param {number} value
 * @param {number} min
 * @param {number} max
 * @param {number} fallback
 * @returns {number}
 */
function clamp(value, min, max, fallback) {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

/**
 * @param {number} bytes
 * @returns {string}
 */
function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));

  if (options.help) {
    console.log(USAGE);
    return 0;
  }

  const url = options.url || DEFAULT_URL;
  const verbose = !options.raw;
  const log = (color, label, message) => {
    if (verbose) console.log(`${color}[${label}]${colors.reset} ${message}`);
  };

  // Ctrl-C stops the browser and any in-flight downloads cleanly.
  const controller = new AbortController();
  let interrupted = false;
  process.on('SIGINT', () => {
    if (interrupted) process.exit(130);
    interrupted = true;
    controller.abort();
    if (verbose) console.log(`\n${colors.yellow}[Stopping]${colors.reset} Finishing up — press Ctrl-C again to force quit.`);
  });

  if (!options.url && verbose) {
    log(colors.cyan, 'Info', `No URL provided. Using the sample URL:\n${url}\n`);
  }

  if (verbose) {
    console.log(`${colors.bold}${colors.green}=== DramaDown Video URI Extractor & Downloader ===${colors.reset}`);
    console.log(`${colors.dim}Target:${colors.reset} ${url}\n`);
  }

  const startTime = Date.now();

  const result = await extractDramaVideoUris(url, {
    headless: options.headless,
    title: options.title,
    maxEpisodes: options.maxEpisodes,
    signal: controller.signal,
    onProgress: progress => log(colors.blue, 'Progress', progress.message)
  });

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);

  if (verbose) {
    console.log(
      `\n${colors.bold}${colors.green}✔ Done in ${elapsed}s!${colors.reset} ` +
      `Found ${result.uris.length} video URIs for "${result.dramaTitle}":\n`
    );
    console.log(`${colors.dim}${'─'.repeat(80)}${colors.reset}`);
  }

  for (const uri of result.uris) console.log(uri);

  if (verbose) console.log(`${colors.dim}${'─'.repeat(80)}${colors.reset}`);

  // In raw mode nothing is written unless an explicit -o was given, so the
  // output can be piped without side effects.
  const outputPath = options.output || (options.raw ? null : path.join(__dirname, 'uris.txt'));
  if (outputPath) {
    fs.writeFileSync(outputPath, result.uris.join('\n') + '\n', 'utf8');
    log(colors.green, 'Saved', `${result.uris.length} URIs written to: ${outputPath}`);
  }

  if (!options.download || result.uris.length === 0) return 0;

  console.log(`\n${colors.bold}${colors.magenta}=== Downloading episodes ===${colors.reset}`);

  const lastDecile = new Map();
  const summary = await downloadDramaEpisodes(result, {
    concurrency: options.concurrency,
    segmentConcurrency: options.segmentConcurrency,
    force: options.force,
    backend: options.backend,
    signal: controller.signal,
    baseDir: options.outDir ? path.resolve(options.outDir) : undefined,
    onProgress: progress => {
      switch (progress.stage) {
        case 'start':
          log(colors.cyan, 'Folder', `Created directory: ${progress.folder}`);
          break;
        case 'downloading':
          log(colors.yellow, 'Downloading', progress.message);
          break;
        case 'episode_progress': {
          // Throttled to one line per 25% so parallel episodes stay readable.
          if (!progress.totalSegments) return;
          const decile = Math.floor(progress.percentage / 25);
          if (decile === 0 || lastDecile.get(progress.episode) === decile) return;
          lastDecile.set(progress.episode, decile);
          log(
            colors.dim,
            'Segments',
            `${progress.filename} ${progress.percentage}% ` +
            `(${progress.downloadedSegments}/${progress.totalSegments}, ${formatBytes(progress.bytes)})`
          );
          break;
        }
        case 'episode_done':
          log(colors.green, 'Completed', `${progress.message} (${formatBytes(progress.bytes)})`);
          break;
        case 'episode_skipped':
          log(colors.dim, 'Skipped', progress.message);
          break;
        case 'episode_error':
          console.error(`${colors.red}[Failed]${colors.reset} ${progress.message}`);
          break;
        case 'all_done':
          console.log(`\n${colors.bold}${colors.green}✔ ${progress.message}${colors.reset}\n`);
          break;
        default:
          break;
      }
    }
  });

  if (summary.failedCount > 0) {
    console.error(`${colors.red}${summary.failedCount} episode(s) failed. Re-run the same command to retry just those.${colors.reset}`);
    return 1;
  }

  return 0;
}

main()
  .then(code => {
    process.exitCode = code;
  })
  .catch(err => {
    if (err && /cancelled/i.test(err.message)) {
      console.error(`\n${colors.yellow}[Stopped]${colors.reset} Interrupted by user.`);
      process.exitCode = 130;
      return;
    }
    console.error(`${colors.red}[Error]${colors.reset} ${err.message}`);
    process.exitCode = 1;
  });
