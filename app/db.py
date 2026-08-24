import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub TEXT UNIQUE,
    email TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    picture TEXT NOT NULL DEFAULT '',
    gemini_api_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS workbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    workbook_id INTEGER NOT NULL REFERENCES workbooks(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS answer_keys (
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    answer TEXT NOT NULL,
    answer_display TEXT NOT NULL,
    PRIMARY KEY (section_id, number)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    taken_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    score INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    percent REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempt_answers (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    given TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    PRIMARY KEY (attempt_id, number)
);
CREATE INDEX IF NOT EXISTS idx_sections_wb ON sections(workbook_id);
CREATE INDEX IF NOT EXISTS idx_attempts_sec ON attempts(section_id);
"""

# Columns added by the multi-tenant migration on pre-existing databases.
_MIGRATE_COLUMNS = {
    "workbooks": "user_id",
    "sections": "user_id",
    "answer_keys": "user_id",
    "attempts": "user_id",
    "attempt_answers": "user_id",
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(local_user: dict[str, str] | None = None) -> None:
    """Create schema and migrate legacy single-tenant tables securely."""
    import json
    from pathlib import Path

    db_path().parent.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(_SCHEMA)

        # --- migration: add user_id columns to pre-existing databases ---
        for table, col in _MIGRATE_COLUMNS.items():
            cols = [
                r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
            ]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER")

        # --- ensure at least one user exists; backfill orphan rows ---
        local = local_user or {}
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            cur = conn.execute(
                "INSERT INTO users(google_sub, email, name)"
                " VALUES (?, ?, ?)",
                (
                    "local",
                    local.get("email", "local@auto-grader"),
                    local.get("name", "로컬 사용자"),
                ),
            )
            uid = int(cur.lastrowid)
        else:
            uid = int(row["id"])

        for table in _MIGRATE_COLUMNS:
            conn.execute(
                f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (uid,)
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workbooks_user"
            " ON workbooks(user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id)"
        )
        # keep legacy settings.json key usable as the local user's key
        legacy_file = Path(db_path()).parent / "settings.json"
        if legacy_file.exists():
            try:
                data = json.loads(legacy_file.read_text(encoding="utf-8"))
                key = data.get("gemini_api_key", "")
                if key:
                    conn.execute(
                        "UPDATE users SET gemini_api_key = ?"
                        " WHERE id = ? AND gemini_api_key = ''",
                        (key, uid),
                    )
                    data.pop("gemini_api_key", None)
                    legacy_file.write_text(
                        json.dumps(data, ensure_ascii=False), encoding="utf-8"
                    )
            except (OSError, ValueError):
                pass


# ---------------------------------------------------------------- users ----

def get_user(conn: sqlite3.Connection, uid: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def get_user_by_sub(conn: sqlite3.Connection, sub: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM users WHERE google_sub = ?", (sub,)
    ).fetchone()
    return dict(row) if row else None


def upsert_google_user(
    conn: sqlite3.Connection,
    sub: str,
    email: str,
    name: str,
    picture: str,
) -> dict[str, Any]:
    existing = get_user_by_sub(conn, sub)
    if existing:
        conn.execute(
            "UPDATE users SET email = ?, name = ?, picture = ? WHERE id = ?",
            (
                email or existing["email"],
                name or existing["name"],
                picture,
                existing["id"],
            ),
        )
        existing.update(
            email=email or existing["email"],
            name=name or existing["name"],
            picture=picture,
        )
        return existing
    cur = conn.execute(
        "INSERT INTO users(google_sub, email, name, picture) VALUES (?, ?, ?, ?)",
        (sub, email, name, picture),
    )
    return get_user(conn, int(cur.lastrowid))


def set_user_api_key(conn: sqlite3.Connection, uid: int, api_key: str) -> None:
    conn.execute(
        "UPDATE users SET gemini_api_key = ? WHERE id = ?", (api_key, uid)
    )


def _q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


# ------------------------------------------------------------ workbooks ----

def list_workbooks(conn: sqlite3.Connection, uid: int) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        """
        SELECT w.id, w.title, w.created_at,
               (SELECT COUNT(*) FROM sections s WHERE s.workbook_id = w.id) AS section_count,
               (SELECT COUNT(*) FROM answer_keys k JOIN sections s2 ON k.section_id = s2.id
                 WHERE s2.workbook_id = w.id) AS problem_count,
               (SELECT a.percent FROM attempts a JOIN sections s3 ON a.section_id = s3.id
                 WHERE s3.workbook_id = w.id ORDER BY a.id DESC LIMIT 1) AS latest_percent
        FROM workbooks w WHERE w.user_id = ? ORDER BY w.id DESC
        """,
        (uid,),
    )
    return [dict(r) for r in rows]


def create_workbook(conn: sqlite3.Connection, uid: int, title: str) -> int:
    cur = conn.execute(
        "INSERT INTO workbooks(user_id, title) VALUES (?, ?)", (uid, title)
    )
    return int(cur.lastrowid)


def get_workbook(
    conn: sqlite3.Connection, wid: int, uid: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM workbooks WHERE id = ? AND user_id = ?", (wid, uid)
    ).fetchone()
    return dict(row) if row else None


def get_workbook_summary(
    conn: sqlite3.Connection, wid: int, uid: int
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT w.id, w.title, w.created_at,
               (SELECT COUNT(*) FROM sections s WHERE s.workbook_id = w.id) AS section_count,
               (SELECT COUNT(*) FROM answer_keys k JOIN sections s2 ON k.section_id = s2.id
                 WHERE s2.workbook_id = w.id) AS problem_count,
               (SELECT a.percent FROM attempts a JOIN sections s3 ON a.section_id = s3.id
                 WHERE s3.workbook_id = w.id ORDER BY a.id DESC LIMIT 1) AS latest_percent
        FROM workbooks w WHERE w.id = ? AND w.user_id = ?
        """,
        (wid, uid),
    ).fetchone()
    return dict(row) if row else None


def delete_workbook(conn: sqlite3.Connection, wid: int, uid: int) -> bool:
    cur = conn.execute(
        "DELETE FROM workbooks WHERE id = ? AND user_id = ?", (wid, uid)
    )
    return cur.rowcount > 0


# ------------------------------------------------------------- sections ----

def list_sections(
    conn: sqlite3.Connection, wid: int, uid: int
) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        """
        SELECT s.id, s.workbook_id, s.label, s.position,
               (SELECT COUNT(*) FROM answer_keys k WHERE k.section_id = s.id) AS problem_count,
               (SELECT COUNT(*) FROM attempts a WHERE a.section_id = s.id) AS attempt_count,
               (SELECT a.percent FROM attempts a WHERE a.section_id = s.id
                 ORDER BY a.id DESC LIMIT 1) AS latest_percent,
               (SELECT MAX(a.percent) FROM attempts a WHERE a.section_id = s.id) AS best_percent
        FROM sections s
        JOIN workbooks w ON w.id = s.workbook_id
        WHERE s.workbook_id = ? AND w.user_id = ?
        ORDER BY s.position, s.id
        """,
        (wid, uid),
    )
    return [dict(r) for r in rows]


