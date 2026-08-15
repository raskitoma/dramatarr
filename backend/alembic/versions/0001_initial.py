"""Initial schema: identity, library, watch state, jobs.

Revision ID: 0001_initial
Revises:
Create Date: 2024-01-01 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `*_at` columns map to app.models.base.UTCDateTime, a TypeDecorator over DateTime; the DDL
# it emits is plain DATETIME, so the migration stays free of application imports.


def upgrade() -> None:
    # --- Identity -------------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("theme", sa.String(length=8), nullable=False),
        sa.Column("preferred_audio_lang", sa.String(length=16), nullable=True),
        sa.Column("preferred_subtitle_lang", sa.String(length=16), nullable=True),
        sa.Column("autoplay_next", sa.Boolean(), nullable=False),
        sa.Column("avatar_color", sa.String(length=9), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "invites",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.String(length=32), nullable=True),
        sa.Column("accepted_user_id", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invites_email"), "invites", ["email"], unique=False)
    op.create_index(op.f("ix_invites_token_hash"), "invites", ["token_hash"], unique=True)

    op.create_table(
        "credentials",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("aaguid", sa.String(length=64), nullable=True),
        sa.Column("nickname", sa.String(length=120), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_credentials_credential_id"), "credentials", ["credential_id"], unique=True
    )
    op.create_index(op.f("ix_credentials_user_id"), "credentials", ["user_id"], unique=False)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_auth_sessions_token_hash"), "auth_sessions", ["token_hash"], unique=True
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"], unique=False)

    op.create_table(
        "magic_links",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_magic_links_token_hash"), "magic_links", ["token_hash"], unique=True)
    op.create_index(op.f("ix_magic_links_user_id"), "magic_links", ["user_id"], unique=False)

    op.create_table(
        "webauthn_challenges",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("invite_id", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invite_id"], ["invites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_challenge_kind_expires", "webauthn_challenges", ["kind", "expires_at"], unique=False
    )

    # --- Library --------------------------------------------------------------------------
    op.create_table(
        "series",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("sort_title", sa.String(length=500), nullable=False),
        sa.Column("original_title", sa.String(length=500), nullable=True),
        sa.Column("original_language", sa.String(length=16), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_rating", sa.String(length=32), nullable=True),
        sa.Column("genres", sa.JSON(), nullable=False),
        sa.Column("network", sa.String(length=200), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("poster_path", sa.String(length=500), nullable=True),
        sa.Column("backdrop_path", sa.String(length=500), nullable=True),
        sa.Column("declared_episode_count", sa.Integer(), nullable=True),
        sa.Column("library_path", sa.String(length=1000), nullable=False),
        sa.Column("metadata_locked", sa.Boolean(), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("library_path"),
    )
    op.create_index(op.f("ix_series_slug"), "series", ["slug"], unique=True)
    op.create_index(op.f("ix_series_sort_title"), "series", ["sort_title"], unique=False)
    op.create_index(op.f("ix_series_tmdb_id"), "series", ["tmdb_id"], unique=False)

    op.create_table(
        "series_titles",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("series_id", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("normalized", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "language", "title", name="uq_series_title"),
    )
    # Same name `index=True` on SeriesTitle.normalized generates, so `alembic check` stays
    # clean against Base.metadata.
    op.create_index("ix_series_titles_normalized", "series_titles", ["normalized"], unique=False)
    op.create_index(
        op.f("ix_series_titles_series_id"), "series_titles", ["series_id"], unique=False
    )

    op.create_table(
        "series_translations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("series_id", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("tagline", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "language", name="uq_series_translation"),
    )
    op.create_index(
        op.f("ix_series_translations_series_id"), "series_translations", ["series_id"], unique=False
    )

    op.create_table(
        "episodes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("series_id", sa.String(length=32), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("absolute_number", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("air_date", sa.Date(), nullable=True),
        sa.Column("runtime_seconds", sa.Integer(), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("still_path", sa.String(length=500), nullable=True),
        sa.Column("metadata_locked", sa.Boolean(), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_id", "season_number", "episode_number", name="uq_episode_number"
        ),
    )
    op.create_index(
        "ix_episodes_series_absolute", "episodes", ["series_id", "absolute_number"], unique=False
    )
    op.create_index(op.f("ix_episodes_series_id"), "episodes", ["series_id"], unique=False)

    op.create_table(
        "episode_titles",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("episode_id", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("normalized", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("episode_id", "language", "title", name="uq_episode_title"),
    )
    op.create_index(
        op.f("ix_episode_titles_episode_id"), "episode_titles", ["episode_id"], unique=False
    )
    op.create_index(
        op.f("ix_episode_titles_normalized"), "episode_titles", ["normalized"], unique=False
    )

    op.create_table(
        "media_files",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("episode_id", sa.String(length=32), nullable=True),
        sa.Column("path", sa.String(length=1000), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("container", sa.String(length=32), nullable=True),
        sa.Column("video_codec", sa.String(length=32), nullable=True),
        sa.Column("audio_codec", sa.String(length=32), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("bitrate", sa.Integer(), nullable=True),
        sa.Column("direct_play_ok", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scan_error", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_media_files_episode_id"), "media_files", ["episode_id"], unique=False)
    op.create_index(op.f("ix_media_files_path"), "media_files", ["path"], unique=True)

    op.create_table(
        "audio_tracks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("media_file_id", sa.String(length=32), nullable=False),
        sa.Column("stream_index", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("codec", sa.String(length=32), nullable=True),
        sa.Column("channels", sa.Integer(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["media_file_id"], ["media_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_audio_tracks_media_file_id"), "audio_tracks", ["media_file_id"], unique=False
    )

    op.create_table(
        "subtitle_tracks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("media_file_id", sa.String(length=32), nullable=False),
        sa.Column("stream_index", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("codec", sa.String(length=32), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_forced", sa.Boolean(), nullable=False),
        sa.Column("sidecar_path", sa.String(length=1000), nullable=True),
        sa.Column("extracted_path", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["media_file_id"], ["media_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_subtitle_tracks_media_file_id"), "subtitle_tracks", ["media_file_id"], unique=False
    )

    # --- Watch state ----------------------------------------------------------------------
    op.create_table(
        "watch_progress",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("episode_id", sa.String(length=32), nullable=False),
        sa.Column("position_seconds", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("percent", sa.Float(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("play_count", sa.Integer(), nullable=False),
        sa.Column("first_played_at", sa.DateTime(), nullable=False),
        sa.Column("last_played_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "episode_id", name="uq_watch_user_episode"),
    )
    op.create_index(
        "ix_watch_user_last_played", "watch_progress", ["user_id", "last_played_at"], unique=False
    )
    op.create_index(
        op.f("ix_watch_progress_episode_id"), "watch_progress", ["episode_id"], unique=False
    )
    op.create_index(op.f("ix_watch_progress_user_id"), "watch_progress", ["user_id"], unique=False)

    op.create_table(
        "play_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("episode_id", sa.String(length=32), nullable=False),
        sa.Column("series_id", sa.String(length=32), nullable=False),
        sa.Column("position_seconds", sa.Float(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_play_events_user_at", "play_events", ["user_id", "at"], unique=False)
    op.create_index(op.f("ix_play_events_series_id"), "play_events", ["series_id"], unique=False)
    op.create_index(op.f("ix_play_events_user_id"), "play_events", ["user_id"], unique=False)

    # --- Jobs, audit, settings --------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("progress_percent", sa.Float(), nullable=False),
        sa.Column("progress_message", sa.String(length=500), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("series_id", sa.String(length=32), nullable=True),
        sa.Column("episode_id", sa.String(length=32), nullable=True),
        sa.Column("created_by_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "priority", "created_at"], unique=False)
    op.create_index(op.f("ix_jobs_type"), "jobs", ["type"], unique=False)

    op.create_table(
        "job_logs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_logs_job_id"), "job_logs", ["job_id"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_at", "audit_log", ["at"], unique=False)
    op.create_index(op.f("ix_audit_log_user_id"), "audit_log", ["user_id"], unique=False)

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_id", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")

    op.drop_index(op.f("ix_audit_log_user_id"), table_name="audit_log")
    op.drop_index("ix_audit_at", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index(op.f("ix_job_logs_job_id"), table_name="job_logs")
    op.drop_table("job_logs")

    op.drop_index(op.f("ix_jobs_type"), table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index(op.f("ix_play_events_user_id"), table_name="play_events")
    op.drop_index(op.f("ix_play_events_series_id"), table_name="play_events")
    op.drop_index("ix_play_events_user_at", table_name="play_events")
    op.drop_table("play_events")

    op.drop_index(op.f("ix_watch_progress_user_id"), table_name="watch_progress")
    op.drop_index(op.f("ix_watch_progress_episode_id"), table_name="watch_progress")
    op.drop_index("ix_watch_user_last_played", table_name="watch_progress")
    op.drop_table("watch_progress")

    op.drop_index(op.f("ix_subtitle_tracks_media_file_id"), table_name="subtitle_tracks")
    op.drop_table("subtitle_tracks")

    op.drop_index(op.f("ix_audio_tracks_media_file_id"), table_name="audio_tracks")
    op.drop_table("audio_tracks")

    op.drop_index(op.f("ix_media_files_path"), table_name="media_files")
    op.drop_index(op.f("ix_media_files_episode_id"), table_name="media_files")
    op.drop_table("media_files")

    op.drop_index(op.f("ix_episode_titles_normalized"), table_name="episode_titles")
    op.drop_index(op.f("ix_episode_titles_episode_id"), table_name="episode_titles")
    op.drop_table("episode_titles")

    op.drop_index(op.f("ix_episodes_series_id"), table_name="episodes")
    op.drop_index("ix_episodes_series_absolute", table_name="episodes")
    op.drop_table("episodes")

    op.drop_index(op.f("ix_series_translations_series_id"), table_name="series_translations")
    op.drop_table("series_translations")

    op.drop_index(op.f("ix_series_titles_series_id"), table_name="series_titles")
    op.drop_index("ix_series_titles_normalized", table_name="series_titles")
    op.drop_table("series_titles")

    op.drop_index(op.f("ix_series_tmdb_id"), table_name="series")
    op.drop_index(op.f("ix_series_sort_title"), table_name="series")
    op.drop_index(op.f("ix_series_slug"), table_name="series")
    op.drop_table("series")

    op.drop_index("ix_challenge_kind_expires", table_name="webauthn_challenges")
    op.drop_table("webauthn_challenges")

    op.drop_index(op.f("ix_magic_links_user_id"), table_name="magic_links")
    op.drop_index(op.f("ix_magic_links_token_hash"), table_name="magic_links")
    op.drop_table("magic_links")

    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_token_hash"), table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_index(op.f("ix_credentials_user_id"), table_name="credentials")
    op.drop_index(op.f("ix_credentials_credential_id"), table_name="credentials")
    op.drop_table("credentials")

    op.drop_index(op.f("ix_invites_token_hash"), table_name="invites")
    op.drop_index(op.f("ix_invites_email"), table_name="invites")
    op.drop_table("invites")

    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
