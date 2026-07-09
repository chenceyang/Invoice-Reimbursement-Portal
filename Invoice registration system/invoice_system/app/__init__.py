from flask import send_from_directory
import os
from flask import Flask, session
from .db import close_db, get_db

def format_currency(v):
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return "0.00"

def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret"),
        DB_HOST=os.environ.get("DB_HOST", "127.0.0.1"),
        DB_PORT=int(os.environ.get("DB_PORT", "3306")),
        DB_USER=os.environ.get("DB_USER", "root"),
        DB_PASSWORD=os.environ.get("DB_PASSWORD", "Killerqueen99!"),
        DB_NAME=os.environ.get("DB_NAME", "invoice_system"),
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
        return {"current_user": user, "format_currency": format_currency}

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        upload_folder = app.config["UPLOAD_FOLDER"]
        return send_from_directory(upload_folder, filename)
    return app
