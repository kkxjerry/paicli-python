"""Phase 9: guarded web search and page fetching."""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Protocol

from .tools import ToolRegistry, ToolSpec


class NetworkPolicy:
    def __init__(
        self,
        *,
        blocked_hosts: set[str] | None = None,
        allowed_hosts: set[str] | None = None,
    ) -> None:
        self.blocked_hosts = blocked_hosts or set()
        self.allowed_hosts = allowed_hosts

    def validate(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only http and https URLs are allowed")
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise ValueError("URL has no hostname")
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("local network URLs are blocked")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            raise ValueError("private network URLs are blocked")
        if hostname in self.blocked_hosts:
            raise ValueError(f"host is blocked: {hostname}")
        if self.allowed_hosts is not None and hostname not in self.allowed_hosts:
            raise ValueError(f"host is not allow-listed: {hostname}")
        return url


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav"}

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth and data.strip():
            self.parts.append(data.strip())


class HtmlExtractor:
    @staticmethod
    def extract(html: str) -> str:
        parser = _TextExtractor()
        parser.feed(html)
        return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


@dataclass(frozen=True)
class FetchResult:
    url: str
    title: str
    text: str


class WebFetcher:
    def __init__(
        self,
        policy: NetworkPolicy | None = None,
        *,
        max_bytes: int = 1_000_000,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self.policy = policy or NetworkPolicy()
        self.max_bytes = max_bytes
        self.opener = opener

    def fetch(self, url: str) -> FetchResult:
        safe_url = self.policy.validate(url)
        request = urllib.request.Request(
            safe_url,
            headers={"User-Agent": "PaiCLI-Python/1"},
        )
        with self.opener(request, timeout=20) as response:  # type: ignore[attr-defined]
            body = response.read(self.max_bytes + 1)
            final_url = response.geturl()
        if len(body) > self.max_bytes:
            raise ValueError("web response exceeds size limit")
        html = body.decode("utf-8", errors="replace")
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title = HtmlExtractor.extract(title_match.group(1)) if title_match else ""
        return FetchResult(final_url, title, HtmlExtractor.extract(html))


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Return structured search results."""


class SearxngSearchProvider:
    def __init__(
        self,
        endpoint: str,
        *,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.opener = opener

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        url = f"{self.endpoint}/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json"}
        )
        with self.opener(url, timeout=20) as response:  # type: ignore[attr-defined]
            payload = json.loads(response.read().decode("utf-8"))
        return [
            SearchResult(
                str(item.get("title", "")),
                str(item.get("url", "")),
                str(item.get("content", "")),
            )
            for item in payload.get("results", [])[:limit]
        ]


def register_web_tools(
    registry: ToolRegistry,
    *,
    fetcher: WebFetcher,
    search_provider: SearchProvider | None = None,
) -> None:
    registry.register(
        ToolSpec(
            "web_fetch",
            "Fetch and extract readable text from a public web page.",
            registry.object_schema(
                {"url": {"type": "string", "format": "uri"}},
                required=["url"],
            ),
            lambda arguments: fetcher.fetch(str(arguments["url"])).text,
        )
    )
    if search_provider:
        registry.register(
            ToolSpec(
                "web_search",
                "Search the web for current information.",
                registry.object_schema(
                    {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    required=["query"],
                ),
                lambda arguments: json.dumps(
                    [
                        result.__dict__
                        for result in search_provider.search(
                            str(arguments["query"]),
                            int(arguments.get("limit", 5)),
                        )
                    ],
                    ensure_ascii=False,
                ),
            )
        )
