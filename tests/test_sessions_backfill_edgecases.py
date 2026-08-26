"""Independent, additional DAL/migration coverage for the "Sessions schema,
migration, and core DAL" chunk, written separately from the implementation's
own tests/test_session_migration.py and tests/test_sessions_dal_more.py as a
third, outside check on the same chunk.

Scope mirrors the chunk: app/db.py only (schema, migration, DAL functions),
no router/schemas.py changes exercised. This file focuses specifically on
boundary conditions of the sessions-backfill heuristic that the existing
coverage does not already pin down, plus a couple of DAL "shape" contracts
useful to the later api_layer chunk:

  * The backfill's per-section `open_session` bookkeeping is correct even
    when two sections' orphan attempts are interleaved by id (not neatly
    grouped section-by-section, which is what every existing fixture uses).
  * A chain of MORE than one partial retry after a full attempt (three
    submissions total) reconstructs as one session with submission_seq
    1, 2, 3 -- not just the two-submission case already covered elsewhere.
  * The backfill's open/attach state is scoped to a single init_db() call,
    not across boots: a partial attempt inserted directly (bypassing the
    session API) *after* an earlier boot already migrated that section gets
    defensively promoted to its own new session on the next boot, rather
    than reaching back to attach onto the previous boot's session. This
    documents a real, non-obvious consequence of the "only rows with
    session_id IS NULL are selected" idempotency design.
  * dal.create_attempt() -- unchanged this chunk, and still the only path
    the live /api/attempts endpoint uses -- produces rows whose new
    session_id/is_first_submission/submission_seq columns land on their
    schema defaults (NULL / 1 / 1) rather than erroring or requiring the
    caller to know about the new columns.
  * get_session()/list_finished_sessions() return dicts with exactly the
    sessions table's column set (a shape contract the later api_layer chunk
    can rely on).
  * list_session_attempts(conn, session_id) takes no uid and enforces no
    ownership itself -- confirmed directly, since callers (the future
    api_layer chunk) must gate access via get_session(conn, sid, uid) first.
  * An independent (re-derived, not imported) regression check that
    is_full_attempt still gates only the one known, soon-to-be-deleted
    list_attempts holdout.
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
    r = client.post("/api/workbooks", json={"title": "세션 백필 경계 테스트"})
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


# The pre-sessions on-disk shape: user_id + is_full_attempt present, no
# sessions/session_id/is_first_submission/submission_seq yet. Kept minimal
# and local to this file (mirrors the pattern already used by
# tests/test_sessions_dal_more.py rather than importing across test files).
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


def _seed_base(conn):
    device_id = str(uuid.uuid4())
    uid = conn.execute(
        "INSERT INTO users(device_id) VALUES (?)", (device_id,)
    ).lastrowid
    wid = conn.execute(
        "INSERT INTO workbooks(user_id, title) VALUES (?, ?)", (uid, "wb")
    ).lastrowid
    return uid, wid


def _mk_section(conn, uid, wid, label):
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


def _mk_attempt(conn, uid, sid, is_full, given, status):
    score = 1 if status == "correct" else 0
    aid = conn.execute(
        "INSERT INTO attempts(user_id, section_id, score, total, percent,"
        " is_full_attempt) VALUES (?, ?, ?, 1, ?, ?)",
        (uid, sid, score, 100.0 * score, is_full),
    ).lastrowid
    conn.execute(
        "INSERT INTO attempt_answers(attempt_id, number, user_id, given,"
        " expected, status) VALUES (?, 1, ?, ?, '3', ?)",
        (aid, uid, given, status),
    )
    return aid


# ---------------------------------------------------------------------------
# Backfill boundary conditions.
# ---------------------------------------------------------------------------


class TestBackfillBoundaryConditions:
    def test_interleaved_sections_track_independent_open_state(
        self, tmp_path, monkeypatch
    ):
        """Orphans are walked in one global `ORDER BY id` sweep across every
        section at once; the per-section `open_session` dict must still
        attribute each attempt to the right chain even when two sections'
        rows interleave by id, not merely when the whole fixture is grouped
        section-by-section (as every other seed in this project does)."""
        data_dir = tmp_path / "data"
        db_file = data_dir / "auto-grader.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_file)
        try:
            conn.executescript(_LEGACY_SCHEMA)
            uid, wid = _seed_base(conn)
            sec_a = _mk_section(conn, uid, wid, "A")
            sec_b = _mk_section(conn, uid, wid, "B")

            # Interleaved by id: A-full, B-full, A-retry, B-retry, A-retake.
            a1 = _mk_attempt(conn, uid, sec_a, 1, "9", "incorrect")
            b1 = _mk_attempt(conn, uid, sec_b, 1, "3", "correct")
            a2 = _mk_attempt(conn, uid, sec_a, 0, "3", "correct")
            b2 = _mk_attempt(conn, uid, sec_b, 0, "1", "incorrect")
            a3 = _mk_attempt(conn, uid, sec_a, 1, "5", "incorrect")
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))
        dal.init_db()

        conn = dal.connect()
        try:
            link = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, section_id, session_id, is_first_submission,"
                    " submission_seq FROM attempts"
                )
            }
            # A's retry attaches onto A's base session...
            assert link[a2]["session_id"] == link[a1]["session_id"]
            assert link[a2]["is_first_submission"] == 0
            assert link[a2]["submission_seq"] == 2
            # ...B's retry attaches onto B's base session (a DIFFERENT
            # session than A's, despite interleaved ids)...
            assert link[b2]["session_id"] == link[b1]["session_id"]
            assert link[a1]["session_id"] != link[b1]["session_id"]
            # ...and A's later full retake starts a brand-new third session,
            # unaffected by B's rows landing in between by id.
            assert link[a3]["session_id"] not in (
                link[a1]["session_id"],
                link[b1]["session_id"],
            )
            assert link[a3]["is_first_submission"] == 1
            assert link[a3]["submission_seq"] == 1

            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            assert total == 3
        finally:
            conn.close()

    def test_chain_of_two_partial_retries_increments_seq_to_three(
        self, tmp_path, monkeypatch
    ):
        """A full attempt followed by TWO partial retries (not just one, as
        the implementation's own fixtures exercise) must all land in one
        session with submission_seq 1, 2, 3 and is_first_submission
        1, 0, 0."""
        data_dir = tmp_path / "data"
        db_file = data_dir / "auto-grader.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_file)
        try:
            conn.executescript(_LEGACY_SCHEMA)
            uid, wid = _seed_base(conn)
            sid = _mk_section(conn, uid, wid, "Day 01")
            a1 = _mk_attempt(conn, uid, sid, 1, "9", "incorrect")
            a2 = _mk_attempt(conn, uid, sid, 0, "1", "incorrect")
            a3 = _mk_attempt(conn, uid, sid, 0, "3", "correct")
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))
        dal.init_db()

        conn = dal.connect()
        try:
            link = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, session_id, is_first_submission, submission_seq"
                    " FROM attempts ORDER BY id"
                )
            }
            assert link[a1]["session_id"] == link[a2]["session_id"] == link[a3]["session_id"]
            assert [link[a]["is_first_submission"] for a in (a1, a2, a3)] == [1, 0, 0]
            assert [link[a]["submission_seq"] for a in (a1, a2, a3)] == [1, 2, 3]

            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            assert total == 1
        finally:
            conn.close()

    def test_orphan_inserted_between_boots_does_not_reattach_to_prior_session(
        self, tmp_path, monkeypatch
    ):
        """The backfill's `open_session` bookkeeping lives only for the
        duration of one init_db() call (it is rebuilt from an empty dict
        each time, and only ever looks at rows currently satisfying
        `session_id IS NULL`). A partial attempt written directly to the
        attempts table (bypassing the session API entirely, as every attempt
        through the live, unmodified /api/attempts endpoint does today)
        *after* an earlier boot already finished migrating that section's
        prior rows must NOT reach back and attach onto that earlier
        session -- it has no preceding *orphan* full row to attach to in
        this boot's own sweep, so it is defensively promoted to its own new
        one-submission finished session instead. This is a real, non-obvious
        consequence of the migration's idempotency design worth pinning
        down explicitly."""
        data_dir = tmp_path / "data"
        db_file = data_dir / "auto-grader.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_file)
        try:
            conn.executescript(_LEGACY_SCHEMA)
            uid, wid = _seed_base(conn)
            sid = _mk_section(conn, uid, wid, "Day 01")
            a1 = _mk_attempt(conn, uid, sid, 1, "3", "correct")
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))
        dal.init_db()  # first boot: a1 migrates into its own session

        conn = dal.connect()
        try:
            first_session_id = conn.execute(
                "SELECT session_id FROM attempts WHERE id = ?", (a1,)
            ).fetchone()["session_id"]
            assert first_session_id is not None

            # Simulate a partial attempt made through old, un-migrated code
            # between the two boots -- written directly, no session linkage.
            a2 = conn.execute(
                "INSERT INTO attempts(user_id, section_id, score, total,"
                " percent, is_full_attempt) VALUES (?, ?, 0, 1, 0.0, 0)",
                (uid, sid),
            ).lastrowid
            conn.execute(
                "INSERT INTO attempt_answers(attempt_id, number, user_id,"
                " given, expected, status) VALUES (?, 1, ?, '9', '3',"
                " 'incorrect')",
                (a2, uid),
            )
            conn.commit()
        finally:
            conn.close()

        dal.init_db()  # second boot: must migrate a2 without disturbing a1

        conn = dal.connect()
        try:
            link = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, session_id, is_first_submission, submission_seq"
                    " FROM attempts"
                )
            }
            # a1's original linkage from the first boot is untouched.
            assert link[a1]["session_id"] == first_session_id
            assert link[a1]["submission_seq"] == 1

            # a2 gets its OWN new session, not first_session_id.
            assert link[a2]["session_id"] is not None
            assert link[a2]["session_id"] != first_session_id
            assert link[a2]["is_first_submission"] == 1
            assert link[a2]["submission_seq"] == 1

            a2_sess = dal.get_session(conn, link[a2]["session_id"], uid)
            assert a2_sess["status"] == "finished"
            assert (a2_sess["first_score"], a2_sess["first_total"], a2_sess["first_percent"]) == (
                0,
                1,
                0.0,
            )

            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            assert total == 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# create_attempt() (unchanged this chunk) against the new schema's defaults.
# ---------------------------------------------------------------------------


class TestCreateAttemptAgainstNewSchemaDefaults:
    def test_new_columns_land_on_schema_defaults_not_errors(self, client, wb, device_id):
        """create_attempt's own INSERT statement was not touched by this
        chunk and lists no session_id/is_first_submission/submission_seq --
        confirms the new NOT NULL columns' schema defaults absorb that
        cleanly rather than raising, and that session_id stays NULL (this is
        exactly the transitional state every attempt made through the live,
        unmodified /api/attempts endpoint is in until the API-layer chunk
        wires session linkage in)."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            aid = dal.create_attempt(
                conn,
                uid,
                sid,
                1,
                1,
                100.0,
                [{"number": 1, "given": "3", "expected": "3", "status": "correct"}],
            )
            conn.commit()

            row = conn.execute(
                "SELECT session_id, is_first_submission, submission_seq"
                " FROM attempts WHERE id = ?",
                (aid,),
            ).fetchone()
            assert row["session_id"] is None
            assert row["is_first_submission"] == 1
            assert row["submission_seq"] == 1

            # get_attempt's dict output carries the same columns through.
            att = dal.get_attempt(conn, aid, uid)
            assert att["session_id"] is None
            assert att["is_first_submission"] == 1
            assert att["submission_seq"] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# DAL "shape" contracts useful to the later api_layer chunk.
