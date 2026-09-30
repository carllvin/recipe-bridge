const state = {
  jobId: null,
  job: null,
  activeRecipeId: null,
  pollTimer: null,
};

const el = (id) => document.getElementById(id);

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str ?? '';
  return d.innerHTML;
}

// ---------- Job URL (survive an accidental reload / allow bookmarking) ----------

function setJobUrl(jobId) {
  const url = new URL(window.location);
  if (jobId) {
    url.searchParams.set('job', jobId);
  } else {
    url.searchParams.delete('job');
  }
  window.history.replaceState({}, '', url);
}

async function tryRestoreJobFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const jobId = params.get('job');
  if (!jobId) return;

  try {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) throw new Error('not found');
    const job = await res.json();
    state.jobId = jobId;
    state.job = job;
    updateUsageDisplay(job.token_usage);

    if (job.status === 'ready') {
      showReview();
      return;
    }

    el('upload-screen').classList.add('hidden');
    el('processing-screen').classList.remove('hidden');

    if (job.status === 'error') {
      el('processing-text').textContent = `${t('processingErrorPrefix')} ${job.error}`;
      el('progress-track').classList.add('hidden');
      return;
    }

    updateProgressUI(job);
    pollJob();
  } catch {
    // Job no longer exists (e.g. cleaned up) or the id is invalid - just
    // drop it from the URL and show the normal upload screen.
    setJobUrl(null);
  }
}

// ---------- Tandoor connection badge ----------

async function checkTandoor() {
  const badge = el('tandoor-badge');
  const banner = el('tandoor-error-banner');
  try {
    const res = await fetch('/api/tandoor/status');
    const data = await res.json();
    if (data.connected) {
      badge.textContent = t('tandoorConnected');
      badge.className = 'tandoor-badge ok tandoor-badge-clickable';
      badge.title = t('tandoorOpenHint');
      banner.classList.add('hidden');
    } else {
      badge.textContent = t('tandoorUnreachable');
      badge.className = 'tandoor-badge fail';
      badge.removeAttribute('title');
      const message = data.error || t('tandoorUnknownError');
      banner.innerHTML = `<strong>${t('tandoorConnFailedPrefix')}</strong> ${escapeHtml(message)}`;
      banner.classList.remove('hidden');
    }
  } catch {
    badge.textContent = t('tandoorUnknown');
    badge.className = 'tandoor-badge fail';
    badge.removeAttribute('title');
    banner.textContent = t('tandoorNoConfigHint');
    banner.classList.remove('hidden');
  }
}

el('tandoor-badge').addEventListener('click', () => {
  if (APP_CONFIG.manager_url && el('tandoor-badge').classList.contains('ok')) {
    window.open(APP_CONFIG.manager_url, '_blank', 'noopener');
  }
});

function isMealie() {
  return !!APP_CONFIG.recipe_manager && APP_CONFIG.recipe_manager !== 'Tandoor';
}

// Tandoor's ids are numbers, Mealie's uuids (and its meal types words).
function idValue(v) {
  return /^\d+$/.test(String(v)) ? Number(v) : v;
}

// With Mealie only some tools exist (APP_CONFIG.available_tools; null = all).
function toolAvailable(tool) {
  return !APP_CONFIG.available_tools || APP_CONFIG.available_tools.includes(tool);
}

// Link to a recipe imported into Tandoor (id) or Mealie (slug) - null without one.
function importedRecipeUrl(id) {
  return id && APP_CONFIG.recipe_url_base ? `${APP_CONFIG.recipe_url_base}${id}` : null;
}

// ---------- Token usage badge ----------

function updateUsageDisplay(usage) {
  if (!usage) return;
  const total = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  const badge = el('usage-badge');
  if (total <= 0) {
    badge.classList.add('hidden');
    return;
  }
  badge.textContent = tf('usageBadge', { total: total.toLocaleString() });
  badge.title = tf('usageBadgeTitle', {
    input: (usage.input_tokens || 0).toLocaleString(),
    output: (usage.output_tokens || 0).toLocaleString(),
  });
  badge.classList.remove('hidden');
}

// ---------- Upload ----------

const dropzone = el('dropzone');
const fileInput = el('file-input');

const FALLBACK_IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.heif'];
const FALLBACK_PDF_EXTENSIONS = ['.pdf'];
const FALLBACK_EPUB_EXTENSIONS = ['.epub'];
const SINGLE_DOC_EXTENSIONS = ['.txt', '.md', '.markdown', '.docx'];  // links or recipe text, Word
const BOOKMARK_EXTENSIONS = ['.html', '.htm'];  // browser bookmark exports

function extOf(filename) {
  const i = filename.lastIndexOf('.');
  return i === -1 ? '' : filename.slice(i).toLowerCase();
}

function classifyFiles(files) {
  const imageExts = APP_CONFIG.image_extensions || FALLBACK_IMAGE_EXTENSIONS;
  const pdfExts = FALLBACK_PDF_EXTENSIONS;
  const epubExts = FALLBACK_EPUB_EXTENSIONS;
  const exts = files.map((f) => extOf(f.name));

  if (files.length === 1 && pdfExts.includes(exts[0])) return { ok: true };
  if (files.length === 1 && epubExts.includes(exts[0])) return { ok: true };
  if (files.length === 1 && SINGLE_DOC_EXTENSIONS.includes(exts[0])) return { ok: true };
  if (files.length === 1 && ((APP_CONFIG.audio_extensions || []).includes(exts[0]) || (files[0].type || '').startsWith('audio/'))) {
    return APP_CONFIG.voice_available ? { ok: true } : { ok: false, error: t('voiceUnavailable') };
  }
  if (exts.length > 0 && exts.every((e) => imageExts.includes(e))) return { ok: true };
  if (files.length > 1 && exts.some((e) => pdfExts.includes(e) || epubExts.includes(e) || SINGLE_DOC_EXTENSIONS.includes(e) || BOOKMARK_EXTENSIONS.includes(e))) {
    return { ok: false, error: t('onlyOnePdfOrEpubError') };
  }
  return { ok: false, error: t('unsupportedFileTypeError') };
}

['dragenter', 'dragover'].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  })
);
['dragleave', 'drop'].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
  })
);
dropzone.addEventListener('drop', (e) => {
  const files = Array.from(e.dataTransfer.files || []);
  if (files.length) handleChosenFiles(files);
});
fileInput.addEventListener('change', () => {
  const files = Array.from(fileInput.files || []);
  fileInput.value = '';
  if (files.length) handleChosenFiles(files);
});

// ---------- Photo collector ----------
// Photos are gathered first (camera one at a time, or several from the
// gallery), can be reordered/removed, and are then imported together - each
// photo is one page, in this order.

const photoState = { files: [] };

function isImageFile(f) {
  return (APP_CONFIG.image_extensions || FALLBACK_IMAGE_EXTENSIONS).includes(extOf(f.name)) || (f.type || '').startsWith('image/');
}

function handleChosenFiles(files) {
  if (files.every(isImageFile)) { addPhotos(files); return; }
  if (files.length === 1 && BOOKMARK_EXTENSIONS.includes(extOf(files[0].name))) { openBookmarks(files[0]); return; }
  uploadFiles(files);
}

function addPhotos(files) {
  el('upload-error').classList.add('hidden');
  files.filter(isImageFile).forEach((f) => photoState.files.push({ file: f, url: URL.createObjectURL(f) }));
  renderPhotos();
  el('photo-collector').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderPhotos() {
  const list = photoState.files;
  el('photo-collector').classList.toggle('hidden', !list.length);
  el('photo-thumbs').innerHTML = list.map((p, i) => `
    <div class="photo-thumb">
      <img src="${p.url}" alt="">
      <span class="photo-no">${i + 1}</span>
      <div class="photo-btns">
        <button type="button" data-move="-1" data-i="${i}" ${i === 0 ? 'disabled' : ''} aria-label="${escapeHtml(t('photoMoveLeft'))}">←</button>
        <button type="button" data-remove="${i}" aria-label="${escapeHtml(t('photoRemove'))}">✕</button>
        <button type="button" data-move="1" data-i="${i}" ${i === list.length - 1 ? 'disabled' : ''} aria-label="${escapeHtml(t('photoMoveRight'))}">→</button>
      </div>
    </div>`).join('');
  el('photo-import').textContent = tf('photoImportBtn', { n: list.length });
  el('photo-thumbs').querySelectorAll('[data-move]').forEach((b) => b.addEventListener('click', () => {
    const i = Number(b.dataset.i), j = i + Number(b.dataset.move);
    [list[i], list[j]] = [list[j], list[i]];
    renderPhotos();
  }));
  el('photo-thumbs').querySelectorAll('[data-remove]').forEach((b) => b.addEventListener('click', () => {
    const [removed] = list.splice(Number(b.dataset.remove), 1);
    URL.revokeObjectURL(removed.url);
    renderPhotos();
  }));
}

function clearPhotos() {
  photoState.files.forEach((p) => URL.revokeObjectURL(p.url));
  photoState.files = [];
  el('photo-single').checked = false;
  el('photo-handwriting').checked = false;
  renderPhotos();
}

el('photo-camera').addEventListener('change', (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';  // so the same photo can be taken/picked again
  if (files.length) addPhotos(files);
});
// ---------- Dictating a recipe (voice note) ----------
// Records in the browser and uploads the recording like a file - the
// server transcribes it and reads the recipe from the text.
const voiceState = { recorder: null, chunks: [] };

function initVoice() {
  const ok = APP_CONFIG.voice_available && window.MediaRecorder && navigator.mediaDevices;
  el('voice-record').classList.toggle('hidden', !ok);
}

// Moving recipes from the other recipe manager (Tandoor <-> Mealie).
function initMigration() {
  const source = APP_CONFIG.migration_source;
  el('more-way-move').classList.toggle('hidden', !source);
  if (!source) return;
  el('move-title').textContent = tf('moveTitle', { source });
  el('move-hint').textContent = tf('moveHint', { source });
}

let moveCookbooksLoaded = false;
document.querySelector('.more-ways').addEventListener('toggle', async (e) => {
  if (!e.target.open || moveCookbooksLoaded || !APP_CONFIG.migration_source) return;
  moveCookbooksLoaded = true;
  try {
    const data = await (await fetch('/api/migration/cookbooks')).json();
    const select = el('move-cookbook');
    (data.names || []).forEach((name) => {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    });
  } catch (err) { moveCookbooksLoaded = false; }
});

el('move-start').addEventListener('click', () => {
  startImportRequest('/api/migration', { cookbook: el('move-cookbook').value },
    tf('moveLoading', { source: APP_CONFIG.migration_source }));
});

el('voice-record').addEventListener('click', async () => {
  const btn = el('voice-record');
  if (voiceState.recorder) {  // second click: stop and import
    voiceState.recorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = ['audio/webm', 'audio/ogg', 'audio/mp4'].find((m) => MediaRecorder.isTypeSupported(m)) || '';
    const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    voiceState.chunks = [];
    recorder.ondataavailable = (e) => { if (e.data.size) voiceState.chunks.push(e.data); };
    recorder.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      const mime = recorder.mimeType || type || 'audio/webm';
      const ext = mime.includes('ogg') ? '.ogg' : mime.includes('mp4') ? '.m4a' : '.webm';
      const blob = new Blob(voiceState.chunks, { type: mime });
      voiceState.recorder = null;
      btn.textContent = t('voiceRecordBtn');
      btn.classList.remove('recording');
      if (blob.size > 2000) uploadFiles([new File([blob], `${t('voiceFileName')}${ext}`, { type: mime })], {});
    };
    recorder.start();
    voiceState.recorder = recorder;
    btn.textContent = t('voiceStopBtn');
    btn.classList.add('recording');
  } catch (e) {
    alert(`${t('voiceMicFailed')}: ${e.message}`);
  }
});

el('photo-clear').addEventListener('click', clearPhotos);
el('photo-import').addEventListener('click', () => {
  const files = photoState.files.map((p) => p.file);
  if (!files.length) return;
  const single = el('photo-single').checked;
  const handwriting = el('photo-handwriting').checked;
  clearPhotos();
  uploadFiles(files, { singleRecipe: single, handwriting });
});

async function uploadFiles(files, options = {}) {
  el('upload-error').classList.add('hidden');
  requestNotificationPermission();

  const check = classifyFiles(files);
  if (!check.ok) {
    showUploadError(check.error);
    return;
  }

  const formData = new FormData();
  files.forEach((f) => formData.append('files', f));
  if (options.singleRecipe) formData.append('single_recipe', 'true');
  if (options.handwriting) formData.append('handwriting', 'true');

  const label = files.length === 1 ? files[0].name : tf('photosCount', { n: files.length });

  el('upload-screen').classList.add('hidden');
  el('processing-screen').classList.remove('hidden');
  el('progress-track').classList.add('hidden');
  el('usage-badge').classList.add('hidden');
  el('processing-text').textContent = tf('uploadingFile', { filename: label });

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `${t('uploadFailedPrefix')} (${res.status})`);
    }
    const data = await res.json();
    state.jobId = data.job_id;
    setJobUrl(data.job_id);
    el('processing-text').textContent = t('processingDefault');
    pollJob();
  } catch (e) {
    el('processing-screen').classList.add('hidden');
    el('upload-screen').classList.remove('hidden');
    showUploadError(e.message);
  }
}

// Single recipe from a web page: same processing / review / import flow.
// Starts an import that isn't a file upload (web page, pasted text, picked
// links) and switches to the processing screen.
async function startImportRequest(endpoint, body, loadingText, onStarted) {
  el('upload-error').classList.add('hidden');
  requestNotificationPermission();
  el('upload-screen').classList.add('hidden');
  el('processing-screen').classList.remove('hidden');
  el('progress-track').classList.add('hidden');
  el('usage-badge').classList.add('hidden');
  el('processing-text').textContent = loadingText;
  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `${t('uploadFailedPrefix')} (${res.status})`);
    }
    const data = await res.json();
    state.jobId = data.job_id;
    setJobUrl(data.job_id);
    if (onStarted) onStarted();
    pollJob();
  } catch (err) {
    el('processing-screen').classList.add('hidden');
    el('upload-screen').classList.remove('hidden');
    showUploadError(err.message);
  }
}

// ---------- One field for links and pasted text ----------

// "www.site.de/rezept" or "site.de/..." without https:// counts as a link too.
function smartLink(value) {
  if (/^https?:\/\/\S+$/i.test(value)) return value;
  if (/^(www\.)?[\w-]+(\.[\w-]+)*\.[a-z]{2,}(\/\S*)?$/i.test(value)) return 'https://' + value;
  return null;
}

function isVideoLink(url) {
  try {
    const host = new URL(url).hostname.replace(/^(www|m)\./, '');
    return /(^|\.)(youtube\.com|youtu\.be|instagram\.com|tiktok\.com)$/.test(host);
  } catch (e) { return false; }
}

function smartMode() {
  const value = el('smart-input').value.trim();
  const link = !value.includes('\n') ? smartLink(value) : null;
  if (link) return { mode: 'url', value: link };
  if (value.length >= 20) return { mode: 'text', value };
  return { mode: null, value };
}

