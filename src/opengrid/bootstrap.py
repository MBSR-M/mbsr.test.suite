import os

import pymysql
from sqlalchemy import text

from opengrid.db import session_factory
from opengrid.messaging import TOPICS, initialize_topics
from opengrid.config import settings
from opengrid.ui_auth import bootstrap_admin


def main():
    TOPICS["recompute"] = 6
    initialize_topics()
    with session_factory().begin() as s:
        bootstrap_admin(s, settings().ui_admin_username, settings().ui_admin_password)
        views = {
            "v_balance": "SELECT b.*, a.code transformer_code FROM transformer_energy_balance b JOIN asset a ON a.id=b.asset_id",
            "v_assets": "SELECT id, code, kind, active FROM asset",
            "v_cases": "SELECT c.*, a.code entity_code FROM investigation_case c JOIN asset a ON a.id=c.asset_id",
            "v_quality": "SELECT i.asset_id, i.start, i.status, a.code, a.kind FROM interval_energy i JOIN asset a ON a.id=i.asset_id",
            "v_anomaly": "SELECT n.*, a.code entity_code FROM anomaly n JOIN asset a ON a.id=n.asset_id",
            "v_system": "SELECT service, time, processed FROM worker_heartbeat",
            "v_meter_energy": "SELECT i.*, a.code FROM interval_energy i JOIN asset a ON a.id=i.asset_id WHERE a.kind='METER'",
        }
        for name, query in views.items():
            s.execute(text(f"CREATE OR REPLACE SQL SECURITY DEFINER VIEW {name} AS {query}"))
    password = os.environ["GRAFANA_DB_PASSWORD"]
    connection = pymysql.connect(
        host="mysql", user="root", password=os.environ["MYSQL_ROOT_PASSWORD"]
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("CREATE USER IF NOT EXISTS 'grafana'@'%%' IDENTIFIED BY %s", (password,))
            cursor.execute("ALTER USER 'grafana'@'%%' IDENTIFIED BY %s", (password,))
            for view in (
                "v_balance",
                "v_assets",
                "v_cases",
                "v_quality",
                "v_anomaly",
                "v_system",
                "v_meter_energy",
            ):
                cursor.execute(f"GRANT SELECT ON open_grid_loss.{view} TO 'grafana'@'%'")
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
