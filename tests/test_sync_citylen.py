import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sync_citylen import (ApiError, BOUND_EMAIL, BOUND_ORDER, Config, EMAIL, Feishu,
                         GitHub, INVITE_ID, LOGIN, NOTE, ORDER, STATUS, Sync,
                         fieldtext, validate)

MINIMUM = "P100000000000000000"
VALID = "P100000000000000001"
TEAM = {"id": 42, "name": "cityLen", "slug": "citylen"}


def config(**overrides):
    return Config("app", "secret", "base", "table", "token", "org", MINIMUM, **overrides)


def record(record_id="r1", order=VALID, email="buyer@example.com", **fields):
    return {"record_id": record_id, "fields": {ORDER: order, EMAIL: email, STATUS: "待处理", **fields}}


class FakeFeishu:
    def __init__(self, records, fail_after_invite=False):
        self.rows = records
        self.writes = []
        self.fail_after_invite = fail_after_invite

    def records(self):
        # 模拟 API 返回快照，而非直接修改持久存储。
        return copy.deepcopy(self.rows)

    def update(self, row, fields):
        if self.fail_after_invite and fields.get(STATUS) == "邀请待接受":
            raise ApiError("飞书", status=503)
        self.writes.append(copy.deepcopy(fields))
        persisted = next(r for r in self.rows if r["record_id"] == row["record_id"])
        persisted["fields"].update(fields)
        row["fields"].update(fields)


class FakeGitHub:
    def __init__(self, pending=None, error=None, active=False):
        self.invitations = pending or []
        self.error = error
        self.is_active = active
        self.calls = []

    def team(self):
        return TEAM

    def pending(self, team):
        return copy.deepcopy(self.invitations)

    def active(self, team, login):
        return bool(login) and self.is_active

    def invite(self, team, email):
        self.calls.append((team["id"], email))
        if self.error:
            raise self.error
        result = {"id": 77, "email": email, "login": "buyer"}
        self.invitations.append(result)
        return result


class SyncTests(unittest.TestCase):
    def test_new_invitation_is_pending_not_completed(self):
        fs, gh = FakeFeishu([record(email="Buyer@Example.com")]), FakeGitHub()
        self.assertEqual(Sync(config(), fs, gh).run(), 0)
        self.assertEqual(gh.calls, [(42, "buyer@example.com")])
        self.assertEqual(fs.rows[0]["fields"][STATUS], "邀请待接受")
        self.assertEqual(fs.rows[0]["fields"][INVITE_ID], "77")
        self.assertEqual(fs.writes[0][BOUND_ORDER], VALID)

    def test_same_order_different_email_only_invites_once_across_runs(self):
        fs = FakeFeishu([record(), record("r2", email="other@example.com")])
        gh = FakeGitHub()
        Sync(config(), fs, gh).run()
        Sync(config(), fs, gh).run()
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(fs.rows[1]["fields"][STATUS], "重复申请")

    def test_invalid_order_or_email_never_invites(self):
        fs = FakeFeishu([record(order=MINIMUM), record("r2", order="P123"),
                         record("r3", email="not-an-email")])
        gh = FakeGitHub()
        Sync(config(), fs, gh).run()
        self.assertEqual(gh.calls, [])
        self.assertTrue(all(r["fields"][STATUS] == "已拒绝" for r in fs.rows))

    def test_timeout_without_pending_never_blindly_retries(self):
        fs, gh = FakeFeishu([record()]), FakeGitHub(error=ApiError("GitHub"))
        self.assertEqual(Sync(config(), fs, gh).run(), 1)
        gh.error = None
        Sync(config(), fs, gh).run()
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(fs.rows[0]["fields"][STATUS], "结果待确认")

    def test_explicit_rejection_preserves_failure_on_next_run(self):
        fs, gh = FakeFeishu([record()]), FakeGitHub(error=ApiError("GitHub", status=422))
        Sync(config(), fs, gh).run()
        Sync(config(), fs, gh).run()
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(fs.rows[0]["fields"][STATUS], "邀请失败")

    def test_remote_success_writeback_failure_recovers_without_new_invite(self):
        fs, gh = FakeFeishu([record()], fail_after_invite=True), FakeGitHub()
        with self.assertRaises(ApiError):
            Sync(config(), fs, gh).run()
        self.assertEqual(fs.rows[0]["fields"][STATUS], "处理中")
        fs.fail_after_invite = False
        Sync(config(), fs, gh).run()
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(fs.rows[0]["fields"][STATUS], "邀请待接受")

    def test_email_edit_cannot_change_saved_binding(self):
        fs, gh = FakeFeishu([record()]), FakeGitHub(error=ApiError("GitHub", status=429))
        Sync(config(), fs, gh).run()
        fs.rows[0]["fields"][EMAIL] = "other@example.com"
        gh.error = None
        Sync(config(), fs, gh).run()
        self.assertEqual(gh.calls[-1], (42, "buyer@example.com"))

    def test_existing_pending_invitation_is_reused(self):
        fs = FakeFeishu([record()])
        gh = FakeGitHub(pending=[{"id": 90, "email": "buyer@example.com", "login": None}])
        Sync(config(), fs, gh).run()
        self.assertEqual(gh.calls, [])
        self.assertEqual(fs.rows[0]["fields"][INVITE_ID], "90")

    def test_active_member_confirms_completion(self):
        fs = FakeFeishu([record(**{BOUND_ORDER: VALID, BOUND_EMAIL: "buyer@example.com",
                                  INVITE_ID: "77", LOGIN: "buyer", STATUS: "邀请待接受"})])
        gh = FakeGitHub(active=True)
        Sync(config(), fs, gh).run()
        self.assertEqual(fs.rows[0]["fields"][STATUS], "已完成")
        self.assertEqual(gh.calls, [])

    def test_missing_login_and_disappearing_invite_is_not_assumed_accepted(self):
        fs = FakeFeishu([record(**{BOUND_ORDER: VALID, BOUND_EMAIL: "buyer@example.com",
                                  INVITE_ID: "77", STATUS: "邀请待接受"})])
        gh = FakeGitHub(active=True)
        Sync(config(), fs, gh).run()
        self.assertEqual(fs.rows[0]["fields"][STATUS], "结果待确认")
        self.assertEqual(gh.calls, [])

    def test_dry_run_does_not_write_or_invite(self):
        fs, gh = FakeFeishu([record()]), FakeGitHub()
        Sync(config(dry_run=True), fs, gh).run()
        self.assertEqual(fs.writes, [])
        self.assertEqual(gh.calls, [])

    def test_request_cap_is_resumable(self):
        fs = FakeFeishu([record(), record("r2", order="P100000000000000002", email="b@example.com")])
        gh = FakeGitHub()
        Sync(config(max_invites=1), fs, gh).run()
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(fs.rows[1]["fields"][STATUS], "待重试")
        Sync(config(max_invites=1), fs, gh).run()
        self.assertEqual(len(gh.calls), 2)

    def test_duplicate_history_stops_before_any_invite(self):
        fields = {BOUND_ORDER: VALID, BOUND_EMAIL: "buyer@example.com"}
        fs, gh = FakeFeishu([record(**fields), record("r2", **fields)]), FakeGitHub()
        with self.assertRaises(ValueError):
            Sync(config(), fs, gh).run()
        self.assertEqual(gh.calls, [])


