"""Telling a member their request landed.

Until this, a fulfilled request changed colour on a screen nobody had a reason to open. For the
way this household actually uses the app — ask, forget, come back weeks later — that means
finding out late or not at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RequestStatus, SeriesRequest, User, UserRole

pytestmark = pytest.mark.anyio


async def a_member(db: AsyncSession, email: str) -> User:
    user = User(email=email, display_name="V", role=UserRole.VIEWER, locale="es")
    db.add(user)
    await db.commit()
    return user


_next_key = iter(range(1000))


async def a_request(db: AsyncSession, user: User, status: str, **fields) -> SeriesRequest:
    # `dedupe_key` is what stops one member asking twice for the same title; unique per row here
    # so the fixtures do not collide with each other.
    row = SeriesRequest(
        user_id=user.id,
        title="Djinn Under Contract",
        status=status,
        dedupe_key=f"test:{next(_next_key)}",
        **fields,
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RequestStatus.FULFILLED, True),
        (RequestStatus.DECLINED, True),
        # "Not yet" is not news. Nagging somebody about a thing that has not happened is how
        # they learn to ignore the badge.
        (RequestStatus.PENDING, False),
        (RequestStatus.APPROVED, False),
    ],
)
async def test_only_a_terminal_answer_is_worth_telling_them(
    db: AsyncSession, status: str, expected: bool
) -> None:
    user = await a_member(db, f"{status}@example.com")
    row = await a_request(db, user, status)

    assert row.awaits_the_requester is expected


async def test_an_answer_already_seen_stops_counting(db: AsyncSession) -> None:
    from app.utils.time import utcnow

    user = await a_member(db, "seen@example.com")
    row = await a_request(db, user, RequestStatus.FULFILLED, seen_at=utcnow())

    assert row.awaits_the_requester is False


async def test_acknowledging_clears_every_answered_row_at_once(db: AsyncSession) -> None:
    """The page *is* the notification; dismissing six things you have just read is a chore
    invented by the software."""
    from app.services import requests as requests_service

    user = await a_member(db, "ack@example.com")
    await a_request(db, user, RequestStatus.FULFILLED)
    await a_request(db, user, RequestStatus.DECLINED)
    still_open = await a_request(db, user, RequestStatus.PENDING)

    cleared = await requests_service.acknowledge(db, user)

    assert cleared == 2
    await db.refresh(still_open)
    # The open one is untouched: there is nothing to acknowledge yet.
    assert still_open.seen_at is None


async def test_acknowledging_never_touches_somebody_elses(db: AsyncSession) -> None:
    """This route is reachable by every member, so the scope is in the query rather than in
    the caller's good manners."""
    from app.services import requests as requests_service

    mine = await a_member(db, "mine@example.com")
    theirs = await a_member(db, "theirs@example.com")
    await a_request(db, mine, RequestStatus.FULFILLED)
    other = await a_request(db, theirs, RequestStatus.FULFILLED)

    assert await requests_service.acknowledge(db, mine) == 1

    await db.refresh(other)
    assert other.seen_at is None
    assert other.awaits_the_requester is True


async def test_the_wire_carries_the_flag(db: AsyncSession) -> None:
    """Asserted on the serialized shape: a field missing from the schema is stripped by
    `response_model` while every assertion on the Python object still passes."""
    from app.services import requests as requests_service

    user = await a_member(db, "wire@example.com")
    row = await a_request(db, user, RequestStatus.FULFILLED)

    assert requests_service.request_out(row).model_dump(by_alias=True)["unseen"] is True


async def test_a_request_answered_before_this_existed_is_still_news(db: AsyncSession) -> None:
    """Existing rows migrate to NULL, which reads as "not seen". That is correct rather than
    convenient: nobody has ever been told about them."""
    user = await a_member(db, "legacy@example.com")
    row = await a_request(db, user, RequestStatus.FULFILLED)

    assert row.seen_at is None
    assert row.awaits_the_requester is True
