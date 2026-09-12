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
import unicodedata
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
CREATE TABLE IF NOT EXISTS stocktakes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tray_id INTEGER NOT NULL REFERENCES trays(id),
  status TEXT NOT NULL DEFAULT 'counting', -- counting / reviewing / posted / cancelled
  created_at TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,                       -- 全盘格位录完、进入差异复核
  posted_at TEXT,                         -- 一次性入账时间
  cancelled_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stocktake_cells (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id),
  cell_id INTEGER NOT NULL REFERENCES cells(id),
  seq INTEGER NOT NULL,                   -- 盘内蛇形高亮次序
  book_label TEXT NOT NULL DEFAULT '',
  book_char TEXT NOT NULL DEFAULT '', book_font TEXT NOT NULL DEFAULT '',
  book_size TEXT NOT NULL DEFAULT '', book_qty INTEGER NOT NULL DEFAULT 0,
  actual_qty INTEGER,                     -- 实物点数；暂时不能盘时为 NULL
  actual_char TEXT, actual_font TEXT, actual_size TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending / counted / mixed / unrecognized / blocked
  counted_at TEXT,
  UNIQUE(stocktake_id, cell_id)
);
CREATE TABLE IF NOT EXISTS discrepancies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id),
  cell_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                     -- short / surplus / review
  review_kind TEXT NOT NULL DEFAULT '',   -- kind=review：mixed / unrecognized / blocked
  spec_char TEXT NOT NULL DEFAULT '',
  spec_font TEXT NOT NULL DEFAULT '',
  spec_size TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 0,         -- 差额绝对值
  decision TEXT NOT NULL DEFAULT '',      -- '' / gain(盘盈) / loss(盘亏)
  move_task_id INTEGER,
  posted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discrepancy_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stocktake_id INTEGER NOT NULL,
  code TEXT NOT NULL,                     -- P1、P2…
  spec_char TEXT NOT NULL DEFAULT '',
  spec_font TEXT NOT NULL DEFAULT '',
  spec_size TEXT NOT NULL DEFAULT '',
  src_disc_id INTEGER NOT NULL,           -- 溢出端：实物多出的格（移格来源）
  dst_disc_id INTEGER NOT NULL,           -- 短缺端：账面应有却少的格（移格目标）
  qty INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate', -- candidate / moved / writeoff / posted
  move_task_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS move_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id),
  source_cell_id INTEGER NOT NULL,
  target_cell_id INTEGER NOT NULL,
  spec_char TEXT NOT NULL DEFAULT '', spec_font TEXT NOT NULL DEFAULT '',
  spec_size TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', -- pending / src_ok / done / cancelled / posted
  src_scan TEXT NOT NULL DEFAULT '',
  tgt_scan TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  done_at TEXT
);
CREATE TABLE IF NOT EXISTS postings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stocktake_id INTEGER NOT NULL,
  cell_id INTEGER NOT NULL,
  move_task_id INTEGER,
  kind TEXT NOT NULL,                     -- gain / loss / spec / move_out / move_in
  qty INTEGER NOT NULL DEFAULT 0,
  before_qty INTEGER NOT NULL,
  after_qty INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requisitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'planning', -- planning / active / done / cancelled
  include_spaces INTEGER NOT NULL DEFAULT 0,
  include_punct INTEGER NOT NULL DEFAULT 1,
  input_mode TEXT NOT NULL DEFAULT 'text', -- text / list
  source_text TEXT NOT NULL DEFAULT '',
  tray_order TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  finished_at TEXT,
  cancelled_at TEXT,
  returned_session_id INTEGER              -- 一键带入归还流程后生成的归还批次
);
CREATE TABLE IF NOT EXISTS req_demands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id INTEGER NOT NULL REFERENCES requisitions(id),
  char TEXT NOT NULL,
  font TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL,                   -- 汇总后的总需求
  issue TEXT NOT NULL DEFAULT '',         -- '' / short / variant / spec / none
  issue_text TEXT NOT NULL DEFAULT '',
  seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS req_allocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id INTEGER NOT NULL REFERENCES requisitions(id),
  demand_id INTEGER NOT NULL REFERENCES req_demands(id),
  tray_id INTEGER NOT NULL REFERENCES trays(id),
  cell_id INTEGER NOT NULL REFERENCES cells(id),
  qty INTEGER NOT NULL,                   -- 锁定时在该格的预留 / 应取数量
  taken_qty INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'reserved', -- reserved / active / done / gap / cancelled
  seq INTEGER NOT NULL DEFAULT 0,         -- 盘内路线次序
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS req_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id INTEGER NOT NULL REFERENCES requisitions(id),
  allocation_id INTEGER,                  -- 来源格操作（pick/change/gap）时的分配
  kind TEXT NOT NULL,                     -- pick / gap / change
  cell_id INTEGER,
  tray_id INTEGER,
  qty INTEGER NOT NULL DEFAULT 0,
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

# 盘点相关名称
STOCK_STATUS_NAMES = {"counting": "盘点中", "reviewing": "差异复核",
                      "posted": "已入账", "cancelled": "已取消"}
COUNT_STATUS_NAMES = {"pending": "待盘", "counted": "已盘",
                      "mixed": "混字", "unrecognized": "无法辨认",
                      "blocked": "暂不能盘"}
REVIEW_NAMES = {"mixed": "混字", "unrecognized": "无法辨认",
                "blocked": "暂时不能盘点"}
DISC_KIND_NAMES = {"short": "短缺", "surplus": "溢出", "review": "待复核"}

# 盘点进行中（锁住归还确认与存量编辑）的状态
ST_ACTIVE = ("counting", "reviewing")

# 领用相关名称
REQ_STATUS_NAMES = {"planning": "待锁定", "active": "执行中",
                    "done": "已完成", "cancelled": "已取消"}
ALLOC_STATUS_NAMES = {"reserved": "已预留", "active": "当前格",
                      "done": "已取", "gap": "缺口", "cancelled": "已取消"}
REQ_ISSUE_NAMES = {"pick": "实取", "gap": "保留缺口", "change": "改选来源格"}


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
        # 盘点：进行中的（最多每盘一个）全量载入；另附最近历史概要与名称表
        stock_rows = rows("SELECT * FROM stocktakes WHERE status IN ('counting',"
                          "'reviewing') ORDER BY id")
        stocktakes = [_load_stocktake(s) for s in stock_rows]
        history = [_stock_brief(s) for s in rows(
            "SELECT * FROM stocktakes WHERE status IN ('posted','cancelled') "
            "ORDER BY id DESC LIMIT 10")]
        # 配字领用：最近一张进行中的领用单全量载入；另附最近历史概要
        req_row = row("SELECT * FROM requisitions WHERE status IN "
                      "('planning','active') ORDER BY id DESC LIMIT 1")
        requisition = _load_requisition(req_row) if req_row else None
        req_history = [_req_brief(x) for x in rows(
            "SELECT * FROM requisitions WHERE status IN ('done','cancelled') "
            "ORDER BY id DESC LIMIT 10")]
        return {"trays": trays, "cells": cells, "session": session,
                "last_done": last_done, "pending": pending,
                "conflicts": capacity_conflicts(cells, session),
                "reason_names": REASON_NAMES,
                "stocktakes": stocktakes, "stocktake_history": history,
                "stock_status_names": STOCK_STATUS_NAMES,
                "count_status_names": COUNT_STATUS_NAMES,
                "review_names": REVIEW_NAMES,
                "disc_kind_names": DISC_KIND_NAMES,
                "requisition": requisition, "req_history": req_history,
                "req_status_names": REQ_STATUS_NAMES,
                "alloc_status_names": ALLOC_STATUS_NAMES,
                "req_issue_names": REQ_ISSUE_NAMES}


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

# ---------------------------------------------------------------- 盘点锁


def _active_stocktake(tray_id):
    """该字盘是否有进行中的盘点（盘点中 / 差异复核）。有则返回盘点行。"""
    return row("SELECT * FROM stocktakes WHERE tray_id=? AND status IN ('counting',"
               "'reviewing') ORDER BY id DESC LIMIT 1", (tray_id,))


def _assert_no_stocktake(tray_id, verb):
    """盘点期间锁住该盘的归还确认与存量编辑。"""
    st = _active_stocktake(tray_id)
    if st:
        raise ApiError("字盘正在盘点（盘点单 #%d，%s），盘点结束前不能%s"
                       % (st["id"], STOCK_STATUS_NAMES[st["status"]], verb))


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
    if row("SELECT COUNT(*) AS n FROM req_allocations WHERE tray_id=?",
           (tray_id,))["n"]:
        raise ApiError("字盘「%s」已有领用单记录，不能删除" % t["name"])
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
    _assert_no_stocktake(tray_id, "在该盘新建格位")
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
    _assert_no_stocktake(cell["tray_id"], "编辑该盘格位")
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
    tray_id = row("SELECT tray_id FROM cells WHERE id=?", (cell_id,))["tray_id"]
    _assert_no_stocktake(tray_id, "删除该盘格位")
    used = row("SELECT COUNT(*) AS n FROM tasks WHERE cell_id=?", (cell_id,))
    if used["n"]:
        raise ApiError("该格已有归还任务记录，不能删除")
    used = row("SELECT COUNT(*) AS n FROM req_allocations WHERE cell_id=?",
               (cell_id,))
    if used["n"]:
        raise ApiError("该格已有领用单记录，不能删除")
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
        if _active_stocktake(tray_id):
            raise ApiError("目标字盘正在盘点，盘点结束前不能新建格位")
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
    _assert_no_stocktake(t["tray_id"], "确认归还（该盘正在盘点）")
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
    _assert_no_stocktake(new["tray_id"], "改派（该盘正在盘点）")
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

# ---------------------------------------------------------------- 配字领用 API


def _is_space_char(ch):
    return len(ch) == 1 and unicodedata.category(ch) == "Zs"


def _is_punct_char(ch):
    return len(ch) == 1 and unicodedata.category(ch).startswith("P")


def parse_requisition_text(text, mode):
    """解析配字领用输入。

    mode='text'：整段待排文字，逐字符计数（无字体字号，换行等控制符一律不计）；
    mode='list'：每行「字符、数量、字体、字号」，沿用归还清单写法。
    返回 [(char, qty, font, size, raw), ...]
    """
    if mode == "text":
        return [(ch, 1, "", "", ch) for ch in (text or "")
                if unicodedata.category(ch) != "Cc"]
    return parse_return_text(text)


