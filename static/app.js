'use strict';
/* 铅字归还助手 —— 前端逻辑（原生 JS + SVG，多字盘换盘导航） */

let S = { trays: [], cells: [], session: null, last_done: null, pending: [],
          conflicts: {}, reason_names: {},
          stocktakes: [], stocktake_history: [],
          requisition: null, req_history: [],
          req_status_names: {}, alloc_status_names: {}, req_issue_names: {} };
let view = 'run';
let editTrayId = null;       // 字盘编辑页当前选中的字盘
let startTrayId = null;      // 规划页选择的起始盘（本地草稿）
let planOrder = [];          // 规划页调整中的换盘顺序
let selectedCellId = null;   // 字盘编辑页选中的格
let conflictCtx = null;      // 当前未解决的冲突
let suppressClickUntil = 0;  // 拖拽后短暂抑制点击选中

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const SVGNS = 'http://www.w3.org/2000/svg';

/* ---------------- 基础 ---------------- */

async function api(path, method = 'GET', body = null) {
  const opt = { method, headers: {} };
  if (body !== null) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = isErr ? 'err' : '';
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

async function guard(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
}

function applyState(st) {
  S = st;
  if (!S.trays.some((t) => t.id === editTrayId)) {
    editTrayId = S.trays.length ? S.trays[0].id : null;
  }
  renderAll();
}

async function refresh() {
  await guard(async () => applyState(await api('/api/state')));
}

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const trayById = (id) => S.trays.find((t) => t.id === id) || null;
const trayCells = (tid) => S.cells.filter((c) => c.tray_id === tid);

/* ---------------- SVG 字盘 ---------------- */

function svgEl(name, attrs, parent) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

/**
 * 绘制单个字盘。
 * cells: 该字盘的格位；opts: {
 *   cls(cell) -> 额外 class, remain: Map<cellId, n>,
 *   onClick(cell), draggable: bool, onDrop(cell, x, y)
 * }
 */
function drawTray(svg, cells, opts) {
  svg.innerHTML = '';
  let maxX = 460, maxY = 320;
  for (const c of cells) {
    maxX = Math.max(maxX, c.x + c.w + 24);
    maxY = Math.max(maxY, c.y + c.h + 24);
  }
  svg.setAttribute('viewBox', '0 0 ' + maxX + ' ' + maxY);
  for (const c of cells) {
    const cls = ['cell'];
    if (!c.char) cls.push('empty');
    if (c.qty > c.capacity) cls.push('over');
    const extra = opts.cls && opts.cls(c);
    if (extra) cls.push(extra);
    const g = svgEl('g', { 'class': cls.join(' '), 'data-id': c.id }, svg);
    svgEl('rect', { x: c.x, y: c.y, width: c.w, height: c.h, rx: 4 }, g);
    if (c.char) {
      const t = svgEl('text', {
        x: c.x + c.w / 2, y: c.y + c.h / 2, 'class': 'cell-char',
      }, g);
      t.textContent = c.char;
    }
    const lab = svgEl('text', { x: c.x + 3, y: c.y + 9, 'class': 'cell-label' }, g);
    lab.textContent = c.label;
    const q = svgEl('text', {
      x: c.x + c.w - 3, y: c.y + c.h - 3,
      'class': 'cell-qty', 'text-anchor': 'end',
    }, g);
    q.textContent = c.qty + '/' + c.capacity;
    if (opts.remain && opts.remain.has(c.id)) {
      const b = svgEl('text', {
        x: c.x + c.w / 2, y: c.y + c.h - 5, 'class': 'cell-remain',
      }, g);
      b.textContent = '余' + opts.remain.get(c.id);
    }
    if (opts.sub) {
      const sub = opts.sub(c);
      if (sub) {
        const s = svgEl('text', {
          x: c.x + c.w - 3, y: c.y + 10,
          'class': 'cell-sub ' + (sub.cls || ''), 'text-anchor': 'end',
        }, g);
        s.textContent = sub.text;
      }
    }
    if (opts.onClick) {
      g.addEventListener('click', () => {
        if (Date.now() < suppressClickUntil) return;
        opts.onClick(c);
      });
    }
    if (opts.draggable) enableDrag(g, c, svg, opts.onDrop);
  }
}

/* 拖拽摆位：pointer 事件，结束后写回坐标 */
function enableDrag(g, cell, svg, onDrop) {
  let start = null;
  g.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    const pt = toSvg(svg, ev);
    start = { pt, x: cell.x, y: cell.y, moved: false };
    g.classList.add('dragging');
    g.setPointerCapture(ev.pointerId);
  });
  g.addEventListener('pointermove', (ev) => {
    if (!start) return;
    const pt = toSvg(svg, ev);
    const dx = pt.x - start.pt.x, dy = pt.y - start.pt.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) start.moved = true;
    g.setAttribute('transform', 'translate(' + dx + ',' + dy + ')');
  });
  g.addEventListener('pointerup', (ev) => {
    if (!start) return;
    g.classList.remove('dragging');
    if (start.moved) {
      const pt = toSvg(svg, ev);
      const nx = Math.round((start.x + pt.x - start.pt.x) / 2) * 2;
      const ny = Math.round((start.y + pt.y - start.pt.y) / 2) * 2;
      suppressClickUntil = Date.now() + 250;
      onDrop(cell, Math.max(0, nx), Math.max(0, ny));
    } else {
      g.removeAttribute('transform');
    }
    start = null;
  });
}

function toSvg(svg, ev) {
  const pt = svg.createSVGPoint();
  pt.x = ev.clientX; pt.y = ev.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

/* ---------------- 视图切换 ---------------- */

function switchView(v) {
  view = v;
  $$('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach((s) => { s.hidden = s.id !== 'view-' + v; });
  if (v === 'run') setTimeout(focusRunInput, 50);
  if (v === 'pick') setTimeout(() => {
    const inp = S.requisition && S.requisition.status === 'active'
      ? ($('#reqGateInput') || $('#reqScan')) : $('#reqText');
    if (inp) inp.focus();
  }, 50);
}

function focusRunInput() {
  const sess = S.session;
  if (!sess) { $('#pasteBox').focus(); return; }
  if (sess.status === 'active' && !curTask()) {
    const gi = $('#gateInput');
    if (gi) gi.focus();
  } else {
    $('#scanInput').focus();
  }
}

/* ---------------- 会话派生状态 ---------------- */

const curTask = () => S.session &&
  S.session.tasks.find((t) => t.status === 'active');
const nextTaskInTray = (tid) => S.session &&
  S.session.tasks.find((t) => t.status === 'pending' && t.tray_id === tid);

function sessionOrder(sess) {
  const fromTasks = Array.from(new Set(sess.tasks.map((t) => t.tray_id)));
  const stored = (sess.tray_order || []).filter((id) =>
    S.trays.some((t) => t.id === id));
  return stored.length ? stored : fromTasks;
}

/* 换盘闸门：active 且当前没有进行中的格位任务。
   锁定起始盘后 current_tray_id 已指向起始盘、盘内任务仍全部 pending，
   此时同样必须停在闸门，等待扫描字盘码后才激活首格。 */
function gateExpectedTray(sess) {
  if (!sess || sess.status !== 'active') return null;
  if (curTask()) return null;
  const pending = new Set(sess.tasks
    .filter((t) => t.status === 'pending').map((t) => t.tray_id));
  return sessionOrder(sess).find((id) => pending.has(id)) || null;
}

function traySummary(sess, tid) {
  const ts = sess.tasks.filter((t) => t.tray_id === tid);
  return {
    total: ts.length,
    done: ts.filter((t) => t.status === 'done').length,
    qty: ts.reduce((a, t) => a + t.qty, 0),
    doneQty: ts.reduce((a, t) => a + t.done_qty, 0),
    pending: ts.filter((t) => t.status === 'pending').length,
  };
}

/* ---------------- 归还作业：总渲染 ---------------- */

function renderRun() {
  const sess = S.session;
  const lastDone = S.last_done;
  $('#runEmpty').hidden = !!sess;
  $('#runPlan').hidden = !(sess && sess.status === 'planning');
  $('#runActive').hidden = !(sess && sess.status === 'active');
  $('#runDone').hidden = !!sess || !lastDone;
  if (lastDone && !sess) {
    const ts = lastDone.tasks || [];
    const qty = ts.reduce((a, t) => a + t.done_qty, 0);
    const nTray = new Set(ts.map((t) => t.tray_id)).size;
    $('#doneSummary').textContent =
      '批次 #' + lastDone.id + '（' + (lastDone.finished_at || '') +
      '）· 共经 ' + nTray + ' 个字盘、' + ts.length + ' 格 / ' + qty +
      ' 枚。可撤销最后一次确认、补打标签，或在下方直接粘贴新清单。';
  }
  if (!sess) return;
  if (sess.status === 'planning') renderPlanning(sess);
  else renderActive(sess);
}

/* ---------------- 规划页 ---------------- */

function renderPlanning(sess) {
  const order = sessionOrder(sess);
  // 初次进入规划页时初始化本地草稿
  if (!planOrder.length || !order.every((id) => planOrder.includes(id)) ||
      planOrder.length !== order.length) {
    planOrder = order.slice();
    startTrayId = sess.start_tray_id || order[0] || null;
  }
  $('#planTitle').textContent = sess.name + '（#' + sess.id + '）· 换盘规划';
  const nTasks = sess.tasks.length;
  const nQty = sess.tasks.reduce((a, t) => a + t.qty, 0);
  $('#planProgress').textContent =
    order.length + ' 个字盘 · ' + nTasks + ' 格 · ' + nQty + ' 枚';
  $('#planNoTask').hidden = nTasks > 0;

  // 扫描码体检：参与批次的每个字盘都要有非空且唯一的可扫描码
  const codeProblems = new Map();
  const seen = new Map();
  for (const tid of order) {
    const tr = trayById(tid);
    const code = (tr.scan_code || '').trim();
    if (!code) codeProblems.set(tid, '未设扫描码');
    else if (seen.has(code.toUpperCase())) {
      codeProblems.set(tid, '与「' + seen.get(code.toUpperCase()) + '」扫描码重复');
    } else seen.set(code.toUpperCase(), tr.name);
  }
  const hasBad = codeProblems.size > 0;
  $('#btnLockPlan').disabled = nTasks === 0 || hasBad;

  const tb = $('#planTable tbody');
  tb.innerHTML = '';
  planOrder.forEach((tid, i) => {
    const tr = trayById(tid);
    if (!tr) return;
    const problem = codeProblems.get(tid);
    const ts = sess.tasks.filter((t) => t.tray_id === tid);
    const qty = ts.reduce((a, t) => a + t.qty, 0);
    const trEl = document.createElement('tr');
    trEl.className = problem ? 'tray-bad' : '';
    trEl.innerHTML =
      '<td>' + (i + 1) + '</td>' +
      '<td><b>' + esc(tr.name) + '</b></td>' +
      '<td class="mono">' +
        (tr.scan_code ? esc(tr.scan_code)
          : '<span class="diff-up">（未设扫描码）</span>') +
        (problem && tr.scan_code
          ? '<div class="diff-up">' + esc(problem) + '</div>' : '') +
      '</td>' +
      '<td>' + ts.length + '</td><td>' + qty + '</td>' +
      '<td><label class="radio"><input type="radio" name="startTray" ' +
        (startTrayId === tid ? 'checked' : '') +
        (problem ? ' disabled' : '') + '> 起始盘</label></td>' +
      '<td><button class="mini" data-act="up" ' + (i === 0 ? 'disabled' : '') +
        '>↑</button> <button class="mini" data-act="down" ' +
        (i === planOrder.length - 1 ? 'disabled' : '') + '>↓</button></td>';
    trEl.querySelector('input').onchange = () => { startTrayId = tid; renderPlanning(sess); };
    trEl.querySelector('[data-act=up]').onclick = () => movePlanTray(i, -1);
    trEl.querySelector('[data-act=down]').onclick = () => movePlanTray(i, 1);
    tb.appendChild(trEl);
  });

  // 问题字盘提示
  let warn = $('#planCodeWarn');
  if (hasBad) {
    if (!warn) {
      warn = document.createElement('p');
      warn.id = 'planCodeWarn';
      warn.className = 'diff-up code-warn';
      $('#btnLockPlan').parentNode.insertBefore(warn, $('#btnLockPlan'));
    }
    warn.innerHTML = '无法开始：以下字盘缺少唯一可扫描的字盘码 — ' +
      order.filter((tid) => codeProblems.has(tid)).map((tid) =>
        '「' + esc(trayById(tid).name) + '」' + codeProblems.get(tid)).join('；') +
      '。请到「字盘编辑」页设置。';
  } else if (warn) {
    warn.remove();
  }

  // 盘内次序预览
  $('#planGroups').innerHTML = planOrder.map((tid, i) => {
    const tr = trayById(tid);
    const ts = sess.tasks.filter((t) => t.tray_id === tid)
      .sort((a, b) => a.seq - b.seq);
    return '<div class="pgroup' + (tid === startTrayId ? ' start' : '') + '">' +
      '<h4>' + (i + 1) + '. ' + esc(tr.name) +
      (tid === startTrayId ? ' · <span class="start-tag">起始盘</span>' : '') +
      '</h4><p class="mini-seq">' +
      ts.map((t) => esc(t.label) + '「' + esc(t.char) + '」×' + t.qty)
        .join(' → ') + '</p></div>';
  }).join('');
}

function movePlanTray(i, d) {
  const j = i + d;
  if (j < 0 || j >= planOrder.length) return;
  const tmp = planOrder[i];
  planOrder[i] = planOrder[j];
  planOrder[j] = tmp;
  renderPlanning(S.session);
}

async function savePlan(lock) {
  if (!S.session) return;
  await guard(async () => {
    const res = await api('/api/sessions/' + S.session.id + '/plan', 'POST',
      { tray_order: planOrder, start_tray_id: startTrayId, lock });
    applyState(res.state);
    if (lock) {
      toast('换盘顺序已锁定，请扫描起始字盘的字盘码');
      setTimeout(focusRunInput, 60);
    } else {
      toast('规划已保存');
    }
  });
}

/* ---------------- 执行页 ---------------- */

function renderActive(sess) {
  const order = sessionOrder(sess);
  const tasks = sess.tasks;
  const done = tasks.filter((t) => t.status === 'done');
  const doneQty = done.reduce((a, t) => a + t.done_qty, 0);
  const totalQty = tasks.reduce((a, t) => a + t.qty, 0);
  $('#sessTitle').textContent = sess.name + '（#' + sess.id + '）';
  const cur = curTask();
  const curTrayId = cur ? cur.tray_id : (gateExpectedTray(sess) ||
    sess.current_tray_id);
  const curTray = trayById(curTrayId);
  const g = curTrayId ? traySummary(sess, curTrayId) : null;
  $('#sessProgress').textContent =
    (curTray ? curTray.name + ' ' + g.done + '/' + g.total + ' 格 · ' : '') +
    '全盘 ' + done.length + '/' + tasks.length + ' · ' + doneQty + '/' + totalQty + ' 枚';
  $('#progressBar').style.width =
    (tasks.length ? (done.length / tasks.length) * 100 : 0) + '%';

  // 字盘标题 / 扫描码
  if (curTray) {
    $('#runTrayName').textContent = '当前字盘：' + curTray.name;
    $('#runTrayCode').textContent = curTray.scan_code
      ? '字盘码 ' + curTray.scan_code : '（未设扫描码）';
  }

  // 下一字盘与剩余任务提示
  const nextTrayId = gateExpectedTray(sess);
  const unfinishedTrays = sessionOrder(sess).filter((tid) =>
    sess.tasks.some((t) => t.tray_id === tid && t.status !== 'done'
      && t.status !== 'skipped'));
  const remainingTasks = tasks.filter((t) => t.status !== 'done'
    && t.status !== 'skipped').length;
  const remainingQty = tasks.reduce((a, t) =>
    a + (t.status === 'done' || t.status === 'skipped' ? 0 : t.qty - t.done_qty), 0);
  let hint = '';
  if (nextTrayId) {
    const nt = trayById(nextTrayId);
    const ng = traySummary(sess, nextTrayId);
    hint = '本盘格位已完成 → <b>下一字盘：' + esc(nt.name) + '</b>' +
      (nt.scan_code ? '（码 ' + esc(nt.scan_code) + '）' : '（未设扫描码）') +
      '，' + (ng.total - ng.done) + ' 格待还。请先换盘并扫描字盘码。';
  } else if (curTray) {
    const inTray = tasks.filter((t) => t.tray_id === curTrayId
      && t.status !== 'done' && t.status !== 'skipped').length;
    const others = unfinishedTrays.filter((id) => id !== curTrayId).length;
    hint = '当前盘 <b>' + esc(curTray.name) + '</b> 余 ' + inTray + ' 格' +
      (others ? '；之后还有 ' + others + ' 个字盘' : '') +
      '。全部剩余 ' + remainingTasks + ' 格 / ' + remainingQty + ' 枚。';
  }
  $('#traySwitchHint').innerHTML = hint;

  // 当前任务卡片（闸门开启时不提前高亮新盘任何格位）
  const nxt = cur && curTrayId ? nextTaskInTray(curTrayId) : null;
  const box = $('#curTask');
  if (cur) {
    const remain = cur.qty - cur.done_qty;
    box.innerHTML =
      '<div class="cur-char">' + esc(cur.char) + '</div>' +
      '<div class="cur-meta">' +
      '<div>' + esc(cur.tray_name) + ' · 目标格 <b>' + esc(cur.label) +
      '</b> · ' + esc(cur.font || '—') + ' · ' + esc(cur.size || '—') + '</div>' +
      '<div>本批余量 <b>' + remain + '</b> / ' + cur.qty + ' 枚 · ' +
      '格内 ' + cur.cell_qty + '/' + cur.capacity + '</div>' +
      (cur.note ? '<div class="muted">' + esc(cur.note) + '</div>' : '') +
      (nxt ? '<div class="next-hint">盘内下一格：' + esc(nxt.label) + ' 「' +
        esc(nxt.char) + '」×' + nxt.qty + '</div>' : '') +
      '</div>';
  } else {
    box.innerHTML = '<p class="hint">等待扫描字盘码，确认换到下一个字盘。</p>';
  }
  $('#btnConfirm').disabled = !cur;
  $('#btnSkip').disabled = !cur;
  $('#scanInput').disabled = !cur;
  $('#qtyInput').disabled = !cur;

  // 换盘闸门
  renderGate(sess, nextTrayId);

  // 冲突面板
  renderConflict();

  // 换盘分组 + 格位顺序
  $('#trayGroups').innerHTML = order.map((tid) => {
    const tr = trayById(tid);
    const ts = sess.tasks.filter((t) => t.tray_id === tid)
      .sort((a, b) => a.seq - b.seq);
    const sum = traySummary(sess, tid);
    const isCur = tid === curTrayId && !nextTrayId;
    const isGate = tid === nextTrayId;
    return '<div class="tgroup' + (isCur ? ' cur' : isGate ? ' gate' : '') +
      (sum.done === sum.total ? ' done' : '') + '">' +
      '<div class="tg-head">' +
      (isGate ? '➡ ' : isCur ? '● ' : '○ ') +
      '<b>' + esc(tr.name) + '</b> ' +
      '<span class="mono dim">' + esc(tr.scan_code || '—') + '</span>' +
      '<span class="tg-prog">' + sum.done + '/' + sum.total + '</span></div>' +
      '<div class="tg-cells">' + ts.map((t) =>
        '<span class="tg-cell st-' + t.status + '" title="' +
        esc(t.char) + ' ×' + t.qty + '">' + esc(t.label) + '</span>').join(' ') +
      '</div></div>';
  }).join('');

  // 只绘制当前字盘
  const remain = new Map();
  for (const t of tasks) {
    if (t.tray_id === curTrayId &&
        (t.status === 'active' || t.status === 'pending')) {
      remain.set(t.cell_id, t.qty - t.done_qty);
    }
  }
  drawTray($('#runTray'), curTrayId ? trayCells(curTrayId) : [], {
    remain,
    cls: (c) => {
      if (cur && c.id === cur.cell_id) return 'cur';
      if (nxt && c.id === nxt.cell_id) return 'next';
      const t = tasks.find((x) => x.cell_id === c.id && x.tray_id === curTrayId);
      if (t && t.status === 'done') return 'done';
      return '';
    },
    onClick: (c) => {
      $('#scanInput').value = c.label;
      $('#scanInput').focus();
    },
  });
}

/* ---------------- 换盘闸门 ---------------- */

function renderGate(sess, expectedId) {
  const panel = $('#gatePanel');
  if (!expectedId) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  const expected = trayById(expectedId);
  const order = sessionOrder(sess);
  const idx = order.indexOf(expectedId);
  const groups = order.map((tid, i) => {
    const tr = trayById(tid);
    const sum = traySummary(sess, tid);
    const done = sum.done === sum.total;
    return { i, tid, tr, sum, done, gate: tid === expectedId };
  }).filter((x) => !x.done || x.gate);
  panel.hidden = false;
  panel.innerHTML =
    '<div class="gate-expect">预期字盘：<b>' + esc(expected.name) + '</b>' +
    (expected.scan_code ? ' <span class="mono">（' + esc(expected.scan_code) +
      '）</span>' : ' <span class="dim">（该盘未设扫描码，可直接确认）</span>') +
    '</div>' +
    '<div class="scan-row"><input id="gateInput" autocomplete="off" ' +
      'placeholder="扫描实体字盘上的字盘码后回车">' +
      '<button id="btnGateConfirm" class="primary">确认换盘</button></div>' +
    '<div id="gateMsg" class="gate-msg"></div>' +
    '<div class="gate-seq">' + groups.map((x) =>
      '<span class="gate-step' + (x.gate ? ' on' : x.done ? ' fin' : '') + '">' +
      (x.i + 1) + '. ' + esc(x.tr.name) + '（' +
      (x.sum.total - x.sum.done) + ' 格）</span>').join(' → ') + '</div>';
  $('#btnGateConfirm').onclick = doConfirmTray;
  $('#gateInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') doConfirmTray();
  });
  setTimeout(() => { const i = $('#gateInput'); if (i) i.focus(); }, 0);
}

