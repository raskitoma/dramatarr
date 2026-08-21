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


# --- The mail, which is the half that reaches somebody not in the app --------------------------


async def test_the_requester_is_told_when_it_lands(db: AsyncSession, monkeypatch) -> None:
    from app.models import Series, UserRole
    from app.schemas.requests import RequestPatchIn
    from app.services import requests as requests_service

    sent: list[tuple[str, str, dict]] = []

    async def fake_send(_session, to, template, _locale, context):
        sent.append((to, template, context))

    monkeypatch.setattr(requests_service, "send_mail", fake_send)

    admin = User(email="a@example.com", display_name="A", role=UserRole.ADMIN, locale="es")
    user = await a_member(db, "wants@example.com")
    series = Series(
        title="Djinn Under Contract",
        slug="djinn-under-contract",
        sort_title="djinn",
        library_path="Djinn",
        year=2026,
    )
    db.add_all([admin, series])
    await db.commit()
    row = await a_request(db, user, RequestStatus.PENDING)

    await requests_service.update(
        db, admin, row.id, RequestPatchIn(status="fulfilled", seriesId=series.id)
    )

    assert len(sent) == 1
    to, template, context = sent[0]
    assert to == "wants@example.com"
    assert template == "request_ready"
    # The series' own title and a link that resolves, not the wishlist text the member typed.
    assert context["title"] == "Djinn Under Contract"
    assert context["series_url"].endswith("/series/djinn-under-contract")


async def test_editing_a_delivered_request_does_not_announce_it_again(
    db: AsyncSession, monkeypatch
) -> None:
    """Only the *transition* sends mail. An admin fixing a typo in the note on something
    delivered last week must not post the announcement a second time."""
    from app.models import UserRole
    from app.schemas.requests import RequestPatchIn
    from app.services import requests as requests_service

    sent: list[str] = []

    async def fake_send(_session, to, _template, _locale, _context):
        sent.append(to)

    monkeypatch.setattr(requests_service, "send_mail", fake_send)

    admin = User(email="a2@example.com", display_name="A", role=UserRole.ADMIN, locale="es")
    db.add(admin)
    user = await a_member(db, "already@example.com")
    row = await a_request(db, user, RequestStatus.FULFILLED)
    await db.commit()

    await requests_service.update(db, admin, row.id, RequestPatchIn(adminNote="typo fixed"))

    assert sent == []


async def test_a_decline_is_not_mailed(db: AsyncSession, monkeypatch) -> None:
    """The in-app alert covers a decline — it carries the admin's reason, which is the point of
    it. A mail saying no is a different decision and not one this makes on the owner's behalf."""
    from app.models import UserRole
    from app.schemas.requests import RequestPatchIn
    from app.services import requests as requests_service

    sent: list[str] = []

    async def fake_send(_session, to, _template, _locale, _context):
        sent.append(to)

    monkeypatch.setattr(requests_service, "send_mail", fake_send)

    admin = User(email="a3@example.com", display_name="A", role=UserRole.ADMIN, locale="es")
    db.add(admin)
    user = await a_member(db, "declined@example.com")
    row = await a_request(db, user, RequestStatus.PENDING)
    await db.commit()

    await requests_service.update(db, admin, row.id, RequestPatchIn(status="declined"))

    assert sent == []


async def test_unconfigured_mail_does_not_break_the_admin_action(
    db: AsyncSession, monkeypatch
) -> None:
    """A row that was delivered has been delivered whether or not SMTP was reachable."""
    from app.errors import MailNotConfigured
    from app.models import UserRole
    from app.schemas.requests import RequestPatchIn
    from app.services import requests as requests_service

    async def refuse(*_args, **_kwargs):
        raise MailNotConfigured()

    monkeypatch.setattr(requests_service, "send_mail", refuse)

    admin = User(email="a4@example.com", display_name="A", role=UserRole.ADMIN, locale="es")
    db.add(admin)
    user = await a_member(db, "nomail@example.com")
    row = await a_request(db, user, RequestStatus.PENDING)
    await db.commit()

    out = await requests_service.update(db, admin, row.id, RequestPatchIn(status="fulfilled"))

    assert out.status == "fulfilled"


async def test_a_fulfilled_request_with_no_series_still_links_somewhere(
    db: AsyncSession, monkeypatch
) -> None:
    """An admin can mark a request done without binding it, and a mail whose only link 404s is
    worse than a mail with no link."""
    from app.models import UserRole
    from app.schemas.requests import RequestPatchIn
    from app.services import requests as requests_service

    captured: list[dict] = []

    async def fake_send(_session, _to, _template, _locale, context):
        captured.append(context)

    monkeypatch.setattr(requests_service, "send_mail", fake_send)

    admin = User(email="a5@example.com", display_name="A", role=UserRole.ADMIN, locale="es")
    db.add(admin)
    user = await a_member(db, "unbound@example.com")
    row = await a_request(db, user, RequestStatus.PENDING)
    await db.commit()

    await requests_service.update(db, admin, row.id, RequestPatchIn(status="fulfilled"))

    assert captured[0]["series_url"].endswith("/requests")
