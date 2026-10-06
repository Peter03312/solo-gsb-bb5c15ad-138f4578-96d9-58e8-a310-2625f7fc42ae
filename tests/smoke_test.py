"""端到端冒烟测试（仅标准库）：

  python tests/smoke_test.py

会用临时 SQLite 启动真实 uvicorn 子进程，走完整 HTTP 流程。
"""

import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.stats import binomial_tail, paired_binomial_two_sided  # noqa: E402

P, Q = 1 / 3, 2 / 3
FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 1. 统计函数：与直接 comb 求和交叉验证
# ---------------------------------------------------------------------------

def ref_tail(n, k):
    return sum(
        math.comb(n, i) * P ** i * Q ** (n - i)
        for i in range(k, n + 1)
    )


for n in range(0, 31):
    for k in range(0, n + 2):
        got = binomial_tail(n, k)
        want = ref_tail(n, min(k, n + 1))
        assert abs(got - want) < 1e-9, (n, k, got, want)

check("二项尾概率与直接求和一致 (0≤n≤30)", True)
check("n=3,k=3 ≈ (1/3)^3", abs(binomial_tail(3, 3) - 1 / 27) < 1e-12,
      binomial_tail(3, 3))
check("k=0 尾概率为 1", binomial_tail(10, 0) == 1.0)
check("k>n 尾概率为 0", binomial_tail(10, 11) == 0.0)
# 经典三角检验数值：n=6 全对或5对的上尾约 0.0178…
check("n=6,k=5 ≈ 0.0178", abs(binomial_tail(6, 5) - 0.01783) < 1e-4,
      binomial_tail(6, 5))
# 大 n 不溢出：正规浮点范围内单调（允许 1 附近 1e-12 舍入误差），次正规以下截断为 0
seq = [binomial_tail(200, k) for k in range(0, 120)]
meaningful = [x for x in seq if x >= 2.3e-308]
check("n=200 正规范围内尾概率随 k 单调不增",
      all(meaningful[i] + 1e-12 >= meaningful[i + 1]
          for i in range(len(meaningful) - 1)))
check("n=200 次正规尾概率截断为 0 且无负值",
      all(x == 0.0 for x in seq if x < 2.3e-308) and min(seq) >= 0)


# ---------------------------------------------------------------------------
# 1b. 配对二项检验（McNemar 精确检验）双侧 p 值：与直接 comb 求和交叉验证
# ---------------------------------------------------------------------------

def ref_paired(n, k):
    if n <= 0:
        return 1.0
    kk = max(k, n - k)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(kk, n + 1)) / 2 ** n)


for n in range(0, 61):
    for k in range(0, n + 1):
        got = paired_binomial_two_sided(n, k)
        want = ref_paired(n, k)
        assert abs(got - want) < 1e-12, (n, k, got, want)

check("配对二项双侧 p 与直接求和一致 (0≤n≤60)", True)
check("配对：差异人数 0 → p=1", paired_binomial_two_sided(0, 0) == 1.0)
check("配对：n=1 单侧极端也为 1（双侧不显著）",
      paired_binomial_two_sided(1, 0) == 1.0
      and paired_binomial_two_sided(1, 1) == 1.0)
check("配对：n=6 全同向 p=2/64=0.03125",
      abs(paired_binomial_two_sided(6, 0) - 2 / 64) < 1e-12
      and abs(paired_binomial_two_sided(6, 6) - 2 / 64) < 1e-12,
      paired_binomial_two_sided(6, 0))
check("配对：n=6 五对一 p=0.21875",
      abs(paired_binomial_two_sided(6, 1) - 0.21875) < 1e-12)
check("配对：n=11 九对二 p≈0.0654 不显著",
      abs(paired_binomial_two_sided(11, 2) - 0.0654296875) < 1e-12)
check("配对：对称性 p(k)=p(n-k)",
      all(abs(paired_binomial_two_sided(n, k) - paired_binomial_two_sided(n, n - k))
          < 1e-12 for n in range(0, 40) for k in range(0, n + 1)))


# ---------------------------------------------------------------------------
# 2. 启动服务，走 HTTP 全流程
# ---------------------------------------------------------------------------

tmpdir = tempfile.mkdtemp(prefix="sniff-test-")
db_path = os.path.join(tmpdir, "test.db")
server_log = open(os.path.join(tmpdir, "server.log"), "w")
port = 8000 + (uuid.uuid4().int % 1000)
base = f"http://127.0.0.1:{port}"

env = dict(os.environ, SNIFF_DB_PATH=db_path)
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app",
     "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    env=env, stdout=server_log, stderr=subprocess.STDOUT, text=True,
)


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


