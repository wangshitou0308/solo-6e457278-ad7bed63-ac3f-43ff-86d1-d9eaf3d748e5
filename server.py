#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铅字归还助手 —— 活字印刷工坊拆版后归还铅字的本地 Web 工具。

支持多个实体字盘（tray）：每个字盘可命名、设置扫描码并单独编辑布局；
归还批次先按目标字盘归组、盘内蛇形规划，可指定起始盘并锁定换盘顺序，
跨盘前必须扫描字盘码核对，执行页只绘制当前字盘。

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
CREATE TABLE IF NOT EXISTS trays (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL DEFAULT '',
  scan_code  TEXT NOT NULL DEFAULT '',   -- 实体字盘上的条码 / 二维码内容
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cells (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  tray_id   INTEGER NOT NULL REFERENCES trays(id),
  label     TEXT NOT NULL,               -- 格号（扫描标签），同一字盘内唯一
  char      TEXT NOT NULL DEFAULT '',    -- 字符，空串 = 空格位
  font      TEXT NOT NULL DEFAULT '',    -- 字体
  size      TEXT NOT NULL DEFAULT '',    -- 字号
  x REAL NOT NULL DEFAULT 0,  y REAL NOT NULL DEFAULT 0,
  w REAL NOT NULL DEFAULT 46, h REAL NOT NULL DEFAULT 46,
  qty      INTEGER NOT NULL DEFAULT 0,   -- 当前数量
  capacity INTEGER NOT NULL DEFAULT 50   -- 容量
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'planning', -- planning / active / done / abandoned
  created_at TEXT NOT NULL,
  finished_at TEXT,
  tray_order TEXT NOT NULL DEFAULT '[]',   -- JSON：规划/锁定后的换盘顺序
  start_tray_id INTEGER,                   -- 操作员指定的起始盘
  locked INTEGER NOT NULL DEFAULT 0,       -- 换盘顺序是否已锁定
  current_tray_id INTEGER                  -- 当前执行盘（确认换盘后推进）
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  tray_id INTEGER NOT NULL REFERENCES trays(id),
  cell_id INTEGER REFERENCES cells(id),
  char TEXT NOT NULL,
  font TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL,                    -- 本批应还数量
  done_qty INTEGER NOT NULL DEFAULT 0,     -- 已确认数量
  status TEXT NOT NULL DEFAULT 'pending',  -- pending / active / done / skipped
  seq INTEGER NOT NULL DEFAULT 0,          -- 盘内规划顺序
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


TASK_COLS = ("SELECT t.*, c.label, c.x, c.y, c.qty AS cell_qty, "
             "c.capacity, tr.name AS tray_name, tr.scan_code AS tray_code "
             "FROM tasks t "
             "LEFT JOIN cells c ON c.id = t.cell_id "
             "JOIN trays tr ON tr.id = t.tray_id ")


def _load_session(sess):
    sess = dict(sess)
    sess["tasks"] = rows(TASK_COLS + "WHERE t.session_id=? ORDER BY t.seq",
                         (sess["id"],))
    sess["tray_order"] = _loads(sess.get("tray_order") or "[]")
    return sess


def get_state():
    with DB_LOCK:
        trays = rows("SELECT * FROM trays ORDER BY sort_order, id")
        cells = rows("SELECT * FROM cells ORDER BY tray_id, y, x, id")
        sess = row("SELECT * FROM sessions WHERE status IN ('planning','active') "
                   "ORDER BY id DESC LIMIT 1")
        session = _load_session(sess) if sess else None
        # 最近完成的批次：供完成页撤销收尾、补打标签
        ds = row("SELECT * FROM sessions WHERE status='done' "
                 "ORDER BY id DESC LIMIT 1")
        last_done = _load_session(ds) if ds else None
        pending = rows("SELECT * FROM pending_items WHERE status='open' "
                       "ORDER BY id")
        return {"trays": trays, "cells": cells, "session": session,
                "last_done": last_done, "pending": pending,
                "conflicts": capacity_conflicts(cells, session),
                "reason_names": REASON_NAMES}


def conflict(ctype, message, **kw):
    c = {"type": ctype, "message": message}
    c.update(kw)
    return {"ok": False, "conflict": c, "state": get_state()}


def _loads(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _tray_ids(session):
    """会话换盘顺序：优先锁定/规划顺序，回退按盘内任务推导。"""
    ordered = [i for i in (session.get("tray_order") or []) if isinstance(i, int)]
    if ordered:
        return ordered
    return sorted({t["tray_id"] for t in session["tasks"]})

# ---------------------------------------------------------------- 推进逻辑


def _advance(session_id):
    """确认 / 跳过之后推进：激活下一格或下一盘；全部完成则结束批次。

    只激活当前盘内的下一个任务；跨盘时进入等待扫描字盘码的闸门状态。
    """
    sess = row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not sess or sess["status"] not in ("active",):
        return
    cur_tray = sess["current_tray_id"]
    nxt = row("SELECT id FROM tasks WHERE session_id=? AND status='pending' "
              "AND tray_id=? ORDER BY seq LIMIT 1", (session_id, cur_tray))
    if nxt:
        db.execute("UPDATE tasks SET status='active' WHERE id=?", (nxt["id"],))
        return
    left = row("SELECT COUNT(*) AS n FROM tasks WHERE session_id=? "
               "AND status IN ('pending','active')", (session_id,))["n"]
    if left == 0:
        db.execute("UPDATE sessions SET status='done', finished_at=? "
                   "WHERE id=?", (now(), session_id))


def _current_task(session_id):
    return row("SELECT * FROM tasks WHERE session_id=? AND status='active' "
               "ORDER BY seq LIMIT 1", (session_id,))

# ---------------------------------------------------------------- 字盘 API


def get_tray(tray_id):
    t = row("SELECT * FROM trays WHERE id=?", (tray_id,))
    if not t:
        raise ApiError("字盘不存在", 404)
    return t


def _unique_scan_code(preferred=""):
    """为新字盘分配一个非空且全局唯一的扫描码。"""
    used = {r["scan_code"].strip().upper()
            for r in rows("SELECT scan_code FROM trays") if r["scan_code"]}

    def free(c):
        return c and c.strip().upper() not in used

    p = (preferred or "").strip()
    if free(p):
        return p
    if p:
        for i in range(2, 100):
            cand = "%s-%d" % (p, i)
            if free(cand):
                return cand
    for i in range(1, 10000):
        cand = "TRAY-%02d" % i
        if free(cand):
            return cand
    return "TRAY-%d" % int(now().replace("-", "").replace(":", "")
                           .replace(" ", ""))


def _validate_scan_code(code, exclude_id=None):
    """编辑字盘时校验：参与批次的扫描码必须非空且全局唯一。"""
    code = (code or "").strip()
    if not code:
        raise ApiError("扫描码不能为空：每个字盘都要有可扫描的唯一字盘码")
    q = "SELECT t.id, t.name FROM trays t WHERE upper(t.scan_code)=?"
    args = [code.upper()]
    if exclude_id is not None:
        q += " AND t.id!=?"
        args.append(exclude_id)
    other = row(q, tuple(args))
    if other:
        raise ApiError("扫描码「%s」已被字盘「%s」使用，请改用唯一扫描码"
                       % (code, other["name"]))
    return code


def normalize_scan_codes():
    """修复既有数据：为空补码、为重复码保留首个并给其余重新分配。"""
    used = set()
    for t in rows("SELECT * FROM trays ORDER BY id"):
        code = (t["scan_code"] or "").strip()
        key = code.upper()
        if code and key not in used:
            used.add(key)
            if code != t["scan_code"]:
                db.execute("UPDATE trays SET scan_code=? WHERE id=?",
                           (code, t["id"]))
            continue
        cand = _unique_scan_code(code)
        used.add(cand.upper())
        db.execute("UPDATE trays SET scan_code=? WHERE id=?", (cand, t["id"]))


def _check_tray_codes(tray_ids):
    """检查参与批次的字盘是否都有非空且唯一的扫描码。

    返回 [{"id","name","problem"}, ...]，空列表表示全部正常。
    """
    ids = list(tray_ids)
    trays = rows("SELECT * FROM trays WHERE id IN (%s) ORDER BY id"
                 % ",".join("?" * len(ids)), tuple(ids)) if ids else []
    seen, bad = {}, []
    for t in trays:
        code = (t["scan_code"] or "").strip()
        if not code:
            bad.append({"id": t["id"], "name": t["name"], "problem": "未设扫描码"})
        elif code.upper() in seen:
            bad.append({"id": t["id"], "name": t["name"],
                        "problem": "扫描码「%s」与「%s」重复"
                                   % (code, seen[code.upper()])})
        else:
            seen[code.upper()] = t["name"]
    return bad


@transactional
def create_tray(body):
    name = str(body.get("name") or "").strip() or "新字盘"
    # 未填则自动生成；填了但与现有码冲突则明确报错（避免悄悄改码）
    code = str(body.get("scan_code") or "").strip()
    if code:
        code = _validate_scan_code(code)
    else:
        code = _unique_scan_code()
    mx = row("SELECT COALESCE(MAX(sort_order),0) AS m FROM trays")["m"]
    cur = db.execute(
        "INSERT INTO trays(name,scan_code,sort_order,created_at) "
        "VALUES(?,?,?,?)", (name, code, mx + 10, now()))
    return {"ok": True, "tray_id": cur.lastrowid, "scan_code": code,
            "state": get_state()}


@transactional
def update_tray(tray_id, body):
    get_tray(tray_id)
    sets, args = [], []
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError("字盘名称不能为空")
        sets.append("name=?")
        args.append(name)
    if "scan_code" in body:
        sets.append("scan_code=?")
        args.append(_validate_scan_code(body.get("scan_code"), tray_id))
    if sets:
        args.append(tray_id)
        db.execute("UPDATE trays SET %s WHERE id=?" % ", ".join(sets), args)
    return {"ok": True, "state": get_state()}


@transactional
def delete_tray(tray_id):
    t = get_tray(tray_id)
    if row("SELECT COUNT(*) AS n FROM trays")["n"] <= 1:
        raise ApiError("至少保留一个字盘")
    if row("SELECT COUNT(*) AS n FROM cells WHERE tray_id=?",
           (tray_id,))["n"]:
        raise ApiError("字盘「%s」中仍有格位，请先清空" % t["name"])
    if row("SELECT COUNT(*) AS n FROM tasks WHERE tray_id=?",
           (tray_id,))["n"]:
        raise ApiError("字盘「%s」已有归还任务记录，不能删除" % t["name"])
    db.execute("DELETE FROM trays WHERE id=?", (tray_id,))
    return {"ok": True, "state": get_state()}


@transactional
def reorder_trays(body):
    order = body.get("order") or []
    if not isinstance(order, list) or not all(isinstance(i, int) for i in order):
        raise ApiError("顺序格式不正确")
    existing = {r["id"] for r in rows("SELECT id FROM trays")}
    if set(order) != existing:
        raise ApiError("顺序必须包含全部字盘且不重复")
    for i, tid in enumerate(order):
        db.execute("UPDATE trays SET sort_order=? WHERE id=?",
                   (i * 10, tid))
    return {"ok": True, "state": get_state()}


@transactional
def duplicate_tray(tray_id, body):
    """复制布局：只复制格位结构（格号/坐标/尺寸/字种/容量），不带存量。"""
    src = get_tray(tray_id)
    name = str(body.get("name") or "").strip() or (src["name"] + "·副本")
    # 副本是另一个实体字盘，必须分配独立扫描码：指定则校验，缺省自动生成
    code = str(body.get("scan_code") or "").strip()
    code = _validate_scan_code(code) if code else _unique_scan_code()
    mx = row("SELECT COALESCE(MAX(sort_order),0) AS m FROM trays")["m"]
    cur = db.execute(
        "INSERT INTO trays(name,scan_code,sort_order,created_at) "
        "VALUES(?,?,?,?)", (name, code, mx + 10, now()))
    nid = cur.lastrowid
    n = 0
    for c in rows("SELECT * FROM cells WHERE tray_id=? ORDER BY id", (tray_id,)):
        db.execute(
            "INSERT INTO cells(tray_id,label,char,font,size,x,y,w,h,"
            "qty,capacity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (nid, c["label"], c["char"], c["font"], c["size"],
             c["x"], c["y"], c["w"], c["h"], 0, c["capacity"]))
        n += 1
    return {"ok": True, "tray_id": nid, "copied": n, "scan_code": code,
            "state": get_state()}

# ---------------------------------------------------------------- 格位 API


CELL_FIELDS = ("label", "char", "font", "size", "x", "y", "w", "h",
               "qty", "capacity")


def _cell_tray(body, default=None):
    tid = body.get("tray_id", default)
    if tid is None:
        t = row("SELECT id FROM trays ORDER BY sort_order, id LIMIT 1")
        tid = t["id"] if t else None
    return int(tid)


@transactional
def create_cell(body):
    tray_id = _cell_tray(body)
    get_tray(tray_id)
    label = str(body.get("label") or "").strip()
    if not label:
        raise ApiError("格号不能为空")
    if row("SELECT id FROM cells WHERE tray_id=? AND label=?",
           (tray_id, label)):
        raise ApiError("字盘内格号「%s」已存在" % label)
    db.execute(
        "INSERT INTO cells(tray_id,label,char,font,size,x,y,w,h,qty,capacity) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (tray_id, label, str(body.get("char") or ""),
         str(body.get("font") or ""), str(body.get("size") or ""),
         float(body.get("x") or 0), float(body.get("y") or 0),
         float(body.get("w") or 46), float(body.get("h") or 46),
         int(body.get("qty") or 0), int(body.get("capacity") or 50)))
    return {"ok": True, "state": get_state()}


@transactional
def update_cell(cell_id, body):
    cell = row("SELECT * FROM cells WHERE id=?", (cell_id,))
    if not cell:
        raise ApiError("格位不存在", 404)
    if "tray_id" in body and int(body["tray_id"]) != cell["tray_id"]:
        raise ApiError("不能把格位移到另一个字盘，请在目标盘新建")
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
            dup = row("SELECT id FROM cells WHERE tray_id=? AND label=? "
                      "AND id!=?", (cell["tray_id"], v, cell_id))
            if dup:
                raise ApiError("字盘内格号「%s」已存在" % v)
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
    default_tray = body.get("tray_id")
    for c in body.get("cells", []):
        tray_id = int(c.get("tray_id") or default_tray or 0)
        if not tray_id:
            t = row("SELECT id FROM trays ORDER BY sort_order, id LIMIT 1")
            tray_id = t["id"]
        if not row("SELECT id FROM trays WHERE id=?", (tray_id,)):
            skipped.append(str(c.get("label") or ""))
            continue
        label = str(c.get("label") or "").strip()
        if not label:
            continue
        if row("SELECT id FROM cells WHERE tray_id=? AND label=?",
               (tray_id, label)):
            skipped.append(label)
            continue
        db.execute(
            "INSERT INTO cells(tray_id,label,char,font,size,x,y,w,h,"
            "qty,capacity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tray_id, label, str(c.get("char") or ""),
             str(c.get("font") or ""), str(c.get("size") or ""),
             float(c.get("x") or 0), float(c.get("y") or 0),
             float(c.get("w") or 46), float(c.get("h") or 46),
             int(c.get("qty") or 0), int(c.get("capacity") or 50)))
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
    old = row("SELECT id FROM sessions WHERE status IN ('planning','active')")
    if old:
        raise ApiError("已有进行中的批次（#%d），请先完成或作废" % old["id"])
    cells = rows("SELECT * FROM cells")
    cur = db.execute("INSERT INTO sessions(name,status,created_at) "
                     "VALUES(?, 'planning', ?)",
                     (body.get("name") or "归还批次 " + now(), now()))
    sid = cur.lastrowid
    # 先按目标字盘归组：merged[tray_id][cell_id] -> 合并后的任务
    merged = {}
    new_pending = []
    for ch, qty, font, size, raw in items:
        kind, reason, cell, note = classify(ch, font, size, cells)
        if kind == "task":
            group = merged.setdefault(cell["tray_id"], {})
            if cell["id"] in group:
                group[cell["id"]]["qty"] += qty
            else:
                group[cell["id"]] = {
                    "cell_id": cell["id"], "tray_id": cell["tray_id"],
                    "char": ch, "font": cell["font"], "size": cell["size"],
                    "qty": qty, "x": cell["x"], "y": cell["y"]}
        else:
            new_pending.append((sid, ch, qty, reason, note, now()))
    # 再在每个盘内蛇形规划格位次序
    tray_order = []
    for tr in rows("SELECT * FROM trays ORDER BY sort_order, id"):
        group = merged.get(tr["id"])
        if not group:
            continue
        tray_order.append(tr["id"])
        by_id = {c["id"]: c for c in cells if c["tray_id"] == tr["id"]}
        for i, t in enumerate(plan_order(list(group.values()))):
            cell = by_id[t["cell_id"]]
            note = "预计超容" if cell["qty"] + t["qty"] > cell["capacity"] \
                else ""
            db.execute(
                "INSERT INTO tasks(session_id,tray_id,cell_id,char,font,size,"
                "qty,seq,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, tr["id"], t["cell_id"], t["char"], t["font"],
                 t["size"], t["qty"], i + 1, note))
    db.execute("UPDATE sessions SET tray_order=?, start_tray_id=? WHERE id=?",
               (json.dumps(tray_order),
                tray_order[0] if tray_order else None, sid))
    for p in new_pending:
        db.execute(
            "INSERT INTO pending_items"
            "(session_id,raw,qty,reason,suggestion,created_at) "
            "VALUES(?,?,?,?,?,?)", p)
    if not tray_order:
        # 全部落入待确认：批次直接停在规划页等待补录
        pass
    return {"ok": True, "state": get_state()}


