"""Idempotently upgrade an existing invoice_system database to the current schema."""

import os
from pathlib import Path

import pymysql
from dotenv import load_dotenv


VERSION = "20260714_current_schema"
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def column_exists(cursor, table_name, column_name):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (table_name, column_name),
    )
    return cursor.fetchone()[0] > 0


def index_exists(cursor, table_name, index_name):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
        """,
        (table_name, index_name),
    )
    return cursor.fetchone()[0] > 0


def constraint_exists(cursor, table_name, constraint_name):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = %s
        """,
        (table_name, constraint_name),
    )
    return cursor.fetchone()[0] > 0


def main():
    connection = pymysql.connect(
        host=os.getenv("DB_HOST") or os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT") or os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("DB_USER") or os.getenv("MYSQL_USER", "root"),
        password=os.getenv("DB_PASSWORD") or os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("DB_NAME") or os.getenv("MYSQL_DB", "invoice_system"),
        charset="utf8mb4",
        autocommit=False,
    )

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(50) NOT NULL PRIMARY KEY,
                    applied_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (VERSION,))
            if cursor.fetchone():
                print(f"Database is already current: {VERSION}")
                connection.commit()
                return

            invoice_columns = {
                "approved_amount": "DECIMAL(12,2) NOT NULL DEFAULT 0.00",
                "finance_comment": "VARCHAR(255) NULL",
                "buyer_tax_no": "VARCHAR(50) NULL",
                "seller_tax_no": "VARCHAR(50) NULL",
            }
            for name, definition in invoice_columns.items():
                if not column_exists(cursor, "invoices", name):
                    cursor.execute(f"ALTER TABLE invoices ADD COLUMN `{name}` {definition}")

            cursor.execute("ALTER TABLE reimbursement_logs MODIFY comment TEXT NULL")

            if not index_exists(cursor, "reimbursement_logs", "idx_reimburse_item_id"):
                cursor.execute(
                    "ALTER TABLE reimbursement_logs ADD INDEX idx_reimburse_item_id (reimburse_item_id)"
                )

            foreign_keys = (
                (
                    "fk_reimbursement_logs_invoice",
                    "invoice_id",
                    "invoices",
                    "id",
                    " ON DELETE CASCADE",
                ),
                (
                    "fk_reimbursement_logs_user",
                    "user_id",
                    "users",
                    "id",
                    " ON DELETE CASCADE",
                ),
                (
                    "fk_reimbursement_logs_item",
                    "reimburse_item_id",
                    "reimburse_items",
                    "id",
                    "",
                ),
            )
            for name, column, target_table, target_column, delete_rule in foreign_keys:
                if not constraint_exists(cursor, "reimbursement_logs", name):
                    cursor.execute(
                        f"ALTER TABLE reimbursement_logs ADD CONSTRAINT `{name}` "
                        f"FOREIGN KEY (`{column}`) REFERENCES `{target_table}` (`{target_column}`)"
                        f"{delete_rule}"
                    )

            cursor.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                (VERSION,),
            )
        connection.commit()
        print(f"Database upgraded successfully: {VERSION}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()

