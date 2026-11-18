# interpark自动点击购票（通过设置cx和cy进行定位）
import cv2
import numpy as np
import pytesseract
from PIL import Image
import io
import base64
import logging
import warnings
import json
import re
import subprocess
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
# 必须在所有 selenium 相关导入之前
logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('selenium').setLevel(logging.WARNING)
warnings.filterwarnings('ignore')
from selenium.webdriver.remote.webelement import WebElement
from typing import Optional
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
)
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.selenium_manager import SeleniumManager
import time
import threading
import queue
# ========== 配置区域 ==========
class Config:
    """配置类 - 集中管理所有配置参数"""

    # Seat coordinate ranges (inclusive).
    SEAT_CX_MIN = 3
    SEAT_CX_MAX = 302
    SEAT_CY_MIN = 3
    SEAT_CY_MAX = 260

    # 浏览器配置
    CHROME_DEBUG_PORT = "127.0.0.1:"
    CHROMEDRIVER_PATH = r"chromedriver.exe"

    # 抢票配置
    TARGET_DATE = "28"
    TARGET_TIME = "5:00"
    TICKET_COUNT = 1
    PHONE_NUMBER = ""  # 手机号

    # 优先座位 (可选，留空则不指定)
    PREFERRED_SEATS = []  # 例如: ['seat-A1', 'seat-A2']

    # 超时配置
    BUY_NOW_TIMEOUT = 300
    DATE_SELECT_TIMEOUT = 300
    TIME_SELECT_TIMEOUT = 300
    CAPTCHA_TIMEOUT = 300
    SEAT_SELECT_TIMEOUT = 1800
    TICKET_COUNT_TIMEOUT = 30
    CHECKOUT_TIMEOUT = 30

# ========== 日志配置 ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ========== WebDriver 初始化 ==========
def get_debug_browser_version():
    """以正在连接的调试浏览器为准，不猜测系统里其他 Chrome 的版本。"""
    url = f"http://{Config.CHROME_DEBUG_PORT}/json/version"
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
            info = json.load(response)
    except Exception as exc:
        raise RuntimeError(
            f"无法访问 Chrome 调试端口 {Config.CHROME_DEBUG_PORT}，请先启动调试窗口"
        ) from exc
    match = re.search(r"(?:Chrome|Chromium|HeadlessChrome)/(\d+(?:\.\d+){3})",
                      info.get("Browser", ""))
    if not match:
        raise RuntimeError(f"无法识别调试浏览器版本：{info.get('Browser')}")
    return match.group(1)


def get_chromedriver_version(path):
    """只执行 --version 检查驱动，不启动浏览器会话。"""
    result = subprocess.run(
        [str(path), "--version"], capture_output=True, text=True,
        timeout=5, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    match = re.search(r"ChromeDriver (\d+(?:\.\d+){3})", result.stdout)
    if not match:
        raise RuntimeError(f"无法识别 ChromeDriver 版本：{result.stdout.strip()}")
    return match.group(1)


def resolve_chromedriver(browser_version):
    major = browser_version.split(".")[0]
    configured = Path(Config.CHROMEDRIVER_PATH) if Config.CHROMEDRIVER_PATH else None
    if configured and configured.is_file():
        try:
            version = get_chromedriver_version(configured)
            if version.split(".")[0] == major:
                logger.info('使用已配置的 ChromeDriver %s', version)
                return str(configured)
            logger.warning('已配置的驱动 %s 与 Chrome %s 不匹配，自动获取匹配驱动',
                           version, browser_version)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            logger.warning('已配置的驱动不可用：%s，自动获取匹配驱动', exc)

    cache = Path(__file__).resolve().parent.parent / '.drivers'
    logger.info('按 Chrome %s 获取驱动，缓存目录：%s', browser_version, cache)
    result = SeleniumManager().binary_paths([
        '--browser', 'chrome', '--browser-version', browser_version,
        '--avoid-browser-download', '--skip-driver-in-path', '--avoid-stats',
        '--cache-path', str(cache), '--timeout', '45',
    ])
    path = Path(result['driver_path'])
    version = get_chromedriver_version(path)
    if version.split('.')[0] != major:
        raise RuntimeError(
            f'获取的 ChromeDriver {version} 与 Chrome {browser_version} 不匹配，停止连接')
    logger.info('使用 ChromeDriver %s：%s', version, path)
    return str(path)


def init_driver():
    """初始化并返回 WebDriver"""
    try:
        options = webdriver.ChromeOptions()
        options.debugger_address = Config.CHROME_DEBUG_PORT
        browser_version = get_debug_browser_version()
        logger.info('调试端口浏览器版本：Chrome %s', browser_version)
        service = Service(resolve_chromedriver(browser_version))
        driver = webdriver.Chrome(service=service, options=options, keep_alive=False)
        logger.info("✅ WebDriver 初始化成功")
        return driver
    except Exception as e:
        logger.error(f"❌ WebDriver 初始化失败: {e}")
        raise

# ========== 工具函数 ==========
def safe_click(driver, element, method="js"):
    """安全点击元素，支持多种点击方式"""
    try:
        if method == "js":
            driver.execute_script("arguments[0].click();", element)
        elif method == "native":
            element.click()
        elif method == "event":
            driver.execute_script(
                "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));",
                element,
            )
        logger.info(f"座位点击成功")
        return True
    except Exception as e:
        logger.warning(f"⚠️ 座位点击失败: {e}")
        return False

def wait_and_find(driver, by, value, timeout=10.0, condition="presence"):
    """通用等待并查找元素"""
    try:
        if condition == "clickable":
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, value))
            )
        elif condition == "visible":
            element = WebDriverWait(driver, timeout).until(
                EC.visibility_of_element_located((by, value))
            )
        else:  # presence
            element = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
        return element
    except TimeoutException:
        logger.warning(f"⚠️ 元素未找到: {value}")
        return None

