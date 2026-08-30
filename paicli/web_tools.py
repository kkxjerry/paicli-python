"""Phase 9：带网络策略的网页搜索和正文抓取。

    URL -> NetworkPolicy -> WebFetcher -> HtmlExtractor -> 可读文本
    query -> SearchProvider -> 结构化搜索结果

两条链路最终都可注册成 Agent 工具。
"""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Protocol

from .tools import (
    ConcurrencyPolicy,
    ToolRegistry,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)


class NetworkPolicy:
    """在发起请求前检查 URL，降低访问本机/内网的 SSRF 风险。"""

    def __init__(
        self,
        *,
        blocked_hosts: set[str] | None = None,
        allowed_hosts: set[str] | None = None,
    ) -> None:
        self.blocked_hosts = blocked_hosts or set()
        self.allowed_hosts = allowed_hosts

    def validate(self, url: str) -> str:
        # urlparse 只负责拆分 URL，后面仍需显式检查协议和主机。
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only http and https URLs are allowed")
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise ValueError("URL has no hostname")
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("local network URLs are blocked")
        try:
            # 当 hostname 本身是 IP 字面量时，可直接检测私网、回环、链路本地等地址。
            address = ipaddress.ip_address(hostname)
        except ValueError:
            # 普通域名在本期不做 DNS 解析，因此不能防御域名解析到私网的情况。
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
    """收集 HTML 中可见文本的内部 Parser。"""

    SKIP = {"script", "style", "noscript", "svg", "nav"}

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        # depth 允许处理被跳过标签内部的嵌套结构。
        if tag.lower() in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth and data.strip():
            self.parts.append(data.strip())


class HtmlExtractor:
    """将 HTML 转为去除脚本、样式和多余空白的纯文本。"""

    @staticmethod
    def extract(html: str) -> str:
        parser = _TextExtractor()
        parser.feed(html)
        return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


@dataclass(frozen=True)
class FetchResult:
    """网页抓取后保留最终 URL、标题和正文。"""

    url: str
    title: str
    text: str


class WebFetcher:
    """执行受控 HTTP 请求，限制体积并提取可读文本。"""

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
        # 必须先通过策略，再构建网络请求。
        safe_url = self.policy.validate(url)
        request = urllib.request.Request(
            safe_url,
            headers={"User-Agent": "PaiCLI-Python/1"},
        )
        with self.opener(request, timeout=20) as response:  # type: ignore[attr-defined]
            # 多读 1 字节，才能区分“刚好等于限制”和“已经超限”。
            body = response.read(self.max_bytes + 1)
            # geturl() 可能是重定向后的地址；本期没有对最终 URL 再次校验。
            final_url = response.geturl()
        if len(body) > self.max_bytes:
            raise ValueError("web response exceeds size limit")
        html = body.decode("utf-8", errors="replace")
        # 标题单独提取，正文则交给 HTMLParser 处理。
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
    """屏蔽具体搜索引擎的最小接口。"""

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Return structured search results."""


class SearxngSearchProvider:
    """调用 SearXNG JSON API 的搜索实现。"""

    def __init__(
        self,
        endpoint: str,
        *,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.opener = opener

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        # urlencode 正确处理查询中的空格、中文和特殊字符。
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
    # web_fetch 始终可注册，handler 只将正文字符串回灌给模型。
    registry.register(
        ToolSpec(
            "web_fetch",
            "Fetch and extract readable text from a public web page.",
            registry.object_schema(
                {"url": {"type": "string", "format": "uri"}},
                required=["url"],
            ),
            lambda arguments: fetcher.fetch(str(arguments["url"])).text,
            risk=ToolRisk.SAFE,
            side_effect=ToolSideEffect.READ_ONLY,
            concurrency=ConcurrencyPolicy.PARALLEL,
        )
    )
    if search_provider:
        # 只有配置搜索服务时才暴露 web_search，结果序列化为 JSON 供模型阅读。
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
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.PARALLEL,
            )
        )
