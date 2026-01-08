import os
import io
import zipfile
import sqlite3
from datetime import date, datetime
from functools import wraps

from flask import Flask, render_template_string, request, redirect, url_for, session, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash

from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, Spacer
from reportlab.lib.styles import getSampleStyleSheet


DB_FILE = "loan.db"

# Admin credentials come from Render environment variables
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")  # set in Render
SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret")  # set in Render

DEFAULT_PRINCIPAL = 2_225_000.00
DEFAULT_INTEREST_RATE = 0.064  # annual, as decimal (6.4%)

PEOPLE_DEFAULT = ["Person A", "Person B", "Person C", "Person D"]

app = Flask(__name__)
app.secret_key = SECRET_KEY


# ---------------- DB ----------------
def get_db():
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.execute(
        """CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            balance REAL NOT NULL,
            last_calc DATE NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            interest REAL NOT NULL,
            pay_date DATE NOT NULL,
            FOREIGN KEY(person_id) REFERENCES people(id)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        )"""
    )

    # Defaults
    if not db.execute("SELECT 1 FROM settings WHERE key='principal'").fetchone():
        db.execute("INSERT INTO settings(key,value) VALUES('principal', ?)", (DEFAULT_PRINCIPAL,))
    if not db.execute("SELECT 1 FROM settings WHERE key='interest_rate'").fetchone():
        db.execute("INSERT INTO settings(key,value) VALUES('interest_rate', ?)", (DEFAULT_INTEREST_RATE,))

    # Seed people if empty
    c = db.execute("SELECT COUNT(*) AS c FROM people").fetchone()["c"]
    if c == 0:
        principal = db.execute("SELECT value FROM settings WHERE key='principal'").fetchone()["value"]
        share = principal / 4.0
        start = date.today().isoformat()
        for name in PEOPLE_DEFAULT:
            db.execute("INSERT INTO people(name,balance,last_calc) VALUES(?,?,?)", (name, share, start))

    db.commit()
    db.close()


init_db()


# ---------------- AUTH ----------------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def get_admin_password_hash() -> str:
    # Hash is generated at runtime from env var; fine for a single-instance service.
    return generate_password_hash(ADMIN_PASSWORD)


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == ADMIN_USERNAME and check_password_hash(get_admin_password_hash(), p):
            session["is_admin"] = True
            return redirect(url_for("index"))
        err = "Invalid username or password."
    return render_template_string(
        """
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
          body{font-family:Arial;padding:16px;max-width:420px;margin:0 auto}
          input,button{width:100%;padding:10px;margin:6px 0;font-size:16px}
          .err{color:#b00020}
        </style>
        <h2>Admin Login</h2>
        {% if err %}<p class="err">{{err}}</p>{% endif %}
        <form method="post">
          <input name="username" placeholder="Username" required>
          <input name="password" type="password" placeholder="Password" required>
          <button type="submit">Log in</button>
        </form>
        """,
        err=err,
    )


@app.route("/logout")
def logout():
    session.pop("is_admin", None)
    return redirect(url_for("login"))


# ---------------- INTEREST ENGINE ----------------
def daily_rate(db) -> float:
    r = db.execute("SELECT value FROM settings WHERE key='interest_rate'").fetchone()["value"]
    return float(r) / 365.0


def accrue_interest_to(db, person_id: int, to_date: date) -> float:
    """
    Accrue interest for one person from last_calc up to to_date (not including to_date?).
    We'll accrue for full day differences: (to_date - last_calc).days
    Returns interest added.
    """
    p = db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    last = datetime.strptime(p["last_calc"], "%Y-%m-%d").date()
    days = (to_date - last).days
    if days <= 0:
        return 0.0

    bal = float(p["balance"])
    interest = bal * daily_rate(db) * days
    new_bal = bal + interest

    db.execute("UPDATE people SET balance=?, last_calc=? WHERE id=?", (new_bal, to_date.isoformat(), person_id))
    return interest


