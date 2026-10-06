# 三角嗅辨盲评工作台（Triangle Test Bench）

基于 **FastAPI + SQLite + 原生 HTML/JS（无构建步骤）** 的 Web 应用，用于以
**三杯法（Triangle Test）** 比较两种气味样本是否可辨。

## 功能说明

**实验负责人**

1. 输入：两种样本编号（A / B）、评员名单（每行一人，自动去重）、截止时间、最少有效答卷数。
2. 系统为每位评员生成**仅本人可用、只能提交一次**的高熵盲评码与专属链接。
3. 每位评员看到三杯随机顺序：两杯同一样本、一杯另一样本；**异样杯所在位置（1/2/3）
   在全体分配中尽量均衡**，异样杯是 A 还是 B 也在全体中均衡。
4. 截止前管理台只显示「已交 / 弃权 / 有效」汇总计数，**不显示任何人的答案与杯号对应关系**。
5. 截止后一键结算，报告：
   - 有效答卷数（弃权、未交不计入）、答对数、弃权数、未交数；
   - 随机猜中概率为 1/3 时的**二项分布单侧上尾概率**
     `p = P(X ≥ 答对 | X ~ Binomial(n, 1/3))`（对数空间递推，大样本不溢出）；
   - 判定：**有效答卷数 ≥ 最少有效数 且 p ≤ 0.05 才判为“可辨”**；
     否则明确说明“样本不足（有效答卷少于最低要求）”或“证据未达阈值（p > 0.05）”。
6. 结果冻结后可回看每位评员的异样杯位置、异样样本、提交答案与对错；
   重复结算返回同一份冻结结果。
7. **截止前处理缺席（撤回 / 补位）**：凭管理凭证提交要撤回的盲评码与补位评员姓名，
   可只补位、只撤回，或在一次操作中撤回并补位：
   - 仅**未提交**者可撤回；已作答或已弃权者不可撤回；撤回后的旧码立即无法查看或提交；
   - 补位评员取得**新的专属盲评码**；同一实验中姓名（含已撤回者）不得重用；
   - 已有评员的杯序、异样样本与答卷**完全不变**；新分配在当前有效名单约束下，
     使异样杯位置与异样样本 A/B 计数尽量均衡（各档极差 ≤ 1）；
   - 调整后有效人数必须满足 **最少有效答卷数 ≤ 有效人数 ≤ 500**；
     任一校验失败则**整次操作无效**（事务回滚，不留中间态）；
   - 截止、提前结束或结算后一律拒绝调整；与答卷并发时按**实际先完成**的操作判定
     （答卷先完成则撤回报「已作答」，撤回先完成则旧码立即失效、答卷按 404 拒绝）。
8. **截止前提前结束收集（负责人）**：截止时间到来之前，负责人可填写 **1～200 字原因**
   提前结束一场实验：
   - 成功后**立即冻结名单与答卷入口**：评员再访问会看到“已结束”、提交一律 403 拒绝，
     撤回 / 补位一律 409 拒绝；
   - **重复结束返回同一结束记录**（`reused=true`，`ended_at` 不变、不重复记录时间线）；
     已过截止时间（未提前结束）不能再结束，实验保持原截止行为；
   - **结束后可立即结算**（无需等到截止时间），结算仍按现有**有效答卷、最少有效数与
     p 值**规则冻结结果，重复结算返回同一份冻结结果；
   - 结束操作与并发答卷按**先取得写锁者**生效：结束事务先提交则并发答卷在锁内复查到
     已结束而被拒；答卷事务先提交则该答卷正常计入；
   - 结束原因与服务端时间以 `end` 事件写入操作时间线（与冻结状态同一事务）。
