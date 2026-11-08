import os
import re
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, render_template_string, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "docutrack.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "receipts")

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("DOCUTRACK_SECRET", "docutrack-umtc-secret-key")

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}

UM_EMAIL = re.compile(
    r"^[a-z]\.[a-z]+\.\d+\.tc@umindanao\.edu\.ph$",
    re.IGNORECASE
)

PH_MOBILE = re.compile(r"^09\d{9}$")

DOCUMENT_FEES = {
    "Transcript of Records (TOR)": 500,
    "Certificate of Enrollment (COE)": 150,
    "Good Moral Certificate": 150,
    "Certificate of Grades": 200,
    "Honorable Dismissal": 300,
    "Certificate of Graduation": 200,
    "Course Description": 250,
    "Authentication": 100,
    "Other Academic Document": 150
}

STATUSES = [
    "Pending Payment",
    "Payment Verified",
    "Under Processing",
    "Ready for Pickup",
    "Released",
    "Rejected",
    "Cancelled"
]

ACTIVE_QUEUE_STATUSES = (
    "Payment Verified",
    "Under Processing"
)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def columns(connection, table):
    return {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


def migrate():
    connection = db()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT UNIQUE,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student',
            course TEXT,
            year_level TEXT,
            contact TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_type TEXT UNIQUE NOT NULL,
            amount REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            document_type TEXT NOT NULL,
            purpose TEXT,
            copies INTEGER DEFAULT 1,
            amount REAL DEFAULT 0,
            payment_method TEXT,
            reference_number TEXT,
            receipt_file TEXT,
            status TEXT DEFAULT 'Pending Payment',
            queue_number INTEGER,
            requirements TEXT,
            assigned_staff TEXT,
            registrar_notes TEXT,
            request_date TEXT,
            updated_at TEXT,
            released_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            details TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            request_id INTEGER,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(request_id) REFERENCES requests(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            old_status TEXT,
            new_status TEXT,
            remarks TEXT,
            changed_by INTEGER,
            created_at TEXT,
            FOREIGN KEY(request_id) REFERENCES requests(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS queue_counter (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_date TEXT UNIQUE,
            last_number INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assessment_ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            request_id INTEGER,
            service_quality INTEGER,
            processing_speed INTEGER,
            staff_assistance INTEGER,
            overall_satisfaction INTEGER,
            comments TEXT,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(request_id) REFERENCES requests(id)
        )
    """)

    user_additions = {
        "student_id": "TEXT",
        "course": "TEXT",
        "year_level": "TEXT",
        "contact": "TEXT",
        "created_at": "TEXT"
    }

    request_additions = {
        "purpose": "TEXT",
        "copies": "INTEGER DEFAULT 1",
        "amount": "REAL DEFAULT 0",
        "payment_method": "TEXT",
        "reference_number": "TEXT",
        "receipt_file": "TEXT",
        "status": "TEXT DEFAULT 'Pending Payment'",
        "queue_number": "INTEGER",
        "requirements": "TEXT",
        "assigned_staff": "TEXT",
        "registrar_notes": "TEXT",
        "request_date": "TEXT",
        "updated_at": "TEXT",
        "released_at": "TEXT"
    }

    for column, definition in user_additions.items():
        if column not in columns(connection, "users"):
            cursor.execute(
                f"ALTER TABLE users ADD COLUMN {column} {definition}"
            )

    for column, definition in request_additions.items():
        if column not in columns(connection, "requests"):
            cursor.execute(
                f"ALTER TABLE requests ADD COLUMN {column} {definition}"
            )

    for document, amount in DOCUMENT_FEES.items():
        cursor.execute("""
            INSERT OR IGNORE INTO fees (document_type, amount)
            VALUES (?, ?)
        """, (document, amount))

    admin_exists = cursor.execute("""
        SELECT id FROM users WHERE role = 'admin' LIMIT 1
    """).fetchone()

    if not admin_exists:
        cursor.execute("""
            INSERT INTO users
            (student_id, name, email, password, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            "ADMIN-001",
            "System Administrator",
            "registrar@umindanao.edu.ph",
            generate_password_hash("admin123"),
            "admin",
            now()
        ))

    connection.commit()
    connection.close()


def log_action(user_id, action, details=""):
    connection = db()

    user = connection.execute(
        "SELECT email FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    account_email = user["email"] if user else ""

    connection.execute("""
        INSERT INTO activity_logs
        (admin_email, user_id, action, details, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (account_email, user_id, action, details, now()))

    connection.commit()
    connection.close()


def status_change(request_id, new_status, remarks="", changed_by=None):
    connection = db()

    record = connection.execute("""
        SELECT * FROM requests WHERE id = ?
    """, (request_id,)).fetchone()

    if not record:
        connection.close()
        return False

    old_status = record["status"]

    connection.execute("""
        UPDATE requests
        SET status = ?, updated_at = ?
        WHERE id = ?
    """, (new_status, now(), request_id))

    connection.execute("""
        INSERT INTO status_history
        (request_id, old_status, new_status, remarks, changed_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        request_id,
        old_status,
        new_status,
        remarks,
        changed_by,
        now()
    ))

    connection.execute("""
        INSERT INTO notifications
        (user_id, request_id, message, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        record["user_id"],
        request_id,
        f"Your request #{request_id} status changed to {new_status}. {remarks}",
        now()
    ))

    connection.commit()
    connection.close()
    return True


def assign_queue(request_id):
    connection = db()
    record = connection.execute("""
        SELECT * FROM requests WHERE id = ?
    """, (request_id,)).fetchone()

    if not record:
        connection.close()
        return None

    queue_date = datetime.now().strftime("%Y-%m-%d")

    counter = connection.execute("""
        SELECT last_number FROM queue_counter
        WHERE queue_date = ?
    """, (queue_date,)).fetchone()

    if counter:
        next_number = counter["last_number"] + 1
        connection.execute("""
            UPDATE queue_counter
            SET last_number = ?
            WHERE queue_date = ?
        """, (next_number, queue_date))
    else:
        next_number = 1
        connection.execute("""
            INSERT INTO queue_counter (queue_date, last_number)
            VALUES (?, ?)
        """, (queue_date, next_number))

    connection.execute("""
        UPDATE requests
        SET queue_number = ?, updated_at = ?
        WHERE id = ?
    """, (next_number, now(), request_id))

    connection.commit()
    connection.close()
    return next_number



def queue_rows():
    connection = db()

    rows = connection.execute("""
        SELECT
            requests.*,
            users.first_name || ' ' || users.last_name AS name,
            users.student_id,
            users.email
        FROM requests
        JOIN users ON users.id = requests.user_id
        WHERE requests.status IN (?, ?)
        ORDER BY requests.queue_number ASC, requests.request_date ASC
    """, ACTIVE_QUEUE_STATUSES).fetchall()

    connection.close()
    return rows



def recalculate_queue():
    connection = db()
    queue_date = datetime.now().strftime("%Y-%m-%d")

    rows = connection.execute("""
        SELECT id
        FROM requests
        WHERE status IN (?, ?)
        AND substr(updated_at, 1, 10) = ?
        ORDER BY queue_number ASC, request_date ASC
    """, (*ACTIVE_QUEUE_STATUSES, queue_date)).fetchall()

    for number, row in enumerate(rows, start=1):
        connection.execute("""
            UPDATE requests SET queue_number = ?
            WHERE id = ?
        """, (number, row["id"]))

    connection.execute("""
        INSERT INTO queue_counter (queue_date, last_number)
        VALUES (?, ?)
        ON CONFLICT(queue_date)
        DO UPDATE SET last_number = excluded.last_number
    """, (queue_date, len(rows)))

    connection.commit()
    connection.close()


def interpret(value):
    if value is None:
        return "No data"

    if value >= 4.21:
        return "Strongly Agree"
    if value >= 3.41:
        return "Agree"
    if value >= 2.61:
        return "Neutral"
    if value >= 1.81:
        return "Disagree"

    return "Strongly Disagree"


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )
def login_required(role=None):
    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                if role == "admin":
                    return redirect(url_for("admin_login"))
                return redirect(url_for("login"))

            if role and session.get("role") != role:
                abort(403)

            if session.get("role") not in ("admin", "student"):
                abort(403)

            return function(*args, **kwargs)
        return wrapper
    return decorator


BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} | UMTC DocuTrack</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Arial,sans-serif;background:#f5f3f3;color:#292929}
header{background:#650019;color:white;padding:18px 5%;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap}
header h2{margin:0}
nav{display:flex;gap:10px;flex-wrap:wrap}
nav a{color:white;text-decoration:none;padding:9px 12px;border-radius:6px}
nav a:hover{background:#8b1734}
.container{max-width:1150px;margin:25px auto;padding:0 18px}
.card{background:white;padding:22px;border-radius:12px;box-shadow:0 3px 12px #00000012;margin-bottom:20px}
h1,h2,h3{color:#650019}
input,select,textarea{width:100%;padding:11px;margin:7px 0 15px;border:1px solid #ccc;border-radius:6px;font-size:15px}
button,.btn{display:inline-block;background:#75001e;color:white;border:0;padding:11px 17px;border-radius:6px;text-decoration:none;cursor:pointer;font-size:14px}
button:hover,.btn:hover{background:#97002b}
.btn-secondary{background:#777}
.btn-gold{background:#b38b32;color:#fff}
table{width:100%;border-collapse:collapse;background:white}
th,td{padding:11px;border-bottom:1px solid #ddd;text-align:left}
th{background:#650019;color:white}
.table-wrap{overflow-x:auto}
.alert{padding:12px;border-radius:6px;margin-bottom:12px;background:#fff0f0;color:#8b001e}
.success{background:#e9f8ed;color:#17652a}
.muted{color:#777}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:15px}
.stat{background:#fff;border-left:5px solid #850025;padding:18px;border-radius:8px}
.stat h2{margin:5px 0}
footer{text-align:center;color:#777;padding:25px}
</style>
</head>
<body>
<header>
<div><h2>UMTC DocuTrack</h2><small>University of Mindanao Tagum College</small></div>
<nav>
{% if session.get("role") == "admin" %}
<a href="{{ url_for('dashboard') }}">Dashboard</a>
<a href="{{ url_for('admin_requests') }}">Requests</a>
<a href="{{ url_for('admin_queue') }}">Overall Queue</a>
<a href="{{ url_for('assessment') }}">Assessment</a>
<a href="{{ url_for('logs') }}">Activity Logs</a>
<a href="{{ url_for('logout') }}">Logout</a>
{% elif session.get("role") == "student" %}
<a href="{{ url_for('dashboard') }}">Dashboard</a>
<a href="{{ url_for('new_request') }}">Request Document</a>
<a href="{{ url_for('notifications') }}">Notifications</a>
<a href="{{ url_for('logout') }}">Logout</a>
{% else %}
<a href="{{ url_for('guest') }}">Home</a>
<a href="{{ url_for('login') }}">Student Login</a>
<a href="{{ url_for('register') }}">Register</a>
{% endif %}
</nav>
</header>

<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% for category, message in messages %}
<div class="alert {{ 'success' if category == 'success' else '' }}">{{ message }}</div>
{% endfor %}
{% endwith %}
{{ content|safe }}
</div>

<footer>UMTC DocuTrack © 2026 | University of Mindanao Tagum College</footer>
</body>
</html>
"""


def page(title, content, **context):
    return render_template_string(
        BASE,
        title=title,
        content=render_template_string(content, **context),
        session=session
    )


@app.route("/")
def guest():
    if session.get("role") == "admin":
        return redirect(url_for("dashboard"))

    if session.get("role") == "student":
        return redirect(url_for("dashboard"))

    content = """
    <div class="card" style="text-align:center;padding:45px">
        <h1>UMTC DocuTrack</h1>
        <h3>Document Request and Tracking System</h3>
        <p>University of Mindanao Tagum College</p>
        <p>Submit your academic document requests and monitor their processing status.</p>
        <br>
        <a class="btn" href="{{ url_for('login') }}">Student Login</a>
<a class="btn btn-gold" href="{{ url_for('register') }}">Create Student Account</a>
<a class="btn" href="{{ url_for('admin_login') }}">Admin Login</a>
    </div>
    """
    return page("Home", content)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("role") in ("admin", "student"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        student_id = request.form.get("student_id", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        contact = request.form.get("contact", "").strip()
        course = request.form.get("course", "").strip()
        year_level = request.form.get("year_level", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        errors = []

        if not student_id:
            errors.append("Student ID is required.")

        if not name:
            errors.append("Full name is required.")

        if not UM_EMAIL.fullmatch(email):
            errors.append("Use your official UM email format.")

        if not PH_MOBILE.fullmatch(contact):
            errors.append("Enter a valid Philippine mobile number.")

        if not course:
            errors.append("Course is required.")

        if not year_level:
            errors.append("Year level is required.")

        if len(password) < 8:
            errors.append("Password must contain at least 8 characters.")

        if password != confirm_password:
            errors.append("Passwords do not match.")

        connection = db()

        duplicate = connection.execute("""
            SELECT id FROM users
            WHERE lower(email) = ? OR student_id = ?
        """, (email, student_id)).fetchone()

        if duplicate:
            errors.append("Student ID or email is already registered.")

        if errors:
            connection.close()
            for error in errors:
                flash(error, "error")
        else:
            connection.execute("""
                INSERT INTO users
                (student_id, name, email, password, role, course, year_level, contact, created_at)
                VALUES (?, ?, ?, ?, 'student', ?, ?, ?, ?)
            """, (
                student_id,
                name,
                email,
                generate_password_hash(password),
                course,
                year_level,
                contact,
                now()
            ))

            connection.commit()
            connection.close()

            flash("Registration successful. You may now log in.", "success")
            return redirect(url_for("login"))

        if connection:
            try:
                connection.close()
            except Exception:
                pass

    content = """
    <div class="card">
        <h2>Student Registration</h2>
        <form method="POST">
            <label>Student ID</label>
            <input name="student_id" required value="{{ request.form.get('student_id','') }}">

            <label>Full Name</label>
            <input name="name" required value="{{ request.form.get('name','') }}">

            <label>Official UM Email</label>
            <input type="email" name="email" required
            placeholder="a.datuin.150960.tc@umindanao.edu.ph"
            value="{{ request.form.get('email','') }}">

            <label>Contact Number</label>
            <input name="contact" required placeholder="09XXXXXXXXX"
            value="{{ request.form.get('contact','') }}">

            <label>Course</label>
            <input name="course" required value="{{ request.form.get('course','') }}">

            <label>Year Level</label>
            <select name="year_level" required>
                <option value="">Select Year Level</option>
                <option>1st Year</option>
                <option>2nd Year</option>
                <option>3rd Year</option>
                <option>4th Year</option>
                <option>5th Year</option>
            </select>

            <label>Password</label>
            <input type="password" name="password" required>

            <label>Confirm Password</label>
            <input type="password" name="confirm_password" required>

            <button type="submit">Create Account</button>
        </form>
    </div>
    """
    return page("Student Registration", content)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("role") in ("admin", "student"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not UM_EMAIL.fullmatch(email):
            flash("Please use your official UM student email.", "error")
            return redirect(url_for("login"))

        connection = db()
        user = connection.execute("""
            SELECT * FROM users
            WHERE lower(email) = ? AND role = 'student'
        """, (email,)).fetchone()
        connection.close()

        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["name"] = user["first_name"]
            session["role"] = "student"

            log_action(user["id"], "Student Login", "Student logged in")
            return redirect(url_for("dashboard"))

        flash("Invalid student email or password.", "error")

    content = """
    <div class="card" style="max-width:500px;margin:auto">
        <h2>Student Login</h2>
        <form method="POST">
            <label>Official UM Email</label>
            <input type="email" name="email" required
            placeholder="a.datuin.150960.tc@umindanao.edu.ph">

            <label>Password</label>
            <input type="password" name="password" required>

            <button type="submit">Login</button>
        </form>
        <p class="muted">Don't have an account? <a href="{{ url_for('register') }}">Register here</a>.</p>
    </div>
    """
    return page("Student Login", content)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("role") == "admin":
        return redirect(url_for("dashboard"))

    if session.get("role") == "student":
        abort(403)

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        connection = db()
        user = connection.execute("""
            SELECT * FROM users
            WHERE lower(email) = ? AND role = 'admin'
        """, (email,)).fetchone()
        connection.close()

        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["name"] = (user["first_name"] or "") + " " + (user["last_name"] or "")
            session["role"] = "admin"

            log_action(user["id"], "Admin Login", "Administrator logged in")
            return redirect(url_for("dashboard"))

        flash("Invalid administrator email or password.", "error")

    content = """
    <div class="card" style="max-width:500px;margin:auto">
        <h2>Administrator Login</h2>
        <p class="muted">Authorized registrar personnel only.</p>
        <form method="POST">
            <label>Administrator Email</label>
            <input type="email" name="email" required>

            <label>Password</label>
            <input type="password" name="password" required>

            <button type="submit">Admin Login</button>
        </form>
    </div>
    """
    return page("Administrator Login", content)


@app.route("/logout")
def logout():
    user_id = session.get("user_id")

    if user_id:
        log_action(user_id, "Logout", "User logged out")

    session.clear()
    flash("You have successfully logged out.", "success")
    return redirect(url_for("guest"))
@app.route("/dashboard")
@login_required()
def dashboard():
    user_id = session["user_id"]
    role = session.get("role")

    connection = db()

    if role == "admin":
        total_users = connection.execute("""
            SELECT COUNT(*) AS total FROM users
            WHERE role = 'student'
        """).fetchone()["total"]

        total_requests = connection.execute("""
            SELECT COUNT(*) AS total FROM requests
        """).fetchone()["total"]

        pending = connection.execute("""
            SELECT COUNT(*) AS total FROM requests
            WHERE status = 'Pending Payment'
        """).fetchone()["total"]

        processing = connection.execute("""
            SELECT COUNT(*) AS total FROM requests
            WHERE status IN (?, ?)
        """, ACTIVE_QUEUE_STATUSES).fetchone()["total"]

        recent = connection.execute("""
            SELECT requests.*, TRIM(COALESCE(users.first_name, '') || ' ' || COALESCE(users.last_name, '')) AS name, users.student_id
            FROM requests
            JOIN users ON users.id = requests.user_id
            ORDER BY requests.id DESC
            LIMIT 10
        """).fetchall()

        connection.close()

        content = """
        <h1>Administrator Dashboard</h1>
        <p>Welcome, {{ session.get('name') }}.</p>

        <div class="grid">
            <div class="stat">
                <p>Total Students</p>
                <h2>{{ total_users }}</h2>
            </div>
            <div class="stat">
                <p>Total Requests</p>
                <h2>{{ total_requests }}</h2>
            </div>
            <div class="stat">
                <p>Pending Payments</p>
                <h2>{{ pending }}</h2>
            </div>
            <div class="stat">
                <p>Active Processing</p>
                <h2>{{ processing }}</h2>
            </div>
        </div>

        <div class="card">
            <h2>Recent Document Requests</h2>
            <div class="table-wrap">
            <table>
                <tr>
                    <th>Request ID</th>
                    <th>Student</th>
                    <th>Document</th>
                    <th>Status</th>
                    <th>Action</th>
                </tr>
                {% for item in recent %}
                <tr>
                    <td>#{{ item.id }}</td>
                    <td>{{ item.name }}</td>
                    <td>{{ item.document_type }}</td>
                    <td>{{ item.status }}</td>
                    <td><a href="{{ url_for('request_detail', rid=item.id) }}">View</a></td>
                </tr>
                {% else %}
                <tr><td colspan="5">No requests found.</td></tr>
                {% endfor %}
            </table>
            </div>
        </div>
        """

        return page(
            "Administrator Dashboard",
            content,
            total_users=total_users,
            total_requests=total_requests,
            pending=pending,
            processing=processing,
            recent=recent
        )

    if role != "student":
        connection.close()
        abort(403)

    user = connection.execute("""
        SELECT * FROM users WHERE id = ?
    """, (user_id,)).fetchone()

    requests_list = connection.execute("""
        SELECT * FROM requests
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    total = len(requests_list)

    pending = connection.execute("""
        SELECT COUNT(*) AS total FROM requests
        WHERE user_id = ? AND status = 'Pending Payment'
    """, (user_id,)).fetchone()["total"]

    processing = connection.execute("""
        SELECT COUNT(*) AS total FROM requests
        WHERE user_id = ? AND status IN (?, ?)
    """, (user_id, *ACTIVE_QUEUE_STATUSES)).fetchone()["total"]

    ready = connection.execute("""
        SELECT COUNT(*) AS total FROM requests
        WHERE user_id = ? AND status = 'Ready for Pickup'
    """, (user_id,)).fetchone()["total"]

    connection.close()

    content = """
    <h1>Student Dashboard</h1>
    <p>Welcome, {{ user.name }}!</p>

    <div class="grid">
        <div class="stat">
            <p>Total Requests</p>
            <h2>{{ total }}</h2>
        </div>
        <div class="stat">
            <p>Pending Payment</p>
            <h2>{{ pending }}</h2>
        </div>
        <div class="stat">
            <p>Processing</p>
            <h2>{{ processing }}</h2>
        </div>
        <div class="stat">
            <p>Ready for Pickup</p>
            <h2>{{ ready }}</h2>
        </div>
    </div>

    <div class="card">
        <h2>My Document Requests</h2>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Request ID</th>
                <th>Document</th>
                <th>Queue Number</th>
                <th>Status</th>
                <th>Date</th>
                <th>Action</th>
            </tr>
            {% for item in requests_list %}
            <tr>
                <td>#{{ item.id }}</td>
                <td>{{ item.document_type }}</td>
                <td>{{ item.queue_number if item.queue_number else 'Not assigned' }}</td>
                <td>{{ item.status }}</td>
                <td>{{ item.request_date }}</td>
                <td><a href="{{ url_for('my_request', rid=item.id) }}">View</a></td>
            </tr>
            {% else %}
            <tr><td colspan="6">You have no document requests yet.</td></tr>
            {% endfor %}
        </table>
        </div>
        <br>
        <a class="btn" href="{{ url_for('new_request') }}">Request a Document</a>
    </div>
    """

    return page(
        "Student Dashboard",
        content,
        user=user,
        requests_list=requests_list,
        total=total,
        pending=pending,
        processing=processing,
        ready=ready
    )


@app.route("/request/new", methods=["GET", "POST"])
@login_required("student")
def new_request():
    if request.method == "POST":
        document_type = request.form.get("document_type", "").strip()
        purpose = request.form.get("purpose", "").strip()
        copies_raw = request.form.get("copies", "1").strip()

        try:
            copies = int(copies_raw)
        except ValueError:
            copies = 0

        if document_type not in DOCUMENT_FEES:
            flash("Please select a valid document type.", "error")
            return redirect(url_for("new_request"))

        if not purpose:
            flash("Please provide the purpose of your request.", "error")
            return redirect(url_for("new_request"))

        if copies < 1 or copies > 20:
            flash("Copies must be between 1 and 20.", "error")
            return redirect(url_for("new_request"))

        amount = DOCUMENT_FEES[document_type] * copies

        connection = db()
        cursor = connection.execute("""
            INSERT INTO requests
            (user_id, document_type, purpose, copies, amount,
             status, request_date, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Pending Payment', ?, ?)
        """, (
            session["user_id"],
            document_type,
            purpose,
            copies,
            amount,
            now(),
            now()
        ))

        request_id = cursor.lastrowid

        connection.execute("""
            INSERT INTO notifications
            (user_id, request_id, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            session["user_id"],
            request_id,
            f"Your request #{request_id} has been submitted. Please proceed with payment.",
            now()
        ))

        connection.commit()
        connection.close()

        log_action(
            session["user_id"],
            "Document Request",
            f"Submitted request #{request_id}"
        )

        flash("Document request submitted successfully.", "success")
        return redirect(url_for("payment", rid=request_id))

    content = """
    <div class="card">
        <h2>Request Academic Document</h2>

        <form method="POST">
            <label>Document Type</label>
            <select name="document_type" id="document_type" required>
                <option value="">Select Document</option>
                {% for document, fee in fees.items() %}
                <option value="{{ document }}">{{ document }} — ₱{{ fee }}</option>
                {% endfor %}
            </select>

            <label>Purpose of Request</label>
            <textarea name="purpose" rows="3" required></textarea>

            <label>Number of Copies</label>
            <input type="number" name="copies" min="1" max="20" value="1" required>

            <button type="submit">Submit Request</button>
        </form>
    </div>

    <div class="card">
        <h3>Document Fees</h3>
        <div class="table-wrap">
        <table>
            <tr><th>Document</th><th>Fee</th></tr>
            {% for document, fee in fees.items() %}
            <tr><td>{{ document }}</td><td>₱{{ fee }}</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>
    """

    return page(
        "Request Document",
        content,
        fees=DOCUMENT_FEES
    )


@app.route("/payment/<int:rid>", methods=["GET", "POST"])
@login_required("student")
def payment(rid):
    connection = db()

    record = connection.execute("""
        SELECT * FROM requests
        WHERE id = ? AND user_id = ?
    """, (rid, session["user_id"])).fetchone()

    connection.close()

    if not record:
        abort(404)

    if record["status"] != "Pending Payment":
        flash("This request is no longer accepting payment submissions.", "error")
        return redirect(url_for("my_request", rid=rid))

    if request.method == "POST":
        payment_method = request.form.get("payment_method", "").strip()
        reference_number = request.form.get("reference_number", "").strip()
        receipt = request.files.get("receipt")

        if payment_method != "GCash":
            flash("Please select GCash as your payment method.", "error")
            return redirect(url_for("payment", rid=rid))

        if not reference_number:
            flash("GCash reference number is required.", "error")
            return redirect(url_for("payment", rid=rid))

        if not receipt or not receipt.filename:
            flash("Please upload your payment receipt.", "error")
            return redirect(url_for("payment", rid=rid))

        if not allowed_file(receipt.filename):
            flash("Receipt must be PNG, JPG, JPEG, or PDF.", "error")
            return redirect(url_for("payment", rid=rid))

        filename = secure_filename(
            f"receipt_{rid}_{session['user_id']}_{receipt.filename}"
        )

        receipt.save(os.path.join(UPLOAD_DIR, filename))

        connection = db()
        connection.execute("""
            UPDATE requests
            SET payment_method = ?,
                reference_number = ?,
                receipt_file = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (
            payment_method,
            reference_number,
            filename,
            now(),
            rid,
            session["user_id"]
        ))

        connection.execute("""
            INSERT INTO notifications
            (user_id, request_id, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            session["user_id"],
            rid,
            "Your GCash payment information has been submitted and is awaiting verification.",
            now()
        ))

        connection.commit()
        connection.close()

        log_action(
            session["user_id"],
            "Payment Submitted",
            f"Submitted GCash payment for request #{rid}"
        )

        flash("Payment details submitted. Please wait for registrar verification.", "success")
        return redirect(url_for("my_request", rid=rid))

    content = """
    <div class="card">
        <h2>GCash Payment</h2>
        <p><strong>Request ID:</strong> #{{ record.id }}</p>
        <p><strong>Document:</strong> {{ record.document_type }}</p>
        <p><strong>Copies:</strong> {{ record.copies }}</p>
        <h2>Amount: ₱{{ "%.2f"|format(record.amount or 0) }}</h2>

        <form method="POST" enctype="multipart/form-data">
            <label>Payment Method</label>
            <select name="payment_method" required>
                <option value="GCash">GCash</option>
            </select>

            <label>GCash Reference Number</label>
            <input name="reference_number" required>

            <label>Upload Payment Receipt</label>
            <input type="file" name="receipt" accept=".png,.jpg,.jpeg,.pdf" required>

            <button type="submit">Submit Payment Information</button>
        </form>
    </div>
    """

    return page("GCash Payment", content, record=record)

@app.route("/request/<int:rid>")
@login_required("student")
def my_request(rid):
    connection = db()

    record = connection.execute("""
        SELECT * FROM requests
        WHERE id = ? AND user_id = ?
    """, (rid, session["user_id"])).fetchone()

    if not record:
        connection.close()
        abort(404)

    history = connection.execute("""
        SELECT * FROM status_history
        WHERE request_id = ?
        ORDER BY id DESC
    """, (rid,)).fetchall()

    ahead = 0
    if record["status"] in ACTIVE_QUEUE_STATUSES and record["queue_number"]:
        ahead = connection.execute("""
            SELECT COUNT(*) AS total
            FROM requests
            WHERE status IN (?, ?)
            AND queue_number < ?
            AND substr(updated_at, 1, 10) = ?
        """, (
            *ACTIVE_QUEUE_STATUSES,
            record["queue_number"],
            datetime.now().strftime("%Y-%m-%d")
        )).fetchone()["total"]

    connection.close()

    content = """
    <div class="card">
        <h2>My Document Request</h2>

        <p><strong>Request ID:</strong> #{{ record.id }}</p>
        <p><strong>Document:</strong> {{ record.document_type }}</p>
        <p><strong>Purpose:</strong> {{ record.purpose }}</p>
        <p><strong>Copies:</strong> {{ record.copies }}</p>
        <p><strong>Total Amount:</strong> ₱{{ "%.2f"|format(record.amount or 0) }}</p>
        <p><strong>Payment Method:</strong> {{ record.payment_method or 'Not submitted' }}</p>
        <p><strong>Reference Number:</strong> {{ record.reference_number or 'Not submitted' }}</p>
        <p><strong>Status:</strong> {{ record.status }}</p>
        <p><strong>Queue Number:</strong> {{ record.queue_number or 'Not assigned' }}</p>

        {% if record.status in ['Payment Verified', 'Under Processing'] and record.queue_number %}
        <div class="stat">
            <p>Your Current Queue Position</p>
            <h2>#{{ ahead + 1 }}</h2>
            <p>{{ ahead }} request(s) ahead of you.</p>
        </div>
        {% endif %}

        <p><strong>Assigned Staff:</strong> {{ record.assigned_staff or 'Not yet assigned' }}</p>
        <p><strong>Registrar Notes:</strong> {{ record.registrar_notes or 'No remarks yet' }}</p>
        <p><strong>Requirements:</strong> {{ record.requirements or 'No additional requirements' }}</p>
        <p><strong>Request Date:</strong> {{ record.request_date }}</p>
        <p><strong>Last Updated:</strong> {{ record.updated_at }}</p>

        {% if record.receipt_file %}
        <a class="btn" href="{{ url_for('receipt', rid=record.id) }}">View Payment Receipt</a>
        {% endif %}
    </div>

    <div class="card">
        <h2>Status History</h2>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Previous Status</th>
                <th>New Status</th>
                <th>Remarks</th>
                <th>Date</th>
            </tr>
            {% for item in history %}
            <tr>
                <td>{{ item.old_status or 'New Request' }}</td>
                <td>{{ item.new_status }}</td>
                <td>{{ item.remarks or '-' }}</td>
                <td>{{ item.created_at }}</td>
            </tr>
            {% else %}
            <tr><td colspan="4">No status history available.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>

    <a class="btn btn-secondary" href="{{ url_for('dashboard') }}">Back to Dashboard</a>
    """

    return page("My Request", content, record=record, history=history, ahead=ahead)


@app.route("/notifications")
@login_required("student")
def notifications():
    connection = db()

    records = connection.execute("""
        SELECT * FROM notifications
        WHERE user_id = ?
        ORDER BY id DESC
    """, (session["user_id"],)).fetchall()

    connection.execute("""
        UPDATE notifications
        SET is_read = 1
        WHERE user_id = ?
    """, (session["user_id"],))

    connection.commit()
    connection.close()

    content = """
    <div class="card">
        <h2>My Notifications</h2>

        <div class="table-wrap">
        <table>
            <tr>
                <th>Message</th>
                <th>Date</th>
                <th>Request</th>
            </tr>
            {% for item in records %}
            <tr>
                <td>{{ item.message }}</td>
                <td>{{ item.created_at }}</td>
                <td>
                    {% if item.request_id %}
                    <a href="{{ url_for('my_request', rid=item.request_id) }}">View Request</a>
                    {% else %}
                    -
                    {% endif %}
                </td>
            </tr>
            {% else %}
            <tr><td colspan="3">No notifications available.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>
    """

    return page("Notifications", content, records=records)


@app.route("/receipt/<int:rid>")
@login_required()
def receipt(rid):
    connection = db()

    record = connection.execute("""
        SELECT * FROM requests
        WHERE id = ?
    """, (rid,)).fetchone()

    connection.close()

    if not record:
        abort(404)

    if session.get("role") == "student":
        if record["user_id"] != session["user_id"]:
            abort(403)
    elif session.get("role") != "admin":
        abort(403)

    filename = record["receipt_file"]

    if not filename:
        abort(404)

    return send_from_directory(
        UPLOAD_DIR,
        filename,
        as_attachment=False
    )


@app.route("/admin/requests")
@login_required("admin")
def admin_requests():
    search = request.args.get("search", "").strip()
    selected_status = request.args.get("status", "").strip()

    connection = db()

    query = """
        SELECT requests.*, TRIM(COALESCE(users.first_name, '') || ' ' || COALESCE(users.last_name, '')) AS name, users.student_id, users.email,
        users.course, users.contact
        FROM requests
        JOIN users ON users.id = requests.user_id
        WHERE 1=1
    """

    parameters = []

    if search:
        query += """
            AND (
                (users.first_name || ' ' || users.last_name) LIKE ?
                OR users.student_id LIKE ?
                OR users.email LIKE ?
                OR CAST(requests.id AS TEXT) LIKE ?
            )
        """
        term = f"%{search}%"
        parameters.extend([term, term, term, term])

    if selected_status in STATUSES:
        query += " AND requests.status = ?"
        parameters.append(selected_status)

    query += " ORDER BY requests.id DESC"

    records = connection.execute(
        query, parameters
    ).fetchall()

    connection.close()

    content = """
    <div class="card">
        <h2>Document Request Management</h2>

        <form method="GET">
            <label>Search Student or Request ID</label>
            <input name="search" value="{{ search }}" placeholder="Search name, ID, or email">

            <label>Filter by Status</label>
            <select name="status">
                <option value="">All Statuses</option>
                {% for status in statuses %}
                <option value="{{ status }}" {% if selected_status == status %}selected{% endif %}>
                    {{ status }}
                </option>
                {% endfor %}
            </select>

            <button type="submit">Apply Filter</button>
            <a class="btn btn-secondary" href="{{ url_for('admin_requests') }}">Reset</a>
        </form>
    </div>

    <div class="card">
        <h2>All Requests ({{ records|length }})</h2>

        <div class="table-wrap">
        <table>
            <tr>
                <th>Request ID</th>
                <th>Student</th>
                <th>Student ID</th>
                <th>Document</th>
                <th>Amount</th>
                <th>Status</th>
                <th>Queue</th>
                <th>Action</th>
            </tr>

            {% for item in records %}
            <tr>
                <td>#{{ item.id }}</td>
                <td>{{ item.name }}</td>
                <td>{{ item.student_id }}</td>
                <td>{{ item.document_type }}</td>
                <td>₱{{ "%.2f"|format(item.amount or 0) }}</td>
                <td>{{ item.status }}</td>
                <td>{{ item.queue_number or '-' }}</td>
                <td><a href="{{ url_for('request_detail', rid=item.id) }}">Manage</a></td>
            </tr>
            {% else %}
            <tr><td colspan="8">No requests found.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>
    """

    return page(
        "Manage Requests",
        content,
        records=records,
        search=search,
        selected_status=selected_status,
        statuses=STATUSES
    )


@app.route("/admin/request/<int:rid>", methods=["GET", "POST"])
@login_required("admin")
def request_detail(rid):
    connection = db()

    record = connection.execute("""
        SELECT requests.*, TRIM(COALESCE(users.first_name, '') || ' ' || COALESCE(users.last_name, '')) AS name, users.student_id,
        users.email, users.contact, users.course, users.year_level
        FROM requests
        JOIN users ON users.id = requests.user_id
        WHERE requests.id = ?
    """, (rid,)).fetchone()

    if not record:
        connection.close()
        abort(404)

    if request.method == "POST":
        connection.close()
        flash("Use the registrar processing form in the next section to update this request.", "error")
        return redirect(url_for("request_detail", rid=rid))

    history = connection.execute("""
        SELECT * FROM status_history
        WHERE request_id = ?
        ORDER BY id DESC
    """, (rid,)).fetchall()

    connection.close()

    content = """
    <div class="card">
        <h2>Registrar Request Information</h2>

        <h3>Student Information</h3>
        <p><strong>Name:</strong> {{ record.name }}</p>
        <p><strong>Student ID:</strong> {{ record.student_id }}</p>
        <p><strong>UM Email:</strong> {{ record.email }}</p>
        <p><strong>Contact Number:</strong> {{ record.contact }}</p>
        <p><strong>Course:</strong> {{ record.course }}</p>
        <p><strong>Year Level:</strong> {{ record.year_level }}</p>

        <hr>

        <h3>Document Request Information</h3>
        <p><strong>Request ID:</strong> #{{ record.id }}</p>
        <p><strong>Document Type:</strong> {{ record.document_type }}</p>
        <p><strong>Purpose:</strong> {{ record.purpose }}</p>
        <p><strong>Copies:</strong> {{ record.copies }}</p>
        <p><strong>Request Date:</strong> {{ record.request_date }}</p>

        <hr>

        <h3>Payment Information</h3>
        <p><strong>Amount:</strong> ₱{{ "%.2f"|format(record.amount or 0) }}</p>
        <p><strong>Payment Method:</strong> {{ record.payment_method or 'Not submitted' }}</p>
        <p><strong>Reference Number:</strong> {{ record.reference_number or 'Not submitted' }}</p>

        {% if record.receipt_file %}
<a class="btn" href="{{ url_for('receipt', rid=record.id) }}">View Receipt</a>
{% else %}
<p class="muted">No receipt uploaded.</p>
{% endif %}

{% if record.status == 'Pending Payment' and record.receipt_file and record.reference_number %}
<form method="POST" action="{{ url_for('verify_payment', rid=record.id) }}">
    <button type="submit">Verify GCash Payment</button>
</form>
{% endif %}
    </div>

    <div class="card">
        <h2>Registrar Processing Information</h2>

        <p><strong>Current Status:</strong> {{ record.status }}</p>
        <p><strong>Queue Number:</strong> {{ record.queue_number or 'Not assigned' }}</p>
        <p><strong>Assigned Staff:</strong> {{ record.assigned_staff or 'Not assigned' }}</p>
        <p><strong>Requirements:</strong> {{ record.requirements or 'Not specified' }}</p>
        <p><strong>Registrar Notes:</strong> {{ record.registrar_notes or 'No notes' }}</p>
        <p><strong>Released Date:</strong> {{ record.released_at or 'Not yet released' }}</p>
    </div>

    <div class="card">
        <h2>Update Request</h2>

        <form method="POST" action="{{ url_for('process_request', rid=record.id) }}">
            <label>Request Status</label>
            <select name="status" required>
                {% for status in statuses %}
                <option value="{{ status }}" {% if record.status == status %}selected{% endif %}>
                    {{ status }}
                </option>
                {% endfor %}
            </select>

            <label>Assigned Registrar / Staff</label>
            <input name="assigned_staff" value="{{ record.assigned_staff or '' }}" placeholder="Enter staff name">

            <label>Requirements / Documents to Prepare</label>
            <textarea name="requirements" rows="3">{{ record.requirements or '' }}</textarea>

            <label>Registrar Notes / Processing Remarks</label>
            <textarea name="registrar_notes" rows="4">{{ record.registrar_notes or '' }}</textarea>

            <label>Remarks for Status History</label>
            <textarea name="remarks" rows="2" placeholder="Explain the update"></textarea>

            <button type="submit">Save Processing Information</button>
        </form>
    </div>

    <div class="card">
        <h2>Processing History</h2>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Previous Status</th>
                <th>New Status</th>
                <th>Remarks</th>
                <th>Date</th>
            </tr>
            {% for item in history %}
            <tr>
                <td>{{ item.old_status or 'New Request' }}</td>
                <td>{{ item.new_status }}</td>
                <td>{{ item.remarks or '-' }}</td>
                <td>{{ item.created_at }}</td>
            </tr>
            {% else %}
            <tr><td colspan="4">No processing history.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>

    <a class="btn btn-secondary" href="{{ url_for('admin_requests') }}">Back to Requests</a>
    """

    return page(
        "Request Management",
        content,
        record=record,
        history=history,
        statuses=STATUSES
    )

@app.route("/admin/process/<int:rid>", methods=["POST"])
@login_required("admin")
def process_request(rid):
    new_status = request.form.get("status", "").strip()
    assigned_staff = request.form.get("assigned_staff", "").strip()
    requirements = request.form.get("requirements", "").strip()
    registrar_notes = request.form.get("registrar_notes", "").strip()
    remarks = request.form.get("remarks", "").strip()

    if new_status not in STATUSES:
        flash("Invalid request status.", "error")
        return redirect(url_for("request_detail", rid=rid))

    connection = db()

    record = connection.execute("""
        SELECT * FROM requests WHERE id = ?
    """, (rid,)).fetchone()

    if not record:
        connection.close()
        abort(404)

    if new_status == "Payment Verified":
        if not record["reference_number"] or not record["receipt_file"]:
            connection.close()
            flash("Payment cannot be verified without a submitted reference number and receipt.", "error")
            return redirect(url_for("request_detail", rid=rid))

    old_status = record["status"]
    queue_number = record["queue_number"]
    current_time = now()
    released_at = record["released_at"]

    if new_status in ACTIVE_QUEUE_STATUSES and old_status not in ACTIVE_QUEUE_STATUSES:
        queue_date = datetime.now().strftime("%Y-%m-%d")

        counter = connection.execute("""
            SELECT last_number FROM queue_counter
            WHERE queue_date = ?
        """, (queue_date,)).fetchone()

        if counter:
            queue_number = counter["last_number"] + 1

            connection.execute("""
                UPDATE queue_counter
                SET last_number = ?
                WHERE queue_date = ?
            """, (queue_number, queue_date))
        else:
            queue_number = 1

            connection.execute("""
                INSERT INTO queue_counter (queue_date, last_number)
                VALUES (?, ?)
            """, (queue_date, queue_number))

    if new_status == "Released":
        released_at = current_time

    connection.execute("""
        UPDATE requests
        SET status = ?,
            assigned_staff = ?,
            requirements = ?,
            registrar_notes = ?,
            queue_number = ?,
            updated_at = ?,
            released_at = ?
        WHERE id = ?
    """, (
        new_status,
        assigned_staff,
        requirements,
        registrar_notes,
        queue_number,
        current_time,
        released_at,
        rid
    ))

    if old_status != new_status:
        connection.execute("""
            INSERT INTO status_history
            (request_id, old_status, new_status, remarks, changed_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            rid,
            old_status,
            new_status,
            remarks,
            session["user_id"],
            current_time
        ))

        connection.execute("""
            INSERT INTO notifications
            (user_id, request_id, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            record["user_id"],
            rid,
            f"Your document request #{rid} status changed from {old_status} to {new_status}. {remarks}",
            current_time
        ))

    connection.commit()
    connection.close()

    log_action(
        session["user_id"],
        "Registrar Processing",
        f"Updated request #{rid}: {old_status} to {new_status}"
    )

    flash("Request processing information successfully saved.", "success")
    return redirect(url_for("request_detail", rid=rid))


@app.route("/admin/verify-payment/<int:rid>", methods=["POST"])
@login_required("admin")
def verify_payment(rid):
    connection = db()

    record = connection.execute("""
        SELECT * FROM requests WHERE id = ?
    """, (rid,)).fetchone()

    connection.close()

    if not record:
        abort(404)

    if record["status"] != "Pending Payment":
        flash("This payment is no longer awaiting verification.", "error")
        return redirect(url_for("request_detail", rid=rid))

    if not record["reference_number"] or not record["receipt_file"]:
        flash("Student has not submitted complete payment information.", "error")
        return redirect(url_for("request_detail", rid=rid))

    status_value = "Payment Verified"

    with db() as connection:
        queue_date = datetime.now().strftime("%Y-%m-%d")

        counter = connection.execute("""
            SELECT last_number FROM queue_counter
            WHERE queue_date = ?
        """, (queue_date,)).fetchone()

        if counter:
            queue_number = counter["last_number"] + 1
            connection.execute("""
                UPDATE queue_counter
                SET last_number = ?
                WHERE queue_date = ?
            """, (queue_number, queue_date))
        else:
            queue_number = 1
            connection.execute("""
                INSERT INTO queue_counter (queue_date, last_number)
                VALUES (?, ?)
            """, (queue_date, queue_number))

        current_time = now()

        connection.execute("""
            UPDATE requests
            SET status = ?,
                queue_number = ?,
                updated_at = ?
            WHERE id = ?
        """, (status_value, queue_number, current_time, rid))

        connection.execute("""
            INSERT INTO status_history
            (request_id, old_status, new_status, remarks, changed_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            rid,
            record["status"],
            status_value,
            "GCash payment verified by registrar.",
            session["user_id"],
            current_time
        ))

        connection.execute("""
            INSERT INTO notifications
            (user_id, request_id, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            record["user_id"],
            rid,
            f"Your GCash payment has been verified. Your queue number is #{queue_number}.",
            current_time
        ))

    log_action(
        session["user_id"],
        "Payment Verified",
        f"Verified payment for request #{rid}. Queue #{queue_number}"
    )

    flash(f"Payment verified successfully. Queue number: #{queue_number}", "success")
    return redirect(url_for("request_detail", rid=rid))

@app.route("/admin/queue")
@login_required("admin")
def admin_queue():
    records = queue_rows()

    content = """
    <div class="card">
        <h2>Overall Student Queue</h2>
        <p class="muted">Students with verified payments and active processing requests.</p>

        <div class="table-wrap">
        <table>
            <tr>
                <th>Queue Number</th>
                <th>Student Name</th>
                <th>Student ID</th>
                <th>Document</th>
                <th>Status</th>
                <th>Assigned Staff</th>
                <th>Action</th>
            </tr>

            {% for item in records %}
            <tr>
                <td><strong>#{{ item.queue_number or '-' }}</strong></td>
                <td>{{ item.name }}</td>
                <td>{{ item.student_id }}</td>
                <td>{{ item.document_type }}</td>
                <td>{{ item.status }}</td>
                <td>{{ item.assigned_staff or 'Not assigned' }}</td>
                <td><a href="{{ url_for('request_detail', rid=item.id) }}">Manage</a></td>
            </tr>
            {% else %}
            <tr><td colspan="7">There are no active queue records.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>
    """

    return page("Overall Queue", content, records=records)


@app.route("/assessment/submit/<int:rid>", methods=["GET", "POST"])
@login_required("student")
def submit_assessment(rid):
    connection = db()

    record = connection.execute("""
        SELECT * FROM requests
        WHERE id = ? AND user_id = ?
    """, (rid, session["user_id"])).fetchone()

    if not record:
        connection.close()
        abort(404)

    if record["status"] != "Released":
        connection.close()
        flash("You can submit an assessment after your document has been released.", "error")
        return redirect(url_for("my_request", rid=rid))

    existing = connection.execute("""
        SELECT id FROM assessment_ratings
        WHERE request_id = ? AND user_id = ?
    """, (rid, session["user_id"])).fetchone()

    connection.close()

    if existing:
        flash("You have already submitted feedback for this request.", "error")
        return redirect(url_for("my_request", rid=rid))

    if request.method == "POST":
        try:
            service_quality = int(request.form.get("service_quality", 0))
            processing_speed = int(request.form.get("processing_speed", 0))
            staff_assistance = int(request.form.get("staff_assistance", 0))
            overall_satisfaction = int(request.form.get("overall_satisfaction", 0))
        except ValueError:
            flash("Please provide valid ratings.", "error")
            return redirect(url_for("submit_assessment", rid=rid))

        ratings = [
            service_quality,
            processing_speed,
            staff_assistance,
            overall_satisfaction
        ]

        if any(value < 1 or value > 5 for value in ratings):
            flash("All ratings must be between 1 and 5.", "error")
            return redirect(url_for("submit_assessment", rid=rid))

        comments = request.form.get("comments", "").strip()

        connection = db()
        connection.execute("""
            INSERT INTO assessment_ratings
            (user_id, request_id, service_quality, processing_speed,
             staff_assistance, overall_satisfaction, comments, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session["user_id"],
            rid,
            service_quality,
            processing_speed,
            staff_assistance,
            overall_satisfaction,
            comments,
            now()
        ))

        connection.commit()
        connection.close()

        log_action(
            session["user_id"],
            "Submitted Assessment",
            f"Submitted feedback for request #{rid}"
        )

        flash("Thank you for submitting your feedback!", "success")
        return redirect(url_for("my_request", rid=rid))

    content = """
    <div class="card">
        <h2>Student Service Assessment</h2>
        <p>Request #{{ record.id }} — {{ record.document_type }}</p>
        <p>Please rate your experience from 1 to 5.</p>

        <form method="POST">
            {% for field, label in [
                ('service_quality', 'Service Quality'),
                ('processing_speed', 'Processing Speed'),
                ('staff_assistance', 'Staff Assistance'),
                ('overall_satisfaction', 'Overall Satisfaction')
            ] %}
                <label>{{ label }}</label>
                <select name="{{ field }}" required>
                    <option value="">Select Rating</option>
                    <option value="5">5 - Strongly Agree</option>
                    <option value="4">4 - Agree</option>
                    <option value="3">3 - Neutral</option>
                    <option value="2">2 - Disagree</option>
                    <option value="1">1 - Strongly Disagree</option>
                </select>
            {% endfor %}

            <label>Comments and Suggestions</label>
            <textarea name="comments" rows="4"></textarea>

            <button type="submit">Submit Assessment</button>
        </form>
    </div>
    """

    return page("Service Assessment", content, record=record)


@app.route("/admin/assessment")
@login_required("admin")
def assessment():
    connection = db()

    ratings = connection.execute("""
        SELECT assessment_ratings.*,
               TRIM(COALESCE(users.first_name, '') || ' ' || COALESCE(users.last_name, '')) AS name,
               users.student_id,
               requests.document_type
        FROM assessment_ratings
        LEFT JOIN users ON users.id = assessment_ratings.user_id
        LEFT JOIN requests ON requests.id = assessment_ratings.request_id
        ORDER BY assessment_ratings.id DESC
    """).fetchall()

    summary = connection.execute("""
        SELECT
            AVG(service_quality) AS service_quality,
            AVG(processing_speed) AS processing_speed,
            AVG(staff_assistance) AS staff_assistance,
            AVG(overall_satisfaction) AS overall_satisfaction,
            COUNT(*) AS total
        FROM assessment_ratings
    """).fetchone()

    connection.close()

    content = """
    <h1>Assessment and Analytics</h1>

    <div class="grid">
        <div class="stat">
            <p>Total Responses</p>
            <h2>{{ summary.total }}</h2>
        </div>
        <div class="stat">
            <p>Service Quality</p>
            <h2>{{ "%.2f"|format(summary.service_quality or 0) }}/5</h2>
            <small>{{ interpret(summary.service_quality) }}</small>
        </div>
        <div class="stat">
            <p>Processing Speed</p>
            <h2>{{ "%.2f"|format(summary.processing_speed or 0) }}/5</h2>
            <small>{{ interpret(summary.processing_speed) }}</small>
        </div>
        <div class="stat">
            <p>Staff Assistance</p>
            <h2>{{ "%.2f"|format(summary.staff_assistance or 0) }}/5</h2>
            <small>{{ interpret(summary.staff_assistance) }}</small>
        </div>
        <div class="stat">
            <p>Overall Satisfaction</p>
            <h2>{{ "%.2f"|format(summary.overall_satisfaction or 0) }}/5</h2>
            <small>{{ interpret(summary.overall_satisfaction) }}</small>
        </div>
    </div>

    <div class="card">
        <h2>Student Feedback Records</h2>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Student</th>
                <th>Student ID</th>
                <th>Document</th>
                <th>Service</th>
                <th>Speed</th>
                <th>Staff</th>
                <th>Overall</th>
                <th>Comments</th>
                <th>Date</th>
            </tr>

            {% for item in ratings %}
            <tr>
                <td>{{ item.name or 'Unknown' }}</td>
                <td>{{ item.student_id or '-' }}</td>
                <td>{{ item.document_type or '-' }}</td>
                <td>{{ item.service_quality }}/5</td>
                <td>{{ item.processing_speed }}/5</td>
                <td>{{ item.staff_assistance }}/5</td>
                <td>{{ item.overall_satisfaction }}/5</td>
                <td>{{ item.comments or '-' }}</td>
                <td>{{ item.created_at }}</td>
            </tr>
            {% else %}
            <tr><td colspan="9">No assessment responses yet.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>
    """

    return page(
        "Assessment",
        content,
        summary=summary,
        ratings=ratings,
        interpret=interpret
    )


@app.route("/admin/logs")
@login_required("admin")
def logs():
    connection = db()

    records = connection.execute("""
        SELECT activity_logs.*,
               TRIM(COALESCE(users.first_name, '') || ' ' || COALESCE(users.last_name, '')) AS name,
               users.email,
               users.role
        FROM activity_logs
        LEFT JOIN users ON users.id = activity_logs.user_id
        ORDER BY activity_logs.id DESC
        LIMIT 500
    """).fetchall()

    connection.close()

    content = """
    <div class="card">
        <h2>System Activity Logs</h2>
        <p class="muted">Latest 500 recorded system activities.</p>

        <div class="table-wrap">
        <table>
            <tr>
                <th>User</th>
                <th>Email</th>
                <th>Role</th>
                <th>Activity</th>
                <th>Details</th>
                <th>Date and Time</th>
            </tr>

            {% for item in records %}
            <tr>
                <td>{{ item.name or 'System' }}</td>
                <td>{{ item.email or '-' }}</td>
                <td>{{ item.role or '-' }}</td>
                <td>{{ item.action }}</td>
                <td>{{ item.details }}</td>
                <td>{{ item.created_at }}</td>
            </tr>
            {% else %}
            <tr><td colspan="6">No activity logs found.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>
    """

    return page("Activity Logs", content, records=records)


@app.route("/health")
def health():
    connection = db()

    connection.execute("SELECT 1").fetchone()

    connection.close()

    return {
        "application": "UMTC DocuTrack",
        "database": "connected",
        "status": "running"
    }


if __name__ == "__main__":
    migrate()
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )


