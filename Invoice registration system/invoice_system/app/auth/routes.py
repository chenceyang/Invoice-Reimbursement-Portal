from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import check_password_hash
from ..db import get_db

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        employee_no = request.form.get("employee_no", "").strip()
        password = request.form.get("password", "").strip()
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE employee_no = %s", (employee_no,))
            user = cur.fetchone()
        if not user or not check_password_hash(user["password"], password):

            flash("工号或密码错误。")
            return render_template("auth/login.html")
        session.clear()
        session["user_id"] = user["id"]
        session["employee_no"] = user["employee_no"]
        session["user_name"] = user["name"]
        session["role"] = user["role"]
        flash(f"欢迎，{user['name']}！")
        return redirect(url_for("main.home"))
    return render_template("auth/login.html")

@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("您已退出登录。")
    return redirect(url_for("auth.login"))
