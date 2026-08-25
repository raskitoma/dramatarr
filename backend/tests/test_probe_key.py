"""Does the "you already asked for this" flag ever fire on a DramaDex search result?"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RequestStatus, SeriesRequest, User, UserRole

pytestmark = pytest.mark.anyio


async def test_probe(db: AsyncSession) -> None:
    from app.schemas.requests import DiscoveredIds
    from app.services import requests as rq

    user = User(email="p@example.com", display_name="P", role=UserRole.VIEWER, locale="es")
    db.add(user)
    await db.commit()

    # What create() stores for a DramaDex card the member asked for.
    stored = rq.dedupe_key(DiscoveredIds(), "Bound by Love", 2025)
    db.add(SeriesRequest(user_id=user.id, title="Bound by Love",
                         status=RequestStatus.PENDING, dedupe_key=stored))
    await db.commit()

    # What the search card carries when the same show comes back.
    card = rq.RequestCandidate(key="dramadex:1065", title="Bound by Love", year=2025)
    await rq._mark_requested(db, user.id, [card])

    print(f"\n  stored dedupe_key : {stored!r}")
    print(f"  search card key   : {card.key!r}")
    print(f"  card.requested    : {card.requested}")
    assert card.requested, "the member is not told they already asked for this"
