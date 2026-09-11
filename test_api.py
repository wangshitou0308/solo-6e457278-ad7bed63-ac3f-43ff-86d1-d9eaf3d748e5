#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铅字归还助手 —— API 流程自测。

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


def run_tests():
    print("== 1. 初始状态与种子字盘 ==")
    code, st = call("GET", "/api/state")
    check("GET /api/state 200", code == 200)
    check("种子字盘非空", len(st["cells"]) >= 40, str(len(st["cells"])))
    check("无进行中批次", st["session"] is None)
    check("含空格位（供改派）", any(c["char"] == "" for c in st["cells"]))

    print("== 2. 粘贴解析与批次创建 ==")
    text = "的 3\n我×2\n體 1\n天地 2\n? 1\n永 4 宋体 五号\n， 1"
    code, res = call("POST", "/api/sessions", {"text": text})
    check("创建批次成功", code == 200 and res.get("ok"), str(res)[:200])
    st = res["state"]
    sess = st["session"]
    check("批次为 active", sess and sess["status"] == "active")
    chars = [t["char"] for t in sess["tasks"]]
    check("普通字成任务", "的" in chars and "我" in chars and "永" in chars
          and "，" in chars, str(chars))
    reasons = {p["raw"]: p["reason"] for p in st["pending"]}
    check("异体字进待确认", reasons.get("體") == "variant", str(reasons))
    check("合字进待确认", reasons.get("天地") == "ligature", str(reasons))
    check("无法辨认进待确认", reasons.get("?") == "unknown", str(reasons))
    seqs = [t["seq"] for t in sess["tasks"]]
    check("任务已规划顺序", seqs == sorted(seqs), str(seqs))
    check("首任务为 active", sess["tasks"][0]["status"] == "active")

    print("== 3. 重复创建批次被拒 ==")
    code, res = call("POST", "/api/sessions", {"text": "的 1"})
    check("已有进行中批次时拒绝", code == 400, str(code))

    print("== 4. 正常确认（空回车 = 确认当前格）==")
    t0 = sess["tasks"][0]
    cell0 = next(c for c in st["cells"] if c["id"] == t0["cell_id"])
    before_qty = cell0["qty"]
    code, res = call("POST", "/api/tasks/%d/confirm" % t0["id"], {"label": ""})
    check("确认成功", res.get("ok"), str(res)[:200])
    st = res["state"]
    cell0b = next(c for c in st["cells"] if c["id"] == t0["cell_id"])
    check("格内数量增加", cell0b["qty"] == before_qty + t0["qty"],
          "%d -> %d" % (before_qty, cell0b["qty"]))
    t0b = next(t for t in st["session"]["tasks"] if t["id"] == t0["id"])
    check("任务完成", t0b["status"] == "done")
    check("下一任务激活", any(t["status"] == "active"
                              for t in st["session"]["tasks"]))

    print("== 5. 重复确认被拒 ==")
    code, res = call("POST", "/api/tasks/%d/confirm" % t0["id"], {})
    check("已完成任务报 duplicate",
          not res.get("ok") and res["conflict"]["type"] == "duplicate",
          str(res)[:200])

    print("== 6. 扫描错误格号 → 实物不符 ==")
    cur = next(t for t in st["session"]["tasks"] if t["status"] == "active")
    other = next(c for c in st["cells"]
                 if c["id"] != cur["cell_id"] and c["char"])
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"],
                     {"label": other["label"]})
    check("报 mismatch", not res.get("ok")
          and res["conflict"]["type"] == "mismatch", str(res)[:200])
    check("冲突含改派前后两格",
          "scanned" in res["conflict"] and "expected" in res["conflict"])
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"],
                     {"label": "ZZZ9"})
    check("未知格号报 unknown_label",
          not res.get("ok") and res["conflict"]["type"] == "unknown_label")

    print("== 7. 改派到扫描格并确认 ==")
    code, res = call("POST", "/api/tasks/%d/reassign" % cur["id"],
                     {"cell_id": other["id"]})
    check("改派成功", res.get("ok"), str(res)[:200])
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"],
                     {"label": other["label"]})
    check("改派后扫描确认成功", res.get("ok"), str(res)[:200])

    print("== 8. 容量不足 → 改派候选 ==")
    st = res["state"]
    cur = next(t for t in st["session"]["tasks"] if t["status"] == "active")
    # 把当前格容量压到装不下
    cell = next(c for c in st["cells"] if c["id"] == cur["cell_id"])
    code, res = call("PUT", "/api/cells/%d" % cell["id"],
                     {"capacity": cell["qty"]})
    check("压缩容量", res.get("ok"))
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
    check("报 overflow", not res.get("ok")
          and res["conflict"]["type"] == "overflow", str(res)[:200])
    check("提供候选格", len(res["conflict"].get("candidates", [])) > 0)
    cand = res["conflict"]["candidates"][0]
    code, res = call("POST", "/api/tasks/%d/reassign" % cur["id"],
                     {"cell_id": cand["id"]})
    check("改派到候选格", res.get("ok"))
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
    check("改派后确认成功", res.get("ok"), str(res)[:200])

    print("== 9. 撤销最近一次确认 ==")
    st = res["state"]
    sess = st["session"]
    cell_c = next(c for c in st["cells"] if c["id"] == cand["id"])
    code, res = call("POST", "/api/sessions/%d/undo" % sess["id"], {})
    check("撤销成功", res.get("ok"), str(res)[:200])
    st = res["state"]
    cell_c2 = next(c for c in st["cells"] if c["id"] == cand["id"])
    check("格内数量回滚", cell_c2["qty"] == cell_c["qty"] - cur["qty"],
          "%d vs %d" % (cell_c2["qty"], cell_c["qty"] - cur["qty"]))
    tb = next(t for t in st["session"]["tasks"] if t["id"] == cur["id"])
    check("任务回到 active", tb["status"] == "active" and tb["done_qty"] == 0)
    # 撤销后重新确认，继续流程
    code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
    check("撤销后可重新确认", res.get("ok"))

    print("== 10. 完成剩余任务 ==")
    st = res["state"]
    guard_n = 0
    while st["session"] and st["session"]["status"] == "active":
        cur = next((t for t in st["session"]["tasks"]
                    if t["status"] == "active"), None)
        if not cur:
            break
        code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
        if not res.get("ok"):
            # 超容则改派到第一个候选再确认
            cand = res["conflict"]["candidates"][0]
            call("POST", "/api/tasks/%d/reassign" % cur["id"],
                 {"cell_id": cand["id"]})
            code, res = call("POST", "/api/tasks/%d/confirm" % cur["id"], {})
        st = res["state"]
        guard_n += 1
        if guard_n > 50:
            break
    check("批次自动完成", st["session"] is None, str(guard_n))

    print("== 11. 待确认归位 ==")
    code, st = call("GET", "/api/state")
    check("有待确认项", len(st["pending"]) == 3, str(len(st["pending"])))
    target = next(c for c in st["cells"] if c["char"] == "")
    p0 = st["pending"][0]
    code, res = call("POST", "/api/pending/%d/resolve" % p0["id"],
                     {"cell_id": target["id"]})
    check("归位成功", res.get("ok"), str(res)[:200])
    st = res["state"]
    check("生成补录批次", st["session"] is not None
          and any(t["char"] == p0["raw"] for t in st["session"]["tasks"]))
    p1 = st["pending"][0]
    code, res = call("POST", "/api/pending/%d/resolve" % p1["id"],
                     {"ignore": True})
    check("忽略成功", res.get("ok"))
    check("待确认减少", len(res["state"]["pending"]) == 1)
    # 清理补录批次
    sid = res["state"]["session"]["id"]
    call("POST", "/api/sessions/%d/abandon" % sid, {})

    print("== 12. 容量冲突检查 ==")
    code, st = call("GET", "/api/state")
    over_cell = st["cells"][0]
    call("PUT", "/api/cells/%d" % over_cell["id"], {"capacity": 1})
    code, st = call("GET", "/api/state")
    check("检出已超容格", any(c["id"] == over_cell["id"]
                              for c in st["conflicts"]["overflow_cells"]))

    print("== 13. 备份与恢复 ==")
    code, bk = call("GET", "/api/backup")
    check("导出备份", code == 200 and bk["app"] == "sortify")
    n_cells = len(bk["tables"]["cells"])
    # 破坏数据后恢复
    call("DELETE", "/api/cells/%d" % bk["tables"]["cells"][-1]["id"])
    code, res = call("POST", "/api/restore", bk)
    check("恢复成功", res.get("ok"), str(res)[:200])
    check("格位数复原", len(res["state"]["cells"]) == n_cells)
    code, res = call("POST", "/api/restore", {"bad": 1})
    check("坏备份被拒", code == 400)

    print("== 14. 静态页面 ==")
    code, _ = call("GET", "/api/state")
    req = urllib.request.Request(BASE + "/")
    with urllib.request.urlopen(req) as r:
        html = r.read().decode("utf-8")
    check("首页可访问", "铅字归还助手" in html)
    for f in ("/static/app.js", "/static/style.css"):
        with urllib.request.urlopen(BASE + f) as r:
            check("静态资源 %s" % f, r.status == 200)

    print("== 15. 中断续做（状态持久化在 SQLite）==")
    code, res = call("POST", "/api/sessions", {"text": "的 1\n一 1"})
    check("新批次创建", res.get("ok"))
    sid = res["state"]["session"]["id"]
    # 模拟中断：直接重新拉取状态（等价于刷新页面 / 重开浏览器）
    code, st = call("GET", "/api/state")
    check("刷新后批次仍在", st["session"] and st["session"]["id"] == sid)
    check("任务进度保留", len(st["session"]["tasks"]) == 2)
    call("POST", "/api/sessions/%d/abandon" % sid, {})

    print("== 16. 格位增删改与批量生成 ==")
    code, res = call("POST", "/api/cells",
                     {"label": "T1", "char": "测", "x": 500, "y": 20,
                      "capacity": 30})
    check("新建格位", res.get("ok"), str(res)[:150])
    tid = next(c["id"] for c in res["state"]["cells"] if c["label"] == "T1")
    code, res = call("POST", "/api/cells", {"label": "T1"})
    check("重复格号被拒", code == 400)
    code, res = call("PUT", "/api/cells/%d" % tid, {"x": 520, "qty": 5})
    c = next(c for c in res["state"]["cells"] if c["id"] == tid)
    check("拖拽写回坐标与数量", c["x"] == 520 and c["qty"] == 5)
    code, res = call("POST", "/api/cells/bulk", {"cells": [
        {"label": "T%d" % i, "x": 500 + i * 54, "y": 80, "capacity": 40}
        for i in range(2, 6)]})
    check("批量生成 4 格", res.get("created") == 4, str(res)[:150])
    code, res = call("DELETE", "/api/cells/%d" % tid)
    check("删除格位", res.get("ok"))
    st = res["state"]
    check("格位已删除", not any(c["label"] == "T1" for c in st["cells"]))
    # 清理测试格
    for c in st["cells"]:
        if c["label"].startswith("T"):
            call("DELETE", "/api/cells/%d" % c["id"])


if __name__ == "__main__":
    main()