function updateSmartForm() {
  const input = el('smart-input');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 320)}px`;
  const { mode } = smartMode();
  el('smart-import').disabled = !mode;
  el('smart-import').textContent = t(mode === 'text' ? 'smartImportText' : mode === 'url' ? 'smartImportUrl' : 'smartImport');
  // A video link: the recipe comes from the description / subtitles - no website scan.
  const video = mode === 'url' && isVideoLink(smartMode().value);
  el('smart-scan').classList.toggle('hidden', mode !== 'url' || video);
  el('smart-depth-wrap').classList.toggle('hidden', mode !== 'url' || video);
  el('smart-hint').textContent = video ? t('smartHintVideo')
    : mode === 'url' ? `${t('smartHintUrl')} ${t('scanDepthHint')}` : mode === 'text' ? t('smartHintText') : '';
}

function clearSmartForm() {
  el('smart-input').value = '';
  updateSmartForm();
}

el('smart-input').addEventListener('input', updateSmartForm);
// Enter imports a link; in a recipe text it's a new line.
el('smart-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && smartMode().mode === 'url') {
    e.preventDefault();
    el('smart-form').requestSubmit();
  }
});

el('smart-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const { mode, value } = smartMode();
  if (mode === 'url') startImportRequest('/api/import-url', { url: value }, t('urlImportLoading'), clearSmartForm);
  else if (mode === 'text') startImportRequest('/api/import-text', { text: value }, t('textImportLoading'), clearSmartForm);
});

// ---------- Pick list: recipes found on a website, or browser bookmarks ----------

const pickState = { mode: null, items: [], selected: new Set(), scanId: null, timer: null, name: '' };
const MAX_PICK = 200;

function openPickList(mode, title, items, name) {
  clearTimeout(pickState.timer);
  Object.assign(pickState, { mode, items, selected: new Set(), scanId: null, name });
  el('pick-title').textContent = title;
  el('pick-filter').value = '';
  const folders = [...new Set(items.map((i) => i.folder).filter(Boolean))].sort();
  el('pick-folder').classList.toggle('hidden', mode !== 'bookmarks' || folders.length < 2);
  el('pick-folder').innerHTML = `<option value="">${escapeHtml(t('pickAllFolders'))}</option>`
    + folders.map((f) => `<option value="${escapeHtml(f)}">${escapeHtml(f)}</option>`).join('');
  el('pick-stop').classList.add('hidden');
  el('pick-status').textContent = mode === 'bookmarks' ? tf('pickBookmarksCount', { n: items.length }) : '';
  el('pick-panel').classList.remove('hidden');
  renderPickList();
  el('pick-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function visiblePickItems() {
  const q = el('pick-filter').value.trim().toLowerCase();
  const folder = el('pick-folder').value;
  return pickState.items.filter((i) => (!folder || i.folder === folder)
    && (!q || `${i.title} ${i.url} ${i.folder || ''}`.toLowerCase().includes(q)));
}

function renderPickList() {
  const items = visiblePickItems();
  el('pick-list').innerHTML = items.length ? items.map((i) => `
    <label class="pick-row">
      <input type="checkbox" data-url="${escapeHtml(i.url)}" ${pickState.selected.has(i.url) ? 'checked' : ''}>
      ${pickState.mode === 'scan' ? (i.image ? `<img src="${escapeHtml(i.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('span'), { className: 'pick-noimg' }))">` : '<span class="pick-noimg"></span>') : ''}
      <span class="pick-main">
        <span class="pick-name">${escapeHtml(i.title || i.url)}<a href="${escapeHtml(i.url)}" target="_blank" rel="noopener" title="${escapeHtml(i.url)}">↗</a>
          ${i.in_tandoor ? `<span class="pick-badge">${t('pickInTandoor')}</span>` : ''}</span>
        <span class="pick-meta">${escapeHtml([i.minutes ? `${i.minutes} min` : '', i.folder || '', hostOf(i.url)].filter(Boolean).join(' · '))}</span>
      </span>
    </label>`).join('') : `<p class="settings-hint">${t(pickState.mode === 'scan' && pickState.scanId ? 'pickSearching' : 'pickNone')}</p>`;
  el('pick-list').querySelectorAll('input[type=checkbox]').forEach((cb) => cb.addEventListener('change', () => {
    if (cb.checked) pickState.selected.add(cb.dataset.url); else pickState.selected.delete(cb.dataset.url);
    updatePickFoot();
  }));
  el('pick-all').checked = items.length > 0 && items.every((i) => pickState.selected.has(i.url));
  updatePickFoot();
}

function updatePickFoot() {
  const n = pickState.selected.size;
  el('pick-import').textContent = tf('pickImportBtn', { n });
  el('pick-import').disabled = n === 0 || n > MAX_PICK;
  el('pick-hint').textContent = n > MAX_PICK ? tf('pickLimit', { n: MAX_PICK }) : '';
}

el('pick-filter').addEventListener('input', renderPickList);
el('pick-folder').addEventListener('change', renderPickList);
el('pick-all').addEventListener('change', () => {
  visiblePickItems().forEach((i) => {
    if (el('pick-all').checked) pickState.selected.add(i.url); else pickState.selected.delete(i.url);
  });
  renderPickList();
});
el('pick-close').addEventListener('click', () => {
  if (pickState.scanId) fetch(`/api/scan/${pickState.scanId}/cancel`, { method: 'POST' }).catch(() => {});
  clearTimeout(pickState.timer);
  pickState.scanId = null;
  el('pick-panel').classList.add('hidden');
});
el('pick-stop').addEventListener('click', () => {
  if (pickState.scanId) fetch(`/api/scan/${pickState.scanId}/cancel`, { method: 'POST' }).catch(() => {});
});
el('pick-import').addEventListener('click', () => {
  const urls = pickState.items.filter((i) => pickState.selected.has(i.url)).map((i) => i.url);
  if (!urls.length) return;
  if (pickState.scanId) fetch(`/api/scan/${pickState.scanId}/cancel`, { method: 'POST' }).catch(() => {});
  clearTimeout(pickState.timer);
  const folder = el('pick-folder').value;
  el('pick-panel').classList.add('hidden');
  startImportRequest('/api/import-links', { urls, name: folder || pickState.name }, tf('linksImportLoading', { n: urls.length }));
});

// Website scan: runs on the server (no AI); results show up while it's running.
el('smart-scan').addEventListener('click', () => {
  const { mode, value: url } = smartMode();
  if (mode !== 'url') return;
  const depth = Number(el('smart-depth').value) || 2;
  try { localStorage.setItem('th.scanDepth', String(depth)); } catch (e) { /* not available */ }
  startSiteScan(url, depth);
});

async function startSiteScan(url, depth) {
  el('upload-error').classList.add('hidden');
  try {
    const res = await fetch('/api/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url, depth }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    openPickList('scan', tf('pickTitleScan', { host: hostOf(url) }), [], hostOf(url));
    pickState.scanId = data.scan_id;
    pollScan();
  } catch (err) {
    showUploadError(err.message);
  }
}

async function pollScan() {
  const scanId = pickState.scanId;
  if (!scanId) return;
  let st;
  try {
    st = await (await fetch(`/api/scan/${scanId}`)).json();
  } catch (e) { pickState.timer = setTimeout(pollScan, 3000); return; }
  if (pickState.scanId !== scanId) return;
  const known = new Set(pickState.items.map((i) => i.url));
  const added = (st.found || []).filter((f) => !known.has(f.url));
  if (added.length) pickState.items = pickState.items.concat(added);
  const running = st.status === 'scanning';
  const source = st.source === 'sitemap' ? t('pickSourceSitemap') : st.source === 'links' ? t('pickSourceLinks') : '';
  el('pick-status').textContent = st.status === 'error' ? `${t('toolStatusError')}: ${st.error}`
    : running && !st.checked ? t('pickStatusStarting')  // still reading robots.txt / sitemaps
    : tf(running ? 'pickStatusScanning' : st.status === 'cancelled' ? 'pickStatusCancelled' : 'pickStatusDone',
      { checked: st.checked, total: st.total, found: pickState.items.length }) + (source ? ` · ${source}` : '');
  el('pick-stop').classList.toggle('hidden', !running);
  if (!running) pickState.scanId = null;
  if (added.length || !running) renderPickList();
  if (running) pickState.timer = setTimeout(pollScan, 1500);
}

// Browser bookmarks (the HTML export of Chrome, Firefox, Safari, Edge) are
// read right here in the browser - the folder of each link becomes a filter.
async function openBookmarks(file) {
  const doc = new DOMParser().parseFromString(await file.text(), 'text/html');
  const items = [];
  doc.querySelectorAll('a[href]').forEach((a) => {
    const url = a.getAttribute('href');
    if (!/^https?:\/\//i.test(url)) return;
    const folders = [];
    for (let node = a.parentElement; node; node = node.parentElement) {
      if (node.tagName === 'DL') {
        const heading = node.previousElementSibling && node.previousElementSibling.tagName === 'H3'
          ? node.previousElementSibling
          : node.parentElement && node.parentElement.querySelector(':scope > h3');
        if (heading) folders.unshift(heading.textContent.trim());
      }
    }
    if (!items.some((i) => i.url === url)) items.push({ url, title: a.textContent.trim(), folder: folders.join(' / ') });
  });
  if (!items.length) { showUploadError(t('pickBookmarksNone')); return; }
  openPickList('bookmarks', t('pickTitleBookmarks'), items, t('pickTitleBookmarks'));
}

function showUploadError(msg) {
  const box = el('upload-error');
  box.textContent = msg;
  box.classList.remove('hidden');
}

// ---------- Browser notifications (so you can leave the tab during a long extraction) ----------

function requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
}

function notifyJobFinished(job) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (document.visibilityState === 'visible') return; // don't bug the user if they're already looking at it

  const count = (job.recipes && job.recipes.length) || 0;
  let title;
  let body;
  if (job.status === 'ready') {
    title = t('notificationReadyTitle');
    body = tf('notificationReadyBody', { n: count });
  } else {
    title = t('notificationErrorTitle');
    body = job.error || '';
  }

  try {
    const notification = new Notification(title, { body, icon: undefined, tag: `job-${job.id}` });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch {
    // Some browsers/contexts (e.g. no service worker, insecure origin) can
    // reject the Notification constructor - fail silently, it's a nice-to-have.
  }
}

// ---------- Polling ----------

function pollJob() {
  clearTimeout(state.pollTimer);
  const tick = async () => {
    try {
      const res = await fetch(`/api/jobs/${state.jobId}`);
      if (!res.ok) throw new Error(t('jobNotFoundError'));
      const job = await res.json();
      state.job = job;
      updateUsageDisplay(job.token_usage);

      if (job.status === 'ready') {
        notifyJobFinished(job);
        showReview();
        return;
      }
      if (job.status === 'error') {
        notifyJobFinished(job);
        el('processing-text').textContent = `${t('processingErrorPrefix')} ${job.error}`;
        el('progress-track').classList.add('hidden');
        return;
      }

      updateProgressUI(job);
      state.pollTimer = setTimeout(tick, 2000);
    } catch (e) {
      el('processing-text').textContent = `${t('processingErrorPrefix')} ${e.message}`;
    }
  };
  tick();
}

function updateProgressUI(job) {
  el('processing-text').textContent = job.progress_label || t('processingDefault');
  const track = el('progress-track');
  const fill = el('progress-fill');
  if (job.progress_total > 0) {
    track.classList.remove('hidden');
    const pct = Math.min(100, Math.round((job.progress_current / job.progress_total) * 100));
    fill.style.width = `${pct}%`;
  } else {
    track.classList.add('hidden');
  }
}

// ---------- Review screen ----------

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return url; }
}

function showReview() {
  // also when a finished import is opened straight from the address (?job=)
  el('upload-screen').classList.add('hidden');
  el('processing-screen').classList.add('hidden');
  el('review-screen').classList.remove('hidden');
  el('action-bar').classList.remove('hidden');
  setCookbook(state.job.cookbook_name || state.job.suggested_cookbook_name || '');
  loadCookbooks();
  updateUsageDisplay(state.job.token_usage);
  // e.g. links of a link list that couldn't be read
  let notes = state.job.notes || [];
  let summary = tf('reviewNotesSummary', { n: notes.length });
  if (state.job.source === 'migration') {  // moved recipes also go into their old cookbooks
    notes = [...new Set(state.job.recipes.flatMap((r) => r.cookbooks || []))]
      .filter((b) => b !== state.job.cookbook_name).sort((a, b) => a.localeCompare(b));
    summary = tf('moveCookbooksSummary', { n: notes.length });
  }
  el('review-notes').classList.toggle('hidden', !notes.length);
  el('review-notes-summary').textContent = summary;
  el('review-notes-list').innerHTML = notes.map((n) => `<li>${escapeHtml(n)}</li>`).join('');
  renderRecipeList();
  updateSelectionCount();
  if (state.job.recipes.length > 0) {
    selectRecipe(state.job.recipes[0].id);
  }
}

// Cookbook: a choice of the existing ones plus "new cookbook …" (a name
// field). #cookbook-name-input always holds the name that is used - for an
// existing cookbook it's just hidden. Without the list (not reachable) only
// the name field shows.
const NEW_COOKBOOK = '__new__';
const cookbookState = { names: null };

async function loadCookbooks() {
  if (cookbookState.names === null) {
    try {
      const data = await (await fetch('/api/cookbooks')).json();
      cookbookState.names = data.error ? [] : data.names;
    } catch (e) { cookbookState.names = []; }
  }
  setCookbook(el('cookbook-name-input').value.trim());
}

function setCookbook(name) {
  const select = el('cookbook-select');
  const input = el('cookbook-name-input');
  const names = cookbookState.names || [];
  input.value = name;
  if (!names.length) {  // no list: just the name field
    select.classList.add('hidden');
    input.classList.remove('hidden');
    el('cookbook-hint').classList.remove('hidden');
    return;
  }
  const existing = names.find((n) => n.toLowerCase() === name.toLowerCase());
  select.innerHTML = `<option value="">${escapeHtml(t('cookbookNone'))}</option>`
    + names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('')
    + `<option value="${NEW_COOKBOOK}">${escapeHtml(t('cookbookNew'))}</option>`;
  select.value = existing || (name ? NEW_COOKBOOK : '');
  if (existing) input.value = existing;
  const isNew = select.value === NEW_COOKBOOK;
  select.classList.remove('hidden');
  input.classList.toggle('hidden', !isNew);
  el('cookbook-hint').classList.toggle('hidden', !isNew);
}

function saveCookbook() {
  const value = el('cookbook-name-input').value.trim();
  state.job.cookbook_name = value;
  fetch(`/api/jobs/${state.jobId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cookbook_name: value }),
  }).catch(() => {});
}

el('cookbook-select').addEventListener('change', () => {
  const value = el('cookbook-select').value;
  const input = el('cookbook-name-input');
  if (value === NEW_COOKBOOK) {
    // start from the suggested name unless it is one of the existing ones
    const suggested = state.job.suggested_cookbook_name || '';
    const known = (cookbookState.names || []).some((n) => n.toLowerCase() === suggested.toLowerCase());
    input.value = known ? '' : suggested;
    input.classList.remove('hidden');
    el('cookbook-hint').classList.remove('hidden');
    input.focus();
    input.select();
  } else {
    input.value = value;
    input.classList.add('hidden');
    el('cookbook-hint').classList.add('hidden');
  }
  saveCookbook();
});

el('cookbook-name-input').addEventListener('blur', saveCookbook);

function imageUrl(imageId) {
  if (!imageId) return null;
  return `/api/jobs/${state.jobId}/images/${imageId}`;
}

