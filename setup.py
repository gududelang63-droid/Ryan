#!/usr/bin/env python3
"""Fresh Ubuntu installer. No existing x-ui installation is ever overwritten."""
import argparse
import fcntl
import hashlib
import http.cookiejar
import http.client
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

VERSION = "v2.9.2"
HASHES = {
    "amd64": "ca93c552ea4ddb8d0940932f79d0c0f1ad864f93f1f49bce34b9c9a348936693",
    "arm64": "6f4f155f143522d49565841a84df08c97c119b4390e8e670094f1ea6ea49bfc4",
}
ROOT = Path("/var/lib/langge-3xui")
APP = Path("/usr/local/x-ui")
DB = Path("/etc/x-ui")
SERVICE = Path("/etc/systemd/system/x-ui.service")
F2B = Path("/etc/fail2ban/jail.d/langge-sshd.local")
INFO = ROOT / "credentials.txt"
STATE = ROOT / "state.json"
RECOVERY = ROOT / "recovery.json"
ACME = ROOT / "acme" / "acme.sh"
CERT = ROOT / "cert"
ACME_VERSION = "3.1.6"
ACME_HASH = "c7d68b021cfd6380ea83a82962abde5b484779fee0b97d38681dfa1396bbc8d7"
RENEW_SERVICE = Path("/etc/systemd/system/langge-cert-renew.service")
RENEW_TIMER = Path("/etc/systemd/system/langge-cert-renew.timer")
MANAGER = Path("/usr/local/bin/langge-3xui")


