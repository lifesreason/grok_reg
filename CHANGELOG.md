# 更新日志 (2026-06-26)

## 新增功能

### 1. CloudMail 邮箱服务支持

新增 `cloudmail` 邮箱服务商，适配 [maillab/cloud-mail](https://github.com/maillab/cloud-mail) 项目（基于 Cloudflare Workers + D1 的完整邮箱服务）。

**与 cloudflare_temp_email 的区别**：
- cloudflare_temp_email：临时邮箱，需要通过 API 创建地址
- cloudmail：完整域名邮箱，利用 Cloudflare Email Routing catch-all 特性，任意地址自动收件

**工作流程**：
1. 生成随机邮箱 `xxx@yourdomain.com`（无需在 CloudMail 注册）
2. 用该邮箱在 Grok 注册
3. 通过 CloudMail 公开 API 查询收到的验证码
4. 填入验证码完成注册

**配置项**：
| 字段 | 说明 | 示例 |
|------|------|------|
| `email_provider` | 邮箱服务商 | `cloudmail` |
| `cloudmail_url` | CloudMail Worker 地址 | `https://mail.xxx.workers.dev` |
| `cloudmail_admin_email` | 管理员邮箱（用于获取公开 token） | `admin@yourdomain.com` |
| `cloudmail_password` | 管理员密码 | `your_password` |
| `defaultDomains` | 可用域名（逗号分隔，支持轮换） | `sub1.domain.com,sub2.domain.com` |

**CloudMail API 说明**：
- 所有接口挂载在 `/api/` 前缀下
- 认证格式：`Authorization: <token>`（不带 Bearer 前缀）
- 公开 token 通过 `POST /api/public/genToken` 获取（需管理员账号）
- 邮件查询通过 `POST /api/public/emailList`（需完整邮箱地址过滤）

### 2. Turnstile Patch 扩展

新增 `turnstilePatch/` 目录，包含 Chrome 扩展，用于优化 Cloudflare Turnstile 人机验证。

**功能**：
- 隐藏 `navigator.webdriver` 自动化标识
- 移除 `chrome.runtime` 自动化痕迹
- 修补 `plugins` / `languages` 等指纹属性
- 自动监控并点击 Turnstile 复选框（每 500ms 检查一次）

**使用方式**：无需额外配置，脚本自动检测 `turnstilePatch/` 目录并加载。

**效果**：Turnstile 验证时间从 ~15 秒缩短到 ~3-5 秒。

### 3. Turnstile 预热机制

在填写注册资料前预热 Turnstile，实现后台并行求解。

**之前**：填资料(1s) → 提交 → 等 Turnstile(15s)
**现在**：预热(2s，插件自动点击) → 填资料(1s) → 提交 → Turnstile 大概率已通过

## 优化改进

### 4. 公开 Token 线程安全共享

多线程并发时，CloudMail 的公开 token 会被覆盖导致其他线程查询失败。

**解决方案**：全局单例 + 互斥锁
- `_cloudmail_public_token` 全局变量缓存 token
- `_cloudmail_public_token_lock` 互斥锁保护
- 首次调用生成 token，后续返回缓存值
- token 失效时加锁刷新，避免多线程重复生成

### 5. 动态邮件轮询间隔

验证码邮件通常在 30 秒内到达，固定 5 秒间隔浪费前期时间。

**优化**：前 30 秒用 2 秒间隔，之后用 5 秒间隔。

### 6. 邮箱域名分隔符兼容

`defaultDomains` 字段现在支持多种分隔符：
- 英文逗号 `,`
- 中文逗号 `，`
- 空格

**之前**：`grok1.domain.com，grok1.domain.com`（中文逗号导致解析异常）
**现在**：自动正确分割。

### 7. 邮箱输入框写入优化

改进 React 受控组件的 JS 注入逻辑：
- 移除 `checkValidity()` 检查（部分站点自定义校验会误判）
- 添加完整事件序列：`focus → beforeinput → input → change → blur`
- 添加逐字符输入兜底方案

## 文件变更

| 文件 | 变更 |
|------|------|
| `grok_register_ttk.py` | 新增 cloudmail provider、预热逻辑、共享 token、动态轮询 |
| `config.example.json` | 新增 `cloudmail_url`、`cloudmail_admin_email`、`cloudmail_password` |
| `turnstilePatch/manifest.json` | 新增：Chrome 扩展配置 |
| `turnstilePatch/content.js` | 新增：注入脚本（隐藏指纹 + 自动点击 Turnstile） |
| `CHANGELOG.md` | 新增：本文件 |
