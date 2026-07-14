from flask import send_from_directory
import os
import threading
from dotenv import load_dotenv
from flask import Flask, session
from .db import close_db, get_db

load_dotenv()

def format_currency(v):
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return "0.00"

def status_label(status):
    labels = {
        "Pending": "待审核",
        "Approved": "待核销",
        "Rejected": "核销未通过",
        "Reimbursed": "已核销",
    }
    return labels.get(status, status or "")

def role_label(role):
    labels = {
        "admin": "管理员",
        "employee": "员工",
    }
    return labels.get(role, role or "")

def warm_ocr_async():
    """后台加载模型，使网站先启动；终端会明确提示预热是否完成。"""
    def warm():
        print("PaddleOCR 模型正在后台预热...", flush=True)
        try:
            from .ocr_service import _get_paddle_ocr
            model = _get_paddle_ocr()
            if model is not None:
                print("PaddleOCR 模型预热完成，可以上传发票。", flush=True)
            else:
                print("PaddleOCR 模型加载失败，将使用备用 OCR。", flush=True)
        except Exception as exc:
            print(f"PaddleOCR 模型预热失败：{exc}", flush=True)

    threading.Thread(target=warm, name="paddle-ocr-warmup", daemon=True).start()

def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-secret-change-before-production",
        DB_HOST=os.environ.get("DB_HOST") or os.environ.get("MYSQL_HOST", "127.0.0.1"),
        DB_PORT=int(os.environ.get("DB_PORT") or os.environ.get("MYSQL_PORT", "3306")),
        DB_USER=os.environ.get("DB_USER") or os.environ.get("MYSQL_USER", "root"),
        DB_PASSWORD=os.environ.get("DB_PASSWORD") or os.environ.get("MYSQL_PASSWORD", ""),
        DB_NAME=os.environ.get("DB_NAME") or os.environ.get("MYSQL_DB", "invoice_system"),
        UPLOAD_FOLDER=os.path.join(app.root_path, "static", "uploads"),
        EXPORT_FOLDER=os.path.join(app.root_path, "static", "exports"),
        EXCEL_EXPORT_PATH=os.path.join(app.root_path, "static", "exports", "invoice_export.xlsx"),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    )
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["EXPORT_FOLDER"], exist_ok=True)

    from .auth.routes import auth_bp
    from .invoice.routes import invoice_bp
    from .admin.routes import admin_bp
    from .templates.main.routes import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(invoice_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(main_bp)

    warm_ocr_async()

    app.teardown_appcontext(close_db)

    @app.context_processor
    def inject_globals():
        user = None
        if session.get("user_id"):
            user = {
                "id": session.get("user_id"),
                "employee_no": session.get("employee_no"),
                "name": session.get("user_name"),
                "role": session.get("role"),
            }
        return {
            "current_user": user,
            "format_currency": format_currency,
            "status_label": status_label,
            "role_label": role_label,
        }

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        upload_folder = app.config["UPLOAD_FOLDER"]
        return send_from_directory(upload_folder, filename)
    return app
