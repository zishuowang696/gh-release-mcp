#!/usr/bin/env python3
"""gh-release-mcp — an MCP server that lists and downloads GitHub release assets.

Pure standard library: no aria2, no curl, no jq, no third-party packages.
Downloads use parallel HTTP Range requests across mirrors (ghproxy/ghfast by
default), verify sha256 when GitHub provides a digest, and fall back to a plain
stream when a mirror does not support Range.

Speaks MCP over stdio: newline-delimited JSON-RPC 2.0.
"""

import fnmatch
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "gh-release-mcp"
SERVER_VERSION = "0.1.0"
USER_AGENT = f"{SERVER_NAME}/{SERVER_VERSION}"

DEFAULT_MIRRORS = ["https://ghproxy.net", "https://ghfast.top"]
DEFAULT_CONNECTIONS = 8

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
_print_lock = threading.Lock()


def log(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- GitHub API

def _api_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def get_release(owner: str, repo: str, tag: str) -> dict:
    path = (
        f"/repos/{owner}/{repo}/releases/latest"
        if tag in ("", "latest")
        else f"/repos/{owner}/{repo}/releases/tags/{tag}"
    )
    req = urllib.request.Request("https://api.github.com" + path, headers=_api_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API {e.code} for {owner}/{repo}@{tag}: {e.reason}") from e


def select_assets(assets: list[dict], name: str | None, pattern: str | None) -> list[dict]:
    if name:
        return [a for a in assets if a["name"] == name]
    if pattern:
        return [a for a in assets if fnmatch.fnmatch(a["name"], pattern)]
    return list(assets)


# ---------------------------------------------------------------- downloader

def _candidate_urls(browser_url: str, mirrors: list[str]) -> list[str]:
    urls = [browser_url]
    for m in mirrors:
        urls.append(f"{m.rstrip('/')}/{browser_url}")
    return urls


def _probe_range(url: str) -> bool:
    req = urllib.request.Request(
        url, headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 206
    except Exception:
        return False


def _get_segment(url: str, start: int, end: int) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"Range": f"bytes={start}-{end}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _download_ranged(urls: list[str], size: int, dest: str, connections: int) -> None:
    seg = max(1, size // connections)
    segments = []
    start = 0
    while start < size:
        segments.append((start, min(start + seg - 1, size - 1)))
        start += seg

    fd = os.open(dest, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        os.ftruncate(fd, size)
        done = 0
        counter = threading.Lock()

        def worker(i: int, rng: tuple[int, int]) -> None:
            nonlocal done
            url = urls[i % len(urls)]
            data = _get_segment(url, rng[0], rng[1])
            os.pwrite(fd, data, rng[0])
            with counter:
                done += len(data)
                pct = 100.0 * done / size
                log(f"  [{pct:5.1f}%] {done}/{size} bytes")

        with ThreadPoolExecutor(max_workers=min(connections, len(segments))) as pool:
            futures = [pool.submit(worker, i, r) for i, r in enumerate(segments)]
            for f in futures:
                f.result()
    finally:
        os.close(fd)


def _download_stream(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                log(f"  [{100.0 * done / total:5.1f}%] {done}/{total} bytes")


def download_asset(
    asset: dict, dest_dir: str, mirrors: list[str], connections: int
) -> dict:
    name = asset["name"]
    size = int(asset.get("size") or 0)
    browser_url = asset["browser_download_url"]
    dest = os.path.join(dest_dir, name)
    os.makedirs(dest_dir, exist_ok=True)

    good = [u for u in _candidate_urls(browser_url, mirrors) if _probe_range(u)]
    log(f"[{name}] range-capable sources: {len(good)}/{len(mirrors) + 1}")

    if size > 0 and good:
        _download_ranged(good, size, dest, connections)
    else:
        source = good[0] if good else browser_url
        log(f"[{name}] falling back to single stream ({source})")
        _download_stream(source, dest)

    result = {"name": name, "path": os.path.abspath(dest), "bytes": os.path.getsize(dest)}

    expected = (asset.get("digest") or "").removeprefix("sha256:")
    if expected:
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        actual = h.hexdigest()
        result["sha256"] = actual
        result["sha256_ok"] = actual == expected
    return result


# ------------------------------------------------------------------ MCP glue

TOOLS = [
    {
        "name": "list_release_assets",
        "description": "List the downloadable assets of a GitHub release (name, size, url).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner, e.g. NationalSecurityAgency"},
                "repo": {"type": "string", "description": "Repo name, e.g. ghidra"},
                "tag": {"type": "string", "description": "Release tag; omit or 'latest' for the latest release."},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "download_release_asset",
        "description": (
            "Download release asset(s) with parallel ranged requests across mirrors. "
            "Pick one asset by exact name, or several via a glob pattern. "
            "Verifies sha256 when GitHub exposes a digest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "tag": {"type": "string", "description": "Release tag; omit or 'latest'."},
                "name": {"type": "string", "description": "Exact asset name (optional)."},
                "pattern": {"type": "string", "description": "Glob, e.g. '*PUBLIC*.zip' (optional)."},
                "dest": {"type": "string", "description": "Destination directory (default: current dir)."},
                "mirrors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Mirror prefixes (must support HTTP Range). Default: {DEFAULT_MIRRORS}",
                },
                "connections": {"type": "integer", "description": "Parallel segments per file (default 8)."},
            },
            "required": ["owner", "repo"],
        },
    },
]


def call_tool(name: str, args: dict) -> dict:
    if name == "list_release_assets":
        release = get_release(args["owner"], args["repo"], args.get("tag", "latest"))
        assets = [
            {"name": a["name"], "size": a.get("size"), "url": a["browser_download_url"]}
            for a in release.get("assets", [])
        ]
        return {"release": release.get("tag_name"), "assets": assets}

    if name == "download_release_asset":
        release = get_release(args["owner"], args["repo"], args.get("tag", "latest"))
        chosen = select_assets(release.get("assets", []), args.get("name"), args.get("pattern"))
        if not chosen:
            raise RuntimeError("no asset matched the given name/pattern")
        mirrors = args.get("mirrors") or DEFAULT_MIRRORS
        connections = int(args.get("connections") or DEFAULT_CONNECTIONS)
        dest_dir = args.get("dest") or os.getcwd()
        results = [download_asset(a, dest_dir, mirrors, connections) for a in chosen]
        return {"release": release.get("tag_name"), "downloaded": results}

    raise RuntimeError(f"unknown tool: {name}")


def handle(req: dict) -> dict | None:
    method = req.get("method")
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "prompts/list":
        return {"prompts": []}
    if method == "resources/list":
        return {"resources": []}
    if method == "tools/call":
        params = req.get("params") or {}
        data = call_tool(params.get("name"), params.get("arguments") or {})
        return {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
            "isError": False,
        }
    raise RuntimeError(f"unknown method: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("skip non-JSON line")
            continue
        rid = req.get("id")
        if rid is None:  # notification, no reply
            handle(req)
            continue
        try:
            result = handle(req)
            message = {"jsonrpc": "2.0", "id": rid, "result": result if result is not None else {}}
        except Exception as e:  # noqa: BLE001 - report any failure back to the client
            message = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
        with _print_lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