async function doConfirmTray() {
  if (!S.session) return;
  const input = $('#gateInput');
  const code = input ? input.value.trim() : '';
  await guard(async () => {
    const res = await api('/api/sessions/' + S.session.id +
      '/confirm-tray', 'POST', { scan_code: code });
    if (res.ok) {
      conflictCtx = null;
      applyState(res.state);
      toast('已确认换盘，请从高亮格位开始');
      $('#scanInput').focus();
    } else {
      conflictCtx = res.conflict;
      applyState(res.state);
    }
  });
}

/* 用户在数量框填写的本次放入枚数（未填 = 全部剩余） */
const userQty = () => parseInt($('#qtyInput').value, 10) || null;

async function doConfirm() {
  const t = curTask();
  if (!t) return;
  const label = $('#scanInput').value.trim();
  await guard(async () => {
    const res = await api('/api/tasks/' + t.id + '/confirm', 'POST',
      { label, qty: userQty() });
    if (res.ok) {
      conflictCtx = null;
      $('#scanInput').value = '';
      $('#qtyInput').value = '';
      applyState(res.state);
      toast(res.state.session ? '已确认放入' : '本批归还完毕');
    } else {
      conflictCtx = res.conflict;
      applyState(res.state);
    }
    focusRunInput();
  });
}

function cellBox(title, c, extra) {
  return '<div class="box"><h4>' + esc(title) + '</h4>' +
    '格号 <b>' + esc(c.label) + '</b><br>' +
    '坐标 (' + Math.round(c.x) + ', ' + Math.round(c.y) + ')<br>' +
    '存量 ' + c.qty + ' / ' + c.capacity +
    (extra || '') + '</div>';
}

function renderConflict() {
  const p = $('#conflictPanel');
  if (!conflictCtx) { p.hidden = true; p.innerHTML = ''; return; }
  const c = conflictCtx;
  let html = '<h3>暂停：' + esc(c.message) + '</h3>';
  if (c.type === 'overflow') {
    const cell = c.cell;
    const hasCand = c.candidates && c.candidates.length > 0;
    // 预览与确认一致：采用本次填写的数量（未填则为全部剩余）
    const need = Math.min(userQty() || c.need, c.need);
    html += '<p>同盘改派前后对比（本次放入 ' + need + ' 枚）：</p><div class="compare">' +
      cellBox('改派前（当前格）', cell,
        '<br><span class="diff-up">+' + need + ' 枚 → ' +
        (cell.qty + need) + '/' + cell.capacity + ' 超容</span>') +
      '<div class="arrow">→</div>' +
      '<div class="box"><h4>改派后（同盘候选格）</h4><div id="candInfo">' +
      (hasCand ? '请选择候选格' : '本盘无可用候选格，请先在字盘编辑页添加空格位') +
      '</div></div>' +
      '</div>' +
      '<div class="row">' +
      (hasCand
        ? '<select id="candSel">' +
          c.candidates.map((k) =>
            '<option value="' + k.id + '">' + esc(k.label) +
            (k.char ? '「' + esc(k.char) + '」' : '（空）') +
            ' 余量 ' + k.free + '</option>').join('') +
          '</select>' +
          '<button id="btnReassign" class="primary">改派并放入</button>'
        : '') +
      '<button id="btnForce">强制放入原格</button>' +
      '<button id="btnCancelConflict">取消</button></div>';
  } else if (c.type === 'mismatch') {
    html += (c.duplicate_of
      ? '<p class="diff-up">注意：该格在本批次中已完成，可能是重复确认。</p>' : '') +
      '<div class="compare">' +
      cellBox('应为', c.expected) +
      '<div class="arrow">≠</div>' +
      cellBox('扫描到', c.scanned) +
      '</div>' +
      '<div class="row">' +
      '<button id="btnReassignScanned" class="primary">实物确属扫描格，改派并放入</button>' +
      '<button id="btnCancelConflict">取消，重新核对</button></div>';
  } else if (c.type === 'tray_mismatch') {
    html += '<div class="compare">' +
      trayBox('预期字盘', c.expected) +
      '<div class="arrow">≠</div>' +
      trayBox('实际扫到', c.scanned) +
      '</div>' +
      '<p class="hint">流程已暂停。请对照预期与实际，把实物换成预期字盘后重新扫描；' +
      '若扫到的确实是当前所需，请检查字盘码设置。</p>' +
      '<div class="row"><button id="btnCancelConflict">知道了，重新换盘</button></div>';
  } else if (c.type === 'tray_unknown') {
    html += '<div class="compare">' + trayBox('预期字盘', c.expected) +
      '<div class="arrow">?</div>' +
      '<div class="box"><h4>实际扫描</h4><span class="mono">' +
      esc(c.scanned_code) + '</span><br>没有任何字盘使用该扫描码</div></div>' +
      '<div class="row"><button id="btnCancelConflict">知道了，重新扫描</button></div>';
  } else {
    html += '<div class="row"><button id="btnCancelConflict">知道了</button></div>';
  }
  p.innerHTML = html;
  p.hidden = false;

  const candSel = $('#candSel');
  if (candSel) {
    const showCand = () => {
      const k = c.candidates.find((x) => String(x.id) === candSel.value);
      if (k) {
        const need = Math.min(userQty() || c.need, c.need);
        $('#candInfo').innerHTML =
          '格号 <b>' + esc(k.label) + '</b><br>坐标 (' +
          Math.round(k.x) + ', ' + Math.round(k.y) + ')<br>存量 ' +
          k.qty + ' / ' + k.capacity +
          '<br><span class="' + (k.free >= need ? 'diff-ok' : 'diff-up') +
          '">+' + need + ' 枚 → ' + (k.qty + need) + '/' + k.capacity +
          '</span>';
      }
    };
    candSel.onchange = showCand;
    showCand();
    $('#btnReassign').onclick = () => doReassign(parseInt(candSel.value, 10));
  }
  const force = $('#btnForce');
  if (force) force.onclick = () => doForce();
  const rs = $('#btnReassignScanned');
  if (rs) rs.onclick = () => doReassign(c.scanned.id);
  const cancel = $('#btnCancelConflict');
  if (cancel) cancel.onclick = () => { conflictCtx = null; renderConflict(); };
}

function trayBox(title, t) {
  return '<div class="box"><h4>' + esc(title) + '</h4>' +
    esc(t.name) + '<br>字盘码 <b class="mono">' +
    esc(t.scan_code || '（未设扫描码）') + '</b></div>';
}

