import os
import uuid
from datetime import datetime
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for, abort, send_from_directory
from ..db import get_db
from ..utils import login_required, allowed_file, safe_filename
from ..ocr_service import extract_invoice_info
from ..excel_service import append_invoice_to_excel
from datetime import date

invoice_bp = Blueprint("invoice", __name__, url_prefix="/invoice")


def build_unique_filename(original_filename: str) -> str:
    safe_name = safe_filename(original_filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:12]
    return f"{timestamp}_{short_uuid}_{safe_name}"


def remove_uploaded_file(file_path: str):
    if not file_path:
        return
    try:
        filename = file_path.split("/")[-1]
        full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
        if os.path.exists(full_path):
            os.remove(full_path)
    except Exception as e:
        print("remove_uploaded_file error:", e)


def normalize_amount(val: str) -> str:
    val = (val or "").replace("￥", "").replace("¥", "").replace(",", "").replace("元", "").strip()
    return val

def validate_invoice_year(invoice_date_str: str):
    """
    规则：
    - 每年1月1日~1月3日：允许上传当年 + 上一年发票
    - 1月4日~12月31日：只允许上传当年发票
    返回: (True, "") / (False, "提示语")
    """
    if not invoice_date_str:
        return False, "开票日期不能为空。"

    try:
        inv_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date()
    except ValueError:
        return False, "开票日期格式不正确，请使用 YYYY-MM-DD。"

    today = date.today()
    current_year = today.year

    if today.month == 1 and today.day <= 3:
        allowed_years = {current_year, current_year - 1}
    else:
        allowed_years = {current_year}

    if inv_date.year not in allowed_years:
        if today.month == 1 and today.day <= 3:
            return False, f"当前仅允许上传 {current_year} 年或 {current_year - 1} 年的发票。"
        else:
            return False, f"当前仅允许上传 {current_year} 年的发票。"

    return True, ""

def check_duplicate_invoice(cur, invoice_no, invoice_date=None, amount=None, seller_name=None, exclude_id=None):
    """
    返回重复发票ID；没有重复返回 None
    优先按 invoice_no 查重；
    如果 invoice_no 为空或想更稳，也可用 invoice_date + amount + seller_name 兜底。
    """
    invoice_no = (invoice_no or "").strip()
    invoice_date = (invoice_date or "").strip()
    seller_name = (seller_name or "").strip()
    amount = normalize_amount(amount or "")

    # 1) 发票号查重
    if invoice_no:
        sql = "SELECT id FROM invoices WHERE invoice_no = %s"
        params = [invoice_no]
        if exclude_id:
            sql += " AND id <> %s"
            params.append(exclude_id)

        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        if row:
            return row["id"]

    # 2) 兜底组合查重：开票日期 + 金额 + 销售方
    if invoice_date and amount and seller_name:
        sql = """
            SELECT id
            FROM invoices
            WHERE invoice_date = %s
              AND amount = %s
              AND seller_name = %s
        """
        params = [invoice_date, amount, seller_name]
        if exclude_id:
            sql += " AND id <> %s"
            params.append(exclude_id)

        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        if row:
            return row["id"]

    return None

