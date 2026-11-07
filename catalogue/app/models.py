"""Database schema.

Two invariants are worth pointing out because they are enforced here rather
than in application code:

  * `uq_sync_run_scheduled` is a PARTIAL unique index. Scheduled runs are capped
    at one per source per job type per day; manual runs bypass it. That is the
    "once daily at most, except manual triggers" rule, as a database constraint.
  * Every fact that came from a source carries `source_id`. That is what makes
    conflicts resolvable, IMDb-derived data strippable, and a bad source
    revocable in a single DELETE.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Timestamp default, generated in Python rather than by the database.

    `server_default=func.now()` maps to SQLite CURRENT_TIMESTAMP, which stores
    second granularity ('2026-08-16 22:24:08'), while SQLAlchemy binds a Python
    datetime as '2026-08-16 22:24:08.000000'. SQLite compares those as strings,
    so equality never matched -- which silently truncated keyset pagination at
    the first page whenever rows shared a timestamp.
    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --- job / run vocabulary ----------------------------------------------------


class JobType:
    FULL_CATALOG = "full_catalog"
    INCREMENTAL = "incremental"
    DETAIL_BACKFILL = "detail_backfill"
    ACTOR_ROSTER = "actor_roster"
    IMDB_REFRESH = "imdb_refresh"
    POSTER_FETCH = "poster_fetch"
    #: Actor headshots. Split from poster_fetch because it walks people rather
    #: than titles and hits the same CDNs at a different cadence.
    AVATAR_FETCH = "avatar_fetch"
    #: Structured facts about people, mined from a source's performer pages.
    #: Separate from actor_roster because it walks stored person ids rather than
    #: a published index, and DramaBox has no index at all.
    PERSON_FACTS = "person_facts"
    REPARSE = "reparse"
    LINK_HEALTH = "link_health"
    ROBOTS_CHECK = "robots_check"
    #: Find duplicate titles and merge the confident ones. Runs catalogue-wide
    #: rather than per-source, like backup.
    DEDUP = "dedup"
    BACKUP = "backup"
    PROBE = "probe"


class RunState:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BreakerState:
    CLOSED = "closed"
    OPEN = "open"


class MatchState:
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ImageKind:
    POSTER = "poster"
    STILL = "still"
    AVATAR = "avatar"


# --- sources & studios -------------------------------------------------------


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # The durable watch URL is rendered from this template plus external_id.
    # Both platforms 301 the id-only form to the current canonical slug, so a
    # link built this way self-heals when a title is renamed.
    watch_url_template: Mapped[str | None] = mapped_column(String(255))

    rate_json: Mapped[str] = mapped_column(Text, default="{}")
    config_json: Mapped[str] = mapped_column(Text, default="{}")

    studios: Mapped[list[Studio]] = relationship(back_populates="source")


class Studio(Base):
    """The platform a title comes from: ReelShort, DramaBox, ..."""

    __tablename__ = "studio"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    homepage_url: Mapped[str | None] = mapped_column(String(255))

    source: Mapped[Source] = relationship(back_populates="studios")


# --- titles ------------------------------------------------------------------


class Title(Base):
    __tablename__ = "title"

    id: Mapped[int] = mapped_column(primary_key=True)
    primary_title: Mapped[str] = mapped_column(String(512))
    normalized_title: Mapped[str] = mapped_column(String(512))
    #: `normalized_title` with every separator deleted -- see app/dedup.match_key.
    #: Stored rather than computed because cross-source identity resolution runs
    #: once per ingested record, and comparing a computed key against 10,000 rows
    #: per record is quadratic; this makes it one indexed equality lookup.
    identity_key: Mapped[str | None] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer)
    synopsis: Mapped[str | None] = mapped_column(Text)
    episode_count: Mapped[int | None] = mapped_column(Integer)
    first_paid_episode: Mapped[int | None] = mapped_column(Integer)
    audience_rating: Mapped[str | None] = mapped_column(String(32))
    completed: Mapped[bool | None] = mapped_column(Boolean)
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Set when this row was merged into another as a duplicate. The row is kept
    #: rather than deleted: a downstream consumer already stores these ids, and a
    #: 404 tells it nothing about where the title went. Deliberately NOT reusing
    #: `delisted_at`, which means "the platform dropped it" -- an operator seeing
    #: a delisted title asks a different question than one seeing a merged title.
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("title.id", ondelete="SET NULL")
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    sources: Mapped[list[TitleSource]] = relationship(
        back_populates="title", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_title_normalized", "normalized_title"),
        Index("ix_title_identity", "identity_key"),
        Index("ix_title_year", "year"),
        # Keyset pagination for the downstream app's ?updated_since= polling.
        Index("ix_title_updated", "updated_at", "id"),
        # Following a tombstone to its survivor is on the hot path of every
        # detail request for a merged id.
        Index("ix_title_merged_into", "merged_into_id"),
    )


class TitleSource(Base):
    __tablename__ = "title_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    studio_id: Mapped[int | None] = mapped_column(ForeignKey("studio.id"))
    external_id: Mapped[str] = mapped_column(String(128))

    #: Cross-locale identity as the source publishes it. ReelShort gives the same
    #: show a different `book_id` in every language catalogue, so without this an
    #: edition is unrecognisable as the same work and would land as its own
    #: title. Indexed because ingest looks it up once per record.
    locale_key: Mapped[str | None] = mapped_column(String(32))
    #: Language of THIS link's title and synopsis, as the source labels it.
    lang: Mapped[str | None] = mapped_column(String(16))

    # Resolved canonical page, refreshed each sync. Pretty, allowed to go stale;
    # never the thing the watch button depends on.
    url: Mapped[str | None] = mapped_column(String(512))
    # First-party share link where the source publishes one.
    share_url: Mapped[str | None] = mapped_column(String(512))
    # Key art as published by this source. Kept here rather than on Title
    # because it is a source artefact, and because the image pipeline needs it
    # without re-deriving it from the archive.
    poster_url: Mapped[str | None] = mapped_column(String(512))

    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    #: This source's resolved view of the title, as JSON. Keeping it means the
    #: canonical Title row can be recomputed from its sources at any time,
    #: which is what makes precedence order-independent rather than
    #: last-writer-wins.
    parsed_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    title: Mapped[Title] = relationship(back_populates="sources")

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_title_source_external"),
        Index("ix_title_source_studio", "studio_id"),
        # There was no index on title_id at all: every lookup of "which links
        # does this title have" was a sequential scan. Survivable for one API
        # request, fatal for a backfill that asks it once per episode row.
        Index("ix_title_source_title", "title_id", "source_id"),
        # Ingest looks this up once per incoming record to decide whether a
        # localised edition belongs to a title we already hold.
        Index("ix_title_source_locale", "source_id", "locale_key"),
    )


class TitleAlias(Base):
    __tablename__ = "title_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(512))
    normalized_alias: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str | None] = mapped_column(String(32))
    region: Mapped[str | None] = mapped_column(String(16))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))

    __table_args__ = (
        UniqueConstraint(
            "title_id", "normalized_alias", "source_id", name="uq_title_alias"
        ),
        Index("ix_title_alias_normalized", "normalized_alias"),
    )


class TitleChange(Base):
    """Field-level history.

    These platforms retitle and re-shelve constantly, so "what changed and when"
    is a real question. Cheap because every payload is hashed anyway.
    """

    __tablename__ = "title_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (Index("ix_title_change_title", "title_id", "observed_at"),)


# --- episodes ----------------------------------------------------------------


class Episode(Base):
    """Per-episode metadata.

    Deliberately carries no video URL: both platforms hand us signed, expiring
    stream links in the same payload. They expire, they are not metadata, and
    this is a metadata indexer.
    """

    __tablename__ = "episode"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    #: The EDITION this row came from. One platform publishes the same show more
    #: than once -- a `（DUBBED）` twin, a GoodShort republish, a Spanish
    #: catalogue -- and those links share a title_id AND a source_id, so neither
    #: identifies which listing an episode belongs to. Language lives on the
    #: link, so without this a `?lang=` filter has nothing to filter on.
    #: Nullable: rows predating the column whose edition the archive could not
    #: settle stay unknown rather than being guessed at.
    title_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("title_source.id", ondelete="CASCADE")
    )
    external_id: Mapped[str] = mapped_column(String(128))
    number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(512))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    is_free: Mapped[bool | None] = mapped_column(Boolean)
    still_image_id: Mapped[int | None] = mapped_column(ForeignKey("image.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        # Scoped to the TITLE, not just the source. ReelShort reuses placeholder
        # chapter ids -- '0000' appears on more than one title -- so a globally
        # unique constraint killed a full crawl 386 titles in. An episode id is
        # only meaningful within the title it belongs to.
        UniqueConstraint(
            "source_id", "title_id", "external_id", name="uq_episode_external"
        ),
        Index("ix_episode_title", "title_id", "number"),
        # `?lang=` resolves through the edition, so this is on the read path of
        # every filtered episode request.
        Index("ix_episode_edition", "title_source_id"),
    )


# --- people ------------------------------------------------------------------


class Person(Base):
    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))
    bio: Mapped[str | None] = mapped_column(Text)

    # Structured facts, parsed out of a source's prose rather than stored as
    # prose. DramaBox publishes a biography only in Chinese, which this index's
    # reader cannot read -- but the sentence is formulaic enough to be a record,
    # so the facts are extracted and the prose deliberately discarded.
    #: "male" | "female", normalised from the source's own term.
    gender: Mapped[str | None] = mapped_column(String(16))
    height_cm: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[int | None] = mapped_column(Integer)
    #: English sign name, mapped from a fixed twelve-term lookup.
    zodiac: Mapped[str | None] = mapped_column(String(16))

    #: Set when this person was folded into another. The row is kept rather than
    #: deleted so an id a consumer already stored keeps resolving -- the same
    #: contract `title` offers. Everything else moved to the survivor, so a
    #: tombstone holds no links and must be filtered out of every listing.
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL")
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_person_normalized", "normalized_name"),
        # Following a tombstone to its survivor is on the hot path of the person
        # routes, the same as ix_title_merged_into is for titles.
        Index("ix_person_merged_into", "merged_into_id"),
    )


class PersonAlias(Base):
    """Other names for one person -- notably the Han form of a Chinese name.

    Deliberately NOT `person.name`. The romanised form stays the primary and the
    only thing search touches, because this index's reader cannot type Chinese;
    an alias that cannot be typed is worse than useless as a search key, since it
    would push the name people CAN type out of the field they search on.

    So this is storage and display only. Mirrors `title_alias`, including the
    per-source uniqueness: two sources may publish the same alternate name and
    neither should win.
    """

    __tablename__ = "person_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(255))
    normalized_alias: Mapped[str] = mapped_column(String(255))
    #: BCP-47-ish tag as the source labels it, e.g. "zh-Hans". Kept so a consumer
    #: can decide what to render rather than guessing from the characters.
    lang: Mapped[str | None] = mapped_column(String(16))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))

    __table_args__ = (
        UniqueConstraint(
            "person_id", "normalized_alias", "source_id", name="uq_person_alias"
        ),
        Index("ix_person_alias_normalized", "normalized_alias"),
    )


class PersonSource(Base):
    __tablename__ = "person_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    # dramabox performerId | reelshort tag_id | fandom url | imdb nconst
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(String(512))
    # Where the source publishes this person's photo. Kept alongside the fetched
    # image the same way TitleSource.poster_url is: the adapters parse it during
    # a catalogue sync, and the avatar_fetch job downloads it later, so a slow
    # image CDN never holds up the crawl.
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    avatar_image_id: Mapped[int | None] = mapped_column(ForeignKey("image.id"))

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_person_source_external"),
    )


class TitlePerson(Base):
    __tablename__ = "title_person"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"))
    role: Mapped[str | None] = mapped_column(String(64))
    billing_order: Mapped[int | None] = mapped_column(Integer)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))

    __table_args__ = (
        UniqueConstraint(
            "title_id", "person_id", "role", "source_id", name="uq_title_person"
        ),
        Index("ix_title_person_person", "person_id"),
    )


# --- taxonomy ----------------------------------------------------------------


class Tag(Base):
    """Namespaced deliberately.

    ReelShort alone has 15 tag namespaces and 228 distinct tags appeared in 30
    sampled detail pages. Flattening them into one vocabulary would destroy
    information and force arbitrary judgement calls.
    """

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(255))

    __table_args__ = (UniqueConstraint("namespace", "value", name="uq_tag"),)


class TitleTag(Base):
    __tablename__ = "title_tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))

    __table_args__ = (
        UniqueConstraint("title_id", "tag_id", "source_id", name="uq_title_tag"),
        Index("ix_title_tag_tag", "tag_id"),
    )


# --- media -------------------------------------------------------------------


class Image(Base):
    __tablename__ = "image"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    local_path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    # Perceptual hash: free during the decode we do to downscale, and doubles as
    # a cross-source match signal.
    phash: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))
    source_url: Mapped[str | None] = mapped_column(String(512))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    license_tag: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_image_phash", "phash"),)


class TitleImage(Base):
    __tablename__ = "title_image"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    image_id: Mapped[int] = mapped_column(ForeignKey("image.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32), default=ImageKind.POSTER)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("title_id", "image_id", "kind", name="uq_title_image"),
    )


# --- ratings -----------------------------------------------------------------


class Rating(Base):
    """Kept per-source and separable.

    `is_editorial` flags DramaBox's floor-clamped 7.1-10 and ReelShort's
    4.5-4.9 promotional scores. They are real fields on the source so we store
    them, but they must never leak into sorting or into Title as a real score.
    IMDb is the only genuine rating source in this project.
    """

    __tablename__ = "rating"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    value: Mapped[float] = mapped_column(Float)
    votes: Mapped[int | None] = mapped_column(Integer)
    scale: Mapped[float] = mapped_column(Float, default=10.0)
    is_editorial: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("title_id", "source_id", name="uq_rating_title_source"),
    )


# --- identity resolution -----------------------------------------------------


class MatchCandidate(Base):
    __tablename__ = "match_candidate"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    target_source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    target_external_id: Mapped[str] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(64))
    score: Mapped[float | None] = mapped_column(Float)
    # Why this was proposed: candidates considered, episode counts compared.
    # The review queue shows this so a decision takes seconds.
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    state: Mapped[str] = mapped_column(String(32), default=MatchState.PROPOSED)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint(
            "title_id", "target_source_id", "target_external_id", name="uq_match"
        ),
        Index("ix_match_state", "state"),
    )


class MergeState:
    PROPOSED = "proposed"  #: evidence disagreed; waiting on an operator
    APPLIED = "applied"
    REJECTED = "rejected"  #: an operator said these are different shows
    REVERTED = "reverted"


class TitleMerge(Base):
    """One title folded into another -- proposed, applied, or refused.

    Doubles as the review queue and the audit trail, deliberately: an operator
    looking at a proposed merge and an operator auditing an applied one want the
    same columns, and one table means one place to look.

    The audit half is what makes a merge reversible. `title.merged_into_id` records
    THAT a merge happened; only `report_json` records what moved, and without the
    captured `title_source` links an undo cannot know which to hand back -- the
    links are the identity anchors, so a tombstone cleared without them leaves a
    title no sync can ever repopulate.
    """

    __tablename__ = "title_merge"

    id: Mapped[int] = mapped_column(primary_key=True)
    # No CASCADE: the audit row must outlive a title row being deleted, otherwise
    # deleting a title quietly erases the evidence of what was merged into it.
    loser_id: Mapped[int] = mapped_column(ForeignKey("title.id"), index=True)
    survivor_id: Mapped[int] = mapped_column(ForeignKey("title.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default=MergeState.PROPOSED)
    #: Which key found the pair: title, dubbed, or poster.
    key_kind: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    #: The evidence classify() weighed -- episode counts, years, sources. Shown in
    #: the review queue so a decision takes seconds.
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    #: Per-table row counts plus the loser's links. Populated on APPLIED only.
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    merged_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Re-running the job must not pile up duplicate proposals for the same pair.
        UniqueConstraint("loser_id", "survivor_id", name="uq_title_merge_pair"),
        Index("ix_title_merge_state", "state"),
    )


# --- jobs --------------------------------------------------------------------


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    job_type: Mapped[str] = mapped_column(String(64))
    run_date: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(32), default=RunState.PENDING)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pid: Mapped[int | None] = mapped_column(Integer)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")

    __table_args__ = (
        # PARTIAL unique index: scheduled runs are capped at one per source per
        # job type per day; manual triggers bypass it. Supported identically by
        # SQLite and Postgres, hence both dialect predicates.
        Index(
            "uq_sync_run_scheduled",
            "source_id",
            "job_type",
            "run_date",
            unique=True,
            sqlite_where=text("manual = 0"),
            postgresql_where=text("manual = false"),
        ),
        Index("ix_sync_run_state", "state"),
    )


class SyncProgress(Base):
    __tablename__ = "sync_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_run.id", ondelete="CASCADE"), unique=True
    )
    phase: Mapped[str | None] = mapped_column(String(64))
    items_done: Mapped[int] = mapped_column(Integer, default=0)
    items_total: Mapped[int | None] = mapped_column(Integer)
    # EWMA over a short window, so a run that slows down reports honestly
    # instead of hiding behind a flattering lifetime average.
    rate_ewma: Mapped[float | None] = mapped_column(Float)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class SourceState(Base):
    __tablename__ = "source_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), unique=True
    )
    watermark: Mapped[str | None] = mapped_column(String(128))
    breaker_state: Mapped[str] = mapped_column(String(32), default=BreakerState.CLOSED)
    breaker_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Halt the source if its robots.txt rules tighten.
    robots_sha256: Mapped[str | None] = mapped_column(String(64))
    robots_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schema_canary_json: Mapped[str] = mapped_column(Text, default="{}")
    # Held by a running worker, so no two jobs hit the same site at once.
    lock_pid: Mapped[int | None] = mapped_column(Integer)
    lock_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParseQuarantine(Base):
    """A record that failed to parse. The run continues; fix the parser, then
    `reparse` over the archive without touching the network."""

    __tablename__ = "parse_quarantine"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    external_id: Mapped[str | None] = mapped_column(String(128))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
