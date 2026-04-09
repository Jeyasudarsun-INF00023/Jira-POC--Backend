import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
EMAIL = os.getenv("JIRA_EMAIL")
API_TOKEN = os.getenv("JIRA_API_TOKEN")
PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

def fetch_jira_issues():
    """
    Step 4: Fetch real tickets from Jira API using the latest search/jql endpoint.
    """
    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"

    headers = {
        "Accept": "application/json"
    }

    # JQL to search for issues in the specified project
    query = {
        "jql": f"project={PROJECT_KEY} ORDER BY created DESC",
        "maxResults": 10
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=query,
            auth=HTTPBasicAuth(EMAIL, API_TOKEN)
        )
        if response.status_code != 200:
            print(f"Jira API Error {response.status_code}: {response.text}")
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Exception during Jira fetch: {str(e)}")
        return {"issues": []}

def transform_issues(data):
    """
    Step 5: Convert Jira data into a cleaner UI format.
    """
    issues = []
    
    # Check if 'issues' key exists and is a list
    if not isinstance(data.get("issues"), list):
        return issues

    for issue in data["issues"]:
        fields = issue.get("fields", {})
        issues.append({
            "key": issue.get("key"),
            "summary": fields.get("summary", "No Summary"),
            "description": fields.get("description", ""),
            "priority": fields.get("priority", {}).get("name", "Medium") if fields.get("priority") else "Medium",
            "status": fields.get("status", {}).get("name", "To Do"),
            "reporter_email": fields.get("reporter", {}).get("emailAddress") or fields.get("reporter", {}).get("displayName", "unknown"),
            "timestamp": fields.get("created", ""),
            "type": "Pending",
            "action": "Pending"
        })

    return issues
