"""县界覆盖核验任务的生命周期管理。

关键一致性规则（与权重任务同源、且更严格——核验记录持久化）：
1. 提交核验时钉取该版乡镇图与县界各自的 revision；
2. 核验在后台线程执行，期间 API 仍可受理乡镇图/县界更正；
3. 同版任一来源更正时，数据库层立即把该版 pending/running/completed
   核验置为 stale（旧图斑清空，绝不交付）；
4. 工作线程收尾前再次比对 revision，并通过 state 乐观锁写回，
   双保险保证来源变化后不会落出 completed；
5. 核验记录持久化在 SQLite，重启后可重查；重启时仍处于
   pending/running 的记录一律落为 failed（中断任务不能恢复为通过），
   已 stale/failed 的终态原样保留——过期结论任何情况下都不会复活为通过。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from . import config, geo
from .db import Database

# 核验状态
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STALE = "stale"


class VerificationError(Exception):
    """核验提交/计算失败（如同版县界或乡镇图缺失）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerificationManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=config.MAX_WORKERS,
            thread_name_prefix="verify-job",
        )

    # ------------------------------------------------------------------
    def recover_after_restart(self) -> int:
        """进程启动恢复：中断的 pending/running 核验落为 failed。

        已 completed 的记录保留，但交付时若 revision 不再匹配会被
        deliver() 判为 stale；stale/failed 终态永不改变。
        """
        interrupted = self.db.list_pending_verifications()
        for row in interrupted:
            self.db.fail_verification(
                row["verification_id"],
                _now(),
                "进程重启导致核验中断，结论未完成，请重新提交核验",
                {"reason": "interrupted_by_restart"},
            )
        return len(interrupted)

    def submit(self, version: str) -> dict:
        """创建并提交核验任务；同版县界或乡镇图缺失立即报错。"""
        township_rev = self.db.get_revision(version)
        if township_rev is None:
            raise VerificationError(f"同版乡镇图不存在：{version}")
        county = self.db.get_county_boundary(version)
        if county is None:
            raise VerificationError(f"同版县界不存在：{version}")

        record = {
            "verification_id": uuid.uuid4().hex,
            "version": version,
            "state": PENDING,
            "pinned_township_revision": township_rev,
            "pinned_county_revision": county["revision"],
            "submitted_at": _now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "error_detail": None,
            "result": None,
        }
        self.db.insert_verification(record)
        self._pool.submit(self._run, record["verification_id"])
        return self.db.get_verification(record["verification_id"])

    def get(self, verification_id: str) -> Optional[dict]:
        return self.db.get_verification(verification_id)

    def list(self, version: Optional[str] = None) -> list[dict]:
        return self.db.list_verifications(version)

    # ------------------------------------------------------------------
    def _run(self, verification_id: str) -> None:
        # 乐观锁：若任务已被更正流程置 stale，running 抢占失败直接退出
        if not self.db.mark_verification_running(verification_id, _now()):
            return
        try:
            self._compute(verification_id)
        except VerificationError as exc:
            self.db.fail_verification(verification_id, _now(), str(exc))
        except Exception as exc:  # 防御：意外异常落 failed，不拖垮线程池
            self.db.fail_verification(
                verification_id,
                _now(),
                f"核验异常：{type(exc).__name__}: {exc}",
            )

    def _compute(self, verification_id: str) -> None:
        record = self.db.get_verification(verification_id)
        if record is None or record["state"] != RUNNING:
            return

        version = record["version"]
        # 1. 读取当前来源（核验期间允许被更正）
        township_stored = self.db.get_features(version)
        county_stored = self.db.get_county_boundary(version)
        if township_stored is None:
            raise VerificationError(f"同版乡镇图已不存在：{version}")
        if county_stored is None:
            raise VerificationError(f"同版县界已不存在：{version}")

        # 2. 收尾前复核 revision：来源在计算期间变化则不再产出旧图斑
        current_township_rev = self.db.get_revision(version)
        current_county_rev = county_stored["revision"]
        if (
            current_township_rev != record["pinned_township_revision"]
            or current_county_rev != record["pinned_county_revision"]
        ):
            self.db._update_verification(
                verification_id,
                ("running",),
                {
                    "state": STALE,
                    "finished_at": _now(),
                    "error": "同版县界或乡镇图在核验期间更正，旧核验已拒绝交付，请重新提交核验",
                    "error_detail": {
                        "changed_source": "revision_mismatch_at_finalize",
                        "current_township_revision": current_township_rev,
                        "current_county_revision": current_county_rev,
                    },
                },
            )
            return

        from shapely.geometry import shape

        county_geom = shape(county_stored["geom"])
        township_geoms = [(tid, shape(g)) for tid, g in township_stored]

        # 3. 等面积投影下核对，图斑反投影回 WGS84 交付
        result = geo.verify_county_coverage(county_geom, township_geoms)
        result["version"] = version
        result["pinned_township_revision"] = record["pinned_township_revision"]
        result["pinned_county_revision"] = record["pinned_county_revision"]

        # 4. 乐观锁写回：仅当仍为 running 时才能 completed；
        #    若期间已被更正流程置 stale，写入失败，旧图斑绝不落库
        if not self.db.complete_verification(verification_id, _now(), result):
            return

    # ------------------------------------------------------------------
    def deliver(self, verification_id: str) -> dict:
        """交付前失效检查；来源已变则置 stale 并拒绝交付旧图斑。"""
        record = self.get(verification_id)
        if record is None:
            raise KeyError(verification_id)

        if record["state"] in (PENDING, RUNNING, FAILED, STALE):
            return record

        # completed：再次核对两个来源的当前 revision
        version = record["version"]
        current_township = self.db.get_revision(version)
        current_county = self.db.get_county_revision(version)
        changed: dict[str, dict[str, int | None]] = {}
        if (
            current_township is None
            or current_township != record["pinned_township_revision"]
        ):
            changed["township_map"] = {
                "pinned_revision": record["pinned_township_revision"],
                "current_revision": current_township,
            }
        if (
            current_county is None
            or current_county != record["pinned_county_revision"]
        ):
            changed["county_boundary"] = {
                "pinned_revision": record["pinned_county_revision"],
                "current_revision": current_county,
            }
        if changed:
            self.db._update_verification(
                verification_id,
                ("completed",),
                {
                    "state": STALE,
                    "finished_at": _now(),
                    "error": "同版县界或乡镇图已更正，旧核验结论已失效，请重新提交核验",
                    "error_detail": {"changed_sources": changed},
                    "result": None,
                },
            )
            record = self.get(verification_id)
        return record

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
