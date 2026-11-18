import unittest
from tempfile import TemporaryDirectory

from hr_service import lifecycle, recruitment, registry
from hr_service.db import ServiceError, Store, now


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        recruitment.seed(self.store)
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO recruitment_events(application,stage,occurred_at) VALUES ('APP-01','面试完成',?)", (now(),))

    def call(self, action, params, operation=None, actor="bob"):
        return lifecycle.execute(self.store, actor, action, params, operation)

    def sent(self):
        draft = self.call("offer.save_draft", {"applicationId":"APP-01","expectedRevision":0,"plannedDate":"2026-12-07"}, "draft")
        fields = {key: draft[key] for key in ("offerId","version","revision")}
        return self.call("offer.record_sent", fields, "send")

    def acceptance(self):
        offer = self.sent()
        params = {key: offer[key] for key in ("offerId","version","revision")}
        return {**params,"response":"accepted","reason":"已收到模拟回复","plannedDate":"2026-12-07","template":"intern-v1"}

    def test_accept_replay_restart_and_unique_handoff(self):
        params = self.acceptance()
        first = self.call("offer.record_response", params, "accept")
        self.store = Store(self.store.path.parent)
        self.assertEqual(lifecycle.operation_result(self.store,"bob","offer.record_response","accept"), first)
        self.assertEqual(self.call("offer.record_response", params, "accept"), first)
        another = self.call("offer.record_response", params, "second-card", actor="carol")
        self.assertEqual(first["onboardingId"], another["onboardingId"])
        with self.assertRaises(ServiceError):
            self.call("offer.record_response", {**params,"plannedDate":"2026-12-10"}, "conflict")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hr_onboarding").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM hr_onboarding_tasks").fetchone()[0],3)

    def test_report_arrival_tasks_and_close(self):
        offer = self.call("offer.record_response", self.acceptance(), "accept")
        key = offer["onboardingId"]
        with self.assertRaises(ServiceError):
            self.call("onboarding.get", {"onboardingId":key}, actor="marco")
        row = self.call("onboarding.reschedule", {"onboardingId":key,"revision":1,"plannedDate":"2026-12-10","reason":"改期"}, "move")
        self.assertEqual(row["tasks"][0]["due"],"2026-12-08")
        row = self.call("onboarding.record_arrival", {"onboardingId":key,"revision":2,"actualDate":"2026-12-10"}, "arrive")
        with self.assertRaises(ServiceError):
            self.call("onboarding.close", {"onboardingId":key,"revision":row["revision"]}, "early")
        for task in row["tasks"]:
            row = self.call("onboarding.update_task", {"onboardingId":key,"revision":row["revision"],
                            "taskId":task["taskId"],"state":"completed","evidence":"演示核对"}, "done-"+task["taskId"])
        result = self.call("onboarding.close", {"onboardingId":key,"revision":row["revision"]}, "close")
        self.assertEqual(result["state"],"completed")

    def test_old_contract_compatibility_window_is_action_specific(self):
        self.assertTrue(registry.allows_previous(recruitment.ADVANCE, registry.PREVIOUS_FINGERPRINT))
        self.assertFalse(registry.allows_previous("offer.record_response", registry.PREVIOUS_FINGERPRINT))
        self.assertFalse(registry.allows_previous(recruitment.ADVANCE, ("1.0.0","invalid")))
