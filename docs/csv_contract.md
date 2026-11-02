# Local CSV data contracts

`data_factory.csv_quality.validate_csv(config_path)` and the `validate-csv` CLI
execute the same offline workflow. It reuses `ColumnProfiler` and the existing
factory exception/exit-code contract. NYC ingestion, chronology, population join
and finalization keep their existing dataset-specific implementations.

## Run configuration

```json
{
  "source": {
    "path": "../data/stage_03/raw/cs-training.csv",
    "encoding": "utf-8-sig",
    "delimiter": ","
  },
  "contract": "credit_schema.json",
  "output_dir": "../data/stage_03/curated/validation"
}
```

All three paths resolve relative to this JSON file. `source.path`, `contract`
and `output_dir` are required. `encoding` defaults to `utf-8-sig`, `delimiter`
to comma. The optional `source.expected_sha256` pins the input bytes and must
contain 64 lowercase hex digits. The delivered credit config includes that pin.
Changing the source deliberately requires updating/removing the pin after review.
No API calls, Socrata checkpoint or database connection are needed.

## Version 1 contract

An unrelated inventory dataset can use this contract without changing Python:

```json
{
  "version": 1,
  "dataset": "Inventory",
  "primary_key": ["sku"],
  "extra_columns": "reject",
  "max_quarantine_fraction": 0.01,
  "fields": [
    {"source": "SKU", "name": "sku", "type": "string", "nullable": false, "role": "identifier"},
    {"source": "Units", "name": "units", "type": "integer", "nullable": false, "min": 0},
    {"source": "Price", "name": "price", "type": "number", "nullable": true, "min": 0}
  ]
}
```

Unknown keys, duplicate JSON keys, non-finite constants, inconsistent policies
and unsupported rule types fail before row processing. The top-level required
keys are `version`, `dataset`, `fields` and `primary_key`. Optional `description`
documents dataset policy. Threshold defaults to 1%; `extra_columns` defaults to
`reject` and can be set to `ignore` for explicit projection.

| Field setting | Meaning |
|---|---|
| `source` | Exact source header; `""` addresses the original unnamed credit ID |
| `name` | Unique nonempty output name; supports original credit column names |
| `type` | `string`, `integer`, `number`, `boolean`, `date`, or `datetime` |
| `nullable` | Required boolean; missing values are accepted only if true |
| `required_presence` | Header must exist; defaults to true; false requires nullable=true |
| `missing_tokens` | Case-insensitive trimmed tokens including `""`; defaults to `[""]` |
| `on_invalid` | `quarantine` by default; `set_null` requires nullable=true |
| `min`, `max` | Inclusive bounds for numeric/integer fields |
| `allowed` | Nonempty list of permitted typed values |
| `pattern` | Full-match regular expression for string fields |
| `warn_values` | Typed values retained with `UNUSUAL_VALUE` flags |
| `warn_min`, `warn_max` | Inclusive normal bounds; values outside are retained with flags |
| `role` | `feature` by default; also `identifier`, `target`, or `metadata` |
| `description` | Human-readable field policy |

Primary keys refer to output names. Use multiple names for a composite key or
`[]` to disable deduplication. Key fields must be non-null with quarantine policy.
The first valid row reserves its key. Subsequent byte-value-equivalent parsed
records become `DUPLICATE_RECORD`; different records sharing that normalized key
become `DUPLICATE_KEY_CONFLICT`. Comparison includes ignored source columns and
original text formatting. Identical feature vectors under different IDs are
retained; this is not an entity-resolution engine.

Strings preserve leading zeros and are trimmed with flags. Integer conversion
uses decimal arithmetic, so `2.0` becomes `2`, `1.5` fails, and large integers
are not rounded through binary floats. Numbers must be finite. Booleans accept
case-insensitive `true`/`false` and `1`/`0`. Dates use ISO date parsing;
timestamps require `T` and use ISO datetime parsing. Dates/timestamps serialize
to ISO text. Allowed/warning values use the normalized type and canonical ISO
text. Missing values never become zero. There are no expressions, arbitrary
Python callbacks in contracts, cross-field relations, joins or learned rules.

