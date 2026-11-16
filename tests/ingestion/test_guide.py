"""Exercise the Peppi guide parser, request seam and capture driver over source-shaped fixtures."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from course_explorer.config import KoriSettings
from course_explorer.enums import (
    IngestionRunStatus,
    ModuleRuleType,
    ModuleType,
    OfficialTextField,
    SourceObjectType,
)
from course_explorer.ingestion.capture import capture_curriculum
from course_explorer.ingestion.guide import (
    GuideContractError,
    GuideRequestError,
    GuideServerError,
    GuideSource,
    guide_path,
)
from course_explorer.ingestion.guide_capture import GuideCapture
from course_explorer.ingestion.guide_normalization import GuideParser
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.normalization import parse_capture, walk_normalized_rule
from course_explorer.ingestion.source import CapturedResponse, CurriculumPeriodRef
from course_explorer.instance import UNIVERSITIES, GuideSourceMetadata, PeppiPeriod
from tests.ingestion._capture_support import RecordingCapturePersistence
from tests.support import settings_factory

BLOCK_KEY = "diak"
PERIOD = "2026-2027"


def guide_block(
    periods: tuple[PeppiPeriod, ...] = (PeppiPeriod(2026, 2027),),
) -> GuideSourceMetadata:
    """Return one configured guide block replaced with the fixture periods."""
    block = UNIVERSITIES[BLOCK_KEY].guide
    assert block is not None
    return replace(block, periods=periods)


def guide_parser(block: GuideSourceMetadata) -> GuideParser:
    """Build the family parser of one replaced guide block."""
    return GuideParser(block, UNIVERSITIES[BLOCK_KEY].official_languages)


def root_html() -> str:
    """Build one root page linking three programme pages and one external site."""
    return """<html><body>
    <a href="https://outside.example/">Outside</a>
    <a href="/100/fi/200">Programme one</a>
    <a href="/101/fi/201">Programme two</a>
    <a href="/102/fi/202">Programme three</a>
    </body></html>"""


def programme_html(code: str | None, structure_path: str) -> str:
    """Build one programme page with a mapped year group and structure link."""
    code_paragraph = f"<p>Opetussuunnitelman tunnus ({code})</p>" if code is not None else ""
    return f"""<html><body>
    <h1>2026 Degree Programme in Social Services: track</h1>
    <h2>Lukuvuosi 2026 - 2027</h2>
    {code_paragraph}
    <a href="{structure_path}">Structure</a>
    </body></html>"""


def structure_html(
    root_name: str,
    course_path: str,
    *,
    metadata: bool = True,
    category: bool = False,
    nested: bool = False,
    programme_code: str | None = None,
    zero_count: bool = False,
    repeated_code_less: bool = False,
    zero_range: bool = False,
    own_code_titles: bool = False,
    credit_selection: bool = False,
) -> str:
    """Build one structure page carrying a module, course rows and programme metadata."""
    card = "<dl><dt>Laajuus</dt><dd>210 op</dd></dl>" if metadata else ""
    code_paragraph = (
        f"<p>Opetussuunitelman tunnus: {programme_code}</p>" if programme_code is not None else ""
    )
    course_dir = course_path.rsplit("/", 1)[0]
    extra_rows = ""
    if zero_count:
        extra_rows += f"""
    <tr class="level-2 CATEGORY"><td class="koodi"></td>
      <td class="nimi">Zero Count<p class="optionality">Valitaan (kpl): 0</p></td>
      <td class="laajuus"></td></tr>
    <tr class="level-3 COURSE_UNIT"><td class="koodi">CODE-4</td>
      <td class="nimi"><a href="{course_dir}/403">Course Four</a></td>
      <td class="laajuus">5 op</td></tr>"""
    if zero_range:
        extra_rows += f"""
    <tr class="level-2 CATEGORY"><td class="koodi"></td>
      <td class="nimi">Zero Range<p class="optionality">Valitaan (kpl): 0-5</p></td>
      <td class="laajuus"></td></tr>
    <tr class="level-3 COURSE_UNIT"><td class="koodi">CODE-7</td>
      <td class="nimi"><a href="{course_dir}/406">Course Seven</a></td>
      <td class="laajuus">5 op</td></tr>"""
    if credit_selection:
        extra_rows += f"""
    <tr class="level-2 CATEGORY"><td class="koodi"></td>
      <td class="nimi">Credit Choice<p class="optionality">Valitaan opintopisteitä</p></td>
      <td class="laajuus">10 op</td></tr>
    <tr class="level-3 COURSE_UNIT"><td class="koodi">CODE-4</td>
      <td class="nimi"><a href="{course_dir}/403">Course Four</a></td>
      <td class="laajuus">5 op</td></tr>"""
    if repeated_code_less:
        extra_rows += f"""
    <tr class="level-2 CATEGORY"><td class="koodi"></td>
      <td class="nimi">Valinnaiset opinnot<p class="optionality">Valitaan kaikki</p></td>
      <td class="laajuus"></td></tr>
    <tr class="level-3 COURSE_UNIT"><td class="koodi">CODE-5</td>
      <td class="nimi"><a href="{course_dir}/404">Course Five</a></td>
      <td class="laajuus">5 op</td></tr>
    <tr class="level-2 CATEGORY"><td class="koodi"></td>
      <td class="nimi">Valinnaiset opinnot<p class="optionality">Valitaan kaikki</p></td>
      <td class="laajuus"></td></tr>
    <tr class="level-3 COURSE_UNIT"><td class="koodi">CODE-6</td>
      <td class="nimi"><a href="{course_dir}/405">Course Six</a></td>
      <td class="laajuus">5 op</td></tr>"""
    if own_code_titles:
        extra_rows += f"""
    <tr class="level-2 COURSE_UNIT"><td class="koodi">N-KV598F</td>
      <td class="nimi"><a href="{course_dir}/407">Basics of Finnish N-KV598F</a></td>
      <td class="laajuus">5 op</td></tr>
    <tr class="level-2 COURSE_UNIT"><td class="koodi">EAE03-AEP</td>
      <td class="nimi"><a href="{course_dir}/408">Experiential learning and adventure education in pedagogical development EAE03-AEP</a></td>
      <td class="laajuus">5 op</td></tr>"""
    category_rows = ""
    if category:
        second_path = course_dir + "/401"
        category_rows = f"""
    <tr class="level-1 CATEGORY"><td class="koodi"></td>
      <td class="nimi">Shared Studies<p class="optionality">Valitaan kaikki</p></td>
      <td class="laajuus"></td></tr>
    <tr class="level-2 COURSE_UNIT"><td class="koodi">CODE-2</td>
      <td class="nimi"><a href="{second_path}">Course Two</a></td>
      <td class="laajuus">5 op</td></tr>"""
    if nested:
        optionality = "Välj alla"
        child_rows = f"""
    <tr class="level-2 STUDY_MODULE"><td class="koodi">SUB-1</td>
      <td class="nimi">Sub Module One<p class="optionality">Välj en</p></td>
      <td class="laajuus"></td></tr>
    <tr class="level-3 COURSE_UNIT"><td class="koodi">CODE-1</td>
      <td class="nimi"><a href="{course_path}">Course One</a></td>
      <td class="laajuus">5 op</td></tr>
    <tr class="level-2 CATEGORY"><td class="koodi"></td>
      <td class="nimi">Shared Studies<p class="optionality">Välj (st): 60-61</p></td>
      <td class="laajuus"></td></tr>
    <tr class="level-3 COURSE_UNIT"><td class="koodi">CODE-3</td>
      <td class="nimi"><a href="{course_dir}/402">Course Three</a></td>
      <td class="laajuus">5 op</td></tr>"""
    else:
        optionality = "Valitaan kaikki"
        child_rows = f"""
    <tr class="level-2 COURSE_UNIT"><td class="koodi">CODE-1</td>
      <td class="nimi"><a href="{course_path}">Course One</a></td>
      <td class="laajuus">5 op</td></tr>"""
    return f"""<html><body>
    <h1>2026 {root_name}</h1>
    {card}
    {code_paragraph}
    <table class="opas_ajoitusrakennetaulu">
    <tr><th>Tunnus</th><th>Opinnon nimi</th><th>Laajuus</th></tr>
    <tr class="level-1 STUDY_MODULE"><td class="koodi">MOD-1</td>
      <td class="nimi">Module One<p class="optionality">Valitaan yksi</p></td>
      <td class="laajuus">15 op</td></tr>
    <tr class="level-2 COURSE_UNIT"><td class="koodi">CODE-1</td>
      <td class="nimi"><a href="{course_path}">Course One</a></td>
      <td class="laajuus">5 op</td></tr>
    </table>
    <table class="opas_rakenne">
    <tr><th>Tunnus</th><th>Opinnon nimi</th><th>Laajuus</th></tr>
    <tr class="level-1 STUDY_MODULE"><td class="koodi">MOD-1</td>
      <td class="nimi">Module One<p class="optionality">{optionality}</p>
      <button>show</button></td><td class="laajuus">15 op</td></tr>{child_rows}{extra_rows}{category_rows}
    </table>
    </body></html>"""


def course_html(
    outcome: str = "Outcome text.", name: str = "Course One", *, title: str | None = None
) -> str:
    """Build one course detail page with the observed Finnish labels and one exact title text."""
    page_title = title if title is not None else f"{name} (5 op)"
    return f"""<html><head><title>{page_title}</title></head><body>
    <h1>{name} (5 op)</h1>
    <dl><dt>Laajuus</dt><dd>5 op</dd></dl>
    <h2>Opintojakson perustiedot</h2><p>Details.</p>
    <h2>Osaamistavoitteet</h2><p>{outcome}</p>
    <h2>Sisältö</h2><p>Content text.</p>
    <h2>Esitietovaatimukset</h2><p>Prereq text.</p>
    <h2>Lisätiedot</h2><p>Additional text.</p>
    </body></html>"""


def captured(path: str, html: str, object_type: SourceObjectType | None) -> tuple:
    """Build one captured response and its object type in the manifest tuple shape."""
    return (CapturedResponse(path, (), html.encode("utf-8")), object_type)


def completed_store(
    tmp_path: Path,
    block: GuideSourceMetadata,
    *,
    zero_range: bool = False,
    credit_selection: bool = False,
) -> RawCaptureStore:
    """Build one completed guide capture staged as the recorder commits it."""
    store = RawCaptureStore.create(
        tmp_path / "raw",
        run_id=uuid4(),
        started_at=datetime(2026, 11, 16, tzinfo=UTC),
        parser=guide_parser(block),
    )
    store.checkpoint_responses(
        (
            captured("/", root_html(), None),
            captured("/100/fi/200", programme_html("DSS26", "/100/fi/200/300"), None),
            captured("/101/fi/201", programme_html(None, "/101/fi/201/301"), None),
            captured("/102/fi/202", programme_html("EMPTY26", "/102/fi/202/302"), None),
        )
    )
    store.checkpoint_responses(
        (
            captured(
                "/100/fi/200/300",
                structure_html(
                    "Degree Programme in Social Services",
                    "/100/fi/200/400",
                    category=True,
                    nested=True,
                    zero_count=True,
                    repeated_code_less=True,
                    zero_range=zero_range,
                    credit_selection=credit_selection,
                ),
                SourceObjectType.MODULE,
            ),
            captured(
                "/101/fi/201/301",
                structure_html(
                    "Degree Programme in Business",
                    "/100/fi/200/400",
                    programme_code="2020SH",
                ),
                SourceObjectType.MODULE,
            ),
            captured(
                "/102/fi/202/302",
                """<html><body><h1>2026 Empty Programme</h1>
                <p>Opetusuunitelman tunnus: EMPTY26</p>
                <table class="opas_rakenne">
                <tr><th>Tunnus</th><th>Opinnon nimi</th><th>Laajuus</th></tr></table></body></html>""",
                SourceObjectType.MODULE,
            ),
        )
    )
    store.checkpoint_responses(
        (
            captured("/100/fi/200/400", course_html(), SourceObjectType.COURSE_UNIT),
            captured(
                "/100/fi/200/401",
                course_html(name="Course Two"),
                SourceObjectType.COURSE_UNIT,
            ),
            captured(
                "/100/fi/200/402",
                course_html(name="Course Three"),
                SourceObjectType.COURSE_UNIT,
            ),
            captured(
                "/100/fi/200/403",
                course_html(name="Course Four"),
                SourceObjectType.COURSE_UNIT,
            ),
            captured(
                "/100/fi/200/404",
                course_html(name="Course Five"),
                SourceObjectType.COURSE_UNIT,
            ),
            captured(
                "/100/fi/200/405",
                course_html(name="Course Six"),
                SourceObjectType.COURSE_UNIT,
            ),
            captured(
                "/100/fi/200/406",
                course_html(name="Course Seven"),
                SourceObjectType.COURSE_UNIT,
            ),
        )
    )
    store.set_periods((CurriculumPeriodRef(PERIOD, 2026),))
    store.mark_completed()
    return store


def test_guide_parser_maps_period_scoped_programme_modules_and_course_text(
    tmp_path: Path,
) -> None:
    """Map period-scoped modules and courses onto their records.

    The modules keep their full ancestor chains, repeated code-less sibling names keep their
    ordinals, a published zero-minimum range becomes the non-mandatory composite with a minimum of
    zero and the published maximum, a published zero count becomes the non-mandatory composite with a
    minimum of zero and no maximum, a published credit-selection phrase becomes a credits rule over
    one non-mandatory composite child, and a header-only structure table records no object. Course
    sections and hashes map onto each record, and a hyphenated vendor product code in a title does
    not become the official code. A structure path published under two periods and one official code
    carrying two listing facts in one period are refused.
    """
    block = guide_block()
    store = completed_store(tmp_path, block, zero_range=True, credit_selection=True)
    batch = parse_capture(store)
    courses = {course.provenance.source_object_id: course for course in batch.courses}
    modules = {module.provenance.source_object_id: module for module in batch.modules}

    assert set(modules) == {
        "PROGRAMME:DSS26|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:STUDY_MODULE:SUB-1|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Shared Studies|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Zero Count|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Zero Range|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Credit Choice|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Valinnaiset opinnot #1|2026-2027",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Valinnaiset opinnot #2|2026-2027",
        "PROGRAMME:DSS26:CATEGORY:Shared Studies|2026-2027",
        "PROGRAMME:2020SH|2026-2027",
        "PROGRAMME:2020SH:STUDY_MODULE:MOD-1|2026-2027",
    }
    assert set(courses) == {
        f"CODE-1|{PERIOD}",
        f"CODE-2|{PERIOD}",
        f"CODE-3|{PERIOD}",
        f"CODE-4|{PERIOD}",
        f"CODE-5|{PERIOD}",
        f"CODE-6|{PERIOD}",
        f"CODE-7|{PERIOD}",
    }
    assert batch.dropped_period_membership_count == 0

    # A published structure table with only its header row records a module response with no object
    # and adds no programme root.
    empty_structure = next(
        response
        for response in store.manifest.responses
        if response.request_path == "/102/fi/202/302"
    )
    assert empty_structure.object_type is SourceObjectType.MODULE
    assert empty_structure.objects == ()

    root = modules["PROGRAMME:DSS26|2026-2027"]
    assert root.group_id == "PROGRAMME:DSS26"
    assert root.code == "DSS26"
    assert root.module_type is ModuleType.DEGREE_PROGRAMME
    assert root.names == {"fi": "Degree Programme in Social Services"}
    assert (root.target_credits_min, root.target_credits_max) == (210, 210)
    assert root.rule is not None
    assert (root.rule.require_min, root.rule.require_max) == (0, None)
    assert [child.referenced_group_id for child in root.rule.children] == [
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1",
        "PROGRAMME:DSS26:CATEGORY:Shared Studies",
    ]

    module = modules["PROGRAMME:DSS26:STUDY_MODULE:MOD-1|2026-2027"]
    assert module.group_id == "PROGRAMME:DSS26:STUDY_MODULE:MOD-1"
    assert module.code == "MOD-1"
    assert module.module_type is ModuleType.STUDY_MODULE
    assert module.rule is not None
    assert module.rule.all_mandatory is True
    assert [child.referenced_group_id for child in module.rule.children] == [
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:STUDY_MODULE:SUB-1",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Shared Studies",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Zero Count",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Zero Range",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Credit Choice",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Valinnaiset opinnot #1",
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Valinnaiset opinnot #2",
    ]
    assert [child.node_type for child in module.rule.children] == [
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
    ]
    assert all(node.local_id is None for node in walk_normalized_rule(module.rule))

    # A nested module carries its parent's chain and keeps its own published code.
    submodule = modules["PROGRAMME:DSS26:STUDY_MODULE:MOD-1:STUDY_MODULE:SUB-1|2026-2027"]
    assert submodule.group_id == "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:STUDY_MODULE:SUB-1"
    assert submodule.code == "SUB-1"
    assert submodule.rule is not None
    assert (submodule.rule.require_min, submodule.rule.require_max) == (1, 1)
    assert [child.referenced_group_id for child in submodule.rule.children] == ["CODE-1"]
    assert submodule.rule.children[0].node_type is ModuleRuleType.COURSE_UNIT_RULE

    # The same code-less name under two different parents keeps two distinct full ancestor chains.
    nested_category = modules[
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Shared Studies|2026-2027"
    ]
    assert nested_category.code is None
    assert nested_category.names == {"fi": "Shared Studies"}
    assert nested_category.rule is not None
    assert nested_category.rule.node_type is ModuleRuleType.COURSE_UNIT_COUNT_RULE
    assert nested_category.rule.all_mandatory is None
    assert (nested_category.rule.count_min, nested_category.rule.count_max) == (60, 61)
    count_pool = nested_category.rule.children[0]
    assert count_pool.node_type is ModuleRuleType.COMPOSITE_RULE
    assert (count_pool.require_min, count_pool.require_max) == (0, None)
    assert count_pool.all_mandatory is False
    assert [child.referenced_group_id for child in count_pool.children] == ["CODE-3"]

    # A published zero count is the non-mandatory composite with a minimum of zero and no maximum.
    zero_count = modules["PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Zero Count|2026-2027"]
    assert zero_count.code is None
    assert zero_count.rule is not None
    assert zero_count.rule.node_type is ModuleRuleType.COMPOSITE_RULE
    assert zero_count.rule.all_mandatory is False
    assert (zero_count.rule.require_min, zero_count.rule.require_max) == (0, None)
    assert (zero_count.rule.count_min, zero_count.rule.count_max) == (None, None)
    assert (zero_count.rule.credits_min, zero_count.rule.credits_max) == (None, None)
    assert [child.referenced_group_id for child in zero_count.rule.children] == ["CODE-4"]

    # A zero-minimum range is the non-mandatory composite with a minimum of zero
    # and the published maximum.
    zero_range = modules["PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Zero Range|2026-2027"]
    assert zero_range.code is None
    assert zero_range.rule is not None
    assert zero_range.rule.node_type is ModuleRuleType.COMPOSITE_RULE
    assert zero_range.rule.all_mandatory is False
    assert (zero_range.rule.require_min, zero_range.rule.require_max) == (0, 5)
    assert (zero_range.rule.count_min, zero_range.rule.count_max) == (None, None)
    assert (zero_range.rule.credits_min, zero_range.rule.credits_max) == (None, None)
    assert [child.referenced_group_id for child in zero_range.rule.children] == ["CODE-7"]

    # A published credit-selection phrase is one credits rule over one non-mandatory child.
    credit_choice = modules["PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Credit Choice|2026-2027"]
    assert credit_choice.code is None
    assert credit_choice.rule is not None
    assert credit_choice.rule.node_type is ModuleRuleType.CREDITS_RULE
    assert credit_choice.rule.all_mandatory is None
    assert (credit_choice.rule.credits_min, credit_choice.rule.credits_max) == (10, 10)
    assert (credit_choice.rule.count_min, credit_choice.rule.count_max) == (None, None)
    credit_pool = credit_choice.rule.children[0]
    assert credit_pool.node_type is ModuleRuleType.COMPOSITE_RULE
    assert (credit_pool.require_min, credit_pool.require_max) == (0, None)
    assert credit_pool.all_mandatory is False
    assert [child.referenced_group_id for child in credit_pool.children] == ["CODE-4"]

    # Two code-less modules with one published name keep separate numbered identities.
    first_repeat = modules[
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Valinnaiset opinnot #1|2026-2027"
    ]
    second_repeat = modules[
        "PROGRAMME:DSS26:STUDY_MODULE:MOD-1:CATEGORY:Valinnaiset opinnot #2|2026-2027"
    ]
    for repeated, child_code in ((first_repeat, "CODE-5"), (second_repeat, "CODE-6")):
        assert repeated.code is None
        assert repeated.names == {"fi": "Valinnaiset opinnot"}
        assert repeated.rule is not None
        assert [child.referenced_group_id for child in repeated.rule.children] == [child_code]

    category = modules["PROGRAMME:DSS26:CATEGORY:Shared Studies|2026-2027"]
    assert category.group_id == "PROGRAMME:DSS26:CATEGORY:Shared Studies"
    assert category.code is None
    assert category.names == {"fi": "Shared Studies"}
    assert category.module_type is ModuleType.GROUPING_MODULE
    assert category.rule is not None
    assert category.rule.all_mandatory is True
    assert [child.referenced_group_id for child in category.rule.children] == ["CODE-2"]

    # A structure page publishes the programme code a year group does not label.
    labelled_root = modules["PROGRAMME:2020SH|2026-2027"]
    assert labelled_root.group_id == "PROGRAMME:2020SH"
    assert labelled_root.code == "2020SH"
    assert modules["PROGRAMME:2020SH:STUDY_MODULE:MOD-1|2026-2027"].group_id == (
        "PROGRAMME:2020SH:STUDY_MODULE:MOD-1"
    )

    course = courses[f"CODE-1|{PERIOD}"]
    assert course.group_id == "CODE-1"
    assert course.code == "CODE-1"
    assert course.curriculum_period_ids == (PERIOD,)
    assert (course.credits_min, course.credits_max) == (5, 5)
    assert course.official_url == "https://opinto-opas.diak.fi/100/fi/200/400"
    assert course.texts[OfficialTextField.NAME] == {"fi": "Course One"}
    assert course.texts[OfficialTextField.OUTCOMES] == {"fi": "Outcome text."}
    assert course.texts[OfficialTextField.CONTENT] == {"fi": "Content text."}
    assert course.texts[OfficialTextField.PREREQUISITE_TEXT] == {"fi": "Prereq text."}
    assert course.texts[OfficialTextField.ADDITIONAL_INFO] == {"fi": "Additional text."}

    # The two programmes that share one published module code keep separate scoped records.
    first_hash = modules["PROGRAMME:DSS26:STUDY_MODULE:MOD-1|2026-2027"].provenance.source_hash
    second_hash = modules["PROGRAMME:2020SH:STUDY_MODULE:MOD-1|2026-2027"].provenance.source_hash
    assert first_hash != second_hash

    parser = store.parser
    assert isinstance(parser, GuideParser)
    changed = parser.decode_objects(
        CapturedResponse("/100/fi/200/400", (), course_html("Changed outcome.").encode()),
        SourceObjectType.COURSE_UNIT,
        store=store,
    )
    assert changed[0].source_hash != course.provenance.source_hash

    # A title that names another code-shaped token still fails the page's own code claim.
    named_other = course_html().replace(
        "<title>Course One (5 op)</title>", "<title>Course One XX 99999</title>"
    )
    with pytest.raises(GuideContractError):
        parser.decode_objects(
            CapturedResponse("/100/fi/200/400", (), named_other.encode()),
            SourceObjectType.COURSE_UNIT,
            store=store,
        )

    # A hyphenated vendor product code in the title is not the guide's course code.
    vendor_title = course_html(name="Edistynyt Power BI: Microsoft PL-300")
    accepted = parser.decode_objects(
        CapturedResponse("/100/fi/200/400", (), vendor_title.encode()),
        SourceObjectType.COURSE_UNIT,
        store=store,
    )
    assert [record.source_object_id for record in accepted] == [f"CODE-1|{PERIOD}"]

    # A structure page without a programme metadata card stores no programme credit rather than
    # reading a table header row or its first data row.
    header_store = RawCaptureStore.create(
        tmp_path / "header-only",
        run_id=uuid4(),
        started_at=datetime(2026, 11, 16, tzinfo=UTC),
        parser=guide_parser(block),
    )
    header_store.checkpoint_responses(
        (
            captured(
                "/",
                """<html><body><a href="/100/fi/200">Programme</a></body></html>""",
                None,
            ),
            captured("/100/fi/200", programme_html("DSS26", "/100/fi/200/300"), None),
        )
    )
    header_store.checkpoint_responses(
        (
            captured(
                "/100/fi/200/300",
                structure_html(
                    "Degree Programme in Social Services",
                    "/100/fi/200/400",
                    metadata=False,
                    own_code_titles=True,
                ),
                SourceObjectType.MODULE,
            ),
        )
    )
    header_store.checkpoint_responses(
        (
            captured("/100/fi/200/400", course_html(), SourceObjectType.COURSE_UNIT),
            captured(
                "/100/fi/200/407",
                course_html(
                    name="Basics of Finnish N-KV598F",
                    title=("Basics of Finnish N-KV598F\n     |\n    Opinto-opas, TAMK"),
                ),
                SourceObjectType.COURSE_UNIT,
            ),
            captured(
                "/100/fi/200/408",
                course_html(
                    name=(
                        "Experiential learning and adventure education in pedagogical "
                        "development EAE03-AEP"
                    ),
                    title=(
                        "Experiential learning and adventure education in pedagogical "
                        "development EAE03-AEP\n     |\n    Opetussuunnitelmat, Humak"
                    ),
                ),
                SourceObjectType.COURSE_UNIT,
            ),
        )
    )
    header_store.set_periods((CurriculumPeriodRef(PERIOD, 2026),))
    header_store.mark_completed()
    header_batch = parse_capture(header_store)
    header_root = next(
        module
        for module in header_batch.modules
        if module.provenance.source_object_id == "PROGRAMME:DSS26|2026-2027"
    )
    assert (header_root.target_credits_min, header_root.target_credits_max) == (None, None)

    # Each served title's page-specific part before its site suffix ends with its listing code and
    # the whole title carries a guide-shaped token that does not end with that code, so acceptance
    # proves the own-code check runs before the token search.
    header_courses = {course.provenance.source_object_id: course for course in header_batch.courses}
    assert set(header_courses) == {
        f"CODE-1|{PERIOD}",
        f"N-KV598F|{PERIOD}",
        f"EAE03-AEP|{PERIOD}",
    }
    assert header_courses[f"N-KV598F|{PERIOD}"].texts[OfficialTextField.NAME] == {
        "fi": "Basics of Finnish N-KV598F"
    }
    assert header_courses[f"EAE03-AEP|{PERIOD}"].texts[OfficialTextField.NAME] == {
        "fi": "Experiential learning and adventure education in pedagogical development EAE03-AEP"
    }

    # Two published periods list one course with different structure-row credits and names. The
    # course page's h1 name wins for both periods, and each period keeps its own listing credits.
    two_period_block = guide_block((PeppiPeriod(2026, 2027), PeppiPeriod(2027, 2028)))

    def listing_structure(credits: str, name: str) -> str:
        return f"""<html><body>
        <h1>2026 Listing Programme</h1>
        <table><tr><th>Laajuus</th><th>Opinnon nimi</th></tr></table>
        <table class="opas_rakenne">
        <tr><th>Tunnus</th><th>Opinnon nimi</th><th>Laajuus</th></tr>
        <tr class="level-1 COURSE_UNIT"><td class="koodi">CODE-1</td>
          <td class="nimi"><a href="/100/fi/200/400">{name}</a></td>
          <td class="laajuus">{credits}</td></tr>
        </table></body></html>"""

    two_period_store = RawCaptureStore.create(
        tmp_path / "two-periods",
        run_id=uuid4(),
        started_at=datetime(2026, 11, 16, tzinfo=UTC),
        parser=guide_parser(two_period_block),
    )
    two_period_store.checkpoint_responses(
        (
            captured(
                "/",
                """<html><body><a href="/100/fi/200">Programme</a></body></html>""",
                None,
            ),
            captured(
                "/100/fi/200",
                """<html><body>
                <h2>Lukuvuosi 2026 - 2027</h2><a href="/100/fi/200/300">First</a>
                <h2>Lukuvuosi 2027 - 2028</h2><a href="/100/fi/200/301">Second</a>
                </body></html>""",
                None,
            ),
        )
    )
    two_period_store.checkpoint_responses(
        (
            captured(
                "/100/fi/200/300",
                listing_structure("5 op", "Listing 2026"),
                SourceObjectType.MODULE,
            ),
            captured(
                "/100/fi/200/301",
                listing_structure("8 op", "Listing 2027"),
                SourceObjectType.MODULE,
            ),
            captured(
                "/100/fi/200/400",
                """<html><head><title>Page Course One (5 op)</title></head><body>
                <h1>Page Course One (5 op)</h1>
                <h2>Opintojakson perustiedot</h2><p>Details.</p>
                <h2>Osaamistavoitteet</h2><p>Outcome.</p>
                </body></html>""",
                SourceObjectType.COURSE_UNIT,
            ),
        )
    )
    two_period_store.set_periods(
        (CurriculumPeriodRef("2026-2027", 2026), CurriculumPeriodRef("2027-2028", 2027))
    )
    two_period_store.mark_completed()
    two_period_batch = parse_capture(two_period_store)
    by_period = {
        course.curriculum_period_ids[0]: course
        for course in two_period_batch.courses
        if course.code == "CODE-1"
    }
    assert set(by_period) == {"2026-2027", "2027-2028"}
    for period in ("2026-2027", "2027-2028"):
        assert by_period[period].texts[OfficialTextField.NAME] == {"fi": "Page Course One"}
    assert (by_period["2026-2027"].credits_min, by_period["2026-2027"].credits_max) == (5, 5)
    assert (by_period["2027-2028"].credits_min, by_period["2027-2028"].credits_max) == (8, 8)
    # The Laajuus header cell labels its column, so the next header is not read as a credit value.
    listing_root = next(
        module
        for module in two_period_batch.modules
        if module.provenance.source_object_id == "PROGRAMME:200|2026-2027"
    )
    assert listing_root.names == {"fi": "Listing Programme"}
    assert (listing_root.target_credits_min, listing_root.target_credits_max) == (None, None)

    # A parsed capture cannot hold one structure path under two periods.
    duplicate_periods = RawCaptureStore.create(
        tmp_path / "duplicate-periods",
        run_id=uuid4(),
        started_at=datetime(2026, 11, 16, tzinfo=UTC),
        parser=guide_parser(guide_block((PeppiPeriod(2026, 2027), PeppiPeriod(2027, 2028)))),
    )
    duplicate_periods.checkpoint_responses(
        (
            captured("/", root_html(), None),
            captured(
                "/100/fi/200",
                """<html><body>
                <h2>Lukuvuosi 2026 - 2027</h2><a href="/100/fi/200/300">First</a>
                <h2>Lukuvuosi 2027 - 2028</h2><a href="/100/fi/200/300">Second</a>
                </body></html>""",
                None,
            ),
        )
    )
    duplicate_periods.set_periods(
        (CurriculumPeriodRef("2026-2027", 2026), CurriculumPeriodRef("2027-2028", 2027))
    )
    duplicate_periods.mark_completed()
    with pytest.raises(
        GuideContractError,
        match="structure path /100/fi/200/300 is published under 2026-2027 and 2027-2028",
    ):
        parse_capture(duplicate_periods)

    # One course code that carries two listing facts in one period is refused. The store decodes
    # every typed response as it checkpoints it, so the parser refuses the second structure page.
    listing_conflict = RawCaptureStore.create(
        tmp_path / "listing-conflict",
        run_id=uuid4(),
        started_at=datetime(2026, 11, 16, tzinfo=UTC),
        parser=guide_parser(guide_block()),
    )
    listing_conflict.checkpoint_responses(
        (
            captured("/", root_html(), None),
            captured("/100/fi/200", programme_html("DSS26", "/100/fi/200/300"), None),
        )
    )
    with pytest.raises(GuideContractError, match="one course code carries two listing facts"):
        listing_conflict.checkpoint_responses(
            (
                captured(
                    "/100/fi/200/300",
                    """<html><body><h1>2026 Degree Programme</h1>
                    <table class="opas_rakenne">
                    <tr><th>Tunnus</th><th>Opinnon nimi</th><th>Laajuus</th></tr>
                    <tr class="level-1 COURSE_UNIT"><td class="koodi">CODE-1</td>
                      <td class="nimi"><a href="/100/fi/200/400">Course One</a></td>
                      <td class="laajuus">5 op</td></tr>
                    <tr class="level-1 COURSE_UNIT"><td class="koodi">CODE-1</td>
                      <td class="nimi"><a href="/100/fi/200/400">Another Name</a></td>
                      <td class="laajuus">5 op</td></tr>
                    </table></body></html>""",
                    SourceObjectType.MODULE,
                ),
            )
        )


def html_response(value: str) -> httpx.Response:
    """Return one HTML mock response the guide client accepts."""
    return httpx.Response(200, text=value, headers={"content-type": "text/html; charset=utf-8"})


def route_handler(routes: dict[str, str | httpx.Response], calls: list[str]):
    """Answer the declared guide paths and fail on an unexpected request."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        value = routes.get(path)
        if value is None:
            raise AssertionError(f"unexpected guide request {path}")
        return value if isinstance(value, httpx.Response) else html_response(value)

    return handle


