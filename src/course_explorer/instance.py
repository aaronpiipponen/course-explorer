"""The university metadata types and the loader for the tracked university table.

The loader reads ``universities.toml`` at the repository root once at package load. It converts
every entry into these types and validates the whole table, so a missing or malformed value stops
the import with one error naming the file, the entry key and the field.
"""

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from course_explorer import administration
from course_explorer.enums import LANGUAGE_CODE_PATTERN, OfficialTextField, TeachingTextOrigin

# The tracked shared data file behind every instance key, read once at package load.
_UNIVERSITIES_FILE = "universities.toml"
# Every entry names one source family, and its one source block carries the same name.
_SOURCE_FAMILIES = ("sisu", "peppi_legacy", "peppi_psg", "guide")
# The study-plan import script and format names a table entry may configure.
_STUDY_PLAN_SCRIPTS = ("sisu", "peppi")
# Each source family serves only its platform's saved page and capture formats, with a PDF for Sisu.
_FAMILY_STUDY_PLAN_FORMATS = {
    "sisu": ("sisu_page", "sisu_pdf", "sisu_capture"),
    "peppi_legacy": ("peppi_page", "peppi_capture"),
    "peppi_psg": ("peppi_page", "peppi_capture"),
    "guide": ("peppi_page", "peppi_capture"),
}
_STUDY_PLAN_CAPTURE_FORMATS = {"sisu": "sisu_capture", "peppi": "peppi_capture"}
# The discovery variants a source block may name.
_DISCOVERY_VARIANTS = (
    "organisation_navigation",
    "navigation_education_type",
    "organisation_education_type",
)
# The fetch variants a source block may name.
_FETCH_VARIANTS = ("plain_http", "headless_browser")
# The canonical language-code syntax is shared with the Pydantic language type and enforced here
# with a full match so a configured table entry cannot carry an unanchored variant.
_LANGUAGE_CODE = re.compile(LANGUAGE_CODE_PATTERN)
# One lowercase brand label: hyphen-separated alphanumeric segments with no leading or trailing
# hyphen, so a label renders safely as one path and title segment.
_DISPLAY_LABEL_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DISPLAY_LANGUAGES = ("fi", "en")
_FI_EN_OFFICIAL = ("fi", "en")
_FI_EN_TRANSLATION_PRIORITY = {"fi": ("en",), "en": ("fi",)}
# The inference prompt field defaults every table entry carries. The target describes the analysed
# course, and the pool describes every candidate in the same prompt.
_TARGET_PROMPT_FIELDS = (
    OfficialTextField.NAME,
    OfficialTextField.CONTENT,
    OfficialTextField.OUTCOMES,
    OfficialTextField.PREREQUISITE_TEXT,
)
_POOL_PROMPT_FIELDS = (
    OfficialTextField.NAME,
    OfficialTextField.CONTENT,
    OfficialTextField.OUTCOMES,
)
_OFFICIAL_FIELD_ORDER = tuple(OfficialTextField)
# The baseline preparation policy whose overrides are omitted from compatibility identities.
_BASELINE_INFERENCE_LANGUAGES = (
    ("fi", TeachingTextOrigin.OFFICIAL),
    ("en", TeachingTextOrigin.OFFICIAL),
    ("fi", TeachingTextOrigin.DERIVED),
    ("en", TeachingTextOrigin.DERIVED),
)


def _canonical_inference_pairs(
    pairs: tuple[tuple[str, TeachingTextOrigin], ...],
) -> tuple[tuple[str, str], ...]:
    """Return sorted JSON-ready language/origin pairs for one compatibility identity."""
    return tuple(sorted((language, origin.value) for language, origin in pairs))


def validate_prompt_fields(fields: tuple[OfficialTextField, ...], name: str) -> None:
    """Reject an empty, repeated or out-of-canonical-order prompt-field selection."""
    if not fields:
        raise ValueError(f"{name} must not be empty")
    for field in fields:
        if not isinstance(field, OfficialTextField):
            raise ValueError(f"{name} entries must be official text fields")
    if len(set(fields)) != len(fields):
        raise ValueError(f"{name} must not repeat a field")
    if tuple(sorted(fields, key=_OFFICIAL_FIELD_ORDER.index)) != fields:
        raise ValueError(f"{name} must follow canonical field order")


