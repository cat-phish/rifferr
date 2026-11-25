# Handoff: moving Rifferr development to the VM (2026-11-25)

Development moves from the desktop (`jordan@desktop`, repo at `~/code/rifferr`) to an
isolated VM that the user reaches with `ssh code`. This file is everything the next Claude
session needs. Read it in full, then read `CLAUDE.md` and `docs/architecture.md`.

**Your first job on the VM:** work out the VM's user name, home directory and where you
cloned the repo, then give the user the exact `scp` commands (section 6) to run **on the
desktop** to copy the test files over. Fill in real paths. Don't leave placeholders.

## 1. Repository state

- Remote: `git@github.com:cat-phish/rifferr.git`. Every local branch is pushed.
- Work branch: **`feat/ui-refresh`** (not yet merged to `main`). Check it out on the VM.
- Other pushed branches (`audit/tests`, `docs/musicbrainz-plugin`, `feat/plugin-extras`,
  `fix/skip-changed-folder`) are already merged or parked. Leave them alone.

Last commits on `feat/ui-refresh`:
- `162fb11` C-FOLDER-FACTS and F-PACKAGING: specs drafted, Status Draft.
- `ec41c1f` / `4b19d23` C-TEMPLATE-CONDITIONS: `if:` / `unless:` template conditions on
  actions, `%startswith`, built-in template functions on FakeBackend, the `kind-album-name`
  step ("A / B - Album"). Coverage row 10 and B-084 done.
- `f91c6fe` S-TAGS-CHOOSE: `tags.choose` (choice reviews), ranked rules files,
  `%genre_root`, `learn`, "This is one artist"; foobar-style `album-kind`, `dates-missing`,
  `subgenres`, `genre-bucket` steps.
- `5d44274` F-SECURITY: persistent sessions, password sign-in and sign-out.
- `c661492` C-FILE-TAGS, C-ALBUM-FACTS: flexible attributes written as file tags, album facts
  (`rf_*`), artist-name rules, foobar-style ftintitle, dates and replaygain steps, likely-EP
  review.

Last green run (at `ec41c1f`): backend 2109 passed / 58 skipped, ruff, pyright, all three
example workflows validate. Frontend 176 tests, 16/16 Playwright.

### VM setup

```bash
git clone git@github.com:cat-phish/rifferr.git && cd rifferr && git checkout feat/ui-refresh
pre-commit install
cd backend && uv sync --extra plugins     # NOT plain `uv sync`, see 3.2
cd ../frontend && pnpm install && pnpm build
```
System packages the tests and beets need: Python (version from `backend/pyproject.toml`),
`uv`, Node + `pnpm`, `ffmpeg` (fixtures, ReplayGain), `sqlite3` CLI (test reset),
`chromaprint`/`fpcalc` (beets `chroma` plugin), Playwright browsers (`pnpm exec playwright
install --with-deps chromium`). Then run every check in section 4 once to prove the VM is
green before changing anything.

## 2. Rules

### 2.1 Project rules

`CLAUDE.md` in the repo root is the project's rule file and is binding. It covers the
non-negotiables (nothing user-specific hardcoded, beets only in `rifferr.backends.beets`,
all modifications as ChangeSets, AI proposes and never applies, security rules always on,
unattended first, built-ins through public plugin APIs), the AGPL dependency rule, layout,
commands, testing rules and the spec-driven loop. Don't duplicate it; read it.

### 2.2 The user's global rules (from `~/.claude/CLAUDE.md` on the desktop)

Copy these into `~/.claude/CLAUDE.md` on the VM (section 6 copies the file itself):

> **Git commits**
> - Never author or co-author commits as Claude unless I explicitly say so for that commit.
>   Commits use my configured git identity only: no `Co-Authored-By: Claude …` trailer, no
>   "Generated with Claude Code" lines, no `--author` override. The same applies to PR
>   descriptions.
>
> **Token use**
> - Keep chats short: most usage is re-reading the conversation. Terse replies, no recaps,
>   no re-reading files already in context; read only the lines needed.
> - Background jobs: wait for their completion notice. Never poll them or wake them in a
>   loop.
> - Agents: never wake an idle agent just to ask for status. Message it only with a real
>   next step.
> - At most 2 helper agents at a time. Use a cheaper model (haiku) for jobs that only read or
>   search files.
> - Suggest `/compact` only when the context is actually large (long session, many file
>   reads or big tool outputs since the last compact), at a natural break. Never right after
>   a compact or after a small task. Don't repeat the suggestion if I ignored it.

Git identity on the VM must be the user's (`cat-phish`, `jvaand@gmail.com`); check
`git config user.name` / `user.email` before the first commit.

