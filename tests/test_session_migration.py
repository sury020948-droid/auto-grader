"""Sessions schema, migration, and core DAL coverage.

This targets ground the implementation's own tests (test_sessions.py,
test_api.py) do not cover, and is scoped to app/db.py only -- schema,
migration, and DAL functions -- with no HTTP layer involved. The API layer
(routers/schemas.py wiring these new DAL functions in, and the request-level
"is there an open session?" / retry-detection logic) is a later chunk's job.

  * Migration safety for a pre-existing on-disk database. Two shapes are
    exercised, since the real data/auto-grader.db (confirmed by direct,
    read-only inspection) is the *newer* of the two:
      - `_LEGACY_SCHEMA`: the actual current on-disk shape (has user_id and
        is_full_attempt, predates sessions/session_id/is_first_submission/
        submission_seq). init_db() must reconstruct a `sessions` row (plus
        linkage) for every pre-existing attempt, self-healing with no data
        loss and no crash, staying idempotent across repeated boots.
      - `_ANCIENT_SCHEMA`: predates is_full_attempt too (the previous
        chunk's own legacy fixture). One boot must chain *both* migrations
        correctly: is_full_attempt backfills to 1 first, then the sessions
        backfill reconstructs one session per attempt off that.
  * DAL-level coverage of the new session functions (app/db.py), exercised
    directly and independent of the HTTP layer: get_open_session,
    create_session (including its IntegrityError race path),
    finish_session (including idempotency), list_finished_sessions,
    get_session, list_session_attempts, and the session_count/latest_percent
    /best_percent/top_missed aggregates now sourced from `sessions` instead
    of attempts.is_full_attempt.
  * A regression test asserting is_full_attempt (SUPERSEDED, but still
    written as an inert mirror -- see schema_design) never again gates a
    query's results.
"""

import re
import sqlite3
import uuid
from pathlib import Path

import pytest

from app import db as dal

DAY_SAMPLE = (
    "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2\n"
    "Day 02\n1. 2 2. 3 3. 4 4. 1 5. 5"
)


def _import_headers(client, wid, sample=DAY_SAMPLE):
    preview = client.post("/api/extract-text", json={"raw_text": sample}).json()
    entries = [
        {"number": e["number"], "answer": e["answer"], "line": e.get("line", 0)}
        for e in preview["entries"]
    ]
    return client.post(
        f"/api/workbooks/{wid}/sections/import",
        json={
            "structure": "headers",
            "header_type": "day",
            "entries": entries,
            "headers": preview["headers"],
        },
    )