def _filter_req_items(items, include_spaces, include_punct):
    kept, n_space, n_punct = [], 0, 0
    for ch, qty, font, size, raw in items:
        if _is_space_char(ch):
            if include_spaces:
                kept.append((ch, qty, font, size, raw))
            else:
                n_space += qty
            continue
        if _is_punct_char(ch):
            if include_punct:
                kept.append((ch, qty, font, size, raw))
            else:
                n_punct += qty
            continue
        kept.append((ch, qty, font, size, raw))
    return kept, n_space, n_punct


def _aggregate_req(items):
    """按 字符+字体+字号 汇总需求，保留首次出现顺序。"""
    total, order = {}, []
    for ch, qty, font, size, raw in items:
        key = (ch, font, size)
        if key not in total:
            total[key] = 0
            order.append(key)
        total[key] += qty
    return [(ch, font, size, total[(ch, font, size)])
            for ch, font, size in order]


def _hold_map(exclude_req_id=None):
    """各格当前被未完成领用单预留（尚未实取）的数量。

    只计 reserved / active：gap（缺口）与 cancelled 不占库存，
    done 部分通过 taken_qty 已经实取扣过库存。
    """
    sql = ("SELECT cell_id, COALESCE(SUM(qty-taken_qty),0) AS held "
           "FROM req_allocations a JOIN requisitions r ON r.id=a.req_id "
           "WHERE r.status IN ('planning','active') "
           "AND a.status IN ('reserved','active') ")
    args = []
    if exclude_req_id is not None:
        sql += "AND a.req_id!=? "
        args.append(exclude_req_id)
    sql += "GROUP BY cell_id"
    return {r["cell_id"]: r["held"] for r in rows(sql, tuple(args))}


def _allocatable(cell, held):
    return max(0, cell["qty"] - held.get(cell["id"], 0))


def _held_on_cell(cell_id, exclude_req_id=None, exclude_alloc_ids=()):
    """某格被未完成领用单预留的数量，可排除指定领用单 / 指定分配行。"""
    sql = ("SELECT COALESCE(SUM(qty-taken_qty),0) AS n FROM req_allocations a "
           "JOIN requisitions r ON r.id=a.req_id "
           "WHERE a.cell_id=? AND r.status IN ('planning','active') "
           "AND a.status IN ('reserved','active') ")
    args = [cell_id]
    if exclude_req_id is not None:
        sql += "AND a.req_id!=? "
        args.append(exclude_req_id)
    if exclude_alloc_ids:
        ph = ",".join("?" * len(exclude_alloc_ids))
        sql += "AND a.id NOT IN (%s) " % ph
        args.extend(exclude_alloc_ids)
    return row(sql, tuple(args))["n"]


def _cell_avail(cell_id, exclude_req_id=None, exclude_alloc_ids=()):
    c = row("SELECT qty FROM cells WHERE id=?", (cell_id,))
    if not c:
        return 0
    return max(0, c["qty"] - _held_on_cell(
        cell_id, exclude_req_id, exclude_alloc_ids))


def _shortage_issue(ch, font, size, cells, remain):
    """没有任何可配格位时区分：仅有异体字 / 规格不符 / 无格位。不自动替换。"""
    same = [c for c in cells if c["char"] == ch]
    if same:
        specs = sorted({"%s / %s" % (c["font"] or "—", c["size"] or "—")
                        for c in same})
        return ("spec",
                "规格不符：需求 %s / %s，字盘中仅有 %s"
                % (font or "任意字体", size or "任意字号", "、".join(specs)))
    var = VARIANTS.get(ch)
    if var and any(c["char"] == var for c in cells):
        return ("variant", "仅有异体字：字盘中有正字「%s」的格位，不自动替换" % var)
    return ("short", "字盘中无「%s」的格位，缺 %d 枚" % (ch, remain))


def _plan_req_allocation(cells, trays, demands, exclude_req_id):
    """从多字盘同规格格位贪心分配可用铅字，扣除其他未完成领用单的预留。

    demands: [(seq, char, font, size, qty), ...]
    返回 (alloc_rows, issues)：alloc_rows=[(demand_seq, tray_id, cell_id, qty)]
    """
    held = _hold_map(exclude_req_id)
    rank = {t["id"]: i for i, t in enumerate(trays)}
    alloc_rows, issues = [], {}
    for seq, ch, font, size, need in demands:
        exact = [c for c in cells if c["char"] == ch
                 and (not font or c["font"] == font)
                 and (not size or c["size"] == size)]
        exact.sort(key=lambda c: (-_allocatable(c, held),
                                  rank.get(c["tray_id"], 1 << 30),
                                  c["y"], c["x"]))
        remain = need
        for c in exact:
            avail = _allocatable(c, held)
            if avail <= 0 or remain <= 0:
                break
            n = min(avail, remain)
            alloc_rows.append((seq, c["tray_id"], c["id"], n))
            held[c["id"]] = held.get(c["id"], 0) + n
            remain -= n
        if remain > 0:
            if exact:
                issues[seq] = ("short",
                               "数量不足：需求 %d 枚，已配 %d 枚，缺口 %d 枚"
                               % (need, need - remain, remain))
            else:
                issues[seq] = _shortage_issue(ch, font, size, cells, remain)
    return alloc_rows, issues


@transactional
def create_requisition(body):
    text = body.get("text") or ""
    mode = "list" if str(body.get("mode") or "") == "list" else "text"
    inc_spaces = bool(body.get("include_spaces"))
    inc_punct = bool(body.get("include_punct", True))
    items = parse_requisition_text(text, mode)
    items, d_space, d_punct = _filter_req_items(items, inc_spaces, inc_punct)
    if not items:
        raise ApiError("未解析到任何需要领用的字符（可能全部为空格 / 标点，已被排除）")
    agg = _aggregate_req(items)
    cells = rows("SELECT * FROM cells")
    trays = rows("SELECT * FROM trays ORDER BY sort_order, id")
    cur = db.execute(
        "INSERT INTO requisitions(name,status,include_spaces,include_punct,"
        "input_mode,source_text,created_at) VALUES(?, 'planning',?,?,?,?,?)",
        (str(body.get("name") or "").strip() or ("领用单 " + now()),
         int(inc_spaces), int(inc_punct), mode, text, now()))
    rid = cur.lastrowid
    demands = [(i + 1, ch, font, size, qty)
               for i, (ch, font, size, qty) in enumerate(agg)]
    alloc_rows, issues = _plan_req_allocation(cells, trays, demands, rid)
    for seq, ch, font, size, qty in demands:
        issue, itext = issues.get(seq, ("", ""))
        db.execute(
            "INSERT INTO req_demands(req_id,char,font,size,qty,issue,issue_text,"
            "seq) VALUES(?,?,?,?,?,?,?,?)",
            (rid, ch, font, size, qty, issue, itext, seq))
    demand_id = {r["seq"]: r["id"] for r in rows(
        "SELECT id, seq FROM req_demands WHERE req_id=?", (rid,))}
    # 盘内路线：按字盘归组后蛇形排序，盘内序号各自从 1 起
    cells_by_id = {c["id"]: c for c in cells}
    groups = {}
    for dseq, tid, cid, qty in alloc_rows:
        groups.setdefault(tid, []).append((dseq, cid, qty))
    involved = [t["id"] for t in trays if t["id"] in groups]
    ts = now()
    for tid in involved:
        ordered = plan_order([
            {"x": cells_by_id[cid]["x"], "y": cells_by_id[cid]["y"],
             "dseq": dseq, "cid": cid, "qty": qty}
            for dseq, cid, qty in groups[tid]])
        for i, it in enumerate(ordered):
            db.execute(
                "INSERT INTO req_allocations(req_id,demand_id,tray_id,cell_id,"
                "qty,status,seq,created_at) VALUES(?,?,?,?,?, 'reserved', ?,?)",
                (rid, demand_id[it["dseq"]], tid, it["cid"], it["qty"],
                 i + 1, ts))
    db.execute("UPDATE requisitions SET tray_order=? WHERE id=?",
               (json.dumps(involved), rid))
    return {"ok": True, "requisition_id": rid,
            "dropped_spaces": d_space, "dropped_punct": d_punct,
            "state": get_state()}


def _get_req(rid):
    r = row("SELECT * FROM requisitions WHERE id=?", (rid,))
    if not r:
        raise ApiError("领用单不存在", 404)
    return r


REQ_ALLOC_COLS = (
    "SELECT a.*, c.label, c.char AS cell_char, c.font AS cell_font, "
    "c.size AS cell_size, c.x, c.y, c.qty AS cell_qty, c.capacity, "
    "tr.name AS tray_name, tr.scan_code AS tray_code "
    "FROM req_allocations a "
    "JOIN cells c ON c.id=a.cell_id "
    "JOIN trays tr ON tr.id=a.tray_id ")


def _load_requisition(r):
    if not r:
        return None
    d = dict(r)
    d["tray_order"] = _loads(d.get("tray_order") or "[]")
    d["demands"] = rows(
        "SELECT * FROM req_demands WHERE req_id=? ORDER BY seq", (d["id"],))
    d["allocations"] = rows(
        REQ_ALLOC_COLS + "WHERE a.req_id=? ORDER BY a.tray_id, a.seq", (d["id"],))
    d["issues"] = rows(
        "SELECT * FROM req_issues WHERE req_id=? ORDER BY id", (d["id"],))
    held = _hold_map(d["id"])
    for a in d["allocations"]:
        a["avail_other"] = max(0, a["cell_qty"] - held.get(a["cell_id"], 0))
    return d


def _req_brief(r):
    d = dict(r)
    d["tray_order"] = _loads(d.get("tray_order") or "[]")
    n = row("SELECT COUNT(*) AS n, COALESCE(SUM(qty),0) AS q, "
            "COALESCE(SUM(taken_qty),0) AS t FROM req_allocations "
            "WHERE req_id=?", (r["id"],))
    d["n_allocs"] = n["n"]
    d["qty_reserved"] = n["q"]
    d["qty_taken"] = n["t"]
    d["n_gap"] = row("SELECT COUNT(*) AS n FROM req_allocations "
                     "WHERE req_id=? AND status='gap'", (r["id"],))["n"]
    d["n_issue_demands"] = row(
        "SELECT COUNT(*) AS n FROM req_demands WHERE req_id=? AND issue!=''",
        (r["id"],))["n"]
    return d


def _req_current(rid):
    return row(REQ_ALLOC_COLS + "WHERE a.req_id=? AND a.status='active' "
               "ORDER BY a.seq LIMIT 1", (rid,))


def _req_expected_tray(rid, order=None):
    act = _req_current(rid)
    if act:
        return act["tray_id"]
    if order is None:
        order = _loads(row("SELECT tray_order FROM requisitions WHERE id=?",
                           (rid,))["tray_order"])
    rem = {x["tray_id"] for x in rows(
        "SELECT DISTINCT tray_id FROM req_allocations WHERE req_id=? "
        "AND status='reserved'", (rid,))}
    return next((t for t in order if t in rem), None)


