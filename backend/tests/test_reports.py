"""Exercise report privacy, moderation and the complete report lifecycle."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user_optional
from app.main import create_app
from app.utils.time import utcnow

pytestmark = pytest.mark.anyio


async def test_reports_are_private_and_moderated(db, admin_user, viewer_user):
    app = create_app()
    current = None
    app.dependency_overrides[get_current_user_optional] = lambda: current
    payload = {
        "category": "error",
        "section": "/admin/queue",
        "message": "Shared context",
        "errors": [
            {
                "message": f"Source error {index}",
                "section": "/admin/queue",
                "timestamp": utcnow().isoformat().replace("+00:00", "Z"),
                "comment": f"Context {index}",
            }
            for index in range(2)
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/reports", json=payload)).status_code == 401
        current = viewer_user
        assert (await client.get("/api/admin/reports")).status_code == 403
        assert (await client.post("/api/reports", json=payload)).status_code == 204

        current = admin_user
        response = await client.get("/api/admin/reports")
        assert response.status_code == 200
        report = response.json()[0]
        assert report["reporter"] == viewer_user.email
        assert report["errors"] == payload["errors"]
        path = f"/api/admin/reports/{report['id']}"
        response = await client.patch(path, json={"priority": "critical", "note": "Investigating"})
        assert response.status_code == 200
        assert response.json()["priority"] == "critical"
        response = await client.patch(path, json={"closed": True, "note": "Resolved"})
        assert response.json()["closed"] is True
        assert [note["message"] for note in response.json()["notes"]] == [
            "Investigating",
            "Resolved",
        ]
        block_path = f"/api/admin/reports/blocks/{viewer_user.id}"
        assert (await client.put(block_path)).status_code == 204
        assert (await client.get("/api/admin/reports/blocks")).json()[0]["id"] == viewer_user.id

        current = viewer_user
        assert (await client.post("/api/reports", json=payload)).status_code == 403
        assert (await client.delete(block_path)).status_code == 403
        current = admin_user
        assert (await client.delete(block_path)).status_code == 204
        current = viewer_user
        empty = {"category": "feature", "section": "/"}
        assert (await client.post("/api/reports", json=empty)).status_code == 422
        assert (
            await client.post("/api/reports", json={**empty, "message": "Add an option"})
        ).status_code == 204
