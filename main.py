import re
import os
import time
import threading
import datetime
import pytz
import requests
import schedule
import base64
import urllib.parse
import json
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from flask import Flask, send_file, request, jsonify

# --- 全局配置区 ---
SOURCE_URL = "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5"
BASE_URL = "http://play.sportsteam368.com"
OUTPUT_M3U_FILE = "/app/output/playlist.m3u"
OUTPUT_TXT_FILE = "/app/output/playlist.txt"
CACHE_FILE = "/app/output/stream_cache.json"  # 新增：用于存放已经抓取过的直链
TARGET_KEY = "ABCDEFGHIJKLMNOPQRSTUVWX"
# ------------------

app = Flask(__name__)

# ==========================================
# 本地缓存管理 (实现增量抓取)
# ==========================================
def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cache(cache_data):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

# ==========================================
# 内置轻量级 XXTEA 解密算法
# ==========================================
def str2long(s):
    v = []
    for i in range(0, len(s), 4):
        val = ord(s[i])
        if i + 1 < len(s): val |= ord(s[i+1]) << 8
        if i + 2 < len(s): val |= ord(s[i+2]) << 16
        if i + 3 < len(s): val |= ord(s[i+3]) << 24
        v.append(val)
    return v

def long2str(v):
    s = ""
    for val in v:
        s += chr(val & 0xff)
        s += chr((val >> 8) & 0xff)
        s += chr((val >> 16) & 0xff)
        s += chr((val >> 24) & 0xff)
    return s

def xxtea_decrypt(data, key):
    if not data: return ""
    v = str2long(data)
    k = str2long(key)
    while len(k) < 4: k.append(0)
    
    n = len(v) - 1
    if n < 1: return ""
    z = v[n]
    y = v[0]
    delta = 0x9E3779B9
    q = 6 + 52 // (n + 1)
    sum_val = (q * delta) & 0xffffffff

    while sum_val != 0:
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
            y = v[p] = (v[p] - mx) & 0xffffffff
        p = 0
        z = v[n]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
        y = v[0] = (v[0] - mx) & 0xffffffff
        sum_val = (sum_val - delta) & 0xffffffff

    m = v[-1]
    limit = (len(v) - 1) << 2
    if m < limit - 3 or m > limit: return None
    return long2str(v)[:m]

def decrypt_id_to_url(encrypted_id):
    try:
        decoded_id = urllib.parse.unquote(encrypted_id)
        pad = 4 - (len(decoded_id) % 4)
        if pad != 4: decoded_id += "=" * pad
        bin_str = base64.b64decode(decoded_id).decode('latin1')
        decrypted_bin = xxtea_decrypt(bin_str, TARGET_KEY)
        if decrypted_bin:
            json_str = decrypted_bin.encode('latin1').decode('utf-8')
            return json.loads(json_str).get("url")
    except Exception:
        pass
    return None

def get_html_from_js(js_url):
    try:
        response = requests.get(js_url, timeout=10)
        response.encoding = 'utf-8'
        return "".join(re.findall(r"document\.write\('(.*?)'\);", response.text))
    except Exception:
        return ""

def extract_from_resource_tree(page):
    for frame in page.frames:
        if 'paps.html?id=' in frame.url:
            return frame.url.split('paps.html?id=')[-1]
    for url in page.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)"):
        if 'paps.html?id=' in url:
            return url.split('paps.html?id=')[-1]
    return None

