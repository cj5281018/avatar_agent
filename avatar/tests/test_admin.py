import unittest
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from hr_service.admin import set_password
from hr_service.app import create_app
from hr_service.db import Store


class AdminTest(unittest.TestCase):
    def test_employee_lifecycle_and_access_controls(self):
        with TemporaryDirectory() as directory:
            set_password(Store(directory), "local-test-password")
            with TestClient(create_app(directory)) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/api/admin/dashboard").status_code, 401)
                login = client.post("/api/admin/login", json={
                    "username": "admin", "password": "local-test-password",
                })
                self.assertEqual(login.status_code, 200)
                headers = {"x-hrdaily-csrf": login.json()["csrf"]}
                person = {"handle": "new_employee", "displayName": "模拟员工",
                          "jobTitle": "测试", "approvers": ["admin"], "active": True}
                url = "/api/admin/employees"
                self.assertEqual(client.post(url, json=person).status_code, 403)
                self.assertEqual(client.post(url, json=person, headers=headers).status_code, 200)
                self.assertEqual(client.post(url, json=person, headers=headers).status_code, 409)
                url += "/new_employee"
                invalid = {**person, "approvers": ["new_employee"]}
                self.assertEqual(client.put(url, json=invalid, headers=headers).status_code, 400)
                disabled = client.put(url, json={**person, "active": False}, headers=headers)
                self.assertFalse(disabled.json()["employee"]["active"])
                enabled = client.put(url, json={**person, "jobTitle": "测试工程师"}, headers=headers)
                self.assertTrue(enabled.json()["employee"]["active"])
                data = client.get("/api/admin/dashboard").json()
                row = next(item for item in data["employees"] if item["handle"] == "new_employee")
                self.assertEqual(row["jobTitle"], "测试工程师")
                self.assertEqual([a["handle"] for a in row["approvers"]], ["admin"])
                self.assertEqual(client.post("/api/admin/logout", headers=headers).status_code, 200)
                self.assertEqual(client.get("/api/admin/dashboard").status_code, 401)
