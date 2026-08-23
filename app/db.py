import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workbook_id INTEGER NOT NULL REFERENCES workbooks(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS answer_keys (
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    answer TEXT NOT NULL,
    answer_display TEXT NOT NULL,
    PRIMARY KEY (section_id, number)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    taken_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    score INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    percent REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempt_answers (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    given TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    PRIMARY KEY (attempt_id, number)
);
CREATE INDEX IF NOT EXISTS idx_sections_wb ON sections(workbook_id);
CREATE INDEX IF NOT EXISTS idx_attempts_sec ON attempts(section_id);
"""


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


def init_db() -> None:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(_SCHEMA)


def _q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def list_workbooks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        """
        SELECT w.id, w.title, w.created_at,
               (SELECT COUNT(*) FROM sections s WHERE s.workbook_id = w.id) AS section_count,
               (SELECT COUNT(*) FROM answer_keys k JOIN sections s2 ON k.section_id = s2.id
                 WHERE s2.workbook_id = w.id) AS problem_count,
               (SELECT a.percent FROM attempts a JOIN sections s3 ON a.section_id = s3.id
                 WHERE s3.workbook_id = w.id ORDER BY a.id DESC LIMIT 1) AS latest_percent
        FROM workbooks w ORDER BY w.id DESC
        """,
    )
    return [dict(r) for r in rows]


def create_workbook(conn: sqlite3.Connection, title: str) -> int:
    cur = conn.execute("INSERT INTO workbooks(title) VALUES (?)", (title,))
    return int(cur.lastrowid)


def get_workbook(conn: sqlite3.Connection, wid: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM workbooks WHERE id = ?", (wid,)).fetchone()
    return dict(row) if row else None


def get_workbook_summary(conn: sqlite3.Connection, wid: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT w.id, w.title, w.created_at,
               (SELECT COUNT(*) FROM sections s WHERE s.workbook_id = w.id) AS section_count,
               (SELECT COUNT(*) FROM answer_keys k JOIN sections s2 ON k.section_id = s2.id
                 WHERE s2.workbook_id = w.id) AS problem_count,
               (SELECT a.percent FROM attempts a JOIN sections s3 ON a.section_id = s3.id
                 WHERE s3.workbook_id = w.id ORDER BY a.id DESC LIMIT 1) AS latest_percent
        FROM workbooks w WHERE w.id = ?
        """,
        (wid,),
    ).fetchone()
    return dict(row) if row else None


def delete_workbook(conn: sqlite3.Connection, wid: int) -> bool:
    cur = conn.execute("DELETE FROM workbooks WHERE id = ?", (wid,))
    return cur.rowcount > 0


def list_sections(conn: sqlite3.Connection, wid: int) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        """
        SELECT s.id, s.workbook_id, s.label, s.position,
               (SELECT COUNT(*) FROM answer_keys k WHERE k.section_id = s.id) AS problem_count,
               (SELECT COUNT(*) FROM attempts a WHERE a.section_id = s.id) AS attempt_count,
               (SELECT a.percent FROM attempts a WHERE a.section_id = s.id
                 ORDER BY a.id DESC LIMIT 1) AS latest_percent,
               (SELECT MAX(a.percent) FROM attempts a WHERE a.section_id = s.id) AS best_percent
        FROM sections s WHERE s.workbook_id = ? ORDER BY s.position, s.id
        """,
        (wid,),
    )
    return [dict(r) for r in rows]


def get_section(conn: sqlite3.Connection, sid: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sections WHERE id = ?", (sid,)).fetchone()
    return dict(row) if row else None


def insert_section(
    conn: sqlite3.Connection, wid: int, label: str, position: int
) -> int:
    cur = conn.execute(
        "INSERT INTO sections(workbook_id, label, position) VALUES (?, ?, ?)",
        (wid, label, position),
    )
    return int(cur.lastrowid)


def insert_keys(
    conn: sqlite3.Connection,
    sid: int,
    items: list[tuple[int, str, str]],
) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO answer_keys(section_id, number, answer, answer_display)"
        " VALUES (?, ?, ?, ?)",
        [(sid, n, c, d) for n, c, d in items],
    )


