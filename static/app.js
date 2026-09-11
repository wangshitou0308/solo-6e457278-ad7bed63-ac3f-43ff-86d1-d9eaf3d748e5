'use strict';
/* 铅字归还助手 —— 前端逻辑（原生 JS + SVG） */

let S = { cells: [], session: null, last_done: null, pending: [],
          conflicts: {}, reason_names: {} };
let view = 'run';
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
  renderAll();
}

async function refresh() {
  await guard(async () => applyState(await api('/api/state')));
}

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

/* ---------------- SVG 字盘 ---------------- */

function svgEl(name, attrs, parent) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

/**
 * 绘制字盘。
 * opts: {
 *   cls(cell) -> 额外 class, remain: Map<cellId, n>,
 *   onClick(cell), draggable: bool, onDrop(cell, x, y)
 * }
 */
function drawTray(svg, opts) {
  svg.innerHTML = '';
  let maxX = 460, maxY = 320;
  for (const c of S.cells) {
    maxX = Math.max(maxX, c.x + c.w + 24);
    maxY = Math.max(maxY, c.y + c.h + 24);
  }
  svg.setAttribute('viewBox', '0 0 ' + maxX + ' ' + maxY);
  for (const c of S.cells) {
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
  if (v === 'run') setTimeout(() => $('#scanInput').focus(), 50);
}

/* ---------------- 归还作业 ---------------- */

const curTask = () => S.session &&
  S.session.tasks.find((t) => t.status === 'active');
const nextTask = () => S.session &&
  S.session.tasks.find((t) => t.status === 'pending');

function renderRun() {
  const sess = S.session;
  const lastDone = S.last_done;
  // 无进行中批次时：粘贴区始终可用；上一批次的收尾操作一并保留
  $('#runEmpty').hidden = !!sess;
  $('#runActive').hidden = !sess;
  $('#runDone').hidden = !!sess || !lastDone;
  if (lastDone && !sess) {
    const ts = lastDone.tasks || [];
    const qty = ts.reduce((a, t) => a + t.done_qty, 0);
    $('#doneSummary').textContent =
      '批次 #' + lastDone.id + '（' + (lastDone.finished_at || '') + '）· 共还 ' +
      ts.length + ' 格 / ' + qty + ' 枚。可撤销最后一次确认、补打标签，' +
      '或在下方直接粘贴新清单。';
  }
  if (!sess) return;

  const tasks = sess.tasks;
  const done = tasks.filter((t) => t.status === 'done');
  const doneQty = done.reduce((a, t) => a + t.done_qty, 0);
  const totalQty = tasks.reduce((a, t) => a + t.qty, 0);
  $('#sessTitle').textContent = sess.name + '（#' + sess.id + '）';
  $('#sessProgress').textContent =
    '格位 ' + done.length + '/' + tasks.length + ' · 铅字 ' + doneQty + '/' + totalQty;
  $('#progressBar').style.width =
    (tasks.length ? (done.length / tasks.length) * 100 : 0) + '%';

  // 当前任务卡片
  const cur = curTask(), nxt = nextTask();
  const box = $('#curTask');
  if (cur) {
    const remain = cur.qty - cur.done_qty;
    box.innerHTML =
      '<div class="cur-char">' + esc(cur.char) + '</div>' +
      '<div class="cur-meta">' +
      '<div>目标格 <b>' + esc(cur.label) + '</b> · ' +
      esc(cur.font || '—') + ' · ' + esc(cur.size || '—') + '</div>' +
      '<div>本批余量 <b>' + remain + '</b> / ' + cur.qty + ' 枚 · ' +
      '格内 ' + cur.cell_qty + '/' + cur.capacity + '</div>' +
      (cur.note ? '<div class="muted">' + esc(cur.note) + '</div>' : '') +
      (nxt ? '<div class="next-hint">下一格：' + esc(nxt.label) + ' 「' +
        esc(nxt.char) + '」×' + nxt.qty + '</div>' : '') +
      '</div>';
  } else {
    box.innerHTML = '<p class="hint">没有待确认的任务（全部完成或已跳过）。</p>';
  }
  $('#btnConfirm').disabled = !cur;
  $('#btnSkip').disabled = !cur;

  // 冲突面板
  renderConflict();

  // 任务表
  const tb = $('#taskTable tbody');
  tb.innerHTML = '';
  const stName = { active: '当前', pending: '待还', done: '已还', skipped: '跳过' };
  for (const t of tasks) {
    const tr = document.createElement('tr');
    tr.className = t.status === 'active' ? 't-cur'
      : t.status === 'done' ? 't-done'
      : t.status === 'skipped' ? 't-skip' : '';
    tr.innerHTML =
      '<td>' + t.seq + '</td><td>' + esc(t.label || '—') + '</td>' +
      '<td class="big-char">' + esc(t.char) + '</td>' +
      '<td>' + t.done_qty + '/' + t.qty + '</td>' +
      '<td><span class="st st-' + t.status + '">' + stName[t.status] + '</span></td>';
    tb.appendChild(tr);
  }

  // 字盘高亮：当前 / 下一 / 已完成 + 本批余量
  const remain = new Map();
  for (const t of tasks) {
    if (t.status === 'active' || t.status === 'pending') {
      remain.set(t.cell_id, t.qty - t.done_qty);
    }
  }
  drawTray($('#runTray'), {
    remain,
    cls: (c) => {
      if (cur && c.id === cur.cell_id) return 'cur';
      if (nxt && c.id === nxt.cell_id) return 'next';
      const t = tasks.find((x) => x.cell_id === c.id);
      if (t && t.status === 'done') return 'done';
      return '';
    },
    onClick: (c) => { $('#scanInput').value = c.label; $('#scanInput').focus(); },
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
    $('#scanInput').focus();
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
    html += '<p>改派前后对比（本次放入 ' + need + ' 枚）：</p><div class="compare">' +
      cellBox('改派前（当前格）', cell,
        '<br><span class="diff-up">+' + need + ' 枚 → ' +
        (cell.qty + need) + '/' + cell.capacity + ' 超容</span>') +
      '<div class="arrow">→</div>' +
      '<div class="box"><h4>改派后（候选格）</h4><div id="candInfo">' +
      (hasCand ? '请选择候选格' : '无可用候选格，请先在字盘编辑页添加空格位') +
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

function renderTrayEditor() {
  drawTray($('#editTray'), {
    draggable: true,
    cls: (c) => (c.id === selectedCellId ? 'selected' : ''),
    onClick: (c) => { selectedCellId = c.id; renderTrayEditor(); fillCellForm(); },
    onDrop: (cell, x, y) => guard(async () => {
      applyState(await api('/api/cells/' + cell.id, 'PUT', { x, y }).then((r) => r.state));
      toast('已移动 ' + cell.label);
    }),
  });
}

function fillCellForm() {
  const c = S.cells.find((x) => x.id === selectedCellId);
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
    const label = prompt('新格号（如 H1）：');
    if (!label) return;
    const res = await api('/api/cells', 'POST',
      { label, x: 20, y: 20, capacity: 50 });
    applyState(res.state);
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
    const res = await api('/api/cells/bulk', 'POST', { cells });
    applyState(res.state);
    toast('已生成 ' + res.created + ' 格' +
      (res.skipped.length ? '，跳过已存在：' + res.skipped.join(' ') : ''));
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
    // 候选格：字符相同者优先，其次空格位
    const cands = S.cells
      .filter((c) => c.char === p.raw || c.char === '')
      .concat(S.cells.filter((c) => c.char !== p.raw && c.char !== ''))
      .slice(0, 60);
    tr.innerHTML =
      '<td class="big-char">' + esc(p.raw) + '</td>' +
      '<td>' + p.qty + '</td>' +
      '<td>' + esc(reason) + '</td>' +
      '<td class="muted">' + esc(p.suggestion) + '</td>' +
      '<td><select>' + cands.map((c) =>
        '<option value="' + c.id + '">' + esc(c.label) +
        (c.char ? '「' + esc(c.char) + '」' : '（空）') + '</option>').join('') +
      '</select></td>' +
      '<td><button class="ok">归位</button> <button class="ign">忽略</button></td>';
    tr.querySelector('.ok').onclick = () => guard(async () => {
      const cellId = parseInt(tr.querySelector('select').value, 10);
      const res = await api('/api/pending/' + p.id + '/resolve', 'POST',
        { cell_id: cellId });
      applyState(res.state);
      toast('已归位，加入归还批次');
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
  let html = '';
  if (!cf.overflow_cells.length && !cf.projected.length) {
    html = '<p class="hint">无容量冲突。</p>';
  } else {
    if (cf.overflow_cells.length) {
      html += '<h3>已超容格位</h3><table class="tasks"><thead><tr>' +
        '<th>格号</th><th>字符</th><th>存量 / 容量</th><th>超出</th></tr></thead><tbody>' +
        cf.overflow_cells.map((c) =>
          '<tr><td>' + esc(c.label) + '</td><td>' + esc(c.char || '（空）') +
          '</td><td>' + c.qty + ' / ' + c.capacity + '</td><td class="diff-up">+' +
          (c.qty - c.capacity) + '</td></tr>').join('') + '</tbody></table>';
    }
    if (cf.projected.length) {
      html += '<h3>当前批次预计超容</h3><table class="tasks"><thead><tr>' +
        '<th>格号</th><th>字符</th><th>存量 / 容量</th><th>待还</th><th>超出</th>' +
        '</tr></thead><tbody>' +
        cf.projected.map((p) =>
          '<tr><td>' + esc(p.cell.label) + '</td><td>' +
          esc(p.cell.char || '（空）') + '</td><td>' + p.cell.qty + ' / ' +
          p.cell.capacity + '</td><td>' + p.incoming +
          '</td><td class="diff-up">+' + p.overflow_by + '</td></tr>').join('') +
        '</tbody></table>';
    }
  }
  el.innerHTML = html;
}

/* 打印某批次的临时分拣标签（当前批次或最近完成的批次均可） */
function printLabels(sess) {
  if (!sess || !sess.tasks || !sess.tasks.length) {
    toast('没有可打印标签的批次', true);
    return;
  }
  $('#printArea').innerHTML =
    '<div class="label-grid">' + sess.tasks.map((t) =>
      '<div class="label-card">' +
      '<div class="lc-head"><span>格号 <b>' + esc(t.label || '—') +
      '</b></span><span>#' + t.seq + '</span></div>' +
      '<div class="lc-char">' + esc(t.char) + '</div>' +
      '<div class="lc-meta">' + esc(t.font || '—') + ' · ' +
      esc(t.size || '—') + ' · ' + t.qty + ' 枚<br>' +
      '坐标 (' + Math.round(t.x || 0) + ', ' + Math.round(t.y || 0) + ')' +
      ' · 批次 #' + sess.id + '<br>' + esc(sess.created_at) + '</div></div>'
    ).join('') + '</div>';
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
    toast('备份已导出');
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
    const n = res.state.session ? res.state.session.tasks.length : 0;
    toast('已创建批次：' + n + ' 个目标格' +
      (res.state.pending.length ? '，' + res.state.pending.length + ' 项待确认' : ''));
    $('#scanInput').focus();
  });
  $('#btnConfirm').onclick = doConfirm;
  $('#scanInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') doConfirm();
  });
  $('#btnUndo').onclick = () => guard(async () => {
    if (!S.session) return;
    applyState((await api('/api/sessions/' + S.session.id + '/undo', 'POST')).state);
    conflictCtx = null;
    toast('已撤销最近一次确认');
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
    applyState((await api('/api/sessions/' + S.session.id + '/finish', 'POST')).state);
    toast('批次已完成');
  });
  $('#btnAbandon').onclick = () => guard(async () => {
    if (!S.session || !confirm('作废当前批次？已确认的数量将保留在格内。')) return;
    applyState((await api('/api/sessions/' + S.session.id + '/abandon', 'POST')).state);
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
  bindCellForm();
  bindData();
  switchView('run');
  refresh();
}

main();
