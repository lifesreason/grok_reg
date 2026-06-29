# Grok Register Web

Grok 注册自动化工具，已改为 Web 控制台方式运行。适合在 NAS 上通过 Docker Compose 部署，并由 GitHub Actions 自动构建 Docker 镜像到 GHCR。

## 功能

- Web 页面编辑邮箱服务商、Cloudflare/CloudMail、并发、代理和 grok2api 入池配置。
- 后台线程执行注册任务，页面实时查看状态和日志。
- 配置、成功账号、邮箱凭据写入持久化数据目录。
- GitHub Actions 推送 `master` 后自动构建镜像。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn web_app:app --host 0.0.0.0 --port 8787
```

打开：

```text
http://127.0.0.1:8787
```

默认数据目录是项目根目录。可以指定：

```bash
GROK_REG_DATA_DIR=./data uvicorn web_app:app --host 0.0.0.0 --port 8787
```

## Docker Compose 部署

首次推送到 GitHub 后，GitHub Actions 会发布：

```text
ghcr.io/<你的GitHub用户名>/grok_reg:latest
```

把 `docker-compose.yml` 中的镜像名替换为你的 GHCR 镜像名：

```yaml
image: ghcr.io/<你的GitHub用户名>/grok_reg:latest
```

然后在 NAS 上执行：

```bash
mkdir -p data
docker compose pull
docker compose up -d
```

访问：

```text
http://<NAS-IP>:8787
```

## 持久化文件

Compose 默认挂载：

```text
./data:/app/data
```

其中会保存：

- `config.json`
- `accounts_*.txt`
- `mail_credentials.txt`

## 配置提示

Cloudflare 模式需要填写 `Cloudflare API Base`。CloudMail 模式需要填写 `CloudMail URL`、管理员邮箱、管理员密码和默认域名。

建议首次运行时使用：

- 注册数量：`1`
- 并发线程：`1`

确认邮箱、验证码和 Grok 注册流程稳定后再提高并发。

## GitHub Actions

`.github/workflows/docker-image.yml` 会在 `master` 分支推送时构建并推送镜像到 GHCR。仓库需要保持 Packages 权限可写，默认 `GITHUB_TOKEN` 即可。
