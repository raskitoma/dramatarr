'use strict';

let currentExtractedData = null;

/** Per-episode row handles, keyed by episode number. */
const episodeRows = new Map();

/** Episodes that failed in the last download run. */
let failedEpisodes = new Set();

/** Episodes already present on disk when the results were rendered. */
let existingEpisodes = new Set();

/** Aborts the in-flight download when the user presses Stop. */
let downloadController = null;

document.addEventListener('DOMContentLoaded', () => {
  const urlInput = document.getElementById('url-input');
  const btnExtract = document.getElementById('btn-extract');
  const concurrencyInput = document.getElementById('concurrency-input');
  const forceInput = document.getElementById('force-input');

  const statusCard = document.getElementById('status-card');
  const statusTitle = document.getElementById('status-title');
  const statusSpinner = document.getElementById('status-spinner');
  const statusBadge = document.getElementById('status-badge');
  const progressBar = document.getElementById('progress-bar');
  const logsConsole = document.getElementById('logs-console');

  const resultsCard = document.getElementById('results-card');
  const dramaTitle = document.getElementById('drama-title');
  const dramaSummary = document.getElementById('drama-summary');
  const folderSummary = document.getElementById('folder-summary');
  const folderInput = document.getElementById('folder-input');
  const urisTextarea = document.getElementById('uris-textarea');
  const tableBody = document.getElementById('table-body');
  const selectAll = document.getElementById('select-all');
  const selectionSummary = document.getElementById('selection-summary');
  const onlyMissing = document.getElementById('only-missing');

  const btnDownloadAll = document.getElementById('btn-download-videos');
  const btnDownloadSelected = document.getElementById('btn-download-selected');
  const btnStop = document.getElementById('btn-stop');
  const btnRetryFailed = document.getElementById('btn-retry-failed');
  const btnCopyAll = document.getElementById('btn-copy-all');
  const btnDownloadTxt = document.getElementById('btn-download-txt');

  const libraryList = document.getElementById('library-list');
  const libraryPath = document.getElementById('library-path');
  const btnRefreshLibrary = document.getElementById('btn-refresh-library');

  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.remove('active'));
      tabContents.forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${btn.getAttribute('data-tab')}`).classList.add('active');
    });
  });

  // ------------------------------------------------------------------ helpers

  function addLog(msg, type = 'info') {
    if (!msg) return;
    const line = document.createElement('div');
    line.className = `log-line log-${type}`;
    line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
    logsConsole.appendChild(line);

    while (logsConsole.childElementCount > 400) logsConsole.removeChild(logsConsole.firstChild);
    logsConsole.scrollTop = logsConsole.scrollHeight;
  }

  function setStatus({ title, badge, badgeClass, spinning, percent, barColor }) {
    if (title !== undefined) statusTitle.textContent = title;
    if (badge !== undefined) statusBadge.textContent = badge;
    if (badgeClass !== undefined) statusBadge.className = `badge ${badgeClass}`;
    if (spinning !== undefined) statusSpinner.classList.toggle('hidden', !spinning);
    if (percent !== undefined) progressBar.style.width = `${percent}%`;
    if (barColor !== undefined) progressBar.style.background = barColor;
  }

  /**
   * Puts every control back to its "nothing has happened yet" state. Called
   * before each new extraction so labels, statuses and progress bars from the
   * previous series never linger.
   */
  function resetWorkspace() {
    currentExtractedData = null;
    failedEpisodes = new Set();
    existingEpisodes = new Set();

    if (downloadController) {
      downloadController.abort();
      downloadController = null;
    }

    episodeRows.clear();
    tableBody.replaceChildren();
    urisTextarea.value = '';
    logsConsole.replaceChildren();

    dramaTitle.textContent = '';
    dramaSummary.textContent = '';
    folderSummary.replaceChildren();
    folderSummary.style.display = 'none';
    folderInput.value = '';

    selectAll.checked = false;
    selectAll.indeterminate = false;
    selectionSummary.textContent = '';
    onlyMissing.checked = false;

    btnDownloadAll.disabled = false;
    btnDownloadAll.replaceChildren(icon('📥'), document.createTextNode(' Download All'));
    btnDownloadSelected.disabled = true;
    btnDownloadSelected.replaceChildren(icon('✅'), document.createTextNode(' Download Selected'));
    btnStop.classList.add('hidden');
    btnRetryFailed.classList.add('hidden');

    resultsCard.classList.add('hidden');
    setStatus({ percent: 0, barColor: '' });

    // Show the episode table first; the plain list is the secondary view.
    tabBtns.forEach(b => b.classList.toggle('active', b.getAttribute('data-tab') === 'table'));
    tabContents.forEach(c => c.classList.toggle('active', c.id === 'tab-table'));
  }

  function icon(glyph) {
    const span = document.createElement('span');
    span.className = 'action-icon';
    span.textContent = glyph;
    return span;
  }

  function selectedEpisodes() {
    const out = [];
    for (const [episode, row] of episodeRows) {
      // A hidden row is not part of what the user is looking at, so it is not
      // part of what "selected" means.
      if (row.checkbox && row.checkbox.checked && !row.checkbox.disabled && !row.row.classList.contains('row-hidden')) {
        out.push(episode);
      }
    }
    return out.sort((a, b) => a - b);
  }

  /**
   * Applies the "only missing" filter. Episodes already on disk are hidden so
   * what remains is exactly the work still outstanding.
   */
  function applyEpisodeFilter() {
    const hideDownloaded = onlyMissing.checked;
    let visible = 0;

    for (const [episode, row] of episodeRows) {
      const hide = hideDownloaded && existingEpisodes.has(episode);
      row.row.classList.toggle('row-hidden', hide);
      if (!hide) visible++;
    }

    if (hideDownloaded) {
      const hidden = episodeRows.size - visible;
      dramaSummary.textContent = hidden
        ? `Showing ${visible} episode${visible === 1 ? '' : 's'} still missing (${hidden} already downloaded).`
        : 'Every episode is already downloaded.';
    } else if (currentExtractedData) {
      dramaSummary.textContent =
        `${currentExtractedData.uris.length} of ${currentExtractedData.totalEpisodes} episodes have a playable stream URI.`;
    }

    refreshSelectionState();
  }

  function refreshSelectionState() {
    const selectable = [...episodeRows.values()].filter(
      r => r.checkbox && !r.checkbox.disabled && !r.row.classList.contains('row-hidden')
    );
    const chosen = selectable.filter(r => r.checkbox.checked);

    btnDownloadSelected.disabled = chosen.length === 0 || Boolean(downloadController);
    selectionSummary.textContent = chosen.length ? `${chosen.length} selected` : '';

    selectAll.checked = chosen.length > 0 && chosen.length === selectable.length;
    selectAll.indeterminate = chosen.length > 0 && chosen.length < selectable.length;
  }

  selectAll.addEventListener('change', () => {
    for (const row of episodeRows.values()) {
      if (row.checkbox && !row.checkbox.disabled && !row.row.classList.contains('row-hidden')) {
        row.checkbox.checked = selectAll.checked;
      }
    }
    refreshSelectionState();
  });

  onlyMissing.addEventListener('change', applyEpisodeFilter);
  folderInput.addEventListener('input', updateFolderPreview);

  // ------------------------------------------------------------------ extract

  btnExtract.addEventListener('click', () => {
    const url = urlInput.value.trim();
    if (!url) {
      alert('Paste a drama watch URL first.');
      urlInput.focus();
      return;
    }

    resetWorkspace();

    btnExtract.disabled = true;
    statusCard.classList.remove('hidden');

    setStatus({
      title: 'Connecting to drama server...',
      badge: 'In Progress',
      badgeClass: 'badge-active',
      spinning: true,
      percent: 10,
      barColor: ''
    });

    addLog(`Initiating extraction for: ${url}`, 'info');

    const eventSource = new EventSource(`/api/extract-stream?url=${encodeURIComponent(url)}`);
    let finished = false;

    const failWith = message => {
      if (finished) return;
      finished = true;
      setStatus({
        title: 'Extraction Failed',
        badge: 'Failed',
        badgeClass: 'badge-error',
        spinning: false,
        percent: 100,
        barColor: '#ef4444'
      });
      addLog(`Error: ${message}`, 'error');
      btnExtract.disabled = false;
      eventSource.close();
    };

    eventSource.addEventListener('progress', e => {
      const data = JSON.parse(e.data);
      addLog(data.message, 'info');

      const stagePercent = {
        init: 10,
        browser_start: 20,
        navigating: 35,
        challenge: 45,
        loading_metadata: 60,
        meta_ready: 70,
        strategy: 72,
        fallback: 72
      };

      if (data.stage === 'episode_resolved' && data.total) {
        setStatus({
          percent: 72 + Math.round((data.done / data.total) * 26),
          title: `Retrieving stream URIs (${data.done}/${data.total})...`
        });
      } else if (stagePercent[data.stage] !== undefined) {
        setStatus({ percent: stagePercent[data.stage], title: data.message });
      }
    });

    eventSource.addEventListener('complete', e => {
      finished = true;
      const result = JSON.parse(e.data);
      currentExtractedData = result;

      setStatus({
        title: `Extraction finished (${result.uris.length} episodes)`,
        badge: 'Completed',
        badgeClass: 'badge-success',
        spinning: false,
        percent: 100
      });
      addLog(`Extracted ${result.uris.length} URIs successfully!`, 'success');

      btnExtract.disabled = false;
      eventSource.close();
      renderResults(result);
    });

    eventSource.addEventListener('extract_error', e => {
      let message = 'An error occurred during extraction.';
      try {
        message = JSON.parse(e.data).message || message;
      } catch (err) {
        /* keep default */
      }
      failWith(message);
    });

    eventSource.addEventListener('error', () => {
      failWith('Lost connection to the DramaDown server.');
    });
  });

  // ------------------------------------------------------------------ results

  function renderResults(data) {
    dramaTitle.textContent = data.dramaTitle || 'Drama Series';
    dramaSummary.textContent = `${data.uris.length} of ${data.totalEpisodes} episodes have a playable stream URI.`;

    folderInput.value = data.dramaTitle || 'Drama Series';
    updateFolderPreview();

    urisTextarea.value = data.uris.join('\n');

    // Built with DOM APIs rather than innerHTML: these URIs come from a remote
    // site and must never be parsed as markup.
    tableBody.replaceChildren();
    episodeRows.clear();

    for (const episode of data.episodes || []) {
      const number = Number(episode.episode);
      const row = document.createElement('tr');

      const pickCell = document.createElement('td');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'ep-check';
      checkbox.disabled = !episode.uri;
      checkbox.addEventListener('change', refreshSelectionState);
      pickCell.appendChild(checkbox);

      const numberCell = document.createElement('td');
      const strong = document.createElement('strong');
      strong.textContent = `#${number}`;
      numberCell.appendChild(strong);

      const nameCell = document.createElement('td');
      nameCell.textContent = episode.name || `Episode ${number}`;

      const uriCell = document.createElement('td');
      uriCell.className = 'uri-cell';
      if (episode.uri) {
        uriCell.textContent = episode.uri;
        uriCell.title = episode.uri;
      } else {
        const missing = document.createElement('span');
        missing.className = 'uri-missing';
        missing.textContent = episode.error || 'Not available';
        uriCell.appendChild(missing);
      }

      const statusCell = document.createElement('td');
      const statusWrap = document.createElement('div');
      statusWrap.className = 'ep-status-wrap';

      const statusLabel = document.createElement('span');
      statusLabel.className = 'ep-status';
      statusLabel.textContent = episode.uri ? 'Ready' : '—';
      statusWrap.appendChild(statusLabel);

      if (episode.uri) {
        // Continue: finished episodes are skipped, interrupted ones resume.
        const resume = document.createElement('button');
        resume.className = 'btn-copy-mini';
        resume.textContent = '↓';
        resume.title = 'Download or continue this episode';
        resume.addEventListener('click', () => startDownload({ episodes: [number], force: false }));
        statusWrap.appendChild(resume);

        // Start over: throws away the existing file and any partial progress.
        const restart = document.createElement('button');
        restart.className = 'btn-copy-mini';
        restart.textContent = '⟳';
        restart.title = 'Re-download this episode from the start';
        restart.addEventListener('click', () => startDownload({ episodes: [number], force: true }));
        statusWrap.appendChild(restart);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'btn-copy-mini';
        copyBtn.textContent = 'Copy';
        copyBtn.addEventListener('click', () => copyToClipboard(episode.uri, copyBtn, 'Copy'));
        statusWrap.appendChild(copyBtn);
      }

      const track = document.createElement('div');
      track.className = 'ep-progress-track';
      const bar = document.createElement('div');
      bar.className = 'ep-progress-bar';
      track.appendChild(bar);

      statusCell.append(statusWrap, track);
      row.append(pickCell, numberCell, nameCell, uriCell, statusCell);
      tableBody.appendChild(row);

      episodeRows.set(number, { status: statusLabel, bar, checkbox, row });
    }

    btnDownloadAll.disabled = data.uris.length === 0;
    refreshSelectionState();
    markAlreadyDownloaded(folderInput.value);

    resultsCard.classList.remove('hidden');
    resultsCard.scrollIntoView({ behavior: 'smooth' });
  }

  // Mirrors sanitizeTitle() in src/downloader.js so the preview matches the
  // folder that will actually be created.
  function updateFolderPreview() {
    const clean = (folderInput.value || 'Drama Series')
      .replace(/[\\/:*?"<>|\x00-\x1f]/g, '_')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/^\.+|\.+$/g, '')
      .trim();

    folderSummary.style.display = 'block';
    folderSummary.replaceChildren(
      document.createTextNode('Saving to '),
      Object.assign(document.createElement('code'), { textContent: `downloads/${clean || 'Drama_Series'}/` })
    );
  }

  /**
   * Flags episodes whose file is already on disk, so it is obvious which ones a
   * plain download would skip and which need forcing.
   * @param {string} folderName
   */
  async function markAlreadyDownloaded(folderName) {
    existingEpisodes = new Set();
    if (!folderName) return;

    try {
      const response = await fetch(`/api/library/files?folder=${encodeURIComponent(folderName)}`);
      if (!response.ok) return;

      const { files } = await response.json();
      for (const file of files || []) {
        const match = file.name.match(/Episode_(\d+)\.\w+$/i);
        if (match) existingEpisodes.add(Number(match[1]));
      }
    } catch (err) {
      return;
    }

    for (const episode of existingEpisodes) {
      const row = episodeRows.get(episode);
      if (row && row.status.textContent === 'Ready') {
        row.status.textContent = '• On disk';
        row.status.style.color = '#94a3b8';
        row.bar.style.width = '100%';
      }
    }

    applyEpisodeFilter();
  }

  function updateEpisodeRow(episodeNumber, text, color, percent) {
    const row = episodeRows.get(Number(episodeNumber));
    if (!row) return;
    if (text !== undefined) row.status.textContent = text;
    if (color !== undefined) row.status.style.color = color;
    if (percent !== undefined) row.bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }

  // ----------------------------------------------------------------- download

  btnDownloadAll.addEventListener('click', () => startDownload({ force: forceInput.checked }));

  btnDownloadSelected.addEventListener('click', () =>
    startDownload({ episodes: selectedEpisodes(), force: forceInput.checked })
  );

  // Retry continues from whatever each episode already has on disk. Use the
  // force checkbox (or a row's ⟳) when the partial data itself is suspect.
  btnRetryFailed.addEventListener('click', () =>
    startDownload({ episodes: [...failedEpisodes].sort((a, b) => a - b), force: false })
  );

  btnStop.addEventListener('click', () => {
    if (!downloadController) return;
    addLog('Stopping download…', 'info');
    // Aborting the request closes the connection; the server sees that and
    // cancels the work in flight rather than finishing it unattended.
    downloadController.abort();
  });

  /**
   * @param {{episodes?: number[], force?: boolean}} options
   */
  async function startDownload(options = {}) {
    if (!currentExtractedData?.sessionId) {
      alert('Run an extraction first.');
      return;
    }
    if (downloadController) return;

    const episodes = options.episodes && options.episodes.length ? options.episodes : null;
    const scope = episodes ? `${episodes.length} episode${episodes.length === 1 ? '' : 's'}` : 'all episodes';

    downloadController = new AbortController();
    failedEpisodes = new Set();

    setDownloadingUi(true);
    statusCard.classList.remove('hidden');
    statusCard.scrollIntoView({ behavior: 'smooth' });

    setStatus({
      title: `Downloading ${scope} to "downloads/${folderInput.value.trim() || currentExtractedData.dramaTitle}"...`,
      badge: 'Downloading',
      badgeClass: 'badge-active',
      spinning: true,
      percent: 2,
      barColor: 'linear-gradient(90deg, #059669, #34d399)'
    });
    addLog(`Starting download of ${scope}...`, 'info');

    try {
      const response = await fetch('/api/download-stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: downloadController.signal,
        body: JSON.stringify({
          sessionId: currentExtractedData.sessionId,
          titleOverride: folderInput.value.trim(),
          concurrency: Number(concurrencyInput.value) || 3,
          force: options.force === true,
          episodes
        })
      });

      if (!response.ok) {
        const problem = await response.json().catch(() => ({}));
        throw new Error(problem.error || `Server responded ${response.status}`);
      }

      await consumeEventStream(response, handleDownloadEvent);
    } catch (err) {
      if (err.name === 'AbortError') {
        setStatus({ title: 'Download stopped', badge: 'Stopped', badgeClass: 'badge-error', spinning: false });
        addLog('Download stopped. Already-finished episodes are kept; re-run to resume.', 'info');
      } else {
        setStatus({ title: 'Download Error', badge: 'Error', badgeClass: 'badge-error', spinning: false });
        addLog(`Failed to download: ${err.message}`, 'error');
      }
    } finally {
      downloadController = null;
      setDownloadingUi(false);
      refreshSelectionState();
      loadLibrary();
    }
  }

  function setDownloadingUi(active) {
    btnDownloadAll.disabled = active;
    btnDownloadSelected.disabled = active || selectedEpisodes().length === 0;
    btnExtract.disabled = active;
    btnStop.classList.toggle('hidden', !active);
    if (active) btnRetryFailed.classList.add('hidden');
  }

  function handleDownloadEvent(event, data) {
    if (event === 'download_progress') {
      switch (data.stage) {
        case 'resumed':
          updateEpisodeRow(data.episode, '↻ Resuming…', '#fbbf24');
          addLog(`${data.filename}: ${data.message}`, 'info');
          return;
        case 'restarted':
          updateEpisodeRow(data.episode, 'Restarting…', '#fbbf24', 0);
          addLog(`${data.filename}: ${data.message}`, 'info');
          return;
        case 'episode_progress':
          updateEpisodeRow(
            data.episode,
            data.totalSegments
              ? `${data.percentage}% (${data.downloadedSegments}/${data.totalSegments})`
              : formatBytes(data.bytes),
            '#38bdf8',
            data.percentage
          );
          return;
        case 'downloading':
          updateEpisodeRow(data.episode, 'Downloading...', '#38bdf8', 0);
          break;
        case 'episode_done':
          updateEpisodeRow(data.episode, '✔ Downloaded', '#34d399', 100);
          failedEpisodes.delete(Number(data.episode));
          existingEpisodes.add(Number(data.episode));
          break;
        case 'episode_skipped':
          updateEpisodeRow(data.episode, '• Already saved', '#94a3b8', 100);
          break;
        case 'episode_error':
          updateEpisodeRow(data.episode, '✖ Failed', '#ef4444', 0);
          failedEpisodes.add(Number(data.episode));
          break;
        default:
          break;
      }

      if (data.completed && data.total) {
        const percent = Math.round((data.completed / data.total) * 100);
        setStatus({ percent, title: `Downloaded ${data.completed}/${data.total} episodes (${percent}%)` });
      }

      addLog(data.message, data.stage === 'episode_error' ? 'error' : 'info');
      return;
    }

    if (event === 'download_complete') {
      const allGood = data.failedCount === 0;
      setStatus({
        title: `${data.downloadedCount}/${data.total} episodes saved`,
        badge: allGood ? 'All Downloaded' : 'Finished With Errors',
        badgeClass: allGood ? 'badge-success' : 'badge-error',
        spinning: false,
        percent: 100
      });
      addLog(`Saved into: ${data.folder}`, allGood ? 'success' : 'error');
      for (const failure of data.errors || []) addLog(`${failure.filename}: ${failure.error}`, 'error');

      btnRetryFailed.classList.toggle('hidden', failedEpisodes.size === 0);
      if (failedEpisodes.size) {
        btnRetryFailed.replaceChildren(icon('↻'), document.createTextNode(` Retry ${failedEpisodes.size} Failed`));
      }
      return;
    }

    if (event === 'download_error') {
      setStatus({ title: 'Download Failed', badge: 'Failed', badgeClass: 'badge-error', spinning: false });
      addLog(`Download error: ${data.message}`, 'error');
    }
  }

  // ------------------------------------------------------------------ library

  async function loadLibrary() {
    try {
      const response = await fetch('/api/library');
      if (!response.ok) throw new Error(`Server responded ${response.status}`);

      const { folder, series } = await response.json();
      libraryPath.textContent = folder || '';
      renderLibrary(series || []);
    } catch (err) {
      libraryList.replaceChildren(
        Object.assign(document.createElement('p'), {
          className: 'library-empty',
          textContent: `Could not read the downloads folder: ${err.message}`
        })
      );
    }
  }

  function renderLibrary(series) {
    libraryList.replaceChildren();

    if (series.length === 0) {
      libraryList.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'library-empty',
          textContent: 'Nothing downloaded yet.'
        })
      );
      return;
    }

    for (const item of series) {
      const details = document.createElement('details');
      details.className = 'library-item';

      const summary = document.createElement('summary');
      const name = document.createElement('span');
      name.className = 'library-name';
      name.textContent = item.name;

      const meta = document.createElement('span');
      meta.className = 'library-meta';
      // When the series total is known, show progress against it rather than a
      // bare file count.
      meta.textContent = item.totalEpisodes
        ? `${item.downloadedEpisodes}/${item.totalEpisodes} episodes · ${formatBytes(item.bytes)}`
        : `${item.episodes} file${item.episodes === 1 ? '' : 's'} · ${formatBytes(item.bytes)}`;

      summary.append(name);

      if (item.missingEpisodes > 0) {
        const missing = document.createElement('span');
        missing.className = 'library-missing';
        missing.textContent = `${item.missingEpisodes} missing`;
        summary.appendChild(missing);
      } else if (item.totalEpisodes) {
        const complete = document.createElement('span');
        complete.className = 'library-complete';
        complete.textContent = 'complete';
        summary.appendChild(complete);
      }

      summary.appendChild(meta);
      details.appendChild(summary);

      const body = document.createElement('div');
      body.className = 'library-body';

      if (item.sourceUrl) {
        const source = document.createElement('div');
        source.className = 'library-source';

        const label = document.createElement('span');
        label.className = 'library-source-url';
        label.textContent = item.sourceUrl;
        label.title = item.sourceUrl;

        // Puts the original watch URL back in the box, ready to re-extract.
        const reuse = document.createElement('button');
        reuse.className = 'btn-copy-mini';
        reuse.textContent = '↺ Use';
        reuse.title = 'Load this URL into the extractor';
        reuse.addEventListener('click', () => {
          urlInput.value = item.sourceUrl;
          urlInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          urlInput.focus();
        });

        const copy = document.createElement('button');
        copy.className = 'btn-copy-mini';
        copy.textContent = 'Copy';
        copy.addEventListener('click', () => copyToClipboard(item.sourceUrl, copy, 'Copy'));

        source.append(label, reuse, copy);
        body.appendChild(source);
      }

      const files = document.createElement('div');
      files.className = 'library-files';
      files.textContent = 'Loading…';
      body.appendChild(files);
      details.appendChild(body);

      let loaded = false;
      details.addEventListener('toggle', async () => {
        if (!details.open || loaded) return;
        loaded = true;

        try {
          const response = await fetch(`/api/library/files?folder=${encodeURIComponent(item.name)}`);
          const { files: list } = await response.json();
          files.replaceChildren();

          for (const file of list || []) {
            const row = document.createElement('div');
            row.className = 'library-file';

            const label = document.createElement('span');
            label.textContent = file.name;

            const size = document.createElement('span');
            size.className = 'library-file-size';
            size.textContent = formatBytes(file.bytes);

            row.append(label, size);
            files.appendChild(row);
          }
        } catch (err) {
          files.textContent = 'Could not list this folder.';
        }
      });

      libraryList.appendChild(details);
    }
  }

  btnRefreshLibrary.addEventListener('click', loadLibrary);
  loadLibrary();

  // ------------------------------------------------------------------- export

  btnCopyAll.addEventListener('click', () => {
    if (!urisTextarea.value) return;
    copyToClipboard(urisTextarea.value, btnCopyAll, btnCopyAll.textContent);
  });

  btnDownloadTxt.addEventListener('click', () => {
    if (!urisTextarea.value) return;
    const blob = new Blob([urisTextarea.value], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    const slug = (dramaTitle.textContent || 'drama').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    link.href = URL.createObjectURL(blob);
    link.download = `${slug || 'drama'}_uris.txt`;
    link.click();
    URL.revokeObjectURL(link.href);
  });
});

