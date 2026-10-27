"""Baidu Search MCP server.

Exposes Baidu web search and page extraction over the Model Context Protocol.
Speaks stdio and streamable HTTP, and drives either a bundled Playwright
Chromium or the browser your IDE/harness already runs, over CDP
(``--cdp-url``, ``BAIDU_MCP_CDP_URL``, or an auto-discovered oh-my-pi browser).
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlencode, urlparse

import httpx
import markdownify
import readabilipy.simple_json
from bs4 import BeautifulSoup
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__

logger = logging.getLogger("baidu_mcp_server")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

SEARCH_URL = "https://m.baidu.com/s"
CARD_SELECTOR = "div.c-result.result"
# Baidu's own widgets (AI answer, "people also search", related list) aren't results.
INTERNAL_HOSTS = ("m.baidu.com", "recommend_list.baidu.com", "ikan.baidu.com")
CHALLENGE_MARKERS = ("/static/captcha", "wappass.baidu.com/static", "百度安全验证")

BrowserMode = Literal["auto", "cdp", "playwright", "http"]

ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=True)


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    position: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "position": self.position,
        }


class SearchHit(BaseModel):
    title: str = Field(description="Result title as shown by Baidu")
    url: str = Field(description="Real target URL, not a Baidu redirect")
    snippet: str = Field(description="Abstract text, plus labels or a content preview")
    position: int = Field(description="1-based rank")


class SearchResponse(BaseModel):
    query: str
    engine: str = "baidu"
    transport: Literal["cdp", "playwright", "http"] = Field(
        description="Browser backend that served the request"
    )
    results: list[SearchHit]


class FetchResponse(BaseModel):
    url: str = Field(description="Final URL after redirects")
    content: str = Field(description="Main content as Markdown")
    truncated: bool = Field(description="True when content was cut at max_chars")


class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self.window = timedelta(minutes=1)
        self.requests: list[datetime] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = datetime.now()
            self.requests = [r for r in self.requests if now - r < self.window]
            if len(self.requests) >= self.requests_per_minute:
                wait = 60 - (now - self.requests[0]).total_seconds()
                if wait > 0:
                    await asyncio.sleep(wait)
            self.requests.append(datetime.now())


# --------------------------------------------------------------------------- #
# HTML parsing (pure functions: trivially testable)
# --------------------------------------------------------------------------- #
def _text(element: Any) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _is_internal(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in INTERNAL_HOSTS)


def _target_in_log(node: Any) -> Optional[str]:
    """Baidu keeps the real target URL in a ``data-log`` attribute's ``mu`` key.

    It sits on an ancestor of the title node, so walk up until one matches.
    """
    while node is not None and getattr(node, "name", None) not in (None, "[document]"):
        for attr in ("data-log", "data-show-ext"):
            raw = node.get(attr)
            if not raw or '"mu"' not in raw:
                continue
            try:
                value = json.loads(raw).get("mu")
            except ValueError:
                match = re.search(r'"mu":"([^"]+)"', raw)
                value = match.group(1) if match else None
            if isinstance(value, str) and value.startswith("http"):
                return value
        node = node.parent
    return None


def _result_url(card: Any, title_node: Any) -> str:
    url = _target_in_log(title_node) or _target_in_log(card)
    if url:
        return url
    for element in card.select("[rl-link-href]"):
        value = element.get("rl-link-href")
        if value:
            return value.strip()
    for anchor in card.select("a[href]"):
        href = anchor["href"]
        if href.startswith("http") and not _is_internal(href):
            return href
    return ""


def parse_search_page(html: str) -> list[SearchResult]:
    """Extract organic results from a Baidu mobile SERP."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []
    seen: set[str] = set()

    for card in soup.select(CARD_SELECTOR):
        title_node = card.select_one('[data-module="title"]')
        if title_node is None:
            continue  # Baidu widget card, not an organic result
        title = _text(title_node)
        url = _result_url(card, title_node)
        if not title or not url or url in seen or _is_internal(url):
            continue
        seen.add(url)
        labels = [t for t in (_text(tag) for tag in card.select('[class*="sc-tag"]')) if t]
        snippet = _text(card.select_one('[data-module="abstract"]'))
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=" \n".join(x for x in (" · ".join(labels), snippet) if x),
                position=len(results) + 1,
            )
        )
    return results