async function doReassign(cellId) {
  const t = curTask();
  if (!t) return;
  await guard(async () => {
    const res = await api('/api/tasks/' + t.id + '/reassign', 'POST',
      { cell_id: cellId });
    if (res.warn) toast(res.warn, true);
    if (res.synced) toast('空格位已登记字种「' + t.char + '」');
    // 改派成功后确认放入：严格采用本次填写的数量
    const r2 = await api('/api/tasks/' + t.id + '/confirm', 'POST',
      { qty: userQty() });
    if (r2.ok) {
      conflictCtx = null;
      $('#scanInput').value = '';
      $('#qtyInput').value = '';
      applyState(r2.state);
      toast('已改派并确认放入');
    } else {
      conflictCtx = r2.conflict;
      applyState(r2.state);
    }
  });
}

async function doForce() {
  const t = curTask();
  if (!t) return;
  await guard(async () => {
    const res = await api('/api/tasks/' + t.id + '/confirm', 'POST',
      { force: true, qty: userQty() });
    if (res.ok) {
      conflictCtx = null;
      $('#qtyInput').value = '';
      applyState(res.state);
      toast('已强制放入（超容）', true);
    } else {
      conflictCtx = res.conflict;
      applyState(res.state);
    }
  });
}

/* ---------------- 字盘编辑 ---------------- */

function renderTrayBar() {
  const sel = $('#traySelect');
  sel.innerHTML = S.trays.map((t) =>
    '<option value="' + t.id + '"' + (t.id === editTrayId ? ' selected' : '') +
    '>' + esc(t.name) + (t.scan_code ? '（' + esc(t.scan_code) + '）' : '') +
    '</option>').join('');
  const t = trayById(editTrayId);
  if (t) {
    $('#tfName').value = t.name;
    $('#tfCode').value = t.scan_code;
  }
  const idx = S.trays.findIndex((x) => x.id === editTrayId);
  $('#btnTrayUp').disabled = idx <= 0;
  $('#btnTrayDown').disabled = idx < 0 || idx >= S.trays.length - 1;
  $('#btnDelTray').disabled = S.trays.length <= 1;
}

function renderTrayEditor() {
  renderTrayBar();
  const cells = editTrayId ? trayCells(editTrayId) : [];
  drawTray($('#editTray'), cells, {
    draggable: true,
    cls: (c) => (c.id === selectedCellId ? 'selected' : ''),
    onClick: (c) => { selectedCellId = c.id; renderTrayEditor(); fillCellForm(); },
    onDrop: (cell, x, y) => guard(async () => {
      const res = await api('/api/cells/' + cell.id, 'PUT', { x, y });
      applyState(res.state);
      toast('已移动 ' + cell.label);
    }),
  });
  fillCellForm();
}

function fillCellForm() {
  const c = S.cells.find((x) => x.id === selectedCellId &&
    x.tray_id === editTrayId);
  $('#cellForm').hidden = !c;
  $('#cellFormHint').hidden = !!c;
  if (!c) return;
  $('#fLabel').value = c.label;
  $('#fChar').value = c.char;
  $('#fFont').value = c.font;
  $('#fSize').value = c.size;
  $('#fQty').value = c.qty;
  $('#fCapacity').value = c.capacity;
  $('#fW').value = c.w;
  $('#fH').value = c.h;
}

