"""Section 8: development-only interpretation of the frozen decision recipe."""

from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance
from sklearn.metrics import roc_auc_score
from threadpoolctl import threadpool_limits

from . import credit_final_evaluation as evaluation
from . import credit_missingness as missingness
from . import credit_preprocessing as preprocessing
from . import credit_threshold_selection as thresholding


RESULT_FILE = "section_08_interpretation.json"
AGE_LABELS = ("under_35", "35_to_49", "50_to_64", "65_plus")
LABELS = dict(zip(missingness.FEATURES, (
    "استفاده از اعتبار بدون وثیقه", "سن", "دیرکرد ۳۰ تا ۵۹ روز", "نسبت بدهی",
    "درآمد ماهانه", "تعداد خطوط اعتباری باز", "دیرکرد ۹۰ روز و بیشتر",
    "تعداد وام‌های ملکی", "دیرکرد ۶۰ تا ۸۹ روز", "تعداد افراد تحت تکفل")))
HYPOTHESES = {
    "RevolvingUtilizationOfUnsecuredLines": "استفاده بیشتر از اعتبار معمولاً با ریسک بیشتر سازگار است.",
    "age": "ریسک می‌تواند بین سنین متفاوت باشد؛ سن به‌تنهایی علت نکول نیست.",
    "DebtRatio": "بار بدهی بالاتر معمولاً ریسک بیشتری دارد؛ معنای مخرج نسبت باید بررسی شود.",
    "MonthlyIncome": "درآمد بیشتر ممکن است توان بازپرداخت را تقویت کند؛ ثبات درآمد ثبت نشده است.",
    "NumberOfOpenCreditLinesAndLoans": "تعداد خطوط هم سابقه اعتبار و هم تعهد بیشتر را بازتاب می‌دهد؛ اثر یکنواخت لازم نیست.",
    "NumberRealEstateLoansOrLines": "وام ملکی می‌تواند نشانه پشتوانه یا بار تعهد باشد؛ جهت اثر قطعی نیست.",
    "NumberOfDependents": "افراد تحت تکفل بیشتر ممکن است فشار بودجه را افزایش دهند.",
    **{name: "تکرار دیرکرد با ریسک بیشتر سازگار است؛ شدت دیرکرد نیز اهمیت دارد."
       for name in missingness.LATE_COLUMNS},
}


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _wilson(successes, count):
    """Approximate descriptive binomial interval; not selection-adjusted inference."""
    if not count:
        return [None, None]
    z = 1.959963984540054
    rate = successes / count
    denominator = 1 + z * z / count
    center = (rate + z * z / (2 * count)) / denominator
    radius = z * np.sqrt(rate * (1 - rate) / count + z * z / (4 * count**2)) / denominator
    return [float(max(0, center - radius)), float(min(1, center + radius))]


def cohort_metrics(target, probabilities, selection):
    """Explicit denominators, including support for empty/single-class slices."""
    y = np.asarray(target)
    if not len(y):
        return {"records": 0}
    m = thresholding.metrics_at_threshold(y, probabilities, selection["threshold"],
                                         fn_cost=selection["fn_cost"], fp_cost=selection["fp_cost"])
    tp, fp, fn, tn = (m[k] for k in ("true_positive", "false_positive", "false_negative", "true_negative"))
    positive, negative = tp + fn, tn + fp
    return {**m, "positives": positive, "negatives": negative,
            "positive_rate": positive / len(y), "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / positive if positive else None,
            "false_negative_rate": fn / positive if positive else None,
            "false_positive_rate": fp / negative if negative else None,
            "roc_auc": float(roc_auc_score(y, probabilities)) if positive and negative else None,
            "recall_ci95": _wilson(tp, positive), "fpr_ci95": _wilson(fp, negative)}


def _age_groups(clean):
    return pd.cut(clean.age, [0, 35, 50, 65, np.inf], right=False, labels=AGE_LABELS)