# ========== step1 点击buy now按钮 ==========
class BuyNowButtonClickable:
    """按桌面购票区或移动端底栏定位主按钮，不依赖按钮文案。"""
    def __call__(self, driver):
        buttons = driver.execute_script(
            """
            return Array.from(document.querySelectorAll(
                'button.nds-e-rectangle-button.nds-e-rectangle-button--variant_filled_primary'
            )).filter(button => {
                if (button.closest('[role="dialog"], [aria-modal="true"]')) return false;
                const r = button.getBoundingClientRect(), s = getComputedStyle(button);
                if (!r.width || !r.height || s.display === 'none'
                        || s.visibility === 'hidden' || s.visibility === 'collapse') return false;
                if (button.closest('[class*="grid-area_purchase-button"]')) return true;
                for (let parent = button.parentElement; parent && parent !== document.body;
                        parent = parent.parentElement) {
                    const style = getComputedStyle(parent);
                    if (style.position === 'fixed' && style.bottom !== 'auto') return true;
                }
                return false;
            });
            """
        ) or []
        candidates = []
        for button in buttons:
            try:
                if (button.is_displayed() and button.is_enabled()
                        and button.get_attribute('aria-disabled') != 'true'):
                    candidates.append(button)
            except StaleElementReferenceException:
                continue
        if len(candidates) > 1:
            labels = [' '.join(button.text.split()) for button in candidates]
            raise RuntimeError(f'购票区域存在多个可用主按钮，未点击：{labels}')
        return candidates[0] if candidates else False

# ========== 选择日期和时间 ==========
class SpecificDateClickable:
    """自定义等待条件：指定日期按钮可点击"""
    def __init__(self, date_str):
        self.date_str = date_str

    def __call__(self, driver):
        try:
            buttons = driver.find_elements(
                By.CSS_SELECTOR, "button.EntCalendar_dateButton__6TxQi"
            )
            for b in buttons:
                if b.is_displayed() and not b.get_attribute("disabled"):
                    if self.date_str in b.text:
                        return b
        except StaleElementReferenceException:
            pass
        return False

class TimeButtonClickable:
    """自定义等待条件：时间按钮可点击"""
    def __init__(self, target_time):
        self.target_time = target_time

    def __call__(self, driver):
        try:
            time_buttons = driver.find_elements(
                By.XPATH,
                "//*[@id='__next']/div/div[1]/div/section[2]/div[1]/div/button",
            )
            for t in time_buttons:
                if t.is_displayed() and not t.get_attribute("disabled"):
                    if self.target_time is None or self.target_time in t.text:
                        return t
        except StaleElementReferenceException:
            pass
        return False

class CaptchaVisible:
    """自定义等待条件：验证码图片可见"""

    def __call__(self, driver):
        try:
            captcha_elem = driver.find_element(
                By.XPATH, "//*[@id='__next']/div[2]/div/div/div/div/div[1]/div[2]/img"
            )
            if captcha_elem.is_displayed():
                return captcha_elem
        except NoSuchElementException:
            pass
        return False

