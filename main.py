import os
import json
import requests
from google import genai
from openai import OpenAI
import shutil
from fastapi import FastAPI, Request, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy.orm import Session

# Local Project Imports
from jira_service import fetch_jira_issues, transform_issues, transition_jira_issue, PROJECT_KEY
from email_utils import send_email_via_n8n
from database import init_db, SessionLocal, IncidentDB

load_dotenv()

# Initialize Database on Startup
init_db()

# AI Clients
PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "KAN")
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
gemini_api_key = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=gemini_api_key) if gemini_api_key else None
app = FastAPI(title="Jira Incident Automation Agent")

# Auto-resolve background task
async def auto_resolve_checker():
    print("Background task: Auto-resolve checker started.")
    while True:
        await asyncio.sleep(60) # Check every minute
        db = SessionLocal()
        try:
            five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
            # Find issues in progress for more than 5 mins
            expired_issues = db.query(IncidentDB).filter(
                IncidentDB.status == "In Progress",
                IncidentDB.in_progress_at <= five_mins_ago
            ).all()

            if expired_issues:
                print(f"Auto-resolve check: Found {len(expired_issues)} incidents ready for resolution.")
            
            for issue in expired_issues:
                print(f"Auto-resolving {issue.key} after 5 mins...")
                success, msg = transition_jira_issue(issue.key, "Resolved")
                if success:
                    issue.status = "Resolved"
                    issue.action = "Auto-resolved after 5 minutes"
                    db.commit()
                    update_jira_ticket(issue.key, "AI Agent: Incident automatically resolved after 5 minutes of inactivity.")
                else:
                    print(f"Failed to auto-resolve {issue.key}: {msg}")
        except Exception as e:
            print(f"Auto-resolve error: {e}")
        finally:
            db.close()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_resolve_checker())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    print("Home endpoint hit")
    return {"message": "Jira AI Agent is running"}

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

def ai_agent_decision(summary, description, email, history_context="No previous history"):
    prompt = f"""
    You are an enterprise IT incident automation agent.

    Your job:
    - Understand the issue
    - Identify intent
    - Identify application/system
    - Decide best action
    - Generate user-ready response

    Allowed actions:
    - send_email
    - resolve_ticket
    - escalate

    Rules:
    - Be precise and professional.
    - Prefer self-service solutions with clear, numbered steps.
    - Use real links:
        * Google Account: https://accounts.google.com/signin/recovery
        * Office 365: https://passwordreset.microsoftonline.com
        * Software: Provide direct official download links (e.g., VS Code, Chrome, etc. if known)
    - Specific Guidance:
        * For "WiFi/Network" issues, provide steps like checking the SSID, toggling WiFi, and forgetting the network.
        * For "Performance/Slow Internet", suggest clearing browser cache, checking background apps, or running a speed test.
        * For "Software Requests", identify the software and provide its official download URL.
    - Avoid generic answers; be helpful.

    Return ONLY JSON:

    {{
      "intent": "...",
      "application": "...",
      "action_type": "send_email | resolve_ticket | escalate",
      "email_subject": "...",
      "email_body": "...",
      "confidence": "high | medium | low"
    }}

    History (Previous Tickets):
    {history_context}

    Incident:
    Summary: {summary}
    Description: {description}
    User Email: {email}
    """

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )

        content = response.choices[0].message.content.strip()

        # Extract JSON safely
        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1: raise ValueError("No JSON found in response")

        return json.loads(content[start:end])

    except Exception as e:
        print("AI Error:", e)
        return {
            "intent": "UNKNOWN",
            "application": "unknown",
            "action_type": "escalate",
            "email_subject": "AI Error",
            "email_body": f"Agent failed: {str(e)}",
            "confidence": "low"
        }

def validate_action(ai_decision):
    # Example safety check: Avoid destructive actions suggested in email
    body = ai_decision.get("email_body", "").lower()
    unsafe_keywords = ["delete all", "format drive", "uninstall everything"]
    for word in unsafe_keywords:
        if word in body:
            return False
    return True


