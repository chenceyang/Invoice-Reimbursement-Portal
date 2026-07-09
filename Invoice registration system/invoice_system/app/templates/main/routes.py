
from flask import Blueprint, render_template, session
from ...db import get_db
from ...utils import login_required

main_bp = Blueprint("main", __name__)


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

@main_bp.route("/")
@login_required
def home():
    db = get_db()
    user_id = session["user_id"]
    role = session.get("role")
    with db.cursor() as cur:
        quota_total, quota_used = sync_user_quota(cur, user_id)
        db.commit()
        cur.execute("SELECT total_quota, used_quota, name, role FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()
        quota_remaining = max(quota_total - quota_used, 0)

        cur.execute("""
            SELECT COUNT(*) AS cnt FROM invoices WHERE user_id=%s
        """, (user_id,))
        my_invoice_count = cur.fetchone()["cnt"]

        cur.execute("""
            SELECT i.*, it.type_name
            FROM invoices i
            JOIN invoice_types it ON i.invoice_type_id = it.id
            WHERE i.user_id=%s
            ORDER BY i.id DESC
            LIMIT 10
        """, (user_id,))
        recent_invoices = cur.fetchall()

        admin_pending_count = 0
        if role == "admin":
            cur.execute("SELECT COUNT(*) AS cnt FROM invoices WHERE status='Pending'")
            admin_pending_count = cur.fetchone()["cnt"]

    return render_template("main/home.html",
                           quota_total=quota_total,
                           quota_used=quota_used,
                           quota_remaining=quota_remaining,
                           my_invoice_count=my_invoice_count,
                           recent_invoices=recent_invoices,
                           admin_pending_count=admin_pending_count)