def _req_advance(rid):
    """确认一笔后推进：同盘下一笔自动激活；跨盘停在扫盘闸门；全部结束则完成。"""
    r = row("SELECT * FROM requisitions WHERE id=?", (rid,))
    if not r or r["status"] != "active":
        return
    cur = _req_current(rid)
    if cur:
        return
    # 依据最近一次已处理的来源格判断当前盘（撤销也能回到原盘）
    last_done = row(REQ_ALLOC_COLS +
                    "WHERE a.req_id=? AND a.status='done' "
                    "ORDER BY a.id DESC LIMIT 1", (rid,))
    if last_done:
        nxt = row("SELECT id FROM req_allocations WHERE req_id=? "
                  "AND tray_id=? AND status='reserved' ORDER BY seq LIMIT 1",
                  (rid, last_done["tray_id"]))
        if nxt:
            db.execute("UPDATE req_allocations SET status='active' WHERE id=?",
                       (nxt["id"],))
            return
    if _req_expected_tray(rid) is not None:
        return
    left = row("SELECT COUNT(*) AS n FROM req_allocations WHERE req_id=? "
               "AND status IN ('reserved','active')", (rid,))["n"]
    if left == 0:
        db.execute("UPDATE requisitions SET status='done', finished_at=? "
                   "WHERE id=?", (now(), rid))


def _req_remaining(alloc):
    return alloc["qty"] - alloc["taken_qty"]


def _req_candidates(alloc, exclude_cell_id, limit=8):
    """同字盘、同字符同规格的其他来源格（不自动替换异体字 / 异规格）。

    可用量要排除：其他领用单的预留、本单在该格尚未处理的预留
    （改选进来会与之叠加，不能重复拿同一批铅字）。
    """
    out = []
    same_req_reserved = {r["cell_id"]: r["n"] for r in rows(
        "SELECT cell_id, COALESCE(SUM(qty-taken_qty),0) AS n "
        "FROM req_allocations WHERE req_id=? AND status='reserved' "
        "GROUP BY cell_id", (alloc["req_id"],))}
    held = _hold_map(alloc["req_id"])
    for c in rows(
            "SELECT * FROM cells WHERE tray_id=? AND id!=? AND char=? "
            "AND font=? AND size=? ORDER BY qty DESC, y, x, label",
            (alloc["tray_id"], exclude_cell_id, alloc["cell_char"],
             alloc["cell_font"], alloc["cell_size"])):
        other_held = held.get(c["id"], 0)
        same = same_req_reserved.get(c["id"], 0)
        avail = max(0, c["qty"] - other_held - same)
        if avail <= 0:
            continue
        out.append({"id": c["id"], "label": c["label"], "char": c["char"],
                    "font": c["font"], "size": c["size"], "x": c["x"],
                    "y": c["y"], "qty": c["qty"], "capacity": c["capacity"],
                    "tray_id": c["tray_id"], "avail": avail})
        if len(out) >= limit:
            break
    return out


def req_conflict(ctype, message, **kw):
    c = {"type": ctype, "message": message}
    c.update(kw)
    return {"ok": False, "conflict": c, "state": get_state()}


def _req_log_pick(rid, alloc_id, cell_id, tray_id, n):
    db.execute(
        "INSERT INTO req_issues(req_id,allocation_id,kind,cell_id,tray_id,qty,"
        "payload,created_at) VALUES(?,?, 'pick', ?,?,?,?,?)",
        (rid, alloc_id, cell_id, tray_id, n,
         json.dumps({"cell_id": cell_id, "qty": n}, ensure_ascii=False), now()))


@transactional
def plan_requisition(rid, body):
    """规划页：调整换盘顺序并可锁定开始执行（与归还批次同一套扫码闸门规则）。"""
    r = _get_req(rid)
    if r["status"] not in ("planning", "active"):
        raise ApiError("领用单已结束，不能调整")
    current = list(_loads(r["tray_order"]))
    order = body.get("tray_order")
    if order is not None:
        if r["status"] == "active":
            raise ApiError("领用单已锁定执行，不能再调整换盘顺序")
        if not isinstance(order, list) or not all(isinstance(i, int)
                                                  for i in order):
            raise ApiError("换盘顺序格式不正确")
        if set(order) != set(current):
            raise ApiError("换盘顺序必须恰好包含本单涉及的 %d 个字盘"
                           % len(current))
        current = order
    if body.get("lock"):
        if r["status"] == "active":
            raise ApiError("领用单已开始执行")
        involved = {x["tray_id"] for x in rows(
            "SELECT DISTINCT tray_id FROM req_allocations WHERE req_id=? "
            "AND status!='cancelled'", (rid,))}
        if not involved:
            raise ApiError("本单没有可领用的格位分配（全部需求均缺字），不能锁定")
        for tid in involved:
            _assert_no_stocktake(tid, "锁定领用单（该盘正在盘点）")
        bad = _check_tray_codes(involved)
        if bad:
            names = "；".join("「%s」%s" % (x["name"], x["problem"]) for x in bad)
            raise ApiError("以下字盘缺少唯一可扫描的字盘码，无法开始：" + names
                           + "。请到字盘编辑页设置后再锁定。")
        ordered = [t for t in current if t in involved]
        ordered += [t for t in involved if t not in ordered]
        db.execute("UPDATE requisitions SET status='active', tray_order=? "
                   "WHERE id=?", (json.dumps(ordered), rid))
    else:
        db.execute("UPDATE requisitions SET tray_order=? WHERE id=?",
                   (json.dumps(current), rid))
    return {"ok": True, "state": get_state()}


@transactional
def confirm_req_tray(rid, body):
    """领用换盘闸门：每次换盘扫描字盘码核对，通过后才激活该盘首个格位。"""
    r = _get_req(rid)
    if r["status"] != "active":
        raise ApiError("领用单未在执行中")
    expected_id = _req_expected_tray(rid)
    if expected_id is None:
        raise ApiError("没有等待确认的字盘")
    expected = get_tray(expected_id)
    code = str(body.get("scan_code") or "").strip()
    if not code:
        raise ApiError("请扫描字盘码")
    scanned = row("SELECT * FROM trays WHERE scan_code=? AND scan_code!=''",
                  (code,))
    if not scanned:
        return req_conflict("tray_unknown",
                            "无法识别的字盘码「%s」，请重新扫描实体字盘" % code,
                            expected=expected, scanned_code=code)
    if scanned["id"] != expected_id:
        return req_conflict(
            "tray_mismatch",
            "字盘不符：应为「%s」（%s），扫到「%s」（%s）。请暂停核对实物，"
            "确认换到正确字盘后再扫"
            % (expected["name"], expected["scan_code"] or "未设码",
               scanned["name"], scanned["scan_code"]),
            expected=expected, scanned=scanned)
    first = row("SELECT id FROM req_allocations WHERE req_id=? AND tray_id=? "
                "AND status='reserved' ORDER BY seq LIMIT 1",
                (rid, expected_id))
    if first and not _req_current(rid):
        db.execute("UPDATE req_allocations SET status='active' WHERE id=?",
                   (first["id"],))
    return {"ok": True, "state": get_state()}


@transactional
def pick_requisition(rid, body):
    """扫描格号并确认实取数量：全取才扣库存；少取 / 扫错格先出冲突，不扣库存。"""
    r = _get_req(rid)
    if r["status"] != "active":
        raise ApiError("领用单未在执行中，不能扫码取字")
    cur = _req_current(rid)
    if not cur:
        raise ApiError("请先扫描当前字盘的字盘码")
    _assert_no_stocktake(cur["tray_id"], "领用取字（该盘正在盘点）")
    label = str(body.get("label") or "").strip()
    if not label:
        raise ApiError("请扫描格号")
    scanned = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                  "JOIN trays tr ON tr.id=c.tray_id "
                  "WHERE c.tray_id=? AND upper(c.label)=?",
                  (cur["tray_id"], label.upper()))
    if not scanned:
        other = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                    "JOIN trays tr ON tr.id=c.tray_id "
                    "WHERE upper(c.label)=?", (label.upper(),))
        if other:
            cur_tray = get_tray(cur["tray_id"])
            return req_conflict(
                "wrong_tray",
                "扫到的格号「%s」属于字盘「%s」，当前应在「%s」上领用"
                % (other["label"], other["tray_name"], cur_tray["name"]),
                label=label, other_tray=other["tray_name"])
        return req_conflict("unknown_label",
                            "当前字盘无法识别的格号「%s」" % label, label=label)
    if scanned["id"] != cur["cell_id"]:
        same_spec = (scanned["char"] == cur["cell_char"]
                     and scanned["font"] == cur["cell_font"]
                     and scanned["size"] == cur["cell_size"])
        return req_conflict(
            "mismatch",
            "实物不符：扫描到「%s」，当前应取「%s」"
            % (scanned["label"], cur["label"]),
            expected={"id": cur["cell_id"], "label": cur["label"],
                      "char": cur["cell_char"], "font": cur["cell_font"],
                      "size": cur["cell_size"], "qty": cur["cell_qty"]},
            scanned=scanned, same_spec=same_spec,
            candidates=_req_candidates(cur, cur["cell_id"]))
    remain = _req_remaining(cur)
    try:
        n = int(body.get("qty")) if body.get("qty") not in (None, "") else remain
    except (TypeError, ValueError):
        raise ApiError("实取数量不正确")
    n = max(1, min(n, remain))
    held = _hold_map(rid).get(cur["cell_id"], 0)
    avail = max(0, cur["cell_qty"] - held)
    if avail < n:
        return req_conflict(
            "short_avail",
            "格 %s 账面 %d 枚，扣除其他领用单预留后仅剩 %d 枚，不能取 %d 枚"
            % (cur["label"], cur["cell_qty"], avail, n),
            allocation=_alloc_brief(cur), picked=n,
            candidates=_req_candidates(cur, cur["cell_id"]))
    if n < remain:
        return req_conflict(
            "short_pick",
            "少取：本格应取 %d 枚，实取 %d 枚，还差 %d 枚。请改选同盘另一来源格，"
            "或把差额保留为缺口（确认前不扣库存）"
            % (remain, n, remain - n),
            allocation=_alloc_brief(cur), need=remain, picked=n,
            candidates=_req_candidates(cur, cur["cell_id"]))
    db.execute("UPDATE cells SET qty=qty-? WHERE id=?", (n, cur["cell_id"]))
    db.execute("UPDATE req_allocations SET taken_qty=taken_qty+?, status='done' "
               "WHERE id=?", (n, cur["id"]))
    _req_log_pick(rid, cur["id"], cur["cell_id"], cur["tray_id"], n)
    _req_advance(rid)
    return {"ok": True, "state": get_state()}


