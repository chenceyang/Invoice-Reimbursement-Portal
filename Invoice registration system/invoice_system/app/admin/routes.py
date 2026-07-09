import os
from datetime import datetime
from flask import Blueprint, flash, redirect, render_template, request, url_for, current_app, abort, send_from_directory
from ..db import get_db
from ..utils import login_required, admin_required
from ..excel_service import export_invoice_rows_to_excel, export_entry_rows_to_excel, export_reimbursement_logs_to_excel

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

PAGE_SIZE = 10


def ensure_admin_config_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reimburse_items (
            id INT AUTO_INCREMENT PRIMARY KEY,
            item_name VARCHAR(100) NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reimbursement_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            invoice_id INT NOT NULL,
            user_id INT NOT NULL,
            reimburse_item_id INT NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            comment VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_invoice_id (invoice_id),
            INDEX idx_user_id (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        ALTER TABLE reimbursement_logs
        MODIFY comment TEXT
    """)
    cur.execute("SELECT COUNT(*) AS cnt FROM reimburse_items")
    if (cur.fetchone()["cnt"] or 0) == 0:
        cur.executemany(
            "INSERT INTO reimburse_items (item_name, is_active) VALUES (%s, 1)",
            [("日常报销",), ("差旅报销",), ("项目报销",)]
        )
    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'invoices'
          AND COLUMN_NAME = 'approved_amount'
    """)
    if (cur.fetchone()["cnt"] or 0) == 0:
        cur.execute("""
            ALTER TABLE invoices
            ADD COLUMN approved_amount DECIMAL(12,2) NOT NULL DEFAULT 0
        """)
    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'invoices'
          AND COLUMN_NAME = 'finance_comment'
    """)
    if (cur.fetchone()["cnt"] or 0) == 0:
        cur.execute("""
            ALTER TABLE invoices
            ADD COLUMN finance_comment VARCHAR(255)
        """)


def parse_money(value, default=0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return None


def approve_invoice(cur, inv, approved_amount, finance_comment):
    invoice_amount = float(inv["amount"] or 0)
    approved_amount = parse_money(approved_amount, invoice_amount)
    if approved_amount is None:
        return False, "分配额度格式不正确。"
    if approved_amount < 0:
        return False, "分配额度不能小于 0。"
    if approved_amount > invoice_amount:
        return False, "分配额度不能超过发票金额。"

    previous_approved = float(inv.get("approved_amount") or 0)
    delta = approved_amount - previous_approved
    if abs(delta) > 0.00001:
        cur.execute("""
            UPDATE users
            SET total_quota = GREATEST(total_quota + %s, 0)
            WHERE id = %s
        """, (delta, inv["user_id"]))

    cur.execute("""
        UPDATE invoices
        SET status='Approved', approved_amount=%s, finance_comment=%s
        WHERE id=%s
    """, (approved_amount, finance_comment, inv["id"]))
    return True, ""


def reject_invoice(cur, inv, finance_comment):
    if inv["status"] == "Approved":
        previous_approved = float(inv.get("approved_amount") or 0)
        if previous_approved > 0:
            cur.execute("""
                UPDATE users
                SET total_quota = GREATEST(total_quota - %s, 0)
                WHERE id = %s
            """, (previous_approved, inv["user_id"]))

    cur.execute("""
        UPDATE invoices
        SET status='Rejected', approved_amount=0, finance_comment=%s
        WHERE id=%s
    """, (finance_comment, inv["id"]))
    return True, ""


def status_label(status):
    labels = {
        "Pending": "待审核",
        "Approved": "待核销",
        "Rejected": "核销未通过",
        "Reimbursed": "已核销",
    }
    return labels.get(status, status or "")


def sync_user_quota(cur, user_id):
    cur.execute("""
        SELECT COALESCE(SUM(
            CASE
                WHEN approved_amount IS NOT NULL AND approved_amount > 0 THEN approved_amount
                ELSE amount
            END
        ), 0) AS total_quota
        FROM invoices
        WHERE user_id=%s AND status IN ('Approved', 'Reimbursed')
    """, (user_id,))
    total_quota = float(cur.fetchone()["total_quota"] or 0)
    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS used_quota
        FROM reimbursement_logs
        WHERE user_id=%s
    """, (user_id,))
    used_quota = float(cur.fetchone()["used_quota"] or 0)
    cur.execute("""
        UPDATE users
        SET total_quota=%s, used_quota=%s
        WHERE id=%s
    """, (total_quota, used_quota, user_id))
    return total_quota, used_quota


