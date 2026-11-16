"""Anonymous public Peppi guide client seam and the pure HTML link walk.

This module owns the guide request contract and the one page-row walk shared by the capture
driver and the parser. It issues the requests whose responses the capture driver records, and
the family parser converts the records.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Protocol, Self
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from course_explorer.config import KoriSettings
from course_explorer.http_retry import request_json_with_retries
from course_explorer.ingestion.source import CapturedResponse, SourceResponse
from course_explorer.instance import GuideSourceMetadata

_ASSET_SUFFIXES = (
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".json",
    ".xml",
    ".map",
)
# Paths below a guide root that address one course, realization, attachment or offering rather
# than a programme or category page.
_EXCLUDED_PATH = re.compile(
    r"/(?:course|opintojakso|kurssi|learningunit|realization|attachment|offerings)/",
    re.IGNORECASE,
)
_YEAR_HEADING = re.compile(
    r"^(?:Lukuvuosi|Läsår|Academic\s+year|Studieår)\s+(\d{4})\s*[-\u2013]\s*(\d{4})",
    re.IGNORECASE,
)
_PROGRAMME_CODE = re.compile(
    r"(?:Opetussuunnitelman\s+tunnus|Opetussuunitelman\s+tunnus|Kod)\s*"
    r"(?:\(([^()]+)\)|:\s*([^\s()]+))",
    re.IGNORECASE,
)
_CREDIT_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")
_NODE_TYPE = re.compile(r"[A-Z][A-Z0-9_]*")


# A guide site can reject a request without a client identity, so every guide request carries this
# fixed user agent.
_USER_AGENT = "course-explorer-guide/1"


class GuideRequestError(RuntimeError):
    """A public Peppi guide request failed after its configured retry budget."""


class GuideServerError(GuideRequestError):
    """A guide page kept answering HTTP 500 after the configured retry budget."""


class GuideContractError(ValueError):
    """A public Peppi guide page violates the source contract."""


class GuidePageTransport(Protocol):
    """Return one HTTP-shaped response for one guide page path."""

    def get(self, path: str) -> httpx.Response: ...


@dataclass(frozen=True)
class GuideCourseLink:
    """One course row of a structure page: its code, guide path and listing facts."""

    code: str
    path: str
    url: str
    name: str
    credits_min: Decimal | None
    credits_max: Decimal | None


@dataclass(frozen=True)
class GuideStructureLink:
    """One structure page below a programme year group."""

    period_id: str
    programme_code: str | None
    programme_path: str
    path: str


@dataclass(frozen=True)
class _GuideRow:
    """One structure-table row with the facts the capture and parser share."""

    level: int
    node_type: str
    code: str
    name: str
    optionality: str | None
    credits: str
    href: str | None


def _origin(parsed: object) -> tuple[str, str | None, int | None]:
    return (parsed.scheme, parsed.hostname, parsed.port)


def guide_path(base_url: str, url: str) -> str:
    """Return one absolute same-origin guide URL as a query-free path.

    The configured origin owns every guide request, so a cross-origin, credential-bearing or
    query-carrying URL raises :class:`GuideContractError` instead of being fetched.
    """
    base = urlparse(base_url)
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise GuideContractError("guide page URL must not carry credentials")
    if parsed.query:
        raise GuideContractError("guide page URL must not carry a query")
    if _origin(parsed) != _origin(base):
        raise GuideContractError("guide page URL is outside the configured guide origin")
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise GuideContractError("guide page path must begin with /")
    return path


def _validated_anchor_path(anchor: Tag, *, base_url: str, primary_language: str) -> str | None:
    """Return one anchor's same-origin query-free page path, or ``None`` when it fails a rule.

    The rules cover the href, the URL scheme, the configured origin, credentials, a query, an asset
    target and a foreign content-language segment. A caller applies only its own extra shape
    filters to the returned path.
    """
    href = anchor.get("href")
    if not isinstance(href, str):
        return None
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    url = urljoin(base_url.rstrip("/") + "/", href)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if _origin(parsed) != _origin(urlparse(base_url)):
        return None
    if parsed.username is not None or parsed.password is not None or parsed.query:
        return None
    path = guide_path(base_url, url)
    if _is_asset(path) or _EXCLUDED_PATH.search(path):
        return None
    if _has_foreign_language_segment(path, primary_language):
        return None
    return path


def _anchor_paths(html: str, *, base_url: str, primary_language: str) -> Iterator[str]:
    """Yield the validated same-origin page paths of one page, in document order."""
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        path = _validated_anchor_path(anchor, base_url=base_url, primary_language=primary_language)
        if path is not None:
            yield path


def _is_asset(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(suffix) for suffix in _ASSET_SUFFIXES)


def _has_foreign_language_segment(path: str, primary_language: str) -> bool:
    """Return whether the path names a guide content language other than the primary one."""
    for segment in path.split("/"):
        if segment in {"fi", "en", "sv"} and segment != primary_language:
            return True
    return False


def _split_segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def discover_programme_paths(
    root_html: str, *, base_url: str, primary_language: str
) -> tuple[str, ...]:
    """Return the leaf programme and category page paths linked from one guide root."""
    candidates: set[str] = set()
    for path in _anchor_paths(root_html, base_url=base_url, primary_language=primary_language):
        segments = _split_segments(path)
        if not 1 <= len(segments) <= 6 or not re.search(r"\d", path):
            continue
        candidates.add(path)
    kept = sorted(
        path
        for path in candidates
        if not any(other != path and other.startswith(path + "/") for other in candidates)
    )
    if not kept:
        raise GuideContractError("guide root links no programme page")
    return tuple(kept)


def _year_match(tag: object) -> re.Match[str] | None:
    if not isinstance(tag, Tag) or tag.name not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return None
    return _YEAR_HEADING.match(tag.get_text(" ", strip=True))


def _text_until(start: Tag, stop: Tag) -> str:
    """Return the visible page text between one heading and one later element, in order."""
    parts: list[str] = []
    for element in start.next_elements:
        if element is stop:
            break
        if isinstance(element, str) and element.strip():
            parent = element.parent
            if isinstance(parent, Tag) and (
                parent.name in {"script", "style", "noscript"}
                or parent.find_parent(["script", "style", "noscript"]) is not None
            ):
                continue
            parts.append(element.strip())
    return " ".join(parts)


def published_programme_code(text: str) -> str | None:
    """Return the governing published programme code that follows its label, or None.

    A year group that lists several programmes publishes one label before each programme's
    structures, so the last label before the anchor governs it. A page that publishes one code
    returns that code.
    """
    matches = list(_PROGRAMME_CODE.finditer(text))
    if not matches:
        return None
    value = (matches[-1].group(1) or matches[-1].group(2) or "").strip()
    return value or None


def programme_path_id(programme_path: str) -> str:
    """Return the last non-empty segment of one programme page path."""
    segments = _split_segments(programme_path)
    return segments[-1] if segments else programme_path


def _block_anchors(heading: Tag) -> Iterator[Tag]:
    """Yield the anchors of one year group, stopping before the next year heading."""
    for element in heading.next_elements:
        if element is heading:
            continue
        if _year_match(element) is not None:
            return
        if isinstance(element, Tag) and element.name == "a" and element.get("href"):
            yield element


def structure_blocks(
    programme_html: str, *, base_url: str, programme_path: str, primary_language: str
) -> tuple[GuideStructureLink, ...]:
    """Return one structure link for every linked structure page.

    A year group links its structure pages at the shallowest depth it publishes below the
    programme path, so a deeper module page of the same group is not fetched as a structure. A
    year group that links no structure page adds none. Each kept structure page carries the year
    group's published label and the programme page path. A group that publishes no label leaves the
    programme code unresolved until the parser reads the structure page's own published code or the
    programme page's last segment.
    """
    soup = BeautifulSoup(programme_html, "html.parser")
    headings = [
        heading for heading in soup.find_all(re.compile(r"^h[1-6]$")) if _year_match(heading)
    ]
    blocks: list[GuideStructureLink] = []
    for heading in headings:
        match = _year_match(heading)
        assert match is not None
        candidates: list[tuple[int, GuideStructureLink]] = []
        for anchor in _block_anchors(heading):
            path = _validated_anchor_path(
                anchor, base_url=base_url, primary_language=primary_language
            )
            if path is None or not path.startswith(programme_path + "/"):
                continue
            remainder = path[len(programme_path) + 1 :]
            segments = _split_segments(remainder)
            if not segments:
                continue
            code = published_programme_code(_text_until(heading, anchor))
            candidates.append(
                (
                    len(segments),
                    GuideStructureLink(
                        period_id=f"{match.group(1)}-{match.group(2)}",
                        programme_code=code,
                        programme_path=programme_path,
                        path=path,
                    ),
                )
            )
        if not candidates:
            continue
        depth = min(candidate_depth for candidate_depth, _link in candidates)
        blocks.extend(link for candidate_depth, link in candidates if candidate_depth == depth)
    return tuple(blocks)


def discover_structure_links(
    programme_html: str, *, base_url: str, programme_path: str, primary_language: str
) -> tuple[GuideStructureLink, ...]:
    """Return the structure pages of one programme page's year groups, in document order."""
    return structure_blocks(
        programme_html,
        base_url=base_url,
        programme_path=programme_path,
        primary_language=primary_language,
    )


