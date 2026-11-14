# Reference workflow coverage

Maps each line of [original-checklist.md](original-checklist.md) to how
`examples/workflows/foobar-style.yaml` does it. Updated whenever the reference
workflow gains a capability (agent loop step 6).

| # | Checklist line | Workflow step (`id` / `uses:`) | Spec ID | Status |
|---|---|---|---|---|
| 1 | `beet import -q` the unsorted folder | `import` / `beets.import` (`mode: quiet`, path `{{ run.input }}`). `on.watch` on `vars.inbox` starts one run per new album folder after 5 minutes without changes; manual runs import the whole inbox (`input:`); albums that wait for review hold only their own lane, the others finish | S-BEETS-IMPORT, F-TRIGGERS, C-LANES | Done (M1); unattended (M2) |
| 2 | Interactive `beet import`: match albums or skip | — (ambiguous albums wait in review as import decisions) | F-IMPORT-UI | Planned (M2) |
| 3 | Open the importer in foobar2000 | — (not needed: Rifferr's queue and tag grid replace it) | — | Not applicable |
| 4 | Delete leftover files and empty folders in the unsorted folder | — | S-FILES-CLEANUP | Planned (M2) |
| 5 | Fix leftovers' tags by hand and move them to "beets sorted" | Manual edits in the tag grid; the move is not covered yet | F-TAG-GRID | Partly (M1) |
| 6 | First pass: album art and album type | — (art: M3 cover search) | S-COVERS-SEARCH | Planned (M3) |
| 6.1 | Run the conditional EP and Single tagging script | `release-types` / `tags.script` with `examples/scripts/release-types.yaml` (`apply: auto`): `albumtype` from the name, " (EP)" / " (Single)" suffix | S-TAGSCRIPT | Done (M1) |
| 6.1a | Flag albums that look like an EP by track count and running time (≤ 6 tracks, < 30 min) for review | — (needs album track count and length in conditions, B-044) | — | Not yet |
| 6.2 | Search for "EP (EP)" and "(EP) (EP)" | Not needed: the script's guards never add a second suffix, and `idempotence: fail` refuses a script that would | S-TAGSCRIPT | Done (M1) |
| 6.3 | Compilations get (Anthology) or (Compilation) if appropriate | `compilations` / `tags.script` with `examples/scripts/compilation.yaml` (`apply: review`); Anthology is chosen by rejecting and editing in the tag grid | S-TAGSCRIPT, F-REVIEW-QUEUE | Done (M1) |
| 7 | Artist clean-up: names, Various Artists → Splits/Collabs/Soundtracks, album artists split by `;` | — | — | Not yet (M5) |
| 8 | Dates: ORIGINALDATE, ORIGINALYEAR, DATE, RELEASEDATE | — (copy actions exist; the rules and manual cases aren't written yet) | S-TAGSCRIPT | Not yet (M5) |
| 9 | Album Artist → Album Artists, splits and collabs | — | — | Not yet (M5) |
| 10 | Splits and collabs carry the artists in the album name | — | — | Not yet (M5) |
| 11 | Replace "/", ":", "?" in album, title, artist, album artist | — (a `replace` action can do it) | S-TAGSCRIPT | Not yet (M5) |
| 12 | Singles, EPs, Anthologies, Compilations, 24-bit and Vinyl in names and tags | Singles, EPs, Compilations: rows 6.1, 6.3. Vinyl: B-045. 24-bit: B-046 | S-TAGSCRIPT | Partly (M1) |
| 13 | DATE ADDED tag as YYYY-MM-DD | — | — | Not yet (M5) |
| 14 | Genres: copy to Sub Genre, resolve into broad categories | — (AI genre step) | S-AI-TAG | Planned (M4) |
| 15 | Rename and move files with foobar2000 | — | — | Not yet (M5) |
| 16 | ReplayGain (album and track) | — | — | Not yet (M5) |
| 17 | "The" artist scripts | — | — | Not yet (M5) |
| 18 | Rename Collaboration, Splits, Compilations, Soundtracks folders | — | — | Not yet (M5) |
| 19 | Copy files into the library | — | — | Not yet (M5) |
| 20 | MusicBee: scan for new files, add to playlists | — (outside Rifferr; a `shell` step or integration could trigger a scan) | — | Not yet |
| 21 | iPod sync with ", The" playlists | — (outside Rifferr) | — | Not applicable |

Rows follow the checklist's top-level items; sub-items get their own row where a step
covers them. "Not yet" rows get a step and a spec when their milestone starts.