function renderRecipeList() {
  const container = el('recipe-list-items');
  container.innerHTML = '';
  const recipes = state.job.recipes;
  el('recipe-count-label').textContent = tf(recipes.length === 1 ? 'recipesFound' : 'recipesFoundPlural', { n: recipes.length });

  recipes.forEach((r) => {
    const item = document.createElement('div');
    item.className = 'recipe-item' + (r.id === state.activeRecipeId ? ' active' : '');
    const opensInTandoor = r.import_status === 'imported' && importedRecipeUrl(r.tandoor_recipe_id);
    if (opensInTandoor) item.title = t('tandoorOpenRecipeHint');
    item.innerHTML = `
      <input type="checkbox" ${r.selected ? 'checked' : ''} data-id="${r.id}" class="select-cb" />
      <div class="thumb" style="${r.selected_image_id ? `background-image:url('${imageUrl(r.selected_image_id)}')` : ''}"></div>
      <div class="meta">
        <div class="title">${escapeHtml(r.title)}</div>
        <div class="sub">${r.source_url ? escapeHtml(hostOf(r.source_url)) : `${t('pageLabel')} ${r.source_page_start}${r.source_page_end !== r.source_page_start ? '–' + r.source_page_end : ''}`}</div>
        ${duplicateBadge(r)}
        ${statusPill(r)}
      </div>
    `;
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('select-cb')) return;
      if (opensInTandoor) {
        window.open(importedRecipeUrl(r.tandoor_recipe_id), 'tandoorRecipePopup', 'width=900,height=850,noopener');
        return;
      }
      selectRecipe(r.id);
      // phones: the recipe shows below the list - bring it into view
      if (window.matchMedia('(max-width: 800px)').matches) {
        el('recipe-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
    item.querySelector('.select-cb').addEventListener('click', (e) => {
      e.stopPropagation();
      toggleSelected(r.id, e.target.checked);
    });
    container.appendChild(item);
  });

  updateRetryButtonVisibility();
}

function updateRetryButtonVisibility() {
  const hasFailed = state.job.recipes.some((r) => r.import_status === 'error');
  el('retry-failed-btn').classList.toggle('hidden', !hasFailed);

  const hasImported = state.job.recipes.some((r) => r.import_status === 'imported');
  el('undo-all-btn').classList.toggle('hidden', !hasImported);
}

el('undo-all-btn').addEventListener('click', async () => {
  if (!confirm(t('confirmUndoAll'))) return;

  const btn = el('undo-all-btn');
  btn.disabled = true;
  btn.textContent = t('undoingAll');

  try {
    const res = await fetch(`/api/jobs/${state.jobId}/undo-all-imports`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || t('undoAllFailed'));
    }
    const data = await res.json();
    if (state.jobId !== jobId) return;  // left the review meanwhile - the import still ran
    data.results.forEach((result) => {
      const r = findRecipe(result.id);
      if (!r) return;
      if (result.status === 'undone') {
        r.import_status = 'pending';
        r.tandoor_recipe_id = null;
        r.import_error = null;
      } else {
        r.import_error = result.error;
      }
    });
    renderRecipeList();
    if (state.activeRecipeId) renderDetail(findRecipe(state.activeRecipeId));
  } catch (e) {
    alert(`${t('undoAllFailed')}: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = t('undoAllBtn');
  }
});

function duplicateBadge(r) {
  if (!r.duplicate_match) return '';
  const cls = r.duplicate_exact ? 'duplicate' : 'duplicate-similar';
  const label = r.duplicate_exact ? t('duplicateBadgeExact') : t('duplicateBadgeSimilar');
  return `<span class="status-pill ${cls}">⚠ ${label}</span>`;
}

function statusPill(r) {
  if (r.import_status === 'pending') return '';
  const labels = { importing: t('statusImporting'), imported: t('statusImported'), error: t('statusError') };
  return `<span class="status-pill ${r.import_status}">${labels[r.import_status] || r.import_status}</span>`;
}

function findRecipe(id) {
  return state.job.recipes.find((r) => r.id === id);
}

function toggleSelected(id, checked) {
  const r = findRecipe(id);
  r.selected = checked;
  patchRecipe(id, { selected: checked });
  updateSelectionCount();
}

function updateSelectionCount() {
  const n = state.job.recipes.filter((r) => r.selected).length;
  el('selection-count').textContent = tf('selectionCount', { n, total: state.job.recipes.length });
}

el('toggle-all-btn').addEventListener('click', () => {
  const allSelected = state.job.recipes.every((r) => r.selected);
  state.job.recipes.forEach((r) => { r.selected = !allSelected; });
  renderRecipeList();
  updateSelectionCount();
  state.job.recipes.forEach((r) => patchRecipe(r.id, { selected: r.selected }));
});

function selectRecipe(id) {
  state.activeRecipeId = id;
  renderRecipeList();
  renderDetail(findRecipe(id));
  scrollActiveRecipeIntoView();
}

function scrollActiveRecipeIntoView() {
  const activeEl = document.querySelector('.recipe-item.active');
  if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

// ---------- Keyboard navigation (Up/Down = move, Space = toggle selected) ----------

document.addEventListener('keydown', (e) => {
  if (!state.job || !state.job.recipes || state.job.recipes.length === 0) return;
  if (el('review-screen').classList.contains('hidden')) return;

  // Don't hijack typing in a field, or interact while a modal is open
  const activeTag = document.activeElement ? document.activeElement.tagName : '';
  if (activeTag === 'INPUT' || activeTag === 'TEXTAREA' || activeTag === 'SELECT') return;
  if (!el('success-modal').classList.contains('hidden')) return;

  const recipes = state.job.recipes;

  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const currentIdx = recipes.findIndex((r) => r.id === state.activeRecipeId);
    let nextIdx;
    if (currentIdx === -1) {
      nextIdx = 0;
    } else if (e.key === 'ArrowDown') {
      nextIdx = Math.min(currentIdx + 1, recipes.length - 1);
    } else {
      nextIdx = Math.max(currentIdx - 1, 0);
    }
    selectRecipe(recipes[nextIdx].id);
  } else if (e.code === 'Space' || e.key === ' ') {
    if (!state.activeRecipeId) return;
    e.preventDefault();
    const r = findRecipe(state.activeRecipeId);
    if (r) {
      toggleSelected(r.id, !r.selected);
      renderRecipeList();
    }
  }
});

function matchBadgeHtml(ing) {
  if (ing.tandoor_match === 'exists') {
    return `<span class="ing-match exists" title="${escapeHtml(t('matchExistsTitle'))}">✓</span>`;
  }
  if (ing.tandoor_match === 'matched') {
    const title = tf('matchMatchedTitle', { original: ing.original_name || '' });
    return `<span class="ing-match matched" role="button" title="${escapeHtml(title)}">↺</span>`;
  }
  if (ing.tandoor_match === 'new') {
    return `<span class="ing-match new" title="${escapeHtml(t('matchNewTitle'))}">${t('matchNewLabel')}</span>`;
  }
  return '';
}

// What the AI read before the match with Tandoor ("Zwiebeln" -> "Zwiebel"),
// shown under the ingredient.
function originalLineHtml(ing) {
  const parts = [];
  if (ing.tandoor_match === 'matched' && ing.original_name && ing.original_name !== ing.name) parts.push(ing.original_name);
  if (ing.unit_match === 'matched' && ing.original_unit && ing.original_unit !== ing.unit) parts.push(`${t('placeholderUnit')}: ${ing.original_unit}`);
  return parts.length ? `<div class="ing-original">${escapeHtml(tf('matchOriginalLine', { original: parts.join(' · ') }))}</div>` : '';
}

function unitBadgeHtml(ing) {
  if (!ing.unit) return '';
  if (ing.unit_match === 'matched') {
    return `<span class="ing-match matched unit-match" role="button" title="${escapeHtml(tf('unitMatchedTitle', { original: ing.original_unit || '' }))}">↺</span>`;
  }
  if (ing.unit_match === 'new') {
    return `<span class="ing-match new unit-match" title="${escapeHtml(t('unitNewTitle'))}">${t('matchNewLabel')}</span>`;
  }
  return '';
}

// Existing tag names in Tandoor, loaded once for the "add a tag" field.
const tagState = { names: null };
async function loadExistingTags() {
  if (tagState.names) return tagState.names;
  try {
    tagState.names = (await (await fetch('/api/tandoor/tags')).json()).tags || [];
  } catch (e) {
    tagState.names = [];
  }
  el('tag-options').innerHTML = tagState.names.map((n) => `<option value="${escapeHtml(n)}"></option>`).join('');
  return tagState.names;
}

function tagChipHtml(r, tag, i) {
  const status = (r.tag_status || {})[tag];
  const original = (r.tag_original || {})[tag];
  const marker = status === 'new' ? `<span class="tag-new">${t('matchNewLabel')}</span>`
    : status === 'matched' ? `<button type="button" class="tag-revert" data-idx="${i}" title="${escapeHtml(tf('tagMatchedTitle', { original: original || '' }))}">↺</button>` : '';
  const was = status === 'matched' && original ? `<span class="tag-original">(${escapeHtml(original)})</span>` : '';
  return `<span class="tag ${status || ''}">${escapeHtml(tag)}${was}${marker}<button type="button" class="tag-remove" data-idx="${i}" aria-label="${escapeHtml(t('tagRemove'))}">×</button></span>`;
}

function renderDetail(r) {
  const detail = el('recipe-detail');

  const ingredientsHtml = r.ingredients.map((ing, i) => {
    const stepOptions = r.steps.map((s, si) => `<option value="${si}" ${((ing.step_index ?? 0) === si) ? 'selected' : ''}>${si + 1}</option>`).join('');
    return `
    <div class="ingredient-row ${originalLineHtml(ing) ? 'has-original' : ''}" data-idx="${i}" draggable="true">
      <span class="ing-drag-handle" title="${t('dragToReorder')}">⠿</span>
      <input class="ing-amount" value="${ing.amount ?? ''}" placeholder="${t('placeholderAmount')}" />
      <div class="ing-unit-wrap">
        <input class="ing-unit" value="${escapeHtml(ing.unit ?? '')}" placeholder="${t('placeholderUnit')}" />
        ${unitBadgeHtml(ing)}
      </div>
      <div class="ing-name-wrap">
        <input class="ing-name" value="${escapeHtml(ing.name)}" placeholder="${t('placeholderIngredient')}" />
        ${matchBadgeHtml(ing)}
      </div>
      <select class="ing-step" title="${t('fieldStepAssignment')}">${stepOptions || '<option value="0">1</option>'}</select>
      ${originalLineHtml(ing)}
      <input class="ing-note" value="${escapeHtml(ing.note ?? '')}" placeholder="${t('placeholderNote')}" />
    </div>
  `;
  }).join('') || `<p style="color:#8f9689;font-size:0.88rem;">${t('noIngredients')}</p>`;

  const matchCounts = { exists: 0, matched: 0, new: 0 };
  r.ingredients.forEach((ing) => { if (ing.tandoor_match) matchCounts[ing.tandoor_match] += 1; });
  const matchSummary = (matchCounts.exists + matchCounts.matched + matchCounts.new) > 0
    ? `<div class="ing-match-summary">${escapeHtml(tf('matchSummary', matchCounts))}</div>`
    : '';

  const stepsHtml = r.steps.map((s, i) => `
    <div class="step-row" data-idx="${i}">
      <div class="step-num">${i + 1}</div>
      <textarea class="step-instruction">${escapeHtml(s.instruction)}</textarea>
    </div>
  `).join('') || `<p style="color:#8f9689;font-size:0.88rem;">${t('noSteps')}</p>`;

  const tagsHtml = r.tags.map((tag, i) => tagChipHtml(r, tag, i)).join('')
    + `<input class="tag-add" list="tag-options" placeholder="${escapeHtml(t('tagAddPlaceholder'))}" />`;

  const generateTileHtml = APP_CONFIG.image_gen_available
    ? `<div class="image-choice generate-tile" data-generate="1" title="${t('generateImageBtn')}">
         <span class="generate-tile-icon">✨</span>
       </div>`
    : '';

  const imageChoicesHtml = r.candidate_image_ids.map((iid) => {
    const ai = ((state.job.images || {})[iid] || {}).ai;
    const badge = ai ? `<span class="image-ai-badge">${t(ai === 'enhanced' ? 'imageBadgeEnhanced' : 'imageBadgeGenerated')}</span>` : '';
    // The selected photo from the source can be improved by the image AI.
    const enhance = APP_CONFIG.image_gen_available && !ai && iid === r.selected_image_id
      ? `<button type="button" class="image-enhance-btn" data-enhance="${iid}" title="${escapeHtml(t('enhanceImageBtn'))}"><span>✨</span></button>` : '';
    return `<div class="image-choice ${iid === r.selected_image_id ? 'selected' : ''}" data-image-id="${iid}"
         style="background-image:url('${imageUrl(iid)}')">${badge}${enhance}</div>`;
  }).join('') + `<div class="image-choice none ${!r.selected_image_id ? 'selected' : ''}" data-image-id="">${t('noImage')}</div>` + generateTileHtml;

  const duplicateNotice = r.duplicate_match
    ? `<div class="error-banner duplicate-banner" style="margin-bottom:20px;">${escapeHtml(
        tf(r.duplicate_exact ? 'duplicateExactWarning' : 'duplicateSimilarWarning', { name: r.duplicate_match })
      )}</div>`
    : '';

  const tandoorLink = importedRecipeUrl(r.tandoor_recipe_id)
    ? `<a class="btn secondary" href="${escapeHtml(importedRecipeUrl(r.tandoor_recipe_id))}" target="_blank" rel="noopener">${t('openInTandoorBtn')}</a>`
    : '';

  const importedNotice = r.import_status === 'imported'
    ? `<div class="imported-banner">
         <span>${t('importedBannerText')}</span>
         <div class="imported-banner-actions">
           ${tandoorLink}
           <button class="btn secondary undo-import-btn" type="button">${t('undoImportBtn')}</button>
         </div>
       </div>`
    : '';

  detail.innerHTML = `
    ${r.import_error ? `<div class="error-banner" style="margin-bottom:20px;">${escapeHtml(r.import_error)}</div>` : ''}
    ${duplicateNotice}
    ${importedNotice}
    <div class="field title-field">
      <input id="f-title" value="${escapeHtml(r.title)}" />
    </div>
    <div class="field description-field">
      <textarea id="f-description" placeholder="${t('descriptionPlaceholder')}">${escapeHtml(r.description ?? '')}</textarea>
    </div>
    ${r.source_url ? `<div class="source-line">${t('sourceUrlLabel')}: <a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">${escapeHtml(r.source_url.replace(/^https?:\/\/(www\.)?/, ''))}</a></div>` : ''}

    <div class="section-title">${t('fieldImage')}</div>
    <div class="image-picker">${imageChoicesHtml}</div>

    <div class="section-title">${t('fieldDetails')}</div>
    <div class="field-row">
      <div class="field"><label>${t('fieldServings')}</label><input id="f-servings" type="number" value="${r.servings ?? ''}" /></div>
      <div class="field"><label>${t('fieldPrep')}</label><input id="f-prep" type="number" value="${r.prep_time_minutes ?? ''}" /></div>
      <div class="field"><label>${t('fieldCook')}</label><input id="f-cook" type="number" value="${r.cook_time_minutes ?? ''}" /></div>
    </div>

    <div class="section-title">${t('fieldTags')}</div>
    <div class="tags-row">${tagsHtml}</div>

    <div class="section-title">${t('fieldIngredients')} <span class="hint-inline">(${t('fieldStepAssignment')})</span></div>
    ${matchSummary}
    <div id="ingredients-wrap">${ingredientsHtml}</div>

    <div class="section-title">${t('fieldSteps')}</div>
    <div id="steps-wrap">${stepsHtml}</div>
  `;

  detail.querySelectorAll('.image-enhance-btn').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    enhanceImage(r, b);
  }));

  // Image selection / AI generation tile
  detail.querySelectorAll('.image-choice').forEach((elm) => {
    if (elm.dataset.generate) {
      elm.addEventListener('click', () => triggerImageGeneration(r, elm));
      return;
    }
    elm.addEventListener('click', () => {
      const imageId = elm.dataset.imageId || null;
      r.selected_image_id = imageId;
      patchRecipe(r.id, { selected_image_id: imageId });
      renderDetail(r);
      renderRecipeList();
    });
  });

  // Collect field edits and save them on blur/change
  const save = () => {
    r.title = detail.querySelector('#f-title').value;
    r.description = detail.querySelector('#f-description').value || null;
    r.servings = numOrNull(detail.querySelector('#f-servings').value);
    r.prep_time_minutes = numOrNull(detail.querySelector('#f-prep').value);
    r.cook_time_minutes = numOrNull(detail.querySelector('#f-cook').value);

    const previous = r.ingredients;
    r.ingredients = Array.from(detail.querySelectorAll('.ingredient-row')).map((row) => {
      const before = previous[Number(row.dataset.idx)] || {};
      const name = row.querySelector('.ing-name').value;
      // Keep the Tandoor match info only while the name is untouched - a
      // hand-edited name hasn't been checked against Tandoor.
      const unchanged = name === before.name;
      const unit = row.querySelector('.ing-unit').value || null;
      const unitUnchanged = unit === (before.unit ?? null);
      return {
        amount: numOrNull(row.querySelector('.ing-amount').value),
        unit,
        unit_match: unitUnchanged ? (before.unit_match ?? null) : null,
        original_unit: unitUnchanged ? (before.original_unit ?? null) : null,
        name,
        note: row.querySelector('.ing-note').value || null,
        group: null,
        step_index: numOrNull(row.querySelector('.ing-step')?.value) ?? 0,
        tandoor_match: unchanged ? (before.tandoor_match ?? null) : null,
        original_name: unchanged ? (before.original_name ?? null) : null,
      };
    });

    r.steps = Array.from(detail.querySelectorAll('.step-row')).map((row) => ({
      instruction: row.querySelector('.step-instruction').value,
      title: null,
      time_minutes: null,
    }));

    patchRecipe(r.id, {
      title: r.title,
      description: r.description,
      servings: r.servings,
      prep_time_minutes: r.prep_time_minutes,
      cook_time_minutes: r.cook_time_minutes,
      ingredients: r.ingredients,
      steps: r.steps,
    });
    renderRecipeList();
  };

  detail.querySelectorAll('input, textarea').forEach((inp) => {
    inp.addEventListener('blur', save);
  });
  detail.querySelectorAll('select').forEach((sel) => {
    sel.addEventListener('change', save);
  });

  // Undo a unit match: back to the unit as extracted (a new unit).
  detail.querySelectorAll('.unit-match.matched').forEach((badge) => {
    badge.addEventListener('click', () => {
      const ing = r.ingredients[Number(badge.closest('.ingredient-row').dataset.idx)];
      if (!ing || !ing.original_unit) return;
      ing.unit = ing.original_unit;
      ing.original_unit = null;
      ing.unit_match = 'new';
      patchRecipe(r.id, { ingredients: r.ingredients });
      renderDetail(r);
    });
  });

  // Tags: remove, undo a match, add (existing ones offered while typing).
  const saveTags = () => {
    patchRecipe(r.id, { tags: r.tags, tag_status: r.tag_status || {}, tag_original: r.tag_original || {} });
    renderDetail(r);
  };
  detail.querySelectorAll('.tag-remove').forEach((b) => b.addEventListener('click', () => {
    const [tag] = r.tags.splice(Number(b.dataset.idx), 1);
    delete (r.tag_status || {})[tag];
    delete (r.tag_original || {})[tag];
    saveTags();
  }));
  detail.querySelectorAll('.tag-revert').forEach((b) => b.addEventListener('click', () => {
    const i = Number(b.dataset.idx);
    const tag = r.tags[i];
    const original = (r.tag_original || {})[tag];
    if (!original) return;
    r.tags[i] = original;
    delete r.tag_original[tag];
    delete r.tag_status[tag];
    r.tag_status[original] = 'new';
    saveTags();
  }));
  const tagInput = detail.querySelector('.tag-add');
  loadExistingTags();
  const addTag = async () => {
    const value = tagInput.value.trim();
    if (!value) return;
    const names = await loadExistingTags();
    const existing = names.find((n) => n.toLowerCase() === value.toLowerCase());
    const tag = existing || value;
    if (!r.tags.some((x) => x.toLowerCase() === tag.toLowerCase())) {
      r.tags.push(tag);
      r.tag_status = { ...(r.tag_status || {}), [tag]: existing ? 'exists' : 'new' };
    }
    saveTags();
    detail.querySelector('.tag-add')?.focus();
  };
  tagInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } });
  tagInput.addEventListener('change', addTag);

  // Undo a match: back to the extracted name, which becomes a new ingredient.
  detail.querySelectorAll('.ing-match.matched:not(.unit-match)').forEach((badge) => {
    badge.addEventListener('click', () => {
      const ing = r.ingredients[Number(badge.closest('.ingredient-row').dataset.idx)];
      if (!ing || !ing.original_name) return;
      ing.name = ing.original_name;
      ing.original_name = null;
      ing.tandoor_match = 'new';
      patchRecipe(r.id, { ingredients: r.ingredients });
      renderDetail(r);
    });
  });

  // Drag & drop to reorder ingredients
  const ingredientsWrap = el('ingredients-wrap');
  let dragSrcRow = null;

  detail.querySelectorAll('.ingredient-row').forEach((row) => {
    row.addEventListener('dragstart', (e) => {
      dragSrcRow = row;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', row.dataset.idx || ''); } catch {}
    });

    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      ingredientsWrap.querySelectorAll('.ingredient-row').forEach((el2) => {
        el2.classList.remove('drag-over-top', 'drag-over-bottom');
      });
      dragSrcRow = null;
    });

    row.addEventListener('dragover', (e) => {
      if (!dragSrcRow || dragSrcRow === row) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const rect = row.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      row.classList.toggle('drag-over-top', before);
      row.classList.toggle('drag-over-bottom', !before);
    });

    row.addEventListener('dragleave', () => {
      row.classList.remove('drag-over-top', 'drag-over-bottom');
    });

    row.addEventListener('drop', (e) => {
      if (!dragSrcRow || dragSrcRow === row) return;
      e.preventDefault();
      const rect = row.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      ingredientsWrap.insertBefore(dragSrcRow, before ? row : row.nextSibling);
      row.classList.remove('drag-over-top', 'drag-over-bottom');
      save();
    });
  });

  const undoBtn = detail.querySelector('.undo-import-btn');
  if (undoBtn) {
    undoBtn.addEventListener('click', async () => {
      undoBtn.disabled = true;
      undoBtn.textContent = t('undoingImport');
      try {
        const res = await fetch(`/api/jobs/${state.jobId}/recipes/${r.id}/undo-import`, { method: 'POST' });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || t('undoImportFailed'));
        }
        const updated = await res.json();
        Object.assign(r, updated);
        renderRecipeList();
        renderDetail(r);
      } catch (e) {
        alert(`${t('undoImportFailed')}: ${e.message}`);
        undoBtn.disabled = false;
        undoBtn.textContent = t('undoImportBtn');
      }
    });
  }
}

async function triggerImageGeneration(recipe, tileEl) {
  tileEl.classList.add('loading');
  tileEl.innerHTML = '<span class="generate-tile-icon spin">✨</span>';
  const allTiles = tileEl.parentElement.querySelectorAll('.image-choice');
  allTiles.forEach((t) => { if (t !== tileEl) t.style.pointerEvents = 'none'; });

  try {
    const res = await fetch(`/api/jobs/${state.jobId}/recipes/${recipe.id}/generate-image`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || t('imageGenFailed'));
    }
    const data = await res.json();
    Object.assign(recipe, data.recipe);
    state.job.images[data.image_id] = { page: recipe.source_page_start, filename: `${data.image_id}.png` };
    renderRecipeList();
    renderDetail(recipe);
  } catch (e) {
    alert(`${t('imageGenFailed')}: ${e.message}`);
    tileEl.classList.remove('loading');
    tileEl.innerHTML = '<span class="generate-tile-icon">✨</span>';
    allTiles.forEach((t) => { t.style.pointerEvents = ''; });
  }
}

async function enhanceImage(recipe, btn) {
  btn.classList.add('loading');
  btn.disabled = true;
  btn.closest('.image-choice').parentElement.querySelectorAll('.image-choice').forEach((x) => { x.style.pointerEvents = 'none'; });
  try {
    const res = await fetch(`/api/jobs/${state.jobId}/recipes/${recipe.id}/enhance-image`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_id: btn.dataset.enhance }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    Object.assign(recipe, data.recipe);
    state.job.images[data.image_id] = data.image;
  } catch (e) {
    alert(`${t('imageEnhanceFailed')}: ${e.message}`);
  }
  renderRecipeList();
  if (state.activeRecipeId === recipe.id) renderDetail(recipe);
}

function numOrNull(v) {
  if (v === '' || v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
}

let patchTimer = null;
function patchRecipe(id, partial) {
  clearTimeout(patchTimer);
  const jobId = state.jobId;  // still the right job if the review is left meanwhile
  patchTimer = setTimeout(() => {
    fetch(`/api/jobs/${jobId}/recipes/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(partial),
    }).catch(() => {});
  }, 150);
}

