"""Explicit, fail-closed wiring for optional PaiCLI extensions.

The core runtime must remain usable without external services.  Skills, MCP,
and web access are therefore enabled only by a checked-in/user-selected JSON
configuration.  Installing an extension returns prompt metadata and close
callbacks so the bootstrap layer can expose the real runtime capability set
instead of documenting unreachable modules.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping

from .mcp import McpClient, StdioTransport, StreamableHttpTransport
from .skills import SkillContextBuffer, SkillRegistry, register_skill_tool
from .tool_contracts import (
    ConcurrencyPolicy,
    ToolRisk,
    ToolSideEffect,
)
from .tools import ToolRegistry, ToolSpec


class ExtensionConfigurationError(ValueError):
    """An optional extension configuration is unsafe or malformed."""


@dataclass(frozen=True)
class SkillExtensionConfig:
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str
    command: tuple[str, ...] = ()
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class WebExtensionConfig:
    enabled: bool = False
    allowed_hosts: tuple[str, ...] = ()
    search_endpoint: str = ""
    max_response_bytes: int = 1_000_000
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class ExtensionConfig:
    skills: SkillExtensionConfig = SkillExtensionConfig()
    mcp_servers: tuple[McpServerConfig, ...] = ()
    web: WebExtensionConfig = WebExtensionConfig()


@dataclass
class InstalledExtensions:
    """Resources installed into one ToolRegistry."""

    mcp_clients: list[McpClient] = field(default_factory=list)
    skill_registry: SkillRegistry | None = None
    skill_buffer: SkillContextBuffer | None = None
    skill_index: tuple[str, ...] = ()
    resource_index: tuple[str, ...] = ()
    installed_tools: tuple[str, ...] = ()

    def close(self) -> None:
        for client in reversed(self.mcp_clients):
            try:
                client.transport.close()
            except Exception:
                # Runtime shutdown is best-effort; the original operation has
                # already completed and close failures must not hide its result.
                pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


class _VisibleTextParser(HTMLParser):
    IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_stack: list[str] = []
        self.parts: list[str] = []

    @property
    def incomplete_ignored_tags(self) -> tuple[str, ...]:
        return tuple(self._ignored_stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in self.IGNORED_TAGS:
            self._ignored_stack.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized not in self.IGNORED_TAGS or not self._ignored_stack:
            return
        # HTML in the wild can be imperfect. Close through the matching ignored
        # element rather than decrementing an anonymous depth counter, while
        # retaining any truly unclosed element for the end-of-input check.
        if normalized in self._ignored_stack:
            while self._ignored_stack:
                opened = self._ignored_stack.pop()
                if opened == normalized:
                    break

    def handle_data(self, data: str) -> None:
        if not self._ignored_stack and data.strip():
            self.parts.append(data.strip())


class SafeWebFetcher:
    """Allow-list web fetcher that validates DNS and every redirect hop."""

    MAX_REDIRECTS = 5

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        max_response_bytes: int = 1_000_000,
        timeout_seconds: float = 20.0,
    ) -> None:
        hosts = tuple(
            dict.fromkeys(_normalize_host(value) for value in allowed_hosts if value)
        )
        if not hosts:
            raise ExtensionConfigurationError(
                "web access requires at least one explicit allowed host"
            )
        if max_response_bytes < 1:
            raise ExtensionConfigurationError("web max_response_bytes must be positive")
        if timeout_seconds <= 0:
            raise ExtensionConfigurationError("web timeout_seconds must be positive")
        self.allowed_hosts = hosts
        self.max_response_bytes = int(max_response_bytes)
        self.timeout_seconds = float(timeout_seconds)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def fetch(self, url: str) -> str:
        current = str(url).strip()
        for redirect_index in range(self.MAX_REDIRECTS + 1):
            self._validate_url(current)
            request = urllib.request.Request(
                current,
                headers={
                    "User-Agent": "PaiCLI/1.0 (+local-agent)",
                    "Accept": "text/plain,text/html,application/json;q=0.9,*/*;q=0.1",
                },
                method="GET",
            )
            try:
                response = self._opener.open(request, timeout=self.timeout_seconds)
            except urllib.error.HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location", "").strip()
                    if not location:
                        raise ValueError("web redirect has no Location header") from exc
                    if redirect_index >= self.MAX_REDIRECTS:
                        raise ValueError("web redirect limit exceeded") from exc
                    current = urllib.parse.urljoin(current, location)
                    continue
                raise ValueError(f"web request failed with HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise ValueError(f"web request failed: {exc.reason}") from exc

            with response:
                final_url = response.geturl()
                self._validate_url(final_url)
                content_type = response.headers.get_content_type().lower()
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise ValueError(
                        f"web response exceeds {self.max_response_bytes} bytes"
                    )
            text = body.decode(charset, errors="replace")
            return _render_web_body(text, content_type, final_url)
        raise ValueError("web redirect limit exceeded")

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("web URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("web URL credentials are not allowed")
        host = _normalize_host(parsed.hostname or "")
        if not host or not any(_host_matches(host, item) for item in self.allowed_hosts):
            raise ValueError(f"web host is not allowed: {host or '(missing)'}")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ValueError(f"web host cannot be resolved: {host}") from exc
        if not addresses:
            raise ValueError(f"web host has no address: {host}")
        for value in addresses:
            address = ipaddress.ip_address(value)
            if not address.is_global:
                raise ValueError(
                    f"web host resolves to a non-public address: {host} -> {address}"
                )


def load_extension_config(
    path: str | Path,
    project_root: str | Path,
) -> ExtensionConfig:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ExtensionConfigurationError(f"extension config does not exist: {source}")
    try:
        root = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExtensionConfigurationError(
            f"invalid extension config JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(root, dict):
        raise ExtensionConfigurationError("extension config root must be an object")

    base = Path(project_root).resolve()
    skills_raw = root.get("skills", {})
    if not isinstance(skills_raw, dict):
        raise ExtensionConfigurationError("extensions.skills must be an object")
    skill_roots = tuple(
        _resolve_config_path(value, base, source.parent).as_posix()
        for value in _string_list(skills_raw.get("roots", []), "skills.roots")
    )

    mcp_root = root.get("mcp", {})
    if not isinstance(mcp_root, dict):
        raise ExtensionConfigurationError("extensions.mcp must be an object")
    servers_raw = mcp_root.get("servers", [])
    if not isinstance(servers_raw, list):
        raise ExtensionConfigurationError("mcp.servers must be an array")
    servers: list[McpServerConfig] = []
    names: set[str] = set()
    for index, item in enumerate(servers_raw):
        if not isinstance(item, dict):
            raise ExtensionConfigurationError(f"mcp.servers[{index}] must be an object")
        name = str(item.get("name", "")).strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ExtensionConfigurationError(
                f"mcp.servers[{index}].name must be a safe non-empty identifier"
            )
        if name in names:
            raise ExtensionConfigurationError(f"duplicate MCP server name: {name}")
        names.add(name)
        transport = str(item.get("transport", "stdio")).strip().lower()
        timeout = float(item.get("timeout_seconds", 30.0))
        if timeout <= 0:
            raise ExtensionConfigurationError("MCP timeout_seconds must be positive")
        if transport == "stdio":
            command = tuple(
                _string_list(item.get("command", []), f"mcp.servers[{index}].command")
            )
            if not command:
                raise ExtensionConfigurationError(
                    f"mcp.servers[{index}] stdio transport requires command"
                )
            servers.append(McpServerConfig(name, transport, command=command, timeout_seconds=timeout))
        elif transport == "http":
            url = str(item.get("url", "")).strip()
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ExtensionConfigurationError(
                    f"mcp.servers[{index}] HTTP transport requires an http(s) URL"
                )
            headers_raw = item.get("headers", {})
            if not isinstance(headers_raw, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in headers_raw.items()
            ):
                raise ExtensionConfigurationError(
                    f"mcp.servers[{index}].headers must be a string map"
                )
            servers.append(
                McpServerConfig(
                    name,
                    transport,
                    url=url,
                    headers=dict(headers_raw),
                    timeout_seconds=timeout,
                )
            )
        else:
            raise ExtensionConfigurationError(
                f"mcp.servers[{index}].transport must be stdio or http"
            )

    web_raw = root.get("web", {})
    if not isinstance(web_raw, dict):
        raise ExtensionConfigurationError("extensions.web must be an object")
    web = WebExtensionConfig(
        enabled=bool(web_raw.get("enabled", False)),
        allowed_hosts=tuple(
            _string_list(web_raw.get("allowed_hosts", []), "web.allowed_hosts")
        ),
        search_endpoint=str(web_raw.get("search_endpoint", "")).strip(),
        max_response_bytes=int(web_raw.get("max_response_bytes", 1_000_000)),
        timeout_seconds=float(web_raw.get("timeout_seconds", 20.0)),
    )
    return ExtensionConfig(SkillExtensionConfig(skill_roots), tuple(servers), web)


def install_extensions(
    registry: ToolRegistry,
    project_root: str | Path,
    config: ExtensionConfig,
) -> InstalledExtensions:
    before = set(registry.names())
    installed = InstalledExtensions()

    if config.skills.roots:
        skills = SkillRegistry(config.skills.roots)
        discovered = skills.discover()
        installed.skill_registry = skills
        installed.skill_buffer = register_skill_tool(registry, skills)
        installed.skill_index = tuple(
            f"- {item.name}: {item.description}" for item in sorted(discovered, key=lambda value: value.name)
        )

    for server in config.mcp_servers:
        transport = (
            StdioTransport(list(server.command), timeout_seconds=server.timeout_seconds)
            if server.transport == "stdio"
            else StreamableHttpTransport(
                server.url,
                headers=dict(server.headers),
                timeout_seconds=server.timeout_seconds,
            )
        )
        client = McpClient(server.name, transport)
        try:
            client.initialize()
            client.register_tools(registry)
        except Exception:
            transport.close()
            installed.close()
            raise
        installed.mcp_clients.append(client)
        installed.resource_index += (
            f"- MCP server {server.name}: {client.server_info or {'status': 'ready'}}",
        )

    if config.web.enabled:
        fetcher = SafeWebFetcher(
            config.web.allowed_hosts,
            max_response_bytes=config.web.max_response_bytes,
            timeout_seconds=config.web.timeout_seconds,
        )
        registry.register(
            ToolSpec(
                "web_fetch",
                "Fetch text from an explicitly allow-listed public HTTP(S) URL.",
                registry.object_schema(
                    {
                        "url": {"type": "string", "minLength": 1, "maxLength": 2_000}
                    },
                    required=["url"],
                ),
                lambda arguments: fetcher.fetch(str(arguments["url"])),
                risk=ToolRisk.MEDIUM,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.PARALLEL,
            )
        )
        if config.web.search_endpoint:
            endpoint = config.web.search_endpoint

            def web_search(arguments: dict[str, Any]) -> str:
                query = str(arguments["query"]).strip()
                if not query:
                    raise ValueError("web search query cannot be empty")
                separator = "&" if "?" in endpoint else "?"
                url = endpoint + separator + urllib.parse.urlencode({"q": query})
                return fetcher.fetch(url)

            registry.register(
                ToolSpec(
                    "web_search",
                    "Search through the configured allow-listed HTTP endpoint.",
                    registry.object_schema(
                        {
                            "query": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500,
                            }
                        },
                        required=["query"],
                    ),
                    web_search,
                    risk=ToolRisk.MEDIUM,
                    side_effect=ToolSideEffect.READ_ONLY,
                    concurrency=ConcurrencyPolicy.PARALLEL,
                )
            )

    installed.installed_tools = tuple(
        name for name in registry.names() if name not in before
    )
    return installed


def _resolve_config_path(value: str, project_root: Path, config_parent: Path) -> Path:
    text = str(value).strip()
    if not text:
        raise ExtensionConfigurationError("configured path cannot be empty")
    if text.startswith("~/"):
        return Path(text).expanduser().resolve()
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate.resolve()
    # Project-relative roots are the predictable default; a leading ./ in a
    # config next to the project still resolves against the project root.
    return (project_root / candidate).resolve()


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExtensionConfigurationError(f"{name} must be a string array")
    return [item for item in value if item.strip()]


def _normalize_host(value: str) -> str:
    return str(value).strip().rstrip(".").lower()


def _host_matches(host: str, allowed: str) -> bool:
    normalized = _normalize_host(allowed)
    if normalized.startswith("*."):
        suffix = normalized[2:]
        return host.endswith("." + suffix) and host != suffix
    return host == normalized


def _render_web_body(text: str, content_type: str, url: str) -> str:
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleTextParser()
        parser.feed(text)
        parser.close()
        if parser.incomplete_ignored_tags:
            tags = ", ".join(f"<{tag}>" for tag in parser.incomplete_ignored_tags)
            raise ValueError(
                "web HTML extraction is incomplete because the document ended "
                f"inside ignored element(s): {tags}"
            )
        body = "\n".join(parser.parts)
    elif content_type == "application/json":
        try:
            body = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            body = text
    else:
        body = text
    compact = re.sub(r"[ \t]+", " ", html.unescape(body)).strip()
    return f"URL: {url}\n\n{compact}"


__all__ = [
    "ExtensionConfig",
    "ExtensionConfigurationError",
    "InstalledExtensions",
    "McpServerConfig",
    "SafeWebFetcher",
    "SkillExtensionConfig",
    "WebExtensionConfig",
    "install_extensions",
    "load_extension_config",
]
