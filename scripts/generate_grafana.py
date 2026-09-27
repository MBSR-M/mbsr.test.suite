"""Generate reproducible Grafana OSS dashboards using only the standard library."""

import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "grafana"
(root / "dashboards").mkdir(parents=True, exist_ok=True)
(root / "provisioning" / "dashboards").mkdir(parents=True, exist_ok=True)
(root / "provisioning" / "datasources").mkdir(parents=True, exist_ok=True)
(root / "provisioning" / "datasources" / "mysql.yaml").write_text("""apiVersion: 1
datasources:
  - name: OpenGrid MySQL
    uid: opengrid-mysql
    type: mysql
    url: mysql:3306
    user: grafana
    jsonData:
      database: open_grid_loss
      maxOpenConns: 5
      timezone: '+00:00'
    secureJsonData:
      password: $GRAFANA_DB_PASSWORD
""")
(root / "provisioning" / "dashboards" / "default.yaml").write_text("""apiVersion: 1
providers:
  - name: OpenGrid
    folder: OpenGrid Loss
    type: file
    options:
      path: /var/lib/grafana/dashboards
""")

dashboards = {
    "overview": (
        "Executive Overview",
        [
            ("Assets", "SELECT kind, COUNT(*) count FROM v_assets GROUP BY kind", "table"),
            (
                "Eligible energy accounting",
                "SELECT SUM(input_kwh) input_kwh, SUM(downstream_kwh) downstream_kwh, SUM(accounting_difference_kwh) difference_kwh, 100*SUM(accounting_difference_kwh)/NULLIF(SUM(input_kwh),0) difference_percent FROM v_balance WHERE $__timeFilter(start) AND status='COMPLETE'",
                "table",
            ),
            (
                "Open investigations",
                "SELECT status, COUNT(*) cases FROM v_cases GROUP BY status",
                "table",
            ),
            (
                "Coverage",
                "SELECT status, COUNT(*) intervals FROM v_balance WHERE $__timeFilter(start) GROUP BY status",
                "table",
            ),
        ],
    ),
    "transformer-loss": (
        "Transformer Accounting",
        [
            (
                "Accounting results",
                "SELECT transformer_code, start, input_kwh, downstream_kwh, accounting_difference_kwh, accounting_difference_percent, completeness, status FROM v_balance WHERE $__timeFilter(start) ORDER BY start DESC LIMIT 1000",
                "table",
            ),
        ],
    ),
    "transformer-drilldown": (
        "Transformer Drilldown",
        [
            (
                "Input and downstream kWh",
                "SELECT start time, input_kwh, downstream_kwh FROM v_balance WHERE $__timeFilter(start) AND transformer_code=${transformer:sqlstring} ORDER BY start",
                "timeseries",
            ),
            (
                "Accounting difference and completeness",
                "SELECT start time, accounting_difference_percent, completeness FROM v_balance WHERE $__timeFilter(start) AND transformer_code=${transformer:sqlstring} ORDER BY start",
                "timeseries",
            ),
            (
                "Evidence",
                "SELECT start, severity, score, evidence FROM v_anomaly WHERE entity_code=${transformer:sqlstring} AND $__timeFilter(start) ORDER BY start DESC LIMIT 100",
                "table",
            ),
        ],
    ),
    "meter-health": (
        "Meter Health",
        [
            (
                "Interval energy",
                "SELECT start time, import_kwh, export_kwh FROM v_meter_energy WHERE code=${meter:sqlstring} AND $__timeFilter(start) ORDER BY start",
                "timeseries",
            ),
            (
                "Quality",
                "SELECT start, status FROM v_quality WHERE code=${meter:sqlstring} AND $__timeFilter(start) ORDER BY start DESC LIMIT 1000",
                "table",
            ),
        ],
    ),
    "data-quality": (
        "Data Quality",
        [
            (
                "Interval statuses",
                "SELECT kind, status, COUNT(*) intervals FROM v_quality WHERE $__timeFilter(start) GROUP BY kind,status",
                "table",
            ),
        ],
    ),
    "investigations": (
        "Investigation Queue",
        [
            (
                "Cases",
                "SELECT id, case_no, entity_code, priority, status, assigned_to, opened_at FROM v_cases ORDER BY priority DESC LIMIT 1000",
                "table",
            ),
        ],
    ),
    "system-health": (
        "System Health",
        [
            (
                "Worker heartbeats",
                "SELECT service, time, TIMESTAMPDIFF(SECOND,time,UTC_TIMESTAMP()) age_seconds, processed FROM v_system",
                "table",
            ),
        ],
    ),
}
for uid, (title, definitions) in dashboards.items():
    panels = []
    for index, (name, query, kind) in enumerate(definitions):
        panel = {
            "id": index + 1,
            "title": name,
            "type": kind,
            "datasource": {"type": "mysql", "uid": "opengrid-mysql"},
            "gridPos": {"x": 0, "y": index * 9, "w": 24, "h": 9},
            "targets": [
                {
                    "refId": "A",
                    "rawSql": query,
                    "format": "time_series" if kind == "timeseries" else "table",
                }
            ],
            "fieldConfig": {"defaults": {}, "overrides": []},
        }
        if uid == "investigations":
            panel["fieldConfig"]["overrides"] = [
                {
                    "matcher": {"id": "byName", "options": "id"},
                    "properties": [
                        {
                            "id": "links",
                            "value": [
                                {
                                    "title": "Open investigation",
                                    "url": "http://localhost:8000/investigations/${__value.raw}",
                                    "targetBlank": True,
                                }
                            ],
                        }
                    ],
                }
            ]
        if uid == "transformer-loss":
            panel["fieldConfig"]["overrides"] = [
                {
                    "matcher": {"id": "byName", "options": "transformer_code"},
                    "properties": [
                        {
                            "id": "links",
                            "value": [
                                {
                                    "title": "Transformer detail",
                                    "url": "/d/opengrid-transformer-drilldown?var-transformer=${__value.raw}",
                                }
                            ],
                        }
                    ],
                }
            ]
        panels.append(panel)
    variables = []
    for variable, asset_kind in (("transformer", "TRANSFORMER"), ("meter", "METER")):
        variables.append(
            {
                "name": variable,
                "type": "query",
                "datasource": {"type": "mysql", "uid": "opengrid-mysql"},
                "query": f"SELECT code FROM v_assets WHERE kind='{asset_kind}' ORDER BY code",
                "refresh": 1,
            }
        )
    body = {
        "uid": "opengrid-" + uid,
        "title": title,
        "schemaVersion": 39,
        "version": 1,
        "timezone": "utc",
        "refresh": "30s",
        "time": {"from": "now-2d", "to": "now"},
        "panels": panels,
        "templating": {"list": variables},
        "links": [{"type": "dashboards", "title": "OpenGrid dashboards", "tags": ["opengrid"]}],
        "tags": ["opengrid"],
    }
    (root / "dashboards" / f"{uid}.json").write_text(json.dumps(body, indent=2) + "\n")
