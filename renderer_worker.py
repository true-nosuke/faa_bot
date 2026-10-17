"""Typstのコードを画像に変換する子プロセス用スクリプト。

使い方: python renderer_worker.py <出力フォルダ>   （Typstのソースは標準入力から渡す）
Bot本体と分けて動かすことで、時間制限での強制終了や、ファイルアクセスの制限をしやすくしている。
"""
import pathlib
import sys
import traceback

import typst

PPI = 160
MAX_PAGES = 3
FONT_DIR = pathlib.Path(__file__).parent / "fonts"


def limit_memory(mb: int = 1024) -> None:
    """Linux/macOSでは、メモリ使用量に上限をかける（Windowsでは何もしない）。"""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024, mb * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass


def main() -> int:
    limit_memory()
    if len(sys.argv) != 2:
        sys.stderr.write("使い方: python renderer_worker.py <出力フォルダ>\n")
        return 2
    out = pathlib.Path(sys.argv[1])
    root = out / "root"  # 空のフォルダ。Typstのコードからはここより外のファイルは読めない
    root.mkdir(exist_ok=True)
    source = sys.stdin.buffer.read()
    try:
        compile_options = {
            "root": str(root),
            "format": "png",
            "ppi": PPI,
        }
        if FONT_DIR.is_dir():
            compile_options["font_paths"] = [str(FONT_DIR)]
        result = typst.compile(source, **compile_options)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        traceback.print_exc(file=sys.stderr)
        return 1
    pages = result if isinstance(result, list) else [result]
    for i, data in enumerate(pages[:MAX_PAGES]):
        (out / f"page-{i}.png").write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())