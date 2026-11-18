# interpark自动点击购票（通过设置cx和cy进行定位）
import cv2
import numpy as np
import pytesseract
from PIL import Image
import io
import base64
import logging
import warnings
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
import time
import threading
import queue
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
# ========== 配置区域 ==========
class Config:
    """配置类 - 集中管理所有配置参数"""

    # 浏览器配置
    CHROME_DEBUG_PORT = "127.0.0.1"
    CHROMEDRIVER_PATH = r"chromedriver.exe"

    # 抢票配置
    TARGET_DATE = "28"
    TARGET_TIME = "5:00"
    TICKET_COUNT = 1
    PHONE_NUMBER = "1"  # 手机号

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
def init_driver():
    """初始化并返回 WebDriver"""
    try:
        options = webdriver.ChromeOptions()
        options.debugger_address = Config.CHROME_DEBUG_PORT
        service = Service(Config.CHROMEDRIVER_PATH)
        # service = Service(
        #     ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install()
        # )
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
    """自定义等待条件：Buy now 按钮可点击"""
    def __call__(self, driver):
        js_buy = """
        let btn = document.querySelector(
            "body > div.sc-55fb52ed-0.gUeTO > div.sc-c0486d96-0.fxHvXX > div.fixed-bottom___StyledDiv-sc-c736ce1c-0.jXQEGO > div > button"
        );
        return btn ? btn : null;
        """
        buy_btn = driver.execute_script(js_buy)
        return buy_btn if buy_btn else False

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
                "circle.SeatMap_seatSvg__ruvlB:not([class*='SeatMap_disabled'])",
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

    def check_submit_button_exists(self, timeout=0.15):
        """检查提交按钮 - 优化超时时间"""
        try:
            submit_btn = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//*[@id='__next']/div/div/main/div[2]/aside/section/div/div[3]/button",
                    )
                )
            )
            return submit_btn
        except TimeoutException:
            return None

    def update_seats_continuously(self):
        """区域极速抢座线程"""
        logger.info("⚡ 区域极速抢座线程启动")
        self.running = True

        while self.running and not self.seat_selected:
            try:
                with self.driver_lock:
                    url_before = self.driver.current_url

                    # ！！！！！！⚠️⚠️⚠️在此修改 CX 和 CY 规定范围 ⚠️⚠️！！！！！！
                    clicked = self.driver.execute_script(
                        """
                        const seats = document.querySelectorAll(
                            "circle.SeatMap_seatSvg__ruvlB:not([class*='SeatMap_disabled'])"
                        );
                        for (const seat of seats) {
                            const cx = parseFloat(seat.getAttribute("cx"));
                            const cy = parseFloat(seat.getAttribute("cy"));
                            if (cx >= 128 && cx <= 302 && cy >= 143 && cy <= 232) {
                                seat.dispatchEvent(new MouseEvent('click', {bubbles:true}));          
                                return [true, cx, cy];
                            }
                        }
                        return [false, null, null];
                        """
                    )

                    clicked_flag, cx_clicked, cy_clicked = clicked
                    if not clicked_flag:
                        logger.info("⏳ 当前目标区域无可用座位")
                        time.sleep(0.5)
                        continue

                    logger.info(
                        f"🎯 点击目标区域座位成功 (cx={cx_clicked}, cy={cy_clicked})"
                    )

                    # 检查弹窗（可能被别人抢走）
                    popup_btn = self.check_popup_exists(timeout=0.35)
                    if popup_btn:
                        logger.warning("❌ 座位被占，关闭弹窗")
                        safe_click(self.driver, popup_btn, method="js")
                        continue

                    # 等待提交按钮出现
                    submit_btn = self.check_submit_button_exists(timeout=0.35)
                    if not submit_btn:
                        logger.warning("⚠️ 未检测到提交按钮，重试")
                        time.sleep(0.1)
                        continue

                    # 点击提交
                    safe_click(self.driver, submit_btn, method="js")
                    time.sleep(0.08)

                    # 提交后检查弹窗
                    popup_btn = self.check_popup_exists(timeout=0.35)
                    if popup_btn:
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
                logger.warning(f"⚠️ 区域抢座线程异常: {e}")
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
    """监控并点击 Buy now 按钮"""
    logger.info("🔍 开始监控 Buy now 按钮")
    try:
        buy_btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[3]/div[2]/div[5]/div/button/div")))
        safe_click(driver, buy_btn, method="js")
        logger.info("✅ 成功点击 Buy now 按钮")
        return True
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到 Buy now 按钮")
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
    """验证码识别并提交"""
    solver = CaptchaSolver()
    logger.info("🔍 开始监控验证码")

    start_time = time.time()
    have_try = 0
    while time.time() - start_time < timeout:
        try:
            captcha_elem = WebDriverWait(driver, 10).until(CaptchaVisible())
            text = solver.solve(captcha_elem)
            if text:
                if have_try != 0:
                    return True

                input_box = wait_and_find(
                    driver,
                    By.XPATH,
                    "//*[@id='__next']/div[2]/div/div/div/div/input",
                    5,
                )
                if input_box:
                    input_box.clear()
                    input_box.send_keys(text)
                    have_try += 1
                    logger.info(f"✅ 验证码已输入: {text}")

                submit_btn = wait_and_find(
                    driver,
                    By.XPATH,
                    "//*[@id='__next']/div[2]/div/footer/button",
                    0.8,
                    "clickable",
                )
                if submit_btn:
                    safe_click(driver, submit_btn, method="js")
                    have_try += 1
                    logger.info("验证码已提交")

                try:
                    WebDriverWait(driver, 3).until(SubmitButtonDisappeared())
                    logger.info("✅ 验证码验证成功")
                    return True
                except TimeoutException:
                    logger.warning("⚠️ 验证码可能错误，重试")
                    time.sleep(retry_interval)
                    continue

        except TimeoutException:
            logger.warning("⚠️ 等待验证码超时")
            time.sleep(retry_interval)
        except Exception as e:
            logger.error(f"⚠️ 验证码识别异常: {e}")
            time.sleep(retry_interval)

    logger.error(f"❌ 验证码识别超时 {timeout}秒")
    return False