@transactional
def plan_session(session_id, body):
    """规划页：调整换盘顺序、指定起始盘，并可一次性锁定开始执行。"""
    sess = row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not sess:
        raise ApiError("批次不存在", 404)
    if sess["status"] not in ("planning", "active"):
        raise ApiError("批次已结束，不能调整规划")
    task_trays = {r["tray_id"] for r in rows(
        "SELECT DISTINCT tray_id FROM tasks WHERE session_id=?", (session_id,))}
    if not task_trays:
        raise ApiError("本批次还没有可执行的格位任务（请先在待确认中归位）")
    order = body.get("tray_order")
    if order is not None:
        if sess["locked"]:
            raise ApiError("换盘顺序已锁定，不能再调整")
        if not isinstance(order, list) or not all(isinstance(i, int)
                                                  for i in order):
            raise ApiError("换盘顺序格式不正确")
        if set(order) != task_trays:
            raise ApiError("换盘顺序必须恰好包含本批次涉及的 %d 个字盘"
                           % len(task_trays))
    else:
        order = [i for i in _loads(sess["tray_order"]) if i in task_trays]
        order += [t for t in task_trays if t not in order]
    start = body.get("start_tray_id")
    if start is not None:
        start = int(start)
        if start not in task_trays:
            raise ApiError("起始盘必须是本批次涉及的字盘")
        # 起始盘提到首位，其余保持调整后的相对顺序
        order = [start] + [t for t in order if t != start]
    lock = bool(body.get("lock"))
    if lock:
        if sess["status"] == "active":
            raise ApiError("批次已开始执行")
        bad = _check_tray_codes(task_trays)
        if bad:
            names = "；".join("「%s」%s" % (x["name"], x["problem"])
                              for x in bad)
            raise ApiError("以下字盘缺少唯一可扫描的字盘码，无法开始：" + names
                           + "。请到字盘编辑页设置后再锁定。")
        start_tray = start or sess["start_tray_id"] or order[0]
        db.execute("UPDATE sessions SET status='active', locked=1, "
                   "tray_order=?, start_tray_id=?, current_tray_id=? "
                   "WHERE id=?",
                   (json.dumps(order), start_tray, start_tray, session_id))
        # 进入起始盘的换盘闸门：首个格位等扫描字盘码确认后才激活
    else:
        db.execute("UPDATE sessions SET tray_order=?, start_tray_id=? "
                   "WHERE id=?",
                   (json.dumps(order),
                        start or sess["start_tray_id"] or order[0],
                        session_id))
    return {"ok": True, "state": get_state()}


