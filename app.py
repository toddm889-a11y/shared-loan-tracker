import os
import io
import zipfile
import hmac
from datetime import date, datetime
from functools import wraps
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import psycopg2
import psycopg2.extras

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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, Spacer
from reportlab.lib.styles import getSampleStyleSheet


# ---------------- CONFIG ----------------
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

DEFAULT_PRINCIPAL = 2_225_000.00
DEFAULT_INTEREST_RATE = 0.064  # annual as decimal
DEFAULT_PEOPLE = ["Person A", "Person B", "Person C", "Person D"]


app = Flask(__name__)
app.secret_key = SECRET_KEY


# ---------------- DB (Postgres) ----------------
def _with_sslmode(url: str) -> str:
    """Ensure sslmode is set. Supabase typically requires SSL."""
    if not url:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    if "sslmode" not in q:
        q["sslmode"] = "require"
        u = u._replace(query=urlencode(q))
    return urlunparse(u)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL env var in Render.")
    return psycopg2.connect(_with_sslmode(DATABASE_URL))


def db_exec(sql: str, params=None, fetch="none"):
    """
    fetch: 'none' | 'one' | 'all'
    Returns dict rows when fetching.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None


def get_setting(key: str) -> str:
    row = db_exec("SELECT value FROM settings WHERE key=%s;", (key,), fetch="one")
    if not row:
        raise RuntimeError(f"Missing setting: {key}")
    return row["value"]


def set_setting(key: str, value: str):
    db_exec(
        """
        INSERT INTO settings(key, value)
        VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;
        """,
        (key, value),
    )


def init_db():
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    db_exec(
        """
        CREATE TABLE IF NOT EXISTS people (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            balance NUMERIC NOT NULL,
            last_calc DATE NOT NULL
        );
        """
    )

    db_exec(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            amount NUMERIC NOT NULL,
            interest NUMERIC NOT NULL,
            pay_date DATE NOT NULL,
            notes TEXT
        );
        """
    )

    # Defaults (only if missing)
    db_exec(
        """
        INSERT INTO settings(key, value)
        VALUES ('principal', %s)
        ON CONFLICT (key) DO NOTHING;
        """,
        (str(DEFAULT_PRINCIPAL),),
    )

    db_exec(
        """
        INSERT INTO settings(key, value)
        VALUES ('interest_rate', %s)
        ON CONFLICT (key) DO NOTHING;
        """,
        (str(DEFAULT_INTEREST_RATE),),
    )

    db_exec(
        """
        INSERT INTO settings(key, value)
        VALUES ('start_date', %s)
        ON CONFLICT (key) DO NOTHING;
        """,
        (date.today().isoformat(),),
    )

    # Seed people if empty
    cnt = db_exec("SELECT COUNT(*) AS c FROM people;", fetch="one")["c"]
    if int(cnt) == 0:
        principal = float(get_setting("principal"))
        share = principal / 4.0
        start = get_setting("start_date")
        for nm in DEFAULT_PEOPLE:
            db_exec("INSERT INTO people(name, balance, last_calc) VALUES (%s, %s, %s);", (nm, share, start))


def daily_rate() -> float:
    return float(get_setting("interest_rate")) / 365.0


def accrue_interest_between(balance: float, d_rate: float, from_date: date, to_date: date) -> float:
    days = (to_date - from_date).days
    if days <= 0:
        return 0.0
    return balance * d_rate * days


def earliest_payment_date():
    row = db_exec("SELECT MIN(pay_date) AS d FROM payments;", fetch="one")
    return row["d"]  # may be None