function bindTrayBar() {
  $('#traySelect').onchange = (ev) => {
    editTrayId = parseInt(ev.target.value, 10);
    selectedCellId = null;
    renderAll();
  };
  const moveTray = async (d) => {
    const order = S.trays.map((t) => t.id);
    const i = order.indexOf(editTrayId);
    const j = i + d;
    if (i < 0 || j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    await guard(async () => {
      applyState((await api('/api/trays/reorder', 'POST', { order })).state);
      editTrayId = order[j];
      renderAll();
    });
  };
  $('#btnTrayUp').onclick = () => moveTray(-1);
  $('#btnTrayDown').onclick = () => moveTray(1);
  $('#btnNewTray').onclick = () => guard(async () => {
    const res = await api('/api/trays', 'POST', {});
    applyState(res.state);
    editTrayId = res.tray_id;
    renderAll();
    toast('已新建字盘，自动分配扫描码 ' + res.scan_code +
      '（可在上方改名 / 改码，扫描码不可为空或与其他字盘重复）');
  });
  $('#btnDupTray').onclick = () => guard(async () => {
    if (!editTrayId) return;
    const src = trayById(editTrayId);
    const res = await api('/api/trays/' + editTrayId + '/duplicate', 'POST',
      { name: src.name + '·副本' });
    applyState(res.state);
    editTrayId = res.tray_id;
    renderAll();
    toast('已复制布局：' + res.copied + ' 个格位（存量未复制），新盘扫描码 ' +
      res.scan_code);
  });
  $('#btnDelTray').onclick = () => guard(async () => {
    const t = trayById(editTrayId);
    if (!t || !confirm('删除字盘「' + t.name + '」？需先清空其全部格位。')) return;
    applyState((await api('/api/trays/' + t.id, 'DELETE')).state);
    toast('已删除字盘');
  });
  $('#tfName').addEventListener('change', (ev) => guard(async () => {
    if (!editTrayId) return;
    applyState((await api('/api/trays/' + editTrayId, 'PUT',
      { name: ev.target.value })).state);
    toast('字盘名称已保存');
  }));
  $('#tfCode').addEventListener('change', async (ev) => {
    if (!editTrayId) return;
    const prev = (trayById(editTrayId) || {}).scan_code || '';
    try {
      const res = await api('/api/trays/' + editTrayId, 'PUT',
        { scan_code: ev.target.value });
      applyState(res.state);
      toast('字盘扫描码已保存');
    } catch (e) {
      // 校验失败：回滚输入框为已保存的值并明确提示
      ev.target.value = prev;
      toast(e.message, true);
    }
  });
}

function bindCellForm() {
  const map = { fLabel: 'label', fChar: 'char', fFont: 'font', fSize: 'size',
                fQty: 'qty', fCapacity: 'capacity', fW: 'w', fH: 'h' };
  for (const id in map) {
    $('#' + id).addEventListener('change', (ev) => {
      if (!selectedCellId) return;
      const field = map[id];
      let v = ev.target.value;
      if (['qty', 'capacity', 'w', 'h'].includes(field)) v = parseFloat(v) || 0;
      guard(async () => {
        const res = await api('/api/cells/' + selectedCellId, 'PUT',
          { [field]: v });
        applyState(res.state);
        toast('已保存');
      });
    });
  }
  $('#btnNewCell').onclick = () => guard(async () => {
    if (!editTrayId) return;
    const label = prompt('新格号（同一字盘内唯一，如 H1）：');
    if (!label) return;
    const res = await api('/api/cells', 'POST',
      { tray_id: editTrayId, label, x: 20, y: 20, capacity: 50 });
    applyState(res.state);
    selectedCellId = res.state.cells.filter(
      (c) => c.tray_id === editTrayId && c.label === label).pop().id;
    renderAll();
    toast('已创建格位 ' + label);
  });
  $('#btnDeleteCell').onclick = () => guard(async () => {
    const c = S.cells.find((x) => x.id === selectedCellId);
    if (!c || !confirm('删除格位 ' + c.label + '？')) return;
    applyState((await api('/api/cells/' + c.id, 'DELETE')).state);
    selectedCellId = null;
    toast('已删除');
  });
  $('#btnGenRow').onclick = () => guard(async () => {
    if (!editTrayId) return;
    const prefix = $('#gPrefix').value.trim() || 'H';
    const cols = parseInt($('#gCols').value, 10) || 8;
    const x0 = parseFloat($('#gX').value) || 0;
    const y0 = parseFloat($('#gY').value) || 0;
    const gap = parseFloat($('#gGap').value) || 54;
    const cap = parseInt($('#gCap').value, 10) || 50;
    const cells = [];
    for (let i = 0; i < cols; i++) {
      cells.push({ label: prefix + (i + 1), x: x0 + i * gap, y: y0,
                   capacity: cap });
    }
    const res = await api('/api/cells/bulk', 'POST',
      { tray_id: editTrayId, cells });
    applyState(res.state);
    toast('已生成 ' + res.created + ' 格' +
      (res.skipped.length ? '，跳过盘内已存在：' + res.skipped.join(' ') : ''));
  });
}

/* ---------------- 待确认 ---------------- */

function renderPending() {
  const badge = $('#pendingBadge');
  badge.hidden = !S.pending.length;
  badge.textContent = S.pending.length;
  const tb = $('#pendingTable tbody');
  tb.innerHTML = '';
  $('#pendingEmpty').hidden = S.pending.length > 0;
  for (const p of S.pending) {
    const tr = document.createElement('tr');
    const reason = (S.reason_names && S.reason_names[p.reason]) || p.reason;
    // 候选格：字符相同者优先，其次空格位；标注所属字盘
    const opt = (c) =>
      '<option value="' + c.id + '">[' +
      esc((trayById(c.tray_id) || {}).name || '?') + '] ' + esc(c.label) +
      (c.char ? '「' + esc(c.char) + '」' : '（空）') + '</option>';
    const cands = S.cells
      .filter((c) => c.char === p.raw || c.char === '')
      .concat(S.cells.filter((c) => c.char !== p.raw && c.char !== ''));
    tr.innerHTML =
      '<td class="big-char">' + esc(p.raw) + '</td>' +
      '<td>' + p.qty + '</td>' +
      '<td>' + esc(reason) + '</td>' +
      '<td class="muted">' + esc(p.suggestion) + '</td>' +
      '<td><select>' + cands.slice(0, 200).map(opt).join('') + '</select></td>' +
      '<td><button class="ok">归位</button> <button class="ign">忽略</button></td>';
    tr.querySelector('.ok').onclick = () => guard(async () => {
      const cellId = parseInt(tr.querySelector('select').value, 10);
      const res = await api('/api/pending/' + p.id + '/resolve', 'POST',
        { cell_id: cellId });
      applyState(res.state);
      toast('已归位，加入对应字盘的归还顺序');
    });
    tr.querySelector('.ign').onclick = () => guard(async () => {
      applyState((await api('/api/pending/' + p.id + '/resolve', 'POST',
        { ignore: true })).state);
      toast('已忽略');
    });
    tb.appendChild(tr);
  }
}

/* ---------------- 数据页 ---------------- */

function renderData() {
  const el = $('#conflictList');
  const cf = S.conflicts || { overflow_cells: [], projected: [] };
  const cname = (c) => {
    const t = trayById(c.tray_id);
    return (t ? t.name + ' / ' : '') + c.label;
  };
  let html = '';
  if (!cf.overflow_cells.length && !cf.projected.length) {
    html = '<p class="hint">无容量冲突。</p>';
  } else {
    if (cf.overflow_cells.length) {
      html += '<h3>已超容格位</h3><table class="tasks"><thead><tr>' +
        '<th>字盘 / 格号</th><th>字符</th><th>存量 / 容量</th><th>超出</th></tr></thead><tbody>' +
        cf.overflow_cells.map((c) =>
          '<tr><td>' + esc(cname(c)) + '</td><td>' + esc(c.char || '（空）') +
          '</td><td>' + c.qty + ' / ' + c.capacity + '</td><td class="diff-up">+' +
          (c.qty - c.capacity) + '</td></tr>').join('') + '</tbody></table>';
    }
    if (cf.projected.length) {
      html += '<h3>当前批次预计超容</h3><table class="tasks"><thead><tr>' +
        '<th>字盘 / 格号</th><th>字符</th><th>存量 / 容量</th><th>待还</th><th>超出</th>' +
        '</tr></thead><tbody>' +
        cf.projected.map((p) =>
          '<tr><td>' + esc(cname(p.cell)) + '</td><td>' +
          esc(p.cell.char || '（空）') + '</td><td>' + p.cell.qty + ' / ' +
          p.cell.capacity + '</td><td>' + p.incoming +
          '</td><td class="diff-up">+' + p.overflow_by + '</td></tr>').join('') +
        '</tbody></table>';
    }
  }
  el.innerHTML = html;
}

/* 打印某批次的临时分拣标签：字盘名、字盘码、格号齐备，按换盘顺序分组 */
function printLabels(sess) {
  if (!sess || !sess.tasks || !sess.tasks.length) {
    toast('没有可打印标签的批次', true);
    return;
  }
  const order = (sess.tray_order || []).filter((id) =>
    sess.tasks.some((t) => t.tray_id === id));
  const byTray = {};
  for (const t of sess.tasks) (byTray[t.tray_id] = byTray[t.tray_id] || []).push(t);
  const cards = [];
  for (const tid of order) {
    const tr = S.trays.find((x) => x.id === tid) ||
      { name: byTray[tid][0].tray_name, scan_code: byTray[tid][0].tray_code };
    byTray[tid].sort((a, b) => a.seq - b.seq).forEach((t) => {
      cards.push(
        '<div class="label-card">' +
        '<div class="lc-head"><span>字盘 <b>' + esc(tr.name) + '</b></span>' +
        '<span class="mono">' + esc(tr.scan_code || '—') + '</span></div>' +
        '<div class="lc-code">格号 <b>' + esc(t.label || '—') + '</b></div>' +
        '<div class="lc-char">' + esc(t.char) + '</div>' +
        '<div class="lc-meta">' + esc(t.font || '—') + ' · ' +
        esc(t.size || '—') + ' · ' + t.qty + ' 枚<br>' +
        '盘内次序 #' + t.seq + ' · 批次 #' + sess.id +
        '<br>' + esc(sess.created_at) + '</div></div>');
    });
  }
  $('#printArea').innerHTML = '<div class="label-grid">' + cards.join('') + '</div>';
  window.print();
}

function bindData() {
  $('#btnBackup').onclick = () => guard(async () => {
    const data = await api('/api/backup');
    const blob = new Blob([JSON.stringify(data, null, 2)],
      { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'sortify-backup-' +
      new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('备份已导出（含字盘归属）');
  });
  $('#restoreFile').addEventListener('change', (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => guard(async () => {
      if (!confirm('恢复备份将覆盖当前全部数据，确定？')) return;
      const res = await api('/api/restore', 'POST', JSON.parse(rd.result));
      applyState(res.state);
      toast('备份已恢复');
    });
    rd.readAsText(f);
    ev.target.value = '';
  });
  // 无进行中批次时，补打最近完成批次的标签
  $('#btnPrint').onclick = () => printLabels(S.session || S.last_done);
  $('#btnPrintReq').onclick = () => guard(async () => {
    let req = S.requisition;
    if (!req) {
      const h = (S.req_history || [])[0];
      if (h) req = (await api('/api/requisitions/' + h.id)).requisition;
    }
    if (!req) { toast('没有可打印的领用单', true); return; }
    printRequisition(req);
  });
  $('#btnPrintStock').onclick = () => guard(async () => {
    let st = (S.stocktakes || [])[0];
    if (!st) {
      const h = (S.stocktake_history || [])[0];
      if (h) {
        st = (await api('/api/stocktakes/' + h.id)).stocktake;
      }
    }
    if (!st) { toast('没有可打印差异单的盘点单', true); return; }
    printStockSheet(st);
  });
}

/* ---------------- 作业页绑定 ---------------- */

function bindRun() {
  $('#btnParse').onclick = () => guard(async () => {
    const text = $('#pasteBox').value;
    const res = await api('/api/sessions', 'POST', { text });
    applyState(res.state);
    $('#pasteBox').value = '';
    const s = res.state.session;
    const nTrays = new Set((s.tasks || []).map((t) => t.tray_id)).size;
    toast('已创建批次：' + (s.tasks || []).length + ' 个目标格 / ' + nTrays +
      ' 个字盘' + (res.state.pending.length ? '，' +
      res.state.pending.length + ' 项待确认' : '') + '，请规划换盘顺序');
  });
  $('#btnLockPlan').onclick = () => savePlan(true);
  $('#btnPlanAbandon').onclick = () => guard(async () => {
    if (!S.session || !confirm('作废当前批次？已确认的数量将保留在格内。')) return;
    applyState((await api('/api/sessions/' + S.session.id + '/abandon',
      'POST')).state);
    planOrder = [];
    conflictCtx = null;
    toast('批次已作废');
  });
  $('#btnConfirm').onclick = doConfirm;
  $('#scanInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') doConfirm();
  });
  $('#btnUndo').onclick = () => guard(async () => {
    if (!S.session) return;
    applyState((await api('/api/sessions/' + S.session.id + '/undo',
      'POST')).state);
    conflictCtx = null;
    toast('已撤销最近一次确认（已回到对应字盘）');
  });
  $('#btnSkip').onclick = () => guard(async () => {
    const t = curTask();
    if (!t) return;
    applyState((await api('/api/tasks/' + t.id + '/skip', 'POST')).state);
    conflictCtx = null;
    toast('已跳过 ' + t.label);
  });
  $('#btnFinish').onclick = () => guard(async () => {
    if (!S.session) return;
    applyState((await api('/api/sessions/' + S.session.id + '/finish',
      'POST')).state);
    toast('批次已完成');
  });
  $('#btnAbandon').onclick = () => guard(async () => {
    if (!S.session || !confirm('作废当前批次？已确认的数量将保留在格内。')) return;
    applyState((await api('/api/sessions/' + S.session.id + '/abandon',
      'POST')).state);
    conflictCtx = null;
    toast('批次已作废');
  });
  // 上一批次收尾：撤销最后一次确认 / 补打标签 / 进入新清单
  $('#btnDoneUndo').onclick = () => guard(async () => {
    if (!S.last_done) return;
    applyState((await api('/api/sessions/' + S.last_done.id + '/undo',
      'POST')).state);
    toast('已撤销，批次恢复进行中');
    switchView('run');
  });
  $('#btnDonePrint').onclick = () => printLabels(S.last_done);
  $('#btnNewSession').onclick = () => {
    $('#pasteBox').scrollIntoView({ behavior: 'smooth', block: 'center' });
    $('#pasteBox').focus();
  };
  // 数量变化时实时刷新冲突面板的预览
  $('#qtyInput').addEventListener('input', () => {
    if (conflictCtx) renderConflict();
  });
  document.addEventListener('keydown', (ev) => {
    if (view !== 'run' || !S.session) return;
    const tag = (ev.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if (ev.key === 'u' || ev.key === 'U') $('#btnUndo').click();
  });
}

/* ---------------- 实体盘点 ---------------- */

let stockSelId = null;        // 盘点页当前打开的盘点单
let stockConflict = null;     // 盘点扫码冲突
let moveStageCtx = null;      // 移格复扫面板上下文 {taskId, inputVal}

/* ---------------- 配字领用 ---------------- */

let reqSelId = null;          // 领用页当前查看的历史领用单
let reqConflict = null;       // 领用扫码冲突上下文
let reqTrayGate = null;       // 换盘闸门扫码结果冲突

const stockById = (id) =>
  (S.stocktakes || []).find((x) => x.id === id) || null;

function stockName(st, field) {
  const map = S.stock_status_names || {};
  return map[st[field]] || st[field];
}
const discName = (k) => (S.disc_kind_names || {})[k] || k;
const reviewName = (k) => (S.review_names || {})[k] || k;
const countName = (k) => (S.count_status_names || {})[k] || k;
const specText = (ch, font, size) =>
  (ch || '（空）') + ' ' + (font || '—') + ' ' + (size || '—');

function renderStock() {
  const list = S.stocktakes || [];
  const hist = S.stocktake_history || [];
  $('#stockBadge').hidden = list.length === 0;
  $('#stockBadge').textContent = list.length;
  if (!stockById(stockSelId)) {
    stockSelId = list.length ? list[0].id : (hist.length ? null : null);
  }
  // 进行中盘点单卡片
  $('#stockActiveList').innerHTML = list.length
    ? list.map((st) =>
      '<div class="stock-item' + (st.id === stockSelId ? ' sel' : '') + '">' +
      '<div><b>' + esc(st.tray_name) + '</b> ' +
      '<span class="mono dim">' + esc(st.tray_code || '—') + '</span> ' +
      '<span class="stk-status st-' + st.status + '">' +
      stockName(st, 'status') + '</span></div>' +
      '<div class="muted">盘点单 #' + st.id + ' · ' + st.n_counted + '/' +
      st.total + ' 格 · 待复核 ' + st.n_flagged +
      ' · 开始 ' + esc(st.started_at) + '</div>' +
      '<div class="row"><button data-open="' + st.id + '"' +
      (st.id === stockSelId ? ' class="primary"' : '') + '>' +
      (st.id === stockSelId ? '正在查看' : '进入盘点') + '</button></div></div>'
    ).join('')
    : '<p class="hint">当前没有进行中的盘点单。</p>';
  $('#stockActiveList').querySelectorAll('[data-open]').forEach((b) => {
    b.onclick = () => {
      stockSelId = parseInt(b.dataset.open, 10);
      stockConflict = null;
      renderAll();
    };
  });
  // 历史
  $('#stockHistory').innerHTML = hist.length
    ? '<table class="tasks"><thead><tr><th>#</th><th>字盘</th><th>状态</th>' +
      '<th>进度</th><th>开始</th><th>入账</th><th></th></tr></thead><tbody>' +
      hist.map((st) =>
      '<tr><td>' + st.id + '</td><td>' + esc(st.tray_name) +
      ' <span class="mono dim">' + esc(st.tray_code || '—') + '</span></td>' +
      '<td>' + stockName(st, 'status') + '</td><td>' + st.n_counted + '/' +
      st.total + '</td><td class="muted">' + esc(st.started_at) +
      '</td><td class="muted">' + esc(st.posted_at || '—') + '</td>' +
      '<td>' + (st.status === 'posted'
        ? '<button data-detail="' + st.id + '">查看入账</button>'
        : '<button data-detail="' + st.id + '">查看</button>') +
      ' <button data-print="' + st.id + '">打印差异单</button></td></tr>').join('')
      + '</tbody></table>'
    : '<p class="hint">暂无历史盘点单。</p>';
  $('#stockHistory').querySelectorAll('[data-detail]').forEach((b) => {
    b.onclick = () => guard(async () => {
      const res = await api('/api/stocktakes/' + b.dataset.detail);
      stockSelId = res.stocktake.id;
      stockConflict = null;
      renderStockWork(res.stocktake);
      $('#stockWork').scrollIntoView({ behavior: 'smooth' });
    });
  });
  $('#stockHistory').querySelectorAll('[data-print]').forEach((b) => {
    b.onclick = () => guard(async () => {
      const res = await api('/api/stocktakes/' + b.dataset.print);
      printStockSheet(res.stocktake);
    });
  });
  // 工作台
  const st = stockById(stockSelId);
  const work = $('#stockWork');
  if (st) {
    work.hidden = false;
    renderStockWork(st);
  } else {
    work.hidden = true;
    work.innerHTML = '';
  }
}

function renderStockWork(st) {
  const work = $('#stockWork');
  if (st.status === 'counting') work.innerHTML = stockCountingHtml(st);
  else if (st.status === 'reviewing') work.innerHTML = stockReviewHtml(st);
  else work.innerHTML = stockFinishedHtml(st);
  bindStockWork(st);
}

/* ---- 盘点中 ---- */

function stockCountingHtml(st) {
  const cur = st.cells.find((c) => c.status === 'pending');
  const done = st.cells.filter((c) => c.status !== 'pending').length;
  return '<div class="stock-layout">' +
    '<div class="tray-wrap panel"><div class="tray-titlebar">' +
    '<h2>盘点：' + esc(st.tray_name) + '</h2>' +
    '<span class="mono muted">' + esc(st.tray_code) + ' · 盘点单 #' + st.id +
    '</span></div>' + stockSvgBlock(st, cur) +
    '<div class="legend"><span><i class="sw sw-stock-cur"></i>当前待盘</span>' +
    '<span><i class="sw sw-stock-done"></i>已盘</span>' +
    '<span><i class="sw sw-stock-flag"></i>待复核</span></div></div>' +
    '<div class="stock-side">' +
    '<div class="panel"><div class="row between"><h3>盘点进度</h3>' +
    '<span class="muted">' + done + '/' + st.total + '</span></div>' +
    '<div class="progress"><div style="width:' + (st.total ? done / st.total * 100 : 0) +
      '%;height:100%;background:var(--done)"></div></div>' +
    (cur
      ? '<div class="cur-task"><div class="cur-char">' +
        esc(cur.book_char || '空') + '</div>' +
        '<div class="cur-meta"><div>第 <b>' + cur.seq + '</b> 格 · 格号 <b>' +
        esc(cur.book_label) + '</b></div><div>账面：' +
        esc(specText(cur.book_char, cur.book_font, cur.book_size)) +
        ' · <b>' + cur.book_qty + '</b> 枚</div></div></div>' +
        '<label class="stock-field">扫描格号 / 输入当前格号' +
        '<input id="scLabel" autocomplete="off" class="mono" value="' +
        esc(cur.book_label) + '" placeholder="扫到当前高亮格号"></label>' +
        '<label class="stock-field">实物点数' +
        '<input id="scQty" type="number" min="0" placeholder="实数，如 18"></label>' +
        '<details class="spec-details"><summary>字符 / 字体 / 字号与账面不符？录入实物规格（三项可分别填写）</summary>' +
        '<p class="hint" style="margin:.2rem 0">哪一项与账面不同就填哪一项，其余留空即按账面；只改字体或只改字号同样会列为差异。</p>' +
        '<div class="form" style="margin-top:.4rem">' +
        '<label>实物字符 <input id="scChar" maxlength="4" placeholder="留空=与账面一致"></label>' +
        '<label>实物字体 <input id="scFont" placeholder="留空=同账面，可单独改"></label>' +
        '<label>实物字号 <input id="scSize" placeholder="留空=同账面，可单独改"></label>' +
        '</div></details>' +
        '<div class="row"><button id="btnScSubmit" class="primary">录入本格</button></div>' +
        '<div class="row">' +
        '<button data-flag="mixed">混字待复核</button>' +
        '<button data-flag="unrecognized">无法辨认</button>' +
        '<button data-flag="blocked">暂时不能盘</button></div>' +
        '<div class="row"><button id="btnScUndo">撤销上一格</button>' +
        '<button id="btnScCancel" class="danger">取消盘点单</button></div>'
      : '<p class="hint">全部格位已录入，正在生成差异…</p>') +
    '<div id="stockConflict"></div></div>' +
    '<div class="panel"><h3>已盘 / 待复核</h3>' + stockCountedList(st) + '</div>' +
    '</div></div>';
}

function stockSvgBlock(st, curCell) {
  return '<svg id="stockTray" class="tray" role="img" aria-label="盘点字盘"></svg>';
}

function stockCountedList(st) {
  const recent = st.cells.filter((c) => c.status !== 'pending')
    .slice(-12).reverse();
  if (!recent.length) return '<p class="hint">尚无录入。</p>';
  return '<div class="stock-donelist">' + recent.map((c) =>
    '<div class="stock-done stk-' + c.status + '"><b>' + esc(c.cell_label) +
    '</b> ' + (c.status === 'counted'
      ? (c.actual_qty + ' 枚' +
         ((c.actual_char !== c.book_char || c.actual_font !== c.book_font ||
           c.actual_size !== c.book_size)
          ? ' · <span class="diff-up">规格不符：' +
            esc(specText(c.actual_char, c.actual_font, c.actual_size)) + '</span>'
          : ''))
      : '<span class="diff-up">' + countName(c.status) + '</span>') +
    '</div>').join('') + '</div>';
}

/* ---- 差异复核 ---- */

function stockDiscMap(st) {
  // 每格：review 项 + short/surplus 项；配对覆盖数量
  const byCell = {};
  for (const d of st.discrepancies) {
    (byCell[d.cell_id] = byCell[d.cell_id] || []).push(d);
  }
  const covered = {};
  for (const p of st.pairs) {
    if (p.status === 'candidate') continue;
    covered[p.src_disc_id] = (covered[p.src_disc_id] || 0) + p.qty;
    covered[p.dst_disc_id] = (covered[p.dst_disc_id] || 0) + p.qty;
  }
  return { byCell, covered };
}

function stockReviewHtml(st) {
  const { byCell, covered } = stockDiscMap(st);
  const pairs = st.pairs;
  const moves = st.move_tasks;
  const pv = st.preview || { changes: [], errors: [], ready: false };
  const nDisc = st.discrepancies.length;
  let body = '';
  if (!nDisc) {
    body = '<div class="panel"><h3>账实相符</h3>' +
      '<p class="hint">全盘格位账面与实物一致，可以直接入账（不改变存量）。</p>' +
      '<div class="row"><button id="btnScPost" class="primary">一次性入账</button>' +
      '<button id="btnScCancel" class="danger">取消盘点单</button></div></div>';
  } else {
    body += '<div class="panel"><h3>差异预览（入账前）</h3>' +
      '<div id="stockPreview">' + stockPreviewHtml(st, pv) + '</div>' +
      '<div class="row">' +
      '<button id="btnScPost" class="primary" ' + (pv.ready ? '' : 'disabled') +
      '>一次性入账</button>' +
      '<button id="btnScPrint">打印差异单</button>' +
      '<button id="btnScCancel" class="danger">取消盘点单</button></div></div>';
    if (pairs.length) {
      body += '<div class="panel"><h3>错放候选（同规格跨格相反差额，不自动改库存）</h3>' +
        stockPairsHtml(st, pairs, moves) + '</div>';
    }
    body += '<div class="panel"><h3>按格差异：短缺 / 溢出 / 待复核</h3>' +
      '<table class="tasks"><thead><tr><th>格号</th><th>账面</th><th>实物</th>' +
      '<th>差异</th><th>处理</th></tr></thead><tbody>' +
      st.cells.filter((c) => byCell[c.cell_id]).map((c) => {
        const ds = byCell[c.cell_id];
        const diffHtml = ds.map((d) => {
          if (d.kind === 'review') {
            return '<div class="diff-up">待复核：' + reviewName(d.review_kind) +
              '</div>';
          }
          const cov = covered[d.id] || 0;
          const res = d.qty - cov;
          return '<div class="' + (d.kind === 'short' ? 'diff-up' : 'diff-ok') +
            '">' + discName(d.kind) + ' ' + esc(specText(d.spec_char, d.spec_font,
            d.spec_size)) + ' ' + d.qty + ' 枚' +
            (cov ? '（错放/核销 ' + cov + '，余 ' + Math.max(res, 0) + '）' : '') +
            (d.decision ? ' <b>→ ' + (d.decision === 'gain' ? '盘盈' : '盘亏') +
              '</b>' : '') + '</div>';
        }).join('');
        return '<tr><td><b>' + esc(c.cell_label) + '</b></td>' +
          '<td>' + esc(specText(c.book_char, c.book_font, c.book_size)) + '<br>' +
          c.book_qty + ' 枚</td>' +
          '<td>' + (c.status === 'counted'
            ? esc(specText(c.actual_char, c.actual_font, c.actual_size)) + '<br>' +
              c.actual_qty + ' 枚'
            : '<span class="diff-up">' + countName(c.status) + '</span>') + '</td>' +
          '<td>' + diffHtml + '</td>' +
          '<td>' + stockDiscActionsHtml(c, ds, covered) + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }
  return '<div class="stock-review">' + body + '</div>';
}

function stockDiscActionsHtml(c, ds, covered) {
  return ds.map((d) => {
    if (d.kind === 'review') {
      return '<button data-recheck="' + c.cell_id + '">复盘该格</button>';
    }
    const res = d.qty - (covered[d.id] || 0);
    if (d.decision) {
      return '<button data-revoke-disc="' + d.id + '">撤销' +
        (d.decision === 'gain' ? '盘盈' : '盘亏') + '</button>';
    }
    if (res <= 0) return '<span class="muted">已由错放处理</span>';
    return (d.kind === 'surplus'
      ? '<button class="ok" data-decide="gain" data-did="' + d.id +
        '">认定盘盈</button>'
      : '<button class="ign" data-decide="loss" data-did="' + d.id +
        '">认定盘亏</button>');
  }).join(' ');
}

function stockPairsHtml(st, pairs, moves) {
  return pairs.map((p) => {
    const m = p.move_task_id
      ? moves.find((x) => x.id === p.move_task_id) : null;
    const stateTxt = { candidate: '待处理', tasked: '已生成移格 #' + p.move_task_id,
      moved: '移格已复扫 #' + p.move_task_id, writeoff: '已核销（盘盈/盘亏）',
      posted: '已入账' }[p.status] || p.status;
    let acts = '';
    if (p.status === 'candidate') {
      acts = '<button class="primary" data-move="' + p.id + '">生成移格任务</button> ' +
        '<button data-writeoff="' + p.id + '">不移动，核销为盘盈/盘亏</button>';
    } else if (m && (m.status === 'pending' || m.status === 'src_ok'
                     || m.status === 'done')) {
      acts = '<button data-scanmove="' + m.id + '">' +
        (m.status === 'done' ? '移格已复扫（可取消）' : '双端复扫') + '</button> ' +
        '<button data-cancelmove="' + m.id + '" class="danger">取消移格</button> ' +
        '<button data-revokepair="' + p.id + '">撤销候选处理</button>';
    } else {
      acts = '<button data-revokepair="' + p.id + '">撤销</button>';
    }
    return '<div class="pair-line ' + p.status + '"><div>' +
      '<b>' + p.code + '</b>：' + esc(p.spec_char) + ' ' +
      esc(p.spec_font || '—') + ' ' + esc(p.spec_size || '—') + ' ×' + p.qty +
      '　<span class="diff-ok">溢出格 ' + esc(p.src_label) + '</span> → ' +
      '<span class="diff-up">短缺格 ' + esc(p.dst_label) + '</span></div>' +
      '<div class="muted">状态：' + stateTxt + '</div><div class="row">' + acts +
      '</div>' + (m && moveStageCtx && moveStageCtx.taskId === m.id
        ? '<div id="moveScanBox" class="move-scan"></div>' : '') + '</div>';
  }).join('');
}

function stockPreviewHtml(st, pv) {
  if (!pv.changes.length && !pv.errors.length) {
    return '<p class="hint">入账不会改变任何存量。</p>';
  }
  let html = '';
  if (pv.errors.length) {
    html += '<p class="diff-up">不能入账：</p><ul class="stock-errors">' +
      pv.errors.map((e) => '<li>' + esc(e) + '</li>').join('') + '</ul>';
  }
  if (pv.changes.length) {
    html += '<table class="tasks"><thead><tr><th>格号</th><th>账面值</th>' +
      '<th>实物</th><th>移格</th><th>入账后</th><th>规格变更</th></tr></thead><tbody>' +
      pv.changes.map((c) =>
      '<tr><td><b>' + esc(c.label) + '</b></td><td>' + c.before_qty + '</td>' +
      '<td>' + c.actual_qty + '</td><td>' +
      (c.move_out ? '<span class="diff-up">出 ' + c.move_out + '</span>' : '') +
      (c.move_in ? '<span class="diff-ok">入 ' + c.move_in + '</span>' : '') +
      (c.move_out || c.move_in ? '' : '—') + '</td><td><b>' + c.final_qty +
      '</b></td><td>' + (c.spec_changed
        ? '<span class="diff-up">' +
          esc(specText(c.actual_char, c.actual_font, c.actual_size)) + ' → ' +
          esc(specText(c.final_char, c.final_font, c.final_size)) + '</span>'
        : '—') + '</td></tr>').join('') + '</tbody></table>';
  }
  html += '<p class="hint">短缺 ' + pv.n_short + ' 项 · 溢出 ' + pv.n_surplus +
    ' 项 · 待复核 ' + pv.n_review + ' 项。入账后每格保留盘点前后值，且不可再撤销。</p>';
  return html;
}

/* ---- 已入账 / 已取消 ---- */

function stockFinishedHtml(st) {
  const posted = st.status === 'posted';
  let html = '<div class="panel"><h2>盘点单 #' + st.id + ' · ' +
    stockName(st, 'status') + '</h2>' +
    '<p class="hint">字盘 <b>' + esc(st.tray_name) + '</b>（' +
    esc(st.tray_code) + '）· 开始 ' + esc(st.started_at) +
    (posted ? ' · 入账 ' + esc(st.posted_at) : ' · 取消 ' + esc(st.cancelled_at)) +
    '</p></div>';
  if (posted) {
    html += '<div class="panel"><h3>入账明细（保留盘点前后值）</h3>' +
      '<table class="tasks"><thead><tr><th>格号</th><th>类型</th><th>数量</th>' +
      '<th>前</th><th>后</th></tr></thead><tbody>' +
      st.postings.map((p) => {
        const cc = st.cells.find((c) => c.cell_id === p.cell_id);
        const kn = { gain: '盘盈', loss: '盘亏', spec: '规格订正',
          move_out: '移格出', move_in: '移格入' }[p.kind] || p.kind;
        return '<tr><td>' + esc(cc ? cc.cell_label : '#' + p.cell_id) +
          '</td><td>' + kn + (p.move_task_id ? '（移格 #' + p.move_task_id + '）'
            : '') + '</td><td>' + p.qty + '</td><td>' + p.before_qty +
          '</td><td><b>' + p.after_qty + '</b></td></tr>';
      }).join('') + '</tbody></table></div>';
  }
  html += '<div class="panel"><h3>盘点记录</h3>' + stockFinishedCells(st) +
    '<div class="row"><button data-print="' + st.id + '">打印差异单</button></div></div>';
  return html;
}

function stockFinishedCells(st) {
  return '<table class="tasks"><thead><tr><th>格号</th><th>账面</th><th>实物</th>' +
    '<th>状态</th></tr></thead><tbody>' + st.cells.map((c) =>
    '<tr><td>' + esc(c.cell_label) + '</td><td>' +
    esc(specText(c.book_char, c.book_font, c.book_size)) + ' · ' + c.book_qty +
    '</td><td>' + (c.actual_qty == null
      ? '—'
      : esc(specText(c.actual_char, c.actual_font, c.actual_size)) + ' · ' +
        c.actual_qty) + '</td><td>' + countName(c.status) + '</td></tr>').join('')
    + '</tbody></table>';
}

/* ---- 盘点字盘绘制 ---- */

function drawStockTray(st, cur) {
  const cells = S.cells.filter((c) => c.tray_id === st.tray_id);
  const scByCell = {};
  for (const sc of st.cells) scByCell[sc.cell_id] = sc;
  drawTray($('#stockTray'), cells, {
    cls: (c) => {
      const sc = scByCell[c.id];
      if (cur && c.id === cur.cell_id) return 'stock-cur';
      if (sc) {
        if (sc.status === 'counted') return 'stock-done';
        if (sc.status === 'pending') return '';
        return 'stock-flag';
      }
      return '';
    },
    sub: (c) => {
      const sc = scByCell[c.id];
      if (!sc || sc.status === 'pending') return null;
      if (sc.status === 'counted') {
        const diff = sc.actual_qty - sc.book_qty;
        return { text: sc.actual_qty, cls: diff ? (diff < 0
          ? 'cell-sub-down' : 'cell-sub-up') : '' };
      }
      return { text: { mixed: '混', unrecognized: '?', blocked: '禁' }[sc.status],
               cls: 'cell-sub-flag' };
    },
    onClick: (c) => {
      const inp = $('#scLabel');
      if (inp) { inp.value = c.label; $('#scQty').focus(); }
    },
  });
}

/* ---- 事件绑定 ---- */

function bindStockWork(st) {
  // 字盘图（盘点中）
  const svg = $('#stockTray');
  if (svg && st.status === 'counting') {
    drawStockTray(st, st.cells.find((c) => c.status === 'pending'));
  }
  const startBtn = $('#btnScSubmit');
  if (startBtn) startBtn.onclick = () => submitStockCount(st);
  const labelInput = $('#scLabel');
  if (labelInput) {
    labelInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); $('#scQty').focus(); }
    });
    setTimeout(() => $('#scQty').focus(), 0);
  }
  const qty = $('#scQty');
  if (qty) qty.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') submitStockCount(st);
  });
  document.querySelectorAll('#stockWork [data-flag]').forEach((b) => {
    b.onclick = () => submitStockCount(st, b.dataset.flag);
  });
  const undo = $('#btnScUndo');
  if (undo) undo.onclick = () => guard(async () => {
    applyState((await api('/api/stocktakes/' + st.id + '/undo-count',
      'POST')).state);
    toast('已撤销上一格录入');
  });
  document.querySelectorAll('#stockWork #btnScCancel').forEach((b) => {
    b.onclick = () => guard(async () => {
      if (!confirm('取消盘点单 #' + st.id + '？已录入数据保留记录，盘锁解除。')) return;
      applyState((await api('/api/stocktakes/' + st.id + '/cancel',
        'POST')).state);
      stockSelId = null;
      toast('盘点单已取消');
    });
  });
  const post = $('#btnScPost');
  if (post) post.onclick = () => guard(async () => {
    if (!confirm('确认一次性入账？入账后保留盘点前后值，决议不可再撤销。')) return;
    const res = await api('/api/stocktakes/' + st.id + '/post', 'POST');
    applyState(res.state);
    stockSelId = null;
    toast('盘点差异已入账');
  });
  const pr = $('#btnScPrint');
  if (pr) pr.onclick = () => printStockSheet(st);
  document.querySelectorAll('#stockWork [data-recheck]').forEach((b) => {
    b.onclick = () => guard(async () => {
      applyState((await api('/api/stocktakes/' + st.id + '/recheck', 'POST',
        { cell_id: parseInt(b.dataset.recheck, 10) })).state);
      toast('该格已拉回复盘，请按高亮重新录入');
    });
  });
  document.querySelectorAll('#stockWork [data-decide]').forEach((b) => {
    b.onclick = () => guard(async () => {
      applyState((await api('/api/discrepancies/' + b.dataset.did + '/decide',
        'POST', { decision: b.dataset.decide })).state);
      toast(b.dataset.decide === 'gain' ? '已认定盘盈' : '已认定盘亏');
    });
  });
  document.querySelectorAll('#stockWork [data-revoke-disc]').forEach((b) => {
    b.onclick = () => guard(async () => {
      applyState((await api('/api/discrepancies/' + b.dataset.revokeDisc +
        '/revoke', 'POST')).state);
      toast('决议已撤销');
    });
  });
  document.querySelectorAll('#stockWork [data-move]').forEach((b) => {
    b.onclick = () => guard(async () => {
      const res = await api('/api/pairs/' + b.dataset.move + '/move', 'POST');
      applyState(res.state);
      moveStageCtx = { taskId: res.move_task_id };
      toast('移格任务已生成，请依次复扫来源格与目标格');
    });
  });
  document.querySelectorAll('#stockWork [data-writeoff]').forEach((b) => {
    b.onclick = () => guard(async () => {
      applyState((await api('/api/pairs/' + b.dataset.writeoff + '/writeoff',
        'POST')).state);
      toast('已核销：来源端记盘盈、目标端记盘亏');
    });
  });
  document.querySelectorAll('#stockWork [data-revokepair]').forEach((b) => {
    b.onclick = () => guard(async () => {
      applyState((await api('/api/pairs/' + b.dataset.revokepair + '/revoke',
        'POST')).state);
      toast('已撤销候选处理');
    });
  });
  document.querySelectorAll('#stockWork [data-cancelmove]').forEach((b) => {
    b.onclick = () => guard(async () => {
      if (!confirm('取消该移格任务？两端都不会改库存。')) return;
      applyState((await api('/api/move-tasks/' + b.dataset.cancelmove +
        '/cancel', 'POST')).state);
      toast('移格任务已取消');
    });
  });
  document.querySelectorAll('#stockWork [data-scanmove]').forEach((b) => {
    b.onclick = () => openMoveScan(st, parseInt(b.dataset.scanmove, 10));
  });
  renderStockConflict(st);
  const box = $('#moveScanBox');
  if (box && moveStageCtx) renderMoveScan(st, box);
}