@app.get("/incidents")
async def get_incidents(db: Session = Depends(get_db)):
    print("[GET /incidents] Request received")
    try:
        print(f"Fetching from Jira (Project: {PROJECT_KEY})...")
        raw_data = fetch_jira_issues()
        real_issues = transform_issues(raw_data)
        print(f"Found {len(real_issues)} issues in Jira")
        
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
        simulated = []
        for i in db_issues:
            if not any(r["key"] == i.key for r in real_issues):
                try:
                    ts = i.timestamp.isoformat() if i.timestamp else ""
                except:
                    ts = ""
                simulated.append({
                    "key": i.key, "summary": i.summary, "reporter_email": i.reporter_email,
                    "priority": i.priority, "status": i.status, "type": i.type,
                    "action": i.action, "confidence": i.confidence, "timestamp": ts,
                    "project": i.project, "issuetype": i.issuetype, "assignee": i.assignee,
                    "duedate": i.duedate, "labels": i.labels, "team": i.team, "start_date": i.start_date
                })
        
        combined = real_issues + simulated
        # Filter out any issues that might have broken timestamps for sorting
        sorted_incidents = sorted(combined, key=lambda x: str(x.get("timestamp", "")), reverse=True)
        print(f"Returning {len(sorted_incidents)} incidents ({len(real_issues)} from Jira, {len(simulated)} from DB)")
        return {"incidents": sorted_incidents, "jira_error": False}
    except Exception as e:
        print(f"get_incidents error: {e}")
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
            key=j_incident["key"], 
            summary=j_incident["summary"], 
            description=j_incident.get("description", ""),
            reporter_email=j_incident["reporter_email"], 
            priority=j_incident["priority"],
            project=j_incident.get("project"), 
            issuetype=j_incident.get("issuetype"),
            assignee=j_incident.get("assignee"),
            duedate=j_incident.get("duedate"),
            labels=j_incident.get("labels"),
            team=j_incident.get("team"),
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

@app.post("/resolve/{issue_key}")
async def resolve_manual(issue_key: str, db: Session = Depends(get_db)):
    success, message = transition_jira_issue(issue_key, "Resolved")
    if success:
        update_incident_state_db(db, issue_key, status="Resolved", action="Manually Resolved")
        update_jira_ticket(issue_key, "AI Agent: Incident resolved by manual confirmation.", status="Resolved")
        return {"status": "resolved", "issue_key": issue_key}
    else:
        return {"status": "error", "message": message}

