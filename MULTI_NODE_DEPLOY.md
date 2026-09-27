# 两台本地服务器联合部署

本方案适用于两台服务器位于同一个校园局域网的考试场景。学生只访问主节点地址，Nginx 将普通学生请求分配给两个 Web 节点；两个节点共享同一个 MySQL 和 Redis。

## 1. 请求是否会被执行两次

不会。Nginx 每次只选择一个 Web 节点处理请求。配置没有启用 `proxy_next_upstream non_idempotent`，因此答案提交这类 POST 请求不会被 Nginx 自动转发到第二台机器重做。

应用还使用 Redis 提交锁。同一学生对同一题存在一个尚未完成的提交时，第二次点击会收到“答案正在提交”，不会同时写入数据库。

## 2. 节点分工

建议使用以下地址，部署时替换成真实固定 IP：

| 节点 | 示例地址 | 服务 |
| --- | --- | --- |
| Linux 主节点 | `192.168.10.10` | Nginx、Web 1、MySQL、Redis、图片 |
| Windows 工作节点 | `192.168.10.11` | Web 2 |

两台机器必须接入同一个交换机或校园局域网，并使用有线连接和固定 IPv4 地址。

## 3. 首次部署主节点

在 Linux 主节点获取完全相同的项目版本，然后执行：

```bash
cd /opt/physics
cp deploy/env.primary.example deploy/env.primary
vi deploy/env.primary
```

必须修改：

```env
PRIMARY_LAN_IP=主节点真实IP
WORKER_HOST=工作节点真实IP
SECRET_KEY=两台节点完全相同的长随机值
MYSQL_ROOT_PASSWORD=数据库管理员密码
MYSQL_PASSWORD=应用数据库密码
REDIS_PASSWORD=Redis密码
REDIS_URL=redis://:Redis密码@redis:6379/0
```

密码建议只使用大小写字母和数字，避免 Redis URL 中出现需要转义的字符。

启动主节点：

```bash
chmod +x deploy/deploy-primary.sh
./deploy/deploy-primary.sh
```

确认主节点：

```bash
curl http://127.0.0.1:5001/health
curl http://主节点IP/load-balancer-health
```

首次启动必须先等主节点的数据库初始化完成，再启动工作节点。

## 4. 首次部署工作节点

Windows 安装 Docker Desktop，并切换到 Linux Containers。获取与主节点完全相同的 Git 提交后，在 PowerShell 执行：

```powershell
cd C:\physics
Copy-Item deploy\env.worker.example deploy\env.worker
notepad deploy\env.worker
```

必须修改：

```env
PRIMARY_LAN_IP=主节点真实IP
MYSQL_HOST=主节点真实IP
REDIS_URL=redis://:Redis密码@主节点真实IP:6379/0
SECRET_KEY=与主节点完全相同
MYSQL_PASSWORD=与主节点应用密码完全相同
```

启动工作节点：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-worker.ps1
```

工作节点的 `RUN_DB_MIGRATIONS=false` 不要修改。它只提供 Web 计算能力，不负责建表和升级数据库。

## 5. 防火墙和连通性

工作节点需要能够访问主节点：

```powershell
Test-NetConnection 主节点IP -Port 3306
Test-NetConnection 主节点IP -Port 6379
```

主节点需要能够访问工作节点：

```bash
curl http://工作节点IP:5000/health
```

Windows 防火墙只向主节点 IP 放行工作节点的 5000 端口。主节点的 3306 和 6379 也只应向工作节点 IP 放行。学生电脑只需要访问主节点的 80 或 443 端口。

## 6. 验证两台机器共同工作

直接检查两个节点：

```bash
curl http://127.0.0.1:5001/health
curl http://工作节点IP:5000/health
```

返回结果中的节点应分别为：

```text
physics-web-1
physics-web-2
```

两边的 `version` 必须完全相同，`database` 和 `redis` 都必须是 `connected` 或 `redis`。然后通过主节点地址登录、进入考试、答错刷新、答对保存，并分别查看两个 Web 容器日志，确认两边都收到过请求。

## 7. SEB 配置

SEB 的 Start URL 和 Quit URL 必须填写学生统一访问地址，例如：

```text
http://192.168.10.10/seb/start/考试编号
```

不要填写工作节点的 `:5000` 地址。最终域名或 IP 确定后再生成 Config Key，因为 SEB 验证包含完整访问地址。

## 8. 更新版本

每次更新必须让两台节点处于同一个 Git 提交：

1. 先更新并部署工作节点。
2. 检查工作节点 `/health` 的版本。
3. 再更新并部署主节点。
4. 检查主节点和统一入口。

更新期间 Nginx 会把不可用节点暂时移出。不要同时停止两台 Web 服务，也不要在工作节点执行数据库结构升级。

## 9. 考试前检查

- 两台服务器关闭睡眠、休眠和计划重启。
- Windows Docker Desktop保持运行。
- 两台服务器使用有线网络和固定 IP。
- 备份 MySQL 数据库和主节点上传图片卷。
- 提前预热题目池。
- 使用与正式环境相同的 SEB 配置完成整场模拟考试。
- 使用压测工具验证登录、进入考试、提交答案和切题流程。
- 考试期间不修改题目或上传图片。
