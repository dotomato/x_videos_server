# X Videos Server

一个自托管工具，用于抓取 X (Twitter) 首页时间线中的视频，并通过带密码保护的 Web 界面进行浏览和下载。

## 功能

**时间线 & 书签 & 下载**
- 通过 X 的 GraphQL API（Cookie 认证）获取首页时间线和书签
- 时间线/书签页支持无限滚动分页加载
- 输入推文 URL 直接获取并下载视频（下载器页面）
- 以最高码率自动选择最佳视频质量下载
- 在浏览器中下载视频，实时显示进度（Server-Sent Events）

**视频管理**
- 按作者或时间浏览已下载的视频
- 视频评分系统（1–5 星），「喜欢」页按评分展示视频
- 首页显示总视频占用容量和磁盘可用空间
- 自动生成视频缩略图（OpenCV 提取第一帧）
- 支持在线播放和删除视频

**移动端适配**
- 响应式布局，适配手机屏幕
- 时间线/书签页在手机上以卡片形式展示，保留全部信息
- 手机端预览图点击全屏放大，桌面端悬停浮动预览

**安全**
- 基于 Session 的登录认证，所有页面均受密码保护
- 支持多用户，密码使用 bcrypt 加密存储
- 登录接口速率限制，防止暴力破解
- HTTP 安全响应头（CSP、X-Frame-Options 等）

## 项目结构

```
x_videos_server/
├── app.py              # Flask Web 服务器
├── x_timeline.py       # CLI 脚本，用于获取时间线/书签和下载视频
├── manage_users.py     # 用户管理工具
├── templates/          # HTML 模板
│   ├── base_timeline.html  # 时间线/书签共用基础模板
│   ├── index.html
│   ├── author.html
│   ├── play.html
│   ├── login.html
│   ├── timeline.html
│   ├── bookmarks.html
│   └── liked.html
├── static/
│   └── style.css
├── videos/             # 下载的视频（自动创建）
│   └── {用户名}/
│       ├── {tweet_id}_{序号}.mp4
│       └── {tweet_id}_{序号}.jpg  # 自动生成的缩略图
├── ratings.json        # 视频评分数据（不纳入版本控制）
└── users.json          # 用户凭证（不纳入版本控制）
```

## 环境要求

- Python 3.11+
- 依赖：`flask`、`httpx`、`bcrypt`、`opencv-python-headless`、`wcwidth`、`pytest`、`pytest-mock`

安装依赖：

```bash
pip install -r requirements.txt
```

## 配置

### X 账号凭证

凭证通过环境变量注入，不可硬编码：

```bash
# 复制示例文件并填入凭证
cp .env.example .env
# 编辑 .env，填入以下两个变量：
X_AUTH_TOKEN=你的_auth_token
X_CT0=你的_ct0
```

获取方式：
1. 在浏览器中打开 x.com 并登录
2. 打开开发者工具 → Application → Cookies → `https://x.com`
3. 复制 `auth_token`（→ `X_AUTH_TOKEN`）和 `ct0`（→ `X_CT0`）的值

本地运行时可用 `source .env` 加载，或在 systemd service 中配置 `EnvironmentFile=`。

### Web 应用用户

在启动 Web 服务器之前，先创建 `users.json`：

```bash
python3.11 manage_users.py add <用户名>
```

按提示输入密码，若文件不存在会自动创建。

## 测试

```bash
# 运行全部单元测试
python3.11 -m pytest tests/ -v

# 快速模式（精简输出）
python3.11 -m pytest tests/ -q
```

测试不依赖网络、X API 或真实视频文件，均使用 `monkeypatch`/mock 隔离。覆盖范围：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_x_timeline.py` | `extract_videos`、`_parse_instructions`、`parse_timeline`、`parse_bookmarks`、queryId 缓存、`make_headers`、`get_user_id`、`get_tweet_by_id` 等 |
| `tests/test_app.py` | 路径安全校验、路由 400/404/200、登录速率限制、开放重定向修复、HTTP 安全响应头、bcrypt 密码验证、OpenCV 缩略图生成、视频扫描与排序、SSE 下载进度流 |

## 使用方法

### CLI — 获取时间线

```bash
# 获取 5 条推文（默认）
python3.11 x_timeline.py

# 获取 N 条推文
python3.11 x_timeline.py 20

# 获取推文并交互式下载视频
python3.11 x_timeline.py --download

# 获取 N 条推文并下载视频
python3.11 x_timeline.py 20 --download
```

### Web 服务器

```bash
python3.11 app.py
```

启动后在浏览器中访问 `http://localhost:5000`，所有页面均需登录。

| 路由 | 说明 |
|---|---|
| `/` | 首页：最新视频 + 所有作者（含视频数和磁盘信息） |
| `/timeline` | 从 X 获取 20 条时间线推文，支持无限滚动加载和视频下载 |
| `/bookmarks` | 从 X 获取 20 条书签推文，支持无限滚动加载和视频下载 |
| `/liked` | 喜欢：按评分展示已评分的视频 |
| `/downloader` | 下载器：输入推文 URL 查看并下载视频 |
| `/author/<名称>` | 指定作者的所有视频 |
| `/play/<作者>/<文件>` | 视频播放页（含评分和删除） |
| `/user/<用户名>` | 指定 X 用户的时间线 |

### 用户管理

```bash
python3.11 manage_users.py list               # 列出所有用户
python3.11 manage_users.py add <用户名>       # 添加新用户
python3.11 manage_users.py passwd <用户名>    # 修改密码
python3.11 manage_users.py del <用户名>       # 删除用户
```

## 部署

### 使用部署脚本

```bash
# 仅部署（不提交，适合已提交的情况）
./deploy.sh

# 提交当前变更并部署
./deploy.sh "提交说明"
```

脚本会依次执行：
1. （可选）`git add -A && git commit`
2. **本地运行 `pytest tests/`** — 测试不通过则立即中止，不推送
3. `git push origin main`
4. 服务器上 `git pull origin main`
5. `sudo systemctl restart x_videos_server`
6. 检查服务状态，异常时打印最近 20 行日志

### 手动部署

```bash
# 本地推送
git push origin main

# 登录服务器后执行
cd ~/x_videos_server
git pull origin main
sudo systemctl restart x_videos_server
sudo systemctl is-active x_videos_server
```

## 注意事项

- `users.json`、`ratings.json` 和 `videos/` 目录已通过 `.gitignore` 排除在版本控制之外
- X GraphQL 的 `queryId` 缓存于 `.query_id_cache.json`，当 API 返回 400/403 时会自动刷新；书签和时间线各自独立缓存
- 缩略图在首次访问时生成，并与视频文件存放在同一目录
