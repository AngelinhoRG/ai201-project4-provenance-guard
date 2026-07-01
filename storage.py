import json
import sqlite3

DB_PATH = "provenance.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            content_id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL,
            text TEXT NOT NULL,
            attribution TEXT NOT NULL,
            confidence REAL NOT NULL,
            llm_score REAL NOT NULL,
            stylo_score REAL NOT NULL,
            label TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event TEXT NOT NULL,
            content_id TEXT NOT NULL,
            details TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_submission(record):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO submissions
            (content_id, creator_id, text, attribution, confidence,
             llm_score, stylo_score, label, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["content_id"],
            record["creator_id"],
            record["text"],
            record["attribution"],
            record["confidence"],
            record["llm_score"],
            record["stylo_score"],
            record["label"],
            record["status"],
            record["created_at"],
        ),
    )
    conn.commit()
    conn.close()


def get_submission(content_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_submission_status(content_id, status):
    conn = get_conn()
    conn.execute(
        "UPDATE submissions SET status = ? WHERE content_id = ?",
        (status, content_id),
    )
    conn.commit()
    conn.close()


def log_event(event, content_id, details):
    conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log (timestamp, event, content_id, details) VALUES (?, ?, ?, ?)",
        (details.get("timestamp"), event, content_id, json.dumps(details)),
    )
    conn.commit()
    conn.close()


def get_log(limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    entries = []
    for row in rows:
        entry = json.loads(row["details"])
        entry["event"] = row["event"]
        entries.append(entry)
    return entries