@transactional
def confirm_tray(session_id, body):
    """跨盘闸门：扫描实体字盘码，核对预期盘后确认换盘。"""
    sess = row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not sess:
        raise ApiError("批次不存在", 404)
    if sess["status"] != "active":
        raise ApiError("批次未在执行中")
    expected_id = None
    t = _current_task(session_id)
    if t:
        expected_id = t["tray_id"]
    else:
        # 当前盘的格位已做完，按锁定顺序找下一个还有任务的盘
        order = _loads(sess["tray_order"])
        remaining = {r["tray_id"] for r in rows(
            "SELECT DISTINCT tray_id FROM tasks WHERE session_id=? "
            "AND status='pending'", (session_id,))}
        for tid in order:
            if tid in remaining:
                expected_id = tid
                break
    if expected_id is None:
        raise ApiError("没有等待确认的字盘")
    expected = get_tray(expected_id)
    code = str(body.get("scan_code") or "").strip()
    if not code:
        raise ApiError("请扫描字盘码")
    scanned = row("SELECT * FROM trays WHERE scan_code=? AND scan_code!=''",
                  (code,))
    if not scanned:
        return conflict("tray_unknown",
                        "无法识别的字盘码「%s」，请重新扫描实体字盘" % code,
                        expected=expected, scanned_code=code,
                        remaining=_remaining_summary(session_id, expected_id))
    if scanned["id"] != expected_id:
        return conflict("tray_mismatch",
                        "字盘不符：应为「%s」（%s），扫到「%s」（%s）。"
                        "请暂停核对实物，确认换到正确字盘后再扫"
                        % (expected["name"], expected["scan_code"] or "未设码",
                           scanned["name"], scanned["scan_code"]),
                        expected=expected, scanned=scanned,
                        remaining=_remaining_summary(session_id, expected_id))
    db.execute("UPDATE sessions SET current_tray_id=? WHERE id=?",
               (expected_id, session_id))
    first = row("SELECT id FROM tasks WHERE session_id=? AND tray_id=? "
                "AND status='pending' ORDER BY seq LIMIT 1",
                (session_id, expected_id))
    if first and not _current_task(session_id):
        db.execute("UPDATE tasks SET status='active' WHERE id=?",
                   (first["id"],))
    return {"ok": True, "state": get_state()}


