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
  if (APP_CONFIG.tandoor_url && el('tandoor-badge').classList.contains('ok')) {
    window.open(APP_CONFIG.tandoor_url, '_blank', 'noopener');
  }
});

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
  if (exts.length > 0 && exts.every((e) => imageExts.includes(e))) return { ok: true };
  if (files.length > 1 && exts.some((e) => pdfExts.includes(e) || epubExts.includes(e))) {
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
  renderPhotos();
}

['photo-camera', 'photo-pick'].forEach((id) => el(id).addEventListener('change', (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';  // so the same photo can be taken/picked again
  if (files.length) addPhotos(files);
}));
el('photo-clear').addEventListener('click', clearPhotos);
el('photo-import').addEventListener('click', () => {
  const files = photoState.files.map((p) => p.file);
  if (!files.length) return;
  const single = el('photo-single').checked;
  clearPhotos();
  uploadFiles(files, { singleRecipe: single });
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
el('url-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = el('url-input').value.trim();
  if (!url) return;
  el('upload-error').classList.add('hidden');
  requestNotificationPermission();
  el('upload-screen').classList.add('hidden');
  el('processing-screen').classList.remove('hidden');
  el('progress-track').classList.add('hidden');
  el('usage-badge').classList.add('hidden');
  el('processing-text').textContent = t('urlImportLoading');
  try {
    const res = await fetch('/api/import-url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `${t('uploadFailedPrefix')} (${res.status})`);
    }
    const data = await res.json();
    state.jobId = data.job_id;
    setJobUrl(data.job_id);
    el('url-input').value = '';
    pollJob();
  } catch (err) {
    el('processing-screen').classList.add('hidden');
    el('upload-screen').classList.remove('hidden');
    showUploadError(err.message);
  }
});

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

function showReview() {
  el('processing-screen').classList.add('hidden');
  el('review-screen').classList.remove('hidden');
  el('action-bar').classList.remove('hidden');
  el('cookbook-name-input').value = state.job.cookbook_name || state.job.suggested_cookbook_name || '';
  updateUsageDisplay(state.job.token_usage);
  renderRecipeList();
  updateSelectionCount();
  if (state.job.recipes.length > 0) {
    selectRecipe(state.job.recipes[0].id);
  }
}

el('cookbook-name-input').addEventListener('blur', () => {
  const value = el('cookbook-name-input').value.trim();
  state.job.cookbook_name = value;
  fetch(`/api/jobs/${state.jobId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cookbook_name: value }),
  }).catch(() => {});
});

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
    const opensInTandoor = r.import_status === 'imported' && r.tandoor_recipe_id && APP_CONFIG.tandoor_url;
    if (opensInTandoor) item.title = t('tandoorOpenRecipeHint');
    item.innerHTML = `
      <input type="checkbox" ${r.selected ? 'checked' : ''} data-id="${r.id}" class="select-cb" />
      <div class="thumb" style="${r.selected_image_id ? `background-image:url('${imageUrl(r.selected_image_id)}')` : ''}"></div>
      <div class="meta">
        <div class="title">${escapeHtml(r.title)}</div>
        <div class="sub">${t('pageLabel')} ${r.source_page_start}${r.source_page_end !== r.source_page_start ? '–' + r.source_page_end : ''}</div>
        ${duplicateBadge(r)}
        ${statusPill(r)}
      </div>
    `;
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('select-cb')) return;
      if (opensInTandoor) {
        window.open(`${APP_CONFIG.tandoor_url}/view/recipe/${r.tandoor_recipe_id}`, 'tandoorRecipePopup', 'width=900,height=850,noopener');
        return;
      }
      selectRecipe(r.id);
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

function renderDetail(r) {
  const detail = el('recipe-detail');

  const ingredientsHtml = r.ingredients.map((ing, i) => {
    const stepOptions = r.steps.map((s, si) => `<option value="${si}" ${((ing.step_index ?? 0) === si) ? 'selected' : ''}>${si + 1}</option>`).join('');
    return `
    <div class="ingredient-row" data-idx="${i}" draggable="true">
      <span class="ing-drag-handle" title="${t('dragToReorder')}">⠿</span>
      <input class="ing-amount" value="${ing.amount ?? ''}" placeholder="${t('placeholderAmount')}" />
      <input class="ing-unit" value="${escapeHtml(ing.unit ?? '')}" placeholder="${t('placeholderUnit')}" />
      <div class="ing-name-wrap">
        <input class="ing-name" value="${escapeHtml(ing.name)}" placeholder="${t('placeholderIngredient')}" />
        ${matchBadgeHtml(ing)}
      </div>
      <select class="ing-step" title="${t('fieldStepAssignment')}">${stepOptions || '<option value="0">1</option>'}</select>
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

  const tagsHtml = r.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join('') || `<span style="color:#8f9689;font-size:0.85rem;">${t('noTags')}</span>`;

  const generateTileHtml = APP_CONFIG.image_gen_available
    ? `<div class="image-choice generate-tile" data-generate="1" title="${t('generateImageBtn')}">
         <span class="generate-tile-icon">✨</span>
       </div>`
    : '';

  const imageChoicesHtml = r.candidate_image_ids.map((iid) => `
    <div class="image-choice ${iid === r.selected_image_id ? 'selected' : ''}" data-image-id="${iid}"
         style="background-image:url('${imageUrl(iid)}')"></div>
  `).join('') + `<div class="image-choice none ${!r.selected_image_id ? 'selected' : ''}" data-image-id="">${t('noImage')}</div>` + generateTileHtml;

  const duplicateNotice = r.duplicate_match
    ? `<div class="error-banner duplicate-banner" style="margin-bottom:20px;">${escapeHtml(
        tf(r.duplicate_exact ? 'duplicateExactWarning' : 'duplicateSimilarWarning', { name: r.duplicate_match })
      )}</div>`
    : '';

  const tandoorLink = (APP_CONFIG.tandoor_url && r.tandoor_recipe_id)
    ? `<a class="btn secondary" href="${APP_CONFIG.tandoor_url}/view/recipe/${r.tandoor_recipe_id}" target="_blank" rel="noopener">${t('openInTandoorBtn')}</a>`
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
      return {
        amount: numOrNull(row.querySelector('.ing-amount').value),
        unit: row.querySelector('.ing-unit').value || null,
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

  // Undo a match: back to the extracted name, which becomes a new ingredient.
  detail.querySelectorAll('.ing-match.matched').forEach((badge) => {
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

function numOrNull(v) {
  if (v === '' || v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
}

let patchTimer = null;
function patchRecipe(id, partial) {
  clearTimeout(patchTimer);
  patchTimer = setTimeout(() => {
    fetch(`/api/jobs/${state.jobId}/recipes/${id}`, {
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

  try {
    const res = await fetch(`/api/jobs/${state.jobId}/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ recipe_ids: recipeIds, cookbook_name: cookbookName }),
    });
    const data = await res.json();
    data.results.forEach((result) => {
      const r = findRecipe(result.id);
      if (!r) return;
      r.import_status = result.status;
      r.import_error = result.error;
      r.tandoor_recipe_id = result.tandoor_recipe_id;
    });
    showImportSummary(data.results, data.cookbook_name, data.cookbook_warning, data.post_processing_job_id);
  } catch (e) {
    alert(`${t('importFailedAlertPrefix')} ${e.message}`);
  } finally {
    importBtn.disabled = false;
    retryBtn.disabled = false;
    importBtn.textContent = originalImportLabel;
    renderRecipeList();
    if (state.activeRecipeId) renderDetail(findRecipe(state.activeRecipeId));
  }
}

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

  el('success-modal').classList.remove('hidden');
}

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
  if (state.jobId) {
    if (!confirm(t('confirmGoHome'))) return;
  }
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
  if (area === 'plan') openPlanArea();
  if (area === 'maintain') {
    loadNewRecipesStatus();
    loadUsage();
    loadHealth();
    loadSettings();
  }
  window.scrollTo(0, 0);
}

document.querySelectorAll('.nav-btn').forEach((b) => b.addEventListener('click', () => showArea(b.dataset.area)));

const TOOL_TITLE_KEYS = {
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
  { key: 'plan', icon: '📅', match: (i) => i.kind === 'meal_plan' },
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
    if ((data.running.length || data.queued) && currentArea === 'inbox') inboxState.timer = setTimeout(loadInbox, data.queued ? 2000 : 4000);
  } catch (e) {
    el('inbox-groups').innerHTML = `<div class="error-banner">${escapeHtml(e.message)}</div>`;
  }
}

function renderInbox() {
  const groups = INBOX_GROUPS.map((g) => ({ ...g, items: [] }));
  inboxState.items.forEach((i) => groups.find((g) => g.match(i)).items.push(i));
  const visible = groups.filter((g) => g.items.length);
  if (!visible.length) {
    el('inbox-groups').innerHTML = `<p class="inbox-empty">${t('inboxEmpty')}</p>`;
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
  const line = el('usage-budget');
  line.classList.toggle('hidden', !budget.warn);
  line.classList.toggle('over', budget.exceeded);
  line.textContent = budget.exceeded ? t('budgetExceeded') : budget.warn ? tf('budgetWarn', { pct }) : '';
}

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
    el('set-maint-metrics').innerHTML = data.metrics.map((k) => `
      <label><input type="checkbox" value="${k}" ${m.metrics.includes(k) ? 'checked' : ''}> ${escapeHtml(t('health_' + k))}</label>`).join('');
    el('set-budget-limit').value = data.budget.monthly_tokens;
    el('set-budget-block').checked = data.budget.block_manual;
    renderBudget(data.budget_status);
    renderMaintStatus(data.maintenance_status);
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

el('set-maint-run').addEventListener('click', async () => {
  el('set-maint-run').disabled = true;
  await fetch('/api/maintenance/run', { method: 'POST' });
  loadSettings();
  loadHealth();
});

// metric -> the tool that fixes it (endpoint + optional request body)
const HEALTH_METRICS = [
  { key: 'foods_duplicates', tool: 'ingredients_review', endpoint: '/api/tools/ingredients/review', body: { focus: 'duplicates' } },
  { key: 'foods_without_nutrition', tool: 'ingredients_enrich', endpoint: '/api/tools/ingredients/enrich' },
  { key: 'foods_without_category', tool: 'ingredients_enrich', endpoint: '/api/tools/ingredients/enrich' },
  { key: 'missing_conversions', tool: 'conversions', endpoint: '/api/tools/conversions' },
  { key: 'units_duplicates', tool: 'units_review', endpoint: '/api/tools/units/review', body: { focus: 'duplicates' } },
  { key: 'recipes_not_translated', tool: 'recipes_translate', endpoint: '/api/tools/recipes/translate' },
  { key: 'recipes_need_restructure', tool: 'recipes_restructure', endpoint: '/api/tools/recipes/restructure' },
  { key: 'recipes_without_season', tool: 'tags_season', endpoint: '/api/tools/tags/season' },
  { key: 'recipes_few_tags', tool: 'tags_suggest_more', endpoint: '/api/tools/tags/suggest-more' },
];

const healthState = { open: null, data: null };

function renderHealth(data) {
  healthState.data = data;
  const running = data.running;
  el('health-refresh-btn').disabled = running;
  el('health-when').textContent = running ? t('healthRunning')
    : data.error ? `${t('toolStatusError')}: ${data.error}`
    : data.computed_at ? tf('healthComputedAt', { when: shortWhen(data.computed_at) }) : t('healthNever');
  if (!data.computed_at) { el('health-grid').innerHTML = ''; closeHealthDetail(); return; }
  el('health-grid').innerHTML = HEALTH_METRICS.map((m) => {
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
      : value ? `<button class="btn secondary health-fix" type="button" data-metric="${m.key}">${t('healthFix')}</button>` : '';
    return `<div class="health-tile ${value ? 'todo' : 'ok'} ${healthState.open === m.key ? 'open' : ''} ${running ? 'refreshing' : ''}">
      <div class="health-value">${value ? value.toLocaleString() : '✓'}</div>
      <div class="health-label">${t('health_' + m.key)}</div>
      ${ignoredCount ? `<div class="health-ignored-count">${tf('healthIgnoredCount', { n: ignoredCount })}</div>` : ''}
      ${pendingHtml}
      <div class="health-tile-actions">
        ${fixBtn}
        ${value || ignoredCount ? `<button class="btn secondary health-entries" type="button" data-metric="${m.key}">${t('healthEntries')}</button>` : ''}
      </div>
    </div>`;
  }).join('');
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
  el('health-grid').querySelectorAll('.health-tile.open').forEach((tile) => tile.classList.remove('open'));
}

function healthRow(item) {
  const link = item.recipe_id && APP_CONFIG.tandoor_url
    ? ` <a href="${APP_CONFIG.tandoor_url}/view/recipe/${item.recipe_id}" target="_blank" rel="noopener" title="${escapeHtml(t('openInTandoorBtn'))}">↗</a>`
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

function nextMonday() {
  const d = new Date();
  d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
  return d.toISOString().slice(0, 10);
}

// ---------- What can I cook today? ----------

async function searchCookToday() {
  const have = el('ct-have').value.trim();
  if (!have) return;
  el('ct-status').textContent = t('cookTodaySearching');
  try {
    const params = new URLSearchParams({ have, staples: el('ct-staples').checked });
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
      const link = APP_CONFIG.tandoor_url ? `${APP_CONFIG.tandoor_url}/view/recipe/${r.id}` : null;
      const name = link ? `<a href="${link}" target="_blank" rel="noopener">${escapeHtml(r.name)}</a>` : escapeHtml(r.name);
      const meta = [
        `<span class="ct-have">✓ ${escapeHtml(r.matched.join(', '))}</span>`,
        r.missing.length ? `${t('cookTodayMissing')}: ${escapeHtml(r.missing.slice(0, 6).join(', '))}${r.missing.length > 6 ? ' …' : ''}` : t('cookTodayComplete'),
        r.minutes ? `${r.minutes} min` : '',
        r.rating ? '★'.repeat(Math.round(r.rating)) : '',
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
          recipe: { id: Number(b.dataset.id), name: b.dataset.name },
          meal_type: { id: Number(meal.value), name: meal.options[meal.selectedIndex].text },
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

async function loadMealPlanOptions() {
  if (!el('mp-start').value) el('mp-start').value = nextMonday();
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
const planState = { jobId: null, job: null, timer: null, selected: new Set(), busy: false };

function rememberPlan(jobId) {
  try { if (jobId) localStorage.setItem('th.planJob', jobId); else localStorage.removeItem('th.planJob'); } catch (e) { /* optional */ }
}

async function openPlanArea() {
  loadMealPlanOptions();
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
        meal_type: { id: Number(select.value), name: select.options[select.selectedIndex]?.textContent || '' },
        wishes: el('mp-wishes').value,
        add_to_shopping: el('mp-shopping').checked,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    planState.jobId = data.job_id;
    planState.selected.clear();
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
    return `<div class="plan-day ${planState.selected.has(s.id) ? 'selected' : ''}" data-id="${s.id}">
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
}

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

async function loadNewRecipesStatus() {
  const label = el('new-recipes-status');
  const btn = el('new-recipes-start-btn');
  btn.disabled = true;
  label.textContent = t('toolNewRecipesChecking');
  try {
    const res = await fetch('/api/tools/new-recipes/status');
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    if (data.baseline_created) {
      label.textContent = tf('toolNewRecipesBaseline', { count: data.baseline_count });
    } else {
      label.textContent = data.new_count > 0 ? tf('toolNewRecipesCount', { count: data.new_count }) : t('toolNewRecipesNone');
    }
    btn.disabled = data.new_count === 0;

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

el('tools-back-btn').addEventListener('click', () => {
  clearTimeout(toolsState.pollTimer);
  toolsState.jobId = null;
  toolsState.job = null;
  toolsState.selected.clear();
  el('tools-run-view').classList.add('hidden');
  el('tools-cards-view').classList.remove('hidden');
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
function renderToolSuggestions(job) {
  toolsState.job = job;
  const list = el('tools-suggestions-list');
  const pendingIds = new Set(job.suggestions.filter((s) => s.status === 'pending').map((s) => s.id));
  // Drop selections that are no longer pending (applied/skipped/failed).
  toolsState.selected.forEach((id) => { if (!pendingIds.has(id)) toolsState.selected.delete(id); });

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

(async function init() {
  await initI18n();
  await tryRestoreJobFromUrl();
  showArea('import');
  checkTandoor();
  setInterval(checkTandoor, 15000);
  updateInboxBadge();
  setInterval(updateInboxBadge, 20000);
})();