def get_section(
    conn: sqlite3.Connection, sid: int, uid: int
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT s.* FROM sections s
        JOIN workbooks w ON w.id = s.workbook_id
        WHERE s.id = ? AND w.user_id = ?
        """,
        (sid, uid),
    ).fetchone()
    return dict(row) if row else None


def insert_section(
    conn: sqlite3.Connection, uid: int, wid: int, label: str, position: int
) -> int:
    cur = conn.execute(
        "INSERT INTO sections(user_id, workbook_id, label, position) VALUES (?, ?, ?, ?)",
        (uid, wid, label, position),
    )
    return int(cur.lastrowid)


def insert_keys(
    conn: sqlite3.Connection,
    uid: int,
    sid: int,
    items: list[tuple[int, str, str]],
) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO answer_keys(section_id, number, user_id,"
        " answer, answer_display) VALUES (?, ?, ?, ?, ?)",
        [(sid, n, uid, c, d) for n, c, d in items],
    )


def get_keys(
    conn: sqlite3.Connection, sid: int, uid: int
) -> dict[int, tuple[str, str]]:
    rows = _q(
        conn,
        """
        SELECT k.number, k.answer, k.answer_display
        FROM answer_keys k JOIN sections s ON s.id = k.section_id
        JOIN workbooks w ON w.id = s.workbook_id
        WHERE k.section_id = ? AND w.user_id = ?
        ORDER BY k.number
        """,
        (sid, uid),
    )
    return {int(r["number"]): (r["answer"], r["answer_display"]) for r in rows}


def delete_section(conn: sqlite3.Connection, sid: int, uid: int) -> bool:
    """Remove one owned session; cascades its keys and attempts only."""
    sec = get_section(conn, sid, uid)
    if not sec:
        return False
    cur = conn.execute("DELETE FROM sections WHERE id = ?", (sid,))
    return cur.rowcount > 0


def replace_section_keys(
    conn: sqlite3.Connection,
    uid: int,
    sid: int,
    label: str,
    items: list[tuple[int, str, str]],
) -> None:
    """Overwrite an owned section's label and answer key set in place."""
    conn.execute("UPDATE sections SET label = ? WHERE id = ?", (label, sid))
    conn.execute("DELETE FROM answer_keys WHERE section_id = ?", (sid,))
    insert_keys(conn, uid, sid, items)


def update_section_label(conn: sqlite3.Connection, sid: int, label: str) -> None:
    conn.execute("UPDATE sections SET label = ? WHERE id = ?", (label, sid))


def list_section_numbers(
    conn: sqlite3.Connection, wid: int, uid: int
) -> list[dict[str, Any]]:
    """Owned sections of a workbook with their question-number ranges."""
    rows = _q(
        conn,
        """
        SELECT s.id, s.label, s.position,
               GROUP_CONCAT(k.number) AS numbers_csv
        FROM sections s
        LEFT JOIN answer_keys k ON k.section_id = s.id
        JOIN workbooks w ON w.id = s.workbook_id
        WHERE s.workbook_id = ? AND w.user_id = ?
        GROUP BY s.id ORDER BY s.position, s.id
        """,
        (wid, uid),
    )
    return [
        {
            "id": int(r["id"]),
            "label": str(r["label"]),
            "numbers": sorted(int(n) for n in str(r["numbers_csv"] or "").split(",") if n),
        }
        for r in rows
    ]


def label_exists(conn: sqlite3.Connection, wid: int, uid: int, label: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sections s JOIN workbooks w ON w.id = s.workbook_id
        WHERE s.workbook_id = ? AND w.user_id = ? AND s.label = ? LIMIT 1
        """,
        (wid, uid, label),
    ).fetchone()
    return row is not None


def next_unique_label(
    conn: sqlite3.Connection, wid: int, uid: int, label: str
) -> str:
    """Find 'label (2)', 'label (3)', ... that does not collide yet."""
    if not label_exists(conn, wid, uid, label):
        return label
    i = 2
    while label_exists(conn, wid, uid, f"{label} ({i})"):
        i += 1
    return f"{label} ({i})"


# ------------------------------------------------------------- attempts ----

def delete_attempt(conn: sqlite3.Connection, aid: int, uid: int) -> bool:
    att = get_attempt(conn, aid, uid)
    if not att:
        return False
    cur = conn.execute("DELETE FROM attempts WHERE id = ?", (aid,))
    return cur.rowcount > 0


def create_attempt(
    conn: sqlite3.Connection,
    uid: int,
    sid: int,
    score: int,
    total: int,
    percent: float,
    results: list[dict[str, Any]],
) -> int:
    cur = conn.execute(
        "INSERT INTO attempts(user_id, section_id, score, total, percent)"
        " VALUES (?, ?, ?, ?, ?)",
        (uid, sid, score, total, percent),
    )
    aid = int(cur.lastrowid)
    conn.executemany(
        "INSERT OR REPLACE INTO attempt_answers(attempt_id, number, user_id,"
        " given, expected, status) VALUES (?, ?, ?, ?, ?, ?)",
        [(aid, r["number"], uid, r["given"], r["expected"], r["status"]) for r in results],
    )
    return aid


def get_attempt(
    conn: sqlite3.Connection, aid: int, uid: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM attempts WHERE id = ? AND user_id = ?", (aid, uid)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["results"] = [
        dict(r)
        for r in _q(
            conn,
            "SELECT number, expected, given, status FROM attempt_answers"
            " WHERE attempt_id = ? ORDER BY number",
            (aid,),
        )
    ]
    return d


def list_attempts(conn: sqlite3.Connection, sid: int, uid: int) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        """
        SELECT a.id, a.section_id, a.taken_at, a.score, a.total, a.percent
        FROM attempts a JOIN sections s ON s.id = a.section_id
        JOIN workbooks w ON w.id = s.workbook_id
        WHERE a.section_id = ? AND w.user_id = ?
        ORDER BY a.id DESC
        """,
        (sid, uid),
    )
    return [dict(r) for r in rows]


def top_missed(
    conn: sqlite3.Connection, wid: int, uid: int, limit: int = 10
) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        """
        SELECT aa.number AS number, COUNT(*) AS count, s.label AS section_label
        FROM attempt_answers aa
        JOIN attempts a ON a.id = aa.attempt_id
        JOIN sections s ON s.id = a.section_id
        JOIN workbooks w ON w.id = s.workbook_id
        WHERE w.user_id = ? AND aa.status != 'correct'
        GROUP BY s.id, aa.number
        ORDER BY count DESC, aa.number
        LIMIT ?
        """,
        (uid, limit),
    )
    return [dict(r) for r in rows]
