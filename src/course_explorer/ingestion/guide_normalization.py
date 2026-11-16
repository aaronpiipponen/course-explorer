"""Peppi guide source models, evidence decoding and normalized-record conversion.

This module owns the family's extracted records, its exact section-label and optionality
vocabulary and its typed module-rule conversion. It reads one published ``opas_rakenne`` table,
whose header-only form contributes no record, scopes a module's identity by its programme root and
every parent module's published node type and code or name, takes the module's own identity from its
published official code or its published name, adds the 1-based ordinal of a code-less name that
repeats among its parent's module children, and stores a null code when the source publishes none.
It decodes captured guide pages into the shared neutral normalized course and module records, and
the shared normalization service owns deduplication, period expansion, membership filtering and
closure validation.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, field_validator

from course_explorer.enums import (
    ModuleRuleType,
    ModuleType,
    OfficialTextField,
    SourceObjectType,
)
from course_explorer.hashing import json_sha256
from course_explorer.ingestion.guide import (
    GuideContractError,
    _GuideRow,
    credit_bounds,
    discover_programme_paths,
    guide_path,
    programme_path_id,
    published_programme_code,
    structure_blocks,
    structure_rows,
)
from course_explorer.ingestion.manifest import RawCaptureStore, ReplayedSourceObject
from course_explorer.ingestion.normalization import (
    NormalizedCourseVersion,
    NormalizedModuleRule,
    NormalizedModuleVersion,
    SnapshotProvenance,
)
from course_explorer.ingestion.source import (
    CapturedResponse,
    CurriculumPeriodRef,
    DecodedSourceObject,
    SourcePeriod,
)
from course_explorer.instance import GuideSourceMetadata

# The product module type of each guide structure node type.
_MODULE_TYPES = {
    "PROGRAMME": ModuleType.DEGREE_PROGRAMME,
    "STUDY_MODULE": ModuleType.STUDY_MODULE,
    "CATEGORY": ModuleType.GROUPING_MODULE,
    "OFFERING": ModuleType.GROUPING_MODULE,
    "COURSE_UNIT_PART": ModuleType.GROUPING_MODULE,
    "GROUPING_MODULE": ModuleType.GROUPING_MODULE,
}

# Every observed guide title, in one ordered table: the official field it supplies or ``None``
# when it only marks a page, the language it is written in, and whether it marks a course detail.
_GUIDE_LABELS: dict[str, tuple[OfficialTextField | None, str, bool]] = {
    "osaamistavoitteet": (OfficialTextField.OUTCOMES, "fi", False),
    "tavoitteet": (OfficialTextField.OUTCOMES, "fi", False),
    "objective": (OfficialTextField.OUTCOMES, "en", False),
    "learning outcomes": (OfficialTextField.OUTCOMES, "en", False),
    "learning objectives": (OfficialTextField.OUTCOMES, "en", True),
    "learning objectives and transferable skills": (OfficialTextField.OUTCOMES, "en", False),
    "lärandemål": (OfficialTextField.OUTCOMES, "sv", True),
    "lärandemål och allmänfärdigheter": (OfficialTextField.OUTCOMES, "sv", False),
    "sisältö": (OfficialTextField.CONTENT, "fi", False),
    "content": (OfficialTextField.CONTENT, "en", False),
    "contents": (OfficialTextField.CONTENT, "en", False),
    "innehåll": (OfficialTextField.CONTENT, "sv", False),
    "esitietovaatimukset": (OfficialTextField.PREREQUISITE_TEXT, "fi", False),
    "lähtötaso ja sidonnaisuudet muihin opintojaksoihin": (
        OfficialTextField.PREREQUISITE_TEXT,
        "fi",
        False,
    ),
    "edellytetty osaaminen": (OfficialTextField.PREREQUISITE_TEXT, "fi", False),
    "qualifications": (OfficialTextField.PREREQUISITE_TEXT, "en", False),
    "prerequisites": (OfficialTextField.PREREQUISITE_TEXT, "en", False),
    "förkunskapskrav": (OfficialTextField.PREREQUISITE_TEXT, "sv", False),
    "förhandskunskaper": (OfficialTextField.PREREQUISITE_TEXT, "sv", False),
    "lisätiedot": (OfficialTextField.ADDITIONAL_INFO, "fi", False),
    "further information": (OfficialTextField.ADDITIONAL_INFO, "en", False),
    "tilläggsuppgifter": (OfficialTextField.ADDITIONAL_INFO, "sv", False),
    "tilläggsinformation": (OfficialTextField.ADDITIONAL_INFO, "sv", False),
    "opintojakson perustiedot": (None, "fi", True),
    "opintojakson kuvaustiedot": (None, "fi", True),
    "opinnon kuvaustiedot": (None, "fi", True),
    "opinnon tiedot": (None, "fi", True),
    "kurssin tiedot": (None, "fi", True),
    "course unit description": (None, "en", True),
    "study unit description": (None, "en", True),
    "general information": (None, "en", True),
    "ects credits": (None, "en", True),
    "studieperiodens grunduppgifter": (None, "sv", True),
    "kursens beskrivningar": (None, "sv", True),
}
_TITLE_TO_FIELD = {
    title: field
    for title, (field, _language, _marker) in _GUIDE_LABELS.items()
    if field is not None
}
_COURSE_MARKERS = {title for title, (_field, _language, marker) in _GUIDE_LABELS.items() if marker}
_LABEL_LANGUAGE = {title: language for title, (_field, language, _marker) in _GUIDE_LABELS.items()}
_LABEL_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "th", "dt", "td", "strong", "b", "label")

_STRUCTURE_HEADERS = {
    "tunnus": "fi",
    "opinnon nimi": "fi",
    "laajuus": "fi",
    "code": "en",
    "name": "en",
    "credits": "en",
    "ects credits": "en",
    "kod": "sv",
    "namn": "sv",
    "omfattning": "sv",
}
_OPTIONALITY = {
    "valitaan kaikki": "ALL_OF",
    "choose all": "ALL_OF",
    "väljs alla": "ALL_OF",
    "välj alla": "ALL_OF",
    "valitaan yksi": "ONE_OF",
    "choose one": "ONE_OF",
    "väljs en": "ONE_OF",
    "välj en": "ONE_OF",
}
_OPTIONALITY_PREFIXES: tuple[tuple[str, str | None], ...] = (
    ("valitaan erillisten kriteerien mukaan", "SOME_OF"),
    ("choose according to separate criteria", "SOME_OF"),
    ("väljs enligt separata kriterier", "SOME_OF"),
    ("väljs efter separata kriterier", None),
    ("valitaan opintopisteitä", "SOME_OF"),
    ("choose credits", "SOME_OF"),
    ("välj poäng", "SOME_OF"),
    ("välj studiepoäng", "SOME_OF"),
)
_COUNT_OPTIONALITY = re.compile(
    r"^(?:valitaan\s*\(kpl\)|choose\s*\(pcs\)|välj\s*\(st\))\s*:\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?$"
)
_ACADEMIC_YEAR_PREFIX = re.compile(r"^\s*\d{4}\s*")
_TRAILING_CREDITS = re.compile(
    r"\s*\(\s*\d+(?:[.,]\d+)?\s*(?:op|cr|sp|ects)\s*\)\s*$", re.IGNORECASE
)
# The title check first compares the page-specific title's trailing official code spelled without
# whitespace. This token shape is the fallback: letters followed by digits and then further letters
# or digits, with at most one space. A hyphenated vendor product code is not read as a guide course
# code.
_CODE_SHAPED = re.compile(r"\b([A-ZÅÄÖ]{2,8} ?\d{2,5}[A-Z0-9]*)\b")


class _GuideModel(BaseModel):
    """Frozen extracted-record base that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _GuideChild(_GuideModel):
    type: str
    referenced_group_id: str

    @field_validator("type", "referenced_group_id")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a guide child requires a non-blank type and referenced group id")
        return value


