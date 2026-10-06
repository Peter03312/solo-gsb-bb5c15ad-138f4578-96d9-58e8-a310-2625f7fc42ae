"""三角嗅辨盲评工作台 —— FastAPI 后端。

角色：
- 实验负责人：创建测试、凭管理码 + 令牌查看进度、截止后结算并回看分配与答卷；
  截止前可填写原因提前结束收集，立即冻结名单与答卷入口，结束后即可结算；
  可凭两场已结算实验的测试码与各自令牌做复测配对对照（只读汇总）。
- 评员：凭盲评码进入，截止前（且未被提前结束）仅可见三杯并提交一次（异样杯位置或弃权）。

安全要点：
- 评员接口任何时刻只返回“三杯随机展示”需要的信息；提交前不暴露 odd_pos/odd_sample。
- 截止判定以服务端时间为准；答卷、提前结束、名单调整与结算在同一行锁语义下竞争截止线。
- 管理令牌以 sha256 哈希存储，请求使用恒定时间比较。
"""

import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .stats import binomial_tail, paired_binomial_p

STATIC_DIR = "static"
ABSTAIN = -1
ACTIVE = "active"
WITHDRAWN = "withdrawn"
MAX_PANELISTS = 500
MAX_END_REASON_LEN = 200  # 负责人提前结束收集的原因长度：1～200 字
MAX_SHARE_TTL_SECONDS = 7 * 24 * 3600  # 脱敏结果分享凭证最长有效期：7 天
MAX_SHARE_NOTE_LEN = 200


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_ts() -> int:
    return int(time.time())


def new_code(n: int = 10) -> str:
    # 去除易混字符的 URL 安全随机码
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def hash_token(test_code: str, token: str) -> str:
    return hashlib.sha256(f"{test_code}:{token}".encode()).hexdigest()


def verify_admin(row, token: str):
    if not token or not secrets.compare_digest(
        hash_token(row["code"], token), row["token_hash"]
    ):
        raise HTTPException(status_code=403, detail="管理令牌无效")


def require_admin(test_code: str, token: str):
    conn = db.read_conn()
    try:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="测试不存在")
    verify_admin(row, token)
    return row


def assign_cups(n: int):
    """为 n 名评员分配异样杯位置与异样样本，两者都在全体中尽量均衡。

    - odd_pos：1/2/3 循环打散；odd_sample：A/B 交替，二者独立随机起始。
      => 三杯呈现给评员的映射逐人不同且全体均衡。
    """
    positions = [i % 3 + 1 for i in range(n)]
    samples = ["A" if i % 2 == 0 else "B" for i in range(n)]
    # 随机轮转起始相位并切块打乱，保持计数均衡且顺序不可预测
    shift_p = secrets.randbelow(3)
    positions = positions[shift_p:] + positions[:shift_p]
    shift_s = secrets.randbelow(2)
    samples = samples[shift_s:] + samples[:shift_s]
    order = list(range(n))
    secrets.SystemRandom().shuffle(order)
    result = [None] * n
    for idx, pi in enumerate(order):
        result[pi] = (positions[idx], samples[idx])
    return result


def pick_balanced(c_pos: dict, c_sample: dict):
    """在当前有效名单计数上选 (odd_pos, odd_sample)，使位置与样本计数尽量均衡。

    两维独立贪心：始终挑当前计数最少的那一档；同档随机打破平局。
    逐人插入后，各位置计数极差 ≤ 1、异样样本 A/B 计数极差 ≤ 1。
    """
    pos = min((1, 2, 3), key=lambda p: (c_pos[p], secrets.randbelow(3)))
    sample = min(
        ("A", "B"), key=lambda s: (c_sample[s], secrets.randbelow(2))
    )
    c_pos[pos] += 1
    c_sample[sample] += 1
    return pos, sample


def assignment_public(a) -> dict:
    """评员可见：截止/结算前绝不暴露异样杯对应关系。"""
    return {
        "panelist": a["panelist"],
        "submitted": a["answer"] is not None,
        "abstained": a["answer"] == ABSTAIN,
        "answered_at": a["answered_at"],
    }


def assignment_detail(a) -> dict:
    """负责人回看：含分配与答卷。"""
    return {
        "panelist": a["panelist"],
        "code": a["code"],
        "odd_pos": a["odd_pos"],
        "odd_sample": a["odd_sample"],
        "answer": None if a["answer"] is None else
                  ("弃权" if a["answer"] == ABSTAIN else a["answer"]),
        "correct": (
            None
            if a["answer"] in (None, ABSTAIN)
            else a["answer"] == a["odd_pos"]
        ),
        "answered_at": a["answered_at"],
    }


def settlement_conclusion(frozen) -> str:
    """结算结论文案（settle 接口与时间线 settle 事件共用，保持一致）。"""
    if frozen["valid_count"] < frozen["min_valid"]:
        return (
            f"有效答卷 {frozen['valid_count']} 份，少于最少要求 "
            f"{frozen['min_valid']} 份：样本不足，无法判定。"
        )
    if frozen["p_value"] is not None and frozen["p_value"] > 0.05:
        return (
            f"有效答卷 {frozen['valid_count']} 份、答对 {frozen['correct_count']} 份，"
            f"单侧尾概率 p={frozen['p_value']:.4f} > 0.05：证据未达阈值，"
            "不能判为可辨（差异不显著）。"
        )
    return (
        f"有效答卷 {frozen['valid_count']} 份、答对 {frozen['correct_count']} 份，"
        f"单侧尾概率 p={frozen['p_value']:.4f} ≤ 0.05：判为可辨。"
    )


