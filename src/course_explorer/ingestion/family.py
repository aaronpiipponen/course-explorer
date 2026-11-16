"""Process composition of the configured university's curriculum source family."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from course_explorer.config import load_kori_settings, load_normalization_settings
from course_explorer.ingestion.guide import GuideSource
from course_explorer.ingestion.guide_browser import GuideBrowserTransport
from course_explorer.ingestion.guide_capture import GuideCapture
from course_explorer.ingestion.guide_normalization import GuideParser
from course_explorer.ingestion.kori import KoriSource
from course_explorer.ingestion.peppi_legacy import PeppiLegacySource
from course_explorer.ingestion.peppi_legacy_capture import PeppiLegacyCapture
from course_explorer.ingestion.peppi_legacy_normalization import PeppiLegacyParser
from course_explorer.ingestion.peppi_psg import PeppiPsgSource
from course_explorer.ingestion.peppi_psg_capture import PeppiPsgCapture
from course_explorer.ingestion.peppi_psg_normalization import PeppiPsgParser
from course_explorer.ingestion.sisu_capture import SisuCapture
from course_explorer.ingestion.sisu_normalization import SisuParser
from course_explorer.ingestion.source import CurriculumParser, CurriculumSource


def load_curriculum_parser() -> CurriculumParser:
    """Return the parser of the configured instance's source family."""
    settings = load_normalization_settings()
    university = settings.university
    if university.source_family == "sisu":
        return SisuParser(settings.course_guide_base_url)
    if university.source_family == "peppi_legacy":
        return PeppiLegacyParser(university.peppi_legacy, university.official_languages)
    if university.source_family == "peppi_psg":
        return PeppiPsgParser(university.peppi_psg, university.official_languages)
    if university.source_family == "guide":
        return GuideParser(university.guide, university.official_languages)
    raise ValueError("configured university source family has no curriculum parser")


@contextmanager
def open_curriculum_source(
    *, parser: CurriculumParser, before_request: Callable[[], bool] | None = None
) -> Iterator[CurriculumSource]:
    """Open the configured family's capture driver over its source transport.

    A family that reads discovery evidence during decoding shares one stateful parser instance
    between its driver and its store, so the caller supplies the instance it resolved.
    """
    settings = load_kori_settings()
    university = settings.university
    if university.source_family == "sisu":
        with KoriSource(settings, before_request=before_request) as source:
            yield SisuCapture(source)
        return
    if university.source_family == "peppi_legacy":
        block = university.peppi_legacy
        with PeppiLegacySource(settings, before_request=before_request) as source:
            yield PeppiLegacyCapture(source, block)
        return
    if university.source_family == "peppi_psg":
        block = university.peppi_psg
        with PeppiPsgSource(settings, before_request=before_request) as source:
            yield PeppiPsgCapture(source, block, parser)
        return
    if university.source_family == "guide":
        block = university.guide
        if block.fetch_variant == "headless_browser":
            with GuideBrowserTransport(settings, block) as page_transport:
                with GuideSource(
                    settings,
                    block,
                    page_transport=page_transport,
                    before_request=before_request,
                ) as source:
                    yield GuideCapture(source, block)
            return
        with GuideSource(settings, block, before_request=before_request) as source:
            yield GuideCapture(source, block)
        return
    raise ValueError("configured university source family has no curriculum source")
