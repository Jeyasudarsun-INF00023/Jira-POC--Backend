import requests
import os
from dotenv import load_dotenv

load_dotenv()

def send_email_via_n8n(email, subject, body, issue_key):
    """
    Sends email data to a n8n webhook instead of direct SMTP.
    """
    webhook_url = os.getenv("N8N_WEBHOOK_URL")
    print(f"Attempting n8n trigger to: {webhook_url[:30]}...")
    
    if not webhook_url:
        print("N8N_WEBHOOK_URL missing in .env")
        return False

    payload = {
        "email": email,
        "subject": subject,
        "message": body, # n8n node expects 'message'
        "issuekey": issue_key
    }

    headers = {
        "Content-Type": "application/json"
    }

    try:
        # User's n8n URL from request
        response = requests.post(webhook_url, json=payload, headers=headers)
        response.raise_for_status()
        print(f"[n8n Webhook Sent] To: {email} | Status: {response.status_code}")
        return True
    except Exception as e:
        print(f"Failed to reach n8n: {str(e)}")
        return False
