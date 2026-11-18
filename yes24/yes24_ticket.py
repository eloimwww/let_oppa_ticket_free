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

# ========== 配置区域 ==========
class Config:
    """配置类 - 集中管理所有配置参数"""

    # 浏览器配置
    CHROME_DEBUG_PORT = "127.0.0.1:9222"
    CHROMEDRIVER_PATH = r"chromedriver-win64\chromedriver.exe"

    # 抢票配置
    TARGET_DATE = "2026-05-23"  # ⚠️ 目标日期，格式 YYYY-MM-DD（与日历元素 id 一致）
    TARGET_TIME = "14:00"       # ⚠️ 目标场次时间
    TICKET_COUNT = 1
    PHONE_NUMBER = "1"      # ⚠️ 请填写真实手机号

    # 优先座位（两种方式，任选其一，都留空则抢第一个可用座位）
    # 方式1：指定座位 value ID，例如 ["1000021", "1000022"]
    PREFERRED_SEATS = []
    # 方式2：指定坐标范围（LEFT/TOP px），例如 {"left_min": 150, "left_max": 320, "top_min": 90, "top_max": 200}
    PREFERRED_AREA = {}  # 留空则不限制区域

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
        # 优先使用配置路径，不存在则让 Selenium 自动查找
        import os
        if Config.CHROMEDRIVER_PATH and os.path.isfile(Config.CHROMEDRIVER_PATH):
            service = Service(Config.CHROMEDRIVER_PATH)
        else:
            service = Service()
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
        logger.info(f"点击成功")
        return True
    except Exception as e:
        logger.warning(f"⚠️ 点击失败: {e}")
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


# ========== step1 点击Booking按钮 ==========
class BuyNowButtonClickable:
    """自定义等待条件：Booking 按钮可点击（ID: hlkPurchase）"""
    def __call__(self, driver):
        js_buy = """
        let btn = document.querySelector("#hlkPurchase");
        return btn ? btn : null;
        """
        buy_btn = driver.execute_script(js_buy)
        return buy_btn if buy_btn else False


# ========== 选择日期和时间 ==========
class SpecificDateClickable:
    """自定义等待条件：指定日期按钮可点击
    日期元素格式: <a class="dcursor" id="2026-05-23">23</a>
    """
    def __init__(self, date_str):
        self.date_str = date_str  # 格式: "2026-05-23"

    def __call__(self, driver):
        try:
            btn = driver.find_element(By.CSS_SELECTOR, f'a.dcursor[id="{self.date_str}"]')
            if btn.is_displayed():
                return btn
        except (NoSuchElementException, StaleElementReferenceException):
            pass
        return False


class TimeButtonClickable:
    """自定义等待条件：时间按钮可点击
    时间元素格式: <li timeinfo="17:00" class="on">[1] 17:00</li>
    """
    def __init__(self, target_time):
        self.target_time = target_time  # 格式: "17:00"

    def __call__(self, driver):
        try:
            if self.target_time:
                btn = driver.find_element(By.CSS_SELECTOR, f'li[timeinfo="{self.target_time}"]')
            else:
                # 未指定时间则取第一个可见场次
                btn = driver.find_element(By.CSS_SELECTOR, 'li[timeinfo]')
            if btn.is_displayed():
                return btn
        except (NoSuchElementException, StaleElementReferenceException):
            pass
        return False


