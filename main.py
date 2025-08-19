#!/usr/bin/env python3
"""
main.py – Stable Flask backend for HR Screening App
- Uploads resumes (PDF/DOC/DOCX/TXT)
- Parses text and calculates a simple JD-match score
- Logs to Google Sheets (graceful degradation if Sheets unavailable)
- Sends notification email (optional; skips if creds missing)
- Gemini chat endpoint with retry + clearer errors
- CORS enabled for local dev; large, safe timeouts; structured JSON

Setup (once):
1) Python 3.10+
2) pip install -r requirements.txt (see bottom comment for list)
3) Put your Google service account key as credentials.json in the app folder.
   Share the target Google Sheet to that service account email.
4) Set env vars (recommended):
   SHEET_ID, GEMINI_API_KEY, SMTP_EMAIL, SMTP_APP_PASSWORD
5) Run:  python main.py   (defaults to 0.0.0.0:5000)
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import uuid
import gspread
import datetime
import traceback
import requests
import smtplib
import re
import logging
from typing import Tuple
from werkzeug.utils import secure_filename
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from oauth2client.service_account import ServiceAccountCredentials

# Optional libs
import PyPDF2
import docx  # python-docx

# -------------------------- CONFIG --------------------------
app = Flask(__name__, static_folder=".", static_url_path="/")
CORS(app)

# File upload limits & allowed types
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "txt"}

# If running in render/railway/heroku/etc.
PORT = int(os.environ.get("PORT", 5000))

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Env-configurable secrets (fallback to hard-coded only for local tests)
SHEET_ID = os.environ.get("SHEET_ID", "1FPmGIUFRi_FrVVROi0rcLI7_p-ub18GxOWWiEoWwnJQ")
CREDENTIALS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD", "")

JOB_DESCRIPTION = (
    "Looking for candidates with strong Python, Flask, HTML/CSS, JavaScript skills, "
    "experience in AI/ML projects, attention to detail, and excellent communication."
)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------- HELPERS ----------------------

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def build_gsheet_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            "credentials.json not found. Provide a Google service account key as credentials.json "
            "or set GOOGLE_APPLICATION_CREDENTIALS."
        )
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds)


_cached_sheet = None

def sheet_handle():
    """Get the first worksheet and ensure headers. Cache handle for reuse."""
    global _cached_sheet
    if _cached_sheet is None:
        client = build_gsheet_client()
        sh = client.open_by_key(SHEET_ID)
        ws = sh.sheet1
        headers = ws.row_values(1)
        desired = [
            "Timestamp",
            "Full Name",
            "Email",
            "Phone",
            "Resume File",
            "Score",
            "Decision",
            "Reasons",
        ]
        if not headers:
            ws.insert_row(desired, 1)
        _cached_sheet = ws
    return _cached_sheet


# ---------------------- Resume Parsing ----------------------

def extract_text_from_resume(filepath: str) -> str:
    ext = filepath.lower().rsplit(".", 1)[-1]
    text = ""
    try:
        if ext == "pdf":
            with open(filepath, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    # extract_text may return None if page is image-only
                    page_txt = page.extract_text() or ""
                    text += page_txt + " "
        elif ext in {"doc", "docx"}:
            doc = docx.Document(filepath)
            for para in doc.paragraphs:
                text += para.text + " "
        else:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
    except Exception as e:
        logger.exception("Resume parse failed: %s", e)
        # Return empty text (score will be 0) but don't crash submission
        return ""
    return text


def calculate_score(resume_text: str) -> int:
    if not resume_text.strip():
        return 0
    keywords = re.findall(r"\b\w+\b", JOB_DESCRIPTION.lower())
    resume_words = set(re.findall(r"\b\w+\b", resume_text.lower()))
    # Count unique keyword matches for a fairer score
    unique_keywords = set(keywords)
    matches = sum(1 for k in unique_keywords if k in resume_words)
    score = min(100, int(matches / max(1, len(unique_keywords)) * 100))
    return score


# ---------------------- Email ----------------------

def send_email(to_email: str, subject: str, body_html: str) -> Tuple[bool, str]:
    if not (SMTP_EMAIL and SMTP_APP_PASSWORD and to_email):
        return False, "Email not configured – skipped"
    try:
        msg = MIMEMultipart()
        msg["From"] = "gamingsxr@gmail.com"
        msg["To"] = ""
        msg["Subject"] = subject
        msg.attach(MIMEText(body_html, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, "sent"
    except Exception as e:
        logger.exception("Email failed: %s", e)
        return False, f"failed: {e}"


# ---------------------- Gemini ----------------------

def call_gemini(prompt_text: str) -> str:
    if not GEMINI_API_KEY:
        return "(Gemini API key missing – set GEMINI_API_KEY)"

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.2, "topP": 0.9, "maxOutputTokens": 384},
    }

    # Simple retry loop for flaky network or 429
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code >= 400:
                # Backoff on 429/5xx
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    continue
                return f"(Gemini error {resp.status_code}: {resp.text[:200]})"
            data = resp.json()
            return (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "(No content)")
            )
        except Exception as e:
            logger.warning("Gemini attempt %d failed: %s", attempt + 1, e)
            if attempt == 2:
                return f"(Gemini request failed: {e})"
    return "(Gemini unreachable)"


# ---------------------- Routes ----------------------

@app.route("/")
def home():
    # Serve your front-end file (rename to index.html as needed)
    html_file = "abc.html"
    if not os.path.exists(os.path.join(BASE_DIR, html_file)):
        return (
            "<h2>Backend is running</h2><p>Place your front-end file as abc.html next to main.py.</p>",
            200,
            {"Content-Type": "text/html"},
        )
    return send_from_directory(".", html_file)


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.datetime.utcnow().isoformat() + "Z"})


@app.route("/submit", methods=["POST"])
def submit():
    warnings = []
    try:
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        if not name or not email or not phone:
            return jsonify({
                "ok": False,
                "error": "Name, email and phone are required",
            }), 400

        if "resume" not in request.files:
            return jsonify({"ok": False, "error": "Resume file required"}), 400

        file = request.files["resume"]
        if file.filename == "":
            return jsonify({"ok": False, "error": "No file selected"}), 400
        if not allowed_file(file.filename):
            return jsonify({
                "ok": False,
                "error": f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            }), 400

        safe_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{safe_name or 'resume'}"
        filepath = os.path.join(UPLOAD_DIR, unique_name)
        file.save(filepath)

        resume_text = extract_text_from_resume(filepath)
        score = calculate_score(resume_text)
        decision = "Accepted" if score >= 85 else "Rejected"
        reasons = f"Resume scored {score}/100 against required keywords."

        # Try Sheets, but do not fail submission if Sheets is down
        try:
            ws = sheet_handle()
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws.append_row([timestamp, name, email, phone, unique_name, score, decision, reasons])
        except Exception as e:
            logger.exception("Sheets append failed: %s", e)
            warnings.append(f"Sheets logging failed: {e}")

        # Try email (optional)
        ok_mail, mail_msg = send_email(
            email,
            subject=("Application Accepted" if decision == "Accepted" else "Application Status"),
            body_html=(
                f"<p>Hi {name},</p><p>Your application score: <b>{score}</b>. "
                + (
                    "Proceed to next step: we'll contact you shortly."
                    if decision == "Accepted"
                    else "Thanks for applying. Unfortunately you were not selected for the next round."
                )
                + "</p>"
            ),
        )
        if not ok_mail:
            warnings.append(f"Email: {mail_msg}")

        return jsonify({
            "ok": True,
            "message": f"Application submitted for {name}",
            "score": score,
            "decision": decision,
            "warnings": warnings,
            "file": unique_name,
        }), 200

    except Exception as e:
        logger.exception("/submit crashed: %s", e)
        return jsonify({
            "ok": False,
            "error": str(e),
            "trace": traceback.format_exc(),
        }), 500


@app.route("/chat", methods=["POST"])
def chat():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        user_msg = (payload.get("message") or "").strip()
        if not user_msg:
            return jsonify({"ok": False, "reply": "Please enter a message."}), 400
        prompt = (
            "You are a helpful, concise assistant for an internship program. "
            "Answer briefly and professionally.\n\nUser: "
            + user_msg
            + "\nAssistant:"
        )
        ai_text = call_gemini(prompt)
        return jsonify({"ok": True, "reply": ai_text}), 200
    except Exception as e:
        logger.exception("/chat crashed: %s", e)
        return jsonify({"ok": False, "reply": f"AI error: {e}"}), 500


# ---------------------- Main ----------------------
if __name__ == "__main__":
    logger.info(f"Starting server on 0.0.0.0:{PORT}")
    # Use threaded to avoid blocking during network calls
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


# ---------------------- requirements.txt (reference) ----------------------
# Flask==3.0.3
# flask-cors==4.0.0
# gspread==5.12.4
# oauth2client==4.1.3
# requests==2.32.3
# PyPDF2==3.0.1
# python-docx==1.1.2
# gunicorn==22.0.0  # if deploying
