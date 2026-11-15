# 食品厂过敏原标签核对系统

原料声明变更后，核对每款成品应标注过敏原，并在成品抽检异常时定位可能漏报的缺声明原料。
技术栈：**FastAPI + SQLite（WAL）+ 原生 HTML/JS**，无外部服务依赖。

## 业务规则实现

| 规则 | 实现方式 |
| --- | --- |
| 原料声明 CSV（原料号、修订号、过敏原）上传 | 整版替换；支持 UTF-8（含 BOM）/GBK 编码，过敏原支持逗号、顿号、分号分隔 |
| 配方关系 CSV（父项、子项）上传 | 整版替换；每行一条父子边，支持半成品复用与任意层级嵌套 |
| 按每种原料**最高修订号**推导 | `derive.py` 对配方树后序遍历，同原料只取 `max(修订号)` 的过敏原集合 |
| 成品过敏原 + 来源原料 | 每种过敏原归集全部来源原料号，页面可展开查看 |
| 缺声明 → 标注“待核” | 叶子原料无任何声明时成品状态为 `pending`，并列出缺声明原料；**绝不当作无过敏原**。显式留空过敏原的修订表示“确认无”，与缺声明语义不同 |
| 配方成环 → 拒收整份关系文件 | 三色 DFS 检测，返回环路径（如 `S1 -> S2 -> S1`）；旧配方与版本号不变 |
| 同号同修订异内容 → 拒收声明 | 解析阶段收集冲突行号并整体拒绝，旧声明保留 |
| 更正声明后结果联动更新 | 每次成功提交 `state_version` 自增，标注按当前已提交状态即时重算 |
| 并发上传与查询的一致性 | SQLite WAL + 进程内写锁；`/api/state` 在单事务内读取同一快照，配方、声明、标注保证来自同一 `state_version`；写入先校验后事务，失败回滚保留旧结果 |

## ④ 成品抽检排查（新增）

质控员可指定一种过敏原，上传含 `成品号, 抽检结果`（阳性/阴性）的 CSV，
系统在**当前已提交版本**的配方与声明上排查“可能漏报该过敏原的缺声明原料”。
实现见 `app/investigate.py`（纯函数，只读快照）。

| 规则 | 实现方式 |
| --- | --- |
| 已知声明能解释的阳性 → 不归咎缺声明原料 | 成品子树声明中含该过敏原时归为“已解释”，不参与覆盖求解，并列出声明来源 |
| 已知声明含该过敏原却抽检阴性 → 冲突 | 标记“声明与抽检冲突”，该阴性**不作为排除证据**（不排除任何候选原料） |
| 有效阴性 → 排除其子树内候选 | 无声明冲突的阴性成品中出现的缺声明叶子原料被排除，不进入任何组合 |
| 覆盖全部待解释阳性 | 每个待解释阳性的配方子树至少命中一种被选原料 |
| 最小组合 + 全部并列 | 按候选数从小到大枚举组合，返回全部并列最小解，并列出每个组合对每个阳性的覆盖关系 |
| 无解说明原因 | 分为“候选被阴性全部排除”“阳性无缺声明叶子”等，逐条列出具体成品 |
| 规模上限 | 单次最多 **12 个候选原料、20 款成品**，超出返回 400 并给出现值；未识别的成品号同样 400 |
| 结果过期提示 | 排查结果记录 `analyzed_state_version`；配方或声明更新后页面显示“旧排查结果已过期”，内容置灰仅供追溯 |
| 不修改声明/标签 | 排查只读快照并追加一条 `investigations` 记录，不改变 `state_version`、声明与成品标注 |

抽检 CSV 的阳性/阴性兼容 `positive/negative/pos/neg/+/-` 等写法；
同一成品重复出现且结论不一致时整份拒收并给出冲突行号。

### 演示数据

- `samples/investigation_declarations.csv` + `samples/investigation_relations.csv`
  - `M03/M04/M05` 为缺声明叶子，`M10` 已声明含花生；
  - `FG10`、`FG14` 含 `M10`（前者可解释阳性，后者用于冲突阴性）；
  - `FG11` 含 `M03/M04/M05`，`FG12` 含 `M03/M04`，`FG13` 含 `M05`，
    `FG15` 仅含已声明原料，`FG16` 含 `M03/M04/M05`（无解样例用）。
- `samples/sampling.csv`（并列最小解 + 冲突阴性）、`sampling_conflict.csv`
  （冲突阴性改成阳性）、`sampling_impossible.csv`（无解原因）、
  `sampling_no_blame.csv`（阳性均已被声明解释）、`sampling_no_positive.csv`。