class _GuideCourseRecord(_GuideModel):
    code: str
    period: str
    texts: dict[str, dict[str, str | None]]
    credits_min: Decimal | None
    credits_max: Decimal | None
    official_url: str

    @field_validator("code", "period", "official_url")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a course record requires non-blank identity fields")
        return value

    @field_validator("texts")
    @classmethod
    def require_every_official_field(
        cls, value: dict[str, dict[str, str | None]]
    ) -> dict[str, dict[str, str | None]]:
        if set(value) != {field.value for field in OfficialTextField}:
            raise ValueError("a course record requires exactly the five official text fields")
        return value


class _GuideModuleRecord(_GuideModel):
    """One extracted guide module with its scoped group id and published code or name."""

    group_id: str
    code: str | None
    source_node_type: str
    period: str
    names: dict[str, str | None]
    optionality: Literal["ALL_OF", "SOME_OF", "ONE_OF", "COUNT_OF", "UP_TO"] | None
    count_min: int | None
    count_max: int | None
    target_credits_min: Decimal | None
    target_credits_max: Decimal | None
    children: tuple[_GuideChild, ...]

    @field_validator("group_id", "source_node_type", "period")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a module record requires non-blank identity fields")
        return value

    @field_validator("code")
    @classmethod
    def reject_blank_code(cls, value: str | None) -> str | None:
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("a module code must be null or a non-blank official code")
        return value


