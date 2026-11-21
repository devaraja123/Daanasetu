import os, sqlite3, secrets, math, smtplib, ssl, hashlib, json
from datetime import datetime, timedelta
from email.message import EmailMessage
from dotenv import load_dotenv
import requests
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DB_PATH = os.path.join(os.getenv("DAANASETU_DATA_DIR", BASE_DIR), "daanasetu.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-development-secret")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
CATEGORIES = ["Clothes", "Books", "Food (non-perishable)", "Furniture", "Electronics", "Educational Supplies", "Household Items"]
CONDITIONS = ["New", "Like New", "Good", "Usable"]
URGENCIES = ["Low", "Medium", "High", "Critical"]

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS pending_registrations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
          payload TEXT NOT NULL, otp_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, last_sent_at TEXT NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
          password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('donor','ngo','volunteer','admin')),
          phone TEXT, area TEXT, address TEXT, latitude REAL, longitude REAL, profile_photo TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ngos(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE NOT NULL,
          organization_name TEXT NOT NULL, registration_no TEXT NOT NULL,
          description TEXT, verification TEXT DEFAULT 'Pending',
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS donations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, donor_id INTEGER NOT NULL, title TEXT NOT NULL,
          category TEXT NOT NULL, description TEXT, quantity REAL NOT NULL, unit TEXT NOT NULL,
          condition TEXT NOT NULL, area TEXT NOT NULL, available_until TEXT, image TEXT,
          latitude REAL, longitude REAL, status TEXT DEFAULT 'Available', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(donor_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS requirements(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ngo_id INTEGER NOT NULL, title TEXT NOT NULL,
          category TEXT NOT NULL, description TEXT, quantity REAL NOT NULL, unit TEXT NOT NULL,
          urgency TEXT NOT NULL, acceptable_condition TEXT NOT NULL, area TEXT NOT NULL,
          needed_by TEXT, latitude REAL, longitude REAL, status TEXT DEFAULT 'Open', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(ngo_id) REFERENCES ngos(id)
        );
        CREATE TABLE IF NOT EXISTS matches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, donation_id INTEGER NOT NULL,
          requirement_id INTEGER NOT NULL, score REAL NOT NULL, reasons TEXT NOT NULL,
          status TEXT DEFAULT 'Suggested', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(donation_id, requirement_id),
          FOREIGN KEY(donation_id) REFERENCES donations(id),
          FOREIGN KEY(requirement_id) REFERENCES requirements(id)
        );
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL,
          volunteer_id INTEGER, pickup_date TEXT, notes TEXT, status TEXT DEFAULT 'Pending',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(match_id) REFERENCES matches(id),
          FOREIGN KEY(volunteer_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS pickup_claims(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          donation_id INTEGER NOT NULL,
          claimant_id INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'Accepted',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP, otp_hash TEXT, pickup_date TEXT, claimant_confirmed_at TEXT, donor_confirmed_at TEXT, cancellation_reason TEXT,
          FOREIGN KEY(donation_id) REFERENCES donations(id),
          FOREIGN KEY(claimant_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          message TEXT NOT NULL, is_read INTEGER DEFAULT 0,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS audit_logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL,
          details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS notification_preferences(
          user_id INTEGER PRIMARY KEY, in_app INTEGER DEFAULT 1, email INTEGER DEFAULT 1, sms INTEGER DEFAULT 1,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS notification_delivery_logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel TEXT NOT NULL,
          status TEXT NOT NULL, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS password_resets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
          expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS feedback(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          claim_id INTEGER, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
          comment TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(claim_id) REFERENCES pickup_claims(id),
          UNIQUE(user_id,claim_id)
        );
        CREATE TABLE IF NOT EXISTS pickup_disputes(
          id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id INTEGER NOT NULL, reporter_id INTEGER NOT NULL,
          reason TEXT NOT NULL, status TEXT DEFAULT 'Open', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(claim_id) REFERENCES pickup_claims(id), FOREIGN KEY(reporter_id) REFERENCES users(id)
        );
        """)
        # Migrate the original one-claim-ever table into a reusable claim history.
        # A partial unique index allows only one active pickup per donation while
        # allowing a cancelled donation to be accepted again later.
        claim_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='pickup_claims'").fetchone()
        if claim_sql and 'UNIQUE' in (claim_sql[0] or '').upper():
            con.execute("""CREATE TABLE pickup_claims_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT, donation_id INTEGER NOT NULL,
                claimant_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'Accepted',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(donation_id) REFERENCES donations(id),
                FOREIGN KEY(claimant_id) REFERENCES users(id))""")
            con.execute("""INSERT INTO pickup_claims_new(id,donation_id,claimant_id,status,created_at,updated_at)
                SELECT id,donation_id,claimant_id,status,created_at,updated_at FROM pickup_claims""")
            con.execute("DROP TABLE pickup_claims")
            con.execute("ALTER TABLE pickup_claims_new RENAME TO pickup_claims")
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_pickup_per_donation
            ON pickup_claims(donation_id)
            WHERE status IN ('Accepted','Pickup in Progress','Collected')""")

        # Safe schema migration for databases created by earlier DaanaSetu versions.
        for table, columns in {
            "users": ["latitude REAL", "longitude REAL", "address TEXT", "profile_photo TEXT"],
            "donations": ["latitude REAL", "longitude REAL"],
            "requirements": ["latitude REAL", "longitude REAL"],
            "pickup_claims": ["otp_hash TEXT", "pickup_date TEXT", "claimant_confirmed_at TEXT", "donor_confirmed_at TEXT", "cancellation_reason TEXT"]
        }.items():
            existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for definition in columns:
                col = definition.split()[0]
                if col not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

        if os.getenv("ENABLE_DEMO_DATA", "false").lower() == "true":
            admin = con.execute("SELECT id FROM users WHERE email='admin@daanasetu.local'").fetchone()
            if not admin:
                con.execute("INSERT INTO users(name,email,password,role,phone,area) VALUES(?,?,?,?,?,?)",
                            ("DaanaSetu Admin","admin@daanasetu.local",generate_password_hash("Admin@123"),"admin","0000000000","Bengaluru"))
            # Demo NGO, donor and volunteer for immediate exploration
            demo = [
              ("Demo Donor","donor@demo.com","Donor@123","donor","9000000001","Bengaluru"),
              ("Demo NGO","ngo@demo.com","Ngo@123","ngo","9000000002","Bengaluru"),
              ("Demo Volunteer","volunteer@demo.com","Volunteer@123","volunteer","9000000003","Bengaluru")
            ]
            for name,email,pw,role,phone,area in demo:
                if not con.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone():
                    cur=con.execute("INSERT INTO users(name,email,password,role,phone,area) VALUES(?,?,?,?,?,?)",
                        (name,email,generate_password_hash(pw),role,phone,area))
                    if role=="ngo":
                        con.execute("INSERT INTO ngos(user_id,organization_name,registration_no,description,verification) VALUES(?,?,?,?,?)",
                            (cur.lastrowid,"Demo Community Foundation","DEMO-REG-001","Sample NGO for local testing","Verified"))
            con.execute("INSERT OR IGNORE INTO notification_preferences(user_id) SELECT id FROM users")
            if con.execute("SELECT COUNT(*) FROM donations").fetchone()[0] == 0:
                donor=con.execute("SELECT id FROM users WHERE email='donor@demo.com'").fetchone()["id"]
                con.execute("""INSERT INTO donations(donor_id,title,category,description,quantity,unit,condition,area,available_until)
                             VALUES(?,?,?,?,?,?,?,?,?)""",
                            (donor,"School books bundle","Books","Clean school textbooks for redistribution",30,"pieces","Good","Bengaluru","2027-01-31"))
            if con.execute("SELECT COUNT(*) FROM requirements").fetchone()[0] == 0:
                ngo=con.execute("SELECT id FROM ngos WHERE organization_name='Demo Community Foundation'").fetchone()
                if ngo:
                    con.execute("""INSERT INTO requirements(ngo_id,title,category,description,quantity,unit,urgency,acceptable_condition,area,needed_by)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                (ngo["id"],"Books for students","Books","Books required for students",25,"pieces","High","Good","Bengaluru","2026-12-15"))

def current_user():
    if "user_id" not in session: return None
    with db() as con:
        return con.execute("SELECT * FROM users WHERE id=?",(session["user_id"],)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        if not current_user():
            flash("Please sign in to continue.","warning")
            return redirect(url_for("login"))
        return fn(*args,**kwargs)
    return wrapper

def roles(*allowed):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args,**kwargs):
            user=current_user()
            if not user: return redirect(url_for("login"))
            if user["role"] not in allowed: abort(403)
            return fn(*args,**kwargs)
        return wrapper
    return decorator

def send_email(to_email, subject, body):
    """Send email only when SMTP settings are configured; return a truthful result."""
    host = os.getenv("SMTP_HOST")
    if not host or not to_email:
        return False, "Email not configured or recipient email missing"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", ""))
    msg["To"] = to_email
    msg.set_content(body)
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        if os.getenv("SMTP_USE_SSL", "false").lower() == "true":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as server:
                if os.getenv("SMTP_USER"): server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                if os.getenv("SMTP_USE_TLS", "true").lower() == "true": server.starttls(context=ssl.create_default_context())
                if os.getenv("SMTP_USER"): server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
                server.send_message(msg)
        return True, "Email sent"
    except Exception as exc:
        app.logger.exception("Email delivery failed")
        return False, f"Email failed: {exc.__class__.__name__}"

def send_sms(to_phone, body):
    """Send SMS through Twilio REST API when credentials and a verified destination exist."""
    sid, token, sender = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"), os.getenv("TWILIO_FROM_NUMBER")
    if not (sid and token and sender and to_phone):
        return False, "SMS not configured or recipient phone missing"
    phone = to_phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        return False, "Recipient phone must include country code, e.g. +91XXXXXXXXXX"
    try:
        response = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": sender, "To": phone, "Body": body[:1500]}, auth=(sid, token), timeout=20)
        response.raise_for_status()
        return True, "SMS accepted by Twilio"
    except Exception as exc:
        app.logger.exception("SMS delivery failed")
        return False, f"SMS failed: {exc.__class__.__name__}"

def notify(user_id, message, subject="DaanaSetu update", con=None):
    """Persist in-app notice and record the outcome of each configured channel."""
    own = con is None
    local = db() if own else con
    try:
        pref = local.execute("SELECT * FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
        if not pref:
            local.execute("INSERT OR IGNORE INTO notification_preferences(user_id) VALUES(?)", (user_id,))
            pref = local.execute("SELECT * FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
        user = local.execute("SELECT email,phone FROM users WHERE id=?", (user_id,)).fetchone()
        if pref and pref["in_app"]:
            local.execute("INSERT INTO notifications(user_id,message) VALUES(?,?)", (user_id, message))
        if user and pref and pref["email"] and os.getenv("ENABLE_EMAIL", "false").lower() == "true":
            ok, detail = send_email(user["email"], subject, message)
            local.execute("INSERT INTO notification_delivery_logs(user_id,channel,status,details) VALUES(?,?,?,?)", (user_id,"Email","Sent" if ok else "Failed",detail))
        if user and pref and pref["sms"] and os.getenv("ENABLE_SMS", "false").lower() == "true":
            ok, detail = send_sms(user["phone"], message)
            local.execute("INSERT INTO notification_delivery_logs(user_id,channel,status,details) VALUES(?,?,?,?)", (user_id,"SMS","Sent" if ok else "Failed",detail))
    finally:
        if own: local.close()

def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2): return None
    radius = 6371.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp, dl = math.radians(float(lat2)-float(lat1)), math.radians(float(lon2)-float(lon1))
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)), 2)

def log(action, details=""):
    user=current_user()
    with db() as con:
        con.execute("INSERT INTO audit_logs(actor_id,action,details) VALUES(?,?,?)",
                    (user["id"] if user else None,action,details))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".",1)[1].lower() in ALLOWED_EXTENSIONS

def read_coordinates():
    """Validate optional browser GPS values before storing them in SQLite."""
    raw_lat=request.form.get("latitude","").strip()
    raw_lng=request.form.get("longitude","").strip()
    if not raw_lat and not raw_lng:
        return None, None
    if not raw_lat or not raw_lng:
        raise ValueError("Share both GPS coordinates or leave both blank.")
    lat, lng=float(raw_lat), float(raw_lng)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("GPS coordinates are outside the valid range.")
    return lat, lng

def match_score(donation, requirement):
    # Eligibility checks are applied before weighted scoring.
    if donation["category"] != requirement["category"]:
        return None, "Category mismatch"
    if donation["status"] != "Available" or requirement["status"] != "Open":
        return None, "Not currently available"
    if donation["condition"] not in CONDITIONS or donation["condition"] not in ["New","Like New","Good","Usable"]:
        return None, "Invalid item condition"
    if requirement["acceptable_condition"] == "New" and donation["condition"] != "New":
        return None, "Condition does not meet NGO requirement"
    qty = min(float(donation["quantity"]) / max(float(requirement["quantity"]),1), 1.0)
    urgency = {"Low":0.50,"Medium":0.70,"High":0.90,"Critical":1.0}.get(requirement["urgency"],0.5)
    km = haversine_km(donation["latitude"], donation["longitude"], requirement["latitude"], requirement["longitude"])
    if km is not None:
        dist = max(0.0, 1.0 - min(km, 100.0) / 100.0)
        location_reason = f"GPS distance approximately {km} km"
    else:
        dist = 1.0 if donation["area"].strip().lower() == requirement["area"].strip().lower() else 0.45
        location_reason = "Same area" if dist == 1 else "Area differs; confirm logistics"
    time_fit = 0.8 if not requirement["needed_by"] or not donation["available_until"] else 1.0
    category_fit = 1.0
    score = (0.30*category_fit + 0.25*qty + 0.20*urgency + 0.15*dist + 0.10*time_fit)*100
    reasons = [f"Category matches: {donation['category']}",
               f"Quantity fit: {round(qty*100)}%",
               f"Urgency: {requirement['urgency']}",
               location_reason]
    return round(score,1), "; ".join(reasons)

def refresh_matches():
    with db() as con:
        donations=con.execute("SELECT * FROM donations WHERE status='Available'").fetchall()
        requirements=con.execute("""SELECT r.* FROM requirements r JOIN ngos n ON n.id=r.ngo_id
                                    WHERE r.status='Open' AND n.verification='Verified'""").fetchall()
        count=0
        for d in donations:
            for r in requirements:
                score,reasons=match_score(d,r)
                if score is not None:
                    con.execute("""INSERT INTO matches(donation_id,requirement_id,score,reasons)
                                   VALUES(?,?,?,?) ON CONFLICT(donation_id,requirement_id)
                                   DO UPDATE SET score=excluded.score,reasons=excluded.reasons""",
                                (d["id"],r["id"],score,reasons))
                    count+=1
        return count

@app.context_processor
def inject_globals():
    u=current_user()
    unread=0
    if u:
        with db() as con: unread=con.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",(u["id"],)).fetchone()[0]
    return {"me":u,"unread_count":unread,"categories":CATEGORIES,"conditions":CONDITIONS,"urgencies":URGENCIES}

@app.route("/")
def index():
    with db() as con:
        stats={
          "donations":con.execute("SELECT COUNT(*) FROM donations").fetchone()[0],
          "ngos":con.execute("SELECT COUNT(*) FROM ngos WHERE verification='Verified'").fetchone()[0],
          "completed":con.execute("SELECT COUNT(*) FROM matches WHERE status='Completed'").fetchone()[0],
          "needs":con.execute("SELECT COUNT(*) FROM requirements WHERE status='Open'").fetchone()[0]
        }
        latest=con.execute("""SELECT d.*,u.name donor_name FROM donations d JOIN users u ON u.id=d.donor_id
                              WHERE d.status='Available' ORDER BY d.id DESC LIMIT 6""").fetchall()
    return render_template("index.html",stats=stats,latest=latest)

def registration_code_hash(email, code):
    return hashlib.sha256((email + ":" + code + ":" + app.secret_key).encode()).hexdigest()

@app.route("/register",methods=["GET","POST"])
def register():
    if request.method == "POST":
        name=request.form.get("name","").strip(); email=request.form.get("email","").strip().lower()
        password=request.form.get("password",""); role=request.form.get("role","donor")
        phone=request.form.get("phone","").strip(); area=request.form.get("area","").strip()
        org=request.form.get("organization_name","").strip(); reg=request.form.get("registration_no","").strip()
        if role not in ("donor","ngo","volunteer"):
            flash("Invalid role.","danger")
        elif not name or not email or "@" not in email or len(password)<8 or not area:
            flash("Complete all required fields. Password must be at least 8 characters.","danger")
        elif role=="ngo" and (not org or not reg):
            flash("NGO organization name and registration number are required.","danger")
        else:
            with db() as con:
                exists=con.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone()
            if exists:
                flash("That email is already registered.","danger")
                return render_template("register.html")
            code=f"{secrets.randbelow(1000000):06d}"
            payload=json.dumps(dict(name=name,email=email,password_hash=generate_password_hash(password),role=role,
                phone=phone,area=area,organization_name=org,registration_no=reg,
                description=request.form.get("description","").strip()))
            ok,detail=send_email(email,"DaanaSetu account verification code",
                f"Your DaanaSetu registration code is {code}. It expires in 10 minutes. Do not share it.")
            if not ok:
                flash("Could not send verification email. Check Gmail SMTP settings in .env and Flask terminal.","danger")
                app.logger.warning("Registration email failed: %s",detail)
                return render_template("register.html")
            now=datetime.utcnow(); expires=(now+timedelta(minutes=10)).isoformat()
            with db() as con:
                con.execute("""INSERT INTO pending_registrations(email,payload,otp_hash,expires_at,attempts,last_sent_at)
                    VALUES(?,?,?,?,0,?) ON CONFLICT(email) DO UPDATE SET payload=excluded.payload,
                    otp_hash=excluded.otp_hash,expires_at=excluded.expires_at,attempts=0,last_sent_at=excluded.last_sent_at""",
                    (email,payload,registration_code_hash(email,code),expires,now.isoformat()))
            session["registration_email"]=email
            flash("Verification code sent to your email. Enter it within 10 minutes.","success")
            return redirect(url_for("verify_registration"))
    return render_template("register.html")

@app.route("/verify-registration",methods=["GET","POST"])
def verify_registration():
    email=session.get("registration_email")
    if not email:
        flash("Start by creating an account.","warning")
        return redirect(url_for("register"))
    if request.method=="POST":
        code=request.form.get("code","").strip()
        with db() as con:
            pending=con.execute("SELECT * FROM pending_registrations WHERE email=?",(email,)).fetchone()
            if not pending or datetime.utcnow() > datetime.fromisoformat(pending["expires_at"]):
                flash("Code expired. Please register again to receive a new code.","danger")
                return redirect(url_for("register"))
            if pending["attempts"]>=5:
                flash("Too many incorrect attempts. Please register again.","danger")
                return redirect(url_for("register"))
            if not secrets.compare_digest(registration_code_hash(email,code),pending["otp_hash"]):
                con.execute("UPDATE pending_registrations SET attempts=attempts+1 WHERE email=?",(email,))
                flash("Incorrect verification code.","danger")
                return redirect(url_for("verify_registration"))
            data=json.loads(pending["payload"])
            try:
                cur=con.execute("INSERT INTO users(name,email,password,role,phone,area) VALUES(?,?,?,?,?,?)",
                    (data["name"],email,data["password_hash"],data["role"],data["phone"],data["area"]))
                user_id=cur.lastrowid
                if data["role"]=="ngo":
                    con.execute("INSERT INTO ngos(user_id,organization_name,registration_no,description) VALUES(?,?,?,?)",
                        (user_id,data["organization_name"],data["registration_no"],data["description"]))
                con.execute("DELETE FROM pending_registrations WHERE email=?",(email,))
            except sqlite3.IntegrityError:
                flash("This email already has an account. Please log in.","danger")
                return redirect(url_for("login"))
        session.pop("registration_email",None)
        notify(user_id,"Welcome to DaanaSetu. Your email is verified and account is active.","Welcome to DaanaSetu")
        flash("Email verified! Your account is now created. Please log in.","success")
        return redirect(url_for("login"))
    return render_template("verify_registration.html",email=email)

@app.route("/resend-registration-code",methods=["POST"])
def resend_registration_code():
    email=session.get("registration_email")
    if not email: return redirect(url_for("register"))
    with db() as con:
        pending=con.execute("SELECT * FROM pending_registrations WHERE email=?",(email,)).fetchone()
        if not pending: return redirect(url_for("register"))
        if (datetime.utcnow()-datetime.fromisoformat(pending["last_sent_at"])).total_seconds()<60:
            flash("Wait at least 60 seconds before requesting another code.","warning")
            return redirect(url_for("verify_registration"))
        code=f"{secrets.randbelow(1000000):06d}"
        ok,detail=send_email(email,"DaanaSetu new verification code",
            f"Your new DaanaSetu registration code is {code}. It expires in 10 minutes. Do not share it.")
        if ok:
            now=datetime.utcnow()
            con.execute("UPDATE pending_registrations SET otp_hash=?,expires_at=?,attempts=0,last_sent_at=? WHERE email=?",
                (registration_code_hash(email,code),(now+timedelta(minutes=10)).isoformat(),now.isoformat(),email))
            flash("New code sent to your email.","success")
        else:
            app.logger.warning("Resend registration email failed: %s",detail)
            flash("Email delivery failed. Check SMTP settings.","danger")
    return redirect(url_for("verify_registration"))

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=request.form.get("email","").lower().strip(); password=request.form.get("password","")
        with db() as con: user=con.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
        if user and check_password_hash(user["password"],password):
            session.clear(); session["user_id"]=user["id"]
            flash(f"Welcome, {user['name']}!","success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.","danger")
    return render_template("login.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    dev_link = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with db() as con:
            user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user:
                # Invalidate earlier unused links so only the newest reset request remains valid.
                con.execute("UPDATE password_resets SET used_at=CURRENT_TIMESTAMP WHERE user_id=? AND used_at IS NULL", (user["id"],))
                raw = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(raw.encode()).hexdigest()
                expiry = (datetime.utcnow() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
                con.execute("INSERT INTO password_resets(user_id,token_hash,expires_at) VALUES(?,?,?)", (user["id"],token_hash,expiry))
                link = url_for("reset_password", token=raw, _external=True)
                if os.getenv("ENABLE_EMAIL", "false").lower() == "true":
                    ok, detail = send_email(user["email"], "Reset your DaanaSetu password", f"Use this link within 30 minutes to reset your password:\n{link}")
                    con.execute("INSERT INTO notification_delivery_logs(user_id,channel,status,details) VALUES(?,?,?,?)", (user["id"],"Password reset email","Sent" if ok else "Failed",detail))
                    if not ok and os.getenv("APP_ENV", "development").lower() == "development":
                        dev_link = link
                elif os.getenv("APP_ENV", "development").lower() == "development":
                    dev_link = link
        flash("If that email is registered, password reset instructions have been prepared. Check your inbox.", "info")
    return render_template("forgot_password.html", dev_link=dev_link)

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as con:
        reset = con.execute("SELECT * FROM password_resets WHERE token_hash=? AND used_at IS NULL AND expires_at>=?", (token_hash,datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    if not reset:
        flash("This password reset link is invalid, expired or already used. Request a new one.", "warning")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 8 or password != confirm:
            flash("Passwords must match and contain at least 8 characters.", "danger")
        else:
            with db() as con:
                con.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(password),reset["user_id"]))
                con.execute("UPDATE password_resets SET used_at=CURRENT_TIMESTAMP WHERE user_id=? AND used_at IS NULL", (reset["user_id"],))
            flash("Password updated successfully. Please sign in.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html")

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    u=current_user()
    if request.method == "POST":
        name=request.form.get("name", "").strip(); phone=request.form.get("phone", "").strip(); area=request.form.get("area", "").strip(); address=request.form.get("address", "").strip()
        if not name or not area:
            flash("Name and city/service area are required.", "danger")
        else:
            photo=None; f=request.files.get("profile_photo")
            if f and f.filename:
                if not allowed_file(f.filename):
                    flash("Profile photo must be PNG, JPG, JPEG or WEBP.", "danger")
                    return redirect(url_for("profile"))
                photo=secrets.token_hex(8)+"_"+secure_filename(f.filename)
                f.save(os.path.join(UPLOAD_DIR,photo))
            with db() as con:
                if photo:
                    con.execute("UPDATE users SET name=?,phone=?,area=?,address=?,profile_photo=? WHERE id=?",(name,phone,area,address,photo,u["id"]))
                else:
                    con.execute("UPDATE users SET name=?,phone=?,area=?,address=? WHERE id=?",(name,phone,area,address,u["id"]))
            log("Profile updated",u["email"])
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))
    with db() as con:
        donations=con.execute("SELECT * FROM donations WHERE donor_id=? ORDER BY id DESC",(u["id"],)).fetchall()
        pickups=con.execute("SELECT pc.*,d.donor_id,d.title,d.area,d.quantity,d.unit,don.name donor_name,cl.name claimant_name FROM pickup_claims pc JOIN donations d ON d.id=pc.donation_id JOIN users don ON don.id=d.donor_id JOIN users cl ON cl.id=pc.claimant_id WHERE d.donor_id=? OR pc.claimant_id=? ORDER BY pc.id DESC",(u["id"],u["id"])).fetchall()
    return render_template("profile.html",user=u,donations=donations,pickups=pickups)

@app.route("/profile/change-password", methods=["POST"])
@login_required
def change_password():
    u=current_user(); old=request.form.get("current_password",""); new=request.form.get("new_password",""); confirm=request.form.get("confirm_password","")
    if not check_password_hash(u["password"],old): flash("Current password is incorrect.","danger")
    elif len(new)<8 or new!=confirm: flash("New passwords must match and contain at least 8 characters.","danger")
    else:
        with db() as con: con.execute("UPDATE users SET password=? WHERE id=?",(generate_password_hash(new),u["id"]))
        flash("Password changed successfully.","success")
    return redirect(url_for("profile"))

@app.route("/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notification_id):
    with db() as con: con.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",(notification_id,current_user()["id"]))
    return redirect(url_for("notifications"))

@app.route("/notifications/read-all", methods=["POST"])
@login_required
def mark_all_notifications_read():
    with db() as con: con.execute("UPDATE notifications SET is_read=1 WHERE user_id=?",(current_user()["id"],))
    flash("All notifications marked as read.","success")
    return redirect(url_for("notifications"))

@app.route("/logout")
def logout():
    session.clear(); flash("You have been signed out.","info"); return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    u=current_user()
    with db() as con:
        my_pickups=con.execute("""SELECT pc.id,pc.status,pc.created_at,d.title,d.area,d.quantity,d.unit,
            donor.name donor_name FROM pickup_claims pc
            JOIN donations d ON d.id=pc.donation_id JOIN users donor ON donor.id=d.donor_id
            WHERE pc.claimant_id=? ORDER BY pc.id DESC""",(u["id"],)).fetchall()
        if u["role"]=="donor":
            donations=con.execute("SELECT * FROM donations WHERE donor_id=? ORDER BY id DESC",(u["id"],)).fetchall()
            pickup_claims=con.execute("""SELECT pc.*,d.title,d.area,cu.name claimant_name,cu.phone claimant_phone
                FROM pickup_claims pc JOIN donations d ON d.id=pc.donation_id
                JOIN users cu ON cu.id=pc.claimant_id WHERE d.donor_id=? ORDER BY pc.id DESC""",(u["id"],)).fetchall()
            return render_template("dashboard.html",donations=donations,pickup_claims=pickup_claims,my_pickups=my_pickups)
        if u["role"]=="ngo":
            ngo=con.execute("SELECT * FROM ngos WHERE user_id=?",(u["id"],)).fetchone()
            reqs=con.execute("SELECT * FROM requirements WHERE ngo_id=? ORDER BY id DESC",(ngo["id"],)).fetchall() if ngo else []
            matches=con.execute("""SELECT m.*,d.title donation_title,d.category,d.quantity donation_qty,d.unit,
                    r.title requirement_title,r.quantity required_qty,r.unit req_unit,u.name donor_name
                    FROM matches m JOIN donations d ON d.id=m.donation_id
                    JOIN requirements r ON r.id=m.requirement_id JOIN users u ON u.id=d.donor_id
                    WHERE r.ngo_id=? ORDER BY m.score DESC""",(ngo["id"],)).fetchall() if ngo else []
            return render_template("dashboard.html",ngo=ngo,requirements=reqs,matches=matches,my_pickups=my_pickups)
        if u["role"]=="volunteer":
            return render_template("dashboard.html",my_pickups=my_pickups)
        stats={
          "users":con.execute("SELECT COUNT(*) FROM users").fetchone()[0],
          "ngos_pending":con.execute("SELECT COUNT(*) FROM ngos WHERE verification='Pending'").fetchone()[0],
          "donations":con.execute("SELECT COUNT(*) FROM donations").fetchone()[0],
          "requirements":con.execute("SELECT COUNT(*) FROM requirements WHERE status='Open'").fetchone()[0],
          "matches":con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        }
        pending=con.execute("""SELECT n.*,u.name,u.email FROM ngos n JOIN users u ON u.id=n.user_id
                               WHERE n.verification='Pending' ORDER BY n.id""").fetchall()
        return render_template("dashboard.html",stats=stats,pending=pending,my_pickups=my_pickups)

@app.route("/donations/new",methods=["GET","POST"])
@roles("donor")
def new_donation():
    if request.method=="POST":
        title=request.form.get("title","").strip(); category=request.form.get("category","")
        qty=request.form.get("quantity","0"); unit=request.form.get("unit","").strip()
        area=request.form.get("area","").strip(); condition=request.form.get("condition","")
        if not title or category not in CATEGORIES or not unit or not area or condition not in CONDITIONS:
            flash("Please provide valid donation details.","danger")
        else:
            try:
                quantity=float(qty)
                if quantity<=0: raise ValueError()
                image_name=None; f=request.files.get("image")
                if f and f.filename:
                    if not allowed_file(f.filename): flash("Only PNG, JPG, JPEG and WEBP images are allowed.","danger"); return render_template("donation_form.html")
                    image_name=secrets.token_hex(8)+"_"+secure_filename(f.filename)
                    f.save(os.path.join(UPLOAD_DIR,image_name))
                with db() as con:
                    lat, lng = read_coordinates()
                    con.execute("""INSERT INTO donations(donor_id,title,category,description,quantity,unit,condition,area,available_until,image,latitude,longitude)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (current_user()["id"],title,category,request.form.get("description",""),quantity,unit,condition,area,request.form.get("available_until") or None,image_name,lat,lng))
                log("Donation created",title); flash("Donation listed successfully.","success"); return redirect(url_for("dashboard"))
            except ValueError: flash("Quantity must be a positive number.","danger")
    return render_template("donation_form.html")

@app.route("/donation/<int:donation_id>/edit", methods=["GET", "POST"])
@login_required
def edit_donation(donation_id):
    u=current_user()
    with db() as con: d=con.execute("SELECT * FROM donations WHERE id=? AND donor_id=?",(donation_id,u["id"])).fetchone()
    if not d: abort(404)
    if d["status"] != "Available":
        flash("Only available donations can be edited.","warning")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        title=request.form.get("title","").strip(); category=request.form.get("category",""); unit=request.form.get("unit","").strip(); area=request.form.get("area","").strip(); condition=request.form.get("condition","")
        try:
            qty=float(request.form.get("quantity","0"))
            if not title or category not in CATEGORIES or condition not in CONDITIONS or not unit or not area or qty<=0: raise ValueError()
            lat,lng=read_coordinates()
            with db() as con:
                con.execute("UPDATE donations SET title=?,category=?,description=?,quantity=?,unit=?,condition=?,area=?,available_until=?,latitude=COALESCE(?,latitude),longitude=COALESCE(?,longitude) WHERE id=? AND donor_id=? AND status='Available'",(title,category,request.form.get("description",""),qty,unit,condition,area,request.form.get("available_until") or None,lat,lng,donation_id,u["id"]))
            flash("Donation updated.","success"); return redirect(url_for("dashboard"))
        except ValueError: flash("Check the donation fields and quantity.","danger")
    return render_template("donation_form.html",donation=d,editing=True)

@app.route("/donation/<int:donation_id>/withdraw", methods=["POST"])
@login_required
def withdraw_donation(donation_id):
    u=current_user()
    with db() as con:
        cur=con.execute("UPDATE donations SET status='Withdrawn' WHERE id=? AND donor_id=? AND status='Available'",(donation_id,u["id"]))
    if cur.rowcount: flash("Donation withdrawn from public listings.","success")
    else: flash("This donation cannot be withdrawn because it is not available.","warning")
    return redirect(url_for("dashboard"))

@app.route("/requirements/new",methods=["GET","POST"])
@roles("ngo")
def new_requirement():
    u=current_user()
    with db() as con: ngo=con.execute("SELECT * FROM ngos WHERE user_id=?",(u["id"],)).fetchone()
    if not ngo or ngo["verification"]!="Verified":
        flash("Your NGO must be verified before publishing requirements.","warning"); return redirect(url_for("dashboard"))
    if request.method=="POST":
        title=request.form.get("title","").strip(); category=request.form.get("category","")
        unit=request.form.get("unit","").strip(); area=request.form.get("area","").strip()
        urgency=request.form.get("urgency",""); condition=request.form.get("acceptable_condition","")
        try:
            qty=float(request.form.get("quantity","0"))
            if not title or category not in CATEGORIES or urgency not in URGENCIES or condition not in CONDITIONS or qty<=0 or not unit or not area: raise ValueError()
            with db() as con:
                lat, lng = read_coordinates()
                con.execute("""INSERT INTO requirements(ngo_id,title,category,description,quantity,unit,urgency,acceptable_condition,area,needed_by,latitude,longitude)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ngo["id"],title,category,request.form.get("description",""),qty,unit,urgency,condition,area,request.form.get("needed_by") or None,lat,lng))
            refresh_matches(); log("Requirement created",title); flash("Resource requirement published.","success"); return redirect(url_for("dashboard"))
        except ValueError: flash("Please check all fields and enter a positive quantity.","danger")
    return render_template("requirement_form.html")

@app.route("/browse")
@login_required
def browse():
    u=current_user()
    with db() as con:
        donations=con.execute("""SELECT d.*,u.name donor_name FROM donations d JOIN users u ON u.id=d.donor_id
            WHERE d.status='Available' AND d.donor_id<>? ORDER BY d.id DESC""",(u["id"],)).fetchall()
        requirements=con.execute("""SELECT r.*,n.organization_name,nu.area ngo_area FROM requirements r
            JOIN ngos n ON n.id=r.ngo_id JOIN users nu ON nu.id=n.user_id
            WHERE r.status='Open' AND n.verification='Verified' ORDER BY
            CASE r.urgency WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END,r.id DESC""").fetchall()
    return render_template("browse.html",donations=donations,requirements=requirements)

@app.route("/donation/<int:donation_id>/accept",methods=["POST"])
@login_required
def accept_donation(donation_id):
    u=current_user()
    with db() as con:
        d=con.execute("SELECT * FROM donations WHERE id=?",(donation_id,)).fetchone()
        if not d: abort(404)
        if d["donor_id"]==u["id"]:
            flash("You cannot accept your own donation.","warning")
            return redirect(url_for("browse"))
        # The conditional update and active-claim index protect against double booking.
        cur=con.execute("UPDATE donations SET status='Reserved' WHERE id=? AND status='Available'",(donation_id,))
        if cur.rowcount!=1:
            flash("Sorry, this donation has already been accepted or is no longer available.","warning")
            return redirect(url_for("browse"))
        pickup_code=f"{secrets.randbelow(1000000):06d}"
        otp_hash=hashlib.sha256(pickup_code.encode()).hexdigest()
        con.execute("INSERT INTO pickup_claims(donation_id,claimant_id,status,otp_hash,pickup_date) VALUES(?,?,'Accepted',?,?)",(donation_id,u["id"],otp_hash,request.form.get("pickup_date") or None))
        claim_id=con.execute("SELECT last_insert_rowid()").fetchone()[0]
        notify(d["donor_id"],f"Pickup verification code for '{d['title']}': {pickup_code}. Share this code with the recipient only when they arrive.","Pickup verification code",con)
        con.execute("UPDATE matches SET status='Unavailable' WHERE donation_id=? AND status='Suggested'",(donation_id,))
        notify(d["donor_id"],f"{u['name']} accepted your donation '{d['title']}'. Pickup contact and location are now shared with the recipient.","Your donation was accepted",con)
        notify(u["id"],f"You accepted '{d['title']}'. Open your pickup details to view the donor's phone number and map location.","Donation accepted",con)
    log("Self-service donation accepted",f"Donation {donation_id}; claim {claim_id}")
    flash("Donation accepted! Ask the donor for the 6-digit pickup code when collecting the item.","success")
    return redirect(url_for("pickup_detail",claim_id=claim_id))

@app.before_request
def prompt_completed_delivery_feedback():
    """Open pending delivery feedback once per user/session after donor confirmation."""
    if request.method != "GET" or not session.get("user_id"):
        return None
    if request.endpoint in (None, "static", "feedback", "logout", "health"):
        return None
    # Do not interrupt API or authentication endpoints.
    if request.path.startswith(("/static/", "/api/")):
        return None
    shown = session.get("feedback_prompted", [])
    with db() as con:
        pending = con.execute("""SELECT pc.id FROM pickup_claims pc
            JOIN donations d ON d.id=pc.donation_id
            WHERE pc.status='Completed' AND (pc.claimant_id=? OR d.donor_id=?)
            AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.claim_id=pc.id AND f.user_id=?)
            ORDER BY pc.updated_at DESC,pc.id DESC""",
            (session["user_id"],session["user_id"],session["user_id"])).fetchall()
    for item in pending:
        if item["id"] not in shown:
            session["feedback_prompted"] = (shown + [item["id"]])[-100:]
            return redirect(url_for("feedback",claim_id=item["id"]))
    return None

@app.route("/pickup/<int:claim_id>")
@login_required
def pickup_detail(claim_id):
    u=current_user()
    with db() as con:
        claim=con.execute("""SELECT pc.*,d.title,d.description,d.category,d.quantity,d.unit,d.condition,d.area,d.latitude,d.longitude,d.image,
            donor.id donor_id,donor.name donor_name,donor.phone donor_phone,donor.email donor_email,
            claimant.id claimant_id,claimant.name claimant_name,claimant.phone claimant_phone
            FROM pickup_claims pc JOIN donations d ON d.id=pc.donation_id
            JOIN users donor ON donor.id=d.donor_id JOIN users claimant ON claimant.id=pc.claimant_id
            WHERE pc.id=?""",(claim_id,)).fetchone()
    if not claim: abort(404)
    if u["id"] not in (claim["donor_id"],claim["claimant_id"]) and u["role"]!="admin": abort(403)
    return render_template("pickup_detail.html",claim=claim,is_claimant=u["id"]==claim["claimant_id"],is_donor=u["id"]==claim["donor_id"])

@app.route("/pickup/<int:claim_id>/status",methods=["POST"])
@login_required
def pickup_status(claim_id):
    u=current_user(); requested=request.form.get("status","")
    with db() as con:
        claim=con.execute("SELECT pc.*,d.donor_id,d.title,d.id donation_id FROM pickup_claims pc JOIN donations d ON d.id=pc.donation_id WHERE pc.id=?",(claim_id,)).fetchone()
        if not claim: abort(404)
        is_claimant=claim["claimant_id"]==u["id"]; is_donor=claim["donor_id"]==u["id"]; is_admin=u["role"]=="admin"
        if not (is_claimant or is_donor or is_admin): abort(403)
        current=claim["status"]
        if requested=="Pickup in Progress" and is_claimant and current=="Accepted":
            con.execute("UPDATE pickup_claims SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(requested,claim_id))
        elif requested=="Collected" and is_claimant and current=="Pickup in Progress":
            code=request.form.get("pickup_code","").strip()
            if len(code)!=6 or not code.isdigit() or not secrets.compare_digest(hashlib.sha256(code.encode()).hexdigest(),claim["otp_hash"] or ""):
                flash("Pickup verification code is incorrect.","danger"); return redirect(url_for("pickup_detail",claim_id=claim_id))
            con.execute("UPDATE pickup_claims SET status='Collected',claimant_confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(claim_id,))
            notify(claim["donor_id"],f"Recipient entered the pickup code and marked '{claim['title']}' as collected. Please confirm handover.","Collection awaiting confirmation",con)
        elif requested=="Completed" and (is_donor or is_admin) and current=="Collected":
            con.execute("UPDATE pickup_claims SET status='Completed',donor_confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(claim_id,))
            con.execute("UPDATE donations SET status='Completed' WHERE id=?",(claim["donation_id"],))
            notify(claim["claimant_id"],f"The donor confirmed handover of '{claim['title']}'. You can now leave feedback from the pickup details page.","Pickup completed",con)
        elif requested=="Cancelled" and (is_claimant or is_donor or is_admin) and current in ("Accepted","Pickup in Progress"):
            reason=request.form.get("cancellation_reason","").strip()
            if len(reason)<5:
                flash("Please provide a cancellation reason (at least 5 characters).","warning"); return redirect(url_for("pickup_detail",claim_id=claim_id))
            con.execute("UPDATE pickup_claims SET status='Cancelled',cancellation_reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(reason,claim_id))
            con.execute("UPDATE donations SET status='Available' WHERE id=? AND status='Reserved'",(claim["donation_id"],))
            con.execute("UPDATE matches SET status='Suggested' WHERE donation_id=? AND status IN ('Accepted','Unavailable')",(claim["donation_id"],))
            notify(claim["donor_id"],f"Pickup cancelled for '{claim['title']}'. Reason: {reason}. Donation is available again.","Pickup cancelled",con)
            notify(claim["claimant_id"],f"Your pickup for '{claim['title']}' was cancelled. Reason: {reason}.","Pickup cancelled",con)
        else:
            flash("That pickup status change is not allowed.","warning"); return redirect(url_for("pickup_detail",claim_id=claim_id))
    log("Self-service pickup status",f"Claim {claim_id}: {requested}")
    flash("Pickup status updated.","success")
    if requested == "Completed" and (is_donor or is_admin):
        # Donor sees the review form immediately; recipient sees it on next page load.
        if is_donor:
            prompted = session.get("feedback_prompted", [])
            session["feedback_prompted"] = (prompted + [claim_id])[-100:]
            return redirect(url_for("feedback", claim_id=claim_id))
    return redirect(url_for("pickup_detail",claim_id=claim_id))

@app.route("/pickup/<int:claim_id>/dispute", methods=["POST"])
@login_required
def pickup_dispute(claim_id):
    u=current_user(); reason=request.form.get("reason","").strip()
    with db() as con:
        claim=con.execute("SELECT * FROM pickup_claims WHERE id=?",(claim_id,)).fetchone()
        if not claim: abort(404)
        if u["id"] not in (claim["claimant_id"],) and u["role"]!="admin":
            donor=con.execute("SELECT donor_id FROM donations WHERE id=?",(claim["donation_id"],)).fetchone()
            if not donor or donor["donor_id"]!=u["id"]: abort(403)
        if len(reason)<10:
            flash("Please describe the issue in at least 10 characters.","warning")
        else:
            con.execute("INSERT INTO pickup_disputes(claim_id,reporter_id,reason) VALUES(?,?,?)",(claim_id,u["id"],reason))
            notify(1,"A pickup dispute was reported. Please review the admin dashboard.","Pickup dispute reported",con)
            flash("Dispute submitted to the admin team.","success")
    return redirect(url_for("pickup_detail",claim_id=claim_id))

@app.route("/matches")
@login_required
def matches():
    refresh_matches()
    u=current_user()
    with db() as con:
        if u["role"]=="ngo":
            ngo=con.execute("SELECT * FROM ngos WHERE user_id=?",(u["id"],)).fetchone()
            rows=con.execute("""SELECT m.*,d.title donation_title,d.category,d.quantity,d.unit,d.condition,d.area,
                    r.title requirement_title,r.urgency,r.quantity required_qty,r.unit req_unit,du.name donor_name
                    FROM matches m JOIN donations d ON d.id=m.donation_id JOIN requirements r ON r.id=m.requirement_id
                    JOIN users du ON du.id=d.donor_id WHERE r.ngo_id=? ORDER BY m.score DESC""",(ngo["id"],)).fetchall() if ngo else []
        elif u["role"] in ["donor","admin"]:
            rows=con.execute("""SELECT m.*,d.title donation_title,d.category,d.quantity,d.unit,d.condition,d.area,
                    r.title requirement_title,r.urgency,r.quantity required_qty,r.unit req_unit,nu.name ngo_name
                    FROM matches m JOIN donations d ON d.id=m.donation_id JOIN requirements r ON r.id=m.requirement_id
                    JOIN ngos n ON n.id=r.ngo_id JOIN users nu ON nu.id=n.user_id
                    WHERE d.donor_id=? OR ?='admin' ORDER BY m.score DESC""",(u["id"],u["role"])).fetchall()
        else: rows=[]
    return render_template("matches.html",matches=rows)

@app.route("/match/<int:match_id>/<action>",methods=["POST"])
@roles("donor","ngo","admin")
def match_action(match_id,action):
    if action not in ["accept","decline"]: abort(404)
    u=current_user(); pickup_claim_id=None
    with db() as con:
        m=con.execute("""SELECT m.*,d.donor_id,d.status donation_status,d.title donation_title,
            r.ngo_id,n.user_id ngo_user_id FROM matches m
            JOIN donations d ON d.id=m.donation_id JOIN requirements r ON r.id=m.requirement_id
            JOIN ngos n ON n.id=r.ngo_id WHERE m.id=?""",(match_id,)).fetchone()
        if not m: abort(404)
        if u["role"]=="donor" and m["donor_id"]!=u["id"]: abort(403)
        if u["role"]=="ngo" and m["ngo_user_id"]!=u["id"]: abort(403)
        if action=="accept":
            # A verified NGO accepts the suggested match and becomes the self-pickup recipient.
            if u["role"]!="ngo": abort(403)
            ngo=con.execute("SELECT verification FROM ngos WHERE user_id=?",(u["id"],)).fetchone()
            if not ngo or ngo["verification"]!="Verified": abort(403)
            if m["status"]!="Suggested" or m["donation_status"]!="Available":
                flash("This donation is no longer available for this match.","warning")
                return redirect(url_for("matches"))
            existing=con.execute("""SELECT id FROM pickup_claims WHERE donation_id=?
                AND status IN ('Accepted','Pickup in Progress','Collected')""",(m["donation_id"],)).fetchone()
            if existing:
                flash("This donation already has an active pickup claim.","warning")
                return redirect(url_for("pickup_detail",claim_id=existing["id"]))
            cur=con.execute("UPDATE donations SET status='Reserved' WHERE id=? AND status='Available'",(m["donation_id"],))
            if cur.rowcount!=1:
                flash("This donation has already been accepted.","warning")
                return redirect(url_for("matches"))
            con.execute("UPDATE matches SET status='Accepted' WHERE id=? AND status='Suggested'",(match_id,))
            con.execute("UPDATE matches SET status='Unavailable' WHERE donation_id=? AND id<>? AND status='Suggested'",(m["donation_id"],match_id))
            con.execute("INSERT INTO pickup_claims(donation_id,claimant_id,status) VALUES(?,?,'Accepted')",
                        (m["donation_id"],m["ngo_user_id"]))
            pickup_claim_id=con.execute("SELECT last_insert_rowid()").fetchone()[0]
            notify(m["donor_id"],"A verified NGO accepted your donation match. The NGO can now view your pickup contact and location.","NGO accepted your donation",con)
            notify(m["ngo_user_id"],"You accepted a donation match. Open pickup details to contact the donor and collect the item directly.","Donation match accepted",con)
        else:
            if m["status"]!="Suggested":
                flash("Only suggested matches can be declined.","warning")
                return redirect(url_for("matches"))
            con.execute("UPDATE matches SET status='Declined' WHERE id=? AND status='Suggested'",(match_id,))
            flash("Match declined.","info")
    log("Match "+action,str(match_id))
    if pickup_claim_id:
        flash("Match accepted. Donor phone number and pickup map are now available.","success")
        return redirect(url_for("pickup_detail",claim_id=pickup_claim_id))
    return redirect(url_for("matches"))

@app.route("/admin/ngo/<int:ngo_id>/<decision>",methods=["POST"])
@roles("admin")
def verify_ngo(ngo_id,decision):
    if decision not in ["Verified","Rejected","Suspended"]: abort(404)
    with db() as con:
        ngo=con.execute("SELECT * FROM ngos WHERE id=?",(ngo_id,)).fetchone()
        if not ngo: abort(404)
        con.execute("UPDATE ngos SET verification=? WHERE id=?",(decision,ngo_id))
        notify(ngo["user_id"],f"Your NGO verification status is now: {decision}.",con=con)
    log("NGO verification",f"NGO {ngo_id}: {decision}")
    flash(f"NGO status updated to {decision}.","success"); return redirect(url_for("dashboard"))

@app.route("/tasks")
@login_required
def tasks():
    flash("DaanaSetu uses direct self-service pickup. Accept a donation to arrange collection yourself.","info")
    return redirect(url_for("browse"))

@app.route("/task/create/<int:match_id>",methods=["POST"])
@login_required
def create_task(match_id):
    flash("Volunteer delivery is disabled in the self-service pickup workflow.","info")
    return redirect(url_for("browse"))

@app.route("/task/<int:task_id>/claim",methods=["POST"])
@login_required
def claim_task(task_id):
    flash("Volunteer delivery is disabled. You can accept a donation and collect it directly.","info")
    return redirect(url_for("browse"))

@app.route("/task/<int:task_id>/status",methods=["POST"])
@login_required
def task_status(task_id):
    flash("Volunteer delivery is disabled in the self-service pickup workflow.","info")
    return redirect(url_for("browse"))

@app.route("/map")
@login_required
def map_view():
    return render_template("map.html")

@app.route("/api/map-data")
@login_required
def map_data():
    with db() as con:
        # Exact donor pickup coordinates are deliberately never exposed by the general map API.
        needs = con.execute("SELECT r.id,r.title,r.area,r.latitude,r.longitude,r.urgency,r.status,n.organization_name FROM requirements r JOIN ngos n ON n.id=r.ngo_id WHERE r.latitude IS NOT NULL AND r.longitude IS NOT NULL AND r.status='Open' AND n.verification='Verified'").fetchall()
    return {"donations":[],"requirements":[dict(x) for x in needs],"tasks":[]}

@app.route("/settings/notifications", methods=["GET","POST"])
@login_required
def notification_settings():
    user_id=current_user()["id"]
    with db() as con:
        con.execute("INSERT OR IGNORE INTO notification_preferences(user_id) VALUES(?)",(user_id,))
        if request.method == "POST":
            con.execute("UPDATE notification_preferences SET in_app=?,email=?,sms=? WHERE user_id=?",
                (1 if request.form.get("in_app") else 0,1 if request.form.get("email") else 0,1 if request.form.get("sms") else 0,user_id))
            flash("Your notification preferences have been saved.","success")
        prefs=con.execute("SELECT * FROM notification_preferences WHERE user_id=?",(user_id,)).fetchone()
    return render_template("notification_settings.html", prefs=prefs,
        email_enabled=os.getenv("ENABLE_EMAIL","false").lower()=="true",
        sms_enabled=os.getenv("ENABLE_SMS","false").lower()=="true")

@app.route("/notifications")
@login_required
def notifications():
    user_id=current_user()["id"]
    with db() as con:
        rows=con.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",(user_id,)).fetchall()
        logs=con.execute("SELECT * FROM notification_delivery_logs WHERE user_id=? ORDER BY id DESC LIMIT 50",(user_id,)).fetchall()
    return render_template("notifications.html",notifications=rows,delivery_logs=logs)

@app.route("/reports")
@roles("admin")
def reports():
    with db() as con:
        categories=con.execute("SELECT category,COUNT(*) total FROM donations GROUP BY category").fetchall()
        statuses=con.execute("SELECT status,COUNT(*) total FROM donations GROUP BY status").fetchall()
        totals={"donations":con.execute("SELECT COUNT(*) FROM donations").fetchone()[0],
                "verified_ngos":con.execute("SELECT COUNT(*) FROM ngos WHERE verification='Verified'").fetchone()[0],
                "fulfilled":con.execute("SELECT COUNT(*) FROM matches WHERE status='Completed'").fetchone()[0],
                "open_needs":con.execute("SELECT COUNT(*) FROM requirements WHERE status='Open'").fetchone()[0]}
    return render_template("reports.html",categories=categories,statuses=statuses,totals=totals)

@app.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    u = current_user()
    claim_id_raw = request.form.get("claim_id", "") if request.method == "POST" else request.args.get("claim_id", "")
    try:
        claim_id = int(claim_id_raw) if claim_id_raw else None
    except (ValueError, TypeError):
        abort(400)
    with db() as con:
        if claim_id is not None:
            claim = con.execute("""SELECT pc.id, pc.status, d.title, d.donor_id, pc.claimant_id
                FROM pickup_claims pc JOIN donations d ON d.id=pc.donation_id WHERE pc.id=?""", (claim_id,)).fetchone()
            if not claim: abort(404)
            if u["id"] not in (claim["donor_id"], claim["claimant_id"]): abort(403)
            if claim["status"] != "Completed":
                flash("Feedback for a pickup is available after completion.", "warning")
                return redirect(url_for("pickup_detail", claim_id=claim_id))
        if request.method == "POST":
            if claim_id is None:
                flash("Feedback can only be submitted for a successfully completed delivery.", "warning")
                return redirect(url_for("dashboard"))
            try: rating = int(request.form.get("rating", ""))
            except (ValueError, TypeError): rating = 0
            comment = request.form.get("comment", "").strip()
            if rating not in range(1, 6) or not 5 <= len(comment) <= 1000:
                flash("Choose 1–5 stars and enter feedback between 5 and 1000 characters.", "danger")
            elif claim_id is not None and con.execute("SELECT 1 FROM feedback WHERE user_id=? AND claim_id=?", (u["id"], claim_id)).fetchone():
                flash("You have already reviewed this pickup.", "warning")
            else:
                con.execute("INSERT INTO feedback(user_id,claim_id,rating,comment) VALUES(?,?,?,?)", (u["id"],claim_id,rating,comment))
                flash("Thank you! Your feedback has been submitted.", "success")
                return redirect(url_for("feedback"))
        mine = con.execute("""SELECT f.*, pc.donation_id, d.title FROM feedback f
            LEFT JOIN pickup_claims pc ON pc.id=f.claim_id
            LEFT JOIN donations d ON d.id=pc.donation_id
            WHERE f.user_id=? ORDER BY f.created_at DESC, f.id DESC""", (u["id"],)).fetchall()
        recent = con.execute("""SELECT f.rating,f.comment,f.created_at,u.name,d.title
            FROM feedback f JOIN users u ON u.id=f.user_id
            LEFT JOIN pickup_claims pc ON pc.id=f.claim_id
            LEFT JOIN donations d ON d.id=pc.donation_id
            ORDER BY f.created_at DESC,f.id DESC LIMIT 12""").fetchall()
        completed = con.execute("""SELECT pc.id,d.title FROM pickup_claims pc JOIN donations d ON d.id=pc.donation_id
            WHERE pc.status='Completed' AND (pc.claimant_id=? OR d.donor_id=?)
            AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.claim_id=pc.id AND f.user_id=?)
            ORDER BY pc.updated_at DESC""", (u["id"],u["id"],u["id"])).fetchall()
        already = bool(claim_id and con.execute("SELECT 1 FROM feedback WHERE user_id=? AND claim_id=?", (u["id"], claim_id)).fetchone())
    return render_template("feedback.html", mine=mine, recent=recent, claim_id=claim_id, already=already, completed=completed)

@app.route("/health")
def health(): return {"status":"ok","application":"DaanaSetu"}

@app.errorhandler(403)
def forbidden(e): return render_template("error.html",code=403,message="You do not have permission to access this page."),403
@app.errorhandler(404)
def not_found(e): return render_template("error.html",code=404,message="The page or record could not be found."),404

if __name__=="__main__":
    init_db()
    app.run(debug=True)
