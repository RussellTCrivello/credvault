/* ==========================================================================
   LogIntel workspace — import screen
   --------------------------------------------------------------------------
   Three phases, one continuous readout:

     1. Transfer  -- measured by the browser itself (XMLHttpRequest upload
                     progress: real bytes handed to the server).
     2. Ingest    -- measured by the importer (bytes read per file, lines
                     classified, rows written) and polled from
                     /import/status/<job>/ while the import request runs.
     3. Summary   -- the same numbers, final.

   The job is registered before the upload starts (POST /import/prepare/), so
   the panel is driven by server state for the whole run and the totals it
   shows while waiting are the totals the server will actually read.

   Every string comes from the `import-labels` payload rendered by the view, so
   the progress readout follows the workspace language like the rest of the UI.
   ========================================================================== */

(function () {
  'use strict';

  const workspace = document.querySelector('[data-import-workspace]');
  if (!workspace) return;

  const BINARY_EXT = new Set([
    'sqlite3', 'db', 'pyc', 'pyo', 'mo', 'png', 'jpg', 'jpeg', 'gif', 'webp',
    'ico', 'pdf', 'zip', 'tar', 'gz', '7z', 'exe', 'dll', 'so', 'dylib',
    'woff', 'woff2', 'ttf', 'eot',
  ]);
  const POLL_FAST = 350;
  const POLL_SLOW = 1200;
  const MAX_404_RETRIES = 40;
  const ANNOUNCE_EVERY = 1500;

  const $ = (sel) => workspace.querySelector(sel);
  const $$ = (sel) => Array.from(workspace.querySelectorAll(sel));

  const urls = readJson('#import-urls') || {};
  const strings = readJson('#import-labels') || {};

  const form = $('[data-import-form]');
  const jobField = $('[data-import-job]');
  const startButton = $('[data-import-start]');
  const stopButton = $('[data-import-stop]');
  const againButton = $('[data-import-again]');
  const viewLink = $('[data-progress-view]');
  const progressCard = $('[data-import-progress]');
  const queueList = $('[data-import-queue]');
  const queueBar = $('[data-import-summary]');
  const queueEmpty = $('[data-import-empty]');
  const dropzone = $('[data-import-dropzone]');
  const scanButton = $('[data-import-scan]');
  const scanResult = $('[data-import-scan-result]');
  const pathInput = $('[data-import-path]');
  const liveRegion = $('[data-progress-live]');
  const resultBox = $('[data-import-result]');

  const state = {
    mode: 'upload',
    files: [],
    job: null,
    running: false,
    phase: 'idle',
    uploadBytes: 0,
    uploadTotal: 0,
    timer: null,
    xhr: null,
    notFoundTries: 0,
    lastAnnounce: 0,
    lastSnapshot: null,
  };

  /* ------------------------------------------------------------------ utils */

  function readJson(selector) {
    const node = document.querySelector(selector);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return null;
    }
  }

  function t(key, vars) {
    let text = strings[key];
    if (typeof text !== 'string') return key;
    if (vars) {
      Object.keys(vars).forEach((name) => {
        text = text.split('{' + name + '}').join(String(vars[name]));
      });
    }
    return text;
  }

  function urlFor(key, jobId) {
    return (urls[key] || '').replace('__JOB_ID__', jobId || '');
  }

  function csrfToken() {
    const field = form && form.querySelector('[name=csrfmiddlewaretoken]');
    return field ? field.value : '';
  }

  function extOf(name) {
    const dot = String(name || '').lastIndexOf('.');
    return dot === -1 ? '' : name.slice(dot + 1).toLowerCase();
  }

  function bytesText(value) {
    let size = Number(value) || 0;
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    for (let i = 0; i < units.length; i += 1) {
      if (size < 1024 || i === units.length - 1) {
        return units[i] === 'B'
          ? `${Math.round(size)} ${units[i]}`
          : `${size.toFixed(1)} ${units[i]}`;
      }
      size /= 1024;
    }
    return `${size} B`;
  }

  function numText(value) {
    return (Number(value) || 0).toLocaleString(document.documentElement.lang || 'en');
  }

  function durationText(seconds) {
    const total = Math.max(Math.round(Number(seconds) || 0), 0);
    if (total < 60) return t('secondsShort', { n: total });
    if (total < 3600) {
      return t('minutesShort', { m: Math.floor(total / 60), s: String(total % 60).padStart(2, '0') });
    }
    return t('hoursShort', {
      h: Math.floor(total / 3600),
      m: String(Math.floor((total % 3600) / 60)).padStart(2, '0'),
    });
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function iconNode(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.appendChild(use);
    return svg;
  }

  function setText(selector, value) {
    const node = $(selector);
    if (!node) return;
    const next = value === null || value === undefined ? '' : String(value);
    if (node.textContent !== next) node.textContent = next;
  }

  function setWidth(selector, percent) {
    const node = $(selector);
    if (!node) return;
    const value = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
    if (node.style.width !== value) node.style.width = value;
  }

  /* ---------------------------------------------------------- source picker */

  $$('.mode-button').forEach((button) => {
    button.addEventListener('click', () => setMode(button.dataset.importMode));
  });

  function setMode(mode) {
    state.mode = mode === 'path' ? 'path' : 'upload';
    $$('.mode-button').forEach((button) => {
      const active = button.dataset.importMode === state.mode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', String(active));
    });
    $$('[data-import-pane]').forEach((pane) => {
      pane.hidden = pane.dataset.importPane !== state.mode;
    });
    syncStartButton();
  }

  $$('[data-import-input]').forEach((input) => {
    input.addEventListener('change', () => {
      addFiles(input.files);
      // Reset so picking the same file again still fires a change event.
      input.value = '';
    });
  });

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    let ignored = 0;
    incoming.forEach((file) => {
      const path = file.webkitRelativePath || file.name;
      const key = `${path}:${file.size}`;
      if (state.files.some((item) => item.key === key)) return;
      const binary = BINARY_EXT.has(extOf(file.name));
      state.files.push({
        key,
        file,
        name: file.name,
        path,
        bytes: file.size,
        included: !binary,
        reason: binary ? 'binary' : '',
      });
      if (binary) ignored += 1;
    });
    renderQueue();
    if (ignored) announce(t('binaryExcluded', { n: numText(ignored) }));
  }

  function includedFiles() {
    return state.files.filter((item) => item.included);
  }

  function renderQueue() {
    if (!queueList) return;
    queueList.textContent = '';

    includedFiles().concat(state.files.filter((item) => !item.included))
      .slice(0, 400)
      .forEach((item) => queueList.appendChild(queueRow(item)));

    if (state.files.length > 400) {
      const more = el('li', 'queue-row');
      more.appendChild(el('span', 'queue-name', t('moreFiles', {
        n: numText(state.files.length - 400),
      })));
      queueList.appendChild(more);
    }

    const total = includedFiles().reduce((sum, item) => sum + (item.bytes || 0), 0);
    setText('[data-import-count]', numText(includedFiles().length));
    setText('[data-import-size]', bytesText(total));
    if (queueBar) queueBar.hidden = state.files.length === 0;
    if (queueEmpty) queueEmpty.hidden = state.files.length > 0;
    syncStartButton();
  }

  function queueRow(item) {
    const row = el('li', 'queue-row');
    if (!item.included) row.classList.add('is-excluded');

    const check = el('button', 'queue-check');
    check.type = 'button';
    check.setAttribute('role', 'checkbox');
    check.setAttribute('aria-checked', String(item.included));
    check.setAttribute('aria-label', `${item.included ? t('exclude') : t('include')}: ${item.name}`);
    check.appendChild(iconNode('check'));
    check.addEventListener('click', () => {
      item.included = !item.included;
      renderQueue();
    });

    const names = el('span', 'queue-name');
    names.appendChild(el('span', null, item.name));
    if (item.path !== item.name) names.appendChild(el('small', 'queue-path', item.path));
    if (item.reason) names.appendChild(el('small', 'queue-path', t('notTextFile')));

    const remove = el('button', 'queue-remove');
    remove.type = 'button';
    remove.appendChild(iconNode('x'));
    remove.appendChild(el('span', 'sr-only', `${t('remove')}: ${item.name}`));
    remove.addEventListener('click', () => {
      state.files = state.files.filter((entry) => entry.key !== item.key);
      renderQueue();
    });

    row.appendChild(check);
    row.appendChild(names);
    row.appendChild(el('span', 'queue-size', bytesText(item.bytes)));
    row.appendChild(el('span', 'queue-state', item.included ? t('ready') : t('excluded')));
    row.appendChild(remove);
    return row;
  }

  $('[data-import-clear]')?.addEventListener('click', () => {
    state.files = [];
    renderQueue();
  });

  const setAll = (included) => () => {
    state.files.forEach((item) => {
      if (!item.reason) item.included = included;
    });
    renderQueue();
  };
  $('[data-import-select-all]')?.addEventListener('click', setAll(true));
  $('[data-import-select-none]')?.addEventListener('click', setAll(false));

  function syncStartButton() {
    if (!startButton || state.running) return;
    startButton.disabled = state.mode === 'upload' && includedFiles().length === 0;
  }

  if (dropzone) {
    ['dragenter', 'dragover'].forEach((type) =>
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.add('is-over');
      })
    );
    ['dragleave', 'drop'].forEach((type) =>
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.remove('is-over');
      })
    );
    dropzone.addEventListener('drop', (event) => {
      const items = event.dataTransfer && event.dataTransfer.items;
      if (items && items.length && items[0].webkitGetAsEntry) {
        collectEntries(items).then((files) => addFiles(files));
      } else {
        addFiles(event.dataTransfer ? event.dataTransfer.files : null);
      }
    });
  }

  /* Dragging a folder is only readable through the entry API, which is
     asynchronous and unbounded in depth: walk the tree before touching the
     queue, so "342 files" means 342 files rather than a growing surprise. */
  function collectEntries(items) {
    const roots = Array.from(items)
      .filter((item) => item.kind === 'file' || item.kind === 'directory')
      .map((item) => item.webkitGetAsEntry());
    const collected = [];

    function readFile(entry) {
      return new Promise((resolve) => {
        entry.file(
          (file) => {
            collected.push(file);
            resolve();
          },
          () => resolve()
        );
      });
    }

    function readEntries(reader) {
      return new Promise((resolve) => {
        const children = [];
        const step = () =>
          reader.readEntries((batch) => {
            if (!batch || !batch.length) {
              resolve(children);
              return;
            }
            children.push(...batch);
            step();
          }, () => resolve(children));
        step();
      });
    }

    function walk(entry) {
      if (!entry) return Promise.resolve();
      if (entry.isFile) return readFile(entry);
      if (entry.isDirectory) {
        return readEntries(entry.createReader()).then((children) =>
          Promise.all(children.map(walk)).then(() => null)
        );
      }
      return Promise.resolve();
    }

    return Promise.all(roots.map(walk)).then(() => collected);
  }

  /* ------------------------------------------------------- server path mode */

  scanButton?.addEventListener('click', () => {
    const folder = (pathInput && pathInput.value.trim()) || '';
    if (!folder) {
      pathInput?.focus();
      announce(t('noPath'));
      return;
    }
    withButtonLabel(scanButton, t('listing'), () =>
      prepare('path', { folder_path: folder })
        .then((data) => {
          state.job = data.job_id;
          if (jobField) jobField.value = data.job_id;
          renderScanSummary(data);
        })
        .catch((error) => showScanError(error.message))
    );
  });

  function withButtonLabel(button, busyLabel, task) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = busyLabel;
    return Promise.resolve()
      .then(task)
      .finally(() => {
        button.disabled = false;
        button.textContent = original;
      });
  }

  function showScanError(message) {
    if (!scanResult) return;
    scanResult.hidden = false;
    scanResult.textContent = '';
    scanResult.appendChild(el('p', null, message || t('prepareFailed')));
  }

  function renderScanSummary(data) {
    if (!scanResult || !data) return;
    scanResult.hidden = false;
    scanResult.textContent = '';

    const numbers = el('dl', 'scan-numbers');
    [
      [t('files'), numText(data.files)],
      [t('totalSize'), bytesText(data.bytes)],
      [t('skipped'), numText(data.skipped || 0)],
    ].forEach(([label, value]) => {
      const cell = el('div', 'scan-number');
      cell.appendChild(el('dt', null, label));
      cell.appendChild(el('dd', null, value));
      numbers.appendChild(cell);
    });
    scanResult.appendChild(numbers);

    const sample = Array.isArray(data.sample) ? data.sample : [];
    if (sample.length) {
      const list = el('ul', 'scan-list');
      sample.forEach((name) => list.appendChild(el('li', null, name)));
      scanResult.appendChild(list);
      if ((data.files || 0) > sample.length) {
        scanResult.appendChild(el('p', 'field-help',
          t('firstOf', { a: sample.length, b: numText(data.files) })));
      }
    }
    scanResult.appendChild(el('p', 'field-help', t('scanNothing')));
  }

  /* ----------------------------------------------------------------- import */

  form.addEventListener('submit', (event) => {
    if (!window.XMLHttpRequest || !('upload' in new XMLHttpRequest())) return;
    event.preventDefault();
    if (state.running) return;
    start();
  });

  function start() {
    const payload = new FormData();
    payload.append('csrfmiddlewaretoken', csrfToken());

    if (state.mode === 'path') {
      const folder = (pathInput && pathInput.value.trim()) || '';
      if (!folder) {
        pathInput?.focus();
        announce(t('noPath'));
        return;
      }
      payload.append('folder_path', folder);
    } else {
      const files = includedFiles();
      if (!files.length) {
        announce(t('noFiles'));
        return;
      }
      files.forEach((item) => {
        payload.append('folder', item.file, item.name);
        payload.append('relpath', item.path);
      });
    }

    beginRun();
    prepare(state.mode, {
      files: includedFiles().map((item) => ({ name: item.name, bytes: item.bytes })),
      folder_path: state.mode === 'path' ? payload.get('folder_path') : undefined,
    })
      .catch(() => null)
      .then((data) => {
        if (data && data.job_id) {
          state.job = data.job_id;
          if (jobField) jobField.value = data.job_id;
          if (state.mode === 'path') renderScanSummary(data);
        }
        payload.append('job', state.job || '');
        upload(payload);
      });
  }

  function beginRun() {
    state.running = true;
    state.phase = 'transfer';
    state.notFoundTries = 0;
    state.uploadBytes = 0;
    if (progressCard) {
      progressCard.hidden = false;
      progressCard.classList.remove('is-done', 'is-stopped', 'is-failed', 'is-idle');
    }
    if (resultBox) {
      resultBox.hidden = true;
      resultBox.textContent = '';
    }
    if (startButton) {
      startButton.disabled = true;
      setButtonLabel(startButton, t('importing'));
    }
    if (stopButton) {
      stopButton.hidden = false;
      stopButton.disabled = false;
    }
    if (againButton) againButton.hidden = true;
    if (viewLink) viewLink.hidden = true;
    if (scanButton) scanButton.disabled = true;
    setRail(0);
    renderTransfer();
    progressCard?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    window.addEventListener('beforeunload', warnOnLeave);
    poll();
  }

  function endRun() {
    state.running = false;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (startButton) {
      startButton.disabled = false;
      setButtonLabel(startButton, t('start'));
    }
    if (stopButton) {
      stopButton.hidden = true;
      stopButton.disabled = false;
    }
    if (againButton) againButton.hidden = false;
    if (viewLink) viewLink.hidden = false;
    if (scanButton) scanButton.disabled = false;
    syncStartButton();
    window.removeEventListener('beforeunload', warnOnLeave);
  }

  function setButtonLabel(button, text) {
    const target = button.querySelector('span') || button;
    if (target.textContent !== text) target.textContent = text;
  }

  function warnOnLeave(event) {
    event.preventDefault();
    event.returnValue = '';
    return '';
  }

  function prepare(mode, extra) {
    return fetch(urlFor('prepare'), {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken(),
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify(Object.assign({ mode }, extra || {})),
    })
      .then((response) =>
        response.json().catch(() => ({})).then((data) => ({ ok: response.ok, data: data || {} }))
      )
      .then(({ ok, data }) => {
        if (!ok || data.ok === false) {
          throw new Error(data.error || t('prepareFailed'));
        }
        return data;
      });
  }

  function upload(payload) {
    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    state.uploadTotal = includedFiles().reduce((sum, item) => sum + (item.bytes || 0), 0);

    if (state.mode === 'upload') {
      xhr.upload.addEventListener('progress', (event) => {
        state.uploadBytes = event.loaded || 0;
        if (event.total) state.uploadTotal = event.total;
        renderTransfer();
      });
    }

    xhr.addEventListener('loadend', () => {
      const aborted = xhr.status === 0;
      state.xhr = null;
      state.phase = 'ingest';
      let data = null;
      try {
        data = JSON.parse(xhr.responseText || '{}');
      } catch (error) {
        data = null;
      }
      if (aborted) {
        // Stopping the transfer is normal: the server finishes the current
        // batch, so keep polling until it reports the outcome.
        poll(true, null);
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok !== false) {
        if (data.job_id) {
          state.job = data.job_id;
          if (jobField) jobField.value = data.job_id;
        }
        // The last read comes from the same place as every other one; the
        // snapshot in this response is only the fallback.
        poll(true, data.summary || null);
      } else {
        showFailure(
          (data && (data.error || errorText(data))) ||
          t('requestFailed', { status: xhr.status || 0 }),
          data && data.errors
        );
      }
    });

    xhr.open('POST', urlFor('import'), true);
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.send(payload);
  }

  function errorText(data) {
    if (!data || !data.errors) return '';
    return Object.keys(data.errors)
      .map((key) => `${key}: ${[].concat(data.errors[key]).join(' ')}`)
      .join(' · ');
  }

  /* ---------------------------------------------------------------- polling */

  function poll(final, fallbackSnapshot) {
    if (!state.job) {
      scheduleNext(final, fallbackSnapshot);
      return;
    }
    fetch(urlFor('status', state.job), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then((response) => {
        if (response.status === 404) {
          state.notFoundTries += 1;
          if (state.notFoundTries > MAX_404_RETRIES) {
            throw new Error(t('stoppedReporting'));
          }
          return null;
        }
        return response.json().then((data) => (data && data.progress) || null);
      })
      .then((snapshot) => {
        if (snapshot) {
          state.lastSnapshot = snapshot;
          renderProgress(snapshot);
        }
        if (final) {
          finish(snapshot || fallbackSnapshot);
          return;
        }
        if (snapshot && snapshot.finished) {
          finish(snapshot);
          return;
        }
        scheduleNext(false, null);
      })
      .catch((error) => {
        if (final) {
          if (fallbackSnapshot) finish(fallbackSnapshot);
          else endRun();
          return;
        }
        showFailure(error.message);
      });
  }

  function scheduleNext(final, fallback) {
    if (!state.running && !final) return;
    const snapshot = state.lastSnapshot;
    const slow = snapshot && snapshot.files_total > 400;
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(() => poll(final, fallback), slow ? POLL_SLOW : POLL_FAST);
  }

  /* -------------------------------------------------------------- rendering */

  function renderTransfer() {
    const total = state.uploadTotal || 0;
    const done = state.uploadBytes || 0;
    const percent = total ? Math.min(100, (done / total) * 100) : 0;
    setText('[data-progress-percent]', `${percent.toFixed(1)}%`);
    setWidth('[data-progress-bar]', percent);
    const bar = $('[data-progress-bar]');
    if (bar) {
      bar.setAttribute('aria-valuenow', String(Math.round(percent)));
      bar.setAttribute('aria-valuetext', t('uploadedOf', {
        done: bytesText(done),
        total: bytesText(total),
      }));
    }
    setText('[data-progress-stage]', t('sending'));
    setText('[data-progress-sub]', t('transferSub', {
      done: bytesText(done),
      total: bytesText(total),
    }));
    setText('[data-stat-bytes]', `${bytesText(done)} / ${bytesText(total)}`);
    maybeAnnounce(`${t('sending')} ${Math.round(percent)}%`);
  }

  function setRail(index) {
    $$('[data-progress-rail] li').forEach((item, position) => {
      item.classList.toggle('is-active', position === index);
      item.classList.toggle('is-done', position < index);
    });
  }

  function renderProgress(snapshot) {
    if (!snapshot) return;
    // While the browser is still sending, its own byte count is the only
    // accurate number available; the server bar starts when the ingest does.
    if (state.phase === 'transfer' && !snapshot.finished) {
      if (snapshot.stage && snapshot.stage !== 'uploading' && snapshot.stage !== 'scanning') {
        state.phase = 'ingest';
      } else {
        setRail(snapshot.stage === 'scanning' ? 1 : 0);
        return;
      }
    }
    if (snapshot.stage === 'scanning' && !snapshot.finished) setRail(1);

    const percent = snapshot.percent;
    progressCard.classList.toggle('is-idle', Boolean(snapshot.stale));

    setText('[data-progress-percent]',
      percent === null || percent === undefined ? '—' : `${Number(percent).toFixed(1)}%`);
    setWidth('[data-progress-bar]', percent);
    const track = $('[data-progress-track]');
    if (track) track.classList.toggle('is-indeterminate', percent === null || percent === undefined);
    const bar = $('[data-progress-bar]');
    if (bar) {
      if (percent === null || percent === undefined) {
        bar.removeAttribute('aria-valuenow');
        bar.removeAttribute('aria-valuetext');
      } else {
        bar.setAttribute('aria-valuenow', String(Math.round(percent)));
        bar.setAttribute('aria-valuetext', t('percentRead', {
          percent: Math.round(percent),
          total: bytesText(snapshot.bytes_total),
        }));
      }
    }

    setText('[data-progress-label]', snapshot.label || '');
    setText('[data-progress-stage]', snapshot.stage_label || '');

    const badge = $('[data-progress-state]');
    if (badge) {
      badge.textContent = snapshot.stage_label || snapshot.state;
      badge.className = `badge ${badgeClass(snapshot)}`;
    }

    setText('[data-progress-sub]', subline(snapshot));
    if (!snapshot.finished) setRail(railIndex(snapshot));

    setText('[data-stat-created]', numText(snapshot.created));
    setText('[data-stat-duplicates]', numText(snapshot.duplicates));
    setText('[data-stat-lines]', numText(snapshot.lines));
    setText('[data-stat-invalid]', numText(snapshot.invalid));
    setText('[data-stat-blank]', numText(snapshot.blank));
    setText('[data-stat-files]', `${numText(snapshot.files_done)} / ${numText(snapshot.files_total)}`);
    setText('[data-stat-bytes]', `${bytesText(snapshot.bytes_done)} / ${bytesText(snapshot.bytes_total)}`);
    setText('[data-stat-speed]', snapshot.bytes_per_second
      ? `${bytesText(snapshot.bytes_per_second)}/s · ${numText(snapshot.lines_per_second)} l/s`
      : '—');
    setText('[data-stat-eta]', snapshot.eta_seconds ? `≈ ${durationText(snapshot.eta_seconds)}` : '—');
    setText('[data-stat-elapsed]', durationText(snapshot.elapsed_seconds));

    const invalidMeter = $('[data-stat-invalid]');
    if (invalidMeter) invalidMeter.classList.toggle('is-warn', (snapshot.invalid || 0) > 0);

    renderCurrentFile(snapshot);
    renderFiles(snapshot);
    renderNotes(snapshot);

    if (!snapshot.finished && snapshot.current_file) {
      maybeAnnounce(`${snapshot.stage_label}: ${snapshot.current_file.name}, ${Math.round(percent || 0)}%`);
    }
  }

  function badgeClass(snapshot) {
    if (snapshot.state === 'done') return 'completed';
    if (snapshot.state === 'cancelled') return 'cancelled';
    if (snapshot.state === 'error') return 'failed';
    if (snapshot.stage === 'uploading' || snapshot.stage === 'queued') return 'queued';
    return 'in_progress';
  }

  function railIndex(snapshot) {
    if (snapshot.finished) return 4;
    return Number(snapshot.stage_index || 0);
  }

  function subline(snapshot) {
    const parts = [];
    if (snapshot.bytes_total) {
      parts.push(t('readOf', {
        done: bytesText(snapshot.bytes_done),
        total: bytesText(snapshot.bytes_total),
      }));
    }
    if (snapshot.files_total) {
      parts.push(t('filesDone', {
        done: numText(snapshot.files_done),
        total: numText(snapshot.files_total),
      }));
    }
    if (snapshot.pending) parts.push(t('pendingLines', { n: numText(snapshot.pending) }));
    if (snapshot.stale) parts.push(t('stale'));
    if (snapshot.cancel_requested && !snapshot.finished) parts.push(t('stopping'));
    return parts.join(' · ') || t('waitingForServer');
  }

  function renderCurrentFile(snapshot) {
    const node = $('[data-progress-file]');
    if (!node) return;
    const current = snapshot.current_file;
    if (!current) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    node.textContent = `${current.label || ''} · ${current.name}` +
      (current.percent ? ` (${Math.round(current.percent)}%)` : '');
  }

  function renderFiles(snapshot) {
    const list = $('[data-progress-files]');
    const wrap = $('[data-progress-files-wrap]');
    if (!list || !wrap) return;
    const rows = Array.isArray(snapshot.files) ? snapshot.files : [];
    if (!rows.length) {
      wrap.hidden = true;
      list.textContent = '';
      return;
    }
    wrap.hidden = false;
    const range = snapshot.files_window || [rows[0].index, rows[rows.length - 1].index];
    setText('[data-progress-files-range]', snapshot.files_total > rows.length
      ? t('rowsOf', { a: range[0], b: range[1], total: numText(snapshot.files_total) })
      : t('filesCount', { n: numText(snapshot.files_total) }));

    list.textContent = '';
    rows.forEach((entry) => list.appendChild(fileRow(entry)));

    const active = list.querySelector('.file-row.is-active');
    if (active && !isInView(active, list)) {
      list.scrollTop = Math.max(0, active.offsetTop - list.clientHeight / 3);
    }
  }

  function fileRow(entry) {
    const row = el('li', 'file-row');
    if (entry.state === 'active') row.classList.add('is-active');
    if (entry.state === 'failed') row.classList.add('is-failed');

    row.appendChild(el('span', 'file-index', String(entry.index)));

    const name = el('span', 'file-name');
    name.appendChild(el('span', null, entry.name));
    if (entry.path && entry.path !== entry.name) {
      name.appendChild(el('small', null, entry.path));
    }
    row.appendChild(name);

    row.appendChild(el('span', 'file-stats',
      `+${numText(entry.created)} · ${numText(entry.lines)} l`));

    const meter = el('div', 'file-meter');
    const miniTrack = el('div', 'mini-track');
    const fill = el('span', 'mini-fill');
    fill.style.width = `${Math.max(0, Math.min(100, entry.percent || 0))}%`;
    miniTrack.appendChild(fill);
    meter.appendChild(miniTrack);
    meter.appendChild(el('small', null, entry.bytes_total
      ? `${bytesText(entry.bytes_done)} / ${bytesText(entry.bytes_total)}`
      : bytesText(entry.bytes_done)));
    row.appendChild(meter);

    row.appendChild(el('span', 'file-state',
      entry.error ? t('unreadable') : stateWord(entry.state)));
    return row;
  }

  function isInView(node, container) {
    return node.offsetTop >= container.scrollTop &&
      node.offsetTop + node.offsetHeight <= container.scrollTop + container.clientHeight;
  }

  function stateWord(value) {
    const keys = {
      queued: 'queuedState', active: 'readingState', done: 'storedState',
      failed: 'failedState', stopped: 'stoppedState',
    };
    return keys[value] ? t(keys[value]) : value;
  }

  function renderNotes(snapshot) {
    const notes = $('[data-progress-notes]');
    if (!notes) return;
    const lines = Array.from(snapshot.warnings || []);
    if (snapshot.files_failed) {
      lines.unshift(t('filesUnreadable', { n: numText(snapshot.files_failed) }));
    }
    if (snapshot.warnings_dropped) {
      lines.push(t('moreNotes', { n: numText(snapshot.warnings_dropped) }));
    }
    if (!lines.length) {
      notes.hidden = true;
      notes.textContent = '';
      return;
    }
    notes.hidden = false;
    notes.textContent = '';
    const list = el('ul');
    lines.forEach((line) => list.appendChild(el('li', null, line)));
    notes.appendChild(list);
  }

  function finish(snapshot) {
    if (snapshot) {
      renderProgress(snapshot);
      if (snapshot.state === 'done') progressCard.classList.add('is-done');
      if (snapshot.state === 'cancelled') progressCard.classList.add('is-stopped');
      if (snapshot.state === 'error') progressCard.classList.add('is-failed');
    }
    endRun();
    setRail(4);
    renderResult(snapshot);
    announce(snapshot ? summaryText(snapshot) : t('lostJob'));
  }

  function summaryText(snapshot) {
    return t('summaryLine', {
      created: numText(snapshot.created),
      lines: numText(snapshot.lines),
      duplicates: numText(snapshot.duplicates),
      invalid: numText(snapshot.invalid),
    });
  }

  function renderResult(snapshot) {
    if (!resultBox) return;
    if (!snapshot) {
      resultBox.hidden = true;
      return;
    }
    resultBox.hidden = false;
    resultBox.textContent = '';

    const head = el('p', 'result-head');
    head.appendChild(iconNode(snapshot.state === 'done' ? 'check'
      : snapshot.state === 'cancelled' ? 'stop' : 'alert'));
    const time = durationText(snapshot.duration_seconds || snapshot.elapsed_seconds);
    head.appendChild(el('span', null, snapshot.state === 'done'
      ? t('finishedIn', { time })
      : snapshot.state === 'cancelled' ? t('stoppedAfter', { time }) : t('couldNotFinish')));
    resultBox.appendChild(head);

    if (snapshot.error) resultBox.appendChild(el('p', null, snapshot.error));

    const numbers = el('dl', 'result-numbers');
    [
      [t('newRecords'), numText(snapshot.created)],
      [t('alreadyStored'), numText(snapshot.duplicates)],
      [t('notRecords'), numText(snapshot.invalid)],
      [t('emptyLines'), numText(snapshot.blank)],
      [t('linesRead'), numText(snapshot.lines)],
      [t('files'), `${numText((snapshot.files_done || 0) - (snapshot.files_failed || 0))} / ${numText(snapshot.files_total)}`],
      [t('dataRead'), bytesText(snapshot.bytes_done)],
      [t('time'), durationText(snapshot.duration_seconds || snapshot.elapsed_seconds)],
    ].forEach(([label, value]) => {
      const cell = el('div');
      cell.appendChild(el('dt', null, label));
      cell.appendChild(el('dd', null, value));
      numbers.appendChild(cell);
    });
    resultBox.appendChild(numbers);

    resultBox.appendChild(el('p', 'field-help',
      snapshot.created === 0 && snapshot.duplicates > 0
        ? t('nothingNew')
        : t('measured')));
  }

  function showFailure(message, errors) {
    endRun();
    if (progressCard) {
      progressCard.hidden = false;
      progressCard.classList.add('is-failed');
    }
    const notes = $('[data-progress-notes]');
    if (notes) {
      notes.hidden = false;
      notes.textContent = '';
      notes.appendChild(el('p', null, message || t('couldNotFinish')));
      if (errors && typeof errors === 'object') {
        Object.keys(errors).forEach((key) => {
          notes.appendChild(el('p', null, `${key}: ${[].concat(errors[key]).join(' ')}`));
        });
      }
    }
    const badge = $('[data-progress-state]');
    if (badge) {
      badge.textContent = t('failedLabel');
      badge.className = 'badge failed';
    }
    setText('[data-progress-stage]', t('failedLabel'));
    setText('[data-progress-sub]', t('nothingAfterFailure'));
  }

  function announce(message) {
    if (liveRegion && message) liveRegion.textContent = message;
  }

  function maybeAnnounce(message) {
    const now = Date.now();
    if (now - state.lastAnnounce < ANNOUNCE_EVERY) return;
    state.lastAnnounce = now;
    announce(message);
  }

  stopButton?.addEventListener('click', () => {
    if (!state.job) return;
    stopButton.disabled = true;
    setText('[data-progress-sub]', t('finishingBatch'));
    fetch(urlFor('stop', state.job), {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRFToken': csrfToken(),
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: '',
    }).catch(() => {
      /* the import response carries the outcome if this request is lost */
    });
  });

  againButton?.addEventListener('click', () => {
    if (progressCard) {
      progressCard.hidden = true;
      progressCard.classList.remove('is-done', 'is-stopped', 'is-failed', 'is-idle');
    }
    if (resultBox) {
      resultBox.hidden = true;
      resultBox.textContent = '';
    }
    if (scanResult) {
      scanResult.hidden = true;
      scanResult.textContent = '';
    }
    const list = $('[data-progress-files]');
    if (list) list.textContent = '';
    const notes = $('[data-progress-notes]');
    if (notes) {
      notes.hidden = true;
      notes.textContent = '';
    }
    state.job = null;
    state.phase = 'idle';
    state.lastSnapshot = null;
    if (jobField) jobField.value = '';
    setRail(0);
    workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  /* --------------------------------------------------------------- wiring up */

  setMode('upload');
  renderQueue();
  setRail(0);

  if (pathInput) {
    pathInput.addEventListener('input', () => {
      if (scanResult) {
        scanResult.hidden = true;
        scanResult.textContent = '';
      }
      state.job = null;
      if (jobField) jobField.value = '';
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.metaKey || event.ctrlKey || state.running) return;
    const tag = event.target && event.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const focus = state.mode === 'path' ? pathInput : $('[data-import-input="folder"]');
    if (focus) {
      event.preventDefault();
      focus.focus();
    }
  });
})();
