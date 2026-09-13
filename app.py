from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gate5.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gate5-demo-secret-change-me")


# ---------- DB helpers ----------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(reset: bool = False) -> None:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_no VARCHAR(20) NOT NULL UNIQUE,
                model_name VARCHAR(50) NOT NULL,
                device_type VARCHAR(20) NOT NULL CHECK(device_type IN ('LAPTOP','TABLET','MONITOR')),
                status VARCHAR(20) NOT NULL CHECK(status IN ('AVAILABLE','LENT','REPAIR','DISPOSED')),
                purchased_at DATE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(50) NOT NULL,
                department VARCHAR(50) NOT NULL,
                employment_status VARCHAR(20) NOT NULL CHECK(employment_status IN ('ACTIVE','LEAVE','RETIRED'))
            );

            CREATE TABLE IF NOT EXISTS lendings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                lent_at DATETIME NOT NULL,
                due_date DATE NOT NULL,
                returned_at DATETIME NULL,
                purpose VARCHAR(100) NOT NULL,
                request_token VARCHAR(64) NOT NULL UNIQUE,
                FOREIGN KEY(device_id) REFERENCES devices(id),
                FOREIGN KEY(user_id) REFERENCES employees(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_lending_per_device
            ON lendings(device_id)
            WHERE returned_at IS NULL;
            """
        )

        count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO devices(asset_no, model_name, device_type, status, purchased_at) VALUES(?,?,?,?,?)",
                [
                    ("PC-0001", "ThinkPad X1", "LAPTOP", "AVAILABLE", "2025-04-10"),
                    ("PC-0002", "Latitude 7440", "LAPTOP", "REPAIR", "2025-03-15"),
                    ("PC-0003", "HP EliteBook", "LAPTOP", "LENT", "2024-11-20"),
                    ("PC-0004", "Surface Laptop", "LAPTOP", "AVAILABLE", "2025-06-01"),
                    ("TB-0001", "iPad Air", "TABLET", "AVAILABLE", "2025-01-12"),
                    ("PC-0099", "Old Notebook", "LAPTOP", "DISPOSED", "2021-02-01"),
                ],
            )

        count = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO employees(name, department, employment_status) VALUES(?,?,?)",
                [
                    ("佐藤 太郎", "営業部", "ACTIVE"),
                    ("田中 花子", "企画部", "LEAVE"),
                    ("鈴木 一郎", "総務部", "RETIRED"),
                    ("高橋 美咲", "開発部", "ACTIVE"),
                ],
            )

        # Seed one active lending for PC-0003.
        active = conn.execute(
            "SELECT COUNT(*) FROM lendings l JOIN devices d ON d.id=l.device_id WHERE d.asset_no='PC-0003' AND l.returned_at IS NULL"
        ).fetchone()[0]
        if active == 0:
            dev_id = conn.execute("SELECT id FROM devices WHERE asset_no='PC-0003'").fetchone()[0]
            user_id = conn.execute("SELECT id FROM employees WHERE name='佐藤 太郎'").fetchone()[0]
            conn.execute(
                """INSERT INTO lendings(device_id,user_id,lent_at,due_date,returned_at,purpose,request_token)
                   VALUES(?,?,?,?,NULL,?,?)""",
                (dev_id, user_id, datetime.now().isoformat(timespec="seconds"), (date.today()+timedelta(days=7)).isoformat(), "顧客訪問", "seed-pc0003"),
            )
        conn.commit()
    finally:
        conn.close()


def current_employee_id() -> int:
    # Demo login: switch user via /switch-user/<id>
    if "employee_id" not in session:
        session["employee_id"] = 1
    return int(session["employee_id"])


def fetch_current_employee(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM employees WHERE id=?", (current_employee_id(),)).fetchone()


def validate_due_date(raw: str) -> tuple[date | None, str | None]:
    if not raw:
        return None, "返却予定日を入力してください。"
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return None, "返却予定日は正しい日付で入力してください。"

    today = date.today()
    if value < today:
        return None, "返却予定日は本日以降の日付を入力してください。"
    if value > today + timedelta(days=90):
        return None, "返却予定日は90日以内で入力してください。"
    return value, None


def has_overdue_lending(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM lendings
           WHERE user_id=? AND returned_at IS NULL AND due_date < ? LIMIT 1""",
        (user_id, date.today().isoformat()),
    ).fetchone()
    return row is not None


def new_request_token() -> str:
    token = secrets.token_urlsafe(24)
    session["loan_request_token"] = token
    return token


# ---------- Routes ----------
@app.route("/")
def index():
    return redirect(url_for("loan_form"))


