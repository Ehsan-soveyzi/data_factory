"""Section 6: exact cost-based thresholds from the frozen development OOF scores."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.metrics import roc_curve
from sklearn.model_selection import FixedThresholdClassifier

from . import credit_missingness as missingness
from . import credit_model_comparison as comparison


FAMILIES = comparison.FAMILIES
FN_COSTS = (12, 4)
FP_COST = 1
POSITIVE_LABEL = 1


def _records(frame):
    # pandas.to_json(double_precision=15) can move an observed-score threshold.
    # Python's JSON float representation round-trips the exact binary value.
    return [{key: value.item() if isinstance(value, np.generic) else value
             for key, value in row.items()} for row in frame.to_dict(orient="records")]


def load_predictions(evidence_dir):
    """Validate the Section 5 artifact; no raw-data read, DB call or model fit."""
    evidence_dir = Path(evidence_dir)
    study = json.loads((evidence_dir / "section_05_model_comparison.json").read_text(encoding="utf-8"))
    partition = json.loads((evidence_dir / "section_02_partition.json").read_text(encoding="utf-8"))
    receipt = study["oof_artifact"]
    if Path(receipt["file"]).name != receipt["file"]:
        raise ValueError("The OOF artifact must be local to the evidence directory.")
    path = evidence_dir / receipt["file"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != receipt["sha256"]:
        raise ValueError("The Section 5 OOF artifact hash changed; regenerate Section 5 explicitly.")
    if (study["source"]["source_fingerprint"] != partition["source_fingerprint"] or
            study["holdout_evaluations"] != 0 or receipt["final_holdout_included"]):
        raise ValueError("The OOF artifact must belong to the reserved development snapshot.")
    with np.load(path, allow_pickle=False) as loaded:
        expected_keys = {"row_id", "fold", "target", "source_fingerprint", *FAMILIES}
        if set(loaded.files) != expected_keys:
            raise ValueError("Unexpected OOF artifact schema.")
        arrays = {key: loaded[key].copy() for key in loaded.files}
    if str(arrays.pop("source_fingerprint")) != partition["source_fingerprint"]:
        raise ValueError("The artifact embeds a different source fingerprint.")
    n = len(arrays["row_id"])
    if n != study["development_records"] or any(values.shape != (n,) for values in arrays.values()):
        raise ValueError("OOF vectors must cover the same complete development population.")
    if (not np.issubdtype(arrays["row_id"].dtype, np.integer) or
            not np.isin(arrays["target"], [0, 1]).all() or len(np.unique(arrays["target"])) != 2):
        raise ValueError("Unique integer source IDs and two binary target classes are required.")
    expected_ids = sorted(i for ids in partition["development_validation_row_ids"].values() for i in ids)
    if arrays["row_id"].tolist() != expected_ids or len(np.unique(arrays["row_id"])) != n:
        raise ValueError("OOF row IDs differ from the exact saved development membership.")
    if set(arrays["row_id"]).intersection(partition["holdout_row_ids"]):
        raise ValueError("Final holdout rows must never enter threshold selection.")
    if not np.isin(arrays["fold"], [1, 2, 3, 4, 5]).all():
        raise ValueError("Unexpected development fold identifiers.")
    for fold, ids in partition["development_validation_row_ids"].items():
        if arrays["row_id"][arrays["fold"] == int(fold)].tolist() != sorted(ids):
            raise ValueError("OOF folds differ from their saved membership.")
    y = arrays["target"]
    for family in FAMILIES:
        probability = arrays[family]
        if not np.isfinite(probability).all() or np.any((probability < 0) | (probability > 1)):
            raise ValueError("All OOF probabilities must be finite and between zero and one.")
        old = next(row for row in study["summary"] if row["model"] == family)
        current = metrics_at_threshold(y, probability, study["reference_threshold"], fn_cost=12)
        if any(current[key] != old[key] for key in ["cost", "false_negative", "false_positive"]):
            raise ValueError("The frozen OOF predictions do not reproduce Section 5 decisions.")
    audit = {"records": n, "models": list(FAMILIES), "source_fingerprint": partition["source_fingerprint"],
             "oof_file": receipt["file"], "oof_sha256": digest, "hash_verified": True,
             "saved_fold_memberships_verified": True, "section5_counts_reproduced": True,
             "holdout_records": len(partition["holdout_row_ids"]), "holdout_rows_included": 0,
             "positive_label": POSITIVE_LABEL, "database_reads": 0, "model_fits": 0}
    return pd.DataFrame(arrays), study, partition, audit


def _validated_vectors(target, probabilities):
    y, scores = np.asarray(target), np.asarray(probabilities, dtype=float)
    if (y.ndim != 1 or not len(y) or scores.shape != y.shape or
            not np.isin(y, [0, 1]).all() or not np.isfinite(scores).all() or
            np.any((scores < 0) | (scores > 1))):
        raise ValueError("Aligned binary targets and finite [0,1] probabilities are required.")
    return y.astype(np.int64), scores


def metrics_at_threshold(target, probabilities, threshold, *, fn_cost, fp_cost=FP_COST):
    y, scores = _validated_vectors(target, probabilities)
    if not np.isfinite(threshold) or fn_cost <= 0 or fp_cost <= 0:
        raise ValueError("A finite threshold and positive decision costs are required.")
    predicted = scores >= threshold
    fn = int(np.sum((y == 1) & ~predicted))
    fp = int(np.sum((y == 0) & predicted))
    tp = int(np.sum((y == 1) & predicted))
    tn = int(np.sum((y == 0) & ~predicted))
    cost = int(fn_cost * fn + fp_cost * fp)
    return {"records": len(y), "threshold": float(threshold), "false_negative": fn,
            "false_positive": fp, "true_positive": tp, "true_negative": tn,
            "predicted_positive": tp + fp, "cost": cost,
            "cost_per_1000": 1000 * cost / len(y)}


def threshold_counts(target, probabilities):
    """All distinct decision partitions, including all-negative and all-positive."""
    y, scores = _validated_vectors(target, probabilities)
    order = np.argsort(-scores, kind="stable")
    ordered_scores, ordered_y = scores[order], y[order]
    # Identical probabilities move together: no threshold splits a tied group.
    ends = np.flatnonzero(np.r_[np.diff(ordered_scores) != 0, True])
    positives = np.r_[0, ends + 1]
    tp = np.r_[0, np.cumsum(ordered_y, dtype=np.int64)[ends]]
    fp = positives - tp
    total_positive = int(np.sum(y))
    total_negative = len(y) - total_positive
    # Finite unlike ROC's initial +inf; also handles a maximum score of exactly 1.
    thresholds = np.r_[np.nextafter(ordered_scores[0], np.inf), ordered_scores[ends]]
    return pd.DataFrame({"threshold": thresholds, "false_negative": total_positive - tp,
                         "false_positive": fp, "true_positive": tp,
                         "true_negative": total_negative - fp, "predicted_positive": positives})


def select_thresholds(oof):
    """Select cost minima separately for three frozen models and two cost ratios."""
    y = oof.target.to_numpy(dtype=int)
    summaries, operating_points, folds, audits = [], [], [], []
    curves = {}
    for family in FAMILIES:
        scores = oof[family].to_numpy(dtype=float)
        counts = threshold_counts(y, scores)
        # Independent library check of every decision partition, not AUC optimization.
        fpr, tpr, _ = roc_curve(y, scores, pos_label=1, drop_intermediate=False)
        roc_fn = np.rint(np.sum(y == 1) * (1 - tpr)).astype(np.int64)
        roc_fp = np.rint(np.sum(y == 0) * fpr).astype(np.int64)
        if len(roc_fn) != len(counts):
            raise RuntimeError("Exact threshold candidates differ from full ROC decision partitions.")
        roc_error = int(max(np.max(np.abs(roc_fn - counts.false_negative.to_numpy())),
                            np.max(np.abs(roc_fp - counts.false_positive.to_numpy()))))
        if roc_error:
            raise RuntimeError("Independent ROC counts disagree with the threshold sweep.")
        for fn_cost in FN_COSTS:
            curve = counts.copy()
            curve["cost"] = fn_cost * curve.false_negative + FP_COST * curve.false_positive
            curve["cost_per_1000"] = 1000 * curve.cost / len(y)
            costs = curve.cost.to_numpy(dtype=np.int64)
            minimum_positions = np.flatnonzero(costs == costs.min())
            # Candidates are in descending threshold order: the first minimum
            # is the highest threshold, hence fewer positive/rejection decisions.
            best_position = int(minimum_positions[0])
            threshold = float(curve.iloc[best_position]["threshold"])
            selected = metrics_at_threshold(y, scores, threshold, fn_cost=fn_cost)
            row = curve.iloc[best_position]
            fields = ["false_negative", "false_positive", "true_positive", "true_negative", "cost"]
            direct_error = int(max(abs(selected[name] - int(row[name])) for name in fields))
            theoretical_threshold = FP_COST / (fn_cost + FP_COST)
            default = metrics_at_threshold(y, scores, .5, fn_cost=fn_cost)
            theoretical = metrics_at_threshold(y, scores, theoretical_threshold, fn_cost=fn_cost)
            if direct_error or selected["cost"] > min(default["cost"], theoretical["cost"]):
                raise RuntimeError("The selected threshold is not an exact decision-cost minimum.")
            fold_costs = []
            for fold in sorted(oof.fold.unique()):
                mask = oof.fold.to_numpy() == fold
                metrics = metrics_at_threshold(y[mask], scores[mask], threshold, fn_cost=fn_cost)
                folds.append({"model": family, "fn_cost": fn_cost, "fold": int(fold), **metrics})
                fold_costs.append(metrics["cost_per_1000"])
            baseline_cost = fn_cost * int(np.sum(y == 1))
            summaries.append({"model": family, "fn_cost": fn_cost, "fp_cost": FP_COST, **selected,
                "theoretical_threshold": theoretical_threshold,
                "cost_at_0_5_per_1000": default["cost_per_1000"],
                "cost_at_theoretical_per_1000": theoretical["cost_per_1000"],
                "change_vs_theoretical_per_1000": selected["cost_per_1000"] - theoretical["cost_per_1000"],
                "baseline_cost_per_1000": 1000 * baseline_cost / len(y),
                "reduction_vs_baseline_percent": 100 * (baseline_cost - selected["cost"]) / baseline_cost,
                "fold_cost_std_descriptive": float(np.std(fold_costs, ddof=1)),
                "candidate_thresholds": len(curve), "equal_minimum_partitions": len(minimum_positions)})
            for label, metrics in [("threshold_0.5", default), ("cost_derived", theoretical), ("selected", selected)]:
                operating_points.append({"model": family, "fn_cost": fn_cost, "rule": label, **metrics})
            audits.append({"model": family, "fn_cost": fn_cost, "candidate_thresholds": len(curve),
                           "unique_probabilities": len(counts) - 1,
                           "all_negative_included": bool(curve.iloc[0].predicted_positive == 0),
                           "all_positive_included": bool(curve.iloc[-1].predicted_positive == len(y)),
                           "tied_probabilities_kept_together": True,
                           "roc_count_max_error": roc_error, "selected_direct_count_max_error": direct_error,
                           "selected_global_cost": int(costs[best_position]),
                           "minimum_enumerated_cost": int(costs.min()),
                           "exact_threshold_hex": threshold.hex()})
            curves[(family, fn_cost)] = curve
    summary = pd.DataFrame(summaries)
    sensitivity = []
    for family in FAMILIES:
        twelve = next(r for r in summaries if r["model"] == family and r["fn_cost"] == 12)
        four = next(r for r in summaries if r["model"] == family and r["fn_cost"] == 4)
        fixed_under_four = metrics_at_threshold(y, oof[family].to_numpy(), twelve["threshold"], fn_cost=4)
        if four["threshold"] < twelve["threshold"] or four["false_negative"] < twelve["false_negative"] or four["false_positive"] > twelve["false_positive"]:
            raise RuntimeError("Cost sensitivity violates the nested threshold decision order.")
        sensitivity.append({"model": family, "threshold_12": twelve["threshold"],
            "threshold_4": four["threshold"], "threshold_change": four["threshold"] - twelve["threshold"],
            "fn_at_12": twelve["false_negative"], "fn_at_4": four["false_negative"],
            "fp_at_12": twelve["false_positive"], "fp_at_4": four["false_positive"],
            "cost_4_at_threshold_12_per_1000": fixed_under_four["cost_per_1000"],
            "cost_4_at_threshold_4_per_1000": four["cost_per_1000"]})
    return summary, pd.DataFrame(operating_points), pd.DataFrame(folds), pd.DataFrame(audits), pd.DataFrame(sensitivity), curves


def selected_decisions(summary, source, integrity):
    decisions = {}
    for fn_cost in FN_COSTS:
        rows = _records(summary.loc[summary.fn_cost.eq(fn_cost)])
        best = min(rows, key=lambda row: (row["cost"], FAMILIES.index(row["model"])))
        family = best["model"]
        decisions[str(fn_cost)] = {
            "model": family, "threshold": float(best["threshold"]),
            "threshold_hex": float(best["threshold"]).hex(), "comparison": ">=", "positive_label": 1,
            "fn_cost": fn_cost, "fp_cost": FP_COST,
            "features": source["candidate_features"][family], "policy": source["imputation_policy"],
            "model_parameters": source["candidate_parameters"][family],
            "source_fingerprint": integrity["source_fingerprint"], "oof_sha256": integrity["oof_sha256"],
            "development_cost_per_1000": best["cost_per_1000"],
            "reduction_vs_baseline_percent": best["reduction_vs_baseline_percent"],
            "holdout_evaluations": 0, "decision_estimator": "sklearn.model_selection.FixedThresholdClassifier",
        }
    return decisions


def make_decision_estimator(selection):
    """Unfitted native sklearn wrapper, preserving the exact >= threshold rule."""
    if selection["positive_label"] != 1 or selection["comparison"] != ">=":
        raise ValueError("This decision policy uses positive class 1 and p >= threshold.")
    threshold = float(selection["threshold"])
    if threshold.hex() != selection["threshold_hex"] or not np.isfinite(threshold):
        raise ValueError("The saved threshold lost numeric precision.")
    pipeline = comparison.make_model_pipeline(
        family=selection["model"], features=selection["features"], policy=selection["policy"])
    pipeline.set_params(**{"classifier__" + key: value for key, value in selection["model_parameters"].items()})
    return FixedThresholdClassifier(pipeline, threshold=threshold, pos_label=1, response_method="predict_proba")


def save_study(evidence_dir, *, source, partition, integrity, summary, operating_points,
               fold_results, audits, sensitivity):
    evidence_dir = Path(evidence_dir)
    decisions = selected_decisions(summary, source, integrity)
    selected = {"primary": decisions["12"], "sensitivity_4_to_1": decisions["4"],
        "threshold_tie_break": "Highest threshold among equal integer minimum costs; fewer positive/rejection decisions.",
        "model_tie_break": "Lowest integer cost, then the declared Section 5 model order.",
        "model_order": list(FAMILIES),
        "scope": "Model/threshold selection on pooled development OOF predictions, not unbiased final performance. Feature selection used this same development population. Final holdout evaluation remains pending."}
    result = {"section": "Challenge 3 Section 6: cost-based decision thresholds",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": source["source"], "source_artifact_audit": integrity,
        "development_records": integrity["records"], "holdout_records": len(partition["holdout_row_ids"]),
        "holdout_evaluations": 0, "model_fits": 0, "database_reads": 0,
        "positive_label": 1, "comparison": ">=",
        "search": "Exact sorted score groups plus a finite threshold above the maximum score; all-negative/all-positive decisions included, ties never split.",
        "theoretical_rule": "C_FP / (C_FN + C_FP), assuming calibrated posterior probabilities and the stated zero correct-decision costs. Calibration is not assumed for empirical OOF selection.",
        "summary": _records(summary), "operating_points": _records(operating_points),
        "fold_results": _records(fold_results), "threshold_audit": _records(audits),
        "sensitivity": _records(sensitivity), "selected_decisions": selected,
        "fold_scope": "The pooled OOF-selected threshold is applied to each development fold for descriptive cost variation only; these folds are not an independent evaluation of threshold selection.",
        "probabilities_changed": False, "roc_auc_changed_by_thresholding": False,
        "versions": {"numpy": np.__version__, "pandas": pd.__version__, "scikit-learn": sklearn.__version__},
        "data_changes": "No database/CSV writes or changes to prior artifacts. Only development decision policies were selected."}
    choice_path = evidence_dir / "section_06_selected_decision.json"
    missingness.write_json(choice_path, selected)
    reloaded = json.loads(choice_path.read_text(encoding="utf-8"))
    for key in ["primary", "sensitivity_4_to_1"]:
        if reloaded[key]["threshold"].hex() != selected[key]["threshold_hex"]:
            raise RuntimeError("The selected threshold changed during JSON persistence.")
        make_decision_estimator(reloaded[key])
    result["threshold_round_trip_exact"] = True
    missingness.write_json(evidence_dir / "section_06_threshold_selection.json", result)
    return result, reloaded


def plot_cost_curves(curves, summary, path):
    import matplotlib.pyplot as plt

    labels = {"logistic": "Logistic", "random_forest": "Random Forest",
              "hist_gradient_boosting": "HistGradientBoosting"}
    colors = {"logistic": "#5ba9ef", "random_forest": "#ffb454", "hist_gradient_boosting": "#67cf9d"}
    with plt.style.context("dark_background"):
        figure, axes = plt.subplots(1, 2, figsize=(12, 4), constrained_layout=True)
        for axis, fn_cost in zip(axes, FN_COSTS):
            current = summary.loc[summary.fn_cost.eq(fn_cost)]
            upper = min(1.05, max(.30, 1.25 * float(current.threshold.max())))
            for family in FAMILIES:
                curve = curves[(family, fn_cost)]
                best = current.loc[current.model.eq(family)].iloc[0]
                indices = np.unique(np.r_[np.linspace(0, len(curve) - 1, min(2400, len(curve))).astype(int),
                                          np.flatnonzero(curve.threshold.to_numpy() == best.threshold)])
                displayed = curve.iloc[indices].sort_values("threshold")
                axis.plot(displayed.threshold, displayed.cost_per_1000,
                          label=labels[family], color=colors[family], linewidth=1.6)
                axis.scatter(best.threshold, best.cost_per_1000, marker="*", s=85, color=colors[family], zorder=4)
            axis.axvline(FP_COST / (fn_cost + FP_COST), color="#dddddd", linestyle="--", linewidth=1,
                         label="Cost-derived threshold")
            axis.axhline(float(current.iloc[0].baseline_cost_per_1000), color="#999999", linestyle=":", linewidth=1)
            axis.set(xlim=(0, upper), xlabel="Threshold (predict 1 when p >= t)",
                     ylabel="Cost per 1,000 applicants", title=f"FN:FP cost = {fn_cost}:1")
            axis.grid(alpha=.15)
        axes[0].legend(fontsize=8)
        figure.savefig(path, dpi=160, facecolor=figure.get_facecolor())
        plt.close(figure)
