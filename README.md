# 校园网助手 1.1.1

一个面向 Windows 的校园网自动重连工具，使用 tkinter 提供 GUI，并通过 Playwright 调用 Microsoft Edge 完成 Portal 认证。

当前页面识别逻辑适配 `auth1.ysu.edu.cn`。如用于其他学校，需要根据实际认证页面调整域名、登录控件和网络服务名称。

## 功能

- 实时显示监控状态和 Internet 状态。
- 编辑学号并在校园网、中国电信、中国移动、中国联通之间单选。
- 自定义检测间隔、失败重试间隔和连续恢复失败上限。
- Microsoft HTTP 与 Apple HTTPS 双站点探测；连续三轮失败后才启动恢复，降低短暂网络抖动造成的误报。
- Playwright 使用独立 Edge Profile，调用 Edge 已保存密码；源码、配置和日志均不保存密码。
- GUI 可切换“隐藏正常检测”和“显示全部日志”。过滤只影响窗口，`campus_watchdog.log` 始终保留完整记录。
- 点击窗口 X 后隐藏到 Windows 托盘；可通过 GUI 或托盘菜单安全退出。

## 隐私与安全

仓库不包含任何真实学号、密码、Cookie、日志、配置文件、浏览器 Profile、EXE 或本机绝对路径。

程序运行时会在自身目录创建：

- `config.json`：学号、网络选择及监控参数。学号以明文保存。
- `campus_watchdog.log*`：完整运行日志。
- `edge_campus_profile/`：专用 Edge Profile，可能包含登录 Cookie 和浏览器保存密码。
- `campus_self_test.json`：可选自检报告，可能包含本机路径。

这些文件已加入 `.gitignore`，不要手工提交或分享。

程序只检查密码框是否有内容，不读取、记录或导出密码值。自动填充仍受 Edge 密码管理器、Windows 验证、验证码及认证页面变化影响。

## 运行源码

要求：Windows、Python 3.10 或更高版本、Microsoft Edge。

```powershell
python -m pip install -r requirements.txt
python campus_net_watchdog_gui.py
```

首次运行后填写学号并选择网络。如果专用 Profile 尚未保存学校密码，先停止监控，点击“配置已保存密码”，在打开的 Edge 中手工登录并保存密码。

配置和 Profile 与脚本放在同一目录。如从旧版升级，请保留原来的 `config.json` 和 `edge_campus_profile/`。

## 日志过滤

默认显示全部 GUI 日志。点击“隐藏正常检测”后，窗口仅隐藏 INFO 级别的周期性“Internet 正常/连接正常”记录，掉线、认证、服务选择、恢复结果、警告和异常继续显示。

切换过滤状态会重新渲染本次运行最近约 3000 条 GUI 历史。文件日志不受过滤影响。

## 可选参数

```text
--setup        打开专用 Edge，手工配置保存密码
--no-autostart 只打开界面，不自动启动监控
--once         检查或恢复一次后保留窗口
--force        强制打开一次 Portal
--debug        失败时保留 Edge 120 秒，可取消
--self-test    使用临时 Profile 和本地页面检查运行依赖
```

## 测试建议

1. 启动监控，确认状态显示 Internet 正常，GUI 与 `campus_watchdog.log` 都有日志。
2. 点击“隐藏正常检测”，确认正常心跳从 GUI 消失，但文件日志仍有记录；再点击“显示全部日志”，确认历史恢复。
3. 在电脑旁让校园网认证失效，但保持网卡连接，确认程序依次记录掉线、恢复开始、认证页面、网络选择、确认和恢复结果。
4. 点击 X，确认窗口隐藏而监控继续；从托盘恢复后使用“退出程序”或“关闭脚本”安全退出。

不要在远程会话中主动制造掉线，也不要通过输入错误密码测试失败上限，以免远程失联或触发账户锁定。

## 打包 EXE

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --collect-all playwright --name "校园网助手1.1.1" campus_net_watchdog_gui.py
```

生成的 EXE 未进行数字签名。发布二进制文件时应提供 SHA-256，并明确标注构建来源。

## 验证范围

1.1.1 版本在隔离配置、临时 Edge Profile 和本地模拟页面上通过了配置、网络探测、失败保护、日志过滤、历史重绘、托盘、停止与退出等自动化检查。真实校园网、SSO 过期和 Edge 密码自动填充仍需使用者在本机验证。
