"""Peppi Solenovo PSG source models, evidence decoding and normalized-record conversion.

This module owns the family's extracted record, its flat plan tree and its plan-credit join. It
decodes captured response bytes into the shared neutral normalized course and module records. The
shared normalization service owns deduplication, period expansion, membership filtering and closure
validation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, field_validator

from course_explorer.enums import (
    ModuleRuleType,
    ModuleType,
    OfficialTextField,
    SourceObjectType,
)
from course_explorer.hashing import json_sha256
from course_explorer.ingestion.manifest import RawCaptureStore, ReplayedSourceObject
from course_explorer.ingestion.normalization import (
    NormalizedCourseVersion,
    NormalizedModuleRule,
    NormalizedModuleVersion,
    SnapshotProvenance,
)
from course_explorer.ingestion.peppi_psg import PeppiPsgContractError
from course_explorer.ingestion.source import (
    CapturedResponse,
    CurriculumPeriodRef,
    DecodedSourceObject,
    SourcePeriod,
)
from course_explorer.instance import PeppiPsgSourceMetadata

# One accomplishment plan's flat learning-unit list, and one learning unit's detail.
_PLAN_PATH = re.compile(r"^/app/rest/accomplishmentplan/([^/]+)/learningunits$")
# One group read of a year, an education type and a degree programme. A plan response names no
# year, so this path is the listing evidence a plan's published years come from.
_PLAN_GROUP_PATH = re.compile(
    r"^/app/rest/accomplishmentplan/year/(\d+)/educationtype/([^/]+)/degreeprogramme/([^/]+)$"
)
_COURSE_PATH = re.compile(r"^/app/rest/learningunit/([^/]+)$")

# The product module type of each PSG learning-unit type that stores a module.
_MODULE_TYPES = {
    "STUDY_MODULE": ModuleType.STUDY_MODULE,
    "CATEGORY": ModuleType.GROUPING_MODULE,
    "OFFERING": ModuleType.GROUPING_MODULE,
}
_PLAN_UNIT_TYPES = frozenset(_MODULE_TYPES) | {"COURSE_UNIT"}


class _PeppiPsgModel(BaseModel):
    """Frozen extracted-record base that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _PeppiPsgChild(_PeppiPsgModel):
    type: str
    code: str

    @field_validator("type", "code")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a learning-unit child requires a non-blank type and code")
        return value


class _PeppiPsgCourseRecord(_PeppiPsgModel):
    code: str
    path_id: str
    period: str
    texts: dict[str, dict[str, str | None]]
    credits_min: Decimal | None
    credits_max: Decimal | None

    @field_validator("code", "path_id", "period")
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


class _PeppiPsgModuleRecord(_PeppiPsgModel):
    """One extracted plan module, scoped to the plan whose structure it belongs to.

    A plan response publishes no programme root module, so every record, including a level-1
    structure root, carries the plan identity as its owner. The owner is part of the record and of
    its source hash.
    """

    code: str
    source_node_type: str
    owning_group_id: str
    period: str
    names: dict[str, str | None]
    target_credits_min: Decimal | None
    target_credits_max: Decimal | None
    children: tuple[_PeppiPsgChild, ...]

    @field_validator("code", "source_node_type", "owning_group_id", "period")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a module record requires non-blank identity fields")
        return value


@dataclass(frozen=True)
class _PeppiPsgEntry:
    """One plan learning-unit entry with its published tree position and direct children."""

    unit_id: str
    code: str
    node_type: str
    names: dict[str, str | None]
    level: int
    order_number: int
    credits: Decimal | None
    children: tuple[_PeppiPsgChild, ...]


def _required_text(value: object, label: str) -> str:
    """Return one non-blank source text or raise a contract error."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise PeppiPsgContractError(f"Peppi PSG {label} must be a non-blank value")
    return value


def _localized(value: object, official_languages: tuple[str, ...]) -> dict[str, str | None]:
    """Map one source localized text map to the configured official languages."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PeppiPsgContractError("Peppi PSG localized source text must be an object")
    localized: dict[str, str | None] = {}
    for language, text in value.items():
        if language not in official_languages:
            continue
        if text is not None and not isinstance(text, str):
            raise PeppiPsgContractError("Peppi PSG localized source text must be a string or null")
        localized[language] = text
    return localized


