"""Peppi legacy source models, evidence decoding and normalized-record conversion.

This module owns the family's extracted record, its exact section-title mapping and its typed
module-rule conversion. It decodes captured response bytes into the shared neutral normalized
course and module records. The shared normalization service owns deduplication, period expansion,
membership filtering and closure validation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Literal
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
from course_explorer.ingestion.peppi_legacy import PeppiLegacyContractError, walk_nodes
from course_explorer.ingestion.source import (
    CapturedResponse,
    CurriculumPeriodRef,
    DecodedSourceObject,
    SourcePeriod,
)
from course_explorer.instance import PeppiLegacySourceMetadata

# The product language of each localized source key. Any other key is ignored.
_LANGUAGE_BY_SOURCE_KEY = {"valueFi": "fi", "valueEn": "en", "valueSv": "sv"}

# The exact section titles observed on the legacy guides, mapped to the product official fields.
_MAPPED_TITLES = {
    OfficialTextField.OUTCOMES.value: (
        "osaamistavoitteet",
        "learning outcomes",
        "learning objectives and transferable skills",
        "lärandemål och allmänfärdigheter",
        "lärandemål",
    ),
    OfficialTextField.CONTENT.value: ("sisältö", "content", "contents", "innehåll"),
    OfficialTextField.PREREQUISITE_TEXT.value: (
        "esitietovaatimukset",
        "prerequisites",
        "qualifications",
        "edellytetty osaaminen",
        "förkunskapskrav",
        "förhandskunskaper",
    ),
    OfficialTextField.ADDITIONAL_INFO.value: (
        "lisätiedot",
        "further information",
        "tilläggsuppgifter",
        "tilläggsinformation",
    ),
}
_TITLE_TO_FIELD = {title: field for field, titles in _MAPPED_TITLES.items() for title in titles}

# The product module type of each legacy curriculum-tree node type.
_MODULE_TYPES = {
    "PROGRAMME": ModuleType.DEGREE_PROGRAMME,
    "STUDY_MODULE": ModuleType.STUDY_MODULE,
    "CATEGORY": ModuleType.GROUPING_MODULE,
    "OFFERING": ModuleType.GROUPING_MODULE,
    "COURSE_UNIT_PART": ModuleType.GROUPING_MODULE,
    "GROUPING_MODULE": ModuleType.GROUPING_MODULE,
}


class _PeppiModel(BaseModel):
    """Frozen extracted-record base that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _PeppiChild(_PeppiModel):
    type: str
    code: str

    @field_validator("type", "code")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a curriculum child requires a non-blank type and code")
        return value


class _PeppiCourseRecord(_PeppiModel):
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


class _PeppiModuleRecord(_PeppiModel):
    """One extracted curriculum module, carrying the owning programme of a nested node.

    A tree root stores ``owning_group_id`` as ``None`` and keeps its own published identity. Every
    node below the root stores the root's ``<node type>:<code>`` identity, so the owning identity is
    part of the record and of its source hash.
    """

    code: str
    source_node_type: str
    owning_group_id: str | None
    period: str
    names: dict[str, str | None]
    optionality: Literal["ALL_OF", "SOME_OF", "ONE_OF", "NUMBER_OF", "ECTS"] | None
    target_credits_min: Decimal | None
    target_credits_max: Decimal | None
    children: tuple[_PeppiChild, ...]

    @field_validator("code", "source_node_type", "period")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a module record requires non-blank identity fields")
        return value

    @field_validator("owning_group_id")
    @classmethod
    def reject_blank_owner(cls, value: str | None) -> str | None:
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("a module owner must be null or a non-blank identity")
        return value


def _localized(value: object) -> dict[str, str | None]:
    """Map one source localized text map to product language keys, ignoring other keys."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PeppiLegacyContractError("localized source text must be an object")
    localized: dict[str, str | None] = {}
    for key, item in value.items():
        language = _LANGUAGE_BY_SOURCE_KEY.get(key)
        if language is None:
            continue
        if item is not None and not isinstance(item, str):
            raise PeppiLegacyContractError("localized source text must be a string or null")
        localized[language] = item
    return localized


def _credit_bound(value: object) -> Decimal | None:
    """Return one numeric credit bound exactly, or ``None`` when the source omits it."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return Decimal(str(value))


def _json_body(response: CapturedResponse) -> object:
    """Decode one captured response body, mapping any JSON error to a contract error."""
    try:
        return json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PeppiLegacyContractError("Peppi response body must be valid JSON") from exc


