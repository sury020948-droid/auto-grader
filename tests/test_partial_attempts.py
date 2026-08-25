"""New coverage for the partial-retry `is_full_attempt` flag.

This targets ground the implementation's own tests (test_sessions.py::
TestRetryMerge, test_api.py::TestImportAndGrading) do not already cover:

  * Migration safety for a pre-existing on-disk database that predates the
    `is_full_attempt` column. This repo's real data/auto-grader.db is
    *exactly* such a database today (confirmed by direct, read-only
    inspection: its `attempts` table currently has columns
    ['id', 'section_id', 'taken_at', 'score', 'total', 'percent', 'user_id']
    -- no `is_full_attempt`). init_db() must self-heal that shape on the
    next boot with no data loss, no crash, and stay idempotent across
    repeated boots (a missing "column already exists" guard would otherwise
    crash every restart after the first with a "duplicate column name"
    OperationalError).
  * DAL-level filtering (app/db.py), exercised directly and independent of
    the HTTP layer, including the "only partial attempts exist yet" edge
    case and a `top_missed` double-count regression a naive implementation
    could reintroduce.
  * HTTP-level edge cases the implementation's own new tests did not touch:
    the GET /api/workbooks *list* endpoint's latest_percent (only the
    singular GET /workbooks/{wid} was covered), a retry chained onto a
    previous retry (merge_attempt_id pointing at a partial attempt rather
    than the original full one), and top_missed staying stable across a
    real merge-retry HTTP round trip.
"""

import sqlite3
import uuid