def delete_section(conn: sqlite3.Connection, sid: int) -> bool:
    """Remove one section; cascades its answer keys and attempts only."""
    cur = conn.execute("DELETE FROM sections WHERE id = ?", (sid,))
    return cur.rowcount > 0


def replace_section_keys(
    conn: sqlite3.Connection,
    sid: int,
    label: str,
    items: list[tuple[int, str, str]],
) -> None:
    """Overwrite a section's label and answer key set in place."""
    conn.execute("UPDATE sections SET label = ? WHERE id = ?", (label, sid))
    conn.execute("DELETE FROM answer_keys WHERE section_id = ?", (sid,))
    insert_keys(conn, sid, items)


def update_section_label(conn: sqlite3.Connection, sid: int, label: str) -> None:
    conn.execute("UPDATE sections SET label = ? WHERE id = ?", (label, sid))


def list_section_numbers(conn: sqlite3.Connection, wid: int) -> list[dict[str, Any]]:
    """Existing sections of a workbook with their question-number ranges."""
    rows = _q(
        conn,
        """
        SELECT s.id, s.label, s.position,
               GROUP_CONCAT(k.number) AS numbers_csv
        FROM sections s
        LEFT JOIN answer_keys k ON k.section_id = s.id
        WHERE s.workbook_id = ?
        GROUP BY s.id ORDER BY s.position, s.id
        """,
        (wid,),
    )
    return [
        {
            "id": int(r["id"]),
            "label": str(r["label"]),
            "numbers": sorted(
                int(n) for n in str(r["numbers_csv"] or "").split(",") if n
            ),
        }
        for r in rows
    ]


def label_exists(conn: sqlite3.Connection, wid: int, label: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sections WHERE workbook_id = ? AND label = ? LIMIT 1
        """,
        (wid, label),
    ).fetchone()
    return row is not None


def next_unique_label(conn: sqlite3.Connection, wid: int, label: str) -> str:
    """Find 'label (2)', 'label (3)', ... that does not collide yet."""
    if not label_exists(conn, wid, label):
        return label
    i = 2
    while label_exists(conn, wid, f"{label} ({i})"):
        i += 1
    return f"{label} ({i})"


def get_keys(conn: sqlite3.Connection, sid: int) -> dict[int, tuple[str, str]]:
    rows = _q(
        conn,
        "SELECT number, answer, answer_display FROM answer_keys WHERE section_id = ?"
        " ORDER BY number",
        (sid,),
    )
    return {int(r["number"]): (r["answer"], r["answer_display"]) for r in rows}


def delete_attempt(conn: sqlite3.Connection, aid: int) -> bool:
    cur = conn.execute("DELETE FROM attempts WHERE id = ?", (aid,))
    return cur.rowcount > 0


def create_attempt(
    conn: sqlite3.Connection,
    sid: int,
    score: int,
    total: int,
    percent: float,
    results: list[dict[str, Any]],
) -> int:
    cur = conn.execute(
        "INSERT INTO attempts(section_id, score, total, percent) VALUES (?, ?, ?, ?)",
        (sid, score, total, percent),
    )
    aid = int(cur.lastrowid)
    conn.executemany(
        "INSERT OR REPLACE INTO attempt_answers(attempt_id, number, given, expected, status)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (aid, r["number"], r["given"], r["expected"], r["status"])
            for r in results
        ],
    )
    return aid


def get_attempt(conn: sqlite3.Connection, aid: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM attempts WHERE id = ?", (aid,)).fetchone()
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


def list_attempts(conn: sqlite3.Connection, sid: int) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        "SELECT id, section_id, taken_at, score, total, percent FROM attempts"
        " WHERE section_id = ? ORDER BY id DESC",
        (sid,),
    )
    return [dict(r) for r in rows]


def top_missed(conn: sqlite3.Connection, wid: int, limit: int = 10) -> list[dict[str, Any]]:
    rows = _q(
        conn,
        """
        SELECT aa.number AS number, COUNT(*) AS count, s.label AS section_label
        FROM attempt_answers aa
        JOIN attempts a ON a.id = aa.attempt_id
        JOIN sections s ON s.id = a.section_id
        WHERE s.workbook_id = ? AND aa.status != 'correct'
        GROUP BY s.id, aa.number
        ORDER BY count DESC, aa.number
        LIMIT ?
        """,
        (wid, limit),
    )
    return [dict(r) for r in rows]
