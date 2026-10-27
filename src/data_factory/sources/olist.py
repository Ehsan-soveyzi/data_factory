"""CSV source adapter for the shared factory's Olist preparation stage."""

import json
from pathlib import Path

import pandas as pd

from ..exceptions import DataError
from ..quality import _digest


def load_olist_snapshot(project_root):
    """Preserve lexical source values and fingerprint every immutable CSV."""
    root = Path(project_root).resolve()
    contract_path = root / "config/olist_schema.json"
    contract_hash = _digest(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    settings = json.loads((root / "config/database.example.json").read_text(encoding="utf-8"))
    raw_dir = root / settings["paths"]["olist_raw_dir"]
    frames, inputs = {}, {}
    for name, definition in contract["tables"].items():
        path = raw_dir / definition["file"]
        before = _digest(path)
        frame = pd.read_csv(path, dtype="string", keep_default_na=False)
        expected = [column["source"] for column in definition["columns"]]
        if list(frame.columns) != expected:
            raise DataError(f"Olist source columns changed for {name}: {list(frame.columns)}")
        if _digest(path) != before:
            raise DataError(f"Olist source changed while reading: {path.name}")
        frames[name] = frame
        inputs[name] = {"file": path.relative_to(root).as_posix(), "sha256": before,
                        "records": len(frame)}
    if _digest(contract_path) != contract_hash:
        raise DataError("Olist contract changed while reading the snapshot.")
    return frames, inputs, contract, contract_hash, settings