@pytest.fixture()
def wb(client):
    r = client.post("/api/workbooks", json={"title": "세션 마이그레이션 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def two_sections(client):
    wid = client.post("/api/workbooks", json={"title": "세션 DAL 테스트"}).json()["id"]
    secs = _import_headers(client, wid).json()["sections"]
    return wid, secs[0]["id"], secs[1]["id"]


# ---------------------------------------------------------------------------
# Migration safety: reconstructing `sessions` for pre-existing attempts rows.
# ---------------------------------------------------------------------------

# The *actual current* on-disk schema (confirmed by direct inspection of the
# real data/auto-grader.db): post user_id/device_id/is_full_attempt
# migrations, but predating the sessions table entirely.
_LEGACY_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT UNIQUE,
    gemini_api_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE workbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    workbook_id INTEGER NOT NULL REFERENCES workbooks(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE answer_keys (
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    answer TEXT NOT NULL,
    answer_display TEXT NOT NULL,
    PRIMARY KEY (section_id, number)
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    taken_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    score INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    percent REAL NOT NULL DEFAULT 0,
    is_full_attempt INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE attempt_answers (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    given TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    PRIMARY KEY (attempt_id, number)
);
"""

# The even-older, pre-is_full_attempt shape -- kept to prove both migrations
# chain correctly in one boot.
_ANCIENT_SCHEMA = _LEGACY_SCHEMA.replace(
    ",\n    is_full_attempt INTEGER NOT NULL DEFAULT 1\n);", "\n);"
)


def _seed_legacy_db(db_file):
    """Build a fresh sqlite file with the current on-disk schema (user_id +
    is_full_attempt present, sessions/session_id absent), populated across
    three sections that exercise the three shapes the sessions-backfill
    heuristic must handle:

      * "Day 01": one full attempt only -> one 1-submission finished session.
      * "Day 02": full attempt, a partial retry merged onto it, then ANOTHER
        full attempt (a later retake) -> two separate finished sessions,
        proving a fresh full row always starts a new session even mid-
        section and that "most recently opened" tracking resets correctly.
      * "Day 03": a partial attempt with NO preceding full row in its
        section (e.g. its base attempt was since deleted) -> the defensive
        path, promoted to its own 1-submission finished session.
    """
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(_LEGACY_SCHEMA)
        device_id = str(uuid.uuid4())
        uid = conn.execute(
            "INSERT INTO users(device_id) VALUES (?)", (device_id,)
        ).lastrowid
        wid = conn.execute(
            "INSERT INTO workbooks(user_id, title) VALUES (?, ?)",
            (uid, "레거시 워크북"),
        ).lastrowid

        def mk_section(label):
            sid = conn.execute(
                "INSERT INTO sections(user_id, workbook_id, label, position)"
                " VALUES (?, ?, ?, 0)",
                (uid, wid, label),
            ).lastrowid
            conn.execute(
                "INSERT INTO answer_keys(section_id, number, user_id, answer,"
                " answer_display) VALUES (?, 1, ?, '3', '3')",
                (sid, uid),
            )
            return sid

        def mk_attempt(sid, score, total, percent, is_full, given, status):
            aid = conn.execute(
                "INSERT INTO attempts(user_id, section_id, score, total,"
                " percent, is_full_attempt) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, sid, score, total, percent, is_full),
            ).lastrowid
            conn.execute(
                "INSERT INTO attempt_answers(attempt_id, number, user_id,"
                " given, expected, status) VALUES (?, 1, ?, ?, '3', ?)",
                (aid, uid, given, status),
            )
            return aid

        s1 = mk_section("Day 01")
        a1 = mk_attempt(s1, 1, 1, 100.0, 1, "3", "correct")

        s2 = mk_section("Day 02")
        a2 = mk_attempt(s2, 0, 1, 0.0, 1, "9", "incorrect")  # base full
        a3 = mk_attempt(s2, 1, 1, 100.0, 0, "3", "correct")  # merged retry
        a4 = mk_attempt(s2, 0, 1, 0.0, 1, "5", "incorrect")  # later retake

        s3 = mk_section("Day 03")
        a5 = mk_attempt(s3, 0, 1, 0.0, 0, "9", "incorrect")  # orphan partial

        conn.commit()
    finally:
        conn.close()
    return {
        "device_id": device_id,
        "uid": uid,
        "wid": wid,
        "s1": s1,
        "s2": s2,
        "s3": s3,
        "a1": a1,
        "a2": a2,
        "a3": a3,
        "a4": a4,
        "a5": a5,
    }


def _seed_ancient_db(db_file):
    """The even-older pre-is_full_attempt shape, with two plain attempt rows
    -- both implicitly "full" under the old model, since the partial concept
    didn't exist yet."""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(_ANCIENT_SCHEMA)
        device_id = str(uuid.uuid4())
        uid = conn.execute(
            "INSERT INTO users(device_id) VALUES (?)", (device_id,)
        ).lastrowid
        wid = conn.execute(
            "INSERT INTO workbooks(user_id, title) VALUES (?, ?)",
            (uid, "고대 워크북"),
        ).lastrowid
        sid = conn.execute(
            "INSERT INTO sections(user_id, workbook_id, label, position)"
            " VALUES (?, ?, 'Day 01', 0)",
            (uid, wid),
        ).lastrowid
        conn.execute(
            "INSERT INTO answer_keys(section_id, number, user_id, answer,"
            " answer_display) VALUES (?, 1, ?, '3', '3')",
            (sid, uid),
        )
        aid1 = conn.execute(
            "INSERT INTO attempts(user_id, section_id, score, total, percent)"
            " VALUES (?, ?, 1, 1, 100.0)",
            (uid, sid),
        ).lastrowid
        aid2 = conn.execute(
            "INSERT INTO attempts(user_id, section_id, score, total, percent)"
            " VALUES (?, ?, 0, 1, 0.0)",
            (uid, sid),
        ).lastrowid
        for aid, given, status in ((aid1, "3", "correct"), (aid2, "9", "incorrect")):
            conn.execute(
                "INSERT INTO attempt_answers(attempt_id, number, user_id,"
                " given, expected, status) VALUES (?, 1, ?, ?, '3', ?)",
                (aid, uid, given, status),
            )
        conn.commit()
    finally:
        conn.close()
    return {"device_id": device_id, "uid": uid, "wid": wid, "sid": sid, "aid1": aid1, "aid2": aid2}


class TestSessionsBackfillMigration:
    def test_reconstructs_sessions_from_current_on_disk_shape(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        seed = _seed_legacy_db(data_dir / "auto-grader.db")
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))

        # Must not raise -- this is what boots against the real, pre-existing
        # data/auto-grader.db the very next time the server starts.
        dal.init_db()

        conn = dal.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
            for col in ("session_id", "is_first_submission", "submission_seq"):
                assert col in cols

            # --- no data loss: every attempts/attempt_answers value from
            # before the migration survives completely untouched ---
            rows = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, section_id, score, total, percent,"
                    " is_full_attempt FROM attempts ORDER BY id"
                )
            }
            assert set(rows) == {seed["a1"], seed["a2"], seed["a3"], seed["a4"], seed["a5"]}
            assert rows[seed["a1"]]["is_full_attempt"] == 1
            assert rows[seed["a3"]]["is_full_attempt"] == 0  # untouched mirror
            assert rows[seed["a3"]]["percent"] == 100.0

            ans = conn.execute(
                "SELECT attempt_id, given, status FROM attempt_answers ORDER BY attempt_id"
            ).fetchall()
            assert [(r["attempt_id"], r["given"], r["status"]) for r in ans] == [
                (seed["a1"], "3", "correct"),
                (seed["a2"], "9", "incorrect"),
                (seed["a3"], "3", "correct"),
                (seed["a4"], "5", "incorrect"),
                (seed["a5"], "9", "incorrect"),
            ]

            # --- Day 01: one full attempt -> one 1-submission session ---
            link = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, session_id, is_first_submission, submission_seq"
                    " FROM attempts"
                )
            }
            s1_sess_id = link[seed["a1"]]["session_id"]
            assert link[seed["a1"]] == {
                "id": seed["a1"],
                "session_id": s1_sess_id,
                "is_first_submission": 1,
                "submission_seq": 1,
            }
            s1_sess = dal.get_session(conn, s1_sess_id, seed["uid"])
            assert s1_sess["status"] == "finished"
            assert (s1_sess["first_score"], s1_sess["first_total"], s1_sess["first_percent"]) == (
                1,
                1,
                100.0,
            )
            assert s1_sess["started_at"] == s1_sess["finished_at"]

            # --- Day 02: full + merged retry share one session; the LATER
            # full retake starts a brand-new, second session ---
            assert link[seed["a2"]]["session_id"] == link[seed["a3"]]["session_id"]
            day2_sess1 = link[seed["a2"]]["session_id"]
            day2_sess2 = link[seed["a4"]]["session_id"]
            assert day2_sess2 != day2_sess1
            assert link[seed["a2"]]["is_first_submission"] == 1
            assert link[seed["a2"]]["submission_seq"] == 1
            assert link[seed["a3"]]["is_first_submission"] == 0  # retry, not first
            assert link[seed["a3"]]["submission_seq"] == 2
            assert link[seed["a4"]]["is_first_submission"] == 1
            assert link[seed["a4"]]["submission_seq"] == 1

            sess1 = dal.get_session(conn, day2_sess1, seed["uid"])
            assert (sess1["first_score"], sess1["first_total"], sess1["first_percent"]) == (
                0,
                1,
                0.0,
            )  # frozen at the BASE (first) attempt's score, not the retry's
            sess2 = dal.get_session(conn, day2_sess2, seed["uid"])
            assert (sess2["first_score"], sess2["first_total"], sess2["first_percent"]) == (
                0,
                1,
                0.0,
            )

            # --- Day 03: orphan partial (no preceding full row) promotes to
            # its own one-submission finished session, nothing dropped ---
            assert link[seed["a5"]]["is_first_submission"] == 1
            assert link[seed["a5"]]["submission_seq"] == 1
            d3 = dal.get_session(conn, link[seed["a5"]]["session_id"], seed["uid"])
            assert d3["status"] == "finished"
            assert (d3["first_score"], d3["first_total"], d3["first_percent"]) == (0, 1, 0.0)

            # exactly 4 sessions total: 1 (Day01) + 2 (Day02) + 1 (Day03)
            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            assert total == 4

            # --- migrated DAL read paths run cleanly end-to-end ---
            secs = {s["id"]: s for s in dal.list_sections(conn, seed["wid"], seed["uid"])}
            assert secs[seed["s1"]]["session_count"] == 1
            assert secs[seed["s1"]]["latest_percent"] == 100.0
            assert secs[seed["s1"]]["best_percent"] == 100.0
            assert secs[seed["s2"]]["session_count"] == 2
            assert secs[seed["s2"]]["latest_percent"] == 0.0  # day2_sess2, the later retake
            assert secs[seed["s2"]]["best_percent"] == 0.0
            assert secs[seed["s3"]]["session_count"] == 1
            assert secs[seed["s3"]]["latest_percent"] == 0.0

            books = dal.list_workbooks(conn, seed["uid"])
            assert books[0]["latest_percent"] == 0.0  # the globally-latest finished session

            summary = dal.get_workbook_summary(conn, seed["wid"], seed["uid"])
            assert summary["latest_percent"] == 0.0
        finally:
            conn.close()

        # --- idempotency: a second boot must not touch already-migrated
        # rows (no duplicate sessions, no "duplicate column name" crash) ---
        dal.init_db()
        conn = dal.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
            assert cols.count("session_id") == 1
            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            assert total == 4
            still = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, session_id, submission_seq FROM attempts"
                )
            }
            assert still[seed["a3"]]["submission_seq"] == 2  # unchanged, not re-derived
        finally:
            conn.close()

    def test_both_migrations_chain_from_pre_is_full_attempt_shape_in_one_boot(
        self, tmp_path, monkeypatch
    ):
        """A database even older than `_LEGACY_SCHEMA` (predating
        is_full_attempt entirely) must self-heal both migrations -- the
        is_full_attempt backfill, then the sessions backfill that depends on
        it -- correctly in a single boot."""
        data_dir = tmp_path / "data"
        seed = _seed_ancient_db(data_dir / "auto-grader.db")
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))

        dal.init_db()

        conn = dal.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
            for col in ("is_full_attempt", "session_id", "is_first_submission", "submission_seq"):
                assert col in cols

            rows = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, is_full_attempt, is_first_submission,"
                    " submission_seq, session_id FROM attempts"
                )
            }
            # both pre-existing rows backfilled as full (that's all that
            # existed under the old model) -> each starts its own session.
            for aid in (seed["aid1"], seed["aid2"]):
                assert rows[aid]["is_full_attempt"] == 1
                assert rows[aid]["is_first_submission"] == 1
                assert rows[aid]["submission_seq"] == 1
            assert rows[seed["aid1"]]["session_id"] != rows[seed["aid2"]]["session_id"]

            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            assert total == 2

            secs = dal.list_sections(conn, seed["wid"], seed["uid"])
            assert secs[0]["session_count"] == 2
            assert secs[0]["best_percent"] == 100.0
            assert secs[0]["latest_percent"] == 0.0  # aid2 has the higher id
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# DAL-level coverage of the new session functions and rewired aggregates.
# ---------------------------------------------------------------------------


