# 阿里云 Docker 部署说明

本项目推荐使用 GitHub + Docker Compose 部署。以后本地改完代码后，只需要推送到 GitHub，服务器执行 `git pull` 和 `docker compose up -d --build`，不用再手动上传文件。

## 1. 首次部署

在阿里云服务器上安装好 Git、Docker 和 Docker Compose 后，进入你准备放项目的目录：

```bash
cd /opt
git clone https://github.com/2648156614/physics.git
cd physics
```

创建服务器专用环境变量文件：

```bash
cp .env.example .env
vi .env
```

至少填写这些值：

```env
SECRET_KEY=换成一串随机密钥
MYSQL_ROOT_PASSWORD=换成数据库root密码
MYSQL_PASSWORD=换成同一个数据库root密码
```

注意：`MYSQL_ROOT_PASSWORD` 用于初始化 MySQL 容器，`MYSQL_PASSWORD` 用于 Web 应用连接数据库。两者建议保持一致。已经运行过的服务器不要随意改数据库密码，否则应用可能连不上旧数据库。

启动服务：

```bash
docker compose up -d --build
```

如果服务器使用的是旧版 Docker Compose，命令改成：

```bash
docker-compose up -d --build
```

查看运行状态：

```bash
docker compose ps
docker compose logs -f --tail=100 web
```

浏览器访问：

```text
http://你的服务器公网IP/
```

## 2. 日常更新代码

本地代码修改完成后，推送到 GitHub：

```bash
git add .
git commit -m "本次修改说明"
git push
```

然后在阿里云服务器执行：

```bash
cd /opt/physics
git pull
docker compose up -d --build
```

如果想看启动日志：

```bash
docker compose logs -f --tail=100 web
```

## 3. 一键更新脚本

可以在服务器项目目录创建 `deploy.sh`：

```bash
vi deploy.sh
```

写入：

```bash
#!/bin/bash
set -e

cd /opt/physics
git pull
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 web
```

保存后赋予执行权限：

```bash
chmod +x deploy.sh
```

以后更新只需要：

```bash
./deploy.sh
```

## 4. 重要数据说明

不要把服务器上的 `.env` 上传到 GitHub。它包含密钥和数据库密码，本项目已经通过 `.gitignore` 忽略。

Docker Compose 使用了这些持久化卷：

```text
db_data       MySQL 数据
redis_data    Redis AOF 数据
uploads_data  题目图片
avatars_data  头像文件
```

日常更新代码时不要删除这些卷。尤其不要随便执行：

```bash
docker compose down -v
```

这个命令会删除数据库和上传文件对应的数据卷。

正常重启或重建使用：

```bash
docker compose up -d --build
```

或：

```bash
docker compose restart web
```

## 5. 回滚版本

如果新版本有问题，可以先查看提交记录：

```bash
git log --oneline -5
```

回到某个提交：

```bash
git checkout 提交号
docker compose up -d --build
```

确认没问题后，如果要回到主分支继续更新：

```bash
git checkout main
git pull
docker compose up -d --build
```

## 6. 常用排查命令

查看容器：

```bash
docker compose ps
```

查看 Web 日志：

```bash
docker compose logs -f web
```

查看 MySQL 日志：

```bash
docker compose logs -f db
```

重启 Web：

```bash
docker compose restart web
```

完全重建 Web 镜像：

```bash
docker compose build --no-cache web
docker compose up -d
```
