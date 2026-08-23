from app import db as dal
from app.db import init_db, session
from app.services.normalizer import normalize_answer

DEMO_DAYS = {
    "Day 01": ["3", "4", "1", "5", "2", "4", "3", "2", "5", "1"],
    "Day 02": ["2", "3", "4", "1", "5", "1", "2", "5", "3", "4"],
    "Day 03": ["①", "②", "⑤", "④", "③", "1,3", "2,4", "O", "X", "ㄱ"],
}


def seed() -> int:
    init_db()
    with session() as conn:
        existing = [w for w in dal.list_workbooks(conn) if w["title"] == "데모 워크북"]
        if existing:
            return existing[0]["id"]
        wid = dal.create_workbook(conn, "데모 워크북")
        for pos, (label, answers) in enumerate(DEMO_DAYS.items()):
            sid = dal.insert_section(conn, wid, label, pos)
            items = []
            for i, ans in enumerate(answers, start=1):
                items.append((i, normalize_answer(ans), ans))
            dal.insert_keys(conn, sid, items)
        return wid


if __name__ == "__main__":
    wid = seed()
    print(f"데모 워크북이 준비되었습니다. id={wid}")
