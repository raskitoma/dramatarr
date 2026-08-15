"""End-to-end cover for the folder-level import assistant.

The invariant every test here defends is the one the whole application rests on: `LIBRARY_ROOT`
is mounted read-only, so importing a folder writes **catalogue rows** and nothing else. Every
test that applies a plan snapshots the mount before and asserts it is byte-for-byte identical
afterwards — names, sizes and mtimes — because a regression that starts renaming files on a
NAS is not the kind of bug you find out about from a failing assertion later.

The fixture is the production case that motivated the feature: a folder of 58 files called
`701151231.720p.narrowv3.mp4` … `701151288.720p.narrowv3.mp4`, a shared prefix and suffix
around a contiguous run of integers, whose correct reading is episodes 1..58 in numeric order.

The rename script gets the same treatment from the other side: it is checked with `bash -n`,
then actually run — against a *copy* of the folder in the tmp tree, never against the library —
so that "properly quoted" means the shell agrees, not that the string looks plausible.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Episode, MediaFile, Series, User

pytestmark = pytest.mark.anyio

#: The real folder from production. Shared prefix, shared suffix, contiguous integers.
OPAQUE_FOLDER = "Tempest The Last Mecha"
OPAQUE_FIRST = 701151231
OPAQUE_COUNT = 58

#: A folder name that needs real shell quoting: spaces and an apostrophe.
QUOTED_FOLDER = "Cindy's Tempest (2019)"
QUOTED_NAMES = ("IMG_0012.MP4", "IMG_0013.MP4", "IMG_0014.MP4")

_FILE_BODY = b"pretend this is an mp4"


# --- Helpers ---------------------------------------------------------------------------------


def opaque_names() -> list[str]:
    return [f"{OPAQUE_FIRST + offset}.720p.narrowv3.mp4" for offset in range(OPAQUE_COUNT)]


def tree(root: Path) -> list[tuple[str, int, int]]:
    """Every file under `root` with its size and mtime — the mount, as the tests know it.

    mtime is included deliberately: a rename that happened to preserve names and sizes would
    still show up here, and so would a file rewritten in place.
    """
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size, int(path.stat().st_mtime_ns))
        for path in root.rglob("*")
        if path.is_file()
    )


async def count(session: AsyncSession, model: Any, *where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int((await session.execute(stmt)).scalar_one())


async def seed_folder(
    session: AsyncSession, root: Path, folder: str, names: list[str]
) -> list[MediaFile]:
    """Real files on the mount plus the `media_files` rows a scan would have written.

    `episode_id` is null on every row, which is precisely what "unmatched" means: the scanner
    could not read an episode number out of the name and refused to guess.
    """
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    rows: list[MediaFile] = []
    for name in names:
        on_disk = directory / name
        on_disk.write_bytes(_FILE_BODY)
        stat = on_disk.stat()
        media = MediaFile(
            path=f"{folder}/{name}",
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
            container="mp4",
            video_codec="h264",
            audio_codec="aac",
            width=1280,
            height=720,
            duration_seconds=1500.0,
        )
        session.add(media)
        rows.append(media)
    await session.flush()
    return rows


def assignments_of(plan: dict[str, Any]) -> list[dict[str, int | str]]:
    """A plan's assignments in the shape `apply` wants them back."""
    return [
        {
            "mediaFileId": item["mediaFileId"],
            "seasonNumber": item["seasonNumber"],
            "episodeNumber": item["episodeNumber"],
        }
        for item in plan["assignments"]
    ]


def numbering(plan: dict[str, Any]) -> dict[str, int]:
    return {item["filename"]: item["episodeNumber"] for item in plan["assignments"]}


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


async def bash(*args: str) -> subprocess.CompletedProcess[str]:
    """Run bash off the event loop, so the async test does not block it."""
    return await asyncio.to_thread(
        subprocess.run, ["bash", *args], capture_output=True, text=True, check=False
    )


# --- Fixtures --------------------------------------------------------------------------------


@pytest.fixture
def library_root(tmp_env: Path) -> Path:
    from app.config import get_settings

    return get_settings().library_root


