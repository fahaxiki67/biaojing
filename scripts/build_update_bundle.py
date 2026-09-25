#!/usr/bin/env python3
"""Build the allow-listed source bundle attached to a GitHub Release."""

import ast
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main(tag: str) -> int:
    version = tag.removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
        raise SystemExit("Release tag must be vMAJOR.MINOR.PATCH")
    init = ROOT / "app" / "biaojing" / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))
    actual = next((ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "VERSION"
                           for t in node.targets)), None)
    if actual != version:
        raise SystemExit(f"Release tag {tag} does not match app version {actual}")
    output = ROOT / f"biaojing-source-{tag}.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted((ROOT / "app" / "biaojing").rglob("*")):
            if (path.is_file() and "__pycache__" not in path.parts
                    and path.suffix != ".pyc"):
                bundle.write(path, path.relative_to(ROOT).as_posix())
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) == 2 else ""))
