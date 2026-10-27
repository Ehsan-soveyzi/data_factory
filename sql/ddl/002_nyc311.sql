-- Initial PostgreSQL structure for the three NYC 311 tables.
-- Matches config/nyc_schema.json and the saved stage 01 delivery.
-- Execute this file as one transaction on an initialized PostgreSQL database.
-- This script defines objects only; it does not load or download data.
-- NYC objects are independent of the olist schema in 001_olist.sql.

CREATE SCHEMA IF NOT EXISTS nyc311;

-- Source: population_join/<run>/population_areas.jsonl.
-- matched_case_count is a snapshot diagnostic and is not a reference attribute.
-- The reference date is preserved; these are not current population estimates.
CREATE TABLE nyc311.population_areas (
    population_area           CHAR(5) PRIMARY KEY,
    population_geography      TEXT NOT NULL,
    population_reference_date DATE NOT NULL,
    reference_population      NUMERIC(14,2) NOT NULL,
    reference_borough         TEXT NOT NULL,
    reference_neighborhood    TEXT,

    CONSTRAINT population_areas_key_check
        CHECK (population_area ~ '^[0-9]{5}$'),
    CONSTRAINT population_areas_geography_check
        CHECK (btrim(population_geography) <> ''),
    CONSTRAINT population_areas_population_check
        CHECK (reference_population > 0 AND reference_population <> 'NaN'::numeric),
    CONSTRAINT population_areas_borough_check
        CHECK (btrim(reference_borough) <> '')
);

-- Source: explode the zip_codes array of each saved population reference row.
-- Several ZIP aliases can map to one MODZCTA population area.
CREATE TABLE nyc311.population_zip_aliases (
    incident_zip    CHAR(5) PRIMARY KEY,
    population_area CHAR(5) NOT NULL,

    CONSTRAINT population_zip_aliases_zip_check
        CHECK (incident_zip ~ '^[0-9]{5}$'),
    CONSTRAINT population_zip_aliases_area_fk
        FOREIGN KEY (population_area)
        REFERENCES nyc311.population_areas (population_area)
);

-- Source: delivery/<run>/analytical_cases.jsonl, selected through latest.json.
-- The five repeated reference attributes are stored in population_areas.
-- Identifiers remain text, and source timestamps have no invented timezone.
CREATE TABLE nyc311.cases (
    agency                         TEXT NOT NULL,
    agency_name                    TEXT,
    analysis_area_key              TEXT NOT NULL,
    borough                        TEXT,
    borough_comparison             TEXT NOT NULL,
    city                           TEXT,
    closed_date                    TIMESTAMP WITHOUT TIME ZONE,
    closure_days                   DOUBLE PRECISION,
    closure_hours                  DOUBLE PRECISION,
    complaint_type                 TEXT NOT NULL,
    created_date                   TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    created_month                  CHAR(7) NOT NULL,
    descriptor                     TEXT,
    has_recorded_closure           BOOLEAN NOT NULL,
    incident_zip                   CHAR(5),
    latitude                       DOUBLE PRECISION,
    location_type                  TEXT,
    longitude                      DOUBLE PRECISION,
    open_data_channel_type         TEXT,
    population_area                CHAR(5),
    population_join_status         TEXT NOT NULL,
    population_rate_eligible       BOOLEAN NOT NULL,
    quality_flags                  JSONB NOT NULL DEFAULT '[]'::jsonb,
    resolution_action_updated_date TIMESTAMP WITHOUT TIME ZONE,
    resolution_description         TEXT,
    status                         TEXT,
    unique_key                     TEXT PRIMARY KEY,

    CONSTRAINT cases_unique_key_check
        CHECK (unique_key ~ '^[0-9]+$'),
    CONSTRAINT cases_agency_check
        CHECK (btrim(agency) <> ''),
    CONSTRAINT cases_analysis_area_key_check
        CHECK (btrim(analysis_area_key) <> ''),
    CONSTRAINT cases_complaint_type_check
        CHECK (btrim(complaint_type) <> ''),
    CONSTRAINT cases_created_month_check
        CHECK (created_month = to_char(created_date, 'YYYY-MM')),
    CONSTRAINT cases_incident_zip_check
        CHECK (incident_zip ~ '^[0-9]{5}$'),
    CONSTRAINT cases_latitude_check
        CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT cases_longitude_check
        CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT cases_population_area_fk
        FOREIGN KEY (population_area)
        REFERENCES nyc311.population_areas (population_area),
    CONSTRAINT cases_population_join_status_check
        CHECK (population_join_status IN ('matched', 'missing_zip', 'unmatched_zip')),
    CONSTRAINT cases_population_join_consistency_check
        CHECK (
            (population_join_status = 'matched'
                AND population_area IS NOT NULL AND incident_zip IS NOT NULL)
            OR (population_join_status = 'missing_zip'
                AND population_area IS NULL AND incident_zip IS NULL)
            OR (population_join_status = 'unmatched_zip'
                AND population_area IS NULL AND incident_zip IS NOT NULL)
        ),
    CONSTRAINT cases_borough_comparison_check
        CHECK (borough_comparison IN ('match', 'mismatch', 'unavailable')),
    CONSTRAINT cases_population_rate_eligible_check
        CHECK (population_rate_eligible =
               (population_area IS NOT NULL AND borough_comparison <> 'mismatch')),
    CONSTRAINT cases_closure_flag_check
        CHECK (has_recorded_closure = (closed_date IS NOT NULL)),
    CONSTRAINT cases_closure_nulls_check
        CHECK ((closure_hours IS NULL) = (closed_date IS NULL)
           AND (closure_days IS NULL) = (closed_date IS NULL)),
    CONSTRAINT cases_closure_order_check
        CHECK (closed_date >= created_date),
    CONSTRAINT cases_closure_hours_check
        CHECK (closure_hours >= 0 AND closure_hours < 'Infinity'::double precision),
    CONSTRAINT cases_closure_days_check
        CHECK (closure_days >= 0 AND closure_days < 'Infinity'::double precision),
    CONSTRAINT cases_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

-- Unmatched/missing ZIP cases and borough mismatches are retained.
-- incident_zip therefore has no foreign key to population_zip_aliases.
-- A missing closure remains NULL, not zero, in both duration columns.
-- Only primary-key indexes are created in this baseline DDL.