class CaptchaVisible:
    """自定义等待条件：验证码图片可见"""
    def __call__(self, driver):
        try:
            # ⚠️ TODO: 根据实际页面更新选择器
            captcha_elem = driver.find_element(
                By.XPATH, ""
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
            # ⚠️ TODO: 根据实际页面更新选择器
            driver.find_element(By.XPATH, "")
            return False
        except NoSuchElementException:
            return True


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


# ========== 座位监控类（待实现） ==========
class SeatMonitor:
    """座位监控器 - 负责自动抢座（待实现）"""
    def __init__(self, driver, preferred_seats=None, preferred_area=None, auto_scroll=True):
        self.driver = driver
        self.seat_queue = queue.Queue(maxsize=1)
        self.running = True
        self.driver_lock = threading.Lock()
        self.seat_selected = False
        self.preferred_seats = preferred_seats or []
        self.preferred_area = preferred_area or {}
        self.auto_scroll = auto_scroll

        self.start_time = None
        self.end_time = None
        self.attempt_count = 0
        self.seat_update_count = 0

        try:
            self.driver.command_executor._conn.pool_maxsize = 50
            self.driver.command_executor._conn.pool_block = False
            self.driver.command_executor._timeout = 30
        except Exception as e:
            logger.warning(f"⚠️ 连接池配置失败: {e}")

    def format_duration(self, seconds):
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
        """检查错误弹窗"""
        try:
            # ⚠️ TODO: 根据实际页面更新选择器
            popup_btn = WebDriverWait(self.driver, timeout).until(
                EC.visibility_of_element_located(
                    (By.XPATH, "")
                )
            )
            return popup_btn
        except TimeoutException:
            return None

    def check_submit_button_exists(self, timeout=0.15):
        """检查座位提交按钮"""
        try:
            # ⚠️ TODO: 根据实际页面更新选择器
            submit_btn = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "")
                )
            )
            return submit_btn
        except TimeoutException:
            return None

    def update_seats_continuously(self):
        """极速抢座线程：切换 iframe → 点击 s9 座位 → 回主页面 → 点确认"""
        logger.info("⚡ 极速抢座线程启动")

        while self.running and not self.seat_selected:
            try:
                with self.driver_lock:
                    url_before = self.driver.current_url

                    # 1. 切换进 iframe，按优先顺序找可用座位并点击
                    clicked = self.driver.execute_script("""
                        let preferred_seats = arguments[0];
                        let preferred_area  = arguments[1];
                        let iframe = document.querySelector('iframe[name="ifrmSeatFrame"]');
                        if (!iframe) return [false, null];
                        let iDoc = iframe.contentDocument;
                        let seat = null;

                        // 优先方式1：指定座位 ID
                        if (preferred_seats.length > 0) {
                            for (let sid of preferred_seats) {
                                let s = iDoc.querySelector('div.s9[value="' + sid + '"]');
                                if (s) { seat = s; break; }
                            }
                        }

                        // 优先方式2：坐标范围
                        if (!seat && preferred_area && preferred_area.left_min !== undefined) {
                            let all = iDoc.querySelectorAll('div.s9[name="tk"]');
                            for (let s of all) {
                                let left = parseFloat(s.style.left);
                                let top  = parseFloat(s.style.top);
                                if (left >= preferred_area.left_min && left <= preferred_area.left_max &&
                                    top  >= preferred_area.top_min  && top  <= preferred_area.top_max) {
                                    seat = s; break;
                                }
                            }
                        }

                        // 兜底：第一个可用座位
                        if (!seat) seat = iDoc.querySelector('div.s9[name="tk"]');

                        if (!seat) return [false, null];
                        let seatId = seat.getAttribute('value');
                        seat.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                        return [true, seatId];
                    """, self.preferred_seats, self.preferred_area)

                    clicked_flag, seat_id = clicked
                    if not clicked_flag:
                        logger.info("⏳ 暂无可用座位 (s9)，继续等待")
                        time.sleep(0.5)
                        continue

                    logger.info(f"🎯 点击座位成功: value={seat_id}")
                    time.sleep(0.15)

                    # 2. 在主页面点击「Check my seat(s)」确认按钮
                    confirm_btn = self.driver.execute_script("""
                        let btn = document.querySelector('img[src*="btn_chkseat"]');
                        return btn ? btn : null;
                    """)

                    if not confirm_btn:
                        logger.warning("⚠️ 未找到确认按钮，重试")
                        time.sleep(0.1)
                        continue

                    self.driver.execute_script("arguments[0].click();", confirm_btn)
                    logger.info("✅ 点击 Check my seat(s) 确认按钮")
                    time.sleep(0.15)

                    # 3. 检查页面是否跳转（成功标志）
                    try:
                        WebDriverWait(self.driver, 2).until(
                            lambda d: d.current_url != url_before
                        )
                        logger.info(f"🎉 抢座成功！座位: {seat_id}")
                        self.seat_selected = True
                        self.end_time = time.time()
                        self.seat_update_count += 1
                        break
                    except TimeoutException:
                        logger.warning("⚠️ 提交后未跳转，重试")
                        time.sleep(0.15)

            except Exception as e:
                logger.warning(f"⚠️ 抢座线程异常: {e}")
                time.sleep(0.3)

        self.running = False
        logger.info("✅ 抢座线程结束")

    def print_statistics(self):
        if self.start_time and self.end_time:
            duration = self.end_time - self.start_time
            print("\n" + "=" * 60)
            print("📊 抢座统计信息")
            print("=" * 60)
            print(f"⏱️  总耗时: {self.format_duration(duration)}")
            print(f"🔄 座位更新: {self.seat_update_count}次")
            print(f"🎯 尝试选座: {self.attempt_count}次")
            print("=" * 60 + "\n")

    def collect_available_seats(self, max_time=1800):
        """主循环：启动抢座线程并等待结果"""
        self.start_time = time.time()
        self.running = True
        self.seat_selected = False

        logger.info(f"🎯 开始抢座（最多 {max_time} 秒）")

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
        logger.warning("❌ 超时未抢到座位")
        self.print_statistics()
        return None