def _mapped_field(titles: dict[str, str | None]) -> str | None:
    """Return the official field of one section whose title matches the observed vocabulary."""
    for title in titles.values():
        if title is None or not title.strip():
            continue
        field = _TITLE_TO_FIELD.get(title.strip().casefold())
        if field is not None:
            return field
    return None


def _extract_texts(content_list: object) -> dict[str, dict[str, str]]:
    """Map one course content list onto the mapped official fields in section order."""
    if content_list is None:
        entries: list[object] = []
    elif isinstance(content_list, list):
        entries = content_list
    else:
        raise PeppiLegacyContractError("course contentList must be a list")
    collected: dict[str, dict[str, list[str]]] = {field: {} for field in _MAPPED_TITLES}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PeppiLegacyContractError("course contentList entry must be an object")
        field = _mapped_field(_localized(entry.get("title")))
        if field is None:
            continue
        for language, text in _localized(entry.get("content")).items():
            if text is None or not text.strip():
                continue
            collected[field].setdefault(language, []).append(text)
    return {
        field: {language: "\n\n".join(texts) for language, texts in languages.items()}
        for field, languages in collected.items()
    }


def _course_path_id(request_path: str) -> str:
    """Return the one source path id named by a course request path."""
    prefix = "/api/course/"
    if not request_path.startswith(prefix):
        raise PeppiLegacyContractError("course request must address one course path")
    path_id = unquote(request_path.removeprefix(prefix))
    if not path_id.strip() or "/" in path_id:
        raise PeppiLegacyContractError("course request must address one course path id")
    return path_id


def _request_period(response: CapturedResponse, block: PeppiLegacySourceMetadata) -> str:
    """Return the published period named by one period-dependent request.

    A period-dependent request carries exactly one ``period`` parameter whose value is one of the
    configured block's published periods.
    """
    params = response.request_params
    if len(params) != 1 or params[0][0] != "period":
        raise PeppiLegacyContractError("request must carry exactly its published period")
    period = params[0][1]
    if period not in {candidate.id for candidate in block.periods}:
        raise PeppiLegacyContractError("request period is not one of the published periods")
    return period


def _extract_course(
    response: CapturedResponse, block: PeppiLegacySourceMetadata
) -> _PeppiCourseRecord:
    """Extract one course detail into its record without any request-time value."""
    path_id = _course_path_id(response.request_path)
    period = _request_period(response, block)
    body = _json_body(response)
    if not isinstance(body, dict):
        raise PeppiLegacyContractError("course response must be an object")
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise PeppiLegacyContractError("course code must be a non-blank string")
    credits = body.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, int | float):
        credits_min = _credit_bound(body.get("minCredits"))
        credits_max = _credit_bound(body.get("maxCredits"))
    else:
        credits_min = credits_max = _credit_bound(credits)
    texts: dict[str, dict[str, str | None]] = {field.value: {} for field in OfficialTextField}
    texts[OfficialTextField.NAME.value] = _localized(body.get("name"))
    texts.update(_extract_texts(body.get("contentList")))
    return _PeppiCourseRecord(
        code=code,
        path_id=path_id,
        period=period,
        texts=texts,
        credits_min=credits_min,
        credits_max=credits_max,
    )


