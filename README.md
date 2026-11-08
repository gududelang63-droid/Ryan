# 3x-ui 快捷部署

面向个人使用的全新 Ubuntu VPS：执行一次安装命令，获得 **HTTPS 后台地址、随机账号和密码**。入站留空，由你在面板手动添加。

**状态：候选版本。** 已通过真实 3x-ui 二进制的 HTTPS 登录、证书校验、空入站和密码变更测试；尚未在一台独立公网 VPS 完成真实证书签发、系统防火墙、续期和重启验收。请先在新机器测试，不要在现有生产节点上运行。

## 默认行为

- 固定使用 `3x-ui v2.9.2`，沿用此前验证过的兼容版本；这不代表它是最新版或永远无需更新。
- 随机生成账号、强密码、四位后台端口与访问路径；支持安装前指定 `PANEL_PORT`。
- 用 Let's Encrypt IP 证书启用 HTTPS，无需域名；每六小时检查续期，约三天到续期点时申请新证书。
- UFW 先放行 SSH，再放行证书验证 `80/TCP`、后台端口，以及预留节点 `443/TCP`；默认拒绝其余入站。
- Fail2ban 防护 SSH：10 分钟内 6 次失败，封禁 1 小时。它不等于后台防暴力破解或 DDoS 清洗服务。
- 禁用订阅服务；不自动创建 VLESS、REALITY 或其他入站。
- 不改 SSH 登录方式，不安装新内核，不修改 BBR，不自动升级系统或重启。
- 已完成安装后重复执行只展示保存的信息，不覆盖入站；安装失败可以修复原因后重跑。
- 密码、证书私钥、数据库只保存在 VPS，绝不回传 GitHub。

## 使用条件

- 全新 Ubuntu **22.04 / 24.04 LTS**，amd64 或 arm64，systemd；建议至少 1 GB 内存。
- root 权限（普通账号先执行 `sudo -i`）。可通过 SSH 或厂商控制台执行。
- 独立、固定、可直接入站的公网 IPv4。NAT 共享端口机器不在支持范围。
- 没有既有 3x-ui、Docker、自定义 UFW 规则或其他网络服务。
- VPS 能访问 GitHub、Ubuntu 软件源、Let's Encrypt；外部网络能访问此 IP 的 TCP 80。
- 云厂商防火墙/安全组需要另外放行 SSH、80、面板端口、节点端口。脚本只能管理系统内的 UFW。

## 一条命令安装

在 **全新 VPS 的终端**整行执行（推荐固定后台端口为 `8848`，方便先配置云安全组）：

```bash
apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates && curl -fL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/gududelang63-droid/Ryan/main/install.sh -o /root/3xui-install.sh && PANEL_PORT=8848 bash /root/3xui-install.sh
```

如果 VPS 已有 curl，以上命令即可启动；若首次提示 `curl: command not found`，先执行 `apt-get update && apt-get install -y curl ca-certificates`。入口随后会自动补齐 python3 等依赖。

如想提前固定后台端口，便于放行云安全组：

```bash
PANEL_PORT=8848 bash /root/3xui-install.sh
```

公网 IP 自动检测要求两个独立服务返回一致的 IPv4；服务不可访问时，可明确指定本机真实公网 IPv4：

```bash
SERVER_IPV4=你的公网IPv4 PANEL_PORT=8848 bash /root/3xui-install.sh
```

安装结束会输出：

```text
后台：https://你的公网IP:后台端口/随机路径/
账号：随机生成
密码：随机生成
```

完整复制链接，包括端口和随机路径。面板入站为空，需要自己添加节点并导入客户端。`443/TCP` 已预留；如果节点使用其他端口，需要同步放行 UFW 和云安全组。

## 日常管理