try:
    # 等待启动
    last_err = None
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/time", timeout=2)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.2)
    else:
        raise RuntimeError(f"服务未启动: {last_err!r}，见 " + server_log.name)

    s, t = req("GET", "/api/time")
    check("GET /api/time", s == 200 and "now" in t)

    # --- 2.0 旧库兼容：服务启动前用旧版表结构直接造一个已结算的旧测试 ---
    import hashlib as _hashlib
    legacy_code = "Leg0ld01"
    legacy_token = "legacy-token-1234"
    legacy_deadline = int(time.time()) - 3600
    legacy_settled = int(time.time()) - 1800
    lcon = sqlite3.connect(db_path)
    lcon.execute(
        "CREATE TABLE IF NOT EXISTS tests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, "
        "token_hash TEXT NOT NULL, sample_a TEXT NOT NULL, sample_b TEXT NOT NULL, "
        "deadline INTEGER NOT NULL, min_valid INTEGER NOT NULL, created_at INTEGER, "
        "settled_at INTEGER, valid_count INTEGER, correct_count INTEGER, "
        "p_value REAL, distinguishable INTEGER)")
    lcon.execute(
        "INSERT INTO tests (code, token_hash, sample_a, sample_b, deadline, "
        "min_valid, created_at, settled_at, valid_count, correct_count, p_value, "
        "distinguishable) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (legacy_code,
         _hashlib.sha256(f"{legacy_code}:{legacy_token}".encode()).hexdigest(),
         "OLD-A", "OLD-B", legacy_deadline, 1, legacy_deadline - 7200,
         legacy_settled, 1, 1, 1 / 3, 0))
    lcon.execute(
        "CREATE TABLE IF NOT EXISTS assignments ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, test_id INTEGER NOT NULL, "
        "panelist TEXT NOT NULL, code TEXT NOT NULL UNIQUE, odd_pos INTEGER, "
        "odd_sample TEXT, answer INTEGER, answered_at INTEGER, "
        "UNIQUE(test_id, panelist))")
    lcon.execute(
        "INSERT INTO assignments (test_id, panelist, code, odd_pos, odd_sample, "
        "answer, answered_at) VALUES "
        "((SELECT id FROM tests WHERE code=?), '老评员', 'LEGACYCODE1', "
        "1, 'A', 1, ?)", (legacy_code, legacy_deadline - 100))
    lcon.commit()
    lcon.close()
    # 触发一次 lifespan 建表/迁移逻辑（模拟重启）：新连接执行 init_db 的等价迁移
    from app import db as app_db  # noqa: E402
    app_db.DB_PATH = db_path
    app_db.init_db()
    s, lt = req("GET", f"/api/tests/{legacy_code}/timeline?token={legacy_token}")
    check("旧库：时间线可查 200", s == 200, str(lt))
    check("旧库：以 history_start 标示记录起点，不虚构既往操作",
          s == 200 and len(lt["events"]) == 1
          and lt["events"][0]["type"] == "history_start"
          and lt["history_complete"] is False, str(lt))
    check("旧库：起点标记明确说明不做历史重建",
          "不做历史重建" in lt["events"][0]["payload"]["note"]
          or "不虚构" in lt["events"][0]["payload"]["note"],
          str(lt["events"][0]["payload"]))
    # 旧接口行为兼容：进度、结算冻结结果照常
    s, lprog = req("GET", f"/api/tests/{legacy_code}?token={legacy_token}")
    check("旧库：进度接口兼容", s == 200 and lprog["settled"] is True)
    s, lset = req("POST", f"/api/tests/{legacy_code}/settle",
                  {"token": legacy_token})
    check("旧库：重复结算仍返回冻结结果", s == 200 and lset["valid_count"] == 1)
    # 起点标记只补一次（幂等）
    app_db.init_db()
    s, lt2 = req("GET", f"/api/tests/{legacy_code}/timeline?token={legacy_token}")
    check("旧库：迁移幂等，起点标记不重复",
          sum(1 for e in lt2["events"] if e["type"] == "history_start") == 1)

    # --- 2.1 输入校验 ---
    s, _ = req("POST", "/api/tests", {
        "sample_a": "A1", "sample_b": "A1",
        "panelists": ["x"], "deadline": t["now"] + 60000, "min_valid": 1})
    check("相同样本编号被拒", s == 422)

    s, _ = req("POST", "/api/tests", {
        "sample_a": "A1", "sample_b": "B1",
        "panelists": ["x"], "deadline": t["now"] - 60000, "min_valid": 1})
    check("过去截止时间被拒", s == 422)

    # --- 2.2 均衡性测试（9 人） ---
    s, bal = req("POST", "/api/tests", {
        "sample_a": "S-1", "sample_b": "S-2",
        "panelists": [f"P{i}" for i in range(9)],
        "deadline": t["now"] + 3600_000, "min_valid": 3})
    check("创建 9 人测试", s == 200, str(bal))

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    tid = con.execute("SELECT id FROM tests WHERE code=?", (bal["test_code"],)).fetchone()["id"]
    rows = con.execute("SELECT * FROM assignments WHERE test_id=?", (tid,)).fetchall()
    pos_counts = {p: sum(1 for r in rows if r["odd_pos"] == p) for p in (1, 2, 3)}
    samp_counts = {x: sum(1 for r in rows if r["odd_sample"] == x) for x in ("A", "B")}
    check("异样杯位置均衡 (3/3/3)", list(pos_counts.values()) == [3, 3, 3],
          str(pos_counts))
    check("异样样本均衡 (4/5 或 5/4)", sorted(samp_counts.values()) == [4, 5],
          str(samp_counts))
    codes = [r["code"] for r in rows]
    check("盲评码唯一", len(set(codes)) == 9)

    # 评员接口不得泄露分配
    s, ev = req("GET", f"/api/eval/{codes[0]}")
    check("评员页面可访问", s == 200)
    check("结算前不泄露 odd_pos/odd_sample/样本编号",
          "odd_pos" not in ev and "odd_sample" not in ev and
          "sample_a" not in ev, str(ev))

    # 无效应答 404
    s, _ = req("GET", "/api/eval/NOPE-NOPE")
    check("无效盲评码 404", s == 404)

    # 负责人鉴权
    s, _ = req("GET", f"/api/tests/{bal['test_code']}?token=wrong")
    check("错误令牌 403", s == 403)
    s, admin = req("GET", f"/api/tests/{bal['test_code']}?token={bal['token']}")
    check("负责人可见分发链接", s == 200 and len(admin["codes"]) == 9
          and all(c["code"] for c in admin["codes"]))
    leaked = json.dumps(admin)
    check("进度接口无任何 odd_pos/answer 泄露",
          "odd_pos" not in leaked and '"answer"' not in leaked)

    # 未截止不能结算
    s, _ = req("POST", f"/api/tests/{bal['test_code']}/settle",
               {"token": bal["token"]})
    check("截止前结算被拒 (409)", s == 409)

    # --- 2.3 主测试：6 人，2 秒后截止，min_valid=6 ---
    s, t2 = req("POST", "/api/tests", {
        "sample_a": "X-100", "sample_b": "X-200",
        "panelists": ["甲", "乙", "丙", "丁", "戊", "己"],
        "deadline": int(time.time() * 1000) + 2500, "min_valid": 6})
    check("创建 6 人短截止测试", s == 200)

    rows2 = con.execute("SELECT * FROM assignments WHERE test_id="
                        "(SELECT id FROM tests WHERE code=?)",
                        (t2["test_code"],)).fetchall()
    # 前 3 人答对，第 4 人答错，第 5 人弃权，第 6 人不交
    plan = {0: "correct", 1: "correct", 2: "correct",
            3: "wrong", 4: "abstain", 5: "missing"}
    for i, r in enumerate(rows2):
        if plan[i] == "missing":
            continue
        if plan[i] == "abstain":
            st, _ = req("POST", f"/api/eval/{r['code']}/submit", {"abstain": True})
            check(f"评员{i} 弃权提交 200", st == 200)
        else:
            ans = r["odd_pos"] if plan[i] == "correct" else (
                r["odd_pos"] % 3 + 1)
            st, _ = req("POST", f"/api/eval/{r['code']}/submit", {"answer": ans})
            check(f"评员{i} 选杯提交 200", st == 200)
        # 重复提交
        st2, _ = req("POST", f"/api/eval/{r['code']}/submit", {"answer": 1})
        check(f"评员{i} 二次提交 409", st2 == 409)

    # 等截止
    time.sleep(3.0)

    # 截止后拒答（未交的第 6 人）
    st, body = req("POST", f"/api/eval/{rows2[5]['code']}/submit", {"answer": 1})
    check("截止后答卷被拒 (403)", st == 403, str(body))

    # 结算
    s, rep = req("POST", f"/api/tests/{t2['test_code']}/settle",
                 {"token": t2["token"]})
    check("截止后结算 200", s == 200, str(rep))
    check("有效答卷=4 (弃权/未交不计)", rep["valid_count"] == 4, str(rep["valid_count"]))
    check("答对=3", rep["correct_count"] == 3, str(rep["correct_count"]))
    check("弃权=1/未交=1", rep["abstained_count"] == 1 and rep["missing_count"] == 1)
    check("p = P(X≥3|n=4,p=1/3)≈0.111", abs(rep["p_value"] - 0.111) < 1e-3,
          str(rep["p_value"]))
    # 样本不足（4 < 6）→ 不可辨
    check("有效数不足 → 不可辨", rep["distinguishable"] is False)
    check("结论文案说明样本不足", "样本不足" in rep["conclusion"], rep["conclusion"])

    # 冻结：重复结算返回同一结果
    s, rep2 = req("POST", f"/api/tests/{t2['test_code']}/settle",
                  {"token": t2["token"]})
    check("重复结算返回冻结结果",
          s == 200 and rep2["settled_at"] == rep["settled_at"]
          and rep2["valid_count"] == rep["valid_count"])

    # --- 时间线（t2）：创建 → 5 份答卷（4 选杯 + 1 弃权，第 6 人未交）→ 结算 ---
    s, pre = req("GET", f"/api/tests/{t2['test_code']}/timeline?token={t2['token']}")
    check("时间线鉴权错误令牌 403",
          req("GET", f"/api/tests/{t2['test_code']}/timeline?token=nope")[0] == 403)
    check("不存在的测试时间线 404",
          req("GET", "/api/tests/NOPE9999/timeline?token=x")[0] == 404)
    # 结算前的时间线在结算前一刻取不到（现已结算）；这里改用一个未结算测试验证盲态
    s, tl = req("GET", f"/api/tests/{t2['test_code']}/timeline?token={t2['token']}")
    check("时间线 200", s == 200, str(tl))
    check("时间线历史完整（无起点标记）", tl["history_complete"] is True)
    types = [e["type"] for e in tl["events"]]
    check("时间线类型序列 create/answer×5/settle",
          types[0] == "create"
          and types.count("answer") == 5
          and types[-1] == "settle" and types.count("settle") == 1,
          str(types))
    check("时间线 seq 严格递增（提交顺序稳定）",
          [e["seq"] for e in tl["events"]]
          == sorted(e["seq"] for e in tl["events"]))
    check("时间线 ts 单调不减（服务端时间）",
          all(tl["events"][i]["ts"] <= tl["events"][i + 1]["ts"]
              for i in range(len(tl["events"]) - 1)))
    ans_ev = [e for e in tl["events"] if e["type"] == "answer"]
    abn = [e for e in ans_ev if e["payload"]["abstained"]]
    chk = [e for e in ans_ev if "correct" in e["payload"]]
    check("结算后：弃权事件 answer 为 null", len(abn) == 1
          and abn[0]["payload"].get("answer") is None, str(abn))
    check("结算后：选杯事件透出 answer/odd_pos/odd_sample/correct",
          len(chk) == 4
          and all({"answer", "odd_pos", "odd_sample", "correct"}
                  <= set(e["payload"]) for e in chk))
    check("结算后：判定对错与实际一致（3 对 1 错）",
          sum(1 for e in chk if e["payload"]["correct"] is True) == 3
          and sum(1 for e in chk if e["payload"]["correct"] is False) == 1,
          str([e["payload"]["correct"] for e in chk]))
    sev = [e for e in tl["events"] if e["type"] == "settle"][0]
    check("结算事件含结果与判定",
          sev["payload"]["valid_count"] == 4
          and sev["payload"]["correct_count"] == 3
          and sev["payload"]["abstained_count"] == 1
          and sev["payload"]["missing_count"] == 1
          and sev["payload"]["distinguishable"] is False,
          str(sev["payload"]))
    cre = tl["events"][0]["payload"]
    check("创建事件含样本/截止/初始名单",
          cre["sample_a"] == "X-100" and len(cre["panelists"]) == 6
          and all({"panelist", "code"} <= set(p) for p in cre["panelists"]),
          str(cre))
    # 重复结算不得产生第二条 settle 事件
    s, tl_r = req("GET", f"/api/tests/{t2['test_code']}/timeline?token={t2['token']}")
    check("重复结算不追加事件",
          sum(1 for e in tl_r["events"] if e["type"] == "settle") == 1
          and len(tl_r["events"]) == len(tl["events"]))

    # 结算前盲态：另建测试，答卷后、结算前时间线不得泄露选杯/异样杯/对错
    s, bm = req("POST", "/api/tests", {
        "sample_a": "B-1", "sample_b": "B-2",
        "panelists": ["b1", "b2"],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    bm_rows = con.execute(
        "SELECT code FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (bm["test_code"],)).fetchall()
    req("POST", f"/api/eval/{bm_rows[0]['code']}/submit", {"answer": 2})
    req("POST", f"/api/eval/{bm_rows[1]['code']}/submit", {"abstain": True})
    s, bmt = req("GET", f"/api/tests/{bm['test_code']}/timeline?token={bm['token']}")
    check("结算前时间线 200 且未结算", s == 200 and bmt["settled"] is False)
    blob = json.dumps(bmt, ensure_ascii=False)
    check("结算前不泄露选杯(answer)/异样杯/异样样本/对错",
          '"answer":' not in blob and '"odd_pos"' not in blob
          and '"odd_sample"' not in blob and '"correct"' not in blob, blob)
    bm_ans = [e for e in bmt["events"] if e["type"] == "answer"]
    check("结算前只显示已答/弃权状态",
          len(bm_ans) == 2
          and {e["payload"]["submitted"] for e in bm_ans} == {True}
          and sum(1 for e in bm_ans if e["payload"]["abstained"]) == 1
          and all("panelist" in e["payload"] for e in bm_ans),
          str(bm_ans))

    # 结算后评员可见本人分配
    s, ev2 = req("GET", f"/api/eval/{rows2[0]['code']}")
    check("结算后评员可回看 odd_pos", ev2.get("odd_pos") == rows2[0]["odd_pos"])
    check("结算后评员可见样本编号", ev2.get("sample_a") == "X-100")

    # --- 2.4 显著场景：min_valid=4，4 人全对 → p=(1/3)^4≈0.0123 → 可辨 ---
    s, t3 = req("POST", "/api/tests", {
        "sample_a": "K-1", "sample_b": "K-2",
        "panelists": ["q1", "q2", "q3", "q4"],
        "deadline": int(time.time() * 1000) + 2000, "min_valid": 4})
    rows3 = con.execute("SELECT * FROM assignments WHERE test_id="
                        "(SELECT id FROM tests WHERE code=?)",
                        (t3["test_code"],)).fetchall()
    for r in rows3:
        st, _ = req("POST", f"/api/eval/{r['code']}/submit",
                    {"answer": r["odd_pos"]})
        assert st == 200
    time.sleep(2.6)
    s, rep3 = req("POST", f"/api/tests/{t3['test_code']}/settle",
                  {"token": t3["token"]})
    check("4/4 全对 p≈0.0123", abs(rep3["p_value"] - 1 / 81) < 1e-9,
          str(rep3["p_value"]))
    check("4/4 全对且达标 → 判为可辨", rep3["distinguishable"] is True,
          rep3["conclusion"])
    check("回看含全部分配与答卷", len(rep3["assignments"]) == 4
          and all(a["correct"] is True for a in rep3["assignments"]))

    # --- 2.5 零有效答卷边界：全员未交 → p=1.0、样本不足、不可辨 ---
    s, t4 = req("POST", "/api/tests", {
        "sample_a": "Z-1", "sample_b": "Z-2",
        "panelists": ["z1", "z2", "z3"],
        "deadline": int(time.time() * 1000) + 2000, "min_valid": 2})
    assert s == 200
    time.sleep(2.6)
    s, rep4 = req("POST", f"/api/tests/{t4['test_code']}/settle",
                  {"token": t4["token"]})
    check("零答卷结算 200", s == 200)
    check("零有效答卷 valid=0/correct=0/p=1.0",
          rep4["valid_count"] == 0 and rep4["correct_count"] == 0
          and rep4["p_value"] == 1.0, str(rep4))
    check("零有效答卷判为样本不足而非证据未达阈值",
          rep4["distinguishable"] is False and "样本不足" in rep4["conclusion"],
          rep4["conclusion"])

    # --- 2.7 名单调整：撤回 / 补位（复用 9 人均衡测试 bal，截止尚远、无人提交） ---
    # 先记录调整前分配（已有评员的杯序/异样样本必须保持不变）
    before = con.execute(
        "SELECT panelist, odd_pos, odd_sample FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY panelist",
        (bal["test_code"],)).fetchall()
    before_map = {r["panelist"]: (r["odd_pos"], r["odd_sample"]) for r in before}

    # 只补位：+1 人，补到当前计数最缺的位置与样本
    s, add1 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                  {"token": bal["token"], "add_panelists": ["P9"]})
    check("只补位 1 人 200", s == 200, str(add1))
    check("补位返回新专属码",
          len(add1["added"]) == 1 and add1["added"][0]["panelist"] == "P9"
          and add1["added"][0]["code"] and add1["added"][0]["url"])
    check("补位后有效名单=10", add1["progress"]["total"] == 10)

    # 一次操作撤回 3 人并补位 2 人：10-3+2=9，位置计数应恢复 3/3/3
    withdraw3 = [codes[6], codes[7], codes[8]]
    s, adj = req("POST", f"/api/tests/{bal['test_code']}/adjust", {
        "token": bal["token"],
        "withdraw_codes": withdraw3,
        "add_panelists": ["R1", "R2"]})
    check("撤回3补位2 一次操作 200", s == 200, str(adj))
    check("撤回名单含 3 人", len(adj["withdrawn"]) == 3)
    check("补位名单含 2 人",
          [a["panelist"] for a in adj["added"]] == ["R1", "R2"])
    check("调整后有效名单=9", adj["progress"]["total"] == 9)

    after = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND status='active'",
        (bal["test_code"],)).fetchall()
    pc = {p: sum(1 for r in after if r["odd_pos"] == p) for p in (1, 2, 3)}
    sc = {x: sum(1 for r in after if r["odd_sample"] == x) for x in ("A", "B")}
    # 已有评员杯序不可变：撤 3 补 2 且撤回集中在同一档时（如 4/3/0），
    # 补位只能加在最少档，最终 4/3/2（极差 2）即不重排前提下的可达最优；
    # 不要求必然 3/3/3，只校验总数正确且各档极差 ≤ 2。
    check("撤回+补位后异样杯位置总数 9 且计数极差 ≤ 2（不重排前提下可达最优）",
          sum(pc.values()) == 9 and max(pc.values()) - min(pc.values()) <= 2,
          str(pc))
    check("撤回+补位后异样样本仍均衡 (4/5 或 5/4)",
          sorted(sc.values()) == [4, 5], str(sc))
    check("已有评员的杯序/异样样本不变",
          all(before_map[r["panelist"]] == (r["odd_pos"], r["odd_sample"])
              for r in after if r["panelist"] in before_map))
    check("补位者取得全新唯一码",
          all(r["code"] not in codes for r in after if r["panelist"] in ("R1", "R2")))

    # 撤回后的旧码立即无法查看或提交
    st, _ = req("GET", f"/api/eval/{codes[6]}")
    check("已撤回旧码查看 → 404", st == 404)
    st, _ = req("POST", f"/api/eval/{codes[6]}/submit", {"answer": 1})
    check("已撤回旧码提交 → 404", st == 404)

    # 进度接口只统计有效名单，并给出 withdrawn_count
    s, admin2 = req("GET", f"/api/tests/{bal['test_code']}?token={bal['token']}")
    check("进度接口有效名单=9、已撤回=3",
          s == 200 and admin2["progress"]["total"] == 9
          and admin2["withdrawn_count"] == 3, str(admin2.get("progress")))
    check("分发名单不含已撤回者",
          sorted(c["panelist"] for c in admin2["codes"]
                 if c["panelist"] in (f"P{i}" for i in (6, 7, 8))) == [])

    # 已作答 / 已弃权者不可撤回：单独建一个 3 人测试
    s, aw = req("POST", "/api/tests", {
        "sample_a": "W-1", "sample_b": "W-2",
        "panelists": ["w1", "w2", "w3"],
        "deadline": t["now"] + 3600_000, "min_valid": 1})
    assert s == 200
    aw_rows = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?)", (aw["test_code"],)).fetchall()
    req("POST", f"/api/eval/{aw_rows[0]['code']}/submit", {"answer": 1})
    req("POST", f"/api/eval/{aw_rows[1]['code']}/submit", {"abstain": True})
    st, b1 = req("POST", f"/api/tests/{aw['test_code']}/adjust", {
        "token": aw["token"], "withdraw_codes": [aw_rows[0]["code"]]})
    check("已作答者不可撤回 (409)", st == 409 and "已作答" in b1["detail"],
          str(b1))
    st, b2 = req("POST", f"/api/tests/{aw['test_code']}/adjust", {
        "token": aw["token"], "withdraw_codes": [aw_rows[1]["code"]]})
    check("已弃权者不可撤回 (409)", st == 409 and "已弃权" in b2["detail"],
          str(b2))

    # 姓名不得重用（与有效名单、已撤回者都冲突）
    st, b3 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"], "add_panelists": ["R1"]})
    check("与有效名单重名被拒 (422)", st == 422 and "重用" in b3["detail"])
    st, b4 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"], "add_panelists": ["P7"]})
    check("与已撤回者重名被拒 (422)", st == 422 and "重用" in b4["detail"])
    st, b5 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"],
                  "add_panelists": ["dup", "dup"]})
    check("补位名单内部重名被拒 (422)", st == 422)
    st, b6 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"],
                  "withdraw_codes": [codes[0], codes[0]]})
    check("撤回名单内部重复被拒 (422)", st == 422)
    st, b7 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"]})
    check("空操作被拒 (422)", st == 422)
    st, b8 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"], "withdraw_codes": ["NOPE-NOPE"]})
    check("撤回无效盲评码 → 404", st == 404)
    st, b9 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": "wrong", "add_panelists": ["x"]})
    check("调整接口错误令牌 403", st == 403)

    # 下限：新建 min_valid=4 的 4 人测试，撤回 1 人 → 3 < 4，整次操作无效
    s, mn = req("POST", "/api/tests", {
        "sample_a": "M-1", "sample_b": "M-2",
        "panelists": ["m1", "m2", "m3", "m4"],
        "deadline": t["now"] + 3600_000, "min_valid": 4})
    assert s == 200
    mn_rows = con.execute(
        "SELECT code FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (mn["test_code"],)).fetchall()
    st, low = req("POST", f"/api/tests/{mn['test_code']}/adjust", {
        "token": mn["token"], "withdraw_codes": [mn_rows[0]["code"]]})
    check("低于最少有效人数整次拒绝 (422)",
          st == 422 and "低于" in low["detail"], str(low))
    mn_left = con.execute(
        "SELECT COUNT(*) AS c FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND status='active'",
        (mn["test_code"],)).fetchone()["c"]
    check("拒绝后名单不变（仍 4 人有效 / 0 撤回）", mn_left == 4, str(mn_left))
    st, _ = req("GET", f"/api/eval/{mn_rows[0]['code']}")
    check("被回滚的撤回码仍可正常查看", st == 200)
    # 撤回并同数补位使人数不变 → 允许
    st, keep = req("POST", f"/api/tests/{mn['test_code']}/adjust", {
        "token": mn["token"],
        "withdraw_codes": [mn_rows[0]["code"]],
        "add_panelists": ["m5"]})
    check("撤回1补位1 人数仍=4 允许",
          st == 200 and keep["progress"]["total"] == 4, str(st))

    # 上限：新建 2 人测试（min_valid=1），直接补到 500 允许，再补 1 人拒绝
    s, cap = req("POST", "/api/tests", {
        "sample_a": "C-1", "sample_b": "C-2",
        "panelists": ["c1", "c2"],
        "deadline": t["now"] + 3600_000, "min_valid": 1})
    assert s == 200
    st, cap498 = req("POST", f"/api/tests/{cap['test_code']}/adjust",
                     {"token": cap["token"],
                      "add_panelists": [f"N{i}" for i in range(498)]})
    check("补位至 500 人允许 (200)",
          st == 200 and cap498["progress"]["total"] == 500, str(st))
    st, cap501 = req("POST", f"/api/tests/{cap['test_code']}/adjust",
                     {"token": cap["token"], "add_panelists": ["N498"]})
    check("补位超过 500 人拒绝 (422)", st == 422 and "上限" in cap501["detail"],
          str(cap501))
    cap_rows = con.execute(
        "SELECT odd_pos FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND status='active'",
        (cap["test_code"],)).fetchall()
    cap_pc = {p: sum(1 for r in cap_rows if r["odd_pos"] == p)
              for p in (1, 2, 3)}
    check("补位到 500 人位置计数极差 ≤ 1",
          max(cap_pc.values()) - min(cap_pc.values()) <= 1, str(cap_pc))

    # 截止后 / 结算后拒绝调整（复用已结算的 t2）
    st, da = req("POST", f"/api/tests/{t2['test_code']}/adjust",
                 {"token": t2["token"], "add_panelists": ["late"]})
    check("已结算测试调整被拒 (409)", st == 409)

    # 截止（未结算）同样拒绝：建 1 人短截止测试并等过截止
    s, dl = req("POST", "/api/tests", {
        "sample_a": "D-1", "sample_b": "D-2",
        "panelists": ["d1"],
        "deadline": int(time.time() * 1000) + 1500, "min_valid": 1})
    assert s == 200
    time.sleep(2.0)
    st, dle = req("POST", f"/api/tests/{dl['test_code']}/adjust",
                  {"token": dl["token"], "add_panelists": ["d2"]})
    check("截止后未结算也拒绝调整 (409)", st == 409 and "截止" in dle["detail"],
          str(dle))

    # 结算只统计有效名单：新建 4 人，1 答对 1 弃权 1 未交 1 撤回
    s, ex = req("POST", "/api/tests", {
        "sample_a": "E-1", "sample_b": "E-2",
        "panelists": ["e1", "e2", "e3", "e4"],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    ex_rows = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (ex["test_code"],)).fetchall()
    req("POST", f"/api/eval/{ex_rows[0]['code']}/submit",
        {"answer": ex_rows[0]["odd_pos"]})      # 答对
    req("POST", f"/api/eval/{ex_rows[1]['code']}/submit", {"abstain": True})
    # e3 未交；e4 未交且在截止前被撤回
    st, _ = req("POST", f"/api/tests/{ex['test_code']}/adjust", {
        "token": ex["token"],
        "withdraw_codes": [ex_rows[3]["code"]],
        "add_panelists": ["e5"]})                 # e5 补位后也未交
    assert st == 200
    # 直接在库里把截止时间改到过去以便立即结算
    con.execute("UPDATE tests SET deadline=? WHERE code=?",
                (int(time.time()) - 10, ex["test_code"]))
    con.commit()
    s, exr = req("POST", f"/api/tests/{ex['test_code']}/settle",
                 {"token": ex["token"]})
    check("结算排除已撤回：有效名单 4 人",
          exr["valid_count"] == 1 and exr["abstained_count"] == 1
          and exr["missing_count"] == 2, str((exr["valid_count"],
                                              exr["abstained_count"],
                                              exr["missing_count"])))
    check("结算回看不含已撤回评员 e4",
          all(a["panelist"] != "e4" for a in exr["assignments"])
          and {a["panelist"] for a in exr["assignments"]}
              == {"e1", "e2", "e3", "e5"})

    # 一次名单调整保留为一笔含撤回与补位明细的事件；失败操作不留任何记录
    s, bat = req("GET", f"/api/tests/{bal['test_code']}/timeline?token={bal['token']}")
    check("调整时间线可查", s == 200, str(bat))
    adj_ev = [e for e in bat["events"] if e["type"] == "adjust"]
    check("成功调整各一笔（补位1 + 撤3补2 = 2 笔）",
          len(adj_ev) == 2, str(len(adj_ev)))
    second = adj_ev[-1]["payload"]
    check("调整事件同笔含撤回与补位明细",
          len(second["withdrawn"]) == 3 and len(second["added"]) == 2
          and second["active_total"] == 9, str(second))
    check("调整事件撤回明细含评员与码",
          all({"panelist", "code"} <= set(w) for w in second["withdrawn"]))
    before_n = len(bat["events"])
    # 触发若干必然失败的调整（409/422/404/403），均不得留下事件
    req("POST", f"/api/tests/{bal['test_code']}/adjust",
        {"token": bal["token"], "add_panelists": ["R1"]})          # 422 重名
    req("POST", f"/api/tests/{bal['test_code']}/adjust",
        {"token": bal["token"]})                                    # 422 空操作
    req("POST", f"/api/tests/{bal['test_code']}/adjust",
        {"token": bal["token"], "withdraw_codes": ["NOPE-NOPE"]})  # 404
    req("POST", f"/api/tests/{bal['test_code']}/adjust",
        {"token": "wrong", "add_panelists": ["z"]})                 # 403
    s, bat2 = req("GET", f"/api/tests/{bal['test_code']}/timeline?token={bal['token']}")
    check("校验失败的调整不留下任何记录",
          len(bat2["events"]) == before_n
          and sum(1 for e in bat2["events"] if e["type"] == "adjust") == 2,
          f"{before_n} -> {len(bat2['events'])}")

    # --- 2.8 并发：同一评员码上「撤回」与「答卷」竞争，按实际先完成者判定 ---
    now_s, _ = req("GET", "/api/time")
    s, ct = req("POST", "/api/tests", {
        "sample_a": "F-1", "sample_b": "F-2",
        "panelists": [f"c{i}" for i in range(40)],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    ct_rows = con.execute(
        "SELECT code FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (ct["test_code"],)).fetchall()

    def race_round(c):
        result = {}
        barrier = threading.Barrier(2)

        def do_submit():
            barrier.wait()
            stt, _ = req("POST", f"/api/eval/{c}/submit", {"answer": 1})
            result["submit"] = stt

        def do_withdraw():
            barrier.wait()
            stt, _ = req("POST", f"/api/tests/{ct['test_code']}/adjust",
                         {"token": ct["token"], "withdraw_codes": [c]})
            result["withdraw"] = stt

        t1 = threading.Thread(target=do_submit)
        t2 = threading.Thread(target=do_withdraw)
        t1.start(); t2.start(); t1.join(); t2.join()
        return result

    n_submit_win = n_withdraw_win = 0
    race_ok = no_5xx = True
    for i in range(20):  # 20 个不同的码；最坏全撤回也仍有 20 名有效评员
        c = ct_rows[i]["code"]
        r = race_round(c)
        row = con.execute(
            "SELECT status, answer FROM assignments WHERE code=?", (c,)
        ).fetchone()
        # 答卷先完成：submit=200、withdraw=409（已作答不可撤回），active 且有答卷
        if r["submit"] == 200 and r["withdraw"] == 409:
            if not (row["status"] == "active" and row["answer"] == 1):
                race_ok = False
            n_submit_win += 1
        # 撤回先完成：withdraw=200、submit=404（旧码立即失效），withdrawn 且无答卷
        elif r["withdraw"] == 200 and r["submit"] == 404:
            if not (row["status"] == "withdrawn" and row["answer"] is None):
                race_ok = False
            n_withdraw_win += 1
        else:
            race_ok = False  # 不允许双赢 / 双输 / 其他状态
        if r["submit"] >= 500 or r["withdraw"] >= 500:
            no_5xx = False

    check("并发：撤回与答卷结果互补、数据库状态与胜出方一致，无脏中间态",
          race_ok and n_submit_win + n_withdraw_win == 20,
          f"答卷先完成 {n_submit_win} 轮，撤回先完成 {n_withdraw_win} 轮")
    check("并发过程中无 5xx 服务端错误", no_5xx)

    # 并发后时间线：每个胜出方恰好一笔记录，且顺序与最终状态一致
    s, ctt = req("GET", f"/api/tests/{ct['test_code']}/timeline?token={ct['token']}")
    race_answers = [e for e in ctt["events"] if e["type"] == "answer"]
    race_adjusts = [e for e in ctt["events"] if e["type"] == "adjust"]
    check("并发：胜出答卷数与 answer 事件数一致",
          len(race_answers) == n_submit_win,
          f"{len(race_answers)} vs {n_submit_win}")
    check("并发：胜出撤回数与 adjust 事件撤回明细总数一致",
          sum(len(e["payload"]["withdrawn"]) for e in race_adjusts)
          == n_withdraw_win,
          str([len(e["payload"]["withdrawn"]) for e in race_adjusts]))
    # 时间线里出现的答卷评员在库中必为 active 且已答；撤回明细中的码必为 withdrawn
    timeline_codes_ans = {e["payload"]["code"] for e in race_answers}
    timeline_codes_wd = {w["code"] for e in race_adjusts
                         for w in e["payload"]["withdrawn"]}
    state_ok = True
    for c in timeline_codes_ans:
        r = con.execute(
            "SELECT status, answer FROM assignments WHERE code=?", (c,)).fetchone()
        if not (r["status"] == "active" and r["answer"] is not None):
            state_ok = False
    for c in timeline_codes_wd:
        r = con.execute(
            "SELECT status, answer FROM assignments WHERE code=?", (c,)).fetchone()
        if not (r["status"] == "withdrawn" and r["answer"] is None):
            state_ok = False
    check("并发：时间线记录与最终状态一一对应，顺序一致",
          state_ok and timeline_codes_ans.isdisjoint(timeline_codes_wd))
    check("并发：时间线仍按 seq 稳定排序",
          [e["seq"] for e in ctt["events"]]
          == sorted(e["seq"] for e in ctt["events"]))

    # --- 2.9 脱敏结果分享凭证（仅已结算实验；读取脱敏；到期/撤销拒绝） ---
    # 422：未结算测试不能生成；管理凭证错误 403
    st, _ = req("POST", f"/api/tests/{bal['test_code']}/shares",
                {"token": bal["token"], "ttl_seconds": 3600})
    check("未结算实验生成分享凭证被拒 (409)", st == 409, str(st))
    st, _ = req("POST", f"/api/tests/{t3['test_code']}/shares",
                {"token": "wrong", "ttl_seconds": 3600})
    check("分享凭证管理令牌错误 403", st == 403)
    st, _ = req("POST", "/api/tests/NOPE9999/shares",
                {"token": "x", "ttl_seconds": 3600})
    check("不存在实验生成分享凭证 404", st == 404)
    # 有效期边界：0、超过 7 天被 pydantic 拒绝（422）
    st, _ = req("POST", f"/api/tests/{t3['test_code']}/shares",
                {"token": t3["token"], "ttl_seconds": 0})
    check("有效期 0 秒被拒 (422)", st == 422)
    st, _ = req("POST", f"/api/tests/{t3['test_code']}/shares",
                {"token": t3["token"], "ttl_seconds": 7 * 24 * 3600 + 1})
    check("有效期超过 7 天被拒 (422)", st == 422)

    # 成功生成（t3：4/4 全对，可辨）
    st, sh = req("POST", f"/api/tests/{t3['test_code']}/shares",
                 {"token": t3["token"], "ttl_seconds": 3600, "note": " 合作方A "})
    check("已结算实验生成分享凭证 200",
          st == 200 and len(sh["share_code"]) == 16 and sh["reused"] is False
          and sh["read_url"] == f"/api/shares/{sh['share_code']}"
          and sh["note"] == "合作方A"
          and sh["expires_at"] - sh["created_at"] == 3600_000, str(sh))
    share_code = sh["share_code"]

    # 读取：无管理凭证，只返回脱敏字段
    st, rd = req("GET", f"/api/shares/{share_code}")
    check("凭分享凭证读取 200（无需令牌）", st == 200, str(rd))
    allowed = {"sample_a", "sample_b", "deadline", "settled_at", "valid_count",
               "correct_count", "abstained_count", "missing_count", "p_value",
               "distinguishable", "conclusion", "now"}
    check("读取载荷字段严格等于白名单", set(rd.keys()) == allowed, str(rd.keys()))
    check("读取结果与冻结结算一致（4/4、p=1/81、可辨）",
          rd["sample_a"] == "K-1" and rd["sample_b"] == "K-2"
          and rd["deadline"] == rep3["deadline"]
          and rd["settled_at"] == rep3["settled_at"]
          and rd["valid_count"] == 4 and rd["correct_count"] == 4
          and rd["abstained_count"] == 0 and rd["missing_count"] == 0
          and abs(rd["p_value"] - rep3["p_value"]) < 1e-15
          and rd["distinguishable"] is True, str(rd))
    blob = json.dumps(rd, ensure_ascii=False)
    for forbidden in ("panelist", "code", "odd_pos", "odd_sample",
                      "answer", "评员", "盲评码", "q1"):
        check(f"脱敏读取不含 {forbidden!r}", forbidden not in blob, blob)

    # 幂等：同实验同备注（归一化）同有效期 → 原凭证 reused=true
    st, sh_dup = req("POST", f"/api/tests/{t3['test_code']}/shares",
                     {"token": t3["token"], "ttl_seconds": 3600,
                      "note": "合作方A"})
    check("同备注同有效期重复请求返回原凭证",
          st == 200 and sh_dup["reused"] is True
          and sh_dup["share_code"] == share_code, str(sh_dup))
    # 同备注不同有效期 → 409 冲突
    st, cf1 = req("POST", f"/api/tests/{t3['test_code']}/shares",
                  {"token": t3["token"], "ttl_seconds": 7200, "note": "合作方A"})
    check("同备注不同有效期冲突 (409)", st == 409 and "冲突" in cf1["detail"],
          str(cf1))
    # 不同备注同有效期 → 新凭证
    st, sh_b = req("POST", f"/api/tests/{t3['test_code']}/shares",
                   {"token": t3["token"], "ttl_seconds": 3600, "note": "公示"})
    check("不同备注生成独立凭证",
          st == 200 and sh_b["reused"] is False
          and sh_b["share_code"] != share_code, str(sh_b))
    # 无备注（None 与空串等价）也可去重
    st, sh_n = req("POST", f"/api/tests/{t3['test_code']}/shares",
                   {"token": t3["token"], "ttl_seconds": 86400})
    st, sh_n2 = req("POST", f"/api/tests/{t3['test_code']}/shares",
                    {"token": t3["token"], "ttl_seconds": 86400, "note": "   "})
    check("无备注与空白备注归一化为同一凭证",
          st == 200 and sh_n2["reused"] is True
          and sh_n2["share_code"] == sh_n["share_code"], str((sh_n, sh_n2)))

    # 撤销：负责人凭证；错误路径 404/403
    st, _ = req("POST",
                f"/api/tests/{t3['test_code']}/shares/{share_code}/revoke",
                {"token": "wrong"})
    check("撤销凭证错误令牌 403", st == 403)
    st, _ = req("POST",
                f"/api/tests/{t2['test_code']}/shares/{share_code}/revoke",
                {"token": t2["token"]})
    check("跨实验撤销凭证 404", st == 404)
    st, _ = req("POST",
                f"/api/tests/{t3['test_code']}/shares/NOOOOOOOOOOOOOOO/revoke",
                {"token": t3["token"]})
    check("撤销不存在凭证 404", st == 404)
    st, rv = req("POST",
                 f"/api/tests/{t3['test_code']}/shares/{share_code}/revoke",
                 {"token": t3["token"]})
    check("到期前撤销 200", st == 200 and rv["revoked"] is True
          and rv["active"] is False, str(rv))
    st, rd_rev = req("GET", f"/api/shares/{share_code}")
    check("撤销后读取被拒 (403)",
          st == 403 and "撤销" in rd_rev["detail"], str(rd_rev))
    st, _ = req("POST",
                f"/api/tests/{t3['test_code']}/shares/{share_code}/revoke",
                {"token": t3["token"]})
    check("重复撤销被拒 (409)", st == 409)
    # 被撤销后同备注同有效期仍返回原凭证（不新发）
    st, sh_after = req("POST", f"/api/tests/{t3['test_code']}/shares",
                       {"token": t3["token"], "ttl_seconds": 3600,
                        "note": "合作方A"})
    check("撤销后同参数请求仍返回原凭证（reused）",
          st == 200 and sh_after["reused"] is True
          and sh_after["share_code"] == share_code
          and sh_after["active"] is False, str(sh_after))
    # 无凭证 / 伪造凭证
    st, rd404 = req("GET", "/api/shares/ZZZZZZZZZZZZZZZZ")
    check("未知分享凭证读取 404", st == 404 and "无效" in rd404["detail"],
          str(rd404))

    # 到期拒绝：1 秒有效期凭证，等过期后读取 403，撤销也 409
    st, sh_t = req("POST", f"/api/tests/{t3['test_code']}/shares",
                   {"token": t3["token"], "ttl_seconds": 1, "note": "临时"})
    assert st == 200
    time.sleep(2.0)
    st, rd_exp = req("GET", f"/api/shares/{sh_t['share_code']}")
    check("到期后读取被拒 (403)",
          st == 403 and "过期" in rd_exp["detail"], str(rd_exp))
    st, _ = req("POST",
                f"/api/tests/{t3['test_code']}/shares/{sh_t['share_code']}/revoke",
                {"token": t3["token"]})
    check("到期凭证撤销被拒 (409)", st == 409)

    # 时间线：成功生成/撤销各一笔 share 事件，失败操作不留痕；不泄露完整凭证
    s, sht = req("GET", f"/api/tests/{t3['test_code']}/timeline?token={t3['token']}")
    creates = [e for e in sht["events"] if e["type"] == "share_create"]
    revokes = [e for e in sht["events"] if e["type"] == "share_revoke"]
    check("时间线：成功生成各一笔 share_create（合作方A/公示/无备注/临时 = 4）",
          len(creates) == 4, str([e["payload"]["note"] for e in creates]))
    check("时间线：成功撤销一笔 share_revoke", len(revokes) == 1,
          str(len(revokes)))
    check("share 事件含备注/有效期/到期时间但不含完整凭证明文",
          all({"note", "ttl_seconds", "expires_at"} <= set(e["payload"])
              and share_code not in json.dumps(e["payload"], ensure_ascii=False)
              and sh_b["share_code"] not in json.dumps(e["payload"], ensure_ascii=False)
              and sh_n["share_code"] not in json.dumps(e["payload"], ensure_ascii=False)
              for e in creates + revokes),
          str(creates + revokes))
    n_before_sh = len(sht["events"])
    req("POST", f"/api/tests/{t3['test_code']}/shares",
        {"token": t3["token"], "ttl_seconds": 0})                    # 422
    req("POST", f"/api/tests/{t3['test_code']}/shares",
        {"token": t3["token"], "ttl_seconds": 7200, "note": "合作方A"})  # 409
    req("POST", f"/api/tests/{t3['test_code']}/shares",
        {"token": "wrong", "ttl_seconds": 7200})                     # 403
    req("POST",
        f"/api/tests/{t3['test_code']}/shares/{share_code}/revoke",
        {"token": t3["token"]})                                      # 409 重复撤销
    s, sht2 = req("GET", f"/api/tests/{t3['test_code']}/timeline?token={t3['token']}")
    check("失败的生成/撤销不留下任何记录", len(sht2["events"]) == n_before_sh,
          f"{n_before_sh} -> {len(sht2['events'])}")

    # 管理台进度接口（纯增量）列出全部凭证及状态
    s, prog = req("GET", f"/api/tests/{t3['test_code']}?token={t3['token']}")
    check("进度接口含 shares 列表（纯增量、仅负责人可见）",
          s == 200 and len(prog["shares"]) == 4
          and all({"share_code", "read_url", "note", "ttl_seconds",
                   "created_at", "expires_at", "revoked_at", "active",
                   "revoked"} <= set(x) for x in prog["shares"]),
          str(prog.get("shares")))
    by_note = {x["note"]: x for x in prog["shares"]}
    check("进度接口中合作方A凭证已撤销、临时凭证已过期、公示凭证有效",
          by_note["合作方A"]["revoked"] is True
          and by_note["临时"]["active"] is False
          and by_note["公示"]["active"] is True, str(by_note))

    # 旧库已结算实验同样可以生成/读取（迁移兼容）
    st, lsh = req("POST", f"/api/tests/{legacy_code}/shares",
                  {"token": legacy_token, "ttl_seconds": 3600, "note": "legacy"})
    check("旧库已结算实验可生成分享凭证", st == 200 and lsh["reused"] is False,
          str(lsh))
    st, lrd = req("GET", f"/api/shares/{lsh['share_code']}")
    check("旧库凭证读取脱敏结果（1/1 答对）",
          st == 200 and lrd["valid_count"] == 1 and lrd["correct_count"] == 1
          and "老评员" not in json.dumps(lrd, ensure_ascii=False)
          and "LEGACYCODE1" not in json.dumps(lrd), str(lrd))

    # --- 2.10 负责人提前结束收集 ---
    s, ec = req("POST", "/api/tests", {
        "sample_a": "H-1", "sample_b": "H-2",
        "panelists": [f"h{i}" for i in range(8)],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 4})
    check("提前结束：创建 8 人测试", s == 200, str(ec))
    ec_rows = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (ec["test_code"],)).fetchall()
    # 前 3 人答对，第 4 人弃权，其余未交
    req("POST", f"/api/eval/{ec_rows[0]['code']}/submit",
        {"answer": ec_rows[0]["odd_pos"]})
    req("POST", f"/api/eval/{ec_rows[1]['code']}/submit",
        {"answer": ec_rows[1]["odd_pos"]})
    req("POST", f"/api/eval/{ec_rows[2]['code']}/submit",
        {"answer": ec_rows[2]["odd_pos"]})
    req("POST", f"/api/eval/{ec_rows[3]['code']}/submit", {"abstain": True})

    # 原因校验：空（含纯空白）/ 超 200 字 → 422
    st, er0 = req("POST", f"/api/tests/{ec['test_code']}/end",
                  {"token": ec["token"], "reason": "   "})
    check("提前结束：空白原因被拒 (422)", st == 422, str(er0))
    st, erl = req("POST", f"/api/tests/{ec['test_code']}/end",
                  {"token": ec["token"], "reason": "原" * 201})
    check("提前结束：超 200 字原因被拒 (422)", st == 422, str(erl))
    st, _ = req("POST", f"/api/tests/{ec['test_code']}/end",
                {"token": ec["token"]})
    check("提前结束：缺字段被拒 (422)", st == 422)
    st, _ = req("POST", f"/api/tests/NOPE9999/end",
                {"token": "x", "reason": "r"})
    check("提前结束：不存在测试 404", st == 404)
    st, _ = req("POST", f"/api/tests/{ec['test_code']}/end",
                {"token": "wrong", "reason": "r"})
    check("提前结束：错误令牌 403", st == 403)
    # 校验失败不留结束痕迹
    s, ec_prog0 = req("GET", f"/api/tests/{ec['test_code']}?token={ec['token']}")
    check("提前结束：被拒后实验仍未结束",
          ec_prog0["ended"] is False and ec_prog0["ended_at"] is None)

    # 成功结束（原因两端带空白，服务端 trim；恰 200 字允许）
    reason200 = "足" * 200
    st, endr = req("POST", f"/api/tests/{ec['test_code']}/end",
                   {"token": ec["token"], "reason": f"  {reason200}  "})
    check("提前结束：200 字原因 200（trim 存储）",
          st == 200 and endr["ok"] is True and endr["reused"] is False
          and endr["ended"] is True and endr["end_reason"] == reason200
          and endr["ended_at"] <= int(time.time() * 1000),
          str(endr))

    # 名单与答卷入口立即冻结
    st, sub_denied = req("POST", f"/api/eval/{ec_rows[4]['code']}/submit",
                         {"answer": 1})
    check("提前结束后：未交评员提交被拒 (403)",
          st == 403 and "结束" in sub_denied["detail"], str(sub_denied))
    st, adj_denied = req("POST", f"/api/tests/{ec['test_code']}/adjust",
                         {"token": ec["token"],
                          "withdraw_codes": [ec_rows[4]["code"]]})
    check("提前结束后：撤回被拒 (409)",
          st == 409 and "结束" in adj_denied["detail"], str(adj_denied))
    st, add_denied = req("POST", f"/api/tests/{ec['test_code']}/adjust",
                         {"token": ec["token"], "add_panelists": ["hx"]})
    check("提前结束后：补位被拒 (409)", st == 409, str(add_denied))
    # 被冻结的未交码仍可访问（不是 withdrawn），但视图为已结束且不泄露分配
    st, ev_ended = req("GET", f"/api/eval/{ec_rows[4]['code']}")
    check("提前结束后：评员视图标记 ended、含原因",
          st == 200 and ev_ended["ended"] is True
          and ev_ended["ended_at"] == endr["ended_at"]
          and ev_ended["end_reason"] == reason200
          and ev_ended["submitted"] is False and ev_ended["settled"] is False,
          str(ev_ended))
    check("提前结束未结算前不泄露 odd_pos/odd_sample",
          "odd_pos" not in ev_ended and "odd_sample" not in ev_ended
          and "sample_a" not in ev_ended, str(ev_ended))
    # 已交评员视图：submitted 优先，仍标记 ended
    st, ev_done = req("GET", f"/api/eval/{ec_rows[0]['code']}")
    check("提前结束后：已交评员保留已提交状态",
          st == 200 and ev_done["submitted"] is True and ev_done["ended"] is True)

    # 重复结束返回同一结束记录（reused=true，同一 ended_at），不产生第二条事件
    st, endr2 = req("POST", f"/api/tests/{ec['test_code']}/end",
                    {"token": ec["token"], "reason": "另一个原因也应幂等"})
    check("提前结束：重复结束返回同一结束记录",
          st == 200 and endr2["reused"] is True and endr2["ok"] is False
          and endr2["ended_at"] == endr["ended_at"]
          and endr2["end_reason"] == reason200, str(endr2))

    # 结束后立即结算（截止时间仍在未来），规则不变：有效 3、弃权 1、未交 4、min=4
    st, ecrep = req("POST", f"/api/tests/{ec['test_code']}/settle",
                    {"token": ec["token"]})
    check("提前结束后可立即结算 (200)", st == 200, str(ecrep))
    check("提前结束结算：有效=3/答对=3/弃权=1/未交=4，样本不足不可辨",
          ecrep["valid_count"] == 3 and ecrep["correct_count"] == 3
          and ecrep["abstained_count"] == 1 and ecrep["missing_count"] == 4
          and ecrep["distinguishable"] is False
          and "样本不足" in ecrep["conclusion"], str(ecrep))
    check("提前结束结算：p=(1/3)^3≈0.037",
          abs(ecrep["p_value"] - 1 / 27) < 1e-9, str(ecrep["p_value"]))
    check("提前结束结算：响应带结束信息",
          ecrep["ended"] is True and ecrep["ended_at"] == endr["ended_at"]
          and ecrep["end_reason"] == reason200, str(ecrep))
    st, ecrep2 = req("POST", f"/api/tests/{ec['test_code']}/settle",
                     {"token": ec["token"]})
    check("提前结束后重复结算仍返回冻结结果",
          st == 200 and ecrep2["settled_at"] == ecrep["settled_at"]
          and ecrep2["valid_count"] == 3)
    # 已结算（曾提前结束）后再结束：幂等返回同一结束记录
    st, endr3 = req("POST", f"/api/tests/{ec['test_code']}/end",
                    {"token": ec["token"], "reason": "r"})
    check("提前结束：结算后重复结束仍返回同一结束记录",
          st == 200 and endr3["reused"] is True
          and endr3["ended_at"] == endr["ended_at"]
          and endr3["settled"] is True, str(endr3))

    # 时间线：create/answer×4/end/settle，end 事件记录原因与服务端时间
    s, ectl = req("GET", f"/api/tests/{ec['test_code']}/timeline?token={ec['token']}")
    etypes = [e["type"] for e in ectl["events"]]
    check("提前结束：时间线类型序列含唯一 end（位于 answer 后、settle 前）",
          etypes[0] == "create" and etypes.count("end") == 1
          and etypes.count("answer") == 4
          and etypes.index("end") > etypes.index("answer")
          and etypes.index("end") < etypes.index("settle"),
          str(etypes))
    end_ev = [e for e in ectl["events"] if e["type"] == "end"][0]
    check("提前结束：end 事件记录结束原因与服务端时间（ts=ended_at）",
          end_ev["payload"]["reason"] == reason200
          and end_ev["ts"] == endr["ended_at"]
          and end_ev["payload"]["deadline"] == ec_prog0["deadline"],
          str(end_ev))
    settle_ev = [e for e in ectl["events"] if e["type"] == "settle"][0]
    check("提前结束：结算事件标记 settled_after_end",
          settle_ev["payload"].get("settled_after_end") is True,
          str(settle_ev["payload"]))
    check("提前结束：时间线响应含结束状态字段",
          ectl["ended"] is True and ectl["ended_at"] == endr["ended_at"]
          and ectl["end_reason"] == reason200)

    # 已过截止（未提前结束）的实验不能再 end：建短截止测试等过点
    s, ed = req("POST", "/api/tests", {
        "sample_a": "HD-1", "sample_b": "HD-2",
        "panelists": ["d1"],
        "deadline": int(time.time() * 1000) + 1000, "min_valid": 1})
    assert s == 200
    time.sleep(1.6)
    st, ede = req("POST", f"/api/tests/{ed['test_code']}/end",
                  {"token": ed["token"], "reason": "晚了"})
    check("提前结束：过截止后结束被拒 (409)，保持原截止行为",
          st == 409 and "截止" in ede["detail"], str(ede))
    st, edp = req("GET", f"/api/tests/{ed['test_code']}?token={ed['token']}")
    check("提前结束：未结束实验保持原截止行为（ended=false）",
          st == 200 and edp["ended"] is False and edp["ended_at"] is None)
    # 截止后正常结算不受影响
    st, _ = req("POST", f"/api/tests/{ed['test_code']}/settle",
                {"token": ed["token"]})
    check("提前结束：未结束实验到期后正常结算", st == 200)

    # --- 2.11 并发：结束收集与答卷竞争，按先取得写锁者生效 ---
    s, rc = req("POST", "/api/tests", {
        "sample_a": "G-1", "sample_b": "G-2",
        "panelists": [f"g{i}" for i in range(30)],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    rc_rows = con.execute(
        "SELECT code FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (rc["test_code"],)).fetchall()

    def end_submit_race(c):
        result = {}
        barrier = threading.Barrier(2)

        def do_submit():
            barrier.wait()
            stt, body = req("POST", f"/api/eval/{c}/submit", {"answer": 1})
            result["submit"] = (stt, body)

        def do_end():
            barrier.wait()
            stt, body = req("POST", f"/api/tests/{rc['test_code']}/end",
                            {"token": rc["token"], "reason": "并发结束原因"})
            result["end"] = (stt, body)

        t1 = threading.Thread(target=do_submit)
        t2 = threading.Thread(target=do_end)
        t1.start(); t2.start(); t1.join(); t2.join()
        return result

    race2_ok = no_5xx2 = True
    n_answer_win = n_end_win = 0
    for i in range(10):
        c = rc_rows[i]["code"]
        r = end_submit_race(c)
        sc, sbody = r["submit"]
        ec_st, ebody = r["end"]
        if ec_st >= 500 or sc >= 500:
            no_5xx2 = False
        row = con.execute(
            "SELECT answer FROM assignments WHERE code=?", (c,)).fetchone()
        # 结束先拿到写锁：end=200；该码答卷必为 403（已结束），且无答案
        if ec_st == 200 and sc == 403 and "结束" in sbody["detail"]:
            if row["answer"] is not None:
                race2_ok = False
            n_end_win += 1
        # 答卷先拿到写锁：submit=200；结束看到已结束记录返回 200 reused（首码之后），
        # 或首个码上 end=200 ok——无论如何答案必须落库，且全库仅有一条 end 事件
        elif sc == 200 and ec_st == 200 and row["answer"] == 1:
            n_answer_win += 1
        else:
            race2_ok = False

    end_count = con.execute(
        "SELECT COUNT(*) AS c FROM events WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND type='end'",
        (rc["test_code"],)).fetchone()["c"]
    tstate = con.execute(
        "SELECT ended_at, end_reason FROM tests WHERE code=?",
        (rc["test_code"],)).fetchone()
    check("并发：结束与答卷结果互补、状态与胜出方一致，无脏中间态",
          race2_ok and n_answer_win + n_end_win == 10 and end_count == 1
          and tstate["ended_at"] is not None
          and tstate["end_reason"] == "并发结束原因",
          f"答卷先完成 {n_answer_win} 轮，结束先完成 {n_end_win} 轮")
    check("并发：结束/答卷竞争过程中无 5xx", no_5xx2)

    # 结束生效后，其余所有未交码提交一律 403（冻结彻底）
    later_denied = all(
        req("POST", f"/api/eval/{rc_rows[i]['code']}/submit", {"answer": 1})[0]
        == 403
        for i in range(10, 15))
    check("并发：结束落库后后续答卷全部 403", later_denied)
    # 时间线：end 事件之后不再有任何 answer 事件
    s, rctl = req("GET", f"/api/tests/{rc['test_code']}/timeline?token={rc['token']}")
    end_seq = [e["seq"] for e in rctl["events"] if e["type"] == "end"][0]
    answers_after_end = [e for e in rctl["events"]
                         if e["type"] == "answer" and e["seq"] > end_seq]
    answer_wins_in_db = con.execute(
        "SELECT COUNT(*) AS c FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND answer IS NOT NULL",
        (rc["test_code"],)).fetchone()["c"]
    check("并发：先于写锁完成的答卷数与 end 前 answer 事件数一致，end 后无答卷",
          not answers_after_end
          and sum(1 for e in rctl["events"] if e["type"] == "answer")
              == answer_wins_in_db
          and answer_wins_in_db == n_answer_win,
          f"answer_wins={n_answer_win}, db_answers={answer_wins_in_db}")
    # 结束后立即结算被允许，且只冻结先完成的答卷
    st, rcrep = req("POST", f"/api/tests/{rc['test_code']}/settle",
                    {"token": rc["token"]})
    check("并发：结束后立即结算，冻结先完成的答卷",
          st == 200 and rcrep["valid_count"] == n_answer_win
          and rcrep["missing_count"] == 30 - n_answer_win, str(rcrep))

    # --- 2.12 两场复测配对对照（精确双侧配对二项检验 / McNemar 精确检验） ---
    def make_settled(sample_a, sample_b, panelists, plans, code_hint=""):
        """创建一场实验、按 plans（与 panelists 同序）提交并立即结算。

        plans 每项：correct / wrong / abstain / missing。
        返回 (compare 所需 (code, token), 按姓名索引的 assignment rows)。
        """
        s, tt = req("POST", "/api/tests", {
            "sample_a": sample_a, "sample_b": sample_b,
            "panelists": panelists,
            "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
        assert s == 200, (code_hint, tt)
        rows = con.execute(
            "SELECT * FROM assignments WHERE test_id="
            "(SELECT id FROM tests WHERE code=?) ORDER BY id",
            (tt["test_code"],)).fetchall()
        assert len(rows) == len(panelists)
        for r, plan in zip(rows, plans):
            if plan == "missing":
                continue
            if plan == "abstain":
                stt, _ = req("POST", f"/api/eval/{r['code']}/submit",
                             {"abstain": True})
            else:
                ans = r["odd_pos"] if plan == "correct" else (r["odd_pos"] % 3 + 1)
                stt, _ = req("POST", f"/api/eval/{r['code']}/submit",
                             {"answer": ans})
            assert stt == 200, (code_hint, r["panelist"], plan)
        con.execute("UPDATE tests SET deadline=? WHERE code=?",
                    (int(time.time()) - 10, tt["test_code"]))
        con.commit()
        stt, rep = req("POST", f"/api/tests/{tt['test_code']}/settle",
                       {"token": tt["token"]})
        assert stt == 200 and rep["settled_at"] is not None, (code_hint, rep)
        by_name = {r["panelist"]: r for r in rows}
        return (tt["test_code"], tt["token"]), by_name

    def compare(c1, tok1, c2, tok2):
        return req("POST", "/api/compare", {
            "first_code": c1, "first_token": tok1,
            "second_code": c2, "second_token": tok2})

    compare_allowed = {
        "first_test_code", "second_test_code", "first_samples", "second_samples",
        "samples_swapped", "unpaired_count", "paired_invalid_count",
        "valid_pair_count", "both_correct_count", "first_only_correct_count",
        "second_only_correct_count", "both_wrong_count",
        "p_value", "different", "conclusion", "now"}

    # 场景 A：同名单 10 人，覆盖全部配对类别
    names_a = [f"a{i}" for i in range(10)]
    plans_a1 = ["correct"] * 2 + ["wrong"] * 2 + ["correct"] * 2 + ["wrong"] * 2 \
        + ["abstain", "missing"]
    plans_a2 = ["correct"] * 2 + ["wrong"] * 2 + ["wrong"] * 2 + ["correct"] * 2 \
        + ["missing", "abstain"]
    A1, _ = make_settled("茉莉", "玫瑰", names_a, plans_a1, "A1")
    A2, _ = make_settled("茉莉", "玫瑰", names_a, plans_a2, "A2")
    s, cmp_a = compare(*A1, *A2)
    check("配对：同名单同序对照 200", s == 200, str(cmp_a))
    check("配对：未配对=0/配对无效=2（弃权+未交）",
          cmp_a["unpaired_count"] == 0 and cmp_a["paired_invalid_count"] == 2
          and cmp_a["valid_pair_count"] == 8, str(cmp_a))
    check("配对：都对2/仅首场对2/仅次场对2/都错2",
          cmp_a["both_correct_count"] == 2
          and cmp_a["first_only_correct_count"] == 2
          and cmp_a["second_only_correct_count"] == 2
          and cmp_a["both_wrong_count"] == 2, str(cmp_a))
    check("配对：n=4 各半 p=1.0000 无差异",
          abs(cmp_a["p_value"] - 1.0) < 1e-12 and cmp_a["different"] is False,
          str(cmp_a["p_value"]))
    check("配对：响应字段严格等于白名单（无个人答案/杯序/盲评码）",
          set(cmp_a.keys()) == compare_allowed, str(cmp_a.keys()))
    blob_a = json.dumps(cmp_a, ensure_ascii=False)
    for forbidden in ("odd_pos", "odd_sample", "盲评码", "answer", "code\""):
        check(f"配对：响应不含 {forbidden!r}",
              ("\"code\"" if forbidden == 'code"' else forbidden) not in blob_a,
              blob_a)
    check("配对：响应不含任何评员姓名",
          all(n not in blob_a for n in names_a), blob_a)

    # 场景 B：名单部分不同 + A/B 顺序交换 + 6:0 显著（次场提升）
    names_b1 = [f"b{i}" for i in range(8)] + ["x1", "x2"]
    names_b2 = [f"b{i}" for i in range(8)] + ["y1", "y2"]
    # b0..b5 首场错次场对；b6,b7 两场都错；x* 仅首场；y* 仅次场
    plans_b1 = ["wrong"] * 6 + ["wrong", "wrong", "correct", "correct"]
    plans_b2 = ["correct"] * 6 + ["wrong", "wrong", "correct", "correct"]
    B1, _ = make_settled("茉莉", "玫瑰", names_b1, plans_b1, "B1")
    B2, _ = make_settled("玫瑰", "茉莉", names_b2, plans_b2, "B2")
    s, cmp_b = compare(*B1, *B2)
    check("配对：A/B 顺序交换合法 200 且 samples_swapped=true",
          s == 200 and cmp_b["samples_swapped"] is True, str(cmp_b))
    check("配对：未配对=4（x1,x2,y1,y2）", cmp_b["unpaired_count"] == 4,
          str(cmp_b["unpaired_count"]))
    check("配对：有效配对=8：仅次场对6/都错2",
          cmp_b["valid_pair_count"] == 8
          and cmp_b["second_only_correct_count"] == 6
          and cmp_b["both_wrong_count"] == 2
          and cmp_b["first_only_correct_count"] == 0
          and cmp_b["both_correct_count"] == 0, str(cmp_b))
    check("配对：n=6 全同向 p=0.03125 ≤ 0.05 标记有差异",
          abs(cmp_b["p_value"] - 0.03125) < 1e-12
          and cmp_b["different"] is True, str(cmp_b["p_value"]))
    check("配对：结论说明复测提升", "提升" in cmp_b["conclusion"],
          cmp_b["conclusion"])
    # 交换首/次场：仍显著，但方向变为“下降”，p 相同
    s, cmp_br = compare(*B2, *B1)
    check("配对：交换场序 p 相同，方向变为仅首场对 6",
          s == 200 and abs(cmp_br["p_value"] - 0.03125) < 1e-12
          and cmp_br["first_only_correct_count"] == 6
          and cmp_br["second_only_correct_count"] == 0
          and cmp_br["samples_swapped"] is True
          and "下降" in cmp_br["conclusion"], str(cmp_br))

    # 差异人数为 0（全对/全错一致）→ p=1
    names_p = ["p1", "p2", "p3"]
    P1, _ = make_settled("茉莉", "玫瑰", names_p,
                         ["correct", "wrong", "correct"], "P1")
    P2, _ = make_settled("茉莉", "玫瑰", names_p,
                         ["correct", "wrong", "correct"], "P2")
    s, cmp_p = compare(*P1, *P2)
    check("配对：差异人数 0 时 p=1（无论有多少有效配对）",
          s == 200 and cmp_p["p_value"] == 1.0
          and cmp_p["different"] is False
          and cmp_p["valid_pair_count"] == 3
          and "无差异" in cmp_p["conclusion"], str(cmp_p))

    # 无有效配对：共同评员全部弃权/未交（外加各场独有者）→ p=null，无法比较
    names_n1 = ["n1", "n2", "onlyN"]
    names_n2 = ["n1", "n2", "onlyM"]
    N1, _ = make_settled("茉莉", "玫瑰", names_n1,
                         ["abstain", "missing", "correct"], "N1")
    N2, _ = make_settled("茉莉", "玫瑰", names_n2,
                         ["missing", "abstain", "correct"], "N2")
    s, cmp_n = compare(*N1, *N2)
    check("配对：无有效配对 200", s == 200, str(cmp_n))
    check("配对：未配对=2、配对无效=2、有效配对=0",
          cmp_n["unpaired_count"] == 2
          and cmp_n["paired_invalid_count"] == 2
          and cmp_n["valid_pair_count"] == 0, str(cmp_n))
    check("配对：无有效配对时 p_value=null 且 different=false",
          cmp_n["p_value"] is None and cmp_n["different"] is False, str(cmp_n))
    check("配对：结论明确无法比较", "无法" in cmp_n["conclusion"]
          and "比较" in cmp_n["conclusion"], cmp_n["conclusion"])

    # 撤回者只影响其所在场名单：p8 在首场被撤回（未提交），次场在名单且答对
    names_w = [f"w{i}" for i in range(8)]
    W2, w2_rows = make_settled("茉莉", "玫瑰", names_w,
                               ["correct"] * 8, "W2")
    s, w1t = req("POST", "/api/tests", {
        "sample_a": "茉莉", "sample_b": "玫瑰", "panelists": names_w,
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    w1_rows = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (w1t["test_code"],)).fetchall()
    # w0..w6 首场答错；w7 未交，在截止前撤回（故不在首场有效名单）
    for r in w1_rows[:7]:
        stt, _ = req("POST", f"/api/eval/{r['code']}/submit",
                     {"answer": r["odd_pos"] % 3 + 1})
        assert stt == 200
    stt, _ = req("POST", f"/api/tests/{w1t['test_code']}/adjust",
                 {"token": w1t["token"],
                  "withdraw_codes": [w1_rows[7]["code"]]})
    assert stt == 200
    con.execute("UPDATE tests SET deadline=? WHERE code=?",
                (int(time.time()) - 10, w1t["test_code"]))
    con.commit()
    stt, _ = req("POST", f"/api/tests/{w1t['test_code']}/settle",
                 {"token": w1t["token"]})
    assert stt == 200
    W1 = (w1t["test_code"], w1t["token"])
    s, cmp_w = compare(*W1, *W2)
    check("配对：首场撤回者不在名单→计未配对（unpaired=1）",
          s == 200 and cmp_w["unpaired_count"] == 1
          and cmp_w["paired_invalid_count"] == 0
          and cmp_w["valid_pair_count"] == 7
          and cmp_w["second_only_correct_count"] == 7
          and cmp_w["both_wrong_count"] == 0, str(cmp_w))

    # 评员姓名两端空白以库内归一化后的姓名同名配对（创建时即 trim 存储）
    names_s1 = ["  张三", "李四 ", " 王五 "]
    names_s2 = ["张三", " 李四", "王五"]
    S1, _ = make_settled("茉莉", "玫瑰", names_s1,
                         ["correct", "wrong", "correct"], "S1")
    S2, _ = make_settled("茉莉", "玫瑰", names_s2,
                         ["wrong", "wrong", "correct"], "S2")
    s, cmp_s = compare(*S1, *S2)
    check("配对：姓名去首尾空白后同名配对（unpaired=0）",
          s == 200 and cmp_s["unpaired_count"] == 0
          and cmp_s["valid_pair_count"] == 3
          and cmp_s["first_only_correct_count"] == 1
          and cmp_s["second_only_correct_count"] == 0, str(cmp_s))

    # ---- 准入拒绝矩阵 ----
    # 同一场与自身配对
    st, rej_self = compare(*A1, *A1)
    check("配对：与自身配对被拒 (422)", st == 422 and "不同" in rej_self["detail"],
          str(rej_self))
    # 令牌错误（分别鉴权）
    st, rej_t1 = compare(A1[0], "wrong-token", *A2)
    check("配对：首场令牌错误 403", st == 403, str(st))
    st, rej_t2 = compare(*A1, A2[0], "wrong-token")
    check("配对：次场令牌错误 403", st == 403, str(st))
    # 测试不存在：先验首场再验次场
    st, rej_404a = compare("NOPE1234", "x", *A2)
    check("配对：首场不存在 404", st == 404 and "首场" in rej_404a["detail"],
          str(rej_404a))
    st, rej_404b = compare(*A1, "NOPE1234", "x")
    check("配对：首场令牌通过后次场不存在 404", st == 404
          and "次场" in rej_404b["detail"], str(rej_404b))
    # 样本编号集合不同
    names_d = [f"d{i}" for i in range(3)]
    D1, _ = make_settled("茉莉", "桂花", names_d,
                         ["correct"] * 3, "D1")
    st, rej_smp = compare(*D1, *A2)
    check("配对：样本集合不同被拒 (422)", st == 422
          and "样本编号集合不同" in rej_smp["detail"], str(rej_smp))
    # 未结算：建一场不结算
    s, ut = req("POST", "/api/tests", {
        "sample_a": "茉莉", "sample_b": "玫瑰",
        "panelists": names_a,
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    st, rej_un1 = compare(ut["test_code"], ut["token"], *A2)
    check("配对：首场未结算 409", st == 409 and "首场" in rej_un1["detail"],
          str(rej_un1))
    st, rej_un2 = compare(*A1, ut["test_code"], ut["token"])
    check("配对：次场未结算 409", st == 409 and "次场" in rej_un2["detail"],
          str(rej_un2))
    # 未结算实验即便名单完全不同，也先报“未结算”（准入顺序）
    st, rej_order = compare(ut["test_code"], ut["token"], *D1)
    check("配对：准入顺序——未结算优先于样本/名单检查",
          st == 409 and "首场" in rej_order["detail"], str(rej_order))

    # 对照表是只读的：调用后两场结算冻结结果与时间线不受影响
    s, a1_rep = req("POST", f"/api/tests/{A1[0]}/settle", {"token": A1[1]})
    s, a2_rep = req("POST", f"/api/tests/{A2[0]}/settle", {"token": A2[1]})
    check("配对：对照后重复结算仍返回同一冻结结果",
          a1_rep["settled_at"] is not None and a2_rep["settled_at"] is not None)
    for code, tok in (A1, A2):
        s, tlx = req("GET", f"/api/tests/{code}/timeline?token={tok}")
        check(f"配对：{code} 时间线不新增 compare 事件",
              s == 200 and all(e["type"] != "compare" for e in tlx["events"]),
              str([e["type"] for e in tlx["events"]]))

    # --- 2.6 页面可达性 ---
    for path in ("/", "/admin", "/eval", f"/eval/code/{codes[0]}"):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            check(f"页面 {path} → 200", r.status == 200 and b"<!DOCTYPE html>" in r.read(100))

    con.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    server_log.close()

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("全部通过 ✓")
