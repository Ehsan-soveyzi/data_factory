# NYC 311 Data Factory

## Resumable acquisition

Run `challenge_solution.ipynb` from this directory. Configuration lives in
`config/setting.json`; the notebook imports acquisition logic from
`src/data_factory/ingestion.py`.

The acquisition cell calls:

```python
download_summary = download_batches(config)
```

This single call downloads all remaining batches automatically. Interrupt the
cell when needed and rerun it to resume. It never stops at the minimum record
requirement and does not sample the selected scope.

Current output locations:

- `data/raw_data/batch_000001.json`, `batch_000002.json`, ...: raw responses.
- `data/raw_data/checkpoint.json`: cursor, scope fingerprint, counts, batch
  checksums and completion status. The filename is configured by
  `paths.checkpoint_file` and is always inside `paths.raw_dir`.

The raw file is atomically saved before the checkpoint. A batch saved immediately
before an interruption is recovered on the next run. Existing committed files
are checked for missing files or changed checksums before resuming. Invalid
responses do not advance the checkpoint. An OS-managed lock prevents two writers
from downloading into the same directory; kernel termination releases the lock.

`http.max_retries` is the number of retries after the first attempt. Network
failures, HTTP 429/5xx and malformed JSON are retried with bounded delays. A 2xx
response must contain valid JSON; other statuses fail immediately unless they
are retryable. A short nonempty page is retained and followed by another query.
Only an empty response marks the snapshot complete.

After completion, repeating the call uses the saved snapshot without HTTP calls.
This is acquisition resume, not synchronization of updates to old source records.
The live dataset can change during a download; a fixed date interval does not
provide an upstream immutable snapshot.

Change `raw_dir` to a separate directory when changing the endpoint, dates,
complaint types or boroughs. Batch size and retry settings can change on resume.
Keep raw batches and their checkpoint together. If raw batches exist without a
checkpoint, acquisition refuses to guess their provenance or overwrite them.
Raw data and state are excluded from Git.

For a bounded integration check, `download_batches(config, max_batches=1)` saves
at most one new batch in that invocation. This limit is optional; the notebook's
normal acquisition cell has no batch limit. `prepare_checkpoint(config)` only
creates/verifies local state, without HTTP requests.

