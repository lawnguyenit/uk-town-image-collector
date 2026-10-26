// UK Town Image Collector Dashboard - Client Application
let state = {
  status: 'idle',
  progress: {},
  logs: [],
  records: [],
  summary: {},
  available_runs: [],
  current_run_id: '',
  filter: 'all',
  searchQuery: '',
  currentPage: 1,
  pageSize: 6,
};

let pollInterval = null;

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  setupEventListeners();
  loadState();
  // Poll periodically if busy, otherwise every 5s
  setInterval(() => {
    if (state.status === 'running') {
      loadState();
    }
  }, 1000);
});

function setupEventListeners() {
  // Search bar
  const searchInput = document.getElementById('search-input');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      state.searchQuery = e.target.value.toLowerCase().trim();
      state.currentPage = 1;
      renderCards();
    });
  }

  // Filter tabs
  document.querySelectorAll('.filter-tab').forEach((tab) => {
    tab.addEventListener('click', (e) => {
      document.querySelectorAll('.filter-tab').forEach((t) => t.classList.remove('active'));
      e.target.classList.add('active');
      state.filter = e.target.dataset.filter || 'all';
      state.currentPage = 1;
      renderCards();
    });
  });

  // Run Dropdown
  const runSelect = document.getElementById('run-select');
  if (runSelect) {
    runSelect.addEventListener('change', (e) => {
      if (e.target.value) {
        loadSpecificRun(e.target.value);
      }
    });
  }

  // Modals
  const lightboxModal = document.getElementById('lightbox-modal');
  if (lightboxModal) {
    lightboxModal.addEventListener('click', (e) => {
      if (e.target === lightboxModal || e.target.classList.contains('modal-close')) {
        closeLightbox();
      }
    });
  }

  const runModal = document.getElementById('run-modal');
  if (runModal) {
    runModal.addEventListener('click', (e) => {
      if (e.target === runModal || e.target.classList.contains('modal-close')) {
        closeRunModal();
      }
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeLightbox();
      closeRunModal();
    }
  });
}

// ---------------- API Calls & State ----------------

async function loadState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error('Failed to load state');
    const data = await res.json();
    
    state.status = data.status || 'idle';
    state.progress = data.progress || {};
    state.logs = data.logs || [];
    state.records = data.records || [];
    state.summary = data.summary || {};
    state.available_runs = data.available_runs || [];
    state.current_run_id = data.current_run_id || '';

    updateNavbar();
    updateMetrics();
    updateProgressPanel();
    updateRunSelect();
    renderCards();
  } catch (err) {
    console.error('Error loading state:', err);
  }
}

async function loadSpecificRun(runId) {
  try {
    showToast('Loading run ' + runId + '...', 'info');
    const res = await fetch('/api/load_run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId })
    });
    if (!res.ok) throw new Error('Failed to load run');
    await loadState();
    showToast('Loaded run ' + runId, 'success');
  } catch (err) {
    showToast('Error loading run: ' + err.message, 'error');
  }
}

