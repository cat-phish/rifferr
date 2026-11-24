# F-PACKAGING: PyPI package with the frontend, and the Docker image
Status: Draft
Milestone: M5

## Context
Architecture §8 (install modes, Docker mode) and §10 (distribution). M5 is done when Rifferr
installs from PyPI and runs from the Docker image (`docs/roadmap.md`). Today the API serves
`frontend/dist` from the repository only (`api/app.py` `REPO_FRONTEND`), so an installed
wheel has no UI, and `docker/` doesn't exist. Default paths already follow XDG natively and
`RIFFERR_CONFIG_DIR` / data dir for `/config` (`profile_store.py`), and `/healthz` exists.

## Behavior
### PyPI (native)
- PACKAGING-1: The wheel MUST contain the built frontend at `rifferr/web/` (package data), and
  `rifferr serve` MUST serve it when no repository build exists. A repository `frontend/dist`
  still wins in a checkout, so `pnpm build` keeps working in development.
- PACKAGING-2: The release build MUST run `pnpm build` and fail when the frontend build or the
  bundled copy is missing; an sdist MUST also contain the built frontend, so a build from the
  sdist needs no Node.
- PACKAGING-3: `uv tool install rifferr` MUST give a working `rifferr` with beets pinned as
  today. The beets plugin extras (`chroma`, `lastgenre`, `discogs`, `plugins`) stay installable
  with `uv tool install 'rifferr[plugins]'`.
- PACKAGING-4: `rifferr doctor` MUST report the system tools a profile's config needs (ffmpeg
  for `replaygain` with the ffmpeg backend, `fpcalc` for `chroma`; cover resizing uses Pillow, so
  no image tool) and missing Python packages for enabled beets plugins (the
  existing B-067 check), and exit non-zero when one is missing. `rifferr serve` logs the same
  report at startup and keeps running.
- PACKAGING-5: The version MUST come from one place (the git tag at release, `0.0.0.dev`
  otherwise), and `rifferr --version` and `/healthz` report it.
- PACKAGING-6: Every dependency in the wheel, the frontend bundle and the image MUST have an
  AGPL-compatible license; the build writes a third-party license list into the wheel
  (`rifferr/web/licenses.txt` for the frontend, `rifferr/LICENSES.md` for Python) and the image.

### Docker
- PACKAGING-7: `docker/Dockerfile` MUST build a multi-stage image: Node stage builds the
  frontend, a uv stage builds the wheel, and the runtime stage (Debian slim, Python 3.12+)
  installs the wheel with `[plugins]` plus ffmpeg, chromaprint (`fpcalc`) and `tini`.
- PACKAGING-8: The entrypoint MUST run Rifferr as `PUID`/`PGID` (default 1000/1000), creating
  the user and group when needed and `chown`ing only `/config` (never `/music` or inboxes),
  then drop root with `setpriv`/`gosu` before starting. `UMASK` (default `022`) is applied.
  Running the container as a non-root user directly (`user:` in compose) MUST also work,
  with the entrypoint skipping the switch.
- PACKAGING-9: The image MUST set `RIFFERR_CONFIG_DIR=/config` and the data dir under
  `/config`, expose port 8765, declare volumes `/config` and `/music`, and start
  `rifferr serve --host 0.0.0.0 --no-browser`. Startup in the container MUST keep the
  F-SECURITY rule: no password, no listening beyond the container's localhost, with a log
  line saying how to set one (`docker exec -it rifferr rifferr passwd`).
- PACKAGING-10: The image MUST have a `HEALTHCHECK` on `/healthz` and stop gracefully on
  `SIGTERM` (pause running steps, resume them on the next start, architecture §8) within the
  compose default stop timeout or a documented `stop_grace_period`. If the server doesn't yet
  pause and resume runs on shutdown, that is a separate F-WORKER change this spec depends on.
- PACKAGING-11: `docker/compose.example.yaml` MUST show `/config`, `/music`, one inbox mount,
  `PUID`/`PGID`, `TZ`, the port, and a reverse-proxy note; profile paths in the example refer
  to container paths.
- PACKAGING-12: Extending the image for extra tools MUST be documented (`FROM` the image plus
  `apt-get install`). Extra Python packages (third-party beets plugins) come from
  `EXTRA_PIP_PACKAGES` (B-073, author's choice): the entrypoint installs them before dropping
  to `PUID`/`PGID`, the docs say to pin versions, and a failed install stops the container
  with the pip error.

### Release
- PACKAGING-13: A CI job on version tags MUST build the wheel and sdist and the image for
  linux/amd64 and linux/arm64, run a smoke test against each (install the wheel in a clean
  venv, `rifferr --version`, start `serve` on a temp profile, `GET /healthz` and `/`; start
  the image with a temp `/config`, check the health status and the files' owner), and publish
  only when the smoke tests pass.

## Interfaces
```
docker/
  Dockerfile
  entrypoint.sh
  compose.example.yaml
backend/src/rifferr/web/      # built frontend, generated at build time, not committed
backend/src/rifferr/cli/doctor.py
scripts/build_release.py      # pnpm build → copy to rifferr/web → uv build → license lists
```
Environment (image): `PUID`, `PGID`, `UMASK`, `TZ`, `EXTRA_PIP_PACKAGES`,
`RIFFERR_CONFIG_DIR=/config`.

## Examples
```yaml
# docker/compose.example.yaml
services:
  rifferr:
    image: ghcr.io/<owner>/rifferr:latest
    environment: { PUID: "1000", PGID: "1000", TZ: "Europe/Berlin" }
    volumes:
      - ./config:/config
      - /srv/music:/music
      - /srv/downloads/complete:/inbox
    ports: ["8765:8765"]
    restart: unless-stopped
```

## Extensibility
Users extend the image with extra tools and beets plugins (PACKAGING-12). Plugins installed
with `--with` natively or into the image's environment are found through entry points as
today.

## Security & safety
- The image never runs Rifferr as root (PACKAGING-8) and never `chown`s the music or inboxes.
- Non-localhost binding still needs a password (F-SECURITY); the image doesn't weaken that,
  and no default password exists.
- No secret is baked into the image; secrets come from env or Docker secrets.
- The release pipeline runs gitleaks over the build context and the license check (PACKAGING-6).

## Non-goals
- Desktop wrapper (pywebview/Tauri): later (architecture §10).
- Windows/macOS installers; Homebrew/AUR packages.
- An image variant with a GPU or local LLM.

## Open questions
1. Image registry and name: `ghcr.io/<owner>/rifferr`, Docker Hub, or both? Who owns the PyPI
   name `rifferr` (is it free and registered)?
2. Base image: Debian slim (ffmpeg from apt, bigger) or Alpine (smaller, musl wheel issues)?
   Proposed Debian slim.
3. In the container, should binding beyond localhost without a password refuse to start
   (as natively) or start bound to container-localhost only, which makes the port useless
   until a password is set? Proposed: start, serve only `/healthz` publicly, and log how to
   set a password.
4. ~~ImageMagick?~~ Not needed: covers resize with Pillow (`covers/resize.py`).
5. arm64 builds in CI: needed now (Raspberry Pi / ARM NAS), or amd64 only first?
6. Release trigger: git tags `v*` on `main` only?

## Tasks
- [ ] Frontend bundled in the wheel and sdist; serve fallback
- [ ] `rifferr doctor`; startup report
- [ ] Single-source version
- [ ] License lists
- [ ] Dockerfile, entrypoint (PUID/PGID/UMASK), compose example, docs
- [ ] Graceful stop and health check in the image
- [ ] Release CI with smoke tests
