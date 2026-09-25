#!/bin/zsh
set -eu
cd -- "${0:A:h}"
python_bin=""
for candidate in "${BIAOJING_PYTHON:-}" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  [[ -n "$candidate" ]] || continue
  if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    python_bin="$candidate"
    break
  fi
done
if [[ -z "$python_bin" ]]; then
  print '未找到 Python 3.10+。请先安装 Python，再重新运行。'
  exit 1
fi
"$python_bin" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python launch.py --check
print 'Python 依赖安装完成。可双击 start_macos.command 启动；扫描件还需本机 Tesseract 和 chi_sim 中文模型。'
