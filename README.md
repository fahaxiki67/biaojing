# 标镜

标镜是本地招投标资料工作台，支持 PDF、DOCX、XLSX、目录和 ZIP 导入，提供解析覆盖率、原页证据、候选字段确认和规则筛查。扫描 PDF 使用本机 Tesseract OCR。筛查结果是人工核查线索。

## Mac 安装与启动

当前交付为源码运行版，需要 Python 3.10 或更高版本。本轮在 Apple Silicon Mac、Python 3.14.7 上实际运行；Windows 已加入安装脚本和 GitHub 自动化回归，仍需在真实 Windows 电脑上做最终安装验收。

1. 将整个项目目录放在可写位置，例如「文稿」。保留 `app`、`samples`、`requirements.txt` 和启动脚本之间的相对位置。
2. 首次使用，双击 `install_macos.command`。脚本会在项目内创建独立的 `.venv`，按 `requirements.txt` 安装依赖。首次安装需要联网下载依赖，不上传项目资料。
3. 安装完成后，双击 `start_macos.command`。浏览器会打开本地工作台，终端显示本次地址和工作区位置。端口自动选择，无需记住固定端口。
4. 选择多个文件或整个目录导入。先查看文件状态和原因，再确认字段和运行筛查。
5. 退出时，在启动终端按 Control+C。工作区保留，可下次继续使用。同一工作区只允许一个服务进程打开。

如果系统不允许直接执行脚本，可在终端进入项目目录后运行：

```sh
zsh install_macos.command
zsh start_macos.command
```

如果没有 Python 3.10+，安装 Python 后重新运行安装脚本。已有多个 Python 时，可用 `BIAOJING_PYTHON` 指定安装环境所用解释器。脚本不会替换系统 Python。

## Windows 安装与启动

