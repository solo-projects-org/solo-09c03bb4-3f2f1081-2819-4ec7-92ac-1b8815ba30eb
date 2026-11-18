"""SQLite 持久化层。

只持久化“已受理”的地图版本；计算任务是临时的，仅保留在内存中
（详见 jobs.py 与 README 的说明）。
所有写入在单个连接上串行化，并通过 WAL 提升并发读能力。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from . import config


class Database:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS maps (
                    version     TEXT PRIMARY KEY,
                    revision    INTEGER NOT NULL DEFAULT 1,
                    feature_cnt INTEGER NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS map_features (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    version     TEXT NOT NULL REFERENCES maps(version) ON DELETE CASCADE,
                    township_id TEXT NOT NULL,
                    geom_json   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_map_features_version
                    ON map_features(version);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_map_features_version_township
                    ON map_features(version, township_id);

                CREATE TABLE IF NOT EXISTS county_boundaries (
                    version     TEXT PRIMARY KEY,
                    revision    INTEGER NOT NULL DEFAULT 1,
                    geom_json   TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS verifications (
                    verification_id   TEXT PRIMARY KEY,
                    version           TEXT NOT NULL,
                    state             TEXT NOT NULL,
                    pinned_township_revision INTEGER NOT NULL,
                    pinned_county_revision   INTEGER NOT NULL,
                    submitted_at      TEXT NOT NULL,
                    started_at        TEXT,
                    finished_at       TEXT,
                    error             TEXT,
                    error_detail_json TEXT,
                    result_json       TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_verifications_version
                    ON verifications(version);
                CREATE INDEX IF NOT EXISTS idx_verifications_state
                    ON verifications(state);
                """
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def replace_map(
        self, version: str, features: list[tuple[str, dict]]
    ) -> int:
        """整版写入（同版重传 = 整版更正）。

        在单个事务内先删后插；任何一步失败都会回滚，旧版完整保留。
        返回写入后的版本修订号 revision。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT revision FROM maps WHERE version = ?", (version,)
                ).fetchone()
                next_revision = (row["revision"] + 1) if row else 1

                self._conn.execute(
                    "INSERT INTO maps(version, revision, feature_cnt) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(version) DO UPDATE SET "
                    "revision = excluded.revision, "
                    "feature_cnt = excluded.feature_cnt, "
                    "updated_at = datetime('now')",
                    (version, next_revision, len(features)),
                )
                self._conn.execute(
                    "DELETE FROM map_features WHERE version = ?", (version,)
                )
                self._conn.executemany(
                    "INSERT INTO map_features(version, township_id, geom_json) "
                    "VALUES (?, ?, ?)",
                    [
                        (version, township_id, json.dumps(geom, ensure_ascii=False))
                        for township_id, geom in features
                    ],
                )
                self._conn.execute("COMMIT")
                return next_revision
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get_revision(self, version: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM maps WHERE version = ?", (version,)
            ).fetchone()
            return int(row["revision"]) if row else None

    def get_features(self, version: str) -> Optional[list[tuple[str, dict]]]:
        """返回 [(township_id, geojson_geometry_dict), ...]；版本不存在返回 None。"""
        with self._lock:
            if self.get_revision(version) is None:
                return None
            rows = self._conn.execute(
                "SELECT township_id, geom_json FROM map_features "
                "WHERE version = ? ORDER BY id",
                (version,),
            ).fetchall()
            return [(r["township_id"], json.loads(r["geom_json"])) for r in rows]

    def list_versions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, revision, feature_cnt, created_at, updated_at "
                "FROM maps ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 县界（按版受理，同版重传 = 整版更正）
    # ------------------------------------------------------------------
    def replace_county_boundary(
        self, version: str, geom: dict
    ) -> int:
        """整版写入县界；单事务，失败回滚并保留旧县界。返回新修订号。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT revision FROM county_boundaries WHERE version = ?",
                    (version,),
                ).fetchone()
                next_revision = (row["revision"] + 1) if row else 1
                self._conn.execute(
                    "INSERT INTO county_boundaries(version, revision, geom_json) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(version) DO UPDATE SET "
                    "revision = excluded.revision, "
                    "geom_json = excluded.geom_json, "
                    "updated_at = datetime('now')",
                    (version, next_revision, json.dumps(geom, ensure_ascii=False)),
                )
                self._conn.execute("COMMIT")
                return next_revision
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_county_revision(self, version: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM county_boundaries WHERE version = ?",
                (version,),
            ).fetchone()
            return int(row["revision"]) if row else None

    def get_county_boundary(self, version: str) -> Optional[dict]:
        """返回 {revision, geom}；版本不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT revision, geom_json FROM county_boundaries "
                "WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None:
                return None
            return {"revision": int(row["revision"]), "geom": json.loads(row["geom_json"])}

    def list_county_versions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, revision, created_at, updated_at "
                "FROM county_boundaries ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 核验记录（持久化，重启可重查；过期结论只允许 stale/failed）
    # ------------------------------------------------------------------
    def insert_verification(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO verifications(verification_id, version, state, "
                "pinned_township_revision, pinned_county_revision, submitted_at, "
                "started_at, finished_at, error, error_detail_json, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["verification_id"],
                    row["version"],
                    row["state"],
                    row["pinned_township_revision"],
                    row["pinned_county_revision"],
                    row["submitted_at"],
                    row.get("started_at"),
                    row.get("finished_at"),
                    row.get("error"),
                    json.dumps(row["error_detail"], ensure_ascii=False)
                    if row.get("error_detail") is not None
                    else None,
                    json.dumps(row["result"], ensure_ascii=False)
                    if row.get("result") is not None
                    else None,
                ),
            )

    @staticmethod
    def _verification_row(row: sqlite3.Row) -> dict:
        return {
            "verification_id": row["verification_id"],
            "version": row["version"],
            "state": row["state"],
            "pinned_township_revision": int(row["pinned_township_revision"]),
            "pinned_county_revision": int(row["pinned_county_revision"]),
            "submitted_at": row["submitted_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "error_detail": json.loads(row["error_detail_json"])
            if row["error_detail_json"]
            else None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def get_verification(self, verification_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM verifications WHERE verification_id = ?",
                (verification_id,),
            ).fetchone()
            return self._verification_row(row) if row else None

    def list_verifications(self, version: Optional[str] = None) -> list[dict]:
        with self._lock:
            if version is None:
                rows = self._conn.execute(
                    "SELECT * FROM verifications ORDER BY submitted_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM verifications WHERE version = ? "
                    "ORDER BY submitted_at",
                    (version,),
                ).fetchall()
            return [self._verification_row(r) for r in rows]

    def list_pending_verifications(self) -> list[dict]:
        """重启恢复用：取出所有未终结的核验（pending/running）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM verifications WHERE state IN ('pending', 'running')"
            ).fetchall()
            return [self._verification_row(r) for r in rows]

    def _update_verification(
        self, verification_id: str, expected_states: tuple[str, ...], fields: dict
    ) -> bool:
        """条件更新：仅当当前 state 属于 expected_states 时写入，返回是否命中。

        用 state 做乐观锁，保证“更正置 stale”与“工作线程收尾”并发时
        stale 不会被 completed 覆盖。
        """
        if not fields:
            return False
        columns = {
            "state",
            "started_at",
            "finished_at",
            "error",
            "error_detail",
            "result",
        }
        assignments: list[str] = []
        params: list = []
        for key, value in fields.items():
            if key not in columns:
                continue
            if key in ("error_detail", "result"):
                assignments.append(f"{key}_json = ?")
                params.append(
                    json.dumps(value, ensure_ascii=False) if value is not None else None
                )
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        if not assignments:
            return False
        params.extend([verification_id, *expected_states])
        sql = (
            f"UPDATE verifications SET {', '.join(assignments)} "
            f"WHERE verification_id = ? AND state IN "
            f"({', '.join('?' for _ in expected_states)})"
        )
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.rowcount > 0

    def mark_verification_running(
        self, verification_id: str, started_at: str
    ) -> bool:
        return self._update_verification(
            verification_id, ("pending",), {"state": "running", "started_at": started_at}
        )

    def complete_verification(
        self, verification_id: str, finished_at: str, result: dict
    ) -> bool:
        return self._update_verification(
            verification_id,
            ("running",),
            {
                "state": "completed",
                "finished_at": finished_at,
                "result": result,
                "error": None,
                "error_detail": None,
            },
        )

    def fail_verification(
        self,
        verification_id: str,
        finished_at: str,
        error: str,
        error_detail: Optional[dict] = None,
    ) -> bool:
        return self._update_verification(
            verification_id,
            ("pending", "running"),
            {
                "state": "failed",
                "finished_at": finished_at,
                "error": error,
                "error_detail": error_detail,
            },
        )

    def mark_version_verifications_stale(
        self,
        version: str,
        stale_at: str,
        changed_source: str,
        current_township_revision: Optional[int],
        current_county_revision: Optional[int],
    ) -> int:
        """同版县界或乡镇图更正后立即失效该版全部未终结/已通过核验。

        - pending/running：更正后来源已变，不能再交付旧图斑 -> 置 stale；
        - completed：旧结论立即失效 -> 置 stale（即使原结论为未通过，
          其图斑也基于旧来源，同样不得再交付）；
        - 已 failed/stale：保持终态，不复活、不覆盖。
        返回受影响行数。
        """
        detail = {
            "changed_source": changed_source,
            "current_township_revision": current_township_revision,
            "current_county_revision": current_county_revision,
            "invalidated_at": stale_at,
        }
        with self._lock:
            cur = self._conn.execute(
                "UPDATE verifications SET state = 'stale', finished_at = ?, "
                "error = ?, error_detail_json = ?, result_json = NULL "
                "WHERE version = ? AND state IN ('pending', 'running', 'completed')",
                (
                    stale_at,
                    "同版县界或乡镇图已更正，旧核验结论立即失效，请重新提交核验",
                    json.dumps(detail, ensure_ascii=False),
                    version,
                ),
            )
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
