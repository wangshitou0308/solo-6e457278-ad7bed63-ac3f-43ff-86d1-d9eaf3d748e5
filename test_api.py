#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铅字归还助手 —— API 流程自测（多字盘换盘导航）。

用法: python3 test_api.py
使用临时数据库与随机端口，不影响正式数据。
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request
import urllib.error

# 在导入 server 前指定临时数据库
_tmp = tempfile.NamedTemporaryFile(prefix="sortify-test-", suffix=".db",
                                   delete=False)
_tmp.close()
os.environ["SORTIFY_DB"] = _tmp.name

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

PORT = 18753
BASE = "http://127.0.0.1:%d" % PORT

PASS = FAIL = 0


def call(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s  %s" % (name, extra))


def main():
    srv = server.make_server(PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        run_tests()
    finally:
        srv.shutdown()
        srv.server_close()
        os.unlink(_tmp.name)
    print("\n结果: %d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


def _gate(st):
    """当前是否停在换盘闸门；返回预期盘 id，否则 None。"""
    sess = st["session"]
    if not sess or sess["status"] != "active":
        return None
    if any(t["status"] == "active" for t in sess["tasks"]):
        return None
    pending = {t["tray_id"] for t in sess["tasks"] if t["status"] == "pending"}
    order = sess["tray_order"]
    return next((tid for tid in order if tid in pending), None)


def confirm_gate(st, code=None, expect_ok=True):
    """扫描字盘码通过闸门。"""
    sess = st["session"]
    tid = _gate(st)
    if code is None:
        tr = next(t for t in st["trays"] if t["id"] == tid)
        code = tr["scan_code"]
    rcode, res = call("POST", "/api/sessions/%d/confirm-tray" % sess["id"],
                      {"scan_code": code})
    if expect_ok:
        check("扫描字盘码「%s」通过闸门" % code, res.get("ok"), str(res)[:200])
        return res["state"]
    return rcode, res


def run_until_gate_or_done(st):
    """确认当前盘的全部格位，直到撞闸门或批次结束。"""
    guard = 0
    while st["session"] and st["session"]["status"] == "active" and not _gate(st):
        cur = next((t for t in st["session"]["tasks"]
                    if t["status"] == "active"), None)
        if not cur:
            break
        code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
        if not res.get("ok"):
            # 超容则改派到同盘第一个候选格再确认
            if res["conflict"]["type"] != "overflow" or \
                    not res["conflict"].get("candidates"):
                check("盘内确认顺利", False, str(res)[:200])
                break
            cid = res["conflict"]["candidates"][0]["id"]
            call("POST", "/api/tasks/%d/reassign" % cur["id"],
                 {"cell_id": cid})
            code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
            if not res.get("ok"):
                check("改派后确认", False, str(res)[:200])
                break
        st = res["state"]
        guard += 1
        if guard > 80:
            break
    return st


def run_tests():
    print("== 1. 初始状态：默认字盘与种子格位 ==")
    code, st = call("GET", "/api/state")
    check("GET /api/state 200", code == 200)
    check("至少一个字盘", len(st["trays"]) >= 1, str(len(st["trays"])))
    t0 = st["trays"][0]
    check("旧格位归入默认字盘", t0["name"] == "默认字盘", t0["name"])
    check("默认字盘有扫描码", bool(t0["scan_code"]))
    check("种子格位非空", len(st["cells"]) >= 40, str(len(st["cells"])))
    check("格位全部有字盘归属", all(c["tray_id"] for c in st["cells"]))
    check("无进行中批次", st["session"] is None)
    check("含空格位", any(c["char"] == "" for c in st["cells"]))

    print("== 2. 字盘 CRUD 与显示排序 ==")
    code, res = call("POST", "/api/trays", {"name": "二号宋体盘",
                                            "scan_code": "TRAY-02"})
    check("新建字盘", res.get("ok"), str(res)[:200])
    t1 = res["tray_id"]
    code, res = call("POST", "/api/trays", {"name": "三号盘"})
    check("再建字盘", res.get("ok"))
    t2 = res["tray_id"]
    code, res = call("PUT", "/api/trays/%d" % t2, {"name": "三号黑体盘",
                                                   "scan_code": "TRAY-03"})
    check("改名 / 设码", res.get("ok") and
          any(t["id"] == t2 and t["name"] == "三号黑体盘" and
              t["scan_code"] == "TRAY-03" for t in res["state"]["trays"]))
    order = [t["id"] for t in st["trays"]] + [t1, t2]
    order[order.index(t2)], order[order.index(t1)] = t1, t2  # t2 提到 t1 前
    code, res = call("POST", "/api/trays/reorder", {"order": order})
    check("调整字盘顺序", res.get("ok"), str(res)[:150])
    names = [t["id"] for t in res["state"]["trays"]]
    check("顺序生效", names.index(t2) < names.index(t1), str(names))
    code, res = call("POST", "/api/trays/reorder", {"order": [t0["id"]]})
    check("顺序缺盘被拒", code == 400)

    print("== 3. 复制布局：只带格位结构，不带存量 ==")
    code, res = call("POST", "/api/trays/%d/duplicate" % t0["id"],
                     {"name": "默认副本盘", "scan_code": "TRAY-COPY"})
    check("复制成功", res.get("ok"), str(res)[:150])
    tc = res["tray_id"]
    src = [c for c in res["state"]["cells"] if c["tray_id"] == t0["id"]]
    dst = [c for c in res["state"]["cells"] if c["tray_id"] == tc]
    check("格位数相同", len(src) == len(dst) == res["copied"],
          "%d/%d/%d" % (len(src), len(dst), res["copied"]))
    check("副本存量全部清零", all(c["qty"] == 0 for c in dst))
    check("源盘存量未受影响", any(c["qty"] > 0 for c in src))
    pairs = {(c["label"]): (c["x"], c["y"], c["char"]) for c in src}
    check("格号 / 坐标 / 字种一致",
          all((c["x"], c["y"], c["char"]) == pairs[c["label"]] for c in dst))

    print("== 4. 格号仅在同一字盘内唯一 ==")
    label0 = next(c["label"] for c in st["cells"] if c["tray_id"] == t0["id"])
    code, res = call("POST", "/api/cells", {"tray_id": t1, "label": label0,
                                            "char": "测"})
    check("不同盘允许同格号", res.get("ok"), str(res)[:150])
    dup_id = next(c["id"] for c in res["state"]["cells"]
                  if c["tray_id"] == t1 and c["label"] == label0)
    code, res2 = call("POST", "/api/cells", {"tray_id": t1, "label": label0})
    check("同盘重复格号被拒", code == 400)
    code, res = call("DELETE", "/api/cells/%d" % dup_id)
    check("删除新盘格位", res.get("ok"))
    a1 = next(c["id"] for c in res["state"]["cells"]
              if c["tray_id"] == t0["id"] and c["label"] == "A1")
    code, res = call("PUT", "/api/cells/%d" % a1, {"tray_id": t1})
    check("禁止跨盘移动格位", code == 400)

    print("== 5. 删除保护 ==")
    code, res = call("DELETE", "/api/trays/%d" % t0["id"])
    check("有格位的盘不能删", code == 400)
    code, res = call("DELETE", "/api/trays/%d" % t1)
    check("空盘可删除", res.get("ok"), str(res)[:150])
    t1 = None
    # 只剩有格位的盘时也不能删
    code, res = call("DELETE", "/api/trays/%d" % tc)
    check("唯一剩余盘保护", code == 400)

    # 副本盘存量填满，避免它以更大余量抢走默认盘的字种匹配；
    # 再建一个含独有字种的新盘，供跨盘批次使用
    code, st0 = call("GET", "/api/state")
    for c in [c for c in st0["cells"] if c["tray_id"] == tc]:
        call("PUT", "/api/cells/%d" % c["id"], {"qty": c["capacity"]})
    code, res = call("POST", "/api/trays", {"name": "生僻字盘",
                                            "scan_code": "TRAY-X"})
    tx = res["tray_id"]
    code, res = call("POST", "/api/cells/bulk", {"cells": [
        {"tray_id": tx, "label": "X1", "char": "鉛", "x": 20, "y": 20},
        {"tray_id": tx, "label": "X2", "char": "鑄", "x": 74, "y": 20},
    ]})
    check("生僻字盘建格", res.get("created") == 2, str(res)[:150])

    print("== 6. 创建跨盘批次：先归组、再盘内规划 ==")
    text = "的 3\n我×1\n鉛 2\n體 1\n天地 2\n? 1\n， 1\n鑄 1"
    code, res = call("POST", "/api/sessions", {"text": text})
    check("创建批次（规划态）", code == 200 and res.get("ok"), str(res)[:200])
    st = res["state"]
    sess = st["session"]
    check("批次状态为 planning", sess["status"] == "planning", sess["status"])
    task_trays = {t["tray_id"] for t in sess["tasks"]}
    check("任务按字盘归组", task_trays <= {t0["id"], tx}, str(task_trays))
    check("涉及两个字盘", task_trays == {t0["id"], tx}, str(task_trays))
    # 盘内蛇形序号各自从 1 开始
    for tid in task_trays:
        seqs = sorted(t["seq"] for t in sess["tasks"] if t["tray_id"] == tid)
        check("盘 %s 盘内序号连续" % tid, seqs == list(range(1, len(seqs) + 1)),
              str(seqs))
    check("规划顺序按字盘默认排序",
          sess["tray_order"] == sorted(task_trays), str(sess["tray_order"]))
    check("默认起始盘为第一盘", sess["start_tray_id"] == sess["tray_order"][0])
    chars = [t["char"] for t in sess["tasks"]]
    check("两盘字种均成任务", {"的", "鉛", "鑄", "，"} <= set(chars), str(chars))
    reasons = {p["raw"]: p["reason"] for p in st["pending"]}
    check("异体 / 合字 / 待辨认进待确认",
          reasons.get("體") == "variant" and reasons.get("天地") == "ligature"
          and reasons.get("?") == "unknown", str(reasons))

    print("== 7. 调整换盘顺序、指定起始盘并锁定 ==")
    # 默认盘在前（其格号在副本盘上有同号格，便于扫描核对测试），生僻字盘随后
    fwd = sorted({t["tray_id"] for t in sess["tasks"]})
    code, res = call("POST", "/api/sessions/%d/plan" % sess["id"],
                     {"tray_order": fwd, "start_tray_id": fwd[0], "lock": True})
    check("锁定成功", res.get("ok"), str(res)[:200])
    st = res["state"]
    sess = st["session"]
    rev = fwd
    check("批次进入 active", sess["status"] == "active")
    check("顺序已锁定且起始盘为指定盘",
          sess["locked"] == 1 and sess["tray_order"] == rev and
          sess["current_tray_id"] == rev[0] and
          sess["start_tray_id"] == rev[0], str(sess)[:200])
    check("锁定后首格未激活，等待扫字盘码",
          not any(t["status"] == "active" for t in sess["tasks"]))
    code, res = call("POST", "/api/sessions/%d/plan" % sess["id"],
                     {"tray_order": list(reversed(rev))})
    check("锁定后不能再调顺序", code == 400)

    print("== 8. 跨盘闸门：扫错暂停、对照预期实际 ==")
    gate = _gate(st)
    check("停在起始盘闸门", gate == rev[0])
    other_code = next(t["scan_code"] for t in st["trays"]
                      if t["id"] != gate and t["scan_code"])
    code, res = call("POST", "/api/sessions/%d/confirm-tray" % sess["id"],
                     {"scan_code": other_code})
    check("扫错字盘码报 tray_mismatch",
          not res.get("ok") and res["conflict"]["type"] == "tray_mismatch",
          str(res)[:200])
    check("冲突对照预期与实际",
          res["conflict"]["expected"]["id"] == gate and
          res["conflict"]["scanned"]["scan_code"] == other_code)
    check("扫错后仍然暂停（首格不激活）",
          not any(t["status"] == "active"
                  for t in res["state"]["session"]["tasks"]))
    code, res = call("POST", "/api/sessions/%d/confirm-tray" % sess["id"],
                     {"scan_code": "NO-SUCH-CODE"})
    check("未知字盘码报 tray_unknown",
          not res.get("ok") and
          res["conflict"]["type"] == "tray_unknown", str(res)[:150])
    code, res = call("POST", "/api/sessions/%d/confirm-tray" % sess["id"],
                     {"scan_code": ""})
    check("空扫描码被拒", code == 400)

    print("== 9. 确认换盘后才激活首个格位 ==")
    st = confirm_gate(st)
    sess = st["session"]
    act = [t for t in sess["tasks"] if t["status"] == "active"]
    check("首个格位激活", len(act) == 1 and act[0]["tray_id"] == rev[0],
          str([(t["label"], t["status"]) for t in sess["tasks"]]))

    print("== 10. 执行页只认当前盘格号 ==")
    cur = act[0]
    # 第一格正常确认，使后续格位与“别的盘的同号格”形成对照
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
    check("首格确认成功", res.get("ok"), str(res)[:200])
    st = res["state"]
    cur = next(t for t in st["session"]["tasks"] if t["status"] == "active")
    # 新建一个含“当前盘没有的格号”的字盘 → 扫它的格号属于 wrong_tray
    code, res = call("POST", "/api/trays", {"name": "核对用盘",
                                            "scan_code": "TRAY-DUP"})
    td = res["tray_id"]
    foreign_label = "ZZ9"
    call("POST", "/api/cells", {"tray_id": td, "label": foreign_label,
                                "char": cur["char"], "x": 20, "y": 20})
    code, st_td = call("GET", "/api/state")
    other_cell = next(c for c in st_td["cells"]
                      if c["tray_id"] == td and c["label"] == foreign_label)
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"],
                     {"label": foreign_label})
    check("扫到别的盘的格号 → wrong_tray",
          not res.get("ok") and
          res["conflict"]["type"] == "wrong_tray", str(res)[:200])
    own = next(c for c in res["state"]["cells"]
               if c["tray_id"] == cur["tray_id"] and c["id"] != cur["cell_id"]
               and c["char"])
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"],
                     {"label": own["label"]})
    check("盘内扫错格 → mismatch",
          not res.get("ok") and res["conflict"]["type"] == "mismatch")
    code, res = call("POST", "/api/tasks/%d/reassign" % cur["id"],
                     {"cell_id": other_cell["id"]})
    check("禁止跨字盘改派", code == 400)
    # 正常确认
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
    check("确认当前格成功", res.get("ok"), str(res)[:200])
    st = res["state"]

    print("== 11. 盘内做完后撞下一盘闸门并跨盘 ==")
    st = run_until_gate_or_done(st)
    gate2 = _gate(st)
    check("第一盘做完进入下一盘闸门", gate2 == rev[1], str(gate2))
    check("批次未结束", st["session"] is not None)
    st = confirm_gate(st)
    check("换盘后首个格位激活于第二盘",
          [t for t in st["session"]["tasks"] if t["status"] == "active"][0]
          ["tray_id"] == rev[1])
    st = run_until_gate_or_done(st)
    check("两盘完成后批次自动结束", st["session"] is None,
          str(st["session"])[:200])
    check("state 含最近完成批次", st["last_done"] is not None)

    print("== 12. 待确认归位：追加到对应字盘 ==")
    code, st = call("GET", "/api/state")
    check("有待确认项", len(st["pending"]) == 3, str(len(st["pending"])))
    target = next(c for c in st["cells"] if c["char"] == ""
                  and c["tray_id"] == t0["id"])
    p0 = st["pending"][0]
    code, res = call("POST", "/api/pending/%d/resolve" % p0["id"],
                     {"cell_id": target["id"]})
    check("归位成功", res.get("ok"), str(res)[:200])
    st = res["state"]
    check("生成补录批次（规划态）",
          st["session"] is not None and st["session"]["status"] == "planning"
          and any(t["char"] == p0["raw"] and t["tray_id"] == t0["id"]
                  for t in st["session"]["tasks"]),
          str(st["session"])[:200])
    # 归位到另一个字盘 → 规划顺序包含两个盘
    target2 = next(c for c in st["cells"] if c["char"] == ""
                   and c["tray_id"] == tc)
    p1 = next(p for p in st["pending"] if p["id"] != p0["id"])
    code, res = call("POST", "/api/pending/%d/resolve" % p1["id"],
                     {"cell_id": target2["id"]})
    check("归位到另一字盘", res.get("ok"))
    st = res["state"]
    trays_in = {t["tray_id"] for t in st["session"]["tasks"]}
    check("规划顺序追加新字盘", trays_in == {t0["id"], tc}, str(trays_in))
    sid = st["session"]["id"]
    call("POST", "/api/sessions/%d/abandon" % sid, {})

    print("== 13. 撤销跨盘：回到对应字盘与闸门内 ==")
    code, res = call("POST", "/api/sessions",
                     {"text": "的 1\n， 1"})
    # 「的」只在默认盘；「，」默认盘与副本盘都有，按余量择一
    check("创建撤销测试批次", res.get("ok"), str(res)[:150])
    st = res["state"]
    sid = st["session"]["id"]
    order = st["session"]["tray_order"]
    call("POST", "/api/sessions/%d/plan" % sid,
         {"tray_order": order, "start_tray_id": order[0], "lock": True})
    code, st = call("GET", "/api/state")
    st = confirm_gate(st)
    st = run_until_gate_or_done(st)
    if _gate(st):
        st = confirm_gate(st)
        st = run_until_gate_or_done(st)
    check("批次完成", st["session"] is None)
    last = st["last_done"]
    last_task = last["tasks"][-1]
    code, res = call("POST", "/api/sessions/%d/undo" % last["id"], {})
    check("完成后撤销成功", res.get("ok"), str(res)[:200])
    st = res["state"]
    back = next(t for t in st["session"]["tasks"] if t["id"] == last_task["id"])
    check("批次恢复 active", st["session"]["status"] == "active")
    check("当前盘回到该任务所在字盘",
          st["session"]["current_tray_id"] == back["tray_id"] and
          back["status"] == "active",
          "%s/%s" % (st["session"]["current_tray_id"], back["tray_id"]))
    check("撤销后无需重新扫盘即可操作", not _gate(st))
    call("POST", "/api/sessions/%d/abandon" % last["id"], {})

    print("== 14. 中断续做：刷新恢复当前盘 / 锁定顺序 / 闸门进度 ==")
    code, res = call("POST", "/api/sessions", {"text": "的 1\n， 1"})
    st = res["state"]
    sid = st["session"]["id"]
    order = st["session"]["tray_order"]
    call("POST", "/api/sessions/%d/plan" % sid,
         {"tray_order": order, "start_tray_id": order[0], "lock": True})
    code, st = call("GET", "/api/state")
    check("锁定顺序已持久化", st["session"]["locked"] == 1 and
          st["session"]["tray_order"] == order)
    check("刷新后仍停在闸门", _gate(st) == order[0])
    st = confirm_gate(st)
    st = run_until_gate_or_done(st)
    if _gate(st):
        # 模拟停在第二盘闸门时刷新
        code, st2 = call("GET", "/api/state")
        check("闸门进度可恢复", _gate(st2) == order[-1])
        st = confirm_gate(st)
        st = run_until_gate_or_done(st)
    check("批次完成", st["session"] is None)

    print("== 15. 容量冲突（含字盘归属信息）==")
    code, st = call("GET", "/api/state")
    over_cell = st["cells"][0]
    call("PUT", "/api/cells/%d" % over_cell["id"], {"capacity": 1})
    code, st = call("GET", "/api/state")
    check("检出已超容格", any(c["id"] == over_cell["id"]
                              for c in st["conflicts"]["overflow_cells"]))

    print("== 16. 扫描码：非空、唯一、自动分配与编辑校验 ==")
    code, st = call("GET", "/api/state")
    # 先为后面的恢复测试留存一份正常数据备份
    code, bk = call("GET", "/api/backup")
    base = st["trays"][0]["id"]
    base_code = st["trays"][0]["scan_code"]
    code, res = call("POST", "/api/trays", {})
    check("新建字盘自动分配非空码",
          res.get("ok") and bool(res["scan_code"]), str(res)[:150])
    ta = res["tray_id"]
    check("自动分配码与现有码不重复",
          all(t["id"] == ta or t["scan_code"] != res["scan_code"]
              for t in res["state"]["trays"]))
    code, res = call("PUT", "/api/trays/%d" % ta, {"scan_code": "  "})
    check("保存空扫描码被拒", code == 400)
    code, res = call("PUT", "/api/trays/%d" % ta,
                     {"scan_code": base_code})
    check("保存重复扫描码被拒", code == 400)
    code, res = call("PUT", "/api/trays/%d" % ta,
                     {"scan_code": base_code.lower()})
    check("大小写不同也视为重复", code == 400)
    code, res = call("PUT", "/api/trays/%d" % ta,
                     {"scan_code": "TRAY-UNIQUE-A"})
    check("设置唯一扫描码成功", res.get("ok"))
    # 复制布局必须给副本独立的扫描码
    code, res = call("POST", "/api/trays/%d/duplicate" % base, {})
    check("复制字盘自动分配独立码",
          res.get("ok") and res["scan_code"] and
          res["scan_code"] != base_code, str(res)[:150])
    tb_dup = res["tray_id"]
    code, res = call("POST", "/api/trays/%d/duplicate" % base,
                     {"scan_code": base_code})
    check("复制时指定重复码被拒", code == 400)

    print("== 17. 既有数据修复：启动时补齐空码 / 重复码 ==")
    # 直接写库制造一个空码和一个重复码（临时撤下唯一索引以便构造脏数据）
    with server.DB_LOCK:
        server.db.execute("DROP INDEX idx_trays_scan_code")
        server.db.execute("UPDATE trays SET scan_code='' WHERE id=?", (ta,))
        server.db.execute("UPDATE trays SET scan_code=? WHERE id=?",
                          (base_code, tb_dup))
        server.db.commit()
    server.init_db()
    code, st = call("GET", "/api/state")
    codes = [t["scan_code"] for t in st["trays"]]
    check("修复后无空扫描码", all(codes), str(codes))
    check("修复后扫描码全局唯一", len(codes) == len(set(c.upper()
                                                         for c in codes)),
          str(codes))
    check("原有正确码保持不变",
          next(t for t in st["trays"] if t["id"] == base)["scan_code"]
          == base_code)
    check("唯一索引兜底存在", any(
        r["name"] == "idx_trays_scan_code" for r in server.rows(
            "SELECT name FROM sqlite_master WHERE type='index'")))

    print("== 18. 锁定执行前拦截问题字盘 ==")
    # 临时制造一个缺码字盘并放一个独有可匹配格位
    code, res = call("POST", "/api/trays", {"name": "缺码盘",
                                            "scan_code": "TRAY-FIXME"})
    tn = res["tray_id"]
    call("POST", "/api/cells/bulk", {"cells": [
        {"tray_id": tn, "label": "N1", "char": "爵", "x": 20, "y": 20}]})
    with server.DB_LOCK:
        server.db.execute("DROP INDEX IF EXISTS idx_trays_scan_code")
        server.db.execute("UPDATE trays SET scan_code='' WHERE id=?", (tn,))
        server.db.commit()
    code, res = call("POST", "/api/sessions", {"text": "爵 1"})
    check("任务落在缺码字盘",
          {t["tray_id"] for t in res["state"]["session"]["tasks"]} == {tn},
          str(res)[:200])
    sid = res["state"]["session"]["id"]
    code, res = call("POST", "/api/sessions/%d/plan" % sid,
                     {"tray_order": [tn], "start_tray_id": tn, "lock": True})
    check("缺码字盘阻止锁定", code == 400 and "扫描码" in res.get("error", ""),
          str(res)[:200])
    # 修复后可锁定并完成扫码确认
    server.init_db()
    code, st = call("GET", "/api/state")
    fixed = next(t["scan_code"] for t in st["trays"] if t["id"] == tn)
    code, res = call("POST", "/api/sessions/%d/plan" % sid,
                     {"tray_order": [tn], "start_tray_id": tn, "lock": True})
    check("补齐码后可锁定", res.get("ok"), str(res)[:150])
    code, res = call("POST", "/api/sessions/%d/confirm-tray" % sid,
                     {"scan_code": fixed})
    check("起始盘扫码后首格激活",
          res.get("ok") and any(t["status"] == "active"
                                for t in res["state"]["session"]["tasks"]),
          str(res)[:200])
    call("POST", "/api/sessions/%d/abandon" % sid, {})

    print("== 19. 备份 / 恢复（v4，保留字盘归属、盘点记录与领用单）==")
    # bk 在第 16 节开头、扫描码改动前留存
    check("导出 v4 备份", bk["app"] == "sortify" and bk["version"] == 4)
    check("备份含领用单表", all(t in bk["tables"] for t in
          ("requisitions", "req_demands", "req_allocations", "req_issues")))
    check("备份含 trays 表", "trays" in bk["tables"]
          and len(bk["tables"]["trays"]) >= 2)
    check("格位备份带 tray_id", all("tray_id" in c
                                    for c in bk["tables"]["cells"]))
    check("备份中扫描码非空且唯一",
          all(t.get("scan_code") for t in bk["tables"]["trays"]) and
          len({t["scan_code"].upper() for t in bk["tables"]["trays"]})
          == len(bk["tables"]["trays"]))
    n_trays = len(bk["tables"]["trays"])
    n_cells = len(bk["tables"]["cells"])
    call("DELETE", "/api/cells/%d" % bk["tables"]["cells"][-1]["id"])
    code, res = call("POST", "/api/restore", bk)
    check("恢复成功", res.get("ok"), str(res)[:200])
    check("字盘数复原", len(res["state"]["trays"]) == n_trays)
    check("格位数与归属复原", len(res["state"]["cells"]) == n_cells and
          all(c["tray_id"] for c in res["state"]["cells"]))
    code, res = call("POST", "/api/restore", {"bad": 1})
    check("坏备份被拒", code == 400)

    print("== 20. 恢复含空 / 重复扫描码的 v2 备份时自动修复 ==")
    bad2 = json.loads(json.dumps(bk))
    trs = bad2["tables"]["trays"]
    trs[0]["scan_code"] = ""
    if len(trs) > 1:
        trs[1]["scan_code"] = trs[2]["scan_code"] if len(trs) > 2 \
            else trs[0]["scan_code"] or "SAME"
        if len(trs) == 2:
            trs[0]["scan_code"] = trs[1]["scan_code"] = "SAME"
    code, res = call("POST", "/api/restore", bad2)
    check("含问题码的备份仍可恢复", res.get("ok"), str(res)[:200])
    codes = [t["scan_code"] for t in res["state"]["trays"]]
    check("恢复后扫描码被补齐且唯一",
          all(codes) and len(codes) == len(set(c.upper() for c in codes)),
          str(codes))
    # 重新恢复干净备份，保证后续静态检查不受影响
    call("POST", "/api/restore", bk)

    print("== 21. v1 备份恢复：自动归入默认字盘 ==")
    v1 = {"app": "sortify", "version": 1, "tables": {
        "cells": [{"id": 901, "label": "V1A", "char": "甲", "font": "宋体",
                   "size": "五号", "x": 0, "y": 0, "w": 46, "h": 46,
                   "qty": 3, "capacity": 50}],
        "sessions": [], "tasks": [], "pending_items": [], "actions": []}}
    code, res = call("POST", "/api/restore", v1)
    check("v1 备份可恢复", res.get("ok"), str(res)[:200])
    st = res["state"]
    check("仅一个默认字盘", len(st["trays"]) == 1 and
          st["trays"][0]["name"] == "默认字盘", str(len(st["trays"])))
    check("旧格位归入默认字盘", len(st["cells"]) == 1 and
          st["cells"][0]["label"] == "V1A" and
          st["cells"][0]["tray_id"] == st["trays"][0]["id"],
          str(st["cells"]))

    print("== 22. 静态页面 ==")
    req = urllib.request.Request(BASE + "/")
    with urllib.request.urlopen(req) as r:
        html = r.read().decode("utf-8")
    check("首页可访问", "铅字归还助手" in html)
    check("首页含换盘规划入口", "换盘" in html)
    check("首页含实体盘点入口", "实体盘点" in html)
    for f in ("/static/app.js", "/static/style.css"):
        with urllib.request.urlopen(BASE + f) as r:
            check("静态资源 %s" % f, r.status == 200)

    print("== 23. 实体盘点：锁盘、扫码续盘、差异复核、移格、入账 ==")
    code, st = call("GET", "/api/state")
    tr0 = st["trays"][0]
    # 第 21 节的 v1 恢复后库里可能只有 V1A 一格：补齐本节目所需格位
    have = {c["label"]: c for c in st["cells"] if c["tray_id"] == tr0["id"]}

    def ensure_cell(label, ch, x, y):
        if label in have:
            return have[label]
        r = call("POST", "/api/cells",
                 {"tray_id": tr0["id"], "label": label, "char": ch,
                  "x": x, "y": y})
        return next(c for c in r[1]["state"]["cells"]
                    if c["tray_id"] == tr0["id"] and c["label"] == label)

    sa = ensure_cell("A1", "的", 20, 20)
    sb = ensure_cell("A2", "一", 74, 20)
    sc = ensure_cell("A3", "中", 128, 20)
    ensure_cell("G7", "", 20, 400)
    cs = [c for c in call("GET", "/api/state")[1]["cells"]
          if c["tray_id"] == tr0["id"]]
    # 构造账面：A1 的 20、A2 一 20、A3 中 30
    call("PUT", "/api/cells/%d" % sa["id"],
         {"char": "的", "font": "宋体", "size": "五号", "qty": 20})
    call("PUT", "/api/cells/%d" % sb["id"],
         {"char": "一", "font": "宋体", "size": "五号", "qty": 20})
    call("PUT", "/api/cells/%d" % sc["id"],
         {"char": "中", "font": "宋体", "size": "五号", "qty": 30})
    # 未知码不能开始；重复开始被拒
    code, res = call("POST", "/api/stocktakes", {"scan_code": "NO-SUCH"})
    check("未知字盘码不能开始盘点", code == 400)
    code, res = call("POST", "/api/stocktakes", {"scan_code": tr0["scan_code"]})
    check("扫描字盘码开始盘点", res.get("ok"), str(res)[:150])
    stock_id = res["stocktake_id"]
    code, _ = call("POST", "/api/stocktakes", {"scan_code": tr0["scan_code"]})
    check("同一字盘不能重复开盘点单", code == 400)
    # 盘点期间锁住该盘归还确认与存量编辑 / 新建格位
    code, _ = call("PUT", "/api/cells/%d" % sa["id"], {"qty": 21})
    check("盘点中存量编辑被锁", code == 400)
    code, _ = call("POST", "/api/cells",
                   {"tray_id": tr0["id"], "label": "ZZ9"})
    check("盘点中新建格位被锁", code == 400)
    # 另一个字盘不被影响
    code, res = call("POST", "/api/trays", {"name": "盘点外盘",
                                            "scan_code": "TRAY-STK"})
    other_t = res["tray_id"]
    code, _ = call("POST", "/api/cells",
                   {"tray_id": other_t, "label": "Q1", "char": "甲"})
    check("其他字盘的编辑不受盘点锁影响", code == 200)
    # 归还确认被锁：建一个落在此盘的批次锁定后到执行态
    call("POST", "/api/sessions", {"text": "的 1"})
    code, st2 = call("GET", "/api/state")
    lock_sid = st2["session"]["id"]
    o = st2["session"]["tray_order"]
    call("POST", "/api/sessions/%d/plan" % lock_sid,
         {"tray_order": o, "start_tray_id": o[0], "lock": True})
    code, st2 = call("GET", "/api/state")
    st2 = confirm_gate(st2)
    lock_task = next(t for t in st2["session"]["tasks"]
                     if t["status"] == "active")
    code, res = call("POST", "/api/tasks/%d/confirm" % lock_task["id"], {})
    check("盘点中该盘归还确认被锁", code == 400 and "盘点" in res.get("error", ""),
          str(res)[:150])
    call("POST", "/api/sessions/%d/abandon" % lock_sid, {})

    # 按蛇形顺序逐格盘点；制造差异：
    #  A1：实物 18 枚且整格规格为「一」（账面 的20 全缺、实物 一18 溢出）
    #  A2：实物 18（一 缺2）→ 与 A1 的溢出配出错放 P1（一 2 枚 A1→A2）
    #  A3：实物 28（中 缺2，纯盘亏）；G7 暂时不能盘
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    check("盘点单含全部格位快照", sk["total"] == len(cs),
          "%d/%d" % (sk["total"], len(cs)))
    check("刷新后可恢复盘点单与进度", sk["n_pending"] == sk["total"])
    blocked = next(c for c in sk["cells"] if c["cell_label"] == "G7")
    guard = 0
    while True:
        sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
        if sk["status"] != "counting":
            break
        cur = next(c for c in sk["cells"] if c["status"] == "pending")
        if cur["id"] == blocked["id"]:
            body = {"label": "G7", "flag": "blocked"}
        elif cur["cell_label"] == "A1":
            body = {"label": "A1", "qty": 18, "actual_char": "一"}
        elif cur["cell_label"] == "A2":
            body = {"label": "A2", "qty": 18}
        elif cur["cell_label"] == "A3":
            body = {"label": "A3", "qty": 28}
        else:
            body = {"label": cur["cell_label"], "qty": cur["book_qty"]}
        code, res = call("POST", "/api/stocktakes/%d/count" % stock_id, body)
        if not res.get("ok"):
            check("逐格录入 " + cur["cell_label"], False, str(res)[:150])
            break
        guard += 1
        if guard > 100:
            break
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    check("全盘录完进入差异复核", sk["status"] == "reviewing", sk["status"])
    # 撤销最后一格（G7）回到盘点中：盘内扫错 / 未知格号应被拦截
    call("POST", "/api/stocktakes/%d/undo-count" % stock_id, {})
    code, st2 = call("GET", "/api/state")
    sk = next(x for x in st2["stocktakes"] if x["id"] == stock_id)
    check("撤销上一格回到盘点中", sk["status"] == "counting")
    g7 = next(c for c in sk["cells"] if c["status"] == "pending")
    other_label = next(c["cell_label"] for c in sk["cells"]
                       if c["cell_label"] != g7["cell_label"])
    code, res = call("POST", "/api/stocktakes/%d/count" % stock_id,
                     {"label": other_label, "qty": g7["book_qty"]})
    check("盘内扫错格拦截（count_mismatch）",
          not res.get("ok") and res["conflict"]["type"] == "count_mismatch",
          str(res)[:150])
    code, res = call("POST", "/api/stocktakes/%d/count" % stock_id,
                     {"label": "ZZ-NOPE", "qty": 1})
    check("无法识别格号拦截（count_unknown）",
          not res.get("ok") and res["conflict"]["type"] == "count_unknown")
    # 重新把 G7 录为 blocked，再次进入复核
    code, res = call("POST", "/api/stocktakes/%d/count" % stock_id,
                     {"label": g7["cell_label"], "flag": "blocked"})
    check("正确格录入成功", res.get("ok"), str(res)[:150])
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    check("再次进入复核", sk["status"] == "reviewing")

    # ---- 差异结构 ----
    d_by = {}
    for d in sk["discrepancies"]:
        d_by.setdefault(d["cell_id"], []).append(d)
    a1_discs = d_by[sa["id"]]
    a2_discs = d_by[sb["id"]]
    a3_discs = d_by[sc["id"]]
    check("A1 账面规格短缺 + 实物规格溢出",
          {d["kind"] for d in a1_discs} == {"short", "surplus"}
          and any(d["spec_char"] == "一" for d in a1_discs),
          str([(d["kind"], d["spec_char"], d["qty"]) for d in a1_discs]))
    check("A2 记录短缺", any(d["kind"] == "short" for d in a2_discs))
    check("A3 中 短缺 2 枚",
          any(d["kind"] == "short" and d["spec_char"] == "中" and d["qty"] == 2
              for d in a3_discs))
    check("G7 列为待复核",
          any(d["kind"] == "review" and d["review_kind"] == "blocked"
              for d in sk["discrepancies"]))
    check("同规格相反差额配出错放候选（不自动改库存）",
          len(sk["pairs"]) == 1 and sk["pairs"][0]["src_label"] == "A1"
          and sk["pairs"][0]["dst_label"] == "A2"
          and sk["pairs"][0]["spec_char"] == "一"
          and sk["pairs"][0]["qty"] == 2, str(sk["pairs"]))
    code, st2 = call("GET", "/api/state")
    check("配对 / 复核阶段库存未被自动修改",
          next(c for c in st2["cells"] if c["id"] == sa["id"])["qty"] == 20)
    code, res = call("POST", "/api/stocktakes/%d/post" % stock_id, {})
    check("有待复核格时不能入账", code == 400)

    # ---- 复盘 G7：先有决议时被拦，撤销后可复盘 ----
    g7_id = next(c["cell_id"] for c in sk["cells"]
                 if c["cell_label"] == "G7")
    a3_short = next(d for d in a3_discs if d["kind"] == "short")
    call("POST", "/api/discrepancies/%d/decide" % a3_short["id"],
         {"decision": "loss"})
    code, res = call("POST", "/api/stocktakes/%d/recheck" % stock_id,
                     {"cell_id": g7_id})
    check("已有盘亏决议时复盘待复核格被拦", code == 400)
    call("POST", "/api/discrepancies/%d/revoke" % a3_short["id"], {})
    code, res = call("POST", "/api/stocktakes/%d/recheck" % stock_id,
                     {"cell_id": g7_id})
    check("撤销决议后可复盘待复核格", res.get("ok"), str(res)[:150])
    code, st2 = call("GET", "/api/state")
    sk = next(x for x in st2["stocktakes"] if x["id"] == stock_id)
    check("复盘后差异清空、回到盘点中", sk["status"] == "counting"
          and len(sk["discrepancies"]) == 0)
    g7c = next(c for c in sk["cells"] if c["status"] == "pending")
    code, res = call("POST", "/api/stocktakes/%d/count" % stock_id,
                     {"label": g7c["cell_label"], "qty": g7c["book_qty"]})
    check("复盘格正常录入", res.get("ok"), str(res)[:150])
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    check("复盘完成重新生成差异（已无待复核项）",
          sk["status"] == "reviewing" and len(sk["pairs"]) == 1
          and not any(d["kind"] == "review" for d in sk["discrepancies"]))

    # ---- 只改字体（字符留空）独立进入差异复核 ----
    code, res = call("POST", "/api/stocktakes/%d/recheck" % stock_id,
                     {"cell_id": sc["id"]})
    check("把 A3 拉回复盘（仅字体变化场景）", res.get("ok"), str(res)[:150])
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    pend_a3 = [c for c in sk["cells"]
               if c["cell_label"] == "A3" and c["status"] == "pending"]
    check("A3 已回到待盘", len(pend_a3) == 1, str(sk["status"]))
    code, res = call("POST", "/api/stocktakes/%d/count" % stock_id,
                     {"label": "A3", "qty": 30, "actual_font": "黑体"})
    check("只填实物字体、字符留空也提交成功", res.get("ok"), str(res)[:150])
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    a3_after = next(c for c in sk["cells"] if c["cell_label"] == "A3")
    check("字体独立保存、字符仍取账面",
          a3_after["actual_char"] == "中" and a3_after["actual_font"] == "黑体"
          and a3_after["actual_size"] == "五号",
          str((a3_after["actual_char"], a3_after["actual_font"],
               a3_after["actual_size"])))
    check("仅字体不符也生成差异（账面字体短缺 + 实物字体溢出）",
          sk["status"] == "reviewing"
          and any(d["cell_id"] == sc["id"] and d["spec_font"] == "宋体"
                  for d in sk["discrepancies"])
          and any(d["cell_id"] == sc["id"] and d["spec_font"] == "黑体"
                  for d in sk["discrepancies"]),
          str([(d["kind"], d["spec_font"], d["qty"])
               for d in sk["discrepancies"] if d["cell_id"] == sc["id"]]))

    # ---- 只改字号（字符、字体留空）同样独立进入差异 ----
    call("POST", "/api/stocktakes/%d/recheck" % stock_id,
         {"cell_id": sc["id"]})
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    code, res = call("POST", "/api/stocktakes/%d/count" % stock_id,
                     {"label": "A3", "qty": 30, "actual_size": "四号"})
    check("只填实物字号、字符留空也提交成功", res.get("ok"), str(res)[:150])
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    a3_after = next(c for c in sk["cells"] if c["cell_label"] == "A3")
    check("字号独立保存、字符与字体仍取账面",
          a3_after["actual_char"] == "中" and a3_after["actual_font"] == "宋体"
          and a3_after["actual_size"] == "四号",
          str((a3_after["actual_char"], a3_after["actual_font"],
               a3_after["actual_size"])))
    check("仅字号不符也生成差异",
          any(d["cell_id"] == sc["id"] and d["spec_size"] == "五号"
              for d in sk["discrepancies"])
          and any(d["cell_id"] == sc["id"] and d["spec_size"] == "四号"
                  for d in sk["discrepancies"]))

    # 恢复 A3 为“中 / 宋体 / 五号 28（短缺 2）”场景，供后续入账
    call("POST", "/api/stocktakes/%d/recheck" % stock_id,
         {"cell_id": sc["id"]})
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    code, res = call("POST", "/api/stocktakes/%d/count" % stock_id,
                     {"label": "A3", "qty": 28})
    check("A3 恢复为短缺 2 枚", res.get("ok"), str(res)[:150])
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]

    # ---- 决议方向校验与撤销 ----
    a3_short = next(d for d in sk["discrepancies"]
                    if d["cell_id"] == sc["id"] and d["kind"] == "short")
    code, _ = call("POST", "/api/discrepancies/%d/decide" % a3_short["id"],
                   {"decision": "gain"})
    check("短缺项不能认定盘盈", code == 400)
    call("POST", "/api/discrepancies/%d/decide" % a3_short["id"],
         {"decision": "loss"})
    check("盘亏决议可撤销",
          call("POST", "/api/discrepancies/%d/revoke" % a3_short["id"],
               {})[0] == 200)
    call("POST", "/api/discrepancies/%d/decide" % a3_short["id"],
         {"decision": "loss"})
    a1_surplus = next(d for d in sk["discrepancies"]
                      if d["cell_id"] == sa["id"] and d["kind"] == "surplus")
    call("POST", "/api/discrepancies/%d/decide" % a1_surplus["id"],
         {"decision": "gain"})
    a1_book_short = next(d for d in sk["discrepancies"]
                         if d["cell_id"] == sa["id"] and d["kind"] == "short")
    call("POST", "/api/discrepancies/%d/decide" % a1_book_short["id"],
         {"decision": "loss"})

    # 候选未处理不能入账
    code, res = call("POST", "/api/stocktakes/%d/post" % stock_id, {})
    check("错放候选未处理不能入账", code == 400)
    # 生成移格任务并依次复扫两端
    pair = sk["pairs"][0]
    code, res = call("POST", "/api/pairs/%d/move" % pair["id"], {})
    check("错放候选生成移格任务", res.get("ok") and res["move_task_id"],
          str(res)[:150])
    mid = res["move_task_id"]
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    mv = next(m for m in sk["move_tasks"] if m["id"] == mid)
    code, res = call("POST", "/api/move-tasks/%d/scan" % mid,
                     {"label": mv["target_label"]})
    check("移格先扫目标格被拦（move_wrong_cell）",
          not res.get("ok") and res["conflict"]["type"] == "move_wrong_cell",
          str(res)[:150])
    code, res = call("POST", "/api/move-tasks/%d/scan" % mid,
                     {"label": "ZZ-NOPE"})
    check("移格扫未知格号被拦",
          not res.get("ok") and res["conflict"]["type"] == "move_unknown")
    code, res = call("POST", "/api/move-tasks/%d/scan" % mid,
                     {"label": mv["source_label"]})
    check("复扫来源格通过", res.get("ok") and res["stage_done"] == "source")
    code, res = call("POST", "/api/move-tasks/%d/scan" % mid,
                     {"label": mv["source_label"]})
    check("第二端仍扫来源格被拦",
          not res.get("ok") and res["conflict"]["type"] == "move_wrong_cell")
    code, res = call("POST", "/api/move-tasks/%d/scan" % mid,
                     {"label": mv["target_label"]})
    check("复扫目标格通过（仍不改库存）",
          res.get("ok") and res["stage_done"] == "target")
    code, st2 = call("GET", "/api/state")
    check("移格双端复扫完成、入账前库存不动",
          next(c for c in st2["cells"] if c["id"] == sa["id"])["qty"] == 20)
    sk = next(x for x in st2["stocktakes"] if x["id"] == stock_id)
    check("预览就绪", sk["preview"]["ready"], str(sk["preview"]["errors"]))
    chg = {c["label"]: c for c in sk["preview"]["changes"]}
    check("预览 A1 入账后 16、A2 入账后 20",
          chg["A1"]["final_qty"] == 16 and chg["A2"]["final_qty"] == 20
          and chg["A1"]["move_out"] == 2 and chg["A2"]["move_in"] == 2,
          str({k: (v["final_qty"], v["move_out"], v["move_in"])
               for k, v in chg.items()}))

    code, res = call("POST", "/api/stocktakes/%d/post" % stock_id, {})
    check("一次性入账成功", res.get("ok"), str(res)[:200])
    st2 = res["state"]
    fa1 = next(c for c in st2["cells"] if c["id"] == sa["id"])
    fa2 = next(c for c in st2["cells"] if c["id"] == sb["id"])
    fa3 = next(c for c in st2["cells"] if c["id"] == sc["id"])
    check("入账后 A1=16（一）、A2=20（一）、A3=28（中，盘亏2）",
          fa1["qty"] == 16 and fa1["char"] == "一" and fa2["qty"] == 20
          and fa2["char"] == "一" and fa3["qty"] == 28 and fa3["char"] == "中",
          "%s/%s/%s" % ((fa1["char"], fa1["qty"]), (fa2["char"], fa2["qty"]),
                        (fa3["char"], fa3["qty"])))
    check("入账后盘锁解除，可编辑",
          call("PUT", "/api/cells/%d" % sa["id"], {"qty": 16})[0] == 200)
    sk = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    check("盘点单状态 posted 并保留前后值",
          sk["status"] == "posted" and sk["postings"] and
          any(p["kind"] == "move_out" and p["before_qty"] == 18
              and p["after_qty"] == 16 for p in sk["postings"])
          and any(p["kind"] == "move_in" and p["after_qty"] == 20
                  for p in sk["postings"]),
          str([(p["kind"], p["before_qty"], p["after_qty"])
               for p in sk["postings"]]))
    code, _ = call("POST", "/api/discrepancies/%d/revoke" % a1_surplus["id"],
                   {})
    check("入账后决议不可撤销", code == 400)
    check("入账盘点单进入历史概要",
          any(h["id"] == stock_id and h["status"] == "posted"
              for h in st2["stocktake_history"]))
    bk_post = call("GET", "/api/backup")[1]
    check("备份包含盘点记录与任务进度",
          bk_post["version"] >= 3
          and len(bk_post["tables"]["stocktakes"]) >= 1
          and len(bk_post["tables"]["stocktake_cells"]) >= len(cs)
          and any(r["status"] == "posted"
                  for r in bk_post["tables"]["move_tasks"])
          and len(bk_post["tables"]["postings"]) >= 4)

    print("== 24. 盘点：未入账决议撤销 / 取消盘点单 / 候选核销 ==")
    code, res = call("POST", "/api/stocktakes", {"scan_code": "TRAY-STK"})
    sid2 = res["stocktake_id"]
    sk = call("GET", "/api/stocktakes/%d" % sid2)[1]["stocktake"]
    q1_book = sk["cells"][0]["book_qty"]
    call("POST", "/api/stocktakes/%d/count" % sid2,
         {"label": "Q1", "qty": q1_book + 3})  # 纯溢出 3
    sk = call("GET", "/api/stocktakes/%d" % sid2)[1]["stocktake"]
    check("单格纯溢出进入复核", sk["status"] == "reviewing"
          and any(d["kind"] == "surplus" and d["qty"] == 3
                  for d in sk["discrepancies"]))
    d_sur = next(d for d in sk["discrepancies"] if d["kind"] == "surplus")
    call("POST", "/api/discrepancies/%d/decide" % d_sur["id"],
         {"decision": "gain"})
    code, res = call("POST", "/api/discrepancies/%d/revoke" % d_sur["id"], {})
    check("未入账盘盈决议可撤销", res.get("ok"))
    code, res = call("POST", "/api/stocktakes/%d/cancel" % sid2, {})
    check("取消盘点单（盘锁解除）", res.get("ok"), str(res)[:150])
    code, st2 = call("GET", "/api/state")
    check("取消后该盘可立即编辑",
          call("POST", "/api/cells",
               {"tray_id": other_t, "label": "Q2", "char": "乙"})[0] == 200)
    check("取消的盘点单进入历史且不再活动",
          all(x["id"] != sid2 for x in st2["stocktakes"])
          and any(h["id"] == sid2 and h["status"] == "cancelled"
                  for h in st2["stocktake_history"]))

    # 错放候选核销路径：默认盘再开一盘，造一对同规格相反差额，选“核销”
    code, res = call("POST", "/api/stocktakes", {"scan_code": tr0["scan_code"]})
    sid3 = res["stocktake_id"]
    sk = call("GET", "/api/stocktakes/%d" % sid3)[1]["stocktake"]
    for c0 in sorted(sk["cells"], key=lambda c: c["seq"]):
        if c0["cell_label"] == "A1":
            body = {"label": "A1", "qty": fa1["qty"] + 2}
        elif c0["cell_label"] == "A2":
            body = {"label": "A2", "qty": fa2["qty"] - 2}
        else:
            body = {"label": c0["cell_label"], "qty": c0["book_qty"]}
        code, res = call("POST", "/api/stocktakes/%d/count" % sid3, body)
        if not res.get("ok"):
            check("核销流程逐格录入 " + c0["cell_label"], False, str(res)[:150])
            break
    sk = call("GET", "/api/stocktakes/%d" % sid3)[1]["stocktake"]
    check("核销流程配出错放候选", len(sk["pairs"]) == 1
          and sk["pairs"][0]["spec_char"] == "一", str(sk["pairs"])[:150])
    code, res = call("POST", "/api/pairs/%d/writeoff" % sk["pairs"][0]["id"], {})
    check("候选可核销为盘盈 / 盘亏", res.get("ok"), str(res)[:150])
    sk = call("GET", "/api/stocktakes/%d" % sid3)[1]["stocktake"]
    check("核销后预览可入账", sk["preview"]["ready"],
          str(sk["preview"]["errors"])[:200])
    code, res = call("POST", "/api/stocktakes/%d/post" % sid3, {})
    check("核销盘点单可入账", res.get("ok"), str(res)[:150])

    print("== 25. 恢复盘点备份（v3 往返）==")
    bk3 = call("GET", "/api/backup")[1]
    code, res = call("POST", "/api/restore", bk3)
    check("v3 备份恢复成功", res.get("ok"), str(res)[:150])
    hist = res["state"]["stocktake_history"]
    check("恢复后保留入账盘点单与快照",
          any(h["id"] == stock_id and h["status"] == "posted" for h in hist))
    det = call("GET", "/api/stocktakes/%d" % stock_id)[1]["stocktake"]
    check("恢复后仍可查看入账前后值",
          det["status"] == "posted" and len(det["postings"]) >= 4
          and len(det["cells"]) == len(cs))

    print("== 26. 配字领用：汇总 / 多盘分配 / 预留互斥 / 扫码取字 ==")
    code, st = call("GET", "/api/state")
    t0 = st["trays"][0]
    f4 = next((c for c in st["cells"] if c["char"] == "永"
               and c["tray_id"] == t0["id"]), None)
    if f4 is None:
        slot = next((c for c in st["cells"] if c["tray_id"] == t0["id"]
                     and not c["char"]), st["cells"][0])
        call("PUT", "/api/cells/%d" % slot["id"],
             {"char": "永", "font": "宋体", "size": "五号"})
        f4 = dict(slot, char="永")
    call("PUT", "/api/cells/%d" % f4["id"], {"qty": 21})
    f4["qty"] = 21
    # 新字盘：同规格两格 + 异体字格 + 一个不参与分配的备用格
    code, res = call("POST", "/api/trays",
                     {"name": "领用备盘", "scan_code": "TRAY-R2"})
    tr2 = res["tray_id"]
    for lab, ch, font, size, q in (
            ("R1", "永", "宋体", "五号", 6),
            ("R2", "永", "宋体", "五号", 4),
            ("R3", "體", "宋体", "五号", 5),
            ("R4", "永", "宋体", "五号", 0)):
        call("POST", "/api/cells", {"tray_id": tr2, "label": lab, "char": ch,
                                    "font": font, "size": size, "qty": q})
    r1 = next(c for c in call("GET", "/api/state")[1]["cells"]
              if c["tray_id"] == tr2 and c["label"] == "R1")

    # 文字模式：逐字汇总；标点 / 空格可分别排除（逗号不计入、空格不计入）
    code, res = call("POST", "/api/requisitions",
                     {"text": "永永和和 ", "mode": "text",
                      "include_punct": False, "include_spaces": False})
    check("文字模式领用单创建（排除标点空格）", res.get("ok")
          and res["dropped_spaces"] == 1 and res["dropped_punct"] == 0,
          str(res)[:200])
    rid0 = res["requisition_id"]
    req0 = call("GET", "/api/requisitions/%d" % rid0)[1]["requisition"]
    dm0 = {d["char"]: d for d in req0["demands"]}
    check("逐字汇总需求", dm0["永"]["qty"] == 2 and dm0["和"]["qty"] == 2,
          str([(d["char"], d["qty"]) for d in req0["demands"]]))
    # 计入标点 / 空格时标点也进入需求
    code, res_p = call("POST", "/api/requisitions",
                       {"text": "永，", "mode": "text",
                        "include_punct": True, "include_spaces": True})
    req0b = call("GET", "/api/requisitions/%d"
                 % res_p["requisition_id"])[1]["requisition"]
    check("可计入标点", any(d["char"] == "，" for d in req0b["demands"]))
    call("POST", "/api/requisitions/%d/cancel" % res_p["requisition_id"], {})
    call("POST", "/api/requisitions/%d/cancel" % rid0, {})
    check("待锁定领用单可取消并释放预留", True)

    # 清单模式：数量不足 / 仅有异体字 / 规格不符分别列出，不自动替换
    # 需求 25：F4(21) + R4(4) 贪心配满；R1/R2 留给后续预留互斥验证
    code, res = call("POST", "/api/requisitions",
                     {"text": "永,25,宋体,五号\n體,2\n永,5,黑体,五号",
                      "mode": "list"})
    rid = res["requisition_id"]
    req = call("GET", "/api/requisitions/%d" % rid)[1]["requisition"]
    issues = {(d["char"], d["font"], d["size"]): d for d in req["demands"]}
    check("数量不足单列", issues[("永", "宋体", "五号")]["issue"] == "short"
          and issues[("永", "宋体", "五号")]["qty"] == 25,
          str([(d["char"], d["font"], d["issue"]) for d in req["demands"]]))
    check("异体字单列不替换", issues[("體", "", "")]["issue"] == "variant",
          str([(d["char"], d["issue"]) for d in req["demands"]]))
    check("规格不符单列不替换",
          issues[("永", "黑体", "五号")]["issue"] == "spec")
    yong_allocs = [a for a in req["allocations"] if a["cell_char"] == "永"]
    check("从多个字盘同规格格位分配且不自动替换异规格",
          sum(a["qty"] for a in yong_allocs) == 25
          and {a["tray_id"] for a in yong_allocs} == {t0["id"], tr2},
          str([(a["label"], a["qty"]) for a in yong_allocs]))
    tray2_yong = {a["label"]: a["qty"] for a in yong_allocs
                  if a["tray_id"] == tr2}
    check("贪心优先大容量格（R1=6 先于 R2=4）",
          "R1" in tray2_yong)
    order = req["tray_order"]
    check("换盘路线按字盘归组", set(order) == {t0["id"], tr2})

    # 预留互斥：第二张领用单只能看到扣除预留后的余量
    # 本领用单未占用的格位 = R1..R4 中未分配的那些
    used_ids = {a["cell_id"] for a in req["allocations"]}
    free = {c["label"]: c["qty"] for c in call("GET", "/api/state")[1]["cells"]
            if c["tray_id"] == tr2 and c["id"] not in used_ids
            and c["char"] == "永"}
    free_total = sum(free.values())
    code, res = call("POST", "/api/requisitions",
                     {"text": "永,%d" % (free_total + 3), "mode": "list"})
    rid2 = res["requisition_id"]
    req2 = call("GET", "/api/requisitions/%d" % rid2)[1]["requisition"]
    check("扣除其他未完成领用单的预留量",
          all(a["cell_id"] != f4["id"] for a in req2["allocations"])
          and sum(a["qty"] for a in req2["allocations"]) == free_total
          and set(free) >= {a["label"] for a in req2["allocations"]}
          and any(d["issue"] == "short" for d in req2["demands"]),
          str([(a["label"], a["qty"]) for a in req2["allocations"]]))
    call("POST", "/api/requisitions/%d/cancel" % rid2, {})

    # R4 补库存（本领用单未占用），供后续少取改选来源格使用
    r4_cell = next(c for c in call("GET", "/api/state")[1]["cells"]
                   if c["tray_id"] == tr2 and c["label"] == "R4")
    call("PUT", "/api/cells/%d" % r4_cell["id"], {"qty": 8})

    # 锁定与换盘闸门
    code, res = call("POST", "/api/requisitions/%d/plan" % rid, {"lock": True})
    check("可锁定来源格", res.get("ok"), str(res)[:200])
    code, res = call("POST", "/api/requisitions/%d/pick" % rid,
                     {"label": "R1"})
    check("未扫字盘码不能取字", code == 400)
    code, res = call("POST", "/api/requisitions/%d/confirm-tray" % rid,
                     {"scan_code": "NOPE"})
    check("扫错字盘码拦截",
          res["conflict"]["type"] == "tray_unknown")
    code, res = call("POST", "/api/requisitions/%d/confirm-tray" % rid,
                     {"scan_code": "TRAY-R2"})
    check("换错盘暂停对照", res["conflict"]["type"] == "tray_mismatch")
    code, res = call("POST", "/api/requisitions/%d/confirm-tray" % rid,
                     {"scan_code": "TRAY-01"})
    check("扫正确字盘码后激活首格", res.get("ok")
          and any(a["status"] == "active"
                  for a in res["state"]["requisition"]["allocations"]))
    req = res["state"]["requisition"]
    cur = next(a for a in req["allocations"] if a["status"] == "active")
    qty_before = next(c for c in res["state"]["cells"]
                      if c["id"] == cur["cell_id"])["qty"]
    code, res = call("POST", "/api/requisitions/%d/pick" % rid,
                     {"label": "R1"})
    check("格号只认当前字盘", res["conflict"]["type"] == "wrong_tray")
    code, res = call("POST", "/api/requisitions/%d/pick" % rid,
                     {"label": cur["label"], "qty": cur["qty"]})
    check("全取确认并扣库存", res.get("ok")
          and next(c for c in res["state"]["cells"]
                   if c["id"] == cur["cell_id"])["qty"]
          == qty_before - cur["qty"], str(res)[:200])
    check("首盘做完停在换盘闸门",
          not any(a["status"] == "active"
                  for a in res["state"]["requisition"]["allocations"]))
    code, res = call("POST", "/api/requisitions/%d/confirm-tray" % rid,
                     {"scan_code": "TRAY-R2"})
    check("扫第二盘码通过", res.get("ok"), str(res)[:200])

    # 少取：确认前不扣库存；同盘候选不含异体格
    req = res["state"]["requisition"]
    cur = next(a for a in req["allocations"] if a["status"] == "active")
    qb = next(c for c in res["state"]["cells"]
              if c["id"] == cur["cell_id"])["qty"]
    code, res = call("POST", "/api/requisitions/%d/pick" % rid,
                     {"label": cur["label"], "qty": 1})
    check("少取先出冲突且不扣库存",
          res["conflict"]["type"] == "short_pick"
          and next(c for c in res["state"]["cells"]
                   if c["id"] == cur["cell_id"])["qty"] == qb)
    cands = [c["label"] for c in res["conflict"]["candidates"]]
    check("候选只列同盘同规格未预留格",
          "R3" not in cands and set(cands) <= {"R4"}, str(cands))
    # 改选到 R4（本单未预留的备用同规格格）
    r4c = next(c for c in res["state"]["cells"]
               if c["tray_id"] == tr2 and c["label"] == "R4")
    code, res = call("POST", "/api/requisitions/%d/change-source" % rid,
                     {"allocation_id": cur["id"], "cell_id": r4c["id"],
                      "picked": 1})
    check("改选同盘另一来源格", res.get("ok"), str(res)[:200])
    req = res["state"]["requisition"]
    newcur = next(a for a in req["allocations"] if a["status"] == "active")
    check("新来源格承接余量", newcur["cell_id"] == r4c["id"]
          and newcur["qty"] == cur["qty"] - 1)
    check("确认前新格不扣库存",
          next(c for c in res["state"]["cells"]
               if c["id"] == r4c["id"])["qty"] == r4c["qty"])
    # 撤销上一笔：恢复原格为当前格、补回 1 枚
    code, res = call("POST", "/api/requisitions/%d/undo" % rid, {})
    req = res["state"]["requisition"]
    check("撤销改选恢复来源格与库存",
          next(c for c in res["state"]["cells"]
               if c["id"] == cur["cell_id"])["qty"] == qb
          and any(a["status"] == "active" and a["cell_id"] == cur["cell_id"]
                  for a in req["allocations"]), str(res)[:200])

    # 收尾：把剩余当前格逐笔走完（少取则保留缺口）
    guard = 0
    while guard < 12:
        req = call("GET", "/api/requisitions/%d" % rid)[1]["requisition"]
        if req["status"] != "active":
            break
        act = next((a for a in req["allocations"]
                    if a["status"] == "active"), None)
        if act is None:
            code, res = call("POST", "/api/requisitions/%d/confirm-tray"
                             % rid, {"scan_code": "TRAY-R2"})
            if not res.get("ok"):
                break
            continue
        code, res = call("POST", "/api/requisitions/%d/pick" % rid,
                         {"label": act["label"], "qty": act["qty"]})
        if res.get("ok"):
            guard += 1
            continue
        if res["conflict"]["type"] in ("short_pick", "short_avail"):
            code, res = call("POST", "/api/requisitions/%d/keep-gap" % rid,
                             {"allocation_id": act["id"], "picked": 0})
            check("收尾保留缺口 " + act["label"], res.get("ok"), str(res)[:150])
            guard += 1
            continue
        check("收尾取字 " + act["label"], False, str(res)[:150])
        break
    req = call("GET", "/api/requisitions/%d" % rid)[1]["requisition"]
    check("领用单完成", req["status"] == "done", req["status"])
    taken = sum(a["taken_qty"] for a in req["allocations"])
    check("实取数量入账", taken > 0)

    # 一键带入归还流程：按实取数量生成规划态批次，逐格扫码照旧
    code, res = call("POST", "/api/requisitions/%d/to-return" % rid, {})
    check("一键带入归还流程", res.get("ok"), str(res)[:200])
    sid = res["session_id"]
    sess = res["state"]["session"]
    check("归还批次按实取数量", sess["id"] == sid
          and sum(t["qty"] for t in sess["tasks"]) == taken
          and all(t["status"] == "pending" for t in sess["tasks"]),
          str([(t["label"], t["qty"]) for t in sess["tasks"]]))
    check("归还仍需逐格扫码（批次停在规划态）", sess["status"] == "planning")
    code, res = call("POST", "/api/requisitions/%d/to-return" % rid, {})
    check("重复带入被拒", code == 400)
    call("POST", "/api/sessions/%d/abandon" % sid, {})

    # 已完成领用单撤销最后一笔（恢复执行态）
    code, res = call("POST", "/api/requisitions/%d/undo" % rid, {})
    check("完成后可撤销上一笔", res.get("ok")
          and res["state"]["requisition"]["status"] == "active",
          str(res)[:150])

    print("== 27. 领用单备份恢复（v4 往返）==")
    bk4 = call("GET", "/api/backup")[1]
    code, res = call("POST", "/api/restore", bk4)
    check("v4 备份恢复成功", res.get("ok"), str(res)[:150])
    hist_ids = [h["id"] for h in res["state"]["req_history"]]
    check("领用单记录保留", rid in hist_ids or
          res["state"]["requisition"] and
          res["state"]["requisition"]["id"] == rid)
    det = call("GET", "/api/requisitions/%d" % rid)[1]["requisition"]
    check("领用预留 / 实取 / 来源格保留",
          len(det["allocations"]) >= 3 and all(a["cell_id"]
                                               for a in det["allocations"]))


if __name__ == "__main__":
    main()