class TestSessionsDal:
    def test_open_create_and_finish_lifecycle(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            assert dal.get_open_session(conn, sid, uid) is None

            session_id = dal.create_session(conn, uid, sid, 3, 5, 60.0)
            conn.commit()

            op = dal.get_open_session(conn, sid, uid)
            assert op["id"] == session_id
            assert op["status"] == "in_progress"
            assert (op["first_score"], op["first_total"], op["first_percent"]) == (3, 5, 60.0)
            assert op["finished_at"] is None

            finished = dal.finish_session(conn, session_id, uid)
            conn.commit()
            assert finished["status"] == "finished"
            assert finished["finished_at"] is not None
            assert finished["first_score"] == 3  # frozen score untouched by finishing
            assert dal.get_open_session(conn, sid, uid) is None

            # idempotent: finishing an already-finished session is a no-op
            # that returns the row unchanged, not an error.
            again = dal.finish_session(conn, session_id, uid)
            assert again == finished
        finally:
            conn.close()

    def test_finish_and_get_session_unknown_or_foreign_id_returns_none(
        self, client, wb, device_id
    ):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            assert dal.finish_session(conn, 999999, uid) is None
            assert dal.get_session(conn, 999999, uid) is None

            session_id = dal.create_session(conn, uid, sid, 1, 1, 100.0)
            conn.commit()

            foreign_uid = dal.get_or_create_device_user(conn, str(uuid.uuid4()))["id"]
            conn.commit()
            # a foreign user must never see or finish someone else's session
            assert dal.get_session(conn, session_id, foreign_uid) is None
            assert dal.finish_session(conn, session_id, foreign_uid) is None
            # ...and it must be completely unaffected for its real owner
            assert dal.get_session(conn, session_id, uid)["status"] == "in_progress"
        finally:
            conn.close()

    def test_create_session_race_absorbs_integrity_error(self, client, wb, device_id):
        """A concurrent request racing idx_sessions_one_open must not crash
        -- the loser's IntegrityError is caught and the winner's already-open
        session is re-read and returned, mirroring
        get_or_create_device_user's own device_id race handling."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            first_id = dal.create_session(conn, uid, sid, 1, 2, 50.0)
            conn.commit()

            second_id = dal.create_session(conn, uid, sid, 9, 9, 9.0)
            conn.commit()
            assert second_id == first_id

            n = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE section_id = ?"
                " AND status = 'in_progress'",
                (sid,),
            ).fetchone()["n"]
            assert n == 1
        finally:
            conn.close()

    def test_list_finished_sessions_excludes_open_and_scopes_to_section(
        self, client, two_sections, device_id
    ):
        _, s1, s2 = two_sections
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            f1 = dal.create_session(conn, uid, s1, 5, 5, 100.0)
            dal.finish_session(conn, f1, uid)
            dal.create_session(conn, uid, s1, 1, 5, 20.0)  # left open
            other = dal.create_session(conn, uid, s2, 2, 5, 40.0)
            dal.finish_session(conn, other, uid)
            conn.commit()

            finished_s1 = dal.list_finished_sessions(conn, s1, uid)
            assert [s["id"] for s in finished_s1] == [f1]

            finished_s2 = dal.list_finished_sessions(conn, s2, uid)
            assert [s["id"] for s in finished_s2] == [other]
        finally:
            conn.close()

    def test_list_session_attempts_orders_by_submission_seq(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 0, 1, 0.0)

            aid1 = dal.create_attempt(
                conn,
                uid,
                sid,
                0,
                1,
                0.0,
                [{"number": 1, "given": "9", "expected": "3", "status": "incorrect"}],
            )
            aid2 = dal.create_attempt(
                conn,
                uid,
                sid,
                1,
                1,
                100.0,
                [{"number": 1, "given": "3", "expected": "3", "status": "correct"}],
            )
            # stand in for what the (later) API-layer chunk does when it
            # links a graded submission to its session.
            conn.execute(
                "UPDATE attempts SET session_id=?, is_first_submission=1,"
                " submission_seq=1 WHERE id=?",
                (session_id, aid1),
            )
            conn.execute(
                "UPDATE attempts SET session_id=?, is_first_submission=0,"
                " submission_seq=2 WHERE id=?",
                (session_id, aid2),
            )
            conn.commit()

            atts = dal.list_session_attempts(conn, session_id)
            assert [a["id"] for a in atts] == [aid1, aid2]
            assert [a["submission_seq"] for a in atts] == [1, 2]
            assert atts[0]["results"][0]["status"] == "incorrect"
            assert atts[1]["results"][0]["status"] == "correct"
        finally:
            conn.close()

    def test_aggregates_source_only_finished_sessions(self, client, wb, device_id):
        """list_sections/list_workbooks/get_workbook_summary must reflect a
        finished session's frozen first_percent, and must NOT be swayed by a
        still-open session even if its (eventual) score would beat it."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            finished_id = dal.create_session(conn, uid, sid, 4, 5, 80.0)
            dal.finish_session(conn, finished_id, uid)
            dal.create_session(conn, uid, sid, 5, 5, 100.0)  # left open
            conn.commit()

            secs = dal.list_sections(conn, wb, uid)
            sec = next(s for s in secs if s["id"] == sid)
            assert sec["session_count"] == 1
            assert sec["latest_percent"] == 80.0
            assert sec["best_percent"] == 80.0  # NOT 100.0 from the open one

            books = dal.list_workbooks(conn, uid)
            assert books[0]["latest_percent"] == 80.0

            summary = dal.get_workbook_summary(conn, wb, uid)
            assert summary["latest_percent"] == 80.0
        finally:
            conn.close()

    def test_section_with_no_finished_sessions_reports_null_stats(
        self, client, wb, device_id
    ):
        """Edge case: a section that only has an open (never-finished)
        session must report zero sessions and null best/latest percent, not
        crash or leak the open session's numbers."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            dal.create_session(conn, uid, sid, 5, 5, 100.0)  # never finished
            conn.commit()

            secs = dal.list_sections(conn, wb, uid)
            sec = next(s for s in secs if s["id"] == sid)
            assert sec["session_count"] == 0
            assert sec["latest_percent"] is None
            assert sec["best_percent"] is None

            books = dal.list_workbooks(conn, uid)
            assert books[0]["latest_percent"] is None

            summary = dal.get_workbook_summary(conn, wb, uid)
            assert summary["latest_percent"] is None
        finally:
            conn.close()

    def test_top_missed_requires_first_submission_of_a_finished_session(
        self, client, wb, device_id
    ):
        """Mirrors the old is_full_attempt-based double-count regression,
        now against the sessions-based gate: a retry (is_first_submission=0)
        that's still wrong must not double-count, and even a *first*
        submission must not count at all while its session stays open."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]

            # finished session: first submission wrong on Q1, retry still
            # wrong on Q1 -- must count once, not twice.
            finished_id = dal.create_session(conn, uid, sid, 0, 1, 0.0)
            aid1 = dal.create_attempt(
                conn,
                uid,
                sid,
                0,
                1,
                0.0,
                [{"number": 1, "given": "9", "expected": "3", "status": "incorrect"}],
            )
            aid2 = dal.create_attempt(
                conn,
                uid,
                sid,
                0,
                1,
                0.0,
                [{"number": 1, "given": "1", "expected": "3", "status": "incorrect"}],
            )
            conn.execute(
                "UPDATE attempts SET session_id=?, is_first_submission=1,"
                " submission_seq=1 WHERE id=?",
                (finished_id, aid1),
            )
            conn.execute(
                "UPDATE attempts SET session_id=?, is_first_submission=0,"
                " submission_seq=2 WHERE id=?",
                (finished_id, aid2),
            )
            dal.finish_session(conn, finished_id, uid)

            # open (never finished) session: its first submission is ALSO
            # wrong on Q1 -- must not count at all while the session is open.
            open_id = dal.create_session(conn, uid, sid, 0, 1, 0.0)
            aid3 = dal.create_attempt(
                conn,
                uid,
                sid,
                0,
                1,
                0.0,
                [{"number": 1, "given": "5", "expected": "3", "status": "incorrect"}],
            )
            conn.execute(
                "UPDATE attempts SET session_id=?, is_first_submission=1,"
                " submission_seq=1 WHERE id=?",
                (open_id, aid3),
            )
            conn.commit()

            missed = dal.top_missed(conn, wb, uid)
            by_num = {m["number"]: m["count"] for m in missed}
            assert by_num == {1: 1}  # only the finished session's first try
        finally:
            conn.close()


class TestIsFullAttemptSuperseded:
    def test_is_full_attempt_never_gates_a_query(self):
        """is_full_attempt is SUPERSEDED by sessions/is_first_submission
        (see schema_design): every query this chunk touches or adds must
        never branch on it again -- it may still be written on insert (an
        inert mirror so a stray direct-SQL reader never sees a misleadingly
        -stale value), but never read to decide what a query returns.

        The one knowing exception left standing is list_attempts's existing
        filter: it backs `GET /sections/{sid}/attempts`, which the session-
        model plan removes outright (not rewires) once the API layer lands,
        so rewiring its filter here would be transitional work on a query
        about to be deleted wholesale, not this chunk's job. If this
        assertion ever needs to grow past that one line, that's a sign a
        *new* is_full_attempt gate crept in somewhere it shouldn't have.
        """
        src = Path(dal.__file__).read_text(encoding="utf-8")
        gating = re.findall(r"(?i)(?:WHERE|AND)\s+[\w.]*\bis_full_attempt\b", src)
        assert gating == ["AND a.is_full_attempt"], (
            "is_full_attempt gates a query beyond the known,"
            f" soon-to-be-deleted list_attempts holdout: {gating}"
        )

    def test_rewired_and_new_session_functions_never_reference_is_full_attempt(self):
        """Precise, per-function check on exactly what this chunk touched or
        added: none of them may read is_full_attempt at all, gating or not."""
        import inspect

        touched = [
            dal.list_workbooks,
            dal.get_workbook_summary,
            dal.list_sections,
            dal.top_missed,
            dal.get_open_session,
            dal.create_session,
            dal.finish_session,
            dal.list_finished_sessions,
            dal.get_session,
            dal.list_session_attempts,
        ]
        for fn in touched:
            body = inspect.getsource(fn)
            assert "is_full_attempt" not in body, (
                f"{fn.__name__} references is_full_attempt: {body}"
            )