def looks_like_challenge(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


def html_to_markdown(html: str) -> str:
    """Readability + Markdown, no Node.js required."""
    extracted = readabilipy.simple_json.simple_json_from_html_string(
        html, use_readability=False
    )
    content = extracted.get("content") if isinstance(extracted, dict) else None
    if content:
        return markdownify.markdownify(content, heading_style=markdownify.ATX).strip()

    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        element.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()


# --------------------------------------------------------------------------- #
# Browser backends: bundled Playwright Chromium, or an existing CDP browser
# --------------------------------------------------------------------------- #
def _cdp_reachable(url: str, timeout: float = 1.0) -> bool:
    """Probe a CDP HTTP endpoint; ``ws://`` endpoints are taken on trust."""
    if url.startswith(("ws://", "wss://")):
        return True
    try:
        with urllib.request.urlopen(
            f"{url.rstrip('/')}/json/version", timeout=timeout
        ) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - any failure means unusable
        return False


def discover_cdp_url() -> Optional[str]:
    """Resolve a CDP endpoint: env var first, then a running oh-my-pi browser."""
    candidates = [
        os.environ.get(key, "").strip() for key in ("BAIDU_MCP_CDP_URL", "BROWSER_CDP_URL")
    ]
    pattern = str(
        Path.home()
        / ".omp"
        / "run"
        / "daemons"
        / "*"
        / "omp.browser*.profile"
        / "DevToolsActivePort"
    )
    for port_file in sorted(glob.glob(pattern), reverse=True):
        try:
            port = int(Path(port_file).read_text().splitlines()[0])
        except (OSError, ValueError, IndexError):
            continue
        candidates.append(f"http://127.0.0.1:{port}")

    for url in candidates:
        if url and _cdp_reachable(url):
            return url
    return None


class BrowserBackend:
    """Lazily starts a Playwright context: attached over CDP or launched locally.

    A harness (oh-my-pi, IDE) browser is preferred whenever one is reachable:
    discovery runs on every (re)connect, and an ``auto`` backend that had to
    launch its own Chromium switches to the harness browser once it appears.
    """

    DISCOVERY_INTERVAL = 30.0  # seconds between probes while no CDP browser is up
    RETRY_START_INTERVAL = 60.0  # back-off after a start attempt produced nothing

    def __init__(
        self,
        mode: BrowserMode = "auto",
        cdp_url: Optional[str] = None,
        cdp_wait: float = 0.0,
    ):
        self.requested_mode = mode
        self.explicit_cdp_url = cdp_url
        self.cdp_url = cdp_url
        self.cdp_wait = cdp_wait
        self.mode: BrowserMode = "http"
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()
        self._last_discovery = 0.0
        self._next_start_attempt = 0.0

    async def context(self) -> Any:
        """A Playwright BrowserContext, or None when plain HTTP is used."""
        async with self._lock:
            if self._context is not None and not self._alive():
                logger.info("Browser connection lost; reconnecting")
                await self._teardown()
            if self._context is not None and self._should_upgrade():
                logger.info("Harness browser available; switching to it")
                await self._teardown()
            if self._context is None and time.monotonic() >= self._next_start_attempt:
                await self._start()
            return self._context

    def _alive(self) -> bool:
        return self.mode != "cdp" or bool(self._browser and self._browser.is_connected())

    def _should_upgrade(self) -> bool:
        """An ``auto`` backend that self-launched should move to the harness browser."""
        if self.requested_mode != "auto" or self.mode != "playwright":
            return False
        now = time.monotonic()
        if now - self._last_discovery < self.DISCOVERY_INTERVAL:
            return False
        self._last_discovery = now
        discovered = discover_cdp_url()
        if not discovered:
            return False
        self.cdp_url = discovered
        return True

    async def _resolve_cdp_url(self) -> Optional[str]:
        """Explicit endpoint wins; otherwise rediscover, optionally waiting for one."""
        if self.explicit_cdp_url:
            return self.explicit_cdp_url
        if self.requested_mode not in ("auto", "cdp"):
            return None
        deadline = time.monotonic() + (self.cdp_wait if self.requested_mode == "cdp" else 0)
        while True:
            self._last_discovery = time.monotonic()
            found = discover_cdp_url()
            if found or time.monotonic() >= deadline:
                return found
            await asyncio.sleep(0.5)

    async def _start(self) -> None:
        if self.requested_mode == "http":
            return

        self.cdp_url = await self._resolve_cdp_url()
        if self.requested_mode == "cdp" and not self.cdp_url:
            raise ToolError(
                "No reachable CDP endpoint. Start a browser with --remote-debugging-port "
                "(or open a tab in oh-my-pi), pass --cdp-url, or raise --cdp-wait."
            )

        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError:
            if self.requested_mode == "auto":
                logger.info("Playwright not installed; using plain HTTP transport")
                return
            raise ToolError(
                'Playwright is not installed. Install it with "pip install baidu-mcp-server[browser]".'
            ) from None

        self._playwright = await async_playwright().start()

        if self.cdp_url and self.requested_mode in ("auto", "cdp"):
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
                self._context = await self._browser.new_context(
                    user_agent=MOBILE_UA,
                    viewport={"width": 390, "height": 844},
                    ignore_https_errors=True,
                )
                self.mode = "cdp"
                logger.info("Attached to browser over CDP at %s", self.cdp_url)
                return
            except Exception as exc:  # noqa: BLE001 - any attach failure falls back
                if self.requested_mode == "cdp":
                    raise ToolError(
                        f"Cannot attach to CDP endpoint {self.cdp_url}: {exc}"
                    ) from exc
                logger.warning("CDP attach failed (%s); launching Chromium instead", exc)

        try:
            self._browser = await self._playwright.chromium.launch()
            self._context = await self._browser.new_context(
                user_agent=MOBILE_UA,
                viewport={"width": 390, "height": 844},
                ignore_https_errors=True,
            )
            self.mode = "playwright"
            logger.info("Launched Playwright Chromium")
        except Exception as exc:  # noqa: BLE001
            if self.requested_mode == "playwright":
                raise ToolError(
                    "Cannot launch Chromium. Install it with \"playwright install chromium\"."
                ) from exc
            logger.warning("Browser unavailable (%s); falling back to HTTP", exc)
            await self._stop_playwright()

        if self._context is None:
            # Nothing started: don't retry on every call.
            self._next_start_attempt = time.monotonic() + self.RETRY_START_INTERVAL

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    async def _teardown(self) -> None:
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
        await self._stop_playwright()
        self._context = self._browser = None
        self.mode = "http"

    async def close(self) -> None:
        await self._teardown()
        await self._stop_playwright()


# --------------------------------------------------------------------------- #
# Transport-agnostic HTML fetch
# --------------------------------------------------------------------------- #
async def fetch_html(
    backend: BrowserBackend, url: str, params: Optional[dict[str, str]] = None
) -> str:
    query = f"?{urlencode(params)}" if params else ""
    context = await backend.context()

    if context is not None:
        page = await context.new_page()
        try:
            response = await page.goto(
                f"{url}{query}", wait_until="domcontentloaded", timeout=30_000
            )
            if response is not None and not response.ok:
                raise RuntimeError(f"HTTP {response.status} from {url}")
            return await page.content()
        finally:
            await page.close()

    try:
        from curl_cffi.requests import AsyncSession
    except ModuleNotFoundError:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": MOBILE_UA}
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.text

    async with AsyncSession(impersonate="chrome", timeout=30) as session:
        response = await session.get(
            url, params=params, headers={"Accept-Language": "zh-CN,zh;q=0.9"}
        )
        response.raise_for_status()
        return response.text


# --------------------------------------------------------------------------- #
# Search + content extraction
# --------------------------------------------------------------------------- #
class BaiduSearcher:
    def __init__(
        self,
        backend: BrowserBackend,
        fetcher: Optional["ContentFetcher"] = None,
        requests_per_minute: int = 30,
    ):
        self.backend = backend
        self.fetcher = fetcher or ContentFetcher(backend)
        self.rate_limiter = RateLimiter(requests_per_minute)

    async def search(
        self,
        query: str,
        max_results: int = 6,
        deep_mode: bool = False,
        max_retries: int = 2,
    ) -> list[SearchResult]:
        html = await self._serp(query, max_retries)
        results = parse_search_page(html)[:max_results]
        if not results:
            if looks_like_challenge(html):
                raise ToolError(
                    "Baidu served a bot-verification page. Retry later, reduce the request "
                    "rate, or attach a real browser with --cdp-url."
                )
            raise ToolError(
                "Baidu returned a page with no parseable results. The layout may have "
                "changed, or the request was throttled."
            )
        if deep_mode:
            previews = await asyncio.gather(
                *(self.fetcher.fetch(r.url, max_chars=600) for r in results),
                return_exceptions=True,
            )
            for result, preview in zip(results, previews):
                if isinstance(preview, FetchResponse) and preview.content:
                    result.snippet = f"{result.snippet}\n\n{preview.content}".strip()
        return results

    async def _serp(self, query: str, max_retries: int) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            await self.rate_limiter.acquire()
            try:
                return await fetch_html(self.backend, SEARCH_URL, {"word": query})
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                logger.warning("Baidu request %d/%d failed: %s", attempt, max_retries, exc)
                if attempt < max_retries:
                    await asyncio.sleep(min(2**attempt, 8))
        raise ToolError(f"Baidu search failed: {last_error}")


class ContentFetcher:
    def __init__(self, backend: BrowserBackend, requests_per_minute: int = 20):
        self.backend = backend
        self.rate_limiter = RateLimiter(requests_per_minute)

    async def fetch(self, url: str, max_chars: int = 8000) -> FetchResponse:
        if not url.startswith(("http://", "https://")):
            raise ToolError("url must start with http:// or https://")
        await self.rate_limiter.acquire()
        try:
            html = await fetch_html(self.backend, url)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
            raise ToolError(f"Failed to fetch {url}: {exc}") from exc

        final_url, html = _follow_client_redirects(url, html)
        markdown = html_to_markdown(html)
        if not markdown:
            raise ToolError(f"No readable text extracted from {final_url}")
        return FetchResponse(
            url=final_url,
            content=markdown[:max_chars],
            truncated=len(markdown) > max_chars,
        )


def _follow_client_redirects(url: str, html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", string=re.compile(r"window\.location\.replace"))
    if script and script.string:
        match = re.search(r'window\.location\.replace\("([^"]+)"\)', script.string)
        if match:
            return match.group(1), html
    meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if meta:
        content = meta.get("content", "")
        if "url=" in content.lower():
            return content.split("url=", 1)[-1].strip(), html
    return url, html


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #
server = MCPServer(
    name="baidu-search",
    title="Baidu Search",
    version=__version__,
    instructions=(
        "Search Baidu's web index and read pages. Use `search` for discovery and "
        "`fetch_url` to read one result in full. Chinese-language queries return the "
        "best coverage; results are ordered by Baidu's ranking."
    ),
)

backend = BrowserBackend()
fetcher = ContentFetcher(backend)
searcher = BaiduSearcher(backend, fetcher=fetcher)


@server.tool(annotations=ANNOTATIONS)
async def search(
    query: str,
    max_results: int = 6,
    deep_mode: bool = False,
) -> SearchResponse:
    """Search Baidu and return organic results.

    Args:
        query: Search query (Chinese works best).
        max_results: Maximum number of results to return (1-20).
        deep_mode: Also fetch each result page and append a short content preview.
    """
    if not query.strip():
        raise ToolError("query must not be empty")
    max_results = max(1, min(max_results, 20))

    results = await searcher.search(query, max_results=max_results, deep_mode=deep_mode)
    return SearchResponse(
        query=query,
        transport=backend.mode,
        results=[
            SearchHit(
                title=r.title, url=r.url, snippet=r.snippet, position=r.position
            )
            for r in results
        ],
    )


@server.tool(annotations=ANNOTATIONS)
async def fetch_url(url: str, max_chars: int = 8000) -> FetchResponse:
    """Fetch a web page and return its main content as Markdown.

    Args:
        url: Absolute http(s) URL.
        max_chars: Maximum characters of Markdown to return (200-50000).
    """
    max_chars = max(200, min(max_chars, 50_000))
    return await fetcher.fetch(url, max_chars=max_chars)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baidu-mcp-server", description="Baidu Search MCP server"
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("BAIDU_MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("BAIDU_MCP_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("BAIDU_MCP_PORT", "8000"))
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "cdp", "playwright", "http"),
        default=os.environ.get("BAIDU_MCP_BROWSER", "auto"),
        help="Browser backend: reuse an existing browser over CDP, launch Playwright "
        "Chromium, or skip browsers entirely (default: auto)",
    )
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("BAIDU_MCP_CDP_URL"),
        help="CDP endpoint of a running browser, e.g. http://127.0.0.1:9222",
    )
    parser.add_argument(
        "--cdp-wait",
        type=float,
        default=float(os.environ.get("BAIDU_MCP_CDP_WAIT", "0")),
        help="With --browser cdp, seconds to wait for a CDP endpoint to appear",
    )
    parser.add_argument(
        "--query", help="Run a single search, print JSON, and exit (smoke test)"
    )
    parser.add_argument("--log-level", default=os.environ.get("BAIDU_MCP_LOG_LEVEL", "INFO"))
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    global backend, fetcher, searcher
    backend = BrowserBackend(
        mode=args.browser, cdp_url=args.cdp_url, cdp_wait=args.cdp_wait
    )
    fetcher = ContentFetcher(backend)
    searcher = BaiduSearcher(backend, fetcher=fetcher)

    if args.query:

        async def run_query() -> list[SearchResult]:
            try:
                return await searcher.search(args.query, max_results=6)
            finally:
                await backend.close()  # same event loop, or Playwright deadlocks

        results = asyncio.run(run_query())
        print(
            json.dumps(
                {"query": args.query, "results": [r.as_dict() for r in results]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run("streamable-http", host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