def recompute_all():
    """
    Recompute balances + per-payment interest from start_date to today.
    Ensures edits/deletes stay mathematically correct.
    """
    principal = float(get_setting("principal"))
    start = datetime.strptime(get_setting("start_date"), "%Y-%m-%d").date()
    d_rate = daily_rate()

    people = db_exec("SELECT id FROM people ORDER BY id;", fetch="all")
    if not people:
        return

    share = principal / float(len(people))

    # Reset each person to equal share at the loan start date
    for p in people:
        db_exec("UPDATE people SET balance=%s, last_calc=%s WHERE id=%s;", (share, start.isoformat(), p["id"]))

    # Apply payments in time order
    payments = db_exec(
        "SELECT id, person_id, amount, pay_date FROM payments ORDER BY pay_date ASC, id ASC;",
        fetch="all",
    )

    for pay in payments:
        pid = int(pay["person_id"])
        amt = float(pay["amount"])
        pay_date = pay["pay_date"]  # date object

        person = db_exec("SELECT balance, last_calc FROM people WHERE id=%s;", (pid,), fetch="one")
        bal = float(person["balance"])
        last = person["last_calc"]

        interest = accrue_interest_between(bal, d_rate, last, pay_date)
        bal = bal + interest - amt

        db_exec("UPDATE people SET balance=%s, last_calc=%s WHERE id=%s;", (bal, pay_date.isoformat(), pid))
        db_exec("UPDATE payments SET interest=%s WHERE id=%s;", (interest, int(pay["id"])))

    # Accrue everyone up to today for live balances
    today = date.today()
    for p in people:
        person = db_exec("SELECT balance, last_calc FROM people WHERE id=%s;", (p["id"],), fetch="one")
        bal = float(person["balance"])
        last = person["last_calc"]
        interest = accrue_interest_between(bal, d_rate, last, today)
        db_exec("UPDATE people SET balance=%s, last_calc=%s WHERE id=%s;", (bal + interest, today.isoformat(), p["id"]))


# Init DB at startup
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
        if u == ADMIN_USERNAME and hmac.compare_digest(p, ADMIN_PASSWORD):
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
    recompute_all()

    people = db_exec("SELECT * FROM people ORDER BY id;", fetch="all")
    principal = float(get_setting("principal"))
    rate = float(get_setting("interest_rate"))
    start_date = get_setting("start_date")
    total = sum(float(p["balance"]) for p in people)

    rows = []
    for p in people:
        interest_paid = db_exec(
            "SELECT COALESCE(SUM(interest),0) AS s FROM payments WHERE person_id=%s;",
            (p["id"],),
            fetch="one",
        )["s"]
        rows.append({**p, "interest_paid": float(interest_paid)})

    return render_template_string(
        BASE_CSS
        + """
        <h2>Shared Loan Tracker</h2>

        <div class="card">
          <div><b>Principal:</b> ${{'%.2f'|format(principal)}}</div>
          <div><b>Interest rate:</b> {{'%.3f'|format(rate*100)}}%</div>
          <div><b>Loan start date (interest begins):</b> {{start_date}}</div>
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

            <form method="post" action="/set_start_date">
              <label><b>Set loan start date</b> (interest begins)</label>
              <input type="date" name="start_date" value="{{start_date}}" required>
              <button type="submit">Update start date</button>
              <div class="muted">Changing this recalculates all balances.</div>
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
        start_date=start_date,
        total=total,
        today=date.today().isoformat(),
        month=f"{date.today().year:04d}-{date.today().month:02d}",
    )


# ---------------- Start date setting ----------------
@app.route("/set_start_date", methods=["POST"])
@admin_required
def set_start_date():
    new_start = request.form.get("start_date", "").strip()
    try:
        new_date = datetime.strptime(new_start, "%Y-%m-%d").date()
    except Exception:
        abort(400, "Invalid start date")

    ep = earliest_payment_date()
    if ep is not None and new_date > ep:
        abort(400, f"Start date cannot be after earliest payment date ({ep}).")

    set_setting("start_date", new_date.isoformat())
    recompute_all()
    return redirect(url_for("index"))


# ---------------- People: rename ----------------
@app.route("/people")
@admin_required
def people_list():
    people = db_exec("SELECT * FROM people ORDER BY id;", fetch="all")
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
    person = db_exec("SELECT * FROM people WHERE id=%s;", (pid,), fetch="one")
    if not person:
        abort(404)

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            abort(400, "Name required")
        db_exec("UPDATE people SET name=%s WHERE id=%s;", (new_name, pid))
        return redirect(url_for("people_list"))

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

    start = datetime.strptime(get_setting("start_date"), "%Y-%m-%d").date()
    if pay_date < start:
        abort(400, f"Payment date cannot be before loan start date ({start}).")

    db_exec(
        "INSERT INTO payments(person_id, amount, interest, pay_date, notes) VALUES(%s,%s,%s,%s,%s);",
        (person_id, amount, 0.0, pay_date.isoformat(), notes),
    )
    recompute_all()
    return redirect(url_for("index"))


@app.route("/payment/<int:pay_id>/edit", methods=["GET", "POST"])
@admin_required
def payment_edit(pay_id: int):
    pay = db_exec("SELECT * FROM payments WHERE id=%s;", (pay_id,), fetch="one")
    if not pay:
        abort(404)

    people = db_exec("SELECT id, name FROM people ORDER BY id;", fetch="all")
    start = datetime.strptime(get_setting("start_date"), "%Y-%m-%d").date()

    if request.method == "POST":
        person_id = int(request.form["person_id"])
        amount = float(request.form["amount"])
        pay_date = datetime.strptime(request.form["pay_date"], "%Y-%m-%d").date()
        notes = request.form.get("notes", "").strip()

        if amount <= 0:
            abort(400, "Payment must be > 0")
        if pay_date < start:
            abort(400, f"Payment date cannot be before loan start date ({start}).")

        db_exec(
            "UPDATE payments SET person_id=%s, amount=%s, pay_date=%s, notes=%s WHERE id=%s;",
            (person_id, amount, pay_date.isoformat(), notes, pay_id),
        )
        recompute_all()
        return redirect(url_for("person_statement", pid=person_id))

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
          <div class="muted">Loan start date is {{start}} (payments cannot be before this).</div>
        </div>

        <div class="card">
          <form method="post" action="/payment/{{pay.id}}/delete" onsubmit="return confirm('Delete this payment?');">
            <button class="danger" type="submit">Delete payment</button>
          </form>
        </div>
        """,
        pay=pay,
        people=people,
        start=start.isoformat(),
    )