def two_year_routes() -> dict[str, str | httpx.Response]:
    """Return the root, programme, two structure and one course page of the fixture."""
    return {
        "/": """<html><body>
            <a href="/100/fi/200">Programme one</a>
            </body></html>""",
        "/100/fi/200": """<html><body>
            <h2>Lukuvuosi 2026 - 2027</h2><a href="/100/fi/200/300">First</a>
            <h2>Lukuvuosi 2027 - 2028</h2><a href="/100/fi/200/301">Second</a>
            </body></html>""",
        "/100/fi/200/300": structure_html("Degree Programme in Social Services", "/100/fi/200/400"),
        "/100/fi/200/301": structure_html("Degree Programme in Social Services", "/100/fi/200/400"),
        "/100/fi/200/400": course_html(),
    }


def capture_guide(
    tmp_path: Path,
    block: GuideSourceMetadata,
    routes: dict[str, str | httpx.Response],
    calls: list[str],
    *,
    on_boundary=None,
    before_request=None,
    name: str = "raw",
) -> tuple[RawCaptureStore, object]:
    """Capture one guide fixture through a mock transport."""
    store = RawCaptureStore.create(
        tmp_path / name,
        run_id=uuid4(),
        started_at=datetime(2026, 11, 16, tzinfo=UTC),
        parser=guide_parser(block),
    )
    settings = settings_factory(KoriSettings, INSTANCE_KEY=BLOCK_KEY)
    with GuideSource(
        settings,
        block,
        transport=httpx.MockTransport(route_handler(routes, calls)),
        sleep=lambda _: None,
        before_request=before_request,
    ) as source:
        report = capture_curriculum(
            GuideCapture(source, block),
            store,
            RecordingCapturePersistence(),
            checkpoint_response_count=1,
            on_boundary=on_boundary,
        )
    return store, report