## Failures, warnings and publication

- Header drift (missing required, duplicate or forbidden extra headers), invalid
  decoding, malformed quoting, and empty/header-only files stop processing.
  No run is published; previous successful/failed results remain on disk.
- A row with the wrong field count goes to quarantine with original header/values.
  Invalid values record every hard error. Optional invalid values can be set null
  when explicitly declared, with a reason flag. Missing nullable values are warnings.
- Exactly one accepted or quarantined record is counted per CSV data record.
  Reason counts count records per reason and may overlap. Warnings and target
  distributions cover accepted records only. Profiling covers accepted contract
  fields before adding provenance metadata.
- A threshold failure publishes all outputs plus `status=failed`, updates
  `latest.json` to that failed run, then raises `QuarantineThresholdError` (CLI 4).
  Equality with the threshold passes. Consumers must check status.
- The raw CSV is hashed before/after validation, and is never rewritten.
  SQLite scratch space holds duplicate keys and exact distinct profile values.
  Outputs publish atomically into a directory identified by source, resolved
  contract, parser options, implementation hashes and engine version.
  Reruns compare every expected artifact and refuse to overwrite changed files.

| Output | Contents |
|---|---|
| `accepted.jsonl` | Typed fields, nulls, `quality_flags`, source filename and record number |
| `accepted.csv` | Accepted rows with only the contract fields in declared order; nulls are empty; no added metadata columns |
| `quarantine.jsonl` | Original parsed source values, all errors/codes, record number and ending physical line |
| `quality_profile.json` | Exact missing counts, distinct counts and typed ranges for accepted fields |
| `contract.json` | Resolved contract including defaults, for audit/reproduction |
| `validation_report.json` | Counts, policy, warnings, target distributions, feature roles and artifact SHA-256 hashes |
| `latest.json` | Pointer to the most recently published run and its passed/failed status |

`source_row` is the one-based data-record number excluding the header. Multiline
quoted cells are one record; `source_line_end` in quarantine includes the header
and physical continuation lines. Raw CSV quoting/bytes remain in the immutable
source, while quarantine stores the parsed values. Metadata names `quality_flags`,
`source_file` and `source_row` are reserved and cannot be contract output names.
`feature_columns` is the authoritative list for later modeling; identifiers,
target and metadata are not included. The entire supplied file is processed,
which does not establish completeness of an external population.

## Credit policy

`config/credit_schema.json` is an initial explicit data-quality policy, not a
claim that all observed extreme values are erroneous. Its ten feature names
are preserved. The unnamed row ID maps to `row_id` with identifier role.
`SeriousDlqin2yrs` must be a nonmissing integer in `{0, 1}`. All count fields
must be nonnegative integers; age must be a positive integer. Age zero is a
hard error. Ratios and nonmissing income must be finite and nonnegative.

Missing income and dependents stay null with warnings. Utilization above one
and delinquency counts 96/98 stay unchanged with review flags. No unverified
sentinel meaning is assigned to 96/98; no upper debt-ratio limit is invented.
Policy edits require only the contract for supported rules and create a new
run. Future imputation/scaling must fit on training data after splitting.

## Verification

```bash
python -m unittest discover -s tests -p test_csv_quality.py -v
python -m data_factory.testing
```

Tests cover unrelated input data, numeric precision, nulls, warnings, categories,
headers, malformed CSV, composite/disabled keys, pinning, source mutation,
threshold equality/failure, artifact tampering, deterministic reruns, path
resolution and CLI exit codes. Tests use local temporary fixtures. Loading
credit data into PostgreSQL and subsequent SQLAlchemy analysis are separate work.

The saved [credit verification](../stages/stage_03/evidence/validation_summary.json)
reconciles all 150,000 input records with 149,999 accepted records and one
age-zero quarantine record. It independently verifies source/output hashes,
source order, target counts and missing values. The saved
[offline regression evidence](../stages/stage_03/evidence/offline_tests.json)
records 217 passing tests and 71 skipped live-database tests, with no failures
or errors. These are recorded local results, not a PostgreSQL acceptance run.
