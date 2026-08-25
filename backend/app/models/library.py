"""Series, episodes, their multilingual titles, and the media files on the NAS."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    MediaStatus,
    SeriesStatus,
    TitleKind,
    TitleSource,
    created_column,
    pk,
    updated_column,
)

if TYPE_CHECKING:
    from app.models.watch import WatchProgress


class OverviewSource(enum.StrEnum):
    """Where a series' `overview` came from.

    Provenance is stored rather than inferred because the three sources have different rules.
    TMDB text is refetched and overwritten by the nightly job; Wikipedia text is licensed for
    reuse **with attribution**, so the page it came from has to survive alongside it; text an
    admin typed belongs to the admin. A row that cannot say which it is holds text nobody is
    allowed to update safely.
    """

    TMDB = "tmdb"
    WIKIPEDIA = "wikipedia"
    MANUAL = "manual"
    #: DramaDex's own synopsis.
    #:
    #: It needs a value of its own, and the absence of one was a real defect: the indexer's text
    #: was being stored as MANUAL, which is precisely the label that means "a person wrote this,
    #: never overwrite it". So a refresh could not replace a synopsis DramaDex itself had
    #: written, "Actualizar metadata" reported no changes forever, and nothing could tell the
    #: two apart afterwards. Rows written before this value existed still say `manual` and are
    #: treated as a person's, because that is the safe direction to be wrong in.
    DRAMADEX = "dramadex"


class Series(Base):
    __tablename__ = "series"

    id: Mapped[str] = pk()
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    sort_title: Mapped[str] = mapped_column(String(500), index=True, nullable=False, default="")
    original_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    overview: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance for `overview`, all three nullable together. NULL is the historical state —
    # every row written before these columns existed holds TMDB text — and is read as "tmdb"
    # by anything that has to name a source. `overview_source_url` is what attribution needs
    # for a Wikipedia extract, and `overview_source_lang` is the language of the *text*, which
    # is not necessarily the household's: an admin may deliberately take a Korean synopsis for
    # a show that has none in Spanish.
    overview_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    overview_source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    overview_source_lang: Mapped[str | None] = mapped_column(String(16), nullable=True)

    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=SeriesStatus.UNKNOWN, nullable=False)
    content_rating: Mapped[str | None] = mapped_column(String(32), nullable=True)
    genres: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    network: Mapped[str | None] = mapped_column(String(200), nullable=True)

    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The household's own indexer. Not "another id like the two above" — for vertical short
    #: drama this is the *only* one that resolves, and it is what carries cast, tags and the
    #: platform watch links. See `app.services.dramadex`.
    dramadex_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    poster_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    backdrop_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    declared_episode_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    library_path: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False)

    # Set the moment an admin edits any metadata field. Two things it is *not*: it is not what
    # protects a chosen poster (that is `artwork_locked` below), and it is not shielding the row
    # from a nightly job — nothing schedules a metadata refresh. `workers/scheduler.py` enqueues
    # `LIBRARY_SCAN` and nothing else, and has since TMDB was removed.
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Adults only. A decision an admin makes and the app enforces, deliberately separate from
    # `content_rating` — that field is free text, DramaDex writes ReelShort's vocabulary rather
    # than the film standard, and four series carry nothing at all. Deriving a permission from
    # it would leave those decided by an absence and would drift the next time an indexer used a
    # new word. Seeded once from the clearly-adult ratings; owned by the admin thereafter.
    restricted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Set when an admin picks artwork. A refresh replaces the indexer's own picture and leaves
    # this one alone — which is the whole point of splitting the two flags: wanting *this*
    # poster and wanting the synopsis kept current are not the same wish.
    artwork_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    added_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()

    episodes: Mapped[list[Episode]] = relationship(
        back_populates="series",
        cascade="all, delete-orphan",
        order_by="(Episode.season_number, Episode.episode_number)",
    )
    titles: Mapped[list[SeriesTitle]] = relationship(
        back_populates="series", cascade="all, delete-orphan", lazy="selectin"
    )
    translations: Mapped[list[SeriesTranslation]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )
    cast: Mapped[list[SeriesCast]] = relationship(
        back_populates="series",
        cascade="all, delete-orphan",
        order_by="(SeriesCast.billing_order, SeriesCast.name)",
        lazy="selectin",
    )
    links: Mapped[list[SeriesLink]] = relationship(
        back_populates="series", cascade="all, delete-orphan", lazy="selectin"
    )


class ScanDirectory(Base):
    """One row per folder on the library mount, holding the mtime the last scan saw.

    A directory's mtime moves when a file is added, removed or renamed inside it — precisely
    the events a scan exists to notice — so comparing it is one stat per folder instead of one
    per file. It does **not** move when a file is rewritten in place, which is the limit of an
    incremental scan and the reason `full` still exists.
    """

    __tablename__ = "scan_directories"

    #: Relative to `LIBRARY_ROOT`, POSIX separators, "" for the root itself.
    rel_path: Mapped[str] = mapped_column(String(1000), primary_key=True)
    mtime: Mapped[float] = mapped_column(Float, nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(nullable=False)


class SeriesCast(Base):
    """Who is in a series, denormalized from DramaDex.

    A copy rather than a foreign key into the indexer, because dramatarr has to keep working
    when DramaDex is down, moved or not yet configured — the series page cannot depend on a
    second service being reachable to render a cast list.

    `dramadex_person_id` is what makes an actor *followable*: it is the key the indexer's
    `/people/{id}` answers on, so "everything else they are in" is one call away and is not
    limited to the shorts this household happens to own.
    """

    __tablename__ = "series_cast"

    id: Mapped[str] = pk()
    series_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dramadex_person_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: DramaDex leaves this null on some rows; those sort last, by name.
    billing_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = created_column()

    series: Mapped[Series] = relationship(back_populates="cast")

    __table_args__ = (
        UniqueConstraint("series_id", "dramadex_person_id", name="uq_series_cast_person"),
    )


class SeriesLink(Base):
    """"Watch on ReelShort" — one row per platform that carries this series.

    The URL is stored as DramaDex renders it, which is built from the platform's **id rather
    than its slug**. Both platforms 301 the id form to whatever the show is called today, and
    these platforms retitle constantly, so a slug-based link rots while this one does not.
    """

    __tablename__ = "series_links"

    id: Mapped[str] = pk()
    series_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: `reelshort`, `dramabox` — the studio key DramaDex uses.
    studio_key: Mapped[str] = mapped_column(String(32), nullable=False)
    studio_name: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = created_column()

    series: Mapped[Series] = relationship(back_populates="links")

    __table_args__ = (
        UniqueConstraint("series_id", "studio_key", name="uq_series_link_studio"),
    )


class SeriesTitle(Base):
    """One searchable title variant.

    Romanizations are materialized as rows here rather than computed at query time, which
    keeps the FTS index plain text and makes the query path a single MATCH.
    """

    __tablename__ = "series_titles"

    id: Mapped[str] = pk()
    series_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default=TitleKind.ALTERNATE, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default=TitleSource.MANUAL, nullable=False)
    normalized: Mapped[str] = mapped_column(String(500), index=True, nullable=False, default="")
    created_at: Mapped[datetime] = created_column()

    series: Mapped[Series] = relationship(back_populates="titles")

    # `index=True` on `normalized` already emits ix_series_titles_normalized; declaring it
    # again here would collide on CREATE INDEX.
    __table_args__ = (UniqueConstraint("series_id", "language", "title", name="uq_series_title"),)


class SeriesTranslation(Base):
    __tablename__ = "series_translations"

    id: Mapped[str] = pk()
    series_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    tagline: Mapped[str | None] = mapped_column(String(500), nullable=True)

    series: Mapped[Series] = relationship(back_populates="translations")

    __table_args__ = (UniqueConstraint("series_id", "language", name="uq_series_translation"),)


class Episode(Base):
    """An episode may exist with no media file — that is how a declared-but-absent
    episode is represented, and it is what lets the UI show 12/24 available."""

    __tablename__ = "episodes"

    id: Mapped[str] = pk()
    series_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    season_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    absolute_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    air_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    runtime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    still_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    added_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()

    series: Mapped[Series] = relationship(back_populates="episodes")
    media_files: Mapped[list[MediaFile]] = relationship(
        back_populates="episode", cascade="all, delete-orphan", lazy="selectin"
    )
    titles: Mapped[list[EpisodeTitle]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )
    progress: Mapped[list[WatchProgress]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("series_id", "season_number", "episode_number", name="uq_episode_number"),
        Index("ix_episodes_series_absolute", "series_id", "absolute_number"),
    )

    @property
    def primary_file(self) -> MediaFile | None:
        for media in self.media_files:
            if media.status == MediaStatus.PRESENT:
                return media
        return None

    @property
    def is_available(self) -> bool:
        return self.primary_file is not None


class EpisodeTitle(Base):
    __tablename__ = "episode_titles"

    id: Mapped[str] = pk()
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default=TitleKind.ALTERNATE, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default=TitleSource.MANUAL, nullable=False)
    normalized: Mapped[str] = mapped_column(String(500), index=True, nullable=False, default="")
    created_at: Mapped[datetime] = created_column()

    episode: Mapped[Episode] = relationship(back_populates="titles")

    __table_args__ = (
        UniqueConstraint("episode_id", "language", "title", name="uq_episode_title"),
    )


class MediaFile(Base):
    """A file on the read-only NAS mount. `episode_id` is nullable so that a file we could
    not parse still shows up in the admin's unmatched view instead of vanishing."""

    __tablename__ = "media_files"

    id: Mapped[str] = pk()
    episode_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    path: Mapped[str] = mapped_column(String(1000), unique=True, index=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mtime: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    container: Mapped[str | None] = mapped_column(String(32), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)

    direct_play_ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=MediaStatus.PRESENT, nullable=False)
    scan_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[datetime] = created_column()
    scanned_at: Mapped[datetime] = updated_column()

    episode: Mapped[Episode | None] = relationship(back_populates="media_files")
    audio_tracks: Mapped[list[AudioTrack]] = relationship(
        back_populates="media_file", cascade="all, delete-orphan", lazy="selectin"
    )
    subtitle_tracks: Mapped[list[SubtitleTrack]] = relationship(
        back_populates="media_file", cascade="all, delete-orphan", lazy="selectin"
    )


class AudioTrack(Base):
    __tablename__ = "audio_tracks"

    id: Mapped[str] = pk()
    media_file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream_index: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    media_file: Mapped[MediaFile] = relationship(back_populates="audio_tracks")


class SubtitleTrack(Base):
    """Either an embedded stream (`stream_index` set) or a sidecar file (`sidecar_path` set).
    `extracted_path` is filled lazily the first time the track is requested as WebVTT."""

    __tablename__ = "subtitle_tracks"

    id: Mapped[str] = pk()
    media_file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_forced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sidecar_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    extracted_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    media_file: Mapped[MediaFile] = relationship(back_populates="subtitle_tracks")


__all__ = [
    "AudioTrack",
    "Episode",
    "EpisodeTitle",
    "MediaFile",
    "OverviewSource",
    "Series",
    "SeriesTitle",
    "SeriesTranslation",
    "SubtitleTrack",
]