def _alloc_brief(a):
    return {"id": a["id"], "label": a["label"], "cell_id": a["cell_id"],
            "tray_id": a["tray_id"], "char": a["cell_char"],
            "font": a["cell_font"], "size": a["cell_size"],
            "qty": a["qty"], "taken_qty": a["taken_qty"],
            "remain": a["qty"] - a["taken_qty"]}


@transactional
def change_req_source(rid, body):
    """少取 / 扫错格后改选另一来源格（限同字盘同规格）。

    picked=已从原格实取的枚数（默认 0）：原格先按实取扣库存并结束，
    余量在新格新建一笔当前分配，继续扫码确认；不直接扣新格库存。
    """
    r = _get_req(rid)
    if r["status"] != "active":
        raise ApiError("领用单未在执行中")
    alloc = row(REQ_ALLOC_COLS + "WHERE a.id=?", (body.get("allocation_id"),))
    if not alloc or alloc["req_id"] != rid:
        alloc = _req_current(rid)
    if not alloc:
        raise ApiError("没有进行中的来源格")
    if alloc["status"] != "active":
        raise ApiError("该来源格已处理")
    _assert_no_stocktake(alloc["tray_id"], "改选来源格（该盘正在盘点）")
    new = row("SELECT * FROM cells WHERE id=?", (body.get("cell_id"),))
    if not new:
        raise ApiError("请选择新的来源格")
    if new["tray_id"] != alloc["tray_id"]:
        raise ApiError("不能跨字盘改选来源格；跨盘必须先扫字盘码走换盘闸门")
    if (new["char"], new["font"], new["size"]) != (
            alloc["cell_char"], alloc["cell_font"], alloc["cell_size"]):
        raise ApiError("异体字 / 规格不符的格位不能自动替换，请人工处理")
    remain = _req_remaining(alloc)
    try:
        picked = int(body.get("picked") or 0)
    except (TypeError, ValueError):
        raise ApiError("实取数量不正确")
    picked = max(0, min(picked, remain))
    qnew = remain - picked
    if qnew < 1:
        raise ApiError("差额为 0：无需改选，直接确认实取即可")
    # 目标格可用量排除旧来源格（即将完成）；若该格在本单已有预留（exist），
    # 余量并入后会与之叠加，合并后总量不能超过该格实际可拿量
    exist = row("SELECT * FROM req_allocations WHERE req_id=? AND cell_id=? "
                "AND id!=? AND status='reserved' ORDER BY seq LIMIT 1",
                (rid, new["id"], alloc["id"]))
    # 可用实物 = 当前存量 − 其他领用单预留；本单在该格的其他预留是本单自己
    # 要取的量，先加回，随后按是否并入 exist 统一校验叠加后的总量
    same_req_held = row(
        "SELECT COALESCE(SUM(qty-taken_qty),0) AS n FROM req_allocations "
        "WHERE req_id=? AND cell_id=? AND id!=? AND status='reserved'",
        (rid, new["id"], alloc["id"]))["n"]
    other_held = _held_on_cell(new["id"]) - same_req_held
    avail = max(0, new["qty"] - other_held)
    if exist:
        if exist["qty"] - exist["taken_qty"] + qnew > avail:
            raise ApiError("格 %s 可用数量不足 %d 枚（含本单已预留 %d 枚）"
                           % (new["label"], qnew,
                              exist["qty"] - exist["taken_qty"]))
    elif avail < qnew:
        raise ApiError("格 %s 可用数量不足 %d 枚" % (new["label"], qnew))
    old_cell = row("SELECT * FROM cells WHERE id=?", (alloc["cell_id"],))
    if old_cell["qty"] < picked:
        raise ApiError("原格实际存量仅 %d 枚，实取数量不符" % old_cell["qty"])
    ts = now()
    if picked:
        db.execute("UPDATE cells SET qty=qty-? WHERE id=?",
                   (picked, old_cell["id"]))
    db.execute("UPDATE req_allocations SET taken_qty=taken_qty+?, status='done' "
               "WHERE id=?", (picked, alloc["id"]))
    if exist:
        # 余量并入同规格同格的既有预留：提前激活，路线上只来访一次
        db.execute("UPDATE req_allocations SET qty=qty+?, status='active' "
                   "WHERE id=?", (qnew, exist["id"]))
        new_id = exist["id"]
    else:
        cur2 = db.execute(
            "INSERT INTO req_allocations(req_id,demand_id,tray_id,cell_id,qty,"
            "taken_qty,status,seq,created_at) VALUES(?,?,?,?,?,0, 'active', ?,?)",
            (rid, alloc["demand_id"], alloc["tray_id"], new["id"], qnew,
             alloc["seq"], ts))
        new_id = cur2.lastrowid
    db.execute(
        "INSERT INTO req_issues(req_id,allocation_id,kind,cell_id,tray_id,qty,"
        "payload,created_at) VALUES(?,?, 'change', ?,?,?,?,?)",
        (rid, alloc["id"], new["id"], alloc["tray_id"], qnew,
         json.dumps({"old_allocation_id": alloc["id"],
                     "new_allocation_id": new_id, "old_cell_id": alloc["cell_id"],
                     "new_cell_id": new["id"], "picked": picked, "qty": qnew,
                     "merged": bool(exist)}, ensure_ascii=False), ts))
    return {"ok": True, "new_allocation_id": new_id, "state": get_state()}


@transactional
def keep_req_gap(rid, body):
    """少取后保留缺口：原格按实取扣库存，差额单列 gap 分配，不再占库存。"""
    r = _get_req(rid)
    if r["status"] != "active":
        raise ApiError("领用单未在执行中")
    alloc = row(REQ_ALLOC_COLS + "WHERE a.id=?", (body.get("allocation_id"),))
    if not alloc or alloc["req_id"] != rid:
        alloc = _req_current(rid)
    if not alloc or alloc["status"] != "active":
        raise ApiError("没有进行中的来源格")
    _assert_no_stocktake(alloc["tray_id"], "保留缺口（该盘正在盘点）")
    remain = _req_remaining(alloc)
    try:
        picked = int(body.get("picked") or 0)
    except (TypeError, ValueError):
        raise ApiError("实取数量不正确")
    picked = max(0, min(picked, remain))
    gap_n = remain - picked
    if gap_n < 1:
        raise ApiError("差额为 0：无需保留缺口，直接确认实取即可")
    old_cell = row("SELECT * FROM cells WHERE id=?", (alloc["cell_id"],))
    if old_cell["qty"] < picked:
        raise ApiError("原格实际存量仅 %d 枚，实取数量不符" % old_cell["qty"])
    ts = now()
    if picked:
        db.execute("UPDATE cells SET qty=qty-? WHERE id=?",
                   (picked, old_cell["id"]))
    db.execute("UPDATE req_allocations SET taken_qty=taken_qty+?, status='done' "
               "WHERE id=?", (picked, alloc["id"]))
    cur = db.execute(
        "INSERT INTO req_allocations(req_id,demand_id,tray_id,cell_id,qty,"
        "taken_qty,status,seq,created_at) VALUES(?,?,?,?,?,0, 'gap', ?,?)",
        (rid, alloc["demand_id"], alloc["tray_id"], alloc["cell_id"], gap_n,
         alloc["seq"], ts))
    gap_id = cur.lastrowid
    db.execute(
        "INSERT INTO req_issues(req_id,allocation_id,kind,cell_id,tray_id,qty,"
        "payload,created_at) VALUES(?,?, 'gap', ?,?,?,?,?)",
        (rid, gap_id, alloc["cell_id"], alloc["tray_id"], gap_n,
         json.dumps({"parent_allocation_id": alloc["id"], "picked": picked,
                     "qty": gap_n}, ensure_ascii=False), ts))
    _req_advance(rid)
    return {"ok": True, "state": get_state()}


@transactional
def undo_requisition(rid):
    """撤销上一笔（实取 / 改选 / 缺口），库存与领用单状态一并回退。"""
    r = _get_req(rid)
    if r["status"] == "cancelled":
        raise ApiError("领用单已取消，不能撤销")
    if r["status"] == "done" and row(
            "SELECT id FROM requisitions WHERE status IN ('planning','active') "
            "AND id!=?", (rid,)):
        raise ApiError("已有其他进行中的领用单，请先处理后再撤销")
    last = row("SELECT * FROM req_issues WHERE req_id=? ORDER BY id DESC LIMIT 1",
               (rid,))
    if not last:
        raise ApiError("没有可撤销的操作")
    payload = json.loads(last["payload"] or "{}")
    if last["kind"] == "pick":
        p = payload
        db.execute("UPDATE cells SET qty=qty+? WHERE id=?",
                   (p["qty"], p["cell_id"]))
        a = row("SELECT * FROM req_allocations WHERE id=?", (last["allocation_id"],))
        if a:
            db.execute("UPDATE req_allocations SET status='active', "
                       "taken_qty=max(taken_qty-?,0) WHERE id=?",
                       (p["qty"], a["id"]))
    elif last["kind"] == "gap":
        gap = row("SELECT * FROM req_allocations WHERE id=?",
                  (last["allocation_id"],))
        parent_id = payload.get("parent_allocation_id")
        parent = row("SELECT * FROM req_allocations WHERE id=?", (parent_id,))
        picked = int(payload.get("picked") or 0)
        if gap:
            db.execute("DELETE FROM req_allocations WHERE id=?", (gap["id"],))
        if parent:
            # 恢复原格：状态回到当前、taken 回退、实取部分库存补回
            db.execute("UPDATE req_allocations SET status='active', "
                       "taken_qty=max(taken_qty-?,0) WHERE id=?",
                       (picked, parent["id"]))
            if picked:
                db.execute("UPDATE cells SET qty=qty+? WHERE id=?",
                           (picked, parent["cell_id"]))
    elif last["kind"] == "change":
        new_id = payload.get("new_allocation_id")
        new_a = row("SELECT * FROM req_allocations WHERE id=?", (new_id,))
        parent = row("SELECT * FROM req_allocations WHERE id=?",
                     (payload.get("old_allocation_id"),))
        picked = int(payload.get("picked") or 0)
        if new_a:
            if payload.get("merged"):
                # 余量并入的是既有预留：拆回预留量与状态，不删除该分配
                db.execute("UPDATE req_allocations SET status='reserved', "
                           "qty=max(qty-?,0), taken_qty=0 WHERE id=?",
                           (last["qty"], new_a["id"]))
            else:
                db.execute("DELETE FROM req_allocations WHERE id=?", (new_a["id"],))
        if parent:
            db.execute("UPDATE req_allocations SET status='active', "
                       "taken_qty=max(taken_qty-?,0) WHERE id=?",
                       (picked, parent["id"]))
            if picked:
                db.execute("UPDATE cells SET qty=qty+? WHERE id=?",
                           (picked, parent["cell_id"]))
    db.execute("DELETE FROM req_issues WHERE id=?", (last["id"],))
    db.execute("UPDATE requisitions SET status='active', finished_at=NULL "
               "WHERE id=?", (rid,))
    return {"ok": True, "state": get_state()}