# ---------------- UI ----------------
@app.route("/")
@admin_required
def index():
    db = get_db()

    # Accrue everyone to today for live display
    today = date.today()
    people = db.execute("SELECT * FROM people ORDER BY id").fetchall()
    for p in people:
        accrue_interest_to(db, p["id"], today)
    db.commit()

    people = db.execute("SELECT * FROM people ORDER BY id").fetchall()
    rate = db.execute("SELECT value FROM settings WHERE key='interest_rate'").fetchone()["value"]
    principal = db.execute("SELECT value FROM settings WHERE key='principal'").fetchone()["value"]

    rows = []
    total = 0.0
    for p in people:
        interest_paid = db.execute(
            "SELECT IFNULL(SUM(interest),0) FROM payments WHERE person_id=?",
            (p["id"],),
        ).fetchone()[0]
        total += float(p["balance"])
        rows.append({**dict(p), "interest_paid": float(interest_paid)})

    db.close()

    return render_template_string(
        """
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
          body{font-family:Arial;padding:12px}
          .card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}
          table{width:100%;border-collapse:collapse}
          th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
          input,select,button{width:100%;padding:10px;font-size:16px;margin:6px 0}
          .row{display:grid;grid-template-columns:1fr;gap:10px}
          @media(min-width:720px){.row{grid-template-columns:1fr 1fr}}
          a{word-break:break-word}
        </style>

        <h2>Shared Loan Tracker</h2>
        <div class="card">
          <div><b>Principal:</b> ${{'%.2f'|format(principal)}}</div>
          <div><b>Interest rate:</b> {{'%.3f'|format(rate*100)}}%</div>
          <div style="margin-top:8px"><b>Total remaining balance:</b> ${{'%.2f'|format(total)}}</div>
          <div style="margin-top:8px"><a href="/logout">Logout</a></div>
        </div>

        <div class="card">
          <h3>People</h3>
          <table>
            <tr><th>Name</th><th>Balance</th><th>Interest paid</th><th></th></tr>
            {% for p in people %}
              <tr>
                <td>{{p.name}}</td>
                <td>${{'%.2f'|format(p.balance)}}</td>
                <td>${{'%.2f'|format(p.interest_paid)}}</td>
                <td><a href="/person/{{p.id}}">Statement</a></td>
              </tr>
            {% endfor %}
          </table>
        </div>

        <div class="row">
          <div class="card">
            <h3>Add Payment</h3>
            <form method="post" action="/pay">
              <select name="person_id">
                {% for p in people %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}
              </select>
              <input type="number" step="0.01" name="amount" placeholder="Payment amount" required>
              <input type="date" name="pay_date" value="{{today}}">
              <button type="submit">Submit payment</button>
            </form>
          </div>

          <div class="card">
            <h3>Admin Settings</h3>

            <form method="post" action="/set_principal">
              <label><b>Set principal</b> (re-splits equally)</label>
              <input type="number" step="0.01" name="principal" placeholder="e.g. 2225000" required>
              <button type="submit">Update principal</button>
            </form>

            <form method="post" action="/set_rate">
              <label><b>Set interest rate</b> (%)</label>
              <input type="number" step="0.001" name="rate_pct" placeholder="e.g. 6.4" required>
              <button type="submit">Update rate</button>
            </form>

            <form method="get" action="/monthly_pdfs">
              <label><b>Monthly PDFs</b> (your backup)</label>
              <input type="month" name="month" value="{{month}}">
              <button type="submit">Download monthly ZIP</button>
            </form>
          </div>
        </div>
        """,
        people=rows,
        total=total,
        rate=rate,
        principal=principal,
        today=date.today().isoformat(),
        month=f"{date.today().year:04d}-{date.today().month:02d}",
    )


@app.route("/set_principal", methods=["POST"])
@admin_required
def set_principal():
    principal = float(request.form["principal"])
    if principal <= 0:
        abort(400, "Principal must be > 0")

    db = get_db()

    # Accrue everyone to today first so we don't erase accrued interest
    today = date.today()
    people = db.execute("SELECT id FROM people").fetchall()
    for p in people:
        accrue_interest_to(db, p["id"], today)

    # Set principal
    db.execute("UPDATE settings SET value=? WHERE key='principal'", (principal,))

    # Re-split equally by setting balances to principal/4 (fresh baseline)
    # NOTE: This is the “simple” interpretation of adjustable principal.
    # It preserves payment history but resets current balances to the new share.
    share = principal / 4.0
    for p in db.execute("SELECT id FROM people").fetchall():
        db.execute("UPDATE people SET balance=?, last_calc=? WHERE id=?", (share, today.isoformat(), p["id"]))

    db.commit()
    db.close()
    return redirect(url_for("index"))


@app.route("/set_rate", methods=["POST"])
@admin_required
def set_rate():
    rate_pct = float(request.form["rate_pct"])
    if rate_pct <= 0:
        abort(400, "Rate must be > 0")
    db = get_db()
    db.execute("UPDATE settings SET value=? WHERE key='interest_rate'", (rate_pct / 100.0,))
    db.commit()
    db.close()
    return redirect(url_for("index"))


@app.route("/pay", methods=["POST"])
@admin_required
def pay():
    person_id = int(request.form["person_id"])
    amount = float(request.form["amount"])
    pay_date = datetime.strptime(request.form["pay_date"], "%Y-%m-%d").date()

    if amount <= 0:
        abort(400, "Payment must be > 0")

    db = get_db()

    # Accrue interest up to payment date for this person
    interest_added = accrue_interest_to(db, person_id, pay_date)

    # Apply payment to balance
    p = db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    new_bal = float(p["balance"]) - amount
    db.execute("UPDATE people SET balance=? WHERE id=?", (new_bal, person_id))

    # Record payment + interest that accrued since last calc
    db.execute(
        "INSERT INTO payments(person_id, amount, interest, pay_date) VALUES(?,?,?,?)",
        (person_id, amount, float(interest_added), pay_date.isoformat()),
    )

    db.commit()
    db.close()
    return redirect(url_for("index"))


