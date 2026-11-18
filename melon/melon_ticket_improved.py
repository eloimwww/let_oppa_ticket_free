# ================= 多进程入口壳 =================
import os
import time
from multiprocessing import Process, set_start_method
# =================================================

# melon自动点击购票
import cv2
import numpy as np
import pytesseract
from PIL import Image
import io
import base64
import logging
import warnings

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
import queue
import pytesseract
import psutil
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
import ddddocr
import threading

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'


# ========== 配置区域 ==========
class Config:
    TICKET_COUNT = 1
    PHONE_NUMBER = ""
    CHROMEDRIVER_PATH = r"chromedriver.exe"

    # 🔥 新增:调试端口配置(每个进程使用不同端口)
    DEBUG_PORTS = [9527, 9526]  # 三个进程使用三个端口

    BUY_NOW_TIMEOUT = 300
    DATE_SELECT_TIMEOUT = 300
    TIME_SELECT_TIMEOUT = 300
    CAPTCHA_TIMEOUT = 300
    SEAT_SELECT_TIMEOUT = 1800
    TICKET_COUNT_TIMEOUT = 30
    CHECKOUT_TIMEOUT = 30


# ========== 日志 ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ========== WebDriver 初始化（🔥 核心修改） ==========
def init_driver(debug_port):
    """
    连接到已打开的Chrome浏览器
    参数: debug_port - Chrome调试端口号
    """
    options = webdriver.ChromeOptions()

    # ⭐ 核心:连接到指定端口的Chrome实例
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")

    # 最小化配置,避免兼容性问题
    options.add_argument("--disable-blink-features=AutomationControlled")

    service = Service(Config.CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)

    logger.info(f"✅ 已连接到端口 {debug_port} 的Chrome浏览器")
    return driver


# =================================================
# ========== 工具函数 ==========
def safe_click(driver, element, method="js"):
    """安全点击元素,支持多种点击方式"""
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
        return True
    except Exception as e:
        logger.warning(f"⚠️ 点击失败: {e}")
        return False


def wait_and_find(driver, by, value, timeout=10, condition="presence"):
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


# ========== 🔥 新增:窗口管理工具 ==========
def switch_to_popup_window(driver, timeout=10):
    """
    切换到新弹出的窗口(选座窗口)
    返回: True表示成功切换, False表示失败
    """
    try:
        # 等待新窗口出现
        WebDriverWait(driver, timeout).until(lambda d: len(d.window_handles) > 1)

        # 切换到最新的窗口
        driver.switch_to.window(driver.window_handles[-1])
        logger.info(f"✅ 已切换到弹窗: {driver.title}")
        return True
    except TimeoutException:
        logger.warning("⚠️ 未检测到新窗口")
        return False


def switch_to_main_window(driver):
    """切换回主窗口"""
    try:
        driver.switch_to.window(driver.window_handles[0])
        logger.info("✅ 已切换回主窗口")
        return True
    except Exception as e:
        logger.warning(f"⚠️ 切换主窗口失败: {e}")
        return False


# ========== 自定义等待条件 ==========
class BuyNowButtonClickable:
    """自定义等待条件: Buy now 按钮可点击"""

    def __call__(self, driver):
        js_buy = """
        let btn = document.querySelector(
            "body > div.sc-55fb52ed-0.gUeTO > div.sc-c0486d96-0.fxHvXX > div.fixed-bottom___StyledDiv-sc-c736ce1c-0.jXQEGO > div > button"
        );
        return btn ? btn : null;
        """
        buy_btn = driver.execute_script(js_buy)
        return buy_btn if buy_btn else False


class CaptchaVisible:
    """自定义等待条件: 验证码图片可见"""

    def __call__(self, driver):
        try:
            captcha_elem = driver.find_element(By.ID, "captchaImg")
            if captcha_elem.is_displayed():
                return captcha_elem
        except NoSuchElementException:
            pass
        return False