def _cohorts(clean, y, probabilities, selection):
    ages = _age_groups(clean)
    if ages.isna().any():
        raise ValueError("Every accepted development age must belong to a declared age group.")
    fairness = [{"age_group": label, **cohort_metrics(y[ages.eq(label).to_numpy()],
        probabilities[ages.eq(label).to_numpy()], selection)} for label in AGE_LABELS]
    if sum(row["records"] for row in fairness) != len(y):
        raise RuntimeError("Age groups must cover development once, without overlap.")
    late = clean.loc[:, list(missingness.LATE_COLUMNS)]
    cases = (
        ("clean_history", "سابقه بدون دیرکرد ثبت‌شده", late.notna().all(axis=1) & late.eq(0).all(axis=1),
         "متقاضی بدون دیرکرد قبلی ممکن است پس از شوک درآمد یا اشتغال نکول کند؛ مدل این شوک آینده را مشاهده نمی‌کند."),
        ("high_utilization", "استفاده حداقل ۱۰۰٪ از اعتبار", clean.RevolvingUtilizationOfUnsecuredLines.ge(1),
         "استفاده بالا همیشه به نکول منجر نمی‌شود؛ افراد سالم نیز ممکن است اشتباه پرخطر اعلام شوند."),
        ("incomplete_record", "پرونده دارای مقدار گمشده یا کد ویژه", clean.isna().any(axis=1),
         "میانه‌گذاری و نشانگر، درآمد واقعی یا معنای کد ویژه را بازیابی نمی‌کنند؛ پرونده ناقص همچنان خطا دارد."),
    )
    failures = [{"scenario": name, "label": label, "mechanism": explanation,
        **cohort_metrics(y[mask.to_numpy()], probabilities[mask.to_numpy()], selection)}
        for name, label, mask, explanation in cases]
    return fairness, failures


def _profiles(clean, y):
    """Observed target rates explain associations, never individual causal effects."""
    rows = []
    for feature in missingness.FEATURES:
        values = clean[feature]
        if feature in missingness.LATE_COLUMNS:
            bins = pd.cut(values, [-1, .5, 1.5, np.inf], labels=["0", "1", "2+"])
        elif feature == "age":
            bins = _age_groups(clean)
        elif feature == "RevolvingUtilizationOfUnsecuredLines":
            bins = pd.cut(values, [-np.inf, .25, .5, 1, np.inf], right=False)
        elif feature == "DebtRatio":
            bins = pd.cut(values, [-np.inf, .25, .5, 1, 10, np.inf], right=False)
        else:
            bins = pd.qcut(values, 4, duplicates="drop")
        labels = bins.astype("string").fillna("missing_or_special_code")
        table = pd.DataFrame({"bin": labels.to_numpy(), "target": y})
        for label, group in table.groupby("bin", sort=False):
            rows.append({"feature": feature, "bin": str(label), "records": len(group),
                         "positives": int(group.target.sum()), "default_rate": float(group.target.mean())})
    return rows