@pytest.fixture
async def library(db: AsyncSession, library_root: Path) -> dict[str, list[MediaFile]]:
    return {
        OPAQUE_FOLDER: await seed_folder(db, library_root, OPAQUE_FOLDER, opaque_names()),
        QUOTED_FOLDER: await seed_folder(db, library_root, QUOTED_FOLDER, list(QUOTED_NAMES)),
    }


def _client(db: AsyncSession, user: User, *, admin: bool) -> AsyncClient:
    """The real app over ASGI on the test's own session.

    A viewer gets `get_current_user` overridden and nothing else, so `require_admin` runs for
    real and the 403 under test is the one production would raise.
    """
    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: user
    if admin:
        app.dependency_overrides[require_admin] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(db: AsyncSession, admin_user: User) -> AsyncIterator[AsyncClient]:
    from app.main import app

    try:
        async with _client(db, admin_user, admin=True) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def viewer_client(db: AsyncSession, viewer_user: User) -> AsyncIterator[AsyncClient]:
    from app.main import app

    try:
        async with _client(db, viewer_user, admin=False) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def opaque_plan(client: AsyncClient, library: dict[str, list[MediaFile]]) -> dict[str, Any]:
    response = await client.post("/api/library/unmatched/plan", json={"folder": OPAQUE_FOLDER})
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- Groups ----------------------------------------------------------------------------------


async def test_groups_lists_each_folder_with_a_complete_plan(
    client: AsyncClient, library: dict[str, list[MediaFile]], library_root: Path
) -> None:
    before = tree(library_root)
    response = await client.get("/api/library/unmatched/groups")
    assert response.status_code == 200, response.text

    groups = {group["folder"]: group for group in response.json()}
    assert set(groups) == {OPAQUE_FOLDER, QUOTED_FOLDER}

    group = groups[OPAQUE_FOLDER]
    assert group["fileCount"] == OPAQUE_COUNT
    assert group["totalBytes"] == OPAQUE_COUNT * len(_FILE_BODY)
    assert group["suggestedSeriesTitle"] == OPAQUE_FOLDER
    assert group["existingSeriesId"] is None
    assert group["strategies"], "a group with no strategy offers the admin nothing to flip"
    assert all(0.0 <= strategy["confidence"] <= 1.0 for strategy in group["strategies"])

    # The whole point of the feature: 58 opaque ids read as episodes 1..58 in numeric order.
    plan = group["plan"]
    assert len(plan["assignments"]) == OPAQUE_COUNT
    assert numbering(plan) == {name: index + 1 for index, name in enumerate(opaque_names())}
    assert {item["seasonNumber"] for item in plan["assignments"]} == {1}
    assert plan["hasConflicts"] is False

    # Listing is a read of rows the scan already wrote; it opens nothing on the mount.
    assert tree(library_root) == before


async def test_groups_offers_an_existing_series_to_attach_to(
    client: AsyncClient,
    db: AsyncSession,
    library: dict[str, list[MediaFile]],
    library_root: Path,
    opaque_plan: dict[str, Any],
) -> None:
    """A season subfolder of an already-indexed series must not offer to create a second one."""
    applied = await client.post(
        "/api/library/unmatched/apply",
        json={"folder": OPAQUE_FOLDER, "assignments": assignments_of(opaque_plan)},
    )
    assert applied.status_code == 200, applied.text
    series_id = applied.json()["seriesId"]

    subfolder = f"{OPAQUE_FOLDER}/Season 02"
    await seed_folder(db, library_root, subfolder, ["801151201.720p.narrowv3.mp4"])

    groups = {
        group["folder"]: group
        for group in (await client.get("/api/library/unmatched/groups")).json()
    }
    assert subfolder in groups
    assert groups[subfolder]["existingSeriesId"] == series_id
    assert groups[subfolder]["plan"]["existingSeriesId"] == series_id
    # A folder that names its own season is believed, and the *series* title is read one
    # level up rather than proposing a series called "Season 02".
    assert groups[subfolder]["plan"]["seasonNumber"] == 2
    assert groups[subfolder]["suggestedSeriesTitle"] == OPAQUE_FOLDER
    # The folder that was just imported has no unmatched files left, so it is gone from view.
    assert OPAQUE_FOLDER not in groups