def run(args, **kwargs):
    check = kwargs.pop("check", True)
    kwargs.setdefault("timeout", 900)
    try:
        return subprocess.run([str(x) for x in args], check=check, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout.strip()
    except subprocess.CalledProcessError as exc:
        if ROOT.is_dir():
            write_private(ROOT / "system-error.log",
                str(args[0]) + "\n" + (exc.stdout or "") + "\n" + (exc.stderr or ""))
        raise


def write_private(path, text):
    path = Path(path)
    # Caller owns the 0700 parent; atomic replacement never follows a symlink.
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def port_number(value):
    n = int(value)
    if not 1 <= n <= 65535:
        raise ValueError("端口必须在 1–65535")
    return n


def parse_ssh_connection(value):
    parts = value.split()
    if len(parts) != 4:
        raise ValueError("请通过 SSH 登录后安装，无法确认当前 SSH 端口时拒绝启用防火墙")
    ipaddress.ip_address(parts[0])
    ipaddress.ip_address(parts[2])
    return port_number(parts[3])


def public_ipv4(value):
    ip = ipaddress.ip_address(value.strip())
    if ip.version != 4 or not ip.is_global:
        raise ValueError("节点地址必须是公网 IPv4")
    return str(ip)


def detect_ipv4():
    seen = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        with opener.open(url, timeout=15) as r:
            seen.append(public_ipv4(r.read(100).decode()))
    if len(set(seen)) != 1:
        raise RuntimeError("公网 IP 检测不一致，请显式设置 SERVER_IPV4")
    return seen[0]


def free_port(low=1000, high=9999, exclude=()):
    for _ in range(100):
        p = low + secrets.randbelow(high - low + 1)
        if p in exclude:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                pass
    raise RuntimeError("找不到可用的面板端口")


def download_archive(arch, dest):
    url = f"https://github.com/MHSanaei/3x-ui/releases/download/{VERSION}/x-ui-linux-{arch}.tar.gz"
    run(["curl", "--fail", "--location", "--proto", "=https", "--tlsv1.2", "--retry", "3",
         "--connect-timeout", "20", "--max-time", "600", "--output", dest, url])
    digest = hashlib.sha256(Path(dest).read_bytes()).hexdigest()
    if digest != HASHES[arch]:
        raise RuntimeError("上游压缩包 SHA-256 不匹配，停止执行")


def extract_archive(archive, directory):
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            p = Path(member.name)
            if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != "x-ui":
                raise ValueError("压缩包包含不安全路径")
            if not (member.isfile() or member.isdir()):
                raise ValueError("压缩包包含链接或特殊文件")
            member.uid, member.gid, member.uname, member.gname = os.getuid(), os.getgid(), "", ""
        tar.extractall(directory, members=tar.getmembers())


def initialize_panel(app, db, username, password, port, path):
    db.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = dict(os.environ, XUI_DB_FOLDER=str(db), XUI_BIN_FOLDER=str(app / "bin"))
    run([app / "x-ui", "setting", "-username", username, "-password", password,
         "-port", str(port), "-webBasePath", path, "-listenIP", "127.0.0.1"], env=env, cwd=app)
    desired = {"webListen": "127.0.0.1", "webPort": str(port), "webBasePath": path,
               "subEnable": "false", "subJsonEnable": "false", "subClashEnable": "false",
               "subListen": "127.0.0.1"}
    with sqlite3.connect(db / "x-ui.db") as con:
        for key, val in desired.items():
            con.execute("DELETE FROM settings WHERE key=?", (key,))
            con.execute("INSERT INTO settings(key,value) VALUES (?,?)", (key, val))
    os.chmod(db / "x-ui.db", 0o600)


class LoopbackHTTPS(http.client.HTTPSConnection):
    """Connect locally, but verify the certificate against the public IP host."""
    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class LoopbackHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(LoopbackHTTPS, req, context=self._context)


class Panel:
    def __init__(self, port, path, ip=None, context=None):
        self.base = f"https://{ip}:{port}{path}" if ip else f"http://127.0.0.1:{port}{path}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            LoopbackHTTPSHandler(context=context or ssl.create_default_context()))

    def call(self, route, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + route, data=data,
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=10) as response:
            result = json.load(response)
        if result.get("success") is not True:
            raise RuntimeError("面板 API 操作未成功：" + route)
        return result.get("obj")

    def login(self, username, password):
        for _ in range(30):
            try:
                self.call("login", {"username": username, "password": password})
                return
            except (OSError, ValueError):
                time.sleep(1)
        raise RuntimeError("面板启动超时或登录验证失败")


def ssh_ports():
    ports = set()
    if os.environ.get("SSH_CONNECTION"):
        ports.add(parse_ssh_connection(os.environ["SSH_CONNECTION"]))
    for row in run(["sshd", "-T"]).splitlines():
        if row.startswith("port "):
            ports.add(port_number(row.split()[1]))
    listeners = run(["ss", "-H", "-lntp"])
    for row in listeners.splitlines():
        if '"sshd"' in row:
            ports.add(port_number(row.split()[3].rsplit(":", 1)[1]))
    if not ports:
        raise RuntimeError("无法识别 SSH 端口，停止以免失联")
    return sorted(ports)


def check_fresh_server():
    for path in (APP, DB, SERVICE, Path("/etc/default/x-ui"), F2B, RENEW_SERVICE, RENEW_TIMER, MANAGER):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"检测到已有文件 {path}，停止。此脚本只安装新服务器，不覆盖旧配置")
    if not Path("/run/systemd/system").exists():
        raise RuntimeError("需要运行中的 systemd")
    if shutil.which("ufw") and "Status: active" in run(["ufw", "status"], env=dict(os.environ, LC_ALL="C")):
        raise RuntimeError("已启用 UFW；请使用全新 Ubuntu，不覆盖现有防火墙")
    if shutil.which("ufw") and any(line.startswith("ufw ") for line in run(["ufw", "show", "added"]).splitlines()):
        raise RuntimeError("存在自定义 UFW 规则，请使用全新 Ubuntu")
    ssh = ssh_ports()
    for proc in ("dockerd", "podman", "xray", "x-ui"):
        if subprocess.run(["pgrep", "-x", proc], stdout=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError("检测到 Docker / 代理服务，请使用全新 Ubuntu")
    listeners = run(["ss", "-H", "-lntup"])
    for row in listeners.splitlines():
        local = row.split()[4]
        host = local.rsplit(":", 1)[0].strip("[]")
        if host in ("127.0.0.1", "127.0.0.53", "127.0.0.54", "::1"):
            continue
        if any(p in row for p in ('"sshd"', '"systemd-resolve"', '"systemd-network"', '"chronyd"')):
            continue
        if '"systemd"' in row and int(local.rsplit(":", 1)[1]) in ssh:
            continue
        raise RuntimeError("检测到其他网络监听服务，拒绝改变防火墙；请使用全新 Ubuntu")


def service_text():
    return """[Unit]
Description=3x-ui personal panel (langge installer)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=/usr/local/x-ui
Environment=XUI_DB_FOLDER=/etc/x-ui
Environment=XUI_BIN_FOLDER=/usr/local/x-ui/bin
ExecStart=/usr/local/x-ui/x-ui
Restart=on-failure
RestartSec=5
UMask=0077
[Install]
WantedBy=multi-user.target
"""


def configure_tls(app, db, cert, key, listen="0.0.0.0"):
    # Called only with x-ui stopped; never expose a plaintext admin panel.
    env = dict(os.environ, XUI_DB_FOLDER=str(db), XUI_BIN_FOLDER=str(app / "bin"))
    run([app / "x-ui", "cert", "-webCert", cert, "-webCertKey", key], env=env, cwd=app)
    run([app / "x-ui", "setting", "-listenIP", listen], env=env, cwd=app)


def acme_args(*args):
    return ["/bin/sh", ACME, "--home", ACME.parent, "--config-home", ACME.parent, *args]


def download_acme(dest):
    run(["curl", "--fail", "--location", "--proto", "=https", "--tlsv1.2",
         "--retry", "3", "--connect-timeout", "20", "--max-time", "120", "-o", dest,
         f"https://raw.githubusercontent.com/acmesh-official/acme.sh/{ACME_VERSION}/acme.sh"])
    if hashlib.sha256(Path(dest).read_bytes()).hexdigest() != ACME_HASH:
        raise RuntimeError("acme.sh SHA-256 不匹配，停止执行")


def issue_certificate(ip):
    CERT.mkdir(mode=0o700, exist_ok=True)
    # Use staging only for a deliberately separate test server, not a silent fallback.
    cmd = acme_args("--issue", "--server", "letsencrypt", "-d", ip,
        "--standalone", "--httpport", "80", "--listen-v4", "--keylength", "ec-256",
        "--certificate-profile", "shortlived", "--days", "3")
    result = subprocess.run([str(x) for x in cmd], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
    write_private(ROOT / "certificate.log", result.stdout)
    # acme.sh returns 2 when a matching valid certificate is not due for renewal.
    if result.returncode not in (0, 2):
        raise RuntimeError("HTTPS 证书申请失败。检查云安全组 TCP 80、公网 IP 和 "
                           + str(ROOT / "certificate.log") + "；修复后重跑同一安装命令")
    run(acme_args("--install-cert", "-d", ip, "--ecc", "--key-file", CERT / "privkey.pem",
        "--fullchain-file", CERT / "fullchain.pem", "--reloadcmd", "systemctl try-restart x-ui"))
    os.chmod(CERT / "privkey.pem", 0o600)
    # Check lifetime and IP identity before exposing the panel.
    run(["openssl", "x509", "-in", CERT / "fullchain.pem", "-noout", "-checkend", "86400"])
    out = run(["openssl", "x509", "-in", CERT / "fullchain.pem", "-noout", "-checkip", ip])
    if "does match certificate" not in out:
        raise RuntimeError("证书 IP 与服务器 IP 不一致")


def install_renewal():
    RENEW_SERVICE.write_text(f"""[Unit]
Description=Renew 3x-ui HTTPS IP certificate
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
UMask=0077
ExecStart=/bin/sh {ACME} --cron --home {ACME.parent} --config-home {ACME.parent}
TimeoutStartSec=900
""")
    RENEW_TIMER.write_text("""[Unit]
Description=Check 3x-ui HTTPS certificate every six hours
[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
RandomizedDelaySec=600
Persistent=true
[Install]
WantedBy=timers.target
""")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", RENEW_TIMER.name])


def configure_firewall(c):
    # No reset: preserve SSH rules first, including an active nonstandard SSH port.
    for port in c["ssh_ports"]:
        run(["ufw", "insert", "1", "allow", f"{port}/tcp", "comment", "SSH preserved"])
    for port, label in ((80, "ACME renewal"), (c["panel_port"], "3x-ui HTTPS"),
                        (443, "Reserved manual inbound")):
        run(["ufw", "allow", f"{port}/tcp", "comment", label])
    run(["ufw", "default", "deny", "incoming"])
    run(["ufw", "default", "allow", "outgoing"])
    run(["ufw", "--force", "enable"])


def configure_fail2ban(ports):
    F2B.write_text("[sshd]\nenabled = true\nbackend = systemd\nport = " + ",".join(map(str, ports)) +
                   "\nmaxretry = 6\nfindtime = 10m\nbantime = 1h\n")
    run(["fail2ban-client", "-t"])
    run(["systemctl", "enable", "--now", "fail2ban"])
    run(["systemctl", "restart", "fail2ban"])
    for _ in range(15):
        try:
            run(["fail2ban-client", "status", "sshd"])
            return
        except subprocess.CalledProcessError:
            time.sleep(1)
    raise RuntimeError("Fail2ban SSH 防护未正常启动")


def credentials_text(c):
    return (f"3x-ui {VERSION} 安装完成；入站留空，请在面板手动添加。\n\n"
        f"后台：https://{c['ip']}:{c['panel_port']}{c['path']}\n"
        f"账号：{c['username']}\n密码：{c['password']}\n\n"
        f"云安全组需放行 TCP：SSH {c['ssh_ports']}、80、{c['panel_port']}、443。\n"
        "80 用于证书签发和自动续期；443 预留给手动创建的节点。\n"
        "如节点使用其他端口：langge-3xui open-port 端口 tcp（并同步云安全组）。\n"
        "查看信息：langge-3xui show\n重置密码：langge-3xui reset-password\n"
        "检查状态：langge-3xui status\n"
        "实际外网可达性、节点连通和速度，仍需安装后实测。\n")


def prepare_install():
    release = Path("/etc/os-release").read_text()
    if not re.search(r'^ID=ubuntu$', release, re.M) or not re.search(r'^VERSION_ID="(?:22\.04|24\.04)"$', release, re.M):
        raise RuntimeError("当前仅支持 Ubuntu 22.04 / 24.04 LTS")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise RuntimeError("只支持 amd64 / arm64")
    check_fresh_server()
    ports = ssh_ports()
    if 80 in ports or 443 in ports:
        raise RuntimeError("SSH 占用了 80 或 443，当前预设不适用；未更改防火墙")
    ip = public_ipv4(os.environ["SERVER_IPV4"]) if os.environ.get("SERVER_IPV4") else detect_ipv4()
    panel_port = port_number(os.environ["PANEL_PORT"]) if os.environ.get("PANEL_PORT") else free_port(exclude=ports+[80,443])
    if panel_port in ports+[80,443] or panel_port < 1024:
        raise RuntimeError("后台端口须在 1024–65535 且不能与 SSH/80/443 重叠")
    for port in (80, panel_port):
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", port))
    c = {"version": VERSION, "arch": arch, "ssh_ports": ports, "ip": ip,
         "panel_port": panel_port, "username": "u_" + secrets.token_hex(4),
         "password": secrets.token_urlsafe(24), "path": "/" + secrets.token_urlsafe(18) + "/"}
    print(f"准备安装 {VERSION}；后台端口 {panel_port}。下载并校验官方文件…", flush=True)
    with tempfile.TemporaryDirectory(prefix="langge-3xui-") as temp:
        archive = Path(temp) / "x-ui.tar.gz"
        download_archive(arch, archive)
        extract_archive(archive, temp)
        download_acme(Path(temp) / "acme.sh")
        ROOT.mkdir(mode=0o700)
        if Path("/etc/ufw").exists():
            shutil.copytree("/etc/ufw", ROOT / "ufw-before")
        # Persist intent before mutations; retries resume only this install.
        write_private(RECOVERY, json.dumps(c, indent=2))
        ACME.parent.mkdir(mode=0o700)
        shutil.copy2(Path(temp) / "acme.sh", ACME)
        shutil.copytree(Path(temp) / "x-ui", APP)
    return c


def install():
    if STATE.exists():
        print("保留已有安装和入站配置。\n" + INFO.read_text())
        return
    if ROOT.exists() and not RECOVERY.exists():
        raise RuntimeError(f"存在未完成目录 {ROOT}，请检查；不会自动删除数据")
    c = json.loads(RECOVERY.read_text()) if RECOVERY.exists() else prepare_install()
    # Make the local recovery command available even if the certificate is blocked.
    if Path(__file__).resolve() != (ROOT / "setup.py").resolve():
        shutil.copy2(__file__, ROOT / "setup.py")
    os.chmod(ROOT / "setup.py", 0o700)
    MANAGER.write_text('#!/bin/sh\nexec /usr/bin/python3 /var/lib/langge-3xui/setup.py "$@"\n')
    os.chmod(MANAGER, 0o700)
    print("安装依赖和安全防护；不会自动升级系统或重启…", flush=True)
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l")
    run(["apt-get", "update"], env=env)
    run(["apt-get", "install", "-y", "ca-certificates", "curl", "ufw", "fail2ban",
         "python3-systemd", "socat", "openssl"], env=env)
    # Refresh SSH ports on retry so a moved SSH port is never forgotten.
    c["ssh_ports"] = sorted(set(c["ssh_ports"]) | set(ssh_ports()))
    write_private(RECOVERY, json.dumps(c, indent=2))
    try:
        configure_firewall(c)
        configure_fail2ban(c["ssh_ports"])
        run(["systemctl", "stop", "x-ui"], check=False)
        initialize_panel(APP, DB, c["username"], c["password"], c["panel_port"], c["path"])
        print("申请 HTTPS IP 证书（云安全组必须允许 TCP 80）…", flush=True)
        issue_certificate(c["ip"])
        configure_tls(APP, DB, CERT / "fullchain.pem", CERT / "privkey.pem")
        SERVICE.write_text(service_text())
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "start", "x-ui"])
        panel = Panel(c["panel_port"], c["path"], c["ip"])
        panel.login(c["username"], c["password"])
        if panel.call("panel/api/inbounds/list"):
            raise RuntimeError("检测到非空入站，停止；新安装应为空")
        install_renewal()
        run(["systemctl", "enable", "x-ui"])
    except BaseException:
        run(["systemctl", "stop", "x-ui"], check=False)
        # Retain SSH allow rules and firewall; never disable protection on failure.
        raise
    message = credentials_text(c)
    write_private(INFO, message)
    write_private(STATE, json.dumps({k:v for k,v in c.items() if k not in ("username", "password", "path")}, indent=2))
    print(message)