def sync_all_user_quotas(cur):
    cur.execute("SELECT id FROM users")
    user_ids = [row["id"] for row in cur.fetchall()]
    for user_id in user_ids:
        sync_user_quota(cur, user_id)


def remove_uploaded_file(file_path: str):
    if not file_path:
        return
    try:
        filename = file_path.split("/")[-1]
        full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
        if os.path.exists(full_path):
            os.remove(full_path)
    except Exception as e:
        print("admin remove_uploaded_file error:", e)

@admin_bp.route("/reimburse", methods=["GET", "POST"])
@login_required
@admin_required
def reimburse():
    db = get_db()
    with db.cursor() as cur:
        ensure_admin_config_tables(cur)
        sync_all_user_quotas(cur)
        db.commit()

    if request.method == "POST":
        invoice_id = request.form.get("invoice_id")
        user_id = request.form.get("user_id")
        action = request.form.get("action")
        finance_comment = request.form.get("finance_comment", "").strip()
        reimburse_item_id = request.form.get("reimburse_item_id")
        reimburse_amount_raw = request.form.get("amount", "").strip()

        with db.cursor() as cur:
            if action == "reimburse":
                cur.execute("SELECT * FROM reimburse_items WHERE id=%s AND is_active=1", (reimburse_item_id,))
                reimburse_item = cur.fetchone()
                if not reimburse_item:
                    flash("请选择有效的核销事项。")
                    return redirect(url_for("admin.reimburse"))

                reimburse_amount = parse_money(reimburse_amount_raw)
                if reimburse_amount is None:
                    flash("核销金额格式不正确。")
                    return redirect(url_for("admin.reimburse"))
                if reimburse_amount <= 0:
                    flash("核销金额必须大于 0。")
                    return redirect(url_for("admin.reimburse"))

                sync_user_quota(cur, user_id)
                cur.execute("SELECT id, name, total_quota, used_quota FROM users WHERE id=%s", (user_id,))
                user = cur.fetchone()
                if not user:
                    flash("员工不存在。")
                    return redirect(url_for("admin.reimburse"))
                user_remaining_quota = float(user["total_quota"] or 0) - float(user["used_quota"] or 0)
                if reimburse_amount > user_remaining_quota:
                    flash("核销金额不能超过员工当前可核销金额。")
                    return redirect(url_for("admin.reimburse"))

                cur.execute("""
                    SELECT i.*,
                           COALESCE(rl.reimbursed_amount, 0) AS reimbursed_amount,
                           GREATEST(
                               CASE
                                   WHEN i.approved_amount IS NOT NULL AND i.approved_amount > 0 THEN i.approved_amount
                                   ELSE i.amount
                               END - COALESCE(rl.reimbursed_amount, 0),
                               0
                           ) AS remaining_amount
                    FROM invoices i
                    LEFT JOIN (
                        SELECT invoice_id, SUM(amount) AS reimbursed_amount
                        FROM reimbursement_logs
                        GROUP BY invoice_id
                    ) rl ON rl.invoice_id = i.id
                    WHERE i.user_id=%s AND i.status='Approved'
                    HAVING remaining_amount > 0
                    ORDER BY i.id ASC
                """, (user_id,))
                approved_invoices = cur.fetchall()
                available_amount = round(sum(float(row["remaining_amount"] or 0) for row in approved_invoices), 2)
                if reimburse_amount > available_amount:
                    flash("核销金额不能超过员工剩余可核销金额。")
                    return redirect(url_for("admin.reimburse"))

                amount_left = reimburse_amount
                for inv_row in approved_invoices:
                    if amount_left <= 0.00001:
                        break
                    remaining_amount = round(float(inv_row["remaining_amount"] or 0), 2)
                    current_amount = min(amount_left, remaining_amount)
                    cur.execute("""
                        INSERT INTO reimbursement_logs
                            (invoice_id, user_id, reimburse_item_id, amount, comment)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (inv_row["id"], user_id, reimburse_item_id, current_amount, finance_comment))

                    new_remaining = remaining_amount - current_amount
                    new_status = "Reimbursed" if new_remaining <= 0.00001 else "Approved"
                    cur.execute("""
                        UPDATE invoices
                        SET status=%s, finance_comment=%s
                        WHERE id=%s
                    """, (new_status, finance_comment, inv_row["id"]))
                    amount_left = round(amount_left - current_amount, 2)

                cur.execute("""
                    UPDATE users
                    SET used_quota = used_quota + %s
                    WHERE id = %s
                """, (reimburse_amount, user_id))
                db.commit()
                flash("员工核销成功。")
                return redirect(url_for("admin.reimburse"))

            cur.execute("""
                SELECT i.*, u.total_quota
                FROM invoices i
                JOIN users u ON i.user_id = u.id
                WHERE i.id = %s
            """, (invoice_id,))
            inv = cur.fetchone()
            if not inv:
                flash("发票不存在。")
                return redirect(url_for("admin.reimburse"))

            amount = inv["amount"]
            user_id = inv["user_id"]

            if action == "approve":
                ok, msg = approve_invoice(cur, inv, request.form.get("approved_amount", amount), finance_comment)
                if not ok:
                    flash(msg)
                    return redirect(url_for("admin.reimburse"))
            elif action == "reject":
                ok, msg = reject_invoice(cur, inv, finance_comment)
                if not ok:
                    flash(msg)
                    return redirect(url_for("admin.reimburse"))
            elif action == "reimburse":
                if inv["status"] == "Reimbursed":
                    flash("该发票已经核销过，不能重复核销。")
                    return redirect(url_for("admin.reimburse"))

                if inv["status"] != "Approved":
                    flash("只有审核通过的发票才可以核销。")
                    return redirect(url_for("admin.reimburse"))

                cur.execute("SELECT * FROM reimburse_items WHERE id=%s AND is_active=1", (reimburse_item_id,))
                reimburse_item = cur.fetchone()
                if not reimburse_item:
                    flash("请选择有效的核销事项。")
                    return redirect(url_for("admin.reimburse"))

                try:
                    reimburse_amount = round(float(reimburse_amount_raw), 2)
                except ValueError:
                    flash("核销金额格式不正确。")
                    return redirect(url_for("admin.reimburse"))

                if reimburse_amount <= 0:
                    flash("核销金额必须大于 0。")
                    return redirect(url_for("admin.reimburse"))

                cur.execute("""
                    SELECT COALESCE(SUM(amount), 0) AS reimbursed_amount
                    FROM reimbursement_logs
                    WHERE invoice_id = %s
                """, (invoice_id,))
                reimbursed_amount = float(cur.fetchone()["reimbursed_amount"] or 0)
                approved_amount = float(inv.get("approved_amount") or inv.get("amount") or 0)
                remaining_invoice_amount = approved_amount - reimbursed_amount
                if reimburse_amount > remaining_invoice_amount:
                    flash("核销金额不能超过该发票剩余已分配额度。")
                    return redirect(url_for("admin.reimburse"))

                if reimburse_amount > float(inv["total_quota"] or 0):
                    flash("核销金额不能超过员工当前可核销金额。")
                    return redirect(url_for("admin.reimburse"))

                cur.execute("""
                    INSERT INTO reimbursement_logs
                        (invoice_id, user_id, reimburse_item_id, amount, comment)
                    VALUES (%s, %s, %s, %s, %s)
                """, (invoice_id, user_id, reimburse_item_id, reimburse_amount, finance_comment))

                new_status = "Reimbursed" if remaining_invoice_amount - reimburse_amount <= 0.00001 else "Approved"
                cur.execute("""
                    UPDATE invoices
                    SET status=%s, finance_comment=%s
                    WHERE id=%s
                """, (new_status, finance_comment, invoice_id))

                cur.execute("""
                    UPDATE users
                    SET used_quota = used_quota + %s
                    WHERE id = %s
                """, (reimburse_amount, user_id))
            else:
                flash("未知操作。")
                return redirect(url_for("admin.reimburse"))

            db.commit()
        flash("操作成功。")
        return redirect(url_for("admin.reimburse"))

    keyword = request.args.get("keyword", "").strip()
    invoice_type_id = request.args.get("invoice_type_id", "").strip()
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    offset = (page - 1) * PAGE_SIZE

    with db.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(total_quota), 0) AS total_quota_sum,
                   COALESCE(SUM(used_quota), 0) AS used_quota_sum
            FROM users WHERE role='employee'
        """)
        quota_stats = cur.fetchone()
        total_quota_sum = float(quota_stats["total_quota_sum"] or 0)
        used_quota_sum = float(quota_stats["used_quota_sum"] or 0)
        remaining_quota_sum = max(total_quota_sum - used_quota_sum, 0)
        cur.execute("""
            SELECT
                SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'Approved' THEN 1 ELSE 0 END) AS approved_count,
                SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected_count,
                SUM(CASE WHEN status = 'Reimbursed' THEN 1 ELSE 0 END) AS reimbursed_count
            FROM invoices
        """)
        invoice_stats = cur.fetchone()
        cur.execute("SELECT id, item_name FROM reimburse_items WHERE is_active=1 ORDER BY item_name")
        reimburse_items = cur.fetchall()
        cur.execute("SELECT id, type_name FROM invoice_types WHERE is_active=1 ORDER BY type_name")
        invoice_types = cur.fetchall()

    base_sql = """
        FROM invoices i
        JOIN users u ON i.user_id = u.id
        LEFT JOIN (
            SELECT invoice_id, SUM(amount) AS reimbursed_amount
            FROM reimbursement_logs
            GROUP BY invoice_id
        ) rl ON rl.invoice_id = i.id
        WHERE i.status IN ('Pending', 'Approved', 'Reimbursed')
    """
    params = []
    if keyword:
        base_sql += " AND (u.name LIKE %s OR u.employee_no LIKE %s)"
        like_kw = f"%{keyword}%"
        params.extend([like_kw, like_kw])
    if invoice_type_id:
        base_sql += " AND i.invoice_type_id = %s"
        params.append(invoice_type_id)

    with db.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) AS total
            FROM (
                SELECT u.id
                {base_sql}
                GROUP BY u.id
            ) grouped_users
        """, params)
        total = cur.fetchone()["total"] or 0
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

        cur.execute(f"""
        SELECT u.id AS user_id,
               u.name AS employee_name,
               u.employee_no,
               u.total_quota,
               u.used_quota,
               GREATEST(u.total_quota - u.used_quota, 0) AS remaining_quota,
               SUM(i.amount) AS invoice_total_amount,
               SUM(CASE WHEN i.status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
               SUM(CASE WHEN i.status = 'Approved' THEN 1 ELSE 0 END) AS approved_count,
               SUM(CASE WHEN i.status = 'Reimbursed' THEN 1 ELSE 0 END) AS reimbursed_count,
               SUM(CASE
                   WHEN i.status = 'Approved' THEN GREATEST(
                       CASE
                           WHEN i.approved_amount IS NOT NULL AND i.approved_amount > 0 THEN i.approved_amount
                           ELSE i.amount
                       END - COALESCE(rl.reimbursed_amount, 0),
                       0
                   )
                   ELSE 0
               END) AS available_amount
        {base_sql}
        GROUP BY u.id, u.name, u.employee_no, u.total_quota, u.used_quota
        ORDER BY MAX(i.id) DESC
        LIMIT %s OFFSET %s
        """, params + [PAGE_SIZE, offset])
        items = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(SUM(
                CASE
                    WHEN i.status = 'Approved' THEN GREATEST(
                        CASE
                            WHEN i.approved_amount IS NOT NULL AND i.approved_amount > 0 THEN i.approved_amount
                            ELSE i.amount
                        END - COALESCE(rl.reimbursed_amount, 0),
                        0
                    )
                    ELSE 0
                END
            ), 0) AS total_amount
            {base_sql}
        """, params)
        total_amount = cur.fetchone()["total_amount"] or 0

    return render_template("admin/reimburse.html", items=items, keyword=keyword, invoice_type_id=invoice_type_id,
                           invoice_types=invoice_types, reimburse_items=reimburse_items,
                           total_amount=total_amount, page=page, total_pages=total_pages,
                           total_quota_sum=total_quota_sum, used_quota_sum=used_quota_sum,
                           remaining_quota_sum=remaining_quota_sum,
                           status_label=status_label,
                           pending_count=invoice_stats["pending_count"] or 0,
                           approved_count=invoice_stats["approved_count"] or 0,
                           rejected_count=invoice_stats["rejected_count"] or 0,
                           reimbursed_count=invoice_stats["reimbursed_count"] or 0)


