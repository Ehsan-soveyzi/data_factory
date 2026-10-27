-- Initial PostgreSQL structure for the nine Olist source tables.
-- Matches config/olist_schema.json and the curated cleaning output columns.
-- Execute this file as one transaction on an initialized PostgreSQL database.
-- This script defines objects only; it does not load data.
-- product_categories is the target of product_category_name_translation.csv,
-- including categories added during cleaning with a NULL English translation.
-- postal_areas is a separate derived dimension and is outside this script.

CREATE SCHEMA IF NOT EXISTS olist;

CREATE TABLE olist.product_categories (
    product_category_name         TEXT PRIMARY KEY,
    product_category_name_english TEXT,
    source_row                    INTEGER CHECK (source_row > 0),
    quality_flags                 JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT product_categories_name_check
        CHECK (btrim(product_category_name) <> ''),
    CONSTRAINT product_categories_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

CREATE TABLE olist.customers (
    customer_id              TEXT PRIMARY KEY,
    customer_unique_id       TEXT NOT NULL,
    customer_zip_code_prefix CHAR(5) NOT NULL,
    customer_city            TEXT NOT NULL,
    customer_state           CHAR(2) NOT NULL,
    source_row               INTEGER NOT NULL CHECK (source_row > 0),
    quality_flags            JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT customers_id_check
        CHECK (customer_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT customers_unique_id_format_check
        CHECK (customer_unique_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT customers_zip_check
        CHECK (customer_zip_code_prefix ~ '^[0-9]{5}$'),
    CONSTRAINT customers_city_check
        CHECK (btrim(customer_city) <> ''),
    CONSTRAINT customers_state_check
        CHECK (customer_state ~ '^[A-Z]{2}$'),
    CONSTRAINT customers_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

-- customer_unique_id identifies a person and can occur in several orders.
-- It deliberately has no UNIQUE constraint.

CREATE TABLE olist.sellers (
    seller_id              TEXT PRIMARY KEY,
    seller_zip_code_prefix CHAR(5) NOT NULL,
    seller_city            TEXT NOT NULL,
    seller_state           CHAR(2) NOT NULL,
    source_row             INTEGER NOT NULL CHECK (source_row > 0),
    quality_flags          JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT sellers_id_check
        CHECK (seller_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT sellers_zip_check
        CHECK (seller_zip_code_prefix ~ '^[0-9]{5}$'),
    CONSTRAINT sellers_city_check
        CHECK (btrim(seller_city) <> ''),
    CONSTRAINT sellers_state_check
        CHECK (seller_state ~ '^[A-Z]{2}$'),
    CONSTRAINT sellers_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

CREATE TABLE olist.products (
    product_id                 TEXT PRIMARY KEY,
    product_category_name      TEXT,
    product_name_length        INTEGER CHECK (product_name_length >= 0),
    product_description_length INTEGER CHECK (product_description_length >= 0),
    product_photos_qty         INTEGER CHECK (product_photos_qty >= 0),
    product_weight_g           INTEGER CHECK (product_weight_g > 0),
    product_length_cm          INTEGER CHECK (product_length_cm > 0),
    product_height_cm          INTEGER CHECK (product_height_cm > 0),
    product_width_cm           INTEGER CHECK (product_width_cm > 0),
    source_row                 INTEGER NOT NULL CHECK (source_row > 0),
    quality_flags              JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT products_id_check
        CHECK (product_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT products_category_fk
        FOREIGN KEY (product_category_name)
        REFERENCES olist.product_categories (product_category_name),
    CONSTRAINT products_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

-- Product length/weight values that were invalid or unknown remain NULL.
-- The curated names correct the source spelling "lenght" to "length".

CREATE TABLE olist.geolocation (
    geolocation_zip_code_prefix CHAR(5) NOT NULL,
    geolocation_lat             DOUBLE PRECISION NOT NULL,
    geolocation_lng             DOUBLE PRECISION NOT NULL,
    geolocation_city            TEXT NOT NULL,
    geolocation_state           CHAR(2) NOT NULL,
    source_row                  INTEGER NOT NULL CHECK (source_row > 0),
    quality_flags               JSONB NOT NULL DEFAULT '[]'::jsonb,
    geo_point_id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    CONSTRAINT geolocation_zip_check
        CHECK (geolocation_zip_code_prefix ~ '^[0-9]{5}$'),
    CONSTRAINT geolocation_lat_check
        CHECK (geolocation_lat BETWEEN -90 AND 90),
    CONSTRAINT geolocation_lng_check
        CHECK (geolocation_lng BETWEEN -180 AND 180),
    CONSTRAINT geolocation_city_check
        CHECK (btrim(geolocation_city) <> ''),
    CONSTRAINT geolocation_state_check
        CHECK (geolocation_state ~ '^[A-Z]{2}$'),
    CONSTRAINT geolocation_point_unique
        UNIQUE (geolocation_zip_code_prefix, geolocation_lat, geolocation_lng,
                geolocation_city, geolocation_state),
    CONSTRAINT geolocation_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

-- A ZIP prefix can have several distinct geolocation points. Customers and
-- sellers therefore have no foreign key to this nonunique prefix. Some of
-- their valid prefixes also have no geolocation point in the supplied data.
-- A future CSV load must omit geo_point_id from its explicit column list.

CREATE TABLE olist.orders (
    order_id                           TEXT PRIMARY KEY,
    customer_id                        TEXT NOT NULL,
    order_status                       TEXT NOT NULL,
    order_purchase_timestamp           TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    order_approved_at                  TIMESTAMP WITHOUT TIME ZONE,
    order_delivered_carrier_date       TIMESTAMP WITHOUT TIME ZONE,
    order_delivered_customer_date      TIMESTAMP WITHOUT TIME ZONE,
    order_estimated_delivery_date      DATE NOT NULL,
    source_row                         INTEGER NOT NULL CHECK (source_row > 0),
    purchase_to_approval_duration_valid BOOLEAN NOT NULL,
    approval_to_carrier_duration_valid  BOOLEAN NOT NULL,
    carrier_to_customer_duration_valid  BOOLEAN NOT NULL,
    purchase_to_customer_duration_valid BOOLEAN NOT NULL,
    delivery_delay_eligible             BOOLEAN NOT NULL,
    quality_flags                      JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT orders_id_check
        CHECK (order_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT orders_customer_fk
        FOREIGN KEY (customer_id) REFERENCES olist.customers (customer_id),
    CONSTRAINT orders_status_check
        CHECK (btrim(order_status) <> ''),
    CONSTRAINT orders_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

-- Source event reversals are retained with quality flags and duration-validity
-- columns; no chronological CHECK rejects those previously accepted orders.
-- Optional actual event timestamps remain NULL rather than being imputed.

CREATE TABLE olist.order_items (
    order_id            TEXT NOT NULL,
    order_item_id       INTEGER NOT NULL CHECK (order_item_id > 0),
    product_id          TEXT NOT NULL,
    seller_id           TEXT NOT NULL,
    shipping_limit_date TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    price               NUMERIC(12,2) NOT NULL,
    freight_value       NUMERIC(12,2) NOT NULL,
    source_row          INTEGER NOT NULL CHECK (source_row > 0),
    quality_flags       JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT order_items_pk PRIMARY KEY (order_id, order_item_id),
    CONSTRAINT order_items_order_fk
        FOREIGN KEY (order_id) REFERENCES olist.orders (order_id),
    CONSTRAINT order_items_product_fk
        FOREIGN KEY (product_id) REFERENCES olist.products (product_id),
    CONSTRAINT order_items_seller_fk
        FOREIGN KEY (seller_id) REFERENCES olist.sellers (seller_id),
    CONSTRAINT order_items_price_check
        CHECK (price >= 0 AND price <> 'NaN'::numeric),
    CONSTRAINT order_items_freight_check
        CHECK (freight_value >= 0 AND freight_value <> 'NaN'::numeric),
    CONSTRAINT order_items_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

CREATE TABLE olist.order_payments (
    order_id             TEXT NOT NULL,
    payment_sequential   INTEGER NOT NULL CHECK (payment_sequential > 0),
    payment_type         TEXT NOT NULL,
    payment_installments SMALLINT CHECK (payment_installments > 0),
    payment_value        NUMERIC(12,2) NOT NULL,
    source_row           INTEGER NOT NULL CHECK (source_row > 0),
    quality_flags        JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT order_payments_pk PRIMARY KEY (order_id, payment_sequential),
    CONSTRAINT order_payments_order_fk
        FOREIGN KEY (order_id) REFERENCES olist.orders (order_id),
    CONSTRAINT order_payments_type_check
        CHECK (btrim(payment_type) <> ''),
    CONSTRAINT order_payments_value_check
        CHECK (payment_value >= 0 AND payment_value <> 'NaN'::numeric),
    CONSTRAINT order_payments_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

-- Zero installments were normalized to NULL with a quality flag by cleaning.

CREATE TABLE olist.order_reviews (
    review_id               TEXT NOT NULL,
    order_id                TEXT NOT NULL,
    review_score            SMALLINT NOT NULL CHECK (review_score BETWEEN 1 AND 5),
    review_comment_title    TEXT,
    review_comment_message  TEXT,
    review_creation_date    TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    review_answer_timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    source_row              INTEGER NOT NULL CHECK (source_row > 0),
    quality_flags           JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT order_reviews_pk PRIMARY KEY (review_id, order_id),
    CONSTRAINT order_reviews_id_check
        CHECK (review_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT order_reviews_order_fk
        FOREIGN KEY (order_id) REFERENCES olist.orders (order_id),
    CONSTRAINT order_reviews_quality_flags_check
        CHECK (jsonb_typeof(quality_flags) = 'array')
);

-- Neither review_id alone nor order_id alone is unique in the supplied reviews.
-- Only primary-key/unique-constraint indexes are created in this baseline DDL.
