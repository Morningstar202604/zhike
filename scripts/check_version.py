#!/usr/bin/env python
"""版本一致性检查：``core/version.py`` / ``VERSION`` / ``pyproject.toml`` 三处必须一致。

CI 与本地发布前都会跑这个脚本。任何一处漂移都视为失败——版本号不一致会让
"打出来的镜像版本"和"接口自报版本"对不上，排查线上问题时极易误判。

用法：
    python scripts/check_version.py            # 校验，不一致返回 1
    python scripts/check_version.py --print    # 只打印当前版本
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_version_file() -> str:
    path = ROOT / "VERSION"
    if not path.exists():
        raise SystemExit(f"[FAIL] 缺少版本文件：{path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"[FAIL] 版本文件为空：{path}")
    return value


def _read_pyproject_version() -> str:
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    # 只匹配 [project] 段内的 version，避免命中 tool.ruff 的 target-version
    match = re.search(r"^\[project\](.*?)(?=^\[)", text, re.S | re.M)
    if not match:
        raise SystemExit("[FAIL] pyproject.toml 缺少 [project] 段")
    version_match = re.search(r'^version\s*=\s*"([^"]+)"', match.group(1), re.M)
    if not version_match:
        raise SystemExit("[FAIL] pyproject.toml 缺少 version 字段")
    return version_match.group(1)


def _read_module_version() -> str:
    sys.path.insert(0, str(ROOT))
    from core.version import VERSION  # noqa: PLC0415  (延迟导入，避免影响 --print 之外的路径)

    return VERSION


def main() -> int:
    parser = argparse.ArgumentParser(description="校验三处版本号是否一致")
    parser.add_argument("--print", dest="print_only", action="store_true", help="只打印当前版本")
    args = parser.parse_args()

    module_version = _read_module_version()
    if args.print_only:
        print(module_version)
        return 0

    file_version = _read_version_file()
    pyproject_version = _read_pyproject_version()

    pairs = [
        ("core/version.py", module_version),
        ("VERSION", file_version),
        ("pyproject.toml", pyproject_version),
    ]
    for name, value in pairs:
        print(f"  {name:<20} {value}")

    distinct = {value for _, value in pairs}
    if len(distinct) != 1:
        print("\n[FAIL] 版本号不一致，请同步上述三处后再提交。", file=sys.stderr)
        return 1

    print(f"\n[OK] 版本一致：{module_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