@transactional
def cancel_requisition(rid):
    """取消领用单：未取预留全部释放，已实取数量保留在领用清单里。"""
    r = _get_req(rid)
    if r["status"] not in ("planning", "active"):
        raise ApiError("领用单已结束，不能取消")
    db.execute("UPDATE req_allocations SET status='cancelled' WHERE req_id=? "
               "AND status IN ('reserved','active')", (rid,))
    db.execute("UPDATE requisitions SET status='cancelled', cancelled_at=? "
               "WHERE id=?", (now(), rid))
    return {"ok": True, "state": get_state()}


@transactional
def finish_requisition(rid):
    r = _get_req(rid)
    if r["status"] != "active":
        raise ApiError("领用单未在执行中")
    left = row("SELECT COUNT(*) AS n FROM req_allocations WHERE req_id=? "
               "AND status IN ('reserved','active')", (rid,))["n"]
    if left:
        raise ApiError("还有 %d 个来源格未确认" % left)
    db.execute("UPDATE requisitions SET status='done', finished_at=? WHERE id=?",
               (now(), rid))
    return {"ok": True, "state": get_state()}


@transactional
def requisition_to_return(rid):
    """完成的领用单一键带入现有归还流程：按实取数量生成归还批次（逐格扫码照旧）。"""
    r = _get_req(rid)
    if r["status"] != "done":
        raise ApiError("只有已完成的领用单可以带入归还流程")
    if r["returned_session_id"]:
        raise ApiError("本领用单已生成归还批次（#%d）" % r["returned_session_id"])
    if row("SELECT id FROM sessions WHERE status IN ('planning','active')"):
        raise ApiError("已有进行中的归还批次，请先完成或作废")
    allocs = rows(REQ_ALLOC_COLS + "WHERE a.req_id=? AND a.status='done' "
                  "AND a.taken_qty>0", (rid,))
    if not allocs:
        raise ApiError("本领用单没有实取记录，无需归还")
    merged = {}
    for a in allocs:
        merged.setdefault((a["tray_id"], a["cell_id"]), {
            "tray_id": a["tray_id"], "cell_id": a["cell_id"],
            "char": a["cell_char"], "font": a["cell_font"],
            "size": a["cell_size"], "qty": 0, "x": a["x"], "y": a["y"]})
        merged[(a["tray_id"], a["cell_id"])]["qty"] += a["taken_qty"]
    cur = db.execute(
        "INSERT INTO sessions(name,status,created_at) VALUES(?, 'planning', ?)",
        ("领用单 #%d 归还 · %s" % (rid, r["name"]), now()))
    sid = cur.lastrowid
    tray_order = []
    for tr in rows("SELECT * FROM trays ORDER BY sort_order, id"):
        group = [v for v in merged.values() if v["tray_id"] == tr["id"]]
        if not group:
            continue
        tray_order.append(tr["id"])
        for i, t in enumerate(plan_order(group)):
            db.execute(
                "INSERT INTO tasks(session_id,tray_id,cell_id,char,font,size,"
                "qty,seq,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, tr["id"], t["cell_id"], t["char"], t["font"], t["size"],
                 t["qty"], i + 1, "领用单 #%d 带入" % rid))
    db.execute("UPDATE sessions SET tray_order=?, start_tray_id=? WHERE id=?",
               (json.dumps(tray_order),
                tray_order[0] if tray_order else None, sid))
    db.execute("UPDATE requisitions SET returned_session_id=? WHERE id=?",
               (sid, rid))
    return {"ok": True, "session_id": sid, "state": get_state()}


def get_requisition_detail(rid):
    return {"ok": True, "requisition": _load_requisition(_get_req(rid))}


# ---------------------------------------------------------------- 盘点 API


def _stock_brief(st):
    """盘点单概要：进度与所属字盘信息。"""
    d = dict(st)
    tr = row("SELECT name, scan_code FROM trays WHERE id=?", (st["tray_id"],))
    d["tray_name"] = tr["name"] if tr else "（已删除字盘）"
    d["tray_code"] = tr["scan_code"] if tr else ""
    n = row("SELECT COUNT(*) AS total,"
             "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
             "SUM(CASE WHEN status NOT IN ('pending','counted') THEN 1 ELSE 0 END)"
             " AS flagged FROM stocktake_cells WHERE stocktake_id=?",
             (st["id"],))
    d["total"] = n["total"] or 0
    d["n_pending"] = n["pending"] or 0
    d["n_flagged"] = n["flagged"] or 0
    d["n_counted"] = d["total"] - d["n_pending"]
    return d


def _find_tray_by_code(code):
    code = (code or "").strip()
    if not code:
        return None
    return row("SELECT * FROM trays WHERE upper(scan_code)=? AND scan_code<>''",
               (code.upper(),))


@transactional
def start_stocktake(body):
    """扫描字盘码开始盘点：锁定该盘，按蛇形次序生成全部格位快照。"""
    code = str(body.get("scan_code") or "").strip()
    tr = _find_tray_by_code(code)
    if not tr:
        raise ApiError("无法识别的字盘码「%s」，请扫描实体字盘上的字盘码" % code)
    if _active_stocktake(tr["id"]):
        raise ApiError("字盘「%s」已有进行中的盘点单，请先完成或取消" % tr["name"])
    cells = rows("SELECT * FROM cells WHERE tray_id=? ORDER BY y, x", (tr["id"],))
    if not cells:
        raise ApiError("字盘「%s」还没有格位，无法盘点" % tr["name"])
    ts = now()
    cur = db.execute(
        "INSERT INTO stocktakes(tray_id,status,created_at,started_at,note) "
        "VALUES(?, 'counting', ?, ?, ?)",
        (tr["id"], ts, ts, "盘点单 " + ts))
    sid = cur.lastrowid
    ordered = plan_order([{"x": c["x"], "y": c["y"], "id": c["id"]} for c in cells])
    by_id = {c["id"]: c for c in cells}
    for i, it in enumerate(ordered):
        c = by_id[it["id"]]
        db.execute(
            "INSERT INTO stocktake_cells(stocktake_id,cell_id,seq,book_label,"
            "book_char,book_font,book_size,book_qty) VALUES(?,?,?,?,?,?,?,?)",
            (sid, c["id"], i + 1, c["label"], c["char"], c["font"], c["size"],
             c["qty"]))
    return {"ok": True, "stocktake_id": sid, "state": get_state()}


@transactional
def cancel_stocktake(sid):
    st = _get_stocktake_row(sid)
    if st["status"] not in ST_ACTIVE:
        raise ApiError("该盘点单已结束，不能取消")
    db.execute("UPDATE stocktakes SET status='cancelled', cancelled_at=? WHERE id=?",
               (now(), sid))
    # 未执行 / 执行一半的移格任务随单一并取消，决议保留在记录里但不再生效
    db.execute("UPDATE move_tasks SET status='cancelled' WHERE stocktake_id=? "
               "AND status IN ('pending','src_ok')", (sid,))
    return {"ok": True, "state": get_state()}


def _get_stocktake_row(sid):
    st = row("SELECT * FROM stocktakes WHERE id=?", (sid,))
    if not st:
        raise ApiError("盘点单不存在", 404)
    return st


def _current_count_cell(sid):
    return row("SELECT * FROM stocktake_cells WHERE stocktake_id=? "
               "AND status='pending' ORDER BY seq LIMIT 1", (sid,))


def stock_conflict(ctype, message, **kw):
    return {"ok": False, "conflict": dict({"type": ctype, "message": message},
                                          **kw), "state": get_state()}


@transactional
def count_stocktake_cell(sid, body):
    """扫描当前高亮格号并录入实数 / 待复核标记，录完自动进入差异复核。"""
    st = _get_stocktake_row(sid)
    if st["status"] != "counting":
        raise ApiError("该盘点单不在盘点中（当前：%s）"
                       % STOCK_STATUS_NAMES.get(st["status"], st["status"]))
    cur = _current_count_cell(sid)
    if not cur:
        raise ApiError("没有待盘格位")
    label = str(body.get("label") or "").strip()
    if not label:
        raise ApiError("请扫描当前高亮格位的格号")
    matched = row("SELECT c.* FROM stocktake_cells sc JOIN cells c "
                  "ON c.id=sc.cell_id "
                  "WHERE sc.id=? AND upper(c.label)=?", (cur["id"], label.upper()))
    if not matched:
        # 格号不属于本字盘，还是本盘别的格？
        other = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                    "JOIN trays tr ON tr.id=c.tray_id "
                    "WHERE upper(c.label)=?", (label.upper(),))
        if other:
            cur_cell = row("SELECT c.* FROM cells c WHERE id=?", (cur["cell_id"],))
            if other["tray_id"] == st["tray_id"]:
                return stock_conflict(
                    "count_mismatch",
                    "盘内扫错格：当前应盘「%s」，扫到「%s」，请按高亮顺序重新扫描"
                    % (cur_cell["label"], other["label"]),
                    expected=cur_cell, scanned=other)
            return stock_conflict(
                "count_wrong_tray",
                "扫到的格号「%s」属于字盘「%s」，请在当前盘点字盘上操作"
                % (other["label"], other["tray_name"]),
                expected=cur_cell, scanned=other)
        return stock_conflict("count_unknown",
                              "无法识别的格号「%s」" % label, label=label,
                              expected=row("SELECT * FROM cells WHERE id=?",
                                           (cur["cell_id"],)))
    flag = str(body.get("flag") or "").strip()
    if flag:
        if flag not in ("mixed", "unrecognized", "blocked"):
            raise ApiError("待复核标记不正确")
        aq = None
        if flag != "blocked":
            q = body.get("qty")
            if q not in (None, ""):
                try:
                    aq = max(0, int(q))
                except (TypeError, ValueError):
                    raise ApiError("数量不正确")
        db.execute("UPDATE stocktake_cells SET status=?, actual_qty=?, "
                   "actual_char=NULL, actual_font=NULL, actual_size=NULL, "
                   "counted_at=? WHERE id=?",
                   (flag, aq, now(), cur["id"]))
    else:
        try:
            aq = int(body.get("qty"))
        except (TypeError, ValueError):
            raise ApiError("请填写实物点数（非负整数）")
        if aq < 0:
            raise ApiError("实物点数不能为负")
        # 实物规格：字符 / 字体 / 字号三项相互独立，逐项处理——
        # 每项各自“留空取账面、填写即覆盖”，因此只改字体或只改字号
        # （字符留空）也会形成规格不符，进入差异复核。
        def actual_field(key, book):
            v = body.get(key)
            if v is None:
                return book
            v = str(v).strip()
            return v if v else book

        achar = actual_field("actual_char", cur["book_char"])
        afont = actual_field("actual_font", cur["book_font"])
        asize = actual_field("actual_size", cur["book_size"])
        db.execute("UPDATE stocktake_cells SET status='counted', actual_qty=?, "
                   "actual_char=?, actual_font=?, actual_size=?, counted_at=? "
                   "WHERE id=?",
                   (aq, achar, afont, asize, now(), cur["id"]))
    if not _current_count_cell(sid):
        db.execute("UPDATE stocktakes SET status='reviewing', finished_at=? WHERE id=?",
                   (now(), sid))
        _build_discrepancies(sid)
    return {"ok": True, "state": get_state()}