@admin_bp.route("/settings/items", methods=["GET", "POST"])
@login_required
@admin_required
def item_settings():
    kind = request.args.get("kind", "invoice_type")
    if kind not in ("invoice_type", "reimburse_item"):
        kind = "invoice_type"
    return _item_settings(kind)


@admin_bp.route("/settings/invoice-types", methods=["GET", "POST"])
@login_required
@admin_required
def invoice_type_settings():
    return _item_settings("invoice_type")


@admin_bp.route("/settings/reimburse-items", methods=["GET", "POST"])
@login_required
@admin_required
def reimburse_item_settings():
    return _item_settings("reimburse_item")


def _item_settings(item_kind):
    db = get_db()
    is_invoice_type = item_kind == "invoice_type"
    title = "发票事项配置" if is_invoice_type else "核销事项配置"
    item_label = "发票事项" if is_invoice_type else "核销事项"
    list_title = "发票事项列表" if is_invoice_type else "核销事项列表"
    add_endpoint = "admin.invoice_type_settings" if is_invoice_type else "admin.reimburse_item_settings"
    with db.cursor() as cur:
        ensure_admin_config_tables(cur)
        if request.method == "POST":
            item_name = request.form.get("item_name", "").strip()
            if not item_name:
                flash("名称不能为空。")
                return redirect(url_for(add_endpoint))
            if item_kind == "invoice_type":
                cur.execute("INSERT INTO invoice_types (type_name, is_active) VALUES (%s, 1)", (item_name,))
            elif item_kind == "reimburse_item":
                cur.execute("INSERT INTO reimburse_items (item_name, is_active) VALUES (%s, 1)", (item_name,))
            else:
                flash("未知配置类型。")
                return redirect(url_for(add_endpoint))
            db.commit()
            flash("配置项已添加。")
            return redirect(url_for(add_endpoint))

        if is_invoice_type:
            cur.execute("SELECT id, type_name AS item_name, is_active FROM invoice_types ORDER BY is_active DESC, type_name")
        else:
            cur.execute("SELECT id, item_name, is_active FROM reimburse_items ORDER BY is_active DESC, item_name")
        items = cur.fetchall()

    return render_template(
        "admin/item_settings.html",
        title=title,
        item_kind=item_kind,
        item_label=item_label,
        list_title=list_title,
        items=items,
        is_invoice_type=is_invoice_type
    )


