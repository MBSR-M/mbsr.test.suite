import json
import os
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_provisioned_dashboards_and_mysql_queries():
    with httpx.Client(
        base_url="http://grafana:3000", auth=("admin", os.environ["GRAFANA_PASSWORD"]), timeout=30
    ) as client:
        health = client.get("/api/datasources/uid/opengrid-mysql/health")
        assert health.status_code == 200, health.text
        search = client.get("/api/search", params={"tag": "opengrid"})
        assert len(search.json()) == 7
        for path in Path("grafana/dashboards").glob("*.json"):
            dashboard = json.loads(path.read_text())
            assert client.get(f"/api/dashboards/uid/{dashboard['uid']}").status_code == 200
            for panel in dashboard["panels"]:
                target = panel["targets"][0]
                query = (
                    target["rawSql"]
                    .replace("${transformer:sqlstring}", "'DT-1047'")
                    .replace("${meter:sqlstring}", "'M-0000-0000'")
                )
                response = client.post(
                    "/api/ds/query",
                    json={
                        "from": "1767225600000",
                        "to": "1798761600000",
                        "queries": [
                            {
                                "refId": "A",
                                "datasource": {"uid": "opengrid-mysql", "type": "mysql"},
                                "rawSql": query,
                                "format": target["format"],
                                "intervalMs": 900000,
                                "maxDataPoints": 1000,
                            }
                        ],
                    },
                )
                assert response.status_code == 200, f"{path}: {response.text}"
                assert not response.json()["results"]["A"].get("error"), response.text
