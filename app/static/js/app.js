/* ==========================================================================
 * 정답기 (Smart Auto-Grader) — vanilla JS SPA
 * Hash router · Library / Extract / Workbook / Quiz / Results
 * ========================================================================== */
(() => {
  'use strict';

  /* ------------------------------------------------------------------
   * Utilities
   * ------------------------------------------------------------------ */
  const $ = (sel, root = document) => root.querySelector(sel);

  const esc = (v) =>
    String(v ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');

  const ICONS = {
    plus: '<path d="M12 5v14M5 12h14"/>',
    trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/><path d="M10 11v6M14 11v6"/>',
    edit: '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
    chevron: '<path d="M6 9l6 6 6-6"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v4"/><path d="M12 17.5h.01"/>',
    upload: '<path d="M12 16V4"/><path d="M8 8l4-4 4 4"/><path d="M4 20h16"/>',
    clipboard: '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
    play: '<path d="M8 5v14l11-7z"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    arrowLeft: '<path d="M19 12H5"/><path d="M11 18l-6-6 6-6"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-4.5-4.5L7 20"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    x: '<path d="M18 6L6 18M6 6l12 12"/>'
  };
  const ic = (name, size = 16) =>
    `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;

  const pctText = (p, fallback = '–') => {
    if (p === null || p === undefined || p === '' || Number.isNaN(Number(p))) return fallback;
    return `${Math.round(Number(p))}%`;
  };

  const pctBadgeClass = (p) => {
    if (p === null || p === undefined || Number.isNaN(Number(p))) return 'badge-muted';
    const n = Number(p);
    if (n >= 80) return 'badge-green';
    if (n >= 50) return 'badge-amber';
    return 'badge-red';
  };

  const fmtDate = (iso) => {
    const d = new Date(iso);
    if (!iso || Number.isNaN(d.getTime())) return '';
    return d.toLocaleString('ko-KR', {
      month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false
    });
  };

  const storage = {
    localGet(key) { try { return localStorage.getItem(key); } catch { return null; } },
    localSet(key, val) { try { localStorage.setItem(key, val); } catch { /* noop */ } },
    localRemove(key) { try { localStorage.removeItem(key); } catch { /* noop */ } }
  };

  /* Device user ID: generated once per browser, sent on every API call so the
   * server can isolate each device's workbooks/records. */
  const DEVICE_ID_KEY = 'ag_device_user_id';

  function uuidv4() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  const getDeviceUserId = () => {
    let id = storage.localGet(DEVICE_ID_KEY);
    if (!id || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)) {
      id = uuidv4();
      storage.localSet(DEVICE_ID_KEY, id);
    }
    return id;
  };

  /* Circled numerals (①–⑳) → digits; full-width digits → ASCII; "(3)" → "3". */
  function canonAnswer(raw) {
    let s = String(raw == null ? '' : raw).replace(/\u00A0/g, ' ').trim();
    if (!s) return '';
    const circled = s.match(/[\u2460-\u2473]/g);
    if (circled && circled.length) {
      return circled.map((ch) => String(ch.charCodeAt(0) - 0x245F)).join(',');
    }
    s = s.replace(/[\uFF10-\uFF19]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0));
    const wrapped = s.match(/^\((.*)\)$/);
    if (wrapped) s = wrapped[1];
    return s.trim();
  }

  /* Parse free-form bulk lines like "1. 3", "1) ③", "1: ㄱ", "1 3". */
  function parseBulkLines(text) {
    const out = [];
    for (const rawLine of String(text || '').split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;
      const m = line.match(/^(\d{1,3})\s*[.)::\]\-]?\s*(.*)$/);
      if (!m) continue;
      const number = parseInt(m[1], 10);
      if (!Number.isFinite(number) || number <= 0) continue;
      out.push({ number, answer: canonAnswer(m[2]) });
    }
    return out;
  }

  /* ------------------------------------------------------------------
   * API helper + toasts
   * ------------------------------------------------------------------ */
  async function api(path, { method = 'GET', body, formData } = {}) {
    const opts = { method, headers: {} };
    opts.headers['X-Device-User-Id'] = getDeviceUserId();
    if (formData) {
      opts.body = formData;
    } else if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch('/api' + path, opts);
    } catch {
      throw mkErr('서버에 연결할 수 없습니다. 로컬 서버가 실행 중인지 확인해 주세요.', 0);
    }
    if (!res.ok) {
      let detail = `요청이 실패했습니다 (${res.status}).`;
      try {
        const data = await res.json();
        if (data && typeof data.detail === 'string' && data.detail.trim()) detail = data.detail;
      } catch { /* non-JSON body */ }
      throw mkErr(detail, res.status);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function mkErr(message, status) {
    const err = new Error(message);
    err.status = status;
    return err;
  }

  const toastsEl = () => $('#toasts');

  function toast(msg, type = 'info', ms = 3400) {
    const region = toastsEl();
    if (!region) return;
    const el = document.createElement('div');
    el.className = `toast${type === 'success' ? ' toast-success' : type === 'error' ? ' toast-error' : ''}`;
    el.setAttribute('role', 'status');
    el.textContent = msg;
    region.appendChild(el);
    window.setTimeout(() => {
      el.classList.add('hide');
      window.setTimeout(() => el.remove(), 300);
    }, ms);
  }

  function setPending(btn, pending, busyLabel) {
    if (!btn) return;
    if (pending) {
      btn.dataset.label = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `${ic('refresh', 15)} ${esc(busyLabel || '처리 중…')}`;
    } else {
      btn.disabled = false;
      if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
    }
  }

  /* ------------------------------------------------------------------
   * App state
   * ------------------------------------------------------------------ */
  const state = {
    preview: null,   // last ExtractionPreview
    files: [],       // selected Files (1..N) in extract view
    onboarded: false // Gemini key confirmed available for this device
  };

  const HEADER_TYPE_NAMES = {
    day: 'Day', chapter: 'Chapter', unit: 'Unit',
    lesson: 'Lesson', step: 'Step'
  };
  const ISSUE_KIND_NAMES = {
    gap: '번호 누락', duplicate: '중복 번호',
    empty: '빈 답안', noise: '판독 불가'
  };
  const STATUS_NAMES = {
    correct: '정답', incorrect: '오답', unanswered: '미응답'
  };

  const view = document.getElementById('view');

  /* Hook re-bound by the quiz view; the static paste dialog calls it once. */
  let bulkApplyHook = null;

  /* ------------------------------------------------------------------
   * Shared render helpers
   * ------------------------------------------------------------------ */
  function showLoading(label = '불러오는 중…') {
    view.innerHTML = `
      <section class="page">
        <div class="loading" role="status">
          <div class="spinner"></div>${esc(label)}
        </div>
      </section>`;
  }

  function showErrorPage(msg, retry) {
    view.innerHTML = `
      <section class="page">
        <div class="card error-state">
          <h1 class="page-title">문제가 발생했습니다</h1>
          <p class="muted">${esc(msg)}</p>
          ${retry ? `<button class="btn btn-secondary" id="btn-retry-page">${ic('refresh')} 다시 시도</button>` : ''}
        </div>
      </section>`;
    const b = $('#btn-retry-page');
    if (b && retry) b.addEventListener('click', retry);
    focusTitle();
  }

  function focusTitle() {
    const h = $('h1', view);
    if (h) h.focus({ preventScroll: false });
  }

  const backLink = (href, label) =>
    `<a class="back-link" href="${esc(href)}">${ic('arrowLeft', 14)} ${esc(label)}</a>`;

  /* ------------------------------------------------------------------
   * Celebration -- confetti burst for a session's first-submission 100%.
   * One persistent canvas, lazily created as a <body> sibling of #view (like
   * #toasts) so router screen swaps (view.innerHTML = ...) never tear it
   * down mid-burst. Self-contained: no CDN/library, just Canvas 2D + rAF.
   * ------------------------------------------------------------------ */
  let celebrationCanvas = null;
  let celebrationRaf = null;

  function stopCelebration() {
    if (celebrationRaf != null) {
      cancelAnimationFrame(celebrationRaf);
      celebrationRaf = null;
    }
    if (celebrationCanvas) {
      const ctx = celebrationCanvas.getContext('2d');
      if (ctx) ctx.clearRect(0, 0, celebrationCanvas.width, celebrationCanvas.height);
    }
  }

  function celebrate() {
    // Canvas particles driven by rAF aren't CSS animation/transition, so the
    // project's global reduced-motion rule (app.css) can't clamp them --
    // this explicit check is what keeps that accessibility guarantee here.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    stopCelebration(); // no stacked runs if triggered again before finishing

    if (!celebrationCanvas) {
      celebrationCanvas = document.createElement('canvas');
      celebrationCanvas.className = 'celebration-canvas';
      document.body.appendChild(celebrationCanvas);
    }
    const canvas = celebrationCanvas;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const colors = ['--accent', '--green', '--amber', '--red']
      .map((name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim())
      .filter(Boolean);
    if (!colors.length) return;

    const w = window.innerWidth;
    const h = window.innerHeight;
    canvas.width = w;
    canvas.height = h;

    const DURATION_MS = 2800;
    const GRAVITY = 0.12;
    const particles = Array.from({ length: 130 }, () => ({
      x: Math.random() * w,
      y: -20 - Math.random() * h * 0.3,
      size: 5 + Math.random() * 5,
      color: colors[Math.floor(Math.random() * colors.length)],
      vx: (Math.random() - 0.5) * 3,
      vy: 2 + Math.random() * 2,
      rotation: Math.random() * 360,
      spin: (Math.random() - 0.5) * 14
    }));

    const startedAt = performance.now();
    const frame = (now) => {
      ctx.clearRect(0, 0, w, h);
      particles.forEach((p) => {
        p.vy += GRAVITY;
        p.x += p.vx;
        p.y += p.vy;
        p.rotation += p.spin;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate((p.rotation * Math.PI) / 180);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.size / 2, -p.size / 2, p.size, p.size * 0.6);
        ctx.restore();
      });
      if (now - startedAt < DURATION_MS) {
        celebrationRaf = requestAnimationFrame(frame);
      } else {
        celebrationRaf = null;
        ctx.clearRect(0, 0, w, h);
      }
    };
    celebrationRaf = requestAnimationFrame(frame);
  }

  /* ------------------------------------------------------------------
   * Router
   * ------------------------------------------------------------------ */
  function parseRoute() {
    const h = (location.hash || '#/').replace(/^#\/?/, '');
    const parts = h.split('/').filter(Boolean).map(decodeURIComponent);
    if (parts.length === 0) return { name: 'library' };
    if (parts[0] === 'new') {
      const wid = parts[1] ? parseInt(parts[1], 10) : NaN;
      return { name: 'extract', wid: Number.isInteger(wid) ? wid : null };
    }
    if (parts[0] === 'wb' && parts[1]) {
      const id = parseInt(parts[1], 10);
      if (Number.isInteger(id)) {
        if (parts[2] === 'extract-more') return { name: 'extract', wid: id };
        return { name: 'workbook', id };
      }
    }
    if (parts[0] === 'sec' && parts[1] && parts[2] === 'solve') {
      const id = parseInt(parts[1], 10);
      if (Number.isInteger(id)) return { name: 'solve', id };
    }
    if (parts[0] === 'attempt' && parts[1]) {
      const id = parseInt(parts[1], 10);
      if (Number.isInteger(id)) return { name: 'attempt', id };
    }
    if (parts[0] === 'session' && parts[1]) {
      const id = parseInt(parts[1], 10);
      if (Number.isInteger(id)) return { name: 'session', id };
    }
    return { name: 'library' };
  }

  let renderSeq = 0;

  async function render() {
    const seq = ++renderSeq;
    const route = parseRoute();
    showLoading();
    const guard = (fn) => fn().catch((err) => {
      if (seq !== renderSeq) return;
      console.error(err);
      showErrorPage(err.message || '알 수 없는 오류가 발생했습니다.', render);
      toast(err.message || '요청 처리 중 오류가 발생했습니다.', 'error');
    });
    switch (route.name) {
      case 'library':  await guard(viewLibrary); break;
      case 'extract':  await guard(() => viewExtract(route.wid)); break;
      case 'workbook': await guard(() => viewWorkbook(route.id)); break;
      case 'solve':    await guard(() => viewSolve(route.id)); break;
      case 'attempt':  await guard(() => viewAttempt(route.id)); break;
      case 'session':  await guard(() => viewSessionDetail(route.id)); break;
      default:         await guard(viewLibrary);
    }
  }

  /* ==================================================================
   * View: Library (#/)
   * ================================================================== */
  async function viewLibrary() {
    const books = await api('/workbooks');

    const cards = (books || []).map((b) => `
      <article class="book-card-wrap">
        <a class="book-card" href="#/wb/${Number(b.id)}" aria-label="${esc(b.title)} 열기">
          <h2 class="book-title">${esc(b.title)}</h2>
          <p class="book-meta">섹션 ${Number(b.section_count || 0)}개 · 문항 ${Number(b.problem_count || 0)}개</p>
          <div class="book-foot">
            <span class="muted" style="font-size:13px;">최근 성적</span>
            <span class="badge ${pctBadgeClass(b.latest_percent)}">${esc(pctText(b.latest_percent, '기록 없음'))}</span>
          </div>
        </a>
        <button class="icon-btn book-edit" data-rename-wb="${Number(b.id)}"
                data-title="${esc(b.title)}" aria-label="${esc(b.title)} 이름 바꾸기">
          ${ic('edit')}
        </button>
        <button class="icon-btn danger book-del" data-del-wb="${Number(b.id)}"
                data-title="${esc(b.title)}" aria-label="${esc(b.title)} 삭제">
          ${ic('trash')}
        </button>
      </article>`).join('');

    const emptyState = `
      <div class="empty-state">
        <h2>아직 워크북이 없어요</h2>
        <p>사진 한 장으로 정답지를 등록하면, 이후 채점은 30초 안에 끝납니다.</p>
        <ol class="steps">
          <li><span class="step-num">1</span><span>정답지 사진을 찍거나 텍스트를 붙여넣어 등록해요.</span></li>
          <li><span class="step-num">2</span><span>푼 문제 번호에 답만 빠르게 입력해요.</span></li>
          <li><span class="step-num">3</span><span>점수와 틀린 문제가 바로 정리돼요.</span></li>
        </ol>
        <div class="empty-actions">
          <button class="btn" id="btn-empty-create">${ic('plus')} 첫 워크북 만들기</button>
        </div>
      </div>`;

    view.innerHTML = `
      <section class="page" aria-label="워크북 목록">
        <div class="page-head">
          <div>
            <h1 class="page-title" tabindex="-1">내 워크북</h1>
            <p class="sub">정답지를 등록한 문제집이 여기에 모입니다.</p>
          </div>
          <span class="spacer"></span>
          <button class="btn" id="btn-new-workbook">${ic('plus')} 새 워크북</button>
        </div>
        ${(books && books.length)
          ? `<div class="grid-books">${cards}</div>`
          : emptyState}
      </section>`;

    $('#btn-new-workbook').addEventListener('click', openCreateDialog);
    const emptyBtn = $('#btn-empty-create');
    if (emptyBtn) emptyBtn.addEventListener('click', openCreateDialog);

    view.querySelectorAll('[data-del-wb]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const id = btn.getAttribute('data-del-wb');
        const title = btn.getAttribute('data-title') || '워크북';
        if (!window.confirm(`'${title}' 워크북을 삭제할까요?\n포함된 섹션과 채점 기록도 함께 삭제되며 되돌릴 수 없습니다.`)) return;
        setPending(btn, true);
        try {
          await api(`/workbooks/${id}`, { method: 'DELETE' });
          toast('워크북이 삭제되었습니다.', 'success');
          render();
        } catch (err) {
          setPending(btn, false);
          toast(err.message, 'error');
        }
      });
    });

    view.querySelectorAll('[data-rename-wb]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const id = btn.getAttribute('data-rename-wb');
        const title = btn.getAttribute('data-title') || '';
        openRenameDialog(id, title);
      });
    });

    focusTitle();
  }

  function openCreateDialog() {
    const dlg = $('#dlg-create');
    const form = $('#form-create');
    const inp = $('#inp-create-title');
    form.reset();
    dlg.showModal();
    inp.focus();
  }

  /* Shared rename dialog — opened from the library grid and the workbook
     detail header alike; the submit handler (below, with the other static
     dialog wiring) reads `renameTargetId` to know which workbook to PATCH. */
  let renameTargetId = null;

  function openRenameDialog(id, currentTitle) {
    const dlg = $('#dlg-rename');
    const form = $('#form-rename');
    const inp = $('#inp-rename-title');
    form.reset();
    inp.value = currentTitle || '';
    renameTargetId = id;
    dlg.showModal();
    inp.focus();
  }

  /* ==================================================================
   * View: Extract (#/new, #/new/:wid, #/wb/:id/extract-more)
   * ================================================================== */
  async function viewExtract(wid) {
    if (!wid) return renderExtractNoWorkbook();

    // Fresh screen, fresh selection — a leftover pick from a previous visit
    // (e.g. another workbook's extract screen) must never silently ride
    // along into this workbook's upload.
    state.files = [];

    let wbTitle = `워크북 #${wid}`;
    api(`/workbooks/${wid}`).then((wb) => {
      if (wb && wb.title) {
        wbTitle = wb.title;
        const t = $('#ext-wb-title');
        if (t) t.textContent = wb.title;
      }
    }).catch(() => { /* title is cosmetic */ });

    view.innerHTML = `
      <section class="page" aria-label="정답 등록">
        ${backLink('#/', '라이브러리로 돌아가기')}
        <div class="page-head">
          <div>
            <h1 class="page-title" tabindex="-1">정답 등록</h1>
            <p class="sub"><span id="ext-wb-title">${esc(wbTitle)}</span>에 저장됩니다.</p>
          </div>
        </div>

        <div class="card" id="ext-input-card">
          <div class="tabs" role="tablist" aria-label="정답 입력 방식">
            <button class="tab" role="tab" id="tab-photo" aria-controls="panel-photo" aria-selected="true">
              ${ic('image')} 사진 업로드
            </button>
            <button class="tab" role="tab" id="tab-paste" aria-controls="panel-paste" aria-selected="false">
              ${ic('clipboard')} 텍스트 붙여넣기
            </button>
          </div>

          <div class="tabpanel" id="panel-photo" role="tabpanel" aria-labelledby="tab-photo">
            <div class="banner banner-rec upload-guide-banner">
              ${ic('image', 15)}
              <span>인식률을 높이려면 <strong>정답 부분만 크롭</strong>해서 올려주세요.</span>
              <button type="button" class="btn btn-secondary btn-sm" id="btn-upload-guide"
                      style="margin-left:auto;">촬영 가이드</button>
            </div>
            <label class="dropzone" id="dropzone" for="inp-file">
              <span class="dz-icon">${ic('upload', 30)}</span>
              <strong>정답지 사진을 끌어다 놓거나 눌러서 선택</strong>
              <span class="dz-hint">JPG · PNG 지원 · 여러 장 선택 가능 · 정답 페이지를 또렷하게 찍어주세요</span>
              <div id="file-list" class="file-list" hidden></div>
              <input type="file" id="inp-file" accept="image/jpeg,image/png,image/jpg" multiple>
            </label>
            <div class="extract-actions">
              <button class="btn" id="btn-extract-photo" disabled>${ic('target')} 답안 추출하기</button>
            </div>
            <div id="photo-banner-slot"></div>
          </div>

          <div class="tabpanel" id="panel-paste" role="tabpanel" aria-labelledby="tab-paste" hidden>
            <div class="field" style="margin-top:0;">
              <label for="inp-raw-text">정답 텍스트</label>
              <textarea class="ta mono" id="inp-raw-text" rows="8"
                placeholder="예)&#10;1. 3&#10;2) ③&#10;3: ㄱ&#10;Day 02&#10;11. 4"></textarea>
              <p class="sub">메모장에 옮겨 둔 정답 목록을 그대로 붙여넣어도 됩니다.</p>
            </div>
            <div class="extract-actions">
              <button class="btn" id="btn-extract-text">${ic('target')} 답안 추출하기</button>
            </div>
          </div>
        </div>

        <div id="preview-host"></div>
      </section>`;

    const tabPhoto = $('#tab-photo');
    const tabPaste = $('#tab-paste');
    const panelPhoto = $('#panel-photo');
    const panelPaste = $('#panel-paste');

    function selectTab(which) {
      const photo = which === 'photo';
      tabPhoto.setAttribute('aria-selected', String(photo));
      tabPaste.setAttribute('aria-selected', String(!photo));
      panelPhoto.hidden = !photo;
      panelPaste.hidden = photo;
    }
    tabPhoto.addEventListener('click', () => selectTab('photo'));
    tabPaste.addEventListener('click', () => selectTab('paste'));

    const dropzone = $('#dropzone');
    const fileInput = $('#inp-file');
    const fileListEl = $('#file-list');
    const btnPhoto = $('#btn-extract-photo');
    const btnText = $('#btn-extract-text');
    const taText = $('#inp-raw-text');

    /* blob: object URLs, one per selected File — created lazily and reused
       across re-renders so accumulating/removing files never leaks URLs. */
    const fileUrls = new WeakMap();
    function urlFor(f) {
      let u = fileUrls.get(f);
      if (!u) { u = URL.createObjectURL(f); fileUrls.set(f, u); }
      return u;
    }

    $('#btn-upload-guide').addEventListener('click', () => openUploadGuide());
    let guideSeen = null;
    try { guideSeen = localStorage.getItem('ag_upload_guide_seen'); } catch { /* noop */ }
    if (!guideSeen) openUploadGuide(true);

    fileInput.addEventListener('change', () => {
      applyFiles(fileInput.files);
      fileInput.value = ''; // allow re-picking the same file after removal
    });

    ['dragenter', 'dragover'].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add('dragover'); }));
    ['dragleave', 'drop'].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove('dragover'); }));
    dropzone.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) applyFiles(dt.files);
    });

    /* Validate and append newly chosen/dropped files to the current
       selection (repeated drag-drops / picker rounds accumulate) rather
       than replacing it — the natural multi-photo upload pattern. */
    function applyFiles(fileList) {
      const incoming = Array.from(fileList || []);
      if (!incoming.length) return;
      let rejected = 0;
      incoming.forEach((f) => {
        if (/^image\/(jpeg|png)$/.test(f.type)) state.files.push(f);
        else rejected += 1;
      });
      if (rejected) toast('JPG 또는 PNG 이미지만 업로드할 수 있습니다.', 'error');
      renderFileList();
    }

    function renderFileList() {
      btnPhoto.disabled = state.files.length === 0;
      fileListEl.hidden = state.files.length === 0;
      fileListEl.innerHTML = state.files.map((f, idx) => `
        <div class="file-chip" data-idx="${idx}">
          <img class="thumb" src="${esc(urlFor(f))}" alt="">
          <span class="file-meta">${esc(f.name)}</span>
          <button type="button" class="file-chip-remove" data-remove-file="${idx}"
                  aria-label="${esc(f.name)} 제거">${ic('x', 13)}</button>
        </div>`).join('');
    }

    fileListEl.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-remove-file]');
      if (!btn) return;
      const idx = Number(btn.getAttribute('data-remove-file'));
      const [removed] = state.files.splice(idx, 1);
      if (removed) {
        const u = fileUrls.get(removed);
        if (u) { URL.revokeObjectURL(u); fileUrls.delete(removed); }
      }
      renderFileList();
    });

    btnPhoto.addEventListener('click', async () => {
      if (!state.files.length) return;
      const bannerSlot = $('#photo-banner-slot');
      bannerSlot.innerHTML = '';
      setPending(btnPhoto, true, '추출 중…');
      try {
        const fd = new FormData();
        state.files.forEach((f) => fd.append('file', f));
        const preview = await api('/extract', { method: 'POST', formData: fd });
        state.preview = preview;
        renderPreview(preview, wid);
      } catch (err) {
        handleExtractError(err, bannerSlot);
      } finally {
        setPending(btnPhoto, false);
      }
    });

    btnText.addEventListener('click', async () => {
      const text = taText.value.trim();
      if (!text) {
        toast('정답 텍스트를 입력하거나 붙여넣어 주세요.', 'error');
        taText.focus();
        return;
      }
      setPending(btnText, true, '추출 중…');
      try {
        const preview = await api('/extract-text', { method: 'POST', body: { raw_text: text } });
        state.preview = preview;
        renderPreview(preview, wid);
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        setPending(btnText, false);
      }
    });

    focusTitle();
  }

  function handleExtractError(err, bannerSlot) {
    if (err.status === 503) {
      toast('Gemini Vision을 사용할 수 없습니다. API 키를 등록하거나 텍스트 붙여넣기를 이용해 주세요.', 'error', 5200);
      if (bannerSlot) {
        bannerSlot.innerHTML = `
          <div class="banner banner-warn" style="margin-top:12px;">
            ${ic('alert')}
            <span>Gemini API 키가 등록되지 않아 사진 인식을 사용할 수 없습니다.
            <button type="button" class="btn btn-sm" id="btn-open-apikey"
                    style="margin:6px 4px 0 0;">API 키 입력하기</button>
            또는 <code>GEMINI_API_KEY</code> 환경 변수를 설정한 뒤 재시작하세요.
            키가 없어도 <strong>[텍스트 붙여넣기]</strong> 탭으로 정답지를 등록할 수 있어요.</span>
          </div>`;
        const openBtn = $('#btn-open-apikey');
        if (openBtn) openBtn.addEventListener('click', openApiKeyDialog);
      }
      const pasteTab = $('#tab-paste');
      if (pasteTab) pasteTab.click();
      return;
    }
    if (err.status === 502) {
      toast(`${err.message}`, 'error', 5200);
      return;
    }
    if (err.status === 415) {
      toast(`${err.message} JPG/PNG 이미지를 사용해 주세요.`, 'error');
      return;
    }
    if (err.status === 400 || err.status === 422) {
      toast(`${err.message} 텍스트 붙여넣기 탭도 사용해 보세요.`, 'error', 4800);
      return;
    }
    toast(err.message, 'error');
  }

  function renderExtractNoWorkbook() {
    view.innerHTML = `
      <section class="page" aria-label="정답 등록 준비">
        ${backLink('#/', '라이브러리로 돌아가기')}
        <div class="page-head">
          <div>
            <h1 class="page-title" tabindex="-1">정답 등록</h1>
            <p class="sub">먼저 정답을 저장할 워크북을 만들어 주세요.</p>
          </div>
        </div>
        <div class="card" style="max-width:520px;">
          <form id="form-inline-create" novalidate>
            <div class="field" style="margin-top:0;">
              <label for="inp-inline-title">워크북 제목</label>
              <input class="input" id="inp-inline-title" type="text" maxlength="60"
                     placeholder="예) 쎈 미적분 3-2" autocomplete="off" required>
            </div>
            <div class="extract-actions">
              <button type="submit" class="btn" id="btn-inline-create">${ic('plus')} 워크북 만들고 계속하기</button>
              <button type="button" class="btn btn-ghost" id="btn-open-dlg">직접 선택…</button>
            </div>
          </form>
        </div>
      </section>`;
    const form = $('#form-inline-create');
    const inp = $('#inp-inline-title');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const title = inp.value.trim();
      if (!title) {
        toast('워크북 제목을 입력해 주세요.', 'error');
        inp.focus();
        return;
      }
      setPending($('#btn-inline-create'), true);
      try {
        const wb = await api('/workbooks', { method: 'POST', body: { title } });
        toast(`'${wb.title}' 워크북이 생성되었습니다.`, 'success');
        location.hash = `#/new/${wb.id}`;
      } catch (err) {
        setPending($('#btn-inline-create'), false);
        toast(err.message, 'error');
      }
    });
    $('#btn-open-dlg').addEventListener('click', openCreateDialog);
    inp.focus();
    focusTitle();
  }

  /* ---- Extraction preview (editable table + structure picker) ------- */

  function buildStructureOptions(rec) {
    const list = [];
    list.push({
      structure: rec.structure,
      header_type: rec.header_type != null ? rec.header_type : null,
      chunk_size: rec.chunk_size != null ? rec.chunk_size : null,
      label: null,
      recommended: true
    });
    (rec.alternatives || []).forEach((alt) => {
      list.push({
        structure: alt.structure,
        header_type: null,
        chunk_size: alt.chunk_size != null ? alt.chunk_size : null,
        label: alt.label,
        recommended: false
      });
    });
    // Fallbacks so the picker is never a dead-end.
    if (!list.some((o) => o.structure === 'chunks' && o.chunk_size === 5)) {
      list.push({ structure: 'chunks', header_type: null, chunk_size: 5, label: '5문제씩 나누기', recommended: false });
    }
    [10, 20].forEach((cs) => {
      if (!list.some((o) => o.structure === 'chunks' && o.chunk_size === cs)) {
        list.push({ structure: 'chunks', header_type: null, chunk_size: cs, label: `${cs}문제씩 나누기`, recommended: false });
      }
    });
    if (!list.some((o) => o.structure === 'chunks' && o.chunk_size === 0)) {
      list.push({ structure: 'chunks', header_type: null, chunk_size: 0, label: '하나로 묶기', recommended: false });
    }
    list.forEach((o) => {
      if (o.label) return;
      if (o.structure === 'headers') {
        const name = HEADER_TYPE_NAMES[o.header_type] || '헤더';
        o.label = `${name} 단위로 나누기`;
      } else if (o.chunk_size === 0) {
        o.label = '하나로 묶기';
      } else {
        o.label = `${o.chunk_size}문제씩 나누기`;
      }
    });
    return list;
  }

  function optValue(o) {
    return o.structure === 'headers' ? `h:${o.header_type || ''}` : `c:${o.chunk_size}`;
  }

  function renderPreview(preview, wid) {
    const host = $('#preview-host');
    if (!host) return;

    /* Row state is the single source of truth; the DOM is regenerated from it
       whenever the segmentation mode changes so sub-headings stay in sync. */
    const rows = (preview.entries || []).map((e) => ({
      number: Number(e.number),
      answer: e.answer != null ? String(e.answer) : '',
      line: Number(e.line || 0)
    }));
    if (!rows.length) rows.push({ number: 1, answer: '', line: 0 });

    const issues = preview.issues || [];
    const issueHtml = issues.length
      ? `<section class="card issues" aria-label="감지된 경고">
          <h3>${ic('alert', 14)} 확인이 필요한 항목 ${issues.length}개</h3>
          <ul class="issue-list">
            ${issues.map((it) => `
              <li class="issue">
                <span class="kind-badge k-${esc(it.kind)}">${esc(ISSUE_KIND_NAMES[it.kind] || it.kind)}</span>
                <span>${esc(it.message)}</span>
              </li>`).join('')}
          </ul>
        </section>`
      : '';

    const rec = preview.recommendation || {};
    const options = buildStructureOptions(rec);
    const recLabel = (options[0] && options[0].label) || '자동 구성';
    const confPct = rec.confidence != null ? Math.round(Number(rec.confidence) * 100) : null;
    const engineName = preview.engine === 'paste' ? '붙여넣은 텍스트' : `Gemini Vision (${preview.model || preview.engine || 'vision'})`;

    host.innerHTML = `
      <section class="card" style="margin-top:14px;" aria-label="추출 결과 검토">
        <div class="preview-head">
          <h2 style="font-size:17px;font-weight:800;">추출 결과 검토</h2>
          <span class="engine-chip">${esc(engineName)}</span>
          <span class="spacer"></span>
          <span class="muted" style="font-size:13px;">문항 ${rows.length}개</span>
        </div>

        <div class="banner banner-rec">
          ${ic('check')}
          <span>
            <strong>추천 구성: ${esc(recLabel)}</strong>${confPct != null ? `<span class="rec-conf">신뢰도 ${confPct}%</span>` : ''}
            <br>${esc(rec.rationale || '')}
          </span>
        </div>

        ${issueHtml}

        <div class="table-wrap">
          <table class="ext-table" id="ext-table">
            <thead>
              <tr>
                <th scope="col" style="width:90px;">번호</th>
                <th scope="col">정답</th>
                <th scope="col" style="width:44px;"><span class="sr-only">행 삭제</span></th>
              </tr>
            </thead><!-- segment tbodies injected here -->
          </table>
        </div>
        <div class="add-row-wrap">
          <button class="btn btn-secondary btn-sm" id="btn-add-row">${ic('plus', 14)} 행 추가</button>
        </div>

        <hr class="divider">

        <div class="struct-row">
          <label for="sel-structure"><strong>섹션 나누기 방식</strong></label>
          <select class="input" id="sel-structure">
            ${options.map((o, i) => `
              <option value="${esc(optValue(o))}" data-idx="${i}" ${i === 0 ? 'selected' : ''}>
                ${esc(o.label)}${o.recommended ? ' (추천)' : ''}
              </option>`).join('')}
          </select>
          <p class="sub" id="struct-desc"></p>
        </div>

        <div class="preview-footer">
          <button class="btn" id="btn-save-import">${ic('check')} 이대로 저장하기</button>
          <button class="btn btn-ghost" id="btn-reset-preview">다시 추출하기</button>
        </div>
      </section>`;

    const table = $('#ext-table');

    function selectedOpt() {
      return options[Number($('#sel-structure').selectedOptions[0]?.dataset.idx || 0)];
    }

    /* --- segmentation-aware grouping for the preview table -------------- */
    function computeGroups() {
      const opt = selectedOpt();
      if (!opt) return [{ label: '전체', items: rows.map((r, i) => i) }];

      if (opt.structure === 'headers' && (preview.headers || []).length) {
        const hs = [...preview.headers].sort((a, b) => a.line - b.line);
        const buckets = hs.map((h) => ({ label: h.label, type: h.type, items: [] }));
        const orphan = { label: '머리글 없음', type: '', items: [] };
        rows.forEach((r, i) => {
          let gid = -1;
          hs.forEach((h, hi) => { if (r.line >= h.line) gid = hi; });
          if (gid < 0) orphan.items.push(i);
          else buckets[gid].items.push(i);
        });
        const groups = [];
        if (orphan.items.length) groups.push(orphan);
        groups.push(...buckets.filter((b) => b.items.length));
        return groups.length ? groups : [{ label: '전체', items: rows.map((r2, i2) => i2) }];
      }

      if (!opt.chunk_size) {
        return [{ label: '하나로 묶기(전체)', items: rows.map((r, i) => i) }];
      }

      const numbered = rows
        .map((r, i) => ({ r, i }))
        .filter(({ r }) => Number.isFinite(parseInt(r.number, 10)) && parseInt(r.number, 10) > 0)
        .sort((a, b) => parseInt(a.r.number, 10) - parseInt(b.r.number, 10));
      const unnumbered = rows.map((r, i) => ({ r, i })).filter(({ r }) => !numbered.some(({ i: j }) => j === i));
      const groups = [];
      for (let s = 0; s < numbered.length; s += opt.chunk_size) {
        const slice = numbered.slice(s, s + opt.chunk_size);
        const lo = parseInt(slice[0].r.number, 10);
        const hi = parseInt(slice[slice.length - 1].r.number, 10);
        groups.push({ label: lo === hi ? `${lo}` : `${lo}~${hi}`, items: slice.map(({ i }) => i) });
      }
      if (unnumbered.length) {
        groups.push({ label: '번호 미입력', items: unnumbered.map(({ i }) => i) });
      }
      return groups.length ? groups : [{ label: '전체', items: rows.map((r, i) => i) }];
    }

    function rowHtml(idx) {
      const r = rows[idx];
      return `
        <tr data-idx="${idx}">
          <td><input class="input num-input" type="number" min="1" max="999"
                     value="${esc(String(r.number ?? ''))}" aria-label="문항 번호" data-field="number"></td>
          <td><input class="input ans-input" type="text" maxlength="40"
                     value="${esc(r.answer || '')}" aria-label="정답" data-field="answer"></td>
          <td><button class="icon-btn danger" data-del-row aria-label="행 삭제">${ic('trash')}</button></td>
        </tr>`;
    }

    function renderGroups() {
      const groups = computeGroups();
      table.innerHTML = `
        <thead>
          <tr>
            <th scope="col" style="width:90px;">번호</th>
            <th scope="col">정답</th>
            <th scope="col" style="width:44px;"><span class="sr-only">행 삭제</span></th>
          </tr>
        </thead>
        ${groups.map((g, gi) => `
          <tbody class="seg-group" data-gid="${gi}">
            <tr class="group-head">
              <th colspan="3" scope="colgroup">
                <span class="gh-label">[${esc(g.label)}]</span>
                ${g.type ? `<span class="gh-type">${esc(HEADER_TYPE_NAMES[g.type] || '')}</span>` : ''}
                <span class="gh-count">${g.items.length}문항</span>
              </th>
            </tr>
            ${g.items.map(rowHtml).join('')}
          </tbody>`).join('')}`;
    }

    renderGroups();

    /* delegated edits keep `rows` in sync across re-renders */
    table.addEventListener('input', (e) => {
      const tr = e.target.closest('tr[data-idx]');
      if (!tr || !e.target.dataset.field) return;
      const idx = Number(tr.dataset.idx);
      rows[idx][e.target.dataset.field] = e.target.value;
    });
    table.addEventListener('click', (e) => {
      const del = e.target.closest('[data-del-row]');
      if (!del) return;
      const idx = Number(del.closest('tr').dataset.idx);
      rows.splice(idx, 1);
      renderGroups();
    });

    $('#btn-add-row').addEventListener('click', () => {
      const nums = rows
        .map((r) => parseInt(r.number, 10))
        .filter((n) => Number.isFinite(n) && n > 0);
      const maxLine = rows.reduce((m, r) => Math.max(m, Number(r.line) || 0), 0);
      rows.push({ number: nums.length ? Math.max(...nums) + 1 : 1, answer: '', line: maxLine + 1 });
      renderGroups();
      const inputs = table.querySelectorAll('.ans-input');
      if (inputs.length) inputs[inputs.length - 1].focus();
    });

    const selStructure = $('#sel-structure');
    const structDesc = $('#struct-desc');
    function describeSelection() {
      const opt = options[Number(selStructure.selectedOptions[0]?.dataset.idx || 0)];
      if (!opt) return;
      structDesc.textContent = opt.structure === 'headers'
        ? '감지된 헤더(Day·Chapter 등)를 기준으로 섹션을 나눕니다.'
        : opt.chunk_size === 0
          ? '모든 문항을 하나의 섹션으로 저장합니다.'
          : `번호 순서대로 ${opt.chunk_size}문제씩 섹션을 나눕니다.`;
      renderGroups(); // instant sub-heading preview on mode switch
    }
    selStructure.addEventListener('change', describeSelection);
    describeSelection();

    $('#btn-reset-preview').addEventListener('click', () => {
      state.preview = null;
      host.innerHTML = '';
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    /* --- save flow: conflict check -> resolution modal -> import -------- */
    function buildPayload(cleaned, opt) {
      const payload = {
        structure: opt.structure,
        entries: cleaned.map((e2) => ({
          number: e2.number, answer: e2.answer, line: e2.line
        }))
      };
      if (opt.structure === 'headers') {
        payload.header_type = opt.header_type || (rec.header_type || 'day');
        payload.headers = (preview.headers || []).map((h) => ({
          type: h.type, label: h.label, index: h.index, line: h.line
        }));
      } else {
        payload.chunk_size = opt.chunk_size;
      }
      return payload;
    }


    $('#btn-save-import').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const cleaned = [];
      let skippedBad = 0;
      rows.forEach((r) => {
        const aRaw = String(r.answer || '').trim();
        const n = parseInt(r.number, 10);
        if (!aRaw) return;
        if (!Number.isFinite(n) || n <= 0) { skippedBad += 1; return; }
        cleaned.push({ number: n, answer: aRaw, line: Number(r.line || 0) });
      });
      if (skippedBad) toast(`번호가 올바르지 않은 ${skippedBad}행은 건너뛰었어요.`, 'info');
      if (!cleaned.length) {
        toast('저장할 정답이 없습니다. 최소 한 행이라도 채워 주세요.', 'error');
        return;
      }
      // NOTE: do NOT de-duplicate by question number here. Numbers restart at 1
      // in every Day/Chapter — sections are scoped server-side.
      const opt = selectedOpt();
      if (!opt) return;
      const payload = buildPayload(cleaned, opt);

      setPending(btn, true, '확인 중…');
      let conflicts = [];
      try {
        const res = await api(`/workbooks/${wid}/sections/conflicts`, { method: 'POST', body: payload });
        conflicts = res.conflicts || [];
      } catch { /* conflict check is best-effort */ }
      setPending(btn, false);

      if (conflicts.length) openConflictModal(conflicts, payload, btn, wid);
      else doImport(btn, wid, payload);
    });

    host.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ---- Duplicate-conflict resolution modal --------------------------- */
  let conflictCtx = null; // { conflicts, payload, saveBtn, wid }

  async function doImport(btn, wid, payload) {
    setPending(btn, true, '저장 중…');
    try {
      const res = await api(
        `/workbooks/${wid}/sections/import`, { method: 'POST', body: payload }
      );
      const count = res && res.sections ? res.sections.length : '?';
      toast(`섹션 ${count}개가 저장되었습니다.`, 'success');
      location.hash = `#/wb/${wid}`;
    } catch (err) {
      setPending(btn, false);
      toast(err.message, 'error');
    }
  }

  function openConflictModal(conflicts, payload, saveBtn, wid) {
    conflictCtx = { conflicts, payload, saveBtn, wid };
    const list = $('#conflict-list');
    list.innerHTML = conflicts.map((c, i) => {
      const exN = c.existing_section.numbers.length;
      const inN = c.incoming_numbers.length;
      const ov = (c.overlapping_numbers || []).slice(0, 10).join(', ');
      return `
        <div class="conflict-item">
          <div class="conflict-pair" aria-label="충돌 ${i + 1}">
            <span class="ver ver-old">기존 · ${esc(c.existing_section.label)}
              <small>${exN}문항</small></span>
            <span class="vs">${ic('refresh', 13)}</span>
            <span class="ver ver-new">신규 · ${esc(c.incoming_label)}
              <small>${inN}문항</small></span>
          </div>
          <p class="muted conflict-ov">겹침 문항: ${ov ? esc(ov) : '번호 범위 없음(동일 이름)'}
            ${(c.overlapping_numbers || []).length > 10 ? ' 외 …' : ''}</p>
          <div class="res-options" role="radiogroup" aria-label="${esc(c.incoming_label)} 처리 방법">
            <label><input type="radio" name="res-${i}" value="overwrite" checked> 덮어쓰기</label>
            <label><input type="radio" name="res-${i}" value="keep_both"> 둘 다 유지(이름 변경)</label>
            <label><input type="radio" name="res-${i}" value="skip_incoming"> 새 버전 폐기</label>
          </div>
        </div>`;
    }).join('');
    $('#dlg-conflict').showModal();
  }

  $('#form-conflict').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!conflictCtx) return;
    const { conflicts, payload, saveBtn, wid } = conflictCtx;
    const resolutions = conflicts.map((c, i) => {
      const action = document.querySelector(`input[name="res-${i}"]:checked`)?.value || 'overwrite';
      return {
        incoming_label: c.incoming_label,
        action,
        target_section_id:
          action === 'overwrite' ? c.existing_section.id : null
      };
    });
    $('#dlg-conflict').close();
    await doImport(saveBtn, wid, { ...payload, resolutions });
  });

  /* ==================================================================
   * View: Workbook detail (#/wb/:id)
   * ================================================================== */
  async function viewWorkbook(id) {
    const [wb, stats] = await Promise.all([
      api(`/workbooks/${id}`),
      api(`/workbooks/${id}/stats`).catch(() => null)
    ]);

    const sections = wb.sections || [];
    const topMissed = (stats && stats.top_missed) || [];

    const missedHtml = topMissed.length
      ? `<ul class="missed-list">
          ${topMissed.map((m, i) => `
            <li>
              <button type="button" class="missed-item" data-missed-idx="${i}">
                <span class="missed-num">${Number(m.number)}번</span>
                <span>${Number(m.count)}회 틀림<span class="muted"> · ${esc(m.section_label || '')}</span></span>
              </button>
            </li>`).join('')}
        </ul>`
      : `<p class="muted" style="font-size:14px;">아직 채점 기록이 없습니다. 섹션에서 채점을 시작해 보세요.</p>`;

    const sectionCards = sections.map((s) => {
      const sid = Number(s.id);
      // A section with an open session can't be re-entered via a plain
      // "채점 시작" any more — that would silently start a second, competing
      // session (blocked server-side by idx_sessions_one_open) instead of
      // continuing the one already in progress. Route into it explicitly.
      const actionsHtml = s.open_session_id
        ? `<div class="sec-actions">
            <a class="btn" href="#/sec/${sid}/solve">${ic('play')} 이어서 풀기</a>
            <button type="button" class="btn btn-secondary" data-finish-session="${Number(s.open_session_id)}">
              ${ic('refresh')} 채점 끝내고 새로 채점하기
            </button>
          </div>`
        : `<a class="btn" href="#/sec/${sid}/solve">${ic('play')} 채점 시작</a>`;
      return `
      <article class="card section-card" data-sid="${sid}">
        <div class="sec-head">
          <h3 class="sec-label">${esc(s.label)}</h3>
          <button class="icon-btn danger sec-del" data-del-sec="${sid}"
                  data-title="${esc(s.label)}" aria-label="${esc(s.label)} 세션 삭제">
            ${ic('trash')}
          </button>
        </div>
        <div class="sec-stats">
          <span>문항 <b>${Number(s.problem_count || 0)}</b></span>
          <span>응시 <b>${Number(s.session_count || 0)}</b>회</span>
        </div>
        <div class="sec-stats">
          <span>최근 <span class="badge ${pctBadgeClass(s.latest_percent)}">${esc(pctText(s.latest_percent, '–'))}</span></span>
          <span>최고 <span class="badge ${pctBadgeClass(s.best_percent)}">${esc(pctText(s.best_percent, '–'))}</span></span>
        </div>
        ${actionsHtml}
        <button class="btn btn-ghost btn-sm expand-btn" data-expand="${sid}"
                aria-expanded="false" aria-controls="attempts-${sid}">
          ${ic('list', 14)} 응시 기록 보기
        </button>
        <div class="attempts-panel" id="attempts-${sid}" hidden></div>
      </article>`;
    }).join('');

    const sectionEmpty = `
      <div class="empty-state" style="padding:36px 16px;">
        <h2>등록된 섹션이 없어요</h2>
        <p>상단의 "정답 추가 등록" 버튼으로 정답지를 먼저 등록해 주세요.</p>
      </div>`;

    view.innerHTML = `
      <section class="page" aria-label="워크북 상세">
        ${backLink('#/', '라이브러리로 돌아가기')}
        <div class="card detail-head" style="margin-bottom:14px;">
          <div style="min-width:0;">
            <h1 class="detail-title" tabindex="-1">${esc(wb.title)}</h1>
            <p class="meta-line">섹션 ${Number(wb.section_count || 0)}개 · 문항 ${Number(wb.problem_count || 0)}개
               · 최근 성적 ${esc(pctText(wb.latest_percent, '기록 없음'))}</p>
          </div>
          <div class="head-actions">
            <a class="btn btn-secondary" href="#/wb/${id}/extract-more">${ic('plus')} 정답 추가 등록</a>
            <button class="btn btn-secondary" id="btn-rename-wb">${ic('edit')} 이름 바꾸기</button>
            <button class="btn btn-danger" id="btn-del-wb">${ic('trash')} 삭제</button>
          </div>
        </div>

        <div class="card" id="wb-tabs-card">
          <div class="tabs" role="tablist" aria-label="워크북 보기">
            <button class="tab" role="tab" id="tab-sections" aria-controls="panel-sections" aria-selected="true">
              ${ic('list', 15)} 섹션 ${sections.length}개
            </button>
            <button class="tab" role="tab" id="tab-missed" aria-controls="panel-missed" aria-selected="false">
              ${ic('target', 15)} 자주 틀린 문제
            </button>
          </div>

          <div class="tabpanel" id="panel-sections" role="tabpanel" aria-labelledby="tab-sections">
            ${sections.length
              ? `<div class="sections-grid">${sectionCards}</div>`
              : sectionEmpty}
          </div>

          <div class="tabpanel" id="panel-missed" role="tabpanel" aria-labelledby="tab-missed" hidden>
            ${missedHtml}
          </div>
        </div>
      </section>`;

    const tabSections = $('#tab-sections');
    const tabMissed = $('#tab-missed');
    const panelSections = $('#panel-sections');
    const panelMissed = $('#panel-missed');
    function selectWbTab(which) {
      const sectionsOn = which === 'sections';
      tabSections.setAttribute('aria-selected', String(sectionsOn));
      tabMissed.setAttribute('aria-selected', String(!sectionsOn));
      panelSections.hidden = !sectionsOn;
      panelMissed.hidden = sectionsOn;
    }
    tabSections.addEventListener('click', () => selectWbTab('sections'));
    tabMissed.addEventListener('click', () => selectWbTab('missed'));

    $('#btn-del-wb').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      if (!window.confirm(`'${wb.title}' 워크북을 삭제할까요?\n포함된 섹션과 채점 기록도 함께 삭제되며 되돌릴 수 없습니다.`)) return;
      setPending(btn, true);
      try {
        await api(`/workbooks/${id}`, { method: 'DELETE' });
        toast('워크북이 삭제되었습니다.', 'success');
        location.hash = '#/';
      } catch (err) {
        setPending(btn, false);
        toast(err.message, 'error');
      }
    });

    $('#btn-rename-wb').addEventListener('click', () => openRenameDialog(id, wb.title));

    /* --- per-session delete: removes only that section + its records --- */
    view.querySelectorAll('[data-del-sec]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const sid = btn.getAttribute('data-del-sec');
        const title = btn.getAttribute('data-title') || '세션';
        if (!window.confirm(`'${title}' 세션(섹션)을 삭제할까요?\n이 세션의 정답과 채점 기록만 삭제되며 다른 데이터는 영향을 받지 않습니다.`)) return;
        setPending(btn, true);
        try {
          await api(`/sections/${sid}`, { method: 'DELETE' });
          toast(`'${title}' 세션이 삭제되었습니다.`, 'success');
          render();
        } catch (err) {
          setPending(btn, false);
          toast(err.message, 'error');
        }
      });
    });

    /* --- "채점 끝내고 새로 채점하기": finish the open session in place, then
       jump into the same quiz screen, which now starts a blank first
       submission since no session is open any more --- */
    view.querySelectorAll('[data-finish-session]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const openSessionId = btn.getAttribute('data-finish-session');
        const sid = btn.closest('.section-card').getAttribute('data-sid');
        setPending(btn, true, '처리 중…');
        try {
          await api(`/sessions/${openSessionId}/finish`, { method: 'POST' });
          toast('채점을 끝냈습니다.', 'success');
          location.hash = `#/sec/${sid}/solve`;
        } catch (err) {
          setPending(btn, false);
          toast(err.message, 'error');
        }
      });
    });

    view.querySelectorAll('[data-expand]').forEach((btn) => {
      const sid = btn.getAttribute('data-expand');
      const panel = $(`#attempts-${sid}`);
      btn.addEventListener('click', async () => {
        const willOpen = panel.hidden;
        btn.setAttribute('aria-expanded', String(willOpen));
        panel.hidden = !willOpen;
        btn.lastChild.textContent = willOpen ? ' 응시 기록 접기' : ' 응시 기록 보기';
        if (!willOpen || panel.dataset.loaded === '1') return;
        panel.innerHTML = '<p class="muted" style="font-size:13px;">불러오는 중…</p>';
        try {
          // One row per FINISHED session -- one history entry per session,
          // not per submission. An open session never appears here (it's
          // surfaced by the "이어서 풀기" resume button instead), and each
          // row shows the session's frozen first-submission score, which is
          // what actually counts toward this section's history/aggregates.
          const sessions = await api(`/sections/${sid}/sessions`);
          panel.dataset.loaded = '1';
          if (!sessions || !sessions.length) {
            panel.innerHTML = '<p class="muted" style="font-size:13px;">아직 응시 기록이 없습니다.</p>';
            return;
          }
          panel.innerHTML = sessions.map((s) => `
            <div class="attempt-row">
              <span class="att-date">${esc(fmtDate(s.finished_at))}</span>
              <span class="att-score">${Number(s.first_score)}/${Number(s.first_total)}</span>
              <span class="spacer"></span>
              <a href="#/session/${Number(s.session_id)}" class="badge ${pctBadgeClass(s.first_percent)}"
                 style="text-decoration:none;">${esc(pctText(s.first_percent))}</a>
            </div>`).join('');
        } catch (err) {
          panel.innerHTML = `<p class="muted" style="font-size:13px;">${esc(err.message)}</p>`;
        }
      });
    });

    view.querySelectorAll('[data-missed-idx]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const m = topMissed[Number(btn.getAttribute('data-missed-idx'))];
        if (m) openMissedDetailModal(m);
      });
    });

    focusTitle();
  }

  /* "자주 틀린 문제" 항목 클릭 시 소속 워크북/섹션과 실제 오답 내용을 보여주는
     상세 모달 -- given/expected는 이 문제가 (마지막으로) 틀렸던 가장 최근
     채점의 스냅샷이다. given이 빈 값이면 미응답이었다는 뜻. */
  function openMissedDetailModal(m) {
    const unanswered = !m.given;
    $('#missed-detail-body').innerHTML = `
      <p class="missed-detail-headline">
        <span class="missed-num">${Number(m.number)}번</span>
        <span>${Number(m.count)}회 틀림</span>
      </p>
      <dl class="missed-detail-meta">
        <div><dt>워크북</dt><dd>${esc(m.workbook_title || '')}</dd></div>
        <div><dt>섹션</dt><dd>${esc(m.section_label || '')}</dd></div>
      </dl>
      <div class="wrong-card ${unanswered ? 'unanswered' : ''}" style="margin-top:14px;">
        <span class="wc-num">${Number(m.number)}</span>
        <span class="wc-body">
          <span class="wc-line">내 답 <b>${unanswered ? '(미응답)' : esc(m.given)}</b> → 정답 <b>${esc(m.expected)}</b></span>
        </span>
        <span class="wc-status">${unanswered ? esc(STATUS_NAMES.unanswered) : esc(STATUS_NAMES.incorrect)}</span>
      </div>`;
    $('#missed-detail-wb-link').href = `#/wb/${Number(m.workbook_id)}`;
    $('#missed-detail-solve-link').href = `#/sec/${Number(m.section_id)}/solve`;
    $('#dlg-missed-detail').showModal();
  }

  /* ==================================================================
   * View: Quiz (#/sec/:id/solve)
   * ================================================================== */
  async function viewSolve(sid) {
    const sec = await api(`/sections/${sid}`);

    // Server-driven retry/resume: no client-side bookkeeping. A 200 here
    // means a session for this section is already open; a 404 means this
    // is a fresh first submission.
    let openSession = null;
    try {
      openSession = await api(`/sections/${sid}/session`);
    } catch (err) {
      if (err.status !== 404) throw err;
    }

    const resumeMode = openSession !== null;
    const latest = resumeMode ? openSession.latest_attempt : null;
    // "Which numbers still need retrying" and "what was answered last time"
    // both come straight from the open session's latest submission snapshot.
    const remainingResults = resumeMode
      ? (latest?.results || [])
          .filter((r) => r.status !== 'correct')
          .sort((a, b) => Number(a.number) - Number(b.number))
      : [];
    const givenByNumber = new Map(
      remainingResults.map((r) => [Number(r.number), r.given || ''])
    );
    const submissionCount = resumeMode ? (openSession.submission_count || 0) : 0;

    const numbers = resumeMode
      ? remainingResults.map((r) => Number(r.number))
      : (sec.numbers || []).map(Number).sort((a, b) => a - b);
    // Retried everything correct and left without finishing: no numbers left
    // to show, so render a finish-the-session state instead of an empty grid.
    const allCaughtUp = resumeMode && numbers.length === 0;

    const answeredOnlyDefault = storage.localGet('ag_answered_only') === '1';

    const inputsHtml = numbers.map((n) => {
      const hintHtml = resumeMode
        ? `<span class="ans-hint" id="ans-hint-${n}">이전 답: ${
            givenByNumber.get(n) ? esc(givenByNumber.get(n)) : '(미응답)'
          }</span>`
        : '';
      return `
      <li class="ans-cell">
        <span class="ans-num" id="ans-num-${n}">${n}번</span>
        <input class="ans-input" type="text" inputmode="text" autocomplete="off"
               data-number="${n}" aria-labelledby="ans-num-${n}${resumeMode ? ` ans-hint-${n}` : ''}" maxlength="30">
        ${hintHtml}
      </li>`;
    }).join('');

    view.innerHTML = `
      <section class="page" aria-label="채점 세션">
        ${backLink(`#/wb/${sec.workbook_id}`, '워크북으로 돌아가기')}
        <div class="page-head">
          <div>
            <h1 class="page-title" tabindex="-1">${esc(sec.label || '채점')}</h1>
            <p class="sub">${esc(sec.workbook_title || '')} · ${allCaughtUp ? '재도전 문항 모두 정답' : `문항 ${numbers.length}개`}</p>
          </div>
        </div>

        ${resumeMode && !allCaughtUp ? `
        <div class="banner banner-rec quiz-banner">
          ${ic('refresh')}
          <span><strong>이어서 채점 중</strong> — 지금까지 ${submissionCount}번 제출했고, ${numbers.length}개 문항이 남았습니다.</span>
        </div>` : ''}

        ${allCaughtUp ? `
        <div class="card">
          <div class="all-correct">${ic('check', 18)} 재도전한 문항을 모두 맞혔습니다!</div>
          <p class="sub" style="margin-top:12px;">지금까지 ${submissionCount}번 제출했습니다. 채점을 끝내면 이번 세션의 결과가 기록에 저장됩니다.</p>
          <div class="result-actions">
            <button type="button" class="btn" id="btn-finish-session">${ic('check')} 채점 끝내기</button>
            <a class="btn btn-secondary" href="#/wb/${sec.workbook_id}">${ic('list')} 목록으로</a>
          </div>
        </div>` : `
        <form id="quiz-form" novalidate>
          <label style="display:block; margin-bottom:14px;">
            <input type="checkbox" id="chk-answered-only"${answeredOnlyDefault ? ' checked' : ''}>
            응답한 문항만 채점 (미응답 문항은 총 문항수·점수에서 제외)
          </label>
          <ul class="answer-grid">${inputsHtml}</ul>

          <footer class="quiz-footer">
            <span class="progress-text" id="progress-text" aria-live="polite"></span>
            <span class="footer-actions">
              <button type="button" class="btn btn-secondary" id="btn-bulk">${ic('clipboard')} 여러 개 붙여넣기</button>
              <button type="submit" class="btn" id="btn-submit">${ic('check')} 제출</button>
            </span>
          </footer>
        </form>`}
      </section>`;

    if (allCaughtUp) {
      $('#btn-finish-session').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        setPending(btn, true, '처리 중…');
        try {
          await api(`/sessions/${openSession.session_id}/finish`, { method: 'POST' });
          toast('채점을 끝냈습니다.', 'success');
          location.hash = latest ? `#/attempt/${latest.id}` : `#/wb/${sec.workbook_id}`;
        } catch (err) {
          setPending(btn, false);
          toast(err.message, 'error');
        }
      });
      focusTitle();
      return;
    }

    const grid = $('.answer-grid', view);
    const inputs = Array.from(grid.querySelectorAll('.ans-input'));
    const progressEl = $('#progress-text');
    const submitBtn = $('#btn-submit');
    const answeredOnlyChk = $('#chk-answered-only', view);

    function updateProgress() {
      const total = inputs.length;
      const done = inputs.filter((i) => i.value.trim() !== '').length;
      progressEl.textContent = `응답 ${done}/${total} · 미응답 ${total - done}`;
    }
    updateProgress();

    answeredOnlyChk.addEventListener('change', () => {
      storage.localSet('ag_answered_only', answeredOnlyChk.checked ? '1' : '0');
    });

    grid.addEventListener('input', (e) => {
      if (e.target.classList.contains('ans-input')) {
        e.target.classList.toggle('filled', e.target.value.trim() !== '');
        updateProgress();
      }
    });

    grid.addEventListener('keydown', (e) => {
      const t = e.target;
      if (!(t instanceof HTMLInputElement)) return;
      const i = inputs.indexOf(t);
      if (i < 0) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        (inputs[i + 1] || submitBtn).focus();
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
        e.preventDefault();
        if (inputs[i + 1]) inputs[i + 1].focus();
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
        e.preventDefault();
        if (inputs[i - 1]) inputs[i - 1].focus();
      }
    });

    if (inputs.length) inputs[0].focus();

    /* --- bulk paste --- */
    $('#btn-bulk').addEventListener('click', () => {
      const dlg = $('#dlg-paste');
      $('#form-paste').reset();
      dlg.showModal();
      $('#inp-bulk').focus();
    });

    // The paste dialog is static (outside #view); its submit logic is
    // re-bound per quiz render via this hook instead of a new listener.
    bulkApplyHook = () => {
      const dlg = $('#dlg-paste');
      const parsed = parseBulkLines($('#inp-bulk').value);
      if (!parsed.length) {
        toast('한 줄에 한 문제씩 "1. 3" 형식으로 입력해 주세요.', 'error');
        return;
      }
      const map = new Map(inputs.map((i) => [Number(i.dataset.number), i]));
      let filled = 0;
      let skipped = 0;
      parsed.forEach(({ number, answer }) => {
        const input = map.get(number);
        if (!input) { skipped += 1; return; }
        input.value = answer;
        input.classList.toggle('filled', answer !== '');
        filled += 1;
      });
      updateProgress();
      dlg.close();
      toast(
        `${filled}개 답안을 채웠습니다.${skipped ? ` (범위 밖 ${skipped}개 건너뜀)` : ''}`,
        'success'
      );
    };

    /* --- submit --- */
    $('#quiz-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const answers = {};
      inputs.forEach((i) => {
        answers[String(i.dataset.number)] = i.value.trim();
      });
      const answeredOnly = answeredOnlyChk.checked;
      const unanswered = inputs.filter((i) => i.value.trim() === '').length;
      if (answeredOnly && unanswered === inputs.length) {
        toast('응답한 문항만 채점하려면 최소 1문항은 답을 입력해야 합니다.', 'error');
        if (inputs[0]) inputs[0].focus();
        return;
      }
      if (!answeredOnly && unanswered > 0 &&
          !window.confirm(`응답하지 않은 문제가 ${unanswered}개 있습니다. 미응답으로 제출할까요?`)) {
        const firstEmpty = inputs.find((i) => i.value.trim() === '');
        if (firstEmpty) firstEmpty.focus();
        return;
      }
      submitBtn.disabled = true;
      $('#btn-bulk').disabled = true;
      setPending(submitBtn, true, '채점 중…');
      try {
        // Retry auto-detection lives entirely server-side: it just looks at
        // whether this section has an open session (see the load-time check
        // above), so the client always posts the plain answer set.
        const attempt = await api('/attempts', {
          method: 'POST',
          body: { section_id: sid, answers, answered_only: answeredOnly }
        });
        location.hash = `#/attempt/${attempt.id}`;
      } catch (err) {
        setPending(submitBtn, false);
        submitBtn.disabled = false;
        $('#btn-bulk').disabled = false;
        toast(err.message, 'error');
      }
    });

    focusTitle();
  }

  /* ==================================================================
   * View: Results (#/attempt/:id)
   * ================================================================== */
  async function viewAttempt(aid) {
    const attempt = await api(`/attempts/${aid}`);
    const secInfo = await api(`/sections/${attempt.section_id}`)
      .catch(() => null);

    const results = attempt.results || [];
    // answered_only can narrow this submission's own `total` below the full
    // per-question `results` array -- grade() (services/grader.py) always
    // emits one row per answer-key number, status 'unanswered' for anything
    // left blank, even for numbers answered_only excluded from total/percent.
    // `total < results.length` happens if and only if that's what happened
    // for THIS submission. When it has, every never-graded 'unanswered' row
    // must stay out of the chips and the wrong-question list below -- it
    // wasn't graded, so it shouldn't look wrong (or right, or skipped) there.
    const narrowedByAnsweredOnly = results.length > 0 && Number(attempt.total) < results.length;
    const displayResults = narrowedByAnsweredOnly
      ? results.filter((r) => r.status !== 'unanswered')
      : results;

    let nCorrect = 0;
    let nWrong = 0;
    let nSkipped = 0;
    displayResults.forEach((r) => {
      if (r.status === 'correct') nCorrect += 1;
      else if (r.status === 'incorrect') nWrong += 1;
      else nSkipped += 1;
    });
    if (!results.length) {
      nWrong = (attempt.wrong_numbers || []).length;
      nSkipped = (attempt.unanswered_numbers || []).length;
      nCorrect = attempt.score != null ? attempt.score : attempt.total - nWrong - nSkipped;
    }
    // The retry button's gate below is deliberately NOT nWrong + nSkipped --
    // those are the display-only counts above, which hide answered_only-
    // skipped numbers on purpose. Whether there's still something to retry
    // instead reads the server's own (always-unfiltered) wrong/unanswered
    // lists, so a section left with only skipped questions still offers one.
    const nOpenForRetry = (attempt.wrong_numbers || []).length
      + (attempt.unanswered_numbers || []).length;

    const percent = Number(attempt.percent || 0);
    const R = 84;
    const C = 2 * Math.PI * R;
    const ringColor = percent >= 80 ? 'var(--green)' : percent >= 50 ? 'var(--amber)' : 'var(--red)';
    const targetOffset = C * (1 - Math.min(Math.max(percent, 0), 100) / 100);

    const wrongItems = displayResults.filter((r) => r.status !== 'correct');
    const wrongHtml = wrongItems.length
      ? `<ul class="wrong-list">
          ${wrongItems.map((r) => `
            <li class="wrong-card ${r.status === 'unanswered' ? 'unanswered' : ''}">
              <span class="wc-num">${Number(r.number)}</span>
              <span class="wc-body">
                <span class="wc-line">내 답 <b>${r.given ? esc(r.given) : '(미응답)'}</b></span>
                <span class="wc-reveal-row">
                  <button type="button" class="btn btn-secondary btn-sm reveal-btn"
                          aria-expanded="false" aria-controls="ans-${Number(r.number)}">
                    ${ic('target', 14)} 정답보기
                  </button>
                  <span class="wc-line wc-answer" id="ans-${Number(r.number)}" hidden>
                    정답 <b>${esc(r.expected)}</b>
                  </span>
                </span>
              </span>
              <span class="wc-status">${esc(STATUS_NAMES[r.status] || r.status)}</span>
            </li>`).join('')}
        </ul>`
      : `<div class="all-correct">${ic('check', 18)} 전 문항 정답입니다. 완벽해요!</div>`;

    const wbHref = secInfo && secInfo.workbook_id ? `#/wb/${secInfo.workbook_id}` : '#/';

    // A retry's own round can look better (or worse) than what's actually
    // recorded -- only the session's frozen first-submission score ever
    // moves history/aggregates, so say so explicitly rather than let a
    // student assume this ring just replaced it.
    const isRetry = attempt.is_first_submission === false;
    const retryNoteHtml = (isRetry && attempt.first_percent != null) ? `
        <div class="banner banner-rec retry-note" role="note">
          ${ic('refresh', 15)}
          <span><strong>첫 제출 기준 점수: ${pctText(attempt.first_percent)}</strong> · 재도전 결과는 기록에 반영되지 않아요.</span>
        </div>` : '';

    view.innerHTML = `
      <section class="page" aria-label="채점 결과">
        ${backLink(wbHref, '목록으로 돌아가기')}
        <div class="page-head">
          <div>
            <h1 class="page-title" tabindex="-1">채점 결과</h1>
            <p class="sub">${esc(secInfo ? `${secInfo.workbook_title || ''} · ${secInfo.label || ''}` : `시도 #${attempt.id}`)}
               · ${esc(fmtDate(attempt.taken_at))}</p>
          </div>
        </div>
        ${retryNoteHtml}

        <div class="card result-hero">
          <div class="ring-wrap">
            <svg viewBox="0 0 200 200" role="img" aria-label="점수 ${Math.round(percent)}%">
              <circle class="ring-bg" cx="100" cy="100" r="${R}"
                      fill="none" stroke-width="16"></circle>
              <circle class="ring-fg" id="ring-fg" cx="100" cy="100" r="${R}"
                      fill="none" stroke="${ringColor}" stroke-width="16" stroke-linecap="round"
                      stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${C.toFixed(2)}"></circle>
            </svg>
            <div class="ring-center">
              <span class="ring-pct">${Math.round(percent)}%</span>
              <span class="ring-sub">${Number(attempt.score)} / ${Number(attempt.total)} 맞힘</span>
            </div>
          </div>

          <div class="chips">
            <span class="stat-chip ok">${ic('check', 14)} 정답 ${nCorrect}</span>
            <span class="stat-chip bad">${ic('x', 14)} 오답 ${nWrong}</span>
            <span class="stat-chip skip">${ic('alert', 14)} 미응답 ${nSkipped}</span>
          </div>
        </div>

        <div class="card" style="margin-top:14px;">
          <h2 style="font-size:16px;font-weight:800;margin-bottom:4px;">틀린 문제 ${wrongItems.length}개</h2>
          <p class="sub" style="margin-bottom:12px;">먼저 스스로 고민해 보세요 — 정답은 [정답보기]를 누르면 확인할 수 있어요.</p>
          ${wrongHtml}
          <div class="result-actions">
            <button class="btn" id="btn-finish-session">${ic('check')} 채점 끝내기</button>
            <button class="btn" id="btn-retry-misses" ${nOpenForRetry === 0 ? 'disabled title="다시 풀 문제가 없습니다"' : ''}>
              ${ic('refresh')} 틀린 문제만 다시 풀기
            </button>
            <!-- Leaves the session open server-side on purpose -- "채점 끝내기"
                 above is the only action that closes it. -->
            <a class="btn btn-secondary" href="${esc(wbHref)}">${ic('list')} 목록으로</a>
            <button class="btn btn-ghost btn-sm" id="btn-del-section">
              ${ic('trash', 14)} 섹션 삭제
            </button>
          </div>
        </div>
      </section>`;

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const ring = $('#ring-fg');
        if (ring) ring.style.strokeDashoffset = String(targetOffset);
        // Gated on is_first_submission, not percent alone -- a retry that
        // also happens to score 100% is not the session's recorded
        // first-submission accuracy (see isRetry/retryNoteHtml above).
        if (attempt.is_first_submission === true && percent === 100) celebrate();
      });
    });

    /* --- deferred answer reveal: one toggle per wrong card --- */
    view.querySelectorAll('.reveal-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const card = btn.closest('.wrong-card');
        const answer = card && card.querySelector('.wc-answer');
        if (!answer) return;
        const willShow = answer.hidden;
        answer.hidden = !willShow;
        card.classList.toggle('revealed', willShow);
        btn.setAttribute('aria-expanded', String(willShow));
        btn.innerHTML = willShow
          ? `${ic('x', 14)} 정답 숨기기`
          : `${ic('target', 14)} 정답보기`;
      });
    });

    /* --- per-section delete from the results screen --- */
    $('#btn-del-section').addEventListener('click', async () => {
      const label = secInfo?.label || `#${attempt.section_id}`;
      if (!window.confirm(`'${label}' 섹션을 삭제할까요?\n이 섹션의 정답과 모든 채점 기록이 삭제되며 되돌릴 수 없습니다.`)) return;
      try {
        await api(`/sections/${attempt.section_id}`, { method: 'DELETE' });
        toast(`'${label}' 섹션이 삭제되었습니다.`, 'success');
        location.hash = wbHref;
      } catch (err) {
        toast(err.message, 'error');
      }
    });

    // No lookup needed here: this screen only ever renders right after a
    // submission, so the section's open session (if any) is still the one
    // this very attempt belongs to -- the quiz screen resumes it and shows
    // only the not-yet-correct numbers automatically (server-driven, see
    // GET /sections/{sid}/session in viewSolve). No client-supplied id, no
    // extra request.
    $('#btn-retry-misses').addEventListener('click', () => {
      location.hash = `#/sec/${attempt.section_id}/solve`;
    });

    /* --- "채점 끝내기": closes the session server-side, then hands off to
       the finished-session screen. Distinct from "목록으로" above, which
       deliberately leaves the session open. --- */
    $('#btn-finish-session').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      setPending(btn, true, '처리 중…');
      try {
        await api(`/sessions/${attempt.session_id}/finish`, { method: 'POST' });
        toast('채점을 끝냈습니다.', 'success');
        location.hash = `#/session/${attempt.session_id}`;
      } catch (err) {
        setPending(btn, false);
        toast(err.message, 'error');
      }
    });

    focusTitle();
  }

  /* ==================================================================
   * View: Session detail / history entry (#/session/:id)
   *
   * One finished session = one history entry. Reused both as the screen
   * "채점 끝내기" lands on (Results screen, and the quiz screen's
   * all-caught-up state) and as the click-through target for a past row in
   * a section card's "응시 기록 보기" panel -- there is one "finished
   * session" screen, not two.
   * ================================================================== */
  async function viewSessionDetail(sessId) {
    const detail = await api(`/sessions/${sessId}`);
    const secInfo = await api(`/sections/${detail.section_id}`).catch(() => null);

    const results = detail.first_results || [];
    // Same answered_only narrowing as viewAttempt() above, checked against
    // the first submission's own frozen total instead of the latest
    // attempt's -- see the comment there for why `first_total <
    // results.length` exactly identifies it having happened.
    const narrowedByAnsweredOnly = results.length > 0 && Number(detail.first_total) < results.length;
    const displayResults = narrowedByAnsweredOnly
      ? results.filter((r) => r.status !== 'unanswered')
      : results;
    const wrongItems = displayResults.filter((r) => r.status !== 'correct');
    let nCorrect;
    let nWrong;
    let nSkipped;
    if (results.length) {
      nCorrect = displayResults.filter((r) => r.status === 'correct').length;
      nWrong = displayResults.filter((r) => r.status === 'incorrect').length;
      nSkipped = displayResults.length - nCorrect - nWrong;
    } else {
      // Defensive fallback for a session whose first-submission snapshot is
      // unexpectedly missing -- every submission always writes its own
      // per-question results, so this shouldn't happen, but it avoids
      // rendering a false "perfect score" banner if it ever does.
      nCorrect = Number(detail.first_score || 0);
      nWrong = Math.max(Number(detail.first_total || 0) - nCorrect, 0);
      nSkipped = 0;
    }
    const wrongCount = results.length ? wrongItems.length : nWrong + nSkipped;

    const percent = Number(detail.first_percent || 0);
    const R = 84;
    const C = 2 * Math.PI * R;
    const ringColor = percent >= 80 ? 'var(--green)' : percent >= 50 ? 'var(--amber)' : 'var(--red)';
    const targetOffset = C * (1 - Math.min(Math.max(percent, 0), 100) / 100);

    const wrongHtml = results.length
      ? (wrongItems.length
          ? `<ul class="wrong-list">
              ${wrongItems.map((r) => `
                <li class="wrong-card ${r.status === 'unanswered' ? 'unanswered' : ''}">
                  <span class="wc-num">${Number(r.number)}</span>
                  <span class="wc-body">
                    <span class="wc-line">내 답 <b>${r.given ? esc(r.given) : '(미응답)'}</b></span>
                    <span class="wc-reveal-row">
                      <button type="button" class="btn btn-secondary btn-sm reveal-btn"
                              aria-expanded="false" aria-controls="sess-ans-${Number(r.number)}">
                        ${ic('target', 14)} 정답보기
                      </button>
                      <span class="wc-line wc-answer" id="sess-ans-${Number(r.number)}" hidden>
                        정답 <b>${esc(r.expected)}</b>
                      </span>
                    </span>
                  </span>
                  <span class="wc-status">${esc(STATUS_NAMES[r.status] || r.status)}</span>
                </li>`).join('')}
            </ul>`
          : `<div class="all-correct">${ic('check', 18)} 첫 제출에서 전 문항 정답입니다. 완벽해요!</div>`)
      : (nWrong + nSkipped > 0
          ? `<p class="muted" style="font-size:14px;">첫 제출의 문항별 기록을 불러올 수 없습니다.</p>`
          : `<div class="all-correct">${ic('check', 18)} 첫 제출에서 전 문항 정답입니다. 완벽해요!</div>`);

    // breakdown.total_questions is the section's FULL answer-key count --
    // deliberately not the same number as first_total (the score ring's
    // own denominator above), which narrows to the answered subset when
    // answered_only was used on the first submission. Both get their own
    // explicit label below so the two percentages never read as
    // contradictory.
    const bd = detail.breakdown || {
      total_questions: 0,
      first_try: { numbers: [], count: 0, percent: 0 },
      second_try: { numbers: [], count: 0, percent: 0 },
      third_plus: { numbers: [], count: 0, percent: 0 }
    };

    const numChips = (numbers) => (numbers && numbers.length)
      ? `<div class="num-chip-list">${numbers.map((n) => `<span class="num-chip">${Number(n)}</span>`).join('')}</div>`
      : `<p class="breakdown-empty">해당 문항 없음</p>`;

    const breakdownBlock = (cls, badgeCls, title, bucket) => `
      <div class="breakdown-card bd-${cls}">
        <div class="breakdown-head">
          <span class="breakdown-title">${esc(title)}</span>
          <span class="badge ${badgeCls}">${pctText(bucket.percent)}</span>
          <span class="breakdown-count">${Number(bucket.count)} / ${Number(bd.total_questions)}문항</span>
        </div>
        ${numChips(bucket.numbers)}
      </div>`;

    const wbHref = secInfo && secInfo.workbook_id ? `#/wb/${secInfo.workbook_id}` : '#/';

    view.innerHTML = `
      <section class="page" aria-label="채점 기록 상세">
        ${backLink(wbHref, '목록으로 돌아가기')}
        <div class="page-head">
          <div>
            <h1 class="page-title" tabindex="-1">채점 기록 상세</h1>
            <p class="sub">${esc(secInfo ? `${secInfo.workbook_title || ''} · ${secInfo.label || ''}` : `세션 #${detail.session_id}`)}
               · ${esc(fmtDate(detail.finished_at || detail.started_at))} 완료 · 총 ${Number(detail.submission_count || 0)}번 제출</p>
          </div>
        </div>

        <div class="card result-hero">
          <p class="sub score-basis-note">기록에 남는 점수 — 첫 제출 <b>${Number(detail.first_total)}문항</b> 기준</p>
          <div class="ring-wrap">
            <svg viewBox="0 0 200 200" role="img" aria-label="점수 ${Math.round(percent)}%">
              <circle class="ring-bg" cx="100" cy="100" r="${R}"
                      fill="none" stroke-width="16"></circle>
              <circle class="ring-fg" id="ring-fg" cx="100" cy="100" r="${R}"
                      fill="none" stroke="${ringColor}" stroke-width="16" stroke-linecap="round"
                      stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${C.toFixed(2)}"></circle>
            </svg>
            <div class="ring-center">
              <span class="ring-pct">${Math.round(percent)}%</span>
              <span class="ring-sub">${Number(detail.first_score)} / ${Number(detail.first_total)} 맞힘</span>
            </div>
          </div>

          <div class="chips">
            <span class="stat-chip ok">${ic('check', 14)} 정답 ${nCorrect}</span>
            <span class="stat-chip bad">${ic('x', 14)} 오답 ${nWrong}</span>
            <span class="stat-chip skip">${ic('alert', 14)} 미응답 ${nSkipped}</span>
          </div>
        </div>

        <div class="card" style="margin-top:14px;">
          <h2 style="font-size:16px;font-weight:800;margin-bottom:4px;">첫 제출에서 틀린 문제 ${wrongCount}개</h2>
          <p class="sub" style="margin-bottom:12px;">이 세션의 기록 점수를 만든 첫 제출 결과예요 — 정답은 [정답보기]를 누르면 확인할 수 있어요.</p>
          ${wrongHtml}
        </div>

        <div class="card" style="margin-top:14px;">
          <h2 style="font-size:16px;font-weight:800;margin-bottom:4px;">시도 횟수별 정답 분포</h2>
          <p class="sub breakdown-denom-note">
            아래 비율은 이 섹션 전체 <b>${Number(bd.total_questions)}문항</b> 기준이에요 — 위 점수의 분모(첫 제출
            <b>${Number(detail.first_total)}문항</b> 기준)와 다를 수 있어요. '응답한 문항만 채점'을 사용했다면 첫 제출
            당시 실제로 답한 문항 수만 그 분모가 되기 때문이에요. 한 번도 정답을 맞히지 못한 문항(미응답 포함)은
            '3차 이상'에 포함됩니다.
          </p>
          <div class="breakdown-grid">
            ${breakdownBlock('first', 'badge-green', '1차에 정답', bd.first_try)}
            ${breakdownBlock('second', 'badge-amber', '2차에 정답', bd.second_try)}
            ${breakdownBlock('third', 'badge-red', '3차 이상 (미해결 포함)', bd.third_plus)}
          </div>
        </div>
      </section>`;

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const ring = $('#ring-fg');
        if (ring) ring.style.strokeDashoffset = String(targetOffset);
        // This screen only ever shows the session's frozen first submission,
        // so percent === 100 alone unambiguously means a first-try 100%.
        if (percent === 100) celebrate();
      });
    });

    /* --- deferred answer reveal: one toggle per wrong card --- */
    view.querySelectorAll('.reveal-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const card = btn.closest('.wrong-card');
        const answer = card && card.querySelector('.wc-answer');
        if (!answer) return;
        const willShow = answer.hidden;
        answer.hidden = !willShow;
        card.classList.toggle('revealed', willShow);
        btn.setAttribute('aria-expanded', String(willShow));
        btn.innerHTML = willShow
          ? `${ic('x', 14)} 정답 숨기기`
          : `${ic('target', 14)} 정답보기`;
      });
    });

    focusTitle();
  }

  /* ------------------------------------------------------------------
   * Static dialog wiring + boot
   * ------------------------------------------------------------------ */
  document.querySelectorAll('[data-close-dialog]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const dlg = btn.closest('dialog');
      if (dlg) dlg.close();
    });
  });

  $('#form-paste').addEventListener('submit', (e) => {
    e.preventDefault();
    if (bulkApplyHook) bulkApplyHook();
  });

  $('#form-create').addEventListener('submit', async (e) => {
    e.preventDefault();
    const dlg = $('#dlg-create');
    const inp = $('#inp-create-title');
    const btn = $('#btn-create-submit');
    const title = inp.value.trim();
    if (!title) {
      toast('워크북 제목을 입력해 주세요.', 'error');
      inp.focus();
      return;
    }
    setPending(btn, true, '만드는 중…');
    try {
      const wb = await api('/workbooks', { method: 'POST', body: { title } });
      dlg.close();
      toast(`'${wb.title}' 워크북이 생성되었습니다. 정답을 등록해 주세요.`, 'success');
      location.hash = `#/new/${wb.id}`;
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setPending(btn, false);
    }
  });

  $('#form-rename').addEventListener('submit', async (e) => {
    e.preventDefault();
    const dlg = $('#dlg-rename');
    const inp = $('#inp-rename-title');
    const btn = $('#btn-rename-submit');
    const title = inp.value.trim();
    if (!title) {
      toast('워크북 제목을 입력해 주세요.', 'error');
      inp.focus();
      return;
    }
    setPending(btn, true, '저장 중…');
    try {
      await api(`/workbooks/${renameTargetId}`, { method: 'PATCH', body: { title } });
      dlg.close();
      toast('워크북 이름이 변경되었습니다.', 'success');
      render();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setPending(btn, false);
    }
  });

  /* --- Gemini API key settings dialog --- */
  function openApiKeyDialog() {
    const dlg = $('#dlg-apikey');
    const inp = $('#inp-apikey');
    const statusEl = $('#apikey-status');
    const removeBtn = $('#btn-apikey-remove');
    $('#form-apikey').reset();
    statusEl.textContent = '불러오는 중…';
    removeBtn.hidden = true;
    api('/settings/api-key').then((s) => {
      if (s && s.set) {
        statusEl.textContent =
          `등록된 키: ${s.masked}${s.source === 'server' ? ' (서버 공용 키)' : ''} — 새 키를 입력하면 교체됩니다.`;
        removeBtn.hidden = s.source === 'server';
        inp.placeholder = '새 키로 교체하려면 입력하세요';
      } else {
        statusEl.textContent = '등록된 키가 없습니다. 아래 링크에서 무료로 발급할 수 있어요.';
        inp.placeholder = '발급받은 키를 붙여넣으세요';
      }
    }).catch(() => { statusEl.textContent = ''; });
    dlg.showModal();
    inp.focus();
  }

  $('#gemini-chip').addEventListener('click', openApiKeyDialog);

  $('#form-apikey').addEventListener('submit', async (e) => {
    e.preventDefault();
    const dlg = $('#dlg-apikey');
    const inp = $('#inp-apikey');
    const btn = $('#btn-apikey-save');
    const key = inp.value.trim();
    if (!key) {
      toast('API 키를 입력해 주세요.', 'error');
      inp.focus();
      return;
    }
    setPending(btn, true, '저장 중…');
    try {
      const s = await api('/settings/api-key', { method: 'POST', body: { api_key: key } });
      dlg.close();
      toast(`API 키가 저장되었습니다 (${s.masked}). 이제 사진으로 정답지를 등록할 수 있어요.`, 'success');
      checkHealth();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setPending(btn, false);
    }
  });

  $('#btn-apikey-remove').addEventListener('click', async () => {
    if (!window.confirm('저장된 API 키를 삭제할까요?\n이 기기에서만 제거되며, 이후 서버 기본 키(있다면)가 사용됩니다.')) return;
    setPending($('#btn-apikey-remove'), true);
    try {
      await api('/settings/api-key', { method: 'DELETE' });
      $('#dlg-apikey').close();
      toast('저장된 API 키가 삭제되었습니다.', 'success');
      checkHealth();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setPending($('#btn-apikey-remove'), false);
    }
  });

  /* --- first-run onboarding: require a Gemini key before any uploads --- */
  function lockOnboarding() {
    const dlg = $('#dlg-onboarding');
    if (!dlg) return;
    // Non-dismissible: no cancel button in markup + block Esc/backdrop close.
    dlg.addEventListener('cancel', (e) => e.preventDefault());
    dlg.showModal();
    $('#inp-onboard-key').focus();
  }

  $('#form-onboarding').addEventListener('submit', async (e) => {
    e.preventDefault();
    const dlg = $('#dlg-onboarding');
    const inp = $('#inp-onboard-key');
    const btn = $('#btn-onboard-save');
    const key = inp.value.trim();
    if (!key) {
      toast('API 키를 입력해 주세요. 키가 있어야 사진 정답지를 등록할 수 있어요.', 'error');
      inp.focus();
      return;
    }
    setPending(btn, true, '저장 중…');
    try {
      await api('/settings/api-key', { method: 'POST', body: { api_key: key } });
      state.onboarded = true;
      dlg.close();
      toast('API 키가 저장되었습니다. 이제 정답지를 등록할 수 있어요!', 'success');
      checkHealth();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setPending(btn, false);
    }
  });

  async function maybeOnboard() {
    if (state.onboarded) return;
    try {
      const s = await api('/settings/api-key');
      if (!s || !s.set) lockOnboarding();
    } catch { /* server unreachable — normal error UI handles it */ }
  }

  async function checkHealth() {
    const chip = $('#gemini-chip');
    if (!chip) return;
    chip.hidden = false;
    chip.classList.add('chip-btn');
    try {
      const h = await api('/health');
      if (h && h.gemini_available) {
        chip.textContent = `Gemini Vision 사용 가능 (${h.model || 'gemini'})`;
        chip.className = 'chip chip-ok chip-btn';
        chip.title = '클릭하여 Gemini API 키 설정';
      } else {
        chip.textContent = 'Gemini API 키 미설정 · 클릭하여 등록';
        chip.className = 'chip chip-warn chip-btn';
        chip.title = '클릭하여 Gemini API 키 설정';
      }
    } catch {
      chip.textContent = '서버 연결 없음';
      chip.className = 'chip chip-muted chip-btn';
      chip.title = '';
    }
  }

  function openUploadGuide(firstTime = false) {
    const dlg = $('#dlg-upload-guide');
    if (!dlg) return;
    if (firstTime) {
      try { localStorage.setItem('ag_upload_guide_seen', '1'); } catch { /* noop */ }
    }
    dlg.showModal();
  }

  /* ------------------------------------------------------------------
   * Boot
   * ------------------------------------------------------------------ */
  window.addEventListener('hashchange', render);
  // Navigating away mid-burst must not leave a stray celebration rAF loop
  // running under the screen that replaces it.
  window.addEventListener('hashchange', stopCelebration);

  async function boot() {
    getDeviceUserId(); // ensure a device UUID exists before the first request
    await checkHealth();
    render();
    await maybeOnboard(); // blocks the UI until a Gemini key is saved (first run)
  }

  boot();
})();
