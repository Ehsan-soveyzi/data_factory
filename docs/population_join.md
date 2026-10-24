# Population reference and join contract

The reference is the NYC Department of Health and Mental Hygiene's public
[`totals/data-by-modzcta.csv`](https://github.com/nychealth/coronavirus-data/blob/6f61a888d76c596acbbd71c7092e7485f517ad74/totals/data-by-modzcta.csv),
pinned to commit `6f61a888d76c596acbbd71c7092e7485f517ad74` and an exact SHA-256
in `config/setting.json`. Only its population and geography fields are used.
The unrelated disease counts do not enter case enrichment.

The source's [population methodology](https://github.com/nychealth/coronavirus-data/blob/6f61a888d76c596acbbd71c7092e7485f517ad74/README.md#rates-per-100000-people)
describes a July 1, 2019 population baseline. It predates the selected 2024-2025
cases. This is an explicit historical denominator, not a contemporary estimate.
Do not substitute the separately published MODZCTA polygon `pop_est`: its ACS
population series differs from this file's `POP_DENOMINATOR` series.

Each source row is a modified ZIP Code Tabulation Area (MODZCTA). Its `label`
lists the ZIP aliases belonging to that area. The join uses those published
aliases, including grouped codes such as 11211 and 11249. It does not assume
that every postal ZIP equals a ZCTA or derive borough from the complaint data.

The source file is fetched via the shared JSON HTTP client using GitHub's
Contents API. A retrieved copy can also be imported using
`prepare_population_reference(config, csv_text=..., acquired_via=...)`; the same
exact pinned checksum is required. The first import in this workspace used the
connected GitHub tool because direct HTTP acquisition was unavailable.
`data/reference_data/<source-fingerprint>/provenance.json` records the actual
retrieval timestamp and transport, source/documentation links, commit, checksum,
baseline and reference size. A verified cache is reused without HTTP calls.

Reference validation requires unique five-digit area IDs, finite nonnegative
population, recognized boroughs, well-formed CSV and bounded row/byte counts.
Each alias belongs to exactly one area. Duplicate areas/aliases, conflicts,
missing required columns and changed content stop execution before publication.
There is no silent first-row retention for an ambiguous reference.

The input must be the latest passed validation result; accepted, quarantine and
profile checksums are verified before proceeding. Cases are read one JSONL line
at a time. Source columns and their order of records are preserved. Every input
case produces exactly one output case, even when no reference can be attached.
The stage verifies the accepted checksum again after processing and checks the
reported input count before publishing. It never writes to acquisition state or
the validation output.

Nine added case fields:

| Field | Logical type | Meaning |
|---|---|---|
| `population_area` | nullable string | MODZCTA ID, preserving five-digit text |
| `population_geography` | nullable string | MODZCTA for matched cases |
| `reference_population` | nullable float | Entire area's population, never an allocated ZIP population |
| `population_reference_date` | nullable date string | Population baseline, not download date |
| `reference_borough` | nullable string | Independent reference borough, source borough retained |
| `reference_neighborhood` | nullable string | Reference neighborhood label |
| `population_join_status` | string enum | matched, missing_zip, unmatched_zip |
| `borough_comparison` | string enum | match, mismatch, unavailable |
| `population_rate_eligible` | boolean | Matched positive population with no known borough mismatch |

Missing source borough remains missing; the independent borough remains in its
own column. An unavailable comparison with an otherwise usable reference is
eligible, but a known mismatch is not. Missing and unmatched ZIPs have null
reference columns and are ineligible. Zero population is retained but ineligible.

A borough mismatch is a quality finding, not proof that the original borough is
incorrect. ZIP/MODZCTA boundaries can cross administrative boundaries (including
Marble Hill and Rikers Island). Do not overwrite borough or quarantine a case
based only on this coarse comparison. Address-level spatial reconciliation is
outside this stage. Rate eligibility does not remove the limitations of the
historical baseline or the approximate geographic mapping.

Published together under `data/created_data/population_join/<run-hash>/`:

- `enriched_cases.jsonl`: every accepted case plus the nine added fields.
- `join_issues.jsonl`: identifiers, accepted-row positions and join findings.
  These cases remain in the enriched output; this is not quarantine.
- `population_areas.jsonl`: one row per reference area, including its ZIP aliases
  and matched-case count. Use each area's population once when forming compatible
  rate denominators, never sum populations over cases or aliases.
- `population_join_report.json`: before/after counts, coverage, borough comparison,
  unmatched codes, source identity, limitations and output hashes.

The result inherits acquisition completeness from validation. Passing this stage
on a partial snapshot does not complete the 18-month delivery. `latest.json`
points only to fully published outputs. Identical inputs produce identical file
bytes; conflicting existing outputs are preserved and rejected. No clock time is
inserted into deterministic analytical files; retrieval time stays in provenance.

CLI: `data-factory run --stage population` from the project directory. Notebook
cells with IDs starting `step-four-` execute from a fresh kernel using saved
validation outputs. Subsequent work includes final aggregation, rate denominator
coverage, the full pipeline, its final manifest, and EDA.
