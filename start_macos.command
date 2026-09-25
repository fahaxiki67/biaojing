#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print '尚未安装本地环境，请先运行 install_macos.command。'
  exit 1
fi
exec .venv/bin/python launch.py "$@"
