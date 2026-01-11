import os
import io
import zipfile
import sqlite3
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask,
    render_template_string,
    request,
    redirect,
    url_for,
    session,
    send_file,
    abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, Spacer
from reportlab.lib.styles import getSampleStyleSheet


# ---------------- CONFIG ----------------
DB_FILE = "loan.db"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")  # set in Render
ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)

SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret")  # set in Render

DEFAULT_PRINCIPAL = 2_225_000.00
DEFAULT_INTEREST_RATE = 0.064  # annual as decimal (6.4%)

DEFAULT_PEOPLE = ["Person A", "Person B", "Person C", "Person D"]


app = Flask(__name__)
app.secret_key = SECRET_KEY


# ---------------- DB HELPERS ----------------
def get_db():
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    return db


def ensure_column(db, table: str, column: str, coltype: str):
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def get_setting(db, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        raise RuntimeError(f"Missing setting: {key}")
    return row["value"]


def set_setting(db, key: str, value: str):
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def init_db():
    db = get_db()

    db.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )

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
            notes TEXT,
            FOREIGN KEY(person_id) REFERENCES people(id)
        )"""
    )

    # Safe upgrades for existing DBs
    ensure_column(db, "payments", "notes", "TEXT")
    ensure_column(db, "people", "last_calc", "DATE")

    # Default settings
    if not db.execute("SELECT 1 FROM settings WHERE key='principal'").fetchone():
        set_setting(db, "principal", str(DEFAULT_PRINCIPAL))

    if not db.execute("SELECT 1 FROM settings WHERE key='interest_rate'").fetchone():
        set_setting(db, "interest_rate", str(DEFAULT_INTEREST_RATE))

    if not db.execute("SELECT 1 FROM settings WHERE key='start_date'").fetchone():
        # Anchor used for recompute after edits/deletes.
        set_setting(db, "start_date", date.today().isoformat())

    # Seed people if empty
    c = db.execute("SELECT COUNT(*) AS c FROM people").fetchone()["c"]
    if c == 0:
        principal = float(get_setting(db, "principal"))
        share = principal / 4.0
        start = get_setting(db, "start_date")
        for name in DEFAULT_PEOPLE:
            db.execute(
                "INSERT INTO people(name,balance,last_calc) VALUES(?,?,?)",
                (name, share, start),
            )

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


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, p):
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


# ---------------- INTEREST + RECOMPUTE ----------------
def daily_rate(db) -> float:
    return float(get_setting(db, "interest_rate")) / 365.0


def accrue_interest_between(balance: float, d_rate: float, from_date: date, to_date: date) -> float:
    days = (to_date - from_date).days
    if days <= 0:
        return 0.0
    return balance * d_rate * days


def recompute_all(db):
    """
    Recompute:
    - each payment's 'interest' field
    - each person's balance and last_calc
    This keeps everything correct when transactions are edited/deleted.
    """
    principal = float(get_setting(db, "principal"))
    start = datetime.strptime(get_setting(db, "start_date"), "%Y-%m-%d").date()
    d_rate = daily_rate(db)

    people = db.execute("SELECT id FROM people ORDER BY id").fetchall()
    if not people:
        return

    share = principal / float(len(people))

    # Reset everyone to equal share at start date
    for p in people:
        db.execute(
            "UPDATE people SET balance=?, last_calc=? WHERE id=?",
            (share, start.isoformat(), p["id"]),
        )

    # Apply payments chronologically
    payments = db.execute(
        "SELECT id, person_id, amount, pay_date FROM payments ORDER BY pay_date ASC, id ASC"
    ).fetchall()

    for pay in payments:
        pid = int(pay["person_id"])
        amt = float(pay["amount"])
        pay_date = datetime.strptime(pay["pay_date"], "%Y-%m-%d").date()

        person = db.execute("SELECT balance, last_calc FROM people WHERE id=?", (pid,)).fetchone()
        bal = float(person["balance"])
        last = datetime.strptime(person["last_calc"], "%Y-%m-%d").date()

        interest = accrue_interest_between(bal, d_rate, last, pay_date)
        bal = bal + interest - amt

        db.execute("UPDATE people SET balance=?, last_calc=? WHERE id=?", (bal, pay_date.isoformat(), pid))
        db.execute("UPDATE payments SET interest=? WHERE id=?", (float(interest), int(pay["id"])))

    # Accrue everyone to today for live balances
    today = date.today()
    for p in people:
        person = db.execute("SELECT balance, last_calc FROM people WHERE id=?", (p["id"],)).fetchone()
        bal = float(person["balance"])
        last = datetime.strptime(person["last_calc"], "%Y-%m-%d").date()
        interest = accrue_interest_between(bal, d_rate, last, today)
        db.execute("UPDATE people SET balance=?, last_calc=? WHERE id=?", (bal + interest, today.isoformat(), p["id"]))



# ---------------- UI ----------------
BASE_CSS = """
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{font-family:Arial;padding:12px}
  .card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}
  table{width:100%;border-collapse:collapse}
  th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
  input,select,button{width:100%;padding:10px;font-size:16px;margin:6px 0}
  .row{display:grid;grid-template-columns:1fr;gap:10px}
  @media(min-width:720px){.row{grid-template-columns:1fr 1fr}}
  a{word-break:break-word}
  .danger{background:#b00020;color:white;border:none;border-radius:10px}
  .muted{color:#666}
</style>
"""


@app.route("/")
@admin_required
def index():
    db = get_db()
    recompute_all(db)
    db.commit()

    people = db.execute("SELECT * FROM people ORDER BY id").fetchall()
    principal = float(get_setting(db, "principal"))
    rate = float(get_setting(db, "interest_rate"))
    total = sum(float(p["balance"]) for p in people)

    rows = []
    for p in people:
        interest_paid = db.execute(
            "SELECT IFNULL(SUM(interest),0) FROM payments WHERE person_id=?",
            (p["id"],),
        ).fetchone()[0]
        rows.append({**dict(p), "interest_paid": float(interest_paid)})

    db.close()

    return render_template_string(
        BASE_CSS
        + """
        <h2>Shared Loan Tracker</h2>

        <div class="card">
          <div><b>Principal:</b> ${{'%.2f'|format(principal)}}</div>
          <div><b>Interest rate:</b> {{'%.3f'|format(rate*100)}}%</div>
          <div style="margin-top:8px"><b>Total remaining balance:</b> ${{'%.2f'|format(total)}}</div>
          <div style="margin-top:8px"><a href="/logout">Logout</a></div>
        </div>

        <div class="card">
          <h3>People</h3>
          <p><a href="/people">Edit Names</a></p>
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
              <input name="notes" placeholder="Notes (optional)">
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
        principal=principal,
        rate=rate,
        total=total,
        today=date.today().isoformat(),
        month=f"{date.today().year:04d}-{date.today().month:02d}",
    )


# ---------------- People: rename ----------------
@app.route("/people")
@admin_required
def people_list():
    db = get_db()
    people = db.execute("SELECT * FROM people ORDER BY id").fetchall()
    db.close()
    return render_template_string(
        BASE_CSS
        + """
        <a href="/">← Back</a>
        <h2>Edit Names</h2>
        <div class="card">
          <table>
            <tr><th>Current Name</th><th></th></tr>
            {% for p in people %}
              <tr>
                <td>{{p.name}}</td>
                <td><a href="/people/{{p.id}}/edit">Rename</a></td>
              </tr>
            {% endfor %}
          </table>
        </div>
        """,
        people=people,
    )


@app.route("/people/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def people_edit(pid: int):
    db = get_db()
    person = db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not person:
        db.close()
        abort(404)

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            db.close()
            abort(400, "Name required")
        db.execute("UPDATE people SET name=? WHERE id=?", (new_name, pid))
        db.commit()
        db.close()
        return redirect(url_for("people_list"))

    db.close()
    return render_template_string(
        BASE_CSS
        + """
        <a href="/people">← Back</a>
        <h2>Rename Person</h2>
        <div class="card">
          <form method="post">
            <label>New name</label>
            <input name="name" value="{{person.name}}" required>
            <button type="submit">Save</button>
          </form>
        </div>
        """,
        person=person,
    )


# ---------------- Payments: add/edit/delete ----------------
@app.route("/pay", methods=["POST"])
@admin_required
def pay():
    person_id = int(request.form["person_id"])
    amount = float(request.form["amount"])
    pay_date = datetime.strptime(request.form["pay_date"], "%Y-%m-%d").date()
    notes = request.form.get("notes", "").strip()

    if amount <= 0:
        abort(400, "Payment must be > 0")

    db = get_db()
    db.execute(
        "INSERT INTO payments(person_id, amount, interest, pay_date, notes) VALUES(?,?,?,?,?)",
        (person_id, amount, 0.0, pay_date.isoformat(), notes),
    )
    recompute_all(db)
    db.commit()
    db.close()
    return redirect(url_for("index"))


@app.route("/payment/<int:pay_id>/edit", methods=["GET", "POST"])
@admin_required
def payment_edit(pay_id: int):
    db = get_db()
    pay = db.execute("SELECT * FROM payments WHERE id=?", (pay_id,)).fetchone()
    if not pay:
        db.close()
        abort(404)

    people = db.execute("SELECT id, name FROM people ORDER BY id").fetchall()

    if request.method == "POST":
        person_id = int(request.form["person_id"])
        amount = float(request.form["amount"])
        pay_date = request.form["pay_date"]
        notes = request.form.get("notes", "").strip()

        if amount <= 0:
            db.close()
            abort(400, "Payment must be > 0")

        db.execute(
            "UPDATE payments SET person_id=?, amount=?, pay_date=?, notes=? WHERE id=?",
            (person_id, amount, pay_date, notes, pay_id),
        )
        recompute_all(db)
        db.commit()
        db.close()
        return redirect(url_for("person_statement", pid=person_id))

    db.close()
    return render_template_string(
        BASE_CSS
        + """
        <a href="/person/{{pay.person_id}}">← Back</a>
        <h2>Edit Transaction</h2>

        <div class="card">
          <form method="post">
            <label>Person</label>
            <select name="person_id">
              {% for p in people %}
                <option value="{{p.id}}" {% if p.id == pay.person_id %}selected{% endif %}>{{p.name}}</option>
              {% endfor %}
            </select>

            <label>Amount</label>
            <input type="number" step="0.01" name="amount" value="{{pay.amount}}" required>

            <label>Date</label>
            <input type="date" name="pay_date" value="{{pay.pay_date}}" required>

            <label>Notes</label>
            <input name="notes" value="{{pay.notes or ''}}">

            <button type="submit">Save changes</button>
          </form>
        </div>

        <div class="card">
          <form method="post" action="/payment/{{pay.id}}/delete" onsubmit="return confirm('Delete this payment?');">
            <button class="danger" type="submit">Delete payment</button>
          </form>
        </div>
        """,
        pay=pay,
        people=people,
    )


@app.route("/payment/<int:pay_id>/delete", methods=["POST"])
@admin_required
def payment_delete(pay_id: int):
    db = get_db()
    pay = db.execute("SELECT person_id FROM payments WHERE id=?", (pay_id,)).fetchone()
    if not pay:
        db.close()
        abort(404)

    person_id = int(pay["person_id"])
    db.execute("DELETE FROM payments WHERE id=?", (pay_id,))
    recompute_all(db)
    db.commit()
    db.close()
    return redirect(url_for("person_statement", pid=person_id))


# ---------------- Statements + PDFs ----------------
@app.route("/person/<int:pid>")
@admin_required
def person_statement(pid: int):
    db = get_db()
    recompute_all(db)
    db.commit()

    person = db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not person:
        db.close()
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
        BASE_CSS
        + """
        <a href="/">← Back</a>
        <h2>{{person.name}} – Statement</h2>

        <div class="card">
          <div><b>Current balance:</b> ${{'%.2f'|format(person.balance)}}</div>
          <div><b>Total interest (recomputed):</b> ${{'%.2f'|format(interest_paid_total)}}</div>
          <div style="margin-top:8px">
            <a href="/person/{{person.id}}/pdf">Download statement PDF</a>
          </div>
        </div>

        <div class="card">
          <h3>Payment history</h3>
          <div class="muted">Edit/delete any payment. Balances recalc automatically.</div>
          <table>
            <tr><th>Date</th><th>Payment</th><th>Interest since last activity</th><th>Notes</th><th></th></tr>
            {% for p in payments %}
              <tr>
                <td>{{p.pay_date}}</td>
                <td>${{'%.2f'|format(p.amount)}}</td>
                <td>${{'%.2f'|format(p.interest)}}</td>
                <td>{{p.notes or ''}}</td>
                <td><a href="/payment/{{p.id}}/edit">Edit</a></td>
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

    data = [["Date", "Payment", "Interest since last activity", "Notes"]]
    for r in payments_rows:
        data.append(
            [
                r["pay_date"],
                f"${float(r['amount']):,.2f}",
                f"${float(r['interest']):,.2f}",
                (r["notes"] or "")[:60],
            ]
        )

    elements.append(Table(data))
    doc.build(elements)
    return buf.getvalue()


@app.route("/person/<int:pid>/pdf")
@admin_required
def person_pdf(pid: int):
    db = get_db()
    recompute_all(db)
    db.commit()

    person = db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not person:
        db.close()
        abort(404)

    payments = db.execute(
        "SELECT pay_date, amount, interest, notes FROM payments WHERE person_id=? ORDER BY pay_date DESC, id DESC",
        (pid,),
    ).fetchall()
    db.close()

    pdf_bytes = build_statement_pdf_bytes(person["name"], float(person["balance"]), payments)
    filename = f"statement_{person['name'].replace(' ', '_')}.pdf"
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.route("/monthly_pdfs")
@admin_required
def monthly_pdfs_zip():
    month_str = request.args.get("month") or f"{date.today().year:04d}-{date.today().month:02d}"

    try:
        y, m = month_str.split("-")
        y = int(y)
        m = int(m)
        start = date(y, m, 1)
    except Exception:
        abort(400, "Invalid month format. Use YYYY-MM")

    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)

    db = get_db()
    recompute_all(db)
    db.commit()

    people = db.execute("SELECT * FROM people ORDER BY id").fetchall()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for person in people:
            payments = db.execute(
                """SELECT pay_date, amount, interest, notes
                   FROM payments
                   WHERE person_id=? AND pay_date >= ? AND pay_date < ?
                   ORDER BY pay_date DESC, id DESC""",
                (person["id"], start.isoformat(), end.isoformat()),
            ).fetchall()

            pdf_bytes = build_statement_pdf_bytes(person["name"], float(person["balance"]), payments)
            pdf_name = f"{person['name'].replace(' ', '_')}_Statement_{y:04d}_{m:02d}.pdf"
            zf.writestr(pdf_name, pdf_bytes)

    db.close()

    zip_buf.seek(0)
    zip_name = f"Monthly_Statements_{y:04d}_{m:02d}.zip"
    return send_file(zip_buf, mimetype="application/zip", as_attachment=True, download_name=zip_name)


# ---------------- Settings updates ----------------
@app.route("/set_principal", methods=["POST"])
@admin_required
def set_principal():
    principal = float(request.form["principal"])
    if principal <= 0:
        abort(400, "Principal must be > 0")

    db = get_db()
    set_setting(db, "principal", str(principal))
    recompute_all(db)
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
    set_setting(db, "interest_rate", str(rate_pct / 100.0))
    recompute_all(db)
    db.commit()
    db.close()
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