# ========== 主要功能函数 ==========
def switch_to_new_window(driver, original_handles, timeout=15):
    """等待并切换到新弹出的窗口"""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.window_handles) > len(original_handles)
        )
        new_handle = [h for h in driver.window_handles if h not in original_handles][0]
        driver.switch_to.window(new_handle)
        logger.info(f"✅ 已切换到新窗口: {driver.current_url}")
        return True
    except TimeoutException:
        logger.error("❌ 等待新窗口超时")
        return False


def check_buy_now_button(driver, timeout=300):
    """步骤1：点击 Booking 按钮，并切换到弹出的售票新窗口"""
    logger.info("🔍 开始监控 Booking 按钮 (#hlkPurchase)")
    try:
        original_handles = driver.window_handles
        buy_btn = WebDriverWait(driver, timeout).until(BuyNowButtonClickable())
        safe_click(driver, buy_btn, method="js")
        logger.info("✅ 成功点击 Booking 按钮，等待新窗口...")
        switch_to_new_window(driver, original_handles)
        return True
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到 Booking 按钮")
        return False


def click_specific_date(driver, date_str, target_time, timeout=300):
    """步骤2：选择指定日期，然后选时间，再点下一步"""
    logger.info(f"🔍 等待日期 {date_str} 出现")
    try:
        date_btn = WebDriverWait(driver, timeout).until(SpecificDateClickable(date_str))

        # 直接用 id 定位并调用 yes24 的日期点击函数
        js_click_date = f"""
        let btn = document.querySelector('a.dcursor[id="{date_str}"]');
        if (btn) {{
            btn.click();
            return true;
        }}
        return false;
        """
        selected = driver.execute_script(js_click_date)

        if selected:
            logger.info(f"✅ 日期 {date_str} 选择成功")
            wait_and_click_time(driver, target_time, timeout)
            click_next(driver, timeout=10)
            return True
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到日期 {date_str}")
        return False


def wait_and_click_time(driver, target_time=None, timeout=300):
    """步骤2b：选择指定场次时间"""
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


def click_next(driver, timeout=10):
    """步骤2c：点击 Select Seats 按钮 (id=btnSeatSelect)"""
    logger.info("🔍 等待 Select Seats 按钮")
    try:
        btn = wait_and_find(
            driver,
            By.CSS_SELECTOR,
            "#btnSeatSelect",
            timeout,
            "clickable",
        )
        if btn:
            safe_click(driver, btn, method="js")
            logger.info("✅ Select Seats 按钮点击成功")
            return True
        return False
    except Exception:
        logger.error("❌ Select Seats 按钮点击失败")
        return False