def test_guide_capture_walks_period_year_groups_and_resumes(tmp_path: Path) -> None:
    """Walk period year groups, record request evidence and fetch each course page once.

    A paused capture reopens and fetches only the missing pages. An unmapped structure year, a
    structure page without a table and one structure path under two year groups fail the run. A
    structure page with no heading takes the year group's programme code as its root.
    """
    block = guide_block((PeppiPeriod(2026, 2027), PeppiPeriod(2027, 2028), PeppiPeriod(2028, 2029)))
    routes = two_year_routes()
    calls: list[str] = []
    store, report = capture_guide(tmp_path, block, routes, calls)

    assert report is not None and report.status is IngestionRunStatus.COMPLETED
    assert calls == [
        "/",
        "/100/fi/200",
        "/100/fi/200/300",
        "/100/fi/200/301",
        "/100/fi/200/400",
    ]
    assert calls.count("/100/fi/200/400") == 1
    # The guide publishes the first two mapped periods only, so the third mapped period is not a
    # covered period and the capture never selects it.
    assert [period.id for period in store.manifest.periods] == ["2026-2027", "2027-2028"]
    parser = guide_parser(block)
    assert [period.id for period in parser.read_periods(store)] == ["2026-2027", "2027-2028"]
    object_types = {
        (response.request_path, response.request_params): response.object_type
        for response in store.manifest.responses
    }
    assert object_types[("/", ())] is None
    assert object_types[("/100/fi/200", ())] is None
    assert object_types[("/100/fi/200/300", ())] is SourceObjectType.MODULE
    assert object_types[("/100/fi/200/400", ())] is SourceObjectType.COURSE_UNIT

    batch = parse_capture(store)
    assert {course.provenance.source_object_id for course in batch.courses} == {
        f"CODE-1|{period}" for period in ("2026-2027", "2027-2028")
    }
    assert [course.curriculum_period_ids for course in batch.courses] == [
        ("2026-2027",),
        ("2027-2028",),
    ]

    # The unlabelled programme's two year groups decode to one root group id and one version per
    # period.
    roots = {
        module.provenance.source_object_id: module
        for module in batch.modules
        if module.provenance.source_object_id.startswith("PROGRAMME:200|")
    }
    assert set(roots) == {"PROGRAMME:200|2026-2027", "PROGRAMME:200|2027-2028"}
    assert {module.group_id for module in roots.values()} == {"PROGRAMME:200"}

    # A paused capture is reopened, re-reads its recorded pages and fetches only missing pages.
    resumed_calls: list[str] = []
    paused = False

    def pause_after_first_structure(stage: str, completed: int, total: int | None) -> bool:
        nonlocal paused
        if paused:
            return True
        # Root, one programme page and one structure page are committed at this boundary.
        if completed >= 3:
            paused = True
            return False
        return True

    paused_store, paused_report = capture_guide(
        tmp_path,
        block,
        routes,
        resumed_calls,
        on_boundary=pause_after_first_structure,
        name="paused",
    )
    assert paused_report is None
    assert paused_store.manifest.status is IngestionRunStatus.RUNNING
    assert [period.id for period in paused_store.manifest.periods] == ["2026-2027", "2027-2028"]

    # A run paused before discovery records no period and does not call set_periods.
    early_store, early_report = capture_guide(
        tmp_path,
        block,
        two_year_routes(),
        [],
        on_boundary=lambda *_: False,
        name="early",
    )
    assert early_report is None
    assert early_store.manifest.status is IngestionRunStatus.RUNNING
    assert early_store.manifest.periods == ()
    reopened = RawCaptureStore.open(
        tmp_path / "paused",
        paused_store.manifest_path,
        parser=guide_parser(block),
    )
    resumed_calls.clear()
    settings = settings_factory(KoriSettings, INSTANCE_KEY=BLOCK_KEY)
    with GuideSource(
        settings,
        block,
        transport=httpx.MockTransport(route_handler(routes, resumed_calls)),
        sleep=lambda _: None,
    ) as source:
        report = capture_curriculum(
            GuideCapture(source, block),
            reopened,
            RecordingCapturePersistence(),
            checkpoint_response_count=1,
        )
    assert report is not None and report.status is IngestionRunStatus.COMPLETED
    assert resumed_calls == ["/100/fi/200/301", "/100/fi/200/400"]

    # A published structure year outside the map fails the run.
    outside = two_year_routes()
    outside["/100/fi/200"] = """<html><body>
        <h2>Lukuvuosi 2029 - 2030</h2><a href="/100/fi/200/300">Third</a>
        </body></html>"""
    with pytest.raises(
        GuideContractError,
        match="published structure year 2029-2030 for /100/fi/200/300 is not a mapped period",
    ):
        capture_guide(tmp_path, block, outside, [], name="outside")

    # A structure page with no published structure table fails the run.
    not_a_structure = two_year_routes()
    not_a_structure["/100/fi/200/300"] = "<html><body><p>No table.</p></body></html>"
    with pytest.raises(GuideContractError):
        capture_guide(tmp_path, block, not_a_structure, [], name="not-a-structure")

    # One structure path published under two year groups fails the run.
    duplicate = two_year_routes()
    duplicate["/100/fi/200"] = """<html><body>
        <h2>Lukuvuosi 2026 - 2027</h2><a href="/100/fi/200/300">First</a>
        <h2>Lukuvuosi 2027 - 2028</h2><a href="/100/fi/200/300">Again</a>
        </body></html>"""
    with pytest.raises(
        GuideContractError,
        match="structure path /100/fi/200/300 is published under 2026-2027 and 2027-2028",
    ):
        capture_guide(tmp_path, block, duplicate, [], name="duplicate-path")

    # A structure page with no heading takes the year group's published programme code as root.
    no_heading = two_year_routes()
    no_heading["/100/fi/200"] = programme_html("2020SH", "/100/fi/200/300")
    no_heading["/100/fi/200/300"] = structure_html(
        "Degree Programme in Social Services", "/100/fi/200/400"
    ).replace("    <h1>2026 Degree Programme in Social Services</h1>\n", "")
    heading_store, _ = capture_guide(tmp_path, block, no_heading, [], name="no-heading")
    heading_root = next(
        module
        for module in parse_capture(heading_store).modules
        if module.provenance.source_object_id == f"PROGRAMME:2020SH|{PERIOD}"
    )
    assert heading_root.names == {"fi": "2020SH"}

    # A course detail whose only recognized label is a table cell is parsed, not unresolved.
    table_detail = two_year_routes()
    table_detail["/100/fi/200/400"] = """<html><head><title>CODE-1</title></head><body>
        <table><tr><td>Opintojakson perustiedot</td><td>Details.</td></tr></table>
        </body></html>"""
    table_store, _ = capture_guide(tmp_path, block, table_detail, [], name="table-detail")
    table_batch = parse_capture(table_store)
    assert f"CODE-1|{PERIOD}" in {
        course.provenance.source_object_id for course in table_batch.courses
    }
    assert table_store.manifest.unresolved_course_group_ids == ()


