# Consolidating existing installations

Current request flow: approval automatically queues provider/language discovery. Source review is
enabled by default; automatic download is optional and requires a unique provider-published id.
The admin may accept any published language edition. Episode/byte progress lives in the persistent
job payload and is shown in both Requests and Queue. No database schema conversion was needed
for this extension. The initial cutover description below records the earlier manual-only flow.

## Storage contract

| State | New service path | Source |
|---|---|---|
| Household SQLite, artwork, thumbnails, subtitles | API `/data` | Existing household data bind |
| Existing library, including legacy downloads | API `/library` | Existing media bind |
| Catalogue archives, images and backups | Catalogue `/data` | Existing catalogue data bind |
| Catalogue Postgres | External database/network | Existing Postgres database; never a new empty DB |
| Chrome profile | Downloader `/app/.chrome-profile` | Existing external named volume |
| New resumable downloads | API and downloader `/incoming` | New explicit staging bind |

The containers can use different internal paths for one host filesystem. Compare resolved
host sources, not path strings inside containers. The private inventory records those sources.
The downloader no longer writes new episodes directly into the existing library. Old files stay
at their current paths; new jobs stage separately. Cross-filesystem copying is supported.

Library series and catalogue titles remain separate records. Existing integer catalogue ids and
household string ids are retained. Selected metadata is attached explicitly after a scoped import;
a normalized title or matching platform name is never proof of identity.

## Preparation

1. Read all component AGENTS instructions. Preserve original repositories and worktrees.
2. Run `python3 scripts/prepare-unification.py --host USER@HOST --output PRIVATE_DIR`.
   This only inspects existing containers and writes private local configuration. It recovers
   signing secrets, origin, role-independent session settings, catalogue credentials and crawler
   settings, media uid/gid, external database network, Chrome volume, and each bind source.
3. Run `python3 scripts/snapshot-unification.py --host USER@HOST --output BACKUP_DIR`.
4. Run `python3 scripts/verify-snapshots.py --host USER@HOST --snapshots BACKUP_DIR`.
   This verifies checksums and SQLite integrity/foreign keys, then actually restores Postgres
   into a temporary no-network container on tmpfs. It removes that container afterwards.
5. Keep the private inventory, snapshots and original image digests outside this public repo.
   Preserve raw responses, images, media, sidecars and partial downloads in place. Database-only
   backups do not contain these assets. Existing filesystem backup coverage remains necessary.

## Cutover requirements

- Keep the current HTTPS hostname and authentication secrets. WebAuthn credentials and device
  offline caches depend on that origin; a new domain is not a transparent rename.
- Stage source in a NEW directory. Never rsync `--delete` into an existing data directory.
  Install the generated `.env` and `runtime/` privately. No source deployment may overwrite them.
- Create only the new staging directory, owned by the existing media uid/gid. Confirm it is
  outside the scanned library and distinct from every existing data mount. Missing existing
  paths must abort deployment; do not mkdir them as a convenience.
- Build images before stopping any service. Record current image digests and original Compose
  directories so the running stack can be restored without rebuilding historical source.
- Check catalogue `sync_run` for active runs and household `jobs` for running work. Defer until
  idle. Confirm no manual standalone extraction/download is active before stopping its engine.
- Stop the original catalogue and downloader before starting replacements. Never let two
  schedulers share the catalogue or two browsers share one Chrome profile. Disable the old
  Portainer stack's restart/redeploy ownership before it can restart the retired service.
- Quiesce the household API, capture a final SQLite snapshot using the online backup API and
  a final Postgres dump, and record table counts after writers stop. Keep these separate from
  preliminary rehearsal snapshots. Do not leave production writers running against restored
  rehearsal databases.
- Reuse the existing `dramatarr` Compose project, with one API and one queue worker. Root Compose
  adds private catalogue/downloader services and reuses the existing Postgres network and Chrome
  volume. It deliberately does not manage the shared Postgres server.
- Validate exact mounts, resolved database engines/revisions and image provenance at startup.
  Compare all table counts and identifier sets with the quiescent baseline, not only parent rows.
  Check file paths and cached artwork, role visibility, passkey login, watch progress and seeking.
- Verify catalogue search and console authentication, then exercise an acquisition in an isolated
  fixture environment before authorizing a real external download. Do not auto-download a sample.

## Rollback