@transactional
def undo_count(sid):
    """撤销最近录入的一格（刷新续盘时同样从下一待盘格继续）。

    全盘录完进入差异复核后，撤销会同时清空已生成的差异、把盘点单退回盘点中。
    """
    st = _get_stocktake_row(sid)
    if st["status"] not in ("counting", "reviewing"):
        raise ApiError("盘点单已结束，不能撤销")
    # 复核阶段已有盘盈 / 盘亏决议或移格任务时，先撤销 / 取消，避免差异悬挂
    if st["status"] == "reviewing":
        decided = row("SELECT COUNT(*) AS n FROM discrepancies "
                      "WHERE stocktake_id=? AND decision!=''", (sid,))["n"]
        tasked = row("SELECT COUNT(*) AS n FROM move_tasks WHERE stocktake_id=? "
                     "AND status IN ('pending','src_ok','done')", (sid,))["n"]
        if decided or tasked:
            raise ApiError("已有盘盈 / 盘亏决议或移格任务，请先撤销后再修改盘点")
        db.execute("DELETE FROM discrepancy_pairs WHERE stocktake_id=?", (sid,))
        db.execute("DELETE FROM discrepancies WHERE stocktake_id=?", (sid,))
    last = row("SELECT * FROM stocktake_cells WHERE stocktake_id=? "
               "AND status!='pending' ORDER BY id DESC LIMIT 1", (sid,))
    if not last:
        raise ApiError("还没有已录入的格位")
    db.execute("UPDATE stocktake_cells SET status='pending', actual_qty=NULL, "
               "actual_char=NULL, actual_font=NULL, actual_size=NULL, "
               "counted_at=NULL WHERE id=?", (last["id"],))
    db.execute("UPDATE stocktakes SET status='counting', finished_at=NULL "
               "WHERE id=?", (sid,))
    return {"ok": True, "state": get_state()}


@transactional
def recheck_cell(sid, body):
    """差异复核阶段把格位拉回复盘。

    待复核格（混字 / 无法辨认 / 暂不能盘）与已盘但录错（含规格录错）的格
    都可拉回；若该格的差异已有决议、或已据此生成移格任务，则需先撤销。
    """
    st = _get_stocktake_row(sid)
    if st["status"] != "reviewing":
        raise ApiError("只有差异复核阶段可以复盘格位")
    sc = row("SELECT * FROM stocktake_cells WHERE stocktake_id=? AND cell_id=?",
             (sid, body.get("cell_id")))
    if not sc:
        raise ApiError("该格不属于本盘点单")
    if sc["status"] == "pending":
        raise ApiError("该格尚未盘点，无需复盘")
    is_review = sc["status"] in ("mixed", "unrecognized", "blocked")
    if is_review:
        # 待复核格的复盘会重建整盘差异：任何未入账决议 / 移格都必须先撤销
        decided = row("SELECT COUNT(*) AS n FROM discrepancies "
                      "WHERE stocktake_id=? AND decision!=''", (sid,))["n"]
        tasked = row(
            "SELECT COUNT(*) AS n FROM move_tasks WHERE stocktake_id=? AND "
            "status IN ('pending','src_ok','done')", (sid,))["n"]
        if decided or tasked:
            raise ApiError("已有盘盈 / 盘亏决议或移格任务，请先撤销后再复盘该格")
    else:
        # 普通已盘格：只拦该格自身差异上的决议、以及涉及该格的移格任务
        disc_ids = [r["id"] for r in rows(
            "SELECT id FROM discrepancies WHERE stocktake_id=? AND cell_id=?",
            (sid, sc["cell_id"]))]
        decided = 0
        if disc_ids:
            ph = ",".join("?" * len(disc_ids))
            decided = row(
                "SELECT COUNT(*) AS n FROM discrepancies WHERE id IN (%s) "
                "AND decision!=''" % ph, tuple(disc_ids))["n"]
        tasked = row(
            "SELECT COUNT(*) AS n FROM move_tasks WHERE stocktake_id=? AND "
            "status IN ('pending','src_ok','done') AND "
            "(source_cell_id=? OR target_cell_id=?)",
            (sid, sc["cell_id"], sc["cell_id"]))["n"]
        if decided or tasked:
            raise ApiError("该格已有盘盈 / 盘亏决议或移格任务，请先撤销后再复盘")
    db.execute("UPDATE stocktake_cells SET status='pending', actual_qty=NULL, "
               "actual_char=NULL, actual_font=NULL, actual_size=NULL, "
               "counted_at=NULL WHERE id=?", (sc["id"],))
    db.execute("DELETE FROM discrepancy_pairs WHERE stocktake_id=?", (sid,))
    db.execute("DELETE FROM discrepancies WHERE stocktake_id=?", (sid,))
    db.execute("UPDATE stocktakes SET status='counting', finished_at=NULL "
               "WHERE id=?", (sid,))
    return {"ok": True, "state": get_state()}


# ---------------------------------------------------------------- 差异构建