def test_guide_capture_records_a_persistent_course_500_as_unresolved(tmp_path: Path) -> None:
    """A persistent course 500 and a 200 rendered server-error page are recorded once unresolved."""
    block = guide_block()
    routes: dict[str, str | httpx.Response] = {
        "/": """<html><body><a href="/100/fi/200">Programme one</a></body></html>""",
        "/100/fi/200": """<html><body>
            <h2>Lukuvuosi 2026 - 2027</h2><a href="/100/fi/200/300">Structure</a>
            </body></html>""",
        "/100/fi/200/300": """<html><body><h1>2026 Degree Programme</h1>
            <table class="opas_rakenne">
            <tr><th>Tunnus</th><th>Opinnon nimi</th><th>Laajuus</th></tr>
            <tr class="level-1 COURSE_UNIT"><td class="koodi">CODE-1</td>
              <td class="nimi"><a href="/100/fi/200/400">Course One</a></td>
              <td class="laajuus">5 op</td></tr>
            <tr class="level-1 COURSE_UNIT"><td class="koodi">CODE-2</td>
              <td class="nimi"><a href="/100/fi/200/401">Course Two</a></td>
              <td class="laajuus">5 op</td></tr>
            <tr class="level-1 COURSE_UNIT"><td class="koodi">CODE-3</td>
              <td class="nimi"><a href="/100/fi/200/402">Course Three</a></td>
              <td class="laajuus">5 op</td></tr>
            </table></body></html>""",
        "/100/fi/200/400": course_html(),
        "/100/fi/200/401": httpx.Response(500, text="server error"),
        "/100/fi/200/402": """<html><head><title>Server Error</title></head>
            <body><h1>500</h1><p>Server Error</p></body></html>""",
    }
    calls: list[str] = []
    store, report = capture_guide(tmp_path, block, routes, calls)

    assert report is not None and report.status is IngestionRunStatus.COMPLETED
    assert store.manifest.unresolved_course_group_ids == ("CODE-2", "CODE-3")
    assert calls.count("/100/fi/200/401") >= 2
    batch = parse_capture(store)
    assert {course.provenance.source_object_id for course in batch.courses} == {f"CODE-1|{PERIOD}"}
    root = next(
        module
        for module in batch.modules
        if module.provenance.source_object_id == f"PROGRAMME:200|{PERIOD}"
    )
    assert root.rule is not None
    assert [child.referenced_group_id for child in root.rule.children] == [
        "CODE-1",
        "CODE-2",
        "CODE-3",
    ]
    assert all(
        response.request_path not in {"/100/fi/200/401", "/100/fi/200/402"}
        for response in store.manifest.responses
    )

    # A retry refused by the cooperative boundary keeps the ordinary request error and does not
    # become an unresolved reference even though the page answered 500.
    with pytest.raises(GuideRequestError):
        capture_guide(
            tmp_path,
            block,
            routes,
            [],
            before_request=lambda: False,
            name="refused-retry",
        )

    # A root page that keeps answering 500 fails the run instead of becoming unresolved.
    with pytest.raises(GuideServerError):
        capture_guide(tmp_path, block, {"/": httpx.Response(500)}, [], name="root-500")


