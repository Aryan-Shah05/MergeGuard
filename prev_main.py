import os
import io
import zipfile
import json
import hmac
import hashlib
import requests
from fastapi import FastAPI, Request, BackgroundTasks, status
import uvicorn
from github import Github
from dotenv import load_dotenv
import asyncio
from graph import mergeguard_app
from agents.supervisor_agent import run_supervisor_agent
import logging

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

gh_client = Github(GITHUB_TOKEN)

app = FastAPI(title="MergeGuard - Webhook Receiver")


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """Verifies GitHub's HMAC-SHA256 webhook signature so only real GitHub
    deliveries (signed with GITHUB_WEBHOOK_SECRET) can trigger the pipeline."""
    if not GITHUB_WEBHOOK_SECRET or not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def process_ai_review(repo_full_name: str, pr_number: int, artifact_id: int = None):
    """Downloads the coverage artifact (if available), fetches the PR diff, and invokes the
    async AI agent graph. Runs one PR at a time via FastAPI's background task."""
    
    if artifact_id:
        #print(f"\n📦 Downloading artifact {artifact_id} for {repo_full_name} PR #{pr_number}...")
        logger.info(
        "Downloading coverage artifact %s for %s PR #%s",
        artifact_id,
        repo_full_name,
        pr_number,
    )
    else:
        #print(f"\n📦 Running review without coverage artifact for {repo_full_name} PR #{pr_number}...")
        logger.info(
            "Running review without coverage artifact for %s PR #%s",
            repo_full_name,
            pr_number
        )

    try:
        repo = gh_client.get_repo(repo_full_name)
        
        coverage_data = {}
        if artifact_id:
            artifact_url = repo.get_artifact(artifact_id).archive_download_url
            headers = {"Authorization": f"token {GITHUB_TOKEN}"}
            response = requests.get(artifact_url, headers=headers, stream=True)

            if response.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                    if "coverage.json" in z.namelist():
                        with z.open("coverage.json") as f:
                            coverage_data = json.loads(f.read().decode("utf-8"))
                            print("✅ Successfully parsed coverage.json from the cloud!")
            else:
                #print(f"⚠️ Failed to download artifact. Status code: {response.status_code}")
                logger.warning("Failed to download artifact. Status code: %s", response.status_code)
        else:
            #print("ℹ️ Skipping coverage artifact download (no artifact found).")
            logger.info("Skipping coverage artifact download (no artifact found).")

        pull_request = repo.get_pull(pr_number)

        diff_response = requests.get(
            pull_request.diff_url,
            headers={"Accept": "application/vnd.github.v3.diff"}
        )
        pr_diff = diff_response.text if diff_response.status_code == 200 else ""
        #print("✅ Successfully fetched PR diff.")
        logger.info("Successfully fetched PR diff.")

        initial_state = {
            "repo_name": repo_full_name,
            "pr_number": pr_number,
            "pr_diff": pr_diff,
            "coverage_json": json.dumps(coverage_data),
            "security_feedback": "",
            "quality_feedback": "",
            "test_feedback": "",
            "docs_feedback": "",
            "final_report": ""
        }

        #print("⚡ Triggering LangGraph Worker Pool (Async)...")
        logger.info("Triggering LangGraph Worker Pool (Async)...")
        # Run the initial full worker graph
        final_output = await mergeguard_app.ainvoke(initial_state)
        # Safe conversion of LangGraph state to standard python dictionary
        state = final_output.model_dump() if hasattr(final_output, "model_dump") else dict(final_output)

        # -------------------------------------------------------------
        # HUMAN-IN-THE-LOOP TERMINAL INTERACTION LOOP
        # -------------------------------------------------------------
        while True:
            full_comment = state.get("final_report", "").strip()
            if not full_comment:
                full_comment = "⚠️ MergeGuard review completed, but the supervisor agent returned no report."

            print("\n" + "=" * 80)
            print("📋 GENERATED AI REVIEW COMMENT:")
            print("=" * 80)
            print(full_comment)
            print("=" * 80 + "\n")

            # Non-blocking terminal input for approval
            approval = await asyncio.to_thread(
                input, "Do you approve posting this comment to GitHub? (y/n): "
            )
            approval = approval.strip().lower()

            if approval in ["y", "yes"]:
                #print("✅ Approved by human. Posting to GitHub...")
                logger.info("Approved by human. Posting to GitHub...")
                pull_request.create_issue_comment(full_comment)
                #print(f"🎉 Successfully posted complete AI review to PR #{pr_number}!")
                logger.info("Successfully posted complete AI review to PR #%s!", pr_number)
                break
                
            elif approval in ["n", "no"]:
                # Get the human feedback for the modification
                feedback = await asyncio.to_thread(
                    input, "\nEnter your feedback/changes for the supervisor agent: "
                )
                #print("\n🔄 Regenerating report with human feedback...")
                logger.info("Regenerating report with human feedback...")
                # Pass the feedback and previous report to the state dictionary
                state["human_feedback"] = feedback
                state["final_report"] = full_comment
                
                # Call the supervisor node directly (extremely fast; doesn't rerun specialist scans)
                supervisor_res = await run_supervisor_agent(state)
                
                # Update final_report with the refined version
                state["final_report"] = supervisor_res["final_report"]
                
            else:
                print("❌ Invalid input. Please enter 'y' or 'n'.")

    except Exception as e:
        #print(f"❌ Error processing AI review pipeline: {str(e)}")
        logger.exception("Error processing AI review pipeline")

@app.post("/webhook", status_code=status.HTTP_200_OK)
async def handle_github_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_signature(raw_body, signature):
        return {"status": "rejected", "reason": "Invalid or missing webhook signature."}

    payload = json.loads(raw_body)
    event_type = request.headers.get("X-GitHub-Event", "")

    if event_type == "workflow_run":
        action = payload.get("action")
        workflow_run = payload.get("workflow_run", {})
        conclusion = workflow_run.get("conclusion")

        # Allow both "success" and "failure" conclusions to run the review
        if action == "completed" and conclusion in ["success", "failure"] and workflow_run.get("name") == "PR Test Coverage":

            repo_name = payload.get("repository", {}).get("full_name")

            pull_requests = workflow_run.get("pull_requests", [])
            if not pull_requests:
                return {"status": "ignored", "reason": "No active pull request tied to this workflow run."}

            pr_number = pull_requests[0].get("number")
            print(f"\n--- Detected completed workflow for {repo_name} PR #{pr_number} ---")

            artifacts_url = workflow_run.get("artifacts_url")
            headers = {"Authorization": f"token {GITHUB_TOKEN}"}
            artifacts_resp = requests.get(artifacts_url, headers=headers).json()

            artifacts = artifacts_resp.get("artifacts", [])
            coverage_artifact = next((a for a in artifacts if a["name"] == "coverage-data"), None)

            # Extract artifact ID if it exists
            artifact_id = coverage_artifact["id"] if coverage_artifact else None

            background_tasks.add_task(
                process_ai_review,
                repo_name,
                pr_number,
                artifact_id
            )
            return {"status": "queued", "message": "Pipeline processing started in background task."}

    return {"status": "ignored", "reason": "Event type or status not applicable."}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=9000, reload=True)