def credit_bounds(text: str) -> tuple[Decimal | None, Decimal | None]:
    """Return the minimum and maximum credit values of one cell, or two nulls."""
    match = _CREDIT_NUMBER.search(text)
    if match is None:
        return None, None
    values = [match.group(1)]
    tail = text[match.end() :]
    range_match = re.match(r"\s*[-\u2013]\s*(\d+(?:[.,]\d+)?)", tail)
    if range_match is not None:
        values.append(range_match.group(1))
    try:
        numbers = [Decimal(value.replace(",", ".")) for value in values]
    except InvalidOperation as exc:
        raise GuideContractError("guide credit value is not a decimal number") from exc
    minimum = min(numbers)
    maximum = max(numbers)
    return minimum, maximum


def structure_rows(html: str) -> tuple[_GuideRow, ...]:
    """Return the ordered curriculum structure rows of one page's published structure table.

    The timing and classification tables describe display order and supply no curriculum row.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", class_="opas_rakenne")
    if not tables:
        raise GuideContractError("structure page has no published structure table")
    if len(tables) > 1:
        raise GuideContractError("structure page has more than one published structure table")
    rows: list[_GuideRow] = []
    for row in tables[0].find_all("tr"):
        classes = [value for value in (row.get("class") or []) if value]
        if not classes or not classes[0].startswith("level-"):
            continue
        try:
            level = int(classes[0].removeprefix("level-"))
        except ValueError as exc:
            raise GuideContractError("structure row level is not a number") from exc
        node_type = next((value for value in classes[1:] if _NODE_TYPE.fullmatch(value)), None)
        if node_type is None:
            raise GuideContractError("structure row has no node type")
        name_cell = row.find("td", class_="nimi")
        if not isinstance(name_cell, Tag):
            raise GuideContractError("structure row has no name cell")
        optionality_tag = name_cell.find(class_="optionality")
        optionality = (
            optionality_tag.get_text(" ", strip=True) if isinstance(optionality_tag, Tag) else None
        )
        name_copy = BeautifulSoup(str(name_cell), "html.parser")
        for removable in name_copy.find_all(["button"]):
            removable.decompose()
        for removable in name_copy.find_all(class_="optionality"):
            removable.decompose()
        name = name_copy.get_text(" ", strip=True)
        code_cell = row.find("td", class_="koodi")
        code = code_cell.get_text(" ", strip=True) if isinstance(code_cell, Tag) else ""
        credit_cell = row.find("td", class_="laajuus")
        credits = credit_cell.get_text(" ", strip=True) if isinstance(credit_cell, Tag) else ""
        anchor = name_cell.find("a", href=True)
        rows.append(
            _GuideRow(
                level=level,
                node_type=node_type,
                code=code,
                name=name,
                optionality=optionality,
                credits=credits,
                href=anchor["href"].strip() if isinstance(anchor, Tag) else None,
            )
        )
    return tuple(rows)


def discover_course_links(structure_html: str, *, base_url: str) -> tuple[GuideCourseLink, ...]:
    """Return one link per distinct course code listed by one structure page, sorted by code."""
    links: dict[str, GuideCourseLink] = {}
    for row in structure_rows(structure_html):
        if row.node_type != "COURSE_UNIT":
            continue
        if not row.code or row.href is None or not row.href:
            raise GuideContractError("course row has no code or link")
        url = urljoin(base_url.rstrip("/") + "/", row.href)
        path = guide_path(base_url, url)
        credits_min, credits_max = credit_bounds(row.credits)
        link = GuideCourseLink(
            code=row.code,
            path=path,
            url=url,
            name=row.name,
            credits_min=credits_min,
            credits_max=credits_max,
        )
        previous = links.get(row.code)
        if previous is not None:
            if (previous.name, previous.credits_min, previous.credits_max) != (
                link.name,
                link.credits_min,
                link.credits_max,
            ):
                raise GuideContractError("one course code carries two listing facts")
            continue
        links[row.code] = link
    return tuple(links[code] for code in sorted(links))


def _guide_final_status_error(status: int) -> type[RuntimeError]:
    """Return the server error for a terminal 500 and the request error for every other status."""
    return GuideServerError if status == 500 else GuideRequestError


def _decode_html(response: httpx.Response) -> str:
    """Require one HTML response and decode its UTF-8 body or raise a contract error."""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/html":
        raise GuideContractError("guide page response must be text/html")
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuideContractError("guide page response must be UTF-8") from exc


class GuideSource:
    """One synchronous public Peppi guide client with spaced, bounded-retry requests."""

    def __init__(
        self,
        settings: KoriSettings,
        block: GuideSourceMetadata,
        *,
        transport: httpx.BaseTransport | None = None,
        page_transport: GuidePageTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        before_request: Callable[[], bool] | None = None,
    ) -> None:
        expected_variant = block.fetch_variant
        if expected_variant == "headless_browser" and page_transport is None:
            raise ValueError("headless guide fetch variant requires a page transport")
        if expected_variant == "plain_http" and page_transport is not None:
            raise ValueError("plain HTTP guide fetch variant takes no page transport")
        self._settings = settings
        self._sleep = sleep
        self._monotonic = monotonic
        self._before_request = before_request
        self._last_request_start = float("-inf")
        self._page_transport = page_transport
        self._client: httpx.Client | None = None
        if expected_variant == "plain_http":
            self._client = httpx.Client(
                base_url=block.guide_base_url.rstrip("/") + "/",
                timeout=settings.kori_request_timeout_seconds,
                transport=transport,
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
            )

    def close(self) -> None:
        """Close the shared HTTP client and release its connections."""
        if self._client is not None:
            self._client.close()

    @property
    def primary_language(self) -> str:
        """Return the configured guide's first official language."""
        return self._settings.university.official_languages[0]

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _get(self, path: str) -> httpx.Response:
        """Start one request at least the configured interval after the previous request start."""
        wait = (
            self._last_request_start
            + self._settings.kori_request_interval_seconds
            - self._monotonic()
        )
        if wait > 0:
            self._sleep(wait)
        self._last_request_start = self._monotonic()
        if self._page_transport is not None:
            return self._page_transport.get(path)
        assert self._client is not None
        return self._client.get(path)

    def get_page(self, path: str) -> SourceResponse[str]:
        """Fetch one required guide page and return its decoded HTML with its exact bytes.

        A page that answers HTTP 500 after the attempt budget raises :class:`GuideServerError`.
        Every other status raises :class:`GuideRequestError`.
        """
        if not path.startswith("/"):
            raise GuideContractError("guide request path must begin with /")
        response, text = request_json_with_retries(
            request=lambda: self._get(path),
            validate=lambda value: (value, _decode_html(value)),
            max_attempts=self._settings.kori_max_attempts,
            retry_backoff_seconds=self._settings.kori_retry_backoff_seconds,
            sleep=self._sleep,
            error_prefix=f"GET {path}",
            request_error=GuideRequestError,
            contract_error=GuideContractError,
            retry_contract_errors=False,
            before_request=self._before_request,
            check_initial=False,
            # The guide maps a terminal 500 to its server-error class.
            final_status_error=_guide_final_status_error,
        )
        return SourceResponse(
            captured=CapturedResponse(
                request_path=path,
                request_params=(),
                body=response.content,
            ),
            value=text,
        )