9. **操作时间线（可追溯审计）**：实验负责人凭测试码 + 管理令牌调用
   `GET /api/tests/{code}/timeline` 即可按**提交顺序**取得一场实验从创建到结算的
   实际变动：创建、名单调整（撤回 + 补位在同一笔明细内）、评员答卷 / 弃权、结算形成。
   - 每条记录含**服务端时间**（事务内采集）、**操作类型**、**关联评员**与结果，
     以自增序号 `seq` 稳定升序；答卷 / 调整 / 结算的记录与状态变更在**同一写事务**内
     原子提交，校验失败整笔回滚、**不留下记录**，并发时顺序与最终状态一致；
   - **结算前**答卷记录只显示「已答 / 弃权」，不透露选杯、异样杯（位置 / 样本）与对错；
     **结算后**同一条记录才可查看对应答案与判定；
   - 升级前由旧版本创建的测试没有既往操作记录，时间线首条为 **`history_start`
     记录起点标记**（响应中 `history_complete=false`），明确说明既往操作不可追溯，
     **不虚构历史**；升级后新建测试 `history_complete=true`、记录完整。
10. **脱敏结果分享凭证（结算后对外分享）**：实验负责人凭现有管理凭证为**已结算**
   实验生成不记名只读凭证：输入有效期（**最长 7 天**）与可选备注，返回一次性随机
   凭证与可调用地址 `/api/shares/{凭证}`；读取**只**返回样本编号、截止与结算时间、
   有效答卷数、答对数、弃权数、未交数、单侧尾概率与可辨结论，**不**返回评员姓名、
   盲评码、个人答案或异样杯对应关系。
   - 到期前负责人可撤销凭证；**到期或撤销后读取一律拒绝**（403）；
   - 同一实验、同一备注、同一有效期重复请求**返回原凭证**（`reused=true`），
     同备注不同有效期等内容冲突返回 409；
   - 仅已结算实验可生成（409）；生成与撤销均以 `share_create` / `share_revoke`
     写入现有操作时间线（同事务，失败不留痕）；管理台结算后提供可视化卡片。
11. **复测配对对照（跨场比较）**：负责人凭**两场实验的测试码与各自管理令牌**调用
    `POST /api/compare`，判断同一批评员复测后的辨别表现是否改变：
    - 两场必须**不同、均已结算、样本编号集合相同**（A/B 顺序可交换），否则明确拒绝；
    - 按两场有效名单中**去首尾空白后完全同名**的评员配对，仅**双方均提交 1～3 杯
      答案**者纳入；撤回、弃权、未交与仅出现在一场者排除，并分别返回
      **未配对人数**与**已配对但无效人数**；
    - 返回双方都对 / 仅首场对 / 仅次场对 / 都错四格汇总，按两类仅一场答对人数计算
      **精确双侧配对二项检验 p 值**（无有效配对时 p 为空并说明无法比较，
      不一致对为 0 时 p=1，**p ≤ 0.05 才标记有差异**）；
    - 只返回汇总计数与结论，**不返回个人答案、杯序或盲评码**；接口只读，
      不改变两场实验状态，也不写入操作时间线。

**评员**

- 凭专属链接或盲评码进入，只看到 1/2/3 三个杯子，嗅辨后选择异样杯，或选择**弃权**；
- **提交后不可修改、不能再次提交**；截止后或被负责人提前结束后，服务端拒绝一切答卷；
- 提前结束后访问会显示结束时间与原因；结算前任何页面都看不到样本对应关系；
  结算后可在本人页面回看自己的分配与对错。

**并发规则**：答卷、提前结束、名单调整与结算都在 SQLite `BEGIN IMMEDIATE` 写事务中完成，
截止判定一律以**服务端时钟**为准——临界时刻要么答卷落入截止前、要么按截止后拒绝；
撤回与答卷竞争同一评员、结束与答卷竞争整个收集时，均由**先拿到写锁并完成**的一方生效；
结算一旦写入即冻结，不会被后续操作改动。
操作时间线的记录就在这些同一事务内落库，提交顺序（`seq`）与最终状态严格一致。
未提前结束的实验始终保持原截止行为。

## 一键启动（Docker Compose，推荐）

```bash
docker compose up -d --build
```

打开浏览器访问：

