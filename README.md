# Grok Register Web

Grok 注册自动化工具，已改为 Web 控制台方式运行。适合在 NAS 上通过 Docker Compose 部署，并由 GitHub Actions 自动构建 Docker 镜像到 GHCR。

## 功能

- Web 页面编辑邮箱服务商、Cloudflare/CloudMail、并发、代理及 grok2api、sub2api、CPA 推送配置。
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
- `cpa_auths/xai-*.json`：开启 CPA 自动推送时生成的 xAI OAuth 凭证。

## 配置提示

Cloudflare 模式需要填写 `Cloudflare API Base`。CloudMail 模式需要填写 `CloudMail URL`、管理员邮箱、管理员密码和默认域名。

建议首次运行时使用：

- 注册数量：`1`
- 并发线程：`1`

确认邮箱、验证码和 Grok 注册流程稳定后再提高并发。

### Turnstile Solver（推荐，过 CF）

默认优先调用 **YesCaptcha 协议** 的本地/远端 solver 出 token，再回填到注册页；失败时回退 shadow/CDP 点选。

可与 `grokcli-2api/turnstile-solver`（Camoufox）直接对接：

```bash
# 在 grokcli-2api 仓库
cd turnstile-solver
./start.sh
curl -s http://127.0.0.1:5072/health
```

配置项（Web 控制台「Turnstile Solver」区块，或 `config.json`）：

| 项 | 默认 | 说明 |
|----|------|------|
| `turnstile_solver_enabled` | `true` | 启用 solver |
| `turnstile_solver_url` | `http://127.0.0.1:5072` | Docker 内可用环境变量 `GROK_REG_TURNSTILE_SOLVER_URL=http://host.docker.internal:5072` |
| `turnstile_solver_client_key` | `local` | 本地 solver 一般无鉴权，填 `local` 即可 |
| `turnstile_solver_fallback_click` | `true` | solver 不可达/失败时回退页面点选 |
| `turnstile_sitekey` | `0x4AAAAAAAhr9JGVDZbrZOo0` | 页面刮不到 sitekey 时的回退（公开值） |

### CPA Management API 推送

开启“注册成功后生成并自动推送 CPA xAI 凭证”后，程序会以当前账号的
Refresh Token 换取 Access Token，在 `cpa_auth_dir`（默认 `cpa_auths`）生成
`xai-<邮箱>.json`，再请求：

```text
POST <CPA 管理地址>/v0/management/auth-files
Authorization: Bearer <CPA 管理密钥>
Content-Type: multipart/form-data
```

管理地址可填写服务器根地址或已包含 `/v0/management` 的地址。远端上传失败会
保留本地凭证并记录日志，不会使已成功的注册任务失败。

## GitHub Actions

`.github/workflows/docker-image.yml` 会在 `master` 分支推送时构建并推送镜像到 GHCR。仓库需要保持 Packages 权限可写，默认 `GITHUB_TOKEN` 即可。