def _business_review(top, profiles, clean, y):
    review = []
    for row in top:
        feature = row["feature"]
        observed = [p for p in profiles if p["feature"] == feature and p["bin"] != "missing_or_special_code"]
        observed.sort(key=lambda p: p["default_rate"])
        low, high = observed[0], observed[-1]
        finding = (f"نرخ نکول مشاهده‌شده در بازه {low['bin']} برابر {100*low['default_rate']:.1f}٪ "
                   f"و در بازه {high['bin']} برابر {100*high['default_rate']:.1f}٪ است.")
        values = clean[feature]
        if feature in missingness.LATE_COLUMNS:
            first, last, expected_direction = values.eq(0), values.ge(2), 1
        elif feature == "RevolvingUtilizationOfUnsecuredLines":
            first, last, expected_direction = values.lt(.25), values.ge(1), 1
        elif feature == "age":
            first, last, expected_direction = values.lt(35), values.ge(65), -1
        elif feature == "DebtRatio":
            first, last, expected_direction = values.ge(.5) & values.lt(1), values.ge(10), 1
        else:
            first = values.le(values.quantile(.25))
            last = values.ge(values.quantile(.75))
            expected_direction = -1 if feature == "MonthlyIncome" else 0
        first_rate = float(y[first.to_numpy()].mean()) if first.any() else None
        last_rate = float(y[last.to_numpy()].mean()) if last.any() else None
        if first_rate is None or last_rate is None or not expected_direction:
            alignment = "جهت یکنواختی از پیش فرض نشده است؛ وابستگی مشاهده‌ای گزارش می‌شود."
        elif (last_rate - first_rate) * expected_direction > 0:
            alignment = "مقایسه بازه کم و زیاد با شهود ریسک سازگار است؛ رابطه علّی نتیجه نمی‌شود."
        else:
            alignment = "جهت مشاهده‌شده با شهود ساده سازگار نیست؛ قرارداد داده و متغیرهای همراه باید بررسی شوند."
        review.append({"feature": feature, "label": LABELS[feature], "hypothesis": HYPOTHESES[feature],
                       "observed_comparison": finding,
                       "business_alignment": alignment,
                       "contrast_first_default_rate": first_rate, "contrast_last_default_rate": last_rate,
                       "interpretation": "اهمیت، اندازه وابستگی پیش‌بینی را می‌سنجد؛ جهت یا اثر علّی را اثبات نمی‌کند."})
    high = clean.DebtRatio.ge(10).to_numpy()
    moderate = (clean.DebtRatio.ge(.5) & clean.DebtRatio.lt(1)).to_numpy()
    income_uncertain = (clean.MonthlyIncome.isna() | clean.MonthlyIncome.eq(0)).to_numpy()
    debt = {"high_ratio_definition": "DebtRatio >= 10", "comparison_definition": "0.5 <= DebtRatio < 1",
            "high_ratio_records": int(high.sum()), "comparison_records": int(moderate.sum()),
            "high_ratio_default_rate": float(y[high].mean()) if high.any() else None,
            "comparison_default_rate": float(y[moderate].mean()) if moderate.any() else None,
            "missing_or_zero_income_share_in_high_ratio": float(income_uncertain[high].mean()) if high.any() else None,
            "interpretation": "مقادیر بسیار بزرگ نسبت لزوماً ریسک بالاتر ندارند؛ همراهی با درآمد صفر/گمشده و ابهام معنای نسبت بررسی شد. این ارتباط علت قطعی یا مجوز تغییر داده نیست."}
    return review, debt


