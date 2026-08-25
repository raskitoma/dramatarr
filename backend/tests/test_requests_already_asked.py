"""Telling a member they already asked for this.

The card carries two identities and they were being confused for each other. `key` is who the
card is *in this result set* — `dramadex:1065`, which is also what React lists by. `dedupe_key`
is who it becomes *as a stored row* — `title:bound by love:2026`. Matching one against the
other can never hit, so this flag was dead for every result the search returned; and since the
DramaDex client became the only search path, that is all of them.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RequestStatus, SeriesRequest, User, UserRole

pytestmark = pytest.mark.anyio


async def a_member(db: AsyncSession, email: str) -> User:
    user = User(email=email, display_name="M", role=UserRole.VIEWER, locale="es")
    db.add(user)
    await db.commit()
    return user


async def an_asked_for(db: AsyncSession, user: User, title: str, year: int | None) -> str:
    from app.schemas.requests import DiscoveredIds
    from app.services.requests import dedupe_key

    key = dedupe_key(DiscoveredIds(), title, year)
    db.add(
        SeriesRequest(
            user_id=user.id, title=title, year=year, status=RequestStatus.PENDING, dedupe_key=key
        )
    )
    await db.commit()
    return key


def a_card(title: str, year: int | None, dramadex_id: int):
    from app.schemas.requests import RequestCandidate

    # Exactly the shape `_candidate_from` builds: the key names the DramaDex title, which is
    # the namespace no stored row has ever used.
    return RequestCandidate(key=f"dramadex:{dramadex_id}", title=title, year=year)


async def test_the_card_says_you_already_asked(db: AsyncSession) -> None:
    from app.services.requests import _mark_requested

    user = await a_member(db, "asked@example.com")
    await an_asked_for(db, user, "Bound by Love", 2026)

    card = a_card("Bound by Love", 2026, 1065)
    await _mark_requested(db, user.id, [card])

    assert card.requested is True
    assert card.request_status == RequestStatus.PENDING


async def test_it_carries_the_answer_they_got(db: AsyncSession) -> None:
    """A decline is the case that matters most: offering the button again invites them to ask
    a second time for something already answered no."""
    from app.services.requests import _mark_requested

    user = await a_member(db, "declined@example.com")
    key = await an_asked_for(db, user, "Bound by Love", 2026)
    row = (await db.execute(__import__("sqlalchemy").select(SeriesRequest))).scalars().one()
    assert row.dedupe_key == key
    row.status = RequestStatus.DECLINED
    await db.commit()

    card = a_card("Bound by Love", 2026, 1065)
    await _mark_requested(db, user.id, [card])

    assert card.request_status == RequestStatus.DECLINED


async def test_a_yearless_card_matches_the_yearless_row(db: AsyncSession) -> None:
    """NetShort publishes no usable date, so every one of its titles takes this branch —
    `title:<name>` with no year on either side."""
    from app.services.requests import _mark_requested

    user = await a_member(db, "yearless@example.com")
    await an_asked_for(db, user, "Right Beside Me", None)

    card = a_card("Right Beside Me", None, 13032)
    await _mark_requested(db, user.id, [card])

    assert card.requested is True


async def test_a_yearless_card_is_not_confused_with_a_dated_one(db: AsyncSession) -> None:
    """The other side of it: `title:x` and `title:x:2026` are deliberately different keys, so a
    NetShort card must not claim the member asked for the dated row."""
    from app.services.requests import _mark_requested

    user = await a_member(db, "mixed@example.com")
    await an_asked_for(db, user, "Right Beside Me", 2026)

    card = a_card("Right Beside Me", None, 13032)
    await _mark_requested(db, user.id, [card])

    assert card.requested is False


async def test_somebody_elses_request_is_not_theirs(db: AsyncSession) -> None:
    from app.services.requests import _mark_requested

    mine = await a_member(db, "mine2@example.com")
    theirs = await a_member(db, "theirs2@example.com")
    await an_asked_for(db, theirs, "Bound by Love", 2026)

    card = a_card("Bound by Love", 2026, 1065)
    await _mark_requested(db, mine.id, [card])

    assert card.requested is False
