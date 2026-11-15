"""SQLite 持久化层。

并发模型：
- WAL + busy_timeout，读写互不阻塞；
- 进程内 threading.Lock 保证写事务串行，每次上传先在事务外完成全部校验，
  通过后在单事务内整版替换，失败则旧状态原样保留；
- 每次成功提交 state_version 自增，页面/查询可凭版本号确认读到的是
  同一份已提交快照（配方、声明、标注三者同版本）。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 原料声明：(原料号, 修订号) 唯一，过敏原空字符串表示该修订无过敏原
CREATE TABLE IF NOT EXISTS declarations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    allergens   TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(material_id, revision)
);

-- 配方关系边，整版替换
CREATE TABLE IF NOT EXISTS relations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    parent  TEXT NOT NULL,
    child   TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    UNIQUE(parent, child)
);

-- 每次成功提交一条快照记录
CREATE TABLE IF NOT EXISTS snapshots (
    version           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,           -- declarations | relations
    item_count        INTEGER NOT NULL,
    submitted_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('state_version', '0')"
            )

    # ---------- 读 ----------

    def read_state(self) -> dict:
        """读取当前已提交状态的一致性快照（单连接、单事务内完成全部读取）。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            version = int(
                conn.execute(
                    "SELECT value FROM meta WHERE key='state_version'"
                ).fetchone()[0]
            )
            declarations: dict[str, dict[int, tuple[str, ...]]] = {}
            for row in conn.execute(
                "SELECT material_id, revision, allergens FROM declarations"
            ):
                allergens = tuple(
                    a for a in row["allergens"].split("\n") if a
                )
                declarations.setdefault(row["material_id"], {})[
                    row["revision"]
                ] = allergens

            edges: dict[str, list[str]] = {}
            all_nodes: set[str] = set()
            for row in conn.execute(
                "SELECT parent, child FROM relations ORDER BY seq"
            ):
                edges.setdefault(row["parent"], []).append(row["child"])
                all_nodes.add(row["parent"])
                all_nodes.add(row["child"])

            snapshots = [
                dict(r)
                for r in conn.execute(
                    "SELECT version, kind, item_count, submitted_at "
                    "FROM snapshots ORDER BY version DESC LIMIT 10"
                )
            ]
            conn.commit()
        finally:
            conn.close()
        return {
            "version": version,
            "declarations": declarations,
            "edges": edges,
            "all_nodes": all_nodes,
            "snapshots": snapshots,
        }

    # ---------- 写 ----------

    def replace_declarations(
        self, rows: list, on_conflict_revision: bool = False
    ) -> int:
        """整版替换原料声明。

        rows: parse.DeclarationRow 列表（调用方已完成同号同修订异内容校验）。
        单事务：DELETE + INSERT + 快照 + 版本自增，任一步失败全部回滚。
        """
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM declarations")
                conn.executemany(
                    "INSERT INTO declarations(material_id, revision, allergens) "
                    "VALUES (?, ?, ?)",
                    [
                        (r.material_id, r.revision, "\n".join(r.allergens))
                        for r in rows
                    ],
                )
                conn.execute(
                    "INSERT INTO snapshots(kind, item_count) VALUES('declarations', ?)",
                    (len(rows),),
                )
                conn.execute(
                    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                    "WHERE key='state_version'"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return len(rows)

    def replace_relations(self, edges: dict[str, list[str]]) -> int:
        """整版替换配方关系（调用方已完成环检测等全部校验）。"""
        flat = [(p, c, i) for p, kids in edges.items() for i, c in enumerate(kids)]
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM relations")
                conn.executemany(
                    "INSERT INTO relations(parent, child, seq) VALUES (?, ?, ?)",
                    flat,
                )
                conn.execute(
                    "INSERT INTO snapshots(kind, item_count) VALUES('relations', ?)",
                    (len(flat),),
                )
                conn.execute(
                    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                    "WHERE key='state_version'"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return len(flat)
