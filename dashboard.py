import os
import streamlit as st
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="MergeGuard - Review Dashboard", layout="centered")

st.title("MergeGuard")
st.caption("Pending review dashboard")

with st.sidebar:
    st.subheader("Connection")
    base_url = st.text_input(
        "API base URL",
        value=os.getenv("DASHBOARD_API_BASE_URL", "http://mergeguard:8000"),
    )
    api_key = st.text_input(
        "x-api-key",
        value=os.getenv("REVIEW_API_KEY", ""),
        type="password",
    )
    if st.button("Refresh", use_container_width=True):
        st.rerun()

if not api_key:
    st.info("REVIEW_API_KEY not found in .env — enter it manually above.")
    st.stop()

HEADERS = {"x-api-key": api_key}


def api_get(path: str):
    resp = requests.get(f"{base_url}{path}", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def api_post(path: str, json_body: dict | None = None):
    resp = requests.post(f"{base_url}{path}", headers=HEADERS, json=json_body, timeout=60)
    resp.raise_for_status()
    return resp.json()


def api_delete(path: str):
    resp = requests.delete(f"{base_url}{path}", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def format_timestamp(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%b %d, %Y %H:%M")
    except Exception:
        return ts


try:
    data = api_get("/reviews")
    reviews = data.get("pending_reviews", [])
except requests.exceptions.HTTPError as e:
    st.error(f"Failed to load reviews: {e.response.text if e.response else e}")
    st.stop()
except requests.exceptions.RequestException as e:
    st.error(f"Could not reach {base_url} — is the server running? ({e})")
    st.stop()

if not reviews:
    st.write("No pending reviews.")
    st.stop()

STATUS_COLORS = {
    "pending": "🟡",
    "approved": "🟢",
    "rejected_final": "🔴",
}

for review in reviews:
    review_id = review["review_id"]
    icon = STATUS_COLORS.get(review["status"], "⚪")

    with st.expander(
        f"{icon} {review['repo_name']} #{review['pr_number']} — {review['status']}"
    ):
        st.caption(f"ID: {review_id}  ·  Created: {format_timestamp(review['created_at'])}")

        detail_key = f"detail_{review_id}"
        if detail_key not in st.session_state:
            try:
                st.session_state[detail_key] = api_get(f"/reviews/{review_id}")
            except requests.exceptions.RequestException as e:
                st.error(f"Failed to load report: {e}")
                continue

        detail = st.session_state[detail_key]
        st.markdown(detail["final_report"])

        col1, col2, col3 = st.columns(3)

        with col1:
            if st.button("Approve", key=f"approve_{review_id}", use_container_width=True):
                try:
                    api_post(f"/reviews/{review_id}/approve")
                    st.success("Approved and posted to GitHub.")
                    del st.session_state[detail_key]
                    st.rerun()
                except requests.exceptions.RequestException as e:
                    st.error(f"Approve failed: {e}")

        with col2:
            reject_open_key = f"reject_open_{review_id}"
            if st.button("Reject", key=f"reject_{review_id}", use_container_width=True):
                st.session_state[reject_open_key] = True

        with col3:
            delete_confirm_key = f"delete_confirm_{review_id}"
            if not st.session_state.get(delete_confirm_key):
                if st.button("Delete", key=f"delete_{review_id}", use_container_width=True):
                    st.session_state[delete_confirm_key] = True
                    st.rerun()

        if st.session_state.get(f"delete_confirm_{review_id}"):
            st.warning("This permanently deletes the review from the database. Confirm?")
            dcol1, dcol2 = st.columns(2)
            with dcol1:
                if st.button("Yes, delete permanently", key=f"confirm_delete_{review_id}"):
                    try:
                        api_delete(f"/reviews/{review_id}")
                        st.success("Deleted.")
                        del st.session_state[detail_key]
                        del st.session_state[f"delete_confirm_{review_id}"]
                        st.rerun()
                    except requests.exceptions.RequestException as e:
                        st.error(f"Delete failed: {e}")
            with dcol2:
                if st.button("Cancel", key=f"cancel_delete_{review_id}"):
                    st.session_state[f"delete_confirm_{review_id}"] = False
                    st.rerun()

        if st.session_state.get(f"reject_open_{review_id}"):
            feedback = st.text_area(
                "What should change?",
                key=f"feedback_{review_id}",
                placeholder="e.g. Shorten the executive summary to 3 bullets.",
            )
            if st.button("Submit rejection", key=f"submit_reject_{review_id}"):
                if not feedback.strip():
                    st.warning("Enter feedback before submitting.")
                else:
                    try:
                        result = api_post(
                            f"/reviews/{review_id}/reject", {"feedback": feedback}
                        )
                        st.session_state[detail_key]["final_report"] = result["final_report"]
                        st.session_state[f"reject_open_{review_id}"] = False
                        st.success("Report regenerated. Review again before approving.")
                        st.rerun()
                    except requests.exceptions.RequestException as e:
                        st.error(f"Reject failed: {e}")