function submitStockCount(st, flag) {
  const body = { label: $('#scLabel').value.trim() };
  if (flag) {
    body.flag = flag;
    const qv = $('#scQty').value.trim();
    if (qv) body.qty = parseInt(qv, 10);
  } else {
    body.qty = parseInt($('#scQty').value, 10);
    body.actual_char = $('#scChar').value.trim();
    body.actual_font = $('#scFont').value.trim();
    body.actual_size = $('#scSize').value.trim();
  }
  guard(async () => {
    const res = await api('/api/stocktakes/' + st.id + '/count', 'POST', body);
    if (res.ok) {
      stockConflict = null;
      applyState(res.state);
      if (stockById(st.id) && stockById(st.id).status === 'reviewing') {
        toast('全盘录入完成，请处理差异复核');
      }
    } else {
      stockConflict = res.conflict;
      applyState(res.state);
      stockSelId = st.id;
      renderStockConflict(stockById(st.id));
    }
  });
}

function renderStockConflict(st) {
  const box = $('#stockConflict');
  if (!box) return;
  if (!stockConflict) { box.innerHTML = ''; return; }
  const c = stockConflict;
  box.innerHTML = '<div class="panel conflict"><h3>' + esc(c.message) + '</h3>' +
    (c.expected ? '<div class="compare">' +
      cellBox('应盘格', c.expected) +
      (c.scanned ? '<div class="arrow">≠</div>' + cellBox('实际扫描', c.scanned)
        : '') + '</div>' : '') +
    '<div class="row"><button id="btnStockConflictOk">知道了，重新扫描</button></div>' +
    '</div>';
  $('#btnStockConflictOk').onclick = () => {
    stockConflict = null; renderStockConflict(st);
  };
}

