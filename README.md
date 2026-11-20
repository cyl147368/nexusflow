# NexusFlow

NexusFlow 是一体化邮箱与自动注册编排项目。主项目基于
[gpt-auto-register](https://github.com/Regert888/gpt-auto-register)，
集成 [iCloud Prime](https://github.com/forever94yu/icloud-prime) 的
iCloud Hide My Email 创建/删除能力，并内置邮件中转服务。

核心流程：

```text
创建 iCloud 隐藏邮箱
  -> 邮件中转服务登记邮箱并返回 relay_url
  -> 自动写入主项目邮箱池
  -> 启动主项目自动注册
  -> 原有 OTP、代理池、SMS、2FA、凭证保存与导出流程
```

## 架构

| 服务 | 作用 | 网络 |
|---|---|---|
| `nexusflow` | 主 WebUI、邮箱池、注册引擎、统一编排 | 对外 `8770` |
| `icloud-prime` | iCloud 账号、隐藏邮箱创建/删除、iCloud 收件读取 | 仅 Docker 内网 |
| `mail-hub` | 邮箱登记、统一 `relay_url`、iCloud 代理取件、SMTP catch-all | 对外 `8090` / `2525` |
| `payment-detector` | 只读主数据库的 MoMo/UPI 支付资格检测 | 仅 Docker 内网 |

所有持久数据都放在 Docker named volumes 中。`.env` 内有自动生成的
WebUI Basic Auth 密码、邮件服务 Token 和 iCloud Token，权限为 `600`，
不会提交到 Git。

## 一键部署

服务器要求：Linux、Docker Compose v2、可访问 Docker Hub 与 GitHub。

```bash
git clone https://github.com/cyl147368/nexusflow.git
cd nexusflow
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

脚本会：

1. 安装 Docker（尚未安装时）。
2. 创建 `.env` 并生成随机密钥。
3. 构建三个服务并后台启动。
4. 输出 WebUI 地址和登录凭据。

默认地址：

```text
主界面:  http://<服务器IP>:8770/
Mail Hub: http://<服务器IP>:8090
SMTP:     <服务器IP>:2525
```

如服务器已有端口冲突，在 `.env` 中修改：

```dotenv
NEXUSFLOW_PORT=8770
MAILHUB_PORT=8090
MAILHUB_SMTP_PUBLIC_PORT=2525
MAILHUB_PUBLIC_URL=http://你的服务器IP:8090
```

修改后执行：

```bash
docker compose up -d
```

## 首次配置

打开主界面后进入「概览 / 一体化启动」。

### 1. 邮件服务器

内网地址固定为 `http://mail-hub:8090`，管理员 Token 已由 Compose
环境变量自动注入并显示为 `***`。点击“测试”确认服务正常。

“实际收件箱”支持两种模式：

- `iCloud 主收件箱`：在 iCloud 账号区域填写 iCloud 接收邮箱、App 专用密码和 Cookie。
- `外部 IMAP`：填写 IMAP 主机、端口、实际收件邮箱、IMAP 授权码和文件夹。

QQ、Gmail 等邮箱必须使用服务商生成的 IMAP 授权码，不能使用网页登录密码。
外部 IMAP 模式要求 Apple 隐藏邮箱已经设置为转发到这个实际收件箱。

如果要把自有域名邮件直接投递到 Mail Hub，需要把域名的 MX 指向服务器，
并在服务器防火墙上允许 `25/tcp`；默认 Compose 使用宿主机 `2525`
避免占用系统已有邮件服务。可把端口映射改为 `25:2525`。

### 2. iCloud 隐藏邮箱

iCloud 服务内网地址为 `http://icloud-prime:8081`，API Token 同样自动注入。

有两种添加账号方式：

- 粘贴 iCloud Cookie：推荐，可立即创建隐藏邮箱。
- 填写 Apple ID、密码和 App 专用密码：首次登录可能需要 2FA 验证码。

App 专用密码用于读取 iCloud 收件箱。Cookie 用于管理 Hide My Email
别名。两者可以同时配置。

保存账号后页面会显示 `account_id`。后续一键流程会自动使用该账号。

### 3. 主项目注册参数

配置代理、OTP 超时、需要保存的 Token，以及是否注册后绑定 TOTP 2FA。
然后点击“一键启动完整流程”。

主项目原有的「邮箱列表」「注册结果」「运行记录」「代理池」
「接码配置」「自动导出」页面全部保留。

### 4. MoMo / UPI 资格检测

进入「数据 / 支付资格」可以直接读取主项目 `registered` 表中的账号。

检测服务会：

1. 验证账号 AT 与 Cookie。
2. 按 MoMo（Vietnam）或 UPI（India）创建 Checkout。
3. 调用 Stripe init 读取支付方式与金额。
4. 在 payment method 创建、Confirm、Approve 之前停止。
5. 将结果写入独立 `payment-results` 数据卷，不修改主数据库。

MoMo 和 UPI 分别需要越南、印度地区出口代理。服务器上已保留完整 Python
支付核心、Node.js Sentinel 与 Playwright Chromium；Windows WPF 部分未集成。

## 本地开发

### 主 WebUI

```bash
python -m pip install -r requirements.txt
python start_webui.py
```

前端构建：

```bash
cd webui/frontend
npm ci
npm run build
```

### Mail Hub

```bash
cd services/mail-hub
python -m pip install -r requirements.txt
MAILHUB_ADMIN_TOKEN=dev-token \
MAILHUB_DB=./mailhub.db \
MAILHUB_SMTP_PORT=0 \
uvicorn service:app --host 127.0.0.1 --port 8090
```

### iCloud Prime

iCloud Prime 是独立 Go 服务，源码位于 `services/icloud-prime`，构建和 API
说明见其目录内 README 与 API.md。

## 验证

```bash
python -m unittest discover -s tests -v

cd services/mail-hub
python -m unittest -v test_service.py

cd ../../webui/frontend
npm run build
```

## 安全说明

- 不要提交 `.env`、`webui.db`、`mailhub.db`、iCloud Cookie、App 专用密码、
  注册凭证或中转 Token。
- 生产入口启用了 HTTP Basic Auth；iCloud 与 Mail Hub 管理接口使用独立
  Bearer Token。
- Mail Hub 的 `relay_url` 本身包含随机 Token，应视为敏感链接。
- 建议在正式域名上增加 HTTPS 反向代理。当前部署可直接通过服务器 IP
  访问，不会修改服务器上已有的 Nginx 站点。
- 仓库包含 AGPL-3.0 主项目代码与 MIT 许可的 iCloud Prime 代码，详见
  [LICENSE](LICENSE)、[NOTICE](NOTICE) 和
  `services/icloud-prime/LICENSE`。
