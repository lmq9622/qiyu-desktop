# 贡献指南 / Contributing

## 开发环境

```powershell
python -m pip install -r requirements.txt
python demo.py
```

前端文件在 `gateway/static/`；后端入口是 `demo.py`。桌面窗口用 `python client.py`。

## 提交前

1. 跑测试：

   ```powershell
   python -X utf8 -m unittest discover -s tests -p "test_*.py"
   ```

2. 不要提交：

   - `config/settings.yaml`、`.env`、API Key/Token
   - `data/`、`logs/`、`backups/`、`dist/`、`build/`
   - 模型权重、用户聊天记录、上传文件
   - 旧版本 spec、临时分析脚本、截图产物

3. 修改 UI 时同时检查桌面窗口和浏览器模式；修改通道时补 mock 合同测试。
4. 修改角色提示词时，至少跑一组不同题材的人设样本，确认没有明显 AI 模板腔。

## Pull Request

- 说明改了什么、为什么改、怎么验证
- 附测试命令和结果
- 涉及外部平台/隐私/安全时说明风险和边界
- 首次贡献请阅读并签署 [CLA.md](CLA.md)

## 许可证

贡献即表示你同意项目按 AGPL-3.0-only + 商业许可双许可分发，并同意 CLA 条款。