function openMoveScan(st, taskId) {
  const pairBtn = document.querySelector('[data-scanmove="' + taskId + '"]');
  const host = pairBtn ? pairBtn.closest('.pair-line') : null;
  if (!host) return;  // 已入账视图无候选行
  let mb = host.querySelector('#moveScanBox');
  if (!mb) {
    mb = document.createElement('div');
    mb.id = 'moveScanBox';
    mb.className = 'move-scan';
    host.appendChild(mb);
  }
  moveStageCtx = { taskId };
  renderMoveScan(st, mb);
}

function renderMoveScan(st, box) {
  const mid = moveStageCtx.taskId;
  const m = st.move_tasks.find((x) => x.id === mid);
  if (!m) { box.remove(); moveStageCtx = null; return; }
  const stage = m.status === 'pending' ? 'source' : 'target';
  const expectLabel = stage === 'source' ? m.source_label : m.target_label;
  const done = m.status === 'done';
  box.innerHTML = (done
    ? '<p class="diff-ok">两端已复扫完成：' + esc(m.source_label) + ' → ' +
      esc(m.target_label) + '（入账时才改库存）</p>'
    : '<p>' + (stage === 'source'
        ? '第 1 步：复扫<b class="diff-ok">来源格</b>（取出 ' +
          esc(m.spec_char) + ' ×' + m.qty + '）'
        : '来源格 ✓ ' + esc(m.source_label) +
          '；第 2 步：复扫<b class="diff-up">目标格</b>') +
      '，当前应扫 <b>' + esc(expectLabel) + '</b></p>' +
      '<div class="scan-row"><input class="mono" id="moveScanInput" ' +
      'placeholder="扫描格号后回车"><button class="primary" id="moveScanBtn">' +
      '确认本端</button></div><div id="moveScanMsg"></div>');
  const inp = box.querySelector('#moveScanInput');
  if (!done) setTimeout(() => inp && inp.focus(), 0);
  const btn = box.querySelector('#moveScanBtn');
  const doScan = () => guard(async () => {
    const res = await api('/api/move-tasks/' + mid + '/scan', 'POST',
      { label: inp.value.trim() });
    if (res.ok) {
      applyState(res.state);
      stockSelId = st.id;
      toast(res.stage_done === 'source' ? '来源格已确认，请复扫目标格'
        : '目标格已确认，移格待入账');
    } else {
      const msg = box.querySelector('#moveScanMsg');
      if (msg) msg.innerHTML = '<span class="diff-up">' +
        esc(res.conflict.message) + '</span>';
    }
  });
  if (btn) {
    btn.onclick = doScan;
    inp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') doScan(); });
  }
}

/* ---- 打印差异单 ---- */

function printStockSheet(st) {
  if (!st) { toast('没有可打印的盘点单', true); return; }
  const { byCell, covered } = st.status === 'reviewing'
    ? stockDiscMap(st) : { byCell: {}, covered: {} };
  const rows = st.cells.map((c) => {
    const ds = (byCell[c.cell_id] || []);
    if (!ds.length) return null;
    const diffs = ds.map((d) => d.kind === 'review'
      ? '待复核：' + reviewName(d.review_kind)
      : discName(d.kind) + ' ' + specText(d.spec_char, d.spec_font, d.spec_size) +
        ' ' + d.qty + ' 枚' + (d.decision ? '（' +
          (d.decision === 'gain' ? '盘盈' : '盘亏') + '）' : '')).join('；');
    return '<tr><td>' + esc(c.cell_label) + '</td><td>' +
      esc(specText(c.book_char, c.book_font, c.book_size)) + ' · ' + c.book_qty +
      '</td><td>' + (c.actual_qty == null
        ? countName(c.status)
        : esc(specText(c.actual_char, c.actual_font, c.actual_size)) + ' · ' +
          c.actual_qty) + '</td><td>' + esc(diffs) + '</td></tr>';
  }).filter(Boolean).join('');
  const pairHtml = st.pairs.length
    ? '<h3>错放候选 / 移格任务</h3><table><thead><tr><th>编号</th><th>规格</th>' +
      '<th>来源格</th><th>目标格</th><th>数量</th><th>状态</th></tr></thead><tbody>' +
      st.pairs.map((p) => {
        const m = p.move_task_id
          ? st.move_tasks.find((x) => x.id === p.move_task_id) : null;
        const txt = { candidate: '候选', tasked: '待复扫', moved: '已复扫',
          writeoff: '核销', posted: '已入账' }[p.status] || p.status;
        return '<tr><td>' + p.code + '</td><td>' +
          esc(specText(p.spec_char, p.spec_font, p.spec_size)) + '</td><td>' +
          esc(p.src_label) + '</td><td>' + esc(p.dst_label) + '</td><td>' + p.qty +
          '</td><td>' + txt + (m ? '（移格 #' + m.id + '）' : '') + '</td></tr>';
      }).join('') + '</tbody></table>' : '';
  $('#printArea').innerHTML =
    '<div class="stock-sheet"><h2>盘点差异单</h2>' +
    '<p>盘点单 #' + st.id + '　字盘：' + esc(st.tray_name) + '（' +
    esc(st.tray_code) + '）　开始：' + esc(st.started_at) +
    '　状态：' + stockName(st, 'status') + '</p>' +
    '<table><thead><tr><th>格号</th><th>账面</th><th>实物</th><th>差异 / 决议</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' + pairHtml +
    '<p class="sign">盘点人：____________　复核人：____________　日期：____________</p>' +
    '</div>';
  window.print();
}

function bindStockStart() {
  const inp = $('#stockStartInput');
  const go = () => guard(async () => {
    const code = inp.value.trim();
    if (!code) return;
    const res = await api('/api/stocktakes', 'POST', { scan_code: code });
    applyState(res.state);
    stockSelId = res.stocktake_id;
    inp.value = '';
    toast('盘点单 #' + res.stocktake_id + ' 已开始，按高亮顺序扫描格号');
  });
  $('#btnStockStart').onclick = go;
  inp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') go(); });
}

/* ---------------- 配字领用 ---------------- */

const REQ_INPUT_KEY = 'sortify-req-input-v1';
let reqDraft = { mode: 'text', include_spaces: false,
                 include_punct: true, text: '' };

function reqName(st, field) {
  return (S.req_status_names || {})[st[field]] || st[field];
}
const allocName = (s) => (S.alloc_status_names || {})[s] || s;

function reqById(id) {
  const list = [S.requisition].concat(S.req_history || []);
  return list.find((x) => x && x.id === id) || null;
}

function reqDemandProgress(req, dm) {
  const allocs = req.allocations.filter((a) => a.demand_id === dm.id);
  const reserved = allocs.reduce((a, x) =>
    a + (x.status === 'cancelled' ? 0 : x.qty), 0);
  const taken = allocs.reduce((a, x) => a + x.taken_qty, 0);
  const gap = allocs.filter((x) => x.status === 'gap')
    .reduce((a, x) => a + x.qty, 0);
  return { reserved, taken, gap, missing: dm.qty - reserved };
}

function reqOrder(req) {
  const fromTasks = Array.from(new Set(
    req.allocations.filter((a) => a.status !== 'cancelled').map((a) => a.tray_id)));
  const stored = (req.tray_order || []).filter((id) =>
    S.trays.some((t) => t.id === id));
  return stored.length ? stored : fromTasks;
}

function reqCurrentAlloc(req) {
  return (req.allocations || []).find((a) => a.status === 'active') || null;
}

function reqGateTray(req) {
  if (!req || req.status !== 'active' || reqCurrentAlloc(req)) return null;
  const pending = new Set(req.allocations
    .filter((a) => a.status === 'reserved').map((a) => a.tray_id));
  return reqOrder(req).find((id) => pending.has(id)) || null;
}

function reqTraySummary(req, tid) {
  const ts = req.allocations.filter((a) => a.tray_id === tid
    && a.status !== 'cancelled');
  return {
    total: ts.length,
    done: ts.filter((a) => a.status === 'done').length,
    gap: ts.filter((a) => a.status === 'gap').length,
    qty: ts.reduce((a, x) => a + x.qty, 0),
    taken: ts.reduce((a, x) => a + x.taken_qty, 0),
  };
}

function renderPick() {
  const badge = $('#reqBadge');
  if (S.requisition) {
    badge.hidden = false;
    badge.textContent = S.requisition.status === 'active'
      ? (S.requisition.allocations.filter((a) =>
        ['reserved', 'active'].includes(a.status)).length || '')
      : '待';
  } else {
    badge.hidden = true;
  }
  const work = $('#reqWork');
  const req = S.requisition;
  if (!req) {
    work.innerHTML = reqFormHtml() + reqActiveNoneHtml();
    bindReqForm(work);
  } else if (req.status === 'planning') {
    work.innerHTML = reqPlanningHtml(req);
    bindReqPlanning(req);
  } else if (req.status === 'active') {
    work.innerHTML = reqActiveHtml(req);
    bindReqActive(req);
    drawReqTray(req);
  }
  renderReqHistory();
}

function reqActiveNoneHtml() {
  const last = (S.req_history || [])[0];
  if (!last) return '';
  const st = last.status;
  return '<div class="panel"><h2>最近领用单：#' + last.id + ' ' + esc(last.name) +
    ' <span class="stk-status st-' + st + '">' +
    (S.req_status_names[st] || st) + '</span></h2>' +
    '<p class="muted">' + esc(last.created_at) + ' · ' + last.n_allocs +
    ' 个来源格 · 实取 ' + last.qty_taken + ' 枚' +
    (last.n_gap ? ' · <span class="diff-up">缺口 ' + last.n_gap + ' 笔</span>' : '') +
    '</p><div class="row"><button data-req-open="' + last.id + '">查看 / 打印</button>' +
    (st === 'done' && !last.returned_session_id
      ? '<button data-req-return="' + last.id + '" class="primary">一键带入归还流程</button>'
      : '') + '</div></div>';
}

/* ---- 新建领用单 ---- */

function reqFormHtml() {
  try {
    const saved = JSON.parse(localStorage.getItem(REQ_INPUT_KEY) || 'null');
    if (saved) Object.assign(reqDraft, saved);
  } catch (e) { /* ignore */ }
  const d = reqDraft;
  return '<div class="panel"><h2>新建配字领用单</h2>' +
    '<p class="hint">粘贴<strong>待排文字</strong>逐字汇总，或切换为' +
    '「字符、数量、字体、字号」清单（写法同归还清单，如 <code>永,12,宋体,五号</code>）。' +
    '系统从<strong>多个字盘的同规格格位</strong>分配可用铅字，并扣除其他未完成领用单的' +
    '预留量；数量不足、仅有异体字或规格不符分别列出，<b>不自动替换</b>。</p>' +
    '<div class="row"><label class="radio"><input type="radio" name="reqMode" ' +
    (d.mode === 'text' ? 'checked' : '') + ' value="text"> 待排文字（逐字计数）</label>' +
    '<label class="radio"><input type="radio" name="reqMode" ' +
    (d.mode === 'list' ? 'checked' : '') + ' value="list"> 字符 / 数量 / 字体 / 字号清单</label></div>' +
    '<textarea id="reqText" rows="7" placeholder="' +
    (d.mode === 'text' ? '例如：永和九年，岁在癸丑' : '例如：&#10;永,12,宋体,五号&#10;體 2&#10;的×30') +
    '">' + esc(d.text) + '</textarea>' +
    '<div class="row">' +
    '<label class="radio"><input type="checkbox" id="reqSpaces" ' +
    (d.include_spaces ? 'checked' : '') + '> 计入空格</label>' +
    '<label class="radio"><input type="checkbox" id="reqPunct" ' +
    (d.include_punct ? 'checked' : '') + '> 计入标点</label></div>' +
    '<p class="hint" id="reqModeHint"></p>' +
    '<div class="row"><button id="btnReqCreate" class="primary">汇总需求并分配字盘</button></div>' +
    '<div id="reqCreateMsg"></div></div>';
}