The user also runs the `caveman` Claude Code plugin (`juliusbrussee/caveman`, terse replies
in chat; docs, commits and specs stay normal prose). Ask whether they want it installed on
the VM.

### 2.3 Memories (desktop auto-memory, recreate on the VM)

These are not in the repo. Save them as memories on the VM.

1. **User import workflow.** The author's flow is inbox → staging → library. The inbox must
   only ever hold untouched files; anything Rifferr touched (tags written, renamed) belongs
   in staging. Imported albums move out of the inbox to staging right away, and the inbox is
   cleaned of leftovers. Albums are reviewed in staging (tags, cover art, visually), then
   moved into the library only after a manual OK. They value undo and per-album review
   decisions. Undo must never delete the only copy. `examples/workflows/foobar-style.yaml`
   models this (import with `write: no`, cleanup, `library.move` to staging with
   `apply: auto`, `beets.command write`, …, `library.move` to the library with review). Keep
   it an example, never hardcoded behavior. Future per-inbox choices are in BACKLOG B-075.
2. **Dev server needs the `plugins` extra.** The test profile's beets config loads
   `discogs`, `chroma` and `lastgenre`. `uv sync` without `--extra plugins` uninstalls them;
   beets then only logs "couldn't load the plugin" and matching falls back to MusicBrainz
   alone (Lady Gaga – MAYHEM dropped from ~0.78 via Discogs to ~0.43 with wrong MusicBrainz
   albums). Always `uv sync --extra plugins` and start the dev server with
   `uv run --extra plugins rifferr serve --dev --vite-port 5174 --no-browser`. If matching
   gets worse, look for "couldn't load the plugin" in the import step's log first.

## 3. The test setup (manual end-to-end testing)

Automated tests never touch real music (see CLAUDE.md). Manual testing uses a separate
Rifferr profile named **`test`**, its own beets directory, and a small fixed sample of real
albums.

### 3.1 Files on the desktop

| What | Desktop path | Size |
|---|---|---|
| Rifferr profile `test` (profile.yaml, workflows, scripts) | `~/.config/rifferr/profiles/test/` | 32K |
| Test beets dir (config.yaml, library.db, state.pickle) | `~/rifferr-test/beets/` | small |
| Discogs token (beets config points at it) | `~/.config/beets/discogs_token.json` | tiny, **secret** |
| **Pristine sample** (10 albums, never modified) | `~/games_ssd/music/new/riffer-test-sample/` (note: `riffer`, one r) | 2.6G |
| Inbox (`vars.inbox`, a copy of the sample) | `~/games_ssd/music/new/rifferr-test-input/` | 2.6G |
| Staging (`vars.staging`, = beets `directory`) | `~/games_ssd/music/new/rifferr-test-output/` | empty |
| Library (`vars.library`) | `~/games_ssd/music/new/rifferr-test-library/` | empty |
| Rifferr app data (rifferr.db, art cache, server.json) | `~/.local/share/rifferr/` | 55M, not needed |

Only the sample needs copying; the inbox is recreated from it. The app DB, old backups
(`~/rifferr-test/backup-*`), `rifferr-test-old-*` and `rifferr-test-parked` (29G) stay
behind.

The 10 sample albums (known roles: GBH has no cover in the folder, so it tests
fetchart / cover search at the into-library check; Lady Gaga – MAYHEM needs Discogs to
match):
- `[2003] Goin' Home For Christmas`
- `[EGML 01] Bananaman & Blitz - Funk You - Tell Me (1999)`
- `Florence + The Machine - High As Hope (2018) [FLAC]`
- `GBH - 2006 - Punk Junkies`
- `Jesse Stewart - (2015) - Jesse Stewart (Live) [FLAC - WEB (Deezer)]`
- `Lady Gaga - MAYHEM (2025) [FLAC] {Store Exclusive}`
- `Merle Haggard - Ramblin' Fever [2002] [FLAC]`
- `TRESPASSER - יְהִי אוֹר [WEB FLAC] (2025)`
- `Various Artists - Rough Tapes NRC 8 (1982) (Cassette) (FLAC) (16bit)`
- `Violent Way - Release the Skins`

### 3.2 Paths to rewrite on the VM

These files hold absolute desktop paths. After copying, rewrite them for the VM:
- `profiles/test/profile.yaml`: `beets.beetsdir` and `vars.inbox` / `staging` / `library`
  (absolute paths only; `~` is not expanded in vars).
- `rifferr-test/beets/config.yaml`: `directory` (must equal `vars.staging`), `library`,
  `import.log`, `discogs.tokenfile`.
- `library.db` stores absolute item paths. Rather than fixing them, start the VM with a
  fresh reset (3.4), which empties it.

