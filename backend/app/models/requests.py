"""What a member wishes were in the library.

This table is a **wishlist**, and the distinction is the whole design. Nothing here queues a
download, reaches a source, or is read by a worker: a row is a sentence one household member
wrote down for another to act on, and the acting happens off-system — the admin finds the
show by whatever means they normally would, puts the files on the NAS, and marks the row
done. Every column exists to make that hand-off legible, not to automate it.

Two consequences worth stating, because both look like omissions otherwise:

* **`poster_url` and `source_url` are remote and are never fetched by this server.** They are
  what the requester was looking at when they chose — a picture and a link the *admin's
  browser* opens. The write path allowlists the poster host for that reason (see
  `app.services.requests`); nothing downloads either one.
* **The identifiers are recorded, not resolved.** A TMDB id turns the admin's later search
  into one click, and a MyDramaList or DramaBoxDB id is often the only address a short-form
  drama has anywhere. They travel as leads. `series_id` is set by hand, at the end, when the
  admin says *this row became that series*.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, created_column, pk, updated_column

if TYPE_CHECKING:
    from app.models.identity import User
    from app.models.library import Series


class RequestStatus(enum.StrEnum):
    """Where a request sits between "somebody asked" and "it is done".

    Four states rather than a boolean because the admin's answer is genuinely one of four
    things, and collapsing any pair costs the requester the information they came for:
    nobody has looked yet, somebody is on it, it is in the library now, or it is not going to
    happen. `DECLINED` keeps the row instead of deleting it — a request that quietly vanishes
    reads as a bug and gets made again next week.
    """

    PENDING = "pending"
    APPROVED = "approved"
    FULFILLED = "fulfilled"
    DECLINED = "declined"


#: The statuses that still want the admin's attention, in the order the admin should see them.
OPEN_STATUSES: tuple[str, ...] = (RequestStatus.PENDING, RequestStatus.APPROVED)


class SeriesRequest(Base):
    __tablename__ = "series_requests"

    id: Mapped[str] = pk()

    #: CASCADE, like every other per-member row (progress, sessions, passkeys). A member who
    #: leaves the household takes their wishlist with them; keeping orphaned rows would leave
    #: the admin a queue of requests with nobody to deliver them to.
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: What the requester chose, as they saw it. Not normalized and not corrected — if the
    #: admin has to ask "what did they actually mean", the answer should be on the row.
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Remote, display-only, host-allowlisted on write. Null is ordinary: most short-form
    #: drama has no poster anywhere this app is allowed to read.
    poster_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: The page the requester picked, for the admin to open. Never fetched here.
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: `themoviedb.org`, `mydramalist.com` — derived from `source_url` by the server rather
    #: than accepted from the client, since it is the field the admin reads to decide how much
    #: to trust the row.
    source_domain: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    #: Promoted to their own columns because they are the two the rest of the app can act on.
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Everything else `discovery.extract_ids` found — mydramalist, dramaboxdb, wikipedia,
    #: wikidata. A JSON blob rather than four more columns: nothing queries them, they are
    #: printed as links, and the set grows every time a new site is recognized.
    external_ids: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    #: The strongest identifier this row has, as a string, or a normalized title+year when it
    #: has none. What makes "you already asked for this" answerable without a fuzzy match.
    dedupe_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)

    #: The requester's own words. "Please the Korean one, not the remake."
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), default=RequestStatus.PENDING, nullable=False, index=True
    )
    #: The admin's answer, shown to the requester. The reason a decline is not just a colour.
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when the admin says this request became that series. SET NULL rather than CASCADE:
    #: deleting a series must not erase the record that somebody asked for it.
    series_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: When the *requester* acknowledged a terminal answer. NULL means they have not been told
    #: yet, which is what the alert counts. Server-side rather than in the browser because this
    #: household reads the app on three devices, and a badge that clears on one and nags on the
    #: other two is worse than no badge.
    seen_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()

    #: Both relationships name their join explicitly: two columns point at `users`, so
    #: SQLAlchemy cannot pick one on its own.
    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="selectin")
    resolved_by: Mapped[User | None] = relationship(
        foreign_keys=[resolved_by_user_id], lazy="selectin"
    )
    series: Mapped[Series | None] = relationship(lazy="selectin")

    __table_args__ = (
        # One row per member per title, for every status including declined. Re-asking for
        # something that was already answered should reach the existing answer, not bury it
        # under a second identical row — the create path turns this into "here is what you
        # already asked, and here is what came back".
        UniqueConstraint("user_id", "dedupe_key", name="uq_series_request_user_key"),
        # The admin screen's default view: open requests, oldest first.
        Index("ix_series_requests_status_created", "status", "created_at"),
    )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def awaits_the_requester(self) -> bool:
        """An answer this member has not seen.

        Only terminal answers count. `pending` and `approved` are the admin saying "not yet",
        and nagging somebody about a thing that has not happened is how people learn to ignore
        a badge.
        """
        return self.seen_at is None and self.status in {
            RequestStatus.FULFILLED,
            RequestStatus.DECLINED,
        }


__all__ = ["OPEN_STATUSES", "RequestStatus", "SeriesRequest"]
