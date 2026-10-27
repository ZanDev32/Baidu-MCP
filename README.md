# Baidu MCP

A Model Context Protocol server for Baidu web search and page extraction.

Fork of [Evilran/baidu-mcp-server](https://github.com/Evilran/baidu-mcp-server), modernised for
the current MCP SDK: stdio **and** streamable HTTP, structured tool output, and a browser
backend that can reuse the browser your IDE or agent harness already runs (over CDP) instead of
downloading its own Chromium.

## What changed from upstream

| Area | Upstream | This fork |
|---|---|---|
| SDK | `mcp>=1.3` (breaks on mcp 2.x: `mcp.server.fastmcp` was removed) | `mcp>=2,<3` (`MCPServer`) |
| Transport | stdio only | stdio (default) and `--transport streamable-http` |
| Browser | forces a Playwright Chromium download; crashes if absent | `--browser auto\|cdp\|playwright\|http`, degrades to plain HTTP |
| IDE / harness browser | unsupported | attaches over CDP: `--cdp-url`, or auto-discovers the oh-my-pi browser |
| Result URLs | `m.baidu.com/...` tracking redirects | the real target URL (`data-log.mu`) |
| Titles | often `None` | parsed from `[data-module="title"]`, widget cards filtered out |
| Content fetch | 150-character teaser, buried in `deep_mode` | `fetch_url` tool, readable Markdown up to `max_chars` |
| Tool output | prose string | structured JSON (`outputSchema`), `readOnlyHint` annotations |
| Prompt-injection surface | — | bot-challenge pages detected and reported as errors |

## Install

```bash
# no browser download, works out of the box (plain HTTP transport)
uvx baidu-mcp-server

# with browser support (CDP attach or bundled Chromium)
uvx --from "baidu-mcp-server[browser]" baidu-mcp-server
```

`pip install baidu-mcp-server` / `pip install "baidu-mcp-server[browser]"` work too.

## MCP client configuration

### stdio (default)

```json
{
  "mcpServers": {
    "baidu": {
      "command": "uvx",
      "args": ["--from", "baidu-mcp-server[browser]", "baidu-mcp-server"]
    }
  }
}
```

### Streamable HTTP

```bash
baidu-mcp-server --transport streamable-http --host 127.0.0.1 --port 8000
# endpoint: http://127.0.0.1:8000/mcp
```

## Browser backends

Baidu increasingly answers automated requests with a captcha. A real browser with a real
fingerprint is the reliable path, and you may already have one running.

| `--browser` | Behaviour |
|---|---|
| `auto` (default) | Prefers a harness/CDP browser, and switches to it as soon as one appears; else a launched Chromium, else plain HTTP |
| `cdp` | Attach to a running browser — no Chromium download, shares its network/TLS fingerprint. Waits up to `--cdp-wait` seconds for one to appear, then errors |
| `playwright` | Launch a local Playwright Chromium (needs `playwright install chromium`) |
| `http` | No browser at all: `curl_cffi` (Chrome TLS impersonation), `httpx` fallback |

### Reusing the oh-my-pi / IDE browser

```bash
# explicit endpoint
baidu-mcp-server --browser cdp --cdp-url http://127.0.0.1:9222

# or just let it auto-discover the oh-my-pi managed Chromium
baidu-mcp-server --browser auto
```

Discovery is re-run every time a browser is (re)connected, so an `auto` backend that had to launch
its own Chromium moves over to the harness browser within ~30 s of it starting, and reattaches if
the harness browser restarts. `auto`/`cdp` resolve the endpoint in this order:

1. `--cdp-url`
2. `BAIDU_MCP_CDP_URL`
3. `BROWSER_CDP_URL`
4. oh-my-pi's running browser: `~/.omp/run/daemons/*/omp.browser*.profile/DevToolsActivePort`

Any Chrome started with `--remote-debugging-port=9222` works, as do CDP proxies from IDEs and
agent harnesses. The server creates an isolated browser context (own cookies, mobile UA), so it
does not touch your open tabs.

## Tools

### `search`

```json
{
  "query": "python mcp server",
  "max_results": 6,
  "deep_mode": false
}
```

Returns:

```json
{
  "query": "python mcp server",
  "engine": "baidu",
  "transport": "cdp",
  "results": [
    {
      "title": "mcp 1.8.0 - mcp · PyPI",
      "url": "https://pypi.org/project/mcp/1.8.0/",
      "snippet": "We recommend using uv to manage your Python projects...",
      "position": 1
    }
  ]
}
```

`deep_mode: true` additionally fetches each result page and appends a short content preview.

### `fetch_url`

```json
{ "url": "https://example.com/article", "max_chars": 8000 }
```

Returns `{ "url", "content", "truncated" }`, where `content` is readability-extracted Markdown.
Client-side redirects (`window.location.replace`, `<meta refresh>`) are followed.

## Rate limiting

Search: 30 requests/minute. Content fetching: 20 requests/minute. Excess requests wait instead
of failing.

## Configuration

| Flag | Env var | Default |
|---|---|---|
| `--transport` | `BAIDU_MCP_TRANSPORT` | `stdio` |
| `--host` | `BAIDU_MCP_HOST` | `127.0.0.1` |
| `--port` | `BAIDU_MCP_PORT` | `8000` |
| `--browser` | `BAIDU_MCP_BROWSER` | `auto` |
| `--cdp-url` | `BAIDU_MCP_CDP_URL` | auto-discovered |
| `--cdp-wait` | `BAIDU_MCP_CDP_WAIT` | `0` (seconds to wait for a CDP endpoint in `cdp` mode) |
| `--log-level` | `BAIDU_MCP_LOG_LEVEL` | `INFO` |

Smoke test without an MCP client:

```bash
baidu-mcp-server --query "南京天气" --browser http
```

## Requirements

Python 3.10+. Works without any browser download; `[browser]` adds Playwright for the CDP and
launched-Chromium backends.

## License

MIT. Original work © Evilran; this fork © ZanDev32.