// ---------- Import ----------

async function runImport(recipeIds) {
  if (recipeIds.length === 0) return;

  const importBtn = el('import-btn');
  const retryBtn = el('retry-failed-btn');
  importBtn.disabled = true;
  retryBtn.disabled = true;
  const originalImportLabel = importBtn.textContent;
  importBtn.textContent = t('importBtnLoading');

  const idSet = new Set(recipeIds);
  state.job.recipes.forEach((r) => {
    if (idSet.has(r.id)) r.import_status = 'importing';
  });
  renderRecipeList();

  const cookbookName = el('cookbook-name-input').value.trim();
  const jobId = state.jobId;

  try {
    const res = await fetch(`/api/jobs/${jobId}/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ recipe_ids: recipeIds, cookbook_name: cookbookName,
        enhance_photos: APP_CONFIG.image_gen_available && el('enhance-photos').checked }),
    });
    const data = await res.json();
    if (state.jobId !== jobId) return;  // left the review meanwhile - the import still ran
    data.results.forEach((result) => {
      const r = findRecipe(result.id);
      if (!r) return;
      r.import_status = result.status;
      r.import_error = result.error;
      r.tandoor_recipe_id = result.tandoor_recipe_id;
    });
    showImportSummary(data.results, data.cookbook_name, data.cookbook_warning, data.post_processing_job_id);
    cookbookState.names = null;  // a new cookbook may exist now
    loadCookbooks();
  } catch (e) {
    alert(`${t('importFailedAlertPrefix')} ${e.message}`);
  } finally {
    importBtn.disabled = false;
    retryBtn.disabled = false;
    importBtn.textContent = originalImportLabel;
    if (state.jobId === jobId) {
      renderRecipeList();
      if (state.activeRecipeId) renderDetail(findRecipe(state.activeRecipeId));
    }
  }
}

// "Improve photos with AI" at import - only with an image AI; remembered per browser.
function initEnhancePhotos() {
  el('enhance-photos-wrap').classList.toggle('hidden', !APP_CONFIG.image_gen_available);
  try { el('enhance-photos').checked = localStorage.getItem('th.enhancePhotos') === '1'; } catch (e) { /* optional */ }
}
el('enhance-photos').addEventListener('change', () => {
  try { localStorage.setItem('th.enhancePhotos', el('enhance-photos').checked ? '1' : '0'); } catch (e) { /* optional */ }
});

el('import-btn').addEventListener('click', () => {
  const selectedIds = state.job.recipes.filter((r) => r.selected).map((r) => r.id);
  runImport(selectedIds);
});

el('retry-failed-btn').addEventListener('click', () => {
  const failedIds = state.job.recipes.filter((r) => r.import_status === 'error').map((r) => r.id);
  runImport(failedIds);
});

function showImportSummary(results, cookbookName, cookbookWarning, postProcessingJobId) {
  const total = results.length;
  const ok = results.filter((r) => r.status === 'imported').length;
  const failed = total - ok;

  const icon = el('modal-icon');
  const title = el('modal-title');
  const body = el('modal-body');

  if (failed === 0) {
    icon.textContent = '🎉';
    title.textContent = t('modalTitleSuccess');
  } else if (ok === 0) {
    icon.textContent = '⚠️';
    title.textContent = t('modalTitleFailed');
  } else {
    icon.textContent = '⚠️';
    title.textContent = t('modalTitlePartial');
  }

  let lines = [];
  lines.push(tf('modalSummaryLine', { ok, total }));
  if (cookbookName) {
    lines.push(tf('modalCookbookLine', { name: cookbookName }));
  }
  if (cookbookWarning) {
    lines.push(tf('modalCookbookWarningLine', { warning: cookbookWarning }));
  }
  if (failed > 0) {
    lines.push(tf('modalFailedLine', { failed }));
  }
  if (ok > 0) {
    lines.push(t('modalOpenInListHint'));
  }
  if (postProcessingJobId) {
    lines.push(t('modalPostProcessingLine'));
  }
  body.textContent = lines.join('\n');

  // A recipe from a website: offer to look for more on that site.
  const site = ok > 0 ? importedSite() : null;
  const scanBtn = el('modal-scan-btn');
  scanBtn.classList.toggle('hidden', !site);
  if (site) {
    scanBtn.textContent = tf('modalScanSite', { host: hostOf(site) });
    scanBtn.dataset.url = site;
  }

  el('success-modal').classList.remove('hidden');
}

// The homepage of the website all recipes of this import came from (a
// single link, or links from one site) - null for files or several sites.
function importedSite() {
  const recipes = (state.job && state.job.recipes) || [];
  const urls = recipes.map((r) => r.source_url);
  if (!urls.length || urls.some((u) => !u)) return null;
  const origins = new Set(urls.map((u) => { try { return new URL(u).origin; } catch (e) { return null; } }));
  if (origins.size !== 1 || origins.has(null)) return null;
  return `${[...origins][0]}/`;
}

el('modal-scan-btn').addEventListener('click', () => {
  const url = el('modal-scan-btn').dataset.url;
  el('success-modal').classList.add('hidden');
  resetToUpload();
  let depth = 2;
  try { depth = Number(localStorage.getItem('th.scanDepth')) || 2; } catch (e) { /* default */ }
  startSiteScan(url, depth);
});

el('modal-close-btn').addEventListener('click', () => {
  el('success-modal').classList.add('hidden');
});

el('modal-new-upload-btn').addEventListener('click', () => {
  el('success-modal').classList.add('hidden');
  resetToUpload();
});

function resetToUpload() {
  clearTimeout(state.pollTimer);
  state.jobId = null;
  state.job = null;
  state.activeRecipeId = null;
  setJobUrl(null);

  fileInput.value = '';
  el('upload-error').classList.add('hidden');
  el('review-screen').classList.add('hidden');
  el('action-bar').classList.add('hidden');
  el('processing-screen').classList.add('hidden');
  el('usage-badge').classList.add('hidden');
  el('upload-screen').classList.remove('hidden');
  showArea('import');
}

el('brand-link').addEventListener('click', goHome);
el('brand-link').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    goHome();
  }
});

function goHome() {
  const alreadyHome = currentArea === 'import' && !el('upload-screen').classList.contains('hidden');
  if (alreadyHome) return;
  if (currentArea !== 'import') {
    showArea('import');  // back to whatever the import area showed (upload or review)
    return;
  }
  // Edits are saved as you type, and the import stays under "recent imports".
  el('success-modal').classList.add('hidden');
  resetToUpload();
}

// ---------- Areas (Import / Review / Plan / Maintain) ----------

const AREAS = { import: 'area-import', inbox: 'area-inbox', plan: 'area-plan', maintain: 'tools-screen' };
let currentArea = 'import';

function showArea(area) {
  currentArea = area;
  Object.entries(AREAS).forEach(([name, id]) => el(id).classList.toggle('hidden', name !== area));
  document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.area === area));
  if (area === 'import') loadRecentImports();
  if (area === 'inbox') loadInbox();
  if (area === 'plan') { openPlanArea(); loadCooked(); loadSeason(); }
  if (area === 'maintain') {
    loadNewRecipesStatus();
    loadUsage();
    loadHealth();
    loadSettings();
  }
  window.scrollTo(0, 0);
}

// A menu item always opens the start page of its area - also when you are
// already there (e.g. inside a tool run or the review of an import).
function openAreaStart(area) {
  if (area === 'import') {
    el('success-modal').classList.add('hidden');
    if (state.jobId || el('upload-screen').classList.contains('hidden')) { resetToUpload(); return; }
  }
  if (area === 'maintain') {
    closeToolRun();
    closeHealthDetail();
  }
  showArea(area);
}

document.querySelectorAll('.nav-btn').forEach((b) => b.addEventListener('click', () => openAreaStart(b.dataset.area)));

const TOOL_TITLE_KEYS = {
  tags_groups: 'toolTagGroupsTitle',
  unused_foods: 'toolUnusedFoodsTitle',
  unused_units: 'toolUnusedUnitsTitle',
  unused_keywords: 'toolUnusedKeywordsTitle',
  new_recipes: 'toolNewRecipesTitle',
  meal_plan: 'toolMealPlanTitle',
  ingredients_review: 'toolIngredientsReviewTitle',
  ingredients_enrich: 'toolIngredientsEnrichTitle',
  conversions: 'toolConversionsTitle',
  units_review: 'toolUnitsTitle',
  tags_cleanup: 'toolTagsCleanupTitle',
  tags_simplify: 'toolTagsCleanupTitle',
  tags_translate: 'toolTagsCleanupTitle',
  tags_season: 'toolTagsSeasonTitle',
  tags_suggest_more: 'toolTagsSuggestMoreTitle',
  recipes_translate: 'toolRecipesTranslateTitle',
  recipes_restructure: 'toolRecipesRestructureTitle',
  recipes_servings: 'toolRecipesServingsTitle',
  recipes_images: 'toolRecipesImagesTitle',
};

function toolTitle(tool) {
  return TOOL_TITLE_KEYS[tool] ? t(TOOL_TITLE_KEYS[tool]) : tool;
}

function shortWhen(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
}

// ---------- Recent imports (import area) ----------

async function loadRecentImports() {
  const box = el('recent-imports');
  try {
    const items = (await (await fetch('/api/jobs')).json()).filter((j) => j.id !== state.jobId);
    if (!items.length) { box.classList.add('hidden'); return; }
    el('recent-imports-list').innerHTML = items.map((j) => {
      const status = j.status === 'processing' ? t('recentImportRunning')
        : j.status === 'error' ? t('recentImportError')
        : tf('recentImportLine', { recipes: j.recipes, imported: j.imported });
      return `<div class="new-recipes-open-job"><span><strong>${escapeHtml(j.filename)}</strong> · ${escapeHtml(status)} · ${escapeHtml(shortWhen(j.created_at))}</span>
        <button class="btn secondary" type="button" data-job-id="${j.id}">${t('toolNewRecipesOpenJob')}</button></div>`;
    }).join('');
    el('recent-imports-list').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => openImportJob(b.dataset.jobId)));
    box.classList.remove('hidden');
  } catch (e) {
    box.classList.add('hidden');
  }
}

async function openImportJob(jobId) {
  clearTimeout(state.pollTimer);
  setJobUrl(jobId);
  el('upload-screen').classList.add('hidden');
  await tryRestoreJobFromUrl();
  if (state.jobId !== jobId) el('upload-screen').classList.remove('hidden');  // job was gone
}

// ---------- Review inbox ----------

// Order matters: merges first, so ingredient details and conversions are
// applied to the entries that remain.
const INBOX_GROUPS = [
  { key: 'failed', icon: '⚠️', match: (i) => i.failed },
  { key: 'merges', icon: '🔀', match: (i) => i.entity || i.kind === 'merge' || i.kind === 'rename' },
  { key: 'details', icon: '🥗', match: (i) => i.kind === 'enrich' || i.kind === 'set_plural' },
  { key: 'conversions', icon: '⚖️', match: (i) => i.kind === 'conversion' },
  { key: 'recipes', icon: '🧩', match: (i) => i.kind === 'restructure_recipe' || i.kind === 'translate_recipe' },
  { key: 'tags', icon: '🏷️', match: (i) => i.kind === 'season' || i.kind === 'suggest_tags' },
  { key: 'other', icon: '•', match: () => true },
];
const inboxState = { items: [], selected: new Set(), collapsed: new Set(), busyGroup: null, results: {} };

function itemKey(i) { return `${i.job_id}:${i.id}`; }

async function updateInboxBadge() {
  try {
    const data = await (await fetch('/api/inbox/count')).json();
    const badge = el('inbox-badge');
    badge.textContent = data.count > 99 ? '99+' : data.count;
    badge.classList.toggle('hidden', data.count === 0);
  } catch (e) { /* badge is best-effort */ }
}

async function loadInbox(schedule = true) {
  try {
    const data = await (await fetch('/api/inbox')).json();
    inboxState.items = data.items;
    inboxState.imports = data.imports || [];
    renderInboxImports();
    const keys = new Set(data.items.map(itemKey));
    inboxState.selected.forEach((k) => { if (!keys.has(k)) inboxState.selected.delete(k); });
    el('inbox-running').innerHTML = data.running.map((r) => `
      <div class="inbox-running-row"><span class="spinner small"></span>
        ${escapeHtml(tf('inboxRunning', { tool: r.trigger === 'import' ? t('triggerImport') : toolTitle(r.tool) }))}
        ${r.label ? `<span class="inbox-source">${escapeHtml(r.label)}</span>` : ''}</div>`).join('');
    el('inbox-queue').classList.toggle('hidden', !data.queued);
    el('inbox-queue').innerHTML = data.queued
      ? `<span class="spinner small"></span> ${escapeHtml(tf('queueRunning', { n: data.queued }))}` : '';
    renderInbox();
    updateInboxBadge();
    if (!historyState.busy) loadHistory();
    if (!schedule) return;
    clearTimeout(inboxState.timer);
    if ((data.running.length || data.queued || (data.imports || []).some((i) => i.status === 'processing')) && currentArea === 'inbox') inboxState.timer = setTimeout(loadInbox, data.queued ? 2000 : 4000);
  } catch (e) {
    el('inbox-groups').innerHTML = `<div class="error-banner">${escapeHtml(e.message)}</div>`;
  }
}

// Imports that came in from the phone's share menu or the watched folder.
function renderInboxImports() {
  const imports = inboxState.imports || [];
  el('inbox-imports').innerHTML = !imports.length ? '' : `
    <section class="inbox-group inbox-imports">
      <div class="inbox-group-head"><h2>📥 ${t('inboxImportsTitle')} <span class="inbox-count">${imports.length}</span></h2></div>
      <div class="tools-suggestions-list">${imports.map((i) => `
        <div class="tool-suggestion-row ${i.status === 'error' ? 'error' : ''}">
          <div class="suggestion-text">
            <div>${{ folder: '📂', migration: '🚚' }[i.source] || '📱'} <strong>${escapeHtml(i.filename)}</strong></div>
            <div class="inbox-source">${escapeHtml(t({ folder: 'inboxImportFolder', migration: 'inboxImportMigration' }[i.source] || 'inboxImportShare'))} · ${escapeHtml(new Date(i.created_at * 1000).toLocaleString())}</div>
            <div class="suggestion-preview">${i.status === 'processing'
              ? `<span class="spinner small"></span> ${t('inboxImportProcessing')}`
              : i.status === 'error' ? `<span class="inbox-error">${escapeHtml(i.error || '')}</span>`
              : escapeHtml(tf('inboxImportRecipes', { n: i.recipes })) + (i.titles.length ? ': ' + escapeHtml(i.titles.join(', ')) : '')}</div>
          </div>
          <div class="suggestion-actions">
            <button class="btn secondary inbox-import-dismiss" type="button" data-job="${i.job_id}">${t('inboxDismissBtn')}</button>
            <button class="btn inbox-import-open" type="button" data-job="${i.job_id}" ${i.status !== 'ready' ? 'disabled' : ''}>${t('inboxImportOpen')}</button>
          </div>
        </div>`).join('')}
      </div>
    </section>`;
  el('inbox-imports').querySelectorAll('.inbox-import-open').forEach((b) => b.addEventListener('click', () => {
    showArea('import');
    openImportJob(b.dataset.job);
  }));
  el('inbox-imports').querySelectorAll('.inbox-import-dismiss').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    await fetch(`/api/jobs/${b.dataset.job}/dismiss`, { method: 'POST' }).catch(() => {});
    loadInbox(false);
  }));
}

function renderInbox() {
  const groups = INBOX_GROUPS.map((g) => ({ ...g, items: [] }));
  inboxState.items.forEach((i) => groups.find((g) => g.match(i)).items.push(i));
  const visible = groups.filter((g) => g.items.length);
  if (!visible.length) {
    el('inbox-groups').innerHTML = (inboxState.imports || []).length ? '' : `<p class="inbox-empty">${t('inboxEmpty')}</p>`;
    return;
  }
  el('inbox-groups').innerHTML = visible.map((g) => {
    const selected = g.items.filter((i) => inboxState.selected.has(itemKey(i))).length;
    const selectable = g.items.filter((i) => !i.queued).length;
    const selectedRetryable = g.items.filter((i) => inboxState.selected.has(itemKey(i)) && i.retryable).length;
    const busy = inboxState.busyGroup === g.key;
    const collapsed = inboxState.collapsed.has(g.key);
    return `
    <section class="inbox-group" data-group="${g.key}">
      <div class="inbox-group-head">
        <button type="button" class="inbox-collapse" data-group="${g.key}">${collapsed ? '▸' : '▾'}</button>
        <h2>${g.icon} ${t('inboxGroup_' + g.key)} <span class="inbox-count">${g.items.length}</span></h2>
        <label class="tools-select-all"><input type="checkbox" class="inbox-select-all" data-group="${g.key}"
          ${selectable && selected === selectable ? 'checked' : ''} ${busy || !selectable ? 'disabled' : ''}> <span>${t('toolSelectAll')}</span></label>
        <span class="tools-bulk-status" id="inbox-status-${g.key}">${escapeHtml(inboxState.results[g.key] || '')}</span>
        <div class="tools-bulk-actions">
          <button class="btn secondary inbox-skip" type="button" data-group="${g.key}" ${!selected || busy ? 'disabled' : ''}>${t(g.key === 'failed' ? 'inboxDismissBtn' : 'toolSkipBtn')} (${selected})</button>
          <button class="btn inbox-apply" type="button" data-group="${g.key}" ${!selected || busy || (g.key === 'failed' && !selectedRetryable) ? 'disabled' : ''}>${t(g.key === 'failed' ? 'inboxRetryBtn' : 'toolApplyBtn')} (${g.key === 'failed' ? selectedRetryable : selected})</button>
        </div>
      </div>
      ${g.key === 'failed' ? `<p class="inbox-hint">${t('inboxFailedHint')}</p>` : ''}
      ${g.key === 'merges' ? `<p class="inbox-hint">${t('inboxMergesHint')}</p>` : ''}
      <div class="tools-suggestions-list ${collapsed ? 'hidden' : ''}">
        ${g.items.map((i) => `
          <div class="tool-suggestion-row pending ${i.queued ? 'queued' : ''}" data-key="${itemKey(i)}">
            <input type="checkbox" class="suggestion-check" ${inboxState.selected.has(itemKey(i)) ? 'checked' : ''} ${busy || i.queued ? 'disabled' : ''}>
            <div class="suggestion-text">
              ${i.queued ? `<span class="queued-label">⏳ ${t('queuedLabel')}</span> ` : ''}${escapeHtml(i.summary)}
              ${i.failed ? `<div class="inbox-error">${escapeHtml(i.error || t('toolStatusError'))}</div>` : ''}
              <div class="inbox-source">${escapeHtml(i.trigger === 'import' ? t('triggerImport') : toolTitle(i.tool))} · ${escapeHtml(shortWhen(i.job_created_at))}</div>
              ${i.preview ? `<details class="suggestion-preview"><summary>${t('toolShowPreview')}</summary><pre>${escapeHtml(i.preview)}</pre></details>` : ''}
            </div>
          </div>`).join('')}
      </div>
    </section>`;
  }).join('');

  const groupItems = (key) => groups.find((g) => g.key === key).items;
  el('inbox-groups').querySelectorAll('.inbox-collapse').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.group;
    if (inboxState.collapsed.has(k)) inboxState.collapsed.delete(k); else inboxState.collapsed.add(k);
    renderInbox();
  }));
  el('inbox-groups').querySelectorAll('.inbox-select-all').forEach((box) => box.addEventListener('change', () => {
    groupItems(box.dataset.group).forEach((i) => {
      if (box.checked && !i.queued) inboxState.selected.add(itemKey(i)); else inboxState.selected.delete(itemKey(i));
    });
    renderInbox();
  }));
  el('inbox-groups').querySelectorAll('.tool-suggestion-row').forEach((row) => {
    const box = row.querySelector('.suggestion-check');
    const toggle = () => {
      if (box.checked) inboxState.selected.add(row.dataset.key); else inboxState.selected.delete(row.dataset.key);
      renderInbox();
    };
    box.addEventListener('change', toggle);
    row.addEventListener('click', (e) => {
      if (inboxState.busyGroup || box.disabled || e.target === box || e.target.closest('details')) return;
      box.checked = !box.checked;
      toggle();
    });
  });
  el('inbox-groups').querySelectorAll('.inbox-apply, .inbox-skip').forEach((b) => b.addEventListener('click', () => {
    const apply = b.classList.contains('inbox-apply');
    const action = b.dataset.group === 'failed' ? (apply ? 'retry' : 'skip') : (apply ? 'apply' : 'skip');
    const items = groupItems(b.dataset.group).filter((i) => action !== 'retry' || i.retryable);
    runInboxAction(b.dataset.group, action, items);
  }));
}

// ---------- Recently applied (undo) ----------

const historyState = { items: [], selected: new Set(), busy: false };

async function loadHistory() {
  if (!el('history').open) return;
  try {
    const data = await (await fetch('/api/history')).json();
    historyState.items = data.items;
    el('history-hint').textContent = tf('historyHint', { days: data.retention_days });
    const keys = new Set(data.items.map(itemKey));
    historyState.selected.forEach((k) => { if (!keys.has(k)) historyState.selected.delete(k); });
    renderHistory();
  } catch (e) { /* optional */ }
}

function renderHistory() {
  const items = historyState.items;
  el('history-list').innerHTML = items.length ? items.map((i) => `
    <div class="tool-suggestion-row applied ${i.queued ? 'queued' : ''}" data-key="${itemKey(i)}">
      <input type="checkbox" class="suggestion-check" ${historyState.selected.has(itemKey(i)) ? 'checked' : ''} ${historyState.busy || i.queued ? 'disabled' : ''}>
      <div class="suggestion-text">
        ${i.queued ? `<span class="queued-label">⏳ ${t('queuedLabel')}</span> ` : ''}${escapeHtml(i.summary)}
        <div class="inbox-source">${escapeHtml(toolTitle(i.tool))} · ${escapeHtml(shortWhen(i.applied_at))}</div>
        ${i.error ? `<div class="inbox-error">${escapeHtml(i.error)}</div>` : ''}
      </div>
    </div>`).join('') : `<p class="inbox-empty">${t('historyEmpty')}</p>`;
  const n = historyState.selected.size;
  const selectable = items.filter((i) => !i.queued).length;
  el('history-undo-btn').textContent = tf('historyUndoBtn', { count: n });
  el('history-undo-btn').disabled = historyState.busy || !n;
  el('history-select-all').checked = selectable > 0 && n === selectable;
  el('history-select-all').disabled = historyState.busy || !selectable;
  el('history-list').querySelectorAll('.tool-suggestion-row').forEach((row) => {
    const box = row.querySelector('.suggestion-check');
    const toggle = () => {
      if (box.checked) historyState.selected.add(row.dataset.key); else historyState.selected.delete(row.dataset.key);
      renderHistory();
    };
    box.addEventListener('change', toggle);
    row.addEventListener('click', (e) => {
      if (box.disabled || e.target === box) return;
      box.checked = !box.checked;
      toggle();
    });
  });
}

el('history').addEventListener('toggle', loadHistory);
el('history-select-all').addEventListener('change', (e) => {
  historyState.selected = new Set(e.target.checked ? historyState.items.filter((i) => !i.queued).map(itemKey) : []);
  renderHistory();
});
el('history-undo-btn').addEventListener('click', async () => {
  // Newest first - later changes are taken back before the ones they built on.
  const todo = historyState.items.filter((i) => historyState.selected.has(itemKey(i)));
  if (!todo.length || historyState.busy) return;
  historyState.busy = true;
  el('history-status').textContent = '';
  try {
    const batchId = await queueActions('undo', todo);
    historyState.selected.clear();
    const batch = await watchBatch(batchId, async (b) => {
      el('history-status').textContent = b.done < b.total ? tf('historyUndoing', { current: Math.min(b.done + 1, b.total), total: b.total }) : '';
      await loadHistory();
    }, () => currentArea === 'inbox');
    el('history-status').textContent = batchResultText(batch);
  } catch (e) {
    el('history-status').textContent = `${t('toolStatusError')}: ${e.message}`;
  }
  historyState.busy = false;
  await loadHistory();
  loadInbox();
});

// ---------- Background apply/skip ----------
// Selected suggestions are handed to the server's queue, which applies them
// one after another in the background - closing the page doesn't stop it.

async function queueActions(action, items) {
  const res = await fetch('/api/tools/actions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, items: items.map((i) => ({ job_id: i.job_id, id: i.id })) }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()).batch_id;
}

// Polls a batch until it's done while `stillWatching()` holds; calls
// onTick(batch) after every poll. Returns the final batch (or null).
async function watchBatch(batchId, onTick, stillWatching = () => true) {
  for (;;) {
    let batch = null;
    try {
      batch = (await (await fetch(`/api/tools/actions?batch=${batchId}`)).json()).batch;
    } catch (e) { /* try again */ }
    if (batch) await onTick(batch);
    if (!batch || batch.done >= batch.total) return batch;
    if (!stillWatching()) return null;
    await new Promise((r) => setTimeout(r, 1200));
  }
}

function batchProgressText(action, batch, withHint = true) {
  return tf(action === 'skip' ? 'toolSkippingProgress' : 'toolApplyingProgress', { current: Math.min(batch.done + 1, batch.total), total: batch.total })
    + (withHint ? ' · ' + t('queueBackgroundHint') : '');
}

function batchResultText(batch) {
  return batch && batch.failed ? tf('toolBulkFailed', { count: batch.failed }) : '';
}

async function runInboxAction(groupKey, action, items) {
  const todo = items.filter((i) => inboxState.selected.has(itemKey(i)) && !i.queued);
  if (!todo.length || inboxState.busyGroup) return;
  inboxState.busyGroup = groupKey;
  inboxState.results[groupKey] = '';
  let batchId;
  try {
    batchId = await queueActions(action, todo);
  } catch (e) {
    inboxState.busyGroup = null;
    inboxState.results[groupKey] = `${t('toolStatusError')}: ${e.message}`;
    renderInbox();
    return;
  }
  todo.forEach((i) => { inboxState.selected.delete(itemKey(i)); i.queued = true; });
  renderInbox();
  const batch = await watchBatch(batchId, async (b) => {
    inboxState.results[groupKey] = b.done < b.total ? batchProgressText(action, b, false) : ''; // the banner says it goes on in the background
    await loadInbox(false);
  }, () => currentArea === 'inbox');
  inboxState.busyGroup = null;
  // Failed items move to the "failed" group with their error.
  inboxState.results[groupKey] = batchResultText(batch);
  await loadInbox();
}

// ---------- Maintain: AI usage + collection health ----------

async function loadUsage() {
  try {
    const data = await (await fetch('/api/usage?days=30')).json();
    const total = data.total.input_tokens + data.total.output_tokens;
    if (data.budget) renderBudget(data.budget);
    el('usage-total').textContent = total
      ? tf('usageLine', { input: data.total.input_tokens.toLocaleString(), output: data.total.output_tokens.toLocaleString() })
      : t('usageNone');
    el('usage-by-source').innerHTML = Object.entries(data.by_source)
      .sort((a, b) => (b[1].input_tokens + b[1].output_tokens) - (a[1].input_tokens + a[1].output_tokens))
      .map(([src, u]) => `<div class="usage-row"><span>${escapeHtml(src === 'import' ? t('usageImport') : toolTitle(src))}</span>
        <span>${(u.input_tokens + u.output_tokens).toLocaleString()}</span></div>`).join('');
  } catch (e) {
    el('usage-total').textContent = '';
  }
}

// ---------- Automation & budget settings ----------

function renderBudget(budget) {
  const pct = budget.limit ? Math.min(100, Math.round((budget.used / budget.limit) * 100)) : 0;
  el('budget-fill').style.width = `${pct}%`;
  el('budget-fill').classList.toggle('over', budget.exceeded);
  el('budget-text').textContent = budget.limit
    ? tf('budgetUsed', { used: budget.used.toLocaleString(), limit: budget.limit.toLocaleString(), pct })
    : tf('budgetUsedNoLimit', { used: budget.used.toLocaleString() });
  settingsState.budget = budget;
  renderSettingsSummary();
}

// Collapsed "automation & budget": when it runs and how much budget is used.
function renderSettingsSummary() {
  const { maint, budget } = settingsState;
  const parts = [];
  if (maint) {
    const time = `${String(maint.hour).padStart(2, '0')}:00`;
    parts.push(!maint.enabled ? t('maintSummaryOff')
      : maint.every_days === 1 ? tf('maintSummaryDaily', { time }) : tf('maintSummaryEvery', { n: maint.every_days, time }));
  }
  if (budget) {
    const pct = budget.limit ? Math.min(100, Math.round((budget.used / budget.limit) * 100)) : 0;
    parts.push(budget.exceeded ? t('budgetExceeded')
      : budget.limit ? tf('budgetSummary', { pct }) : tf('budgetSummaryNone', { used: budget.used.toLocaleString() }));
  }
  el('settings-summary').textContent = parts.join(' · ');
  el('settings-summary').classList.toggle('over', !!(budget && (budget.exceeded || budget.warn)));
}

const settingsState = { maint: null, budget: null };

function renderMaintStatus(st) {
  const parts = [];
  if (st.running) parts.push(t('maintRunning'));
  else if (st.next_run_at) parts.push(tf('maintNext', { when: new Date(st.next_run_at * 1000).toLocaleString([], { weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) }));
  if (st.last_run_at) {
    const r = st.last_result || {};
    const started = (r.started || []).map(toolTitle).join(', ') || t('maintNothing');
    parts.push(tf('maintLast', { when: shortWhen(st.last_run_at), tools: started }) + (r.budget_stop ? ` ${t('maintBudgetStop')}` : ''));
  }
  el('set-maint-status').textContent = parts.join(' · ');
  el('set-maint-run').disabled = st.running;
}

async function loadSettings() {
  try {
    const data = await (await fetch('/api/settings')).json();
    const m = data.maintenance;
    if (!el('set-maint-hour').options.length) {
      el('set-maint-hour').innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${h}">${String(h).padStart(2, '0')}:00</option>`).join('');
    }
    el('set-maint-enabled').checked = m.enabled;
    el('set-maint-hour').value = String(m.hour);
    el('set-maint-days').value = String(m.every_days);
    // grouped like the tiles
    el('set-maint-metrics').innerHTML = HEALTH_GROUPS.map((g) => {
      const keys = g.metrics.filter((k) => data.metrics.includes(k));
      return keys.length ? `<h5>${t(g.titleKey)}</h5>` + keys.map((k) => `
        <label><input type="checkbox" value="${k}" ${m.metrics.includes(k) ? 'checked' : ''}> ${escapeHtml(t('healthShort_' + k))}</label>`).join('') : '';
    }).join('');
    settingsState.maint = m;
    if (healthState.data) renderHealth(healthState.data);
    el('set-budget-limit').value = data.budget.monthly_tokens;
    el('set-budget-block').checked = data.budget.block_manual;
    renderBudget(data.budget_status);
    renderMaintStatus(data.maintenance_status);
    renderNotifyStatus();
    clearTimeout(loadSettings.timer);
    if (data.maintenance_status.running && currentArea === 'maintain') loadSettings.timer = setTimeout(loadSettings, 5000);
  } catch (e) { /* optional panel */ }
}

