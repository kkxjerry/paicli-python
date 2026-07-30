from __future__ import annotations

import unittest

from paicli.web_tools import HtmlExtractor, NetworkPolicy, WebFetcher


class FakeResponse:
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
        policy = NetworkPolicy()
        for url in ("file:///etc/passwd", "http://127.0.0.1/admin"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                policy.validate(url)

    def test_extracts_readable_content(self) -> None:
        text = HtmlExtractor.extract(
            "<html><style>hidden</style><body><h1>Title</h1>"
            "<script>bad()</script><p>Hello world</p></body></html>"
        )

        self.assertEqual("Title Hello world", text)

    def test_fetcher_enforces_policy_and_extracts_title(self) -> None:
        fetcher = WebFetcher(
            opener=lambda *_args, **_kwargs: FakeResponse(
                b"<title>Docs</title><main>Useful content</main>"
            )
        )

        result = fetcher.fetch("https://example.com/page")

        self.assertEqual("Docs", result.title)
        self.assertIn("Useful content", result.text)


if __name__ == "__main__":
    unittest.main()
