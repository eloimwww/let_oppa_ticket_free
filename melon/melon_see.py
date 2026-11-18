# ================= 多进程入口壳 =================
import os
import time
from multiprocessing import Process, set_start_method
# 可以进行选区
# =================================================
# ======= 以下开始：你的原始代码（原样） ========
# =================================================

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
    # ===== 从环境变量读取 =====
    # TARGET_DATE = os.getenv("TARGET_DATE", "20260222")
    # TARGET_TIME = os.getenv("TARGET_TIME", "17:00")
    # CHROME_DEBUG_PORT = os.getenv("CHROME_DEBUG_PORT", "127.0.0.1:9526")
    # TARGET_SECTIONS = os.getenv("TARGET_SECTIONS",['35', '38'])

    TICKET_COUNT = 1
    PHONE_NUMBER = ""
    CHROMEDRIVER_PATH = r"chromedriver-win64\chromedriver.exe"


    PREFERRED_SEATS = []

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


# ========== WebDriver 初始化（⚠️ 唯一修改点） ==========
def init_driver():
    options = webdriver.ChromeOptions()

    # ⭐ 每个进程一个独立 profile

    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(Config.CHROMEDRIVER_PATH)

    driver = webdriver.Chrome(service=service, options=options, keep_alive=False)
    logger.info("✅ WebDriver 初始化成功（非 debug）")
    return driver


# =================================================
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
        return True
    except Exception as e:
        logger.warning(f"⚠️ 座位点击失败: {e}")
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


# ========== 自定义等待条件 ==========
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
            captcha_elem = driver.find_element(By.ID, "captchaImg")
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
import ddddocr