# ==========================================
# 静默版增量爬虫主流程
# ==========================================
def generate_playlist():
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(tz)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Task started.")
    
    html_content = get_html_from_js(SOURCE_URL)
    if not html_content: 
        print("Task aborted: Source unreadable.")
        return

    soup = BeautifulSoup(html_content, 'html.parser')
    matches = soup.select('ul.item.play')
    
    if len(matches) == 0:
        print("Task aborted: No items found.")
        return

    current_year = now.year
    m3u_lines = ["#EXTM3U\n"]
    txt_dict = {} 
    success_count = 0

    # 加载已有的缓存
    stream_cache = load_cache()
    # 存放本轮有效的缓存（实现自动清理过期数据）
    new_cache = {}

    # 精准时间区间计算：昨天 20:00 到 今天 23:59:59
    today_date = now.date()
    yesterday_date = today_date - datetime.timedelta(days=1)
    start_dt = tz.localize(datetime.datetime.combine(yesterday_date, datetime.time(20, 0)))
    end_dt = tz.localize(datetime.datetime.combine(today_date, datetime.time(23, 59, 59)))

    try:
        with sync_playwright() as p:
            browser = None
            page = None
            
            for match in matches:
                try:
                    time_tag = match.find('li', class_='lab_time')
                    if not time_tag: continue
                    
                    match_time_raw = time_tag.text.strip() 
                    match_time_str = f"{current_year}-{match_time_raw}"
                    match_dt = tz.localize(datetime.datetime.strptime(match_time_str, "%Y-%m-%d %H:%M"))
                    
                    # 过滤：仅保留昨天20点至今晚24点之间的比赛
                    if not (start_dt <= match_dt <= end_dt):
                        continue
                    
                    league_tag = match.find('li', class_='lab_events')
                    league_name = league_tag.find('span', class_='name').text.strip() if league_tag else "综合"
                    group_name = f"JRS-{league_name}"
                    home_team = match.find('li', class_='lab_team_home').find('strong').text.strip()
                    away_team = match.find('li', class_='lab_team_away').find('strong').text.strip()
                    base_channel_name = f"{match_time_raw} {home_team} VS {away_team}"

                    channel_li = match.find('li', class_='lab_channel')
                    target_link = None
                    if channel_li:
                        for a_tag in channel_li.find_all('a', href=True):
                            href_val = a_tag['href']
                            if 'http' in href_val and '/play/' in href_val:
                                target_link = href_val
                                break
                    
                    if not target_link: continue

                    # 如果没有启动浏览器，这里先不启动，因为有可能全命中缓存
                    if browser is None:
                        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
                        page = browser.new_page()

                    try:
                        page.goto(target_link, wait_until="load", timeout=15000)
                        page.wait_for_timeout(2000)
                        detail_html = page.content()
                    except Exception:
                        continue

                    detail_soup = BeautifulSoup(detail_html, 'html.parser')
                    target_lines = []
                    
                    all_lines = detail_soup.select('a[data-play]')
                    for a in all_lines:
                        a_text = a.text.strip()
                        data_play = a.get('data-play')
                        if data_play and ('高清' in a_text or '蓝光' in a_text or '原画' in a_text):
                            target_lines.append({"name": a_text, "path": data_play})
                    
                    if not target_lines: 
                        continue

                    for line_info in target_lines:
                        final_url = urllib.parse.urljoin(target_link, line_info['path'])
                        specific_channel_name = f"{base_channel_name} - {line_info['name']}"
                        
                        # --- 核心：缓存跳过机制 ---
                        if specific_channel_name in stream_cache:
                            cached_url = stream_cache[specific_channel_name]
                            new_cache[specific_channel_name] = cached_url # 续期保留
                            
                            m3u_lines.append(f'#EXTINF:-1 tvg-name="{specific_channel_name}" group-title="{group_name}",{specific_channel_name}\n{cached_url}\n')
                            if group_name not in txt_dict: txt_dict[group_name] = []
                            txt_dict[group_name].append(f"{specific_channel_name},{cached_url}")
                            
                            success_count += 1
                            continue # 直接跳过后续复杂的无头浏览器渲染！
                        # -------------------------
                        
                        try:
                            page.goto(final_url, wait_until="load", timeout=15000)
                            page.wait_for_timeout(3000)
                            
                            encrypted_id = extract_from_resource_tree(page)

                            if encrypted_id:
                                real_stream_url = decrypt_id_to_url(encrypted_id)
                                if real_stream_url:
                                    # 抓取成功，存入新缓存
                                    new_cache[specific_channel_name] = real_stream_url
                                    
                                    m3u_lines.append(f'#EXTINF:-1 tvg-name="{specific_channel_name}" group-title="{group_name}",{specific_channel_name}\n')
                                    m3u_lines.append(f'{real_stream_url}\n')
                                    
                                    if group_name not in txt_dict: txt_dict[group_name] = []
                                    txt_dict[group_name].append(f"{specific_channel_name},{real_stream_url}")
                                    
                                    success_count += 1
                        except Exception:
                            continue

                except Exception:
                    continue
            
            if browser:
                browser.close()
                
    except Exception as e:
        print(f"Task encountered an error: {e}")

    # 保存新的缓存文件（过期的比赛因为没有被加入 new_cache，会被自动丢弃，防止文件无限变大）
    save_cache(new_cache)

    os.makedirs(os.path.dirname(OUTPUT_M3U_FILE), exist_ok=True)
    if success_count == 0:
        m3u_lines.append("# 当前时间段无可用直播\n")
        txt_dict["System"] = ["No streams,http://127.0.0.1/error.mp4"]

    tmp_m3u = OUTPUT_M3U_FILE + ".tmp"
    tmp_txt = OUTPUT_TXT_FILE + ".tmp"

    with open(tmp_m3u, 'w', encoding='utf-8') as f:
        f.writelines(m3u_lines)
    with open(tmp_txt, 'w', encoding='utf-8') as f:
        for group, channels in txt_dict.items():
            f.write(f"{group},#genre#\n")
            for ch in channels: f.write(f"{ch}\n")
            
    os.replace(tmp_m3u, OUTPUT_M3U_FILE)
    os.replace(tmp_txt, OUTPUT_TXT_FILE)
    
    finish_time = datetime.datetime.now(tz)
    print(f"[{finish_time.strftime('%Y-%m-%d %H:%M:%S')}] Task finished. Extracted {success_count} lines.")