class ApiTests(unittest.TestCase):
    def test_rich_text_is_normalized(self):
        self.assertEqual(fieldtext([{"text": " P100"}, {"text": "001 "}]), "P100001")

    def test_order_comparison_preserves_leading_zeroes(self):
        self.assertEqual(validate("P000000000000000002", "b@example.com", "P000000000000000001"), "")

    def test_feishu_http_success_business_error_is_rejected(self):
        fs = object.__new__(Feishu)
        fs.base, fs.headers = "https://example.com", {}
        with patch("sync_citylen.request_json", return_value={"code": 1254000, "msg": "error"}):
            with self.assertRaises(ApiError):
                fs.call("GET", "/records")

    def test_feishu_pagination_includes_history_on_later_page(self):
        fs = object.__new__(Feishu)
        with patch.object(fs, "call", side_effect=[
            {"items": [record()], "has_more": True, "page_token": "next"},
            {"items": [record("r2")], "has_more": False},
        ]) as call:
            self.assertEqual(len(fs.records()), 2)
            self.assertIn("page_token=next", call.call_args_list[1].args[1])

    def test_missing_team_fails_without_fallback_to_ponder(self):
        gh = GitHub(config())
        with patch.object(gh, "pages", return_value=[{"name": "ponder", "slug": "ponder", "id": 1}]):
            with self.assertRaises(ValueError):
                gh.team()

    def test_team_display_name_resolves_lowercase_slug(self):
        gh = GitHub(config())
        with patch.object(gh, "pages", return_value=[TEAM]):
            self.assertEqual(gh.team(), TEAM)

    def test_invitation_payload_uses_email_and_target_team(self):
        gh = GitHub(config())
        with patch.object(gh, "call", return_value={"id": 1}) as call:
            gh.invite(TEAM, "buyer@example.com")
            call.assert_called_once_with("POST", "/invitations", {
                "email": "buyer@example.com", "role": "direct_member", "team_ids": [42]})

    def test_configuration_requires_valid_citylen_threshold(self):
        env = {"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "CITYLEN_FEISHU_APP_TOKEN": "b",
               "CITYLEN_FEISHU_TABLE_ID": "t", "ORG_ADMIN_PAT": "g", "CITYLEN_ORG": "org",
               "CITYLEN_MIN_ORDER_NO": "P123"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
