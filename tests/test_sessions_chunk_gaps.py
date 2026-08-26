"""Additional targeted coverage for the "Sessions schema, migration, and core
DAL" chunk, written on top of the already-substantial existing coverage
(tests/test_session_migration.py, tests/test_sessions_dal_more.py,
tests/test_sessions_backfill_edgecases.py) to close specific gaps that
review found in that coverage rather than duplicate it:

  * None of the existing migration fixtures exercise the *truly* original
    (pre-multi-tenant) on-disk shape -- the one that predates the `users`
    table and every `user_id` column entirely (the actual schema this
    project shipped with, before the OAuth/device-auth chunk). All existing
    `_LEGACY_SCHEMA`/`_ANCIENT_SCHEMA` fixtures already include `user_id`
    columns. This matters because the sessions-backfill migration reads
    `attempts.user_id` directly onto the new `sessions.user_id` column, and
    the ordering guarantee that user_id is *already resolved* by the time
    that runs is exactly the kind of thing a shorter migration chain can't
    prove -- it needs the full, three-migration boot to actually exercise
    the dependency.
  * `get_open_session`'s own `AND user_id = ?` scoping was never directly
    exercised with a foreign uid (only `get_session`/`finish_session` were).
  * `create_session`'s INSERT never lists `started_at`, relying on the
    column's `DEFAULT (datetime('now', 'localtime'))` -- untested.
  * `list_finished_sessions`'s `ORDER BY id DESC` was only ever observed
    with a single row per section; nothing pins down actual ordering
    across 3+ finished sessions.
  * `best_percent` (`MAX(first_percent)`) and `latest_percent` (highest
    `sess.id`) were never exercised with values chosen so the two visibly
    diverge (existing fixtures always have the best also be the latest, or
    degenerate equal values) -- so a regression that silently made
    best_percent just mirror latest_percent would slip through.
  * `top_missed`'s cross-workbook scoping is only covered at the HTTP layer
    in tests/test_sessions.py, and those tests are currently xfail (POST
    /api/attempts doesn't wire session creation yet -- that's the
    api_layer chunk's job). This file adds a DAL-level equivalent, built
    directly against dal.create_session/create_attempt/finish_session, so
    the rewritten JOIN's workbook-scoping is actually verified to pass
    *now*, independent of the pending HTTP wiring.
"""

