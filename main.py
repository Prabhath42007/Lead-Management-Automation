"""
LEAD MANAGEMENT AUTOMATION - Webhook Version
Receives leads via HTTP POST instead of CSV files.

Local:  uvicorn main:app --reload
Test:   http://127.0.0.1:8000/docs
Deploy: Render / Railway
"""

import json
import os
import requests
import phonenumbers
import gspread,logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from email_validator import validate_email, EmailNotValidError
from pathlib import Path
from datetime import date
from google.oauth2.service_account import Credentials


SHEET_ID      = os.getenv("SHEET_ID",      "1SVCTqraL8aKz71PsB43_X2TG7Xo1BKpIkfLO0aJjOpQ")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "https://hooks.slack.com/services/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")
REPS_SHEET    = "Reps"

# Google credentials
# Local  → reads from credentials.json file
# Render → reads from GOOGLE_CREDENTIALS environment variable (paste JSON contents)
GOOGLE_CREDENTIALS_PATH =Path(r" C:/Python_projects/configs/credentials.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")

store = {
    "active_reps": [],
    "index": 0
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
    )
logger = logging.getLogger()

app = FastAPI(
    title="Lead Management API",
    description="Receives leads via webhook instead of CSV",
    version="1.0.0"
)


class Lead(BaseModel):
    Name:   str
    Email:  str
    Phone:  str
    Client: str = "default"

@app.on_event("startup")
def startup():
    load_reps()
    logger.info(f"Loaded {len(store['active_reps'])} active reps")

@app.get("/")
def health_check():
    """Used by UptimeRobot to keep server awake."""
    return {
        "status":  "running",
        "message": "Lead Management API is live",
        "reps":    len(store["active_reps"])
    }


@app.post("/reload-reps")
def reload_reps():
    """
    Call this after editing the Reps sheet in Google Sheets.
    POST https://your-app.onrender.com/reload-reps
    """
    load_reps()
    return {
        "status": "reloaded",
        "reps":   len(store["active_reps"])
    }


@app.post("/leads")
def receive_lead(lead: Lead):
    """
    Main webhook endpoint. Make / n8n posts here.

    Expected body:
    {
        "Name":   "John Doe",
        "Email":  "john@example.com",
        "Phone":  "+919876543210",
        "Client": "client_a"
    }
    """

    valid_phone = is_phone_number_valid(lead.Phone)
    valid_email = is_valid_email_advanced(lead.Email)
    try:
        if not lead.Name.strip():
            raise Exception("Name is required")
        if not valid_email:
            raise Exception(f"Invalid email: {lead.Email}")
        if not valid_phone:
            raise Exception(f"Invalid phone: {lead.Phone}")
    except Exception as e:
        logger.error(f"⚠️  Validation error: {e}")
        return {
            "status": "error",
            "message": str(e)
        }
    clean_lead = {
        "Name":   lead.Name.strip(),
        "Email":  valid_email,
        "Phone":  valid_phone,
        "Client": lead.Client.strip() 
    }
    clean_lead = assign_rep(clean_lead, lead.Client)

    try:
        append_to_sheet(clean_lead)
        logger.info(f"Lead saved for {clean_lead['Name']} assigned to {clean_lead['Assigned_to']}")
    except Exception as e:
        logger.error(f"⚠️  Sheets error: {e}")

    try:
        send_slack(
            f"🆕 *New Lead Assigned*\n"
            f"*Name:* {clean_lead['Name']}\n"
            f"*Email:* {clean_lead['Email']}\n"
            f"*Phone:* {clean_lead['Phone']}\n"
            f"*Assigned To:* {clean_lead['Assigned_to']}"
        )
        logger.info(f"Slack notification sent for {clean_lead['Name']}")
    except Exception as e:
        logger.error(f"⚠️  Slack error: {e}")

    return {
        "status":"received",
        "Name": f"{clean_lead['Name']}",
        "Email":f"{clean_lead['Email']}",
        "Phone": f"{clean_lead['Phone']}",
        "assigned_to": f"{clean_lead['Assigned_to']}"
    }

def get_gspread_client():
    """
    Returns a gspread client.
    Local  → reads from configs/credentials.json
    Render → reads from GOOGLE_CREDENTIALS env variable
    """
    if GOOGLE_CREDENTIALS_JSON:
        # Render: credentials stored as environment variable
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


def load_reps():
    """Loads reps from the Reps tab in Google Sheets."""
    try:
        gc    = get_gspread_client()          # ← uses get_gspread_client()
        sh    = gc.open_by_key(SHEET_ID)
        sheet = sh.worksheet(REPS_SHEET)
        reps  = sheet.get_all_records()

        store["active_reps"] = [r for r in reps if str(r["Active"]).strip() == "Yes"]
        store["index"] = 0

    except Exception as e:
        logger.error(f"⚠️  Could not load reps: {e}")


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


def assign_rep(lead: dict, client_id: str) -> dict:
    """Round-robin assignment filtered by client."""

    # Get reps for this specific client
    client_reps = [
        r for r in store["active_reps"]
        if str(r.get("Client", "default")).strip() == client_id
    ]

    # Fall back to all active reps if none assigned to this client
    if not client_reps:
        client_reps = store["active_reps"]

    if not client_reps:
        raise HTTPException(status_code=500, detail="No active reps available")

    # Each client gets its own round-robin index
    idx_key = f"index_{client_id}"
    index   = store.setdefault(idx_key, 0)
    rep = client_reps[index % len(client_reps)]
    store[idx_key] += 1
    del lead["Client"] 
    lead["Assigned_to"]    = rep["Name"]
    lead["Status"]         = "New"
    lead["Follow-Up Date"] = date.today().isoformat()
    return lead


def append_to_sheet(lead: dict):
    """Appends one lead row to Google Sheets."""
    gc    = get_gspread_client()              # ← uses get_gspread_client()
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
