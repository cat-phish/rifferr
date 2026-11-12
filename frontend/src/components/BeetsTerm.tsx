import { Anchor } from "@mantine/core";
import { BEETS_DOCS, type BeetsConcept } from "../lib/beetsDocs";

/** A beets concept, named as beets names it, linking to the beets docs (UI-SHELL-11). */
export function BeetsTerm({ concept, children }: { concept: BeetsConcept; children?: string }) {
  return (
    <Anchor href={BEETS_DOCS[concept]} target="_blank" rel="noreferrer">
      {children ?? concept}
    </Anchor>
  );
}