```bash
langge-3xui show                 # 显示安装时保存的后台信息
langge-3xui reset-password       # 备份数据库，生成新密码，验证新密码可登录
langge-3xui status               # 查看服务、防火墙、续期计时器和证书日期
langge-3xui open-port 49190 tcp   # 放行手动设置的节点端口
langge-3xui close-port 49190 tcp  # 取消该端口的 UFW 放行
```

端口管理命令不允许修改 SSH、证书验证端口和安装时记录的后台端口。如果你在面板内另改了后台账号、密码、端口或路径，保存的信息不会自动同步；当前管理工具以安装时记录为准。

后台账号密码保存在 `/var/lib/langge-3xui/credentials.txt`（root 专用，权限 600）。不需要把文件或面板数据库发到 GitHub。

## 证书与故障处理

IP 证书是短有效期证书，必须保留 TCP 80 的入站可达性供续期使用。80 端口没有常驻管理页面，只在 ACME 验证时由临时进程监听。续期部署证书会重启 x-ui，可能造成短暂连接中断。

如果证书签发失败：

1. 检查厂商安全组是否放行 TCP 80，IP 是否确实属于此 VPS。
2. 在服务器查看 `/var/lib/langge-3xui/certificate.log`。
3. 修复后重跑安装命令或执行 `langge-3xui install`。不要删数据库、反复重装系统或盲目重复申请证书。

如果后台打不开，先检查完整链接和厂商防火墙，再执行 `langge-3xui status`。不要通过关闭全部防火墙解决。

本工具不修改已有服务器，不提供自动卸载，也不会在失败后自动删除文件。失败后停止面板并保留 SSH 放行和防火墙，保留文件供排查。

## 代码与校验

- `install.sh`：轻量下载入口，校验 `setup.py` 的 SHA-256 后执行。
- `setup.py`：安装、证书、UFW、Fail2ban、查看信息与重置密码。
- 官方 3x-ui 压缩包和 acme.sh 脚本均固定版本并校验 SHA-256，不使用不明镜像。
- SHA-256 防止下载内容意外变化，不代表代码没有漏洞；`main/install.sh` 本身是可变入口。需要固定发布时，可用审阅过的 commit SHA 替换 URL 中的 `main`，并将脚本内的 `setup.py` 地址固定到相应版本。
- GitHub Actions 运行安全测试与真实二进制集成测试，不能替代公网 VPS 验收。

详细验收范围见 [VALIDATION.md](VALIDATION.md)。

## 在 NAS Docker 中安全试跑

Docker 适合验证安装代码、固定上游文件和真实 3x-ui 二进制，但不能代替独立 VPS 的公网证书、UFW、Fail2ban、SSH 保活与重启验收。普通 Ubuntu 容器没有完整 systemd，**不要在 NAS 上直接执行生产安装命令，也不要给测试容器 privileged、host 网络或 NAS 系统目录挂载**。

仓库提供一个隔离的 Ubuntu 24.04 测试容器：不映射端口、不挂载宿主目录、移除 Linux capabilities，并启用 `no-new-privileges`。在已安装 Git 与 Docker Compose 的 NAS 终端执行：

```bash
git clone https://github.com/gududelang63-droid/Ryan.git
cd Ryan
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from verify
```

看到 `PASS：Docker 安全验证全部通过。` 表示以下内容通过：

- Bash、ShellCheck、bootstrap SHA-256 和安全单元测试；
- 固定版本 3x-ui / acme.sh 的下载与 SHA-256 校验；
- 真实 3x-ui 二进制的 HTTPS 登录、空入站、禁用默认凭据与密码变更。

测试容器结束后可以删除镜像和构建缓存；不会在 NAS 上安装 3x-ui，也不会修改 NAS 防火墙。

## 上游

- [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui)
- [acmesh-official/acme.sh](https://github.com/acmesh-official/acme.sh)

本仓库只包含部署代码，安装时从官方源下载上游程序；上游各自保留其许可证。UFW、Fail2ban 和随机后台地址不能保证 IP 信誉，也不能改变运营商线路或承诺带宽。