# ========== 验证码识别类 ==========
class CaptchaSolver:
    def __init__(self):
        self.ocr = ddddocr.DdddOcr(show_ad=False)

    def solve(self, captcha_element: WebElement) -> Optional[str]:
        try:
            image_bytes = captcha_element.screenshot_as_png
            res = self.ocr.classification(image_bytes)
            cleaned_text = "".join(c for c in res if c.isalnum()).upper()
            logger.info(f"🔍 ddddocr 识别结果: {cleaned_text}")
            return cleaned_text
        except Exception as e:
            logger.error(f"❌ 识别异常: {e}")
            return None


# ========== 座位监控类 ==========
class SeatMonitor:
    """座位监控器 - 负责自动抢座"""

    def __init__(self, driver, auto_scroll=True):
        self.driver = driver
        self.seat_queue = queue.Queue(maxsize=1)
        self.running = True
        self.driver_lock = threading.Lock()
        self.seat_selected = False
        self.auto_scroll = auto_scroll

        # 统计信息
        self.start_time = None
        self.end_time = None
        self.attempt_count = 0
        self.seat_update_count = 0

        # 优化连接池配置
        try:
            self.driver.command_executor._conn.pool_maxsize = 50
            self.driver.command_executor._conn.pool_block = False
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

    def check_submit_button_exists(self, timeout=0.5):
        """检查提交按钮"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                btn_parent = self.driver.find_elements(By.CSS_SELECTOR, ".button.btNext.on")
                if btn_parent:
                    target_a = self.driver.find_element(By.ID, "nextTicketSelection")
                    target_a.click()
                    logger.info("🚀 选座按钮已激活并成功点击!")
                    return True
            except:
                pass
            time.sleep(0.15)
        logger.info("⚠️ 等待按钮激活超时")
        return False

    def update_seats_continuously(self):
        """区域极速抢座线程 - 优化版"""
        logger.info("⚡ 区域极速抢座线程启动")
        self.running = True

        time.sleep(0.8)

        # 🔥 进入iframe (确保在正确的窗口上下文)
        try:
            self.driver.switch_to.default_content()
            self.driver.switch_to.frame("oneStopFrame")
            logger.info("✅ 已进入 oneStopFrame")
        except Exception as e:
            logger.error(f"❌ 进入iframe失败: {e}")
            return

        # 暴力展开所有区域列表
        try:
            unopened_sections = WebDriverWait(self.driver, 10).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".seat_name:not(.open)"))
            )
            if unopened_sections:
                for section in unopened_sections:
                    self.driver.execute_script("arguments[0].click();", section)
                    time.sleep(0.1)
                logger.info("✅ 成功进入选座!")
        except Exception as e:
            logger.warning(f"⚠️ 展开区域失败: {e}")

        target_sections = Config.TARGET_SECTIONS
        MAX_ATTEMPTS_PER_SEAT = 3  # 每个区域最多尝试5张票

        while self.running and not self.seat_selected:
            try:
                with self.driver_lock:
                    # 遍历每个目标区域
                    for section_name in target_sections:
                        logger.info(f"{'=' * 60}")
                        logger.info(f"🎯 开始处理区域: {section_name}")
                        logger.info(f"{'=' * 60}")

                        # 在当前区域尝试最多5张票
                        for seat_attempt in range(1, MAX_ATTEMPTS_PER_SEAT + 1):
                            if self.seat_selected:
                                break

                            logger.info(f"🔍 [{section_name}] 第 {seat_attempt}/{MAX_ATTEMPTS_PER_SEAT} 张票")

                            # 🔥 每次尝试都重新点击区域
                            xpath = f"//li[.//span[@class='area_tit' and contains(text(), '{section_name}')]]"

                            try:
                                area_li = self.driver.find_element(By.XPATH, xpath)
                                self.driver.execute_script("arguments[0].click();", area_li)
                                logger.info(f"   ✅ 重新选中区域 {section_name}")
                                ！！！！！！！！！！！没有选中需要重新打开列表！！！！！！！！！！！！！
                                time.sleep(0.65)  # 短暂等待区域加载
                            except Exception as e:
                                logger.warning(f"   ⚠️ 无法点击区域: {e}")
                                continue

                            # 扫描并点击座位
                            clicked = self.driver.execute_script(
                                """
                                const seats = document.querySelectorAll('rect:not([fill="#DDDDDD"])');
                                console.log(`找到候选座位总数: ${seats.length}`); 

                                for (const seat of seats) {
                                    const x = parseFloat(seat.getAttribute("x"));
                                    const y = parseFloat(seat.getAttribute("y"));
                                    const fill = seat.getAttribute("fill");
                                    const width = seat.getAttribute("width");

                                    if (x >= 128 && x <= 3000 && y >= 143 && y <= 2000 && width <= 12) {
                                        console.log(`🎯 命中目标: x=${x}, y=${y}, 颜色=${fill}`);

                                        seat.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                                        seat.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                                        seat.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));

                                        return [true, x, y];
                                    }
                                }
                                return [false, null, null];
                                """
                            )

                            clicked_flag, cx_clicked, cy_clicked = clicked

                            if not clicked_flag:
                                logger.info(f"   ⏳ 第{seat_attempt}张: 当前区域无可用座位")
                                time.sleep(0.5)
                                continue  # 继续尝试下一张

                            logger.info(f"   🎯 第{seat_attempt}张: 点击座位成功 (x={cx_clicked}, y={cy_clicked})")

                            # 等待提交按钮
                            submit_success = self.check_submit_button_exists(timeout=2)

                            if not submit_success:
                                logger.warning(f"   ⚠️ 第{seat_attempt}张: 提交按钮未激活")
                                time.sleep(0.2)
                                continue  # 继续尝试下一张

                            logger.info(f"   ✅ 第{seat_attempt}张: 提交按钮已点击")

                            # 检查是否成功跳转
                            try:
                                target_element = WebDriverWait(self.driver, 20).until(
                                    EC.presence_of_element_located(
                                        (By.XPATH, "//span[contains(@class, 'btWhite')]//span[text()='Previous']"))
                                )

                                logger.info(f"🎉🎉🎉 第{seat_attempt}张票抢座成功!")
                                logger.info(f"🎉 区域: {section_name}")
                                logger.info(f"🎉 座位: (x={cx_clicked}, y={cy_clicked})")
                                self.seat_selected = True
                                self.end_time = time.time()
                                return  # 成功,直接返回

                            except TimeoutException:
                                logger.warning(f"   ❌ 第{seat_attempt}张: 提交失败,未跳转到确认页")
                                time.sleep(0.5)
                                # 继续尝试下一张票

                        # 当前区域5张票都失败了
                        logger.info(f"⚠️ 区域 {section_name} 的5张票都未成功,切换到下一个区域")

                    # 所有区域都尝试完毕,重新开始循环
                    logger.info("⏳ 所有区域都已尝试,重新扫描...")
                    time.sleep(0.5)

            except Exception as e:
                logger.warning(f"⚠️ 区域抢座线程异常: {e}")
                import traceback
                logger.debug(traceback.format_exc())
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

        logger.info(f"🎯 区域模式抢座开始(最多 {max_time} 秒)")

        update_thread = threading.Thread(
            target=self.update_seats_continuously, daemon=True
        )
        update_thread.start()

        while time.time() - self.start_time < max_time:
            if self.seat_selected:
                logger.info("\n🎉🎉🎉 座位已成功选择并提交!")
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

        self.running = False
        self.end_time = time.time()
        logger.warning("❌ 超时未抢到目标区域座位")
        self.print_statistics()
        return None

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
            print("=" * 60 + "\n")

    def collect_available_seats(self, max_time=120):
        self.start_time = time.time()
        self.running = True
        self.seat_selected = False

        logger.info(f"🎯 区域模式抢座开始(最多 {max_time} 秒)")

        update_thread = threading.Thread(
            target=self.update_seats_continuously, daemon=True
        )
        update_thread.start()

        while time.time() - self.start_time < max_time:
            if self.seat_selected:
                logger.info("\n🎉🎉🎉 座位已成功选择并提交!")
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
    """监控并点击 Get ticket 按钮"""
    logger.info("🔍 开始监控 Get ticket 按钮")

    try:
        buy_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'reservationBtn')]")))
        safe_click(driver, buy_btn, method="native")
        logger.info("✅ 成功点击 Get ticket 按钮")
        return True
    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到 Get ticket 按钮")
        return False


def click_specific_date(driver, date_str, target_time, timeout=12000):
    """选择指定日期"""
    logger.info(f"🔍 等待日期 {date_str} 出现")
    try:
        btn_elem = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, f'li[data-perfday="{date_str}"] button')
            )
        )
        if btn_elem:
            safe_click(driver, btn_elem, method="native")
            return True

    except TimeoutException:
        logger.error(f"❌ 等待 {timeout}秒 未找到日期 {date_str}")
        return False


def solve_and_input_captcha(driver, timeout=300, retry_interval=0.8):
    """验证码识别并提交 - 🔥 处理弹窗"""
    solver = CaptchaSolver()
    logger.info("🔍 开始监控验证码(强制判定模式)")

    trys = 0

    # 🔥 等待弹窗出现并切换
    while trys < 100:
        time.sleep(0.1)
        try:
            # 检测新窗口
            WebDriverWait(driver, 0.5).until(lambda d: len(d.window_handles) > 1)

            # 切换到弹窗
            if switch_to_popup_window(driver, timeout=2):
                logger.info("✅ 已切换到购票弹窗")

                # 等待验证码出现
                WebDriverWait(driver, 1).until(CaptchaVisible())
                trys = trys + 1
                logger.info(f"🧩 捕捉到验证码弹窗,进入识别流程 (尝试 {trys})")
                break
        except TimeoutException:
            logger.info("⏳ 继续等待验证码...")

    start_time = time.time()
    have_try = 0

    while time.time() - start_time < timeout:
        try:
            captcha_elem = WebDriverWait(driver, 3).until(CaptchaVisible())

            text = solver.solve(captcha_elem)

            if text and len(text) == 6:
                input_box = wait_and_find(driver, By.ID, "label-for-captcha", 2, "clickable")
                if input_box:
                    input_box.clear()
                    input_box.send_keys(text)
                    have_try += 1
                    logger.info(f"✅ 验证码已输入: {text}")

                submit_btn = wait_and_find(driver, By.ID, "btnComplete", 2, "clickable")
                if submit_btn:
                    safe_click(driver, submit_btn)
                    have_try += 1
                    logger.info("验证码已提交")

                try:
                    WebDriverWait(driver, 2).until_not(
                        EC.visibility_of_element_located((By.ID, "captchaImg"))
                    )
                    logger.info("🎉 验证通过!弹窗已消失")
                    return True
                except TimeoutException:
                    logger.warning("❌ 验证码可能错误或过期,刷新重试")
                    try:
                        driver.find_element(By.ID, "btnReload").click()
                    except:
                        driver.execute_script("arguments[0].click();", captcha_elem)
                    time.sleep(retry_interval)
            else:
                logger.warning("❌ 验证码识别失败,刷新重试")
                try:
                    driver.find_element(By.ID, "btnReload").click()
                except:
                    pass
                time.sleep(retry_interval)

        except TimeoutException:
            if len(driver.find_elements(By.ID, "captchaImg")) == 0:
                logger.info("✅ 验证码弹窗已消失,判定为通过")
                return True
            time.sleep(retry_interval)
        except Exception as e:
            logger.error(f"⚠️ 识别循环异常: {e}")
            time.sleep(retry_interval)

    return False


def click_seat(driver, ticket_num, timeout=60):
    """增加票数并点击购买"""
    logger.info(f"🎟 默认为一张,点击NEXT按钮")
    try:
        book_button = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='nextPayment']/span",
            10,
            "clickable",
        )
        if book_button:
            book_button.click()
            logger.info("✅ 购买按钮点击成功")
            return True

        return False

    except Exception as e:
        logger.error(f"❌ NEXT按钮点击失败: {e}")
        return False


def click_check_now(driver, timeout=60):
    """填写购买信息并结账"""

    # 🔥 确保在正确的窗口和iframe中
    driver.switch_to.default_content()
    driver.switch_to.frame("oneStopFrame")
    logger.info("✅ 已进入 oneStopFrame")

    try:
        # 选择支付方式
        pay_choose = wait_and_find(
            driver,
            By.XPATH,
            "//*[@id='payMethodCode001']",
            timeout,
            "clickable",
        )
        if pay_choose:
            safe_click(driver, pay_choose, method="js")
            logger.info("🌍 选择支付方式")
        time.sleep(0.1)

        card_choose = wait_and_find(
            driver,
            By.XPATH,
            '//*[@id="cardCode"]',
            timeout,
            "clickable",
        )
        if card_choose:
            safe_click(driver, card_choose, method="js")
            logger.info("💳 已触发卡种选择")

        time.sleep(0.3)

        logger.info("💳 正在精准锁定银联支付 (FOREIGN_CHINABANK)...")

        driver.execute_script("""
            var select = document.getElementById('cardCode');
            if (select) {
                select.value = 'FOREIGN_CHINABANK';
                setCardContent('FOREIGN_CHINABANK');
                var hiddenInput = document.getElementById('cardCodeName');
                if (hiddenInput) {
                    hiddenInput.value = 'UnionPay';
                    hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
                }
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """)

        logger.info("✅ 支付参数已强制同步:FOREIGN_CHINABANK")

        # 填写手机号
        phone_input = wait_and_find(driver, By.XPATH, "//*[@id='tel']", 10)
        if phone_input:
            phone_input.clear()
            phone_input.send_keys(Config.PHONE_NUMBER)
            logger.info(f"✅ 手机号已填写: {Config.PHONE_NUMBER}")
        time.sleep(0.1)

        # 勾选同意条款
        checkbox = wait_and_find(
            driver,
            By.XPATH,
            '//*[@id="chkAgreeAll"]',
            10,
            "clickable",
        )
        if checkbox:
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                checkbox,
            )
            checkbox.click()
            logger.info("✅ 同意条款已勾选")
        time.sleep(0.2)

        # 点击支付按钮
        logger.info("⏳ 等待支付按钮激活...")

        try:
            WebDriverWait(driver, 10).until(
                lambda d: "on" in d.find_element(By.CSS_SELECTOR, "span.button.btNext").get_attribute("class")
            )

            logger.info("🔥 按钮已激活!执行最终支付点击...")

            driver.execute_script("document.getElementById('btnFinalPayment').click();")
            return True
        except Exception as e:
            logger.error("❌ 支付按钮未能激活,请检查手机号或条款勾选是否完成")

    except Exception as e:
        logger.error(f"❌ 填写购买信息失败: {e}")
        return False


# ========== 🔥 主worker函数 ==========
import ast


def worker(process_id: int, debug_port: int, target_date, target_time, target_sections):
    """
    工作进程
    参数:
        process_id: 进程ID
        debug_port: Chrome调试端口
        target_date: 目标日期
        target_time: 目标时间
        target_sections: 目标区域
    """
    # 配置
    Config.TARGET_DATE = target_date
    Config.TARGET_TIME = target_time
    if isinstance(target_sections, str):
        target_sections = ast.literal_eval(target_sections)
    Config.TARGET_SECTIONS = target_sections

    driver = None
    try:
        print(f"\n🚀 进程 {process_id} 启动")
        print(f"   端口: {debug_port}")
        print(f"   日期: {target_date}")
        print(f"   时间: {target_time}")
        print(f"   区域: {target_sections}")

        # 连接到已打开的浏览器
        driver = init_driver(debug_port)
        logger.info("✅ 浏览器连接成功")
        time.sleep(0.5)

        # ===== 步骤 1: 选择日期 =====
        if not click_specific_date(driver, Config.TARGET_DATE, Config.TARGET_TIME, timeout=Config.DATE_SELECT_TIMEOUT):
            raise Exception("❌ 日期选择失败")
        logger.info("✅ 日期选择成功")
        time.sleep(0.85)

        # ===== 步骤 2: 点击 get ticket 按钮 =====
        if not check_buy_now_button(driver, timeout=Config.BUY_NOW_TIMEOUT):
            logger.warning("⚠️ Get ticket 按钮未找到,继续下一步")

        # ===== 步骤 3: 验证码识别(会自动处理弹窗) =====
        if not solve_and_input_captcha(driver, timeout=Config.CAPTCHA_TIMEOUT):
            raise Exception("❌ 验证码验证失败")

        # ===== 步骤 4: 自动选座 =====
        monitor = SeatMonitor(driver, auto_scroll=True)
        result = monitor.collect_available_seats(max_time=Config.SEAT_SELECT_TIMEOUT)
        if result != "success":
            raise Exception("❌ 抢座失败")
        logger.info("✅ 座位选择成功")

        # ===== 步骤 5: 设置票数 =====
        if not click_seat(driver, Config.TICKET_COUNT, timeout=Config.TICKET_COUNT_TIMEOUT):
            raise Exception("❌ 票数设置失败")
        logger.info("✅ 票数设置成功")

        # ===== 步骤 6: 填写信息并结账 =====
        if not click_check_now(driver, timeout=Config.CHECKOUT_TIMEOUT):
            raise Exception("❌ 结账失败")
        logger.info("⏭️  跳过自动结账(需手动完成)")

        print("\n🎉🎉🎉 抢票流程完成!")

    except Exception as e:
        logger.error(f"❌ 进程 {process_id} 异常: {e}")
    finally:
        print(f"🟡 进程 {process_id} 执行完毕,浏览器保持打开")
        while True:
            time.sleep(5)


def main():
    """
    🔥 使用说明:
    1. 先手动打开3个Chrome窗口并登录
       窗口1: chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\chrome_profile_1"
       窗口2: chrome.exe --remote-debugging-port=9223 --user-data-dir="C:\chrome_profile_2"
       窗口3: chrome.exe --remote-debugging-port=9224 --user-data-dir="C:\chrome_profile_3"

    2. 在每个窗口中登录到Melon网站

    3. 运行此脚本
    """
    set_start_method("spawn", force=True)

    processes = []

    # ===== 每个窗口独立配置 =====
    configs = [
        (Config.DEBUG_PORTS[0], "20260130", "19:30", ['35', '38']),
        (Config.DEBUG_PORTS[1], "20260201", "16:00", ['36', '39']),
    ]

    print("=" * 60)
    print("🎫 Melon 多进程抢票系统")
    print("=" * 60)
    print("\n⚠️  请确保已经:")
    print("1. 打开了3个Chrome调试窗口 (端口: 9527, 9526)")
    print("2. 在每个窗口中已登录Melon账号")
    print("3. 导航到演出页面\n")
    input("按Enter键开始抢票...")

    for i, (port, date, time_, sections) in enumerate(configs, 1):
        p = Process(target=worker, args=(i, port, date, time_, sections))
        p.start()
        processes.append(p)
        time.sleep(1.5)

    for p in processes:
        p.join()


if __name__ == "__main__":
    main()