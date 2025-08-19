#!/usr/bin/env python3
# main.py - Flask backend: resume scoring, Google Sheets, automated emails, Gemini chat

from flask import Flask, request, jsonify, send_from_directory
import os, uuid, gspread, datetime, traceback, requests, smtplib, re
from werkzeug.utils import secure_filename
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from oauth2client.service_account import ServiceAccountCredentials
import PyPDF2, docx

app = Flask(__name__, static_folder=".", static_url_path="/")

# ---------- CONFIG ----------
SHEET_ID = "1FPmGIUFRi_FrVVROi0rcLI7_p-ub18GxOWWiEoWwnJQ"
CREDENTIALS_FILE = "credentials.json"
API_KEY = "AIzaSyBNKzbzm0mhx0C_ZnbGa2z-KZcpSMy7c94"
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Job description for scoring (can be customized)
JOB_DESCRIPTION = """
Looking for candidates with strong Python, Flask, HTML/CSS, JavaScript skills,
experience in AI/ML projects, attention to detail, and excellent communication.
"""

# Email config
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ADDRESS = "your_email@gmail.com"      # Replace with your email
EMAIL_PASSWORD = "your_app_password"        # Replace with App Password if using Gmail

# ---------- GOOGLE SHEETS SETUP ----------
def build_gsheet_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds)

def get_sheet():
    client = build_gsheet_client()
    sh = client.open_by_key(SHEET_ID)
    worksheet = sh.sheet1
    headers = worksheet.row_values(1)
    desired = ["Timestamp", "Full Name", "Email", "Phone", "Resume File", "Score", "Status", "Reasons"]
    if not headers:
        worksheet.insert_row(desired, 1)
    return worksheet

_cached_sheet = None
def sheet_handle():
    global _cached_sheet
    if _cached_sheet is None:
        _cached_sheet = get_sheet()
    return _cached_sheet

# ---------- GEMINI HELPER ----------
def call_gemini(prompt_text):
    model_name = "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature":0.2, "topP":0.95, "maxOutputTokens":512}
    }
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except:
        return str(data)

# ---------- RESUME PARSING ----------
def extract_text_from_resume(filepath):
    ext = filepath.lower().split(".")[-1]
    text = ""
    try:
        if ext == "pdf":
            with open(filepath, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() + " "
        elif ext in ["doc", "docx"]:
            doc = docx.Document(filepath)
            for para in doc.paragraphs:
                text += para.text + " "
        else:
            with open(filepath, "r", encoding="utf-8") as f:
                text = f.read()
    except Exception as e:
        text = ""
    return text

def calculate_score(resume_text):
    # Simple scoring: count overlap of keywords with job description
    keywords = re.findall(r"\b\w+\b", JOB_DESCRIPTION.lower())
    resume_words = re.findall(r"\b\w+\b", resume_text.lower())
    if not resume_words:
        return 0
    matches = sum(1 for k in keywords if k in resume_words)
    score = min(100, int(matches / len(keywords) * 100))
    return score

# ---------- EMAIL FUNCTION ----------
# def send_email(to_email, subject, body):
#     try:
#         msg = MIMEMultipart()
#         msg['From'] = EMAIL_ADDRESS
#         msg['To'] = to_email
#         msg['Subject'] = subject
#         msg.attach(MIMEText(body, 'html'))

#         server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
#         server.starttls()
#         server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
#         server.send_message(msg)
#         server.quit()
#     except Exception as e:
#         print("Email failed:", e)

# ---------- ROUTES ----------
@app.route("/")
def home():
    return send_from_directory(".", "abc.html")

@app.route("/submit", methods=["POST"])
def submit():
    try:
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name or not email or not phone:
            return jsonify({"status":"error","message":"Name, email, phone required"}),400
        if "resume" not in request.files:
            return jsonify({"status":"error","message":"Resume file required"}),400

        file = request.files["resume"]
        filename = secure_filename(file.filename) or "resume"
        unique_name = f"{uuid.uuid4().hex}_{filename}"
        filepath = os.path.join(UPLOAD_DIR, unique_name)
        file.save(filepath)

        resume_text = extract_text_from_resume(filepath)
        score = calculate_score(resume_text)
        reasons = f"Resume scored {score}/100 based on job description match."

        status = "Accepted" if score >= 85 else "Rejected"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sh = sheet_handle()
        sh.append_row([timestamp, name, email, phone, unique_name, score, status, reasons])

        # Send email
        if status == "Accepted":
            link = f"https://yourcompany.com/next_step?applicant={name}"
            send_email(email, "Application Accepted", f"<p>Congrats {name},</p><p>Your score is {score}. Proceed to the next step: <a href='{link}'>Click here</a></p>")
        else:
            send_email(email, "Application Status", f"<p>Dear {name},</p><p>Thank you for applying. Your score is {score}. Unfortunately, you have not been selected for the next round.</p>")

        return jsonify({"status":"success","message":f"Application submitted for {name}", "score":score,"status":status})
    except Exception as e:
        return jsonify({"status":"error","message":str(e), "trace": traceback.format_exc()}),500

@app.route("/chat", methods=["POST"])
def chat():
    try:
        payload = request.get_json(force=True)
        user_msg = payload.get("message", "")
        if not user_msg:
            return jsonify({"reply":"No message provided."}),400
        prompt = f"You are a friendly assistant for an internship program. Answer briefly and professionally.\nUser: {user_msg}\nAssistant:"
        ai_text = call_gemini(prompt)
        return jsonify({"reply":ai_text})
    except Exception as e:
        return jsonify({"reply":f"AI error: {str(e)}", "trace": traceback.format_exc()}),500

if __name__ == "__main__":
    print("Starting server on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True)