- 负责人首页（创建 / 进入结算台）：<http://localhost:8080/>
- 评员入口：<http://localhost:8080/eval>

停止与查看日志：

```bash
docker compose down
docker compose logs -f
```

SQLite 数据保存在 Docker 命名卷 `sniff-data`（容器内 `/data/sniff.db`），
容器重建后数据不丢失。

### 配置

| 配置项 | 方式 | 默认值 |
| --- | --- | --- |
| 宿主机访问端口 | 环境变量 `HOST_PORT`（compose 自动插值） | `8080` |
| 容器内监听端口 | Dockerfile `EXPOSE` / uvicorn 参数 | `8080` |
| SQLite 数据库路径 | 环境变量 `SNIFF_DB_PATH` | `/data/sniff.db`（卷挂载） |

改用 9090 端口示例：

```bash
HOST_PORT=9090 docker compose up -d --build
# 访问 http://localhost:9090/
```

若想把数据库放到宿主机目录而非命名卷，编辑 `docker-compose.yml` 的 volumes：

```yaml
volumes:
  - ./data:/data
```

## 本地直接运行（无 Docker）

需 Python 3.11+：

```bash
pip install -r requirements.txt
export SNIFF_DB_PATH="$(pwd)/sniff.db"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

访问 <http://localhost:8080/>。

## 测试

测试仅依赖 Python 标准库，会启动真实 uvicorn 子进程走完整 HTTP 流程
（统计函数交叉验证、输入校验、盲态保密、一次提交、截止拒答、结算判定、冻结、回看、
撤回/补位、均衡再分配、有效人数上下限、撤回与答卷并发竞争、负责人提前结束收集
（原因校验、名单/答卷冻结、重复结束幂等、结束后立即结算、结束与答卷写锁竞争、
时间线 end 事件）、操作时间线的顺序/盲态/
原子性/并发一致性、旧数据库升级的记录起点标记，以及脱敏结果分享凭证的生成幂等/
内容冲突/只读脱敏白名单/到期与撤销拒绝/时间线审计/旧库兼容、复测配对对照
（前置校验拒绝、A/B 对调接受、配对/无效/未配对计数、四格汇总、精确双侧 p 值、
无有效配对 p 为空、响应脱敏））：

```bash
pip install -r requirements.txt
python tests/smoke_test.py
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tests` | 创建测试，返回测试码与负责人令牌（令牌仅返回一次） |
| GET  | `/api/tests/{code}?token=` | 负责人进度（截止前仅汇总计数 + 分发链接） |
| POST | `/api/tests/{code}/adjust` | 截止前撤回盲评码 / 补位评员（可单用或合用） |
| POST | `/api/tests/{code}/end` | 截止前凭原因提前结束收集（重复调用返回同一结束记录） |
| POST | `/api/tests/{code}/settle` | 到期或提前结束后结算（重复调用返回冻结结果） |
| GET  | `/api/tests/{code}/timeline?token=` | 负责人操作时间线（结算后才透出选杯与对错） |
| POST | `/api/tests/{code}/shares` | 为**已结算**实验生成脱敏结果分享凭证（有效期 ≤ 7 天，备注可选） |
| POST | `/api/tests/{code}/shares/{share_code}/revoke` | 到期前撤销分享凭证 |
| GET  | `/api/shares/{share_code}` | 不记名只读脱敏结果（到期 / 撤销后拒绝） |
| POST | `/api/compare` | 复测配对对照：凭两场已结算实验的测试码 + 各自令牌，按同名评员配对做精确双侧配对二项检验 |
| GET  | `/api/eval/{code}` | 评员视图（结算前无任何分配/样本信息） |
| POST | `/api/eval/{code}/submit` | 提交 `{"answer":1|2|3}` 或 `{"abstain":true}` |
| GET  | `/api/time` | 服务端当前时间（毫秒） |

### 脱敏结果分享凭证调用示例

实验结算后，负责人凭现有管理凭证（测试码 + 负责人令牌）生成**不记名只读**的
一次性随机凭证，持有者无需任何令牌即可读取该实验的**脱敏结果**。

`POST /api/tests/{test_code}/shares`，请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `token` | string | 创建测试时发放的负责人令牌（必填） |
| `ttl_seconds` | int | 有效期秒数，**最长 604800（7 天）**、最小 1（必填） |
| `note` | string | 可选备注（≤200 字，trim 归一化后参与去重） |

生成（7 天有效，带备注）：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/shares \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌","ttl_seconds":604800,"note":"发给合作方"}'
```

