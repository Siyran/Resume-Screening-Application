import os
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from werkzeug.utils import secure_filename
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials

# Flask app setup
app = Flask(__name__)
CORS(app)

# ---------------- Google Sheets Setup ----------------
import json
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2 import service_account

# Load credentials from local file
creds = service_account.Credentials.from_service_account_file(
    "credentials.json", scopes=SCOPES
)

)
client = gspread.authorize(creds)

SHEET_ID = os.getenv("SHEET_ID")
sheet = client.open_by_key(SHEET_ID).sheet1

# ---------------- Gemini Setup ----------------
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------------- Routes ----------------

@app.route("/")
def index():
    return render_template("abc.html")

@app.route("/submit", methods=["POST"])
def submit():
    try:
        name = request.form.get("name")
        email = request.form.get("email")
        phone = request.form.get("phone")
        resume = request.files.get("resume")

        if not all([name, email, phone, resume]):
            return jsonify({"error": "All fields required"}), 400

        filename = secure_filename(resume.filename)
        resume_path = os.path.join("uploads", filename)
        os.makedirs("uploads", exist_ok=True)
        resume.save(resume_path)

        # AI Screening
        prompt = f"""
        You are an HR assistant. Based on this applicant’s details:
        Name: {name}
        Email: {email}
        Phone: {phone}
        Resume File: {filename}

        Rate applicant suitability for internship on scale 0-100 and give decision.
        Respond in JSON like: {{"score": 85, "decision": "Accepted"}}
        """
        response = model.generate_content(prompt).text.strip()

        import re, json as pyjson
        try:
            match = re.search(r"\{.*\}", response, re.S)
            result = pyjson.loads(match.group()) if match else {"score": 0, "decision": "Rejected"}
        except:
            result = {"score": 0, "decision": "Rejected"}

        # Save to Google Sheets
        sheet.append_row([name, email, phone, result["score"], result["decision"]])

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json()
        question = data.get("question", "")
        if not question:
            return jsonify({"error": "No question provided"}), 400

        prompt = f"You are an HR assistant. Answer clearly:\nQ: {question}\nA:"
        response = model.generate_content(prompt).text.strip()

        return jsonify({"answer": response})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