def change_node_port(action, value, protocol):
    if not STATE.exists():
        raise RuntimeError("请先完成安装")
    port = port_number(value)
    c = json.loads(RECOVERY.read_text())
    protected = set(c["ssh_ports"]) | set(ssh_ports()) | {80, c["panel_port"]}
    if port in protected:
        raise RuntimeError("不能通过节点端口命令修改 SSH、后台或证书端口")
    rule = f"{port}/{protocol}"
    run(["ufw", "allow", rule] if action == "open-port" else ["ufw", "--force", "delete", "allow", rule])
    print(f"已{'放行' if action == 'open-port' else '移除放行'} {rule}；云安全组需要另外同步。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "show", "reset-password", "status", "open-port", "close-port"], nargs="?", default="install")
    parser.add_argument("port", nargs="?")
    parser.add_argument("protocol", choices=["tcp", "udp"], nargs="?", default="tcp")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("请先 sudo -i，使用 root 执行")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    with open("/run/lock/langge-3xui.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "show":
            print(INFO.read_text())
        elif args.action in ("open-port", "close-port"):
            change_node_port(args.action, args.port, args.protocol)
        elif args.action == "status":
            for command in (["systemctl", "is-active", "x-ui", "fail2ban", RENEW_TIMER.name],
                            ["ufw", "status", "verbose"],
                            ["fail2ban-client", "status", "sshd"],
                            ["systemctl", "list-timers", RENEW_TIMER.name, "--no-pager"],
                            ["openssl", "x509", "-in", CERT / "fullchain.pem", "-noout", "-dates"]):
                print(run(command, check=False))
        elif args.action == "reset-password":
            if not STATE.exists():
                raise RuntimeError("此命令仅支持本脚本已完成的安装")
            recovery = json.loads((ROOT / "recovery.json").read_text())
            new_password = secrets.token_urlsafe(24)
            # Database backup uses SQLite's consistent backup API.
            backup = ROOT / ("backup-" + time.strftime("%Y%m%d-%H%M%S") + ".db")
            with sqlite3.connect(DB / "x-ui.db") as src, sqlite3.connect(backup) as dst:
                src.backup(dst)
            try:
                run([APP / "x-ui", "setting", "-username", recovery["username"], "-password", new_password])
                run(["systemctl", "restart", "x-ui"])
                Panel(recovery["panel_port"], recovery["path"], recovery["ip"]).login(recovery["username"], new_password)
            except BaseException:
                run(["systemctl", "stop", "x-ui"], check=False)
                with sqlite3.connect(backup) as src, sqlite3.connect(DB / "x-ui.db") as dst:
                    src.backup(dst)
                run(["systemctl", "start", "x-ui"], check=False)
                raise
            write_private(INFO, INFO.read_text().replace("密码：" + recovery["password"], "密码：" + new_password))
            recovery["password"] = new_password
            write_private(ROOT / "recovery.json", json.dumps(recovery, indent=2))
            print(INFO.read_text())
        else:
            install()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never dump command args: CLI args may contain generated passwords.
        reason = (f"系统命令 {Path(exc.cmd[0]).name} 执行失败，退出码 {exc.returncode}（参数已隐藏）"
                  if isinstance(exc, subprocess.CalledProcessError) else
                  "系统命令超时（参数已隐藏）" if isinstance(exc, subprocess.TimeoutExpired) else str(exc))
        print("安装未完成：" + reason, file=sys.stderr)
        print("保留了已有文件供排查；不会自动重装或覆盖。系统命令错误详见 /var/lib/langge-3xui/system-error.log（如存在）。", file=sys.stderr)
        sys.exit(1)
