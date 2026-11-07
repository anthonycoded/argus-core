"""Initial Global Chess schema, independent of Core and historical databases.

Revision ID: e1b7c42a9d60
Revises: None

Integer primary keys deliberately compile to PostgreSQL SERIAL, matching the
existing model/DDL contract. ORM-side defaults remain in the application models;
the historical schema has no server defaults to add here.
"""

from alembic import op
import sqlalchemy as sa

revision = "e1b7c42a9d60"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "topics",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("freshness_score", sa.Float(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.create_index("ix_topics_id", "topics", ["id"])
    op.create_index("ix_topics_url", "topics", ["url"], unique=True)
    op.create_index("ix_topics_score", "topics", ["score"])
    op.create_index("ix_topics_status", "topics", ["status"])
    op.create_table(
        "story_clusters",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "topic_analyses",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("relevance", sa.Integer(), nullable=False),
        sa.Column("research_priority", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("research_questions", sa.Text(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_topic_analyses_topic_id", "topic_analyses", ["topic_id"], unique=True)
    op.create_table(
        "research_sources",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("publisher", sa.String(255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("topic_id", "url", name="uq_research_sources_topic_url"),
    )
    op.create_index("ix_research_sources_id", "research_sources", ["id"])
    op.create_index("ix_research_sources_topic_id", "research_sources", ["topic_id"])
    op.create_table(
        "story_cluster_members",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("story_clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index("ix_story_cluster_members_cluster_id", "story_cluster_members", ["cluster_id"])
    op.create_index("ix_story_cluster_members_topic_id", "story_cluster_members", ["topic_id"], unique=True)


def downgrade():
    # Child-first, without CASCADE: do not remove unrelated referencing objects.
    op.drop_table("story_cluster_members")
    op.drop_table("research_sources")
    op.drop_table("topic_analyses")
    op.drop_table("story_clusters")
    op.drop_table("topics")