def compare_conclusion(valid_pairs: int, both_correct: int, only_first: int,
                       only_second: int, both_wrong: int,
                       p: Optional[float]) -> str:
    """复测配对对照结论文案（compare 接口使用）。"""
    if valid_pairs == 0:
        return ("两场实验中同名且双方均提交 1～3 杯答案的有效配对为 0 人，"
                "无法比较复测前后辨别表现是否变化。")
    head = (
        f"有效配对 {valid_pairs} 人：双方都对 {both_correct} 人、"
        f"仅首场对 {only_first} 人、仅次场对 {only_second} 人、"
        f"都错 {both_wrong} 人；"
    )
    if p <= 0.05:
        return (
            head + f"精确双侧配对二项检验 p={p:.4f} ≤ 0.05："
            "判为复测前后辨别表现有差异。"
        )
    return (
        head + f"精确双侧配对二项检验 p={p:.4f} > 0.05："
        "证据未达阈值，不能判为辨别表现有变化。"
    )


def render_event(ev: dict, settled: bool, assign_by_code: dict) -> dict:
    """把一条原始事件裁剪为负责人可见的时间线条目。

    盲态规则：settled 之前，answer 事件只呈现“已答 / 弃权”，
    不透露选杯、异样杯及对错；settled 之后才透出对应答案与判定。
    create / adjust / end / settle / history_start 不含盲态答案，按原样呈现。
    """
    payload = json.loads(ev["payload"] or "{}")
    item = {
        "seq": ev["id"],
        "ts": ev["ts"] * 1000,
        "type": ev["type"],
        "payload": payload,
    }
    if ev["type"] == "answer":
        a = assign_by_code.get(payload.get("code"))
        if not settled:
            # 结算前：仅状态（已答 / 弃权），剔除选杯与判定
            payload = {
                "panelist": payload.get("panelist"),
                "code": payload.get("code"),
                "abstained": bool(payload.get("abstained")),
                "submitted": True,
            }
        else:
            payload = dict(payload)
            if a is not None:
                payload["odd_pos"] = a["odd_pos"]
                payload["odd_sample"] = a["odd_sample"]
                if not payload.get("abstained"):
                    payload["correct"] = payload.get("answer") == a["odd_pos"]
        item["payload"] = payload
    return item


# ---------------------------------------------------------------------------
# 脱敏结果分享凭证
# ---------------------------------------------------------------------------

def share_admin_json(s, now: int) -> dict:
    """负责人视角的凭证信息（管理台/进度接口用；凭证本身仍只在生成时发放一次）。"""
    revoked = s["revoked_at"] is not None
    return {
        "share_code": s["share_code"],
        "read_url": f"/api/shares/{s['share_code']}",
        "note": s["note"],
        "ttl_seconds": s["ttl_seconds"],
        "created_at": s["created_at"] * 1000,
        "expires_at": s["expires_at"] * 1000,
        "revoked_at": None if s["revoked_at"] is None else s["revoked_at"] * 1000,
        "active": not revoked and now < s["expires_at"],
        "revoked": revoked,
    }


def sanitized_result(test, counts: dict, read_ts: int) -> dict:
    """分享凭证读取的唯一允许载荷：仅聚合脱敏结果。

    严禁出现评员姓名、盲评码、个人答案或异样杯对应关系（odd_pos/odd_sample）。
    """
    return {
        "sample_a": test["sample_a"],
        "sample_b": test["sample_b"],
        "deadline": test["deadline"] * 1000,
        "settled_at": test["settled_at"] * 1000,
        "valid_count": test["valid_count"],
        "correct_count": test["correct_count"],
        "abstained_count": counts["abstained"],
        "missing_count": counts["missing"],
        "p_value": test["p_value"],
        "distinguishable": bool(test["distinguishable"]),
        "conclusion": settlement_conclusion(test),
        "now": read_ts * 1000,
    }


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class TestCreate(BaseModel):
    sample_a: str = Field(min_length=1, max_length=64)
    sample_b: str = Field(min_length=1, max_length=64)
    panelists: List[str] = Field(min_length=1)
    deadline: int = Field(description="截止时间，Unix 毫秒")
    min_valid: int = Field(ge=1, le=100000)


class AdminAction(BaseModel):
    token: str


class EndCollection(BaseModel):
    """负责人截止前提前结束收集：必填 1～200 字原因（接口内 trim 归一化后校验）。"""
    token: str
    reason: str = Field(default="",
                        description="提前结束原因（trim 后 1～200 字）")


class SubmitAnswer(BaseModel):
    answer: Optional[int] = Field(default=None, ge=1, le=3)
    abstain: bool = False


class AdjustRoster(BaseModel):
    """负责人截止前处理缺席：撤回未交盲评码、补位新评员，可单用或合用。"""
    token: str
    withdraw_codes: List[str] = Field(
        default_factory=list, description="要撤回的盲评码，仅未提交者可撤回"
    )
    add_panelists: List[str] = Field(
        default_factory=list, description="补位评员姓名，同一实验姓名不得重用"
    )


class ShareCreate(BaseModel):
    """为已结算实验生成脱敏结果分享凭证。"""
    token: str
    ttl_seconds: int = Field(
        ge=1, le=MAX_SHARE_TTL_SECONDS,
        description="有效期（秒），最长 7 天（604800 秒）",
    )
    note: Optional[str] = Field(
        default=None, max_length=MAX_SHARE_NOTE_LEN,
        description="可选备注（trim 归一化后参与去重）",
    )


class ShareRevoke(BaseModel):
    token: str


class CompareTests(BaseModel):
    """负责人跨场配对对照：两场实验的测试码与各自管理令牌。"""
    first_test_code: str = Field(min_length=1)
    first_token: str = Field(min_length=1)
    second_test_code: str = Field(min_length=1)
    second_token: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="三角嗅辨盲评工作台", version="1.5", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}
    )