# ---------------------------------------------------------------------------


class TestSessionDictShapeContracts:
    def test_get_session_has_exact_sessions_column_set(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 2, 4, 50.0)
            conn.commit()

            sess = dal.get_session(conn, session_id, uid)
            assert set(sess) == {
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
            assert isinstance(session_id, int)
        finally:
            conn.close()

    def test_list_finished_sessions_rows_have_exact_sessions_column_set(
        self, client, wb, device_id
    ):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            session_id = dal.create_session(conn, uid, sid, 1, 1, 100.0)
            dal.finish_session(conn, session_id, uid)
            conn.commit()

            [row] = dal.list_finished_sessions(conn, sid, uid)
            assert set(row) == {
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
        finally:
            conn.close()


class TestListSessionAttemptsOwnershipIsCallerResponsibility:
    def test_returns_rows_for_any_session_id_regardless_of_owner(
        self, client, wb, device_id
    ):
        """list_session_attempts(conn, session_id) takes no uid parameter at
        all (per this chunk's own spec) and enforces no ownership check
        itself -- unlike get_session/finish_session, which both correctly
        refuse a foreign uid. This is the intended split of responsibility:
        the (future) API-layer chunk must call get_session(conn, sid, uid)
        to authorize *before* calling list_session_attempts. Pinning this
        down guards against either an accidental uid filter being added
        later (breaking the documented signature) or the ownership check
        silently being dropped from get_session (a real access-control gap
        this test would not directly catch, but the asymmetry it documents
        makes that gap easier to notice in review)."""
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

            foreign_uid = dal.get_or_create_device_user(conn, str(uuid.uuid4()))["id"]
            conn.commit()

            # get_session correctly gates on uid...
            assert dal.get_session(conn, session_id, foreign_uid) is None
            # ...but list_session_attempts has no uid parameter to gate
            # with, and returns the real rows for this session_id regardless.
            atts = dal.list_session_attempts(conn, session_id)
            assert [a["id"] for a in atts] == [aid]
        finally:
            conn.close()


class TestIsFullAttemptGatingIndependentlyReverified:
    def test_only_the_known_list_attempts_holdout_branches_on_it(self):
        """Re-derived independently of tests/test_session_migration.py's own
        copy of this same guard: is_full_attempt must not gate any query
        this chunk touched or added -- the sole exception is the pre-
        existing list_attempts filter (backing GET /sections/{sid}/attempts,
        which the session-model plan deletes outright once the API layer
        lands, not this chunk's job to rewire)."""
        import re
        from pathlib import Path

        src = Path(dal.__file__).read_text(encoding="utf-8")
        gating = re.findall(r"(?i)(?:WHERE|AND)\s+[\w.]*\bis_full_attempt\b", src)
        assert gating == ["AND a.is_full_attempt"], gating
