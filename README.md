# NYC 311 Data Factory

Use Python 3.14 and run commands from this project directory:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install --no-build-isolation -e .
python -m data_factory.testing
python -m data_factory run --stage all
```

Open `solution.ipynb` in Jupyter or VS Code and run **Restart Kernel & Run All**.
Saved snapshots replay offline by default; an empty project downloads first.
Resume acquisition with `python -m data_factory run --stage ingest`.
Acquire a separate interval after the last complete one with
`python -m data_factory run --stage all --since-last-run`.

Settings: `config/setting.json`. Decisions: `report.docx`. Field reference:
`data_undrestandation_notes.docx`. Contracts: [data](docs/data_contract.md),
[population](docs/population_join.md), [delivery](docs/final_delivery.md),
[tests and submission](docs/submission_checklist.md).

Raw batches and checkpoint: `data/raw_data/`; verified population cache:
`data/reference_data/`. Current outputs and execution manifest are located by
`data/created_data/delivery/latest.json`. These large datasets are ignored by Git.
Ordinary discovery also works: `python -m unittest discover -s tests -v`.

Current saved snapshot: **330,000 cases, incomplete 18-month scope**.
Live acquisition returned **HTTP 403** during preparation; restore source access,
resume acquisition, then rerun the notebook. See the checklist for remaining
external delivery requirements.
