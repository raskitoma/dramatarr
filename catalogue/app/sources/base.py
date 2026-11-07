"""The source adapter contract.

A new source is a module implementing `Source`, plus a row in the `source`
table. There is deliberately no config DSL: every site probed during recon
needed real code (silent 202 challenges, redirect stubs, non-deterministic
pagination, a 404 that still carries valid-looking JSON), and a selector
language could not express any of it.

`parse` is a pure function -- bytes in, record out, no network. That is what
makes the parser layer testable offline against fixtures and re-runnable over
the archive by the `reparse` job.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.fetch.types import RateSpec, RawDoc, Verdict


class Mode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True)
class Ref:
    """A pointer to one title on a source, yielded by discover()."""

    external_id: str
    url: str | None = None
    #: Fields already known from the listing, so detail fetches can be skipped
    #: when a source's listing is rich enough (DramaBox's carries ~95%).
    hint: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PersonRef:
    external_id: str
    name: str | None = None
    url: str | None = None


@dataclass
class EpisodeRecord:
    external_id: str
    number: int | None = None
    name: str | None = None
    duration_ms: int | None = None
    is_free: bool | None = None
    still_url: str | None = None
    published_at: datetime | None = None


@dataclass
class PersonCredit:
    external_id: str
    name: str
    role: str = "actor"
    billing_order: int | None = None
    avatar_url: str | None = None
    #: IMDb nconst where the source hands it to us (ReelShort does).
    imdb_nconst: str | None = None


@dataclass
class SourceRecord:
    """One title, normalized out of a source payload but not yet merged."""

    external_id: str
    title: str
    url: str | None = None
    share_url: str | None = None
    aliases: list[str] = field(default_factory=list)
    #: BCP-47-ish language of THIS record, as the source labels it. ReelShort
    #: publishes a separate title per locale, so "es" here means the title and
    #: synopsis in this record are Spanish.
    lang: str | None = None
    #: Cross-locale identity, published by the source. ReelShort's `t_book_id`
    #: is `<locale-prefix><serial>`, and the serial is the same for every
    #: language edition of one show -- an EXACT key, not a name match, which is
    #: what makes linking editions defensible under the project's identity rule.
    locale_key: str | None = None
    year: int | None = None
    synopsis: str | None = None
    episode_count: int | None = None
    first_paid_episode: int | None = None
    audience_rating: str | None = None
    completed: bool | None = None
    released_at: datetime | None = None
    poster_url: str | None = None
    #: (namespace, value) pairs; namespaces stay separate on purpose.
    tags: list[tuple[str, str]] = field(default_factory=list)
    cast: list[PersonCredit] = field(default_factory=list)
    episodes: list[EpisodeRecord] = field(default_factory=list)
    #: Promotional scores land here with editorial=True so they can never leak
    #: into sorting. IMDb is the only genuine rating source in this project.
    rating: float | None = None
    rating_votes: int | None = None
    rating_is_editorial: bool = True
    #: IMDb tconst where the source provides it directly.
    imdb_tconst: str | None = None
    raw_sha256: str | None = None


class Ctx(Protocol):
    """Per-run context handed to adapters."""

    fetcher: object
    max_items: int
    watermark: str | None

    def set_watermark(self, value: str) -> None: ...
    def progress(self, done: int, total: int | None = None, phase: str | None = None) -> None: ...
    def cancelled(self) -> bool: ...


@runtime_checkable
class Source(Protocol):
    key: str
    studio_name: str
    base_url: str
    watch_url_template: str
    rate: RateSpec

    def preflight(self, ctx: Ctx) -> None:
        """Per-run setup. DramaBox re-reads its rotating buildId here."""

    def discover(self, ctx: Ctx, mode: Mode) -> Iterator[Ref]:
        """Yield title refs. The adapter owns watermark logic, multi-pass
        saturation and stop conditions -- they differ per site."""

    def fetch_detail(self, ctx: Ctx, ref: Ref) -> RawDoc: ...

    # Both sources split their data across listing and detail responses, and
    # not as a subset: DramaBox's ratings/status/free exist ONLY on the
    # listing, while cast and episodes exist ONLY on the detail record. So the
    # contract has two parsers and the ingest layer merges them, rather than
    # pretending one response is authoritative.

    def parse_listing(self, raw: RawDoc) -> list[SourceRecord]:
        """Pure: no network, no database."""

    def parse_detail(self, raw: RawDoc) -> SourceRecord:
        """Pure: no network, no database."""

    def classify(self, doc: RawDoc) -> Verdict:
        """Per-source, because failure signatures conflict across sites."""

    def roster(self, ctx: Ctx) -> Iterator[PersonRef] | None:
        """Optional actor roster enumeration."""


def watch_url(template: str, external_id: str) -> str:
    """Render the durable watch URL.

    Built from the id rather than the slug: both platforms 301 the id-only form
    to the current canonical URL, so the link survives a retitle.
    """
    return template.format(external_id=external_id)


def merge_records(base: SourceRecord, overlay: SourceRecord) -> SourceRecord:
    """Overlay a detail record onto a listing record.

    Only non-empty overlay values win, because the detail response genuinely
    lacks fields the listing has -- on DramaBox `bookInfo` carries no ratings,
    status or free flag. Letting a detail parse blank them would quietly delete
    real data on every backfill.
    """
    merged = replace(base)
    for field_name, value in vars(overlay).items():
        if value in (None, [], "", {}):
            continue
        setattr(merged, field_name, value)
    return merged
