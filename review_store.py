import sqlite3
import json
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = "data/reviews.db"


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_reviews (
                review_id TEXT PRIMARY KEY,
                repo_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                final_report TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_pending_review(repo_name: str, pr_number: int, state: dict, final_report: str) -> str:
    """Creates a new pending review record. Returns the generated review_id."""
    review_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO pending_reviews
               (review_id, repo_name, pr_number, state_json, final_report, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (review_id, repo_name, pr_number, json.dumps(state), final_report, now, now)
        )
    return review_id


def get_review(review_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        return dict(row) if row else None


def list_pending_reviews() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT review_id, repo_name, pr_number, status, created_at, updated_at "
            "FROM pending_reviews WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_review_report(review_id: str, new_report: str, new_state: dict):
    """Used after a rejection + regeneration — updates the report but keeps status='pending'."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE pending_reviews SET final_report = ?, state_json = ?, updated_at = ? WHERE review_id = ?",
            (new_report, json.dumps(new_state), now, review_id)
        )


def mark_review_status(review_id: str, status: str):
    """status: 'approved' or 'rejected_final' — terminal states."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE pending_reviews SET status = ?, updated_at = ? WHERE review_id = ?",
            (status, now, review_id)
        )

def delete_review(review_id: str) -> bool:
    """Permanently deletes a review record. Returns True if a row was deleted."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM pending_reviews WHERE review_id = ?", (review_id,)
        )
        return cursor.rowcount > 0