def test_guide_client_refuses_unsafe_urls_sends_its_identity_and_spaces_requests() -> None:
    """The guide client refuses unsafe addresses, sends its identity, follows no redirect and
    spaces its requests."""
    block = guide_block()
    settings = settings_factory(KoriSettings, INSTANCE_KEY=BLOCK_KEY)
    for unsafe in (
        "https://user:password@opinto-opas.diak.fi/100/fi/200",
        "https://opinto-opas.diak.fi/100/fi/200?language=fi",
        "https://another.example/100/fi/200",
    ):
        with pytest.raises(GuideContractError):
            guide_path(block.guide_base_url, unsafe)

    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    clock = iter((100.0, 100.1, 100.2, 100.3))

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return html_response(f"<html><body>{request.url.path}</body></html>")

    with GuideSource(
        settings,
        block,
        transport=httpx.MockTransport(handle),
        sleep=sleeps.append,
        monotonic=lambda: next(clock),
    ) as source:
        first = source.get_page("/first")
        second = source.get_page("/second")

    assert first.value == "<html><body>/first</body></html>"
    assert second.value == "<html><body>/second</body></html>"
    assert [request.url.path for request in requests] == ["/first", "/second"]
    assert {request.headers["user-agent"] for request in requests} == {"course-explorer-guide/1"}
    assert sleeps == [pytest.approx(settings.kori_request_interval_seconds - 0.1)]

    redirects: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        redirects.append(request)
        return httpx.Response(302, headers={"location": "/"})

    with GuideSource(
        settings, block, transport=httpx.MockTransport(redirect), sleep=lambda _: None
    ) as source:
        with pytest.raises(GuideRequestError):
            source.get_page("/redirected")
    assert [request.url.path for request in redirects] == ["/redirected"]
