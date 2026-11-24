import json
import os
from pathlib import Path
from unittest.mock import patch

from avatar_service.services import DEFAULT_HR_CONFIG, ServiceBroker
from hr_service import recruitment, registry
from hr_service.db import Store, now
from test_leave_confirmation import ConfirmationFixture


class UpgradeTest(ConfirmationFixture):
    def test_packaged_release_matches_hr_export(self):
        self.assertEqual(json.loads(DEFAULT_HR_CONFIG.read_text(encoding="utf-8")),
                         registry.release_configuration())

    async def test_confirm_existing_recruitment_card_after_upgrade(self):
        store = Store(self.hr_dir)
        recruitment.seed(store)
        with store.connection(write=True) as db:
            db.execute("INSERT INTO recruitment_grants VALUES ('marco', 'JOB-PY')")
        application, = recruitment.list_applications(store, "marco", now(), candidate="小林")
        params = {name: application[name] for name in ("applicationId", "jobTitle", "revision")}
        params.update(fromStage="待筛选", toStage="面试中", reason="初筛完成")
        # Values persisted by the 1.0.0 release, independent of today's exporter.
        version = "1.0.0"
        checksum = "1ec3c4f71b085c2f98ffb47722a87953fddb34807dead6d725ba07d340a9db3f"
        old_config = {**registry.release_configuration(), "contractVersion": version,
                      "manifestSha256": checksum, "allowedActions": list(registry.PREVIOUS["actions"]),
                      "compatibleActions": []}
        pin = Path(self.avatar_dir, "old-contract.json")
        pin.write_text(json.dumps(old_config), encoding="utf-8")

        async def transport(broker, service, path, body):
            if path == "/manifest":
                return (await hr.get(path)).json()
            response = await hr.post(path, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        with patch.object(ServiceBroker, "read_response", transport):
            with (patch.dict(os.environ, {"PY_AVATAR_HR_CONTRACT_FILE": str(pin)}),
                  patch.object(registry, "MANIFEST", registry.PREVIOUS)):
                async with self.services() as (avatar, hr):
                    proposal = await avatar.post("/internal/services/tool", json={
                        "capability": "cap", "service": "hr", "action": recruitment.ADVANCE, "input": params})
                    self.assertEqual(proposal.status_code, 200, proposal.text)
                    operation = proposal.json()["operationId"]

            url = f"/v1/sessions/original/operations/{operation}/confirm"
            disabled = {**registry.release_configuration(), "compatibleActions": []}
            pin.write_text(json.dumps(disabled), encoding="utf-8")
            with patch.dict(os.environ, {"PY_AVATAR_HR_CONTRACT_FILE": str(pin)}):
                async with self.services() as (avatar, hr):
                    denied = await avatar.post(url, headers=self.marco)
                    self.assertEqual(denied.status_code, 409, denied.text)

            async with self.services() as (avatar, hr):
                for _ in range(2):
                    result = await avatar.post(url, headers=self.marco)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(result.json()["status"], "succeeded", result.text)
        with store.connection() as db:
            count = db.execute("SELECT count(*) FROM recruitment_events WHERE application=? AND stage=?",
                               ("APP-01", "面试中")).fetchone()[0]
            self.assertEqual(count, 1)
