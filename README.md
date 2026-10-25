# 栖语 Qiyu

> 本地优先的 Windows AI 陪伴桌面应用。一个 PyWebView 窗口 + 一个只监听 `127.0.0.1` 的 FastAPI 后端；角色、记忆、知识库和聊天记录默认都留在本机。

**版本：v1.0.0**  
**形态：Windows x64 桌面应用 / 可选安装包**  
**项目主页：** <https://github.com/lmq9622/qiyu-desktop>

---

## 1. 这是什么

栖语 Qiyu 不是云端聊天站点，也不是“角色扮演 API 包装器”。它把陪伴型对话拆成几层可以长期维护的东西：

- **角色层**：姓名、年龄、职业、性格、缺点、关系、说话方式、角色参数
- **记忆层**：短期事实、长期记忆、当天大纲、全天总结、关系状态
- **知识层**：本地文档/RAG 检索，用户自己导入资料
- **行为层**：主动消息、消息流水线、情绪状态、冷却与睡眠窗口
- **界面层**：原生 HTML/CSS/JS + PyWebView，无浏览器插件依赖
- **外部通讯层**：企业微信、QQ、飞书、ClawBot/第三方扫码通道（可选，自行配置凭证）

当前版本**出厂不带任何角色**。首次启动是空状态，需要到「角色工坊」创建角色；角色人设可以用 AI 生成，也可以手写。

## 2. 主要功能

### 角色工坊

- 新建、编辑、删除角色
- MBTI / 性格标签 / 关系定位 / 预填参数
- AI 生成结构化角色简历，再整理成自然人格描述
- 内置 24 枚黑白线稿头像（含 12 枚女性向造型）
- 支持上传自定义头像

### 聊天

- 单角色、多角色切换
- 流式回复、多气泡拆分
- 角色卡、记忆面板、关系状态
- 不展示思考过程；不把“思考 / 内部推理”渲染到聊天区
- 支持手动选择用户身份（会话）

### 记忆与知识

- 短期事实、长期记忆、当天大纲、全天总结
- 自动总结器去重，不重复堆叠每天的大纲
- 本地知识库 / RAG 文档索引与检索
- 记忆面板支持搜索、删除、导出
- 记忆默认保存在本机

### 设置

- LLM / 路由模型设置
- 知识库索引
- 外部通讯通道
- 外观主题、头像、用户向设置
- 外部通讯长协议：进入配置页自动弹出，必须滚动到底部才能确认
- 使用说明 / 故障排查入口

### 外部通讯（可选）

已接入：

- ClawBot / 第三方扫码通道
- 企业微信
- QQ 开放平台
- 飞书开放平台

**重要：** 没有真实企业微信/QQ/飞书账号和凭证时，通道不会假装“已打通”。仓库里的合同测试使用本地 mock 官方端点验证 token 与发送 payload；真实上线需要你在平台后台申请凭证、完成审核/备案并做 live selftest。

`wechatauto` 本机窗口接管已移除，不再作为可选通道。

## 3. 从源码运行

### 环境

- Windows 10/11 x64
- Python 3.11+（开发环境实测 Python 3.13）
- 一个 OpenAI 兼容的本地或远程 LLM 端点
- 可选：本地模型中转、Embedding 模型、TTS/STT 模型

### 安装依赖

```bash
python -m pip install -r requirements.txt
```

### 启动开发服务

```bash
python demo.py
```

打开 <http://127.0.0.1:8765/>。

### 启动桌面窗口

```bash
python client.py
```

`client.py` 会拉起本地后端并创建 PyWebView 窗口；如果只想调试前端，用 `demo.py` + 浏览器即可。

### 配置

- 环境变量模板：`.env.example`
- 运行时配置：`config/settings.yaml`（本地文件，不进 Git）
- 数据目录：默认 `~/.ai_companion/data`，可用 `QIYU_DATA_DIR` 覆盖
- API Key、Token、Secret 只填在本地，不要提交到仓库

## 4. 打包与安装包

### PyInstaller 目录包

```bash
python build.py
```

输出：

```text
dist/Qiyu/Qiyu.exe
```

同时生成 sidecar：

```text
dist/Qiyu/runtime/
dist/Qiyu/backends/
dist/Qiyu/models/
dist/Qiyu/assets/
dist/Qiyu/config/
dist/Qiyu/tools/
```

### Windows 安装包

仓库提供 Inno Setup 脚本：

```text
installer/Qiyu.iss
```

使用 Inno Setup 6 的 `ISCC.exe` 编译：

```powershell
& "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\Qiyu.iss
```

输出：

```text
dist/installer/Qiyu-Setup-1.0.0.exe
```

安装包只包含当前 `dist/Qiyu` 构建，不包含测试数据、备份、旧版本 spec 或模型训练产物。

## 5. 数据与隐私

- 默认本地运行，后端只绑定 `127.0.0.1`
- 聊天记录、角色、记忆和日志默认写入 `~/.ai_companion/data`
- 使用外部 LLM 时，对话内容会发送到你配置的 LLM 端点
- 使用外部通讯通道时，消息会发送到对应平台；请遵守平台规则并阅读设置页的长版用户协议/免责声明
- 本项目不提供“绝对无法律风险”的保证；商用、AIGC、自动化营销或大规模消息发送前，请自行完成资质、备案、授权和律师审阅

## 6. 测试

```bash
python -X utf8 -m unittest discover -s tests -p "test_*.py"
```

当前基线：**195 个测试通过**。

覆盖范围包括：

- 角色 CRUD / 角色提示词
- 聊天流式与重复消息防护
- 记忆、总结、关系
- 外部通道 token / 发送 payload / 错误处理（本地 mock）
- 静态资源、头像、配置

## 7. 项目结构

```text
client.py              PyWebView 桌面入口
demo.py                FastAPI 主后端
companion/             LLM、角色提示、行为、关系、调度
gateway/               Web UI 与路由
memory/                记忆/大纲/摘要
rag/                   本地知识库
channels/              外部通讯通道
characters/            角色加载与出厂空角色
runtime/               运行时 Provider / 模型 / 设备
installer/             Inno Setup 安装包脚本
assets/                图标与品牌资源
docs/                  项目简介、架构、交接、交付日志、使用说明
tests/                 单元与合同测试
```

更细的说明见：

- [docs/项目简介.md](docs/项目简介.md)
- [docs/架构说明.md](docs/架构说明.md)
- [docs/交接文档.md](docs/交接文档.md)
- [docs/交付日志.md](docs/交付日志.md)
- [docs/使用说明.md](docs/使用说明.md)
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

## 8. 开源协议

本项目采用双许可：

- 社区版：**AGPL-3.0-only**
- 商业闭源/商用授权：见 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)

贡献前请阅读 [CLA.md](CLA.md)。第三方组件与头像来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 9. 免责声明

本项目按“现状”提供，不保证第三方平台接口永久可用、不保证任何账号不被限制或封禁，也不构成法律、医疗、金融或税务意见。外部通讯功能的用户协议、平台规则、账号/凭证责任与风险告知见应用内「设置 → 外部通讯」长协议。