The query uses Socrata's documented
[`$order`](https://dev.socrata.com/docs/queries/order.html) and
[`$where`](https://dev.socrata.com/docs/queries/where.html) parameters.

## Offline verification

Step one is an observational inspection of existing committed raw batches:

```powershell
data-factory run --stage inspect
```

It selects up to `validation.inspection_batches` approximately equally spaced
batches, including the first and last available batches when selecting more than
one. The report is written to `paths.created_dir / paths.inspection_file`
(`data/created_data/raw_inspection.json` in the current configuration). Notebook
cells beginning with `step-one-` explain this step and display the generated tables.

The report labels both the available snapshot size and the inspected sample
size. It distinguishes absent keys, explicit nulls and blank strings, computes
exact distinct counts using a temporary disk-backed SQLite database, and records
source JSON types and ranges. String ranges are lexicographic; no numeric or date
conversion is applied to source values. Temporary files are removed after use.

Configured sentinel text is counted separately, without treating it as missing
or applying cleaning rules. For example, `UNKNOWN` can be a legitimate submission
channel. Category counts have a configurable memory bound and expose an
`other_count` if that bound is exceeded. Date observations include missing,
unparseable and negative closure intervals, without rejecting any source rows.

This stage makes no HTTP requests and does not require the validation
schema. It does not clean, filter or quarantine source records. The saved report
is reproducible for the same committed snapshot and inspection settings.

Step two finalizes the version-1 contract in `config/schema.json`. Its 17 source
fields and four derived fields have explicit types, nullability, missing-value
policies and error policies, with documented reasons. Dataset-wide scope,
duplicate, closure and coordinate decisions are also documented. A Persian
companion is available at `docs/data_contract.md`; notebook cells beginning with
`step-two-` load and display the same contract without processing source rows.

`load_schema(path, source=...)` checks the contract configuration itself, including
contradictory policies, invalid bounds/domains, duplicate JSON keys and relation
types. `contract_rows(...)` provides table-ready metadata without depending on
pandas. Settings-backed complaint categories are resolved from the source
configuration. This is a custom project contract, not the JSON Schema standard.
Step three executes this contract using `validate_dataset(config)` or:

```powershell
data-factory run --stage validate
```

Run the CLI from the project directory. Notebook cells beginning with
`step-three-` resolve project paths and run independently of acquisition cells.
They execute validation, display reason counts, optional-field warnings and the
accepted-column profile, then preview at most five records per output and verify
row accounting, accepted-ID uniqueness and output SHA-256 hashes.

Validation reads all committed batches without HTTP calls. It retains the first
individually valid occurrence of each ID across batches; later identical full
raw records receive `DUPLICATE_RECORD`, while changed raw content with the same
ID receives `DUPLICATE_KEY_CONFLICT`. Each rejection preserves the original
record, source filename, one-based source row and all field/reason errors.
Optional repairs are visible in sorted `quality_flags`. Missing closure remains
null, zero duration is valid, and a negative closure interval is quarantined.
Keys and ZIPs remain strings, including any leading zeros.

The following files are published together under
`data/created_data/validation/<run-hash>/`:

- `accepted.jsonl`: the 17 normalized source fields and four derived fields.
- `quarantine.jsonl`: original rejected records and their diagnostic evidence.
- `quality_profile.json`: exact column statistics of accepted records only.
- `validation_report.json`: input scope/completeness, contract hash, counts,
  reasons, warnings and output checksums.

JSON has no native datetime: normalized timestamps are ISO strings with
microsecond precision; convert them to datetime when loading analytical data.
Neither raw records nor the entire accepted dataset are concatenated in RAM.
Raw parsing is bounded by a batch; exact distinct values and accepted-ID lookup
use a temporary SQLite database on disk with a bounded profile buffer/cache.

The configured quarantine limit is 1% of raw records. This conservative
operational gate requires investigation when critical violations exceed that
share; it is not a statistical guarantee. Optional-field warnings are reported
separately and do not inflate the quarantine rate. Equality with the limit passes.
Above the limit, diagnostics are published with `status: failed`, the notebook
raises `QuarantineThresholdError`, and the CLI exits with code 4. A structural
schema change stops the stage without publishing incomplete outputs.

`validation/latest.json` points to the last fully evaluated result, including its
pass/fail status. Downstream stages must require `passed`. A passing result on an
incomplete acquisition remains explicitly partial; 300,000 saved rows alone do
not establish coverage of the selected 18-month interval. The report is a stage
report, not the final factory manifest.

The run hash includes the frozen raw snapshot, contract and semantic validation
settings. Independent runs on identical inputs produce identical output bytes.
Repeating a run verifies existing files and refuses to overwrite differing
results; interrupted work is removed and can be rerun from the raw snapshot.
Acquisition can resume independently; a later validation run includes newly
committed batches. Temporary SQLite files are removed after success or failure.

Install the package into the same environment as the notebook (`python -m pip
install -e .`), then run:

```powershell
python -m unittest discover -s tests -v
```

Tests use temporary directories and simulated HTTP responses. They cover
automatic pagination, timestamp ties, interruption and orphan-batch recovery,
idempotent completion, changed scope, file corruption, retry exhaustion,
malformed data and concurrent-run protection. Validation tests additionally cover
cross-batch duplicates, first-valid retention, null/zero/negative closure,
optional repairs, scope boundaries, structural drift, threshold failure with
preserved evidence, deterministic independent runs, corruption, interruption
cleanup, accounting and CLI exit codes.

## Shared HTTP client

`src/data_factory/http_client.py` owns HTTP requests, timeout, retry/backoff and
JSON decoding. `ingestion.py` owns the NYC query, pagination, raw files and
checkpoint. Both the notebook and the CLI use the shared client through the
existing ingestion functions; their calls do not need to change.

The current Socrata source continues to use GET. Other source adapters can call:

```python
from data_factory.http_client import HttpClient

with HttpClient(config["http"]) as client:
    result = client.request_json(
        "POST",
        "https://example.test/query",  # Replace with the source endpoint.
        params={"version": "2"},
        json_body={"query": "read-only"},
        retry_safe=True,
    )
```

Use `params` for URL query parameters, `json_body` for a JSON body, or `data` for
a form body. JSON and form bodies are mutually exclusive. Optional `headers`
are forwarded to Requests. The client accepts only GET and POST and does not
read or write dataset files. It closes sessions it creates, while injected
sessions remain owned by their caller.

GET uses the configured retry budget. POST makes one attempt by default; opt in
with `retry_safe=True` for read-only queries or endpoints whose contract makes
repeating the operation safe. This applies to timeouts, 429/5xx and malformed
JSON alike. Successful JSON responses with 2xx statuses are accepted, including
201 for POST; a successful status with an invalid/empty JSON body is a data error
after the permitted attempts.

Retry delay starts at `http.retry_backoff_seconds` and doubles between retries,
bounded by `http.max_retry_delay_seconds`. A valid `Retry-After` number or HTTP
date takes precedence for that retry, subject to the same cap. Old settings
without the two delay fields retain defaults of 1 and 60 seconds respectively.

An API accepts GET or POST according to its endpoint contract: the server routes
methods separately and may require query parameters or a request body, so
changing the method alone does not make an endpoint accept the request. See
the [Requests interface documentation](https://requests.readthedocs.io/en/latest/api/)
for the underlying method, query and body arguments.

HTTP tests use simulated responses, including a real Requests session with a
mocked transport adapter to verify POST serialization without a network call.
The ingestion tests still cover resume, checkpoint recovery and error handling.

## Population reference and join (step four)

```powershell
data-factory run --stage population
```

This stage joins saved, passed validation output to the pinned NYC Health
population/geography reference. It preserves every case and the original borough,
and reports missing/unmatched ZIPs and independent borough differences. The
reference is historical (July 1, 2019), with population at MODZCTA level; grouped
ZIP aliases share one area population. Do not sum population over case rows.
Source links, actual retrieval time and transport are saved in reference
provenance. Verified cached references require no network access on later runs.

Output files are `enriched_cases.jsonl`, `join_issues.jsonl`,
`population_areas.jsonl` and `population_join_report.json`, under
`data/created_data/population_join/<run-hash>/`. Case data are streamed, reference
size is explicitly bounded, and row counts/checksums are checked before atomic
publication. Failed validation, ambiguous reference keys and changed input/output
files stop the stage. Borough differences remain quality findings, with no
automatic correction or removal of cases.

Notebook cells beginning `step-four-` execute independently using saved inputs
and display provenance, join counts, missing codes, borough findings and previews.
See [the population join contract](docs/population_join.md) for all added columns,
source methodology, rate eligibility, denominator handling and limitations.

Final aggregation/delivery outputs, the complete `--stage all` pipeline, final
manifest and EDA remain subsequent stages.
