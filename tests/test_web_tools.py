from __future__ import annotations

import unittest

from paicli.web_tools import HtmlExtractor, NetworkPolicy, WebFetcher


class FakeResponse:
    """模拟 urlopen 返回的上下文管理器，使测试完全不访问网络。"""

    def __init__(self, body: bytes, url: str = "https://example.com/page") -> None:
        self.body = body
        self.url = url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.body

    def geturl(self) -> str:
        return self.url


class WebToolsTest(unittest.TestCase):
    def test_blocks_private_and_non_http_urls(self) -> None:
        """验证本地文件协议和回环 IP 都在网络请求前被拒绝。"""

        policy = NetworkPolicy()
        # subTest 让两个 URL 分别显示失败原因，同时复用一段断言。
        for url in ("file:///etc/passwd", "http://127.0.0.1/admin"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                policy.validate(url)

    def test_extracts_readable_content(self) -> None:
        """验证 HTML 提取器保留标题/段落，过滤 style 和 script。"""

        # Arrange + Act：用一段包含可见与不可见内容的最小 HTML。
        text = HtmlExtractor.extract(
            "<html><style>hidden</style><body><h1>Title</h1>"
            "<script>bad()</script><p>Hello world</p></body></html>"
        )

        # Assert：只剩可读文本，并合并多余空白。
        self.assertEqual("Title Hello world", text)

    def test_fetcher_enforces_policy_and_extracts_title(self) -> None:
        """验证 WebFetcher 经过策略后可从假 HTTP 响应提取标题和正文。"""

        # Arrange：注入 FakeResponse，避免真实网络使测试变慢或不稳定。
        fetcher = WebFetcher(
            opener=lambda *_args, **_kwargs: FakeResponse(
                b"<title>Docs</title><main>Useful content</main>"
            )
        )

        # Act：example.com 通过默认网络策略。
        result = fetcher.fetch("https://example.com/page")

        # Assert：<title> 和主体文本都被提取。
        self.assertEqual("Docs", result.title)
        self.assertIn("Useful content", result.text)


if __name__ == "__main__":
    unittest.main()
