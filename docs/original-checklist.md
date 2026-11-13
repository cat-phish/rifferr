# Original checklist (reference workflow)

> The author's beets + foobar2000 import checklist, filled in 2026-11-13.
>
> This file is the source for `examples/workflows/foobar-style.yaml` and for
> `docs/coverage.md`. Agents must not invent its contents.

- [ ] Run beet import -q /music/New/Stage-1-Unsorted
- [ ] Run "beet import /music/New/Stage-1-Unsorted" and try to match albums or
      skip
- [ ] Open Importer foobar2000
- [ ] Check for leftover misc. files and delete empty folders in
      /music/New/Stage-1-Unsorted
  - [ ] On Windows with: Get-ChildItem -Directory -Recurse | Where-Object {
        @(Get-ChildItem -Force $_.FullName).Count -eq 0 } | Remove-Item -Force
  - [ ] Or on Linux with: rmdir *" and then "find . -empty -type d -delete
- [ ] Check /music/New/Stage-1-Unsorted for any leftovers and manually edit
      tags in foobar and move to /music/New/Stage-2-Beets-Sorted/
- [ ] Do first pass through entire album list checking for album art and album
      type (EP, Single, Compilation, Anthology, etc.) and ensuring all albums
      are tagged together
  - [ ] Run Conditional EP and Single tagging script
  - [ ] Search for "EP (EP)" and "(EP) (EP)"
  - [ ] Check all albums with "compilation" in album type and ensure they have
        (Anthology) or (Compilation) appended if appropriate
- [ ] Go through artist list:
  - [ ] Clean up improperly named artists
  - [ ] Change Various Artists to Splits, Collabs, Soundtracks, etc. as
        appropriate w/ tagging scripts
  - [ ] Ensure Album Artists field is split by ; for Splits and Collabs
  - [ ] Change any combined artists in the album artist field to Splits or
        Collabs as appropriate
- [ ] Manage Dates/Years
  - [ ] Check to make sure every album has an ORIGINALDATE
  - [ ] For albums without an ORIGINALDATE check if you can copy ORIGINALYEAR
        to ORIGINALDATE
  - [ ] For albums without either, copy Date to ORIGINALDATE
  - [ ] For albums still missing ORIGINALDATE enter manaully (user cover search
        if necessary)
  - [ ] Check for albums without an ORIGINALYEAR, copy ORIGINALDATE to
        ORIGINALYEAR and manually remove the month and day for the necessary
        ORIGINALYEAR tags
  - [ ] Check for albums without a DATE and resolve manually
  - [ ] Copy DATE to RELEASEDATE (use cover search to check google
  - [ ] Copy ORIGINALDATE to DATE
  - [ ] Check ORIGINALYEAR > ORIGINALDATE to make sure they match, otherwise
        set ORIGINALYEAR according to ORIGINALDATE
- [ ] Copy Album Artist to Album Artists and adjust Splits, Collaborations,
      Compilations, and Soundtracks. Splits and Collabs should have:
  - [ ] Copy Album Artist to Album Artists, except for Splits and Collabs
  - [ ] Check that there are no Unknown Album Artists
  - [ ] Ensure all multi-field Album Artists are split by ;
- [ ] Ensure all splits and collabs have the artist names in the album field
  - [ ] For ones that don't manually copy from artist field to beginning of
        album field, add " - ", and run Replace Semicolon to Slash Album script
- [ ] Check for "/", ":", "?" in album, title, artist, and album artist field
      and run replacement scripts.
- [ ] Check for Singles, EPs, Anthologies, Compilations, 24-bit, and Vinyl
      these types of releases and add those to album names and tags
  - [ ] Singles
  - [ ] EPs
  - [ ] Anthologies
- [ ] Add DATE ADDED tag in YYY-MM-DD format
- [ ] Handle Genres
  - [ ] Copy Genre to Sub Genre
  - [ ] Manually Resolve Genre fields into broad categories
- [ ] Run file operations to rename files with foobar2000
  - [ ] Move Split/Collabs first
  - [ ] Move the rest of them
- [ ] Run foobar2000 1.0 for ReplayGain
  - [ ] Album Gain
  - [ ] Track Gain
- [ ] Run "The" artist scripts
- [ ] Rename Collaboration, Splits, Compilations, and Soundtracks folders
- [ ] Copy files into library
- [ ] Scan for new files in MusicBee
- [ ] Add new music to playlists
  - [ ] Check new music against media org file and youtube playlists and add to
        playlists in MusicBee
- [ ] Sync toiPod
  - [ ] Run "The" artist script
  - [ ] Send sync playlist
  - [ ] Send other relevant playlists
  - [ ] Edit playlists to sub ", the" artist
    - [ ] Open .m3u8 files in Notepad++
    - [ ] Press Ctrl+H to open "Replace" dialog
    - [ ] Find what: #+BEGIN_SRC sh \\Music\\The ([^\\]+)\\ #+END_SRC
    - [ ] Replace with: #+BEGIN_SRC sh \\Music\\$1, The\\ #+END_SRC
    - [ ] Ensure "Match case" and "Regular expression" are selected
    - [ ] Ensure that only desired playlists are open in Notepad++
    - [ ] Click "Replace in All Open Documents"
    - [ ] Save All
- [ ] Run ", the" Artist script on iPod