el('settings-save').addEventListener('click', async () => {
  const body = {
    maintenance: {
      enabled: el('set-maint-enabled').checked,
      hour: Number(el('set-maint-hour').value),
      every_days: Number(el('set-maint-days').value),
      metrics: [...el('set-maint-metrics').querySelectorAll('input:checked')].map((c) => c.value),
    },
    budget: { monthly_tokens: Number(el('set-budget-limit').value) || 0, block_manual: el('set-budget-block').checked },
  };
  const res = await fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  el('settings-saved').textContent = res.ok ? t('settingsSaved') : `${t('toolStatusError')}: HTTP ${res.status}`;
  setTimeout(() => { el('settings-saved').textContent = ''; }, 3000);
  loadSettings();
});

function renderNotifyStatus() {
  const channels = APP_CONFIG.notify_channels || [];
  el('notify-status').textContent = channels.length ? tf('notifyOn', { channels: channels.join(', ') }) : t('notifyOff');
  el('notify-test').classList.toggle('hidden', !channels.length);
}

el('notify-test').addEventListener('click', async () => {
  el('notify-test').disabled = true;
  el('notify-result').textContent = '';
  try {
    const data = await (await fetch('/api/notify/test', { method: 'POST' })).json();
    el('notify-result').textContent = data.errors.length ? data.errors.join(' · ') : tf('notifySent', { channels: data.sent.join(', ') });
  } catch (e) {
    el('notify-result').textContent = e.message;
  } finally {
    el('notify-test').disabled = false;
  }
});