import pytest

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
    r = client.post("/api/workbooks", json={"title": "부분 재시도 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def two_sections(client):
    wid = client.post("/api/workbooks", json={"title": "부분 재시도 세션"}).json()["id"]
    secs = _import_headers(client, wid).json()["sections"]
    return wid, secs[0]["id"], secs[1]["id"]


# ---------------------------------------------------------------------------
# Migration safety against a pre-existing, is_full_attempt-less database.
# ---------------------------------------------------------------------------

# Mirrors the *current* on-disk schema (post user_id/device_id migration --
# confirmed by direct inspection of the real data/auto-grader.db) minus only
# the new `is_full_attempt` column, i.e. exactly "predates this column".
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
    percent REAL NOT NULL DEFAULT 0
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


def _seed_legacy_db(db_file):
    """Build a fresh sqlite file with the pre-`is_full_attempt` schema above,
    populated with one workbook/section/answer-key and two attempt rows that
    have no `is_full_attempt` column at all -- standing in for a row created
    before this feature existed."""
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
        for aid, given, status in (
            (aid1, "3", "correct"),
            (aid2, "9", "incorrect"),
        ):
            conn.execute(
                "INSERT INTO attempt_answers(attempt_id, number, user_id,"
                " given, expected, status) VALUES (?, 1, ?, ?, '3', ?)",
                (aid, uid, given, status),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "device_id": device_id,
        "uid": uid,
        "wid": wid,
        "sid": sid,
        "aid1": aid1,
        "aid2": aid2,
    }


class TestMigrationOfPreExistingDatabase:
    def test_init_db_self_heals_missing_column_without_data_loss(
        self, tmp_path, monkeypatch
    ):
        data_dir = tmp_path / "data"
        seed = _seed_legacy_db(data_dir / "auto-grader.db")
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))

        from app import db as dal

        # Must not raise -- this is what boots against the real, pre-existing
        # data/auto-grader.db the very next time the server starts.
        dal.init_db()

        conn = dal.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
            assert "is_full_attempt" in cols

            rows = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT id, score, total, percent, is_full_attempt"
                    " FROM attempts ORDER BY id"
                )
            }
            assert set(rows) == {seed["aid1"], seed["aid2"]}
            # every pre-existing row backfilled to 1 (full attempt), with
            # every other column left exactly as it was -- no data loss.
            assert rows[seed["aid1"]]["is_full_attempt"] == 1
            assert rows[seed["aid1"]]["score"] == 1
            assert rows[seed["aid1"]]["total"] == 1
            assert rows[seed["aid1"]]["percent"] == 100.0
            assert rows[seed["aid2"]]["is_full_attempt"] == 1
            assert rows[seed["aid2"]]["score"] == 0
            assert rows[seed["aid2"]]["percent"] == 0.0

            # the sibling attempt_answers table (untouched by this migration)
            # survived intact too.
            ans = conn.execute(
                "SELECT attempt_id, given, status FROM attempt_answers"
                " ORDER BY attempt_id"
            ).fetchall()
            assert [(r["attempt_id"], r["given"], r["status"]) for r in ans] == [
                (seed["aid1"], "3", "correct"),
                (seed["aid2"], "9", "incorrect"),
            ]

            # migrated DAL read paths run cleanly end-to-end against the
            # backfilled legacy rows -- this is what every page load does
            # against the real deployed database post-migration.
            secs = dal.list_sections(conn, seed["wid"], seed["uid"])
            assert len(secs) == 1
            assert secs[0]["attempt_count"] == 2
            assert secs[0]["best_percent"] == 100.0
            assert secs[0]["latest_percent"] == 0.0  # aid2 has the higher id

            books = dal.list_workbooks(conn, seed["uid"])
            assert books[0]["latest_percent"] == 0.0

            attempts_list = dal.list_attempts(conn, seed["sid"], seed["uid"])
            assert [a["id"] for a in attempts_list] == [seed["aid2"], seed["aid1"]]
        finally:
            conn.close()

        # Idempotency: booting again against the now-migrated file must not
        # error (no "duplicate column name") and must not add the column
        # twice.
        dal.init_db()
        conn = dal.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
            assert cols.count("is_full_attempt") == 1
        finally:
            conn.close()

    def test_migrated_legacy_db_serves_and_accepts_new_attempts_via_http(
        self, tmp_path, monkeypatch
    ):
        """After booting the live app against a pre-existing legacy-shaped
        database, the HTTP API must see the backfilled legacy rows and must
        be able to write both new full and new partial attempts alongside
        them without error."""
        data_dir = tmp_path / "data"
        seed = _seed_legacy_db(data_dir / "auto-grader.db")
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(data_dir))

        from fastapi.testclient import TestClient

        from app.main import create_app

        headers = {"X-Device-User-Id": seed["device_id"]}
        with TestClient(create_app(), headers=headers) as client:
            # app startup (lifespan -> init_db()) migrated the file; the
            # pre-existing legacy attempts are visible and counted.
            hist = client.get(f"/api/sections/{seed['sid']}/attempts").json()
            assert len(hist) == 2

            full = client.post(
                "/api/attempts",
                json={"section_id": seed["sid"], "answers": {"1": "3"}},
            ).json()
            assert full["is_full_attempt"] is True

            hist2 = client.get(f"/api/sections/{seed['sid']}/attempts").json()
            assert len(hist2) == 3

            retry = client.post(
                "/api/attempts",
                json={
                    "section_id": seed["sid"],
                    "answers": {"1": "3"},
                    "merge_attempt_id": full["id"],
                },
            ).json()
            assert retry["is_full_attempt"] is False

            hist3 = client.get(f"/api/sections/{seed['sid']}/attempts").json()
            assert len(hist3) == 3  # retry excluded; legacy rows still present


# ---------------------------------------------------------------------------
# DAL-level filtering (app/db.py), independent of the HTTP layer.
# ---------------------------------------------------------------------------


