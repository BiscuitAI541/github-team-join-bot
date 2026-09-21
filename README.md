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

## 使用方式

用户新建 Issue，选择“申请加入 Ponder”表单并填写订单号。工作流会根据标题前缀 `[申请Ponder]` 自动核验、评论结果并关闭申请 Issue。

## 安全说明

- 不要将任何密钥直接写进仓库。
- `ORG_ADMIN_PAT` 只授予管理目标 Team 成员所需的最小权限。
- 工作流仅处理标题以 `[申请Ponder]` 开头的新 Issue，不依赖可被修改的 Label。
