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
    if not order:
        return "未填写订单号"
    if not ORDER_PATTERN.fullmatch(order):
        return "订单号格式错误，必须为 P 开头加 18 位数字"
    if int(order[1:]) <= int(minimum[1:]):
        return "订单号不大于配置的最小订单号"
    if not email:
        return "未填写 github账号邮箱"
    if not EMAIL_PATTERN.fullmatch(email):
        return "邮箱格式错误，请填写 GitHub 账号中已验证的完整邮箱"
    return ""


def phase(fields):
    """兼容旧状态；新状态将阶段和原因一起写进用户可见的完成状态列。"""
    status = fieldtext(fields.get(STATUS))
    match = re.match(r"^未完成（([^）]+)）：", status)
    return match.group(1) if match else status


class Sync:
    def __init__(self, config, feishu, github):
        self.config, self.feishu, self.github = config, feishu, github
        self.errors, self.sent = 0, 0

    def update(self, record, status, note, **extra):
        visible = "已完成" if status == "已完成" else f"未完成（{status}）：{note}"
        self.feishu.update(record, {STATUS: visible, NOTE: note, **extra})

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
            self.update(record, "已完成", "邀请已发送，用户仍需在 GitHub 接受", **{
                INVITE_ID: str(invitation["id"]), LOGIN: login})
            return True
        login = fieldtext(fields.get(LOGIN))
        if self.github.active(team, login):
            self.update(record, "已完成", "已确认目标 Team 成员状态为 active")
            return True
        if phase(fields) == "待重试":
            return False
        self.update(record, "结果待确认", "未找到待接受邀请，也未确认目标 Team 成员；保留绑定，不自动重复邀请")
        return True

    def reservations(self, records):
        """先扫描全部历史，已完成记录即使没有新增绑定列，也会占用订单。"""
        owners = {}
        # 已完成记录优先于任何未完成绑定，不能只依赖本轮遍历顺序。
        for completed in (True, False):
            for record in records:
                fields = record["fields"]
                if (phase(fields) in ("已完成", "邀请待接受")) != completed:
                    continue
                order = fieldtext(fields.get(BOUND_ORDER))
                if not order and fieldtext(fields.get(BOUND_EMAIL)):
                    order = fieldtext(fields.get(ORDER))
                if completed:
                    order = order or fieldtext(fields.get(ORDER))
                if order:
                    owners.setdefault(order, record)
        return owners

    def duplicate_reason(self, owner, email):
        fields = owner["fields"]
        existing_email = (fieldtext(fields.get(BOUND_EMAIL)) or fieldtext(fields.get(EMAIL))).casefold()
        if phase(fields) in ("已完成", "邀请待接受"):
            return "此订单已有已完成记录，请勿重复申请；同一订单仅允许一个 GitHub 邮箱"
        if existing_email and existing_email != email:
            return "此订单已绑定其他 GitHub 邮箱，同一订单不能为多个邮箱开通"
        return "此订单已有绑定申请，请勿重复提交；同一订单仅处理一条申请记录"

    def process(self, record, owners, team, pending):
        fields = record["fields"]
        status = phase(fields)
        bound_order = fieldtext(fields.get(BOUND_ORDER))
        bound_email = fieldtext(fields.get(BOUND_EMAIL)).casefold()
        order = bound_order or fieldtext(fields.get(ORDER))
        email = bound_email or fieldtext(fields.get(EMAIL)).casefold()
        owner = owners.get(order)
        # 在状态跳过判断之前拦截重复行，历史绑定和已完成订单均不可重新占用。
        if owner and owner["record_id"] != record["record_id"]:
            self.update(record, "重复申请", self.duplicate_reason(owner, email))
            return
        if bound_order or bound_email:
            if not bound_order or not bound_email:
                self.errors += 1
                self.update(record, "绑定异常", "历史订单绑定不完整，不能发送邀请")
                return
            if (fieldtext(fields.get(ORDER)) != bound_order
                    or fieldtext(fields.get(EMAIL)).casefold() != bound_email):
                self.update(record, "绑定不一致", "订单号或邮箱与已保存的绑定不一致，不会重新授权")
                return
        if status == "已完成":
            return
        if bound_order:
            if status in ("邀请失败", "绑定不一致", "绑定异常"):
                # 将旧版只有阶段名的状态补齐原因，不自动重试明确失败。
                self.update(record, status, fieldtext(fields.get(NOTE)) or "历史申请未完成，订单绑定保留")
                return
            if self.reconcile(record, team, pending):
                return
        else:
            if status not in ("", "待处理", "未完成", "待重试"):
                if status in ("已拒绝", "重复申请"):
                    self.update(record, status, fieldtext(fields.get(NOTE)) or "历史申请未通过")
                return
            reason = validate(order, email, self.config.min_order)
            if reason:
                self.update(record, "已拒绝", reason)
                return
            # 先保存绑定，之后哪怕邀请超时或回写失败，也不允许另一邮箱占用订单。
            self.update(record, "处理中", "已绑定订单与邮箱，正在核对邀请", **{
                BOUND_ORDER: order, BOUND_EMAIL: email, INVITE_ID: "", LOGIN: ""})
            owners[order] = record
            if self.matching_invitation(pending, record["fields"]):
                self.reconcile(record, team, pending)
                return
        if self.sent >= self.config.max_invites:
            self.update(record, "待重试", "本轮邀请数量达到上限，下轮继续处理")
            return
        self.sent += 1
        self.update(record, "处理中", "正在发送 GitHub 邀请")
        try:
            invitation = self.github.invite(team, email)
        except ApiError as exc:
            self.errors += 1
            if exc.status in (400, 403, 404, 422, 429):
                reasons = {400: "GitHub 请求参数错误", 403: "GitHub 拒绝请求，权限或组织策略限制",
                           404: "GitHub 未找到目标资源或凭证无访问权限",
                           422: "GitHub 邀请校验失败", 429: "GitHub 请求频率超限"}
                self.update(record, "待重试" if exc.status in (403, 429) else "邀请失败",
                            f"{reasons[exc.status]}（HTTP {exc.status}），订单绑定保留")
            else:
                self.update(record, "结果待确认", "GitHub 邀请响应未知，先核对状态，不自动重新发送")
            return
        if not invitation.get("id"):
            self.errors += 1
            self.update(record, "结果待确认", "GitHub 邀请响应缺少 ID，不自动重新发送")
            return
        pending.append(invitation)
        self.update(record, "已完成", "邀请已发送，用户仍需在 GitHub 接受", **{
            INVITE_ID: str(invitation["id"]), LOGIN: invitation.get("login") or ""})

    def run(self):
        records = self.feishu.records()
        # 有创建时间时按创建顺序处理；没有时保持接口顺序，不按随机 record_id 排序。
        records.sort(key=lambda r: r.get("created_time") or 0)
        for record in records:
            record.setdefault("fields", {})
            for value in record["fields"].values():
                mask(fieldtext(value))
            # 历史待接受状态已证明邀请发送成功，按新的“邀请已发出”口径迁移。
            # 先迁移再建立订单索引，后续重复申请也会被已完成记录拦截。
            if not self.config.dry_run and phase(record["fields"]) == "邀请待接受":
                self.update(record, "已完成", "历史邀请已发送，用户仍需在 GitHub 接受")
        owners = self.reservations(records)
        try:
            team = self.github.team()
            pending = self.github.pending(team)
        except (ApiError, ValueError) as exc:
            if not self.config.dry_run:
                for record in records:
                    fields = record["fields"]
                    if phase(fields) in ("已完成", "已拒绝", "重复申请", "邀请失败"):
                        continue
                    state = "结果待确认" if fieldtext(fields.get(BOUND_ORDER)) else "待重试"
                    self.update(record, state, f"无法核对目标 Team 或邀请列表：{exc}")
            return 1
        for record in records:
            if self.config.dry_run:
                fields = record["fields"]
                order = fieldtext(fields.get(BOUND_ORDER)) or fieldtext(fields.get(ORDER))
                email = (fieldtext(fields.get(BOUND_EMAIL)) or fieldtext(fields.get(EMAIL))).casefold()
                owner = owners.get(order)
                if phase(fields) in ("已完成", "邀请待接受"):
                    message = "邀请已完成，不重复发送；历史待接受状态正式运行会迁移为已完成"
                elif owner and owner["record_id"] != record["record_id"]:
                    message = self.duplicate_reason(owner, email)
                elif fieldtext(fields.get(BOUND_ORDER)):
                    message = "已有绑定，正式运行先核对邀请状态"
                else:
                    message = validate(order, email, self.config.min_order)
                    if not message:
                        message = "规则通过，正式运行将保存绑定并邀请"
                        owners[order] = record
                print("预演：" + message)
                continue
            try:
                self.process(record, owners, team, pending)
            except ApiError as exc:
                if exc.service.startswith("飞书"):
                    # 飞书故障时无法保证状态写入，停止后依靠已保存的绑定恢复。
                    raise
                self.errors += 1
                self.update(record, "结果待确认", f"GitHub 状态核对失败：{exc}；不重复发送邀请")
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
