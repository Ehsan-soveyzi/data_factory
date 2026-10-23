import json
from pathlib import Path


def load_config(config_path):
    """Load project settings from a JSON file."""
    config_path = Path(config_path)

    with config_path.open("r", encoding="utf-8") as file:
        return json.load(file)