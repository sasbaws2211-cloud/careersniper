"""Package the extension for a given CareerSniper server.

    python extension/build.py --api https://jobs.example.com [--out dist]

Rewrites config.js and the manifest's host permission to that server (both must agree, or Chrome blocks the requests),
checks the result, and writes dist/careersniper-extension-<version>.zip, ready to load unpacked or to upload to the Chrome Web Store.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
FILES = ["manifest.json", "config.js", "parse.js", "extract.js", "content.js", "background.js", "popup.html", "popup.js", "icons/icon128.png"]
LOCAL = {"localhost", "127.0.0.1"}


def check_api(raw: str) -> str:
    u = urlparse((raw or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname or u.path.strip("/") or u.query or u.fragment or u.username:
        raise SystemExit("--api must be a plain site address such as https://jobs.example.com (no path, query or login).")
    if u.scheme == "http" and u.hostname not in LOCAL:
        raise SystemExit("--api must use https:// unless it is localhost: job details would otherwise cross the network unencrypted.")
    return f"{u.scheme}://{u.netloc}"


def render(api: str) -> dict[str, bytes]:
    """The files of the package, with the server address filled in."""
    out = {name: (HERE / name).read_bytes() for name in FILES}
    config, n = re.subn(r'const CS_API_BASE = "[^"]*";', f'const CS_API_BASE = "{api}";', out["config.js"].decode("utf-8"))
    if n != 1:
        raise SystemExit("config.js does not contain exactly one CS_API_BASE line.")
    out["config.js"] = config.encode("utf-8")
    manifest = json.loads(out["manifest.json"])
    manifest["host_permissions"] = [f"{api}/*"]
    out["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    return out


def build(api: str, out_dir: Path) -> Path:
    files = render(check_api(api))
    version = json.loads(files["manifest.json"])["version"]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"careersniper-extension-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return target


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", required=True, help="the CareerSniper site, e.g. https://jobs.example.com")
    ap.add_argument("--out", default="dist", help="folder for the zip (default: dist)")
    args = ap.parse_args(argv)
    print(build(args.api, Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