el('set-maint-run').addEventListener('click', async () => {
  el('set-maint-run').disabled = true;
  await fetch('/api/maintenance/run', { method: 'POST' });
  loadSettings();
  loadHealth();
});

// metric -> the tool that fixes it (endpoint + optional request body)
const HEALTH_METRICS = [
  { key: 'foods_duplicates', tool: 'ingredients_review', endpoint: '/api/tools/ingredients/review', body: { focus: 'duplicates' } },
  { key: 'foods_without_nutrition', tool: 'ingredients_enrich', endpoint: '/api/tools/ingredients/enrich', body: { focus: 'tiles' } },
  { key: 'foods_without_category', tool: 'ingredients_enrich', endpoint: '/api/tools/ingredients/enrich', body: { focus: 'tiles' } },
  { key: 'missing_conversions', tool: 'conversions', endpoint: '/api/tools/conversions' },
  { key: 'units_duplicates', tool: 'units_review', endpoint: '/api/tools/units/review', body: { focus: 'duplicates' } },
  { key: 'recipes_not_translated', tool: 'recipes_translate', endpoint: '/api/tools/recipes/translate' },
  { key: 'recipes_need_restructure', tool: 'recipes_restructure', endpoint: '/api/tools/recipes/restructure' },
  { key: 'recipes_without_season', tool: 'tags_season', endpoint: '/api/tools/tags/season' },
  { key: 'recipes_few_tags', tool: 'tags_suggest_more', endpoint: '/api/tools/tags/suggest-more' },
  { key: 'recipes_without_servings', tool: 'recipes_servings', endpoint: '/api/tools/recipes/servings' },
  { key: 'recipes_without_image', tool: 'recipes_images', endpoint: '/api/tools/recipes/images', needsImageGen: true },
  { key: 'foods_unused', tool: 'unused_foods', endpoint: '/api/tools/unused/food' },
  { key: 'units_unused', tool: 'unused_units', endpoint: '/api/tools/unused/unit' },
  { key: 'keywords_unused', tool: 'unused_keywords', endpoint: '/api/tools/unused/keyword' },
  { key: 'keywords_ungrouped', tool: 'tags_groups', endpoint: '/api/tools/tags/groups' },
];

// Tiles grouped by what they're about, each group with its "whole
// collection" tool (for what the counts can't see: typos, same meaning ...).
const HEALTH_GROUPS = [
  { key: 'foods', titleKey: 'healthGroupFoods',
    metrics: ['foods_duplicates', 'foods_without_nutrition', 'foods_without_category', 'missing_conversions', 'foods_unused'],
    tool: { tool: 'ingredients_review', endpoint: '/api/tools/ingredients/review', titleKey: 'groupToolFoods', descKey: 'toolIngredientsReviewDesc' } },
  { key: 'units', titleKey: 'healthGroupUnits', metrics: ['units_duplicates', 'units_unused'],
    tool: { tool: 'units_review', endpoint: '/api/tools/units/review', titleKey: 'groupToolUnits', descKey: 'toolUnitsDesc' } },
  { key: 'recipes', titleKey: 'healthGroupRecipes',
    metrics: ['recipes_not_translated', 'recipes_need_restructure', 'recipes_without_season', 'recipes_few_tags',
      'recipes_without_servings', 'recipes_without_image'] },
  { key: 'tags', titleKey: 'healthGroupTags', metrics: ['keywords_ungrouped', 'keywords_unused'],
    tool: { tool: 'tags_cleanup', endpoint: '/api/tools/tags/cleanup', titleKey: 'groupToolRecipes', descKey: 'toolTagsCleanupDesc' } },
];

const healthState = { open: null, data: null, showFine: new Set() };

function renderHealth(data) {
  healthState.data = data;
  const running = data.running;
  el('health-refresh-btn').disabled = running;
  el('health-when').textContent = running ? t('healthRunning')
    : data.error ? `${t('toolStatusError')}: ${data.error}`
    : data.computed_at ? tf('healthComputedAt', { when: shortWhen(data.computed_at) }) : t('healthNever');
  if (!data.computed_at) { el('health-grid').innerHTML = ''; closeHealthDetail(); return; }
  const auto = settingsState.maint && settingsState.maint.enabled ? settingsState.maint.metrics : [];
  const tile = (m) => {
    const value = data.metrics[m.key] ?? 0;
    const ignoredCount = (data.ignored || {})[m.key] || 0;
    // A run of this tool that is still going or waiting for review replaces
    // "Fix" - so a tile doesn't invite starting the same fix twice.
    const pending = (data.pending || {})[m.tool];
    const pendingHtml = pending
      ? `<div class="health-pending">${pending.scanning ? t('healthPendingScanning') : tf('healthPendingCount', { n: pending.count })}</div>`
      : '';
    const fixBtn = pending
      ? `<button class="btn secondary health-open-run" type="button" data-job="${pending.job_id}" data-tool="${m.tool}">${pending.scanning ? t('healthOpenRun') : t('healthReviewRun')}</button>`
      : value && m.needsImageGen && !APP_CONFIG.image_gen_available
        ? `<button class="btn secondary health-fix" type="button" disabled title="${escapeHtml(t('healthImageGenOff'))}">${t('healthFix')}</button>`
      : value ? `<button class="btn secondary health-fix" type="button" data-metric="${m.key}">${t('healthFix')}</button>` : '';
    return `<div class="health-tile ${value ? 'todo' : 'ok'} ${healthState.open === m.key ? 'open' : ''} ${running ? 'refreshing' : ''}">
      ${auto.includes(m.key) ? `<span class="health-auto" title="${escapeHtml(t('healthAuto'))}">🔁</span>` : ''}
      <div class="health-value">${value ? value.toLocaleString() : '✓'}</div>
      <div class="health-label" title="${escapeHtml(t('health_' + m.key))}">${t('healthShort_' + m.key)}</div>
      ${ignoredCount ? `<div class="health-ignored-count">${tf('healthIgnoredCount', { n: ignoredCount })}</div>` : ''}
      ${pendingHtml}
      ${value && m.needsImageGen && !APP_CONFIG.image_gen_available ? `<div class="health-ignored-count">${t('healthImageGenOff')}</div>` : ''}
      <div class="health-tile-actions">
        ${fixBtn}
        ${value || ignoredCount ? `<button class="btn secondary health-entries" type="button" data-metric="${m.key}">${t('healthEntries')}</button>` : ''}
      </div>
    </div>`;
  };
  const available = APP_CONFIG.health_metrics;  // null = all (Tandoor)
  el('health-grid').innerHTML = HEALTH_GROUPS.map((g) => {
    const metrics = g.metrics.filter((k) => !available || available.includes(k)).map((k) => HEALTH_METRICS.find((m) => m.key === k));
    if (!metrics.length) return '';
    // Nothing to do (and no run going, not opened): just a name in the "all fine" line.
    const done = (m) => !(data.metrics[m.key] ?? 0) && !(data.pending || {})[m.tool] && healthState.open !== m.key;
    const open = metrics.filter((m) => !done(m));
    const fine = metrics.filter(done);
    const showFine = healthState.showFine.has(g.key);
    const status = !fine.length ? ''
      : !open.length ? t('healthGroupAllFine') : tf('healthGroupMoreFine', { n: fine.length });
    // Heading, status and the group's tool in one line; finished tiles only
    // on request (they still lead to ignored entries).
    return `<div class="health-group">
      <div class="health-group-head">
        <h4>${t(g.titleKey)}</h4>
        ${status ? `<button type="button" class="health-ok-toggle" data-group="${g.key}"
          title="${escapeHtml(fine.map((m) => t('healthShort_' + m.key)).join(' · '))}">✓ ${escapeHtml(status)} ${showFine ? '▾' : '▸'}</button>` : ''}
        ${g.tool && toolAvailable(g.tool.tool) ? `<button type="button" class="link-btn health-group-tool" data-group="${g.key}" title="${escapeHtml(t(g.tool.descKey))}">${t(g.tool.titleKey)} →</button>` : ''}
      </div>
      ${open.length || showFine ? `<div class="health-grid">${open.map(tile).join('')}${showFine ? fine.map(tile).join('') : ''}</div>` : ''}
    </div>`;
  }).join('');
  el('health-grid').querySelectorAll('.health-ok-toggle').forEach((b) => b.addEventListener('click', () => {
    const key = b.dataset.group;
    if (healthState.showFine.has(key)) healthState.showFine.delete(key); else healthState.showFine.add(key);
    renderHealth(healthState.data);
  }));
  el('health-grid').querySelectorAll('.health-group-tool').forEach((b) => b.addEventListener('click', () => {
    const g = HEALTH_GROUPS.find((x) => x.key === b.dataset.group);
    startTool(g.tool.endpoint, toolTitle(g.tool.tool));
  }));
  el('health-grid').querySelectorAll('.health-fix').forEach((b) => b.addEventListener('click', () => {
    const m = HEALTH_METRICS.find((x) => x.key === b.dataset.metric);
    startTool(m.endpoint, toolTitle(m.tool), m.body);
  }));
  el('health-grid').querySelectorAll('.health-open-run').forEach((b) => b.addEventListener('click', () => {
    openToolJob(b.dataset.job, toolTitle(b.dataset.tool));
  }));
  el('health-grid').querySelectorAll('.health-entries').forEach((b) => b.addEventListener('click', () => {
    if (healthState.open === b.dataset.metric) closeHealthDetail();
    else openHealthDetail(b.dataset.metric);
  }));
}

function closeHealthDetail() {
  healthState.open = null;
  el('health-detail').classList.add('hidden');
  el('health-detail').innerHTML = '';
  // re-render: a finished tile that was only open for its ignored entries
  // goes back into the "all fine" line
  if (healthState.data && healthState.data.computed_at) renderHealth(healthState.data);
}

function healthRow(item) {
  const link = importedRecipeUrl(item.recipe_id)
    ? ` <a href="${escapeHtml(importedRecipeUrl(item.recipe_id))}" target="_blank" rel="noopener" title="${escapeHtml(t('openInTandoorBtn'))}">↗</a>`
    : '';
  return `<label class="health-row" data-name="${escapeHtml(item.name.toLowerCase())}">
    <input type="checkbox" data-key="${escapeHtml(item.key)}" data-name="${escapeHtml(item.name)}" />
    <span>${escapeHtml(item.name)}${link}</span></label>`;
}

async function openHealthDetail(metric, scroll = true) {
  healthState.open = metric;
  renderHealth(healthState.data);
  let data;
  try {
    data = await (await fetch(`/api/health/items/${metric}`)).json();
  } catch (e) { return; }
  if (healthState.open !== metric) return;
  const box = el('health-detail');
  box.classList.remove('hidden');
  box.innerHTML = `
    <div class="health-detail-head">
      <h4>${escapeHtml(t('health_' + metric))}</h4>
      <button class="btn secondary health-detail-close" type="button">${t('modalClose')}</button>
    </div>
    <p class="health-detail-hint">${t('healthIgnoreHint')}</p>
    ${data.items.length ? `
      <div class="health-detail-bar">
        <input type="search" class="health-filter" placeholder="${escapeHtml(t('healthFilter'))}" />
        <label><input type="checkbox" class="health-select-all" /> ${t('healthSelectAll')}</label>
        <button class="btn health-ignore-btn" type="button" disabled>${t('healthIgnoreSelected')}</button>
      </div>
      <div class="health-list health-open-list">${data.items.map(healthRow).join('')}</div>`
    : `<p class="health-detail-hint">${t('healthNoEntries')}</p>`}
    ${data.ignored.length ? `
      <details class="health-ignored">
        <summary>${tf('healthIgnoredTitle', { n: data.ignored.length })}</summary>
        <div class="health-list health-ignored-list">${data.ignored.map(healthRow).join('')}</div>
        <button class="btn secondary health-unignore-btn" type="button" disabled>${t('healthUnignoreSelected')}</button>
      </details>` : ''}`;

  if (scroll) box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  box.querySelector('.health-detail-close').addEventListener('click', closeHealthDetail);
  const openList = box.querySelector('.health-open-list');
  const ignoreBtn = box.querySelector('.health-ignore-btn');
  const unignoreBtn = box.querySelector('.health-unignore-btn');
  const checked = (list) => (list ? [...list.querySelectorAll('input:checked')] : []);
  const sync = () => {
    if (ignoreBtn) ignoreBtn.disabled = !checked(openList).length;
    if (unignoreBtn) unignoreBtn.disabled = !checked(box.querySelector('.health-ignored-list')).length;
  };
  box.addEventListener('change', sync);

  const filter = box.querySelector('.health-filter');
  if (filter) filter.addEventListener('input', () => {
    const q = filter.value.trim().toLowerCase();
    openList.querySelectorAll('.health-row').forEach((row) => row.classList.toggle('hidden', !!q && !row.dataset.name.includes(q)));
  });
  const selectAll = box.querySelector('.health-select-all');
  if (selectAll) selectAll.addEventListener('change', () => {
    openList.querySelectorAll('.health-row:not(.hidden) input').forEach((cb) => { cb.checked = selectAll.checked; });
    sync();
  });

  const post = async (url, body) => {
    const resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (resp.ok) {
      renderHealth(await resp.json());
      openHealthDetail(metric, false);
    }
  };
  if (ignoreBtn) ignoreBtn.addEventListener('click', () => {
    ignoreBtn.disabled = true;
    post('/api/health/ignore', { metric, items: checked(openList).map((cb) => ({ key: cb.dataset.key, name: cb.dataset.name })) });
  });
  if (unignoreBtn) unignoreBtn.addEventListener('click', () => {
    unignoreBtn.disabled = true;
    post('/api/health/unignore', { metric, keys: checked(box.querySelector('.health-ignored-list')).map((cb) => cb.dataset.key) });
  });
}

async function loadHealth() {
  try {
    let data = await (await fetch('/api/health')).json();
    // Refresh on its own (no AI, runs in the background) when it was never
    // computed or something was applied since - e.g. after "Fix" + review.
    if (currentArea === 'maintain' && !data.running && !data.error && (data.stale || !data.computed_at)) {
      data = await (await fetch('/api/health/refresh', { method: 'POST' })).json();
    }
    renderHealth(data);
    clearTimeout(loadHealth.timer);
    const scanning = Object.values(data.pending || {}).some((p) => p.scanning);
    if ((data.running || scanning) && currentArea === 'maintain') loadHealth.timer = setTimeout(loadHealth, 3000);
  } catch (e) { /* optional panel */ }
}

el('health-refresh-btn').addEventListener('click', async () => {
  renderHealth(await (await fetch('/api/health/refresh', { method: 'POST' })).json());
  loadHealth();
});

// ---------- Maintenance tools ----------

const toolsState = { jobId: null, pollTimer: null, job: null, selected: new Set(), busy: false };

// ---------- Meal plan ----------

// The plan starts on the shopping day: the coming Saturday (today if it is one).
function nextSaturday() {
  const d = new Date();
  d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// ---------- In season now ----------

const SEASON_SHOWN = 6;

async function loadSeason() {
  try {
    const data = await (await fetch('/api/season')).json();
    renderSeason(data.produce, false);
  } catch (e) { /* optional */ }
}

// The first few, the rest behind "+n".
function renderSeason(produce, all) {
  const box = el('season-now');
  box.classList.toggle('hidden', !produce.length);
  const month = new Date().toLocaleDateString(LANG_CODE, { month: 'long' });
  const shown = all ? produce : produce.slice(0, SEASON_SHOWN);
  const rest = produce.length - SEASON_SHOWN;
  box.innerHTML = `🌱 ${escapeHtml(tf('seasonIn', { month }))}: ${escapeHtml(shown.join(' · '))}${rest > 0
    ? `<button type="button">${all ? t('seasonLess') : `+${rest}`}</button>` : ''}`;
  const btn = box.querySelector('button');
  if (btn) btn.addEventListener('click', () => renderSeason(produce, !all));
}

// ---------- How was it? (cook log) ----------

async function loadCooked() {
  try {
    const res = await fetch('/api/cooked/pending');
    if (!res.ok) throw new Error();
    const items = (await res.json()).items;
    el('cooked-card').classList.toggle('hidden', !items.length);
    el('cooked-list').innerHTML = items.map((i) => `
      <div class="cooked-row" data-plan="${i.plan_id}" data-recipe="${i.recipe.id}" data-date="${i.date}" data-servings="${i.servings}">
        <div class="ct-main"><div class="ct-name">${escapeHtml(i.recipe.name)}</div>
          <div class="ct-meta">${escapeHtml(new Date(i.date + 'T12:00').toLocaleDateString([], { weekday: 'short', day: '2-digit', month: '2-digit' }))}${i.meal_type ? ' · ' + escapeHtml(i.meal_type) : ''}</div></div>
        <div class="cooked-stars">${[1, 2, 3, 4, 5].map((n) => `<button type="button" data-rating="${n}" aria-label="${n}">★</button>`).join('')}</div>
        <button class="btn secondary cooked-skip" type="button">${t('cookedNotCooked')}</button>
      </div>`).join('');
    el('cooked-list').querySelectorAll('.cooked-row').forEach((row) => {
      const send = async (rating) => {
        row.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        const res2 = await fetch('/api/cooked', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plan_id: idValue(row.dataset.plan), recipe_id: idValue(row.dataset.recipe), date: row.dataset.date,
            servings: Number(row.dataset.servings), rating }),
        });
        if (res2.ok) {
          row.classList.add('done');
          row.querySelector('.cooked-skip').textContent = rating ? `✓ ${'★'.repeat(rating)}` : t('cookedSkipped');
        } else {
          row.querySelectorAll('button').forEach((b) => { b.disabled = false; });
        }
      };
      row.querySelectorAll('[data-rating]').forEach((b) => b.addEventListener('click', () => send(Number(b.dataset.rating))));
      row.querySelector('.cooked-skip').addEventListener('click', () => send(null));
    });
  } catch (e) {
    el('cooked-card').classList.add('hidden');
  }
}

