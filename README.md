# Dramatarr

Dramatarr is a self-hosted library for short, vertical dramas. It gives a household one place to discover series, request an edition, and watch episodes stored on a server or NAS.

The goal is to make the whole journey simple. Find a drama, choose the provider and language, bring the episodes into your library, and watch them online or save them to a device for offline viewing.

- Browse the catalogue, search for a title, or request something missing.
- Administrators can review the available editions and follow download and import progress.
- Household members can pick up where they left off and download episodes for offline playback.

Dramatarr combines the original Dramatarr library, Dramadex catalogue, and Dramadown download engine. Deploy the combined stack from this repository.

## Run your own instance

You need a Docker host with Docker Compose, a PostgreSQL database for the catalogue, and persistent space for your library, incoming downloads, and application data. The household database is stored in SQLite. Use a browser with passkey support and an HTTPS hostname for access from other devices.

Follow the [installation guide](docs/INSTALL.md) to set up storage, configure the stack, and create your first administrator. If you already run one of the earlier projects, see the [migration runbook](docs/UNIFICATION.md).

Once signed in, use **Admin → Requests** to approve and review a requested edition. **Admin → Queue** shows the download and import. You can also add a provider link from **Admin → Acquire**.
