"""标镜本地启动与环境检查，不依赖调用时所在目录。"""
import argparse
import importlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app'))


def main(argv=None):
    run_args = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description='标镜本地启动与环境检查')
    ap.add_argument('--check', action='store_true', help='只检查依赖，不启动工作台')
    ap.add_argument('--require-ocr', action='store_true', help='OCR 不可用时退出报错')
    ap.add_argument('--workspace', default=str(Path.home() / 'Documents' / '标镜工作区'))
    ap.add_argument('--port', type=int, default=0, help='默认自动选择空闲端口')
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args(run_args)
    if sys.version_info < (3, 10):
        print('需要 Python 3.10 或更高版本。', file=sys.stderr)
        return 2
    if not args.check:
        try:
            from biaojing.updater import update_at_start
            updated_to = update_at_start(workspace_root=args.workspace)
            if updated_to:
                print(f'已自动更新到 {updated_to}，正在重新启动。', flush=True)
                os.execv(sys.executable, [sys.executable, str(ROOT / 'launch.py'), *run_args])
        except Exception as exc:
            print(f'自动更新暂不可用，继续启动当前版本：{exc}', file=sys.stderr)
    for module in ('pymupdf', 'docx', 'openpyxl'):
        try:
            importlib.import_module(module)
        except (ImportError, OSError) as exc:
            print(f'依赖 {module} 无法加载：{exc}。请运行 install_macos.command。', file=sys.stderr)
            return 2
    from biaojing.pdf_parser import _ocr_runtime
    binary, lang, error = _ocr_runtime()
    print(f'Python：{sys.version.split()[0]}；文档解析依赖可加载。', flush=True)
    print(f'OCR：{error}' if error else f'OCR：{binary}；语言 {lang}', flush=True)
    if error and args.require_ocr:
        return 2
    if args.check:
        return 0
    from biaojing.webapp import main as serve
    options = ['--workspace', args.workspace, '--port', str(args.port)]
    if not args.no_browser:
        options.append('--open-browser')
    try:
        return serve(options)
    except OSError as exc:
        print(f'工作区无法打开：{exc}。请指定可写的 --workspace 目录。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
