"""Store official course and module text per version, field and language.

Revision ID: 20261108_0025
Revises: 20261106_0024

The migration creates the two official text cell tables, copies every retained non-null value from
the fixed name and course-text columns byte for byte, and only then drops those columns. Empty
strings and equal language values are copied too. No source hash, derived row, version identity or
preparation state is rewritten. A downgrade cannot express arbitrary-language cells in fixed
columns, so it refuses before any mutation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261108_0025"
down_revision: str | Sequence[str] | None = "20261106_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COURSE_FIELDS = ("name", "content", "outcomes", "prerequisite_text", "additional_info")
_OLD_COURSE_FIELDS = {
    "name": ("name_fi", "name_en"),
    "content": ("content_fi", "content_en"),
    "outcomes": ("outcomes_fi", "outcomes_en"),
    "prerequisite_text": ("prerequisite_text_fi", "prerequisite_text_en"),
    "additional_info": ("additional_info_fi", "additional_info_en"),
}


def _create_tables() -> None:
    op.create_table(
        "official_course_texts",
        sa.Column("course_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "field",
            sa.Enum(
                *_COURSE_FIELDS,
                name="official_text_field",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "language ~ '^[a-z]{2,3}(-[a-z0-9]{2,8})*$'",
            name=op.f("ck_official_course_texts_language_canonical"),
        ),
        sa.ForeignKeyConstraint(
            ["course_version_id"],
            ["course_versions.id"],
            name=op.f("fk_official_course_texts_course_version_id_course_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "course_version_id", "field", "language", name=op.f("pk_official_course_texts")
        ),
    )
    op.create_table(
        "official_module_texts",
        sa.Column("module_version_id", sa.UUID(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint("field = 'name'", name=op.f("ck_official_module_texts_field_name_only")),
        sa.CheckConstraint(
            "language ~ '^[a-z]{2,3}(-[a-z0-9]{2,8})*$'",
            name=op.f("ck_official_module_texts_language_canonical"),
        ),
        sa.ForeignKeyConstraint(
            ["module_version_id"],
            ["module_versions.id"],
            name=op.f("fk_official_module_texts_module_version_id_module_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "module_version_id", "field", "language", name=op.f("pk_official_module_texts")
        ),
    )


def _copy_cells() -> None:
    for field, (column_fi, column_en) in _OLD_COURSE_FIELDS.items():
        table = "course_versions" if field == "name" else "course_texts"
        identifier = "id" if field == "name" else "course_version_id"
        for language, column in (("fi", column_fi), ("en", column_en)):
            op.execute(
                sa.text(
                    f"INSERT INTO official_course_texts "
                    f"(course_version_id, field, language, text) "
                    f"SELECT {identifier}, '{field}', '{language}', {column} "
                    f"FROM {table} WHERE {column} IS NOT NULL"
                )
            )
    for language, column in (("fi", "name_fi"), ("en", "name_en")):
        op.execute(
            sa.text(
                f"INSERT INTO official_module_texts "
                f"(module_version_id, field, language, text) "
                f"SELECT id, 'name', '{language}', {column} "
                f"FROM module_versions WHERE {column} IS NOT NULL"
            )
        )


def upgrade() -> None:
    """Create the cell tables, copy every retained value, then drop the fixed columns."""
    _create_tables()
    _copy_cells()
    op.drop_table("course_texts")
    for column in ("name_fi", "name_en"):
        op.drop_column("course_versions", column)
    for column in ("name_fi", "name_en"):
        op.drop_column("module_versions", column)


def downgrade() -> None:
    """Refuse: fixed columns cannot losslessly express arbitrary-language cells."""
    raise RuntimeError(
        "cannot restore fixed official text columns from arbitrary-language keyed cells"
    )