def _section_texts(soup: BeautifulSoup) -> dict[str, dict[str, list[str]]]:
    """Collect the mapped section texts of one course page in document order."""
    collected: dict[str, dict[str, list[str]]] = {field.value: {} for field in OfficialTextField}
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        title = heading.get_text(" ", strip=True).casefold()
        field = _TITLE_TO_FIELD.get(title)
        level = int(heading.name[1])
        parts: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and re.fullmatch(r"h[1-6]", sibling.name or ""):
                if int(sibling.name[1]) <= level:
                    break
            if isinstance(sibling, Tag):
                text = sibling.get_text(" ", strip=True)
            elif isinstance(sibling, str):
                text = sibling.strip()
            else:
                continue
            if text:
                parts.append(text)
        if field is None or not parts:
            continue
        row = _GUIDE_LABELS[title]
        collected[field.value].setdefault(row[1], []).append("\n\n".join(parts))
    return collected


def _labelled_value(soup: BeautifulSoup, labels: set[str]) -> str | None:
    """Return the value beside the first recognized label, in document order."""
    for tag in soup.find_all(["dt", "th", "td", "span", "div", "strong", "label"]):
        text = tag.get_text(" ", strip=True).casefold()
        if text not in labels:
            continue
        if tag.name in {"th", "td"}:
            # A table header labels its column, not a value in the next row.
            continue
        sibling = tag.find_next_sibling()
        if isinstance(sibling, Tag):
            value = sibling.get_text(" ", strip=True)
            if value:
                return value
        parent = tag.parent
        if isinstance(parent, Tag):
            next_sibling = parent.find_next_sibling()
            if isinstance(next_sibling, Tag):
                value = next_sibling.get_text(" ", strip=True)
                if value:
                    return value
    return None


def _structure_language(soup: BeautifulSoup) -> str:
    """Return the language of one structure page's recognized table headers."""
    for tag in soup.find_all(["th", "dt", "label", "strong"]):
        text = tag.get_text(" ", strip=True).casefold()
        language = _STRUCTURE_HEADERS.get(text)
        if language is not None:
            return language
    raise GuideContractError("structure page has no recognized language")


def _course_language(soup: BeautifulSoup) -> str:
    """Return the language of the first recognized course label on one page."""
    for tag in soup.find_all(_LABEL_TAGS):
        text = tag.get_text(" ", strip=True).casefold()
        language = _LABEL_LANGUAGE.get(text)
        if language is not None:
            return language
    raise GuideContractError("course page has no recognized language")


def _is_course_detail(soup: BeautifulSoup) -> bool:
    labels = {tag.get_text(" ", strip=True).casefold() for tag in soup.find_all(_LABEL_TAGS)}
    return bool(labels & _COURSE_MARKERS)


def rendered_error_page(html: str) -> bool:
    """Return whether one guide page is the guide's own rendered server-error page.

    The page carries a ``Server Error`` title or a ``500`` heading and no recognized course label
    in any published guide language.
    """
    soup = BeautifulSoup(html, "html.parser")
    if _is_course_detail(soup):
        return False
    title = soup.find("title")
    title_text = title.get_text(" ", strip=True).casefold() if isinstance(title, Tag) else ""
    headings = {
        tag.get_text(" ", strip=True).casefold()
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    }
    return "server error" in title_text or "500" in headings


