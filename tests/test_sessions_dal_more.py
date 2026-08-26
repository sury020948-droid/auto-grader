"""Independent DAL-level coverage for the sessions schema/migration chunk,
written separately from tests/test_session_migration.py (the implementation's
own test file) as an outside check on the same chunk.

Scope mirrors the chunk: app/db.py only (schema, migration, DAL functions),
no HTTP/router assertions. Where HTTP endpoints are used at all, it's only
via the pre-existing, unmodified `/api/workbooks` and `/sections/import`
routes to build fixture data -- exactly as tests/test_session_migration.py
already does -- never to exercise anything this chunk changed.

This file focuses on angles the implementation's own tests don't already
nail down:
  * The `idx_sessions_one_open` partial-unique constraint verified directly
    at the schema/SQL level (not only indirectly through create_session's
    catch block) -- including that it does NOT restrict different sections,
    multiple *finished* sessions in one section, or reopening a section
    after its prior session finished.
  * A losing create_session race call's parameters are discarded outright --
    the surviving row keeps the winner's original first_score/total/percent.
  * FK ON DELETE CASCADE actually fires (PRAGMA foreign_keys=ON via
    connect()) from sections -> sessions -> attempts.
  * list_session_attempts is ordered by submission_seq specifically (not
    incidentally by id/insertion order), and its rows are exact key-for-key
    matches of what get_attempt() returns for the same id.
  * The session_count rename is a true rename: "attempt_count" must be gone
    from list_sections' output, not merely superseded by an added key.
  * top_missed's new INNER JOIN against sessions tolerates attempts whose
    session_id is still NULL (the real transitional state of every attempt
    created through the live, unmodified /api/attempts endpoint until the
    API-layer chunk lands) without crashing.
  * A section with zero attempts contributes zero phantom sessions during
    the backfill migration.
  * Fresh (non-legacy) databases stay idempotent across repeated init_db()
    calls too, not only migrated ones.
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
    r = client.post("/api/workbooks", json={"title": "세션 DAL 추가 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def two_sections(client):
    wid = client.post("/api/workbooks", json={"title": "세션 DAL 추가 테스트 2"}).json()["id"]
    secs = _import_headers(client, wid).json()["sections"]
    return wid, secs[0]["id"], secs[1]["id"]


def _link(conn, aid, session_id, is_first, seq):
    """Stand-in for what the (later) API-layer chunk does when it links a
    graded submission to its session -- this chunk's create_attempt takes no
    session_id parameter at all."""
    conn.execute(
        "UPDATE attempts SET session_id=?, is_first_submission=?,"
        " submission_seq=? WHERE id=?",
        (session_id, int(is_first), seq, aid),
    )


# ---------------------------------------------------------------------------
# Fresh-database schema shape (no migration involved).
# ---------------------------------------------------------------------------


class TestFreshSchema:
    def test_sessions_table_and_attempts_columns_exist(self, client):
        conn = dal.connect()
        try:
            sess_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
            assert sess_cols == {
                "id",
                "user_id",
                "section_id",
                "status",
                "started_at",
                "finished_at",
                "first_score",
                "first_total",
                "first_percent",
            }

            att_cols = {
                r["name"]: r
                for r in conn.execute("PRAGMA table_info(attempts)")
            }
            for col in ("session_id", "is_first_submission", "submission_seq"):
                assert col in att_cols
            # is_first_submission/submission_seq are NOT NULL with sensible
            # defaults; session_id stays nullable (an attempt isn't wired to
            # a session by this chunk's create_attempt).
            assert att_cols["is_first_submission"]["notnull"] == 1
            assert att_cols["submission_seq"]["notnull"] == 1
            assert att_cols["session_id"]["notnull"] == 0
        finally:
            conn.close()

    def test_expected_indexes_exist(self, client):
        conn = dal.connect()
        try:
            names = {r["name"] for r in conn.execute("PRAGMA index_list(sessions)")}
            assert {"idx_sessions_section", "idx_sessions_user", "idx_sessions_one_open"} <= names

            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index'"
                " AND name='idx_sessions_one_open'"
            ).fetchone()
            assert row is not None
            # it's the *partial* unique index (status='in_progress'), not a
            # blanket unique-per-section index.
            assert "UNIQUE" in row["sql"].upper()
            assert "in_progress" in row["sql"]
        finally:
            conn.close()

    def test_init_db_idempotent_on_a_fresh_non_legacy_database(self, client):
        """The guarded ALTER blocks must also no-op cleanly on a database
        that got its columns from _SCHEMA directly (CREATE TABLE IF NOT
        EXISTS), never having taken the ALTER branch at all."""
        dal.init_db()
        dal.init_db()
        conn = dal.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
            assert cols.count("session_id") == 1
            assert cols.count("is_first_submission") == 1
            assert cols.count("submission_seq") == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# idx_sessions_one_open: schema-level constraint, independent of how
# create_session happens to handle it.
# ---------------------------------------------------------------------------


class TestOneOpenSessionIndexConstraint:
    def test_second_open_session_same_section_violates_index(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            conn.execute(
                "INSERT INTO sessions(user_id, section_id, status) VALUES (?, ?, 'in_progress')",
                (uid, sid),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sessions(user_id, section_id, status)"
                    " VALUES (?, ?, 'in_progress')",
                    (uid, sid),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_open_sessions_in_different_sections_do_not_conflict(
        self, client, two_sections, device_id
    ):
        _, s1, s2 = two_sections
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            conn.execute(
                "INSERT INTO sessions(user_id, section_id, status) VALUES (?, ?, 'in_progress')",
                (uid, s1),
            )
            conn.execute(
                "INSERT INTO sessions(user_id, section_id, status) VALUES (?, ?, 'in_progress')",
                (uid, s2),
            )  # must not raise
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE status = 'in_progress'"
            ).fetchone()["n"]
            assert n == 2
        finally:
            conn.close()

    def test_multiple_finished_sessions_same_section_do_not_conflict(
        self, client, wb, device_id
    ):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            for _ in range(3):
                conn.execute(
                    "INSERT INTO sessions(user_id, section_id, status)"
                    " VALUES (?, ?, 'finished')",
                    (uid, sid),
                )  # must not raise -- the partial index only covers in_progress
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE section_id = ?", (sid,)
            ).fetchone()["n"]
            assert n == 3
        finally:
            conn.close()

    def test_create_session_can_reopen_a_section_after_its_prior_session_finished(
        self, client, wb, device_id
    ):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            first = dal.create_session(conn, uid, sid, 1, 2, 50.0)
            dal.finish_session(conn, first, uid)
            conn.commit()

            second = dal.create_session(conn, uid, sid, 2, 2, 100.0)
            conn.commit()

            assert second != first
            assert dal.get_open_session(conn, sid, uid)["id"] == second
            finished_ids = [s["id"] for s in dal.list_finished_sessions(conn, sid, uid)]
            assert finished_ids == [first]
        finally:
            conn.close()


class TestCreateSessionRacePreservesWinner:
    def test_losing_calls_values_are_discarded(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            winner_id = dal.create_session(conn, uid, sid, 1, 2, 50.0)
            conn.commit()

            # two more "losing" attempts with different scores each
            loser_a = dal.create_session(conn, uid, sid, 9, 9, 9.0)
            loser_b = dal.create_session(conn, uid, sid, 0, 0, 0.0)
            conn.commit()

            assert loser_a == winner_id
            assert loser_b == winner_id

            row = dal.get_session(conn, winner_id, uid)
            assert (row["first_score"], row["first_total"], row["first_percent"]) == (
                1,
                2,
                50.0,
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# FK ON DELETE CASCADE: sections -> sessions -> attempts.
# ---------------------------------------------------------------------------


class TestCascadeDelete:
    def test_deleting_section_cascades_its_sessions_and_linked_attempts(
        self, client, wb, device_id
    ):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 0, 1, 0.0)
            aid = dal.create_attempt(
                conn,
                uid,
                sid,
                0,
                1,
                0.0,
                [{"number": 1, "given": "9", "expected": "3", "status": "incorrect"}],
            )
            _link(conn, aid, session_id, True, 1)
            conn.commit()

            assert dal.get_session(conn, session_id, uid) is not None
            assert dal.get_attempt(conn, aid, uid) is not None

            assert dal.delete_section(conn, sid, uid) is True
            conn.commit()

            assert conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE id = ?", (aid,)
            ).fetchone()["n"] == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# list_session_attempts: true submission_seq ordering + get_attempt parity.
# ---------------------------------------------------------------------------


class TestListSessionAttemptsShape:
    def test_ordered_by_submission_seq_not_by_id_or_insertion_order(
        self, client, wb, device_id
    ):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 0, 1, 0.0)

            # inserted in this order (so ids increase this way) but assigned
            # submission_seq in the OPPOSITE order -- proves the ORDER BY is
            # genuinely keyed on submission_seq, not incidentally on id.
            first_inserted = dal.create_attempt(
                conn, uid, sid, 0, 1, 0.0,
                [{"number": 1, "given": "9", "expected": "3", "status": "incorrect"}],
            )
            second_inserted = dal.create_attempt(
                conn, uid, sid, 1, 1, 100.0,
                [{"number": 1, "given": "3", "expected": "3", "status": "correct"}],
            )
            _link(conn, first_inserted, session_id, False, 2)
            _link(conn, second_inserted, session_id, True, 1)
            conn.commit()

            atts = dal.list_session_attempts(conn, session_id)
            assert [a["id"] for a in atts] == [second_inserted, first_inserted]
            assert [a["submission_seq"] for a in atts] == [1, 2]
        finally:
            conn.close()

    def test_rows_match_get_attempt_key_for_key(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 0, 1, 0.0)
            aid = dal.create_attempt(
                conn, uid, sid, 1, 1, 100.0,
                [{"number": 1, "given": "3", "expected": "3", "status": "correct"}],
            )
            _link(conn, aid, session_id, True, 1)
            conn.commit()

            [via_session] = dal.list_session_attempts(conn, session_id)
            via_get = dal.get_attempt(conn, aid, uid)
            assert via_session == via_get

        finally:
            conn.close()

    def test_session_with_no_attempts_returns_empty_list(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 0, 1, 0.0)
            conn.commit()
            assert dal.list_session_attempts(conn, session_id) == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# session_count really replaces attempt_count (not merely supplements it).
# ---------------------------------------------------------------------------


class TestSessionCountIsATrueRename:
    def test_attempt_count_key_is_absent_from_list_sections(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            finished_id = dal.create_session(conn, uid, sid, 1, 1, 100.0)
            dal.finish_session(conn, finished_id, uid)
            conn.commit()

            secs = dal.list_sections(conn, wb, uid)
            sec = next(s for s in secs if s["id"] == sid)
            assert "session_count" in sec
            assert "attempt_count" not in sec
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# top_missed's new join must tolerate the real transitional state: every
# attempt created through the live create_attempt() has session_id = NULL,
# since this chunk's create_attempt takes no session_id parameter at all.
# ---------------------------------------------------------------------------


class TestTopMissedTolerantOfUnlinkedAttempts:
    def test_unlinked_attempt_is_silently_excluded_not_a_crash(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            # created via the real DAL entry point new attempts use today --
            # session_id is left NULL, exactly as /api/attempts leaves it
            # until the API-layer chunk wires it up.
            dal.create_attempt(
                conn, uid, sid, 0, 1, 0.0,
                [{"number": 1, "given": "9", "expected": "3", "status": "incorrect"}],
            )
            conn.commit()

            missed = dal.top_missed(conn, wb, uid)  # must not raise
            assert missed == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Migration edge case not covered by the implementation's own fixtures: a
# section with zero pre-existing attempts must get zero phantom sessions.
# ---------------------------------------------------------------------------

_MINIMAL_LEGACY_SCHEMA = """
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


