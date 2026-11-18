import unittest
from unittest.mock import patch

from avatar_service.mcp_bridge import handle


class HrToolSchemaTest(unittest.TestCase):
    def test_discovery_advertises_fields_and_enums(self):
        tools = {tool["name"]: tool["inputSchema"] for tool in handle({"method": "tools/list"})["tools"]}
        submit = tools["prepare_leave_request"]
        review = tools["prepare_leave_review"]
        self.assertEqual(set(submit["required"]), {"startDate", "endDate", "leaveType", "reason"})
        self.assertEqual(set(review["required"]), {"requestId", "decision"})
        self.assertEqual(review["properties"]["decision"]["enum"], ["approve", "reject"])
        self.assertFalse(submit["additionalProperties"])
        self.assertFalse(review["additionalProperties"])

    def test_both_tools_use_existing_confirmation_actions(self):
        for tool, action, arguments in (
            ("prepare_leave_request", "request_leave", {"startDate": "2027-01-12", "endDate": "2027-01-12",
                                                        "leaveType": "annual", "reason": "私事"}),
            ("prepare_leave_review", "review_leave", {"requestId": "leave_example", "decision": "approve"}),
        ):
            with self.subTest(tool=tool), patch("avatar_service.mcp_bridge.call") as invoke:
                handle({"method": "tools/call", "params": {"name": tool, "arguments": arguments}})
                invoke.assert_called_once_with({"service": "hr-leave-python", "action": action, "input": arguments})

    def test_misspelled_or_missing_fields_do_not_dispatch(self):
        with patch("avatar_service.mcp_bridge.call") as invoke:
            result = handle({"method": "tools/call", "params": {"name": "prepare_leave_request",
                                                                 "arguments": {"start_date": "2027-01-12"}}})
        self.assertTrue(result["isError"])
        invoke.assert_not_called()