def _optionality(
    phrase: str | None,
) -> tuple[
    Literal["ALL_OF", "SOME_OF", "ONE_OF", "COUNT_OF", "UP_TO"] | None, tuple[int, int] | None
]:
    if phrase is None or not phrase.strip():
        return None, None
    normalized = phrase.strip().strip("()").strip().casefold()
    mapped = _OPTIONALITY.get(normalized)
    if mapped is not None:
        return mapped, None
    count = _COUNT_OPTIONALITY.fullmatch(normalized)
    if count is not None:
        minimum = int(count.group(1))
        maximum = int(count.group(2)) if count.group(2) is not None else minimum
        if minimum == maximum == 0:
            return None, None
        if minimum > maximum:
            raise GuideContractError("guide count range has a minimum above its maximum")
        if minimum == 0:
            return "UP_TO", (0, maximum)
        return "COUNT_OF", (minimum, maximum)
    for prefix, value in _OPTIONALITY_PREFIXES:
        if normalized.startswith(prefix):
            return value, None
    raise GuideContractError(f"unsupported guide optionality {phrase}")


def _programme_name(soup: BeautifulSoup) -> str:
    heading = soup.find("h1")
    if not isinstance(heading, Tag):
        return ""
    text = heading.get_text(" ", strip=True).split(":", maxsplit=1)[0]
    return _ACADEMIC_YEAR_PREFIX.sub("", text).strip()


def _snapshot(replayed: ReplayedSourceObject) -> SnapshotProvenance:
    return SnapshotProvenance(
        object_type=replayed.record.object_type,
        source_object_id=replayed.record.source_object_id,
        source_group_id=replayed.record.source_group_id,
        response_path=replayed.response_path,
        response_item_index=replayed.record.response_item_index,
        source_hash=replayed.record.source_hash,
    )