# ==========================================
# 极简 Web 路由
# ==========================================
@app.route('/')
def index():
    return "Service OK", 200

@app.route('/m3u')
def get_m3u():
    try: return send_file(OUTPUT_M3U_FILE, mimetype='application/vnd.apple.mpegurl', as_attachment=False)
    except FileNotFoundError: return "File not found", 404

@app.route('/txt')
def get_txt():
    try: return send_file(OUTPUT_TXT_FILE, mimetype='text/plain', as_attachment=False)
    except FileNotFoundError: return "File not found", 404

@app.route('/debug')
def debug_url():
    target_url = request.args.get('url')
    if not target_url: return "Bad Request", 400
    debug_info = {"target_url": target_url, "extracted_token": None, "decrypted_url": None, "frames_found": [], "resources_found": []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = browser.new_page()
            page.goto(target_url, wait_until="load", timeout=15000)
            page.wait_for_timeout(3000) 
            
            for f in page.frames:
                debug_info["frames_found"].append(f.url)
                if 'paps.html?id=' in f.url: debug_info["extracted_token"] = f.url.split('paps.html?id=')[-1]
            
            resource_urls = page.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)")
            debug_info["resources_found"] = resource_urls
            
            if not debug_info["extracted_token"]:
                for url in resource_urls:
                    if 'paps.html?id=' in url: debug_info["extracted_token"] = url.split('paps.html?id=')[-1]; break
            
            if debug_info["extracted_token"]: debug_info["decrypted_url"] = decrypt_id_to_url(debug_info["extracted_token"])
            browser.close()
    except Exception as e: debug_info["error"] = str(e)
    return jsonify(debug_info)

def run_scheduler():
    schedule.every(1).hours.do(generate_playlist)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUTPUT_M3U_FILE), exist_ok=True)
    if not os.path.exists(OUTPUT_M3U_FILE):
        with open(OUTPUT_M3U_FILE, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n#EXTINF:-1,系统正在初始化抓取，请稍后...\nhttp://127.0.0.1/loading.mp4\n")
    if not os.path.exists(OUTPUT_TXT_FILE):
        with open(OUTPUT_TXT_FILE, 'w', encoding='utf-8') as f:
            f.write("系统提示,#genre#\n初始化抓取中...,http://127.0.0.1/loading.mp4\n")

    threading.Thread(target=generate_playlist, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    app.run(host="0.0.0.0", port=80)
