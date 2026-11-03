"""Command-line interface for the data factory."""

import argparse
from datetime import datetime, timezone
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
from .delivery import build_delivery
from .pipeline import run_pipeline
from .incremental import incremental_config


def main():
    parser = argparse.ArgumentParser(prog="data-factory")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run")
    run_parser.add_argument(
        "--stage",
        choices=["ingest", "inspect", "validate", "population", "finalize", "all"],
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
    run_parser.add_argument("--since-last-run", action="store_true",
                            help="Acquire a separate interval after the preceding completed scope.")

    clean_parser = commands.add_parser("clean-olist", help="Prepare local Olist CSVs without a database connection.")
    clean_parser.add_argument("--project-root", default=".")

    csv_parser = commands.add_parser("validate-csv", help="Validate a local CSV using an independent data contract.")
    csv_parser.add_argument("--config", required=True, help="CSV source/contract/output settings; paths resolve relative to this file.")

    predict_parser = commands.add_parser("predict-credit", help="Predict new credit CSV records using the frozen Stage 03 model; no training or DB connection.")
    predict_parser.add_argument("--input", required=True, help="UTF-8 CSV with the ten raw credit features; row_id and target are optional.")
    predict_parser.add_argument("--output", required=True, help="Destination CSV for probabilities and binary predictions.")
    predict_parser.add_argument("--model", default="stages/stage_03/evidence/section_07_fitted_classifier.joblib",
                                help="Fitted joblib; keep section_07_evaluation.json beside it.")
    predict_parser.add_argument("--batch-size", type=int, default=10000)

    args = parser.parse_args()

    if args.command == "predict-credit":
        try:
            from .credit_inference import predict_csv
            result = predict_csv(args.input, args.output, model_path=args.model, batch_size=args.batch_size)
        except ImportError:
            print('Missing ML dependencies: install requirements.txt and the project package.', file=sys.stderr)
            return 2
        except (ValueError, KeyError, TypeError) as error:
            print(f"Prediction data/model error: {error}", file=sys.stderr)
            return 4
        except OSError as error:
            print(f"File-system error: {error}", file=sys.stderr)
            return 5
        except KeyboardInterrupt:
            return 130
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "validate-csv":
        from .csv_quality import validate_csv
        try:
            result = validate_csv(args.config)
        except ConfigurationError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        except DataError as error:
            print(f"Data/state error: {error}", file=sys.stderr)
            return 4
        except OSError as error:
            print(f"File-system error: {error}", file=sys.stderr)
            return 5
        except KeyboardInterrupt:
            return 130
        print(json.dumps({key: result[key] for key in ("dataset", "status", "raw_records",
            "accepted_records", "quarantined_records", "quarantine_fraction", "output_dir", "report_path")}, indent=2))
        return 0

    if args.command == "clean-olist":
        from .olist_cleaning import run_olist_cleaning
        try:
            result = run_olist_cleaning(args.project_root)
        except DataError as error:
            print(f"Data/state error: {error}", file=sys.stderr)
            return 4
        except (ValueError, KeyError) as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        except OSError as error:
            print(f"File-system error: {error}", file=sys.stderr)
            return 5
        except KeyboardInterrupt:
            return 130
        print(json.dumps({"status": result["report"]["status"],
                          "deduplicated_rows": result["report"]["deduplicated_rows"],
                          "quarantined_rows": result["report"]["quarantined_rows"],
                          "output_dir": result["output_dir"], "manifest_path": result["manifest_path"]}, sort_keys=True))
        return 0

    if args.max_batches is not None and args.stage != "ingest":
        parser.error("--max-batches applies only to --stage ingest.")
    if args.since_last_run and args.stage not in ("ingest", "all"):
        parser.error("--since-last-run applies only to ingest or all.")

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    try:
        started_at = datetime.now(timezone.utc).isoformat()
        if args.since_last_run:
            config = incremental_config(config)
        if args.stage == "all":
            result = run_pipeline(config)
            summary = {key: result[key] for key in ("status", "delivery_complete", "analytical_records", "monthly_summary_rows", "output_dir", "manifest_path")}
        elif args.stage == "ingest":
            result = download_batches(config, max_batches=args.max_batches)
            summary = {key: result[key] for key in ("downloaded_records", "last_batch", "completed")}
        elif args.stage == "inspect":
            result = inspect_raw_data(config)
            summary = {key: result[key] for key in ("selected_records", "snapshot_records", "snapshot_completed", "selected_files")}
        elif args.stage == "validate":
            result = validate_dataset(config)
            summary = {key: result[key] for key in ("status", "raw_records", "accepted_records", "quarantined_records", "snapshot_completed", "report_path")}
        elif args.stage == "population":
            result = join_population(config)
            summary = {key: result[key] for key in ("status", "input_records", "output_records", "join_status_counts", "borough_comparison_counts", "snapshot_completed", "report_path")}
        else:
            result = build_delivery(config, started_at=started_at, execution_stage=args.stage)
            summary = {key: result[key] for key in ("status", "delivery_complete", "analytical_records", "monthly_summary_rows", "output_dir", "manifest_path")}
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