def _remaining_summary(session_id, tray_id):
    """闸门提示用：下一盘与剩余任务概况。"""
    order = _loads(row("SELECT tray_order FROM sessions WHERE id=?",
                       (session_id,))["tray_order"])
    pending = rows("SELECT tray_id, COUNT(*) AS n, SUM(qty-done_qty) AS q "
                   "FROM tasks WHERE session_id=? AND status IN "
                   "('pending','active') GROUP BY tray_id", (session_id,))
    by = {r["tray_id"]: r for r in pending}
    trays = {t["id"]: t for t in rows("SELECT * FROM trays")}
    groups, cur_idx = [], order.index(tray_id) if tray_id in order else -1
    for i, tid in enumerate(order):
        if tid not in by:
            continue
        groups.append({"tray_id": tid, "name": trays[tid]["name"],
                       "scan_code": trays[tid]["scan_code"],
                       "tasks": by[tid]["n"], "qty": by[tid]["q"],
                       "current": i == cur_idx})
    nxt = next((g for g in groups if not g["current"]), None)
    total_tasks = sum(g["tasks"] for g in groups)
    total_qty = sum(g["qty"] for g in groups)
    return {"groups": groups, "next_tray": nxt,
            "total_tasks": total_tasks, "total_qty": total_qty}


