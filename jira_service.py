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
    Step 4: Fetch real tickets from Jira API.
    """
    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"
    headers = {"Accept": "application/json"}
    
    # Explicitly request fields to avoid missing ones
    query = {
        "jql": f"project={PROJECT_KEY} ORDER BY created DESC",
        "maxResults": 20,
        "fields": "*all" # Request all fields to ensure Space, Team, etc are included
    }

    try:
        response = requests.get(url, headers=headers, params=query, auth=HTTPBasicAuth(EMAIL, API_TOKEN))
        print(f"Jira API Call: {url} | Status: {response.status_code}")
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Jira fetch error: {str(e)}")
        return {"issues": []}

def transition_jira_issue(issue_key, target_status_name):
    """
    Step 6: Transition a Jira issue to a new status by name (e.g., 'In Progress', 'Resolved').
    """
    transitions_url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}/transitions"
    
    auth = HTTPBasicAuth(EMAIL, API_TOKEN)
    headers = {"Accept": "application/json"}

    try:
        # 1. Get available transitions
        response = requests.get(transitions_url, auth=auth, headers=headers)
        if response.status_code != 200:
            return False, f"API Error {response.status_code}: {response.text}"
            
        transitions = response.json().get("transitions", [])
        
        # 2. Find transition ID for the target status name
        transition_id = None
        target_name_found = ""
        for t in transitions:
            if target_status_name.lower() in t["name"].lower():
                transition_id = t["id"]
                target_name_found = t["name"]
                break
        
        if not transition_id:
            # Try some common Fallbacks if 'Resolved' or 'In Progress' failed
            search_name = target_status_name.lower()
            if search_name == "resolved":
                fallbacks = ["done", "close", "finish", "complete", "fix", "resolve"]
                for t in transitions:
                    t_name = t["name"].lower()
                    if any(f in t_name for f in fallbacks):
                        transition_id = t["id"]
                        target_name_found = t["name"]
                        break
            elif search_name == "in progress":
                fallbacks = ["process", "start", "active", "develop", "progress"]
                for t in transitions:
                    t_name = t["name"].lower()
                    if any(f in t_name for f in fallbacks):
                        transition_id = t["id"]
                        target_name_found = t["name"]
                        break

        if transition_id:
            payload = {"transition": {"id": transition_id}}
            post_response = requests.post(transitions_url, json=payload, auth=auth, headers={"Content-Type": "application/json"})
            if post_response.status_code in [204, 200]:
                print(f"Successfully transitioned {issue_key} to {target_name_found}")
                return True, f"Success: {target_name_found}"
            else:
                error_msg = post_response.text
                # Try resolution fallback
                if "resolution" in error_msg.lower():
                    # Attempt common resolution names
                    for res_name in ["Done", "Fixed", "Resolved", "Complete"]:
                        payload["fields"] = {"resolution": {"name": res_name}}
                        retry_resp = requests.post(transitions_url, json=payload, auth=auth, headers={"Content-Type": "application/json"})
                        if retry_resp.status_code in [204, 200]:
                            return True, f"Success with resolution: {res_name}"
                return False, f"Transition failed: {error_msg}"
        else:
            available = [t["name"] for t in transitions]
            return False, f"Status '{target_status_name}' not found. Available: {available}"
            
    except Exception as e:
        return False, f"Exception: {str(e)}"

def transform_issues(data):
    """
    Step 5: Convert Jira data into a cleaner UI format.
    """
    issues = []
    
    if not isinstance(data.get("issues"), list):
        return issues

    for issue in data["issues"]:
        fields = issue.get("fields", {})
        
        # Space (Project), Work Type (IssueType), Due Date, Team (Custom Field)
        # We try multiple common custom field IDs for Team if 10001 is empty
        team_val = "N/A"
        # Check customfield_10001, then 10005, then others if possible
        for tf in ["customfield_10001", "customfield_10100", "customfield_10005"]:
            val = fields.get(tf)
            if val:
                if isinstance(val, dict): team_val = val.get("name", "N/A")
                elif isinstance(val, str): team_val = val
                break

        # Standard Jira Fields with fallbacks for varied API response shapes
        project_obj = fields.get("project", {})
        project_val = project_obj.get("name") if isinstance(project_obj, dict) else project_obj or "N/A"
        
        type_obj = fields.get("issuetype", {})
        type_val = type_obj.get("name") if isinstance(type_obj, dict) else type_obj or "Incident"
        
        assignee_obj = fields.get("assignee")
        assignee_val = "Unassigned"
        if isinstance(assignee_obj, dict):
            assignee_val = assignee_obj.get("displayName") or assignee_obj.get("emailAddress") or "Unassigned"
        elif isinstance(assignee_obj, str):
            assignee_val = assignee_obj

        issues.append({
            "key": issue.get("key"),
            "summary": fields.get("summary", "No Summary"),
            "description": fields.get("description", ""),
            "priority": fields.get("priority", {}).get("name", "Medium") if isinstance(fields.get("priority"), dict) else fields.get("priority") or "Medium",
            "status": fields.get("status", {}).get("name", "To Do") if isinstance(fields.get("status"), dict) else fields.get("status") or "To Do",
            "reporter_email": (fields.get("reporter", {}).get("emailAddress") or fields.get("reporter", {}).get("displayName", "unknown")) if isinstance(fields.get("reporter"), dict) else "unknown",
            "timestamp": fields.get("created", ""),
            "project": project_val,
            "issuetype": type_val,
            "assignee": assignee_val,
            "duedate": fields.get("duedate") or "No Due Date",
            "labels": ", ".join(fields.get("labels", [])) if isinstance(fields.get("labels"), list) else "",
            "team": team_val,
            "start_date": fields.get("customfield_10015") or "N/A",
            "reporter_id": fields.get("reporter", {}).get("accountId") if isinstance(fields.get("reporter"), dict) else None,
            "type": "Pending",
            "action": "Pending"
        })
    print(f"Transformed {len(issues)} issues from Jira.")
    return issues