_ACADEMIC_YEAR = re.compile(r"([0-9]{4})-([0-9]{4})")


def academic_year_start(value: str) -> int:
    """Validate consecutive academic-year syntax and return its starting year."""
    match = _ACADEMIC_YEAR.fullmatch(value)
    if match is None or int(match.group(2)) != int(match.group(1)) + 1:
        raise ValueError("academic years must match YYYY-YYYY with consecutive years")
    return int(match.group(1))


def validate_display_label(value: str) -> str:
    """Return one valid lowercase display label or raise ``ValueError``."""
    if not isinstance(value, str) or _DISPLAY_LABEL_PATTERN.fullmatch(value) is None:
        raise ValueError("a display label must be lowercase hyphen-separated segments")
    return value


@dataclass(frozen=True)
class SisuSourceMetadata:
    """The Sisu source facts of one university: Kori base, root identity, first academic year and
    course-guide base."""

    kori_base_url: str
    university_id: str
    first_academic_year: str
    course_guide_base_url: str


@dataclass(frozen=True)
class PeppiPeriod:
    """One published Peppi curriculum period: its start year and its exclusive end year."""

    start_year: int
    end_year: int

    @property
    def id(self) -> str:
        """Return the period as its source identifier."""
        return f"{self.start_year}-{self.end_year}"


@dataclass(frozen=True)
class PeppiLegacySourceMetadata:
    """The Peppi legacy JSON facts of one university: API and guide origins, discovery variant,
    published curriculum periods and course-page templates indexed by language."""

    api_base_url: str
    guide_base_url: str
    discovery_variant: Literal[
        "organisation_navigation",
        "navigation_education_type",
        "organisation_education_type",
    ]
    periods: tuple[PeppiPeriod, ...]
    course_page_templates: dict[str, str]


@dataclass(frozen=True)
class PeppiPsgSourceMetadata:
    """The Peppi Solenovo PSG JSON facts of one university: API and guide origins, published
    academic years and course-page template."""

    api_base_url: str
    guide_base_url: str
    years: tuple[int, ...]
    course_page_template: str

    @property
    def periods(self) -> tuple[PeppiPeriod, ...]:
        """Return the published academic years as one-year curriculum periods."""
        return tuple(PeppiPeriod(year, year + 1) for year in self.years)


@dataclass(frozen=True)
class GuideSourceMetadata:
    """The server-rendered Peppi guide facts of one university: guide origin, fetch variant
    and published one-year periods."""

    guide_base_url: str
    fetch_variant: Literal["plain_http", "headless_browser"]
    periods: tuple[PeppiPeriod, ...]


