# daily-reflection-data（云端自动同步版）

每日反思小程序的公开数据源仓库。`reflections.json` 由 GitHub Actions 每天从 TeraCloud WebDAV 拉取当月反思、自动构建并提交。**全程云端，不依赖本地电脑开机。**

## 工作原理

```
OpenClaw 每天 07:05 生成反思 → TeraCloud WebDAV (-A Daily/当月/)
                                        │
                          GitHub Actions（每天 09:00 / 手动）
                                        │ 拉当月 md → build → commit
                                        ▼
                              本仓库 reflections.json
                                        │ jsDelivr CDN
                                        ▼
                                     小程序
```

## 仓库结构

```
├── .github/workflows/sync.yml   定时同步流程
├── scripts/
│   ├── fetch_webdav.py          从 WebDAV 拉当月 md（纯标准库）
│   └── build_reflection_app.py  扫 md → 生成 json/html（需从本地复制，见下）
├── reflections.json             Actions 产物（小程序拉这个）
└── index.html                   Actions 产物（网页版）
```

## 首次部署（一次性）

### 1. 把 build 脚本放进来
`scripts/build_reflection_app.py` 不在本仓库，需从本地复制（已加 `--workspace` 参数的版本）：

```powershell
# 在本地（D:\action\-A Daily\2026-7\）clone 本仓库后，或直接网页上传
Copy-Item "D:\action\-A Daily\2026-7\build_reflection_app.py" "<本仓库本地路径>\scripts\build_reflection_app.py"
```

或直接在 GitHub 网页 `Add file` 把本地的 `build_reflection_app.py` 上传到 `scripts/`。

### 2. 配置 GitHub Secrets
仓库 `Settings → Secrets and variables → Actions → New repository secret`，加两个：
- `WEBDAV_USER`：TeraCloud 用户名
- `WEBDAV_PASSWORD`：TeraCloud WebDAV 密码（应用密码）

> InfiniCloud 凭据轮换后，只需更新 `WEBDAV_PASSWORD` 这一个 Secret。

### 3. 手动触发一次验证
`Actions → sync-reflections → Run workflow`。跑完看 Summary 的条数和 updatedAt，以及 `reflections.json` 是否更新。

## 日常
- 每天 09:00 自动跑；Actions 页可随时手动 `Run workflow`
- 小程序下拉刷新即可看到新内容（jsDelivr 有几分钟缓存延迟）

## 注意
- **仅同步当月**：每月 1 号 `reflections.json` 会重置成只有当月数据，上月从小程序消失。若想保留历史，改 `fetch_webdav.py` 的 `--month-dir` 或扫描范围（参见对话里的"扫描范围"选项）。
- 反思 **md 不进本仓库**（隐私），只在 Actions 内存里临时处理；build 的 glob 只挑 `*-每日反思*` 和 `*-reflection`，其他笔记不会公开。