class SubmitButtonDisappeared:
    """自定义等待条件：提交按钮消失（验证成功）"""
    def __call__(self, driver):
        try:
            driver.find_element(By.XPATH, "//*[@id='__next']/div[2]/div/footer/button")
            return False
        except NoSuchElementException:
            return True

class AvailableSeatsPresent:
    """自定义等待条件：可选座位出现"""
    def __call__(self, driver):
        try:
            available_seats = driver.find_elements(
                By.CSS_SELECTOR,
                "circle.js-seat:not([class*='SeatMap_disabled'])",
            )
            if available_seats:
                return available_seats
        except StaleElementReferenceException:
            pass
        return False

# ========== 验证码识别类 ==========
class CaptchaSolver:
    """验证码识别器"""
    def __init__(self):
        self.tesseract_config = "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def solve(self, captcha_element: WebElement) -> Optional[str]:
        try:
            image = self._capture_element_image(captcha_element)
            processed_image = self._preprocess_image(image)
            text = self._perform_ocr(processed_image)
            # cleaned_text = self._clean_text(text)
            cleaned_text = text
            logger.info(f"🔍 验证码识别结果: {cleaned_text}")
            return cleaned_text
        except Exception as e:
            logger.error(f"❌ 验证码识别失败: {e}")
            return None

    def _capture_element_image(self, element: WebElement) -> np.ndarray:
        captcha_base64 = element.screenshot_as_base64
        image_data = base64.b64decode(captcha_base64)
        image = Image.open(io.BytesIO(image_data))
        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    def _preprocess_image(self, image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        denoised = cv2.medianBlur(gray, 3)
        _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = np.ones((2, 2), np.uint8)
        morph = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        return morph

    def _perform_ocr(self, image: np.ndarray) -> str:
        try:
            text = pytesseract.image_to_string(image, config=self.tesseract_config)
            return text.strip()
        except Exception as e:
            logger.error(f"❌ OCR 执行失败: {e}")
            return ""

    def _clean_text(self, text: str) -> str:
        cleaned = "".join(c for c in text if c.isalnum()).upper()
        common_mistakes = {"O": "0", "I": "1", "Z": "2", "S": "5", "G": "6", "B": "8"}
        for old, new in common_mistakes.items():
            cleaned = cleaned.replace(old, new)
        return cleaned

# ========== 座位监控类 ==========
class SeatMonitor:
    """座位监控器 - 负责自动抢座"""
    def __init__(self, driver, preferred_seats=None, auto_scroll=True):
        self.driver = driver
        self.seat_queue = queue.Queue(maxsize=1)
        self.running = True
        self.driver_lock = threading.Lock()
        self.seat_selected = False
        self.preferred_seats = preferred_seats or []
        self.auto_scroll = auto_scroll

        # 统计信息
        self.start_time = None
        self.end_time = None
        self.attempt_count = 0
        self.seat_update_count = 0

        # 🔥 优化连接池配置
        try:
            # 增加连接池大小
            self.driver.command_executor._conn.pool_maxsize = 50
            self.driver.command_executor._conn.pool_block = False
            # 设置超时
            self.driver.command_executor._timeout = 30
        except Exception as e:
            logger.warning(f"⚠️ 连接池配置失败: {e}")

    def format_duration(self, seconds):
        """格式化时间显示"""
        if seconds < 60:
            return f"{seconds:.2f}秒"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = seconds % 60
            return f"{minutes}分{secs:.2f}秒"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = seconds % 60
            return f"{hours}小时{minutes}分{secs:.2f}秒"

    def check_popup_exists(self, timeout=0.12):
        """检查错误弹窗 - 优化超时时间"""
        try:
            popup_btn = WebDriverWait(self.driver, timeout).until(
                EC.visibility_of_element_located(
                    (By.XPATH, "//*[@id='__next']/div[2]/div/div[2]/button")
                )
            )
            return popup_btn
        except TimeoutException:
            return None

    def check_submit_button_exists(self, timeout=2):
        """Find a visible, enabled Submit button without relying on DOM depth."""
        def find_submit(driver):
            for button in driver.find_elements(
                By.XPATH, "//button[normalize-space(.)='Submit']"
            ):
                try:
                    if (button.is_displayed() and button.is_enabled()
                            and button.get_attribute('aria-disabled') != 'true'):
                        return button
                except StaleElementReferenceException:
                    continue
            return False

        try:
            return WebDriverWait(
                self.driver, timeout, poll_frequency=0.1
            ).until(find_submit)
        except TimeoutException:
            return None

    def update_seats_continuously(self):
        """区域极速抢座线程"""
        logger.info("⚡ 区域极速抢座线程启动")
        self.running = True

        pending_seat = None
        while self.running and not self.seat_selected:
            try:
                with self.driver_lock:
                    url_before = self.driver.current_url

                    # 按坐标查找 SVG 座位，保留原有范围筛选和重试流程。
                    if pending_seat is None:
                        candidate = self.driver.execute_script(
                            """
                            const [cxMin, cxMax, cyMin, cyMax] = arguments;
                            const seats = document.querySelectorAll(
                                "circle.js-seat:not([class*='SeatMap_disabled'])"
                            );
                            for (const seat of seats) {
                                const cx = parseFloat(seat.getAttribute("cx"));
                                const cy = parseFloat(seat.getAttribute("cy"));
                                if (cx >= cxMin && cx <= cxMax && cy >= cyMin && cy <= cyMax
                                    && seat.getClientRects().length) {
                                    return [seat, cx, cy];
                                }
                            }
                            return null;
                            """,
                            Config.SEAT_CX_MIN, Config.SEAT_CX_MAX,
                            Config.SEAT_CY_MIN, Config.SEAT_CY_MAX,
                        )
                        if candidate is None:
                            logger.info("No seat found in the configured region")
                            time.sleep(0.5)
                            continue

                        seat, cx_clicked, cy_clicked = candidate
                        # 当前页面监听 pointerdown/pointerup；SVG 不使用 WebElement.click()。
                        clicked = self.driver.execute_script(
                            """
                            const el = arguments[0];
                            if (!el.isConnected) return false;
                            const box = el.getBoundingClientRect();
                            if (!box.width || !box.height) return false;
                            const options = {
                                bubbles: true, cancelable: true, composed: true,
                                view: window, pointerId: 1, pointerType: 'mouse',
                                isPrimary: true, button: 0,
                                clientX: box.left + box.width / 2,
                                clientY: box.top + box.height / 2
                            };
                            el.dispatchEvent(new PointerEvent('pointerdown',
                                {...options, buttons: 1}));
                            el.dispatchEvent(new PointerEvent('pointerup',
                                {...options, buttons: 0}));
                            return true;
                            """, seat,
                        )
                        if not clicked:
                            time.sleep(0.1)
                            continue
                        self.attempt_count += 1
                        pending_seat = (cx_clicked, cy_clicked)
                        logger.info(
                            "Seat click sent (cx=%s, cy=%s); awaiting page confirmation",
                            cx_clicked, cy_clicked,
                        )
                    else:
                        cx_clicked, cy_clicked = pending_seat

                    popup_btn = self.check_popup_exists(timeout=0.35)
                    if popup_btn:
                        pending_seat = None
                        logger.warning("❌ 座位被占，关闭弹窗")
                        safe_click(self.driver, popup_btn, method="js")
                        continue

                    # 等待提交按钮出现
                    submit_btn = self.check_submit_button_exists(timeout=2)
                    if not submit_btn:
                        logger.warning(
                            "Selection unconfirmed or submit button unavailable; "
                            "waiting without clicking the same seat again"
                        )
                        time.sleep(0.5)
                        continue

                    # 点击提交
                    safe_click(self.driver, submit_btn, method="js")
                    time.sleep(0.08)

                    # 提交后检查弹窗
                    popup_btn = self.check_popup_exists(timeout=0.35)
                    if popup_btn:
                        pending_seat = None
                        logger.warning("❌ 提交失败，关闭弹窗")
                        safe_click(self.driver, popup_btn, method="js")
                        continue

                    # 页面跳转为成功
                    try:
                        WebDriverWait(self.driver, 2).until(
                            lambda d: d.current_url != url_before
                        )
                        logger.info(
                            f"🎉 成功抢到目标区域座位 (cx={cx_clicked}, cy={cy_clicked})"
                        )
                        self.seat_selected = True
                        self.end_time = time.time()
                        break
                    except TimeoutException:
                        logger.warning("⚠️ 提交后未跳转，重试")
                        time.sleep(0.15)

            except Exception as e:
                logger.warning('⚠️ 区域抢座线程异常: %s', str(e).splitlines()[0])
                time.sleep(0.3)

        self.running = False
        logger.info("✅ 区域极速抢座线程结束")

    def print_statistics(self):
        """打印统计信息"""
        if self.start_time and self.end_time:
            duration = self.end_time - self.start_time
            print("\n" + "=" * 60)
            print("📊 抢座统计信息")
            print("=" * 60)
            print(f"⏱️  总耗时: {self.format_duration(duration)}")
            print(f"🔄 座位更新: {self.seat_update_count}次")
            print(f"🎯 尝试选座: {self.attempt_count}次")
            if self.preferred_seats:
                print(f"🎫 优先座位: {', '.join(self.preferred_seats)}")
            print("=" * 60 + "\n")

    def collect_available_seats(self, max_time=120):
        self.start_time = time.time()
        self.running = True
        self.seat_selected = False

        logger.info(f"🎯 区域模式抢座开始（最多 {max_time} 秒）")

        update_thread = threading.Thread(
            target=self.update_seats_continuously, daemon=True
        )
        update_thread.start()

        while time.time() - self.start_time < max_time:
            if self.seat_selected:
                logger.info("\n🎉🎉🎉 座位已成功选择并提交！")
                self.running = False
                time.sleep(0.5)
                self.print_statistics()
                return "success"

            time.sleep(0.1)

        self.running = False
        self.end_time = time.time()
        logger.warning("❌ 超时未抢到目标区域座位")
        self.print_statistics()
        return None

# ========== 主要功能函数 ==========
def check_buy_now_button(driver, timeout=300):
    """监控并点击购票区域主按钮，兼容优先购等不同文案。"""
    logger.info("🔍 开始监控购票区域主按钮（不限定文案）")
    try:
        buy_btn = WebDriverWait(driver, timeout, poll_frequency=0.1).until(BuyNowButtonClickable())
        label = " ".join(buy_btn.text.split())
        if not safe_click(driver, buy_btn, method="js"):
            return False
        logger.info("✅ 已点击购票入口按钮：%s", label)
        return True
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到可用的购票区域主按钮")
        return False


def click_specific_date(driver, date_str, target_time, timeout=600):
    """选择指定日期"""
    logger.info(f"🔍 等待日期 {date_str} 出现")
    try:
        date_btn = WebDriverWait(driver, timeout).until(SpecificDateClickable(date_str))

        js_click_date = f"""
        let btns = document.querySelectorAll('button.EntCalendar_dateButton__6TxQi');
        for (let b of btns) {{
            if (!b.disabled) {{
                let text = b.innerText.trim();
                if (text === '{date_str}') {{
                    b.click();
                    return text;
                }}
            }}
        }}
        return null;
        """
        selected = driver.execute_script(js_click_date)

        if selected:
            logger.info(f"✅ 日期 {date_str} 选择成功")
            wait_and_click_time(driver, target_time, timeout)
            click_buy_now(driver, timeout=10)
            return True
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到日期 {date_str}")
        return False


def wait_and_click_time(driver, target_time=None, timeout=300):
    """选择指定时间"""
    logger.info("🔍 等待时间按钮出现")
    try:
        time_btn = WebDriverWait(driver, timeout).until(
            TimeButtonClickable(target_time)
        )
        safe_click(driver, time_btn, method="js")
        logger.info(f"✅ 时间 {time_btn.text} 选择成功")
        return time_btn.text
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到时间按钮")
        return None

def click_buy_now(driver, timeout=10):
    """点击 Next 按钮"""
    logger.info("🔍 等待 Next 按钮")
    try:
        buy_btn = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='__next']/div/div[1]/div/section[2]/div[2]/div/button",
            timeout,
            "clickable",
        )
        if buy_btn:
            safe_click(driver, buy_btn, method="js")
            logger.info("✅ Next 按钮点击成功")
            return True
        return False
    except Exception:
        logger.error(f"❌ Next 按钮点击失败")
        return False