- `sampling_limits.csv` 演示超过 20 款成品的上限报错。

## 目录结构

```
allergen-label/
├── app/
│   ├── parse.py       # CSV 解析与行级校验（声明/配方/抽检）
│   ├── graph.py       # 环检测、成品（根节点）识别
│   ├── derive.py      # 最高修订选取、过敏原与来源推导、待核判定
│   ├── investigate.py # 抽检排查：分类、阴性排除、并列最小组合、无解原因
│   ├── storage.py     # SQLite 持久化、WAL、整版原子替换、版本号、排查结果
│   ├── service.py     # 业务编排（上传校验 + 状态推导 + 排查）
│   └── main.py        # FastAPI 路由与静态页面
├── web/index.html     # 单页网页（上传、版本状态、标注表、抽检排查、过期提示）
├── samples/           # 可直接上传的演示 CSV（含冲突、成环、更正、抽检样例）
├── tests/test_all.py  # 42 项端到端自测
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 方式一：Docker Compose 启动（推荐）

```bash
cd allergen-label
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `allergen-data`（容器内 `/data/allergen.db`），
容器重建后数据不丢；内置健康检查。

## 方式二：本地 Python 启动

```bash
pip install -r requirements.txt
ALLERGEN_DB=./data/allergen.db uvicorn app.main:app --host 0.0.0.0 --port 8000
# 浏览器打开 http://localhost:8000
```

## 浏览器演示流程

1. 打开首页，在 **① 原料声明** 选择 `samples/declarations.csv` → 上传并整版替换；
2. 在 **② 配方关系** 选择 `samples/relations.csv` → 上传；
3. 成品表出现 3 款成品：
   - `FG01` 已确定：乳、蛋、麸质（经 S2→S1 两层嵌套汇总，可展开看来源）；
   - `FG02` **待核**：缺声明原料 M05 被明确标出，不按无过敏原处理；
   - `FG03` 已确定、无过敏原（M06 声明显式为空）；
4. 上传 `samples/declarations_conflict.csv` → 400 拒收并提示冲突行，旧声明不变；
5. 上传 `samples/relations_cycle.csv` → 400 拒收并给出环路径，旧配方不变；
6. 上传 `samples/declarations_corrected.csv`（补齐 M05=坚果）→
   `FG02` 由待核变为已确定并标注“坚果”，版本号自增，其余成品不受影响。

抽检排查演示：

7. 上传 `samples/investigation_declarations.csv` 与
   `samples/investigation_relations.csv`（注意：二者会整版替换前面的样例数据）；
8. 在 **④ 成品抽检排查** 输入过敏原 `花生`，选择 `samples/sampling.csv` → 开始排查：
   - `FG10` 阳性由已声明 `M10` 解释，不归咎缺声明原料；
   - `FG14` 阴性但声明含花生 → 标记冲突，其阴性**不**排除 `M03`；
   - `FG11`/`FG12` 阳性待解释，`M05` 因出现在有效阴性 `FG13` 被排除，
     结果为 `[M03]` 与 `[M04]` 两个并列最小组合；
9. 再上传 `samples/declarations_corrected.csv` → 顶部出现“旧排查结果已过期”。

也可运行 `bash scripts/demo.sh` 用 curl 跑同一套流程。

## 自测

```bash
python -m unittest discover -s tests -v
```

覆盖：CSV 校验、GBK 编码、重复去重、冲突拒收、环/自环检测、嵌套复用推导、
缺声明待核、最高修订生效、整版替换语义、失败保留旧结果、更正联动、
API 全流程与并发读写同版本一致性；抽检 CSV 校验、声明解释、冲突阴性不作排除、
并列最小组合（含最小规模 > 1 的多组并列）、无解原因、12/20 上限、未识别成品、
只读不改声明、结果过期标记（共 42 项）。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前已提交版本的声明、配方与全部成品标注（同一快照） |
| POST | `/api/declarations` | 上传声明 CSV，multipart 字段 `file`，整版替换 |
| POST | `/api/relations` | 上传关系 CSV，multipart 字段 `file`，整版替换 |
| POST | `/api/investigations` | 指定过敏原做抽检排查：multipart 字段 `allergen` + `file`（抽检 CSV） |
| GET | `/api/health` | 健康检查 |

校验失败统一返回 `400`：`{"error": "...", "details": ["第 N 行：..."]}`，
且不产生任何写入。抽检排查的 `400` 同样不写库；成功时返回结果并记录
`analyzed_state_version`，`/api/state` 的 `investigation.stale` 表明该结果是否已过期。
