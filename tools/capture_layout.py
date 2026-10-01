r"""Capture browser geometry for a React/Figma Make source directory.

Usage (PowerShell):
  $env:PYTHONPATH='D:\uagent'; python D:\uagent\tools\capture_layout.py D:\aiassit\test
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.browser_layout import capture_layout


def main() -> int:
    parser = argparse.ArgumentParser(description="采集 React 页面真实浏览器布局")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = capture_layout(args.source)
    if result is None:
        result = {
            "schema": "uagent.browser-layout/v1", "source": str(args.source),
            "status": "static-fallback",
            "reason": "项目缺少可运行的 Vite/node_modules 或 dist/index.html",
        }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