def solve_and_input_captcha(driver, timeout=300, retry_interval=0.5):
    """仅识别、填入一次；纠错和 Submit 由用户手动完成。"""
    try:
        logger.info('🔍 等待验证码，识别结果只自动填入一次')
        captcha_elem = WebDriverWait(driver, timeout).until(CaptchaVisible())
        input_box = wait_and_find(
            driver, By.XPATH,
            "//*[@id='__next']/div[2]/div/div/div/div/input",
            timeout, 'visible',
        )
        if not input_box:
            return False
        text = CaptchaSolver().solve(captcha_elem)
        if text:
            input_box.clear()
            input_box.send_keys(text)
            logger.info('✅ 验证码已填入：%s；请手动纠错并点击 Submit', text)
        else:
            logger.warning('未识别出文字，请手动填写验证码并点击 Submit')
        # 这里只表示已交给用户处理，不表示网站验证通过；不再自动重填或提交。
        return True
    except Exception as exc:
        logger.error('❌ 验证码填入失败：%s', str(exc).splitlines()[0])
        return False


def click_seat(driver, ticket_num, timeout=60):
    """补足目标票数，在票价页点击唯一可用的底部主操作按钮。"""
    logger.info('🎟 设置票数到 %s 张', ticket_num)
    deadline = time.monotonic() + timeout

    def wait(condition):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutException('设置票数超时')
        return WebDriverWait(driver, remaining, poll_frequency=0.1).until(condition)

    def read_stepper(d):
        for root in d.find_elements(By.CSS_SELECTOR, '.nds-e-stepper__root'):
            try:
                if not root.is_displayed():
                    continue
                field = root.find_element(By.CSS_SELECTOR, "input[role='spinbutton']")
                value = int(field.get_attribute('value'))
                maximum = field.get_attribute('aria-valuemax')
                return root, value, int(maximum) if maximum else None
            except (NoSuchElementException, StaleElementReferenceException, ValueError):
                continue
        return False

    try:
        if not isinstance(ticket_num, int) or ticket_num < 1:
            raise ValueError('目标票数必须为正整数')
        state = wait(read_stepper)
        while state[1] < ticket_num:
            root, current, maximum = state
            if maximum is not None and ticket_num > maximum:
                raise ValueError(f'目标 {ticket_num} 张超过当前控件上限 {maximum} 张')

            def increment_ready(d):
                latest = read_stepper(d)
                if not latest:
                    return False
                button = latest[0].find_element(
                    By.CSS_SELECTOR, 'button.nds-e-stepper__incrementButton')
                return button if button.is_displayed() and button.is_enabled() else False

            wait(increment_ready).click()
            # 必须确认数量真的增加，不能把发送点击当作成功。
            def count_changed(d):
                latest = read_stepper(d)
                return latest if latest and latest[1] != current else False

            state = wait(count_changed)
            logger.info('🎟 当前票数：%s', state[1])
        if state[1] != ticket_num:
            raise ValueError(f'当前票数 {state[1]} 与目标 {ticket_num} 不符')
        logger.info('✅ 已确认票数为 %s 张', ticket_num)
        logger.info('开始定位当前票价面板底部的唯一可用主按钮')
        def find_book_button(d):
            latest = read_stepper(d)
            if not latest or latest[1] != ticket_num:
                return False
            buttons = d.execute_script(
                """
                const stepper = arguments[0];
                const visible = el => {
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none'
                        && s.visibility !== 'hidden' && s.visibility !== 'collapse';
                };
                const main = stepper.closest('main, [role="main"]');
                if (!main || !visible(main)) return [];
                // 响应式页面的可见标题可能在 main 外；以数量控件所属票价面板为准。
                const panel = stepper.closest('[class*="PriceContent_layerWrap"]');
                if (!panel || !visible(panel)) return [];
                if (Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]'))
                    .some(visible)) return [];
                return Array.from(panel.querySelectorAll(
                    '[class*="PriceContent_footer"] button.entButtonGlobal, '
                    + '[class*="PriceContent_footer"] button[class*="EntButton_primary"]'
                )).filter(button => visible(button)
                    && !button.closest('.nds-e-stepper__root, aside, [role="dialog"]'));
                """, latest[0],
            ) or []
            candidates = []
            for button in buttons:
                try:
                    if (not button.is_displayed() or not button.is_enabled()
                            or button.get_attribute('aria-disabled') == 'true'):
                        continue
                    candidates.append((button, ' '.join(button.text.split())))
                except StaleElementReferenceException:
                    continue
            if len(candidates) > 1:
                raise RuntimeError('票价页底部存在多个可用主按钮，未点击：'
                                   + repr([label for _, label in candidates]))
            return candidates[0] if candidates else False

        while True:
            book_button, label = wait(find_book_button)
            try:
                logger.info('票数确认 %s 张，点击主按钮：%s', ticket_num, label)
                book_button.click()
                break
            except StaleElementReferenceException:
                # 只重试尚未执行的失效元素；其他点击异常交给外层报告。
                continue
        logger.info('✅ 购买主按钮已点击')
        return True
    except Exception as exc:
        logger.error('❌ 票数设置或购买按钮点击失败：%s', str(exc).splitlines()[0])
        return False