def _candidates(task, current_cell):
    """改派候选格：限同一字盘；同字符其他格优先，其次空格位。"""
    out = []
    for c in rows("SELECT * FROM cells WHERE id != ? AND tray_id=? ORDER BY "
                  "(char = ?) DESC, (capacity - qty) DESC, label",
                  (current_cell["id"], task["tray_id"], task["char"])):
        if c["char"] not in (task["char"], ""):
            continue
        out.append({"id": c["id"], "label": c["label"], "char": c["char"],
                    "font": c["font"], "size": c["size"], "x": c["x"],
                    "y": c["y"], "qty": c["qty"], "capacity": c["capacity"],
                    "tray_id": c["tray_id"], "free": c["capacity"] - c["qty"]})
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
        raise ApiError("批次未在执行中，不能确认")
    if t["status"] == "done":
        return conflict("duplicate", "该任务已完成，请勿重复确认", task=t)
    if t["status"] != "active":
        return conflict("not_current", "请先完成当前高亮的格位", task=t)
    cell = row("SELECT * FROM cells WHERE id=?", (t["cell_id"],))
    if not cell:
        raise ApiError("任务没有目标格位")
    if label:
        # 只认当前字盘内的格号：扫到别的盘的格也视为实物不符
        scanned = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                      "JOIN trays tr ON tr.id=c.tray_id "
                      "WHERE c.tray_id=? AND upper(c.label)=?",
                      (t["tray_id"], label.upper()))
        if not scanned:
            same_label = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                             "JOIN trays tr ON tr.id=c.tray_id "
                             "WHERE upper(c.label)=?", (label.upper(),))
            if same_label:
                cur_tray = get_tray(t["tray_id"])
                return conflict("wrong_tray",
                                "扫到的格号「%s」属于字盘「%s」，"
                                "当前应在「%s」上操作"
                                % (same_label["label"],
                                   same_label["tray_name"], cur_tray["name"]),
                                label=label, other_tray=same_label["tray_name"])
            return conflict("unknown_label",
                            "当前字盘无法识别的格号「%s」" % label, label=label)
        if scanned["id"] != cell["id"]:
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
                            "tray_id": t["tray_id"], "task_id": task_id}),
                now()))
    if st == "done":
        _advance(t["session_id"])
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
    # 换盘导航下不允许跨字盘改派：跨盘必须走换盘闸门
    if new["tray_id"] != t["tray_id"]:
        raise ApiError("不能跨字盘改派；目标在另一个字盘上，请按换盘顺序操作")
    # 空格位首次接收铅字：同步登记字种（字符/字体/字号），便于之后再匹配
    synced = False
    if new["char"] == "" and t["char"]:
        db.execute("UPDATE cells SET char=?, font=?, size=? WHERE id=?",
                   (t["char"], t["font"], t["size"], new["id"]))
        new = row("SELECT * FROM cells WHERE id=?", (new_id,))
        synced = True
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
    return {"ok": True, "warn": warn, "synced": synced,
            "state": get_state()}


