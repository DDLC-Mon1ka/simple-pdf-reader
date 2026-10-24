# 简易 PDF 阅读器

[English README](README.md)

一个面向 Windows 的轻量、离线优先 PDF 阅读器，使用 Python、Tkinter 和
PDFium 构建。

![应用预览](docs/preview.png)

## 主要功能

- 单页、双页并排和多页连续阅读
- 缩略图、PDF 目录、全文搜索和搜索命中列表
- 拖入 PDF、最近文件、自动续读和阅读进度
- 阅读历史、用户书签、文字选择和复制
- 多色高亮、便签、批注管理和摘录导出
- 将高亮、便签和书签导出为原生批注 PDF；加密文档的副本仍受密码保护
- 夜间护眼模式和页面旋转
- 本地摘要：关键词和核心句子
- 可选 OpenAI-compatible API：AI 摘要、文档问答和页码引用
- API Key 使用 Windows DPAPI 加密保存

## 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

## 构建 EXE

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

输出位于 `dist\PDFReader.exe`。

## API 与隐私

普通阅读、搜索、批注和本地摘要不会上传文件，也不会收集遥测数据。

只有主动使用 AI 摘要或问答时，相关文字片段才会发送到用户自行配置的 API
地址。API Key 使用 Windows 用户级 DPAPI 加密，不写入仓库。

## 项目限制

- 当前仅支持 Windows
- 不含 OCR，扫描版 PDF 需要先添加文字层
- 文字选择目前是单页矩形框选
- 批注保存在独立数据文件中，不修改原 PDF；可通过“导出带批注 PDF”生成可分享副本
- 当前为单文件原型，后续可拆分 PDF、API、状态和 UI 模块

完整功能、快捷键和贡献方式见 [英文 README](README.md)。

## 许可证

应用代码使用 [MIT License](LICENSE)。第三方组件许可见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