def click_check_now(driver, timeout=60):
    """填写手机号，滚动并勾选全部条款，再点击 Checkout。"""
    try:
        phone_input = wait_and_find(driver, By.ID, 'userPhone', timeout, 'clickable')
        if not phone_input:
            return False
        phone_input.clear()
        phone_input.send_keys(Config.PHONE_NUMBER)
        WebDriverWait(driver, timeout, poll_frequency=0.1).until(
            lambda d: d.find_element(By.ID, 'userPhone').get_attribute('value')
            == Config.PHONE_NUMBER)
        logger.info('✅ 手机号已填写并核对')
        driver.execute_script(
            'window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));')

        def find_agreement(d):
            candidates = []
            for label in d.find_elements(By.CSS_SELECTOR,
                    '[class*="Agreements_agreeAll"] label[class*="EntCheckCircle_checkbox"]'):
                try:
                    if not label.is_displayed():
                        continue
                    checkbox = label.find_element(By.CSS_SELECTOR, 'input[type="checkbox"]')
                    if checkbox.is_enabled():
                        candidates.append((label, checkbox))
                except (NoSuchElementException, StaleElementReferenceException):
                    continue
            if len(candidates) > 1:
                raise RuntimeError('存在多个可见的全部条款复选框，未点击')
            return candidates[0] if candidates else False

        label, checkbox = WebDriverWait(driver, timeout, poll_frequency=0.1).until(find_agreement)
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", label)
        if not checkbox.is_selected():
            # 点击关联 label，作用与点击内部的 EntCheckCircle_icon 相同。
            label.click()

        def agreement_checked(d):
            found = find_agreement(d)
            return bool(found and found[1].is_selected())

        WebDriverWait(driver, timeout, poll_frequency=0.1).until(agreement_checked)
        logger.info('✅ 已确认同意全部条款')

        def find_checkout(d):
            if (d.find_element(By.ID, 'userPhone').get_attribute('value')
                    != Config.PHONE_NUMBER or not agreement_checked(d)):
                return False
            current_label, _ = find_agreement(d)
            main = current_label.find_element(By.XPATH, 'ancestor::main[1]')
            candidates = []
            for button in main.find_elements(By.CSS_SELECTOR,
                    '[class*="Payment_payBottomButton"] button'):
                try:
                    text = ' '.join(button.text.split())
                    if (re.match(r'^Checkout(?:\s|$)', text, re.IGNORECASE)
                            and button.is_displayed() and button.is_enabled()
                            and button.get_attribute('aria-disabled') != 'true'):
                        candidates.append((button, text))
                except StaleElementReferenceException:
                    continue
            if len(candidates) > 1:
                raise RuntimeError('存在多个可用 Checkout 按钮，未点击：'
                                   + repr([text for _, text in candidates]))
            return candidates[0] if candidates else False

        checkout_btn, text = WebDriverWait(driver, timeout, poll_frequency=0.1).until(find_checkout)
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", checkout_btn)
        checkout_btn.click()
        logger.info('✅ 已点击 %s，后续支付请手动完成', text)
        return True
    except Exception as exc:
        logger.error('❌ 填写手机号、勾选条款或 Checkout 点击失败：%s', str(exc).splitlines()[0])
        return False