class CaptchaSolver:
    def __init__(self):
        # show_ad=False 可以关闭控制台的广告输出
        self.ocr = ddddocr.DdddOcr(show_ad=False)

    def solve(self, captcha_element: WebElement) -> Optional[str]:
        try:
            # 直接获取图片的二进制数据
            image_bytes = captcha_element.screenshot_as_png

            # ddddocr 直接支持二进制输入
            res = self.ocr.classification(image_bytes)

            # 格式化结果：转大写，过滤掉可能的非字母字符
            cleaned_text = "".join(c for c in res if c.isalnum()).upper()

            logger.info(f"🔍 ddddocr 识别结果: {cleaned_text}")
            return cleaned_text
        except Exception as e:
            logger.error(f"❌ 识别异常: {e}")
            return None


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

    def set_page_scale(self, scale=4):
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "#__next > div > div > main > div.Seat_pageContent__Emqwu > div > section > div > div",
                )
            )
        )

        js = f"""
        const el = document.querySelector("#__next > div > div > main > div.Seat_pageContent__Emqwu > div > section > div > div");
        if (el) {{
            el.style.transform = "scale({scale})";
            el.style.transformOrigin = "top left";
        }}
        """
        self.driver.execute_script(js)

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

    def is_preferred_seat(self, seat):
        """检查是否为优先座位"""
        if not self.preferred_seats:
            return False
        seat_id = seat.get_attribute("id")
        return seat_id in self.preferred_seats

    def sort_seats_by_preference(self, seats):
        """按优先级排序座位"""
        preferred = []
        normal = []

        for seat in seats:
            if self.is_preferred_seat(seat):
                preferred.append(seat)
            else:
                normal.append(seat)

        preferred_sorted = sorted(preferred, key=lambda s: float(s.get_attribute("cy")))
        normal_sorted = sorted(normal, key=lambda s: float(s.get_attribute("cy")))

        return preferred_sorted + normal_sorted

    def update_seats_continuously(self):
        logger.info("⚡ 区域极速抢座线程启动")

        while self.running and not self.seat_selected:
            try:
                # 🔥 唯一入口：完整流程
                success = self.try_select_target_area_seat(max_attempts=30)

                if success:
                    logger.info("🎉 区域座位已成功提交")
                    break

                time.sleep(0.8)

            except Exception as e:
                logger.warning(f"⚠️ 区域抢座线程异常: {e}")
                time.sleep(0.3)

        logger.info("✅ 区域抢座线程结束")

        logger.info("✅ 极速座位监控结束")

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

    def check_submit_button_exists(self, timeout=0.5):
        """检查提交按钮 - 优化超时时间"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            # 查找带有 "on" class 的按钮父级
            # CSS 选择器：.button.btNext.on 意味着同时拥有这三个 class
            try:
                btn_parent = self.driver.find_elements(By.CSS_SELECTOR, ".button.btNext.on")
                if btn_parent:
                    # 找到了激活状态的按钮，执行点击
                    target_a = self.driver.find_element(By.ID, "nextTicketSelection")
                    target_a.click()
                    logger.info("🚀 选座按钮已激活并成功点击！")
                    return True
            except:
                pass
            time.sleep(0.15)  # 快速轮询
        logger.info("⚠️ 等待按钮激活超时")

    def update_seats_continuously(self):
        """区域极速抢座线程"""
        logger.info("⚡ 区域极速抢座线程启动")
        self.running = True

        time.sleep(0.8)
        # 1. 核心动作：推开这扇门 (进入 oneStopFrame)
        self.driver.switch_to.default_content()  # 先回主页面
        self.driver.switch_to.frame("oneStopFrame")
        logger.info("✅ 已进入 oneStopFrame")

        # 1. 暴力展开：把所有能点开的区域列表全部点开
        unopened_sections = WebDriverWait(self.driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".seat_name:not(.open)"))
        )
        if unopened_sections:
            for section in unopened_sections:
                self.driver.execute_script("arguments[0].click();", section)
                time.sleep(0.1)  # 给一点点渲染时间
            logger.info("✅  成功进入选座！")
            target_sections = ['35', '38']

        while self.running and not self.seat_selected:
            try:
                with self.driver_lock:
                    url_before = self.driver.current_url

                    for section_name in target_sections:
                        logger.info(f"🔍 正在尝试匹配区域: {section_name}")

                        # 3. 动态构建 XPath
                        # 逻辑：找到包含该文字的 span，然后定位到它的父级 li
                        xpath = f"//li[.//span[@class='area_tit' and contains(text(), '{section_name}')]]"

                        try:
                            # 寻找该区域对应的 li 容器
                            area_li = self.driver.find_element(By.XPATH, xpath)

                            # 4. 执行点击（触发 li 上的 selectedBlock）
                            self.driver.execute_script("arguments[0].click();", area_li)
                            logger.info(f"🎯 已选中 {section_name}，准备扫描座位...")
                            time.sleep(1)

                            # 扫描目标区域并点击第一个可用座位
                            clicked = self.driver.execute_script(
                                """
                                // 1. 获取所有非灰色的 rect 元素
                                const seats = document.querySelectorAll('rect:not([fill="#DDDDDD"])');
                                console.log(`找到候选座位总数: ${seats.length}`); 

                                for (const seat of seats) {
                                    const x = parseFloat(seat.getAttribute("x"));
                                    const y = parseFloat(seat.getAttribute("y"));
                                    const fill = seat.getAttribute("fill");
                                    const width = seat.getAttribute("width");

                                    // 2. 检查坐标范围
                                    if (x >= 128 && x <= 3000 && y >= 143 && y <= 2000 && width <= 12) {
                                        console.log(`🎯 命中目标: x=${x}, y=${y}, 颜色=${fill}`);

                                        // 模拟点击
                                        seat.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                                        seat.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                                        seat.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));

                                        return [true, x, y]; // 返回给 Python
                                    }
                                }
                                return [false, null, null];
                                """
                            )
                            # time.sleep(2)

                            clicked_flag, cx_clicked, cy_clicked = clicked
                            if clicked_flag:
                                logger.info(
                                    f"🎯 点击目标区域座位成功 (cx={cx_clicked}, cy={cy_clicked})")
                                break
                            else:
                                logger.info("⏳ 当前目标区域无可用座位")

                            time.sleep(0.85)
                        except:
                            pass

                    # # 检查弹窗（可能被别人抢走）
                    # popup_btn = self.check_popup_exists(timeout=0.5)
                    # if popup_btn:
                    #     logger.warning("❌ 座位被占，关闭弹窗")
                    #     safe_click(self.driver, popup_btn, method="js")
                    #     continue

                    # 等待提交按钮出现
                    submit_btn = self.check_submit_button_exists(timeout=2)
                    if not submit_btn:
                        logger.warning("⚠️ 未检测到提交按钮，重试")
                        time.sleep(0.1)
                        continue

                    # 点击提交
                    # safe_click(self.driver, submit_btn, method="js")
                    # time.sleep(0.1)

                    # # 提交后检查弹窗
                    # popup_btn = self.check_popup_exists(timeout=0.35)
                    # if popup_btn:
                    #     logger.warning("❌ 提交失败，关闭弹窗")
                    #     safe_click(self.driver, popup_btn, method="js")
                    #     continue

                    # 页面跳转为成功
                    try:
                        target_element = WebDriverWait(self.driver, 5).until(
                            EC.presence_of_element_located(
                                (By.XPATH, "//span[contains(@class, 'btWhite')]//span[text()='Previous']"))
                        )

                        logger.info("🎉 判定成功：已进入支付/确认页面！")
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
        buy_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@id='ticketing_process_box']/div/div[2]/div/div[2]/button")))
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
        # 等待按钮加载
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


def solve_and_input_captcha(driver, timeout=300, retry_interval=0.8):
    """验证码识别并提交 - 修正逻辑"""
    solver = CaptchaSolver()
    logger.info("🔍 开始监控验证码（强制判定模式）")
    # time.sleep(0.45)
    trys = 0

    while trys < 100:
        time.sleep(0.1)
        try:
            WebDriverWait(driver, 0.5).until(lambda d: len(d.window_handles) > 1)
            driver.switch_to.window(driver.window_handles[-1])
            logger.info(f"➡️ 已切换到购票窗口: {driver.title}")
            WebDriverWait(driver, 1).until(CaptchaVisible())
            trys = trys + 1
            print(trys)
            logger.info("🧩 捕捉到验证码弹窗，进入识别流程")
            break
        except TimeoutException:
            # 此时再检查是否真的已经进入了选座页面
            logger.info("✅ 继续检查")

    start_time = time.time()
    have_try = 0
    while time.time() - start_time < timeout:
        try:
            # 再次确认元素是否还在
            captcha_elem = WebDriverWait(driver, 3).until(CaptchaVisible())

            # 如果图片加载了，但 src 还是空的或旧的，等它更新
            text = solver.solve(captcha_elem)

            if text and len(text) == 6:  # 增加长度判定减少误报
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
                    logger.info("🎉 验证通过！弹窗已消失")
                    return True
                except TimeoutException:
                    logger.warning("❌ 验证码可能错误或过期，刷新重试")
                    # 点击刷新按钮
                    try:
                        driver.find_element(By.ID, "btnReload").click()
                    except:
                        driver.execute_script("arguments[0].click();", captcha_elem)
                    time.sleep(retry_interval)
            else:
                logger.warning("❌ 验证码可能错误或过期，刷新重试")
                # 点击刷新按钮
                try:
                    driver.find_element(By.ID, "btnReload").click()
                except:
                    driver.execute_script("arguments[0].click();", captcha_elem)
                time.sleep(retry_interval)

        except TimeoutException:
            # 如果此时弹窗消失了，说明可能已经过了
            if len(driver.find_elements(By.ID, "captchaImg")) == 0:
                logger.info("✅ 验证码弹窗已消失，判定为通过")
                return True
            time.sleep(retry_interval)
        except Exception as e:
            logger.error(f"⚠️ 识别循环异常: {e}")
            time.sleep(retry_interval)

    return False


def click_seat(driver, ticket_num, timeout=60):
    """增加票数并点击购买"""
    logger.info(f"🎟 默认为一张，点击NEXT按钮")
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

    driver.switch_to.default_content()  # 先回主页面
    driver.switch_to.frame("oneStopFrame")
    logger.info("✅ 已进入 oneStopFrame")
    try:
        # 选择国家
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

        # 使用 JS 直接操作 Select 元素及其关联逻辑
        driver.execute_script("""
            var select = document.getElementById('cardCode');
            if (select) {
                // 1. 设置下拉框的值为银联对应的真实 Value
                select.value = 'FOREIGN_CHINABANK';

                // 2. 强制触发网页自带的同步函数 (非常关键！)
                setCardContent('FOREIGN_CHINABANK');

                // 3. 补刀：手动同步隐藏域并派发改变事件
                var hiddenInput = document.getElementById('cardCodeName');
                if (hiddenInput) {
                    hiddenInput.value = 'UnionPay';
                    hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
                }

                // 4. 派发 Select 的 change 事件确保 UI 响应
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """)

        logger.info("✅ 支付参数已强制同步：FOREIGN_CHINABANK")

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

        # 点击Step2 NEXT按钮
        logger.info("⏳ 等待支付按钮激活 (等待 class 出现 'on')...")

        try:
            # 1. 显式等待：直到 span 标签的 class 中包含了 'on'
            # 这里的定位器直接指向父级 span
            WebDriverWait(driver, 10).until(
                lambda d: "on" in d.find_element(By.CSS_SELECTOR, "span.button.btNext").get_attribute("class")
            )

            logger.info("🔥 按钮已激活！执行最终支付点击...")

            driver.execute_script("document.getElementById('btnFinalPayment').click();")
            return True
        except Exception as e:
            logger.error("❌ 支付按钮未能激活，请检查手机号或条款勾选是否完成")

    except Exception as e:
        logger.error(f"❌ 填写购买信息失败: {e}")
        return False


# ========== 原 main → worker（⚠️ 第二个修改点） ==========
import ast
def worker(process_id: int, target_date, target_time, target_sections):
    # ===== 初始化配置 =====
    Config.TARGET_DATE = target_date
    Config.TARGET_TIME = target_time
    if isinstance(target_sections, str):
        target_sections = ast.literal_eval(target_sections)
    Config.TARGET_SECTIONS = target_sections

    driver = None
    try:

        print(f"\n🚀 进程 {process_id} 启动 (日期={target_date}, 时间={target_time}, 区域={target_sections})")
        driver = init_driver()

        logger.info("✅ 浏览器连接成功")

        # ===== 步骤 1: 选择日期和时间 =====
        if not click_specific_date(driver, Config.TARGET_DATE, Config.TARGET_TIME, timeout=Config.DATE_SELECT_TIMEOUT):
            raise Exception("❌ 日期选择失败")
        logger.info("✅ 日期和时间选择成功")
        time.sleep(0.85)

        # ===== 步骤 2: 点击 get ticket 按钮 =====
        if not check_buy_now_button(driver, timeout=Config.BUY_NOW_TIMEOUT):
            logger.warning("⚠️ Buy now 按钮未找到，继续下一步")

        # ===== 步骤 3: 验证码识别 =====
        if not solve_and_input_captcha(driver, timeout=Config.CAPTCHA_TIMEOUT):
            raise Exception("❌ 验证码验证失败")

        # ===== 步骤 4: 自动选座 =====
        monitor = SeatMonitor(driver, preferred_seats=Config.PREFERRED_SEATS, auto_scroll=True)
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
        logger.info("⏭️  跳过自动结账（需手动完成）")

        print("\n🎉🎉🎉 抢票流程完成！")

    except Exception as e:
        logger.error(f"❌ 进程 {process_id} 异常: {e}")
    finally:
        print(f"🟡 进程 {process_id} 执行完毕，浏览器保持打开")
        while True:
            time.sleep(5)


def main():
    set_start_method("spawn", force=True)

    processes = []

    # ===== 每个窗口独立配置 =====
    configs = [
        ("20260221", "18:00", ['35', '38']),
        ("20260222", "19:00", ['36', '39']),
        ("20260223", "20:00", ['37', '40']),
    ]

    print("========== 启动 3 个抢票窗口 ==========")

    for i, (date, time_, sections) in enumerate(configs, 1):
        p = Process(target=worker, args=(i, date, time_, sections))
        p.start()
        processes.append(p)
        time.sleep(1.5)

    for p in processes:
        p.join()


if __name__ == "__main__":
    main()