@transactional
def skip_task(task_id):
    t = row("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ApiError("任务不存在", 404)
    if t["status"] != "active":
        raise ApiError("只能跳过当前任务")
    db.execute("UPDATE tasks SET status='skipped' WHERE id=?", (task_id,))
    _advance(t["session_id"])
    return {"ok": True, "state": get_state()}


@transactional
def undo(session_id):
    sess = row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not sess:
        raise ApiError("批次不存在", 404)
    if sess["status"] == "abandoned":
        raise ApiError("批次已作废，不能撤销")
    if sess["status"] == "done" and row(
            "SELECT id FROM sessions WHERE status IN ('planning','active')"):
        raise ApiError("已有进行中的批次，请先完成或作废后再撤销上一批次")
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
        # 撤销后该任务成为唯一当前任务（即使此前已推进到换盘闸门）
        db.execute("UPDATE tasks SET status='pending' WHERE session_id=? "
                   "AND status='active' AND id!=?", (session_id, t["id"]))
        db.execute("UPDATE sessions SET status='active', current_tray_id=?, "
                   "finished_at=NULL WHERE id=?",
                   (t["tray_id"], session_id))
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
        raise ApiError("批次未在执行中")
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
    sess = row("SELECT * FROM sessions WHERE status IN ('planning','active') "
               "ORDER BY id DESC LIMIT 1")
    created = False
    if not sess:
        cur = db.execute(
            "INSERT INTO sessions(name,status,created_at) "
            "VALUES(?, 'planning', ?)",
            ("待确认补录 " + now(), now()))
        sess = row("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,))
        created = True
    # 盘内序号：追加到该盘已有任务之后
    mx = row("SELECT COALESCE(MAX(seq),0) AS m FROM tasks "
             "WHERE session_id=? AND tray_id=?",
             (sess["id"], cell["tray_id"]))["m"]
    cur = db.execute(
        "INSERT INTO tasks(session_id,tray_id,cell_id,char,font,size,qty,seq,"
        "note) VALUES(?,?,?,?,?,?,?,?,?)",
        (sess["id"], cell["tray_id"], cell["id"], item["raw"], cell["font"],
         cell["size"], item["qty"], mx + 1, "待确认补录"))
    db.execute("INSERT INTO actions(session_id,task_id,cell_id,kind,delta,"
               "payload,created_at) VALUES(?,?,?,?,?,?,?)",
               (sess["id"], cur.lastrowid, cell["id"], "resolve", 0,
                json.dumps({"pending_id": pid, "raw": item["raw"]},
                           ensure_ascii=False), now()))
    # 更新换盘归组
    order = [i for i in _loads(sess["tray_order"]) if isinstance(i, int)]
    if cell["tray_id"] not in order:
        order.append(cell["tray_id"])
    start = sess["start_tray_id"] or order[0]
    if sess["status"] == "planning":
        db.execute("UPDATE sessions SET tray_order=?, start_tray_id=? "
                   "WHERE id=?", (json.dumps(order), start, sess["id"]))
    else:
        # 执行中补录：若是全新字盘，追加到锁定顺序末尾
        db.execute("UPDATE sessions SET tray_order=? WHERE id=?",
                   (json.dumps(order), sess["id"]))
        act = _current_task(sess["id"])
        if not act:
            # 当前盘已做完又补进别的盘：保持闸门等待扫描，不自动激活
            pass
    return {"ok": True, "state": get_state()}

# ---------------------------------------------------------------- 备份恢复

TABLES = ["trays", "cells", "sessions", "tasks", "pending_items", "actions"]
DELETE_ORDER = ["actions", "pending_items", "tasks", "sessions", "cells",
                "trays"]


def backup():
    with DB_LOCK:
        return {"app": "sortify", "version": 2, "exported_at": now(),
                "tables": {t: rows("SELECT * FROM %s" % t) for t in TABLES}}


@transactional
def restore(data):
    if not isinstance(data, dict) or data.get("app") != "sortify" \
            or not isinstance(data.get("tables"), dict):
        raise ApiError("备份文件格式不正确")
    ver = int(data.get("version") or 1)
    for t in DELETE_ORDER:
        db.execute("DELETE FROM %s" % t)
    tables = data["tables"]
    if ver < 2 or "trays" not in tables or not tables.get("trays"):
        # v1 备份：全部格位归入「默认字盘」，保留归属；默认码非空唯一
        cur = db.execute(
            "INSERT INTO trays(id,name,scan_code,sort_order,created_at) "
            "VALUES(1,?,?,0,?)", ("默认字盘", "TRAY-01", now()))
        for r in tables.get("cells", []):
            r = dict(r)
            r.pop("tray_id", None)
            r["tray_id"] = 1
            _insert("cells", r)
        for r in tables.get("sessions", []):
            r = dict(r)
            r.setdefault("status", "done")
            r.setdefault("tray_order", "[1]")
            r.setdefault("start_tray_id", 1)
            r.setdefault("locked", 1 if r.get("status") == "active" else 0)
            r.setdefault("current_tray_id", 1)
            _insert("sessions", r)
        for t in ("tasks", "pending_items", "actions"):
            for r in tables.get(t, []):
                r = dict(r)
                if t == "tasks":
                    r["tray_id"] = r.get("tray_id") or 1
                _insert(t, r)
        # 显式指定 id 后刷新自增序列，避免后续主键冲突
        for t in ("trays", "cells", "sessions", "tasks", "pending_items",
                  "actions"):
            mx = db.execute("SELECT COALESCE(MAX(id),0) AS m FROM %s" % t) \
                .fetchone()["m"]
            db.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES(?,?)",
                (t, mx))
    else:
        # v2 备份也可能来自旧版本（空 / 重复扫描码）：插入前对整批托盘行
        # 统一规整为空码补齐、重复码重新分配，避免插入时触发唯一索引
        used = set()
        for r in tables.get("trays", []):
            if not isinstance(r, dict) or not r:
                continue
            code = str(r.get("scan_code") or "").strip()
            if not code or code.upper() in used:
                base = code
                cand = None
                n = 2
                while True:
                    cand = ("%s-%d" % (base, n)) if base else "TRAY-%02d" % n
                    if cand.upper() not in used and not row(
                            "SELECT id FROM trays WHERE upper(scan_code)=?",
                            (cand.upper(),)):
                        break
                    n += 1
                code = cand
            r["scan_code"] = code
            used.add(code.upper())
        for t in TABLES:
            for r in tables.get(t, []):
                if isinstance(r, dict) and r:
                    _insert(t, r)
        for t in TABLES:
            mx = db.execute("SELECT COALESCE(MAX(id),0) AS m FROM %s" % t) \
                .fetchone()["m"]
            db.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES(?,?)",
                (t, mx))
    # 兜底：确保恢复后所有字盘都有非空唯一扫描码
    normalize_scan_codes()
    return {"ok": True, "state": get_state()}


