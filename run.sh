#!/usr/bin/env bash
# Chạy bộ test trên macOS/Linux mà không cần tự activate venv.
# Lần đầu tự tạo .venv và cài thư viện; các lần sau chạy luôn.
#   ./run.sh            -> tương đương python run.py check
#   ./run.sh test -k admin   -> tương đương python run.py test -k admin
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Lần đầu chạy: đang tạo môi trường ảo và cài thư viện..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -r requirements.txt
fi
[ $# -eq 0 ] && set -- check
exec .venv/bin/python run.py "$@"
