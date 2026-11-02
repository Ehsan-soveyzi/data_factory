# Stage 03 source data: Give Me Some Credit

The files required by `challenge_details/03_Challenge_Machine_Learning.pdf`
are available locally:

- `raw/cs-training.csv`: 150,000 labeled records, ten input features,
  the target `SeriousDlqin2yrs`, and an unnamed row identifier column.
- `raw/Data Dictionary.xls`: the accompanying variable definitions.
- `raw/source_manifest.json`: download URLs, timestamp, SHA-256 checksums,
  and verification results.

Original competition: <https://www.kaggle.com/c/GiveMeSomeCredit/data>.
Because Kaggle was inaccessible, the files were downloaded from the public
[JLZml GitHub mirror](https://github.com/JLZml/Credit-Scoring-Data-Sets/tree/master/3.%20Kaggle/Give%20Me%20Some%20Credit).
The CSV is byte-for-byte identical to a second public copy linked in the
manifest. This confirms agreement between the mirrors; a direct download
from Kaggle was not used for verification. The competition's data terms
remain applicable.

The downloaded bytes are preserved, including missing values and unusual
values. The row identifier is not an input feature. The target has 10,026
positive records and 139,974 negative records. Raw CSV/XLS files are excluded
from Git by the local `.gitignore`.

These files are the source inputs for the existing Data Factory package.
Stage 03 requires loading them through that package into the Stage 02
database, then reading from the database with SQLAlchemy in the notebook.
Downloading the files does not perform that ingestion.

## Local contract validation

From the repository root, run:

```bash
python -m data_factory validate-csv --config config/credit_validation.json
```

The independent credit contract produces accepted/quarantined records and a
quality report under `curated/validation/`, excluded from Git. Original raw
bytes and their download manifest remain unchanged. This command validates
local data; PostgreSQL loading is still pending. See the
[stage guide](../../stages/stage_03/README.md) for policies and output details.