function bindReqForm(scope) {
  const saveDraft = () => {
    reqDraft.text = $('#reqText').value;
    reqDraft.include_spaces = $('#reqSpaces').checked;
    reqDraft.include_punct = $('#reqPunct').checked;
    localStorage.setItem(REQ_INPUT_KEY, JSON.stringify(reqDraft));
  };
  scope.querySelectorAll('input[name=reqMode]').forEach((r) => {
    r.onchange = () => {
      saveDraft();
      reqDraft.mode = r.value;
      localStorage.setItem(REQ_INPUT_KEY, JSON.stringify(reqDraft));
      renderPick();
    };
  });
  const updateHint = () => {
    const h = $('#reqModeHint');
    if (!h) return;
    const v = $('#reqText').value;
    if (reqDraft.mode === 'text') {
      let nSpace = 0, nPunct = 0, nChar = 0;
      for (const ch of v) {
        const cat = /\p{Z}/u.test(ch) ? 'Z'
          : /\p{P}/u.test(ch) ? 'P' : '';
        if (cat === 'Z') nSpace++;
        else if (cat === 'P') nPunct++;
        else if (ch.trim()) nChar++;
      }
      h.textContent = '当前 ' + nChar + ' 个实字' +
        (nSpace ? '、' + nSpace + ' 个空格' : '') +
        (nPunct ? '、' + nPunct + ' 个标点' : '') + '；字体 / 字号不限，按同字符各格匹配。';
    } else {
      h.textContent = '每行一条，支持 永 12 / 永×12 / 永,12,宋体,五号；未写字体字号时按同字符匹配。';
    }
  };
  $('#reqText').addEventListener('input', () => { saveDraft(); updateHint(); });
  $('#reqSpaces').onchange = saveDraft;
  $('#reqPunct').onchange = saveDraft;
  updateHint();
  $('#btnReqCreate').onclick = () => guard(async () => {
    saveDraft();
    const res = await api('/api/requisitions', 'POST', {
      text: reqDraft.text, mode: reqDraft.mode,
      include_spaces: reqDraft.include_spaces,
      include_punct: reqDraft.include_punct,
    });
    applyState(res.state);
    reqSelId = res.requisition_id;
    let msg = '领用单 #' + res.requisition_id + ' 已生成';
    if (res.dropped_spaces) msg += '，已排除空格 ' + res.dropped_spaces;
    if (res.dropped_punct) msg += '，已排除标点 ' + res.dropped_punct;
    toast(msg);
  });
  scope.querySelectorAll('[data-req-open]').forEach((b) => {
    b.onclick = () => guard(async () => {
      const r = await api('/api/requisitions/' + b.dataset.reqOpen);
      printRequisition(r.requisition);
    });
  });
  scope.querySelectorAll('[data-req-return]').forEach((b) => {
    b.onclick = () => reqToReturn(parseInt(b.dataset.reqReturn, 10));
  });
}

/* ---- 规划页 ---- */

function reqPlanningHtml(req) {
  const order = reqOrder(req);
  const issueBadge = (dm) => {
    if (!dm.issue) return '<span class="diff-ok">已配齐</span>';
    const name = { short: '数量不足', variant: '仅有异体字',
      spec: '规格不符', none: '无格位' }[dm.issue] || dm.issue;
    return '<span class="diff-up">' + name + '</span>';
  };
  const demands = req.demands.map((dm) => {
    const p = reqDemandProgress(req, dm);
    return '<tr><td class="big-char">' + esc(dm.char) + '</td>' +
      '<td>' + esc(dm.font || '任意') + ' / ' + esc(dm.size || '任意') + '</td>' +
      '<td>' + dm.qty + '</td><td>' + p.reserved + '</td>' +
      '<td>' + (p.gap ? '<span class="diff-up">' + p.gap + '</span>' : '—') +
      '</td><td>' + issueBadge(dm) +
      (dm.issue_text ? '<div class="muted">' + esc(dm.issue_text) + '</div>' : '') +
      '</td></tr>';
  }).join('');
  const codeProblems = new Map();
  const seen = new Map();
  for (const tid of order) {
    const tr = trayById(tid);
    const code = (tr.scan_code || '').trim();
    if (!code) codeProblems.set(tid, '未设扫描码');
    else if (seen.has(code.toUpperCase()))
      codeProblems.set(tid, '扫描码重复');
    else seen.set(code.toUpperCase(), tr.name);
  }
  const groups = order.map((tid, i) => {
    const tr = trayById(tid);
    const allocs = req.allocations
      .filter((a) => a.tray_id === tid && a.status !== 'cancelled')
      .sort((a, b) => a.seq - b.seq);
    const sum = reqTraySummary(req, tid);
    return '<div class="tgroup' + (codeProblems.has(tid) ? ' gate' : '') + '">' +
      '<div class="tg-head">' + (i + 1) + '. <b>' + esc(tr.name) + '</b> ' +
      '<span class="mono dim">' + esc(tr.scan_code || '—') + '</span>' +
      '<span class="tg-prog">' + allocs.length + ' 格 / ' +
      sum.qty + ' 枚</span></div>' +
      '<div class="tg-cells">' + allocs.map((a) =>
        '<span class="tg-cell" title="' + esc(a.cell_char) + ' ×' + a.qty +
        '">' + esc(a.label) + '×' + a.qty + '</span>').join(' ') +
      '</div></div>';
  }).join('');
  return '<div class="plan-layout"><div class="panel">' +
    '<div class="row between"><h2>领用单 #' + req.id + ' · ' + esc(req.name) +
    '（待锁定）</h2><span class="muted">' + esc(req.created_at) + '</span></div>' +
    '<table class="tasks"><thead><tr><th>字符</th><th>字体 / 字号</th>' +
    '<th>需求</th><th>已配</th><th>缺口</th><th>状态</th></tr></thead><tbody>' +
    demands + '</tbody></table>' +
    '<div class="row">' +
    '<button id="btnReqLock" class="primary"' +
    (codeProblems.size || !req.allocations.length ? ' disabled' : '') +
    '>锁定来源格，开始领用</button>' +
    '<button id="btnReqCancelPlan" class="danger">取消领用单（释放预留）</button>' +
    '<button id="btnReqPrintPlan">打印领用单</button></div>' +
    (codeProblems.size ? '<p class="diff-up code-warn">涉及字盘存在未设 / 重复扫描码，' +
      '请到字盘编辑页设置后再锁定。</p>' : '') +
    (!req.allocations.length ? '<p class="diff-up code-warn">没有任何可配格位，不能锁定；' +
      '本单需求已逐条列出。</p>' : '') +
    '</div><div class="panel"><h3>按字盘的领用路线（盘内蛇形）</h3>' + groups + '</div></div>';
}

function bindReqPlanning(req) {
  $('#btnReqLock').onclick = () => guard(async () => {
    const res = await api('/api/requisitions/' + req.id + '/plan', 'POST',
      { lock: true });
    applyState(res.state);
    toast('来源格已锁定，请扫描起始字盘的字盘码');
  });
  $('#btnReqCancelPlan').onclick = () => guard(async () => {
    if (!confirm('取消领用单 #' + req.id + '？未取预留将全部释放。')) return;
    applyState((await api('/api/requisitions/' + req.id + '/cancel', 'POST')).state);
    reqConflict = null;
    toast('领用单已取消，预留已释放');
  });
  $('#btnReqPrintPlan').onclick = () => {
    const full = reqById(req.id);
    if (full && full.allocations) printRequisition(full);
  };
}

/* ---- 执行页 ---- */

function reqActiveHtml(req) {
  const order = reqOrder(req);
  const cur = reqCurrentAlloc(req);
  const gateId = reqGateTray(req);
  const curTrayId = cur ? cur.tray_id : (gateId || order[0]);
  const curTray = trayById(curTrayId);
  const taken = req.allocations.reduce((a, x) => a + x.taken_qty, 0);
  const total = req.allocations.filter((a) => a.status !== 'cancelled')
    .reduce((a, x) => a + x.qty, 0);
  const doneAllocs = req.allocations.filter((a) => a.status === 'done').length;
  const totalAllocs = req.allocations.filter((a) =>
    a.status !== 'cancelled').length;
  let curCard = '<p class="hint">等待扫描字盘码，确认换到下一个字盘。</p>';
  if (cur) {
    const remain = cur.qty - cur.taken_qty;
    curCard = '<div class="cur-task"><div class="cur-char">' +
      esc(cur.cell_char) + '</div><div class="cur-meta">' +
      '<div>' + esc(cur.tray_name) + ' · 来源格 <b>' + esc(cur.label) +
      '</b> · ' + esc(cur.cell_font || '—') + ' · ' + esc(cur.cell_size || '—') + '</div>' +
      '<div>应取 <b>' + cur.qty + '</b> 枚 · 已取 ' + cur.taken_qty +
      ' · 格内存量 ' + cur.cell_qty + '（扣除其他预留后可拿 ' + cur.avail_other + '）</div>' +
      '<div class="next-hint">扫描格号核对后确认实取数量；少取可改选同盘来源格或保留缺口。</div>' +
      '</div></div>';
  }
  const groups = order.map((tid) => {
    const tr = trayById(tid);
    const ts = req.allocations.filter((a) => a.tray_id === tid
      && a.status !== 'cancelled').sort((a, b) => a.seq - b.seq);
    const sum = reqTraySummary(req, tid);
    const isCur = tid === curTrayId && !gateId;
    const isGate = tid === gateId;
    return '<div class="tgroup' + (isCur ? ' cur' : isGate ? ' gate' : '') +
      (sum.total === sum.done + sum.gap ? ' done' : '') + '">' +
      '<div class="tg-head">' + (isGate ? '➡ ' : isCur ? '● ' : '○ ') +
      '<b>' + esc(tr.name) + '</b> <span class="mono dim">' +
      esc(tr.scan_code || '—') + '</span><span class="tg-prog">' +
      (sum.done + sum.gap) + '/' + sum.total + '</span></div>' +
      '<div class="tg-cells">' + ts.map((a) =>
        '<span class="tg-cell st-' +
        ({ done: 'done', active: 'active', gap: 'skipped' }[a.status] || 'pending') +
        '" title="' + esc(a.cell_char) + ' 应取' + a.qty + ' 实取' +
        a.taken_qty + '">' + esc(a.label) + (a.status === 'gap' ? '缺' : '') +
        '</span>').join(' ') + '</div></div>';
  }).join('');
  return '<div class="run-layout"><div class="tray-wrap">' +
    '<div class="tray-titlebar"><h2>领用：' + esc(curTray ? curTray.name : '') +
    '</h2><span class="muted mono">' + esc(curTray ? curTray.scan_code : '') +
    ' · 领用单 #' + req.id + '</span></div>' +
    '<svg id="reqTray" class="tray" role="img" aria-label="领用当前字盘"></svg>' +
    '<div class="legend"><span><i class="sw sw-cur"></i>当前来源格</span>' +
    '<span><i class="sw sw-next"></i>盘内下一格</span>' +
    '<span><i class="sw sw-done"></i>已取</span>' +
    '<span><i class="sw sw-over"></i>缺口</span></div>' +
    (gateId
      ? '<div class="panel gate"><h3>请更换字盘并扫描字盘码</h3>' +
        '<div class="gate-expect">预期字盘：<b>' + esc(trayById(gateId).name) +
        '</b> <span class="mono">（' + esc(trayById(gateId).scan_code) + '）</span></div>' +
        '<div class="scan-row"><input id="reqGateInput" autocomplete="off" ' +
        'placeholder="扫描实体字盘上的字盘码后回车">' +
        '<button id="btnReqGate" class="primary">确认换盘</button></div>' +
        '<div id="reqGateMsg" class="gate-msg"></div></div>' : '') +
    '</div><div class="run-side"><div class="panel">' +
    '<div class="row between"><h2>领用单 #' + req.id + '</h2>' +
    '<span class="muted">' + doneAllocs + '/' + totalAllocs + ' 格 · ' +
    taken + '/' + total + ' 枚</span></div>' +
    '<div class="progress"><div style="width:' +
    (totalAllocs ? doneAllocs / totalAllocs * 100 : 0) +
    '%;height:100%;background:var(--done)"></div></div>' + curCard +
    '<div class="scan-row"><input id="reqScan" autocomplete="off" ' +
    'placeholder="扫描 / 输入当前盘格号后回车" ' + (cur ? '' : 'disabled') + '>' +
    '<input id="reqQty" type="number" min="1" placeholder="实取" ' +
    (cur ? '' : 'disabled') + '></div>' +
    '<div class="row"><button id="btnReqPick" class="primary" ' +
    (cur ? '' : 'disabled') + '>确认实取</button>' +
    '<button id="btnReqUndo">撤销上一笔</button></div>' +
    '<div class="row"><button id="btnReqFinish" ' +
    (gateId || cur ? 'disabled' : '') + '>完成领用</button>' +
    '<button id="btnReqCancel" class="danger">取消领用单</button>' +
    '<button id="btnReqPrint">打印</button></div>' +
    '</div><div id="reqConflictBox"></div>' +
    '<div class="panel"><h3>换盘与格位顺序</h3>' + groups + '</div></div></div>';
}

function bindReqActive(req) {
  const gateInp = $('#reqGateInput');
  if (gateInp) {
    const doGate = () => guard(async () => {
      const res = await api('/api/requisitions/' + req.id + '/confirm-tray',
        'POST', { scan_code: gateInp.value.trim() });
      if (res.ok) {
        applyState(res.state);
        toast('已确认换盘，请从高亮来源格开始');
        const s = $('#reqScan'); if (s) s.focus();
      } else {
        reqTrayGate = res.conflict;
        const msg = $('#reqGateMsg');
        if (msg) msg.innerHTML = '<span class="diff-up">' +
          esc(res.conflict.message) + '</span>';
      }
    });
    $('#btnReqGate').onclick = doGate;
    gateInp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') doGate();
    });
    setTimeout(() => gateInp.focus(), 0);
  }
  const doPick = () => guard(async () => {
    const qv = $('#reqQty').value;
    const body = { label: $('#reqScan').value.trim() };
    if (qv) body.qty = parseInt(qv, 10);
    const res = await api('/api/requisitions/' + req.id + '/pick', 'POST', body);
    if (res.ok) {
      reqConflict = null;
      $('#reqScan').value = '';
      $('#reqQty').value = '';
      applyState(res.state);
      toast('已确认实取并扣减库存');
    } else {
      reqConflict = res.conflict;
      applyState(res.state);
    }
  });
  $('#btnReqPick').onclick = doPick;
  $('#reqScan').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') doPick();
  });
  $('#reqQty').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') doPick();
  });
  $('#btnReqUndo').onclick = () => guard(async () => {
    const res = await api('/api/requisitions/' + req.id + '/undo', 'POST');
    applyState(res.state);
    reqConflict = null;
    toast('已撤销上一笔（库存已补回）');
  });
  $('#btnReqFinish').onclick = () => guard(async () => {
    const res = await api('/api/requisitions/' + req.id + '/finish', 'POST');
    applyState(res.state);
    toast('领用单已完成');
  });
  $('#btnReqCancel').onclick = () => guard(async () => {
    if (!confirm('取消领用单？未取预留将释放，已实取数量保留在记录中。')) return;
    applyState((await api('/api/requisitions/' + req.id + '/cancel',
      'POST')).state);
    reqConflict = null;
    toast('领用单已取消');
  });
  $('#btnReqPrint').onclick = () => printRequisition(reqById(req.id) || req);
  renderReqConflict(req);
}

