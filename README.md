# Rifferr

A local web app for building and running user-defined music-import workflows, with [beets](https://beets.io) as its primary library backend.

- Agent and contributor instructions: [AGENTS.md](AGENTS.md) (`CLAUDE.md` is a symlink to it)
- Architecture: [docs/architecture.md](docs/architecture.md)
- Specs: [specs/](specs/), index in [specs/000-overview.md](specs/000-overview.md)

## Try it

[docs/getting-started.md](docs/getting-started.md): a first import with your own beets config, on a test profile that works on copies.

## Development setup

```bash
pre-commit install          # gitleaks + spec lint on every commit
python3 scripts/spec.py list
```

License: AGPL-3.0-or-later.
