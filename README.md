# Aisland

Aisland 是一个使用 Python 和 PySide6 开发的 Windows 桌面灵动岛，将时间、天气、音乐、通知和常用工具放在屏幕边缘。

## 功能

- 时间、天气、多城市天气与预报。
- 系统媒体控制、歌词显示、音量和亮度调节。
- 剪贴板、快捷启动及桌面通知辅助。
- 文本翻译、实时字幕、日语查词、生词本与 Anki 同步。

部分功能依赖本机应用、Windows 权限或额外模型，并非安装后全部自动可用。

## 运行环境

- Windows 10 / 11，建议使用 64 位环境。
- Python 3.10 或更新版本。
- 首次安装依赖、获取天气及使用在线功能时需要联网。

## 开始使用

1. 下载并解压源码，或使用 Git 克隆仓库。
2. 双击 `setup.bat`，按提示安装依赖。
3. 双击 `aisland.vbs` 启动。也可以在项目目录运行：

   ```powershell
   .\.venv\Scripts\python.exe run.pyw
   ```

首次启动会生成本地 `config.json`。可以在应用设置中调整配置；示例字段见 `config.example.json`。

默认快捷键：

| 快捷键 | 功能 |
| --- | --- |
| Ctrl + Alt + I | 显示或隐藏灵动岛 |
| Ctrl + Alt + J | 翻译 |
| Ctrl + Alt + V | 剪贴板 |

## 天气

公开源码版默认使用 [Open-Meteo](https://open-meteo.com/)，无需注册或填写 API Key，城市定位也使用其公开接口。可在应用中添加城市，默认每 15 分钟刷新。

天气数据来源：Open-Meteo，按 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 署名使用。其免费托管接口适用于非商业用途，并有调用次数限制；商业使用请先查看 [Open-Meteo 服务条款](https://open-meteo.com/en/terms)。

## 翻译和实时字幕

仓库不附带在线翻译的大模型 API Key。需要在线翻译时，请在应用中配置自己的服务地址、模型和 Key；可能产生的调用费用由所选服务提供方决定。

实时字幕的语音识别还需要下载本地模型，在项目目录运行：

```powershell
.\.venv\Scripts\python.exe scripts\download_models.py
```

模型体积较大，下载时间取决于网络情况。模型文件不存放在源码仓库中；使用前请查看相应模型的许可说明。

## 开发和打包

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

生成 Windows 安装包需要安装 `requirements-build.txt` 中的依赖、准备字幕模型，并安装 Inno Setup。可以通过 `-Compiler` 指定 `ISCC.exe` 的位置：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1 -Compiler 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' -FullOnly
```

公开版构建流程禁止嵌入个人翻译凭据。`config.json`、数据目录、模型、虚拟环境、日志、安装包和私有默认配置均由 `.gitignore` 排除。

`tests/fixtures/imap_self_signed.pem` 是用于测试 IMAP TLS 校验的公开自签名证书和测试私钥，不用于真实账户或生产环境。

## 许可与致谢

项目源码采用 [MIT License](LICENSE)。实时字幕相关代码保留了 [YishengTranslator 的许可声明](aisland/live/LICENSE-YishengTranslator.txt)。PySide6、Mutagen、语音模型等第三方组件分别适用其自身许可；打包分发时请保留所需声明。
