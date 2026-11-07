"""API response shapes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class StudioLink(BaseModel):
    """What a "Watch on ReelShort" button needs.

    `watch_url` is rendered from the id, not the stored slug: both platforms
    301 the id-only form to the current canonical URL, so it survives a
    retitle. `url` is the prettier resolved form and is allowed to go stale.
    """

    key: str
    name: str
    external_id: str
    watch_url: str
    url: str | None = None
    share_url: str | None = None
    delisted_at: datetime | None = None


class TagOut(BaseModel):
    namespace: str
    value: str


class PersonBrief(BaseModel):
    id: int
    name: str
    role: str | None = None
    billing_order: int | None = None
    #: Our own endpoint, not the source CDN -- same reasoning as poster_url.
    #: None means no headshot is stored, which is common: the platforms publish
    #: one for only some of the cast.
    photo_url: str | None = None


class RatingOut(BaseModel):
    source: str
    value: float
    votes: int | None = None
    scale: float
    #: True for the platforms' promotional scores (DramaBox floor-clamps to
    #: 7.1-10, ReelShort sits at 4.5-4.9). Never sort by these.
    is_editorial: bool


class TitleBrief(BaseModel):
    id: int
    primary_title: str
    year: int | None = None
    episode_count: int | None = None
    audience_rating: str | None = None
    poster_url: str | None = None
    studios: list[StudioLink] = []
    updated_at: datetime
    #: Set when this row was merged into another as a duplicate: the id here is
    #: the title to use instead. It appears on TitleBrief rather than only on
    #: TitleDetail because a ?updated_since= poll -- the one place a consumer can
    #: learn a stored id was superseded -- returns briefs.
    merged_into: int | None = None


class EditionOut(BaseModel):
    """One language edition of a title, as a source published it.

    A platform that runs per-language catalogues lists the same show once per
    language, each with its own id, title, synopsis and watch link. Those names
    already reached consumers through `aliases[]`, but as bare strings: "Atada
    por amor" was indistinguishable from "His Cure His Wife", so a Spanish
    household could SEARCH the Spanish name and still had to display the English
    one. The missing thing was never the data, only the label.

    Additive on purpose. `aliases[]` keeps its shape -- changing it to objects
    would break every consumer that reads it today.
    """

    #: BCP-47 primary subtag as the source labels the edition, e.g. `es`.
    lang: str | None = None
    title: str | None = None
    #: This edition's own synopsis. The text a reader in that language actually
    #: reads, which `TitleDetail.synopsis` (resolved by precedence, in practice
    #: English) does not give them.
    synopsis: str | None = None
    source: str
    #: This edition's own watch link -- a localised edition is a different id on
    #: the platform, so the English link would send a reader to the wrong page.
    watch_url: str | None = None
    #: True for the edition whose title precedence chose as `primary_title`.
    is_primary: bool = False


class TitleDetail(TitleBrief):
    synopsis: str | None = None
    first_paid_episode: int | None = None
    completed: bool | None = None
    delisted_at: datetime | None = None
    aliases: list[str] = []
    #: Every language edition this title is published in, tagged. Empty for the
    #: ordinary case of a title carried in one language only.
    editions: list[EditionOut] = []
    tags: list[TagOut] = []
    cast: list[PersonBrief] = []
    ratings: list[RatingOut] = []


class EpisodeOut(BaseModel):
    number: int | None = None
    name: str | None = None
    duration_ms: int | None = None
    is_free: bool | None = None
    source: str
    #: Language of the edition this episode belongs to, as the source labels it.
    #: `null` is the normal case: only ReelShort publishes per-language
    #: catalogues, and rows whose edition predates the tagging keep it too.
    lang: str | None = None


class Page(BaseModel):
    """Keyset pagination.

    Offset pagination silently skips or repeats rows when the underlying data
    changes mid-scan, and syncs run while clients poll. `next_cursor` encodes
    (updated_at, id), which is stable under concurrent writes.
    """

    items: list
    next_cursor: str | None = None
    count: int


class PersonAliasOut(BaseModel):
    alias: str
    #: BCP-47-ish, as the source labels it -- "zh-Hans" for a Han name. Present
    #: so a consumer can choose what to render rather than sniffing characters.
    lang: str | None = None
    source: str | None = None


class PersonDetail(BaseModel):
    id: int
    name: str
    bio: str | None = None
    #: Served by us at /people/{id}/photo; None when no headshot is stored.
    photo_url: str | None = None
    # Structured facts, mined from a source's prose rather than stored as prose.
    # DramaBox publishes a biography only in Chinese; these are what survives
    # translation without one. All commonly null -- absence is normal.
    #: "male" | "female", normalised from the source's own term.
    gender: str | None = None
    height_cm: int | None = None
    weight_kg: int | None = None
    #: English sign name, from a fixed twelve-term lookup.
    zodiac: str | None = None
    #: Other names, notably the Han form. Deliberately NOT searchable: `name`
    #: stays the romanised form because that is what a reader can type.
    aliases: list[PersonAliasOut] = []
    external_ids: dict[str, str] = {}
    titles: list[TitleBrief] = []


class StudioOut(BaseModel):
    key: str
    name: str
    homepage_url: str | None = None
    title_count: int


class RunOut(BaseModel):
    id: int
    source: str
    job_type: str
    run_date: str
    state: str
    manual: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    items_done: int | None = None
    items_total: int | None = None
    eta_seconds: int | None = None
    rate_per_sec: float | None = None
    stats: dict = {}
