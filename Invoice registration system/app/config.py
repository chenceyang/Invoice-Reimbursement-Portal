import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-before-production")
    MYSQL_HOST = os.getenv("DB_HOST") or os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.getenv("DB_PORT") or os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER = os.getenv("DB_USER") or os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("DB_PASSWORD") or os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("DB_NAME") or os.getenv("MYSQL_DB", "invoice_system")

    UPLOAD_FOLDER = str(STATIC_DIR / "uploads")
    EXPORT_FOLDER = str(STATIC_DIR / "exports")
    EXCEL_EXPORT_PATH = str(STATIC_DIR / "exports" / "invoices_export.xlsx")