@dataclass(frozen=True)
class UniversityMetadata:
    """The display names, display label, source block, language policy, prompt-field selection and
    study-plan import configuration of one university."""

    name_en: str
    name_fi: str
    display_label: str
    source_attribution_en: str
    source_attribution_fi: str
    source_family: Literal["sisu", "peppi_legacy", "peppi_psg", "guide"]
    sisu: SisuSourceMetadata | None
    peppi_legacy: PeppiLegacySourceMetadata | None
    peppi_psg: PeppiPsgSourceMetadata | None
    guide: GuideSourceMetadata | None
    official_languages: tuple[str, ...]
    display_fallback: dict[str, tuple[str, ...]]
    translation_priority: dict[str, tuple[str, ...]]
    inference_languages: tuple[tuple[str, TeachingTextOrigin], ...]
    inference_target_fields: tuple[OfficialTextField, ...] = _TARGET_PROMPT_FIELDS
    inference_pool_fields: tuple[OfficialTextField, ...] = _POOL_PROMPT_FIELDS
    study_plan_script: str | None = None
    study_plan_formats: tuple[str, ...] = ()

    @property
    def first_academic_year_start(self) -> int:
        """Return the one mapped first academic-year start of the source block."""
        if self.sisu is not None:
            return academic_year_start(self.sisu.first_academic_year)
        if self.peppi_legacy is not None:
            return self.peppi_legacy.periods[0].start_year
        if self.guide is not None:
            return self.guide.periods[0].start_year
        if self.peppi_psg is None:
            raise ValueError("the configured university has no readable source block")
        return self.peppi_psg.periods[0].start_year

    @property
    def course_guide_base_url(self) -> str:
        """Return the one mapped course-guide base of the source block."""
        if self.sisu is not None:
            return self.sisu.course_guide_base_url
        if self.peppi_legacy is not None:
            return self.peppi_legacy.guide_base_url
        if self.guide is not None:
            return self.guide.guide_base_url
        if self.peppi_psg is None:
            raise ValueError("the configured university has no readable source block")
        return self.peppi_psg.guide_base_url

    def __post_init__(self) -> None:
        """Reject a malformed configured source block, source family, study-plan import formats,
        language policy, prompt-field selection or display label."""
        validate_display_label(self.display_label)
        if self.source_family not in _SOURCE_FAMILIES:
            raise ValueError(f"source_family must be one of {', '.join(_SOURCE_FAMILIES)}")
        if (
            sum(
                block is not None
                for block in (self.sisu, self.peppi_legacy, self.peppi_psg, self.guide)
            )
            != 1
        ):
            raise ValueError("a university map entry must carry exactly one source block")
        if self.source_family == "sisu" and self.sisu is None:
            raise ValueError("the Sisu source family requires its Sisu source block")
        if self.source_family == "peppi_legacy" and self.peppi_legacy is None:
            raise ValueError("the Peppi legacy source family requires its Peppi source block")
        if self.source_family == "peppi_psg" and self.peppi_psg is None:
            raise ValueError("the Peppi PSG source family requires its Peppi PSG source block")
        if self.source_family == "guide" and self.guide is None:
            raise ValueError("the guide source family requires its guide source block")
        if self.study_plan_script is not None and self.study_plan_script not in _STUDY_PLAN_SCRIPTS:
            raise ValueError("study_plan_script must be one of sisu, peppi")
        if len(set(self.study_plan_formats)) != len(self.study_plan_formats):
            raise ValueError("study_plan_formats must not repeat a format")
        allowed_formats = _FAMILY_STUDY_PLAN_FORMATS[self.source_family]
        for format_id in self.study_plan_formats:
            if format_id not in allowed_formats:
                raise ValueError("study_plan_formats must hold only this source family's formats")
        if (
            self.study_plan_script is not None
            and _STUDY_PLAN_CAPTURE_FORMATS[self.study_plan_script] not in self.study_plan_formats
        ):
            raise ValueError("study_plan_script requires its platform's capture format")
        if self.study_plan_script is None and any(
            format_id.endswith("_capture") for format_id in self.study_plan_formats
        ):
            raise ValueError("a study-plan capture format requires its script")
        validate_prompt_fields(self.inference_target_fields, "inference_target_fields")
        validate_prompt_fields(self.inference_pool_fields, "inference_pool_fields")
        codes = self.official_languages
        if not codes:
            raise ValueError("official_languages must not be empty")
        if len(set(codes)) != len(codes):
            raise ValueError("official_languages must be distinct")
        for code in codes:
            if not isinstance(code, str) or _LANGUAGE_CODE.fullmatch(code) is None:
                raise ValueError(
                    "official language codes must be canonical lowercase language tags"
                )
        if set(self.display_fallback) != set(_DISPLAY_LANGUAGES):
            raise ValueError("display_fallback must hold exactly the fi and en sequences")
        official = set(codes)
        for sequence in self.display_fallback.values():
            if (
                len(sequence) != len(codes)
                or len(set(sequence)) != len(sequence)
                or set(sequence) != official
            ):
                raise ValueError(
                    "each display_fallback sequence must permute the official languages"
                )
        if not set(self.translation_priority) <= official:
            raise ValueError("translation targets must be configured official languages")
        for target, sources in self.translation_priority.items():
            if not sources:
                raise ValueError("each mapped translation target requires source languages")
            if len(set(sources)) != len(sources):
                raise ValueError("translation priority sources must be distinct")
            for source in sources:
                if source not in official:
                    raise ValueError("translation sources must be configured official languages")
                if source == target:
                    raise ValueError("a translation target cannot be its own source")
        if not self.inference_languages:
            raise ValueError("inference_languages must not be empty")
        seen: set[tuple[str, TeachingTextOrigin]] = set()
        for language, origin in self.inference_languages:
            if language not in official:
                raise ValueError("inference languages must be configured official languages")
            if not isinstance(origin, TeachingTextOrigin):
                raise ValueError("inference origins must be teaching-text origins")
            pair = (language, origin)
            if pair in seen:
                raise ValueError("inference language/origin pairs must be distinct")
            seen.add(pair)