成功响应（分享凭证只在生成时发放，请立即保存）：

```json
{
  "test_code": "AbCdEf12",
  "share_code": "rT4kPq7mWx2ZaB9c",
  "read_url": "/api/shares/rT4kPq7mWx2ZaB9c",
  "note": "发给合作方",
  "ttl_seconds": 604800,
  "created_at": 1759000000000,
  "expires_at": 1759604800000,
  "revoked_at": null,
  "active": true,
  "revoked": false,
  "reused": false
}
```

无令牌读取（可直接发给第三方；也可拼上主机地址由浏览器打开）：

```bash
curl "http://localhost:8080/api/shares/rT4kPq7mWx2ZaB9c"
```

读取**只**返回以下脱敏字段——不含评员姓名、盲评码、个人答案或异样杯对应关系：

```json
{
  "sample_a": "茉莉",
  "sample_b": "玫瑰",
  "deadline": 1759002600000,
  "settled_at": 1759003000000,
  "valid_count": 6,
  "correct_count": 5,
  "abstained_count": 1,
  "missing_count": 2,
  "p_value": 0.0178,
  "distinguishable": true,
  "conclusion": "有效答卷 6 份、答对 5 份，单侧尾概率 p=0.0178 ≤ 0.05：判为可辨。",
  "now": 1759004000000
}
```

到期前撤销（撤销后读取立即被拒）：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/shares/rT4kPq7mWx2ZaB9c/revoke \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌"}'
```

规则与常见失败：

- **仅已结算实验**可生成：未结算返回 409；管理令牌错误 403；测试不存在 404。
- 有效期超出 1～604800 秒、类型不符返回 422；备注超 200 字返回 422。
- **幂等去重**：同一实验、同一备注（trim 后）、同一有效期重复请求返回**原凭证**
  （响应 `"reused": true`，即使原凭证已过期或已撤销也不新发）；
  同实验同备注但**有效期不同**返回 **409 内容冲突**，需更换备注或沿用原有效期。
- 读取：未知凭证 404；已撤销 403（`已被撤销`）；已到期 403（`已过期`）。
- 撤销：凭证不属于该实验 / 不存在 404；令牌错误 403；
  已撤销或已到期返回 409。
- 生成与成功撤销都会以 `share_create` / `share_revoke` 写入该实验的
  **操作时间线**（与凭证状态同一事务提交，失败操作不留记录）；时间线事件只含
  备注、有效期、到期时间与凭证前 4 位提示，**不含完整凭证明文**。
- 负责人进度接口 `GET /api/tests/{code}` 以纯增量字段 `shares[]`
  返回该实验全部凭证及其状态；管理台（`/admin`）在结算后提供
  「脱敏结果分享凭证」卡片，可直接生成、复制地址与撤销。

### 名单调整接口调用示例

`POST /api/tests/{test_code}/adjust`，请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `token` | string | 创建测试时发放的负责人令牌（必填） |
| `withdraw_codes` | string[] | 要撤回的盲评码；空/缺省表示不撤回 |
| `add_panelists` | string[] | 补位评员姓名（自动 trim、去空行）；空/缺省表示不补位 |

只补位：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/adjust \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌","add_panelists":["赵六","钱七"]}'
```

只撤回：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/adjust \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌","withdraw_codes":["kPq7mWx2Za"]}'
```

一次操作撤回并补位（整体原子，任一校验失败全部回滚）：

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/adjust \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌",
       "withdraw_codes":["kPq7mWx2Za","nR4tYv8QbC"],
       "add_panelists":["赵六","钱七"]}'
```

