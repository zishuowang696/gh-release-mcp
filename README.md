# gh-release-mcp

> An **MCP server** that lets an agent list and download **GitHub release assets** — with multi-mirror parallel ranged download and sha256 verification. **Zero external binaries** (pure Python standard library: no `aria2`, `curl`, `gh`, `jq`). By [embedai.com.cn](https://embedai.com.cn).

Built for restricted networks: it probes which mirrors support HTTP `Range` (206), splits a file across mirrors and downloads segments in parallel, then verifies sha256 from GitHub's own asset `digest`.

## Tools

| Tool | What it does |
| --- | --- |
| `list_release_assets` | List a release's assets (name, size, url). |
| `download_release_asset` | Download one asset (by `name`) or several (by glob `pattern`) with parallel ranged requests across mirrors; verifies sha256 when available. |

## Install / run

```bash
# run instantly (no install)
uvx gh-release-mcp

# or via pip
pip install gh-release-mcp
gh-release-mcp
```

It speaks MCP over **stdio**.

## Configure your client

**opencode** (`opencode.json`):

```jsonc
{
  "mcp": {
    "gh-release": {
      "type": "local",
      "command": ["uvx", "gh-release-mcp"]
    }
  }
}
```

**Claude Desktop / Cursor** (`mcpServers`):

```json
{
  "mcpServers": {
    "gh-release": { "command": "uvx", "args": ["gh-release-mcp"] }
  }
}
```

## Tools in detail

`download_release_asset` arguments:

| arg | meaning |
| --- | --- |
| `owner`, `repo` | e.g. `NationalSecurityAgency`, `ghidra` |
| `tag` | release tag; omit or `latest` |
| `name` | exact asset name (optional) |
| `pattern` | glob, e.g. `*PUBLIC*.zip` (optional) |
| `dest` | destination directory (default: cwd) |
| `mirrors` | mirror prefixes (must support Range); default `ghproxy.net` + `ghfast.top` |
| `connections` | segments per file (default 8) |

Env vars: `GITHUB_TOKEN` (optional, raises API limits / private repos).

## Example prompts

- "Using gh-release, list the assets of `BurntSushi/ripgrep` latest."
- "Download `NationalSecurityAgency/ghidra` `Ghidra_12.1.4_build` matching `*PUBLIC*.zip` into `./ghidra`."

## Related

- CLI tool (no MCP): [gh-download](https://github.com/zishuowang696/gh-download) · PyPI `gh-download-embedai`
- Blog: <https://embedai.com.cn>

## License

MIT
