'use strict';
/* 铅字归还助手 —— 前端逻辑（原生 JS + SVG，多字盘换盘导航） */

let S = { trays: [], cells: [], session: null, last_done: null, pending: [],
          conflicts: {}, reason_names: {} };
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

/* ---------------- 总渲染 ---------------- */

function renderAll() {
  $('#pendingBadge').hidden = !S.pending.length;
  $('#pendingBadge').textContent = S.pending.length;
  renderRun();
  renderTrayEditor();
  renderPending();
  renderData();
}

function main() {
  $$('.nav-btn').forEach((b) => {
    b.addEventListener('click', () => switchView(b.dataset.view));
  });
  bindRun();
  bindTrayBar();
  bindCellForm();
  bindData();
  switchView('run');
  refresh();
}

main();