# ========== 主程序 ==========
def main():
    """主函数 - 执行完整的抢票流程"""
    driver = None

    try:
        # 打印配置信息
        print("\n" + "=" * 60)
        print("🚀 自动抢票系统")
        print("=" * 60)
        print(f"📅 目标日期: {Config.TARGET_DATE}")
        print(f"🕐 目标时间: {Config.TARGET_TIME}")
        print(f"🎫 票数: {Config.TICKET_COUNT}")
        if Config.PREFERRED_SEATS:
            print(f"🎯 优先座位: {', '.join(Config.PREFERRED_SEATS)}")
        print("=" * 60 + "\n")

        # 初始化浏览器
        print("请确保已用以下命令启动Chrome:")
        print(
            r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:/chrome_debug_profile"'
        )
        print("\n请手动打开目标页面，加载完成后按回车继续...")

        driver = init_driver()
        logger.info("✅ 浏览器连接成功")

        # # 步骤1: 点击 Buy now 按钮
        # logger.info("\n" + "=" * 60)
        # logger.info("步骤 1/6: 监控 Buy now 按钮")
        # logger.info("=" * 60)
        # if not check_buy_now_button(driver, timeout=Config.BUY_NOW_TIMEOUT):
        #     logger.warning("⚠️ Buy now 按钮未找到，继续下一步")

        # # 步骤2: 选择日期和时间
        # logger.info("\n" + "=" * 60)
        # logger.info("步骤 2/6: 选择日期和时间")
        # logger.info("=" * 60)
        # if not click_specific_date(
        #     driver,
        #     Config.TARGET_DATE,
        #     Config.TARGET_TIME,
        #     timeout=Config.DATE_SELECT_TIMEOUT,
        # ):
        #     raise Exception("❌ 日期选择失败")
        # logger.info("✅ 日期和时间选择成功")

        # 步骤3: 验证码识别（可选）
        logger.info("\n" + "=" * 60)
        logger.info("步骤 3/6: 验证码识别")
        logger.info("=" * 60)
        # 如果页面需要验证码，取消下面的注释
        if not solve_and_input_captcha(driver, timeout=Config.CAPTCHA_TIMEOUT):
            raise Exception("❌ 验证码输入框准备或填入失败")
        logger.info("验证码已交由你处理，程序不自动点击 Submit")

        # 步骤4: 选择座位
        logger.info("\n" + "=" * 60)
        logger.info("步骤 4/6: 自动选座")
        logger.info("=" * 60)
        logger.info('等待你手动纠错并点击 Submit，验证码关闭后开始选座')
        WebDriverWait(driver, Config.CAPTCHA_TIMEOUT, poll_frequency=0.1).until(
            lambda d: EC.invisibility_of_element_located((By.XPATH,
                "//*[@id='__next']/div[2]/div/div/div/div/div[1]/div[2]/img"))(d)
            and EC.invisibility_of_element_located((By.XPATH,
                "//*[@id='__next']/div[2]/div/footer/button"))(d))
        monitor = SeatMonitor(driver, preferred_seats=Config.PREFERRED_SEATS, auto_scroll=True)
        result = monitor.collect_available_seats(max_time=Config.SEAT_SELECT_TIMEOUT)
        #
        if result != "success":
            raise Exception("❌ 抢座失败")
        logger.info("✅ 座位选择成功")

        # # 步骤5: 设置票数
        # logger.info("\n" + "=" * 60)
        # logger.info("步骤 5/6: 设置票数")
        # logger.info("=" * 60)
        # time.sleep(1)  # 等待页面稳定
        # ticket_result = click_seat(
        #     driver, Config.TICKET_COUNT, timeout=Config.TICKET_COUNT_TIMEOUT
        # )
        # if not ticket_result:
        #     raise Exception("❌ 票数设置失败")
        # logger.info("✅ 票数设置成功")

        # 步骤6: 填写手机号、勾选条款并结账
        logger.info("\n" + "=" * 60)
        logger.info("步骤 6/6: 填写手机号、勾选条款并结账")
        logger.info("=" * 60)
        if not click_check_now(driver, timeout=Config.CHECKOUT_TIMEOUT):
            raise Exception("❌ 填写手机号、勾选条款或结账失败")
        logger.info("手机号及条款已处理，已点击 Checkout，后续支付请手动完成")

        # 成功完成
        print("\n" + "=" * 60)
        print("🎉🎉🎉 抢票流程完成！")
        print("=" * 60)
        print("✅ 所有步骤已成功执行")
        print("💡 请手动检查并完成最后的支付步骤")
        print("=" * 60 + "\n")

    except KeyboardInterrupt:
        logger.warning("\n⚠️ 用户中断程序")
    except Exception as e:
        logger.error(f"\n❌ 程序异常: {e}")
        import traceback

        traceback.print_exc()
    finally:
        print("\n" + "=" * 60)
        print("程序执行完毕，浏览器保持打开状态")
        print("按 Ctrl+C 可安全退出")
        print("=" * 60)
        # 不自动关闭浏览器
        if driver:
            try:
                input("\n按回车键关闭浏览器...")
                driver.quit()
            except:
                pass

if __name__ == "__main__":
    main()