def _insert(table, r):
    cols = ",".join(r.keys())
    qs = ",".join("?" * len(r))
    db.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, cols, qs),
               list(r.values()))

# ---------------------------------------------------------------- 种子数据

SEED_CHARS = ("的一是不了人在我有他这中大来上国个到说们为子和你地出道也"
              "时年得就那要下以生会自着去之体永文言心")
SEED_PUNCT = "，。、「」"


def seed():
    """首次启动时生成默认字盘：6 行常用字 + 1 行标点与空格位。"""
    cur = db.execute(
        "INSERT INTO trays(name,scan_code,sort_order,created_at) "
        "VALUES(?,?,0,?)", ("默认字盘", "TRAY-01", now()))
    tid = cur.lastrowid
    for i, ch in enumerate(SEED_CHARS):
        r, c = divmod(i, 8)
        db.execute(
            "INSERT INTO cells(tray_id,label,char,font,size,x,y,w,h,"
            "qty,capacity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "%s%d" % (chr(65 + r), c + 1), ch, "宋体", "五号",
             20 + c * 54, 20 + r * 54, 46, 46, 20 + (i * 7) % 25, 50))
    for j, ch in enumerate(SEED_PUNCT):
        db.execute(
            "INSERT INTO cells(tray_id,label,char,font,size,x,y,w,h,"
            "qty,capacity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "G%d" % (j + 1), ch, "宋体", "五号",
             20 + j * 54, 20 + 6 * 54, 46, 46, 20, 50))
    for j in range(2):  # 空格位，供改派使用
        db.execute(
            "INSERT INTO cells(tray_id,label,char,font,size,x,y,w,h,"
            "qty,capacity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "G%d" % (len(SEED_PUNCT) + j + 1), "", "", "",
             20 + (len(SEED_PUNCT) + j) * 54, 20 + 6 * 54, 46, 46, 0, 50))


