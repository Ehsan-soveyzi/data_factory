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


def main():
    parser = argparse.ArgumentParser(prog="data-factory")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run")
    run_parser.add_argument(
        "--stage",
        choices=["ingest", "all"],
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

    # Enable the full pipeline after its remaining stages exist.
    if args.stage == "all":
        parser.error(
            "The full pipeline is not implemented yet. Use --stage ingest."
        )

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    try:
        summary = download_batches(
            config,
            max_batches=args.max_batches,
        )
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

    print(json.dumps({
        "downloaded_records": summary["downloaded_records"],
        "last_batch": summary["last_batch"],
        "completed": summary["completed"],
    }, indent=2))

    return 0