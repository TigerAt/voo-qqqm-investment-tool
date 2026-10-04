#!/bin/zsh

umask 077

root="${0:A:h}"
app_dir="$root/程序文件"

if [[ ! -f "$app_dir/config.json" && -f "$app_dir/config.example.json" ]]; then
  cp -- "$app_dir/config.example.json" "$app_dir/config.json" || exit 1
fi

if [[ ! -f "$app_dir/investment_gui.py" || ! -f "$app_dir/config.json" ]]; then
  print -u2 "程序文件不完整，请确认“程序文件”文件夹与本启动文件位于同一目录。"
  read -k 1 "?按任意键关闭..."
  print
  exit 1
fi

if [[ -x "$root/.venv/bin/python" ]]; then
  python_bin="$root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
else
  print -u2 "未找到 Python 3，无法启动投资管理工具。"
  read -k 1 "?按任意键关闭..."
  print
  exit 1
fi

cd -- "$app_dir" || exit 1
export MPLBACKEND="Agg"
export MPLCONFIGDIR="${TMPDIR:-/tmp}/voo-qqqm-matplotlib-$UID"
export XDG_CACHE_HOME="${TMPDIR:-/tmp}/voo-qqqm-cache-$UID"
mkdir -p -- "$MPLCONFIGDIR" "$XDG_CACHE_HOME"
exec "$python_bin" -B investment_gui.py --config "$app_dir/config.json"
