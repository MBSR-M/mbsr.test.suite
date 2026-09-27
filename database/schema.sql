-- OpenGrid Loss migration 0001. All DATETIME(6) values are UTC.


CREATE TABLE feeder (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	code VARCHAR(64) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	active BOOL NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
)

;


CREATE TABLE inbox (
	consumer VARCHAR(32) NOT NULL, 
	event_id VARCHAR(36) NOT NULL, 
	digest VARCHAR(64) NOT NULL, 
	processed_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (consumer, event_id)
)

;


CREATE TABLE outbox (
	id VARCHAR(36) NOT NULL, 
	owner VARCHAR(32) NOT NULL, 
	topic VARCHAR(100) NOT NULL, 
	`key` VARCHAR(64) NOT NULL, 
	payload JSON NOT NULL, 
	sent BOOL NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE INDEX ix_outbox_sent ON outbox (sent);

CREATE INDEX ix_outbox_owner ON outbox (owner);


CREATE TABLE processing_job (
	id VARCHAR(36) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	payload JSON NOT NULL, 
	error TEXT, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE quarantine (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	service VARCHAR(32) NOT NULL, 
	reason TEXT NOT NULL, 
	payload JSON NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE worker_heartbeat (
	service VARCHAR(32) NOT NULL, 
	time DATETIME(6) NOT NULL, 
	processed INTEGER NOT NULL, 
	PRIMARY KEY (service)
)

;


CREATE TABLE asset (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	code VARCHAR(64) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	feeder_id BIGINT, 
	active BOOL NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code), 
	FOREIGN KEY(feeder_id) REFERENCES feeder (id)
)

;


CREATE TABLE anomaly (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	asset_id BIGINT NOT NULL, 
	start DATETIME(6) NOT NULL, 
	anomaly_type VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	severity VARCHAR(16) NOT NULL, 
	score NUMERIC(20, 6) NOT NULL, 
	evidence JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (asset_id, start, anomaly_type), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_anomaly_asset_id ON anomaly (asset_id);

CREATE INDEX ix_anomaly_start ON anomaly (start);


CREATE TABLE assignment (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	meter_id BIGINT NOT NULL, 
	transformer_id BIGINT NOT NULL, 
	valid_from DATETIME(6) NOT NULL, 
	valid_to DATETIME(6), 
	PRIMARY KEY (id), 
	FOREIGN KEY(meter_id) REFERENCES asset (id), 
	FOREIGN KEY(transformer_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_assignment_meter_id ON assignment (meter_id);

CREATE INDEX ix_assignment_transformer_id ON assignment (transformer_id);


CREATE TABLE evaluation (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	asset_id BIGINT NOT NULL, 
	start DATETIME(6) NOT NULL, 
	eligible BOOL NOT NULL, 
	abnormal BOOL NOT NULL, 
	evidence JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (asset_id, start), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_evaluation_start ON evaluation (start);

CREATE INDEX ix_evaluation_asset_id ON evaluation (asset_id);


CREATE TABLE interval_energy (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	asset_id BIGINT NOT NULL, 
	start DATETIME(6) NOT NULL, 
	import_kwh NUMERIC(20, 6), 
	export_kwh NUMERIC(20, 6), 
	status VARCHAR(32) NOT NULL, 
	revision INTEGER NOT NULL, 
	evidence JSON NOT NULL, 
	processed_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (asset_id, start), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_interval_energy_start ON interval_energy (start);

CREATE INDEX ix_interval_energy_asset_id ON interval_energy (asset_id);


CREATE TABLE measurement_configuration (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	asset_id BIGINT NOT NULL, 
	valid_from DATETIME(6) NOT NULL, 
	valid_to DATETIME(6), 
	multiplier NUMERIC(20, 6) NOT NULL, 
	import_only BOOL NOT NULL, 
	modulus NUMERIC(20, 6), 
	max_power_kw NUMERIC(20, 6), 
	PRIMARY KEY (id), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_measurement_configuration_asset_id ON measurement_configuration (asset_id);


CREATE TABLE meter_event (
	id VARCHAR(36) NOT NULL, 
	asset_id BIGINT NOT NULL, 
	time DATETIME(6) NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	evidence JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_meter_event_asset_id ON meter_event (asset_id);

CREATE INDEX ix_meter_event_time ON meter_event (time);


CREATE TABLE projection_revision (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	kind VARCHAR(32) NOT NULL, 
	asset_id BIGINT NOT NULL, 
	start DATETIME(6) NOT NULL, 
	body JSON NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;


CREATE TABLE reading (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	event_id VARCHAR(36) NOT NULL, 
	asset_id BIGINT NOT NULL, 
	time DATETIME(6) NOT NULL, 
	source VARCHAR(32) NOT NULL, 
	revision INTEGER NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	epoch VARCHAR(64) NOT NULL, 
	`primary` BOOL NOT NULL, 
	import_value NUMERIC(20, 6) NOT NULL, 
	export_value NUMERIC(20, 6), 
	received_at DATETIME(6) NOT NULL, 
	ingested_at DATETIME(6) NOT NULL, 
	payload JSON NOT NULL, 
	digest VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (asset_id, time, source, revision), 
	UNIQUE (event_id), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_reading_time ON reading (time);

CREATE INDEX ix_reading_asset_id ON reading (asset_id);


CREATE TABLE transformer_aggregate (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	asset_id BIGINT NOT NULL, 
	start DATETIME(6) NOT NULL, 
	expected INTEGER NOT NULL, 
	received INTEGER NOT NULL, 
	valid INTEGER NOT NULL, 
	downstream_kwh NUMERIC(20, 6) NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	evidence JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (asset_id, start), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_transformer_aggregate_start ON transformer_aggregate (start);

CREATE INDEX ix_transformer_aggregate_asset_id ON transformer_aggregate (asset_id);


CREATE TABLE transformer_energy_balance (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	asset_id BIGINT NOT NULL, 
	start DATETIME(6) NOT NULL, 
	input_kwh NUMERIC(20, 6), 
	downstream_kwh NUMERIC(20, 6) NOT NULL, 
	accounting_difference_kwh NUMERIC(20, 6), 
	accounting_difference_percent NUMERIC(20, 6), 
	completeness NUMERIC(20, 6), 
	status VARCHAR(32) NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	evidence JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (asset_id, start), 
	FOREIGN KEY(asset_id) REFERENCES asset (id)
)

;

CREATE INDEX ix_transformer_energy_balance_asset_id ON transformer_energy_balance (asset_id);

CREATE INDEX ix_transformer_energy_balance_start ON transformer_energy_balance (start);


CREATE TABLE investigation_case (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	case_no VARCHAR(40) NOT NULL, 
	asset_id BIGINT NOT NULL, 
	anomaly_id BIGINT NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	priority NUMERIC(20, 6) NOT NULL, 
	assigned_to VARCHAR(128), 
	resolution VARCHAR(500), 
	version INTEGER NOT NULL, 
	evidence JSON NOT NULL, 
	opened_at DATETIME(6) NOT NULL, 
	closed_at DATETIME(6), 
	PRIMARY KEY (id), 
	UNIQUE (case_no), 
	FOREIGN KEY(asset_id) REFERENCES asset (id), 
	UNIQUE (anomaly_id), 
	FOREIGN KEY(anomaly_id) REFERENCES anomaly (id)
)

;

CREATE INDEX ix_investigation_case_asset_id ON investigation_case (asset_id);

CREATE INDEX ix_investigation_case_status ON investigation_case (status);


CREATE TABLE case_history (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	case_id BIGINT NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	body JSON NOT NULL, 
	time DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES investigation_case (id)
)

;

CREATE INDEX ix_case_history_case_id ON case_history (case_id);
