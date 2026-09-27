from pathlib import Path

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from opengrid.schema_v1 import metadata

statements = ["-- OpenGrid Loss migration 0001. All DATETIME(6) values are UTC."]
for table in metadata.sorted_tables:
    statements.append(str(CreateTable(table).compile(dialect=mysql.dialect())) + ";")
    statements.extend(
        str(CreateIndex(index).compile(dialect=mysql.dialect())) + ";" for index in table.indexes
    )
Path("database/schema.sql").write_text("\n\n".join(statements) + "\n")
