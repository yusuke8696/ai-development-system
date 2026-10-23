import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class Store:
    """Versioned local records. Updates and replay events commit together."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "workspace.db"
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records(
                  kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                  version INTEGER NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  created_at TEXT NOT NULL, body TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def list(self, kind):
        with self.connection() as db:
            return [dict(json.loads(r["body"]), version=r["version"]) for r in
                    db.execute("SELECT * FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def get(self, kind, record_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        if not row:
            raise KeyError(record_id)
        return dict(json.loads(row["body"]), version=row["version"])

    def put(self, kind, body, expected=None):
        data = dict(body)
        data.setdefault("id", str(uuid.uuid4()))
        data.pop("version", None)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT version FROM records WHERE kind=? AND id=?", (kind, data["id"])).fetchone()
            if old and expected != old["version"]:
                raise Conflict("別の操作で更新されています。再取得してください。")
            if not old and expected not in (None, 0):
                raise Conflict("対象が存在しません。")
            version = old["version"] + 1 if old else 1
            data["updated_at"] = now()
            db.execute("INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,version=excluded.version",
                       (kind, data["id"], json.dumps(data, ensure_ascii=False), version))
            event = {"type": "changed", "kind": kind, "id": data["id"],
                     "project_id": data.get("project_id"), "version": version}
            db.execute("INSERT INTO events(created_at,body) VALUES(?,?)", (now(), json.dumps(event)))
        return dict(data, version=version)

    def change(self, kind, record_id, **changes):
        with self.lock:
            old = self.get(kind, record_id)
            return self.put(kind, dict(old, **changes), old["version"])

    def events(self, after=0):
        with self.connection() as db:
            return [dict(json.loads(r["body"]), seq=r["seq"], timestamp=r["created_at"])
                    for r in db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 200", (after,))]