# --- Planning --------------------------------------------------------------------------------


async def test_plan_follows_season_and_start_overrides(
    client: AsyncClient, library: dict[str, list[MediaFile]]
) -> None:
    response = await client.post(
        "/api/library/unmatched/plan",
        json={"folder": OPAQUE_FOLDER, "seasonNumber": 2, "startEpisode": 5},
    )
    assert response.status_code == 200, response.text
    plan = response.json()

    assert plan["folder"] == OPAQUE_FOLDER
    assert plan["seasonNumber"] == 2
    assert {item["seasonNumber"] for item in plan["assignments"]} == {2}
    # Shifting the start slides the whole run without reordering it.
    assert sorted(item["episodeNumber"] for item in plan["assignments"]) == list(
        range(5, 5 + OPAQUE_COUNT)
    )
    assert numbering(plan) == {name: index + 5 for index, name in enumerate(opaque_names())}
    assert plan["assignments"][0]["proposedFilename"].endswith("S02E05.mp4")


async def test_plan_follows_a_chosen_strategy(
    client: AsyncClient, library: dict[str, list[MediaFile]], opaque_plan: dict[str, Any]
) -> None:
    names = [strategy["name"] for strategy in opaque_plan["strategies"]]
    assert names, "the planner must name the strategies it considered"
    # Whatever the admin picks off the list is what comes back, winner or not.
    chosen = next((name for name in names if name != opaque_plan["strategy"]), names[0])

    response = await client.post(
        "/api/library/unmatched/plan", json={"folder": OPAQUE_FOLDER, "strategy": chosen}
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["strategy"] == chosen
    assert plan["fileCount"] == OPAQUE_COUNT


async def test_plan_refuses_an_unknown_folder_and_a_traversal(client: AsyncClient) -> None:
    missing = await client.post("/api/library/unmatched/plan", json={"folder": "Nowhere"})
    assert missing.status_code == 404
    assert error_code(missing) == "IMPORT_FOLDER_NOT_FOUND"

    escape = await client.post("/api/library/unmatched/plan", json={"folder": "../../etc/systemd"})
    assert escape.status_code == 403
    assert error_code(escape) == "PATH_TRAVERSAL"


# --- Applying --------------------------------------------------------------------------------


async def test_apply_creates_the_series_and_attaches_every_file(
    client: AsyncClient,
    db: AsyncSession,
    library: dict[str, list[MediaFile]],
    library_root: Path,
    opaque_plan: dict[str, Any],
) -> None:
    before = tree(library_root)
    response = await client.post(
        "/api/library/unmatched/apply",
        json={"folder": OPAQUE_FOLDER, "assignments": assignments_of(opaque_plan)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["created"] == {"series": True, "episodes": OPAQUE_COUNT}
    assert payload["attached"] == OPAQUE_COUNT

    series = await db.get(Series, payload["seriesId"])
    assert series is not None
    assert series.slug == payload["seriesSlug"]
    assert series.title == OPAQUE_FOLDER
    assert series.library_path == OPAQUE_FOLDER

    numbers = sorted(
        (await db.execute(select(Episode.episode_number).where(Episode.series_id == series.id)))
        .scalars()
        .all()
    )
    assert numbers == list(range(1, OPAQUE_COUNT + 1))

    attached = (
        (await db.execute(select(MediaFile).where(MediaFile.path.like(f"{OPAQUE_FOLDER}/%"))))
        .scalars()
        .all()
    )
    assert len(attached) == OPAQUE_COUNT
    assert all(media.episode_id is not None for media in attached)
    # The catalogue learned what these files are; the files kept the names they had.
    assert sorted(media.path for media in attached) == sorted(
        f"{OPAQUE_FOLDER}/{name}" for name in opaque_names()
    )
    assert tree(library_root) == before


async def test_apply_twice_is_idempotent(
    client: AsyncClient,
    db: AsyncSession,
    library: dict[str, list[MediaFile]],
    library_root: Path,
    opaque_plan: dict[str, Any],
) -> None:
    body = {"folder": OPAQUE_FOLDER, "assignments": assignments_of(opaque_plan)}
    first = (await client.post("/api/library/unmatched/apply", json=body)).json()

    before = tree(library_root)
    second_response = await client.post("/api/library/unmatched/apply", json=body)
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()

    assert second["seriesId"] == first["seriesId"]
    assert second["seriesSlug"] == first["seriesSlug"]
    # Nothing left to create and nothing left to attach — the second run is a no-op.
    assert second["created"] == {"series": False, "episodes": 0}
    assert second["attached"] == 0

    assert await count(db, Series) == 1
    assert await count(db, Episode, Episode.series_id == first["seriesId"]) == OPAQUE_COUNT
    assert tree(library_root) == before


async def test_apply_refuses_a_colliding_assignment(
    client: AsyncClient,
    db: AsyncSession,
    library: dict[str, list[MediaFile]],
    library_root: Path,
    opaque_plan: dict[str, Any],
) -> None:
    first = (
        await client.post(
            "/api/library/unmatched/apply",
            json={"folder": OPAQUE_FOLDER, "assignments": assignments_of(opaque_plan)},
        )
    ).json()
    series_id = first["seriesId"]

    before = tree(library_root)
    # Episode 1 already holds a present file; sending a *different* file at that number is
    # the mistake the admin must be told about rather than have silently resolved.
    intruder = opaque_plan["assignments"][7]["mediaFileId"]
    collision = await client.post(
        "/api/library/unmatched/apply",
        json={
            "folder": OPAQUE_FOLDER,
            "seriesId": series_id,
            "assignments": [{"mediaFileId": intruder, "seasonNumber": 1, "episodeNumber": 1}],
        },
    )
    assert collision.status_code == 409
    assert error_code(collision) == "EPISODE_NUMBER_TAKEN"

    # Refused before the first row was written: the intruder is still on its own episode.
    media = await db.get(MediaFile, intruder)
    assert media is not None
    episode = await db.get(Episode, str(media.episode_id))
    assert episode is not None
    assert episode.episode_number == opaque_plan["assignments"][7]["episodeNumber"]
    assert await count(db, Episode, Episode.series_id == series_id) == OPAQUE_COUNT
    assert tree(library_root) == before


async def test_apply_refuses_two_files_on_one_number(
    client: AsyncClient, library: dict[str, list[MediaFile]], opaque_plan: dict[str, Any]
) -> None:
    doubled = assignments_of(opaque_plan)[:2]
    doubled[1] = {**doubled[1], "episodeNumber": doubled[0]["episodeNumber"]}

    response = await client.post(
        "/api/library/unmatched/apply", json={"folder": OPAQUE_FOLDER, "assignments": doubled}
    )
    assert response.status_code == 409
    assert error_code(response) == "EPISODE_NUMBER_TAKEN"


async def test_apply_refuses_a_file_from_another_folder(
    client: AsyncClient,
    library: dict[str, list[MediaFile]],
    opaque_plan: dict[str, Any],
) -> None:
    stranger = library[QUOTED_FOLDER][0].id
    response = await client.post(
        "/api/library/unmatched/apply",
        json={
            "folder": OPAQUE_FOLDER,
            "assignments": [{"mediaFileId": stranger, "seasonNumber": 1, "episodeNumber": 1}],
        },
    )
    assert response.status_code == 400
    assert error_code(response) == "IMPORT_FILE_NOT_IN_FOLDER"


async def test_apply_can_name_the_series_it_creates(
    client: AsyncClient, db: AsyncSession, library: dict[str, list[MediaFile]]
) -> None:
    plan = (await client.post("/api/library/unmatched/plan", json={"folder": QUOTED_FOLDER})).json()
    response = await client.post(
        "/api/library/unmatched/apply",
        json={
            "folder": QUOTED_FOLDER,
            "newSeriesTitle": "La Tempestad de Cindy",
            "newSeriesYear": 2019,
            "assignments": assignments_of(plan),
        },
    )
    assert response.status_code == 200, response.text
    series = await db.get(Series, response.json()["seriesId"])
    assert series is not None
    assert series.title == "La Tempestad de Cindy"
    assert series.year == 2019
    assert series.library_path == QUOTED_FOLDER


# --- Roles -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/api/library/unmatched/groups", {}),
        ("POST", "/api/library/unmatched/plan", {"json": {"folder": OPAQUE_FOLDER}}),
        (
            "POST",
            "/api/library/unmatched/apply",
            {
                "json": {
                    "folder": OPAQUE_FOLDER,
                    "assignments": [{"mediaFileId": "x", "seasonNumber": 1, "episodeNumber": 1}],
                }
            },
        ),
        (
            "GET",
            "/api/library/unmatched/rename-script",
            {"params": {"folder": OPAQUE_FOLDER}},
        ),
    ],
)
async def test_import_routes_are_admin_only(
    viewer_client: AsyncClient,
    library: dict[str, list[MediaFile]],
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    response = await viewer_client.request(method, path, **kwargs)
    assert response.status_code == 403
    assert error_code(response) == "ADMIN_REQUIRED"


# --- The rename script -------------------------------------------------------------------------


async def test_rename_script_is_valid_shell_and_quotes_hostile_names(
    client: AsyncClient, library: dict[str, list[MediaFile]], library_root: Path, tmp_path: Path
) -> None:
    if shutil.which("bash") is None:  # pragma: no cover - bash is present on CI and macOS
        pytest.skip("bash is required to check the generated script")

    before = tree(library_root)
    response = await client.get(
        "/api/library/unmatched/rename-script", params={"folder": QUOTED_FOLDER}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    script = response.text

    assert "set -euo pipefail" in script
    assert "mv -n --" in script
    assert ".rename-map.tsv" in script
    # The header has to say, in the artifact itself, who runs this and who does not.
    assert "did NOT run this" in script
    assert "read-only" in script

    script_path = tmp_path / "rename.sh"
    script_path.write_text(script, encoding="utf-8")
    syntax = await bash("-n", str(script_path))
    assert syntax.returncode == 0, syntax.stderr

    # Generating it is a pure read. Nothing on the mount moved.
    assert tree(library_root) == before


async def test_rename_script_runs_correctly_against_a_copy(
    client: AsyncClient, library: dict[str, list[MediaFile]], library_root: Path, tmp_path: Path
) -> None:
    """Proof that the quoting is right: the shell itself renames the copy as planned.

    The copy lives in the tmp tree. `LIBRARY_ROOT` is snapshotted around the whole thing —
    if the script ever ignored its argument and used the baked-in default, this is what
    would catch it.
    """
    if shutil.which("bash") is None:  # pragma: no cover
        pytest.skip("bash is required to run the generated script")

    plan = (await client.post("/api/library/unmatched/plan", json={"folder": QUOTED_FOLDER})).json()
    expected = sorted(item["proposedFilename"] for item in plan["assignments"])
    # The apostrophe survives into the target names, which is the whole point of the folder.
    assert len(expected) == len(QUOTED_NAMES)
    assert all(re.fullmatch(r"Cindy's Tempest - S\d\dE\d\d\.mp4", name) for name in expected)

    script = (
        await client.get("/api/library/unmatched/rename-script", params={"folder": QUOTED_FOLDER})
    ).text
    script_path = tmp_path / "rename.sh"
    script_path.write_text(script, encoding="utf-8")

    sandbox = tmp_path / "sandbox" / QUOTED_FOLDER
    shutil.copytree(library_root / QUOTED_FOLDER, sandbox)

    before = tree(library_root)
    run = await bash(str(script_path), str(sandbox))
    assert run.returncode == 0, run.stderr

    assert sorted(path.name for path in sandbox.iterdir() if path.name != ".rename-map.tsv") == (
        expected
    )
    mapping = (sandbox / ".rename-map.tsv").read_text(encoding="utf-8").splitlines()
    assert sorted(line.split("\t")[0] for line in mapping) == sorted(QUOTED_NAMES)

    # Second run: every source is already gone, so it refuses rather than clobbering.
    again = await bash(str(script_path), str(sandbox))
    assert again.returncode == 0, again.stderr
    assert "skip" in again.stderr
    assert sorted(path.name for path in sandbox.iterdir() if path.name != ".rename-map.tsv") == (
        expected
    )

    # And the library itself never moved.
    assert tree(library_root) == before