def _credit_bound(value: object) -> Decimal | None:
    """Return one numeric credit bound exactly, or ``None`` when the source omits it."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return Decimal(str(value))


def _optional_credit(value: object, label: str) -> Decimal | None:
    """Return one published credit value or ``None`` when it is absent."""
    if value is None:
        return None
    credit = _credit_bound(value)
    if credit is None:
        raise PeppiPsgContractError(f"Peppi PSG {label} must be a number or null")
    return credit


def _decode_json(body: bytes) -> object:
    """Decode one captured response body, mapping any JSON error to a contract error."""
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PeppiPsgContractError("Peppi PSG response body must be valid JSON") from exc


def _plan_entries(value: object, official_languages: tuple[str, ...]) -> tuple[_PeppiPsgEntry, ...]:
    """Return one plan's entries in source tree order with their direct children.

    An empty plan publishes no structure, so it supplies no entries and no records.
    """
    if not isinstance(value, list):
        raise PeppiPsgContractError("Peppi PSG accomplishment plan must be a list")
    if not value:
        return ()
    entries: list[_PeppiPsgEntry] = []
    for item in value:
        if not isinstance(item, dict):
            raise PeppiPsgContractError("Peppi PSG plan entry must be an object")
        level = item.get("level")
        order_number = item.get("orderNumber")
        if isinstance(level, bool) or not isinstance(level, int) or level < 1:
            raise PeppiPsgContractError("Peppi PSG plan entry level must be a positive integer")
        if isinstance(order_number, bool) or not isinstance(order_number, int) or order_number < 1:
            raise PeppiPsgContractError("Peppi PSG plan entry order must be a positive integer")
        unit = item.get("learningUnit")
        if not isinstance(unit, dict):
            raise PeppiPsgContractError("Peppi PSG plan entry requires a learning unit")
        node_type = _required_text(unit.get("type"), "learning-unit type")
        if node_type == "COURSE_UNIT":
            names: dict[str, str | None] = {}
        else:
            source_names = unit.get("names")
            if not isinstance(source_names, dict):
                raise PeppiPsgContractError("Peppi PSG learning-unit names must be a map")
            names = _localized(source_names, official_languages)
        entries.append(
            _PeppiPsgEntry(
                unit_id=_required_text(unit.get("id"), "learning-unit id"),
                code=_required_text(unit.get("code"), "learning-unit code"),
                node_type=node_type,
                names=names,
                level=level,
                order_number=order_number,
                credits=_optional_credit(item.get("credits"), "plan credits"),
                children=(),
            )
        )

    ordered = tuple(sorted(entries, key=lambda entry: entry.order_number))
    if len({entry.order_number for entry in ordered}) != len(ordered):
        raise PeppiPsgContractError("Peppi PSG plan entry order numbers must be distinct")
    if ordered[0].level != 1:
        raise PeppiPsgContractError(
            "Peppi PSG plan entry with the lowest order must sit at level 1"
        )
    unsupported = {entry.node_type for entry in ordered} - _PLAN_UNIT_TYPES
    if unsupported:
        raise PeppiPsgContractError(
            f"unsupported Peppi PSG learning-unit type {sorted(unsupported)}"
        )

    parents: list[tuple[_PeppiPsgEntry, int]] = []
    child_lists: dict[int, list[_PeppiPsgChild]] = {}
    for entry in ordered:
        while parents and parents[-1][1] >= entry.level:
            parents.pop()
        if parents:
            child_lists.setdefault(parents[-1][0].order_number, []).append(
                _PeppiPsgChild(type=entry.node_type, code=entry.code)
            )
        parents.append((entry, entry.level))
    return tuple(
        replace(entry, children=tuple(child_lists.get(entry.order_number, ()))) for entry in ordered
    )


def _course_texts(
    detail: dict[str, object], official_languages: tuple[str, ...]
) -> dict[str, dict[str, str | None]]:
    """Map one learning-unit detail onto the five official text fields."""
    texts: dict[str, dict[str, str | None]] = {field.value: {} for field in OfficialTextField}
    texts[OfficialTextField.NAME.value] = _localized(detail.get("names"), official_languages)
    texts[OfficialTextField.OUTCOMES.value] = _localized(
        detail.get("objectives"), official_languages
    )
    texts[OfficialTextField.CONTENT.value] = _localized(detail.get("content"), official_languages)
    return texts


def _module_rule(record: _PeppiPsgModuleRecord) -> NormalizedModuleRule | None:
    """Return the flat-tree composite rule of one module, or ``None`` when it has no children.

    A module child references its plan-scoped group id, the same identity its record carries. A
    course child references the course code.
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
            referenced_group_id=(
                child.code
                if child.type == "COURSE_UNIT"
                else f"{record.owning_group_id}:{child.type}:{child.code}"
            ),
            children=(),
        )
        for index, child in enumerate(record.children)
    )
    return NormalizedModuleRule(
        sibling_position=0,
        local_id=None,
        node_type=ModuleRuleType.COMPOSITE_RULE,
        require_min=0,
        require_max=None,
        all_mandatory=False,
        description=None,
        credits_min=None,
        credits_max=None,
        count_min=None,
        count_max=None,
        referenced_group_id=None,
        children=children,
    )