@app.post("/webhook")
async def handle_jira_webhook(payload: dict, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    print("[POST /webhook] DATA RECEIVED:")
    try:
        print(json.dumps(payload, indent=2)[:1000])
    except:
        print("Could not print full payload due to encoding")
    issue = payload.get("issue", {})
    issue_key = issue.get("key")
    if not issue_key:
        print("Webhook received but no issue key found in payload.")
        return {"status": "ignored"}
    
    print(f"Processing Webhook for Issue: {issue_key}")

    existing = db.query(IncidentDB).filter(IncidentDB.key == issue.get("key")).first()
    if not existing:
        # Use transform_issues logic for consistency
        transformed = transform_issues({"issues": [issue]})[0]
        new_inc = IncidentDB(
            key=transformed["key"],
            summary=transformed["summary"],
            description=transformed["description"],
            priority=transformed["priority"],
            reporter_email=transformed["reporter_email"],
            project=transformed["project"],
            issuetype=transformed["issuetype"],
            assignee=transformed["assignee"],
            duedate=transformed["duedate"],
            labels=transformed["labels"],
            team=transformed["team"],
            start_date=transformed["start_date"],
            reporter_id=transformed.get("reporter_id"),
            status="Processing",
            timestamp=datetime.now()
        )
        db.add(new_inc)
    else:
        existing.status = "Processing"
    
    db.commit()
    print(f"Issue {issue_key} saved/updated in DB. Starting background process...")
    background_tasks.add_task(process_incident, issue_key)
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

    if not incident:
        print(f"Incident {issue_key} not found")
        return

    print(f"Processing {issue_key}")

    try:
        # Move to In Progress
        transition_jira_issue(issue_key, "In Progress")
        update_incident_state_db(db, issue_key, status="In Progress", in_progress_at=datetime.utcnow())

        #  CONTEXT AWARENESS (Memory)
        previous_tickets = db.query(IncidentDB).filter(
            IncidentDB.reporter_email == incident.reporter_email,
            IncidentDB.key != issue_key
        ).order_by(IncidentDB.timestamp.desc()).limit(3).all()
        
        history_text = "\n".join([f"- {t.key}: {t.summary} ({t.status})" for t in previous_tickets]) if previous_tickets else "No previous history"

        #  AI DECISION
        ai_result = ai_agent_decision(
            incident.summary,
            incident.description or "",
            incident.reporter_email or "unknown",
            history_context=history_text
        )

        print("AI Decision:", ai_result)

        confidence = ai_result.get("confidence", "low")

        #  SAFETY & CONFIDENCE CHECK
        if confidence == "low":
            update_incident_state_db(db, issue_key, status="Escalated", action="AI not confident")
            update_jira_ticket(issue_key, "AI Agent: Could not confidently resolve. Escalated to human IT support.")
            return

        if not validate_action(ai_result):
            update_incident_state_db(db, issue_key, status="Escalated", action="Safety validation failed")
            update_jira_ticket(issue_key, "AI Agent: Safety check failed. Escalating.")
            return

        action = ai_result.get("action_type")
        print(f"AI Result Action: {action}")

        #  EXECUTION LAYER
        if action == "send_email":
            email = incident.reporter_email
            print(f"Attempting to send email to: {email}")
            if not email or "@" not in email:
                email = os.getenv("JIRA_EMAIL")
                print(f"Using fallback email: {email}")

            print(f"Triggering n8n for {issue_key} (Email: {email}, Subject: {ai_result.get('email_subject')})")
            success = send_email_via_n8n(
                email,
                ai_result.get("email_subject", "IT Support Update"),
                ai_result.get("email_body", ""),
                issue_key
            )
            if success:
                msg = "Email sent via AI"
                update_incident_state_db(db, issue_key, status="In Progress", action=msg)
                # Enhanced Comment with Mention
                tag_comment = "Mail received for you to follow those steps and clear your incident."
                update_jira_ticket(issue_key, tag_comment, mention_id=incident.reporter_id)
            else:
                update_incident_state_db(db, issue_key, status="Failed", action="Email failed")

        elif action == "resolve_ticket":
            success, msg = transition_jira_issue(issue_key, "Resolved")
            status = "Resolved" if success else "Failed"
            action_msg = msg

        else:
            status = "Escalated"
            action_msg = "Escalated by AI"

        # Final DB Update for non-email actions
        if action != "send_email":
            update_incident_state_db(
                db,
                issue_key,
                status=status,
                type=ai_result.get("intent"),
                action=action_msg,
                confidence=confidence
            )

            #  Add Jira Comment for non-email actions
            update_jira_ticket(
                issue_key,
                f"AI Agent:\nIntent: {ai_result.get('intent')}\nAction: {action_msg}\nConfidence: {confidence}"
            )

    except Exception as e:
        print("Error:", e)
        update_incident_state_db(db, issue_key, status="Error")

    finally:
        db.close()

def update_jira_ticket(issue_key: str, comment: str, mention_id: str = None):
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}/comment"
    
    content = []
    if mention_id:
        content.append({
            "type": "mention",
            "attrs": {"id": mention_id, "text": "@user"}
        })
        content.append({"type": "text", "text": " "})
    
    content.append({"type": "text", "text": comment})

    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": content}]
        }
    }
    try:
        requests.post(url, json=payload, auth=JIRA_AUTH, headers={"Content-Type": "application/json"})
    except Exception as e:
        print(f"Jira comment error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
