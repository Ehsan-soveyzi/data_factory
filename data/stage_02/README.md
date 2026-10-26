# Stage 02 local data

- `raw/`: the nine original Olist CSV files, kept unchanged.
- `curated/`: generated outputs from validation and transformation.

Directory-local ignore rules exclude all data files while retaining the folder
structure in Git. Stage 01 data remains in its existing directories. Database
loading will consume verified stage 01 delivery artifacts through the shared
factory; do not copy them into stage 02 as a separate source of truth.
