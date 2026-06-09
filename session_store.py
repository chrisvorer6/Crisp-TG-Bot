import sqlite3
from contextlib import contextmanager
from pathlib import Path

db_path = Path("data/sessions.sqlite3")

def init(path=None):
    global db_path
    if path:
        db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                topic_id INTEGER UNIQUE NOT NULL,
                message_id INTEGER,
                enable_ai INTEGER NOT NULL DEFAULT 0,
                nickname TEXT,
                last_activity REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_topic_id ON sessions(topic_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_activity ON sessions(last_activity)")

@contextmanager
def connection():
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def row_to_session(row):
    return {
        "topicId": row[1],
        "messageId": row[2],
        "enableAI": bool(row[3]),
        "nickname": row[4],
        "last_activity": row[5],
    }

def load_all_sessions():
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT session_id, topic_id, message_id, enable_ai, nickname, last_activity
            FROM sessions
            """
        ).fetchall()
    return {row[0]: row_to_session(row) for row in rows}

def get_session_by_topic(topic_id):
    with connection() as conn:
        row = conn.execute(
            """
            SELECT session_id, topic_id, message_id, enable_ai, nickname, last_activity
            FROM sessions
            WHERE topic_id = ?
            """,
            (topic_id,)
        ).fetchone()
    if not row:
        return None
    session = row_to_session(row)
    session["sessionId"] = row[0]
    return session

def upsert_session(session_id, topic_id, message_id, enable_ai, nickname, last_activity):
    with connection() as conn:
        # Keep topic_id unique even when a stale row still points at a rebuilt topic.
        conn.execute(
            "DELETE FROM sessions WHERE topic_id = ? AND session_id != ?",
            (topic_id, session_id)
        )
        conn.execute(
            """
            INSERT INTO sessions (session_id, topic_id, message_id, enable_ai, nickname, last_activity)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                topic_id = excluded.topic_id,
                message_id = excluded.message_id,
                enable_ai = excluded.enable_ai,
                nickname = excluded.nickname,
                last_activity = excluded.last_activity
            """,
            (session_id, topic_id, message_id, int(bool(enable_ai)), nickname, last_activity)
        )

def touch_session(session_id, last_activity):
    with connection() as conn:
        conn.execute(
            "UPDATE sessions SET last_activity = ? WHERE session_id = ?",
            (last_activity, session_id)
        )

def set_enable_ai(session_id, enable_ai, last_activity):
    with connection() as conn:
        conn.execute(
            "UPDATE sessions SET enable_ai = ?, last_activity = ? WHERE session_id = ?",
            (int(bool(enable_ai)), last_activity, session_id)
        )

def delete_sessions(session_ids):
    if not session_ids:
        return
    with connection() as conn:
        placeholders = ",".join("?" for _ in session_ids)
        conn.execute(
            f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
            list(session_ids)
        )