安装 Python 3.10 或更高版本后，在项目目录的 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
```

两个脚本只在项目目录创建独立的 `.venv`，不会修改系统 Python。扫描件 OCR 还需安装含 `chi_sim` 中文模型的 Tesseract；若未加入 PATH，可将 `BIAOJING_TESSERACT` 设为可执行文件完整路径。Windows 安装流程已覆盖自动化测试，真实机器上的安装路径、权限和 OCR 环境仍需复验。

## 作者与 GitHub 自动更新

工作台右上角“关于”显示作者刘奇和当前版本。源码版通过[标镜 GitHub 仓库](https://github.com/fahaxiki67/biaojing)的 Releases 更新：程序启动时检查新版本、下载更新包并校验 GitHub 提供的 SHA-256；只替换 `app/biaojing` 程序代码，资料工作区、原始文件和确认记录保留。程序运行期间可在“关于”中手动检查；下载的版本会在下次启动时自动安装。离线或 GitHub 暂不可用时继续启动当前版本。

发布新版本时，将 `app/biaojing/__init__.py` 中的 `VERSION`、Git 标签与 `.github/release-notes/` 下的中文版本说明保持一致。推送 `v*` 标签后，GitHub Actions 会先在 Linux、Mac 和 Windows 上运行检查；全部通过后才发布 Release 并附上更新包。更新器会校验 SHA-256，安装前编译、导入关键模块，并把现有工作区数据库只读备份到临时目录，在副本上试迁移和执行 SQLite 完整性检查；失败时恢复旧程序、保留原数据库并记下失败版本。更新器面向源码运行版，只替换程序代码，不会自动安装新依赖，也不是完整安装器。

## 扫描件 OCR（PDF 与 Word 图片）

扫描件还需要本机 Tesseract 和 `chi_sim` 中文模型。已经使用 Homebrew 的 Mac 可安装 `tesseract` 与 `tesseract-lang`，安装后执行严格检查：

```sh
.venv/bin/python launch.py --check --require-ocr
```

若 OCR 缺失，普通环境检查会明确提示，文本 PDF、DOCX 和 XLSX 仍可使用。扫描页和 Word 图片会保留为待 OCR，不伪装为已识别。程序查找 PATH、Mac 常用 Homebrew 安装位置及 Windows 常见安装目录；自定义安装位置可用 `BIAOJING_TESSERACT` 指定可执行文件的完整路径。

OCR 不设整份文档页数或总时长限制；单页按 200 DPI 渲染，最多 2,500 万像素，两次识别共用 15 秒时限。自动版面模式为空时，补试一次稀疏文字模式；稀疏补试不足 20 个字符时仍待 OCR。20 字符仅是阻止少量噪声被当成完成的保守门槛，不是准确率评价。补试记录、失败页码与原因会显示在来源表中。

有文字输出不代表数字、手写内容或表格结构准确。当前尚未实现扫描表格的自动行列还原。PDF 以原生文字优先；页面文字很少且含大幅图像时会自动补做 OCR。复杂混排页仍需对照原页核实。

DOCX 正文、表格、页眉和页脚中的内嵌 PNG、JPEG、GIF、BMP 图片也会尝试本机离线 OCR，证据保留段落或表格位置。每个图片位置显示 OCR 状态；识别文本和候选字段必须对照原图人工复核。单张图片上限为 25 MB、2,500 万像素；每份 Word 最多处理 100 MB 图片数据，每个不同图片资源最多用时 15 秒。超限、格式不支持、关系损坏或 OCR 未完成时，证据保留为待 OCR。含图片的 Word 在后台处理，可查看进度并取消；重复出现的同一图片资源只识别一次，但每个位置都保留证据。当前定向重试入口仅支持 PDF，Word 待 OCR 图片需后续增加重试入口。

## 工作区与原件

通过启动脚本启动时，默认工作区为 `~/Documents/标镜工作区`，与源码、虚拟环境分开。可以指定独立的试验目录：

```sh
.venv/bin/python launch.py --workspace "/绝对路径/标镜试验工作区"
```

更换默认目录不会自动迁移旧工作区。继续使用已有工作区时，应显式指定它的路径。备份时，先停止服务，再复制完整工作区目录。

上传文件按 SHA-256 保存，来源引用另行保留。重复字节会记录为重复，不会自动重新识别。PDF 的来源行出现“重试待 OCR”按钮时，可只重试尚未识别的页面；原始文件和已有确认记录不变。来源行显示最近使用的解析版本，证据详情显示该页版本；旧工作区中未记录版本的页面会标记为“legacy”，重试后的页面单独更新版本。

当前单文件上传上限为 128 MB。XLSX 以双只读流读取公式和缓存值，单文件最多扫描 50,000 行、2,000,000 个单元格并输出 20,000 条证据；触顶会明确标为 `partial`。来源记录和候选列表按页加载，候选单页最多 200 条。三页及以上的直接 PDF 上传和所有 PDF 待 OCR 重试会在后台逐页运行，页面显示当前页并可取消；取消在当前页处理结束后生效，已完成页保留，未处理页明确标为待处理，之后可重试。同一工作台一次只运行一个长任务。少于三页的 PDF 上传和 ZIP（包括 ZIP 内 PDF）仍同步处理。不要把“已处理”理解为“全部识别成功”，应查看每份文件的 `partial`、待 OCR 页数和原因。

确认、更正、标记 unknown 和文件绑定会保留操作历史。每次筛查保存事实快照及 SHA-256，方便回看当时参与规则的输入。下载原件前会重新校验 SHA-256；异常线索仍需人工对照原件复核。

## 开发检查

```sh
.venv/bin/python launch.py --check
cd app
../.venv/bin/python -W error::ResourceWarning -m unittest discover -s biaojing/tests -t .
```

GitHub Actions 对 Linux、Mac 和 Windows 运行同一套自动化回归。扫描件 OCR 测试在未安装 Tesseract 的环境会跳过；跳过不代表 OCR 通过。

测试使用合成资料。页面行为测试需要 Node；缺少 Node 或 OCR 环境时，相关测试会显示跳过，验收应检查跳过情况。测试样本路径已改为相对项目位置。

GitHub 仓库仅发布程序源码、安装与启动脚本、依赖清单及合成测试样本。项目资料、审计报告、参考代码副本和本机运行数据不属于公开发布内容。仓库尚未声明开源许可证；公开可见不代表已授予他人复制、修改或分发许可。