@app.route("/switch-user/<int:user_id>")
def switch_user(user_id: int):
    conn = get_db()
    user = conn.execute("SELECT id FROM employees WHERE id=?", (user_id,)).fetchone()
    if user:
        session["employee_id"] = user_id
    return redirect(request.referrer or url_for("loan_form"))


@app.route("/loan", methods=["GET", "POST"])
def loan_form():
    conn = get_db()
    employee = fetch_current_employee(conn)

    # Show only actual loan candidates in the dropdown.
    devices = conn.execute(
        "SELECT * FROM devices WHERE device_type='LAPTOP' AND status='AVAILABLE' ORDER BY asset_no"
    ).fetchall()

    if request.method == "POST":
        device_id_raw = request.form.get("device_id", "")
        due_raw = request.form.get("due_date", "")
        purpose = request.form.get("purpose", "").strip()

        errors: list[str] = []

        try:
            device_id = int(device_id_raw)
        except (TypeError, ValueError):
            device_id = None
            errors.append("指定された端末は存在しません。")

        due_date, due_error = validate_due_date(due_raw)
        if due_error:
            errors.append(due_error)

        if not purpose:
            errors.append("利用目的を入力してください。")
        elif len(purpose) > 100:
            errors.append("利用目的は100文字以内で入力してください。")

        if not employee:
            errors.append("社員情報を確認できませんでした。")
        elif employee["employment_status"] == "RETIRED":
            errors.append("退職済みのためPCを貸し出せません。")
        elif employee["employment_status"] == "LEAVE":
            errors.append("休職中のためPCを貸し出せません。")
        elif has_overdue_lending(conn, employee["id"]):
            errors.append("延滞中のPCを返却してから新しい貸出を申請してください。")

        selected_device = None
        if device_id is not None:
            selected_device = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not selected_device:
                errors.append("指定された端末は存在しません。")
            elif selected_device["device_type"] != "LAPTOP":
                errors.append("この端末は貸出対象外です。")
            elif selected_device["status"] == "LENT":
                errors.append("この端末は現在貸出中です。別の端末を選択してください。")
            elif selected_device["status"] == "REPAIR":
                errors.append("この端末は修理中のため貸出できません。")
            elif selected_device["status"] == "DISPOSED":
                errors.append("この端末は貸出対象外です。")

        if errors:
            for msg in dict.fromkeys(errors):
                flash(msg, "error")
            return render_template("loan_form.html", devices=devices, employee=employee, form=request.form)

        token = new_request_token()
        session["pending_loan"] = {
            "device_id": device_id,
            "due_date": due_date.isoformat(),
            "purpose": purpose,
            "request_token": token,
        }
        return redirect(url_for("loan_confirm"))

    token = new_request_token()
    return render_template("loan_form.html", devices=devices, employee=employee, form={}, request_token=token)


