"""Contract proof for the shared centroid and descriptive-chunk store."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from course_explorer.constants import COURSE_EMBEDDING_DIMENSION
from course_explorer.embeddings.centroids import CourseCentroid
from course_explorer.embeddings.read import TeachingEmbeddingChunk
from course_explorer.embeddings.shared_store import SharedServingStore
from course_explorer.enums import TeachingTextField, TeachingTextOrigin


def _centroid(group_id: str) -> CourseCentroid:
    return CourseCentroid(
        group_id=group_id,
        source_version_id=f"{group_id}-version",
        representative_source_hash="a" * 64,
        chunk_count=1,
        centroid=(0.0,) * COURSE_EMBEDDING_DIMENSION,
    )


def test_shared_store_rejects_another_dataset_identity_or_academic_year(
    tmp_path: Path,
) -> None:
    """A store opened under another key or academic year is refused."""
    root = tmp_path / "store"
    SharedServingStore.build(
        root,
        dataset_key="key-one",
        academic_year_start=2026,
        centroids=(_centroid("course-a"),),
        chunks=(),
    )

    opened = SharedServingStore.open(root, dataset_key="key-one", academic_year_start=2026)
    assert opened.centroid_index == {("course-a", "course-a-version", "a" * 64): 0}

    with pytest.raises(ValueError, match="scripts/admin.py serving-artifacts"):
        SharedServingStore.open(root, dataset_key="key-two", academic_year_start=2026)
    with pytest.raises(ValueError, match="scripts/admin.py serving-artifacts"):
        SharedServingStore.open(root, dataset_key="key-one", academic_year_start=2027)


def test_shared_chunks_round_trip_arbitrary_language_codes(tmp_path: Path) -> None:
    """A Swedish code and a longer canonical code round-trip through build, open and chunks_for."""
    source_version_id = "course-version-1"
    source_hash = "a" * 64
    vector = (0.5,) + (0.0,) * (COURSE_EMBEDDING_DIMENSION - 1)
    chunks = (
        TeachingEmbeddingChunk(
            chunk_id=uuid.uuid4(),
            group_id="course-a",
            field=TeachingTextField.CONTENT,
            language="sv",
            chunk_index=0,
            text_origin=TeachingTextOrigin.OFFICIAL,
            character_count=12,
            embedding=vector,
            source_version_id=source_version_id,
            representative_source_hash=source_hash,
        ),
        TeachingEmbeddingChunk(
            chunk_id=uuid.uuid4(),
            group_id="course-a",
            field=TeachingTextField.OUTCOMES,
            language="zh-hans",
            chunk_index=2,
            text_origin=TeachingTextOrigin.DERIVED,
            character_count=7,
            embedding=vector,
            source_version_id=source_version_id,
            representative_source_hash=source_hash,
        ),
    )
    root = tmp_path / "store"
    SharedServingStore.build(
        root,
        dataset_key="key-one",
        academic_year_start=2026,
        centroids=(),
        chunks=chunks,
    )
    assert (root / "chunks.f64.bin").read_bytes()[:8] == b"CSTCHNK2"

    opened = SharedServingStore.open(root, dataset_key="key-one", academic_year_start=2026)
    assert opened.chunk_languages == ("sv", "zh-hans")
    represented = (
        SimpleNamespace(
            summary=SimpleNamespace(group_id="course-a"),
            source_version_id=source_version_id,
            source_hash=source_hash,
        ),
    )
    decoded = opened.chunks_for(represented)
    assert [
        (
            chunk.chunk_id,
            chunk.language,
            chunk.text_origin,
            chunk.chunk_index,
            chunk.character_count,
        )
        for chunk in decoded
    ] == [
        (chunks[0].chunk_id, "sv", TeachingTextOrigin.OFFICIAL, 0, 12),
        (chunks[1].chunk_id, "zh-hans", TeachingTextOrigin.DERIVED, 2, 7),
    ]
    assert all(tuple(chunk.embedding) == vector for chunk in decoded)

    # A foreign magic fails with rebuild guidance.
    raw = bytearray((root / "chunks.f64.bin").read_bytes())
    raw[:8] = b"CSTCHNK1"
    (root / "chunks.f64.bin").write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="scripts/admin.py serving-artifacts"):
        SharedServingStore.open(root, dataset_key="key-one", academic_year_start=2026)