/**
 * Parses an SSE-formatted fetch response. Used instead of EventSource because
 * the download endpoint is a POST.
 * @param {Response} response
 * @param {(event: string, data: object) => void} onEvent
 */
async function consumeEventStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() ?? '';

    for (const block of blocks) {
      if (!block.trim() || block.startsWith(':')) continue;

      const eventMatch = block.match(/^event:\s*(\S+)/m);
      const dataLines = block
        .split('\n')
        .filter(line => line.startsWith('data:'))
        .map(line => line.slice(5).trim());

      if (dataLines.length === 0) continue;

      try {
        onEvent(eventMatch ? eventMatch[1] : 'message', JSON.parse(dataLines.join('\n')));
      } catch (err) {
        /* ignore malformed frame */
      }
    }
  }
}

/**
 * @param {string} text
 * @param {HTMLElement} button
 * @param {string} originalLabel
 */
function copyToClipboard(text, button, originalLabel) {
  navigator.clipboard
    .writeText(text)
    .then(() => {
      button.textContent = '✔ Copied!';
      button.classList.add('copied');
      setTimeout(() => {
        button.textContent = originalLabel;
        button.classList.remove('copied');
      }, 1600);
    })
    .catch(() => {
      button.textContent = 'Copy failed';
      setTimeout(() => {
        button.textContent = originalLabel;
      }, 1600);
    });
}

/**
 * @param {number} bytes
 * @returns {string}
 */
function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}
