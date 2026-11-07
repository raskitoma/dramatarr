# Install Dramatarr

## Requirements

- A Docker host with Docker Engine and Docker Compose v2, plus internet access for image builds and your chosen catalogue and download sources.
- A PostgreSQL database accessible to the catalogue container. PostgreSQL 17 is used in the reference deployment.
- A browser with passkey support. Use HTTPS for household access and offline playback. HTTP on `localhost` is suitable for initial testing from the Docker host.
- Persistent storage for the mounts below. The download engine is configured with a 2 GiB shared-memory limit. Allow additional RAM for Chrome, FFmpeg, the API and PostgreSQL.

## Storage

Create the bind-mount directories on the Docker host before starting Compose. Use absolute paths in `.env`. For NAS media, mount the share on the Docker host first and ensure it is mounted before Compose starts.

| Setting | Container path | Contents |
| --- | --- | --- |
| `DRAMATARR_LIBRARY_PATH` | API `/library` | Published episodes and subtitles. The API needs write access to publish downloads and rename files. |
| `DRAMATARR_INCOMING_PATH` | API and downloader `/incoming` | Temporary downloads and resumable partial files. Keep this directory outside the library tree. |
| `DRAMATARR_DATA_PATH` | API `/data` | SQLite database, cached artwork, thumbnails, and extracted subtitles. |
| `DRAMATARR_CATALOGUE_DATA_PATH` | Catalogue `/data` | Catalogue images, response archives, and backup files. |
| `DRAMATARR_CHROME_VOLUME` | Downloader `/app/.chrome-profile` | Persistent browser profile in an external Docker volume. |
| PostgreSQL storage | Managed by PostgreSQL | Catalogue titles, providers, and index history. |

Size the media library for the episodes you intend to retain. While publishing, there can be a complete copy in staging and another on the library filesystem. Allow space for partial and retained staging files too. Catalogue storage grows with artwork and response archives. Check usage after the first sync before choosing quotas.

The API and downloader use `DRAMATARR_UID` and `DRAMATARR_GID` (both default to 1000). Give that identity access to the library, incoming and household data directories. The catalogue runs as UID 10001 and needs write access to its data directory. The downloader needs write access to its Chrome profile volume.

## Configure and start

From a checkout of this repository:

```sh
umask 077
cp .env.example .env
mkdir -p runtime
touch runtime/catalogue.env runtime/downloader.env
docker volume create dramatarr-chrome
```

1. Create a PostgreSQL database and a dedicated login that owns it. Connect the PostgreSQL container to a Docker network shared with the catalogue. Set `DRAMATARR_POSTGRES_NETWORK` to the existing network name and `DRAMADEX_DB_URL` to a SQLAlchemy URL such as `postgresql+psycopg://dramatarr:YOUR_PASSWORD@postgres:5432/dramatarr`. Replace the example password and hostname. Percent-encode reserved characters in the password.
2. Set the four host paths from the storage table and set `DRAMATARR_CHROME_VOLUME=dramatarr-chrome`. If the profile volume is pre-existing, check its ownership against the downloader UID and GID.
3. Generate three independent values for `DRAMATARR_SECRET_KEY`, `DRAMADEX_API_KEY` and `DRAMATARR_WORKER_KEY`. Run `openssl rand -hex 32` once for each value. Keep the environment files private and preserve the keys across upgrades.
4. Set `DRAMATARR_BOOTSTRAP_ADMIN_EMAIL` to your email. For HTTPS, set `DRAMATARR_PUBLIC_URL` and `DRAMATARR_WEBAUTHN_ORIGIN` to the browser origin, such as `https://drama.example.com`, and `DRAMATARR_WEBAUTHN_RP_ID` to its hostname. Forward HTTPS through a reverse proxy to the configured web port (8080 by default). For local testing, the example defaults to `http://localhost:8080`.
5. Start the root Compose stack and read the first admin enrollment link from the API logs. Open it at the configured origin and create a passkey. Treat the link as private.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs api
```

The web service is the entry point. The catalogue, API and downloader communicate over Docker networks. Keep the web port reachable only by the intended household network or reverse proxy.

The two runtime environment files can start empty. Use `runtime/catalogue.env` for catalogue-specific scheduling settings. The catalogue starts empty on a new database. Open the catalogue console under **Admin → Acquire** to start a provider sync, or let the catalogue scheduler populate it. Set up SMTP from **Admin → Settings** if you want mail delivery.

For existing media, organize each series under its own folder, for example `Series Name/Season 01/Series Name - S01E01.mp4`, and run a library scan from the admin interface.

## Back up and upgrade

Back up the household SQLite database with its online backup API or while the API is stopped. Back up PostgreSQL with `pg_dump`. Include media, application data, catalogue data, and private configuration. Retain the Chrome volume if you want to preserve browser state.

Pull the desired revision, review its changes, and run `docker compose up -d --build` again. Keep your mount paths, keys and HTTPS origin stable. Use the [migration runbook](UNIFICATION.md) for an existing standalone installation.