// ---------- What can I cook today? ----------

async function searchCookToday() {
  const have = el('ct-have').value.trim();
  if (!have) return;
  el('ct-status').textContent = t('cookTodaySearching');
  try {
    const params = new URLSearchParams({ have });
    const data = await (await fetch(`/api/cook-today?${params}`)).json();
    if (data.building && !data.results.length && !data.built_at) {
      // First use: the ingredient index is being built from every recipe.
      el('ct-status').textContent = t('cookTodayBuilding');
      clearTimeout(searchCookToday.timer);
      searchCookToday.timer = setTimeout(searchCookToday, 4000);
      return;
    }
    el('ct-status').textContent = data.results.length ? '' : t('cookTodayNone');
    el('ct-results').innerHTML = data.results.map((r) => {
      const link = isMealie() ? importedRecipeUrl(r.slug)
        : (APP_CONFIG.tandoor_url ? `${APP_CONFIG.tandoor_url}/view/recipe/${r.id}` : null);
      const name = link ? `<a href="${link}" target="_blank" rel="noopener">${escapeHtml(r.name)}</a>` : escapeHtml(r.name);
      // What's there is what you typed (and the score says how much) - so
      // only what's missing is spelled out.
      const meta = [
        r.missing.length ? `${t('cookTodayMissing')}: ${escapeHtml(r.missing.slice(0, 6).join(', '))}${r.missing.length > 6 ? ' …' : ''}`
          : `<span class="ct-have">✓ ${t('cookTodayComplete')}</span>`,
        r.minutes ? `${r.minutes} min` : '',
        r.season && r.season.length ? `🌱 ${escapeHtml(r.season.join(', '))}` : '',
        r.rating ? '★'.repeat(Math.round(r.rating)) : '',
        r.disliked && r.disliked.length ? `<span class="ct-disliked">👎 ${escapeHtml(r.disliked.join(', '))}</span>` : '',
      ].filter(Boolean).join(' · ');
      return `<div class="ct-row">
        <div class="ct-score">${r.matched.length}/${r.needed}</div>
        <div class="ct-main"><div class="ct-name">${name}</div><div class="ct-meta">${meta}</div></div>
        <button class="btn secondary ct-plan" type="button" data-id="${r.id}" data-name="${escapeHtml(r.name)}">${t('cookTodayPlanBtn')}</button>
      </div>`;
    }).join('');
    el('ct-results').querySelectorAll('.ct-plan').forEach((b) => b.addEventListener('click', async () => {
      const meal = el('mp-meal');
      if (!meal.value) { el('ct-status').textContent = t('mealPlanNoMealTypes'); return; }
      b.disabled = true;
      const res = await fetch('/api/cook-today/plan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          recipe: { id: idValue(b.dataset.id), name: b.dataset.name },
          meal_type: { id: idValue(meal.value), name: meal.options[meal.selectedIndex].text },
          add_to_shopping: el('mp-shopping').checked,
        }),
      });
      b.textContent = res.ok ? `✓ ${tf('cookTodayPlanned', { meal: meal.options[meal.selectedIndex].text })}` : t('toolStatusError');
      if (!res.ok) b.disabled = false;
    }));
  } catch (e) {
    el('ct-status').textContent = `${t('toolStatusError')}: ${e.message}`;
  }
}

el('cook-today-form').addEventListener('submit', (e) => { e.preventDefault(); searchCookToday(); });

// Photo of the fridge / pantry: the AI lists what it sees, which goes into
// the field (next to what was typed) - then the usual search runs.
// Reads the ingredients on fridge / pantry photos and adds them to `input`.
async function addIngredientsFromPhotos(files, input, status) {
  if (!files.length) return false;
  status.textContent = t('cookTodayPhotoReading');
  const form = new FormData();
  files.slice(0, 4).forEach((f) => form.append('files', f));
  try {
    const res = await fetch('/api/cook-today/photo', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (!data.ingredients.length) { status.textContent = t('cookTodayPhotoNone'); return false; }
    const typed = input.value.split(',').map((x) => x.trim()).filter(Boolean);
    const known = new Set(typed.map((x) => x.toLowerCase()));
    input.value = [...typed, ...data.ingredients.filter((x) => !known.has(x.toLowerCase()))].join(', ');
    status.textContent = '';
    return true;
  } catch (err) {
    status.textContent = `${t('toolStatusError')}: ${err.message}`;
    return false;
  }
}

el('ct-photo').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (await addIngredientsFromPhotos(files, el('ct-have'), el('ct-status'))) searchCookToday();
});

el('mp-photo').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  const hint = el('mp-hint');
  hint.classList.remove('hidden');
  await addIngredientsFromPhotos(files, el('mp-have'), hint);
  if (!hint.textContent) hint.classList.add('hidden');
});

async function loadMealPlanOptions() {
  if (!el('mp-start').value) el('mp-start').value = nextSaturday();
  const select = el('mp-meal');
  const hint = el('mp-hint');
  try {
    const res = await fetch('/api/tools/meal-plan/options');
    const data = await res.json();
    const previous = select.value;
    select.innerHTML = data.meal_types.map((m) => `<option value="${m.id}">${escapeHtml(m.name)}</option>`).join('');
    if (previous) select.value = previous;
    // Default to dinner if there is one.
    if (!previous) {
      const dinner = data.meal_types.find((m) => /abend|dinner|dîner|cena/i.test(m.name));
      if (dinner) select.value = dinner.id;
    }
    const none = data.meal_types.length === 0;
    hint.textContent = none ? t('mealPlanNoMealTypes') : '';
    hint.classList.toggle('hidden', !none);
    el('mp-start-btn').disabled = none;
  } catch (e) {
    hint.textContent = `${t('toolNewRecipesStatusFailed')}: ${e.message}`;
    hint.classList.remove('hidden');
  }
}

// The plan area shows one plan run as a week (one card per day) - checkbox
// per day, "another recipe" per day, and one button to add the selected days
// to Tandoor's meal plan. The last run is remembered per browser.
const planState = { jobId: null, job: null, timer: null, selected: new Set(), busy: false, changed: new Set() };

function rememberPlan(jobId) {
  try { if (jobId) localStorage.setItem('th.planJob', jobId); else localStorage.removeItem('th.planJob'); } catch (e) { /* optional */ }
}

// ---------- Household profile ----------
// Weekday names from the browser in the UI language, Monday = 0.
function weekdayName(i) {
  return new Date(2024, 0, 1 + i).toLocaleDateString(LANG_CODE, { weekday: 'long' });
}

function householdSummary(h) {
  return [
    h.persons ? tf('householdSummaryPersons', { n: h.persons }) : '',
    h.avoid ? `🚫 ${h.avoid}` : '',
    Object.keys(h.weekdays || {}).sort().map((d) => `${weekdayName(+d)}: ${h.weekdays[d]}`).join(', '),
  ].filter(Boolean).join(' · ');
}

function fillHousehold(h) {
  el('hh-persons').value = h.persons || '';
  el('hh-avoid').value = h.avoid || '';
  el('hh-dislikes').value = h.dislikes || '';
  el('hh-weekdays').innerHTML = [0, 1, 2, 3, 4, 5, 6].map((d) => `<label><span>${escapeHtml(weekdayName(d))}</span>
    <input type="text" maxlength="100" data-day="${d}" value="${escapeHtml((h.weekdays || {})[d] || '')}" placeholder="–" /></label>`).join('');
  el('household-summary').textContent = householdSummary(h);
}

async function loadHousehold() {
  try {
    fillHousehold((await (await fetch('/api/settings')).json()).household || {});
  } catch (err) { /* the card stays empty */ }
}

el('household-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const weekdays = {};
  el('hh-weekdays').querySelectorAll('input').forEach((i) => { if (i.value.trim()) weekdays[i.dataset.day] = i.value.trim(); });
  const household = { persons: +el('hh-persons').value || 0, avoid: el('hh-avoid').value,
    dislikes: el('hh-dislikes').value, weekdays };
  try {
    const res = await fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ household }) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
    fillHousehold((await res.json()).household);
    el('hh-status').textContent = t('householdSaved');
  } catch (err) {
    el('hh-status').textContent = `${t('householdSaveFailed')}: ${err.message}`;
  }
});

async function openPlanArea() {
  loadMealPlanOptions();
  loadHousehold();
  if (!planState.jobId) {
    try { planState.jobId = localStorage.getItem('th.planJob'); } catch (e) { planState.jobId = null; }
  }
  if (planState.jobId) pollPlan();
}

el('meal-plan-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const select = el('mp-meal');
  el('plan-error').classList.add('hidden');
  el('plan-week').innerHTML = '';
  el('plan-actions').classList.add('hidden');
  el('plan-progress').classList.remove('hidden');
  try {
    const res = await fetch('/api/tools/meal-plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: el('mp-start').value,
        days: Number(el('mp-days').value),
        meal_type: { id: idValue(select.value), name: select.options[select.selectedIndex]?.textContent || '' },
        wishes: el('mp-wishes').value,
        at_home: el('mp-have').value,
        add_to_shopping: el('mp-shopping').checked,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    planState.jobId = data.job_id;
    planState.selected.clear();
    planState.changed.clear();
    rememberPlan(data.job_id);
    pollPlan();
  } catch (err) {
    el('plan-progress').classList.add('hidden');
    showPlanError(err.message);
  }
});

function showPlanError(msg) {
  el('plan-error').textContent = msg;
  el('plan-error').classList.remove('hidden');
}

async function pollPlan() {
  clearTimeout(planState.timer);
  try {
    const res = await fetch(`/api/tools/jobs/${planState.jobId}`);
    if (!res.ok) { planState.jobId = null; rememberPlan(null); el('plan-progress').classList.add('hidden'); return; }
    const job = await res.json();
    if (job.status === 'scanning') {
      el('plan-progress').classList.remove('hidden');
      planState.timer = setTimeout(pollPlan, 1500);
      return;
    }
    el('plan-progress').classList.add('hidden');
    if (job.status === 'error') { showPlanError(job.error || t('toolStartFailed')); return; }
    // New plan: every suggested day is pre-selected.
    if (!planState.job || planState.job.id !== job.id) {
      planState.selected = new Set(job.suggestions.filter((s) => s.status === 'pending').map((s) => s.id));
    }
    renderWeek(job);
  } catch (e) {
    el('plan-progress').classList.add('hidden');
    showPlanError(e.message);
  }
}

function renderWeek(job) {
  planState.job = job;
  const days = job.meta.days || [...new Set(job.suggestions.map((s) => s.detail.date))];
  const taken = new Set(job.meta.taken_days || []);
  const byDate = {};
  job.suggestions.forEach((s) => { byDate[s.detail.date] = s; });
  el('plan-week').innerHTML = days.map((d) => {
    const date = new Date(`${d}T12:00:00`);
    const head = `<div class="plan-day-head"><strong>${date.toLocaleDateString([], { weekday: 'short' })}</strong> ${date.toLocaleDateString([], { day: '2-digit', month: '2-digit' })}</div>`;
    const s = byDate[d];
    if (taken.has(d)) return `<div class="plan-day taken">${head}<div class="plan-empty">${t('planTaken')}</div></div>`;
    const reroll = `<button class="btn secondary plan-reroll" type="button" data-date="${d}" ${planState.busy ? 'disabled' : ''} title="${t('planReroll')}">🎲 ${t('planReroll')}</button>`;
    if (!s || s.status === 'skipped') return `<div class="plan-day empty">${head}<div class="plan-empty">${t('planNoSuggestion')}</div>${reroll}</div>`;
    const d_ = s.detail;
    const body = `<div class="plan-recipe">${escapeHtml(d_.recipe.name)}</div>
      <div class="plan-meta">${d_.minutes ? `${d_.minutes} min` : ''}${d_.reason ? ` · ${escapeHtml(d_.reason)}` : ''}</div>`;
    if (s.status === 'applied') return `<div class="plan-day applied">${head}${body}<div class="plan-done">✓ ${t('planApplied')}</div></div>`;
    const error = s.status === 'error' ? `<div class="inbox-error">${escapeHtml(s.error || '')}</div>` : '';
    return `<div class="plan-day ${planState.selected.has(s.id) ? 'selected' : ''} ${planState.changed.has(s.id) ? 'changed' : ''}" data-id="${s.id}">
      ${head}<label class="plan-check"><input type="checkbox" ${planState.selected.has(s.id) ? 'checked' : ''} ${planState.busy ? 'disabled' : ''}></label>
      ${body}${error}${reroll}</div>`;
  }).join('');

  el('plan-week').querySelectorAll('.plan-day[data-id]').forEach((card) => {
    const box = card.querySelector('input');
    box.addEventListener('change', () => {
      if (box.checked) planState.selected.add(card.dataset.id); else planState.selected.delete(card.dataset.id);
      renderWeek(planState.job);
    });
  });
  el('plan-week').querySelectorAll('.plan-reroll').forEach((b) => b.addEventListener('click', () => rerollDay(b.dataset.date)));
  const pending = job.suggestions.filter((s) => s.status === 'pending' && planState.selected.has(s.id)).length;
  el('plan-apply-btn').textContent = tf('planApplySelected', { count: pending });
  el('plan-apply-btn').disabled = !pending || planState.busy;
  el('plan-actions').classList.toggle('hidden', !days.length);
  renderPlanChat(job, days.length > 0);
}

// ---------- Changing the plan in a chat ----------

function renderPlanChat(job, show) {
  el('plan-chat').classList.toggle('hidden', !show || job.status !== 'ready');
  const log = (job.meta && job.meta.chat) || [];
  el('plan-chat-log').innerHTML = log.map((m) => {
    const text = m.role === 'assistant' && m.changed ? `${m.text} (${tf('planChatChanged', { n: m.changed })})` : m.text;
    return `<div class="plan-chat-msg ${m.role === 'user' ? 'user' : 'assistant'}">${escapeHtml(text)}</div>`;
  }).join('') + (planState.chatPending ? `<div class="plan-chat-msg user">${escapeHtml(planState.chatPending)}</div>
    <div class="plan-chat-msg assistant pending">${t('planChatThinking')}</div>` : '');
}

