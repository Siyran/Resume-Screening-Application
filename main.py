import os
import json
import re
import logging
import traceback
from flask import Flask, request, jsonify
from flask_cors import CORS

# Google Sheets API
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# File parsing
from PyPDF2 import PdfReader
import docx

# Gemini
import google.generativeai as genai

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configurations (set via Render Dashboard → Environment Variables)
# -----------------------------------------------------------------------------
SHEET_ID = os.getenv("1FPmGIUFRi_FrVVROi0rcLI7_p-ub18GxOWWiEoWwnJQ"")  # e.g. "1FPmGIUFRi_FrVVROi0rcLI7_p-ub18GxOWWiEoWwnJQ"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")  # JSON string, not file

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

if not SHEET_ID:
    raise RuntimeError("❌ SHEET_ID not set in environment variables")
if not GEMINI_API_KEY:
    raise RuntimeError("❌ GEMINI_API_KEY not set in environment variables")
if not GOOGLE_CREDENTIALS:
    raise RuntimeError("❌ GOOGLE_CREDENTIALS not set in environment variables")

# -----------------------------------------------------------------------------
# Google Sheets Setup
# -----------------------------------------------------------------------------
creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS), scopes=SCOPES)
sheets_service = build("sheets", "v4", credentials=creds)

# -----------------------------------------------------------------------------
# Gemini Setup
# -----------------------------------------------------------------------------
genai.configure(api_key=GEMINI_API_KEY)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def extract_text_from_file(file_path):
    """Extract raw text from uploaded PDF or DOCX."""
    text = ""
    try:
        if file_path.endswith(".pdf"):
            with open(file_path, "rb") as f:
                reader = PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() or ""
        elif file_path.endswith(".docx"):
            doc = docx.Document(file_path)
            for para in doc.paragraphs:
                text += para.text + "\n"
        else:
            return None
    except Exception as e:
        logger.exception("Error parsing file: %s", e)
        return None
    return text.strip()

def score_resume_with_gemini(resume_text, job_description="AI Intern role"):
    """Ask Gemini to score resume relevance to a job description."""
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
        You are an HR assistant. Score this resume for relevance to {job_description}.
        Resume text:
        {resume_text[:4000]}  # limit to avoid prompt overflow

        Return JSON with:
        - score: 0-100
        - decision: "Accepted" or "Rejected"
        - feedback: short feedback
        """
        response = model.generate_content(prompt)
        if not response or not response.text:
            return {"score": 0, "decision": "Rejected", "feedback": "No response"}
        
        # Try to parse JSON from Gemini
        match = re.search(r"\{.*\}", response.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        else:
            return {"score": 50, "decision": "Rejected", "feedback": response.text.strip()}
    except Exception as e:
        logger.exception("Gemini scoring failed: %s", e)
        return {"score": 0, "decision": "Rejected", "feedback": "Error scoring resume"}

def save_to_sheets(name, email, phone, score, decision):
    """Append applicant data to Google Sheet."""
    try:
        body = {
            "values": [[name, email, phone, str(score), decision]]
        }
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range="Sheet1!A:E",
            valueInputOption="RAW",
            body=body
        ).execute()
        return True
    except Exception as e:
        logger.exception("Failed to save to Google Sheets: %s", e)
        return False

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def home():
    return jsonify({"ok": True, "message": "HR Screening API running 🚀"})

@app.route("/apply", methods=["POST"])
def apply():
    try:
        name = request.form.get("name")
        email = request.form.get("email")
        phone = request.form.get("phone")
        resume_file = request.files.get("resume")

        if not all([name, email, phone, resume_file]):
            return jsonify({"ok": False, "error": "Missing fields"}), 400

        # Save uploaded file temporarily
        upload_path = os.path.join("/tmp", resume_file.filename)
        resume_file.save(upload_path)

        # Extract text
        resume_text = extract_text_from_file(upload_path)
        if not resume_text:
            return jsonify({"ok": False, "error": "Unsupported or unreadable file"}), 400

        # Score resume
        result = score_resume_with_gemini(resume_text)
        score = result.get("score", 0)
        decision = result.get("decision", "Rejected")

        # Save to Google Sheets
        save_ok = save_to_sheets(name, email, phone, score, decision)

        return jsonify({
            "ok": True,
            "message": "Application submitted",
            "score": score,
            "decision": decision,
            "feedback": result.get("feedback", ""),
            "saved_to_sheets": save_ok
        })
    except Exception as e:
        logger.error("Error in /apply: %s", traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ask", methods=["POST"])
def ask_gemini():
    """Let applicants ask Gemini about role or company."""
    try:
        data = request.json
        question = data.get("question", "")
        if not question:
            return jsonify({"ok": False, "error": "No question provided"}), 400

        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(question)
        return jsonify({"ok": True, "answer": response.text.strip()})
    except Exception as e:
        logger.exception("Gemini QnA failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# -----------------------------------------------------------------------------
# Run (for local dev)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