成功响应（截止前仍不披露新评员的异样杯位置与异样样本，仅返回其新专属码）：

```json
{
  "ok": true,
  "test_code": "AbCdEf12",
  "withdrawn": [{"panelist": "张三", "code": "kPq7mWx2Za"}],
  "added": [
    {"panelist": "赵六", "code": "A2bcDeFGhJ", "url": "/eval/code/A2bcDeFGhJ"}
  ],
  "progress": {"total": 9, "submitted": 0, "abstained": 0, "valid": 0}
}
```

常见失败（均为整次操作无效，名单保持原状）：

| HTTP | detail 含义 |
| --- | --- |
| 403 | 管理令牌无效 |
| 404 | 测试不存在，或撤回的盲评码无效/已撤回 |
| 409 | 已作答/已弃权者不可撤回；已截止或已结算，名单不可调整 |
| 422 | 姓名重复（含与已撤回者重名）、撤回码重复、空操作、有效人数低于最少有效数或超过 500 |

管理台页面（`/admin`）在截止前也提供「名单调整」卡片，支持按行粘贴盲评码与姓名直接操作。

### 提前结束收集接口调用示例

`POST /api/tests/{test_code}/end`，请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `token` | string | 创建测试时发放的负责人令牌（必填） |
| `reason` | string | 提前结束原因，**trim 后 1～200 字**（必填） |

```bash
curl -X POST http://localhost:8080/api/tests/AbCdEf12/end \
  -H 'Content-Type: application/json' \
  -d '{"token":"负责人令牌","reason":"有效答卷已达标，提前结算"}'
```

成功响应（冻结立即生效）：

```json
{
  "ok": true,
  "reused": false,
  "test_code": "AbCdEf12",
  "ended": true,
  "ended_at": 1759001000000,
  "end_reason": "有效答卷已达标，提前结算",
  "deadline": 1759002600000,
  "settled": false
}
```

规则与常见失败：

- 成功后名单与答卷入口**立即冻结**：评员 `GET /api/eval/{code}` 返回
  `ended=true / ended_at / end_reason`，再提交返回 **403**（`已提前结束收集`）；
  撤回 / 补位返回 **409**。
- **重复结束返回同一结束记录**：`reused=true`、`ok=false`，`ended_at` 与原因不变，
  时间线不重复追加；即使已经结算，仍返回该结束记录。
- 原因 trim 后为空或超过 200 字 → 422；管理令牌错误 403；测试不存在 404；
  **已过截止时间**（未提前结束）→ 409（保持原截止行为，无需提前结束）。
- 结束后可立即调用 `POST /settle`（截止时间未到也允许），仍按有效答卷、最少有效数
  与 p 值规则冻结结果；`settle` 事件在提前结束场景下带 `settled_after_end=true`。
- 负责人进度接口与时间线接口以纯增量字段返回 `ended / ended_at / end_reason`；
  结束原因与服务端时间以 `end` 事件进入操作时间线（同事务，失败不留痕）。
- 与并发答卷的竞争以 **BEGIN IMMEDIATE 写锁**为准：先拿到锁并提交的一方生效，
  不会出现“结束与答卷双赢”的脏中间态。

### 操作时间线接口调用示例

`GET /api/tests/{test_code}/timeline?token=负责人令牌`（仅该实验负责人可查询，
令牌错误 403、测试不存在 404）：

```bash
curl "http://localhost:8080/api/tests/AbCdEf12/timeline?token=负责人令牌"
```

响应（事件按 `seq` 即写事务提交顺序稳定排列，`ts` 为服务端毫秒时间）：

