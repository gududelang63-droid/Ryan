#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

cleanup() {
  rm -rf .test-release
}
trap cleanup EXIT

printf '%s\n' '==> 1/4 检查 Bash、ShellCheck 与 bootstrap 哈希'
bash -n install.sh
shellcheck install.sh
python3 tests/check_bootstrap.py

printf '%s\n' '==> 2/4 运行安全单元测试'
python3 -m unittest discover -s tests -v

printf '%s\n' '==> 3/4 下载并校验固定版本的 3x-ui 与 acme.sh'
python3 - <<'PY'
import setup
from pathlib import Path

target = Path('.test-release')
target.mkdir()
setup.download_archive('amd64', target / 'release.tar.gz')
setup.download_acme(target / 'acme.sh')
setup.extract_archive(target / 'release.tar.gz', target)
PY

printf '%s\n' '==> 4/4 使用真实 3x-ui 二进制测试 HTTPS、空入站和密码变更'
XUI_TEST_APP="$PWD/.test-release/x-ui" python3 -m unittest discover -s tests -v

printf '\n%s\n' 'PASS：Docker 安全验证全部通过。'
printf '%s\n' '说明：公网证书、UFW、Fail2ban、SSH 保活和重启恢复仍需在独立 Ubuntu VM/VPS 验收。'
