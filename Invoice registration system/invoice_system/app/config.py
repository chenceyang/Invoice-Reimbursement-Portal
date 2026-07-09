import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key")
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "Killerqueen99!")
    MYSQL_DB = os.getenv("MYSQL_DB", "employee")

    UPLOAD_FOLDER = str(STATIC_DIR / "uploads")
    EXPORT_FOLDER = str(STATIC_DIR / "exports")
    EXCEL_EXPORT_PATH = str(STATIC_DIR / "exports" / "invoices_export.xlsx")