class TestDalFiltering:
    def test_create_attempt_defaults_to_full_when_flag_omitted(
        self, client, wb, device_id
    ):
        """Any caller of create_attempt() that doesn't pass is_full_attempt
        (positionally or by keyword) must still get a full attempt -- the
        parameter default, not just the API layer, is what protects every
        other/future caller."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]

        from app import db as dal

        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            aid = dal.create_attempt(conn, uid, sid, 1, 1, 100.0, [])
            conn.commit()
            att = dal.get_attempt(conn, aid, uid)
            assert att["is_full_attempt"] == 1
        finally:
            conn.close()

    def test_full_vs_partial_filtered_from_every_aggregate_query(
        self, client, wb, device_id
    ):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]

        from app import db as dal

        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            full_id = dal.create_attempt(
                conn,
                uid,
                sid,
                4,
                5,
                80.0,
                [
                    {"number": 1, "given": "3", "expected": "3", "status": "correct"},
                    {"number": 2, "given": "9", "expected": "4", "status": "incorrect"},
                ],
                is_full_attempt=True,
            )
            partial_id = dal.create_attempt(
                conn,
                uid,
                sid,
                5,
                5,
                100.0,
                [
                    {"number": 1, "given": "3", "expected": "3", "status": "correct"},
                    {"number": 2, "given": "4", "expected": "4", "status": "correct"},
                ],
                is_full_attempt=False,
            )
            conn.commit()
            assert full_id != partial_id

            attempts = dal.list_attempts(conn, sid, uid)
            assert [a["id"] for a in attempts] == [full_id]

            secs = dal.list_sections(conn, wb, uid)
            sec = next(s for s in secs if s["id"] == sid)
            assert sec["attempt_count"] == 1
            assert sec["latest_percent"] == 80.0
            assert sec["best_percent"] == 80.0  # NOT 100.0 from the partial

            books = dal.list_workbooks(conn, uid)
            assert books[0]["latest_percent"] == 80.0

            summary = dal.get_workbook_summary(conn, wb, uid)
            assert summary["latest_percent"] == 80.0
        finally:
            conn.close()

    def test_top_missed_ignores_partial_retries_and_does_not_double_count(
        self, client, wb, device_id
    ):
        """A retried question that is *still* wrong in the partial attempt
        must not double-count in top_missed, and a question that is *only*
        ever wrong inside a partial attempt must not appear in top_missed
        at all."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]

        from app import db as dal

        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            dal.create_attempt(
                conn,
                uid,
                sid,
                3,
                5,
                60.0,
                [
                    {"number": 1, "given": "3", "expected": "3", "status": "correct"},
                    {"number": 2, "given": "9", "expected": "4", "status": "incorrect"},
                    {"number": 3, "given": "1", "expected": "1", "status": "correct"},
                    {"number": 4, "given": "5", "expected": "5", "status": "correct"},
                    {"number": 5, "given": "1", "expected": "2", "status": "incorrect"},
                ],
                is_full_attempt=True,
            )
            # A partial retry of Q2 and Q5 -- still wrong on both, plus (in
            # spirit of a free-form retry) leaves both re-graded.
            dal.create_attempt(
                conn,
                uid,
                sid,
                0,
                2,
                0.0,
                [
                    {"number": 2, "given": "1", "expected": "4", "status": "incorrect"},
                    {"number": 5, "given": "3", "expected": "2", "status": "incorrect"},
                ],
                is_full_attempt=False,
            )
            conn.commit()

            missed = dal.top_missed(conn, wb, uid)
            by_num = {m["number"]: m["count"] for m in missed}
            # 1 each, from the full attempt only -- NOT 2 (which a query
            # missing the is_full_attempt filter would produce).
            assert by_num == {2: 1, 5: 1}
        finally:
            conn.close()

    def test_section_with_only_partial_attempts_reports_no_stats(
        self, client, wb, device_id
    ):
        """Edge case: a section that has only ever received partial retries
        (no full attempt yet) must report zero attempts and null best/latest
        percent, not crash or leak the partial's numbers."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]

        from app import db as dal

        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            dal.create_attempt(conn, uid, sid, 5, 5, 100.0, [], is_full_attempt=False)
            conn.commit()

            secs = dal.list_sections(conn, wb, uid)
            sec = next(s for s in secs if s["id"] == sid)
            assert sec["attempt_count"] == 0
            assert sec["latest_percent"] is None
            assert sec["best_percent"] is None

            books = dal.list_workbooks(conn, uid)
            assert books[0]["latest_percent"] is None

            summary = dal.get_workbook_summary(conn, wb, uid)
            assert summary["latest_percent"] is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# HTTP-level edge cases not already exercised by the implementation's tests.
# ---------------------------------------------------------------------------


class TestApiAggregateExclusionEdgeCases:
    def test_workbook_list_latest_percent_excludes_partial_retry(self, client, wb):
        """GET /api/workbooks (the list endpoint) was not covered by the
        implementation's own tests -- only the singular GET /workbooks/{wid}
        was. It has its own separate SQL and must be checked independently."""
        sid = _import_headers(client, wb).json()["sections"][0]["id"]

        base = client.post(
            "/api/attempts",
            json={
                "section_id": sid,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5", "5": "2"},
            },
        ).json()
        assert base["percent"] == 80.0

        retry = client.post(
            "/api/attempts",
            json={
                "section_id": sid,
                "answers": {"2": "4"},
                "merge_attempt_id": base["id"],
            },
        ).json()
        assert retry["percent"] == 100.0
        assert retry["is_full_attempt"] is False

        books = client.get("/api/workbooks").json()
        item = next(b for b in books if b["id"] == wb)
        assert item["latest_percent"] == 80.0  # base's, not the retry's 100.0

    def test_retry_chained_onto_previous_retry_stays_partial(
        self, client, two_sections
    ):
        """merge_attempt_id may point at a previous *retry*, not only at the
        original full attempt (get_attempt() fetches by id unfiltered, by
        design, so this is reachable). The whole chain must stay anchored to
        the original full attempt for history/stats purposes."""
        wid, s1, _ = two_sections

        base = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5", "5": "9"},
            },
        ).json()
        assert base["percent"] == 60.0
        assert set(base["wrong_numbers"]) == {2, 5}

        retry1 = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"2": "4"},
                "merge_attempt_id": base["id"],
            },
        ).json()
        assert retry1["is_full_attempt"] is False
        assert set(retry1["wrong_numbers"]) == {5}

        # chained: merges onto retry1, not onto the original base attempt.
        retry2 = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"5": "2"},
                "merge_attempt_id": retry1["id"],
            },
        ).json()
        assert retry2["is_full_attempt"] is False
        assert retry2["percent"] == 100.0

        # neither retry in the chain ever touched history/aggregates --
        # still anchored on the original 60% base attempt throughout.
        hist = client.get(f"/api/sections/{s1}/attempts").json()
        assert len(hist) == 1
        assert hist[0]["id"] == base["id"]

        stats = client.get(f"/api/workbooks/{wid}/stats").json()
        sec = next(s for s in stats["sections"] if s["section_id"] == s1)
        assert sec["attempt_count"] == 1
        assert sec["latest_percent"] == sec["best_percent"] == 60.0

        # both retries remain independently fetchable by id, each with its
        # own (different) score.
        fetched1 = client.get(f"/api/attempts/{retry1['id']}").json()
        assert fetched1["percent"] == retry1["percent"] == 80.0
        fetched2 = client.get(f"/api/attempts/{retry2['id']}").json()
        assert fetched2["percent"] == 100.0

    def test_top_missed_stable_across_real_merge_retry_round_trip(
        self, client, two_sections
    ):
        """End-to-end (through the real from-misses + merge flow, not direct
        DAL calls): retrying a miss and getting it wrong again must not
        double its top_missed count."""
        wid, s1, _ = two_sections

        base = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5", "5": "2"},
            },
        ).json()
        assert set(base["wrong_numbers"]) == {2}

        misses = client.post(
            "/api/attempts/from-misses", json={"attempt_id": base["id"]}
        ).json()
        assert misses["numbers"] == [2]

        client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"2": "1"},  # retried, still wrong (different miss)
                "merge_attempt_id": base["id"],
            },
        )

        top = client.get(f"/api/workbooks/{wid}/stats").json()["top_missed"]
        q2 = [t for t in top if t["number"] == 2 and t["section_id"] == s1]
        assert len(q2) == 1
        assert q2[0]["count"] == 1  # not 2 -- the retry's repeat miss excluded