def _build_discrepancies(sid):
    """全盘录完后按格生成短缺 / 溢出 / 待复核；同规格跨格相反差额配成错放候选。

    规格不符的格：账面规格按账面数量短缺、实物规格按实物数量溢出，
    之后由配对阶段在不同格位间寻找相反差额。
    """
    db.execute("DELETE FROM discrepancy_pairs WHERE stocktake_id=?", (sid,))
    db.execute("DELETE FROM discrepancies WHERE stocktake_id=?", (sid,))
    scs = rows("SELECT * FROM stocktake_cells WHERE stocktake_id=? ORDER BY seq",
               (sid,))
    facts = []  # 可配对的差额
    for sc in scs:
        if sc["status"] in ("mixed", "unrecognized", "blocked"):
            db.execute(
                "INSERT INTO discrepancies(stocktake_id,cell_id,kind,review_kind,"
                "spec_char,spec_font,spec_size,qty,created_at) "
                "VALUES(?,?, 'review', ?, '', '', '', 0, ?)",
                (sid, sc["cell_id"], sc["status"], now()))
        if sc["status"] != "counted" or sc["actual_qty"] is None:
            continue
        aq, bq = sc["actual_qty"], sc["book_qty"]
        same = (sc["actual_char"] == sc["book_char"]
                and sc["actual_font"] == sc["book_font"]
                and sc["actual_size"] == sc["book_size"])
        if same:
            if aq < bq:
                facts.append((sc, "short", sc["book_char"], sc["book_font"],
                              sc["book_size"], bq - aq))
            elif aq > bq:
                facts.append((sc, "surplus", sc["book_char"], sc["book_font"],
                              sc["book_size"], aq - bq))
        else:
            if bq:
                facts.append((sc, "short", sc["book_char"], sc["book_font"],
                              sc["book_size"], bq))
            if aq:
                facts.append((sc, "surplus", sc["actual_char"], sc["actual_font"],
                              sc["actual_size"], aq))
    discs = {}  # (cell_id, side, spec) -> disc row dict
    for sc, kind, ch, font, size, qty in facts:
        cur = db.execute(
            "INSERT INTO discrepancies(stocktake_id,cell_id,kind,spec_char,"
            "spec_font,spec_size,qty,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, sc["cell_id"], kind, ch, font, size, qty, now()))
        discs[(sc["cell_id"], kind, ch, font, size)] = {
            "id": cur.lastrowid, "cell_id": sc["cell_id"], "kind": kind,
            "spec": (ch, font, size), "qty": qty, "left": qty}
    # 同规格、不同格位的溢出与短缺贪心配对（支持部分配对），只出错放候选
    buckets = {}
    for d in discs.values():
        buckets.setdefault(d["spec"], {"surplus": [], "short": []})
        buckets[d["spec"]][d["kind"]].append(d)
    n = 0
    for spec in sorted(buckets, key=lambda k: (k[0], k[1], k[2])):
        for s in buckets[spec]["surplus"]:
            for h in buckets[spec]["short"]:
                if s["left"] <= 0:
                    break
                if h["cell_id"] == s["cell_id"] or h["left"] <= 0:
                    continue
                q = min(s["left"], h["left"])
                n += 1
                db.execute(
                    "INSERT INTO discrepancy_pairs(stocktake_id,code,spec_char,"
                    "spec_font,spec_size,src_disc_id,dst_disc_id,qty,status,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?, 'candidate', ?)",
                    (sid, "P%d" % n, spec[0], spec[1], spec[2], s["id"], h["id"],
                     q, now()))
                s["left"] -= q
                h["left"] -= q


# ---------------------------------------------------------------- 盘点状态组装


def _load_stocktake(st):
    d = _stock_brief(st)
    sid = st["id"]
    d["cells"] = rows(
        "SELECT sc.*, c.label AS cell_label FROM stocktake_cells sc "
        "JOIN cells c ON c.id=sc.cell_id WHERE sc.stocktake_id=? ORDER BY sc.seq",
        (sid,))
    d["discrepancies"] = rows(
        "SELECT * FROM discrepancies WHERE stocktake_id=? ORDER BY id", (sid,))
    pairs = rows(
        "SELECT p.*, sl.label AS src_label, dl.label AS dst_label "
        "FROM discrepancy_pairs p "
        "JOIN discrepancies sd ON sd.id=p.src_disc_id "
        "JOIN discrepancies dd ON dd.id=p.dst_disc_id "
        "JOIN cells sl ON sl.id=sd.cell_id "
        "JOIN cells dl ON dl.id=dd.cell_id "
        "WHERE p.stocktake_id=? ORDER BY p.id", (sid,))
    d["pairs"] = pairs
    d["move_tasks"] = rows(
        "SELECT m.*, sl.label AS source_label, dl.label AS target_label "
        "FROM move_tasks m "
        "JOIN cells sl ON sl.id=m.source_cell_id "
        "JOIN cells dl ON dl.id=m.target_cell_id "
        "WHERE m.stocktake_id=? ORDER BY m.id", (sid,))
    d["postings"] = rows(
        "SELECT * FROM postings WHERE stocktake_id=? ORDER BY id", (sid,))
    if st["status"] == "reviewing":
        d["preview"] = _stock_preview(d)
    else:
        d["preview"] = None
    return d


def _stock_preview(stk):
    """差异复核预览：配对覆盖、剩余决议、移格影响、入账前后值与可入账性。"""
    sid = stk["id"]
    cells = {sc["cell_id"]: sc for sc in stk["cells"]}
    discs = {d["id"]: d for d in stk["discrepancies"]}
    covered = {d["id"]: 0 for d in stk["discrepancies"]}
    errors, warnings = [], []
    done_move_ids = set()
    for m in stk["move_tasks"]:
        if m["status"] in ("pending", "src_ok"):
            errors.append("移格任务 #%d（%s → %s）尚未双端复扫完成"
                          % (m["id"], m["source_label"], m["target_label"]))
        elif m["status"] == "done":
            done_move_ids.add(m["id"])
    for p in stk["pairs"]:
        if p["status"] in ("moved", "writeoff", "posted"):
            covered[p["src_disc_id"]] += p["qty"]
            covered[p["dst_disc_id"]] += p["qty"]
        if p["status"] == "candidate":
            errors.append("错放候选 %s（%s「%s」%s → %s）尚未生成移格任务或核销"
                          % (p["code"], p["src_label"], p["spec_char"],
                             p["spec_size"] or "", p["dst_label"]))
    for d in stk["discrepancies"]:
        if d["kind"] == "review":
            rname = REVIEW_NAMES.get(d["review_kind"], d["review_kind"])
            sc = cells.get(d["cell_id"])
            errors.append("格 %s 待复核（%s），请复盘该格后再入账"
                          % (sc["cell_label"] if sc else "?", rname))
            continue
        residual = d["qty"] - covered[d["id"]]
        if residual > 0 and not d["decision"]:
            sc = cells.get(d["cell_id"])
            errors.append("格 %s 的%s %d 枚尚未认定盘盈 / 盘亏"
                          % (sc["cell_label"] if sc else "?",
                             DISC_KIND_NAMES[d["kind"]], residual))
    # 模拟入账：先对齐账面→实数，再执行已完成移格
    move_out, move_in = {cid: 0 for cid in cells}, {cid: 0 for cid in cells}
    for m in stk["move_tasks"]:
        if m["id"] not in done_move_ids:
            continue
        move_out[m["source_cell_id"]] += m["qty"]
        move_in[m["target_cell_id"]] += m["qty"]
    cur_cells = {c["id"]: c for c in
                 rows("SELECT * FROM cells WHERE tray_id=?", (stk["tray_id"],))}
    changes = []
    for cid, sc in cells.items():
        if sc["status"] != "counted" or sc["actual_qty"] is None:
            continue
        fq = sc["actual_qty"] + move_in.get(cid, 0) - move_out.get(cid, 0)
        if fq < 0:
            errors.append("格 %s 执行移格后存量为 %d，不能入账，请核对移格数量"
                          % (sc["cell_label"], fq))
        same = (sc["actual_char"] == sc["book_char"]
                and sc["actual_font"] == sc["book_font"]
                and sc["actual_size"] == sc["book_size"])
        fch, ffont, fsize = sc["book_char"], sc["book_font"], sc["book_size"]
        if not same:
            # 实物整格移出、账面规格整格移回：账面规格恢复；否则按实物规格改正
            if move_out.get(cid, 0) >= sc["actual_qty"] and move_in.get(cid, 0) > 0:
                fch, ffont, fsize = sc["book_char"], sc["book_font"], sc["book_size"]
            else:
                fch, ffont, fsize = sc["actual_char"], sc["actual_font"], \
                    sc["actual_size"]
        spec_changed = (fch, ffont, fsize) != (
            cur_cells.get(cid, sc)["char"], cur_cells.get(cid, sc)["font"],
            cur_cells.get(cid, sc)["size"])
        before = cur_cells.get(cid, sc)["qty"]
        if before != fq or not same or move_in.get(cid) or move_out.get(cid):
            changes.append({
                "cell_id": cid, "label": sc["cell_label"],
                "book_char": sc["book_char"], "book_font": sc["book_font"],
                "book_size": sc["book_size"], "book_qty": sc["book_qty"],
                "actual_char": sc["actual_char"], "actual_font": sc["actual_font"],
                "actual_size": sc["actual_size"], "actual_qty": sc["actual_qty"],
                "before_qty": before, "final_qty": fq,
                "final_char": fch, "final_font": ffont, "final_size": fsize,
                "spec_changed": spec_changed,
                "move_out": move_out.get(cid, 0), "move_in": move_in.get(cid, 0)})
    return {"ready": not errors, "errors": errors, "warnings": warnings,
            "changes": changes,
            "n_short": sum(1 for d in stk["discrepancies"]
                           if d["kind"] == "short"),
            "n_surplus": sum(1 for d in stk["discrepancies"]
                             if d["kind"] == "surplus"),
            "n_review": sum(1 for d in stk["discrepancies"]
                            if d["kind"] == "review")}


@transactional
def get_stocktake_detail(sid):
    return {"ok": True, "stocktake": _load_stocktake(_get_stocktake_row(sid))}


# ---------------------------------------------------------------- 差异决议


def _reviewing(sid):
    st = _get_stocktake_row(sid)
    if st["status"] != "reviewing":
        raise ApiError("只有差异复核阶段可以处理差异（当前：%s）"
                       % STOCK_STATUS_NAMES.get(st["status"], st["status"]))
    return st


@transactional
def decide_discrepancy(did, body):
    """把差异剩余部分（错放配对之外）认定为盘盈或盘亏；入账前可撤销。"""
    d = row("SELECT * FROM discrepancies WHERE id=?", (did,))
    if not d:
        raise ApiError("差异不存在", 404)
    _reviewing(d["stocktake_id"])
    decision = str(body.get("decision") or "").strip()
    if decision not in ("gain", "loss"):
        raise ApiError("决议类型应为 gain（盘盈）或 loss（盘亏）")
    if d["kind"] == "review":
        raise ApiError("待复核格位不能直接认定盘盈 / 盘亏，请先复盘该格")
    if decision == "gain" and d["kind"] != "surplus":
        raise ApiError("短缺项只能认定为盘亏")
    if decision == "loss" and d["kind"] != "short":
        raise ApiError("溢出项只能认定为盘盈")
    covered = row("SELECT COALESCE(SUM(qty),0) AS n FROM discrepancy_pairs "
                  "WHERE (src_disc_id=? OR dst_disc_id=?) AND status!= 'candidate'",
                  (did, did))["n"]
    if d["qty"] - covered <= 0:
        raise ApiError("该差额已全部由错放移格 / 核销处理，无需再认定")
    db.execute("UPDATE discrepancies SET decision=? WHERE id=?", (decision, did))
    return {"ok": True, "state": get_state()}


@transactional
def revoke_discrepancy(did):
    d = row("SELECT * FROM discrepancies WHERE id=?", (did,))
    if not d:
        raise ApiError("差异不存在", 404)
    _reviewing(d["stocktake_id"])
    if not d["decision"]:
        raise ApiError("该差异还没有决议")
    db.execute("UPDATE discrepancies SET decision='' WHERE id=?", (did,))
    return {"ok": True, "state": get_state()}


@transactional
def create_move_task(pid, body):
    """错放候选生成移格任务：来源格、目标格与数量固定，执行时依次复扫两端。"""
    p = row("SELECT * FROM discrepancy_pairs WHERE id=?", (pid,))
    if not p:
        raise ApiError("错放候选不存在", 404)
    _reviewing(p["stocktake_id"])
    if p["status"] != "candidate":
        raise ApiError("该候选已生成移格任务或已核销")
    sid = p["stocktake_id"]
    src = row("SELECT cell_id FROM discrepancies WHERE id=?", (p["src_disc_id"],))
    dst = row("SELECT cell_id FROM discrepancies WHERE id=?", (p["dst_disc_id"],))
    cur = db.execute(
        "INSERT INTO move_tasks(stocktake_id,source_cell_id,target_cell_id,"
        "spec_char,spec_font,spec_size,qty,status,created_at) "
        "VALUES(?,?,?,?,?,?,?, 'pending', ?)",
        (sid, src["cell_id"], dst["cell_id"], p["spec_char"], p["spec_font"],
         p["spec_size"], p["qty"], now()))
    mid = cur.lastrowid
    db.execute("UPDATE discrepancy_pairs SET status='tasked', move_task_id=? "
               "WHERE id=?", (mid, pid))
    return {"ok": True, "move_task_id": mid, "state": get_state()}


@transactional
def writeoff_pair(pid):
    """不安排移格：错放候选两端差额分别按盘盈（来源）、盘亏（目标）入账。"""
    p = row("SELECT * FROM discrepancy_pairs WHERE id=?", (pid,))
    if not p:
        raise ApiError("错放候选不存在", 404)
    _reviewing(p["stocktake_id"])
    if p["status"] != "candidate":
        raise ApiError("该候选已处理（已生成任务请先取消任务）")
    db.execute("UPDATE discrepancy_pairs SET status='writeoff' WHERE id=?", (pid,))
    return {"ok": True, "state": get_state()}


@transactional
def revoke_pair(pid):
    """撤销候选上的移格任务 / 核销，恢复为待处理候选（未入账才可撤销）。"""
    p = row("SELECT * FROM discrepancy_pairs WHERE id=?", (pid,))
    if not p:
        raise ApiError("错放候选不存在", 404)
    _reviewing(p["stocktake_id"])
    if p["status"] == "candidate":
        raise ApiError("该候选还未处理")
    if p["move_task_id"]:
        # 未入账的移格记录（含已双端复扫）一并取消，候选恢复待处理
        db.execute("UPDATE move_tasks SET status='cancelled' WHERE id=? AND status "
                   "IN ('pending','src_ok','done')", (p["move_task_id"],))
    db.execute("UPDATE discrepancy_pairs SET status='candidate', "
               "move_task_id=NULL WHERE id=?", (pid,))
    return {"ok": True, "state": get_state()}


@transactional
def cancel_move_task(mid):
    m = row("SELECT * FROM move_tasks WHERE id=?", (mid,))
    if not m:
        raise ApiError("移格任务不存在", 404)
    _reviewing(m["stocktake_id"])
    if m["status"] not in ("pending", "src_ok", "done"):
        raise ApiError("当前状态不能取消（已取消或已入账）")
    db.execute("UPDATE move_tasks SET status='cancelled' WHERE id=?", (mid,))
    db.execute("UPDATE discrepancy_pairs SET status='candidate', move_task_id=NULL "
               "WHERE move_task_id=?", (mid,))
    return {"ok": True, "state": get_state()}


@transactional
def scan_move_task(mid, body):
    """依次复扫来源格、目标格：顺序错误 / 格号错误立即拦截，不自动改库存。"""
    m = row("SELECT * FROM move_tasks WHERE id=?", (mid,))
    if not m:
        raise ApiError("移格任务不存在", 404)
    _reviewing(m["stocktake_id"])
    if m["status"] not in ("pending", "src_ok"):
        raise ApiError("移格任务当前状态（%s）不能复扫"
                       % m["status"])
    label = str(body.get("label") or "").strip()
    if not label:
        raise ApiError("请扫描格号")
    tray_id = row("SELECT tray_id FROM stocktakes WHERE id=?",
                  (m["stocktake_id"],))["tray_id"]
    stage = "source" if m["status"] == "pending" else "target"
    expect_id = m["source_cell_id"] if stage == "source" else m["target_cell_id"]
    expect = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                 "JOIN trays tr ON tr.id=c.tray_id WHERE c.id=?", (expect_id,))
    scanned = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                  "JOIN trays tr ON tr.id=c.tray_id "
                  "WHERE c.tray_id=? AND upper(c.label)=?",
                  (tray_id, label.upper()))
    if not scanned:
        other = row("SELECT c.*, tr.name AS tray_name FROM cells c "
                    "JOIN trays tr ON tr.id=c.tray_id WHERE upper(c.label)=?",
                    (label.upper(),))
        if other:
            return stock_conflict(
                "move_wrong_tray",
                "扫到的格号「%s」属于字盘「%s」，移格只在本盘点字盘内进行"
                % (other["label"], other["tray_name"]),
                stage=stage, expected=expect, scanned=other)
        return stock_conflict("move_unknown",
                              "无法识别的格号「%s」" % label, stage=stage,
                              expected=expect, label=label)
    if scanned["id"] != expect_id:
        return stock_conflict(
            "move_wrong_cell",
            ("请先复扫来源格" if stage == "source" else "来源格已确认，请复扫目标格")
            + "：应为「%s」，扫到「%s」" % (expect["label"], scanned["label"]),
            stage=stage, expected=expect, scanned=scanned, move=m)
    if stage == "source":
        db.execute("UPDATE move_tasks SET status='src_ok', src_scan=? WHERE id=?",
                   (label, mid))
    else:
        db.execute("UPDATE move_tasks SET status='done', tgt_scan=?, done_at=? "
                   "WHERE id=?", (label, now(), mid))
        db.execute("UPDATE discrepancy_pairs SET status='moved' "
                   "WHERE move_task_id=?", (mid,))
    return {"ok": True, "stage_done": stage, "state": get_state()}


