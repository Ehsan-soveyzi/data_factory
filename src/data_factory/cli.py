"""Command-line interface for the data factory."""

import argparse
import json
import sys

from .config import load_config
from .exceptions import (
    CheckpointError,
    ConfigurationError,
    DataError,
    DownloadError,
)
from .ingestion import download_batches
from .quality import inspect_raw_data, validate_dataset
from .population import join_population


def main():
    parser = argparse.ArgumentParser(prog="data-factory")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run")
    run_parser.add_argument(
        "--stage",
        choices=["ingest", "inspect", "validate", "population", "all"],
        required=True,
    )
    run_parser.add_argument(
        "--config",
        default="config/setting.json",
    )
    run_parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
    )

    args = parser.parse_args()

    if args.max_batches is not None and args.stage != "ingest":
        parser.error("--max-batches applies only to --stage ingest.")

    # Enable the full pipeline after its remaining stages exist.
    if args.stage == "all":
        parser.error(
            "The full pipeline is not implemented yet. Choose ingest, inspect, validate or population."
        )

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    try:
        if args.stage == "ingest":
            result = download_batches(config, max_batches=args.max_batches)
            summary = {key: result[key] for key in ("downloaded_records", "last_batch", "completed")}
        elif args.stage == "inspect":
            result = inspect_raw_data(config)
            summary = {key: result[key] for key in ("selected_records", "snapshot_records", "snapshot_completed", "selected_files")}
        elif args.stage == "validate":
            result = validate_dataset(config)
            summary = {key: result[key] for key in ("status", "raw_records", "accepted_records", "quarantined_records", "snapshot_completed", "report_path")}
        else:
            result = join_population(config)
            summary = {key: result[key] for key in ("status", "input_records", "output_records", "join_status_counts", "borough_comparison_counts", "snapshot_completed", "report_path")}
    except ConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except DownloadError as error:
        print(f"Network error: {error}", file=sys.stderr)
        return 3
    except (DataError, CheckpointError) as error:
        print(f"Data/state error: {error}", file=sys.stderr)
        return 4
    except OSError as error:
        print(f"File-system error: {error}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("Interrupted. Run the command again to resume.", file=sys.stderr)
        return 130

    print(json.dumps(summary, indent=2))

    return 0