@admin_bp.route("/settings/items/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_item():
    db = get_db()
    item_kind = request.form.get("item_kind")
    item_id = request.form.get("item_id")
    with db.cursor() as cur:
        ensure_admin_config_tables(cur)
        if item_kind == "invoice_type":
            cur.execute("UPDATE invoice_types SET is_active = 1 - is_active WHERE id = %s", (item_id,))
            redirect_endpoint = "admin.invoice_type_settings"
        elif item_kind == "reimburse_item":
            cur.execute("UPDATE reimburse_items SET is_active = 1 - is_active WHERE id = %s", (item_id,))
            redirect_endpoint = "admin.reimburse_item_settings"
        else:
            flash("未知配置类型。")
            return redirect(url_for("admin.item_settings"))
        db.commit()
    flash("配置项状态已更新。")
    return redirect(url_for(redirect_endpoint))

@admin_bp.route("/reimburse/export")
@login_required
@admin_required
def export_reimburse_excel():
    db = get_db()
    keyword = request.args.get("keyword", "").strip()
    invoice_type_id = request.args.get("invoice_type_id", "").strip()

    sql = """
        SELECT i.*, u.name AS employee_name, u.employee_no, u.total_quota, u.used_quota,
               it.type_name
        FROM invoices i
        JOIN users u ON i.user_id = u.id
        JOIN invoice_types it ON i.invoice_type_id = it.id
        WHERE i.status = 'Approved'
    """
    params = []
    if keyword:
        sql += " AND (u.name LIKE %s OR u.employee_no LIKE %s)"
        like_kw = f"%{keyword}%"
        params.extend([like_kw, like_kw])
    if invoice_type_id:
        sql += " AND i.invoice_type_id = %s"
        params.append(invoice_type_id)
    sql += " ORDER BY i.id DESC"

    with db.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    export_dir = current_app.config["EXPORT_FOLDER"]
    os.makedirs(export_dir, exist_ok=True)
    filename = f"reimburse_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    export_path = os.path.join(export_dir, filename)
    export_invoice_rows_to_excel(export_path, rows)
    return send_from_directory(export_dir, filename, as_attachment=True)


@admin_bp.route("/reimburse/export-entry")
@login_required
@admin_required
def export_entry_excel():
    db = get_db()
    with db.cursor() as cur:
        ensure_admin_config_tables(cur)
        sync_user_quota(cur, user_id)
        db.commit()
        cur.execute("""
            SELECT i.*
            FROM invoices i
            ORDER BY i.id DESC
        """)
        rows = cur.fetchall()

    export_dir = current_app.config["EXPORT_FOLDER"]
    os.makedirs(export_dir, exist_ok=True)
    filename = f"entry_detail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    export_path = os.path.join(export_dir, filename)
    export_entry_rows_to_excel(export_path, rows)
    return send_from_directory(export_dir, filename, as_attachment=True)


@admin_bp.route("/reimburse/export-reimbursed")
@login_required
@admin_required
def export_reimbursed_excel():
    db = get_db()
    with db.cursor() as cur:
        ensure_admin_config_tables(cur)
        db.commit()
        cur.execute("""
            SELECT rl.created_at AS reimbursed_at,
                   rl.amount AS reimburse_amount,
                   rl.comment,
                   ri.item_name AS reimburse_item_name,
                   u.name AS employee_name,
                   u.employee_no,
                   i.receipt_date,
                   i.submitter_name,
                   i.pdf_filename,
                   i.invoice_date,
                   i.invoice_no,
                   i.buyer_name,
                   i.seller_name,
                   i.project_name
            FROM reimbursement_logs rl
            JOIN invoices i ON rl.invoice_id = i.id
            JOIN users u ON rl.user_id = u.id
            JOIN reimburse_items ri ON rl.reimburse_item_id = ri.id
            ORDER BY rl.created_at DESC, rl.id DESC
        """)
        rows = cur.fetchall()

    export_dir = current_app.config["EXPORT_FOLDER"]
    os.makedirs(export_dir, exist_ok=True)
    filename = f"reimbursed_detail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    export_path = os.path.join(export_dir, filename)
    export_reimbursement_logs_to_excel(export_path, rows)
    return send_from_directory(export_dir, filename, as_attachment=True)

@admin_bp.route("/invoice/<int:invoice_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_invoice(invoice_id):
    db = get_db()
    with db.cursor() as cur:
        ensure_admin_config_tables(cur)
        cur.execute("SELECT * FROM invoices WHERE id = %s", (invoice_id,))
        inv = cur.fetchone()
        if not inv:
            flash("发票不存在。")
            return redirect(url_for("admin.reimburse"))
        if inv["status"] == "Approved":
            cur.execute("""
                UPDATE users
                SET total_quota = GREATEST(total_quota - %s, 0)
                WHERE id = %s
            """, (inv.get("approved_amount") or 0, inv["user_id"]))
        elif inv["status"] == "Reimbursed":
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS reimbursed_amount
                FROM reimbursement_logs
                WHERE invoice_id = %s
            """, (invoice_id,))
            reimbursed_amount = cur.fetchone()["reimbursed_amount"] or 0
            cur.execute("""
                UPDATE users
                SET used_quota = GREATEST(used_quota - %s, 0)
                WHERE id = %s
            """, (reimbursed_amount, inv["user_id"]))
            cur.execute("DELETE FROM reimbursement_logs WHERE invoice_id = %s", (invoice_id,))
        remove_uploaded_file(inv.get("file_path"))
        cur.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        db.commit()
    flash("发票及附件已删除。")
    return redirect(url_for("admin.reimburse"))

@admin_bp.route("/invoice/<int:invoice_id>/file")
@login_required
@admin_required
def view_invoice_file(invoice_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT file_path FROM invoices WHERE id = %s", (invoice_id,))
        item = cur.fetchone()
    if not item:
        abort(404)
    file_path = item.get("file_path")
    if not file_path:
        flash("该发票没有附件。")
        return redirect(url_for("admin.reimburse"))
    filename = file_path.split("/")[-1]
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)

@admin_bp.route("/invoice/<int:invoice_id>/download")
@login_required
@admin_required
def download_invoice_file(invoice_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT file_path FROM invoices WHERE id = %s", (invoice_id,))
        item = cur.fetchone()
    if not item:
        abort(404)
    file_path = item.get("file_path")
    if not file_path:
        flash("该发票没有附件。")
        return redirect(url_for("admin.reimburse"))
    filename = file_path.split("/")[-1]
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename, as_attachment=True)

@admin_bp.route("/employee/<int:user_id>", methods=["GET", "POST"])
@login_required
@admin_required
def employee_detail(user_id):
    db = get_db()
    with db.cursor() as cur:
        ensure_admin_config_tables(cur)
        db.commit()

    if request.method == "POST":
        action = request.form.get("action")
        invoice_ids = request.form.getlist("invoice_ids")
        finance_comment = request.form.get("finance_comment", "").strip()
        if not invoice_ids:
            flash("请先选择发票。")
            return redirect(url_for("admin.employee_detail", user_id=user_id))

        placeholders = ",".join(["%s"] * len(invoice_ids))
        with db.cursor() as cur:
            cur.execute(f"""
                SELECT *
                FROM invoices
                WHERE user_id = %s AND id IN ({placeholders})
            """, [user_id] + invoice_ids)
            invoices = cur.fetchall()

            if len(invoices) != len(invoice_ids):
                flash("部分发票不存在或不属于该员工。")
                return redirect(url_for("admin.employee_detail", user_id=user_id))

            for inv in invoices:
                if inv["status"] == "Reimbursed":
                    flash("已核销的发票不能重新审核。")
                    return redirect(url_for("admin.employee_detail", user_id=user_id))

                if action == "approve":
                    approved_amount = request.form.get(f"approved_amount_{inv['id']}", inv["amount"])
                    ok, msg = approve_invoice(cur, inv, approved_amount, finance_comment)
                elif action == "reject":
                    ok, msg = reject_invoice(cur, inv, finance_comment)
                else:
                    flash("未知操作。")
                    return redirect(url_for("admin.employee_detail", user_id=user_id))

                if not ok:
                    flash(f"发票 {inv['invoice_no'] or inv['id']}：{msg}")
                    return redirect(url_for("admin.employee_detail", user_id=user_id))

            db.commit()

        flash("批量审核完成。")
        return redirect(url_for("admin.employee_detail", user_id=user_id))

    with db.cursor() as cur:
        cur.execute("SELECT id, employee_no, name, role, total_quota, used_quota FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()
        if not user:
            flash("未找到该员工。")
            return redirect(url_for("admin.reimburse"))
        cur.execute("""
            SELECT COUNT(*) AS total_count,
                   SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN status = 'Approved' THEN 1 ELSE 0 END) AS approved_count,
                   SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected_count,
                   SUM(CASE WHEN status = 'Reimbursed' THEN 1 ELSE 0 END) AS reimbursed_count
            FROM invoices WHERE user_id=%s
        """, (user_id,))
        stats = cur.fetchone()
        cur.execute("""
            SELECT i.*, it.type_name
            FROM invoices i
            JOIN invoice_types it ON i.invoice_type_id = it.id
            WHERE i.user_id = %s
            ORDER BY i.id DESC
        """, (user_id,))
        invoices = cur.fetchall()
    total_quota = float(user["total_quota"] or 0)
    used_quota = float(user["used_quota"] or 0)
    remaining_quota = max(total_quota - used_quota, 0)
    return render_template("admin/employee_detail.html", user=user, invoices=invoices,
                           total_quota=total_quota, used_quota=used_quota, remaining_quota=remaining_quota,
                           total_count=stats["total_count"] or 0, pending_count=stats["pending_count"] or 0,
                           approved_count=stats["approved_count"] or 0, rejected_count=stats["rejected_count"] or 0,
                           reimbursed_count=stats["reimbursed_count"] or 0)