```json
{
  "test_code": "AbCdEf12",
  "settled": false,
  "now": 1759000000000,
  "history_complete": true,
  "events": [
    {"seq": 1, "ts": 1758999000000, "type": "create", "payload": {
        "sample_a": "茉莉", "sample_b": "玫瑰", "deadline": 1759002600000,
        "min_valid": 3,
        "panelists": [{"panelist": "张三", "code": "kPq7mWx2Za"}]}},
    {"seq": 2, "ts": 1758999300000, "type": "answer", "payload": {
        "panelist": "张三", "code": "kPq7mWx2Za",
        "abstained": false, "submitted": true}},
    {"seq": 3, "ts": 1758999400000, "type": "adjust", "payload": {
        "withdrawn": [{"panelist": "李四", "code": "nR4tYv8QbC"}],
        "added": [{"panelist": "赵六", "code": "A2bcDeFGhJ",
                   "url": "/eval/code/A2bcDeFGhJ"}],
        "active_total": 3}}
  ]
}
```

**盲态规则**：`settled=false` 时，`answer` 事件只有 `submitted/abstained` 状态，
**不含** `answer / odd_pos / odd_sample / correct`；`settled=true` 后同一时间线中
这些字段才会透出（弃权事件 `answer` 为 `null` 且不判定对错）：

```json
{"seq": 2, "ts": 1758999300000, "type": "answer", "payload": {
    "panelist": "张三", "code": "kPq7mWx2Za", "abstained": false,
    "answer": 2, "odd_pos": 2, "odd_sample": "B", "correct": true}}
```

事件类型：

| type | 含义 | 关键字段 |
| --- | --- | --- |
| `create` | 实验创建（与测试同事务） | 样本编号、截止、最少有效数、初始名单（评员 + 盲评码） |
| `answer` | 评员答卷 / 弃权（与答卷状态同事务） | 关联评员与盲评码；结算后才有选杯、异样杯、异样样本、对错 |
| `adjust` | 一次名单调整（撤回 + 补位同在一笔明细） | `withdrawn[]`、`added[]`、调整后有效人数 |
| `end` | 负责人提前结束收集（与冻结状态同事务；重复结束不追加） | 结束原因 `reason`、服务端结束时间（`ts`）、原 `deadline` |
| `settle` | 结算形成（与冻结状态同事务；重复结算不追加） | 有效 / 答对 / 弃权 / 未交数、p 值、判定；提前结束后立即结算时含 `settled_after_end=true` |
| `share_create` | 脱敏结果分享凭证生成（重复请求不追加） | 备注、有效期、到期时间、凭证前 4 位提示（不含完整凭证） |
| `share_revoke` | 分享凭证撤销（到期前；重复/过期撤销不追加） | 备注、有效期、生成/到期时间、凭证前 4 位提示 |
| `history_start` | 旧库升级后的记录起点标记 | `note` 说明既往操作不可追溯、`legacy_created_at` |

原子性：答卷、调整、结算的业务状态与对应事件在同一个 `BEGIN IMMEDIATE` 事务中提交，
任一校验失败整体回滚，时间线不会出现半截记录；并发操作以实际先拿到写锁并提交者为准，
事件 `seq` 顺序与数据库最终状态一致。

### 复测配对对照接口调用示例

同一批评员在两场实验中复测后，负责人可凭**两场实验的测试码与各自管理令牌**
做配对对照，判断辨别表现是否发生变化（精确双侧配对二项检验，即 McNemar 精确检验）。

`POST /api/compare`，请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `first_test_code` | string | 首场实验测试码（必填） |
| `first_token` | string | 首场实验负责人令牌（必填） |
| `second_test_code` | string | 次场实验测试码（必填） |
| `second_token` | string | 次场实验负责人令牌（必填） |

```bash
curl -X POST http://localhost:8080/api/compare \
  -H 'Content-Type: application/json' \
  -d '{"first_test_code":"AbCdEf12","first_token":"首场负责人令牌",
       "second_test_code":"XyZwVu34","second_token":"次场负责人令牌"}'
```

成功响应（仅汇总计数，不含任何个人答案、杯序或盲评码）：