def _load_universities(path: Path) -> dict[str, UniversityMetadata]:
    """Read and validate the tracked university table, preserving its entry order."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{_UNIVERSITIES_FILE} is not readable TOML: {error}") from error
    entries = document.get("universities")
    if not isinstance(entries, Mapping) or not entries:
        raise ValueError(f"{_UNIVERSITIES_FILE} must hold a non-empty universities table")
    return {key: _university_metadata(key, entry) for key, entry in entries.items()}


def _university_metadata(key: str, entry: object) -> UniversityMetadata:
    """Build one validated metadata entry from its table."""

    def fail(field: str, reason: str) -> ValueError:
        return ValueError(f"{_UNIVERSITIES_FILE} entry {key!r}: {field} {reason}")

    def text(table: object, field: str, path: str) -> str:
        value = table.get(field) if isinstance(table, Mapping) else None
        if not isinstance(value, str) or not value.strip():
            raise fail(path, "must be a nonblank string")
        return value

    def optional_text(table: object, field: str, path: str) -> str | None:
        if not isinstance(table, Mapping) or field not in table:
            return None
        return text(table, field, path)

    def study_plan_formats() -> tuple[str, ...]:
        if "study_plan_formats" not in entry:
            return ()
        value = entry["study_plan_formats"]
        if not isinstance(value, list):
            raise fail("study_plan_formats", "must be an array of import formats")
        if any(not isinstance(item, str) for item in value):
            raise fail("study_plan_formats", "must hold strings")
        return tuple(value)

    def strings(table: object, field: str, path: str) -> tuple[str, ...]:
        value = table.get(field) if isinstance(table, Mapping) else None
        if not isinstance(value, list) or not value:
            raise fail(path, "must be a non-empty array")
        if any(not isinstance(item, str) for item in value):
            raise fail(path, "must hold strings")
        return tuple(value)

    def periods(table: Mapping[str, object], path: str) -> tuple[PeppiPeriod, ...]:
        value = table.get("periods")
        if not isinstance(value, list) or not value:
            raise fail(path, "must be a non-empty array")
        for pair in value:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(type(year) is not int for year in pair)
                or pair[1] <= pair[0]
            ):
                raise fail(path, "must hold [start_year, end_year] pairs that end after they start")
        return tuple(PeppiPeriod(pair[0], pair[1]) for pair in value)

    def years(table: Mapping[str, object], path: str) -> tuple[int, ...]:
        value = table.get("years")
        if not isinstance(value, list) or not value:
            raise fail(path, "must be a non-empty array of published years")
        if any(type(year) is not int for year in value):
            raise fail(path, "must hold integers")
        return tuple(value)

    def sequences(table: object, path: str) -> dict[str, tuple[str, ...]]:
        if not isinstance(table, Mapping):
            raise fail(path, "must be a table")
        converted: dict[str, tuple[str, ...]] = {}
        for language, value in table.items():
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise fail(f"{path}.{language}", "must be an array of language codes")
            converted[language] = tuple(value)
        return converted

    def origin(value: str) -> TeachingTextOrigin:
        try:
            return TeachingTextOrigin(value)
        except ValueError as error:
            raise fail("inference_languages.origin", "must be `official` or `derived`") from error

    def prompt_fields(field: str) -> tuple[OfficialTextField, ...]:
        value = entry[field]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise fail(field, "must be an array of official text field names")
        try:
            return tuple(OfficialTextField(item) for item in value)
        except ValueError as error:
            raise fail(field, "must hold official text field names") from error

    if not isinstance(entry, Mapping):
        raise fail("entry", "must be a table")
    name_en = text(entry, "name_en", "name_en")
    name_fi = text(entry, "name_fi", "name_fi")
    display_label = text(entry, "display_label", "display_label")
    source_family = text(entry, "source_family", "source_family")
    source_attribution_en = text(entry, "source_attribution_en", "source_attribution_en")
    source_attribution_fi = text(entry, "source_attribution_fi", "source_attribution_fi")
    if source_family not in _SOURCE_FAMILIES:
        raise fail("source_family", f"must be one of {', '.join(_SOURCE_FAMILIES)}")
    blocks = [name for name in _SOURCE_FAMILIES if entry.get(name) is not None]
    if len(blocks) != 1:
        raise fail("source block", f"must be exactly one of {', '.join(_SOURCE_FAMILIES)}")
    block = entry[blocks[0]]
    if not isinstance(block, Mapping):
        raise fail(blocks[0], "must be a table")

    sisu: SisuSourceMetadata | None = None
    peppi_legacy: PeppiLegacySourceMetadata | None = None
    peppi_psg: PeppiPsgSourceMetadata | None = None
    guide: GuideSourceMetadata | None = None
    if blocks[0] == "sisu":
        first_academic_year = text(block, "first_academic_year", "sisu.first_academic_year")
        try:
            academic_year_start(first_academic_year)
        except ValueError as error:
            raise fail("sisu.first_academic_year", str(error)) from error
        sisu = SisuSourceMetadata(
            kori_base_url=text(block, "kori_base_url", "sisu.kori_base_url"),
            university_id=text(block, "university_id", "sisu.university_id"),
            first_academic_year=first_academic_year,
            course_guide_base_url=text(
                block, "course_guide_base_url", "sisu.course_guide_base_url"
            ),
        )
    elif blocks[0] == "peppi_legacy":
        discovery_variant = text(block, "discovery_variant", "peppi_legacy.discovery_variant")
        if discovery_variant not in _DISCOVERY_VARIANTS:
            raise fail(
                "peppi_legacy.discovery_variant",
                f"must be one of {', '.join(_DISCOVERY_VARIANTS)}",
            )
        templates = block.get("course_page_templates")
        if not isinstance(templates, Mapping) or not templates:
            raise fail("peppi_legacy.course_page_templates", "must be a non-empty table")
        for language, template in templates.items():
            if not isinstance(template, str) or not template.strip():
                raise fail(
                    f"peppi_legacy.course_page_templates.{language}",
                    "must be a nonblank template",
                )
        peppi_legacy = PeppiLegacySourceMetadata(
            api_base_url=text(block, "api_base_url", "peppi_legacy.api_base_url"),
            guide_base_url=text(block, "guide_base_url", "peppi_legacy.guide_base_url"),
            discovery_variant=discovery_variant,
            periods=periods(block, "peppi_legacy.periods"),
            course_page_templates=dict(templates),
        )
    elif blocks[0] == "peppi_psg":
        peppi_psg = PeppiPsgSourceMetadata(
            api_base_url=text(block, "api_base_url", "peppi_psg.api_base_url"),
            guide_base_url=text(block, "guide_base_url", "peppi_psg.guide_base_url"),
            years=years(block, "peppi_psg.years"),
            course_page_template=text(
                block, "course_page_template", "peppi_psg.course_page_template"
            ),
        )
    else:
        fetch_variant = text(block, "fetch_variant", "guide.fetch_variant")
        if fetch_variant not in _FETCH_VARIANTS:
            raise fail("guide.fetch_variant", f"must be one of {', '.join(_FETCH_VARIANTS)}")
        guide = GuideSourceMetadata(
            guide_base_url=text(block, "guide_base_url", "guide.guide_base_url"),
            fetch_variant=fetch_variant,
            periods=periods(block, "guide.periods"),
        )

    display_fallback = sequences(entry.get("display_fallback"), "display_fallback")
    translation_priority = sequences(entry.get("translation_priority"), "translation_priority")
    pairs = entry.get("inference_languages")
    if not isinstance(pairs, list) or not pairs:
        raise fail("inference_languages", "must be a non-empty array of language and origin tables")
    inference_languages = []
    for pair in pairs:
        inference_languages.append(
            (
                text(pair, "language", "inference_languages.language"),
                origin(text(pair, "origin", "inference_languages.origin")),
            )
        )
    official_languages = strings(entry, "official_languages", "official_languages")
    study_plan_script = optional_text(entry, "study_plan_script", "study_plan_script")
    plan_formats = study_plan_formats()
    prompt_selection = {
        field: prompt_fields(field)
        for field in ("inference_target_fields", "inference_pool_fields")
        if field in entry
    }

    try:
        return UniversityMetadata(
            name_en=name_en,
            name_fi=name_fi,
            display_label=display_label,
            source_attribution_en=source_attribution_en,
            source_attribution_fi=source_attribution_fi,
            source_family=source_family,
            sisu=sisu,
            peppi_legacy=peppi_legacy,
            peppi_psg=peppi_psg,
            guide=guide,
            official_languages=official_languages,
            display_fallback=display_fallback,
            translation_priority=translation_priority,
            inference_languages=tuple(inference_languages),
            study_plan_script=study_plan_script,
            study_plan_formats=plan_formats,
            **prompt_selection,
        )
    except ValueError as error:
        raise ValueError(f"{_UNIVERSITIES_FILE} entry {key!r}: {error}") from error


# Load at import time from the repository root, so the table is read and validated once per process.
UNIVERSITIES: dict[str, UniversityMetadata] = _load_universities(
    administration.repository_root() / _UNIVERSITIES_FILE
)


def official_display_names(
    names: Mapping[str, str],
    *,
    display_fallback: Mapping[str, Sequence[str]],
) -> dict[str, str | None]:
    """Resolve each interface language's displayed name from exact official-language values.

    Each result is the first nonblank exact value in that interface language's configured order,
    or ``None`` when no configured official language carries one. Returned bytes are never trimmed
    or rewritten.
    """
    return {
        language: next(
            (
                names[code]
                for code in display_fallback[language]
                if code in names and names[code].strip()
            ),
            None,
        )
        for language in _DISPLAY_LANGUAGES
    }


def language_policy_overrides(university: UniversityMetadata) -> dict[str, object]:
    """Return only the preparation-policy values that differ from the Finnish and English baseline.

    The baseline holds the Finnish and English official-language order, the fi-to-en and en-to-fi
    translation priority, and the four canonical inference pairs for Finnish and English official
    text and for Finnish and English derived text. The inference pairs are canonicalized before the
    comparison. A selection equal to the baseline produces an empty mapping, so its compatibility
    identity equals one built with no override.
    """
    overrides: dict[str, object] = {}
    if university.official_languages != _FI_EN_OFFICIAL:
        overrides["official_languages"] = university.official_languages
    if university.translation_priority != _FI_EN_TRANSLATION_PRIORITY:
        overrides["translation_priority"] = {
            target: sources for target, sources in university.translation_priority.items()
        }
    pairs = _canonical_inference_pairs(university.inference_languages)
    if pairs != _canonical_inference_pairs(_BASELINE_INFERENCE_LANGUAGES):
        overrides["inference_languages"] = pairs
    return overrides


def inference_language_policy_overrides(university: UniversityMetadata) -> dict[str, object]:
    """Return only the language-policy values owned by inference compatibility identities.

    The official-language list and the inference language/origin pairs change which cells enter a
    packet, so they belong to the cycle and retained-session fingerprints. Translation priority is
    owned by the translation fingerprint, and an effective-text change it causes enters the
    catalogue identity consumed by the inference cycle.
    """
    overrides = language_policy_overrides(university)
    return {
        key: overrides[key]
        for key in ("official_languages", "inference_languages")
        if key in overrides
    }


__all__ = [
    "UNIVERSITIES",
    "GuideSourceMetadata",
    "PeppiLegacySourceMetadata",
    "PeppiPeriod",
    "PeppiPsgSourceMetadata",
    "SisuSourceMetadata",
    "UniversityMetadata",
    "academic_year_start",
    "inference_language_policy_overrides",
    "language_policy_overrides",
    "official_display_names",
    "validate_display_label",
    "validate_prompt_fields",
]
