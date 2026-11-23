#!/usr/bin/env python3
"""Mermaid ファイルを VS Code 同梱の Mermaid と headless Chrome で PNG にする。

使い方: python3 -I render.py <diagram.mmd> [-o <diagram.png>]

- frontmatter の theme / layout を redux-color / elk に揃え、.mmd に書き戻す
- 図の大きさに合わせて切り抜き、2 倍の解像度で書き出す
- 結果を JSON で標準出力に出す。描画に失敗したら終了コード 1
"""

import argparse
import html
import json
import math
import re
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RENDERER_SUBPATH = "Visual Studio Code.app/Contents/Resources/app/extensions/mermaid-markdown-features/markdown-preview-out"
CHROME_SUBPATH = "Google Chrome.app/Contents/MacOS/Google Chrome"
APP_DIRS = [Path("/Applications"), Path.home() / "Applications"]

CONFIG = {"theme": "redux-color", "layout": "elk"}
SEQUENCE_CSS = ".actor { fill: #FFFFFF !important; stroke: #28253D !important; stroke-width: 2px !important; } .messageLine1 { stroke: #999999 !important; }"
PADDING = 24
SCALE = 2
TIME_BUDGET_MS = "20000"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>html,body{{margin:0;background:#fff}}body{{padding:{padding}px}}#markdown-mermaid{{display:none}}</style>
</head><body>
<span id="markdown-mermaid" data-config='{{"lightModeTheme":"default","darkModeTheme":"default"}}'></span>
<div class="mermaid">{source}</div>
<script type="module" src="out/index.js"></script>
<script>
let last = "";
const timer = setInterval(() => {{
  const error = document.querySelector(".mermaid-error");
  const svg = document.querySelector(".mermaid svg");
  if (error) {{
    document.body.dataset.status = "error";
    document.body.dataset.error = error.innerText;
    clearInterval(timer);
  }} else if (svg) {{
    const box = svg.viewBox.baseVal;
    svg.removeAttribute("width");
    svg.style.maxWidth = "none";
    svg.style.width = `${{box.width}}px`;
    svg.style.height = `${{box.height}}px`;
    const r = svg.getBoundingClientRect();
    const size = `${{r.width}}x${{r.bottom}}`;
    if (size === last && r.width > 0) {{
      document.body.dataset.status = "ok";
      document.body.dataset.width = r.width;
      document.body.dataset.bottom = r.bottom;
      clearInterval(timer);
    }}
    last = size;
  }}
}}, 300);
</script>
</body></html>
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def find_app(subpath):
    for base in APP_DIRS:
        path = base / subpath
        if path.exists():
            return path
    return None


def normalize_config(source):
    text = source.lstrip("\ufeff\n\r\t ")
    if not text.startswith("---"):
        text = "---\n---\n" + text

    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise SystemExit("frontmatter の閉じ '---' が見つからない")
    head = lines[1:end]
    body = lines[end + 1 :]

    config_at = next((i for i, line in enumerate(head) if re.match(r"^config:\s*$", line)), None)
    if config_at is None:
        head.append("config:")
        config_at = len(head) - 1

    for key, value in reversed(CONFIG.items()):
        pattern = re.compile(rf"^  {key}:\s*(.*)$")
        found = next((i for i, line in enumerate(head) if pattern.match(line)), None)
        if found is None:
            head.insert(config_at + 1, f"  {key}: {value}")
        elif pattern.match(head[found]).group(1).strip() != value:
            head[found] = f"  {key}: {value}"

    kind = next((line.strip() for line in body if line.strip() and not line.strip().startswith("%%")), "")
    if kind.startswith("sequenceDiagram") and not any(line.startswith("  themeCSS:") for line in head):
        pos = config_at + 1
        while pos < len(head) and head[pos].startswith("  "):
            pos += 1
        head.insert(pos, f"  themeCSS: '{SEQUENCE_CSS}'")

    normalized = "\n".join(["---", *head, "---", *body])
    return normalized, normalized != source


def chrome(chrome_path, profile, url, *args):
    cmd = [
        str(chrome_path),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--virtual-time-budget={TIME_BUDGET_MS}",
        *args,
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def body_attr(dom, name):
    match = re.search(rf'<body[^>]*\sdata-{name}="([^"]*)"', dom)
    return html.unescape(match.group(1)) if match else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mmd", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    mmd = args.mmd.resolve()
    png = (args.output or mmd.with_suffix(".png")).resolve()

    renderer = find_app(RENDERER_SUBPATH)
    if renderer is None:
        raise SystemExit("VS Code 同梱の mermaid-markdown-features が見つからない")
    chrome_path = find_app(CHROME_SUBPATH)
    if chrome_path is None:
        raise SystemExit("Google Chrome が見つからない")

    source, changed = normalize_config(mmd.read_text(encoding="utf-8"))
    if changed:
        mmd.write_text(source, encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="draw-diagram-") as tmp:
        site = Path(tmp) / "site"
        site.mkdir()
        (site / "out").symlink_to(renderer)
        (site / "index.html").write_text(PAGE.format(padding=PADDING, source=html.escape(source)), encoding="utf-8")

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(site)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
        profile = Path(tmp) / "chrome-profile"

        try:
            dom = chrome(chrome_path, profile, url, "--window-size=8000,6000", "--dump-dom").stdout
            if body_attr(dom, "status") != "ok":
                error = body_attr(dom, "error") or "時間内に SVG が描画されなかった"
                print(json.dumps({"ok": False, "mmd": str(mmd), "error": error}, ensure_ascii=False))
                sys.exit(1)

            width = math.ceil(float(body_attr(dom, "width"))) + PADDING * 2
            height = math.ceil(float(body_attr(dom, "bottom"))) + PADDING
            png.parent.mkdir(parents=True, exist_ok=True)
            chrome(
                chrome_path,
                profile,
                url,
                f"--window-size={width},{height}",
                f"--force-device-scale-factor={SCALE}",
                f"--screenshot={png}",
            )
        finally:
            server.shutdown()

    if not png.exists():
        print(json.dumps({"ok": False, "mmd": str(mmd), "error": "PNG が書き出されなかった"}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ok": True, "mmd": str(mmd), "png": str(png), "css_size": [width, height], "config_fixed": changed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