@app.route("/person/<int:pid>")
@admin_required
def person_statement(pid: int):
    db = get_db()

    # Accrue to today so statement shows up-to-date balance
    accrue_interest_to(db, pid, date.today())
    db.commit()

    person = db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not person:
        abort(404)

    payments = db.execute(
        "SELECT * FROM payments WHERE person_id=? ORDER BY pay_date DESC, id DESC",
        (pid,),
    ).fetchall()

    interest_paid_total = db.execute(
        "SELECT IFNULL(SUM(interest),0) FROM payments WHERE person_id=?",
        (pid,),
    ).fetchone()[0]

    db.close()

    return render_template_string(
        """
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
          body{font-family:Arial;padding:12px}
          table{width:100%;border-collapse:collapse}
          th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
          .card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}
          a{word-break:break-word}
        </style>

        <a href="/">← Back</a>
        <h2>{{person.name}} – Statement</h2>

        <div class="card">
          <div><b>Current balance:</b> ${{'%.2f'|format(person.balance)}}</div>
          <div><b>Total interest paid (from recorded accruals):</b> ${{'%.2f'|format(interest_paid_total)}}</div>
          <div style="margin-top:8px">
            <a href="/person/{{person.id}}/pdf">Download statement PDF</a>
          </div>
        </div>

        <div class="card">
          <h3>Payment history</h3>
          <table>
            <tr><th>Date</th><th>Payment</th><th>Interest accrued since last activity</th></tr>
            {% for p in payments %}
              <tr>
                <td>{{p.pay_date}}</td>
                <td>${{'%.2f'|format(p.amount)}}</td>
                <td>${{'%.2f'|format(p.interest)}}</td>
              </tr>
            {% endfor %}
          </table>
        </div>
        """,
        person=person,
        payments=payments,
        interest_paid_total=float(interest_paid_total),
    )


def build_statement_pdf_bytes(person_name: str, balance: float, payments_rows) -> bytes:
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(buf)
    elements = [
        Paragraph(f"Loan Statement – {person_name}", styles["Heading1"]),
        Paragraph(f"Generated: {date.today().isoformat()}", styles["Normal"]),
        Spacer(1, 12),
        Paragraph(f"Current balance: ${balance:,.2f}", styles["Normal"]),
        Spacer(1, 12),
    ]

    data = [["Date", "Payment", "Interest accrued"]]
    for r in payments_rows:
        data.append([r["pay_date"], f"${float(r['amount']):,.2f}", f"${float(r['interest']):,.2f}"])

    elements.append(Table(data))
    doc.build(elements)
    return buf.getvalue()


@app.route("/person/<int:pid>/pdf")
@admin_required
def person_pdf(pid: int):
    db = get_db()
    person = db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not person:
        abort(404)

    payments = db.execute(
        "SELECT pay_date, amount, interest FROM payments WHERE person_id=? ORDER BY pay_date DESC, id DESC",
        (pid,),
    ).fetchall()
    db.close()

    pdf_bytes = build_statement_pdf_bytes(person["name"], float(person["balance"]), payments)
    filename = f"statement_{person['name'].replace(' ', '_')}.pdf"
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.route("/monthly_pdfs")
@admin_required
def monthly_pdfs_zip():
    """
    Generates PDFs for all people for a given month (YYYY-MM) and returns a ZIP download.
    This is your "backup" artifact.
    """
    month_str = request.args.get("month")
    if not month_str:
        month_str = f"{date.today().year:04d}-{date.today().month:02d}"

    # Parse year-month
    try:
        year, month = month_str.split("-")
        year = int(year)
        month = int(month)
        start = date(year, month, 1)
    except Exception:
        abort(400, "Invalid month format. Use YYYY-MM")

    # Compute end-of-month (simple safe method)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    db = get_db()
    people = db.execute("SELECT * FROM people ORDER BY id").fetchall()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for person in people:
            payments = db.execute(
                """SELECT pay_date, amount, interest
                   FROM payments
                   WHERE person_id=? AND pay_date >= ? AND pay_date < ?
                   ORDER BY pay_date DESC, id DESC""",
                (person["id"], start.isoformat(), end.isoformat()),
            ).fetchall()

            # NOTE: PDF shows current balance; monthly payment list is limited to that month.
            pdf_bytes = build_statement_pdf_bytes(person["name"], float(person["balance"]), payments)
            pdf_name = f"{person['name'].replace(' ', '_')}_Statement_{year:04d}_{month:02d}.pdf"
            zf.writestr(pdf_name, pdf_bytes)

    db.close()

    zip_buf.seek(0)
    zip_name = f"Monthly_Statements_{year:04d}_{month:02d}.zip"
    return send_file(zip_buf, mimetype="application/zip", as_attachment=True, download_name=zip_name)


if __name__ == "__main__":
    # Render provides PORT
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
