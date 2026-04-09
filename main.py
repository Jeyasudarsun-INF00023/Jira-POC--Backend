import os
import json
import requests
import shutil
from fastapi import FastAPI, Request, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from sqlalchemy.orm import Session

# Local Project Imports
from jira_service import fetch_jira_issues, transform_issues
from email_utils import send_email_via_n8n
from database import init_db, SessionLocal, IncidentDB

load_dotenv()

# Initialize Database on Startup
init_db()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_FALLBACK_MODELS = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash"]

client = genai.Client(api_key=GEMINI_API_KEY)
app = FastAPI(title="Jira Incident Automation Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "Jira AI Agent is running 🚀"}

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return None

# Jira Configuration
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_AUTH = (JIRA_EMAIL, JIRA_API_TOKEN)

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class JiraWebhook(BaseModel):
    issue: dict
    timestamp: int

# ---------------------------------------------------------
# Step 2: Agent Service (Core Engine)
# ---------------------------------------------------------

def detect_intent(summary: str, description: str):
    prompt = f"""
    Classify the IT incident into one of these intents: RESET_PASSWORD, SERVICE_DOWN, PERFORMANCE_ISSUE, DISK_FULL, or UNKNOWN.
    Summary: {summary}
    Description: {description}
    Return JSON ONLY:
    {{
      "intent": "RESET_PASSWORD | SERVICE_DOWN | PERFORMANCE_ISSUE | DISK_FULL | UNKNOWN",
      "application": "...",
      "confidence": "high | medium | low",
      "missing_info": "..."
    }}
    """
    if not GEMINI_API_KEY: return {"intent": "UNKNOWN", "application": "unknown", "confidence": "low"}

    for model_name in GEMINI_FALLBACK_MODELS:
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            text = (response.text or "").strip()
            start_idx = text.find('{')
            end_idx = text.rfind('}') + 1
            if start_idx == -1: continue
            return json.loads(text[start_idx:end_idx])
        except: continue
    return {"intent": "UNKNOWN", "application": "unknown", "confidence": "low"}

def handle_ai_action(intent_data: dict, email: str, issue_key: str):
    intent = intent_data.get("intent")
    app_name = intent_data.get("application", "unknown").lower()
    summary_lower = intent_data.get("summary", "").lower()
    
    if app_name == "unknown":
        if "office 365" in summary_lower: app_name = "Office 365"
        elif "google" in summary_lower: app_name = "Google"

    if intent == "RESET_PASSWORD":
        reset_link = "https://your-company.com/reset"
        if "office 365" in app_name.lower(): reset_link = "https://passwordreset.microsoftonline.com"
        elif "google" in app_name.lower(): reset_link = "https://accounts.google.com/signin/recovery"
        
        subject = f"Action Required: {app_name} Password Reset"
        body = f"Hello, we detected a password reset request for {app_name.upper()}.\n\nLink: {reset_link}"
        send_email_via_n8n(email, subject, body, issue_key)
        return f"SENT: {app_name} recovery link sent to {email}"
            
    elif intent == "DISK_FULL": return "Automation: Clearing temp files."
    elif intent == "SERVICE_DOWN": return "Automation: Restarting service."
    return "I need more details to automate a fix."

@app.get("/incidents")
async def get_incidents(db: Session = Depends(get_db)):
    try:
        raw_data = fetch_jira_issues()
        real_issues = transform_issues(raw_data)
        
        db_issues = db.query(IncidentDB).all()
        db_map = {i.key: i for i in db_issues}
        
        for issue in real_issues:
            stored = db_map.get(issue["key"])
            if stored:
                issue.update({
                    "status": stored.status or issue["status"],
                    "type": stored.type or "Pending",
                    "action": stored.action or "Pending",
                    "confidence": stored.confidence or "N/A"
                })
        
        # Include simulated or Jira-deleted ones if they are in DB
        simulated = [
            {
                "key": i.key, "summary": i.summary, "reporter_email": i.reporter_email,
                "priority": i.priority, "status": i.status, "type": i.type,
                "action": i.action, "confidence": i.confidence, "timestamp": i.timestamp.isoformat()
            } 
            for i in db_issues if not any(r["key"] == i.key for r in real_issues)
        ]
        
        combined = real_issues + simulated
        return {"incidents": sorted(combined, key=lambda x: x.get("timestamp", ""), reverse=True)}
    except Exception as e:
        print(f"Error: {e}")
        return {"incidents": [], "jira_error": True}

@app.post("/analyze-fix/{issue_key}")
async def analyze_and_fix(issue_key: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    incident = db.query(IncidentDB).filter(IncidentDB.key == issue_key).first()
    if not incident:
        raw_data = fetch_jira_issues()
        real_issues = transform_issues(raw_data)
        j_incident = next((i for i in real_issues if i["key"] == issue_key), None)
        if not j_incident: return {"status": "error", "message": "Not found"}
        
        incident = IncidentDB(
            key=j_incident["key"], summary=j_incident["summary"], 
            reporter_email=j_incident["reporter_email"], priority=j_incident["priority"],
            status="Processing"
        )
        db.add(incident)
        db.commit()

    background_tasks.add_task(process_incident, issue_key)
    return {"status": "processing", "issue_key": issue_key}

@app.post("/retry/{issue_key}")
async def retry_automation(issue_key: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_incident, issue_key)
    return {"status": "retrying", "issue_key": issue_key}

@app.post("/escalate/{issue_key}")
async def escalate_manual(issue_key: str, db: Session = Depends(get_db)):
    update_incident_state_db(db, issue_key, status="Escalated (Manual)")
    update_jira_ticket(issue_key, "Manually escalated.")
    return {"status": "escalated", "issue_key": issue_key}

@app.post("/webhook")
async def handle_jira_webhook(payload: JiraWebhook, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    issue = payload.issue
    fields = issue.get("fields", {})

    existing = db.query(IncidentDB).filter(IncidentDB.key == issue.get("key")).first()
    if not existing:
        new_inc = IncidentDB(
            key=issue.get("key"),
            summary=fields.get("summary", "No Summary"),
            description=fields.get("description", ""),
            priority=fields.get("priority", {}).get("name", "Medium") if fields.get("priority") else "Medium",
            reporter_email=fields.get("reporter", {}).get("emailAddress") or fields.get("reporter", {}).get("displayName", "unknown"),
            status="Processing",
            timestamp=datetime.now()
        )
        db.add(new_inc)
    else:
        existing.status = "Processing"
    
    db.commit()
    background_tasks.add_task(process_incident, issue.get("key"))
    return {"status": "accepted"}

@app.delete("/incidents/{issue_key}")
async def delete_incident(issue_key: str, db: Session = Depends(get_db)):
    incident = db.query(IncidentDB).filter(IncidentDB.key == issue_key).first()
    if incident:
        db.delete(incident)
        db.commit()
        return {"status": "deleted"}
    return {"status": "not_found"}

def update_incident_state_db(db: Session, key: str, **kwargs):
    incident = db.query(IncidentDB).filter(IncidentDB.key == key).first()
    if incident:
        for k, v in kwargs.items(): setattr(incident, k, v)
        db.commit()

async def process_incident(issue_key: str):
    db = SessionLocal()
    incident = db.query(IncidentDB).filter(IncidentDB.key == issue_key).first()
    if not incident: return

    try:
        update_incident_state_db(db, issue_key, status="Detecting Intent")
        intent_data = detect_intent(incident.summary, incident.description or "")
        
        intent = intent_data.get("intent", "UNKNOWN")
        confidence = intent_data.get("confidence", "low")
        
        if confidence == "low" or intent == "UNKNOWN":
            fallback = classify_incident_text(incident.summary, incident.description or "")
            if fallback != "UNKNOWN":
                intent, confidence = fallback, "medium (fallback)"
            else:
                update_incident_state_db(db, issue_key, status="Awaiting Info", action="Asked for Details")
                update_jira_ticket(issue_key, "AI Agent: Need more details.")
                return

        update_incident_state_db(db, issue_key, type=intent, confidence=confidence, status="Deciding Action")
        intent_data["summary"] = incident.summary
        action_response = handle_ai_action(intent_data, incident.reporter_email or "unknown", issue_key)
        update_incident_state_db(db, issue_key, action=action_response)
        
        if intent == "RESET_PASSWORD":
            update_incident_state_db(db, issue_key, status="Resolved")
            update_jira_ticket(issue_key, f"AI Agent: {action_response}", status="Resolved")
        else:
            # Simple execution mock
            update_incident_state_db(db, issue_key, status="Executing")
            update_incident_state_db(db, issue_key, status="Resolved")
            update_jira_ticket(issue_key, f"AI Agent: Automation executed for {intent}.", status="Resolved")
            
    except Exception as e:
        print(f"Error: {e}")
        update_incident_state_db(db, issue_key, status=f"Error: {e}")
    finally:
        db.close()

def classify_incident_text(summary: str, description: str) -> str:
    text = (summary + " " + description).lower()
    if "password" in text or "reset" in text: return "RESET_PASSWORD"
    if "down" in text or "offline" in text: return "SERVICE_DOWN"
    if "disk" in text or "storage" in text: return "DISK_FULL"
    return "UNKNOWN"

def update_jira_ticket(issue_key: str, comment: str, status: str = None):
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}/comment"
    payload = {
        "body": {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"text": comment, "type": "text"}]}]
        }
    }
    try:
        requests.post(url, json=payload, auth=JIRA_AUTH, headers={"Content-Type": "application/json"})
    except: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