import sqlite3
import uuid

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
    r = client.post("/api/workbooks", json={"title": "세션 갭 커버리지 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


def _link(conn, aid, session_id, is_first, seq):
    """Stand-in for what the (later) API-layer chunk does when it links a
    graded submission to its session -- this chunk's create_attempt takes no
    session_id parameter at all."""
    conn.execute(
        "UPDATE attempts SET session_id=?, is_first_submission=?,"
        " submission_seq=? WHERE id=?",
        (session_id, int(is_first), seq, aid),
    )


def _finished_attempt(conn, uid, sid, score, total, percent, given, status):
    """Create a fully-linked one-submission finished session, the shape
    the (future) api_layer chunk produces for an ordinary graded attempt."""
    session_id = dal.create_session(conn, uid, sid, score, total, percent)
    aid = dal.create_attempt(
        conn, uid, sid, score, total, percent,
        [{"number": 1, "given": given, "expected": "3", "status": status}],
    )
    _link(conn, aid, session_id, True, 1)
    dal.finish_session(conn, session_id, uid)
    return session_id, aid


# ---------------------------------------------------------------------------
# The truly original (pre-multi-tenant) on-disk shape: no `users` table, no
# `user_id` column anywhere, no `is_full_attempt`, no sessions. This is the
# actual schema this project shipped with at commit fa9779d, before the
# device-auth and partial-attempt chunks landed -- older than every fixture
# already covering this migration.
# ---------------------------------------------------------------------------

_GENESIS_SCHEMA = """
CREATE TABLE workbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workbook_id INTEGER NOT NULL REFERENCES workbooks(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE answer_keys (
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    answer TEXT NOT NULL,
    answer_display TEXT NOT NULL,
    PRIMARY KEY (section_id, number)
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    taken_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    score INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    percent REAL NOT NULL DEFAULT 0
);
CREATE TABLE attempt_answers (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    given TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    PRIMARY KEY (attempt_id, number)
);
"""


class TestGenesisSchemaChainsAllThreeMigrationsInOneBoot:
    def test_user_id_is_resolved_before_sessions_backfill_reads_it(
        self, tmp_path, monkeypatch
    ):
        """A database with none of user_id/is_full_attempt/sessions must
        self-heal all three migrations correctly in a single boot, and in
        particular the reconstructed `sessions` row's own `user_id` must be
        the resolved legacy user's id -- not NULL -- proving the sessions
        backfill genuinely runs *after* attempts.user_id has already been
        backfilled, not merely after the column was added."""
        data_dir = tmp_path / "data"
        db_file = data_dir / "auto-grader.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_file)
        try:
            conn.executescript(_GENESIS_SCHEMA)
            wid = conn.execute(
                "INSERT INTO workbooks(title) VALUES ('지네시스 워크북')"
            ).lastrowid
            sid = conn.execute(
                "INSERT INTO sections(workbook_id, label, position)"
                " VALUES (?, 'Day 01', 0)",
                (wid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO answer_keys(section_id, number, answer,"
                " answer_display) VALUES (?, 1, '3', '3')",
                (sid,),
            )
            aid = conn.execute(
                "INSERT INTO attempts(section_id, score, total, percent)"
                " VALUES (?, 1, 1, 100.0)",
                (sid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO attempt_answers(attempt_id, number, given,"
                " expected, status) VALUES (?, 1, '3', '3', 'correct')",
                (aid,),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))
        dal.init_db()  # must not raise

        conn = dal.connect()
        try:
            uid_row = conn.execute(
                "SELECT id FROM users WHERE device_id = 'legacy-device'"
            ).fetchone()
            assert uid_row is not None
            uid = int(uid_row["id"])

            # every migrated table's user_id resolved to the legacy user.
            for table in ("workbooks", "sections", "answer_keys", "attempts"):
                row = conn.execute(f"SELECT user_id FROM {table} LIMIT 1").fetchone()
                assert row["user_id"] == uid, f"{table}.user_id not backfilled"

            att = conn.execute(
                "SELECT is_full_attempt, is_first_submission, submission_seq,"
                " session_id FROM attempts WHERE id = ?",
                (aid,),
            ).fetchone()
            assert att["is_full_attempt"] == 1
            assert att["is_first_submission"] == 1
            assert att["submission_seq"] == 1
            assert att["session_id"] is not None

            sess = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (att["session_id"],)
            ).fetchone()
            # the crux of this test: the backfilled session's user_id must
            # be resolved, not NULL, which only holds if the user_id
            # backfill genuinely ran before this session row was built.
            assert sess["user_id"] == uid
            assert sess["section_id"] == sid
            assert sess["status"] == "finished"
            assert (sess["first_score"], sess["first_total"], sess["first_percent"]) == (
                1,
                1,
                100.0,
            )
            assert sess["started_at"] == sess["finished_at"]

            total_sessions = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions"
            ).fetchone()["n"]
            assert total_sessions == 1

            # end-to-end DAL read path against the resolved uid.
            secs = {s["id"]: s for s in dal.list_sections(conn, wid, uid)}
            assert secs[sid]["session_count"] == 1
            assert secs[sid]["latest_percent"] == 100.0
            assert secs[sid]["best_percent"] == 100.0

            got = dal.get_session(conn, att["session_id"], uid)
            assert got is not None
            assert got["id"] == att["session_id"]
        finally:
            conn.close()

        # idempotent: a second boot must not duplicate columns or sessions.
        dal.init_db()
        conn = dal.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
            for c in ("user_id", "is_full_attempt", "session_id"):
                assert cols.count(c) == 1
            total_sessions = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions"
            ).fetchone()["n"]
            assert total_sessions == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# get_open_session's own user_id scoping (only get_session/finish_session's
# foreign-uid behavior was exercised elsewhere).
# ---------------------------------------------------------------------------


class TestGetOpenSessionForeignUserIsolation:
    def test_foreign_uid_does_not_see_anothers_open_session(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 2, 4, 50.0)
            conn.commit()

            foreign_uid = dal.get_or_create_device_user(conn, str(uuid.uuid4()))["id"]
            conn.commit()

            assert dal.get_open_session(conn, sid, foreign_uid) is None
            # the real owner still sees it, completely unaffected.
            owned = dal.get_open_session(conn, sid, uid)
            assert owned is not None
            assert owned["id"] == session_id
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# create_session relies on the schema DEFAULT for started_at (its own INSERT
# never lists the column).
# ---------------------------------------------------------------------------


class TestCreateSessionStartedAtDefault:
    def test_started_at_is_populated_by_the_schema_default(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 1, 2, 50.0)
            conn.commit()

            sess = dal.get_session(conn, session_id, uid)
            assert sess["started_at"]  # non-null, non-empty
            assert isinstance(sess["started_at"], str)
            # sanity-check it actually looks like the schema's
            # datetime('now', 'localtime') default, not a stray literal.
            assert "-" in sess["started_at"] and ":" in sess["started_at"]
            assert sess["finished_at"] is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# list_finished_sessions ordering with more than one row per section.
# ---------------------------------------------------------------------------


class TestListFinishedSessionsOrderingWithMultipleRows:
    def test_three_finished_sessions_ordered_newest_id_first(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            s1, _ = _finished_attempt(conn, uid, sid, 1, 2, 50.0, "3", "correct")
            s2, _ = _finished_attempt(conn, uid, sid, 0, 2, 0.0, "9", "incorrect")
            s3, _ = _finished_attempt(conn, uid, sid, 2, 2, 100.0, "3", "correct")
            conn.commit()

            finished = dal.list_finished_sessions(conn, sid, uid)
            assert [s["id"] for s in finished] == [s3, s2, s1]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# best_percent (MAX across finished sessions) vs. latest_percent (highest
# sess.id) must be independently correct -- chosen so the best session is
# neither the first nor the last, so the two can't be confused for one
# another by accident.
# ---------------------------------------------------------------------------


class TestBestPercentIsTrueMaximumNotJustLatest:
    def test_middle_session_is_best_but_not_latest(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            _finished_attempt(conn, uid, sid, 1, 2, 50.0, "9", "incorrect")
            _finished_attempt(conn, uid, sid, 9, 10, 90.0, "3", "correct")  # best
            _finished_attempt(conn, uid, sid, 3, 10, 30.0, "9", "incorrect")  # latest
            conn.commit()

            secs = dal.list_sections(conn, wb, uid)
            sec = next(s for s in secs if s["id"] == sid)
            assert sec["session_count"] == 3
            assert sec["best_percent"] == 90.0
            assert sec["latest_percent"] == 30.0
            assert sec["best_percent"] != sec["latest_percent"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# top_missed's rewritten JOIN against sessions: DAL-level proof that
# workbook scoping still holds, independent of the (currently xfail,
# HTTP-level) coverage in tests/test_sessions.py.
# ---------------------------------------------------------------------------


class TestTopMissedCrossWorkbookIsolationAtDalLevel:
    def test_a_finished_miss_in_one_workbook_does_not_leak_into_another(
        self, client, device_id
    ):
        wid_a = client.post("/api/workbooks", json={"title": "워크북 A"}).json()["id"]
        wid_b = client.post("/api/workbooks", json={"title": "워크북 B"}).json()["id"]
        sid_a = _import_headers(client, wid_a).json()["sections"][0]["id"]
        sid_b = _import_headers(client, wid_b).json()["sections"][0]["id"]

        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            # workbook A misses question 1; workbook B misses question 2 --
            # distinct numbers so a leak in either direction is unambiguous.
            _finished_attempt(conn, uid, sid_a, 0, 1, 0.0, "9", "incorrect")

            session_b = dal.create_session(conn, uid, sid_b, 0, 1, 0.0)
            aid_b = dal.create_attempt(
                conn, uid, sid_b, 0, 1, 0.0,
                [{"number": 2, "given": "7", "expected": "3", "status": "incorrect"}],
            )
            _link(conn, aid_b, session_b, True, 1)
            dal.finish_session(conn, session_b, uid)
            conn.commit()

            missed_a = dal.top_missed(conn, wid_a, uid)
            assert [m["number"] for m in missed_a] == [1]
            assert all(m["workbook_id"] == wid_a for m in missed_a)
            assert all(m["section_id"] != sid_b for m in missed_a)

            missed_b = dal.top_missed(conn, wid_b, uid)
            assert [m["number"] for m in missed_b] == [2]
            assert all(m["workbook_id"] == wid_b for m in missed_b)
            assert all(m["section_id"] != sid_a for m in missed_b)
        finally:
            conn.close()
