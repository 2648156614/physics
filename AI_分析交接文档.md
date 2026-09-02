# 物理考试 / 学情分析系统 — AI 分析交接文档

> 用途：把项目现状、已完成的 Docker 化改造、以及"学情 AI 诊断"模块的落地差距一次性交代清楚，方便交给另一个 AI 做下一步开发或审计。
> 配套文件清单见文末「第七节 · 建议提供给 AI 的文件」。

---

## 一、项目介绍（是什么）

一个**基于 Flask 的物理在线考试 / 练习系统**，核心定位是"逐题自适应练习"：

- 学生登录后逐题作答，**答错会自动重出同知识点新题、答对所有题才算 `completed_all`（毕业）**，不是传统的一次性整卷考试。
- 后端用 **MySQL 8** 存用户、题目模板、答题记录；用 **Redis** 做题目池缓存（高并发下减少 sympy 重算）。
- 题目由 **模板 + sympy 公式引擎** 动态生成：模板里写 `solution_formula`，引擎代入随机变量算出标准答案。
- 自带管理后台（导入学生、看完成率、导出答题数据等）和登录安全审计。
- 技术栈：Python 3.9 + Flask + Waitress（生产 WSGI 服务）+ mysql-connector + redis + openpyxl + numpy + sympy。

**当前运行形态**：已在阿里云 Linux 上用 Docker Compose 规范化部署（`web` / `db` / `redis` 三服务），目标是"以后整体搬迁只需搬目录 + 配镜像源"。

---

## 二、本次已经完成的工作（Docker 化改造）

针对从 Windows 搬到 Linux + Docker，已补齐/修复以下内容：

| 类别 | 内容 |
|---|---|
| 缺失文件补齐 | `requirements.txt`（补了被漏掉的 `waitress`、`python-dotenv`，并锁定 Py3.9 兼容版本）、`docker-entrypoint.sh`（Dockerfile 原本引用却不存在）、`.dockerignore`、`.env.example` |
| Dockerfile | 加 `PYTHONUNBUFFERED=1`（否则 `docker logs` 长时间空白）、装 tzdata 并设 `TZ=Asia/Shanghai`、加 CRLF 兜底转换 |
| docker-compose.yml | 补 `redis_data:/data`（原开了 AOF 却没挂卷，等于没持久化）、三服务统一时区、`./logs:/app/logs`、MySQL 加 utf8mb4 / max_connections / skip-name-resolve |
| .env | 修变量名 `PREWARM`→`PREWARM_ON_START`（原变量名不匹配导致预热从未生效）、`WAITRESS_THREADS` 64→32（与 mysql-connector 连接池硬上限 32 对齐）、补 `DB_POOL_SIZE`、`TZ` |
| start_server.py | 补调 `ensure_performance_indexes()`（原只在 app.py `__main__` 里调，容器入口是 start_server.py，导致 9 个高并发索引在 Docker 下从未建） |
| db.py | 连接池初始化加锁（防多线程重复建池）；取连接失败时重试 5×0.15s（原池满直接返回 None，调用方 `conn.cursor()` 抛 AttributeError → 500） |

**已验证**：17 个依赖在 Py3.9/Linux 全部为预编译 wheel；waitress 3.0.1 仍支持 `asyncore_use_poll`；compose YAML 与卷声明通过校验；Python 语法与 import 名均通过。

> 重要提醒（部署前必看）：
> 1. **旧库数据迁移没做**——直接起新库只会重建 7 道内置模板，原学生/题库/答题记录全部丢失，需 Windows 侧 `mysqldump` 后导入。
> 2. 阿里云安全组需放行 80 端口；镜像拉取超时需配 registry mirror（建议用阿里云专属加速地址）。
> 3. 默认管理员口令硬编码为 `@admin123`，上线后立刻改。

---

## 三、学情 AI 诊断模块分析（文档：学情分析系统_需求与方案总结.md）

该文档描述了一套**尚未实现的"学情分析"功能**（双层架构：规则引擎打错因标签 + LLM 出诊断书）。对照实际代码后结论：**底层底座已在，整层功能未建**。

