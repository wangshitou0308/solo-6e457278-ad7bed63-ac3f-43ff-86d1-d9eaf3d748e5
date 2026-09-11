#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铅字归还助手 —— 活字印刷工坊拆版后归还铅字的本地 Web 工具。

仅依赖 Python 标准库（http.server + sqlite3），断网即可运行。

用法:
    python3 server.py [端口]        默认 8000
    浏览器打开 http://127.0.0.1:8000

数据文件: sortify.db（可用环境变量 SORTIFY_DB 指定路径）
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SORTIFY_DB", os.path.join(BASE_DIR, "sortify.db"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DB_LOCK = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  label     TEXT NOT NULL UNIQUE,      -- 格号（扫描标签）
  char      TEXT NOT NULL DEFAULT '',  -- 字符，空串 = 空格位
  font      TEXT NOT NULL DEFAULT '',  -- 字体
  size      TEXT NOT NULL DEFAULT '',  -- 字号
  x REAL NOT NULL DEFAULT 0,  y REAL NOT NULL DEFAULT 0,
  w REAL NOT NULL DEFAULT 46, h REAL NOT NULL DEFAULT 46,
  qty      INTEGER NOT NULL DEFAULT 0,   -- 当前数量
  capacity INTEGER NOT NULL DEFAULT 50   -- 容量
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',   -- active / done / abandoned
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  cell_id INTEGER REFERENCES cells(id),
  char TEXT NOT NULL,
  font TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL,                    -- 本批应还数量
  done_qty INTEGER NOT NULL DEFAULT 0,     -- 已确认数量
  status TEXT NOT NULL DEFAULT 'pending',  -- pending / active / done / skipped
  seq INTEGER NOT NULL DEFAULT 0,          -- 规划顺序
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pending_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  raw TEXT NOT NULL,                       -- 原始字符
  qty INTEGER NOT NULL DEFAULT 1,
  reason TEXT NOT NULL DEFAULT 'unknown',  -- ligature / variant / unknown / unmatched
  suggestion TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',     -- open / resolved / ignored
  resolved_cell_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER, task_id INTEGER, cell_id INTEGER,
  kind TEXT NOT NULL,                      -- confirm / reassign / undo / resolve
  delta INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