class GuideParser:
    """Decode guide page evidence and convert each source object into one normalized record."""

    def __init__(self, block: GuideSourceMetadata, official_languages: tuple[str, ...]) -> None:
        self._block = block
        self._official_languages = official_languages
        self._reset(None)

    def _reset(self, run_id: object) -> None:
        self._run_id = run_id
        self._scanned_response_count = 0
        self._path_index: dict[str, int] = {}
        self._path_index_key: tuple[object, int] | None = None
        self._root_html: str | None = None
        self._programme_paths: tuple[str, ...] = ()
        self._structure_context: dict[str, tuple[str, str | None, str]] = {}
        self._decoded_structures: dict[str, tuple[_GuideModuleRecord, ...]] = {}
        self._course_periods: dict[str, list[str]] = {}
        self._course_listing: dict[
            tuple[str, str],
            tuple[dict[str, str | None], tuple[Decimal | None, Decimal | None], str],
        ] = {}
        self._course_code_by_path: dict[str, str] = {}

    def _ensure_evidence(self, store: RawCaptureStore | None, stop_path: str | None) -> None:
        """Scan recorded discovery evidence up to one response into the parser's state."""
        if store is None:
            raise ValueError("guide decode requires recorded discovery evidence")
        manifest = store.manifest
        if self._run_id != manifest.run_id:
            self._reset(manifest.run_id)
        responses = manifest.responses
        index_key = (manifest.run_id, len(responses))
        if self._path_index_key != index_key:
            self._path_index = {
                response.request_path: index for index, response in enumerate(responses)
            }
            self._path_index_key = index_key
        stop_index = (
            len(responses)
            if stop_path is None or stop_path not in self._path_index
            else self._path_index[stop_path]
        )
        if self._scanned_response_count >= stop_index:
            return
        for response in responses[self._scanned_response_count : stop_index]:
            if response.object_type is None:
                if response.request_path == "/":
                    if self._root_html is not None:
                        raise GuideContractError("guide capture has more than one root page")
                    self._root_html = store.read_response_body(response).decode("utf-8")
                    self._programme_paths = discover_programme_paths(
                        self._root_html,
                        base_url=self._block.guide_base_url,
                        primary_language=self._official_languages[0],
                    )
                elif response.request_path in self._programme_paths:
                    programme_html = store.read_response_body(response).decode("utf-8")
                    for link in structure_blocks(
                        programme_html,
                        base_url=self._block.guide_base_url,
                        programme_path=response.request_path,
                        primary_language=self._official_languages[0],
                    ):
                        existing = self._structure_context.get(link.path)
                        if existing is not None:
                            if existing[0] != link.period_id:
                                raise GuideContractError(
                                    f"structure path {link.path} is published under "
                                    f"{existing[0]} and {link.period_id}"
                                )
                            continue
                        self._structure_context[link.path] = (
                            link.period_id,
                            link.programme_code,
                            link.programme_path,
                        )
                continue
            if response.object_type is not SourceObjectType.MODULE:
                continue
            if self._root_html is None:
                raise GuideContractError("guide capture has no root page")
            context = self._structure_context.get(response.request_path)
            if context is None:
                raise GuideContractError("structure page has no programme context")
            if response.request_path not in self._decoded_structures:
                html = store.read_response_body(response).decode("utf-8")
                self._decoded_structures[response.request_path] = self._decode_structure(
                    html, context
                )
        self._scanned_response_count = stop_index

    def decode_objects(
        self,
        response: CapturedResponse,
        object_type: SourceObjectType,
        *,
        store: RawCaptureStore | None = None,
    ) -> tuple[DecodedSourceObject, ...]:
        """Decode one guide page into its extracted records with identity and source hash.

        A structure page's programme context and course listings come from the recorded
        programme and structure evidence, so the response carries no request-time value.
        """
        self._ensure_evidence(store, response.request_path)
        if object_type is SourceObjectType.COURSE_UNIT:
            records: tuple[_GuideCourseRecord | _GuideModuleRecord, ...] = self._parse_course(
                response
            )
        elif object_type is SourceObjectType.MODULE:
            context = self._structure_context.get(response.request_path)
            if context is None:
                raise GuideContractError("structure page has no programme context")
            cached = self._decoded_structures.get(response.request_path)
            if cached is None:
                cached = self._decode_structure(response.body.decode("utf-8"), context)
                self._decoded_structures[response.request_path] = cached
            records = cached
        else:
            raise ValueError("unsupported captured source object type")
        return tuple(
            DecodedSourceObject(
                source_object_id=self._source_object_id(record),
                source_group_id=(
                    record.group_id if isinstance(record, _GuideModuleRecord) else record.code
                ),
                source_hash=json_sha256(record.model_dump(mode="json"), ensure_ascii=False),
                value=record.model_dump(mode="json"),
            )
            for record in records
        )

    def read_periods(self, store: RawCaptureStore) -> tuple[SourcePeriod, ...]:
        """Return the periods the guide publishes and require the manifest selection to match."""
        self._ensure_evidence(store, None)
        published_ids = {context[0] for context in self._structure_context.values()}
        published = tuple(period for period in self._block.periods if period.id in published_ids)
        selected = tuple(CurriculumPeriodRef(period.id, period.start_year) for period in published)
        if store.manifest.periods != selected:
            raise ValueError("manifest period selection does not match the published periods")
        return tuple(
            SourcePeriod(period.id, period.start_year, period.end_year) for period in published
        )

    def parse_object(
        self, replayed: ReplayedSourceObject
    ) -> NormalizedCourseVersion | NormalizedModuleVersion:
        """Validate and convert one replayed guide course or module record."""
        if replayed.record.object_type is SourceObjectType.COURSE_UNIT:
            return self._course_version(replayed, _GuideCourseRecord.model_validate(replayed.value))
        if replayed.record.object_type is SourceObjectType.MODULE:
            return self._module_version(replayed, _GuideModuleRecord.model_validate(replayed.value))
        raise ValueError("unsupported captured source object type")

    def _decode_structure(
        self, html: str, context: tuple[str, str | None, str]
    ) -> tuple[_GuideModuleRecord, ...]:
        """Decode one structure page into its programme root and non-course module records.

        A published structure table with only its header row contributes no record. The programme
        code is the year group's published label, else the structure page's own published programme
        code, else the programme page's last path segment. A module's group id is the programme root
        followed by every parent module's node type and published code or name.
        """
        rows = structure_rows(html)
        if not rows:
            return ()
        soup = BeautifulSoup(html, "html.parser")
        language = _structure_language(soup)
        period, programme_code, programme_path = context
        if programme_code is None:
            programme_code = published_programme_code(
                soup.get_text(" ", strip=True)
            ) or programme_path_id(programme_path)
        root_group_id = f"PROGRAMME:{programme_code}"
        programme_name = _programme_name(soup) or programme_code
        root_credits = _labelled_credits(soup, {"laajuus"})
        parents = _row_parents(rows)
        group_ids = _module_group_ids(root_group_id, rows, parents)
        root_children = tuple(
            _child_reference(row, group_ids, index)
            for index, row in enumerate(rows)
            if parents[index] is None
        )
        records: list[_GuideModuleRecord] = [
            _GuideModuleRecord(
                group_id=root_group_id,
                code=programme_code,
                source_node_type="PROGRAMME",
                period=period,
                names={language: programme_name},
                optionality=None,
                count_min=None,
                count_max=None,
                target_credits_min=root_credits[0],
                target_credits_max=root_credits[1],
                children=root_children,
            )
        ]
        for index, row in enumerate(rows):
            if row.node_type == "COURSE_UNIT":
                self._store_course_row(row, period, language)
                continue
            if row.node_type not in _MODULE_TYPES:
                raise GuideContractError(f"unsupported guide node type {row.node_type}")
            children = tuple(
                _child_reference(child, group_ids, position)
                for position, child in enumerate(rows)
                if parents[position] == index
            )
            credits = credit_bounds(row.credits)
            optionality, count_bounds = _optionality(row.optionality)
            count_min, count_max = count_bounds or (None, None)
            records.append(
                _GuideModuleRecord(
                    group_id=group_ids[index],
                    code=row.code.strip() or None,
                    source_node_type=row.node_type,
                    period=period,
                    names={language: row.name},
                    optionality=optionality,
                    count_min=count_min,
                    count_max=count_max,
                    target_credits_min=credits[0],
                    target_credits_max=credits[1],
                    children=children,
                )
            )
        return tuple(records)

    def _store_course_row(self, row: _GuideRow, period: str, language: str) -> None:
        code = _course_code(row)
        if row.href is None or not row.href:
            raise GuideContractError("course row has no link")
        url = urljoin(self._block.guide_base_url.rstrip("/") + "/", row.href)
        path = guide_path(self._block.guide_base_url, url)
        periods = self._course_periods.setdefault(code, [])
        if period not in periods:
            periods.append(period)
        facts = ({language: row.name}, credit_bounds(row.credits))
        key = (code, period)
        existing = self._course_listing.get(key)
        if existing is not None:
            if existing[:2] != facts:
                raise GuideContractError("one course code carries two listing facts")
        else:
            # Two programme structures can list one course through their own page prefix, so the
            # first listing supplies the official URL and the name and credit facts must agree.
            self._course_listing[key] = (*facts, url)
        self._course_code_by_path.setdefault(path, code)

    def _parse_course(self, response: CapturedResponse) -> tuple[_GuideCourseRecord, ...]:
        code = self._course_code_by_path.get(response.request_path)
        if code is None or code not in self._course_periods:
            raise GuideContractError("course page has no structure listing")
        soup = BeautifulSoup(response.body.decode("utf-8"), "html.parser")
        if not _is_course_detail(soup):
            raise GuideContractError("page is not a course detail")
        language = _course_language(soup)
        heading = soup.find("h1")
        page_name = ""
        if isinstance(heading, Tag):
            page_name = _TRAILING_CREDITS.sub("", heading.get_text(" ", strip=True)).strip()
        # The guide title ends with the page's official code, which may carry a hyphen that the
        # token shape does not read. A title ending with the listing code passes. Otherwise a
        # code-shaped name word before it, or a number in the name heading, does not name a
        # different course.
        title = soup.find("title")
        if isinstance(title, Tag):
            title_text = title.get_text(" ", strip=True)
            # The served title appends the site's own name after " | " with surrounding
            # whitespace. The comparison reads the whitespace-normalized page-specific part before
            # it.
            page_title = " ".join(title_text.split()).rsplit(" | ", 1)[0]
            listing_code = code.replace(" ", "")
            if not "".join(page_title.split()).endswith(listing_code):
                matches = list(_CODE_SHAPED.finditer(title_text))
                named = matches[-1].group(1).replace(" ", "") if matches else ""
                if named and not named.endswith(listing_code):
                    raise GuideContractError("course page names a different course code")
        page_credits = _labelled_credits(soup, {"laajuus", "ects credits", "studiepoäng"})
        sections = _section_texts(soup)
        periods = sorted(self._course_periods[code], key=self._period_index)
        records: list[_GuideCourseRecord] = []
        for period in periods:
            listing = self._course_listing.get((code, period))
            if listing is None:
                raise GuideContractError("course page has no structure listing")
            listing_names, listing_credits, url = listing
            name = page_name or next(
                (value for value in listing_names.values() if value is not None and value.strip()),
                "",
            )
            credits = page_credits if page_credits[0] is not None else listing_credits
            texts: dict[str, dict[str, str | None]] = {
                field.value: {} for field in OfficialTextField
            }
            if name:
                texts[OfficialTextField.NAME.value] = {language: name}
            for field, languages in sections.items():
                for section_language, sections_text in languages.items():
                    texts[field][section_language] = "\n\n".join(sections_text)
            records.append(
                _GuideCourseRecord(
                    code=code,
                    period=period,
                    texts=texts,
                    credits_min=credits[0],
                    credits_max=credits[1],
                    official_url=url,
                )
            )
        return tuple(records)

    def _period_index(self, period: str) -> int:
        for index, candidate in enumerate(self._block.periods):
            if candidate.id == period:
                return index
        raise GuideContractError("course listing names an unmapped period")

    @staticmethod
    def _source_object_id(record: _GuideCourseRecord | _GuideModuleRecord) -> str:
        if isinstance(record, _GuideModuleRecord):
            return f"{record.group_id}|{record.period}"
        return f"{record.code}|{record.period}"

    def _course_version(
        self, replayed: ReplayedSourceObject, record: _GuideCourseRecord
    ) -> NormalizedCourseVersion:
        return NormalizedCourseVersion(
            provenance=_snapshot(replayed),
            group_id=record.code,
            code=record.code,
            texts={OfficialTextField(field): values for field, values in record.texts.items()},
            credits_min=record.credits_min,
            credits_max=record.credits_max,
            official_url=record.official_url,
            document_state=None,
            validity_start=None,
            validity_end=None,
            source_revision=None,
            source_modification_ordinal=None,
            curriculum_period_ids=(record.period,),
            prerequisite_options=(),
            substitution_options=(),
        )

    def _module_version(
        self, replayed: ReplayedSourceObject, record: _GuideModuleRecord
    ) -> NormalizedModuleVersion:
        return NormalizedModuleVersion(
            provenance=_snapshot(replayed),
            group_id=record.group_id,
            module_type=_MODULE_TYPES[record.source_node_type],
            code=record.code,
            names=record.names,
            target_credits_min=record.target_credits_min,
            target_credits_max=record.target_credits_max,
            document_state=None,
            validity_start=None,
            validity_end=None,
            source_revision=None,
            source_modification_ordinal=None,
            curriculum_period_ids=(record.period,),
            rule=_module_rule(record),
        )