def click_seat(driver, ticket_num, timeout=60):
    """增加票数并点击购买"""
    logger.info(f"🎟 增加票数到 {ticket_num}张")
    try:
        plus_button = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='__next']/div/div/main/div[2]/div[2]/div/div[2]/details/ul/li/div[2]/button[2]",
            timeout,
            "clickable",
        )

        if not plus_button:
            return False

        for i in range(ticket_num):
            plus_button.click()
            logger.info(f"🟢 已点击 ➕ 按钮 ({i+1}/{ticket_num})")
            time.sleep(0.3)

        ticket_count_elem = wait_and_find(
            driver, By.CSS_SELECTOR, "div.Spinner_count__ov_Mg", 5
        )
        if ticket_count_elem:
            logger.info(f"🎟 当前票数: {ticket_count_elem.text}")

        book_button = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='__next']/div/div/main/div[2]/div[2]/div/div[3]/button",
            10,
            "clickable",
        )
        if book_button:
            book_button.click()
            logger.info("✅ 购买按钮点击成功")
            return True

        return False

    except Exception as e:
        logger.error(f"❌ 增加票数失败: {e}")
        return False


def click_check_now(driver, timeout=60):
    """填写购买信息并结账"""
    try:
        # 选择国家
        country_dropdown = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='__next']/main/div[2]/div[1]/div[2]/section[2]/div[1]/div[4]/div/div/div/button",
            timeout,
            "clickable",
        )
        if country_dropdown:
            safe_click(driver, country_dropdown, method="js")
            logger.info("🌍 国家下拉菜单已打开")

        china_option = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='__next']/main/div[2]/div[1]/div[2]/section[2]/div[1]/div[4]/div/div/div/div/div[2]/button[3]/span[1]",
            10,
            "clickable",
        )
        if china_option:
            safe_click(driver, china_option, method="js")
            logger.info("✅ 已选择中国")

        # 填写手机号
        phone_input = wait_and_find(driver, By.XPATH, "//*[@id='userPhone']", 10)
        if phone_input:
            phone_input.clear()
            phone_input.send_keys(Config.PHONE_NUMBER)
            logger.info(f"✅ 手机号已填写: {Config.PHONE_NUMBER}")

        # 勾选同意条款
        checkbox = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='__next']/main/div[2]/div[2]/section[2]/div/div[1]/label/i",
            10,
            "clickable",
        )
        if checkbox:
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                checkbox,
            )
            time.sleep(1)
            checkbox.click()
            logger.info("✅ 同意条款已勾选")

        # 点击结账按钮
        checkout_btn = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='__next']/main/div[2]/div[2]/div/button",
            10,
            "clickable",
        )
        if checkout_btn:
            checkout_btn.click()  # 取消注释以实际点击
            logger.info("✅ 结账按钮已准备")
            return True

        return False

    except Exception as e:
        logger.error(f"❌ 填写购买信息失败: {e}")
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
            '"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:/chrome_debug_profile"'
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

        # 步骤2: 选择日期和时间
        logger.info("\n" + "=" * 60)
        logger.info("步骤 2/6: 选择日期和时间")
        logger.info("=" * 60)
        if not click_specific_date(
            driver,
            Config.TARGET_DATE,
            Config.TARGET_TIME,
            timeout=Config.DATE_SELECT_TIMEOUT,
        ):
            raise Exception("❌ 日期选择失败")
        logger.info("✅ 日期和时间选择成功")

        # 步骤3: 验证码识别（可选）
        logger.info("\n" + "=" * 60)
        logger.info("步骤 3/6: 验证码识别")
        logger.info("=" * 60)
        # 如果页面需要验证码，取消下面的注释
        if not solve_and_input_captcha(driver, timeout=Config.CAPTCHA_TIMEOUT):
            raise Exception("❌ 验证码验证失败")
        logger.info("⏭️ 跳过验证码步骤")

        # 步骤4: 选择座位
        logger.info("\n" + "=" * 60)
        logger.info("步骤 4/6: 自动选座")
        logger.info("=" * 60)
        monitor = SeatMonitor(driver, preferred_seats=Config.PREFERRED_SEATS, auto_scroll=True)
        result = monitor.collect_available_seats(max_time=Config.SEAT_SELECT_TIMEOUT)
        #
        if result != "success":
            raise Exception("❌ 抢座失败")
        logger.info("✅ 座位选择成功")

        # 步骤5: 设置票数
        logger.info("\n" + "=" * 60)
        logger.info("步骤 5/6: 设置票数")
        logger.info("=" * 60)
        time.sleep(1)  # 等待页面稳定
        if not click_seat(
            driver, Config.TICKET_COUNT, timeout=Config.TICKET_COUNT_TIMEOUT
        ):
            raise Exception("❌ 票数设置失败")
        logger.info("✅ 票数设置成功")

        # 步骤6: 填写信息并结账
        logger.info("\n" + "=" * 60)
        logger.info("步骤 6/6: 填写信息并结账")
        logger.info("=" * 60)
        # 如果需要自动结账，取消下面的注释
        if not click_check_now(driver, timeout=Config.CHECKOUT_TIMEOUT):
            raise Exception("❌ 结账失败")
        logger.info("⏭️  跳过自动结账（需手动完成）")

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
