# AI 智能生题试验版

本功能在 `codex/ai-question-generation` 分支开发。未获教师确认前不合入 main，不能使用固定拉取 main 的 `deploy.sh` 部署试验版。

## 使用流程

进入测试题库，点击“智能生题”。先展开“默认生成规范”保存要求，以后上传题目图片或输入文字即可生成。学段、难度、知识点、多问处理、最多答案数和学生示意图均可在“题目设置与示意图”调整。

原题截图保存在管理员专用目录；它不作为学生题图。需要图示时另传 PNG、JPEG 或 WebP 示意图，入库前确认图上没有与随机参数冲突的数字。图片每张最多 6MB。

Agent 设计方案 → 程序穷举 → 最多两次错误反馈修正 → 显示样题与验算报告。条件不完整或暂不适用会停止，不勉强生成。教师核对题意、物理模型和图示后点击“加入当前题库”。

补充要求后重新生成会另存草稿，保留旧版；已入库题目的编辑入口也会引导回生成记录。当前流程将修订作为新模板加入，原模板是否移出题库由教师通过原有题库操作决定，不自动替换历史题目。

无需 AI 密钥也可展开“模板验证与修改”，粘贴 `examples/ai-circle-template.json`，验证圆周运动示例。该例有 10 个目标答案、40 组有效可见参数组合。导入的是标准 JSON，不能粘贴 Python 的 `r"""..."""` 字符串格式。

## Windows 本机独立试运行

位置：Windows PowerShell，项目目录 `C:\Users\26481\Desktop\2026physics`。需要已安装并启动 Docker Desktop。

```powershell
Set-Location C:\Users\26481\Desktop\2026physics
git switch codex/ai-question-generation
Copy-Item -LiteralPath deploy/ai-test.env.example -Destination .env.ai-test
notepad .env.ai-test
```

只在第一次创建 `.env.ai-test` 时复制示例。填写新的 `SECRET_KEY`、测试用 `MYSQL_ROOT_PASSWORD`，以及支持图片和严格 JSON Schema 的 `AI_MODEL`、`AI_API_KEY`。不要复制正式 `.env`，不要把密钥发到聊天或提交 Git。接口默认 `https://api.openai.com/v1`；也可配置支持相同 Responses 协议的 HTTPS 服务，不支持严格格式时会明确失败，不降级为普通文本解析。

```powershell
docker compose --env-file .env.ai-test -p physics-ai-test -f docker-compose.ai-test.yml up -d --build
docker compose --env-file .env.ai-test -p physics-ai-test -f docker-compose.ai-test.yml ps
Invoke-RestMethod http://127.0.0.1:5080/health
```

浏览器打开 `http://127.0.0.1:5080`，登录测试库管理员，在测试题库试用。此配置的 MySQL、Redis、图片和原题截图各有独立数据卷，不映射正式数据库端口。Web 只监听本机 5080。

暂停试验，保留全部测试数据：

```powershell
docker compose --env-file .env.ai-test -p physics-ai-test -f docker-compose.ai-test.yml stop
```

## 云服务器独立试运行

位置：云服务器 Linux Shell，**新目录 `/root/physics_ai_test`**，不在正式 `/root/physics_project` 切分支。

首次准备：

```sh
git clone --branch codex/ai-question-generation https://github.com/2648156614/physics.git /root/physics_ai_test
cd /root/physics_ai_test
cp deploy/ai-test.env.example .env.ai-test
nano .env.ai-test
```

填写新的测试配置后执行：

```sh
docker compose --env-file .env.ai-test -p physics-ai-test -f docker-compose.ai-test.yml up -d --build
docker compose --env-file .env.ai-test -p physics-ai-test -f docker-compose.ai-test.yml ps
curl -fsS http://127.0.0.1:5080/health
```

Windows PowerShell 建立隧道，保持终端打开：

```powershell
ssh -L 5080:127.0.0.1:5080 root@115.29.214.153
```

再从本机浏览器访问 `http://127.0.0.1:5080`。后续更新只在 `/root/physics_ai_test` 执行 `git pull --ff-only` 和上述 compose 构建命令。

## 编译规则和边界

- 固定八个模板字段。接口传输中的动态映射使用封闭 Schema 的命名数组，服务器转换成 `hidden_vars`、`key_vars` 等映射。`analysis` 独立存储推导、范围依据、全部变量的 SI 单位和物理约束，不能伪造程序报告。
- 安全 AST 解释，仅接受 `+ - * / ** sqrt sin cos abs pi`，常整数幂为 -8 到 8。禁止属性、索引、字符串代码及任意函数调用。表达式限制长度和节点数。
- `range` 是允许取到端点的闭区间，不是连续随机分布。被抽取参数必须给有限 `choice` 候选；关联变量可用 `parameter_cases`。最多穷举 2000 个候选组合，保留 30 到 150 个不同可见参数组合；每个独立目标须有 8 到 15 个候选值，实际保留至少 8 种。
- 单位检查采用 SI 量纲。带单位的常数需声明为固定候选变量；目前不自动把 cm 等输入换算成 m。量纲一致不代表物理模型一定正确，教师仍需确认。
- 不允许通过四舍五入把不合格参数“变漂亮”。显示为整数、最多三位有限小数或简单分数后，按显示值重新计算答案，必须与目标吻合。
- 编译器在独立进程运行，25 秒超时；Linux 另限制内存和 CPU 时间。歧义反推解、循环派生、非有限实数、越界或不漂亮的参数被排除，有效组合不足则拒绝。
- 编译结果由服务器签名。学生请求只选已保存的有效组合、替换题干，无 AI、无公式执行、无 SymPy 求解；Redis 预热继续复用原有功能。新严格模板不静默回退，旧模板保留原随机规则。
- 只有管理员能制作和入库；写请求有 CSRF 校验。入库用事务、草稿锁和考试行锁，重复点击返回同一模板编号；已进入学生或有关联归档考试的题库拒绝新增。
- `ai_question_drafts`、`ai_question_settings` 在功能启用后的首次管理员请求幂等创建，不在服务器启动或学生请求中编译。草稿保存在 MySQL，超过 7 分钟无进展的任务标为失败，保留输入供重试。
- 后台单进程一个任务执行、最多两个任务占位，适合管理员少量制题。多工作节点各自有容量限制；没有实现跨节点全局任务配额。
- 题库备份新增保留生成策略。编译题备份恢复先验证本站签名，再因图片重命名重新签名；其他密钥的安装不能直接信任这些组合，需用原草稿在目标站重新编译。更换 SECRET_KEY 也需要重新编译。

## 验证方式

Windows PowerShell，项目目录；本机已存在 `.venv-history` 测试运行时：

```powershell
.venv-history/Scripts/python.exe -X utf8 -m unittest discover -s tests -v
node --check static/js/ai-questions.js
node tests/ai_questions_ui.cjs
```

界面测试需要 Playwright 与本机 Edge。测试数据库使用 `outputs/ai-tests` 下临时 SQLite 文件，适配 MySQL 语法；模型接口使用模拟响应。它们不连接正式 MySQL/Redis，也不验证真实模型识图质量。独立 Docker/MySQL、真实模型和校园网络仍需试运行验收。