def _course_code(row: _GuideRow) -> str:
    """Return one course row's required official course code."""
    code = row.code
    if not isinstance(code, str) or not code.strip():
        raise GuideContractError("course row has no code")
    return code


def _module_identity(row: _GuideRow) -> str:
    """Return one module row's published identity: its code, else its name."""
    code = row.code.strip() if isinstance(row.code, str) else ""
    if code:
        return code
    name = row.name.strip() if isinstance(row.name, str) else ""
    if name:
        return name
    raise GuideContractError("structure row has no module identity")


def _module_group_ids(
    root_group_id: str, rows: tuple[_GuideRow, ...], parents: tuple[int | None, ...]
) -> dict[int, str]:
    """Return each module row's group id over its full ancestor chain, keyed by row position.

    A code-less module whose published name repeats among its parent's module children carries its
    1-based position among those same-named children as `` #<ordinal>``.
    """
    same_name_indexes: dict[tuple[int | None, str], list[int]] = {}
    for index, row in enumerate(rows):
        if row.node_type == "COURSE_UNIT":
            continue
        name = row.name.strip() if isinstance(row.name, str) else ""
        same_name_indexes.setdefault((parents[index], name), []).append(index)
    group_ids: dict[int, str] = {}
    for index, row in enumerate(rows):
        if row.node_type == "COURSE_UNIT":
            continue
        parent = parents[index]
        if parent is None:
            prefix = root_group_id
        else:
            if rows[parent].node_type == "COURSE_UNIT":
                raise GuideContractError("module row is nested under a course row")
            prefix = group_ids[parent]
        identity = _module_identity(row)
        name = row.name.strip() if isinstance(row.name, str) else ""
        siblings = same_name_indexes[(parent, name)]
        coded = isinstance(row.code, str) and bool(row.code.strip())
        if not coded and len(siblings) > 1:
            identity = f"{identity} #{siblings.index(index) + 1}"
        group_ids[index] = f"{prefix}:{row.node_type}:{identity}"
    return group_ids