el('plan-chat-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = el('plan-chat-input');
  const message = input.value.trim();
  if (!message || planState.busy || !planState.jobId) return;
  planState.busy = true;
  planState.chatPending = message;
  input.value = '';
  el('plan-chat-send').disabled = true;
  renderWeek(planState.job);
  try {
    const res = await fetch(`/api/tools/meal-plan/${planState.jobId}/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    planState.changed = new Set(data.meta.last_changed || []);
    planState.changed.forEach((id) => planState.selected.add(id));
    planState.chatPending = null;
    planState.busy = false;
    renderWeek(data);
  } catch (err) {
    planState.chatPending = null;
    planState.busy = false;
    input.value = message;
    renderWeek(planState.job);
    el('plan-status').textContent = `${t('toolStatusError')}: ${err.message}`;
  } finally {
    el('plan-chat-send').disabled = false;
  }
});

async function rerollDay(date) {
  if (planState.busy) return;
  planState.busy = true;
  el('plan-status').textContent = t('planRerolling');
  renderWeek(planState.job);
  try {
    const res = await fetch(`/api/tools/meal-plan/${planState.jobId}/reroll`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ date }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const fresh = data.suggestions.find((s) => s.detail.date === date);
    if (fresh) planState.selected.add(fresh.id);
    el('plan-status').textContent = '';
    planState.busy = false;
    renderWeek(data);
  } catch (e) {
    planState.busy = false;
    el('plan-status').textContent = e.message;
    renderWeek(planState.job);
  }
}

el('plan-apply-btn').addEventListener('click', async () => {
  const todo = planState.job.suggestions.filter((s) => s.status === 'pending' && planState.selected.has(s.id));
  if (!todo.length || planState.busy) return;
  planState.busy = true;
  const jobId = planState.jobId;
  let batch = null;
  try {
    const batchId = await queueActions('apply', todo.map((s) => ({ job_id: jobId, id: s.id })));
    todo.forEach((s) => planState.selected.delete(s.id));
    batch = await watchBatch(batchId, (b) => {
      if (b.done < b.total) el('plan-status').textContent = batchProgressText('apply', b);
    }, () => planState.jobId === jobId);
  } catch (e) {
    el('plan-status').textContent = `${t('toolStatusError')}: ${e.message}`;
  }
  planState.busy = false;
  if (batch) el('plan-status').textContent = batch.failed ? batchResultText(batch) : t('planAppliedAll');
  updateInboxBadge();
  pollPlan();
});

const newRecipesState = { existing: 0 };

function showBaselineChoice(show, count) {
  el('new-recipes-choice').classList.toggle('hidden', !show);
  if (!show) return;
  el('new-recipes-choice-text').textContent = tf('newRecipesChoiceQuestion', { count });
  el('new-recipes-choice-hint').textContent = tf('newRecipesChoiceHint', { count });
}

async function setBaseline(existingDone) {
  ['new-recipes-existing-done', 'new-recipes-existing-new'].forEach((id) => { el(id).disabled = true; });
  try {
    const res = await fetch('/api/tools/new-recipes/baseline', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ existing_done: existingDone }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
  } catch (e) {
    el('new-recipes-status').textContent = `${t('toolNewRecipesStatusFailed')}: ${e.message}`;
  } finally {
    ['new-recipes-existing-done', 'new-recipes-existing-new'].forEach((id) => { el(id).disabled = false; });
  }
  loadNewRecipesStatus();
}

el('new-recipes-existing-done').addEventListener('click', () => setBaseline(true));
el('new-recipes-existing-new').addEventListener('click', () => setBaseline(false));
el('new-recipes-change-baseline').addEventListener('click', () => {
  showBaselineChoice(true, newRecipesState.existing);
});

async function loadNewRecipesStatus() {
  const label = el('new-recipes-status');
  const btn = el('new-recipes-start-btn');
  btn.disabled = true;
  label.textContent = t('toolNewRecipesChecking');
  try {
    const res = await fetch('/api/tools/new-recipes/status');
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    // Before the first run: do the recipes existing now count as done?
    newRecipesState.existing = data.existing_count;
    showBaselineChoice(data.needs_choice, data.existing_count);
    if (data.needs_choice) {
      label.textContent = t('toolNewRecipesChooseFirst');
    } else {
      label.textContent = data.new_count > 0 ? tf('toolNewRecipesCount', { count: data.new_count }) : t('toolNewRecipesNone');
      el('new-recipes-baseline-info').textContent = tf('newRecipesBaselineInfo', { handled: data.handled_count, total: data.existing_count });
    }
    el('new-recipes-change-baseline').classList.toggle('hidden', !!data.needs_choice);
    btn.disabled = !data.new_count;

    const auto = el('new-recipes-auto');
    if (data.auto_interval_hours > 0) {
      const next = data.next_auto_run_at
        ? new Date(data.next_auto_run_at * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
        : '–';
      auto.textContent = tf('toolNewRecipesAuto', { hours: data.auto_interval_hours, next });
      auto.classList.remove('hidden');
    } else {
      auto.classList.add('hidden');
    }

    const list = el('new-recipes-open-jobs');
    list.innerHTML = (data.open_jobs || []).map((job) => {
      const when = new Date(job.created_at * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
      const label = job.status === 'scanning'
        ? tf('toolNewRecipesJobRunning', { when })
        : tf(job.auto ? 'toolNewRecipesJobAuto' : 'toolNewRecipesJobManual', { when, count: job.pending });
      return `<div class="new-recipes-open-job"><span>${escapeHtml(label)}</span>
        <button class="btn secondary" type="button" data-job-id="${job.id}">${t('toolNewRecipesOpenJob')}</button></div>`;
    }).join('');
    list.querySelectorAll('button').forEach((b) => {
      b.addEventListener('click', () => openToolJob(b.dataset.jobId, t('toolNewRecipesTitle')));
    });
  } catch (e) {
    label.textContent = `${t('toolNewRecipesStatusFailed')}: ${e.message}`;
  }
}

function closeToolRun() {
  clearTimeout(toolsState.pollTimer);
  toolsState.jobId = null;
  toolsState.job = null;
  toolsState.selected.clear();
  el('tools-run-view').classList.add('hidden');
  el('tools-cards-view').classList.remove('hidden');
}

el('tools-back-btn').addEventListener('click', () => {
  closeToolRun();
  loadNewRecipesStatus();
  loadHealth();
});

document.querySelectorAll('.tool-start-btn').forEach((btn) => {
  btn.addEventListener('click', () => startTool(btn.dataset.endpoint, toolTitle(btn.dataset.tool)));
});

async function startTool(endpoint, title, body) {
  resetToolRunView(title);
  try {
    const res = await fetch(endpoint, body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : { method: 'POST' });
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (e) { /* not JSON */ }
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    toolsState.jobId = data.job_id;
    pollToolJob();
  } catch (e) {
    showToolError(`${t('toolStartFailed')}: ${e.message}`);
  }
}

// Opens a run that already exists (e.g. one the automatic schedule started).
function openToolJob(jobId, title) {
  resetToolRunView(title);
  toolsState.jobId = jobId;
  pollToolJob();
}

function resetToolRunView(title) {
  el('tools-cards-view').classList.add('hidden');
  el('tools-run-view').classList.remove('hidden');
  el('tools-run-title').textContent = title;
  el('tools-run-progress').classList.remove('hidden');
  el('tools-run-progress-label').textContent = t('toolScanStarting');
  el('tools-run-usage').classList.add('hidden');
  el('tools-run-error').classList.add('hidden');
  el('tools-cancelled-note').classList.add('hidden');
  el('tools-skipped-note').classList.add('hidden');
  el('tools-cancel-btn').disabled = false;
  el('tools-cancel-btn').textContent = t('toolCancelBtn');
  el('tools-suggestions-list').innerHTML = '';
  el('tools-bulk-bar').classList.add('hidden');
  el('tools-bulk-status').textContent = '';
  toolsState.job = null;
  toolsState.selected.clear();
}

el('tools-cancel-btn').addEventListener('click', async () => {
  if (!toolsState.jobId) return;
  const btn = el('tools-cancel-btn');
  btn.disabled = true;
  btn.textContent = t('toolCancelling');
  try {
    await fetch(`/api/tools/jobs/${toolsState.jobId}/cancel`, { method: 'POST' });
  } catch {
    // the next poll tick will show whatever state the job ends up in either way
  }
});

function pollToolJob() {
  clearTimeout(toolsState.pollTimer);
  const tick = async () => {
    try {
      const res = await fetch(`/api/tools/jobs/${toolsState.jobId}`);
      if (!res.ok) throw new Error(t('toolJobNotFoundError'));
      const job = await res.json();
      renderToolUsage(job);

      if (job.status === 'error') {
        showToolError(job.error || t('toolStartFailed'));
        return;
      }
      if (job.status === 'cancelled') {
        el('tools-run-progress').classList.add('hidden');
        el('tools-cancelled-note').classList.remove('hidden');
        renderToolSuggestions(job);
        return;
      }
      if (job.status === 'ready' || job.status === 'done') {
        el('tools-run-progress').classList.add('hidden');
        renderToolSuggestions(job);
        return;
      }

      el('tools-run-progress-label').textContent = job.progress_label || t('toolScanStarting');
      toolsState.pollTimer = setTimeout(tick, 1500);
    } catch (e) {
      showToolError(e.message);
    }
  };
  tick();
}

function showToolError(msg) {
  el('tools-run-progress').classList.add('hidden');
  const box = el('tools-run-error');
  box.textContent = msg;
  box.classList.remove('hidden');
}

function renderToolUsage(job) {
  const box = el('tools-run-usage');
  const hasActual = job.token_usage && job.token_usage.input_tokens > 0;

  if (hasActual) {
    // Once real usage starts coming in, switch from the pre-start estimate
    // to the actual running total - the estimate has done its job by then.
    box.textContent = tf('toolTokenUsage', {
      input: job.token_usage.input_tokens,
      output: job.token_usage.output_tokens,
    });
    box.classList.remove('hidden');
  } else if (job.cost_estimate) {
    box.textContent = job.cost_estimate;
    box.classList.remove('hidden');
  } else {
    box.classList.add('hidden');
  }
}

// Selection + bulk apply: every pending suggestion gets a checkbox, and the
// bar above the list applies/skips all checked ones. They're sent one after
// another (not in parallel) - merges touch shared recipes, and running them
// sequentially keeps the same order and safety as clicking them one by one.
// Recipes a run looked at but made no suggestion for - with the reason, so
// a recipe listed in a tile doesn't silently disappear.
function renderToolSkipped(job) {
  const skipped = (job.meta && job.meta.skipped) || [];
  const box = el('tools-skipped-note');
  box.classList.toggle('hidden', !skipped.length);
  if (!skipped.length) { box.innerHTML = ''; return; }
  const unchanged = skipped.filter((s) => s.reason === 'unchanged');
  const failed = skipped.filter((s) => s.reason === 'failed');
  box.innerHTML = [
    unchanged.length ? `<p>${escapeHtml(tf('toolSkippedUnchanged', { names: unchanged.map((s) => s.name).join(', ') }))}</p>` : '',
    failed.length ? `<p>${escapeHtml(t('toolSkippedFailed'))}</p><ul>${failed.map((s) =>
      `<li><strong>${escapeHtml(s.name)}</strong>: ${escapeHtml(s.error || '')}</li>`).join('')}</ul>` : '',
  ].join('');
}

function renderToolSuggestions(job) {
  toolsState.job = job;
  const list = el('tools-suggestions-list');
  const pendingIds = new Set(job.suggestions.filter((s) => s.status === 'pending').map((s) => s.id));
  // Drop selections that are no longer pending (applied/skipped/failed).
  toolsState.selected.forEach((id) => { if (!pendingIds.has(id)) toolsState.selected.delete(id); });

  renderToolSkipped(job);
  if (job.suggestions.length === 0) {
    el('tools-bulk-bar').classList.add('hidden');
    list.innerHTML = `<p style="color:#8f9689;">${t('toolNoSuggestions')}</p>`;
    return;
  }
  el('tools-bulk-bar').classList.toggle('hidden', pendingIds.size === 0 && !toolsState.busy);

  const queued = new Set(job.queued_ids || []);
  list.innerHTML = job.suggestions.map((s) => `
    <div class="tool-suggestion-row ${s.status} ${queued.has(s.id) ? 'queued' : ''}" data-suggestion-id="${s.id}">
      ${s.status === 'pending'
        ? `<input type="checkbox" class="suggestion-check" ${toolsState.selected.has(s.id) ? 'checked' : ''} ${toolsState.busy || queued.has(s.id) ? 'disabled' : ''}>`
        : ''}
      <div class="suggestion-text">
        ${queued.has(s.id) ? `<span class="queued-label">⏳ ${t('queuedLabel')}</span> ` : ''}${escapeHtml(s.summary)}
        ${s.preview ? `<details class="suggestion-preview"><summary>${t('toolShowPreview')}</summary><pre>${escapeHtml(s.preview)}</pre></details>` : ''}
      </div>
      ${s.status === 'pending' ? '' : `<span class="suggestion-status-label">${s.status === 'applied' ? t('toolStatusApplied') : s.status === 'skipped' ? t('toolStatusSkipped') : s.status === 'undone' ? t('toolStatusUndone') : escapeHtml(s.error || t('toolStatusError'))}</span>`}
      ${s.status === 'applied' && s.undoable && !queued.has(s.id) ? `<button class="btn secondary suggestion-undo" type="button" data-id="${s.id}">${t('undoBtn')}</button>` : ''}
    </div>
  `).join('');

  list.querySelectorAll('.suggestion-undo').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    const jobId = toolsState.jobId;
    try {
      const batchId = await queueActions('undo', [{ job_id: jobId, id: b.dataset.id }]);
      const batch = await watchBatch(batchId, () => {}, () => toolsState.jobId === jobId);
      el('tools-bulk-status').textContent = batchResultText(batch);
    } catch (e) {
      el('tools-bulk-status').textContent = `${t('toolStatusError')}: ${e.message}`;
    }
    const res = await fetch(`/api/tools/jobs/${jobId}`);
    if (res.ok && toolsState.jobId === jobId) renderToolSuggestions(await res.json());
  }));
  list.querySelectorAll('.tool-suggestion-row.pending').forEach((row) => {
    const box = row.querySelector('.suggestion-check');
    const toggle = (checked) => {
      if (checked) toolsState.selected.add(row.dataset.suggestionId);
      else toolsState.selected.delete(row.dataset.suggestionId);
      updateBulkBar();
    };
    box.addEventListener('change', () => toggle(box.checked));
    // Clicking anywhere on the row toggles it too - except the preview.
    row.addEventListener('click', (e) => {
      if (toolsState.busy || box.disabled || e.target === box || e.target.closest('details')) return;
      box.checked = !box.checked;
      toggle(box.checked);
    });
  });
  updateBulkBar();
}

function updateBulkBar() {
  const job = toolsState.job;
  const pending = job ? job.suggestions.filter((s) => s.status === 'pending').length : 0;
  const n = toolsState.selected.size;
  const all = el('tools-select-all');
  all.checked = pending > 0 && n === pending;
  all.indeterminate = n > 0 && n < pending;
  all.disabled = toolsState.busy || pending === 0;
  el('tools-apply-selected-btn').textContent = tf('toolApplySelectedBtn', { count: n });
  el('tools-skip-selected-btn').textContent = tf('toolSkipSelectedBtn', { count: n });
  el('tools-apply-selected-btn').disabled = toolsState.busy || n === 0;
  el('tools-skip-selected-btn').disabled = toolsState.busy || n === 0;
}

el('tools-select-all').addEventListener('change', (e) => {
  const job = toolsState.job;
  if (!job) return;
  const queued = new Set(job.queued_ids || []);
  toolsState.selected = new Set(e.target.checked ? job.suggestions.filter((s) => s.status === 'pending' && !queued.has(s.id)).map((s) => s.id) : []);
  renderToolSuggestions(job);
});

el('tools-apply-selected-btn').addEventListener('click', () => runBulkAction('apply'));
el('tools-skip-selected-btn').addEventListener('click', () => runBulkAction('skip'));

async function runBulkAction(action) {
  const job = toolsState.job;
  if (!job || toolsState.busy) return;
  // Keep the list order, so e.g. merges run in the order they're shown.
  const ids = job.suggestions.filter((s) => toolsState.selected.has(s.id)).map((s) => s.id);
  if (ids.length === 0) return;

  const jobId = toolsState.jobId;
  const status = el('tools-bulk-status');
  let batchId;
  try {
    batchId = await queueActions(action, ids.map((id) => ({ job_id: jobId, id })));
  } catch (e) {
    status.textContent = `${t('toolStatusError')}: ${e.message}`;
    return;
  }
  toolsState.selected.clear();
  toolsState.busy = true;
  renderToolSuggestions(job);
  let batch = null;
  try {
    batch = await watchBatch(batchId, async (b) => {
      if (toolsState.jobId !== jobId) return;
      if (b.done < b.total) status.textContent = batchProgressText(action, b);
      const res = await fetch(`/api/tools/jobs/${jobId}`);
      if (res.ok) renderToolSuggestions(await res.json());
    }, () => toolsState.jobId === jobId); // user left this run - it goes on in the background
  } finally {
    toolsState.busy = false;
    updateInboxBadge();
    if (toolsState.jobId === jobId) {
      status.textContent = batchResultText(batch);
      if (toolsState.job) renderToolSuggestions(toolsState.job);
    }
  }
}

// ---------- Init ----------

// Bookmarklet: opens this app with ?import=<page address> in a new tab.
el('bookmarklet').href = `javascript:(()=>{window.open(${JSON.stringify(location.origin + '/?import=')}+encodeURIComponent(location.href),'_blank')})()`;
el('bookmarklet').addEventListener('click', (e) => { e.preventDefault(); showUploadError(t('bookmarkletClick')); });

// Imports handed over in the address: ?import=<url> (bookmarklet),
// ?text=<recipe text>, ?error=<message> (a failed share from the phone).
function handleIncomingParams() {
  const url = new URL(window.location);
  const importUrl = url.searchParams.get('import');
  const text = url.searchParams.get('text');
  const error = url.searchParams.get('error');
  ['import', 'text', 'error'].forEach((k) => url.searchParams.delete(k));
  window.history.replaceState({}, '', url);
  if (error) showUploadError(error);
  if (importUrl && /^https?:\/\//i.test(importUrl)) {
    startImportRequest('/api/import-url', { url: importUrl }, t('urlImportLoading'));
  } else if (text && text.trim()) {
    startImportRequest('/api/import-text', { text }, t('textImportLoading'));
  }
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

(async function init() {
  await initI18n();
  if (isMealie()) {
    // Maintain shows the tiles that also work with Mealie
    // (APP_CONFIG.health_metrics); planning uses Mealie's meal plan.
    document.querySelectorAll('.tandoor-only').forEach((elm) => elm.classList.add('hidden'));
    document.querySelectorAll('[data-i18n="toolNewRecipesDesc"]').forEach((elm) => {
      elm.setAttribute('data-i18n', 'toolNewRecipesDescMealie');
      elm.textContent = t('toolNewRecipesDescMealie');
    });
    document.querySelectorAll('[data-i18n="cookedHint"]').forEach((elm) => {
      elm.setAttribute('data-i18n', 'cookedHintMealie');
      elm.textContent = t('cookedHintMealie');
    });
  }
  const version = `Recipe Bridge ${APP_CONFIG.version || 'dev'}`;
  el('brand-link').title = version;
  el('app-version').textContent = version;
  initEnhancePhotos();
  initVoice();
  initMigration();
  await tryRestoreJobFromUrl();
  showArea('import');
  if (!state.jobId) handleIncomingParams();
  try { el('smart-depth').value = localStorage.getItem('th.scanDepth') || '2'; } catch (e) { /* default */ }
  updateSmartForm();
  // Installing (and so the share menu) needs HTTPS or localhost.
  el('more-way-insecure').classList.toggle('hidden', window.isSecureContext);
  el('logout-link').classList.toggle('hidden', !APP_CONFIG.auth_enabled);
  el('more-way-folder').textContent = APP_CONFIG.watch_dir
    ? tf('moreWayFolderOn', { dir: APP_CONFIG.watch_dir }) : t('moreWayFolderOff');
  checkTandoor();
  setInterval(checkTandoor, 15000);
  updateInboxBadge();
  setInterval(updateInboxBadge, 20000);
})();