"""

# ---------------------------------------------------------------- 字表

# 常见异体 / 繁简对照（双向可查，可按需扩充）
VARIANTS = {}
for _a, _b in [
    ("體", "体"), ("雲", "云"), ("龍", "龙"), ("馬", "马"), ("門", "门"),
    ("車", "车"), ("見", "见"), ("長", "长"), ("兒", "儿"), ("話", "话"),
    ("愛", "爱"), ("國", "国"), ("書", "书"), ("學", "学"), ("樂", "乐"),
    ("氣", "气"), ("發", "发"), ("時", "时"), ("實", "实"), ("頭", "头"),
    ("萬", "万"), ("與", "与"), ("東", "东"), ("條", "条"), ("無", "无"),
]:
    VARIANTS[_a] = _b
    VARIANTS.setdefault(_b, _a)

SIZES = {"初号", "小初", "一号", "小一", "二号", "小二", "三号", "小三",
         "四号", "小四", "五号", "小五", "六号", "小六", "七号", "八号"}

UNKNOWN_TOKENS = {"?", "？", "□", "▯", "■", "�", "×", "无法辨认"}

REASON_NAMES = {"ligature": "合字", "variant": "异体字",
                "unknown": "无法辨认", "unmatched": "无匹配格位"}


class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def rows(sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def row(sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def transactional(fn):
    """写操作包装：加锁、成功提交、失败回滚。"""
    def wrapper(*a, **kw):
        with DB_LOCK:
            try:
                result = fn(*a, **kw)
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise
    wrapper.__name__ = fn.__name__
    return wrapper

# ---------------------------------------------------------------- 解析与匹配


def _font_size(parts):
    """从剩余字段里识别字体与字号。"""
    font, size = "", ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p in SIZES or p.endswith("号") or p.lower().endswith("pt"):
            if not size:
                size = p
                continue
        if not font:
            font = p
        elif not size:
            size = p
    return font, size


def parse_return_text(text):
    """解析粘贴的待归还清单。

    每行一条，支持:
        永 12            永×12           永*12
        永,12,宋体,五号   永 12 宋体 五号   永
    返回 [(char, qty, font, size, raw), ...]
    """
    items = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+?)\s*[×xX\*]\s*(\d+)\s*(.*)$", line)
        if m:
            ch, qty = m.group(1), int(m.group(2))
            font, size = _font_size(re.split(r"[\s,，、]+", m.group(3)))
            items.append((ch, qty, font, size, line))
            continue
        # 先按空白切；首字段本身可能就是标点（如「，」），不能按逗号切
        parts = [p for p in re.split(r"\s+", line) if p]
        if len(parts) == 1 and ("," in parts[0] or "，" in parts[0]):
            parts = [p for p in re.split(r"[,，、]", parts[0]) if p]
        ch, qty = parts[0], 1
        rest = []
        for p in parts[1:]:
            if p.isdigit() and qty == 1:
                qty = int(p)
            else:
                rest.append(p)
        font, size = _font_size(rest)
        items.append((ch, qty, font, size, line))
    return items


def classify(ch, font, size, cells):
    """把一条待还铅字分类为 task（可匹配格位）或 pending（待确认）。"""
    if ch in UNKNOWN_TOKENS:
        return ("pending", "unknown", None, "无法辨认的铅字，请人工鉴定")
    same = [c for c in cells if c["char"] == ch]
    exact = [c for c in same
             if (not font or c["font"] == font) and (not size or c["size"] == size)]
    if exact:
        best = max(exact, key=lambda c: c["capacity"] - c["qty"])
        return ("task", "", best, "")
    if len(ch) > 1:
        return ("pending", "ligature", None, "合字 / 多字铸体，请人工确认归属")
    if same:
        return ("pending", "unmatched", None,
                "字盘中有「%s」但字体 / 字号不符" % ch)
    if ch in VARIANTS and any(c["char"] == VARIANTS[ch] for c in cells):
        return ("pending", "variant", None,
                "异体字，正字「%s」有格位" % VARIANTS[ch])
    return ("pending", "unmatched", None, "字盘中无此字符的格位")


def plan_order(items):
    """按格位坐标蛇形（之字形）排序，逐行往返、减少走动。"""
    if not items:
        return []
    ROW_TOL = 30  # 纵向容差，视为同一行
    bands = []
    for it in sorted(items, key=lambda t: (t["y"], t["x"])):
        for band in bands:
            if abs(band["y"] - it["y"]) <= ROW_TOL:
                band["items"].append(it)
                band["y"] = min(band["y"], it["y"])
                break
        else:
            bands.append({"y": it["y"], "items": [it]})
    bands.sort(key=lambda b: b["y"])
    out = []
    for i, band in enumerate(bands):
        out.extend(sorted(band["items"], key=lambda t: t["x"],
                          reverse=bool(i % 2)))
    return out

# ---------------------------------------------------------------- 状态组装


def capacity_conflicts(cells, session):
    """容量冲突检查：已超容的格 + 当前批次预计超容的格。"""
    over = [c for c in cells if c["qty"] > c["capacity"]]
    projected = []
    if session:
        incoming = {}
        for t in session["tasks"]:
            if t["status"] in ("pending", "active") and t["cell_id"]:
                incoming[t["cell_id"]] = incoming.get(t["cell_id"], 0) \
                    + (t["qty"] - t["done_qty"])
        by_id = {c["id"]: c for c in cells}
        for cid, n in incoming.items():
            c = by_id.get(cid)
            if c and c["qty"] + n > c["capacity"]:
                projected.append({
                    "cell": c, "incoming": n,
                    "overflow_by": c["qty"] + n - c["capacity"],
                })
    return {"overflow_cells": over, "projected": projected}


def get_state():
    with DB_LOCK:
        cells = rows("SELECT * FROM cells ORDER BY y, x, id")
        sess = row("SELECT * FROM sessions WHERE status='active' "
                   "ORDER BY id DESC LIMIT 1")
        session = None
        if sess:
            session = dict(sess)
            session["tasks"] = rows(
                "SELECT t.*, c.label, c.x, c.y, c.qty AS cell_qty, c.capacity "
                "FROM tasks t LEFT JOIN cells c ON c.id = t.cell_id "
                "WHERE t.session_id=? ORDER BY t.seq", (sess["id"],))
        pending = rows("SELECT * FROM pending_items WHERE status='open' "
                       "ORDER BY id")
        return {"cells": cells, "session": session, "pending": pending,
                "conflicts": capacity_conflicts(cells, session),
                "reason_names": REASON_NAMES}


def conflict(ctype, message, **kw):
    c = {"type": ctype, "message": message}
    c.update(kw)
    return {"ok": False, "conflict": c, "state": get_state()}

# ---------------------------------------------------------------- 格位 API


CELL_FIELDS = ("label", "char", "font", "size", "x", "y", "w", "h",
               "qty", "capacity")


@transactional
def create_cell(body):
    label = str(body.get("label") or "").strip()
    if not label:
        raise ApiError("格号不能为空")
    if row("SELECT id FROM cells WHERE label=?", (label,)):
        raise ApiError("格号「%s」已存在" % label)
    db.execute(
        "INSERT INTO cells(label,char,font,size,x,y,w,h,qty,capacity) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (label, str(body.get("char") or ""), str(body.get("font") or ""),
         str(body.get("size") or ""), float(body.get("x") or 0),
         float(body.get("y") or 0), float(body.get("w") or 46),
         float(body.get("h") or 46), int(body.get("qty") or 0),
         int(body.get("capacity") or 50)))
    return {"ok": True, "state": get_state()}


@transactional
def update_cell(cell_id, body):
    cell = row("SELECT * FROM cells WHERE id=?", (cell_id,))
    if not cell:
        raise ApiError("格位不存在", 404)
    sets, args = [], []
    for f in CELL_FIELDS:
        if f not in body:
            continue
        v = body[f]
        if f in ("qty", "capacity"):
            v = int(v)
        elif f in ("x", "y", "w", "h"):
            v = float(v)
        else:
            v = str(v).strip() if f == "label" else str(v)
        if f == "label":
            if not v:
                raise ApiError("格号不能为空")
            dup = row("SELECT id FROM cells WHERE label=? AND id!=?",
                      (v, cell_id))
            if dup:
                raise ApiError("格号「%s」已存在" % v)
        sets.append("%s=?" % f)
        args.append(v)
    if sets:
        args.append(cell_id)
        db.execute("UPDATE cells SET %s WHERE id=?" % ", ".join(sets), args)
    return {"ok": True, "state": get_state()}


@transactional
def delete_cell(cell_id):
    if not row("SELECT id FROM cells WHERE id=?", (cell_id,)):
        raise ApiError("格位不存在", 404)
    used = row("SELECT COUNT(*) AS n FROM tasks WHERE cell_id=?", (cell_id,))
    if used["n"]:
        raise ApiError("该格已有归还任务记录，不能删除")
    db.execute("UPDATE pending_items SET resolved_cell_id=NULL "
               "WHERE resolved_cell_id=?", (cell_id,))
    db.execute("DELETE FROM cells WHERE id=?", (cell_id,))
    return {"ok": True, "state": get_state()}


@transactional
def bulk_cells(body):
    created, skipped = 0, []
    for c in body.get("cells", []):
        label = str(c.get("label") or "").strip()
        if not label:
            continue
        if row("SELECT id FROM cells WHERE label=?", (label,)):
            skipped.append(label)
            continue
        db.execute(
            "INSERT INTO cells(label,char,font,size,x,y,w,h,qty,capacity) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (label, str(c.get("char") or ""), str(c.get("font") or ""),
             str(c.get("size") or ""), float(c.get("x") or 0),
             float(c.get("y") or 0), float(c.get("w") or 46),
             float(c.get("h") or 46), int(c.get("qty") or 0),
             int(c.get("capacity") or 50)))
        created += 1
    return {"ok": True, "created": created, "skipped": skipped,
            "state": get_state()}

# ---------------------------------------------------------------- 批次 API


@transactional
def create_session(body):
    text = body.get("text") or ""
    items = parse_return_text(text)
    if not items:
        raise ApiError("未解析到任何字符，请检查粘贴内容")
    cells = rows("SELECT * FROM cells")
    old = row("SELECT id FROM sessions WHERE status='active'")
    if old:
        raise ApiError("已有进行中的批次（#%d），请先完成或作废" % old["id"])
    cur = db.execute("INSERT INTO sessions(name, created_at) VALUES(?,?)",
                     (body.get("name") or "归还批次 " + now(), now()))
    sid = cur.lastrowid
    merged = {}   # cell_id -> 合并后的任务
    new_pending = []
    for ch, qty, font, size, raw in items:
        kind, reason, cell, note = classify(ch, font, size, cells)
        if kind == "task":
            cid = cell["id"]
            if cid in merged:
                merged[cid]["qty"] += qty
            else:
                merged[cid] = {"cell_id": cid, "char": ch, "font": cell["font"],
                               "size": cell["size"], "qty": qty,
                               "x": cell["x"], "y": cell["y"]}
        else:
            new_pending.append((sid, ch, qty, reason, note, now()))
    ordered = plan_order(list(merged.values()))
    by_id = {c["id"]: c for c in cells}
    for i, t in enumerate(ordered):
        cell = by_id[t["cell_id"]]
        note = "预计超容" if cell["qty"] + t["qty"] > cell["capacity"] else ""
        db.execute(
            "INSERT INTO tasks(session_id,cell_id,char,font,size,qty,seq,note) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sid, t["cell_id"], t["char"], t["font"], t["size"], t["qty"],
             i + 1, note))
    for p in new_pending:
        db.execute(
            "INSERT INTO pending_items"
            "(session_id,raw,qty,reason,suggestion,created_at) "
            "VALUES(?,?,?,?,?,?)", p)
    first = row("SELECT id FROM tasks WHERE session_id=? ORDER BY seq LIMIT 1",
                (sid,))
    if first:
        db.execute("UPDATE tasks SET status='active' WHERE id=?", (first["id"],))
    else:
        db.execute("UPDATE sessions SET status='done', finished_at=? "
                   "WHERE id=?", (now(), sid))
    return {"ok": True, "state": get_state()}


def _activate_next(session_id):
    nxt = row("SELECT id FROM tasks WHERE session_id=? AND status='pending' "
              "ORDER BY seq LIMIT 1", (session_id,))
    if nxt:
        db.execute("UPDATE tasks SET status='active' WHERE id=?", (nxt["id"],))
        return
    left = row("SELECT COUNT(*) AS n FROM tasks WHERE session_id=? "
               "AND status IN ('pending','active')", (session_id,))["n"]
    if left == 0:
        db.execute("UPDATE sessions SET status='done', finished_at=? "
                   "WHERE id=?", (now(), session_id))


def _candidates(task, current_cell):
    """改派候选格：同字符其他格优先，其次空格位，按剩余容量排序。"""
    out = []
    for c in rows("SELECT * FROM cells WHERE id != ? ORDER BY "
                  "(char = ?) DESC, (capacity - qty) DESC, label",
                  (current_cell["id"], task["char"])):
        if c["char"] not in (task["char"], ""):
            continue
        out.append({"id": c["id"], "label": c["label"], "char": c["char"],
                    "font": c["font"], "size": c["size"], "x": c["x"],
                    "y": c["y"], "qty": c["qty"], "capacity": c["capacity"],
                    "free": c["capacity"] - c["qty"]})
        if len(out) >= 8:
            break
    return out


@transactional
def confirm_task(task_id, body):
    label = str(body.get("label") or "").strip()
    force = bool(body.get("force"))
    t = row("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ApiError("任务不存在", 404)
    sess = row("SELECT * FROM sessions WHERE id=?", (t["session_id"],))
    if not sess or sess["status"] != "active":
        raise ApiError("批次已结束，不能确认")
    if t["status"] == "done":
        return conflict("duplicate", "该任务已完成，请勿重复确认", task=t)
    if t["status"] != "active":
        return conflict("not_current", "请先完成当前高亮的格位", task=t)
    cell = row("SELECT * FROM cells WHERE id=?", (t["cell_id"],))
    if not cell:
        raise ApiError("任务没有目标格位")
    if label and label.upper() != cell["label"].upper():
        scanned = row("SELECT * FROM cells WHERE upper(label)=?",
                      (label.upper(),))
        if not scanned:
            return conflict("unknown_label",
                            "无法识别的格号「%s」" % label, label=label)
        dup = row("SELECT COUNT(*) AS n FROM tasks WHERE session_id=? "
                  "AND cell_id=? AND status='done'",
                  (t["session_id"], scanned["id"]))["n"]
        return conflict("mismatch",
                        "实物不符：扫描到「%s」，当前应为「%s」"
                        % (scanned["label"], cell["label"]),
                        scanned=scanned, expected=cell,
                        duplicate_of=bool(dup))
    remain = t["qty"] - t["done_qty"]
    qty = body.get("qty")
    try:
        n = int(qty) if qty else remain
    except (TypeError, ValueError):
        raise ApiError("数量不正确")
    n = max(1, min(n, remain))
    if not force and cell["qty"] + n > cell["capacity"]:
        return conflict(
            "overflow",
            "格位容量不足：%s 现有 %d/%d，再放入 %d 枚将超出"
            % (cell["label"], cell["qty"], cell["capacity"], n),
            cell=cell, need=n, candidates=_candidates(t, cell))
    db.execute("UPDATE cells SET qty=qty+? WHERE id=?", (n, cell["id"]))
    done_qty = t["done_qty"] + n
    st = "done" if done_qty >= t["qty"] else "active"
    db.execute("UPDATE tasks SET done_qty=?, status=? WHERE id=?",
               (done_qty, st, task_id))
    db.execute("INSERT INTO actions(session_id,task_id,cell_id,kind,delta,"
               "payload,created_at) VALUES(?,?,?,?,?,?,?)",
               (t["session_id"], task_id, cell["id"], "confirm", n,
                json.dumps({"qty": n, "cell_id": cell["id"],
                            "task_id": task_id}), now()))
    if st == "done":
        _activate_next(t["session_id"])
    return {"ok": True, "state": get_state()}


@transactional
def reassign_task(task_id, body):
    new_id = body.get("cell_id")
    t = row("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ApiError("任务不存在", 404)
    if t["status"] not in ("active", "pending"):
        raise ApiError("只能改派未完成的任务")
    old = row("SELECT * FROM cells WHERE id=?", (t["cell_id"],))
    new = row("SELECT * FROM cells WHERE id=?", (new_id,))
    if not new:
        raise ApiError("目标格位不存在")
    if old and new["id"] == old["id"]:
        raise ApiError("目标与原格位相同")
    snap = lambda c: {"id": c["id"], "label": c["label"], "x": c["x"],
                      "y": c["y"], "qty": c["qty"], "capacity": c["capacity"]}
    payload = {"before": snap(old) if old else None, "after": snap(new)}
    db.execute("UPDATE tasks SET cell_id=?, note=? WHERE id=?",
               (new["id"], "自 %s 改派" % (old["label"] if old else "?"),
                task_id))
    db.execute("INSERT INTO actions(session_id,task_id,cell_id,kind,delta,"
               "payload,created_at) VALUES(?,?,?,?,?,?,?)",
               (t["session_id"], task_id, new["id"], "reassign", 0,
                json.dumps(payload, ensure_ascii=False), now()))
    warn = None
    if new["qty"] + (t["qty"] - t["done_qty"]) > new["capacity"]:
        warn = "注意：改派到 %s 后仍将超出容量" % new["label"]
    return {"ok": True, "warn": warn, "state": get_state()}


@transactional
def skip_task(task_id):
    t = row("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ApiError("任务不存在", 404)
    if t["status"] != "active":
        raise ApiError("只能跳过当前任务")
    db.execute("UPDATE tasks SET status='skipped' WHERE id=?", (task_id,))
    _activate_next(t["session_id"])
    return {"ok": True, "state": get_state()}


@transactional
def undo(session_id):
    sess = row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not sess:
        raise ApiError("批次不存在", 404)
    act = row("SELECT * FROM actions WHERE session_id=? AND kind='confirm' "
              "ORDER BY id DESC LIMIT 1", (session_id,))
    if not act:
        raise ApiError("没有可撤销的确认")
    p = json.loads(act["payload"])
    db.execute("UPDATE cells SET qty=max(qty-?,0) WHERE id=?",
               (p["qty"], p["cell_id"]))
    t = row("SELECT * FROM tasks WHERE id=?", (p["task_id"],))
    if t:
        db.execute("UPDATE tasks SET done_qty=max(done_qty-?,0), "
                   "status='active' WHERE id=?", (p["qty"], t["id"]))
        db.execute("UPDATE tasks SET status='pending' WHERE session_id=? "
                   "AND status='active' AND id!=?", (session_id, t["id"]))
    db.execute("UPDATE sessions SET status='active', finished_at=NULL "
               "WHERE id=?", (session_id,))
    db.execute("INSERT INTO actions(session_id,task_id,cell_id,kind,delta,"
               "payload,created_at) VALUES(?,?,?,?,?,?,?)",
               (session_id, p["task_id"], p["cell_id"], "undo", -p["qty"],
                act["payload"], now()))
    db.execute("DELETE FROM actions WHERE id=?", (act["id"],))
    return {"ok": True, "state": get_state()}


@transactional
def finish_session(session_id):
    sess = row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not sess:
        raise ApiError("批次不存在", 404)
    if sess["status"] != "active":
        raise ApiError("批次已结束")
    left = row("SELECT COUNT(*) AS n FROM tasks WHERE session_id=? "
               "AND status IN ('pending','active')", (session_id,))["n"]
    if left:
        raise ApiError("还有 %d 个未完成的归还任务" % left)
    db.execute("UPDATE sessions SET status='done', finished_at=? WHERE id=?",
               (now(), session_id))
    return {"ok": True, "state": get_state()}


@transactional
def abandon_session(session_id):
    sess = row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not sess:
        raise ApiError("批次不存在", 404)
    db.execute("UPDATE sessions SET status='abandoned', finished_at=? "
               "WHERE id=?", (now(), session_id))
    db.execute("UPDATE tasks SET status='skipped' WHERE session_id=? "
               "AND status IN ('pending','active')", (session_id,))
    return {"ok": True, "state": get_state()}

# ---------------------------------------------------------------- 待确认 API


@transactional
def resolve_pending(pid, body):
    item = row("SELECT * FROM pending_items WHERE id=?", (pid,))
    if not item:
        raise ApiError("待确认项不存在", 404)
    if item["status"] != "open":
        raise ApiError("该项已处理")
    if body.get("ignore"):
        db.execute("UPDATE pending_items SET status='ignored' WHERE id=?",
                   (pid,))
        return {"ok": True, "state": get_state()}
    cell = row("SELECT * FROM cells WHERE id=?", (body.get("cell_id"),))
    if not cell:
        raise ApiError("请选择目标格位")
    db.execute("UPDATE pending_items SET status='resolved', "
               "resolved_cell_id=? WHERE id=?", (cell["id"], pid))
    sess = row("SELECT * FROM sessions WHERE status='active' "
               "ORDER BY id DESC LIMIT 1")
    if not sess:
        cur = db.execute("INSERT INTO sessions(name, created_at) VALUES(?,?)",
                         ("待确认补录 " + now(), now()))
        sess = row("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,))
    maxseq = row("SELECT COALESCE(MAX(seq),0) AS m FROM tasks "
                 "WHERE session_id=?", (sess["id"],))["m"]
    cur = db.execute(
        "INSERT INTO tasks(session_id,cell_id,char,font,size,qty,seq,note) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (sess["id"], cell["id"], item["raw"], cell["font"], cell["size"],
         item["qty"], maxseq + 1, "待确认补录"))
    if not row("SELECT id FROM tasks WHERE session_id=? AND status='active'",
               (sess["id"],)):
        db.execute("UPDATE tasks SET status='active' WHERE id=?",
                   (cur.lastrowid,))
        db.execute("UPDATE sessions SET status='active', finished_at=NULL "
                   "WHERE id=?", (sess["id"],))
    db.execute("INSERT INTO actions(session_id,task_id,cell_id,kind,delta,"
               "payload,created_at) VALUES(?,?,?,?,?,?,?)",
               (sess["id"], cur.lastrowid, cell["id"], "resolve", 0,
                json.dumps({"pending_id": pid, "raw": item["raw"]},
                           ensure_ascii=False), now()))
    return {"ok": True, "state": get_state()}

# ---------------------------------------------------------------- 备份恢复

TABLES = ["cells", "sessions", "tasks", "pending_items", "actions"]
DELETE_ORDER = ["actions", "pending_items", "tasks", "sessions", "cells"]


def backup():
    with DB_LOCK:
        return {"app": "sortify", "version": 1, "exported_at": now(),
                "tables": {t: rows("SELECT * FROM %s" % t) for t in TABLES}}


@transactional
def restore(data):
    if not isinstance(data, dict) or data.get("app") != "sortify" \
            or not isinstance(data.get("tables"), dict):
        raise ApiError("备份文件格式不正确")
    for t in DELETE_ORDER:
        db.execute("DELETE FROM %s" % t)
    for t in TABLES:
        for r in data["tables"].get(t, []):
            if not isinstance(r, dict) or not r:
                continue
            cols = ",".join(r.keys())
            qs = ",".join("?" * len(r))
            db.execute("INSERT INTO %s(%s) VALUES(%s)" % (t, cols, qs),
                       list(r.values()))
    return {"ok": True, "state": get_state()}

# ---------------------------------------------------------------- 种子数据

SEED_CHARS = ("的一是不了人在我有他这中大来上国个到说们为子和你地出道也"
              "时年得就那要下以生会自着去之体永文言心")
SEED_PUNCT = "，。、「」"


def seed():
    """首次启动时生成示例字盘：6 行常用字 + 1 行标点与空格位。"""
    for i, ch in enumerate(SEED_CHARS):
        r, c = divmod(i, 8)
        db.execute(
            "INSERT INTO cells(label,char,font,size,x,y,w,h,qty,capacity) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("%s%d" % (chr(65 + r), c + 1), ch, "宋体", "五号",
             20 + c * 54, 20 + r * 54, 46, 46, 20 + (i * 7) % 25, 50))
    for j, ch in enumerate(SEED_PUNCT):
        db.execute(
            "INSERT INTO cells(label,char,font,size,x,y,w,h,qty,capacity) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("G%d" % (j + 1), ch, "宋体", "五号",
             20 + j * 54, 20 + 6 * 54, 46, 46, 20, 50))
    for j in range(2):  # 空格位，供改派使用
        db.execute(
            "INSERT INTO cells(label,char,font,size,x,y,w,h,qty,capacity) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("G%d" % (len(SEED_PUNCT) + j + 1), "", "", "",
             20 + (len(SEED_PUNCT) + j) * 54, 20 + 6 * 54, 46, 46, 0, 50))


def init_db():
    with DB_LOCK:
        db.executescript(SCHEMA)
        if not row("SELECT id FROM cells LIMIT 1"):
            seed()
        db.commit()

# ---------------------------------------------------------------- HTTP 层


def handle_api(method, path, body):
    if method == "GET" and path == "/api/state":
        return get_state()
    if method == "GET" and path == "/api/backup":
        return backup()
    if method == "POST" and path == "/api/restore":
        return restore(body)
    if method == "POST" and path == "/api/cells":
        return create_cell(body)
    if method == "POST" and path == "/api/cells/bulk":
        return bulk_cells(body)
    m = re.fullmatch(r"/api/cells/(\d+)", path)
    if m:
        cid = int(m.group(1))
        if method == "PUT":
            return update_cell(cid, body)
        if method == "DELETE":
            return delete_cell(cid)
    if method == "POST" and path == "/api/sessions":
        return create_session(body)
    m = re.fullmatch(r"/api/sessions/(\d+)/(finish|abandon|undo)", path)
    if m and method == "POST":
        sid, action = int(m.group(1)), m.group(2)
        if action == "finish":
            return finish_session(sid)
        if action == "abandon":
            return abandon_session(sid)
        return undo(sid)
    m = re.fullmatch(r"/api/tasks/(\d+)/(confirm|reassign|skip)", path)
    if m and method == "POST":
        tid, action = int(m.group(1)), m.group(2)
        if action == "confirm":
            return confirm_task(tid, body)
        if action == "reassign":
            return reassign_task(tid, body)
        return skip_task(tid)
    m = re.fullmatch(r"/api/pending/(\d+)/resolve", path)
    if m and method == "POST":
        return resolve_pending(int(m.group(1)), body)
    raise ApiError("Not Found", 404)


MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png",
        ".json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Sortify/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            raise ApiError("请求体不是有效的 JSON")

    def _static(self, path):
        rel = os.path.normpath(path[len("/static/"):]).lstrip(os.sep)
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.abspath(full).startswith(os.path.abspath(STATIC_DIR)) \
                or not os.path.isfile(full):
            return self._json({"error": "Not Found"}, 404)
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        try:
            path = urlparse(self.path).path
            if method == "GET" and path in ("/", "/index.html"):
                return self._static_file_index()
            if method == "GET" and path.startswith("/static/"):
                return self._static(path)
            if path.startswith("/api/"):
                body = self._body() if method in ("POST", "PUT", "DELETE") \
                    else {}
                return self._json(handle_api(method, path, body))
            self._json({"error": "Not Found"}, 404)
        except ApiError as e:
            self._json({"error": e.message}, e.code)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": "服务器错误: %s" % e}, 500)

    def _static_file_index(self):
        full = os.path.join(STATIC_DIR, "index.html")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME[".html"])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def make_server(port=8000, host="127.0.0.1"):
    init_db()
    return ThreadingHTTPServer((host, port), Handler)


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = make_server(port)
    print("铅字归还助手已启动: http://127.0.0.1:%d  (Ctrl+C 停止)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