def _child_reference(row: _GuideRow, group_ids: dict[int, str], index: int) -> _GuideChild:
    """Return one rule child that references a course code or the row's scoped module group id."""
    return _GuideChild(
        type=row.node_type,
        referenced_group_id=(
            _course_code(row) if row.node_type == "COURSE_UNIT" else group_ids[index]
        ),
    )


def _row_parents(rows: tuple[_GuideRow, ...]) -> tuple[int | None, ...]:
    """Return each row's nearest earlier row of a lower level, or ``None`` for a level-1 row."""
    parents: list[int | None] = []
    for index, row in enumerate(rows):
        parent = None
        for earlier in range(index - 1, -1, -1):
            if rows[earlier].level < row.level:
                parent = earlier
                break
        parents.append(parent)
    return tuple(parents)


def _labelled_credits(
    soup: BeautifulSoup, labels: set[str]
) -> tuple[Decimal | None, Decimal | None]:
    value = _labelled_value(soup, labels)
    if value is None:
        return None, None
    return credit_bounds(value)


def _composite(
    children: tuple[NormalizedModuleRule, ...],
    *,
    all_mandatory: bool = False,
    require_min: int | None = 0,
    require_max: int | None = None,
) -> NormalizedModuleRule:
    """Return one composite rule that selects among ``children`` under the given cardinality."""
    return NormalizedModuleRule(
        sibling_position=0,
        local_id=None,
        node_type=ModuleRuleType.COMPOSITE_RULE,
        require_min=require_min,
        require_max=require_max,
        all_mandatory=all_mandatory,
        description=None,
        credits_min=None,
        credits_max=None,
        count_min=None,
        count_max=None,
        referenced_group_id=None,
        children=children,
    )