@app.route("/loan/confirm", methods=["GET", "POST"])
def loan_confirm():
    conn = get_db()
    employee = fetch_current_employee(conn)
    pending = session.get("pending_loan")

    if not pending:
        flash("不正な操作です。貸出申請画面からやり直してください。", "error")
        return redirect(url_for("loan_form"))

    device = conn.execute("SELECT * FROM devices WHERE id=?", (pending["device_id"],)).fetchone()
    if not device:
        flash("指定された端末は存在しません。", "error")
        return redirect(url_for("loan_form"))

    if request.method == "GET":
        return render_template("loan_confirm.html", device=device, employee=employee, pending=pending)

    posted_token = request.form.get("request_token", "")
    if posted_token != pending.get("request_token"):
        flash("この申請はすでに処理されています。", "error")
        return redirect(url_for("loan_form"))

    # Server-side revalidation before writing.
    due_date, due_error = validate_due_date(pending["due_date"])
    if due_error:
        flash(due_error, "error")
        return redirect(url_for("loan_form"))
    if not employee or employee["employment_status"] != "ACTIVE":
        flash("現在の在籍状態ではPCを貸し出せません。", "error")
        return redirect(url_for("loan_form"))
    if has_overdue_lending(conn, employee["id"]):
        flash("延滞中のPCを返却してから新しい貸出を申請してください。", "error")
        return redirect(url_for("loan_form"))

    try:
        conn.execute("BEGIN IMMEDIATE")

        # Atomic state transition. Only one concurrent request can win.
        cur = conn.execute(
            """UPDATE devices SET status='LENT'
               WHERE id=? AND device_type='LAPTOP' AND status='AVAILABLE'""",
            (device["id"],),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            flash("他の利用者が先にこの端末を貸し出しました。別の端末を選択してください。", "error")
            session.pop("pending_loan", None)
            return redirect(url_for("loan_form"))

        conn.execute(
            """INSERT INTO lendings(device_id,user_id,lent_at,due_date,returned_at,purpose,request_token)
               VALUES(?,?,?,?,NULL,?,?)""",
            (
                device["id"],
                employee["id"],
                datetime.now().isoformat(timespec="seconds"),
                due_date.isoformat(),
                pending["purpose"],
                posted_token,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        flash("この申請はすでに処理されています。", "error")
        session.pop("pending_loan", None)
        return redirect(url_for("loan_form"))
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        flash("貸出処理に失敗しました。もう一度お試しください。", "error")
        return redirect(url_for("loan_form"))

    session.pop("pending_loan", None)
    return render_template(
        "loan_complete.html",
        device=device,
        due_date=due_date.strftime("%Y/%m/%d"),
    )


@app.route("/return", methods=["GET", "POST"])
def return_device():
    conn = get_db()
    employee = fetch_current_employee(conn)
    active = conn.execute(
        """SELECT l.*, d.asset_no, d.model_name, d.status AS device_status
           FROM lendings l JOIN devices d ON d.id=l.device_id
           WHERE l.user_id=? AND l.returned_at IS NULL
           ORDER BY l.lent_at DESC""",
        (employee["id"],),
    ).fetchall()

    if request.method == "POST":
        lending_id_raw = request.form.get("lending_id", "")
        try:
            lending_id = int(lending_id_raw)
        except (TypeError, ValueError):
            flash("この端末の貸出情報がありません。", "error")
            return redirect(url_for("return_device"))

        lending = conn.execute(
            """SELECT l.*, d.status AS device_status, d.asset_no
               FROM lendings l JOIN devices d ON d.id=l.device_id
               WHERE l.id=?""",
            (lending_id,),
        ).fetchone()
        if not lending:
            flash("この端末の貸出情報がありません。", "error")
            return redirect(url_for("return_device"))
        if lending["user_id"] != employee["id"]:
            flash("この貸出情報を操作する権限がありません。", "error")
            return redirect(url_for("return_device"))
        if lending["returned_at"] is not None:
            flash("この端末はすでに返却されています。", "error")
            return redirect(url_for("return_device"))
        if lending["device_status"] != "LENT":
            flash("この端末は現在貸出中ではありません。", "error")
            return redirect(url_for("return_device"))

        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE lendings SET returned_at=? WHERE id=? AND returned_at IS NULL AND user_id=?",
                (datetime.now().isoformat(timespec="seconds"), lending_id, employee["id"]),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                flash("この端末はすでに返却されています。", "error")
                return redirect(url_for("return_device"))

            cur = conn.execute(
                "UPDATE devices SET status='AVAILABLE' WHERE id=? AND status='LENT'",
                (lending["device_id"],),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                flash("返却処理に失敗しました。もう一度お試しください。", "error")
                return redirect(url_for("return_device"))
            conn.execute("COMMIT")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            flash("返却処理に失敗しました。もう一度お試しください。", "error")
            return redirect(url_for("return_device"))

        return render_template("return_complete.html", asset_no=lending["asset_no"])

    return render_template("return_form.html", lendings=active, employee=employee)


@app.route("/demo/error/lent")
def demo_error_lent():
    conn = get_db()
    employee = fetch_current_employee(conn)
    device = conn.execute("SELECT * FROM devices WHERE asset_no='PC-0003'").fetchone()
    return render_template(
        "demo_error.html",
        employee=employee,
        title="貸出できません",
        message="この端末は現在貸出中です。別の端末を選択してください。",
        detail=f"{device['asset_no']} / {device['model_name']}",
    )


@app.route("/demo/error/past-date")
def demo_error_past_date():
    conn = get_db()
    employee = fetch_current_employee(conn)
    return render_template(
        "demo_error.html",
        employee=employee,
        title="入力内容を確認してください",
        message="返却予定日は本日以降の日付を入力してください。",
        detail="返却予定日：2026/01/01",
    )


@app.route("/demo/error/retired")
def demo_error_retired():
    conn = get_db()
    employee = conn.execute("SELECT * FROM employees WHERE employment_status='RETIRED' LIMIT 1").fetchone()
    return render_template(
        "demo_error.html",
        employee=employee,
        title="貸出できません",
        message="退職済みのためPCを貸し出せません。",
        detail=f"利用者：{employee['name']} / {employee['department']}",
    )


@app.route("/demo/success")
def demo_success():
    conn = get_db()
    device = conn.execute("SELECT * FROM devices WHERE asset_no='PC-0001'").fetchone()
    return render_template(
        "loan_complete.html",
        device=device,
        due_date=(date.today()+timedelta(days=7)).strftime("%Y/%m/%d"),
    )


@app.route("/admin/reset")
def admin_reset():
    init_db(reset=True)
    session.clear()
    flash("デモデータを初期化しました。", "info")
    return redirect(url_for("loan_form"))


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
