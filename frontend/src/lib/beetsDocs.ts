/** Links for beets concepts named in the UI (UI-SHELL-11). Name them plainly, link here. */
export const BEETS_DOCS = {
  library: "https://beets.readthedocs.io/en/stable/guides/main.html",
  import: "https://beets.readthedocs.io/en/stable/reference/cli.html#import",
  importer: "https://beets.readthedocs.io/en/stable/guides/tagger.html",
  search_ids: "https://beets.readthedocs.io/en/stable/reference/config.html#search-ids",
  query: "https://beets.readthedocs.io/en/stable/reference/query.html",
  template: "https://beets.readthedocs.io/en/stable/reference/pathformat.html",
} as const;

export type BeetsConcept = keyof typeof BEETS_DOCS;
