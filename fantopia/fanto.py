# fantopia网页自动点击
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
import cv2
import numpy as np
import pytesseract
from PIL import Image
import io
import base64
import logging
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
import time
import threading
import queue

# ========== 初始化日志 ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

options = webdriver.ChromeOptions()
options.debugger_address = "127.0.0.1:9222"  # 已打开的浏览器端口

# "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:/chrome_debug_profile"
# 使用 Service 包裹 ChromeDriverManager 自动下载匹配的 chromedriver
service = Service(
    r"chromedriver-win64\chromedriver.exe"
)

driver = webdriver.Chrome(service=service, options=options)
print("请手动打开目标页面并加载完成后，按回车继续...")

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

def click_button_repeatedly(driver, xpath, times=200, interval=0.5):
    """连续点击指定 XPath 按钮"""
    wait = WebDriverWait(driver, 240)
    for i in range(times):
        try:
            # 等待按钮可点击
            button = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
            button.click()
            print(f"✅ 第 {i+1} 次点击成功")
            time.sleep(interval)
        except Exception as e:
            print(f"⚠️ 第 {i+1} 次点击失败: {e}")
            time.sleep(1)

# 示例用法
if __name__ == "__main__":
    click_button_repeatedly(driver, "//*[@id='app']/main/div[1]/div/div/div[2]/div/div[2]/div/div/div[4]/div[2]/div[1]/button/div", times=20, interval=0.3)