class TestMigrationEdgeCases:
    def test_section_with_zero_attempts_gets_zero_phantom_sessions(
        self, tmp_path, monkeypatch
    ):
        data_dir = tmp_path / "data"
        db_file = data_dir / "auto-grader.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_file)
        try:
            conn.executescript(_MINIMAL_LEGACY_SCHEMA)
            device_id = str(uuid.uuid4())
            uid = conn.execute(
                "INSERT INTO users(device_id) VALUES (?)", (device_id,)
            ).lastrowid
            wid = conn.execute(
                "INSERT INTO workbooks(user_id, title) VALUES (?, ?)",
                (uid, "빈 섹션 워크북"),
            ).lastrowid
            # one section with an attempt, one section with none at all
            populated_sid = conn.execute(
                "INSERT INTO sections(user_id, workbook_id, label, position)"
                " VALUES (?, ?, 'Day 01', 0)",
                (uid, wid),
            ).lastrowid
            empty_sid = conn.execute(
                "INSERT INTO sections(user_id, workbook_id, label, position)"
                " VALUES (?, ?, 'Day 02', 1)",
                (uid, wid),
            ).lastrowid
            conn.execute(
                "INSERT INTO answer_keys(section_id, number, user_id, answer,"
                " answer_display) VALUES (?, 1, ?, '3', '3')",
                (populated_sid, uid),
            )
            conn.execute(
                "INSERT INTO answer_keys(section_id, number, user_id, answer,"
                " answer_display) VALUES (?, 1, ?, '3', '3')",
                (empty_sid, uid),
            )
            conn.execute(
                "INSERT INTO attempts(user_id, section_id, score, total,"
                " percent, is_full_attempt) VALUES (?, ?, 1, 1, 100.0, 1)",
                (uid, populated_sid),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))
        dal.init_db()

        conn = dal.connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            assert total == 1  # only the populated section got one

            empty_sessions = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE section_id = ?",
                (empty_sid,),
            ).fetchone()["n"]
            assert empty_sessions == 0

            secs = {s["id"]: s for s in dal.list_sections(conn, wid, uid)}
            assert secs[empty_sid]["session_count"] == 0
            assert secs[empty_sid]["latest_percent"] is None
        finally:
            conn.close()
