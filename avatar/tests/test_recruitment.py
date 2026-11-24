import unittest
from datetime import date, datetime
from tempfile import TemporaryDirectory

from hr_service import recruitment
from hr_service.db import ServiceError, Store, now


class RecruitmentTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        recruitment.seed(self.store)

    def test_scope_draft_and_idempotent_advance_after_restart(self):
        scoped = recruitment.list_applications(self.store, "bob", now(), candidate="小林")
        self.assertEqual([row["applicationId"] for row in scoped], ["APP-01"])
        self.assertEqual(len(recruitment.list_applications(self.store, "carol", now(), candidate="小林")), 2)
        with self.assertRaises(ServiceError):
            recruitment.job_facts(self.store, "bob", "JOB-OPS")
        draft = recruitment.execute(self.store, "bob", "recruitment.jd", {"jobId": "JOB-PY"})
        self.assertEqual(draft["missingFields"], ["工作地点"])
        application = scoped[0]
        parameters = {key: application[key] for key in ("applicationId", "jobTitle", "revision")}
        parameters.update(fromStage="待筛选", toStage="面试中", reason="初筛完成")
        result = recruitment.advance(self.store, "bob", "same-operation", parameters, now())
        restarted = Store(self.directory.name)
        recruitment.seed(restarted)
        self.assertEqual(recruitment.operation_result(restarted, "bob", "same-operation"), result)
        self.assertEqual(recruitment.advance(restarted, "bob", "same-operation", parameters, now()), result)
        with self.assertRaises(ServiceError) as stale:
            recruitment.advance(restarted, "bob", "different-operation", parameters, now())
        self.assertEqual(stale.exception.status, 409)
        with restarted.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM recruitment_events WHERE application='APP-01'")
                             .fetchone()[0], 2)

    def test_weekly_deduplication_and_exclusive_cutoff(self):
        def at(day):
            return int(datetime(2026, 11, day, tzinfo=recruitment.SHANGHAI).timestamp() * 1000)
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM recruitment_events")
            db.executemany("INSERT INTO recruitment_events(application,stage,occurred_at) VALUES (?,?,?)", (
                ("APP-01", "面试中", at(16)), ("APP-01", "待筛选", at(17)),
                ("APP-01", "面试中", at(18)), ("APP-01", "面试完成", at(23)),
                ("APP-02", "面试中", at(15)), ("APP-02", "面试完成", at(19)),
                ("APP-03", "面试中", at(16)),
            ))
        result = recruitment.weekly_report(self.store, "bob", date(2026, 11, 16), at(24))
        self.assertEqual(result["jobIds"], ["JOB-PY"])
        self.assertEqual(result["enteredInterview"], 1)
        self.assertEqual(result["stageCounts"], {"待筛选": 0, "面试中": 1, "面试完成": 1})
