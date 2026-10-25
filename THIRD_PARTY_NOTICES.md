# 第三方组件与资源说明 / Third-Party Notices

栖语 Qiyu 本体采用 AGPL-3.0-only + 商业许可双许可。下列第三方组件/资源按其各自许可证使用。

## 运行时与开发依赖

项目依赖的 Python 包（以 `requirements.txt` 为准）包括但不限于：

- FastAPI / Starlette / Uvicorn / Pydantic（MIT / BSD 系）
- httpx / httpcore（BSD）
- PyWebView（BSD）
- Pillow（HPND/PIL license）
- NumPy（BSD）
- loguru（MIT）
- PyYAML（MIT）
- qrcode（BSD）
- python-dotenv（BSD）
- psutil（BSD）
- PyInstaller（GPL with exception；仅用于打包，不改变项目源代码许可）

实际分发时请以各包随附的 LICENSE 文件为准；`requirements.txt` 锁定的版本可能随发布更新。

## 内置头像

`gateway/static/avatars/a01.svg` – `a24.svg` 使用 **Notionists** 风格头像：

- 原始风格：Notionists by Zoish
- 来源：<https://heyzoish.gumroad.com/l/notionists>
- 许可：CC0 1.0 Universal（public domain dedication）
- 生成/整理方式：通过 DiceBear Notionists 兼容生成器导出为本地 SVG，按项目需要挑选与命名

头像 SVG 文件内的 `<metadata>` 保留了原始来源和 CC0 声明。项目不要求在运行时访问 DiceBear 或任何外部头像 API。

## 图标与品牌资源

`assets/icon.ico`、`assets/icon.png`、`assets/logo.png`、`launcher/assets/icon.*`、`gateway/static/logo.png` 为栖语 Qiyu 项目自制品牌图标，随项目发布；不使用第三方商标。

## 外部平台

企业微信、QQ、飞书、微信、ClawBot 是其各自权利人的商标/服务。本项目不是这些平台的官方产品，不包含其官方 SDK 的私有代码；接入使用平台公开接口或用户自行提供的第三方通道。平台条款见应用内「外部通讯」长协议。

## 字体

界面使用系统字体栈（Segoe UI / PingFang SC / Microsoft YaHei 等），不随包分发商业字体。

## 免责

本文件不是法律意见。分发前请根据实际 `requirements.txt`、打包内容和目标司法辖区复核所有许可证义务。
