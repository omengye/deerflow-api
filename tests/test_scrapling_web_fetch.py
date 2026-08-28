import sys

import pytest

import deerflow.community.scrapling.tools as web_fetch_module


def test_scrapling_import_failure_is_returned_for_direct_fetch_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", None)

    result = web_fetch_module._scrapling_fetch(
        "https://example.com",
        timeout=1,
        https_proxy=None,
    )

    assert result == "Error: Scrapling fetch failed: ModuleNotFoundError"


@pytest.mark.asyncio
async def test_web_fetch_uses_direct_fallback_when_scrapling_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", None)
    monkeypatch.setattr(web_fetch_module, "_resolve_fetch_timeout", lambda: 7)
    monkeypatch.setattr(web_fetch_module, "get_tool_https_proxy", lambda _tool: None)

    async def fake_fetch_direct(
        url: str,
        timeout: int,
        https_proxy: str | None = None,
    ) -> str:
        assert url == "https://example.com"
        assert timeout == 7
        assert https_proxy is None
        return "<html><body>portable fallback</body></html>"

    class FakeArticle:
        def to_markdown(self) -> str:
            return "portable fallback"

    def fake_extract_article(html: str) -> FakeArticle:
        assert "portable fallback" in html
        return FakeArticle()

    monkeypatch.setattr(web_fetch_module, "_fetch_direct", fake_fetch_direct)
    monkeypatch.setattr(
        web_fetch_module.readability_extractor,
        "extract_article",
        fake_extract_article,
    )

    result = await web_fetch_module._web_fetch_impl("https://example.com")

    assert result == "portable fallback"
