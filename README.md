# GitHub Ponder Team Join Bot

通过 GitHub Issue 表单收集订单号，并使用 GitHub Actions 查询飞书多维表格。订单号与申请人的 GitHub 用户名匹配时，自动将申请人加入组织中的 `ponder` team，然后评论处理结果并关闭 Issue。

## 项目结构

```text
.github/
├── ISSUE_TEMPLATE/
│   └── join-team-a.yml
└── workflows/
    └── join-team-a.yml
README.md
```

## 配置步骤

1. 在飞书开放平台创建企业自建应用，并允许它读取对应的多维表格。
2. 确保多维表格包含 `订单号` 和 `GitHub昵称` 两个字段。
3. 在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中添加：

   - `FEISHU_APP_ID`：飞书应用 ID
   - `FEISHU_APP_SECRET`：飞书应用密钥
   - `FEISHU_APP_TOKEN`：多维表格 App Token
   - `FEISHU_TABLE_ID`：数据表 ID
   - `ORG_ADMIN_PAT`：有权管理组织 Team 成员的 GitHub Token

4. 确认 `.github/workflows/join-team-a.yml` 中的 Team slug。当前设置为 `ponder`。
5. 将仓库放在对应 GitHub Organization 下，并启用 GitHub Actions 与 Issues。

## 使用方式

用户新建 Issue，选择“申请加入 Ponder”表单并填写订单号。工作流会根据标题前缀 `[申请Ponder]` 自动核验、评论结果并关闭申请 Issue。

## 安全说明

- 不要将任何密钥直接写进仓库。
- `ORG_ADMIN_PAT` 只授予管理目标 Team 成员所需的最小权限。
- 工作流仅处理标题以 `[申请Ponder]` 开头的新 Issue，不依赖可被修改的 Label。
