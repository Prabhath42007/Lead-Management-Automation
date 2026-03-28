"""
LEAD MANAGEMENT AUTOMATION - Webhook Version
Receives leads via HTTP POST instead of CSV files.

Local:  uvicorn main:app --reload
Test:   http://127.0.0.1:8000/docs
Deploy: Render / Railway (reads config from environment variables)
"""

import csv
import json
import os
import requests
import phonenumbers
import gspread

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from email_validator import validate_email, EmailNotValidError
from pathlib import Path
from datetime import date
from google.oauth2.service_account import Credentials


# ──────────────────────────────────────────
#  CONFIG — reads from environment variables
#  Set these in Render/Railway dashboard
#  For local, they fall back to your original values
# ──────────────────────────────────────────

SHEET_ID      = os.getenv("SHEET_ID",      "1SVCTqraL8aKz71PsB43_X2TG7Xo1BKpIkfLO0aJjOpQ")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "https://hooks.slack.com/services/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")
REPS_FILE     = os.getenv("REPS_FILE",     "rep_data.csv")

# Google credentials — file path (local) or JSON string (Render/Railway)
GOOGLE_CREDENTIALS_PATH = Path.cwd() / "configs/credentials.json"
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")  # set in Render dashboard

# Shared state — loaded once when server starts
store = {
    "active_reps": [],
    "index": 0
}


# ──────────────────────────────────────────
#  APP
# ──────────────────────────────────────────

app = FastAPI(
    title="Lead Management API",
    description="Receives leads via webhook instead of CSV",
    version="1.0.0"
)


# ──────────────────────────────────────────
#  REQUEST MODEL
# ──────────────────────────────────────────

class Lead(BaseModel):
    Name:  str
    Email: str
    Phone: str


# ──────────────────────────────────────────
#  STARTUP — runs once when server starts
# ──────────────────────────────────────────

@app.on_event("startup")
def startup():
    try:
        reps = load_reps(REPS_FILE)
        store["active_reps"] = [r for r in reps if r["Active"] == "Yes"]
        store["index"] = 0
        print(f"✅ Loaded {len(store['active_reps'])} active reps")
    except FileNotFoundError:
        print(f"⚠️  {REPS_FILE} not found — add reps before processing leads")


# ──────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────

@app.get("/")
def health_check():
    """
    Used by UptimeRobot / Make to keep server awake.
    Also confirms deployment is working.
    """
    return {
        "status":  "running",
        "message": "Lead Management API is live",
        "reps":    len(store["active_reps"])
    }


@app.post("/leads")
def receive_lead(lead: Lead):
    """
    Main webhook endpoint.
    Make / n8n / any form posts here.

    Expected body:
    {
        "Name":  "John Doe",
        "Email": "john@example.com",
        "Phone": "+919876543210"
    }
    """

    # ── Step 1: Validate ──
    valid_phone = is_phone_number_valid(lead.Phone)
    valid_email = is_valid_email_advanced(lead.Email)

    if not lead.Name.strip():
        raise HTTPException(status_code=422, detail="Name is required")

    if not valid_email:
        raise HTTPException(status_code=422, detail=f"Invalid email: {lead.Email}")

    if not valid_phone:
        raise HTTPException(status_code=422, detail=f"Invalid phone: {lead.Phone}")

    # ── Step 2: Build clean lead ──
    clean_lead = {
        "Name":  lead.Name.strip(),
        "Email": valid_email,
        "Phone": valid_phone,
    }

    # ── Step 3: Assign rep ──
    clean_lead = assign_rep(clean_lead)

    # ── Step 4: Save to Google Sheets ──
    try:
        append_to_sheet(clean_lead)
    except Exception as e:
        print(f"⚠️  Sheets error: {e}")

    # ── Step 5: Notify Slack ──
    try:
        send_slack(
            f"🆕 *New Lead Assigned*\n"
            f"*Name:* {clean_lead['Name']}\n"
            f"*Email:* {clean_lead['Email']}\n"
            f"*Phone:* {clean_lead['Phone']}\n"
            f"*Assigned To:* {clean_lead['Assigned_to']}"
        )
    except Exception as e:
        print(f"⚠️  Slack error: {e}")

    return {
        "status":      "received",
        "assigned_to": clean_lead["Assigned_to"]
    }


# ──────────────────────────────────────────
#  CORE FUNCTIONS
# ──────────────────────────────────────────

def load_reps(filename):
    data = []
    with open(filename, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append(row)
    return data


def is_phone_number_valid(phone_number_str, region="IN"):
    try:
        parsed_number = phonenumbers.parse(phone_number_str, region)
        is_valid      = phonenumbers.is_valid_number(parsed_number)
        is_possible   = phonenumbers.is_possible_number(parsed_number)
        if is_valid and is_possible:
            return phonenumbers.format_number(
                parsed_number,
                phonenumbers.PhoneNumberFormat.E164
            )
        return False
    except phonenumbers.NumberParseException:
        return False


def is_valid_email_advanced(email):
    try:
        v = validate_email(email, check_deliverability=False)
        return v.normalized
    except EmailNotValidError:
        return False


def assign_rep(lead: dict) -> dict:
    """Round-robin assignment using shared store."""
    reps  = store["active_reps"]
    index = store["index"]

    if not reps:
        raise HTTPException(status_code=500, detail="No active reps available")

    rep = reps[index % len(reps)]
    store["index"] += 1

    lead["Assigned_to"]    = rep["Name"]
    lead["Status"]         = "New"
    lead["Follow-Up Date"] = date.today().isoformat()
    return lead


def get_gspread_client():
    """
    Returns a gspread client.
    Local  → reads credentials from configs/credentials.json file
    Render → reads credentials from GOOGLE_CREDENTIALS env variable
    """
    if GOOGLE_CREDENTIALS_JSON:
        # Render/Railway: credentials stored as env variable (JSON string)
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)
    else:
        # Local: reads from file
        return gspread.service_account(filename=GOOGLE_CREDENTIALS_PATH)


def append_to_sheet(lead: dict):
    """Appends one lead row to Google Sheets."""
    gc    = get_gspread_client()
    sh    = gc.open_by_key(SHEET_ID)
    sheet = sh.sheet1

    # Write header if sheet is empty
    if not sheet.row_values(1):
        sheet.append_row(list(lead.keys()))

    sheet.append_row(list(lead.values()))


def send_slack(message: str):
    requests.post(
        SLACK_WEBHOOK,
        data=json.dumps({"text": message}),
        headers={"Content-Type": "application/json"},
        timeout=5
    )
