"""Give source identities neutral names and make source credits and state optional.

Revision ID: 20261106_0024
Revises: 20261103_0023

The migration renames the four exact source-version identity columns and their source-name
constraints, and relaxes the course minimum-credit and both exact-version document-state columns so
a source that publishes no value stores none. Values are not rewritten. A downgrade refuses while a
relaxed column holds a null, so it never invents a source value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261106_0024"
down_revision: str | Sequence[str] | None = "20261103_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAMES = (
    ("course_versions", "sisu_course_unit_id", "source_version_id"),
    ("course_embedding_centroids", "sisu_course_unit_id", "source_version_id"),
    ("course_embedding_search_groups", "sisu_course_unit_id", "source_version_id"),
    ("module_versions", "sisu_module_id", "source_version_id"),
)

_CONSTRAINT_RENAMES = (
    (
        "course_versions",
        "ck_course_versions_sisu_course_unit_id_not_blank",
        "ck_course_versions_source_version_id_not_blank",
    ),
    (
        "course_versions",
        "uq_course_versions_sisu_course_unit_id",
        "uq_course_versions_source_version_id",
    ),
    (
        "course_embedding_centroids",
        "ck_course_embedding_centroids_ck_course_embedding_centr_1920",
        "ck_course_embedding_centroids_source_version_id_not_blank",
    ),
    (
        "module_versions",
        "ck_module_versions_sisu_module_id_not_blank",
        "ck_module_versions_source_version_id_not_blank",
    ),
    (
        "module_versions",
        "uq_module_versions_sisu_module_id",
        "uq_module_versions_source_version_id",
    ),
)


def upgrade() -> None:
    """Rename the neutral identity columns and relax the three optional source fields."""
    for table, old_name, new_name in _RENAMES:
        op.alter_column(table, old_name, new_column_name=new_name)
    for table, old_name, new_name in _CONSTRAINT_RENAMES:
        op.execute(sa.text(f'ALTER TABLE {table} RENAME CONSTRAINT "{old_name}" TO "{new_name}"'))
    op.alter_column(
        "course_versions",
        "credits_min",
        existing_type=sa.Numeric(precision=6, scale=2),
        nullable=True,
    )
    op.alter_column(
        "course_versions",
        "document_state",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.alter_column(
        "module_versions",
        "document_state",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    """Refuse retained nulls, then restore the source names and required columns."""
    connection = op.get_bind()
    null_count = connection.execute(
        sa.text(
            "SELECT count(*) FROM course_versions "
            "WHERE credits_min IS NULL OR document_state IS NULL "
            "UNION ALL SELECT count(*) FROM module_versions WHERE document_state IS NULL"
        )
    ).scalars()
    if any(count for count in null_count):
        raise RuntimeError(
            "cannot restore required source fields while stored null credits or document state exist"
        )
    op.alter_column(
        "module_versions",
        "document_state",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "course_versions",
        "document_state",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "course_versions",
        "credits_min",
        existing_type=sa.Numeric(precision=6, scale=2),
        nullable=False,
    )
    for table, old_name, new_name in _CONSTRAINT_RENAMES:
        op.execute(sa.text(f'ALTER TABLE {table} RENAME CONSTRAINT "{new_name}" TO "{old_name}"'))
    for table, old_name, new_name in _RENAMES:
        op.alter_column(table, new_name, new_column_name=old_name)