@invoice_bp.route("/my")
@login_required
def my_list():
    db = get_db()

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    page_size = 10
    offset = (page - 1) * page_size

    with db.cursor() as cur:
        # 1. 总条数
        cur.execute("""
            SELECT COUNT(*) AS total
            FROM invoices
            WHERE user_id = %s
        """, (session["user_id"],))
        total = cur.fetchone()["total"]
        total_pages = (total + page_size - 1) // page_size

        # 2. 当前页数据
        cur.execute("""
            SELECT
                i.id,
                i.user_id,
                i.file_path,
                i.status,
                i.amount,
                i.created_at,
                u.name AS employee_name,
                it.type_name
            FROM invoices i
            JOIN users u ON i.user_id = u.id
            JOIN invoice_types it ON i.invoice_type_id = it.id
            WHERE i.user_id = %s
            ORDER BY i.id DESC
            LIMIT %s OFFSET %s
        """, (session["user_id"], page_size, offset))
        items = cur.fetchall()

        # 3. 汇总金额
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) AS total_amount
            FROM invoices
            WHERE user_id = %s
        """, (session["user_id"],))
        total_amount = cur.fetchone()["total_amount"]

    return render_template(
        "invoice/my_list.html",
        items=items,
        total_amount=total_amount,
        page=page,
        total_pages=total_pages
    )


@invoice_bp.route("/create", methods=["GET", "POST"])
@login_required
def create():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id, type_name FROM invoice_types WHERE is_active = 1 ORDER BY type_name")
        invoice_types = cur.fetchall()

    # 默认收票日期 = 今天
    ocr_result = {
        "receipt_date": datetime.now().strftime("%Y-%m-%d"),
        "submitter_name": session.get("user_name", ""),
        "pdf_filename": "",
        "invoice_date": "",
        "invoice_no": "",
        "buyer_name": "",
        "seller_name": "",
        "amount": "",
        "project_name": "",
        "invoice_type_id": "",
        "file_path": ""
    }

    if request.method == "POST":
        action = request.form.get("action", "submit")

        # 第一步：上传并 OCR 回填
        if action == "upload":
            file = request.files.get("invoice_file")
            if not file or not file.filename:
                flash("请先选择发票文件。")
                return render_template("invoice/create.html", invoice_types=invoice_types, ocr=ocr_result)

            if not allowed_file(file.filename):
                flash("仅支持 PDF / PNG / JPG / JPEG 文件。")
                return render_template("invoice/create.html", invoice_types=invoice_types, ocr=ocr_result)

            unique_filename = build_unique_filename(file.filename)
            save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], unique_filename)
            file.save(save_path)

            ocr_result = extract_invoice_info(save_path, current_user_name=session.get("user_name", ""))

            # OCR 没有给收票日期时，默认当天
            if not ocr_result.get("receipt_date"):
                ocr_result["receipt_date"] = datetime.now().strftime("%Y-%m-%d")

            # OCR 没有给交票人时，默认当前登录用户
            if not ocr_result.get("submitter_name"):
                ocr_result["submitter_name"] = session.get("user_name", "")

            # 只存文件名，不要加 uploads/ 前缀
            ocr_result["file_path"] = unique_filename
            ocr_result["pdf_filename"] = safe_filename(file.filename)

            # ========= 上传阶段：重复校验 =========
            with db.cursor() as cur:
                dup_id = check_duplicate_invoice(
                    cur,
                    invoice_no=ocr_result.get("invoice_no", ""),
                    invoice_date=ocr_result.get("invoice_date", ""),
                    amount=ocr_result.get("amount", ""),
                    seller_name=ocr_result.get("seller_name", "")
                )
                if dup_id:
                    # 删掉刚上传的重复附件，避免垃圾文件堆积
                    remove_uploaded_file(unique_filename)
                    flash("该发票已存在，不能重复上传。")
                    return render_template("invoice/create.html", invoice_types=invoice_types, ocr={
                        **ocr_result,
                        "file_path": "",
                        "pdf_filename": ""
                    })

            # ========= 上传阶段：年份校验（有开票日期时先校验） =========
            if ocr_result.get("invoice_date"):
                ok, msg = validate_invoice_year(ocr_result["invoice_date"])
                if not ok:
                    # 同样删掉刚上传但不允许的附件
                    remove_uploaded_file(unique_filename)
                    flash(msg)
                    return render_template("invoice/create.html", invoice_types=invoice_types, ocr={
                        **ocr_result,
                        "file_path": "",
                        "pdf_filename": ""
                    })

            flash("发票已上传，识别结果已自动回填，请确认后提交。")
            return render_template("invoice/create.html", invoice_types=invoice_types, ocr=ocr_result)

        # 第二步：最终提交
        form = {
            "receipt_date": request.form.get("receipt_date", "").strip(),
            "submitter_name": request.form.get("submitter_name", "").strip(),
            "pdf_filename": request.form.get("pdf_filename", "").strip(),
            "invoice_date": request.form.get("invoice_date", "").strip(),
            "invoice_no": request.form.get("invoice_no", "").strip(),
            "buyer_name": request.form.get("buyer_name", "").strip(),
            "seller_name": request.form.get("seller_name", "").strip(),
            "amount": normalize_amount(request.form.get("amount", "")),
            "project_name": request.form.get("project_name", "").strip(),
            "invoice_type_id": request.form.get("invoice_type_id", "").strip(),
            "file_path": request.form.get("file_path", "").strip(),
        }

        # 默认收票日期为空时，自动补今天
        if not form["receipt_date"]:
            form["receipt_date"] = datetime.now().strftime("%Y-%m-%d")

        if any(not form[f] for f in ["invoice_no", "amount", "invoice_type_id"]):
            flash("请填写完整必填项。")
            return render_template("invoice/create.html", invoice_types=invoice_types, ocr=form)

        # ========= 最终提交前：年份校验 =========
        ok, msg = validate_invoice_year(form["invoice_date"])
        if not ok:
            flash(msg)
            return render_template("invoice/create.html", invoice_types=invoice_types, ocr=form)

        with db.cursor() as cur:
            # ========= 最终提交前：重复校验 =========
            dup_id = check_duplicate_invoice(
                cur,
                invoice_no=form["invoice_no"],
                invoice_date=form["invoice_date"],
                amount=form["amount"],
                seller_name=form["seller_name"]
            )
            if dup_id:
                flash("该发票已存在，不能重复提交。")
                return render_template("invoice/create.html", invoice_types=invoice_types, ocr=form)

            # 只校验开票类型
            cur.execute("SELECT * FROM invoice_types WHERE id = %s AND is_active = 1", (form["invoice_type_id"],))
            inv_type = cur.fetchone()
            if not inv_type:
                flash("开票类型不存在或不可用。")
                return render_template("invoice/create.html", invoice_types=invoice_types, ocr=form)

            # 插入发票
            cur.execute("""
                INSERT INTO invoices (
                    user_id, receipt_date, submitter_name, pdf_filename, invoice_date, invoice_no,
                    buyer_name, seller_name, amount, project_name, invoice_type_id,
                    file_path, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pending')
            """, (
                session["user_id"],
                form["receipt_date"] or None,
                form["submitter_name"] or session.get("user_name", ""),
                form["pdf_filename"] or None,
                form["invoice_date"] or None,
                form["invoice_no"],
                form["buyer_name"] or None,
                form["seller_name"] or None,
                form["amount"],
                form["project_name"] or None,
                form["invoice_type_id"],
                form["file_path"] or None
            ))

            db.commit()

            append_invoice_to_excel(current_app.config["EXCEL_EXPORT_PATH"], {
                **form,
                "invoice_type_name": inv_type["type_name"],
                "status": "Pending"
            })

        flash("发票登记成功，已写入数据库和 Excel。")
        return redirect(url_for("invoice.my_list"))

    return render_template("invoice/create.html", invoice_types=invoice_types, ocr=ocr_result)


@invoice_bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit(invoice_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id, type_name FROM invoice_types WHERE is_active = 1 ORDER BY type_name")
        invoice_types = cur.fetchall()
        cur.execute("SELECT * FROM invoices WHERE id = %s AND user_id = %s", (invoice_id, session["user_id"]))
        item = cur.fetchone()

    if not item:
        flash("未找到该发票。")
        return redirect(url_for("invoice.my_list"))

    if item["status"] not in ("Pending", "Rejected"):
        flash("只有待审核或已驳回发票可以修改。")
        return redirect(url_for("invoice.my_list"))

    if request.method == "POST":
        form = {
            "receipt_date": request.form.get("receipt_date", "").strip(),
            "submitter_name": request.form.get("submitter_name", "").strip(),
            "pdf_filename": request.form.get("pdf_filename", "").strip(),
            "invoice_date": request.form.get("invoice_date", "").strip(),
            "invoice_no": request.form.get("invoice_no", "").strip(),
            "buyer_name": request.form.get("buyer_name", "").strip(),
            "seller_name": request.form.get("seller_name", "").strip(),
            "amount": normalize_amount(request.form.get("amount", "")),
            "project_name": request.form.get("project_name", "").strip(),
            "invoice_type_id": request.form.get("invoice_type_id", "").strip(),
        }

        if not form["receipt_date"]:
            form["receipt_date"] = datetime.now().strftime("%Y-%m-%d")

        if any(not form[f] for f in ["invoice_no", "amount", "invoice_type_id"]):
            flash("请填写完整必填项。")
            return render_template("invoice/edit.html", item={**item, **form}, invoice_types=invoice_types)

        with db.cursor() as cur:
            # 发票号去重
            ok, msg = validate_invoice_year(form["invoice_date"])
            if not ok:
                flash(msg)
                return render_template("invoice/edit.html", item={**item, **form}, invoice_types=invoice_types)
            dup_id = check_duplicate_invoice(
                cur,
                invoice_no=form["invoice_no"],
                invoice_date=form["invoice_date"],
                amount=form["amount"],
                seller_name=form["seller_name"],
                exclude_id=invoice_id
            )
            if dup_id:
                flash("该发票号码已存在，不能修改为重复发票号。")
                return render_template("invoice/edit.html", item={**item, **form}, invoice_types=invoice_types)

            # 校验开票类型
            cur.execute("SELECT * FROM invoice_types WHERE id = %s AND is_active = 1", (form["invoice_type_id"],))
            inv_type = cur.fetchone()
            if not inv_type:
                flash("开票类型不存在或不可用。")
                return render_template("invoice/edit.html", item={**item, **form}, invoice_types=invoice_types)

            cur.execute("""
                UPDATE invoices
                SET receipt_date=%s,
                    submitter_name=%s,
                    pdf_filename=%s,
                    invoice_date=%s,
                    invoice_no=%s,
                    buyer_name=%s,
                    seller_name=%s,
                    amount=%s,
                    project_name=%s,
                    invoice_type_id=%s
                WHERE id=%s AND user_id=%s
            """, (
                form["receipt_date"] or None,
                form["submitter_name"] or session.get("user_name", ""),
                form["pdf_filename"] or None,
                form["invoice_date"] or None,
                form["invoice_no"],
                form["buyer_name"] or None,
                form["seller_name"] or None,
                form["amount"],
                form["project_name"] or None,
                form["invoice_type_id"],
                invoice_id,
                session["user_id"]
            ))

            db.commit()

        flash("发票修改成功。")
        return redirect(url_for("invoice.my_list"))

    return render_template("invoice/edit.html", item=item, invoice_types=invoice_types)


@invoice_bp.route("/<int:invoice_id>/delete", methods=["POST"])
@login_required
def delete(invoice_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE id = %s AND user_id = %s", (invoice_id, session["user_id"]))
        item = cur.fetchone()
        if not item:
            flash("未找到该发票，或您没有权限删除。")
            return redirect(url_for("invoice.my_list"))

        # 删除附件
        remove_uploaded_file(item.get("file_path"))

        # 删除发票
        cur.execute("DELETE FROM invoices WHERE id = %s AND user_id = %s", (invoice_id, session["user_id"]))

        db.commit()

    flash("发票及附件已删除。")
    return redirect(url_for("invoice.my_list"))


@invoice_bp.route("/<int:invoice_id>/file")
@login_required
def view_file(invoice_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT file_path FROM invoices WHERE id = %s AND user_id = %s", (invoice_id, session["user_id"]))
        item = cur.fetchone()

    if not item:
        abort(404)

    file_path = item.get("file_path")
    if not file_path:
        flash("该发票没有附件。")
        return redirect(url_for("invoice.my_list"))

    filename = file_path.split("/")[-1]
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)


@invoice_bp.route("/<int:invoice_id>/download")
@login_required
def download_file(invoice_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT file_path FROM invoices WHERE id = %s AND user_id = %s", (invoice_id, session["user_id"]))
        item = cur.fetchone()

    if not item:
        abort(404)

    file_path = item.get("file_path")
    if not file_path:
        flash("该发票没有附件。")
        return redirect(url_for("invoice.my_list"))

    filename = file_path.split("/")[-1]
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename, as_attachment=True)