def _bounded(
    node_type: ModuleRuleType,
    children: tuple[NormalizedModuleRule, ...],
    *,
    credits_min: Decimal | None = None,
    credits_max: Decimal | None = None,
    count_min: int | None = None,
    count_max: int | None = None,
) -> NormalizedModuleRule:
    """Return one credit or count rule over the single selection pool of ``children``."""
    return NormalizedModuleRule(
        sibling_position=0,
        local_id=None,
        node_type=node_type,
        require_min=None,
        require_max=None,
        all_mandatory=None,
        description=None,
        credits_min=credits_min,
        credits_max=credits_max,
        count_min=count_min,
        count_max=count_max,
        referenced_group_id=None,
        children=(_composite(children),),
    )


def _module_rule(record: _GuideModuleRecord) -> NormalizedModuleRule | None:
    """Return the mapped rule of one module, or ``None`` when it has no children.

    Each published optionality maps to one shape the native compiler accepts: an all-mandatory
    composite, a composite that requires one child, a non-mandatory composite carrying a published
    maximum, a course-unit count rule over one selection pool, or a credits rule over one selection
    pool. A published zero count, a some-of without a credit minimum and a null optionality are one
    composite with a minimum of zero and no maximum.
    """
    if not record.children:
        return None
    children = tuple(
        NormalizedModuleRule(
            sibling_position=index,
            local_id=None,
            node_type=(
                ModuleRuleType.COURSE_UNIT_RULE
                if child.type == "COURSE_UNIT"
                else ModuleRuleType.MODULE_RULE
            ),
            require_min=None,
            require_max=None,
            all_mandatory=None,
            description=None,
            credits_min=None,
            credits_max=None,
            count_min=None,
            count_max=None,
            referenced_group_id=child.referenced_group_id,
            children=(),
        )
        for index, child in enumerate(record.children)
    )
    if record.optionality == "ALL_OF":
        return _composite(children, all_mandatory=True, require_min=None)
    if record.optionality == "ONE_OF":
        return _composite(children, require_min=1, require_max=1)
    if record.optionality == "UP_TO":
        return _composite(children, require_min=0, require_max=record.count_max)
    if record.optionality == "COUNT_OF":
        return _bounded(
            ModuleRuleType.COURSE_UNIT_COUNT_RULE,
            children,
            count_min=record.count_min,
            count_max=record.count_max,
        )
    if record.optionality == "SOME_OF" and record.target_credits_min is not None:
        return _bounded(
            ModuleRuleType.CREDITS_RULE,
            children,
            credits_min=record.target_credits_min,
            credits_max=record.target_credits_max,
        )
    return _composite(children)