@app.get("/api/time")
def server_time():
    return {"now": now_ts() * 1000}


@app.post("/api/tests")
def create_test(body: TestCreate):
    sample_a = body.sample_a.strip()
    sample_b = body.sample_b.strip()
    if not sample_a or not sample_b:
        raise HTTPException(status_code=422, detail="样本编号不能为空")
    if sample_a == sample_b:
        raise HTTPException(status_code=422, detail="两种样本编号必须不同")

    seen, names = set(), []
    for raw in body.panelists:
        name = (raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        raise HTTPException(status_code=422, detail="评员名单不能为空")
    if len(names) > MAX_PANELISTS:
        raise HTTPException(status_code=422, detail=f"评员人数不得超过 {MAX_PANELISTS}")

    deadline = body.deadline // 1000  # 毫秒 -> 秒
    if deadline <= now_ts():
        raise HTTPException(status_code=422, detail="截止时间必须晚于当前时间")
    if body.min_valid > len(names):
        raise HTTPException(status_code=422, detail="最少有效答卷数不能多于评员人数")

    test_code = new_code(8)
    token = new_code(16)

    with db.write_tx() as conn:
        ts = now_ts()
        cur = conn.execute(
            """INSERT INTO tests
               (code, token_hash, sample_a, sample_b, deadline, min_valid, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (test_code, hash_token(test_code, token),
             sample_a, sample_b, deadline, body.min_valid, ts),
        )
        test_id = cur.lastrowid
        initial = []
        for name, (odd_pos, odd_sample) in zip(names, assign_cups(len(names))):
            pcode = new_code(10)
            conn.execute(
                """INSERT INTO assignments
                   (test_id, panelist, code, odd_pos, odd_sample)
                   VALUES (?, ?, ?, ?, ?)""",
                (test_id, name, pcode, odd_pos, odd_sample),
            )
            initial.append({"panelist": name, "code": pcode})
        # 创建事件与测试、名单同一事务提交
        db.log_event(conn, test_id, ts, "create", {
            "sample_a": sample_a,
            "sample_b": sample_b,
            "deadline": deadline * 1000,
            "min_valid": body.min_valid,
            "panelists": initial,
        })

    return {
        "test_code": test_code,
        "token": token,
        "dashboard": f"/admin?test={test_code}&token={token}",
    }


@app.get("/api/tests/{test_code}")
def get_test(test_code: str, token: str):
    """负责人进度视图：截止/结算前不包含任何异样杯对应关系与个人答案。"""
    row = require_admin(test_code, token)
    conn = db.read_conn()
    try:
        assigns = conn.execute(
            "SELECT panelist, code, answer, answered_at FROM assignments "
            "WHERE test_id = ? AND status = ? ORDER BY id",
            (row["id"], ACTIVE),
        ).fetchall()
        withdrawn = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments "
            "WHERE test_id = ? AND status = ?",
            (row["id"], WITHDRAWN),
        ).fetchone()["c"]
        shares = conn.execute(
            "SELECT * FROM shares WHERE test_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
    finally:
        conn.close()

    ts = now_ts()
    submitted = sum(1 for a in assigns if a["answer"] is not None)
    abstained = sum(1 for a in assigns if a["answer"] == ABSTAIN)
    valid = sum(1 for a in assigns if a["answer"] not in (None, ABSTAIN))

    return {
        "test_code": row["code"],
        "sample_a": row["sample_a"],
        "sample_b": row["sample_b"],
        "deadline": row["deadline"] * 1000,
        "now": ts * 1000,
        "min_valid": row["min_valid"],
        "ended": row["ended_at"] is not None,
        "ended_at": None if row["ended_at"] is None else row["ended_at"] * 1000,
        "end_reason": row["end_reason"] if row["ended_at"] is not None else None,
        "settled": row["settled_at"] is not None,
        "withdrawn_count": withdrawn,
        "panelists": [a["panelist"] for a in assigns],
        "codes": [  # 仅负责人可见，用于向评员分发专属盲评链接
            {"panelist": a["panelist"],
             "code": a["code"],
             "url": f"/eval/code/{a['code']}"}
            for a in assigns
        ],
        "progress": {
            "total": len(assigns),
            "submitted": submitted,
            "abstained": abstained,
            "valid": valid,
        },
        # 已生成的脱敏结果分享凭证（仅负责人可见，纯增量字段，不影响既有调用方）
        "shares": [share_admin_json(s, ts) for s in shares],
    }


@app.get("/api/eval/{code}")
def get_assignment(code: str):
    conn = db.read_conn()
    try:
        a = conn.execute(
            "SELECT a.*, t.deadline, t.settled_at, t.ended_at, t.end_reason, "
            "t.sample_a, t.sample_b "
            "FROM assignments a JOIN tests t ON a.test_id = t.id "
            "WHERE a.code = ? AND a.status = ?",
            (code, ACTIVE),
        ).fetchone()
    finally:
        conn.close()
    if a is None:
        raise HTTPException(status_code=404, detail="盲评码无效")

    ts = now_ts()
    ended = a["ended_at"] is not None
    past_deadline = ts >= a["deadline"]
    data = {
        "panelist": a["panelist"],
        "deadline": a["deadline"] * 1000,
        "now": ts * 1000,
        "past_deadline": past_deadline,
        "ended": ended,
        "ended_at": None if a["ended_at"] is None else a["ended_at"] * 1000,
        "end_reason": a["end_reason"] if ended else None,
        "settled": a["settled_at"] is not None,
        "submitted": a["answer"] is not None,
        "abstained": a["answer"] == ABSTAIN,
        "answer": (
            None
            if a["answer"] in (None, ABSTAIN)
            else a["answer"]
        ),
        "answered_at": a["answered_at"],
    }
    # 仅在已结算（结果冻结、负责人已结束盲评）后才向评员公开其本人的分配
    if a["settled_at"] is not None:
        data["odd_pos"] = a["odd_pos"]
        data["odd_sample"] = a["odd_sample"]
        data["sample_a"] = a["sample_a"]
        data["sample_b"] = a["sample_b"]
    return data


@app.post("/api/eval/{code}/submit")
def submit(code: str, body: SubmitAnswer):
    if not body.abstain and body.answer is None:
        raise HTTPException(status_code=422, detail="请选择异样杯位置或选择弃权")

    with db.write_tx() as conn:
        ts = now_ts()
        a = conn.execute(
            "SELECT a.*, t.deadline, t.ended_at FROM assignments a "
            "JOIN tests t ON a.test_id = t.id "
            "WHERE a.code = ? AND a.status = ?",
            (code, ACTIVE),
        ).fetchone()
        if a is None:
            raise HTTPException(status_code=404, detail="盲评码无效")
        if a["answer"] is not None:
            raise HTTPException(status_code=409, detail="该盲评码已提交，答卷不可修改")
        if a["ended_at"] is not None:
            raise HTTPException(
                status_code=403,
                detail="负责人已提前结束收集，答卷通道已关闭，答卷被拒绝",
            )
        if ts >= a["deadline"]:
            raise HTTPException(status_code=403, detail="已过截止时间，答卷被拒绝")

        value = ABSTAIN if body.abstain else body.answer
        conn.execute(
            "UPDATE assignments SET answer=?, answered_at=? WHERE id=?",
            (value, ts, a["id"]),
        )
        # 答卷事件与状态变更同事务；结算前只存不泄，读取时再按盲态裁剪
        db.log_event(conn, a["test_id"], ts, "answer", {
            "panelist": a["panelist"],
            "code": a["code"],
            "abstained": bool(body.abstain),
            "answer": None if body.abstain else body.answer,
        })
    return {"ok": True, "answer": "弃权" if body.abstain else body.answer}


@app.post("/api/tests/{test_code}/settle")
def settle(test_code: str, body: AdminAction):
    """到期或提前结束后结算；重复调用返回冻结结果。服务端时间为唯一截止依据。"""
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="测试不存在")
        verify_admin(row, body.token)

        ts = now_ts()
        if ts < row["deadline"] and row["ended_at"] is None:
            raise HTTPException(status_code=409,
                                detail="尚未到截止时间且未提前结束，不能结算")

        if row["settled_at"] is not None:
            frozen = row
        else:
            assigns = conn.execute(
                "SELECT * FROM assignments WHERE test_id = ? AND status = ?",
                (row["id"], ACTIVE),
            ).fetchall()
            valid = sum(1 for a in assigns if a["answer"] not in (None, ABSTAIN))
            correct = sum(
                1 for a in assigns
                if a["answer"] not in (None, ABSTAIN) and a["answer"] == a["odd_pos"]
            )
            abstained = sum(1 for a in assigns if a["answer"] == ABSTAIN)
            missing = sum(1 for a in assigns if a["answer"] is None)
            p = binomial_tail(valid, correct)
            distinguishable = int(valid >= row["min_valid"] and p <= 0.05)
            conn.execute(
                """UPDATE tests SET settled_at=?, valid_count=?, correct_count=?,
                   p_value=?, distinguishable=? WHERE id=?""",
                (ts, valid, correct, p, distinguishable, row["id"]),
            )
            # 结算事件与状态冻结同一事务提交；提前结束后立即结算时记录结束来源
            settle_payload = {
                "valid_count": valid,
                "correct_count": correct,
                "abstained_count": abstained,
                "missing_count": missing,
                "p_value": p,
                "min_valid": row["min_valid"],
                "distinguishable": bool(distinguishable),
            }
            if row["ended_at"] is not None:
                settle_payload["settled_after_end"] = True
            db.log_event(conn, row["id"], ts, "settle", settle_payload)
            frozen = conn.execute(
                "SELECT * FROM tests WHERE id = ?", (row["id"],)
            ).fetchone()

        assigns = conn.execute(
            "SELECT * FROM assignments WHERE test_id = ? AND status = ? "
            "ORDER BY id",
            (row["id"], ACTIVE),
        ).fetchall()

    return {
        "test_code": frozen["code"],
        "sample_a": frozen["sample_a"],
        "sample_b": frozen["sample_b"],
        "deadline": frozen["deadline"] * 1000,
        "ended": frozen["ended_at"] is not None,
        "ended_at": None if frozen["ended_at"] is None else frozen["ended_at"] * 1000,
        "end_reason": frozen["end_reason"] if frozen["ended_at"] is not None else None,
        "settled_at": frozen["settled_at"] * 1000,
        "valid_count": frozen["valid_count"],
        "correct_count": frozen["correct_count"],
        "abstained_count": sum(1 for a in assigns if a["answer"] == ABSTAIN),
        "missing_count": sum(1 for a in assigns if a["answer"] is None),
        "p_value": frozen["p_value"],
        "min_valid": frozen["min_valid"],
        "distinguishable": bool(frozen["distinguishable"]),
        "conclusion": settlement_conclusion(frozen),
        "assignments": [assignment_detail(a) for a in assigns],
    }


# ---------------------------------------------------------------------------
# 提前结束收集
# ---------------------------------------------------------------------------

@app.post("/api/tests/{test_code}/end")
def end_collection(test_code: str, body: EndCollection):
    """负责人截止前提前结束收集：立即冻结名单与答卷入口。

    - 凭管理凭证；原因 trim 后须为 1～200 字。
    - 仅未提前结束、未结算且未过截止时间的实验可结束；
      已提前结束的重复调用返回同一结束记录（reused=true，不新增事件）。
    - 结束后：评员再访问 / 提交一律按已结束拒绝；撤回 / 补位被拒；
      结算可立即执行（仍按现有有效答卷、最少数和 p 值规则冻结结果）。
    - 与并发答卷按“先取得写锁者”生效：答卷事务先提交则该答卷计入，
      结束事务先提交则并发答卷在锁内复查到 ended_at 而被拒。
    - 结束事件与状态冻结在同一写事务提交，时间线记录结束原因与服务端时间。
    """
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="结束原因不能为空")
    if len(reason) > MAX_END_REASON_LEN:
        raise HTTPException(status_code=422,
                            detail=f"结束原因长度不得超过 {MAX_END_REASON_LEN} 字")

    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="测试不存在")
        verify_admin(row, body.token)

        reused = False
        if row["ended_at"] is not None:
            # 幂等：重复结束返回同一结束记录
            ended_at, stored_reason = row["ended_at"], row["end_reason"]
            reused = True
        else:
            if row["settled_at"] is not None:
                raise HTTPException(status_code=409,
                                    detail="测试已结算，无需结束收集")
            ts = now_ts()
            if ts >= row["deadline"]:
                raise HTTPException(status_code=409,
                                    detail="已过截止时间，收集已按原截止自动关闭，"
                                           "无需提前结束")
            conn.execute(
                "UPDATE tests SET ended_at=?, end_reason=? WHERE id=?",
                (ts, reason, row["id"]),
            )
            # 结束事件与冻结状态同一事务；原因与服务端时间进入时间线
            db.log_event(conn, row["id"], ts, "end", {
                "reason": reason,
                "deadline": row["deadline"] * 1000,
            })
            ended_at, stored_reason = ts, reason

    return {
        "ok": not reused,
        "reused": reused,
        "test_code": test_code,
        "ended": True,
        "ended_at": ended_at * 1000,
        "end_reason": stored_reason,
        "deadline": row["deadline"] * 1000,
        "settled": row["settled_at"] is not None,
    }


# ---------------------------------------------------------------------------
# 名单调整（撤回 / 补位）
# ---------------------------------------------------------------------------

@app.post("/api/tests/{test_code}/adjust")
def adjust_roster(test_code: str, body: AdjustRoster):
    """截止前处理评员缺席：可只补位、只撤回，或一次操作撤回并补位。

    - 仅未提交（answer IS NULL）者可撤回；已作答或已弃权不可撤回。
    - 撤回后旧码立即无法查看或提交（标记 withdrawn，评员接口按无效码处理）。
    - 补位取得新专属码；同一实验姓名（含已撤回者）不得重用。
    - 已有评员的杯序、异样样本和答卷不变；新分配在当前有效名单上贪心均衡。
    - 调整后有效人数须满足 min_valid ≤ 人数 ≤ 500。
    - 截止、提前结束或结算后拒绝；任一校验失败整个操作回滚。
    - 与答卷并发时，以实际先拿到写锁并完成的操作为准。
    """
    withdraw_codes = [c.strip() for c in body.withdraw_codes if c and c.strip()]
    if len(withdraw_codes) != len(set(withdraw_codes)):
        raise HTTPException(status_code=422, detail="撤回名单中盲评码重复")

    names, seen_names = [], set()
    for raw in body.add_panelists:
        name = (raw or "").strip()
        if not name:
            continue
        if name in seen_names:
            raise HTTPException(status_code=422, detail=f"补位名单中姓名重复：{name}")
        seen_names.add(name)
        names.append(name)

    if not withdraw_codes and not names:
        raise HTTPException(status_code=422,
                            detail="未提供要撤回的盲评码或补位评员姓名")

    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="测试不存在")
        verify_admin(row, body.token)

        if row["settled_at"] is not None:
            raise HTTPException(status_code=409,
                                detail="测试已结算，名单已冻结，不能调整")
        if row["ended_at"] is not None:
            raise HTTPException(status_code=409,
                                detail="收集已被负责人提前结束，名单已冻结，不能调整")
        if now_ts() >= row["deadline"]:
            raise HTTPException(status_code=409,
                                detail="已过截止时间，不能调整名单")

        # ---- 撤回校验：码属于本实验有效名单，且尚未提交 ----
        withdrawn_rows = []
        for code in withdraw_codes:
            a = conn.execute(
                "SELECT * FROM assignments WHERE test_id = ? AND code = ?",
                (row["id"], code),
            ).fetchone()
            if a is None:
                raise HTTPException(status_code=404, detail=f"盲评码无效：{code}")
            if a["status"] == WITHDRAWN:
                raise HTTPException(
                    status_code=409, detail=f"盲评码 {code} 已撤回，不能重复撤回"
                )
            if a["answer"] is not None:
                kind = "已弃权" if a["answer"] == ABSTAIN else "已作答"
                raise HTTPException(
                    status_code=409,
                    detail=f"评员「{a['panelist']}」{kind}，不可撤回（盲评码 {code}）",
                )
            withdrawn_rows.append(a)

        # ---- 补位校验：同一实验姓名（含已撤回者）不得重用 ----
        if names:
            placeholders = ",".join("?" for _ in names)
            dup_rows = conn.execute(
                f"SELECT panelist FROM assignments WHERE test_id = ? "
                f"AND panelist IN ({placeholders})",
                (row["id"], *names),
            ).fetchall()
            if dup_rows:
                dup = "、".join(r["panelist"] for r in dup_rows)
                raise HTTPException(
                    status_code=422,
                    detail=f"姓名在本实验中已存在（含已撤回者），不得重用：{dup}",
                )

        # ---- 调整后有效人数上下限 ----
        active_count = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments "
            "WHERE test_id = ? AND status = ?",
            (row["id"], ACTIVE),
        ).fetchone()["c"]
        after = active_count - len(withdrawn_rows) + len(names)
        if after < row["min_valid"]:
            raise HTTPException(
                status_code=422,
                detail=f"调整后有效人数 {after} 人，低于最少有效答卷数 "
                       f"{row['min_valid']}，整次操作无效",
            )
        if after > MAX_PANELISTS:
            raise HTTPException(
                status_code=422,
                detail=f"调整后有效人数 {after} 人，超过上限 {MAX_PANELISTS} 人，"
                       "整次操作无效",
            )

        # ---- 执行撤回（旧码保留审计痕迹但立即失效） ----
        for a in withdrawn_rows:
            conn.execute(
                "UPDATE assignments SET status = ? WHERE id = ?",
                (WITHDRAWN, a["id"]),
            )

        # ---- 执行补位：在当前有效名单上逐人贪心均衡 ----
        added = []
        if names:
            c_pos = {1: 0, 2: 0, 3: 0}
            c_sample = {"A": 0, "B": 0}
            for r in conn.execute(
                "SELECT odd_pos, odd_sample, COUNT(*) AS c FROM assignments "
                "WHERE test_id = ? AND status = ? GROUP BY odd_pos, odd_sample",
                (row["id"], ACTIVE),
            ):
                c_pos[r["odd_pos"]] += r["c"]
                c_sample[r["odd_sample"]] += r["c"]

            for name in names:
                odd_pos, odd_sample = pick_balanced(c_pos, c_sample)
                while True:  # 全局 UNIQUE(code)，撞码（极小概率）则重取
                    pcode = new_code(10)
                    if conn.execute(
                        "SELECT 1 FROM assignments WHERE code = ?", (pcode,)
                    ).fetchone() is None:
                        break
                conn.execute(
                    """INSERT INTO assignments
                       (test_id, panelist, code, odd_pos, odd_sample, status)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (row["id"], name, pcode, odd_pos, odd_sample, ACTIVE),
                )
                added.append({
                    "panelist": name,
                    "code": pcode,
                    "url": f"/eval/code/{pcode}",
                })

        active_rows = conn.execute(
            "SELECT answer FROM assignments WHERE test_id = ? AND status = ?",
            (row["id"], ACTIVE),
        ).fetchall()

        # 一次名单调整仅保留一笔事件（撤回与补位同在此明细内），与名单变更同事务
        db.log_event(conn, row["id"], now_ts(), "adjust", {
            "withdrawn": [
                {"panelist": a["panelist"], "code": a["code"]}
                for a in withdrawn_rows
            ],
            "added": added,
            "active_total": len(active_rows),
        })

    submitted = sum(1 for a in active_rows if a["answer"] is not None)
    abstained = sum(1 for a in active_rows if a["answer"] == ABSTAIN)
    valid_answers = sum(
        1 for a in active_rows if a["answer"] not in (None, ABSTAIN)
    )
    return {
        "ok": True,
        "test_code": row["code"],
        "withdrawn": [
            {"panelist": a["panelist"], "code": a["code"]}
            for a in withdrawn_rows
        ],
        "added": added,
        "progress": {
            "total": len(active_rows),
            "submitted": submitted,
            "abstained": abstained,
            "valid": valid_answers,
        },
    }


# ---------------------------------------------------------------------------
# 操作时间线（仅实验负责人凭管理凭证可查）
# ---------------------------------------------------------------------------

@app.get("/api/tests/{test_code}/timeline")
def timeline(test_code: str, token: str):
    """按提交顺序返回一场实验从创建到结算的操作时间线。

    - 鉴权与进度接口一致（测试码 + 负责人令牌）。
    - 条目按 events.id（写事务提交顺序）稳定升序；ts 为事务内采集的服务端时间。
    - 结算前 answer 事件只含已答/弃权状态；结算后才透出选杯、异样杯、异样样本与对错。
    - 旧数据库缺少既往操作时以 history_start 条目标示记录起点，不虚构历史。
    """
    row = require_admin(test_code, token)
    conn = db.read_conn()
    try:
        events = conn.execute(
            "SELECT id, ts, type, payload FROM events WHERE test_id = ? "
            "ORDER BY id",
            (row["id"],),
        ).fetchall()
        assigns = conn.execute(
            "SELECT code, odd_pos, odd_sample FROM assignments WHERE test_id = ?",
            (row["id"],),
        ).fetchall()
    finally:
        conn.close()

    settled = row["settled_at"] is not None
    assign_by_code = {a["code"]: a for a in assigns}
    return {
        "test_code": row["code"],
        "settled": settled,
        "ended": row["ended_at"] is not None,
        "ended_at": None if row["ended_at"] is None else row["ended_at"] * 1000,
        "end_reason": row["end_reason"] if row["ended_at"] is not None else None,
        "now": now_ts() * 1000,
        # 存在 history_start 标记表示该测试既往操作无法追溯（旧库升级）
        "history_complete": all(e["type"] != "history_start" for e in events),
        "events": [render_event(e, settled, assign_by_code) for e in events],
    }


# ---------------------------------------------------------------------------
# 脱敏结果分享凭证：生成 / 撤销（负责人）/ 只读（不记名）
# ---------------------------------------------------------------------------

@app.post("/api/tests/{test_code}/shares")
def create_share(test_code: str, body: ShareCreate):
    """为已结算实验生成脱敏结果分享凭证。

    - 凭现有管理凭证（测试码 + 负责人令牌）；仅已结算实验可生成。
    - ttl_seconds 1..604800（最长 7 天）；note 可选、trim 归一化后参与去重。
    - 同一实验、同一备注、同一有效期重复请求返回原凭证（reused=true）。
    - 同实验同备注但有效期不同（或反之）视为冲突，返回 409。
    - 生成写入操作时间线（share_create，与凭证同一写事务）。
    """
    note = (body.note or "").strip()
    if len(note) > MAX_SHARE_NOTE_LEN:
        raise HTTPException(status_code=422,
                            detail=f"备注长度不得超过 {MAX_SHARE_NOTE_LEN} 字")

    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="测试不存在")
        verify_admin(row, body.token)

        if row["settled_at"] is None:
            raise HTTPException(status_code=409,
                                detail="实验尚未结算，仅已结算实验可生成分享凭证")

        ts = now_ts()
        existing = conn.execute(
            "SELECT * FROM shares WHERE test_id = ? AND note = ? ORDER BY id",
            (row["id"], note),
        ).fetchall()
        same = [s for s in existing if s["ttl_seconds"] == body.ttl_seconds]
        if same:
            # 幂等：同实验同备注同有效期（无论原凭证是否已过期/已撤销）返回原凭证
            s = same[0]
            reused = True
        else:
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail="同一实验已存在相同备注但有效期不同的分享凭证，"
                           "内容冲突；请更换备注或使用原有效期",
                )
            # 全新凭证：高熵随机码，撞 UNIQUE 则重取
            while True:
                share_code = new_code(16)
                if conn.execute(
                    "SELECT 1 FROM shares WHERE share_code = ?", (share_code,)
                ).fetchone() is None:
                    break
            conn.execute(
                """INSERT INTO shares
                   (test_id, share_code, note, ttl_seconds,
                    created_at, expires_at, revoked_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (row["id"], share_code, note, body.ttl_seconds,
                 ts, ts + body.ttl_seconds),
            )
            s = conn.execute(
                "SELECT * FROM shares WHERE share_code = ?", (share_code,)
            ).fetchone()
            # 凭证与审计事件同一写事务提交；事件不含完整凭证明文
            db.log_event(conn, row["id"], ts, "share_create", {
                "share_id": s["id"],
                "note": note,
                "ttl_seconds": body.ttl_seconds,
                "expires_at": (ts + body.ttl_seconds) * 1000,
                "code_hint": share_code[:4],
            })
            reused = False

    out = share_admin_json(s, now_ts())
    out["test_code"] = row["code"]
    out["reused"] = reused
    return out


@app.post("/api/tests/{test_code}/shares/{share_code}/revoke")
def revoke_share(test_code: str, share_code: str, body: ShareRevoke):
    """负责人在到期前撤销分享凭证；重复撤销 / 撤销已过期凭证返回 409。

    撤销写入操作时间线（share_revoke，与状态变更同一写事务）。
    """
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="测试不存在")
        verify_admin(row, body.token)

        s = conn.execute(
            "SELECT * FROM shares WHERE test_id = ? AND share_code = ?",
            (row["id"], share_code),
        ).fetchone()
        if s is None:
            raise HTTPException(status_code=404, detail="分享凭证不存在")
        if s["revoked_at"] is not None:
            raise HTTPException(status_code=409, detail="分享凭证已撤销，不能重复撤销")

        ts = now_ts()
        if ts >= s["expires_at"]:
            raise HTTPException(status_code=409, detail="分享凭证已到期，无需撤销")

        conn.execute("UPDATE shares SET revoked_at = ? WHERE id = ?", (ts, s["id"]))
        db.log_event(conn, row["id"], ts, "share_revoke", {
            "share_id": s["id"],
            "note": s["note"],
            "ttl_seconds": s["ttl_seconds"],
            "created_at": s["created_at"] * 1000,
            "expires_at": s["expires_at"] * 1000,
            "code_hint": s["share_code"][:4],
        })
        s = conn.execute("SELECT * FROM shares WHERE id = ?", (s["id"],)).fetchone()

    out = share_admin_json(s, now_ts())
    out["test_code"] = row["code"]
    out["ok"] = True
    return out


@app.get("/api/shares/{share_code}")
def read_share(share_code: str):
    """不记名只读：凭分享凭证取脱敏后的已结算结果。

    - 凭证不存在 → 404；已撤销或已到期 → 403（一律拒绝读取）。
    - 只返回样本编号、截止与结算时间、有效答卷数、答对数、弃权数、未交数、
      单侧尾概率和可辨结论；不返回评员姓名、盲评码、个人答案或异样杯对应关系。
    """
    conn = db.read_conn()
    try:
        s = conn.execute(
            "SELECT * FROM shares WHERE share_code = ?", (share_code,)
        ).fetchone()
        if s is None:
            raise HTTPException(status_code=404, detail="分享凭证无效")
        if s["revoked_at"] is not None:
            raise HTTPException(status_code=403, detail="分享凭证已被撤销")
        now = now_ts()
        if now >= s["expires_at"]:
            raise HTTPException(status_code=403, detail="分享凭证已过期")

        test = conn.execute(
            "SELECT * FROM tests WHERE id = ?", (s["test_id"],)
        ).fetchone()
        if test is None or test["settled_at"] is None:
            # 数据完整性异常或测试被删除：凭证不能读到任何内容
            raise HTTPException(status_code=404, detail="分享凭证无效")
        agg = conn.execute(
            "SELECT "
            "SUM(CASE WHEN answer = ? THEN 1 ELSE 0 END) AS abstained, "
            "SUM(CASE WHEN answer IS NULL THEN 1 ELSE 0 END) AS missing "
            "FROM assignments WHERE test_id = ? AND status = ?",
            (ABSTAIN, test["id"], ACTIVE),
        ).fetchone()
    finally:
        conn.close()

    return sanitized_result(test, {
        "abstained": agg["abstained"] or 0,
        "missing": agg["missing"] or 0,
    }, now)


# ---------------------------------------------------------------------------
# 复测配对对照（同一批评员两场已结算实验的跨场比较，只读）
# ---------------------------------------------------------------------------

@app.post("/api/compare")
def compare_tests(body: CompareTests):
    """负责人凭两场实验的测试码与各自管理令牌，比较同一批评员复测前后的辨别表现。

    - 两场必须是不同实验、均已结算、样本编号集合相同（A/B 顺序可交换），
      否则明确拒绝（422/409）。
    - 按两场有效名单中去首尾空白后完全同名的评员配对；已撤回者不参与配对。
      仅双方均提交 1～3 杯答案者纳入统计；配对但任一方弃权/未交计入
      paired_invalid_count；仅出现在一场者计入 unpaired_count。
    - 返回双方都对 / 仅首场对 / 仅次场对 / 都错四格汇总，按两类仅一场答对
      人数计算精确双侧配对二项检验 p 值；无有效配对时 p 为空并说明无法比较，
      不一致对为 0 时 p=1，p ≤ 0.05 才标记 changed=true。
    - 只返回汇总计数与结论，不返回个人答案、杯序或盲评码；只读，不写时间线。
    """
    first_code = body.first_test_code.strip()
    second_code = body.second_test_code.strip()
    if first_code == second_code:
        raise HTTPException(status_code=422,
                            detail="两场实验必须不同，不能与自身配对对照")

    conn = db.read_conn()
    try:
        row1 = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (first_code,)
        ).fetchone()
        if row1 is None:
            raise HTTPException(status_code=404, detail="首场测试不存在")
        row2 = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (second_code,)
        ).fetchone()
        if row2 is None:
            raise HTTPException(status_code=404, detail="次场测试不存在")
        for row, token, label in (
            (row1, body.first_token, "首场"),
            (row2, body.second_token, "次场"),
        ):
            if not token or not secrets.compare_digest(
                hash_token(row["code"], token), row["token_hash"]
            ):
                raise HTTPException(status_code=403,
                                    detail=f"{label}管理令牌无效")

        if row1["settled_at"] is None:
            raise HTTPException(status_code=409,
                                detail="首场实验尚未结算，无法配对对照")
        if row2["settled_at"] is None:
            raise HTTPException(status_code=409,
                                detail="次场实验尚未结算，无法配对对照")
        if {row1["sample_a"], row1["sample_b"]} != \
                {row2["sample_a"], row2["sample_b"]}:
            raise HTTPException(status_code=409,
                                detail="两场实验的样本编号集合不一致，无法配对对照")

        assigns1 = conn.execute(
            "SELECT panelist, answer, odd_pos FROM assignments "
            "WHERE test_id = ? AND status = ?",
            (row1["id"], ACTIVE),
        ).fetchall()
        assigns2 = conn.execute(
            "SELECT panelist, answer, odd_pos FROM assignments "
            "WHERE test_id = ? AND status = ?",
            (row2["id"], ACTIVE),
        ).fetchall()
    finally:
        conn.close()

    def roster(rows):
        # 有效名单：姓名去首尾空白后完全同名者配对（库内姓名本已 trim 且唯一）
        m = {}
        for r in rows:
            name = (r["panelist"] or "").strip()
            if name:
                m[name] = r
        return m

    map1, map2 = roster(assigns1), roster(assigns2)
    common = set(map1) & set(map2)
    unpaired_first = len(set(map1) - set(map2))
    unpaired_second = len(set(map2) - set(map1))

    paired_invalid = 0
    both_correct = only_first = only_second = both_wrong = 0
    for name in common:
        a1, a2 = map1[name], map2[name]
        valid1 = a1["answer"] not in (None, ABSTAIN)
        valid2 = a2["answer"] not in (None, ABSTAIN)
        if not (valid1 and valid2):
            paired_invalid += 1
            continue
        correct1 = a1["answer"] == a1["odd_pos"]
        correct2 = a2["answer"] == a2["odd_pos"]
        if correct1 and correct2:
            both_correct += 1
        elif correct1:
            only_first += 1
        elif correct2:
            only_second += 1
        else:
            both_wrong += 1

    valid_pairs = both_correct + only_first + only_second + both_wrong
    p = None if valid_pairs == 0 else paired_binomial_p(only_first, only_second)
    changed = p is not None and p <= 0.05

    return {
        "first_test_code": row1["code"],
        "second_test_code": row2["code"],
        "sample_a": row1["sample_a"],
        "sample_b": row1["sample_b"],
        # 次场样本编号顺序是否与首场对调（集合相同前提下的提示信息）
        "sample_order_swapped": row1["sample_a"] != row2["sample_a"],
        "paired_count": len(common),
        "paired_valid_count": valid_pairs,
        "paired_invalid_count": paired_invalid,
        "unpaired_count": unpaired_first + unpaired_second,
        "unpaired_first_only": unpaired_first,
        "unpaired_second_only": unpaired_second,
        "both_correct": both_correct,
        "only_first_correct": only_first,
        "only_second_correct": only_second,
        "both_wrong": both_wrong,
        "p_value": p,
        "changed": changed,
        "conclusion": compare_conclusion(
            valid_pairs, both_correct, only_first, only_second, both_wrong, p
        ),
    }


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(f"{STATIC_DIR}/index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(f"{STATIC_DIR}/admin.html")


@app.get("/eval")
def eval_entry():
    return FileResponse(f"{STATIC_DIR}/eval.html")


@app.get("/eval/code/{code}")
def eval_by_code(code: str):
    return FileResponse(f"{STATIC_DIR}/eval.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