Retain old source directories, image digests and private configs. On failure, stop all new
writers before restarting original services. Reuse original mounts and secrets. If no schema or
user data changed, reuse the current databases. If data restoration is required, first snapshot
post-cutover state and reconcile new watch history/download jobs; restoring an old snapshot
blindly would lose activity. Keep staged downloads even if rolling back the interface.

## Current behavior and limits

Downloads are explicit admin actions from HTTPS Dramafren URLs; catalogue platform names cannot
be turned into Dramafren URLs automatically. A selected catalogue title supplies metadata, not
proof that a supplied download URL represents that title. The admin makes that selection.

Job rows survive restart. Extraction credentials stay inside the execution service; a retry
re-extracts current links and resumes surviving files. A job with failed episodes is failed,
not reported as complete. Publication uses a unique folder per job, rejects symlinks and refuses
replacement; it retains staging for recovery. It intentionally does not merge a new acquisition
with an existing series. There is no automatic staging cleanup in this release.

The catalogue console is proxied behind Dramatarr admin authentication. Original identifiers,
metadata provenance, source revocation and merge tombstones remain intact. Playback URLs and
cookies are not added to the metadata catalogue. Component folder names and schema keys stay
compatible; the public application identity is Dramatarr.

## First live cutover — 2026-11-06

The combined stack was brought up successfully with the existing household origin and port.
All four services passed health checks. The retired standalone catalogue and downloader were
kept stopped with restart disabled; their original images and private runtime specifications
were retained for rollback. Do not redeploy those retired installations alongside this stack.

The quiescent pre/post comparison preserved every household table row (excluding rebuildable
search-index internals), and every catalogue table count and primary-key fingerprint. This
included 7 users, 43 library series, 2,474 media records, 227 watch-progress records, 29,513
catalogue titles and 1,713,721 catalogue episode rows. No database schema conversion was needed.
The final SQLite snapshot passed integrity and foreign-key checks; the final Postgres dump
was restored successfully in a separate no-network container and then removed.

Runtime checks covered catalogue search with detail metadata, console links/assets, admin and
viewer/anonymous access boundaries, download-engine credentials, and an existing video's range
response compared byte-for-byte to its original file. The original library/data binds, Chrome
volume, signing secret and WebAuthn origin were verified unchanged. External acquisition was
not triggered as a smoke test; no sample series was downloaded.

Private manifests, consistent snapshots, restore evidence and rollback configuration remain
outside the repository. Their paths are deliberately not included in this public document.

## Fleet retirement — 2026-11-06

The retired standalone catalogue stack was exported privately and removed from Portainer.
The old catalogue and downloader containers and their unused default networks were removed.
No volume removal or Docker pruning was used. The shared Postgres server and its backup
service, catalogue data bind, library bind and original external Chrome-profile volume remain
in use by the combined deployment.

Legacy default Compose files in the old household/downloader deployment directories were
renamed with a retirement suffix, with a notice pointing to the combined source directory.
The obsolete catalogue/downloader proxy hosts were disabled; the household hostname and port
were kept. Both Portainer environments were checked for other drama containers. Relevant cron,
systemd and NAS init/shutdown tasks showed no remaining standalone startup definitions.

Rollback now uses the archived private specifications and retained images; the retired container
objects no longer exist. Stop replacement writers before reconstructing any historical service.
Do not delete data directories or named volumes based on the old product names: those names
are still referenced by the current Compose mounts.

## Request acquisition extension — 2026-11-06

Approved requests now queue provider-specific Dramafren searches with language preferences and
an optional administrator review. Published regional codes are retained (for example es-MX and
en-US); no ambiguous region is silently chosen. A unique provider-published identifier is required
for automatic acceptance. Review mode remains the default, and other-language editions can be
accepted explicitly. Searches and downloads remain in the same persistent serialized queue.

Live discovery was verified for two existing approved ReelShort requests in Spanish and English;
both were left awaiting administrator review. Episode counts deduplicate the repeated start link.
Successful import fulfills the linked request through a conditional transition and uses only the
existing terminal notification. Declining a request cancels its acquisition. Episode, byte and
current-episode progress survives refresh in the job payload; unknown total sizes remain unknown.

Verification passed 979 household backend tests, 251 frontend tests and 81 download engine tests,
plus lint/type checks and live admin/viewer/anonymous and invalid-language validation checks.
The update preserved existing database rows, mount identities, profile volume and playback origin;
consistent pre-update backups were restored in isolation. No media download was started on behalf
of the administrator while the discovered editions were awaiting review.