# ---------------------------------------------------------------- 预览与入账


@transactional
def post_stocktake(sid):
    """全部差异处理后一次性入账：先对齐实数，再执行移格，逐格保留前后值。"""
    st = _reviewing(sid)
    detail = _load_stocktake(st)
    pv = detail["preview"]
    if not pv or not pv["ready"]:
        raise ApiError("尚有差异未处理，不能入账：" + "；".join(pv["errors"][:5]))
    scs = {sc["cell_id"]: sc for sc in detail["cells"]}
    done_moves = [mv for mv in detail["move_tasks"]
                  if mv["status"] == "done"]
    ts = now()
    # 第一阶段：账面 → 实物（盘盈 / 盘亏 / 规格订正）
    for cid, sc in scs.items():
        if sc["status"] != "counted" or sc["actual_qty"] is None:
            continue
        target = row("SELECT * FROM cells WHERE id=?", (int(cid),))
        before = target["qty"]
        same_spec = (sc["actual_char"] == sc["book_char"]
                     and sc["actual_font"] == sc["book_font"]
                     and sc["actual_size"] == sc["book_size"])
        # 同一格的对齐只记一条：规格不符记 spec（即便数量同时变化），
        # 否则按数量增减记 gain / loss，数量与规格都不变则不留账
        kind = ("spec" if not same_spec else
                "gain" if sc["actual_qty"] > before else
                "loss" if sc["actual_qty"] < before else "")
        db.execute("UPDATE cells SET qty=?, char=?, font=?, size=? WHERE id=?",
                   (int(sc["actual_qty"]), sc["actual_char"],
                    sc["actual_font"], sc["actual_size"], int(cid)))
        if kind:
            db.execute(
                "INSERT INTO postings(stocktake_id,cell_id,kind,qty,"
                "before_qty,after_qty,created_at) VALUES(?,?,?,?,?,?,?)",
                (sid, int(cid), kind,
                 abs(int(sc["actual_qty"]) - int(before)), int(before),
                 int(sc["actual_qty"]), ts))
    # 第二阶段：执行已双端复扫的移格任务
    for mv in done_moves:
        src_id, tgt_id, q = (int(mv["source_cell_id"]),
                             int(mv["target_cell_id"]), int(mv["qty"]))
        src = row("SELECT * FROM cells WHERE id=?", (src_id,))
        db.execute("UPDATE cells SET qty=qty-? WHERE id=?", (q, src_id))
        after = int(src["qty"]) - q
        db.execute(
            "INSERT INTO postings(stocktake_id,cell_id,move_task_id,kind,qty,"
            "before_qty,after_qty,created_at) "
            "VALUES(?,?,?, 'move_out',?,?,?,?)",
            (sid, src_id, int(mv["id"]), q, int(src["qty"]), after, ts))
        tgt = row("SELECT * FROM cells WHERE id=?", (tgt_id,))
        db.execute("UPDATE cells SET qty=qty+? WHERE id=?", (q, tgt_id))
        after_t = int(tgt["qty"]) + q
        db.execute(
            "INSERT INTO postings(stocktake_id,cell_id,move_task_id,kind,qty,"
            "before_qty,after_qty,created_at) "
            "VALUES(?,?,?, 'move_in',?,?,?,?)",
            (sid, tgt_id, int(mv["id"]), q, int(tgt["qty"]), after_t, ts))
    # 规格恢复 / 订正（以预览计算的最终规格为准）
    for ch in pv["changes"]:
        db.execute("UPDATE cells SET char=?, font=?, size=? WHERE id=?",
                   (ch["final_char"], ch["final_font"], ch["final_size"],
                    int(ch["cell_id"])))
    db.execute("UPDATE discrepancies SET posted=1 WHERE stocktake_id=?", (sid,))
    db.execute("UPDATE discrepancy_pairs SET status='posted' WHERE stocktake_id=?",
               (sid,))
    db.execute("UPDATE move_tasks SET status='posted' WHERE stocktake_id=? "
               "AND status='done'", (sid,))
    db.execute("UPDATE stocktakes SET status='posted', posted_at=? WHERE id=?",
               (ts, sid))
    return {"ok": True, "state": get_state()}


# ---------------------------------------------------------------- 备份恢复

TABLES = ["trays", "cells", "sessions", "tasks", "pending_items", "actions",
          "stocktakes", "stocktake_cells", "discrepancies",
          "discrepancy_pairs", "move_tasks", "postings",
          "requisitions", "req_demands", "req_allocations", "req_issues"]
DELETE_ORDER = ["req_issues", "req_allocations", "req_demands", "requisitions",
                "postings", "move_tasks", "discrepancy_pairs", "discrepancies",
                "stocktake_cells", "stocktakes",
                "actions", "pending_items", "tasks", "sessions", "cells",
                "trays"]


def backup():
    with DB_LOCK:
        return {"app": "sortify", "version": 4, "exported_at": now(),
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
        for t in TABLES:
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

    # ---- 配字领用 ----
    if method == "POST" and path == "/api/requisitions":
        return create_requisition(body)
    m = re.fullmatch(r"/api/requisitions/(\d+)", path)
    if m and method == "GET":
        return get_requisition_detail(int(m.group(1)))
    m = re.fullmatch(r"/api/requisitions/(\d+)/(plan|confirm-tray|pick|undo|"
                     r"cancel|finish|to-return)", path)
    if m and method == "POST":
        rid, action = int(m.group(1)), m.group(2)
        if action == "plan":
            return plan_requisition(rid, body)
        if action == "confirm-tray":
            return confirm_req_tray(rid, body)
        if action == "pick":
            return pick_requisition(rid, body)
        if action == "undo":
            return undo_requisition(rid)
        if action == "cancel":
            return cancel_requisition(rid)
        if action == "finish":
            return finish_requisition(rid)
        return requisition_to_return(rid)
    m = re.fullmatch(r"/api/requisitions/(\d+)/(change-source|keep-gap)", path)
    if m and method == "POST":
        rid = int(m.group(1))
        if m.group(2) == "change-source":
            return change_req_source(rid, body)
        return keep_req_gap(rid, body)

    # ---- 盘点 ----
    if method == "POST" and path == "/api/stocktakes":
        return start_stocktake(body)
    m = re.fullmatch(r"/api/stocktakes/(\d+)/(cancel|undo-count|post|recheck)",
                     path)
    if m and method == "POST":
        sid, action = int(m.group(1)), m.group(2)
        if action == "cancel":
            return cancel_stocktake(sid)
        if action == "undo-count":
            return undo_count(sid)
        if action == "post":
            return post_stocktake(sid)
        return recheck_cell(sid, body)
    m = re.fullmatch(r"/api/stocktakes/(\d+)/count", path)
    if m and method == "POST":
        return count_stocktake_cell(int(m.group(1)), body)
    m = re.fullmatch(r"/api/stocktakes/(\d+)", path)
    if m and method == "GET":
        return get_stocktake_detail(int(m.group(1)))
    m = re.fullmatch(r"/api/discrepancies/(\d+)/(decide|revoke)", path)
    if m and method == "POST":
        did, action = int(m.group(1)), m.group(2)
        if action == "decide":
            return decide_discrepancy(did, body)
        return revoke_discrepancy(did)
    m = re.fullmatch(r"/api/pairs/(\d+)/(move|writeoff|revoke)", path)
    if m and method == "POST":
        pid, action = int(m.group(1)), m.group(2)
        if action == "move":
            return create_move_task(pid, body)
        if action == "writeoff":
            return writeoff_pair(pid)
        return revoke_pair(pid)
    m = re.fullmatch(r"/api/move-tasks/(\d+)/(scan|cancel)", path)
    if m and method == "POST":
        mid, action = int(m.group(1)), m.group(2)
        if action == "scan":
            return scan_move_task(mid, body)
        return cancel_move_task(mid)
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
