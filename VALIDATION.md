# 验收记录

2026-11-07，当前版本为候选安装器；不是公网 VPS 实机验收通过的正式发布。

## 已验证

- Python 语法、Bash 语法、bootstrap SHA-256 一致性。
- 官方 3x-ui v2.9.2 amd64 二进制的真实启动与 HTTPS 登录。
- 自建测试 CA 信任下的 IP SAN 校验；错误 IP、未信任证书均被拒绝，没有关闭 TLS 验证。
- 面板初始入站列表为空，订阅关闭。
- 随机凭据有效，默认 admin/admin 无效；修改密码后新密码有效、旧密码无效。
- 防火墙命令顺序确保先放行识别出的 SSH 端口，再启用默认拒绝。
- 管理命令拒绝关闭 SSH、80 和后台端口。
- 压缩包路径穿越与符号链接拒绝、凭据私密文件权限、证书失败停止。
- 已完成安装再次执行不进行系统变更。

## 尚未验证（上线前必须完成）

1. Ubuntu 22.04 / 24.04 全新 VPS：APT、systemd、UFW、Fail2ban 的真实运行。
2. Let's Encrypt 生产环境 IP 证书签发、至少一次真实自动续期，确认 x-ui 重启后读到新证书。
3. 外部浏览器通过公网 HTTPS 访问后台；另外新开 SSH 会话，确认防火墙未阻断登录。
4. 重启 VPS 后面板、SSH、Fail2ban、续期计时器均恢复。
5. 手动建立入站后，用 Shadowrocket / v2rayN 验证实际联网。
6. arm64 官方包校验固定值已记录，但本地真实二进制集成测试只运行 amd64。

当前环境没有接入用户的新 VPS，不能声称上述项目已经完成。

## 本地与 CI

```bash
bash -n install.sh
python3 tests/check_bootstrap.py
python3 -m unittest discover -s tests -v
XUI_TEST_APP=/path/to/extracted/x-ui python3 -m unittest discover -s tests -v
```

测试用自签名证书只存在于临时测试目录。生产安装强制使用实际受信任的 ACME 证书，失败停止。