async function triggerStartRun() {
  const inputPath = document.getElementById('config-input-file')?.value || 'input.csv';
  const contactUrl = document.getElementById('config-contact-url')?.value || 'https://github.com/lawnguyenit';
  const enableApi = document.getElementById('config-enable-api')?.checked ?? true;
  const refresh = document.getElementById('config-refresh')?.checked ?? false;
  const allowInvalid = document.getElementById('config-allow-invalid')?.checked ?? true;

  try {
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_file: inputPath,
        contact_url: contactUrl,
        enable_api: enableApi,
        refresh: refresh,
        allow_invalid_rows: allowInvalid
      })
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.error || 'Failed to start run');
    closeRunModal();
    showToast('Collection batch started!', 'success');
    loadState();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function setReviewStatus(inputId, fileTitle, reviewedStatus, note = null, licenseChoice = null) {
  try {
    const payload = {
      input_id: inputId,
      file_title: fileTitle,
      reviewed: reviewedStatus,
    };
    if (note !== null) payload.reviewer_note = note;
    if (licenseChoice !== null) payload.reviewer_license_choice = licenseChoice;

    const res = await fetch('/api/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Review update failed');

    // Update in-memory record
    const target = state.records.find((r) => r.input_id === inputId);
    if (target) {
      target.reviewed = reviewedStatus;
      if (note !== null) target.reviewer_note = note;
      if (licenseChoice !== null) target.reviewer_license_choice = licenseChoice;
      if (data.record) Object.assign(target, data.record);
    }
    if (data.summary) state.summary = data.summary;

    updateMetrics();
    renderCards();
    showToast(`${inputId} marked as ${reviewedStatus}`, 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function bulkApproveAll() {
  if (!confirm('Approve all valid candidate rows and immediately update ready_for_import.csv?')) {
    return;
  }
  try {
    showToast('Approving candidates...', 'info');
    const res = await fetch('/api/bulk_approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reviewer: 'Reviewer (UI)' })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Bulk approve failed');
    await loadState();
    showToast(`Successfully approved ${data.approved_count} candidates!`, 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function openOutputFolder() {
  try {
    const res = await fetch('/api/open_folder', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not open folder');
    showToast('Output folder opened in Explorer', 'info');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function exportFile(kind) {
  window.open(`/api/export/${kind}`, '_blank');
}

// ---------------- DOM Updating Functions ----------------

function updateNavbar() {
  const dot = document.getElementById('nav-status-dot');
  const label = document.getElementById('nav-status-label');
  if (!dot || !label) return;

  dot.className = 'status-dot';
  if (state.status === 'running') {
    dot.classList.add('running');
    label.textContent = 'Collecting Images...';
  } else if (state.status === 'error') {
    dot.classList.add('error');
    label.textContent = 'Error Encountered';
  } else if (state.status === 'completed') {
    dot.classList.add('completed');
    label.textContent = 'Batch Completed';
  } else {
    label.textContent = 'Ready';
  }
}

function updateMetrics() {
  const total = state.records.length;
  const candidates = state.records.filter((r) => r.status === 'CANDIDATE').length;
  const ready = state.records.filter((r) => r.status === 'CANDIDATE' && r.reviewed === 'APPROVED').length;
  const pending = state.records.filter((r) => r.status === 'CANDIDATE' && (!r.reviewed || r.reviewed === 'PENDING')).length;

  setText('metric-total-value', total);
  setText('metric-candidates-value', candidates);
  setText('metric-ready-value', ready);
  setText('metric-pending-value', pending);
}

function updateProgressPanel() {
  const panel = document.getElementById('progress-panel');
  if (!panel) return;

  if (state.status === 'running') {
    panel.classList.remove('hidden');
    const p = state.progress;
    const completed = p.completed || 0;
    const total = p.total || 20;
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

    setText('progress-title', `Processing: ${p.place_name || 'Town'} (${completed}/${total})`);
    setText('progress-percent', `${pct}%`);
    
    const fill = document.getElementById('progress-bar-fill');
    if (fill) {
      fill.style.width = `${pct}%`;
      fill.classList.add('striped');
    }

    renderLogs();
  } else if (state.logs && state.logs.length > 0 && panel.dataset.keepLogs === 'true') {
    panel.classList.remove('hidden');
    renderLogs();
  } else {
    panel.classList.add('hidden');
  }
}

function renderLogs() {
  const logStream = document.getElementById('log-stream');
  if (!logStream) return;
  logStream.innerHTML = state.logs
    .slice(-25)
    .map((l) => `<div class="log-line ${l.level}">[${l.time}] ${escapeHtml(l.msg)}</div>`)
    .join('');
  logStream.scrollTop = logStream.scrollHeight;
}

function updateRunSelect() {
  const select = document.getElementById('run-select');
  if (!select) return;
  
  if (state.available_runs.length === 0) {
    select.innerHTML = '<option value="">No runs found</option>';
    return;
  }

  select.innerHTML = state.available_runs
    .map((r) => `<option value="${r}" ${r === state.current_run_id ? 'selected' : ''}>Run: ${r}</option>`)
    .join('');
}

// ---------------- Cards Rendering ----------------

function renderCards() {
  const grid = document.getElementById('cards-grid');
  const pagContainer = document.getElementById('pagination-container');
  if (!grid) return;

  let filtered = state.records;

  // Filter tab
  if (state.filter === 'ready') {
    filtered = filtered.filter((r) => r.status === 'CANDIDATE' && r.reviewed === 'APPROVED');
  } else if (state.filter === 'approved') {
    filtered = filtered.filter((r) => r.reviewed === 'APPROVED');
  } else if (state.filter === 'pending') {
    filtered = filtered.filter((r) => !r.reviewed || r.reviewed === 'PENDING');
  } else if (state.filter === 'needs_review') {
    filtered = filtered.filter((r) => r.status === 'NEEDS_REVIEW' || r.reviewed === 'REJECTED');
  }

  // Search query
  if (state.searchQuery) {
    const q = state.searchQuery;
    filtered = filtered.filter((r) => {
      return (
        (r.place_name && r.place_name.toLowerCase().includes(q)) ||
        (r.input_id && r.input_id.toLowerCase().includes(q)) ||
        (r.file_title && r.file_title.toLowerCase().includes(q)) ||
        (r.creator_text && r.creator_text.toLowerCase().includes(q)) ||
        (r.license_short_name && r.license_short_name.toLowerCase().includes(q))
      );
    });
  }

  if (filtered.length === 0) {
    grid.innerHTML = `
      <div style="grid-column: 1 / -1; padding: 3rem; text-align: center; color: var(--text-muted); background: white; border-radius: var(--radius-md); border: 1px dashed var(--border-color);">
        <h3>No matching towns found</h3>
        <p style="margin-top: 0.5rem; font-size: 0.85rem;">Try adjusting your search query or switching filter tabs.</p>
      </div>
    `;
    if (pagContainer) pagContainer.style.display = 'none';
    return;
  }

  // Pagination calculation
  const totalItems = filtered.length;
  const pageSize = state.pageSize === 'all' ? totalItems : parseInt(state.pageSize, 10);
  const totalPages = Math.ceil(totalItems / pageSize) || 1;

  if (state.currentPage > totalPages) state.currentPage = totalPages;
  if (state.currentPage < 1) state.currentPage = 1;

  const startIndex = (state.currentPage - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize, totalItems);
  const pageRecords = filtered.slice(startIndex, endIndex);

  grid.innerHTML = pageRecords.map((r) => createCardHtml(r)).join('');

  if (pagContainer) {
    pagContainer.style.display = 'flex';
    renderPagination(pagContainer, totalItems, startIndex, endIndex, totalPages);
  }
}

function renderPagination(container, totalItems, startIndex, endIndex, totalPages) {
  let pageButtonsHtml = '';
  for (let i = 1; i <= totalPages; i++) {
    pageButtonsHtml += `
      <button class="page-btn ${i === state.currentPage ? 'active' : ''}" onclick="changePage(${i})">
        ${i}
      </button>
    `;
  }

  container.innerHTML = `
    <div class="pagination-info">
      Showing <strong>${startIndex + 1}–${endIndex}</strong> of <strong>${totalItems}</strong> towns
    </div>

    <div class="pagination-controls">
      <button class="page-btn" ${state.currentPage === 1 ? 'disabled' : ''} onclick="changePage(${state.currentPage - 1})">
        ◀ Prev
      </button>
      ${pageButtonsHtml}
      <button class="page-btn" ${state.currentPage === totalPages ? 'disabled' : ''} onclick="changePage(${state.currentPage + 1})">
        Next ▶
      </button>
    </div>

    <div class="page-size-selector">
      <span>Per page:</span>
      <select class="page-size-select" onchange="changePageSize(this.value)">
        <option value="6" ${state.pageSize === 6 ? 'selected' : ''}>6 towns</option>
        <option value="9" ${state.pageSize === 9 ? 'selected' : ''}>9 towns</option>
        <option value="12" ${state.pageSize === 12 ? 'selected' : ''}>12 towns</option>
        <option value="all" ${state.pageSize === 'all' ? 'selected' : ''}>All</option>
      </select>
    </div>
  `;
}

function changePage(newPage) {
  state.currentPage = newPage;
  renderCards();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function changePageSize(newSize) {
  state.pageSize = newSize === 'all' ? 'all' : parseInt(newSize, 10);
  state.currentPage = 1;
  renderCards();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function createCardHtml(r) {
  const status = r.status || 'UNKNOWN';
  const reviewed = r.reviewed || 'PENDING';
  
  let cardClass = 'card';
  if (reviewed === 'APPROVED') cardClass += ' approved';
  else if (reviewed === 'REJECTED') cardClass += ' rejected';
  else cardClass += ' pending';

  let statusBadgeClass = 'badge-candidate';
  if (status === 'NEEDS_REVIEW') statusBadgeClass = 'badge-needs-review';
  else if (status === 'NO_IMAGE') statusBadgeClass = 'badge-no-image';
  else if (status.includes('ERROR') || status === 'BLOCKED') statusBadgeClass = 'badge-error';

  const hasImage = !!r.thumbnail_url;
  const imageHtml = hasImage
    ? `<img class="card-img" src="${r.thumbnail_url}" alt="${escapeHtml(r.file_title || r.place_name)}" loading="lazy" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=\\'card-no-image-placeholder\\'>⚠️ Image load error</div>'">
       <div class="card-image-overlay">🔍 Click to inspect & zoom</div>`
    : `<div class="card-no-image-placeholder">
         <span style="font-size: 2rem;">🏛️</span>
         <span>No image candidate found</span>
       </div>`;

  const safeAttribution = escapeHtml(r.attribution_text || 'No attribution generated');
  const creatorHtml = r.creator_url
    ? `<a href="${r.creator_url}" target="_blank" rel="noopener">${escapeHtml(r.creator_text || 'Unknown')}</a>`
    : escapeHtml(r.creator_text || 'Unknown');

  const fileLink = r.commons_file_page_url
    ? `<a href="${r.commons_file_page_url}" target="_blank" rel="noopener">${escapeHtml(r.file_title || 'File')}</a>`
    : escapeHtml(r.file_title || '—');

  const articleLink = r.canonical_page_url || r.wikipedia_url || '#';

  // License Choices if multi-license
  let licenseSelectHtml = '';
  if (r.license_evidence) {
    try {
      const choices = JSON.parse(r.license_evidence);
      if (Array.isArray(choices) && choices.length > 1) {
        licenseSelectHtml = `
          <div class="detail-row" style="margin-top: 0.25rem;">
            <span class="detail-label">Choice:</span>
            <select class="license-choice-select" onchange="onLicenseChange('${r.input_id}', '${escapeJs(r.file_title)}', this.value)" style="font-size: 0.72rem; padding: 0.15rem 0.35rem; border-radius: 4px; border: 1px solid var(--border-color);">
              ${choices.map((c) => `<option value="${escapeHtml(c.name)}" ${(r.reviewer_license_choice || r.license_short_name) === c.name ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('')}
            </select>
          </div>
        `;
      }
    } catch (_) {}
  }

  return `
    <div class="${cardClass}" id="card-${r.input_id}">
      <div class="card-header">
        <div class="card-title-group">
          <span class="input-id-pill">${r.input_id}</span>
          <a class="card-place-name" href="${articleLink}" target="_blank" rel="noopener">${escapeHtml(r.place_name)}</a>
        </div>
        <span class="badge ${statusBadgeClass}">${status}</span>
      </div>

      <div class="card-image-box" onclick="openLightboxByInputId('${r.input_id}')">
        ${imageHtml}
      </div>

      <div class="card-body">
        <div class="detail-row">
          <span class="detail-label">File:</span>
          <span class="detail-value" title="${escapeHtml(r.file_title || '')}">${fileLink}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">Creator:</span>
          <span class="detail-value" title="${escapeHtml(r.creator_text || '')}">${creatorHtml}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">License:</span>
          <span class="detail-value">
            <a class="license-pill" href="${r.license_url || '#'}" target="_blank" rel="noopener">${escapeHtml(r.license_short_name || 'Unspecified')}</a>
          </span>
        </div>
        ${licenseSelectHtml}
        <div class="detail-row">
          <span class="detail-label">Dimensions:</span>
          <span class="detail-value">${r.width_px ? `${r.width_px} × ${r.height_px} px` : '—'}</span>
        </div>

        <div class="attribution-box">
          <span class="attribution-text" title="${safeAttribution}">${safeAttribution}</span>
          <button class="btn-copy" onclick="copyAttribution(this, '${escapeJs(r.attribution_text || '')}')">📋 Copy</button>
        </div>
      </div>

      <div class="card-review-footer">
        <div class="review-btn-row">
          <button class="btn btn-sm btn-review-approve ${reviewed === 'APPROVED' ? 'active' : ''}" onclick="setReviewStatus('${r.input_id}', '${escapeJs(r.file_title)}', 'APPROVED')">
            ✓ Approve
          </button>
          <button class="btn btn-sm btn-review-reject ${reviewed === 'REJECTED' ? 'active' : ''}" onclick="setReviewStatus('${r.input_id}', '${escapeJs(r.file_title)}', 'REJECTED')">
            ✗ Reject
          </button>
        </div>
        <input class="reviewer-note-input" type="text" placeholder="Reviewer note / remark..." value="${escapeHtml(r.reviewer_note || '')}" onblur="onNoteBlur('${r.input_id}', '${escapeJs(r.file_title)}', this.value)">
      </div>
    </div>
  `;
}

// ---------------- Lightbox Modal ----------------

function openLightboxByInputId(inputId) {
  const r = state.records.find((item) => item.input_id === inputId);
  if (!r) return;
  openLightbox(r);
}

function openLightbox(r) {
  const modal = document.getElementById('lightbox-modal');
  if (!modal) return;

  setText('modal-place-title', `${r.place_name} (${r.input_id})`);
  
  const img = document.getElementById('modal-img');
  if (img) {
    img.src = r.original_file_url || r.thumbnail_url || '';
    img.alt = r.file_title || r.place_name;
  }

  const metaTbody = document.getElementById('modal-meta-tbody');
  if (metaTbody) {
    metaTbody.innerHTML = `
      <tr><th>Input ID</th><td>${escapeHtml(r.input_id)}</td></tr>
      <tr><th>Town / Place</th><td>${escapeHtml(r.place_name)}</td></tr>
      <tr><th>Wikipedia Article</th><td><a href="${r.canonical_page_url || r.wikipedia_url}" target="_blank">${escapeHtml(r.canonical_page_url || r.wikipedia_url)}</a></td></tr>
      <tr><th>File Title</th><td>${escapeHtml(r.file_title || '—')}</td></tr>
      <tr><th>Commons Page</th><td><a href="${r.commons_file_page_url}" target="_blank">${escapeHtml(r.commons_file_page_url || '—')}</a></td></tr>
      <tr><th>Direct Image File</th><td><a href="${r.original_file_url}" target="_blank">${escapeHtml(r.original_file_url || '—')}</a></td></tr>
      <tr><th>Dimensions</th><td>${r.width_px || '—'} × ${r.height_px || '—'} px (${r.mime_type || '—'})</td></tr>
      <tr><th>Photographer / Creator</th><td>${escapeHtml(r.creator_text || '—')} ${r.creator_url ? `(<a href="${r.creator_url}" target="_blank">profile</a>)` : ''}</td></tr>
      <tr><th>License</th><td><strong>${escapeHtml(r.license_short_name || '—')}</strong> (<a href="${r.license_url}" target="_blank">${escapeHtml(r.license_url || '')}</a>)</td></tr>
      <tr><th>Selection Method</th><td><code>${escapeHtml(r.selection_method || '—')}</code></td></tr>
      <tr><th>Reason Code</th><td><code>${escapeHtml(r.reason_code || '—')}</code></td></tr>
      <tr><th>Attribution Text</th><td><code>${escapeHtml(r.attribution_text || '—')}</code></td></tr>
      <tr><th>Review Status</th><td><strong>${escapeHtml(r.reviewed || 'PENDING')}</strong> (Note: ${escapeHtml(r.reviewer_note || 'None')})</td></tr>
      <tr><th>Evidence File</th><td><code>${escapeHtml(r.evidence_path || '—')}</code></td></tr>
    `;
  }

  modal.classList.add('show');
}

function closeLightbox() {
  const modal = document.getElementById('lightbox-modal');
  if (modal) modal.classList.remove('show');
}

// ---------------- Run Configuration Modal ----------------

function openRunModal() {
  const modal = document.getElementById('run-modal');
  if (modal) modal.classList.add('show');
}

function closeRunModal() {
  const modal = document.getElementById('run-modal');
  if (modal) modal.classList.remove('show');
}

// ---------------- Handlers & Utilities ----------------

function onNoteBlur(inputId, fileTitle, note) {
  const r = state.records.find((item) => item.input_id === inputId);
  if (r && r.reviewer_note !== note) {
    setReviewStatus(inputId, fileTitle, r.reviewed || 'PENDING', note);
  }
}

function onLicenseChange(inputId, fileTitle, licenseChoice) {
  const r = state.records.find((item) => item.input_id === inputId);
  setReviewStatus(inputId, fileTitle, r ? r.reviewed : 'PENDING', null, licenseChoice);
}

function copyAttribution(btn, text) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(
    () => {
      btn.textContent = '✓ Copied!';
      btn.classList.add('copied');
      showToast('Attribution copied to clipboard!', 'success');
      setTimeout(() => {
        btn.textContent = '📋 Copy';
        btn.classList.remove('copied');
      }, 2000);
    },
    () => {
      showToast('Could not copy to clipboard', 'error');
    }
  );
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function escapeJs(str) {
  if (!str) return '';
  return String(str)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '\\"');
}
