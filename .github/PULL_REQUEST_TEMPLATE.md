## What this changes and why

<!-- One or two sentences. If this fixes a wrong-click or a missed node, link the issue. -->

## Does this touch selector generation or labeling?

<!-- If this PR changes `_selector`/`_path`/`_disambiguate` in parser.py, or
     label_linker.py, it needs a benchmark run, not just unit tests — see
     CONTRIBUTING.md's "Changes that need extra evidence" section. Paste the
     before/after resolved-to-exactly-one-element number here. Not applicable
     for docs/CI-only changes — delete this section if so. -->

## Checklist

- [ ] `uv run pytest` passes (131 tests)
- [ ] New behavior has a test, not just a manual check
- [ ] If this touches `js/src/`, the equivalent fix is made in the TypeScript port too (`parser.py`/`label_linker.py` changes aren't done until `parser.ts`/`labelLinker.ts` match)