@app.route("/payment/<int:pay_id>/delete", methods=["POST"])
@admin_required
def payment_delete(pay_id: int):
    pay = db_exec("SELECT person_id FROM payments WHERE id=%s;", (pay_id,), fetch="one")
    if not pay:
        abort(404)

    person_id = int(pay["person_id"])
    db_exec("DELETE FROM payments WHERE id=%s;", (pay_id,))
    recompute_all()
    return redirect(url_for("person_statement", pid=person_id))


# ---------------- Statements + PDFs ----------------
@app.route("/person/<int:pid>")
@admin_required
def person_statement(pid: int):
    recompute_all()

    person = db_exec("SELECT * FROM people WHERE id=%s;", (pid,), fetch="one")
    if not person:
        abort(404)

    payments = db_exec(
        "SELECT * FROM payments WHERE person_id=%s ORDER BY pay_date DESC, id DESC;",
        (pid,),
        fetch="all",
    )

    interest_paid_total = db_exec(
        "SELECT COALESCE(SUM(interest),0) AS s FROM payments WHERE person_id=%s;",
        (pid,),
        fetch="one",
    )["s"]

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
                str(r["pay_date"]),
                f"${float(r['amount']):,.2f}",
                f"${float(r['interest']):,.2f}",
                (r.get("notes") or "")[:60],
            ]
        )

    elements.append(Table(data))
    doc.build(elements)
    return buf.getvalue()


@app.route("/person/<int:pid>/pdf")
@admin_required
def person_pdf(pid: int):
    recompute_all()

    person = db_exec("SELECT * FROM people WHERE id=%s;", (pid,), fetch="one")
    if not person:
        abort(404)

    payments = db_exec(
        "SELECT pay_date, amount, interest, notes FROM payments WHERE person_id=%s ORDER BY pay_date DESC, id DESC;",
        (pid,),
        fetch="all",
    )

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

    recompute_all()
    people = db_exec("SELECT * FROM people ORDER BY id;", fetch="all")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for person in people:
            payments = db_exec(
                """
                SELECT pay_date, amount, interest, notes
                FROM payments
                WHERE person_id=%s AND pay_date >= %s AND pay_date < %s
                ORDER BY pay_date DESC, id DESC;
                """,
                (person["id"], start.isoformat(), end.isoformat()),
                fetch="all",
            )

            pdf_bytes = build_statement_pdf_bytes(person["name"], float(person["balance"]), payments)
            pdf_name = f"{person['name'].replace(' ', '_')}_Statement_{y:04d}_{m:02d}.pdf"
            zf.writestr(pdf_name, pdf_bytes)

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
    set_setting("principal", str(principal))
    recompute_all()
    return redirect(url_for("index"))


@app.route("/set_rate", methods=["POST"])
@admin_required
def set_rate():
    rate_pct = float(request.form["rate_pct"])
    if rate_pct <= 0:
        abort(400, "Rate must be > 0")
    set_setting("interest_rate", str(rate_pct / 100.0))
    recompute_all()
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
