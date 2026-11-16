"""One ephemeral headless Chromium transport that renders one public guide page into the
HTTP-shaped response the guide client retries."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urljoin

import httpx

from course_explorer.config import KoriSettings
from course_explorer.ingestion.guide import GuideContractError, guide_path
from course_explorer.instance import GuideSourceMetadata


@dataclass(frozen=True)
class RenderedGuidePage:
    """One rendered guide page: its status, final address and rendered HTML."""

    status: int
    final_url: str
    body: str


class GuidePageRenderer(Protocol):
    """Render one guide page at a time inside one temporary browser context."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def render(self, url: str) -> RenderedGuidePage: ...


class _PlaywrightGuideRenderer:
    """Render one page in an ephemeral Chromium context with the Finnish interface locale."""

    def __init__(self, settings: KoriSettings) -> None:
        self._settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> Self:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(locale="fi-FI")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the context, the browser and the driver in that order."""
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def render(self, url: str) -> RenderedGuidePage:
        """Render one guide page and return its status, final address and HTML."""
        assert self._context is not None
        page = None
        try:
            page = self._context.new_page()
            response = page.goto(
                url,
                wait_until="networkidle",
                timeout=int(self._settings.kori_request_timeout_seconds * 1000),
            )
            if response is None:
                raise GuideContractError("guide page navigation returned no response")
            return RenderedGuidePage(response.status, page.url, page.content())
        finally:
            if page is not None:
                page.close()


class GuideBrowserTransport:
    """Render one guide page at a time in one ephemeral headless Chromium context."""

    def __init__(
        self,
        settings: KoriSettings,
        block: GuideSourceMetadata,
        *,
        renderer: GuidePageRenderer | None = None,
    ) -> None:
        self._settings = settings
        self._block = block
        self._renderer = _PlaywrightGuideRenderer(settings) if renderer is None else renderer

    def __enter__(self) -> Self:
        self._renderer.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._renderer.__exit__(exc_type, exc_value, traceback)

    def close(self) -> None:
        """Close the renderer's context, browser and driver."""
        self._renderer.__exit__(None, None, None)

    def get(self, path: str) -> httpx.Response:
        """Return one rendered same-origin guide page as an HTTP-shaped response."""
        from playwright.sync_api import Error as PlaywrightError

        url = urljoin(self._block.guide_base_url.rstrip("/") + "/", path.lstrip("/"))
        guide_path(self._block.guide_base_url, url)
        try:
            rendered = self._renderer.render(url)
        except PlaywrightError as error:
            raise httpx.TransportError(type(error).__name__) from error
        if guide_path(self._block.guide_base_url, rendered.final_url) != path:
            raise GuideContractError("guide page redirected")
        return httpx.Response(
            rendered.status,
            content=rendered.body.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )
