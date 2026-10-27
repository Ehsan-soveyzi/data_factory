# Stage deliverables

Each stage has its own notebook and decision report. All stages use the same
installed `data_factory` package, configuration directory, SQL files, and tests
at the repository root. Run CLI commands from the repository root.

| Stage | Notebook | Decision report | Status |
|---|---|---|---|
| 01 - Data Factory | [Notebook](stage_01/solution.ipynb) | [Report](stage_01/report.docx) | Existing implementation; see its delivery status |
| 02 - Database Performance | [Notebook](stage_02/solution.ipynb) | [Report](stage_02/report.md) | Olist prepared and saved NYC reviewed; database work pending |

Open either notebook from its stage directory or from the repository root.
The setup cell locates the shared project root before resolving data paths.
Keep stage-specific analysis in these folders and reusable logic under `src/`.
