#!/usr/bin/env python3
"""
main.py – Stable Flask backend for HR Screening App (NO email)
- Uploads resumes (PDF/DOC/DOCX/TXT)
- Parses text and calculates a simple JD-match score
- Logs to Google Sheets (graceful degradation if Sheets unavailable)
- Gemini chat endpoint with retry + clearer errors
- CORS enabled; structured JSON

Run:
  python main.py
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import uuid
import gspread
import datetime
import traceback
import requests
import re
import logging
from typing import Tuple
from werkzeug.utils import secure_filename
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

# ---- Hard-coded values per your request ----
SHEET_ID = "1FPmGIUFRi_FrVVROi0rcLI7_p-ub18GxOWWiEoWwnJQ"
CREDENTIALS_FILE = "credentials.json"
API_KEY = "AIzaSyBNKzbzm0mhx0C_ZnbGa2z-KZcpSMy7c94"  # Gemini API key
GEMINI_MODEL = "gemini-2.0-flash"

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
            "credentials.json not found. Place your Google service account key as credentials.json"
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
        return ""
    return text


def calculate_score(resume_text: str) -> int:
    if not resume_text.strip():
        return 0
    keywords = re.findall(r"\b\w+\b", JOB_DESCRIPTION.lower())
    resume_words = set(re.findall(r"\b\w+\b", resume_text.lower()))
    unique_keywords = set(keywords)
    matches = sum(1 for k in unique_keywords if k in resume_words)
    score = min(100, int(matches / max(1, len(unique_keywords)) * 100))
    return score


# ---------------------- Gemini ----------------------


def call_gemini(prompt_text: str) -> str:
    if not API_KEY:
        return "(Gemini API key missing)"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={API_KEY}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.2, "topP": 0.9, "maxOutputTokens": 384},
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code >= 400:
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
    try:
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        if not name or not email or not phone:
            return jsonify({"ok": False, "error": "Name, email and phone are required"}), 400

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

        warnings = []
        # Try logging to Google Sheets (non-fatal)
        try:
            ws = sheet_handle()
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws.append_row([timestamp, name, email, phone, unique_name, score, decision, reasons])
        except Exception as e:
            logger.exception("Sheets append failed: %s", e)
            warnings.append(f"Sheets logging failed: {e}")

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
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


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
