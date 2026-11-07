"""Content-addressed archive of raw responses.

This is the highest-leverage piece of the design. Every response is stored
before it is parsed, which means:

  * parsers can be iterated offline against real bytes, with no network;
  * `reparse` re-runs a fixed parser over the whole corpus for zero requests;
  * "did anything change?" is a hash comparison, not a diff of parsed output.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from app.fetch.types import RawDoc


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


class Archive:
    def __init__(self, root: Path, keep_versions: int = 1) -> None:
        self.root = Path(root)
        self.keep_versions = max(1, keep_versions)
        self.blobs = self.root / "blobs"
        self.index = self.root / "index"

    def _blob_path(self, sha: str) -> Path:
        return self.blobs / sha[:2] / f"{sha}.gz"

    def _index_path(self, url: str) -> Path:
        key = _url_key(url)
        return self.index / key[:2] / f"{key}.json"

    def put(self, doc: RawDoc) -> str:
        """Store a response. Returns its sha256."""
        sha = doc.sha256
        blob = self._blob_path(sha)
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            tmp = blob.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(doc.body))
            tmp.replace(blob)

        entry = self.meta(doc.url) or {"url": doc.url, "versions": []}
        entry["latest_sha"] = sha
        entry["status"] = doc.status
        entry["fetched_at"] = doc.fetched_at.isoformat()
        entry["etag"] = doc.header("etag")
        entry["last_modified"] = doc.header("last-modified")
        entry["headers"] = dict(doc.headers)

        versions: list[str] = [v for v in entry.get("versions", []) if v != sha]
        versions.append(sha)
        dropped = versions[: -self.keep_versions]
        entry["versions"] = versions[-self.keep_versions :]

        path = self._index_path(doc.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry))

        # Only remove a superseded blob if no other URL still points at it.
        for old in dropped:
            self._maybe_drop_blob(old)
        return sha

    def _maybe_drop_blob(self, sha: str) -> None:
        for meta_file in self.index.rglob("*.json"):
            try:
                data = json.loads(meta_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if sha in data.get("versions", []) or data.get("latest_sha") == sha:
                return
        self._blob_path(sha).unlink(missing_ok=True)

    def meta(self, url: str) -> dict | None:
        path = self._index_path(url)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def conditional_headers(self, url: str) -> dict[str, str]:
        """ETag / Last-Modified for a revalidating request."""
        meta = self.meta(url)
        if not meta:
            return {}
        headers = {}
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
        return headers

    def load(self, sha: str) -> bytes | None:
        blob = self._blob_path(sha)
        if not blob.exists():
            return None
        return gzip.decompress(blob.read_bytes())

    def get(self, url: str) -> RawDoc | None:
        """Rebuild the latest archived response for a URL."""
        meta = self.meta(url)
        if not meta or not meta.get("latest_sha"):
            return None
        body = self.load(meta["latest_sha"])
        if body is None:
            return None
        return RawDoc(
            url=url,
            status=meta.get("status", 200),
            headers=meta.get("headers", {}),
            body=body,
            fetched_at=datetime.fromisoformat(
                meta.get("fetched_at", datetime.now(UTC).isoformat())
            ),
        )

    def iter_urls(self) -> list[str]:
        """Every archived URL. This is what `reparse` walks."""
        urls: list[str] = []
        if not self.index.exists():
            return urls
        for meta_file in self.index.rglob("*.json"):
            try:
                urls.append(json.loads(meta_file.read_text())["url"])
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return urls
