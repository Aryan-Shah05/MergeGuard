import os
import io
import zipfile
import json
import hmac
import hashlib
import requests
from fastapi import FastAPI, Request, BackgroundTasks, status, HTTPException, Header, Depends
from pydantic import BaseModel
import uvicorn
from github import Github
from dotenv import load_dotenv
import asyncio
from graph import mergeguard_app
from agents.supervisor_agent import run_supervisor_agent
import logging

import review_store  # NEW

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
REVIEW_API_KEY = os.getenv("REVIEW_API_KEY")  # NEW — protects the approve/reject endpoints

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

gh_client = Github(GITHUB_TOKEN)

app = FastAPI(title="MergeGuard - Webhook Receiver")

review_store.init_db()  # NEW — ensures reviews.db + table exist on startup


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET or not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def require_api_key(x_api_key: str = Header(None)):
    """Simple shared-secret auth for the approve/reject endpoints — these
    will be publicly reachable once deployed, and must not be open to anyone
    who guesses a review_id."""
    if not REVIEW_API_KEY or x_api_key != REVIEW_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


async def process_ai_review(repo_full_name: str, pr_number: int, artifact_id: int = None):
    """Downloads the coverage artifact (if available), fetches the PR diff, invokes the
    AI agent graph, and SAVES the resulting report as a pending review — it no longer
    blocks on terminal input. Approval/rejection happens via separate API calls."""

    if artifact_id:
        logger.info("Downloading coverage artifact %s for %s PR #%s", artifact_id, repo_full_name, pr_number)
    else:
        logger.info("Running review without coverage artifact for %s PR #%s", repo_full_name, pr_number)

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
                            logger.info("Successfully parsed coverage.json from the cloud!")
            else:
                logger.warning("Failed to download artifact. Status code: %s", response.status_code)
        else:
            logger.info("Skipping coverage artifact download (no artifact found).")

        pull_request = repo.get_pull(pr_number)

        diff_response = requests.get(
            pull_request.diff_url,
            headers={"Accept": "application/vnd.github.v3.diff"}
        )
        pr_diff = diff_response.text if diff_response.status_code == 200 else ""
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

        logger.info("Triggering LangGraph Worker Pool (Async)...")
        final_output = await mergeguard_app.ainvoke(initial_state)
        state = final_output.model_dump() if hasattr(final_output, "model_dump") else dict(final_output)

        full_comment = state.get("final_report", "").strip()
        if not full_comment:
            full_comment = "⚠️ MergeGuard review completed, but the supervisor agent returned no report."
            state["final_report"] = full_comment

        # ---- REPLACES the old while-True input() loop ----
        review_id = review_store.save_pending_review(
            repo_name=repo_full_name,
            pr_number=pr_number,
            state=state,
            final_report=full_comment,
        )
        logger.info(
            "Review %s saved as PENDING for %s PR #%s. Approve: POST /reviews/%s/approve  "
            "Reject: POST /reviews/%s/reject",
            review_id, repo_full_name, pr_number, review_id, review_id
        )

    except Exception:
        logger.exception("Error processing AI review pipeline")


class RejectPayload(BaseModel):
    feedback: str


@app.get("/reviews")
async def list_reviews(_: None = Depends(require_api_key)):
    """Lists all reviews currently awaiting human approval."""
    return {"pending_reviews": review_store.list_pending_reviews()}


@app.get("/reviews/{review_id}")
async def get_review_detail(review_id: str, _: None = Depends(require_api_key)):
    review = review_store.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")
    return {
        "review_id": review["review_id"],
        "repo_name": review["repo_name"],
        "pr_number": review["pr_number"],
        "status": review["status"],
        "final_report": review["final_report"],
        "created_at": review["created_at"],
        "updated_at": review["updated_at"],
    }


@app.post("/reviews/{review_id}/approve")
async def approve_review(review_id: str, _: None = Depends(require_api_key)):
    review = review_store.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")
    if review["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Review is not pending (status: {review['status']}).")

    try:
        repo = gh_client.get_repo(review["repo_name"])
        pull_request = repo.get_pull(review["pr_number"])
        pull_request.create_issue_comment(review["final_report"])
    except Exception as e:
        logger.error(f"Failed to post comment to GitHub: {e}")

    review_store.mark_review_status(review_id, "approved")
    logger.info("Review %s approved and posted to %s PR #%s", review_id, review["repo_name"], review["pr_number"])
    return {"status": "approved", "message": "Comment posted to GitHub."}


@app.post("/reviews/{review_id}/reject")
async def reject_review(review_id: str, payload: RejectPayload, _: None = Depends(require_api_key)):
    review = review_store.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")
    if review["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Review is not pending (status: {review['status']}).")

    state = json.loads(review["state_json"])
    state["human_feedback"] = payload.feedback
    state["final_report"] = review["final_report"]

    supervisor_res = await run_supervisor_agent(state)
    new_report = supervisor_res["final_report"]
    state["final_report"] = new_report

    review_store.update_review_report(review_id, new_report, state)
    logger.info("Review %s regenerated based on human feedback. Still pending approval.", review_id)

    return {"status": "pending", "message": "Report regenerated. Review again before approving.", "final_report": new_report}

@app.delete("/reviews/{review_id}")
async def delete_review_endpoint(review_id: str, _: None = Depends(require_api_key)):
    deleted = review_store.delete_review(review_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Review not found.")
    logger.info("Review %s deleted.", review_id)
    return {"status": "deleted", "review_id": review_id}

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

        if action == "completed" and conclusion in ["success", "failure"] and workflow_run.get("name") == "PR Test Coverage":
            repo_name = payload.get("repository", {}).get("full_name")
            pull_requests = workflow_run.get("pull_requests", [])
            if not pull_requests:
                return {"status": "ignored", "reason": "No active pull request tied to this workflow run."}

            pr_number = pull_requests[0].get("number")
            logger.info("Detected completed workflow for %s PR #%s", repo_name, pr_number)

            artifacts_url = workflow_run.get("artifacts_url")
            headers = {"Authorization": f"token {GITHUB_TOKEN}"}
            artifacts_resp = requests.get(artifacts_url, headers=headers).json()
            artifacts = artifacts_resp.get("artifacts", [])
            coverage_artifact = next((a for a in artifacts if a["name"] == "coverage-data"), None)
            artifact_id = coverage_artifact["id"] if coverage_artifact else None

            background_tasks.add_task(process_ai_review, repo_name, pr_number, artifact_id)
            return {"status": "queued", "message": "Pipeline processing started in background task."}

    return {"status": "ignored", "reason": "Event type or status not applicable."}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=9000, reload=True)