"""Store canonical language codes in derived translation and embedding rows.

Revision ID: 20261109_0026
Revises: 20261108_0025

The migration replaces the three fixed Finnish/English enum checks with canonical code checks and
widens their textual columns to ``TEXT``. Every row, vector, hash, marker and constraint unrelated to
those three language columns stays unchanged. A downgrade cannot represent arbitrary codes under the
fixed Finnish and English checks, so it refuses before any mutation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261109_0026"
down_revision: str | Sequence[str] | None = "20261108_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LANGUAGE_PATTERN = "^[a-z]{2,3}(-[a-z0-9]{2,8})*$"

_OLD_CHECKS = (
    ("derived_course_texts", "ck_derived_course_texts_derived_target_language"),
    ("derived_course_texts", "ck_derived_course_texts_derived_source_language"),
    ("course_embedding_chunks", "ck_course_embedding_chunks_derived_target_language"),
)

_NEW_CHECKS = (
    ("derived_course_texts", "language", "ck_derived_course_texts_language_canonical"),
    (
        "derived_course_texts",
        "source_language",
        "ck_derived_course_texts_source_language_canonical",
    ),
    ("course_embedding_chunks", "language", "ck_course_embedding_chunks_language_canonical"),
)


def upgrade() -> None:
    # `varchar` to `text` is binary-coercible, so the type change does not rewrite the table or
    # rebuild the HNSW index. It does clear the altered language column's statistics, and the
    # serving artifact build's nearest query reads them, so the final ANALYZE refreshes them.
    for table_name, constraint_name in _OLD_CHECKS:
        op.drop_constraint(op.f(constraint_name), table_name, type_="check")
    for table_name, column_name in (
        ("derived_course_texts", "language"),
        ("derived_course_texts", "source_language"),
        ("course_embedding_chunks", "language"),
    ):
        op.alter_column(
            table_name,
            column_name,
            type_=sa.Text(),
            existing_type=sa.String(length=2),
            existing_nullable=False,
        )
    for table_name, column_name, constraint_name in _NEW_CHECKS:
        op.create_check_constraint(
            op.f(constraint_name),
            table_name,
            f"{column_name} ~ '{_LANGUAGE_PATTERN}'",
        )
    op.execute("ANALYZE course_embedding_chunks")


def downgrade() -> None:
    raise RuntimeError(
        "revision 20261109_0026 cannot be downgraded because arbitrary language codes "
        "cannot be represented by the fixed Finnish/English checks"
    )
