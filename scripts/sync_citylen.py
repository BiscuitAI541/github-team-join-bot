#!/usr/bin/env python3
"""从飞书申请表读取订单和邮箱，为 cityLen Team 发送 GitHub 邀请。"""

import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ORDER_PATTERN = re.compile(r"P[0-9]{18}")
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
ORDER, EMAIL, STATUS = "订单号", "github账号邮箱", "完成状态"
NOTE, INVITE_ID, LOGIN = "处理说明", "邀请ID", "GitHub账号"
BOUND_ORDER, BOUND_EMAIL = "绑定订单号", "绑定邮箱"


class ApiError(Exception):
    def __init__(self, service, status=None, code=None):
        self.service, self.status, self.code = service, status, code
        # 不打印响应体、请求 URL 或用户输入，避免把邮箱、订单和令牌写进日志。
        super().__init__(f"{service} 请求失败（HTTP={status}, code={code}）")


def fieldtext(value):
    """飞书文本字段可能返回字符串，也可能返回富文本片段数组。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(str(part.get("text", "")) for part in value if isinstance(part, dict)).strip()
    return str(value).strip()


def mask(value):
    if os.getenv("GITHUB_ACTIONS") == "true" and value:
        value = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::add-mask::{value}", flush=True)


def request_json(service, method, url, headers, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
    except HTTPError as exc:
        raise ApiError(service, status=exc.code) from None
    except (URLError, TimeoutError, OSError):
        raise ApiError(service) from None
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        # 写请求可能已经生效：响应解析失败也不能盲目重发邀请。
        raise ApiError(service) from None


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    app_token: str
    table_id: str
    github_token: str
    org: str
    min_order: str
    team_name: str = "cityLen"
    dry_run: bool = False
    max_invites: int = 20

    @classmethod
    def from_env(cls):
        keys = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "CITYLEN_FEISHU_APP_TOKEN",
                "CITYLEN_FEISHU_TABLE_ID", "ORG_ADMIN_PAT", "CITYLEN_ORG", "CITYLEN_MIN_ORDER_NO"]
        missing = [key for key in keys if not os.getenv(key, "").strip()]
        if missing:
            raise ValueError("缺少配置：" + ", ".join(missing))
        values = [os.environ[key].strip() for key in keys]
        if not ORDER_PATTERN.fullmatch(values[-1]):
            raise ValueError("CITYLEN_MIN_ORDER_NO 必须为 P + 18 位数字")
        if not re.fullmatch(r"[A-Za-z0-9-]+", values[-2]):
            raise ValueError("CITYLEN_ORG 必须是组织登录名")
        maximum = int(os.getenv("CITYLEN_MAX_INVITES", "20"))
        if not 1 <= maximum <= 50:
            raise ValueError("CITYLEN_MAX_INVITES 必须在 1～50 之间")
        return cls(*values, team_name=os.getenv("CITYLEN_TEAM_NAME", "cityLen").strip(),
                   dry_run=os.getenv("DRY_RUN", "false").lower() == "true", max_invites=maximum)


class Feishu:
    def __init__(self, config):
        response = request_json("飞书认证", "POST",
                                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                {"Content-Type": "application/json"},
                                {"app_id": config.app_id, "app_secret": config.app_secret})
        if response.get("code") != 0 or not response.get("tenant_access_token"):
            raise ApiError("飞书认证", code=response.get("code"))
        token = response["tenant_access_token"]
        mask(token)
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.base = ("https://open.feishu.cn/open-apis/bitable/v1/apps/"
                     f"{quote(config.app_token, safe='')}/tables/{quote(config.table_id, safe='')}")

    def call(self, method, path, body=None):
        response = request_json("飞书", method, self.base + path, self.headers, body)
        # 飞书可能 HTTP 200 但业务 code 非零，必须同时检查。
        if response.get("code") != 0:
            raise ApiError("飞书", code=response.get("code"))
        return response.get("data", {})

    def records(self):
        records, token = [], ""
        for _ in range(50):
            query = {"page_size": 500}
            if token:
                query["page_token"] = token
            data = self.call("GET", "/records?" + urlencode(query))
            if not isinstance(data.get("items"), list):
                raise ApiError("飞书记录列表")
            records.extend(data["items"])
            if not data.get("has_more"):
                return records
            next_token = data.get("page_token")
            if not next_token or next_token == token:
                raise ApiError("飞书分页")
            token = next_token
        # 未读取完整的表，不能开始邀请，否则后面页的历史绑定可能被漏掉。
        raise ValueError("记录超过 50 页，本次未执行任何邀请")

    def update(self, record, fields):
        self.call("PUT", "/records/" + quote(record["record_id"], safe=""), {"fields": fields})
        record["fields"].update(fields)


class GitHub:
    def __init__(self, config):
        self.config = config
        self.headers = {"Authorization": f"Bearer {config.github_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
        self.base = "https://api.github.com/orgs/" + quote(config.org, safe="")

    def call(self, method, path, body=None):
        return request_json("GitHub", method, self.base + path, self.headers, body)

    def pages(self, path):
        items = []
        for page in range(1, 101):
            batch = self.call("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ApiError("GitHub 列表")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ValueError("GitHub 列表超过 100 页，本次停止处理")

    def team(self):
        # 显示名称 cityLen 与 GitHub 生成的 slug citylen 可能不同，使用 API 返回值。
        teams = [team for team in self.pages("/teams")
                 if self.config.team_name.casefold() in
                 (team.get("name", "").casefold(), team.get("slug", "").casefold())]
        if len(teams) != 1:
            raise ValueError("未找到唯一的目标 Team，请检查 CITYLEN_ORG 和 CITYLEN_TEAM_NAME")
        return teams[0]

    def pending(self, team):
        # 只使用目标 Team 的邀请，不误认其他产品的组织邀请。
        return self.pages(f"/teams/{quote(team['slug'], safe='')}/invitations")

    def active(self, team, login):
        if not login:
            return False
        try:
            membership = self.call("GET", f"/teams/{quote(team['slug'], safe='')}/memberships/"
                                   + quote(login, safe=""))
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise
        return membership.get("state") == "active"

    def invite(self, team, email):
        return self.call("POST", "/invitations", {"email": email, "role": "direct_member",
                                                  "team_ids": [team["id"]]})


def validate(order, email, minimum):
    if not ORDER_PATTERN.fullmatch(order) or int(order[1:]) <= int(minimum[1:]):
        return "订单号格式或范围不符合规则"
    if not EMAIL_PATTERN.fullmatch(email):
        return "请填写 GitHub 账号中已验证的完整邮箱"
    return ""


class Sync:
    def __init__(self, config, feishu, github):
        self.config, self.feishu, self.github = config, feishu, github
        self.errors, self.sent = 0, 0

    def update(self, record, status, note, **extra):
        self.feishu.update(record, {STATUS: status, NOTE: note, **extra})

    def matching_invitation(self, pending, fields):
        invitation_id = fieldtext(fields.get(INVITE_ID))
        email = fieldtext(fields.get(BOUND_EMAIL)).casefold()
        return next((item for item in pending
                     if (invitation_id and str(item.get("id")) == invitation_id)
                     or (item.get("email") and item["email"].casefold() == email)), None)

    def reconcile(self, record, team, pending):
        fields = record["fields"]
        invitation = self.matching_invitation(pending, fields)
        if invitation:
            login = invitation.get("login") or fieldtext(fields.get(LOGIN))
            self.update(record, "邀请待接受", "邀请已发送，请在 GitHub 接受", **{
                INVITE_ID: str(invitation["id"]), LOGIN: login})
            return True
        login = fieldtext(fields.get(LOGIN))
        if self.github.active(team, login):
            self.update(record, "已完成", "已确认目标 Team 成员状态为 active")
            return True
        if fieldtext(fields.get(STATUS)) == "待重试":
            return False
        self.update(record, "结果待确认", "未找到待接受邀请，也未确认目标 Team 成员；保留绑定，不自动重复邀请")
        return True

    def run(self):
        records = self.feishu.records()
        required = {ORDER, EMAIL, STATUS, NOTE, INVITE_ID, LOGIN, BOUND_ORDER, BOUND_EMAIL}
        # 所有历史绑定都参与去重，包括处于未知结果状态的记录。
        reservations = {}
        for record in records:
            fields = record.get("fields", {})
            order = fieldtext(fields.get(BOUND_ORDER))
            email = fieldtext(fields.get(BOUND_EMAIL))
            if bool(order) != bool(email):
                raise ValueError("发现不完整的订单绑定，本次停止处理")
            if order:
                if order in reservations:
                    raise ValueError("同一订单存在多条历史绑定，本次停止处理")
                reservations[order] = record["record_id"]
            if not {ORDER, EMAIL}.issubset(fields):
                # 空表无需创建任何邀请；有记录但缺输入字段时及时报告。
                raise ValueError("记录缺少订单号或 github账号邮箱字段，请检查表格列名")
            for field in required:
                mask(fieldtext(fields.get(field)))

        team = self.github.team()
        pending = self.github.pending(team)
        # 同一批重复订单按创建时间排序，绑定成功后其他申请不会再发送。
        records.sort(key=lambda r: (r.get("created_time", 0), r["record_id"]))
        for record in records:
            fields = record["fields"]
            status = fieldtext(fields.get(STATUS))
            bound = fieldtext(fields.get(BOUND_ORDER))
            if self.config.dry_run:
                # 预演不修改飞书、不发送邀请，仅统计符合规则的未绑定申请。
                if not bound and status in ("", "待处理"):
                    reason = validate(fieldtext(fields.get(ORDER)), fieldtext(fields.get(EMAIL)), self.config.min_order)
                    print("预演：" + (reason or "规则通过，正式运行将检查绑定并邀请"))
                continue
            if bound:
                if status in ("已完成", "邀请失败"):
                    continue
                if self.reconcile(record, team, pending):
                    continue
                order = bound
                email = fieldtext(fields.get(BOUND_EMAIL))
            else:
                if status not in ("", "待处理"):
                    continue
                order, email = fieldtext(fields.get(ORDER)), fieldtext(fields.get(EMAIL)).casefold()
                reason = validate(order, email, self.config.min_order)
                if reason:
                    self.update(record, "已拒绝", reason)
                    continue
                if order in reservations:
                    self.update(record, "重复申请", "此订单已经绑定申请记录，不再为其他记录开通")
                    continue
                # 先写入持久绑定再调用 GitHub，崩溃、取消或回写失败后仍有恢复依据。
                self.update(record, "处理中", "已绑定订单与邮箱，正在核对邀请", **{
                    BOUND_ORDER: order, BOUND_EMAIL: email, INVITE_ID: "", LOGIN: ""})
                reservations[order] = record["record_id"]
                if self.matching_invitation(pending, record["fields"]):
                    self.reconcile(record, team, pending)
                    continue
            if self.sent >= self.config.max_invites:
                self.update(record, "待重试", "本轮邀请数量达到上限，下轮继续处理")
                continue
            self.sent += 1
            # 重试也先改为处理中：如果进程在 POST 后被取消，下次不能按待重试直接重发。
            self.update(record, "处理中", "正在发送 GitHub 邀请")
            try:
                invitation = self.github.invite(team, email)
            except ApiError as exc:
                self.errors += 1
                if exc.status in (400, 403, 404, 422, 429):
                    self.update(record, "待重试" if exc.status in (403, 429) else "邀请失败",
                                f"GitHub 拒绝请求（HTTP {exc.status}）；订单绑定保留")
                else:
                    self.update(record, "结果待确认", "邀请响应未知，下次先核对 GitHub，不自动重新发送")
                continue
            if not invitation.get("id"):
                self.errors += 1
                self.update(record, "结果待确认", "邀请响应缺少 ID，不自动重新发送")
                continue
            pending.append(invitation)
            self.update(record, "邀请待接受", "邀请已发送，请在 GitHub 接受", **{
                INVITE_ID: str(invitation["id"]), LOGIN: invitation.get("login") or ""})
        print(f"处理结束：读取 {len(records)} 条，本轮邀请请求 {self.sent} 次，异常 {self.errors} 次，预演={self.config.dry_run}")
        return 1 if self.errors else 0


def main():
    try:
        config = Config.from_env()
        for value in (config.app_secret, config.github_token, config.min_order):
            mask(value)
        return Sync(config, Feishu(config), GitHub(config)).run()
    except (ApiError, ValueError) as exc:
        print(f"任务停止：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