def solve_and_input_captcha(driver, timeout=300, retry_interval=0.5):
    """步骤3（可选）：验证码识别并提交"""
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

                # ⚠️ TODO: 根据实际页面更新验证码输入框选择器
                input_box = wait_and_find(driver, By.XPATH, "", 5)
                if input_box:
                    input_box.clear()
                    input_box.send_keys(text)
                    have_try += 1
                    logger.info(f"✅ 验证码已输入: {text}")

                # ⚠️ TODO: 根据实际页面更新验证码提交按钮选择器
                submit_btn = wait_and_find(driver, By.XPATH, "", 0.8, "clickable")
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
    """步骤5：增加票数并点击购买"""
    logger.info(f"🎟 增加票数到 {ticket_num}张")
    try:
        # ⚠️ TODO: 根据实际页面更新票数 ➕ 按钮选择器
        plus_button = wait_and_find(driver, By.XPATH, "", timeout, "clickable")

        if not plus_button:
            return False

        for i in range(ticket_num):
            plus_button.click()
            logger.info(f"🟢 已点击 ➕ 按钮 ({i + 1}/{ticket_num})")
            time.sleep(0.3)

        # ⚠️ TODO: 根据实际页面更新购买按钮选择器
        book_button = wait_and_find(driver, By.XPATH, "", 10, "clickable")
        if book_button:
            book_button.click()
            logger.info("✅ 购买按钮点击成功")
            return True

        return False

    except Exception as e:
        logger.error(f"❌ 增加票数失败: {e}")
        return False


def click_next_after_seat(driver, timeout=30):
    """步骤4b：座位确认后点击 Next 按钮 (btn_next02)"""
    logger.info("🔍 等待 Next 按钮 (btn_next02)")
    try:
        btn = WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                "let b = document.querySelector('img[src*=\"btn_next02\"]'); return b ? b : null;"
            )
        )
        if btn:
            driver.execute_script("arguments[0].click();", btn)
            logger.info("✅ Next 按钮点击成功")
            return True
        return False
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到 Next 按钮")
        return False


def click_check_now(driver, timeout=60):
    """步骤6：选择信用卡 → 勾选同意 → 点击付款"""
    try:
        # 1. 填写手机号 (#ordererMobile1)
        phone_input = wait_and_find(driver, By.CSS_SELECTOR, "#ordererMobile1", 10)
        if phone_input:
            driver.execute_script("arguments[0].value = '';", phone_input)
            phone_input.send_keys(Config.PHONE_NUMBER)
            logger.info(f"✅ 手机号已填写: {Config.PHONE_NUMBER}")
        else:
            logger.warning("⚠️ 未找到手机号输入框 #ordererMobile1")

        # 2. 选择信用卡付款方式 (#rdoPays2)
        pay_radio = wait_and_find(driver, By.CSS_SELECTOR, "#rdoPays2", timeout, "clickable")
        if pay_radio:
            driver.execute_script("arguments[0].click();", pay_radio)
            logger.info("✅ 信用卡付款方式已选择")
        else:
            logger.warning("⚠️ 未找到信用卡选项 #rdoPays2")

        # 3. 勾选 User Info Agree
        agree1 = wait_and_find(driver, By.CSS_SELECTOR, "#cbxUserInfoAgree", 10, "clickable")
        if agree1:
            driver.execute_script("arguments[0].click();", agree1)
            logger.info("✅ User Info Agree 已勾选")
        else:
            logger.warning("⚠️ 未找到 #cbxUserInfoAgree")

        # 4. 勾选 Cancel Fee Agree
        agree2 = wait_and_find(driver, By.CSS_SELECTOR, "#cbxCancelFeeAgree", 10, "clickable")
        if agree2:
            driver.execute_script("arguments[0].click();", agree2)
            logger.info("✅ Cancel Fee Agree 已勾选")
        else:
            logger.warning("⚠️ 未找到 #cbxCancelFeeAgree")

        # 5. 点击付款按钮 (#imgPayEnd)
        pay_btn = wait_and_find(driver, By.CSS_SELECTOR, "#imgPayEnd", 10, "clickable")
        if pay_btn:
            driver.execute_script("arguments[0].click();", pay_btn)
            logger.info("✅ 付款按钮已点击")
            return True

        logger.error("❌ 未找到付款按钮 #imgPayEnd")
        return False

    except Exception as e:
        logger.error(f"❌ 结账流程失败: {e}")
        return False


