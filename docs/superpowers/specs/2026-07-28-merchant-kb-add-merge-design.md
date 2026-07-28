# Merchant KB Add-Folder Merge Design

## Goal

Add an explicit `python pipeline.py --merge-add` command that merges CSV files
from `add/` into `merchant_kb.csv` without running the XML pipeline.

## Inputs and Output

- Read every `*.csv` file directly inside `add/`, in filename order.
- The target is `merchant_kb.csv`, whose columns are `merchant_name`,
  `keywords`, `link`, `category`, and `keyword_created_at`.
- Add files may provide `keyword_updated_at`; it maps to the target
  `keyword_created_at`. `category_updated_at` has no target column and is
  ignored.

## Matching and Merge Rules

- A merchant match uses its name after `casefold()`, trimming leading and
  trailing whitespace, and collapsing internal whitespace runs to one space.
- For every target row matching an add-file merchant, only blank target fields
  are populated. Existing nonblank values are never overwritten.
- Matching fields are `keywords`, `link`, `category`, and
  `keyword_created_at`.
- If a target contains duplicate matching merchant rows, every matching row is
  supplemented independently.
- An add-file merchant absent from the target is converted to the target's
  five-column format and inserted before all existing target rows. New rows
  retain input-file and row order.

## Safety and Interface

- `--merge-add` is separate from the regular five-stage pipeline, so normal
  pipeline runs cannot accidentally consume add-folder files.
- The merge streams the large target CSV into a temporary file in the target
  directory, then atomically replaces the original only after a successful
  write.
- The command reports source rows, updated target rows, newly inserted rows,
  and skipped unsupported input fields.

## Tests

Tests will cover normalized-name matching, blank-only supplementation,
duplicate target matches, top insertion order, timestamp mapping, and an
empty/missing add directory.