def _importance(raw, y, groups, folds, oof, selection, repeats, progress):
    runs, audits = [], []

    def score(estimator, values, target):
        probabilities = estimator.predict_proba(values)[:, np.flatnonzero(estimator.classes_ == 1)[0]]
        metrics = thresholding.metrics_at_threshold(target, probabilities, selection["threshold"],
            fn_cost=selection["fn_cost"], fp_cost=selection["fp_cost"])
        return {"negative_cost": -metrics["cost_per_1000"], "roc_auc": float(roc_auc_score(target, probabilities))}

    for fold, train_positions, valid_positions in folds:
        if progress:
            progress(f"Interpretation fold {fold}/5: fit frozen recipe; permute 10 raw inputs x {repeats} repeats.")
        train = raw.iloc[train_positions]
        valid = raw.iloc[valid_positions].loc[:, list(missingness.FEATURES)]
        target = y.iloc[valid_positions]
        estimator = thresholding.make_decision_estimator(selection)
        with threadpool_limits(limits=1):
            estimator.fit(train, y.iloc[train_positions])
            prep = estimator.estimator_.named_steps["preprocessing"]
            before = preprocessing._state_hash(prep)
            probabilities = estimator.predict_proba(valid)[:, np.flatnonzero(estimator.classes_ == 1)[0]]
            expected = oof.loc[valid.index.to_numpy(dtype="int64"), selection["model"]].to_numpy()
            error = float(np.max(np.abs(probabilities - expected)))
            if error > 1e-12:
                raise RuntimeError(f"Fold {fold}: frozen recipe no longer reproduces saved OOF probabilities ({error}).")
            result = permutation_importance(estimator, valid, target, scoring=score,
                n_repeats=repeats, random_state=42 + int(fold), n_jobs=1)
        row_overlap = len(set(train.index) & set(valid.index))
        group_overlap = len(set(np.asarray(groups)[train_positions]) & set(np.asarray(groups)[valid_positions]))
        unchanged = before == preprocessing._state_hash(prep)
        if row_overlap or group_overlap or not unchanged:
            raise RuntimeError("Interpretation violated a fold boundary or mutated preprocessing.")
        audits.append({"fold": int(fold), "train_records": len(train), "validation_records": len(valid),
            "oof_probability_max_error": error, "row_overlap": row_overlap, "group_overlap": group_overlap,
            "parameters_unchanged": unchanged,
            "train_ids_sha256": hashlib.sha256(train.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
            "validation_ids_sha256": hashlib.sha256(valid.index.to_numpy(dtype="int64").tobytes()).hexdigest()})
        for j, feature in enumerate(missingness.FEATURES):
            for repeat in range(repeats):
                runs.append({"fold": int(fold), "repeat": repeat + 1, "feature": feature,
                    "validation_records": len(valid),
                    "cost_increase_per_1000": float(result["negative_cost"].importances[j, repeat]),
                    "auc_decrease": float(result["roc_auc"].importances[j, repeat])})
    table = pd.DataFrame(runs)
    ranked = []
    for feature in missingness.FEATURES:
        subset = table[table.feature.eq(feature)]
        fold_means = subset.groupby("fold").cost_increase_per_1000.mean()
        ranked.append({"feature": feature, "label": LABELS[feature],
            "cost_increase_per_1000": float(np.average(subset.cost_increase_per_1000, weights=subset.validation_records)),
            "fold_std": float(fold_means.std(ddof=1)),
            "mean_fold_auc_decrease": float(np.average(subset.auc_decrease, weights=subset.validation_records))})
    ranked.sort(key=lambda row: (-row["cost_increase_per_1000"], row["feature"]))
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    return ranked, runs, audits


def analyze(engine, evidence_dir, *, repeats=5, progress=print):
    """Verify the DB snapshot; reuse evidence or fit five disposable CV clones."""
    if not isinstance(repeats, int) or repeats < 2:
        raise ValueError("At least two integer permutation repetitions are required.")
    evidence_dir = Path(evidence_dir)
    selection, oof, _, saved_partition, _ = evaluation.development_evaluation(evidence_dir)
    raw, y, groups, folds, partition, _, database = preprocessing.load_development(engine, evidence_dir)
    ids = raw.index.to_numpy(dtype="int64")
    oof = oof.set_index("row_id").loc[ids]
    if (partition["source_fingerprint"] != saved_partition["source_fingerprint"] or
            not np.array_equal(y.to_numpy(), oof.target.to_numpy()) or
            set(ids) & set(partition["holdout_row_ids"])):
        raise ValueError("Interpretation must use the exact aligned development population only.")
    protected = {str(p.name): _hash(p) for p in evidence_dir.glob("section_07_*") if p.is_file()}
    final = json.loads((evidence_dir / evaluation.RESULT_FILE).read_text(encoding="utf-8"))
    recipe_hash = evaluation._recipe_hash(selection)
    if final["recipe_sha256"] != recipe_hash:
        raise ValueError("Interpretation recipe differs from the frozen final recipe.")
    for artifact in (final["model_artifact"], final["prediction_artifact"]):
        if protected.get(artifact["file"]) != artifact["sha256"]:
            raise ValueError("Frozen Section 7 artifact integrity failed.")
    signature = {"recipe_sha256": recipe_hash, "source_fingerprint": partition["source_fingerprint"],
        "oof_sha256": selection["oof_sha256"], "repeats": repeats, "module_sha256": _hash(__file__),
        "protected_section7_sha256": protected}
    path = evidence_dir / RESULT_FILE
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous["signature"] == signature:
            if progress:
                progress("Reusing verified Section 8 evidence; no auxiliary refits.")
            return previous
    ranked, runs, audits = _importance(raw, y, groups, folds, oof, selection, repeats, progress)
    clean = preprocessing.semantic_inputs(raw)
    target = y.to_numpy(dtype=int)
    fairness, failures = _cohorts(clean, target, oof[selection["model"]].to_numpy(), selection)
    profiles = _profiles(clean, target)
    review, debt = _business_review(ranked[:5], profiles, clean, target)
    if any(_hash(evidence_dir / name) != digest for name, digest in protected.items()):
        raise RuntimeError("Section 7 artifacts changed during interpretation.")
    result = {"section": "Challenge 3 Section 8: interpretation and model card",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "signature": signature,
        "selection": selection, "database": database, "development_records": len(raw),
        "age_range": [int(clean.age.min()), int(clean.age.max())],
        "auxiliary_cv_model_fits": len(folds), "final_model_fits": 0, "holdout_evaluations": 0,
        "importance": ranked, "top_five": ranked[:5], "permutation_runs": runs, "fit_audit": audits,
        "business_review": review, "observed_risk_profiles": profiles, "debt_ratio_investigation": debt,
        "age_fairness": fairness, "failure_scenarios": failures,
        "fairness_gaps": {name: float(max(r[name] for r in fairness if r[name] is not None) -
            min(r[name] for r in fairness if r[name] is not None))
            for name in ("recall", "false_positive_rate", "false_negative_rate")},
        "method": "Five saved development folds; frozen recipe refitted on training only; raw-input permutation with five repetitions by default, downstream flags/derived features recomputed; rank by validation cost increase, secondary mean-fold AUC decrease.",
        "limits": "Descriptive audit of the selected recipe on selection development data, not an independent performance estimate. Correlated inputs may share importance; marginal permutations may create unlikely combinations. Importance is neither direction nor causation. Wilson intervals are approximate binomial intervals, not adjusted for duplicate-input groups or model/threshold selection. Failure cohorts can overlap. No subgroup-specific threshold was selected.",
        "data_changes": "No changes to DB/CSV, preprocessing policy, selected features, model, threshold or prior evidence."}
    missingness.write_json(path, result)
    return result


def plot_interpretation(result, path):
    import matplotlib.pyplot as plt

    with plt.style.context("dark_background"):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5), layout="constrained")
        rows = result["importance"][::-1]
        # Compact plot labels only; full source names are retained in evidence/tables.
        names = {"RevolvingUtilizationOfUnsecuredLines": "Credit utilization",
                 "NumberOfTime30-59DaysPastDueNotWorse": "Late 30-59 days",
                 "NumberOfTime60-89DaysPastDueNotWorse": "Late 60-89 days",
                 "NumberOfTimes90DaysLate": "Late 90+ days",
                 "NumberOfOpenCreditLinesAndLoans": "Open credit lines",
                 "NumberRealEstateLoansOrLines": "Real estate loans",
                 "NumberOfDependents": "Dependents"}
        axes[0].barh([names.get(r["feature"], r["feature"]) for r in rows],
                     [r["cost_increase_per_1000"] for r in rows],
                     xerr=[r["fold_std"] for r in rows], color="#65b8df", capsize=3)
        axes[0].axvline(0, color="gray", linewidth=.8)
        axes[0].set(title="Raw-input permutation importance (development CV)",
                    xlabel="Cost increase per 1,000 (error bars: fold SD)")
        ages = result["age_fairness"]
        x = np.arange(len(ages))
        for metric, ci, offset, label in (("recall", "recall_ci95", -.07, "Recall"),
                                        ("false_positive_rate", "fpr_ci95", .07, "False-positive rate")):
            values = np.array([r[metric] for r in ages])
            bounds = np.array([r[ci] for r in ages]).T
            axes[1].errorbar(x + offset, values, yerr=np.maximum(0, np.vstack([values - bounds[0], bounds[1] - values])),
                             fmt="o", capsize=4, label=label)
        axes[1].set(xticks=x, xticklabels=[r["age_group"] for r in ages], ylim=(0, 1),
                    title="Age audit: frozen threshold, development OOF", ylabel="Rate (95% Wilson intervals)")
        axes[1].legend()
        fig.savefig(path, dpi=160, facecolor=fig.get_facecolor())
        plt.close(fig)


def write_model_card(result, evidence_dir, path):
    """Reuse already recorded final metrics; never load/predict final records."""
    final = json.loads((Path(evidence_dir) / evaluation.RESULT_FILE).read_text(encoding="utf-8"))
    if final["recipe_sha256"] != result["signature"]["recipe_sha256"]:
        raise ValueError("Model card and final evaluation must describe the same recipe.")
    m = final["final"]
    paragraphs = [
        ("کاربرد", "برآورد ریسک دیرکرد جدی/نکول در دو سال آینده برای اولویت‌بندی بررسی اعتباری؛ کلاس ۱ یعنی نکول. خروجی، تضمین رخداد و جایگزین بررسی پرونده نیست."),
        ("داده", "داده ناشناس Give Me Some Credit از جدول credit.accepted: تعداد ۱۴۹٬۹۹۹ رکورد پذیرفته‌شده؛ ۱۲۰٬۰۰۰ توسعه و ۲۹٬۹۹۹ ارزیابی نهایی ثابت. شناسه، هویت شخص را تضمین نمی‌کند. زمان گردآوری، جمعیت مرجع و واحد پول در فایل موجود روشن نیستند."),
        ("مدل و تصمیم", f"HistGradientBoosting با ۱۰۰ تکرار و حداکثر ۳۱ برگ؛ میانه‌گذاری آموزشی، ۵ نشانگر گمشده و ۳ ویژگی مشتق‌شده، در مجموع ۱۸ ورودی مدل. آستانه ثابت {result['selection']['threshold']!r} با هزینه FN=12 و FP=1 روی توسعه انتخاب شده است. کدهای ۹۶/۹۸ فقط در Pipeline به گمشده تبدیل می‌شوند."),
        ("ارزیابی ثبت‌شده", f"ارزیابی نهایی بخش هفت: AUC={m['roc_auc']:.4f}، Recall={100*m['recall']:.2f}٪، Precision={100*m['precision']:.2f}٪ و هزینه {m['cost_per_1000']:.3f} در هزار؛ کاهش هزینه نسبت به همه‌سالم {final['reduction_vs_final_baseline_percent']:.2f}٪. این نتایج یک ارزیابی ثابت‌اند، نه تضمین عملکرد آینده یا کالیبراسیون احتمال."),
        ("محدودیت و انصاف", "شوک آینده درآمد/اشتغال، متغیرهای ثبت‌نشده و رابطه علّی قابل استنتاج نیستند. اهمیت ویژگی و تفاوت سنی با OOF توسعه بررسی شده‌اند؛ این داده در انتخاب مدل نیز استفاده شده و نتیجه، تأیید مستقل انصاف نیست. گروه‌های کم‌نمونه عدم‌قطعیت بیشتری دارند."),
        ("موارد نامناسب", f"برای جمعیت، دوره، واحد پول یا قرارداد داده متفاوت، پیش از اعتبارسنجی مستقل استفاده نشود؛ از جمله تعمیم مستقیم به مشتریان بانک‌های ایران. سن خارج از دامنه توسعه ({result['age_range'][0]} تا {result['age_range'][1]})، تغییر معنای متغیرها و تصمیم رد خودکار بدون بررسی تکمیلی پوشش معتبر ندارند."),
    ]
    body = '\n'.join(f'<p dir="rtl"><strong>{escape(title)}:</strong> {escape(text)}</p>' for title, text in paragraphs)
    html = '<div dir="rtl" style="direction:rtl;text-align:right;font-family:Tahoma,Arial,sans-serif;line-height:1.9;">\n<h1>شناسنامه مدل اعتباری</h1>\n' + body + '\n</div>\n'
    Path(path).write_text(html, encoding="utf-8")
    return html