# ========== 主程序 ==========
def main():
    """主函数 - 执行完整的yes24抢票流程"""
    driver = None

    try:
        print("\n" + "=" * 60)
        print("🚀 YES24 自动抢票系统")
        print("=" * 60)
        print(f"📅 目标日期: {Config.TARGET_DATE}")
        print(f"🕐 目标时间: {Config.TARGET_TIME}")
        print(f"🎫 票数: {Config.TICKET_COUNT}")
        print("=" * 60 + "\n")

        print("请确保已用以下命令启动Chrome:")
        print('"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:/chrome_debug_profile"')
        print("\n请手动打开目标页面，加载完成后按回车继续...")
        input()

        driver = init_driver()
        logger.info("✅ 浏览器连接成功")

        # 步骤1: 点击 예매하기 按钮
        logger.info("\n" + "=" * 60)
        logger.info("步骤 1/6: 监控 예매하기 按钮")
        logger.info("=" * 60)
        if not check_buy_now_button(driver, timeout=Config.BUY_NOW_TIMEOUT):
            logger.warning("⚠️ 예매하기 按钮未找到，继续下一步")

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

        # 步骤3: 验证码识别（可选，按需取消注释）
        # logger.info("\n" + "=" * 60)
        # logger.info("步骤 3/6: 验证码识别")
        # logger.info("=" * 60)
        # if not solve_and_input_captcha(driver, timeout=Config.CAPTCHA_TIMEOUT):
        #     raise Exception("❌ 验证码验证失败")

        # 步骤4: 选择座位（待实现）
        logger.info("\n" + "=" * 60)
        logger.info("步骤 4/6: 自动选座（待实现）")
        logger.info("=" * 60)
        monitor = SeatMonitor(
            driver,
            preferred_seats=Config.PREFERRED_SEATS,
            preferred_area=Config.PREFERRED_AREA,
            auto_scroll=True,
        )
        result = monitor.collect_available_seats(max_time=Config.SEAT_SELECT_TIMEOUT)
        if result != "success":
            raise Exception("❌ 抢座失败")
        logger.info("✅ 座位选择成功")

        # 步骤4b: 点击第一个 Next 按钮
        logger.info("\n" + "=" * 60)
        logger.info("步骤 4b/6: 点击第一个 Next 按钮")
        logger.info("=" * 60)
        if not click_next_after_seat(driver, timeout=30):
            raise Exception("❌ 第一个 Next 按钮点击失败")
        logger.info("✅ 第一个 Next 按钮点击成功")

        # 步骤4c: 点击第二个 Next 按钮
        logger.info("\n" + "=" * 60)
        logger.info("步骤 4c/6: 点击第二个 Next 按钮")
        logger.info("=" * 60)
        if not click_next_after_seat(driver, timeout=30):
            raise Exception("❌ 第二个 Next 按钮点击失败")
        logger.info("✅ 第二个 Next 按钮点击成功")

        # 步骤5: 设置票数
        logger.info("\n" + "=" * 60)
        logger.info("步骤 5/6: 设置票数")
        logger.info("=" * 60)
        time.sleep(1)
        if not click_seat(driver, Config.TICKET_COUNT, timeout=Config.TICKET_COUNT_TIMEOUT):
            raise Exception("❌ 票数设置失败")
        logger.info("✅ 票数设置成功")

        # 步骤6: 填写信息并结账
        logger.info("\n" + "=" * 60)
        logger.info("步骤 6/6: 填写信息并结账")
        logger.info("=" * 60)
        if not click_check_now(driver, timeout=Config.CHECKOUT_TIMEOUT):
            raise Exception("❌ 结账失败")

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
        if driver:
            try:
                input("\n按回车键关闭浏览器...")
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
