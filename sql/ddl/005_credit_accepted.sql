-- Stage 03: the twelve data fields in the validated accepted.csv.
-- Preserve nullable income/dependents and accepted unusual numeric values.

CREATE SCHEMA IF NOT EXISTS credit;

CREATE TABLE IF NOT EXISTS credit.accepted (
    row_id                                     BIGINT PRIMARY KEY CHECK (row_id > 0),
    "SeriousDlqin2yrs"                          BIGINT NOT NULL CHECK ("SeriousDlqin2yrs" IN (0, 1)),
    "RevolvingUtilizationOfUnsecuredLines"      DOUBLE PRECISION NOT NULL CHECK ("RevolvingUtilizationOfUnsecuredLines" >= 0),
    age                                        BIGINT NOT NULL CHECK (age > 0),
    "NumberOfTime30-59DaysPastDueNotWorse"       BIGINT NOT NULL CHECK ("NumberOfTime30-59DaysPastDueNotWorse" >= 0),
    "DebtRatio"                                DOUBLE PRECISION NOT NULL CHECK ("DebtRatio" >= 0),
    "MonthlyIncome"                            DOUBLE PRECISION CHECK ("MonthlyIncome" >= 0),
    "NumberOfOpenCreditLinesAndLoans"           BIGINT NOT NULL CHECK ("NumberOfOpenCreditLinesAndLoans" >= 0),
    "NumberOfTimes90DaysLate"                   BIGINT NOT NULL CHECK ("NumberOfTimes90DaysLate" >= 0),
    "NumberRealEstateLoansOrLines"              BIGINT NOT NULL CHECK ("NumberRealEstateLoansOrLines" >= 0),
    "NumberOfTime60-89DaysPastDueNotWorse"       BIGINT NOT NULL CHECK ("NumberOfTime60-89DaysPastDueNotWorse" >= 0),
    "NumberOfDependents"                       BIGINT CHECK ("NumberOfDependents" >= 0)
);
