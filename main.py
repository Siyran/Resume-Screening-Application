import os
import json
from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2 import service_account
import google.generativeai as genai

# Flask app
app = Flask(__name__)

# Google Sheets + Gemini setup
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Load credentials from environment variable
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS")
creds_dict = json.loads(GOOGLE_CREDS_JSON)
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

# Authorize Google Sheets
client = gspread.authorize(creds)

# Get Sheet ID from env variable
SHEET_ID = os.getenv("SHEET_ID")
sheet = client.open_by_key(SHEET_ID).sheet1

# Gemini API setup
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
gemini_model = genai.GenerativeModel("gemini-pro")


@app.route("/")
def index():
    return render_template("abc.html")


@app.route("/submit", methods=["POST"])
def submit():
    try:
        name = request.form["name"]
        email = request.form["email"]
        phone = request.form["phone"]
        resume = request.files["resume"]

        # Save resume temporarily
        resume_path = os.path.join("uploads", resume.filename)
        os.makedirs("uploads", exist_ok=True)
        resume.save(resume_path)

        # Ask Gemini to evaluate
        prompt = f"Evaluate this candidate:\nName: {name}\nEmail: {email}\nPhone: {phone}\nResume file: {resume.filename}\nGive a suitability score (0-100)."
        gemini_response = gemini_model.generate_content(prompt)
        score_text = gemini_response.text.strip()
        try:
            score = int("".join([c for c in score_text if c.isdigit()]))
        except:
            score = 50  # fallback if parsing fails

        decision = "Accepted" if score >= 60 else "Rejected"

        # Save to Google Sheet
        sheet.append_row([name, email, phone, resume.filename, score, decision])

        return jsonify({
            "ok": True,
            "message": "Application submitted successfully",
            "score": score,
            "decision": decision
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
