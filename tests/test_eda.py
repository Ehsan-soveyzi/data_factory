"""Verify EDA populations, missing closures and complete-month denominators."""

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from data_factory.eda import summarize_cases


class EdaTests(unittest.TestCase):
    def summary(self, coverage):
        cases = pd.DataFrame({"closure_hours": [0., 2., 1000., None],
                              "complaint_type": ["Noise"] * 4,
                              "created_month": ["2024-01"] * 4,
                              "status": ["Closed", "Closed", "Closed", "Open"]})
        rows = []
        for complaint, count, rate in (("A", 3, 3000), ("B", 1, 1000)):
            for metric, value in (("case_count", count), ("complaints_per_population_scale", rate)):
                rows.append({"analysis_area_key": "MODZCTA:10001", "population_area": "10001",
                             "reference_population": 100, "reference_neighborhood": "Area",
                             "complaint_type": complaint, "metric": metric, "2024-01": value})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wide.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            return summarize_cases(cases, path, coverage)

    def test_zero_and_extreme_durations_are_retained_while_missing_is_excluded(self):
        summary = self.summary({"2024-01": "complete"})
        self.assertEqual(summary["duration_description"]["count"], 3)
        self.assertEqual(summary["duration_description"]["max"], 1000)
        self.assertEqual(summary["complaints"].loc["Noise", "median_hours"], 2)
        self.assertEqual(summary["outliers"]["missing_closure_records"], 1)
        self.assertEqual(summary["outliers"]["zero_duration_records"], 1)

    def test_area_population_is_not_summed_across_complaint_types(self):
        area = self.summary({"2024-01": "complete"})["area"].iloc[0]
        self.assertEqual(area["population"], 100)
        self.assertEqual(area["case_count_complete_months"], 4)
        self.assertEqual(area["rate_per_100k_complete_months"], 4000)

    def test_partial_months_are_excluded_from_population_comparison(self):
        self.assertTrue(self.summary({"2024-01": "partial_acquisition"})["area"].empty)
