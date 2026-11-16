"""Exercise the headless-browser guide transport over a fake page renderer."""

from __future__ import annotations

from types import TracebackType
from typing import Self

import pytest
from playwright.sync_api import Error as PlaywrightError

from course_explorer.config import KoriSettings
from course_explorer.ingestion.guide import (
    GuideContractError,
    GuideRequestError,
    GuideServerError,
    GuideSource,
)
from course_explorer.ingestion.guide_browser import (
    GuideBrowserTransport,
    RenderedGuidePage,
)
from course_explorer.instance import UNIVERSITIES, GuideSourceMetadata
from tests.support import settings_factory

BLOCK_KEY = "metropolia"
PATH = "/88094/fi/29/70419/3349"
BODY = '<html><body><table class="opas_rakenne"></table></body></html>'


def guide_block() -> GuideSourceMetadata:
    """Return the mapped browser guide block."""
    block = UNIVERSITIES[BLOCK_KEY].guide
    assert block is not None
    return block


class FakeRenderer:
    """One page renderer that answers each call with its scripted page or scripted error and
    records each requested address.
    """

    def __init__(self, page: RenderedGuidePage | Exception) -> None:
        self._page = page
        self.urls: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def render(self, url: str) -> RenderedGuidePage:
        self.urls.append(url)
        if isinstance(self._page, Exception):
            raise self._page
        return self._page


def test_guide_browser_transport_returns_the_rendered_page() -> None:
    """One fetched page becomes the decoded HTML and the exact UTF-8 response bytes."""
    settings = settings_factory(KoriSettings, INSTANCE_KEY=BLOCK_KEY)
    block = guide_block()
    renderer = FakeRenderer(RenderedGuidePage(200, block.guide_base_url + PATH, BODY))
    with GuideBrowserTransport(settings, block, renderer=renderer) as transport:
        with GuideSource(settings, block, page_transport=transport) as source:
            response = source.get_page(PATH)

    assert response.value == BODY
    assert response.captured.request_path == PATH
    assert response.captured.body == BODY.encode("utf-8")


def test_guide_browser_transport_refuses_a_redirected_page() -> None:
    """A rendered final path other than the requested path raises a contract error."""
    settings = settings_factory(KoriSettings, INSTANCE_KEY=BLOCK_KEY)
    block = guide_block()
    renderer = FakeRenderer(
        RenderedGuidePage(200, block.guide_base_url + "/88094/fi/29/70419/3350", BODY)
    )
    with GuideBrowserTransport(settings, block, renderer=renderer) as transport:
        with GuideSource(settings, block, page_transport=transport) as source:
            with pytest.raises(GuideContractError):
                source.get_page(PATH)


def test_guide_browser_transport_maps_a_browser_error_to_the_shared_retry() -> None:
    """A browser error consumes the shared attempt budget as one request error."""
    settings = settings_factory(
        KoriSettings,
        INSTANCE_KEY=BLOCK_KEY,
        KORI_MAX_ATTEMPTS=3,
        KORI_RETRY_BACKOFF_SECONDS=0,
    )
    block = guide_block()
    renderer = FakeRenderer(PlaywrightError("navigation failed"))

    with GuideBrowserTransport(settings, block, renderer=renderer) as transport:
        with GuideSource(settings, block, page_transport=transport, sleep=lambda _: None) as source:
            with pytest.raises(GuideRequestError) as raised:
                source.get_page(PATH)

    assert not isinstance(raised.value, GuideServerError)
    assert renderer.urls == [
        block.guide_base_url.rstrip("/") + PATH,
        block.guide_base_url.rstrip("/") + PATH,
        block.guide_base_url.rstrip("/") + PATH,
    ]