### 文档可取之处（已用代码印证，可信）
- 触发锚点选 `completed_all` 的 False→True 翻转（系统无"交卷"概念，这点判断精准）。
- 沿用现有约 1% 判分容差，不照搬文档原方案的 1e-3。
- 复用现有 `classify_error_type` 作兜底（命中精确→回落粗分→随机/未知）。
- 复用 `build_formula_context` + sympy 安全上下文。
- 先线程 worker + 轮询、不引 MQ/WebSocket，契合单体 Flask 现状。
- 建表写进源代码、幂等自动生效，与现有 `repair_database()` 范式一致。

### 文档需要打折扣之处
- **内容工程（Step 6）被低估**：需给约 60 个物理模板填知识点与通病公式，是"教师内容活"，未填前精确错因几乎不会命中，大部分回落粗分。
- "标签 100% 准确"软化的力度还不够，前端文案要再降一档。
- 开关 `ENABLE_LLM_ANALYTICS` 应从 Step 0 就前置，而非放最后。

### 模块做了什么（Step 0–7 拆解）
- **Step 0** 建列/建表（`topic`、`error_distractors`、`error_tags`；新表 `exam_submissions`、`exam_ai_analytics`），写进 `repair_database()`，可重复执行。
- **Step 1** 通病匹配引擎：学生错答 vs 常见错误公式，命中给精确错因标签。
- **Step 2** 接入判分链，落库 `error_tags`，主流程 try/except 包裹。
- **Step 3** `completed_all` 翻转瞬间汇总错题标签、入队（`exam_ai_analytics` 状态 PENDING）。
- **Step 4** 进程内 daemon 线程 Worker 消费，调 DeepSeek/Qwen（OpenAI 兼容、JSON 模式、超时+重试），写回诊断书。
- **Step 5** 新增 `/api/analytics/<id>` 轮询端点 + 前端看板。
- **Step 6** 给模板填知识点与通病公式（最大隐性成本）。
- **Step 7** LLM 超时/重试/降级、失败重跑、整体开关。

### 对照已部署代码的现状
- ✅ 已具备：判分流程、`is_correct` 容差、逐题自适应重试、多答案逐空判分、`classify_error_type`、`build_formula_context`/`evaluate_solution_formula`、Docker 部署层、`repair_database()` 范式。
- 🟡 部分：有 `completed_all` 翻转写入，但**没有"检测翻转"的钩子**，也无入队逻辑。
- ❌ 完全缺失：`exam_submissions`/`exam_ai_analytics` 表、`topic`/`error_distractors`/`error_tags` 列、`distractor_engine.py`/`analytics_pipeline.py`/`analytics_worker.py`/`llm_client.py`、`/api/analytics/<id>` 端点与前端看板、`LLM_*` 配置。
- 🔴 安全债：`evaluate_solution_formula`（question_generation_service.py:73）仍在用 `eval(solution_formula)`，公式来自数据库，存在表达式注入风险，应改 `sympy.sympify`/`parse_expr`。

---

## 四、给下一步开发的核心建议
1. 按文档 Step 0→5 顺序落地，先让闭环跑通（诊断书内容可以为空），再铺 Step 6 内容。
2. 把 `ENABLE_LLM_ANALYTICS`、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 提前写进 `config.py` 与 `.env`。
3. 触发点改 `update_user_completion_status`，在 `completed_all` 由 False→True 时调 `create_submission_and_enqueue`。
4. 上线前先试点 3~5 个高频模板的 distractor，别一上来铺 60 个。
5. 顺手把 `eval` 换成 sympy 安全求值。

---

## 五、项目目录结构（当前）
```
physics_project/
├── app.py                     # 主应用，约 2588 行（最重要也最大）
├── config.py                  # 环境变量、DB_CONFIG、REDIS_URL、安全参数
├── db.py                      # MySQL 连接池（已加固）
├── extensions.py              # Flask app、Redis 客户端（含内存降级）
├── start_server.py            # 生产入口（Waitress），已补索引
├── Dockerfile / docker-compose.yml / docker-entrypoint.sh
├── requirements.txt / .env / .env.example / .dockerignore
├── routes/                    # auth.py, admin.py, exam.py, question.py, student.py
├── services/                  # question_generation_service.py（公式引擎）、redis_cache_service.py
├── utils/                     # datetime/file/request/security 工具
├── static/  templates/  logs/
```

---

## 六、关键代码位置速查（给 AI 直接定位）
| 关注点 | 文件:行 |
|---|---|
| 粗分错因 | app.py:1171 `classify_error_type` |
| 保存答题（多答案/逐空） | app.py:1197 `save_user_response` |
| 完成状态更新 | app.py:2041 与 2119 两处 `update_user_completion_status`（后者覆盖前者，遗留死代码） |
| 幂等建表 | app.py `repair_database` / `ensure_performance_indexes` |
| 公式引擎 + eval 风险 | services/question_generation_service.py:60 `build_formula_context`、:73 `evaluate_solution_formula` |
| 连接池配置 | config.py:39 `DB_CONFIG`、:47 `MYSQL_CONNECTOR_POOL_MAX_SIZE=32` |
| 触发判分入口 | routes/exam.py:710 附近调用 `classify_error_type`/`save_user_response` |
| 现有"导出"非诊断 | routes/admin.py:767 `admin_export_all_student_analytics` |

---

## 七、建议提供给 AI 的文件清单（用于分析 / 继续开发）

> 原则：尽量全给；`app.py` 过大，按下方"裁切说明"提供相关片段即可，不必整文件。

### 必给（核心逻辑）
1. **学情分析系统_需求与方案总结.md** —— 功能蓝图，AI 据此实现 Step 0–7。
2. **config.py** —— 环境变量、DB/Redis 配置、安全参数，AI 需在此加 `LLM_*` 与 `ENABLE_LLM_ANALYTICS`。
3. **db.py** —— 连接池范式，AI 需复用 `get_db_connection()`。
4. **app.py 关键片段**（不要整文件，用下面的行号区间）：
   - `classify_error_type`（约 1171–1195）
   - `save_user_response`（约 1197–1301）
   - 两处 `update_user_completion_status`（约 2041–2194，注意重复定义）
   - `repair_database`（全文，作为 Step 0 建表载体）
   - `initialize_database`（全文，了解现有表结构）
   - `__main__` 启动序列（约 2547–2588）
5. **services/question_generation_service.py** —— 重点给 `build_formula_context` / `evaluate_solution_formula` / `coerce_formula_answers`（约 55–101 行），以及 `generate_problem_from_template`（约 104 起）了解变量如何代入。

### 应给（运行上下文）
6. **routes/exam.py** —— 判分/提交路由（找 `api_submit` 或调用 `save_user_response` 之处，作为 Step 2/3 接入点）。
7. **routes/auth.py** —— 含"后台审计线程 worker"范式，可作为 Step 4 Worker 的参考写法。
8. **extensions.py** —— Redis 客户端与内存降级，AI 需了解缓存现状。
9. **docker-compose.yml / Dockerfile / docker-entrypoint.sh / .env / requirements.txt** —— 让 AI 理解运行环境与 env 注入机制，新增 `LLM_*` 时要往 `.env` 和 compose 的 env 里加。

### 可选（按需）
10. **routes/admin.py** 中 `admin_export_all_student_analytics`（约 767）与 `admin_all_problems_stats` 相关模板 —— 若要让诊断看板复用现有管理界面样式。
11. **templates/** 中完成页模板（找渲染"毕业/完成"的页面，如 `student_*.html` 或 `exam_*.html`）—— Step 5 前端看板要挂这里。

### app.py 过大时的裁切说明
- app.py 约 2588 行、101KB，直接整贴易超上下文。
- 按第六节行号区间复制对应函数即可；其余路由/视图函数 AI 开发 Step 0–7 时基本用不到。
- 若 AI 需要全局视图，可额外提供 `app.py` 的"函数/路由清单"（用 `grep -n "def \|@.*route"` 生成的一页索引）代替全文。

### 安全提示
- 提供 `.env` 时可把 `SECRET_KEY`、`MYSQL_PASSWORD` 的值替换为占位符（如 `****`），避免密钥外泄；结构保留即可。