Proposed VM layout (adjust to the VM's home): `~/rifferr-test/beets/`,
`~/rifferr-test/sample/`, `~/rifferr-test/inbox/`, `~/rifferr-test/staging/`,
`~/rifferr-test/library/`.

### 3.3 The user's current foobar-style workflow

Two copies exist and they differ:
- **`examples/workflows/foobar-style.yaml`** (repo): the current, complete version. It has
  the featured-artists, album-kind labels (`label_split`, `label_collab`,
  `label_soundtrack`, `label_compilation`), dates, genre buckets and ReplayGain steps.
- **`profiles/test/workflows/foobar-style.yaml`** (desktop, copied by scp): the user's test
  copy. It is older (missing those steps), but has test-only settings that must survive:
  - `on.watch.settle: 1m` (the example has `5m`),
  - `on.watch.dedupe: in_progress` (the sample is dropped into the inbox again and again,
    TRIGGERS-26),
  - a first step `id: prune`, `uses: library.prune` (albums deleted by hand between test
    runs stop counting as duplicates; files are never touched).

  Before the next manual test, ask the user whether to bring the test copy up to date with
  the example while keeping those three test-only settings. Don't overwrite it without
  asking. Also copy over `release-suffixes.yaml` and `scripts/` (compilation,
  release-types, path-characters) unchanged. After any edit the workflow's content hash
  changes, so it must be trusted again: `uv run rifferr trust ... --profile test`.

### 3.4 One test round

1. **Fresh reset.** Stop the server, back up and empty both databases, clear staging and
   library, refill the inbox from the sample. The desktop used this (adapt the paths):
   ```bash
   set -e
   ts=$(date +%Y%m%d-%H%M%S); b=$HOME/rifferr-test/backup-$ts; mkdir -p $b
   app=$HOME/.local/share/rifferr/rifferr.db; lib=$HOME/rifferr-test/beets/library.db
   pid=$(python3 -c "import json;print(json.load(open('$HOME/.local/share/rifferr/server.json'))['pid'])" 2>/dev/null || true)
   [ -n "$pid" ] && kill -INT $pid && for i in $(seq 1 15); do kill -0 $pid 2>/dev/null || break; sleep 1; done
   [ -f $app ] && sqlite3 $app ".backup $b/rifferr.db"; sqlite3 $lib ".backup $b/library.db"
   [ -f $HOME/rifferr-test/beets/state.pickle ] && mv $HOME/rifferr-test/beets/state.pickle $b/ || true
   if [ -f $app ]; then for t in $(sqlite3 $app "select name from sqlite_master where type='table' and name not in ('schema_version','trusted_files','sqlite_sequence')"); do sqlite3 $app "delete from $t"; done; fi
   sqlite3 $lib "delete from items; delete from albums; delete from item_attributes; delete from album_attributes;"
   ```
   `trusted_files` is kept so workflows stay trusted. Then empty staging and library and the
   inbox (look before deleting; confirm with the user), and start the server.
2. **Start the dev server** (from `backend/`):
   `uv run --extra plugins rifferr serve --dev --vite-port 5174 --no-browser`. On the VM the
   user reaches it from the desktop through an SSH tunnel, e.g.
   `ssh -L 5174:localhost:5174 -L <api-port>:localhost:<api-port> code`; keep binding to
   localhost (non-localhost binding requires real auth, CLAUDE.md rule 5).
3. **Drop the albums:** `cp -r <sample>/. <inbox>/`. The watch trigger starts one run per
   album folder once it has been quiet for `settle`.
4. **Work through the Review queue** in the UI: import decisions, tag reviews, choice
   reviews, cover picker, and the final into-library check (cover with dimensions, tag
   tables, changed fields marked). Confirm albums land in staging first and the inbox is
   left clean, then in the library after the manual OK.
5. Report bugs found here as BACKLOG entries unless they belong to the current spec.

## 4. Checks (all must pass before a commit)

```bash
# backend/
uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest
uv run rifferr validate ../examples/workflows/*.yaml
uv run python -m rifferr.schemas.export          # when public models change
# frontend/
pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm e2e
# root
pre-commit run --all-files && python3 scripts/spec.py lint
```

## 5. How to continue

The goal is **M5** (`docs/roadmap.md`): `examples/workflows/foobar-style.yaml` reproduces
`docs/original-checklist.md`, every checklist line is mapped in `docs/coverage.md`, and
Rifferr installs from PyPI and runs from Docker. The desktop plan file
(`~/.claude/plans/1-it-should-run-zany-crescent.md`) is copied in section 6 for reference.

The loop for each piece of work (CLAUDE.md, "Agent loop"):
1. Claude drafts a spec and appends open questions; the user answers them.
2. Only the user moves a spec to Approved.
3. Claude writes tests first (test names contain the behavior ID), implements the minimum,
   and runs every check in section 4.
4. Claude ticks Tasks, sets Status to Implemented, updates `docs/coverage.md` and
   `BACKLOG.md`, and commits with the spec ID as prefix (user's identity, no Claude
   attribution).

Done: checklist rows 6–10, 14, 16, 18 and the featured-artists row. Left:
- **Row 12** (vinyl and anthologies): needs C-FOLDER-FACTS.
- **Packaging**: needs F-PACKAGING.
- **Row 20** (MusicBee) is outside Rifferr and stays "Not yet".

Next, once the user answers the questions below:
1. Fold the answers into both specs; the user moves them to Approved.
2. Implement C-FOLDER-FACTS. Then update `foobar-style` (a `vinyl` choice; the compilation
   step becomes a Compilation/Anthology choice), mark coverage row 12 done, close B-045.
3. Implement F-PACKAGING. Before PACKAGING-10, check whether `serve` already pauses runs on
   shutdown and resumes them on restart; if not, that is an F-WORKER change first.

### Open questions for the user

**C-FOLDER-FACTS** (`specs/core/C-FOLDER-FACTS.md`), checklist line 12:
1. Vinyl: what should choosing it change? Options: an " (Vinyl)" album-name suffix (added
   once, like " (EP)"), `media: Vinyl`, a marker in the folder name, `albumkind`. Exact
   formats?
2. Not vinyl (beets said Vinyl, the files say otherwise): set `media` to `Digital Media`,
   clear it, or leave it?
3. Rip evidence: which files or words in downloads show a vinyl rip (`*.log` mentioning
   "vinyl"/"turntable", `*.txt` rip notes, folder name)? The spec's patterns are a guess.
4. Anthology: same suffix style as Compilation (" (Anthology)")? Also set `albumtype` or
   `albumkind`?
5. 24-bit: stay in folder names only (B-046)? If yes, row 12 counts 24-bit as done.

**F-PACKAGING** (`specs/features/F-PACKAGING.md`):
1. Registry: `ghcr.io/<owner>/rifferr`, Docker Hub, or both? Which owner? Is the PyPI name
   `rifferr` free, and who registers it?
2. Base image: Debian slim (ffmpeg from apt, larger) or Alpine (smaller, musl wheel
   trouble)?
3. First start without a password: in the container, start anyway serving only `/healthz`
   and log how to set a password (`docker exec -it rifferr rifferr passwd`), or refuse?
4. arm64 images now, or amd64 first?
5. Releases triggered by `v*` tags on `main` only?

**Older, no rush:**
- S-TAGS-CHOOSE open question 3: should choice reviews time out, and what then?
- "This is one artist" writes the artist-names file, which lives in the beets directory,
  which the docs say Rifferr never writes to. OK, or move it to the profile's `rules/`?

### Gotchas
- pre-commit stashes unstaged changes but not untracked files; eslint can then fail on new
  untracked files that depend on an unstaged, regenerated `schema.d.ts`. Commit them
  together.
- App-DB migrations must be numbered without gaps (`appdb.migrations()`).
- Entry points (step types, template functions in `pyproject.toml`) take effect only after
  `uv sync --extra plugins`.
- Queries are split at spaces on both backends. Quote the term, or write `.` for the space,
  in rules files.
- In zsh, never name a shell variable `path`: it is tied to `$PATH`.
- Playwright serves the built frontend, so run `pnpm build` (or `pnpm e2e`) first.

## 6. scp commands (Claude on the VM: produce these)

Give the user commands to run **on the desktop**, using the `code` SSH host alias, with the
VM paths you chose filled in. They must cover:
1. `~/.config/rifferr/profiles/test/` → the VM's Rifferr config dir (`~/.config/rifferr/profiles/test/`).
2. `~/rifferr-test/beets/config.yaml` and `library.db` → VM test beets dir (no
   `backup-*` dirs, no `*.bak`).
3. `~/.config/beets/discogs_token.json` → VM (it's a secret: `chmod 600` after, never log
   its contents, never commit it).
4. `~/games_ssd/music/new/riffer-test-sample/` (2.6G) → VM sample dir. Use `scp -r` or,
   better for 2.6G and names with spaces, quotes, brackets and Hebrew,
   `rsync -a --info=progress2 -e ssh "<src>/" code:<dest>/`. Quote paths.
5. `~/.claude/CLAUDE.md` → VM `~/.claude/CLAUDE.md`.
6. `~/.claude/plans/1-it-should-run-zany-crescent.md` → VM `~/.claude/plans/`.

Create the destination directories first (`ssh code 'mkdir -p ...'`). After the copy,
rewrite the paths (3.2), do a fresh reset (3.4), fill the VM inbox from the sample, trust
the test workflows, and start a test round.