```json
{
  "first_test_code": "AbCdEf12",
  "second_test_code": "XyZwVu34",
  "sample_a": "茉莉",
  "sample_b": "玫瑰",
  "sample_order_swapped": false,
  "paired_count": 9,
  "paired_valid_count": 8,
  "paired_invalid_count": 1,
  "unpaired_count": 2,
  "unpaired_first_only": 1,
  "unpaired_second_only": 1,
  "both_correct": 3,
  "only_first_correct": 4,
  "only_second_correct": 0,
  "both_wrong": 1,
  "p_value": 0.125,
  "changed": false,
  "conclusion": "有效配对 8 人：双方都对 3 人、仅首场对 4 人、仅次场对 0 人、都错 1 人；精确双侧配对二项检验 p=0.1250 > 0.05：证据未达阈值，不能判为辨别表现有变化。"
}
```

配对与判定规则：

- **前置校验**：两场必须是**不同**实验（同一测试码 422）；两场都必须**已结算**
  （未结算 409，指明是哪一场）；两场的**样本编号集合必须相同**——A/B 顺序可对调
  （响应 `sample_order_swapped` 标示次场是否对调），集合不一致 409；
  任一测试码不存在 404、对应令牌错误 403（指明是哪一场）。
- **配对**：按两场**有效名单**中姓名（去首尾空白后完全同名）配对；
  已撤回评员不参与配对。
- **有效配对**：双方均提交 1～3 杯答案才纳入统计；配对但任一方弃权或未交计入
  `paired_invalid_count`；仅出现在一场的评员计入 `unpaired_count`
  （并分场给出 `unpaired_first_only` / `unpaired_second_only`）。
- **四格汇总**：`both_correct`（双方都对）、`only_first_correct`（仅首场对）、
  `only_second_correct`（仅次场对）、`both_wrong`（都错）。
- **p 值**：仅按两类不一致对（仅首场对、仅次场对）人数计算**精确双侧配对二项检验**：
  n 为不一致对总数，`p = min(1, 2·P(X ≤ 较少一方人数))`，X ~ Binomial(n, 1/2)；
  **不一致对为 0 时 p = 1**。**无有效配对时 `p_value` 为 null**、`changed` 为 false，
  `conclusion` 说明无法比较。**p ≤ 0.05 才标记 `changed=true`（有差异）**。
- 该接口为**只读**：不改变两场实验的任何状态，也不写入操作时间线；
  响应只含汇总计数与结论，**不返回个人答案、杯序（异样杯位置/样本）或盲评码**。

## 安全与使用注意

- **负责人令牌只在创建测试时返回一次**，请立即保存；它与测试码共同构成管理凭证（服务端只存 SHA-256 哈希）。
- **脱敏结果分享凭证是不记名只读能力凭证**：持有者无需登录即可在有效期内读取脱敏结果，请通过点对点方式分发；到期或撤销前一直可读（非一次性读取，「一次性」指每次生成的随机凭证全局唯一）。凭证明文在服务端入库（与盲评码同级），管理时间线只记录其前 4 位提示，不记录完整凭证。
- 盲评码仅用于防混淆与一次性提交，不是身份认证：分发链接时应单独、点对点发给对应评员。
- 部署到不可信网络时建议置于 HTTPS 反向代理之后。
- 应用固定单 uvicorn worker（SQLite 单库写已串行化，满足实验规模；多 worker 也可借助 WAL 工作，但无必要）。

## 目录结构

```
app/
  main.py        # FastAPI 路由与业务逻辑（含操作时间线、脱敏结果分享凭证接口）
  db.py          # SQLite 连接、建表（tests/assignments/events/shares）、写事务、事件写入与旧库起点标记
  stats.py       # 二项分布单侧尾概率与精确双侧配对二项检验（复测对照）
static/
  index.html     # 负责人首页：创建 / 进入
  admin.html     # 负责人结算台：进度、分发、结算、回看、脱敏分享凭证管理
  eval.html      # 评员盲评页：三杯选择 / 弃权 / 截止后回看
  app.js, style.css
tests/smoke_test.py
Dockerfile, docker-compose.yml, requirements.txt
```
