/* ============================================================
   WebToApp — Frontend Logic
   Zero dependencies. Every function earns its place.
   ============================================================ */

(function () {
  'use strict';

  // --- Scroll Reveal ---
  const revealObserver = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('revealed'); revealObserver.unobserve(e.target); } });
  }, { threshold: 0.15 });
  document.querySelectorAll('[data-reveal]').forEach(el => revealObserver.observe(el));

  // --- Counter Animation ---
  function animateCount(el, target, duration = 1800) {
    const start = performance.now();
    const fmt = (n) => n.toLocaleString('en-US');
    const tick = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = fmt(Math.floor(target * eased));
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  const animatedCounters = new WeakSet();
  const counterObserver = new IntersectionObserver((entries) => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        const target = parseInt(e.target.dataset.count);
        if (!Number.isNaN(target) && !animatedCounters.has(e.target)) {
          animateCount(e.target, target);
          animatedCounters.add(e.target);
        }
        counterObserver.unobserve(e.target);
      }
    });
  }, { threshold: 0.5 });
  document.querySelectorAll('[data-count]').forEach(el => counterObserver.observe(el));

  async function loadHomepageStats() {
    try {
      const res = await fetch('/api/stats');
      if (!res.ok) throw new Error('Failed to load stats');
      const stats = await res.json();
      const mappings = [
        ['stat-generated-apps', stats.generatedApps],
        ['stat-downloads', stats.downloads],
        ['stat-views', stats.views],
      ];
      mappings.forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (!el || Number.isNaN(Number(value))) return;
        el.dataset.count = String(value);
        el.textContent = '0';
        animatedCounters.delete(el);
        animateCount(el, Number(value));
        animatedCounters.add(el);
      });
    } catch (_err) {
      // Leave zero values in place rather than showing made-up numbers.
    }
  }
  loadHomepageStats();

  // --- DOM refs ---
  const urlInput = document.getElementById('url-input');
  const distillBtn = document.getElementById('distill-btn');
  const workspace = document.getElementById('workspace');
  const analysisBody = document.getElementById('analysis-body');
  const analysisStatus = document.getElementById('analysis-status');
  const appNameInput = document.getElementById('app-name');
  const appNameSourceNote = document.getElementById('app-name-source-note');
  const appColorInput = document.getElementById('app-color');
  const customIconInput = document.getElementById('custom-icon-input');
  const customIconFileName = document.getElementById('custom-icon-file-name');
  const customIconPreview = document.getElementById('custom-icon-preview');
  const customIconPlaceholder = document.getElementById('custom-icon-placeholder');
  const customIconClearBtn = document.getElementById('custom-icon-clear');
  const androidVersionNameInput = document.getElementById('android-version-name');
  const androidVersionCodeInput = document.getElementById('android-version-code');
  const androidPackagePrefixInput = document.getElementById('android-package-prefix');
  const immersiveFullscreenInput = document.getElementById('feature-immersive-fullscreen');
  const desktopModeInput = document.getElementById('feature-desktop-mode');
  const windowsWidthInput = document.getElementById('windows-window-width');
  const windowsHeightInput = document.getElementById('windows-window-height');
  const windowsMaximizedInput = document.getElementById('windows-window-maximized');
  const linuxWidthInput = document.getElementById('linux-window-width');
  const linuxHeightInput = document.getElementById('linux-window-height');
  const linuxMaximizedInput = document.getElementById('linux-window-maximized');
  const colorHex = document.getElementById('color-hex');
  const generateBtn = document.getElementById('generate-btn');
  const resultPanel = document.getElementById('result-panel');
  const appLink = document.getElementById('app-link');
  const copyBtn = document.getElementById('copy-btn');
  const previewFrame = document.getElementById('preview-frame');
  const previewUrl = document.getElementById('preview-url');
  const previewOpenBtn = document.getElementById('preview-open-btn');
  const historyList = document.getElementById('history-list');
  const historyEmpty = document.getElementById('history-empty');
  const historyRecoverBtn = document.getElementById('history-recover-btn');
  const historyExportBtn = document.getElementById('history-export-btn');
  const historyImportBtn = document.getElementById('history-import-btn');
  const historyImportInput = document.getElementById('history-import-input');
  const historyManageBtn = document.getElementById('history-manage-btn');
  const historySelectBar = document.getElementById('history-select-bar');
  const historySelectAll = document.getElementById('history-select-all');
  const historySelectCount = document.getElementById('history-select-count');
  const historyDeleteSelectedBtn = document.getElementById('history-delete-selected-btn');
  const historyCancelSelectBtn = document.getElementById('history-cancel-select-btn');
  const modeUrlBtn = document.getElementById('mode-url-btn');
  const modeHtmlBtn = document.getElementById('mode-html-btn');
  const urlInputWrap = document.getElementById('url-input-wrap');
  const htmlInputWrap = document.getElementById('html-input-wrap');
  const htmlFileInput = document.getElementById('html-file-input');
  const htmlDropzone = document.getElementById('html-dropzone');
  const htmlFileLabel = document.getElementById('html-file-label');
  const htmlFileHint = document.getElementById('html-file-hint');
  let currentUrl = '';
  let inputMode = 'url'; // 'url' | 'html'
  let htmlFile = null;
  let pendingAutoScrollTimer = null;
  let customIconDataUrl = '';
  let detectedIconDataUrl = '';
  let restoreIconDataUrl = '';
  let restoreIconLabel = '';
  let restoreIconFileName = '';
  let lastHistoryItems = [];
  let historySelectMode = false;
  const historySelected = new Set();
  let restoreIconButtonLabel = '';
  let deviceFingerprint = '';
  const DEVICE_STORAGE_KEY = 'webtoapp-device-fingerprint-v1';
  const DEVICE_COOKIE_KEY = 'webtoapp_device_fingerprint';
  const t = (key, params) => (window.I18n ? window.I18n.t(key, params) : key);
  const locale = () => (window.I18n ? window.I18n.locale() : 'en-US');
  restoreIconButtonLabel = t('icon.noRestore');

  function normalizeFeatureOptions(raw) {
    const options = raw && typeof raw === 'object' ? raw : {};
    const immersiveFullscreen = options['feature-immersive-fullscreen'] === true || options.feature_immersive_fullscreen === true;
    const desktopMode = options['feature-desktop-mode'] === true || options.feature_desktop_mode === true;
    const windowOpts = {};
    ['windows', 'linux'].forEach((prefix) => {
      windowOpts[`${prefix}-window-width`] = sanitizeWindowSize(
        options[`${prefix}-window-width`] != null ? options[`${prefix}-window-width`] : options[`${prefix}_window_width`]
      );
      windowOpts[`${prefix}-window-height`] = sanitizeWindowSize(
        options[`${prefix}-window-height`] != null ? options[`${prefix}-window-height`] : options[`${prefix}_window_height`]
      );
      windowOpts[`${prefix}-window-maximized`] =
        options[`${prefix}-window-maximized`] === true || options[`${prefix}_window_maximized`] === true;
    });
    return {
      immersiveFullscreen,
      desktopMode,
      ...windowOpts,
    };
  }

  function applyFeatureOptionsToForm(raw) {
    const options = normalizeFeatureOptions(raw);
    immersiveFullscreenInput.checked = options.immersiveFullscreen;
    desktopModeInput.checked = options.desktopMode;
    syncInputValue(windowsWidthInput, options['windows-window-width'] ? String(options['windows-window-width']) : '');
    syncInputValue(windowsHeightInput, options['windows-window-height'] ? String(options['windows-window-height']) : '');
    windowsMaximizedInput.checked = options['windows-window-maximized'];
    syncInputValue(linuxWidthInput, options['linux-window-width'] ? String(options['linux-window-width']) : '');
    syncInputValue(linuxHeightInput, options['linux-window-height'] ? String(options['linux-window-height']) : '');
    linuxMaximizedInput.checked = options['linux-window-maximized'];
  }

  function collectFeatureOptions() {
    const featureOptions = normalizeFeatureOptions({
      'feature-immersive-fullscreen': immersiveFullscreenInput.checked,
      'feature-desktop-mode': desktopModeInput.checked,
      'windows-window-width': windowsWidthInput.value,
      'windows-window-height': windowsHeightInput.value,
      'windows-window-maximized': windowsMaximizedInput.checked,
      'linux-window-width': linuxWidthInput.value,
      'linux-window-height': linuxHeightInput.value,
      'linux-window-maximized': linuxMaximizedInput.checked,
    });
    return {
      'feature-immersive-fullscreen': featureOptions.immersiveFullscreen,
      'feature-desktop-mode': featureOptions.desktopMode,
      'windows-window-width': featureOptions['windows-window-width'],
      'windows-window-height': featureOptions['windows-window-height'],
      'windows-window-maximized': featureOptions['windows-window-maximized'],
      'linux-window-width': featureOptions['linux-window-width'],
      'linux-window-height': featureOptions['linux-window-height'],
      'linux-window-maximized': featureOptions['linux-window-maximized'],
    };
  }

  function cancelPendingAutoScroll() {
    if (pendingAutoScrollTimer) {
      clearTimeout(pendingAutoScrollTimer);
      pendingAutoScrollTimer = null;
    }
  }

  function isMostlyInViewport(el, threshold = 0.7) {
    if (!el) return true;
    const rect = el.getBoundingClientRect();
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    const visibleTop = Math.max(rect.top, 0);
    const visibleBottom = Math.min(rect.bottom, viewportHeight);
    const visibleHeight = Math.max(0, visibleBottom - visibleTop);
    const targetHeight = Math.max(1, Math.min(rect.height, viewportHeight));
    return (visibleHeight / targetHeight) >= threshold;
  }

  function scheduleGentleScroll(el, options = {}) {
    cancelPendingAutoScroll();
    const delay = options.delay || 0;
    pendingAutoScrollTimer = window.setTimeout(() => {
      pendingAutoScrollTimer = null;
      if (!el || isMostlyInViewport(el, options.threshold || 0.7)) return;
      el.scrollIntoView({ behavior: 'smooth', block: options.block || 'start' });
    }, delay);
  }

  function sanitizeAndroidVersionName(value) {
    const cleaned = String(value || '').replace(/[^0-9A-Za-z._-]/g, '').replace(/^[._-]+|[._-]+$/g, '');
    return cleaned || '1.0.0';
  }

  function sanitizeAndroidVersionCode(value) {
    if (value === null || value === undefined) return '';
    if (String(value).trim() === '') return '';
    const parsed = parseInt(String(value || '').trim(), 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : '';
  }

  // Desktop shell window dimension (px). '' = browser default (no flag).
  function sanitizeWindowSize(value) {
    if (value === null || value === undefined) return '';
    if (String(value).trim() === '') return '';
    const parsed = parseInt(String(value).trim(), 10);
    if (!Number.isFinite(parsed)) return '';
    return Math.max(320, Math.min(7680, parsed));
  }

  function sanitizeAndroidPackagePrefix(value) {
    const raw = String(value || '').toLowerCase();
    const parts = raw.split('.').map((chunk) => {
      let token = chunk.replace(/[^a-z0-9_]/g, '');
      if (!token) return '';
      if (/^[0-9]/.test(token)) token = `p${token}`;
      return token;
    }).filter(Boolean);
    return parts.length >= 2 ? parts.join('.') : 'com.webtoapp';
  }

  function getDeviceFingerprint() {
    function syncFingerprintCookie(value) {
      if (!value) return;
      const secure = window.location.protocol === 'https:' ? '; Secure' : '';
      document.cookie = `${DEVICE_COOKIE_KEY}=${encodeURIComponent(value)}; Max-Age=31536000; Path=/; SameSite=Lax${secure}`;
    }
    try {
      const existing = window.localStorage.getItem(DEVICE_STORAGE_KEY);
      if (existing) {
        syncFingerprintCookie(existing);
        return existing;
      }
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      const created = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
      window.localStorage.setItem(DEVICE_STORAGE_KEY, created);
      syncFingerprintCookie(created);
      return created;
    } catch (_err) {
      const fallback = `volatile-${Date.now().toString(16)}`;
      syncFingerprintCookie(fallback);
      return fallback;
    }
  }

  function apiHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-Device-Fingerprint': deviceFingerprint,
    };
  }

  // Multipart uploads must not carry a JSON Content-Type; the browser sets
  // the multipart boundary itself.
  function apiUploadHeaders() {
    return { 'X-Device-Fingerprint': deviceFingerprint };
  }

  function apiErrorMessage(detail, fallbackKey) {
    if (detail && typeof detail === 'object') {
      if (detail.code) {
        const translated = t(detail.code, { msg: detail.message || '' });
        if (translated !== detail.code) return translated;
      }
      if (detail.message) return String(detail.message);
      return t(fallbackKey);
    }
    if (typeof detail === 'string' && detail) return detail;
    return t(fallbackKey);
  }

  function setInputMode(mode) {
    inputMode = mode === 'html' ? 'html' : 'url';
    modeUrlBtn.classList.toggle('active', inputMode === 'url');
    modeHtmlBtn.classList.toggle('active', inputMode === 'html');
    urlInputWrap.classList.toggle('hidden', inputMode === 'html');
    htmlInputWrap.classList.toggle('hidden', inputMode !== 'html');
  }

  // --- Platform tabs (per-platform settings) ---
  // Only Android currently exposes extra knobs (they drive the real APK
  // build); the other panels document their fixed shells + shared-settings
  // reuse. IDs of the Android inputs are unchanged so the submit path below
  // keeps working untouched.
  const platformTabs = Array.from(document.querySelectorAll('[data-platform-tab]'));
  const platformPanels = Array.from(document.querySelectorAll('[data-platform-panel]'));
  function selectPlatformTab(name, focusTab) {
    const target = String(name || 'android');
    platformTabs.forEach((tab) => {
      const active = tab.dataset.platformTab === target;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
      if (active && focusTab) tab.focus();
    });
    platformPanels.forEach((panel) => {
      panel.hidden = panel.dataset.platformPanel !== target;
    });
  }
  platformTabs.forEach((tab) => {
    tab.addEventListener('click', () => selectPlatformTab(tab.dataset.platformTab, false));
    tab.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const dir = e.key === 'ArrowRight' ? 1 : -1;
      const rtl = document.documentElement.dir === 'rtl';
      const next = platformTabs[(platformTabs.indexOf(tab) + (rtl ? -dir : dir) + platformTabs.length) % platformTabs.length];
      if (next) selectPlatformTab(next.dataset.platformTab, true);
    });
  });

  function formatBytes(bytes) {
    const n = Number(bytes || 0);
    if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${n} B`;
  }

  function resetHtmlFile() {
    htmlFile = null;
    htmlFileInput.value = '';
    htmlDropzone.classList.remove('has-file');
    htmlFileLabel.textContent = t('mode.htmlPick');
    htmlFileHint.textContent = t('mode.htmlHint');
  }

  async function attachHistoryItem(appId) {
    const value = String(appId || '').trim();
    if (!value) return null;
    const res = await fetch(`/api/history/attach/${encodeURIComponent(value)}`, {
      method: 'POST',
      headers: apiHeaders(),
    });
    if (!res.ok) throw new Error('attach failed');
    return res.json();
  }

  async function recoverHistoryItems() {
    const res = await fetch('/api/history/recover', {
      method: 'POST',
      headers: apiHeaders(),
    });
    if (!res.ok) throw new Error('recover failed');
    return res.json();
  }

  function formatHistoryTime(value) {
    if (!value) return t('history.justNow');
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return t('history.justNow');
    return date.toLocaleString(locale(), {
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function getAbsoluteUrl(pathOrUrl) {
    if (!pathOrUrl) return '';
    try {
      return new URL(pathOrUrl, window.location.origin).toString();
    } catch (_err) {
      return String(pathOrUrl || '');
    }
  }

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function fillIconPreview(dataUrl, label) {
    if (dataUrl) {
      customIconDataUrl = dataUrl;
      customIconPreview.src = dataUrl;
      customIconPreview.parentElement.classList.add('has-image');
      customIconPlaceholder.textContent = label || t('icon.refilled');
      syncRestoreIconButton();
      return;
    }
    customIconDataUrl = '';
    customIconInput.value = '';
    if (customIconFileName) customIconFileName.textContent = t('config.noFileChosen');
    customIconPreview.removeAttribute('src');
    customIconPreview.parentElement.classList.remove('has-image');
    customIconPlaceholder.textContent = t('config.iconAutoFetch');
    syncRestoreIconButton();
  }

  function syncRestoreIconButton() {
    if (!customIconClearBtn) return;
    if (restoreIconDataUrl) {
      customIconClearBtn.textContent = restoreIconButtonLabel;
      customIconClearBtn.disabled = false;
      return;
    }
    if (customIconDataUrl) {
      customIconClearBtn.textContent = t('icon.clear');
      customIconClearBtn.disabled = false;
      return;
    }
    customIconClearBtn.textContent = t('icon.noRestore');
    customIconClearBtn.disabled = true;
  }

  function setRestoreIconState(dataUrl, options = {}) {
    restoreIconDataUrl = String(dataUrl || '').trim();
    restoreIconLabel = String(options.label || '').trim();
    restoreIconFileName = String(options.fileName || '').trim();
    restoreIconButtonLabel = String(options.buttonLabel || t('icon.noRestore')).trim() || t('icon.noRestore');
    syncRestoreIconButton();
  }

  function nameSourceLabel(source) {
    const key = String(source || '').trim();
    const known = ['site_name', 'application_name', 'apple_mobile_web_app_title',
      'title_host_match', 'title_first_part', 'title_full', 'host_fallback'];
    return known.indexOf(key) !== -1 ? t('nameSource.' + key) : t('nameSource.default');
  }

  function updateAppNameSourceNote(source, suggestedName) {
    if (!appNameSourceNote) return;
    if (!suggestedName) {
      appNameSourceNote.textContent = t('config.appNameNote');
      return;
    }
    if (!source) {
      appNameSourceNote.textContent = t('note.currentRefilled');
      return;
    }
    const label = nameSourceLabel(source);
    appNameSourceNote.textContent = t('note.filledBy', { source: label });
  }

  function syncInputValue(input, value) {
    const text = value == null ? '' : String(value);
    input.value = text;
    input.setAttribute('value', text);
  }

  function renderHistory(items) {
    const list = Array.isArray(items) ? items : [];
    lastHistoryItems = list;
    historyList.innerHTML = '';
    historyEmpty.classList.toggle('hidden', list.length > 0);
    historyList.classList.toggle('selecting', historySelectMode);
    updateHistorySelectBar();
    if (!list.length) return;

    const fragment = document.createDocumentFragment();
    list.forEach((item) => {
      const card = document.createElement('article');
      const publicPath = getAbsoluteUrl(item.public_path || `/a/${item.app_id}`);
      const targetUrl = item.target_url || '';
      const breakdown = item.visit_breakdown || {};
      const downloadBreakdown = item.download_breakdown || {};
      const downloadSummary = Object.entries(downloadBreakdown)
        .map(([platform, count]) => `${platform} ${Number(count || 0).toLocaleString(locale())}`)
        .join(' · ') || t('history.none');
      const iconHtml = item.icon_url
        ? `<img class="history-icon" src="${escapeHtml(item.icon_url)}" alt="${escapeHtml(item.name || item.app_id)}">`
        : `<div class="history-icon" aria-hidden="true"></div>`;
      const isHtmlApp = !!(item.recipe && item.recipe.source_type === 'html');
      const htmlBadge = isHtmlApp
        ? `<span class="history-badge-html">${escapeHtml(t('history.badgeHtml'))}</span>`
        : '';
      card.className = 'history-card';
      card.classList.toggle('selected', historySelected.has(item.app_id || ''));
      card._historyItem = item;
      card.innerHTML = `
        <div class="history-main">
          <div class="history-name-row">
            <label class="history-check"><input type="checkbox" data-select="${escapeHtml(item.app_id || '')}" ${historySelected.has(item.app_id || '') ? 'checked' : ''}></label>
            ${iconHtml}
            <div class="history-name-block">
              <div class="history-name">${escapeHtml(item.name || item.app_id)}${htmlBadge}</div>
              <div class="history-link">${escapeHtml(publicPath)}</div>
            </div>
          </div>
          <div class="history-meta">
            <span class="history-meta-chip">${escapeHtml(t('history.visits', { n: Number(item.visit_count || 0).toLocaleString(locale()) }))}</span>
            <span class="history-meta-chip">${escapeHtml(t('history.downloads', { n: Number(item.download_count || 0).toLocaleString(locale()) }))}</span>
            <span class="history-meta-chip">${escapeHtml(t('history.updatedAt', { time: formatHistoryTime(item.updated_at) }))}</span>
            <span class="history-meta-chip">${escapeHtml(t('history.target', { url: targetUrl }))}</span>
          </div>
          <div class="history-breakdown">
            <div class="history-breakdown-row">
              <span>${escapeHtml(t('history.rowLanding'))}</span>
              <strong>${Number(breakdown.landing || 0).toLocaleString(locale())}</strong>
            </div>
            <div class="history-breakdown-row">
              <span>${escapeHtml(t('history.rowInstall'))}</span>
              <strong>${Number(breakdown.install || 0).toLocaleString(locale())}</strong>
            </div>
            <div class="history-breakdown-row">
              <span>${escapeHtml(t('history.rowPwa'))}</span>
              <strong>${Number(breakdown.pwa || 0).toLocaleString(locale())}</strong>
            </div>
            <div class="history-breakdown-row">
              <span>${escapeHtml(t('history.rowLaunch'))}</span>
              <strong>${Number(breakdown.launch || 0).toLocaleString(locale())}</strong>
            </div>
            <div class="history-breakdown-row history-breakdown-row-wide">
              <span>${escapeHtml(t('history.rowPlatform'))}</span>
              <strong>${escapeHtml(downloadSummary)}</strong>
            </div>
          </div>
        </div>
        <div class="history-actions">
          <button class="history-action primary" type="button" data-open="${escapeHtml(publicPath)}">${escapeHtml(t('history.openPage'))}</button>
          <button class="history-action" type="button" data-regenerate="${escapeHtml(item.app_id || '')}">${escapeHtml(t('history.regenerate'))}</button>
          <button class="history-action" type="button" data-edit="${escapeHtml(item.app_id || '')}">${escapeHtml(t('history.editForm'))}</button>
          <button class="history-action" type="button" data-copy="${escapeHtml(publicPath)}">${escapeHtml(t('history.copyLink'))}</button>
        </div>
      `;
      fragment.appendChild(card);
    });
    historyList.appendChild(fragment);
  }

  async function loadHistory() {
    try {
      const res = await fetch('/api/history', {
        headers: { 'X-Device-Fingerprint': deviceFingerprint },
      });
      if (!res.ok) throw new Error('failed');
      const data = await res.json();
      renderHistory(data.items || []);
      return data.items || [];
    } catch (_err) {
      renderHistory([]);
      return [];
    }
  }

  async function recoverHistoryFromPageContext() {
    const candidates = new Set();
    const currentLink = appLink && appLink.value ? appLink.value : '';
    const currentUrl = previewOpenBtn && previewOpenBtn.dataset ? previewOpenBtn.dataset.href : '';
    [currentLink, currentUrl, window.location.href].forEach((value) => {
      const match = String(value || '').match(/\/a\/([a-f0-9]{8})(?:[/?#]|$)/i);
      if (match) candidates.add(match[1]);
    });
    if (!candidates.size) return [];
    for (const appId of candidates) {
      try {
        await attachHistoryItem(appId);
      } catch (_err) {
        // Ignore failed recovery attempts; the app may no longer exist.
      }
    }
    return loadHistory();
  }

  deviceFingerprint = getDeviceFingerprint();
  loadHistory().then((items) => {
    if (!items.length) {
      recoverHistoryFromPageContext();
    }
  });

  async function applyHistoryItemToForm(item) {
    const recipe = item.recipe || {};
    const featureOptions = recipe.options || item.options || {};
    currentUrl = item.target_url || recipe.url || '';
    syncInputValue(urlInput, currentUrl);
    syncInputValue(appNameInput, item.name || recipe.name || '');
    updateAppNameSourceNote('', item.name || recipe.name || '');
    const color = item.color || recipe.color || '#7c3aed';
    syncInputValue(appColorInput, color);
    colorHex.textContent = color;
    syncInputValue(
      androidVersionNameInput,
      sanitizeAndroidVersionName(String(item.android_version_name || recipe.android_version_name || '1.0.0'))
    );
    const previousVersionCode = sanitizeAndroidVersionCode(item.android_version_code || recipe.android_version_code || '');
    syncInputValue(androidVersionCodeInput, '');
    androidVersionCodeInput.placeholder = previousVersionCode
      ? t('versionCode.lastValue', { n: previousVersionCode })
      : t('config.versionCodePlaceholder');
    syncInputValue(
      androidPackagePrefixInput,
      sanitizeAndroidPackagePrefix(item.android_package_prefix || recipe.android_package_prefix || 'com.webtoapp')
    );
    applyFeatureOptionsToForm(featureOptions);
    if (item.icon_url) {
      try {
        const iconRes = await fetch(item.icon_url);
        if (!iconRes.ok) throw new Error('icon fetch failed');
        const iconBlob = await iconRes.blob();
        const iconDataUrl = await readFileAsDataUrl(iconBlob);
        detectedIconDataUrl = '';
        setRestoreIconState(iconDataUrl, {
          label: t('icon.recovered'),
          fileName: t('icon.recoveredFile'),
          buttonLabel: t('icon.restoreCurrent'),
        });
        fillIconPreview(iconDataUrl, t('icon.recovered'));
        if (customIconFileName) customIconFileName.textContent = t('icon.recoveredFile');
      } catch (_err) {
        detectedIconDataUrl = '';
        setRestoreIconState('', {});
        fillIconPreview('', '');
      }
    } else {
        detectedIconDataUrl = '';
        setRestoreIconState('', {});
        fillIconPreview('', '');
    }
    showRecoveredAnalysisResults(item);
    workspace.classList.remove('hidden');
  }

  async function generateAppFromCurrentForm() {
    const options = {};
    const versionName = sanitizeAndroidVersionName(androidVersionNameInput.value);
    const versionCode = sanitizeAndroidVersionCode(androidVersionCodeInput.value);
    const packagePrefix = sanitizeAndroidPackagePrefix(androidPackagePrefixInput.value);
    syncInputValue(androidVersionNameInput, versionName);
    syncInputValue(androidVersionCodeInput, versionCode ? String(versionCode) : '');
    syncInputValue(androidPackagePrefixInput, packagePrefix);
    options['android-version-name'] = versionName;
    if (versionCode) options['android-version-code'] = versionCode;
    options['android-package-prefix'] = packagePrefix;
    if (customIconDataUrl) options['custom-icon-data-url'] = customIconDataUrl;
    Object.assign(options, collectFeatureOptions());

    let submitRes;
    if (inputMode === 'html') {
      if (!htmlFile) throw new Error(t('err.htmlNoFile'));
      const form = new FormData();
      form.append('file', htmlFile, htmlFile.name);
      form.append('name', appNameInput.value);
      form.append('color', appColorInput.value);
      form.append('display', 'fullscreen');
      form.append('orientation', 'any');
      form.append('options', JSON.stringify(options));
      submitRes = await fetch('/api/distill/html', {
        method: 'POST',
        headers: apiUploadHeaders(),
        body: form,
      });
    } else {
      submitRes = await fetch('/api/distill', {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify({
          url: currentUrl,
          name: appNameInput.value,
          color: appColorInput.value,
          display: 'fullscreen',
          orientation: 'any',
          options: options,
        }),
      });
    }
    if (!submitRes.ok) {
      const err = await submitRes.json().catch(() => null);
      throw new Error(apiErrorMessage(err && err.detail, 'err.generateFailed'));
    }
    const task = await submitRes.json();
    if (!task.task_id) throw new Error(t('err.taskSubmitFailed'));

    const stageLabel = (stage, progress) => {
      const key = `generate.stage.${stage || 'running'}`;
      const translated = t(key);
      const base = translated === key ? t('generate.stage.running') : translated;
      if (progress && progress.total) {
        return `${base} (${Number(progress.done || 0)}/${Number(progress.total)})`;
      }
      return base;
    };
    if (generateBtn) {
      generateBtn.dataset.originalText = generateBtn.textContent;
      generateBtn.textContent = stageLabel('queued');
    }
    let data = null;
    try {
      let attempts = 0;
      while (attempts < 240) {
        attempts += 1;
        await sleep(attempts <= 8 ? 400 : 800);
        const pollRes = await fetch(`/api/distill/${encodeURIComponent(task.task_id)}`, {
          headers: apiHeaders(),
        });
        if (pollRes.status === 404) throw new Error(t('err.taskMissing'));
        if (!pollRes.ok) {
          let message = '';
          try {
            const err = await pollRes.json();
            message = apiErrorMessage(err && err.detail, 'err.generateFailed');
          } catch (_err) {}
          throw new Error(message || t('err.generateFailed'));
        }
        const payload = await pollRes.json();
        if (payload && payload.status && payload.task_id) {
          if (generateBtn) generateBtn.textContent = stageLabel(payload.stage || payload.status, payload.progress);
          continue;
        }
        data = payload;
        break;
      }
    } finally {
      if (generateBtn && generateBtn.dataset.originalText) {
        generateBtn.textContent = generateBtn.dataset.originalText;
      }
    }
    if (!data) throw new Error(t('err.generateTimeout'));

    const installLink = `${location.origin}${data.url}`;
    appLink.value = installLink;
    previewUrl.textContent = installLink;
    previewFrame.src = installLink;
    previewOpenBtn.dataset.href = installLink;
    resultPanel.classList.remove('hidden');
    const androidMeta = data.android || {};
    let note = document.getElementById('android-fallback-note');
    if (!note) {
      note = document.createElement('p');
      note.id = 'android-fallback-note';
      note.className = 'result-note';
      if (resultPanel) resultPanel.appendChild(note);
    }
    if (androidMeta.fallback && !androidMeta.apk) {
      note.textContent = t('result.androidFallback');
      note.classList.remove('hidden');
    } else {
      note.textContent = '';
      note.classList.add('hidden');
    }
    await loadHistory();
    scheduleGentleScroll(resultPanel, { block: 'nearest', threshold: 0.45, delay: 180 });
    return data;
  }

  // --- Color picker sync ---
  appColorInput.addEventListener('input', () => { colorHex.textContent = appColorInput.value; });
  ['wheel', 'touchstart', 'pointerdown', 'keydown'].forEach((eventName) => {
    window.addEventListener(eventName, cancelPendingAutoScroll, { passive: true });
  });
  customIconInput.addEventListener('change', async () => {
    const file = customIconInput.files && customIconInput.files[0];
    if (!file) return;
    try {
      customIconDataUrl = await readFileAsDataUrl(file);
      if (customIconFileName) customIconFileName.textContent = file.name;
      customIconPreview.src = customIconDataUrl;
      customIconPreview.parentElement.classList.add('has-image');
      customIconPlaceholder.textContent = file.name;
      syncRestoreIconButton();
    } catch (_err) {
      customIconDataUrl = '';
      customIconInput.value = '';
      if (customIconFileName) customIconFileName.textContent = t('icon.readFailed');
      customIconPreview.removeAttribute('src');
      customIconPreview.parentElement.classList.remove('has-image');
      customIconPlaceholder.textContent = t('icon.readFailed');
      syncRestoreIconButton();
    }
  });
  customIconClearBtn.addEventListener('click', () => {
    if (restoreIconDataUrl) {
      fillIconPreview(restoreIconDataUrl, restoreIconLabel || t('icon.recovered'));
      if (customIconFileName) customIconFileName.textContent = restoreIconFileName || t('icon.recovered');
      syncRestoreIconButton();
      return;
    }
    fillIconPreview('', '');
    syncRestoreIconButton();
  });
  androidVersionNameInput.addEventListener('blur', () => {
    androidVersionNameInput.value = sanitizeAndroidVersionName(androidVersionNameInput.value);
  });
  androidVersionCodeInput.addEventListener('blur', () => {
    const sanitized = sanitizeAndroidVersionCode(androidVersionCodeInput.value);
    androidVersionCodeInput.value = sanitized ? String(sanitized) : '';
  });
  androidPackagePrefixInput.addEventListener('blur', () => {
    androidPackagePrefixInput.value = sanitizeAndroidPackagePrefix(androidPackagePrefixInput.value);
  });
  [windowsWidthInput, windowsHeightInput, linuxWidthInput, linuxHeightInput].forEach((input) => {
    input.addEventListener('blur', () => {
      const sanitized = sanitizeWindowSize(input.value);
      input.value = sanitized ? String(sanitized) : '';
    });
  });

  // --- URL Validation ---
  function isValidUrl(str) {
    try { const u = new URL(str.startsWith('http') ? str : 'https://' + str); return !!u.hostname.includes('.'); } catch { return false; }
  }

  function normalizeUrl(str) {
    return str.startsWith('http') ? str : 'https://' + str;
  }

  // --- Distill Flow ---
  distillBtn.addEventListener('click', () => {
    if (inputMode === 'html') {
      if (!htmlFile) {
        alert(t('err.htmlNoFile'));
        return;
      }
      scheduleGentleScroll(workspace, { block: 'start', threshold: 0.55, delay: 120 });
      return;
    }
    startDistill();
  });
  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter' && inputMode === 'url') startDistill(); });
  modeUrlBtn.addEventListener('click', () => setInputMode('url'));
  modeHtmlBtn.addEventListener('click', () => setInputMode('html'));
  htmlFileInput.addEventListener('change', () => {
    const file = htmlFileInput.files && htmlFileInput.files[0];
    if (!file) return;
    selectHtmlFile(file);
  });
  ['dragover', 'dragenter'].forEach((eventName) => {
    htmlDropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      htmlDropzone.classList.add('dragover');
    });
  });
  ['dragleave', 'drop'].forEach((eventName) => {
    htmlDropzone.addEventListener(eventName, () => htmlDropzone.classList.remove('dragover'));
  });
  htmlDropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) selectHtmlFile(file);
  });

  async function selectHtmlFile(file) {
    htmlFile = file;
    htmlFileLabel.textContent = file.name;
    htmlFileHint.textContent = `${formatBytes(file.size)} · ${t('mode.htmlHint')}`;
    htmlDropzone.classList.add('has-file');
    await startHtmlAnalyze(file);
  }

  async function startHtmlAnalyze(file) {
    let data = null;
    let uploadError = null;
    try {
      const form = new FormData();
      form.append('file', file, file.name);
      const res = await fetch('/api/analyze/html', {
        method: 'POST',
        headers: apiUploadHeaders(),
        body: form,
      });
      if (res.ok) {
        data = await res.json();
      } else {
        const err = await res.json().catch(() => null);
        uploadError = apiErrorMessage(err && err.detail, 'err.generateFailed');
      }
    } catch (_err) {
      // Network failure: fall through to a local, file-name-only prefill.
    }
    if (uploadError) {
      alert(uploadError);
      resetHtmlFile();
      return;
    }
    if (!data) {
      data = {
        sourceType: 'html',
        name: file.name.replace(/\.(html?|zip)$/i, ''),
        color: '',
        iconDataUrl: '',
        fileCount: 0,
        totalBytes: file.size,
      };
    }
    workspace.classList.remove('hidden');
    resultPanel.classList.add('hidden');
    showHtmlAnalysisResults(data);
    scheduleGentleScroll(workspace, { block: 'start', threshold: 0.55, delay: 120 });
  }

  function showHtmlAnalysisResults(data) {
    analysisStatus.textContent = t('analysis.statusDone');
    analysisStatus.className = 'status-badge done';
    const suggestedName = String(data.name || '').trim()
      || (htmlFile ? htmlFile.name.replace(/\.(html?|zip)$/i, '') : '');
    if (suggestedName) {
      syncInputValue(appNameInput, suggestedName);
      updateAppNameSourceNote(data.name ? 'title_full' : '', suggestedName);
    }
    const themeColor = String(data.color || '').trim();
    if (/^#[0-9a-fA-F]{3,8}$/.test(themeColor)) {
      syncInputValue(appColorInput, themeColor);
      colorHex.textContent = themeColor.toUpperCase();
    }
    if (data.iconDataUrl) {
      detectedIconDataUrl = String(data.iconDataUrl);
      setRestoreIconState(detectedIconDataUrl, {
        label: t('icon.autoFetched'),
        fileName: t('icon.autoFetchedFile'),
        buttonLabel: t('icon.restoreAutoFetch'),
      });
      fillIconPreview(detectedIconDataUrl, t('icon.autoFetched'));
      if (customIconFileName) customIconFileName.textContent = t('icon.autoFetchedFile');
    } else {
      detectedIconDataUrl = '';
      setRestoreIconState('', {});
      fillIconPreview('', '');
    }
    analysisBody.innerHTML = `
      <div class="analysis-results">
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.suggestedName'))}</span><span class="value good">${escapeHtml(suggestedName || t('analysis.notSaved'))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.iconStatus'))}</span><span class="value ${data.iconDataUrl ? 'good' : 'info'}">${escapeHtml(data.iconDataUrl ? t('analysis.iconAutoFetched') : t('analysis.iconNotDetected'))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.htmlFiles'))}</span><span class="value info">${Number(data.fileCount || 0).toLocaleString(locale())}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.htmlTotalSize'))}</span><span class="value info">${escapeHtml(formatBytes(data.totalBytes || 0))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.htmlSource'))}</span><span class="value info">${escapeHtml(t('analysis.htmlSourceValue'))}</span></div>
      </div>`;
  }

  async function startDistill() {
    const raw = urlInput.value.trim();
    if (!raw || !isValidUrl(raw)) {
      urlInput.style.boxShadow = '0 0 0 2px #f87171';
      setTimeout(() => urlInput.style.boxShadow = '', 1500);
      return;
    }

    const url = normalizeUrl(raw);
    currentUrl = url;
    workspace.classList.remove('hidden');
    resultPanel.classList.add('hidden');
    analysisStatus.textContent = t('analysis.statusAnalyzing');
    analysisStatus.className = 'status-badge';
    analysisBody.innerHTML = `<div class="analysis-loader"><div class="loader-bar"></div><p id="loader-text">${escapeHtml(t('analysis.fetching'))}</p></div>`;
    scheduleGentleScroll(workspace, { block: 'start', threshold: 0.55, delay: 120 });

    const steps = [
      t('analysis.stepFetch'),
      t('analysis.stepDom'),
      t('analysis.stepDesign'),
      t('analysis.stepOptimize'),
    ];
    let stepIndex = 0;
    const loaderTimer = setInterval(() => {
      const loader = document.getElementById('loader-text');
      if (!loader) return;
      loader.textContent = steps[Math.min(stepIndex, steps.length - 1)];
      stepIndex += 1;
    }, 450);

    let data;
    try {
      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      if (res.ok) data = await res.json();
    } catch (_err) {
    } finally {
      clearInterval(loaderTimer);
    }

    if (!data) data = simulateAnalysis(url);
    showAnalysisResults(data);
  }

  function simulateAnalysis(url) {
    const host = new URL(url).hostname.replace('www.', '');
    const suggestedName = host.split('.')[0].charAt(0).toUpperCase() + host.split('.')[0].slice(1);
    return {
      title: suggestedName,
      suggestedName,
      suggestedNameSource: 'host_fallback',
      url: url,
      host: host,
      favicon: `https://www.google.com/s2/favicons?domain=${host}&sz=64`,
      faviconDataUrl: '',
      themeColor: '#7c3aed',
      // Analysis didn't run (fetch failed) — show N/A rather than fabricated
      // numbers. Previously this returned random ad/tracker counts, which
      // misled users about sites we never actually inspected.
      originalSize: 'N/A',
      distilledSize: 'N/A',
      speedBoost: 'N/A',
    };
  }

  function buildRecoveredAnalysisData(item) {
    const recipe = item && item.recipe && typeof item.recipe === 'object' ? item.recipe : {};
    const targetUrl = String(item.target_url || recipe.url || '').trim();
    let host = '';
    try {
      host = new URL(targetUrl).hostname.replace(/^www\./i, '');
    } catch (_err) {}
    const suggestedName = String(item.name || recipe.name || host || item.app_id || '').trim();
    const title = String(
      recipe.title
      || recipe.site_title
      || recipe.site_name
      || suggestedName
      || host
    ).trim();
    const suggestedNameSource = String(
      recipe.suggestedNameSource
      || recipe.suggested_name_source
      || recipe.name_source
      || ''
    ).trim();
    return {
      title,
      suggestedName,
      suggestedNameSource,
      host,
      targetUrl,
      hasIcon: !!item.icon_url || !!recipe.custom_icon_uploaded,
    };
  }

  function showRecoveredAnalysisResults(item) {
    const data = buildRecoveredAnalysisData(item || {});
    analysisStatus.textContent = t('analysis.statusRecovered');
    analysisStatus.className = 'status-badge done';
    analysisBody.innerHTML = `
      <div class="analysis-results">
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.siteTitle'))}</span><span class="value info">${escapeHtml(data.title || t('analysis.notSaved'))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.suggestedName'))}</span><span class="value good">${escapeHtml(data.suggestedName || t('analysis.notSaved'))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.nameSource'))}</span><span class="value info">${escapeHtml(data.suggestedNameSource ? nameSourceLabel(data.suggestedNameSource) : t('analysis.fromHistory'))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.iconStatus'))}</span><span class="value ${data.hasIcon ? 'good' : 'info'}">${escapeHtml(data.hasIcon ? t('analysis.iconRecovered') : t('analysis.iconNotSaved'))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.targetUrl'))}</span><span class="value info">${escapeHtml(data.targetUrl || data.host || t('analysis.notSaved'))}</span></div>
      </div>
      <div class="analysis-actions">
        <button id="reanalyze-btn" class="btn-secondary" type="button">${escapeHtml(t('analysis.reanalyze'))}</button>
      </div>`;
    const reanalyzeBtn = document.getElementById('reanalyze-btn');
    if (reanalyzeBtn) {
      reanalyzeBtn.addEventListener('click', async () => {
        reanalyzeBtn.disabled = true;
        reanalyzeBtn.textContent = t('analysis.reanalyzing');
        try {
          await startDistill();
        } finally {
          reanalyzeBtn.disabled = false;
          reanalyzeBtn.textContent = t('analysis.reanalyze');
        }
      });
    }
  }

  function showAnalysisResults(data) {
    analysisStatus.textContent = t('analysis.statusDone');
    analysisStatus.className = 'status-badge done';
    const title = String(data.title || data.host || '').trim();
    const suggestedName = String(data.suggestedName || data.siteName || data.title || data.host || '').trim();
    const suggestedNameSource = String(data.suggestedNameSource || '').trim();
    const suggestedNameSourceLabel = nameSourceLabel(suggestedNameSource);
    const themeColor = String(data.themeColor || '#7c3aed').trim() || '#7c3aed';
    syncInputValue(appNameInput, suggestedName);
    updateAppNameSourceNote(suggestedNameSource, suggestedName);
    syncInputValue(appColorInput, themeColor);
    colorHex.textContent = themeColor.toUpperCase();
    if (data.faviconDataUrl) {
      detectedIconDataUrl = String(data.faviconDataUrl);
      setRestoreIconState(detectedIconDataUrl, {
        label: t('icon.autoFetched'),
        fileName: t('icon.autoFetchedFile'),
        buttonLabel: t('icon.restoreAutoFetch'),
      });
      fillIconPreview(String(data.faviconDataUrl), t('icon.autoFetched'));
      if (customIconFileName) customIconFileName.textContent = t('icon.autoFetchedFile');
    } else {
      detectedIconDataUrl = '';
      setRestoreIconState('', {});
      fillIconPreview('', '');
    }

    if (data.authProtected) {
      // Site sits behind an auth proxy (Authelia / Cloudflare Access). The
      // server can't see the real page, so don't show the usual (empty)
      // ad/tracker stats — show an explanatory notice and point the user at
      // the manual name/icon fields. The generated app itself is unaffected:
      // the user authenticates inside the WebView on first launch.
      const providerLabel = data.authProvider === 'cloudflare_access'
        ? t('analysis.authProvider.cloudflare')
        : t('analysis.authProvider.authelia');
      analysisBody.innerHTML = `
        <div class="analysis-results">
          <div class="analysis-item analysis-notice">
            <span class="value warn">${escapeHtml(t('analysis.authProtected.notice', { provider: providerLabel }))}</span>
          </div>
          <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.suggestedName'))}</span><span class="value good">${escapeHtml(suggestedName)}</span></div>
          <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.nameSource'))}</span><span class="value info">${escapeHtml(suggestedNameSourceLabel)}</span></div>
          <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.iconStatus'))}</span><span class="value info">${escapeHtml(t('analysis.authProtected.iconHint'))}</span></div>
          <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.originalSize'))}</span><span class="value">${escapeHtml(String(data.originalSize))}</span></div>
          <div class="analysis-item analysis-hint">
            <span class="value info">${escapeHtml(t('analysis.authProtected.appWorks'))}</span>
          </div>
        </div>`;
      return;
    }

    analysisBody.innerHTML = `
      <div class="analysis-results">
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.siteTitle'))}</span><span class="value info">${escapeHtml(title)}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.suggestedName'))}</span><span class="value good">${escapeHtml(suggestedName)}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.nameSource'))}</span><span class="value info">${escapeHtml(suggestedNameSourceLabel)}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.iconStatus'))}</span><span class="value ${data.faviconDataUrl ? 'good' : 'info'}">${escapeHtml(data.faviconDataUrl ? t('analysis.iconAutoFetched') : t('analysis.iconNotDetected'))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.originalSize'))}</span><span class="value">${escapeHtml(String(data.originalSize))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.distilledSize'))}</span><span class="value good">${escapeHtml(String(data.distilledSize))}</span></div>
        <div class="analysis-item"><span class="label">${escapeHtml(t('analysis.speedBoost'))}</span><span class="value good">${escapeHtml(String(data.speedBoost))}</span></div>
      </div>`;
  }

  // --- Generate App ---

  generateBtn.addEventListener('click', async () => {
    generateBtn.textContent = t('config.generating');
    generateBtn.disabled = true;

    try {
      await generateAppFromCurrentForm();
    } catch (e) {
      alert(t('err.generateRetry', { msg: e.message }));
    }

    generateBtn.textContent = t('config.generateBtnEmoji');
    generateBtn.disabled = false;
  });

  syncRestoreIconButton();

  // --- Copy Link ---
  copyBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(appLink.value).then(() => {
      const orig = copyBtn.textContent;
      copyBtn.textContent = t('result.copied');
      setTimeout(() => copyBtn.textContent = orig, 2000);
    });
  });

  previewOpenBtn.addEventListener('click', () => {
    const href = previewOpenBtn.dataset.href;
    if (!href) return;
    window.open(href, '_blank', 'noopener,noreferrer');
  });

  historyList.addEventListener('click', async (event) => {
    const card = event.target.closest('.history-card');
    const item = card && card._historyItem;
    if (historySelectMode) {
      const id = (item && item.app_id) || '';
      if (!id) return;
      const box = event.target.closest('input[data-select]');
      if (box) {
        if (box.checked) historySelected.add(id); else historySelected.delete(id);
      } else if (historySelected.has(id)) {
        historySelected.delete(id);
      } else {
        historySelected.add(id);
      }
      renderHistory(lastHistoryItems);
      return;
    }
    const target = event.target.closest('[data-open], [data-copy], [data-edit], [data-regenerate]');
    if (!target) return;
    if (target.dataset.open) {
      window.open(target.dataset.open, '_blank', 'noopener,noreferrer');
      return;
    }
    if (target.dataset.edit) {
      if (!item) return;
      await applyHistoryItemToForm(item);
      scheduleGentleScroll(workspace, { block: 'start', threshold: 0.55, delay: 80 });
      return;
    }
    if (target.dataset.regenerate) {
      if (!item) return;
      try {
        target.disabled = true;
        target.textContent = t('history.regenerating');
        await applyHistoryItemToForm(item);
        await generateAppFromCurrentForm();
      } catch (_err) {
        alert(t('err.regenerateRetry'));
      } finally {
        target.disabled = false;
        target.textContent = t('history.regenerate');
      }
      return;
    }
    if (target.dataset.copy) {
      try {
        await navigator.clipboard.writeText(target.dataset.copy);
        const original = target.textContent;
        target.textContent = t('history.copied');
        window.setTimeout(() => { target.textContent = original; }, 1600);
      } catch (_err) {
        alert(t('err.copyManual'));
      }
      return;
    }
  });

  function updateHistorySelectBar() {
    const total = lastHistoryItems.length;
    historySelectCount.textContent = t('history.selectedCount', { n: String(historySelected.size) });
    historyDeleteSelectedBtn.disabled = historySelected.size === 0;
    historySelectAll.checked = total > 0 && historySelected.size === total;
  }

  function setHistorySelectMode(on) {
    historySelectMode = on;
    if (!on) historySelected.clear();
    historySelectBar.classList.toggle('hidden', !on);
    historyManageBtn.classList.toggle('hidden', on);
    historyExportBtn.classList.toggle('hidden', on);
    historyImportBtn.classList.toggle('hidden', on);
    renderHistory(lastHistoryItems);
  }

  historyManageBtn.addEventListener('click', () => setHistorySelectMode(true));
  historyCancelSelectBtn.addEventListener('click', () => setHistorySelectMode(false));

  historySelectAll.addEventListener('change', () => {
    historySelected.clear();
    if (historySelectAll.checked) {
      lastHistoryItems.forEach((entry) => historySelected.add(entry.app_id || ''));
    }
    renderHistory(lastHistoryItems);
  });

  historyDeleteSelectedBtn.addEventListener('click', async () => {
    const ids = Array.from(historySelected);
    if (!ids.length) return;
    if (!window.confirm(t('history.confirmDeleteBulk', { n: String(ids.length) }))) return;
    try {
      historyDeleteSelectedBtn.disabled = true;
      await Promise.all(ids.map((id) => fetch(`/api/history/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: { 'X-Device-Fingerprint': deviceFingerprint },
      })));
      setHistorySelectMode(false);
      await loadHistory();
    } catch (_err) {
      alert(t('err.removeRetry'));
    } finally {
      historyDeleteSelectedBtn.disabled = false;
    }
  });

  historyExportBtn.addEventListener('click', async () => {
    try {
      const res = await fetch('/api/history/export', {
        headers: { 'X-Device-Fingerprint': deviceFingerprint },
      });
      if (!res.ok) throw new Error('export failed');
      const data = await res.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = `webtoapp-history-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(href);
    } catch (_err) {
      alert(t('err.exportRetry'));
    }
  });

  historyImportBtn.addEventListener('click', () => {
    historyImportInput.click();
  });

  historyImportInput.addEventListener('change', async () => {
    const file = historyImportInput.files && historyImportInput.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const res = await fetch('/api/history/import', {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('import failed');
      const data = await res.json();
      renderHistory((data.history && data.history.items) || []);
      alert(t('history.importDone', { imported: data.imported, restored: data.restored }));
    } catch (_err) {
      alert(t('err.importFormat'));
    } finally {
      historyImportInput.value = '';
    }
  });

  if (historyRecoverBtn) {
    historyRecoverBtn.addEventListener('click', async () => {
      const original = historyRecoverBtn.textContent;
      historyRecoverBtn.disabled = true;
      historyRecoverBtn.textContent = t('history.recovering');
      try {
        const data = await recoverHistoryItems();
        renderHistory((data.history && data.history.items) || []);
      } catch (_err) {
        alert(t('err.recoverRetry'));
      } finally {
        historyRecoverBtn.disabled = false;
        historyRecoverBtn.textContent = original;
      }
    });
  }

  // --- Recipe Cards ---
  document.querySelectorAll('.recipe-card').forEach(card => {
    card.addEventListener('click', () => {
      const url = card.dataset.url;
      if (url) {
        urlInput.value = url;
        cancelPendingAutoScroll();
        if (window.scrollY > 120) {
          window.scrollTo({ top: 0, behavior: 'smooth' });
        }
        setTimeout(startDistill, 500);
      }
    });
  });

  // --- Nav scroll effect ---
  const nav = document.getElementById('nav');
  window.addEventListener('scroll', () => {
    nav.style.background = window.scrollY > 50 ? 'rgba(243, 234, 223, 0.94)' : 'rgba(243, 234, 223, 0.9)';
  }, { passive: true });

  // --- Util ---
  function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(reader.error || new Error('file read failed'));
      reader.readAsDataURL(file);
    });
  }

  // --- Language switcher ---
  (function initLanguageSwitcher() {
    const select = document.getElementById('lang-select');
    if (!select || !window.I18n) return;
    window.I18n.supported.forEach((lang) => {
      const opt = document.createElement('option');
      opt.value = lang;
      opt.textContent = window.I18n.nativeNames[lang] || lang;
      select.appendChild(opt);
    });
    select.value = window.I18n.current;
    select.addEventListener('change', () => {
      window.I18n.setLanguage(select.value);
    });
    // Re-render dynamic (JS-generated) content whenever the language changes.
    window.addEventListener('i18n:changed', () => {
      select.value = window.I18n.current;
      if (lastHistoryItems.length) renderHistory(lastHistoryItems);
    });
  })();

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

})();