def _module_children(value: object) -> tuple[tuple[_PeppiChild, str | None], ...]:
    """Return one node's direct child identities and course-unit path ids in source order."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PeppiLegacyContractError("curriculum node children must be a list")
    children: list[tuple[_PeppiChild, str | None]] = []
    for child in value:
        if not isinstance(child, dict):
            raise PeppiLegacyContractError("curriculum child node must be an object")
        child_type = child.get("type")
        code = child.get("code")
        if not isinstance(child_type, str) or not child_type.strip():
            raise PeppiLegacyContractError("curriculum child type must be a non-blank string")
        if not isinstance(code, str) or not code.strip():
            raise PeppiLegacyContractError("curriculum child code must be a non-blank string")
        path_id = None
        if child_type == "COURSE_UNIT":
            path_id = child.get("id")
            if not isinstance(path_id, str) or not path_id.strip():
                raise PeppiLegacyContractError("course-unit child requires a path id")
        children.append((_PeppiChild(type=child_type, code=code), path_id))
    return tuple(children)


def _extract_modules(
    response: CapturedResponse, block: PeppiLegacySourceMetadata
) -> tuple[tuple[_PeppiModuleRecord, ...], tuple[tuple[str | None, ...], ...]]:
    """Extract one programme tree's module nodes and each node's child path ids.

    A module node becomes one record per pre-order visit, and the parallel tuple holds its direct
    children's course path ids so a rule reference can resolve the fetched course's official code.
    The tree root keeps its own published identity and every node below it carries the root's
    identity as its owning programme.
    """
    period = _request_period(response, block)
    body = _json_body(response)
    if not isinstance(body, list):
        raise PeppiLegacyContractError("accomplishment-plan response must be a list")
    if len(body) > 1:
        raise PeppiLegacyContractError("accomplishment-plan response must hold at most one tree")
    if not body:
        return (), ()
    root = body[0]
    if not isinstance(root, dict):
        raise PeppiLegacyContractError("accomplishment-plan tree must be an object")
    root_type = root.get("type")
    if not isinstance(root_type, str) or not root_type.strip():
        raise PeppiLegacyContractError("curriculum root type must be a non-blank string")
    root_code = root.get("code")
    if not isinstance(root_code, str) or not root_code.strip():
        raise PeppiLegacyContractError("curriculum root code must be a non-blank string")
    programme_group_id = f"{root_type}:{root_code}"
    records: list[_PeppiModuleRecord] = []
    child_paths: list[tuple[str | None, ...]] = []
    for position, node in enumerate(walk_nodes(root)):
        node_type = node.get("type")
        if not isinstance(node_type, str) or not node_type.strip():
            raise PeppiLegacyContractError("curriculum node type must be a non-blank string")
        if node_type == "COURSE_UNIT":
            continue
        module_type = _MODULE_TYPES.get(node_type)
        if module_type is None:
            raise PeppiLegacyContractError(f"unsupported Peppi curriculum node type {node_type}")
        code = node.get("code")
        if not isinstance(code, str) or not code.strip():
            raise PeppiLegacyContractError("module code must be a non-blank string")
        children = _module_children(node.get("children"))
        records.append(
            _PeppiModuleRecord(
                code=code,
                source_node_type=node_type,
                owning_group_id=None if position == 0 else programme_group_id,
                period=period,
                names=_localized(node.get("name")),
                optionality=node.get("optionality"),
                target_credits_min=_credit_bound(node.get("minCredits")),
                target_credits_max=_credit_bound(node.get("maxCredits")),
                children=tuple(child for child, _ in children),
            )
        )
        child_paths.append(tuple(path_id for _, path_id in children))
    return tuple(records), tuple(child_paths)


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
    children: tuple[NormalizedModuleRule, ...],
    *,
    credits_min: Decimal,
    credits_max: Decimal | None,
) -> NormalizedModuleRule:
    """Return one credit rule over the single selection pool of ``children``."""
    return NormalizedModuleRule(
        sibling_position=0,
        local_id=None,
        node_type=ModuleRuleType.CREDITS_RULE,
        require_min=None,
        require_max=None,
        all_mandatory=None,
        description=None,
        credits_min=credits_min,
        credits_max=credits_max,
        count_min=None,
        count_max=None,
        referenced_group_id=None,
        children=(_composite(children),),
    )


def _module_rule(
    record: _PeppiModuleRecord,
    child_paths: tuple[str | None, ...],
    course_codes: Mapping[tuple[str, str], str],
) -> NormalizedModuleRule | None:
    """Return the mapped module rule of one node, or ``None`` when it has no children.

    A module child references the same scoped group id its record carries, computed from the
    record's owning programme. A course-unit child references the official code of the detail
    fetched for its path id. When no detail was captured for that path the child references its
    published plan code, which the capture records as an unresolved course reference.
    """
    if not record.children:
        return None
    owner = record.owning_group_id or f"{record.source_node_type}:{record.code}"
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
                course_codes.get((record.period, child_paths[index]), child.code)
                if child.type == "COURSE_UNIT"
                else f"{owner}:{child.type}:{child.code}"
            ),
            children=(),
        )
        for index, child in enumerate(record.children)
    )
    if record.optionality == "ALL_OF":
        return _composite(children, all_mandatory=True, require_min=None)
    if record.optionality == "ONE_OF":
        return _composite(children, require_min=1, require_max=1)
    if (
        record.optionality in {"SOME_OF", "NUMBER_OF", "ECTS"}
        and record.target_credits_min is not None
    ):
        # A number-of node publishes no child count, only its own credit bounds, and some-of and
        # ects publish the same credit target. The published credit bounds determine one credit
        # rule over one selection pool.
        return _bounded(
            children,
            credits_min=record.target_credits_min,
            credits_max=record.target_credits_max,
        )
    return _composite(children)


def _snapshot(replayed: ReplayedSourceObject) -> SnapshotProvenance:
    return SnapshotProvenance(
        object_type=replayed.record.object_type,
        source_object_id=replayed.record.source_object_id,
        source_group_id=replayed.record.source_group_id,
        response_path=replayed.response_path,
        response_item_index=replayed.record.response_item_index,
        source_hash=replayed.record.source_hash,
    )


class PeppiLegacyParser:
    """Decode Peppi evidence and convert each source object into one neutral normalized record."""

    def __init__(
        self, block: PeppiLegacySourceMetadata, official_languages: tuple[str, ...]
    ) -> None:
        self._block = block
        self._official_languages = official_languages
        self._child_paths: dict[tuple[str, str], tuple[str | None, ...]] = {}
        self._course_code_by_path: dict[tuple[str, str], str] = {}

    @staticmethod
    def _module_group_id(record: _PeppiModuleRecord) -> str:
        """Return a module's scoped group id, which carries its owning programme when nested."""
        if record.owning_group_id is None:
            return f"{record.source_node_type}:{record.code}"
        return f"{record.owning_group_id}:{record.source_node_type}:{record.code}"

    @classmethod
    def _source_object_id(cls, record: _PeppiCourseRecord | _PeppiModuleRecord) -> str:
        if isinstance(record, _PeppiModuleRecord):
            return f"{cls._module_group_id(record)}|{record.period}"
        return f"{record.code}|{record.period}"

    def decode_objects(
        self,
        response: CapturedResponse,
        object_type: SourceObjectType,
        *,
        store: RawCaptureStore | None = None,
    ) -> tuple[DecodedSourceObject, ...]:
        """Decode one typed response's extracted records with their identity and source hash.

        Each record's period comes from its own request, so the shared store evidence is accepted
        and not read. The hash covers the extracted record, including a nested module's owning
        programme identity.
        """
        del store
        records: tuple[_PeppiCourseRecord | _PeppiModuleRecord, ...]
        if object_type is SourceObjectType.COURSE_UNIT:
            records = (_extract_course(response, self._block),)
        elif object_type is SourceObjectType.MODULE:
            records, child_paths = _extract_modules(response, self._block)
            for record, paths in zip(records, child_paths, strict=True):
                self._child_paths[(record.period, self._module_group_id(record))] = paths
        else:
            raise ValueError("unsupported captured source object type")
        return tuple(
            DecodedSourceObject(
                source_object_id=self._source_object_id(record),
                source_group_id=(
                    self._module_group_id(record)
                    if isinstance(record, _PeppiModuleRecord)
                    else record.code
                ),
                source_hash=json_sha256(record.model_dump(mode="json"), ensure_ascii=False),
                value=record.model_dump(mode="json"),
            )
            for record in records
        )

    def read_periods(self, store: RawCaptureStore) -> tuple[SourcePeriod, ...]:
        """Return every mapped published period and require the manifest selection to match."""
        self._child_paths = {}
        self._course_code_by_path = {}
        for response in store.manifest.responses:
            if response.object_type is not SourceObjectType.COURSE_UNIT:
                continue
            period = _request_period(
                CapturedResponse(response.request_path, response.request_params, b""), self._block
            )
            path_id = _course_path_id(response.request_path)
            for record in response.objects:
                self._course_code_by_path[(period, path_id)] = record.source_group_id
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
        """Validate and convert one replayed Peppi course or module record."""
        if replayed.record.object_type is SourceObjectType.COURSE_UNIT:
            return self._course_version(replayed, _PeppiCourseRecord.model_validate(replayed.value))
        if replayed.record.object_type is SourceObjectType.MODULE:
            return self._module_version(replayed, _PeppiModuleRecord.model_validate(replayed.value))
        raise ValueError("unsupported captured source object type")

    def _course_version(
        self, replayed: ReplayedSourceObject, record: _PeppiCourseRecord
    ) -> NormalizedCourseVersion:
        template = self._block.course_page_templates[self._official_languages[0]]
        return NormalizedCourseVersion(
            provenance=_snapshot(replayed),
            group_id=record.code,
            code=record.code,
            texts={OfficialTextField(field): values for field, values in record.texts.items()},
            credits_min=record.credits_min,
            credits_max=record.credits_max,
            official_url=template.format(
                code=quote(record.code, safe=""),
                id=quote(record.path_id, safe=""),
                period=record.period,
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
        self, replayed: ReplayedSourceObject, record: _PeppiModuleRecord
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
            rule=_module_rule(
                record,
                self._child_paths.get((record.period, self._module_group_id(record)), ()),
                self._course_code_by_path,
            ),
        )