def _migrate():
    """把旧版（单字盘）数据库升级到多字盘结构，原有格位归入默认字盘。"""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(cells)")}
    if "tray_id" in cols:
        return
    # 重建 cells（旧表 label 为全局 UNIQUE，需去掉并改为盘内唯一索引）
    db.execute("PRAGMA legacy_alter_table=ON")
    db.execute("ALTER TABLE cells RENAME TO cells_old")
    db.executescript("""
    CREATE TABLE cells (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      tray_id   INTEGER NOT NULL REFERENCES trays(id),
      label     TEXT NOT NULL,
      char      TEXT NOT NULL DEFAULT '',
      font      TEXT NOT NULL DEFAULT '',
      size      TEXT NOT NULL DEFAULT '',
      x REAL NOT NULL DEFAULT 0, y REAL NOT NULL DEFAULT 0,
      w REAL NOT NULL DEFAULT 46, h REAL NOT NULL DEFAULT 46,
      qty INTEGER NOT NULL DEFAULT 0,
      capacity INTEGER NOT NULL DEFAULT 50
    );
    CREATE UNIQUE INDEX idx_cells_tray_label ON cells(tray_id, label);
    """)
    cur = db.execute(
        "INSERT INTO trays(name,scan_code,sort_order,created_at) "
        "VALUES(?,?,0,?)", ("默认字盘", "TRAY-01", now()))
    tid = cur.lastrowid
    db.execute(
        "INSERT INTO cells(id,tray_id,label,char,font,size,x,y,w,h,qty,"
        "capacity) SELECT id,?,label,char,font,size,x,y,w,h,qty,capacity "
        "FROM cells_old ORDER BY id", (tid,))
    db.execute("UPDATE cells SET label=('格'||id) WHERE label IS NULL OR label=''")
    db.execute("DROP TABLE cells_old")
    db.execute("PRAGMA legacy_alter_table=OFF")
    db.execute("INSERT OR REPLACE INTO sqlite_sequence(name,seq) "
               "SELECT 'cells', MAX(id) FROM cells")

    scols = {r["name"] for r in db.execute("PRAGMA table_info(sessions)")}
    for ddl in (
        "ALTER TABLE sessions ADD COLUMN tray_order TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE sessions ADD COLUMN start_tray_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN locked INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN current_tray_id INTEGER",
    ):
        col = ddl.split("ADD COLUMN")[1].split()[0]
        if col not in scols:
            db.execute(ddl)
    db.execute("ALTER TABLE tasks ADD COLUMN tray_id INTEGER")
    db.execute("UPDATE tasks SET tray_id=? WHERE tray_id IS NULL", (tid,))
    # 旧批次：仍在进行的升级为已锁定、当前盘=默认盘；历史批次同样补归属
    db.execute("UPDATE sessions SET tray_order=?, start_tray_id=?, "
               "current_tray_id=? WHERE tray_order='[]' OR tray_order IS NULL",
               (json.dumps([tid]), tid, tid))
    db.execute("UPDATE sessions SET locked=1 WHERE status='active' "
               "AND locked=0")


def init_db():
    with DB_LOCK:
        db.executescript(SCHEMA)
        _migrate()
        # 修复既有空 / 重复扫描码后，再建立全局唯一索引兜底
        normalize_scan_codes()
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trays_scan_code "
                   "ON trays(lower(scan_code)) WHERE scan_code <> ''")
        # 盘内格号唯一索引：旧库在迁移加列之后才能创建
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cells_tray_label "
                   "ON cells(tray_id, label)")
        if not row("SELECT id FROM trays LIMIT 1"):
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

    # ---- 字盘 ----
    if method == "POST" and path == "/api/trays":
        return create_tray(body)
    if method == "POST" and path == "/api/trays/reorder":
        return reorder_trays(body)
    m = re.fullmatch(r"/api/trays/(\d+)", path)
    if m:
        tid = int(m.group(1))
        if method == "PUT":
            return update_tray(tid, body)
        if method == "DELETE":
            return delete_tray(tid)
    m = re.fullmatch(r"/api/trays/(\d+)/duplicate", path)
    if m and method == "POST":
        return duplicate_tray(int(m.group(1)), body)

    # ---- 格位 ----
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

    # ---- 批次 ----
    if method == "POST" and path == "/api/sessions":
        return create_session(body)
    m = re.fullmatch(r"/api/sessions/(\d+)/plan", path)
    if m and method == "POST":
        return plan_session(int(m.group(1)), body)
    m = re.fullmatch(r"/api/sessions/(\d+)/confirm-tray", path)
    if m and method == "POST":
        return confirm_tray(int(m.group(1)), body)
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
    server_version = "Sortify/2.0"

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
