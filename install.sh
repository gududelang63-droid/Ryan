#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

if [[ ${EUID} -ne 0 ]]; then
  printf '%s\n' '请先 sudo -i，使用 root 执行。' >&2
  exit 1
fi
if [[ ! -r /etc/os-release ]]; then
  printf '%s\n' '无法识别系统。' >&2
  exit 1
fi
# shellcheck source=/dev/null
. /etc/os-release
if [[ ${ID:-} != ubuntu || ! ${VERSION_ID:-} =~ ^(22\.04|24\.04)$ ]]; then
  printf '%s\n' '仅支持全新 Ubuntu 22.04 / 24.04 LTS。' >&2
  exit 1
fi
missing=0
for command in python3 curl sha256sum; do
  command -v "$command" >/dev/null || missing=1
done
if [[ "$missing" -eq 1 ]]; then
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  apt-get update
  apt-get install -y python3 curl ca-certificates coreutils
fi

work_dir=$(mktemp -d /tmp/langge-bootstrap.XXXXXXXX)
trap 'rm -rf -- "$work_dir"' EXIT
script_sha256='f5f117c78afefef5dc4f17381389b275d70b643b21c9dc5d079f2c07e630d6ff'
script_url='https://raw.githubusercontent.com/gududelang63-droid/Ryan/main/setup.py'
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
  --connect-timeout 20 --max-time 120 --output "$work_dir/setup.py" "$script_url"
printf '%s  %s\n' "$script_sha256" "$work_dir/setup.py" | sha256sum --check --status
python3 "$work_dir/setup.py" "$@"
