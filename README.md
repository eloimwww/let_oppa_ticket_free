# interpark_auto_machine

面向 NOL / Interpark、Melon Ticket、Ticketlink、YES24 和 Fantopia 的浏览器及桌面点击脚本集合。各平台脚本独立运行，根目录的 `main.py` 仅打印欢迎消息，没有统一调度功能。

目前itp通；melon更新停留在26.1月，选座会触发try again；yes24更新停留在26.3，太频繁会触发code6.

## 项目结构

```text
interpark_auto_machine/
├── main.py
├── README.md
├── nol_world/
│   ├── itp.py
│   ├── itp_v1.py
│   └── itp_v2.py  ——最新
├── melon/
│   ├── melon_ticket.py
│   ├── melon_see.py    ——maybe最新
│   ├── melon_ticket_improved.py    ——maybe最新
│   └── melon_ticket_improved_v1.py ——maybe最新
├── yes24/
│   └── yes24_ticket.py ——最新
└──fantopia/
    └── fanto.py
```

## 脚本概览

| 脚本 | 浏览器或桌面连接方式 | 当前入口执行到哪里 |
| --- | --- | --- |
| [main.py](main.py) | 无 | 打印欢迎消息 |
| [nol_world/itp.py](nol_world/itp.py) | 购票入口 → 日期和场次 → 区域选座并提交；验证码、票数、结账调用已注释 |
| [nol_world/itp_v1.py](nol_world/itp_v1.py) | 日期和场次 → 验证码识别及提交 → 区域选座 → 增加票数 → 填写信息并点击结账 |
| [nol_world/itp_v2.py](nol_world/itp_v2.py) | 验证码填入一次 → 人工纠错及提交 → 区域选座 → 填写手机号、勾选条款并点击 Checkout |
| [melon/melon_ticket.py](melon/melon_ticket.py) | 日期 → Get ticket → 验证码 → 分区选座 → Next → 支付信息及最终支付按钮 |
| [melon/melon_see.py](melon/melon_see.py) | 每个进程尝试日期、购票、验证码、选座及支付流程；未实现目标页面导航 |
| [yes24/yes24_ticket.py](yes24/yes24_ticket.py) | Booking → 日期和场次 → 选座 → 两次 Next → 票数 → 信用卡及付款按钮；部分选择器尚未填写 |

## 运行前准备

### Chrome 调试实例

以下是 Windows PowerShell 启动命令示例，Chrome 路径需与本机安装位置一致：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome_debug_profile_9222"
```

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9527 --user-data-dir="C:\chrome_debug_profile_9527"
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9526 --user-data-dir="C:\chrome_debug_profile_9526"
```

多数脚本直接使用写死的 `CHROMEDRIVER_PATH`，需要核对路径及驱动与浏览器的匹配情况。只有 NOL `itp_v2.py` 显式读取正在连接的调试浏览器版本、校验驱动主版本并按需通过 Selenium Manager 获取驱动；YES24 在配置路径不存在时使用 `Service()` 交由 Selenium 处理。

### 相应场次开票日期和个人信息

基本上每个脚本都需要提前填写

## 静态检查结果与排查入口

本次仅进行源码阅读和 `ast.parse()` 语法解析，没有导入业务脚本或启动浏览器。12 个 Python 脚本中，10 个通过语法解析，两个 Melon improved 脚本在第 321 行失败。语法通过不表示依赖齐全、页面选择器有效或购买流程成功。

| 现象或限制 | 先检查的位置 |
| --- | --- |
| improved 版启动即报 `SyntaxError` | 两个文件第 321 行未注释的中文文本 |
| 无法连接调试浏览器 | 对应脚本的端口、已启动的 Chrome 实例、`CHROMEDRIVER_PATH`；NOL v2 还检查 `/json/version` 与驱动获取日志 |
| 提示等待回车但直接开始操作 | NOL 三个版本、Melon 基础版、Fantopia 的启动提示没有对应 `input()` |
| 日期正确但场次未切换 | Melon 的有效日期函数未使用 `target_time` 参数 |
| 区域配置改变却仍扫原区域 | Melon 基础版及 `melon_see.py` 的有效循环写死了区域；NOL 的 `PREFERRED_SEATS` 未参与当前坐标筛选 |
| 日志提示跳过但仍结账 | NOL v1、Melon 基础版和部分多进程版本的实际函数调用仍启用 |
| YES24 在票数设置处失败 | `click_seat()` 中尚未填写的 XPath |
| Ticketlink 桌面版保存截图失败 | 写死的 `E:/Download/interpark_auto_machine_v5/ticketlink/` 路径 |
| 多进程任务结束后程序不退出 | Melon 多进程 worker 的 `finally` 无限等待及主进程 `join()` |

这些限制按当前源码记录，本次文档整理没有修改任何 Python 脚本。