def _snapshot(replayed: ReplayedSourceObject) -> SnapshotProvenance:
    return SnapshotProvenance(
        object_type=replayed.record.object_type,
        source_object_id=replayed.record.source_object_id,
        source_group_id=replayed.record.source_group_id,
        response_path=replayed.response_path,
        response_item_index=replayed.record.response_item_index,
        source_hash=replayed.record.source_hash,
    )


class PeppiPsgParser:
    """Decode Peppi PSG evidence and convert each source object into one neutral normalized record."""

    def __init__(self, block: PeppiPsgSourceMetadata, official_languages: tuple[str, ...]) -> None:
        self._block = block
        self._official_languages = official_languages
        self._plan_years: dict[str, tuple[int, ...]] = {}
        self._plan_course_credits: dict[tuple[int, str], Decimal | None] = {}
        self._course_years: dict[str, tuple[int, ...]] = {}
        self._listing_store: RawCaptureStore | None = None
        self._listing_years: dict[str, tuple[int, ...]] = {}

    def bind_discovery(self, plan_years: Mapping[str, Sequence[int]]) -> None:
        """Bind the plan-to-listing-year evidence the capture read from the group responses."""
        self._plan_years = {plan_id: tuple(years) for plan_id, years in plan_years.items()}

    def _plan_listing_years(self, store: RawCaptureStore | None) -> dict[str, tuple[int, ...]]:
        """Return every recorded group response's plan ids and path years, once per store.

        The store's response list only grows and this instance caches the derived map by store
        identity, so a decode and a later period read reuse one derivation.
        """
        if store is None:
            raise PeppiPsgContractError("plan listing evidence requires the recorded capture store")
        if self._listing_store is store:
            return self._listing_years
        collected: dict[str, list[int]] = {}
        for response in store.manifest.responses:
            match = _PLAN_GROUP_PATH.fullmatch(response.request_path)
            if match is None or response.request_params != ():
                continue
            year = int(match.group(1))
            body = _decode_json(store.read_response_body(response))
            if not isinstance(body, list):
                raise PeppiPsgContractError("Peppi PSG groups response must be a list")
            for group in body:
                if not isinstance(group, dict):
                    raise PeppiPsgContractError("Peppi PSG group must be an object")
                plans = group.get("accomplishmentPlans")
                if not isinstance(plans, list):
                    raise PeppiPsgContractError(
                        "Peppi PSG group accomplishment plans must be a list"
                    )
                for plan in plans:
                    if not isinstance(plan, dict):
                        raise PeppiPsgContractError(
                            "Peppi PSG accomplishment plan must be an object"
                        )
                    years = collected.setdefault(
                        _required_text(plan.get("id"), "accomplishment plan id"), []
                    )
                    if year not in years:
                        years.append(year)
        self._listing_store = store
        self._listing_years = {
            plan_id: tuple(sorted(years)) for plan_id, years in collected.items()
        }
        return self._listing_years

    def _plan_listing_years_of(
        self, plan_id: str, store: RawCaptureStore | None
    ) -> tuple[int, ...]:
        """Return one plan's listing years from the bound evidence or the recorded group reads."""
        years = self._plan_years.get(plan_id)
        if years is None:
            years = self._plan_listing_years(store).get(plan_id)
        if years is None:
            raise PeppiPsgContractError(
                f"accomplishment plan {plan_id} has no published listing year"
            )
        return years

    @staticmethod
    def _module_group_id(record: _PeppiPsgModuleRecord) -> str:
        """Return a module's plan-scoped group id, which carries the plan before its own identity."""
        return f"{record.owning_group_id}:{record.source_node_type}:{record.code}"

    @classmethod
    def _source_object_id(cls, record: _PeppiPsgCourseRecord | _PeppiPsgModuleRecord) -> str:
        if isinstance(record, _PeppiPsgModuleRecord):
            return f"{cls._module_group_id(record)}|{record.period}"
        return f"{record.code}|{record.period}"

    def _remember_plan(self, entries: tuple[_PeppiPsgEntry, ...], year: int) -> None:
        """Record one listing year's plan credits and add that year to each listed learning unit."""
        for entry in entries:
            if entry.node_type != "COURSE_UNIT":
                continue
            key = (year, entry.unit_id)
            previous = self._plan_course_credits.setdefault(key, entry.credits)
            if previous != entry.credits:
                raise PeppiPsgContractError(
                    f"course code {entry.code} carries conflicting plan credits in {year}"
                )
            listed = self._course_years.setdefault(entry.unit_id, ())
            if year not in listed:
                self._course_years[entry.unit_id] = (*listed, year)

    def _decode_course(self, response: CapturedResponse) -> tuple[_PeppiPsgCourseRecord, ...]:
        """Extract one learning-unit detail into one record per year that lists its code."""
        match = _COURSE_PATH.fullmatch(response.request_path)
        if match is None or response.request_params != ():
            raise PeppiPsgContractError("course request must address one learning unit")
        path_id = unquote(match.group(1))
        if not path_id.strip() or "/" in path_id:
            raise PeppiPsgContractError("course request must address one learning-unit id")
        body = _decode_json(response.body)
        if not isinstance(body, dict):
            raise PeppiPsgContractError("learning-unit response must be an object")
        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            raise PeppiPsgContractError("learning-unit code must be a non-blank string")
        years = self._course_years.get(path_id)
        if not years:
            raise PeppiPsgContractError(
                f"course detail {path_id} was decoded before its plan entry"
            )
        credit = _optional_credit(body.get("credits"), "course credits")
        texts = _course_texts(body, self._official_languages)
        return tuple(
            _PeppiPsgCourseRecord(
                code=code,
                path_id=path_id,
                period=f"{year}-{year + 1}",
                texts=texts,
                credits_min=(
                    credit if credit is not None else self._plan_course_credits[(year, path_id)]
                ),
                credits_max=(
                    credit if credit is not None else self._plan_course_credits[(year, path_id)]
                ),
            )
            for year in sorted(years)
        )

    def _decode_plan(
        self, response: CapturedResponse, store: RawCaptureStore | None
    ) -> tuple[_PeppiPsgModuleRecord, ...]:
        """Extract one plan's modules into one record per listing year and entry order.

        Every emitted record carries the plan id as its owning identity for every listing year.
        """
        match = _PLAN_PATH.fullmatch(response.request_path)
        if match is None or response.request_params != ():
            raise PeppiPsgContractError(
                "plan request must address one accomplishment plan's learning units"
            )
        plan_id = unquote(match.group(1))
        years = self._plan_listing_years_of(plan_id, store)
        entries = _plan_entries(_decode_json(response.body), self._official_languages)
        for year in sorted(years):
            self._remember_plan(entries, year)
        owner = f"PLAN:{plan_id}"
        return tuple(
            _PeppiPsgModuleRecord(
                code=entry.code,
                source_node_type=entry.node_type,
                owning_group_id=owner,
                period=f"{year}-{year + 1}",
                names=entry.names,
                target_credits_min=entry.credits,
                target_credits_max=entry.credits,
                children=entry.children,
            )
            for year in sorted(years)
            for entry in entries
            if entry.node_type != "COURSE_UNIT"
        )

    def decode_objects(
        self,
        response: CapturedResponse,
        object_type: SourceObjectType,
        *,
        store: RawCaptureStore | None = None,
    ) -> tuple[DecodedSourceObject, ...]:
        """Decode one typed response's extracted records with their identity and source hash.

        A plan response names no year, so its records come from the bound discovery evidence or,
        when it is unbound, from the group responses the store already recorded. The hash covers
        the extracted record, including a module's plan-scoped owning identity.
        """
        records: tuple[_PeppiPsgCourseRecord | _PeppiPsgModuleRecord, ...]
        if object_type is SourceObjectType.COURSE_UNIT:
            records = self._decode_course(response)
        elif object_type is SourceObjectType.MODULE:
            records = self._decode_plan(response, store)
        else:
            raise ValueError("unsupported captured source object type")
        return tuple(
            DecodedSourceObject(
                source_object_id=self._source_object_id(record),
                source_group_id=(
                    self._module_group_id(record)
                    if isinstance(record, _PeppiPsgModuleRecord)
                    else record.code
                ),
                source_hash=json_sha256(record.model_dump(mode="json"), ensure_ascii=False),
                value=record.model_dump(mode="json"),
            )
            for record in records
        )

    def read_periods(self, store: RawCaptureStore) -> tuple[SourcePeriod, ...]:
        """Read every plan's listing years from recorded group evidence and require the selection."""
        self._plan_course_credits = {}
        self._course_years = {}
        self._listing_store = None
        self._listing_years = {}
        self.bind_discovery(self._plan_listing_years(store))
        for response in store.manifest.responses:
            match = _PLAN_PATH.fullmatch(response.request_path)
            if match is None:
                continue
            if response.request_params != ():
                raise PeppiPsgContractError("plan request must carry no parameters")
            years = self._plan_listing_years_of(unquote(match.group(1)), store)
            entries = _plan_entries(
                _decode_json(store.read_response_body(response)), self._official_languages
            )
            for year in sorted(years):
                self._remember_plan(entries, year)
        selected = tuple(
            CurriculumPeriodRef(period.id, period.start_year) for period in self._block.periods
        )
        if store.manifest.periods != selected:
            raise ValueError("manifest period selection does not match the mapped periods")
        return tuple(
            SourcePeriod(period.id, period.start_year, period.end_year)
            for period in self._block.periods
        )

    def parse_object(
        self, replayed: ReplayedSourceObject
    ) -> NormalizedCourseVersion | NormalizedModuleVersion:
        """Validate and convert one replayed Peppi PSG course or module record."""
        if replayed.record.object_type is SourceObjectType.COURSE_UNIT:
            return self._course_version(
                replayed, _PeppiPsgCourseRecord.model_validate(replayed.value)
            )
        if replayed.record.object_type is SourceObjectType.MODULE:
            return self._module_version(
                replayed, _PeppiPsgModuleRecord.model_validate(replayed.value)
            )
        raise ValueError("unsupported captured source object type")

    def _course_version(
        self, replayed: ReplayedSourceObject, record: _PeppiPsgCourseRecord
    ) -> NormalizedCourseVersion:
        return NormalizedCourseVersion(
            provenance=_snapshot(replayed),
            group_id=record.code,
            code=record.code,
            texts={OfficialTextField(field): values for field, values in record.texts.items()},
            credits_min=record.credits_min,
            credits_max=record.credits_max,
            official_url=self._block.course_page_template.format(
                id=quote(record.path_id, safe=""), lang=self._official_languages[0]
            ),
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
        self, replayed: ReplayedSourceObject, record: _PeppiPsgModuleRecord
    ) -> NormalizedModuleVersion:
        return NormalizedModuleVersion(
            provenance=_snapshot(replayed),
            group_id=self._module_group_id(record),
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