function drawReqTray(req) {
  const svg = $('#reqTray');
  if (!svg) return;
  const cur = reqCurrentAlloc(req);
  const gateId = reqGateTray(req);
  const tid = cur ? cur.tray_id : (gateId || (reqOrder(req)[0]));
  const byCell = {};
  for (const a of req.allocations) {
    if (a.tray_id === tid) byCell[a.cell_id] = a;
  }
  let nextId = null;
  if (cur) {
    const nxt = req.allocations.find((a) => a.tray_id === cur.tray_id
      && a.status === 'reserved' && a.seq > cur.seq);
    nextId = nxt ? nxt.cell_id : null;
  }
  drawTray(svg, tid ? trayCells(tid) : [], {
    cls: (c) => {
      const a = byCell[c.id];
      if (cur && c.id === cur.cell_id) return 'cur';
      if (c.id === nextId) return 'next';
      if (a && a.status === 'done') return 'done';
      if (a && a.status === 'gap') return 'over';
      return '';
    },
    sub: (c) => {
      const a = byCell[c.id];
      if (!a) return null;
      if (a.status === 'gap') return { text: '缺' + a.qty, cls: 'cell-sub-flag' };
      if (a.status === 'done')
        return { text: '取' + a.taken_qty, cls: 'cell-sub-up' };
      return { text: '取' + a.qty, cls: 'cell-sub-down' };
    },
    onClick: (c) => {
      const inp = $('#reqScan');
      if (inp && !inp.disabled) { inp.value = c.label; $('#reqQty').focus(); }
    },
  });
}

/* ---- 领用冲突面板 ---- */

function renderReqConflict(req) {
  const box = $('#reqConflictBox');
  if (!box) return;
  if (!reqConflict) { box.innerHTML = ''; return; }
  const c = reqConflict;
  let html = '<div class="panel conflict"><h3>' + esc(c.message) + '</h3>';
  if (c.type === 'short_pick' || c.type === 'short_avail') {
    const a = c.allocation;
    const opts = (c.candidates || []).map((k) =>
      '<option value="' + k.id + '">' + esc(k.label) + '「' + esc(k.char) +
      '」' + esc(k.font) + ' ' + esc(k.size) + ' · 可拿 ' + k.avail + '</option>')
      .join('');
    html += '<p>原来源格 <b>' + esc(a.label) + '</b>，应取 ' + a.remain +
      ' 枚。确认前不扣库存。</p>' +
      '<div class="row"><label>同盘另一来源格 <select id="reqCandSel">' +
      (opts || '<option value="">无可用同规格格位</option>') + '</select></label>' +
      '<label>原格实取 <input id="reqPicked" type="number" min="0" value="' +
      (c.type === 'short_pick' ? c.picked : 0) + '" style="width:6rem"></label></div>' +
      '<div class="row"><button id="btnReqChange" class="primary"' +
      (!c.candidates.length ? ' disabled' : '') +
      '>改选来源格（新格余量继续扫码确认）</button>' +
      '<button id="btnReqGap">保留差额为缺口</button>' +
      '<button id="btnReqConflictCancel">取消，重新扫描</button></div>';
  } else if (c.type === 'mismatch') {
    html += '<div class="compare">' +
      cellBox('应取来源格', {
        label: c.expected.label, char: c.expected.char, font: c.expected.font,
        size: c.expected.size, qty: c.expected.qty, capacity: 0,
        x: 0, y: 0,
      }) + '<div class="arrow">≠</div>' + cellBox('扫描到', {
        label: c.scanned.label, char: c.scanned.char, font: c.scanned.font,
        size: c.scanned.size, qty: c.scanned.qty,
        capacity: c.scanned.capacity, x: c.scanned.x, y: c.scanned.y,
      }) + '</div>';
    if (c.same_spec) {
      html += '<p class="diff-ok">扫描格与应取格同规格（' + esc(c.scanned.char) +
        ' ' + esc(c.scanned.font) + ' ' + esc(c.scanned.size) + '），可改选为来源格。</p>' +
        '<div class="row"><button id="btnReqUseScanned" class="primary">以「' +
        esc(c.scanned.label) + '」为来源格继续</button>' +
        '<button id="btnReqConflictCancel">取消，重新核对</button></div>';
    } else {
      html += '<p class="diff-up">规格 / 字种不符，系统不会自动替换；请重新核对实物后扫描。</p>' +
        '<div class="row"><button id="btnReqConflictCancel">取消，重新扫描</button></div>';
    }
  } else if (c.type === 'tray_mismatch') {
    html += '<div class="compare">' + trayBox('预期字盘', c.expected) +
      '<div class="arrow">≠</div>' + trayBox('实际扫到', c.scanned) + '</div>' +
      '<div class="row"><button id="btnReqConflictCancel">知道了，重新换盘</button></div>';
  } else if (c.type === 'tray_unknown') {
    html += '<div class="compare">' + trayBox('预期字盘', c.expected) +
      '<div class="arrow">?</div><div class="box"><h4>实际扫描</h4>' +
      '<span class="mono">' + esc(c.scanned_code) + '</span><br>没有字盘使用该码</div></div>' +
      '<div class="row"><button id="btnReqConflictCancel">知道了，重新扫描</button></div>';
  } else {
    html += '<div class="row"><button id="btnReqConflictCancel">知道了，重新扫描</button></div>';
  }
  html += '</div>';
  box.innerHTML = html;

  const cancel = $('#btnReqConflictCancel');
  if (cancel) cancel.onclick = () => { reqConflict = null; renderReqConflict(req); };
  const change = $('#btnReqChange');
  if (change) change.onclick = () => guard(async () => {
    const cid = parseInt($('#reqCandSel').value, 10);
    if (!cid) return;
    const picked = parseInt($('#reqPicked').value, 10) || 0;
    const res = await api('/api/requisitions/' + req.id + '/change-source',
      'POST', { cell_id: cid, picked });
    applyState(res.state);
    reqConflict = null;
    toast('已改选来源格，请在新格扫码确认实取');
  });
  const gap = $('#btnReqGap');
  if (gap) gap.onclick = () => guard(async () => {
    const picked = parseInt($('#reqPicked') && $('#reqPicked').value, 10) || 0;
    const a = reqConflict.allocation;
    const res = await api('/api/requisitions/' + req.id + '/keep-gap',
      'POST', { allocation_id: a.id, picked });
    applyState(res.state);
    reqConflict = null;
    toast('差额已保留为缺口');
  });
  const useScanned = $('#btnReqUseScanned');
  if (useScanned) useScanned.onclick = () => guard(async () => {
    const sc = reqConflict.scanned;
    const res = await api('/api/requisitions/' + req.id + '/change-source',
      'POST', { cell_id: sc.id, picked: 0 });
    applyState(res.state);
    reqConflict = null;
    toast('已改选「' + sc.label + '」为来源格，请扫码确认实取');
  });
}

/* ---- 历史记录 ---- */

function renderReqHistory() {
  const el = $('#reqHistory');
  if (!el) return;
  const hist = S.req_history || [];
  if (!hist.length) {
    el.innerHTML = '<p class="hint">暂无已结束的领用单。</p>';
    return;
  }
  el.innerHTML = '<table class="tasks"><thead><tr><th>#</th><th>名称</th><th>状态</th>' +
    '<th>来源格</th><th>预留</th><th>实取</th><th>缺口</th><th>创建</th><th></th></tr></thead><tbody>' +
    hist.map((r) =>
      '<tr><td>' + r.id + '</td><td>' + esc(r.name) + '</td><td>' +
      (S.req_status_names[r.status] || r.status) + '</td><td>' + r.n_allocs +
      '</td><td>' + r.qty_reserved + '</td><td>' + r.qty_taken + '</td><td>' +
      (r.n_gap ? '<span class="diff-up">' + r.n_gap + '</span>' : '—') +
      '</td><td class="muted">' + esc(r.created_at) + '</td><td>' +
      '<button data-req-open="' + r.id + '">查看</button> ' +
      '<button data-req-print="' + r.id + '">打印</button>' +
      (r.status === 'done' && !r.returned_session_id
        ? ' <button class="ok" data-req-return="' + r.id + '">带入归还</button>'
        : r.returned_session_id
          ? ' <span class="muted">已带批次#' + r.returned_session_id + '</span>' : '') +
      '</td></tr>').join('') + '</tbody></table>';
  el.querySelectorAll('[data-req-open]').forEach((b) => {
    b.onclick = () => guard(async () => {
      const res = await api('/api/requisitions/' + b.dataset.reqOpen);
      printRequisition(res.requisition);
    });
  });
  el.querySelectorAll('[data-req-print]').forEach((b) => {
    b.onclick = () => guard(async () => {
      const res = await api('/api/requisitions/' + b.dataset.reqPrint);
      printRequisition(res.requisition);
    });
  });
  el.querySelectorAll('[data-req-return]').forEach((b) => {
    b.onclick = () => reqToReturn(parseInt(b.dataset.reqReturn, 10));
  });
}

async function reqToReturn(rid) {
  await guard(async () => {
    const res = await api('/api/requisitions/' + rid + '/to-return', 'POST');
    applyState(res.state);
    toast('已生成归还批次 #' + res.session_id + '，请在「归还作业」页逐格扫码确认');
    switchView('run');
  });
}

/* ---- 打印领用单 / 标签 ---- */

function printRequisition(req) {
  if (!req) { toast('没有可打印的领用单', true); return; }
  const stName = S.req_status_names[req.status] || req.status;
  const order = reqOrder(req);
  const demandRows = req.demands.map((dm) => {
    const p = reqDemandProgress(req, dm);
    return '<tr><td>' + esc(dm.char) + '</td><td>' + esc(dm.font || '任意') +
      ' / ' + esc(dm.size || '任意') + '</td><td>' + dm.qty + '</td><td>' +
      p.reserved + '</td><td>' + (p.taken || 0) + '</td><td>' +
      (p.gap ? '<b>' + p.gap + '</b>' : '—') + '</td><td>' +
      esc(dm.issue_text || (p.taken >= dm.qty ? '已领齐' : '')) + '</td></tr>';
  }).join('');
  const cards = [];
  for (const tid of order) {
    const tr = trayById(tid) || { name: '?', scan_code: '?' };
    const allocs = req.allocations.filter((a) => a.tray_id === tid
      && a.status !== 'cancelled').sort((a, b) => a.seq - b.seq);
    allocs.forEach((a) => {
      if (a.status === 'gap') return;  // 缺口不印取字标签
      cards.push(
        '<div class="label-card"><div class="lc-head"><span>字盘 <b>' +
        esc(tr.name) + '</b></span><span class="mono">' + esc(tr.scan_code || '—') +
        '</span></div><div class="lc-code">来源格 <b>' + esc(a.label) +
        '</b> · 盘内 #' + a.seq + '</div><div class="lc-char">' +
        esc(a.cell_char) + '</div><div class="lc-meta">' +
        esc(a.cell_font || '—') + ' · ' + esc(a.cell_size || '—') +
        '<br>预留 <b>' + a.qty + '</b> · 实取 ____' +
        '<br>领用单 #' + req.id + '　' + esc(req.created_at) + '</div></div>');
    });
  }
  const sourceRows = order.map((tid) => {
    const tr = trayById(tid) || { name: '?', scan_code: '?' };
    return req.allocations.filter((a) => a.tray_id === tid
      && a.status !== 'cancelled').sort((a, b) => a.seq - b.seq).map((a) =>
      '<tr><td>' + esc(tr.name) + '</td><td class="mono">' + esc(tr.scan_code || '—') +
      '</td><td>' + esc(a.label) + '</td><td>' + esc(a.cell_char) + '</td><td>' +
      esc(a.cell_font || '—') + ' / ' + esc(a.cell_size || '—') + '</td><td>' +
      a.qty + '</td><td><b>' + a.taken_qty + '</b></td><td>' +
      (a.status === 'gap' ? '<b>缺口 ' + a.qty + '</b>'
        : S.alloc_status_names[a.status] || a.status) + '</td></tr>').join('');
  }).join('');
  const takenTotal = req.allocations.reduce((a, x) => a + x.taken_qty, 0);
  const gapTotal = req.allocations.filter((a) => a.status === 'gap')
    .reduce((a, x) => a + x.qty, 0);
  $('#printArea').innerHTML =
    '<div class="stock-sheet req-sheet"><h2>配字领用清单</h2>' +
    '<p>领用单 <b>#' + req.id + '</b>　' + esc(req.name) + '　状态：' + stName +
    '　创建：' + esc(req.created_at) +
    (req.finished_at ? '　完成：' + esc(req.finished_at) : '') + '<br>' +
    '实取合计 <b>' + takenTotal + '</b> 枚' +
    (gapTotal ? '　<span class="diff-up">缺口合计 ' + gapTotal + ' 枚</span>' : '') +
    '</p><h3>需求汇总</h3><table><thead><tr><th>字符</th><th>规格</th>' +
    '<th>需求</th><th>预留</th><th>实取</th><th>缺口</th><th>备注</th>' +
    '</tr></thead><tbody>' + demandRows + '</tbody></table>' +
    '<h3>来源格明细（按字盘与盘内路线）</h3><table><thead><tr><th>字盘</th>' +
    '<th>字盘码</th><th>格号</th><th>字符</th><th>规格</th><th>预留</th>' +
    '<th>实取</th><th>状态</th></tr></thead><tbody>' + sourceRows + '</tbody></table>' +
    '<p class="sign">领用人：____________　复核人：____________　日期：____________</p>' +
    '<h3 class="page-break">来源格标签（裁剪后贴盘）</h3>' +
    '<div class="label-grid">' + cards.join('') + '</div></div>';
  window.print();
}

/* ---------------- 总渲染 ---------------- */

function renderAll() {
  $('#pendingBadge').hidden = !S.pending.length;
  $('#pendingBadge').textContent = S.pending.length;
  renderRun();
  renderTrayEditor();
  renderPending();
  renderData();
  renderStock();
  renderPick();
}

function main() {
  $$('.nav-btn').forEach((b) => {
    b.addEventListener('click', () => switchView(b.dataset.view));
  });
  bindRun();
  bindTrayBar();
  bindCellForm();
  bindData();
  bindStockStart();
  switchView('run');
  refresh();
}

main();
