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
from flask import Flask, send_file, render_template_string, request, jsonify

# --- 全局配置区 ---
SOURCE_URL = "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5"
OUTPUT_M3U_FILE = "/app/output/playlist.m3u"
OUTPUT_TXT_FILE = "/app/output/playlist.txt"
TARGET_KEY = "ABCDEFGHIJKLMNOPQRSTUVWX"
# ------------------

app = Flask(__name__)
last_update_time = "尚未更新"

# 用于在网页端实时显示抓取状态
crawler_status = {
    "total_matches": 0,
    "in_time_matches": 0,
    "success_lines": 0,
    "current_action": "等待启动..."
}

# ==========================================
# 核心一：内置轻量级 XXTEA 解密算法
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
    """Base64解码 -> XXTEA解密 -> 提取真实流链接"""
    try:
        decoded_id = urllib.parse.unquote(encrypted_id)
        pad = 4 - (len(decoded_id) % 4)
        if pad != 4: decoded_id += "=" * pad
        bin_str = base64.b64decode(decoded_id).decode('latin1')
        decrypted_bin = xxtea_decrypt(bin_str, TARGET_KEY)
        if decrypted_bin:
            json_str = decrypted_bin.encode('latin1').decode('utf-8')
            return json.loads(json_str).get("url")
    except Exception as e:
        print(f"解密出错: {e}")
    return None

# ==========================================
# 核心二：底层资产提取逻辑
# ==========================================
def get_html_from_js(js_url):
    try:
        response = requests.get(js_url, timeout=10)
        response.encoding = 'utf-8'
        return "".join(re.findall(r"document\.write\('(.*?)'\);", response.text))
    except Exception:
        return ""

def extract_from_resource_tree(page):
    # 1. 查 Frame 嵌套
    for frame in page.frames:
        if 'paps.html?id=' in frame.url:
            return frame.url.split('paps.html?id=')[-1]
    # 2. 查浏览器性能加载树
    for url in page.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)"):
        if 'paps.html?id=' in url:
            return url.split('paps.html?id=')[-1]
    return None

# ==========================================
# 核心三：爬虫主流程
# ==========================================
def generate_playlist():
    global last_update_time, crawler_status
    crawler_status["current_action"] = "正在获取赛程列表..."
    crawler_status["success_lines"] = 0
    crawler_status["in_time_matches"] = 0
    
    html_content = get_html_from_js(SOURCE_URL)
    if not html_content: 
        crawler_status["current_action"] = "获取赛程失败，等待重试"
        return

    soup = BeautifulSoup(html_content, 'html.parser')
    matches = soup.find_all('ul', class_='item play d-touch active')
    crawler_status["total_matches"] = len(matches)
    
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(tz)
    current_year = now.year
    
    m3u_lines = ["#EXTM3U\n"]
    txt_dict = {} 

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = browser.new_page()
            
            for match in matches:
                try:
                    time_tag = match.find('li', class_='lab_time')
                    if not time_tag: continue
                    
                    match_time_raw = time_tag.text.strip() 
                    match_time_str = f"{current_year}-{match_time_raw}"
                    match_dt = tz.localize(datetime.datetime.strptime(match_time_str, "%Y-%m-%d %H:%M"))
                    
                    # 时间过滤器：前后 12 小时
                    if abs((match_dt - now).total_seconds()) / 3600 > 12:
                        continue
                    
                    crawler_status["in_time_matches"] += 1
                    
                    # 组装频道名称与分组
                    league_tag = match.find('li', class_='lab_events')
                    league_name = league_tag.find('span', class_='name').text.strip() if league_tag else "综合"
                    group_name = f"JRS-{league_name}"
                    home_team = match.find('li', class_='lab_team_home').find('strong').text.strip()
                    away_team = match.find('li', class_='lab_team_away').find('strong').text.strip()
                    base_channel_name = f"{match_time_raw} {home_team} VS {away_team}"

                    # 寻找主播放页链接
                    channel_li = match.find('li', class_='lab_channel')
                    target_link = None
                    if channel_li:
                        for a_tag in channel_li.find_all('a', href=True):
                            href_val = a_tag['href']
                            if 'http' in href_val and '/play/' in href_val:
                                target_link = href_val
                                break
                    
                    if not target_link: continue

                    # 伪装请求主播放页，防止被盾拦截导致抓不到 data-play
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
                    detail_resp = requests.get(target_link, headers=headers, timeout=10)
                    detail_soup = BeautifulSoup(detail_resp.text, 'html.parser')
                    
                    # 收集所有高质量线路
                    target_lines = []
                    for a in detail_soup.find_all('a', class_='item ok me'):
                        a_text = a.text.strip()
                        data_play = a.get('data-play')
                        if data_play and ('高清' in a_text or '蓝光' in a_text or '原画' in a_text):
                            target_lines.append({"name": a_text, "path": data_play})
                    
                    # 遍历解析每一条找到的高质量线路
                    for line_info in target_lines:
                        # 安全拼接 URL
                        final_url = urllib.parse.urljoin(target_link, line_info['path'])
                        specific_channel_name = f"{base_channel_name} - {line_info['name']}"
                        crawler_status["current_action"] = f"正在解析: {specific_channel_name}"
                        
                        try:
                            # 强力延时：保证多层 iframe 彻底挂载
                            page.goto(final_url, wait_until="load", timeout=15000)
                            page.wait_for_timeout(3000)
                            
                            encrypted_id = extract_from_resource_tree(page)

                            if encrypted_id:
                                real_stream_url = decrypt_id_to_url(encrypted_id)
                                if real_stream_url:
                                    # 写入 M3U
                                    m3u_lines.append(f'#EXTINF:-1 tvg-name="{specific_channel_name}" group-title="{group_name}",{specific_channel_name}\n')
                                    m3u_lines.append(f'{real_stream_url}\n')
                                    # 写入 TXT 缓存
                                    if group_name not in txt_dict: txt_dict[group_name] = []
                                    txt_dict[group_name].append(f"{specific_channel_name},{real_stream_url}")
                                    
                                    crawler_status["success_lines"] += 1
                                    print(f"✅ 成功入库: {specific_channel_name}")
                        except Exception as e:
                            print(f"❌ 线路失败: {specific_channel_name} - {e}")
                            continue

                except Exception as e:
                    print(f"解析某场比赛出错: {e}")
                    continue
            
            browser.close()
    except Exception as e:
        crawler_status["current_action"] = f"严重错误: {e}"

    # 结果落盘
    os.makedirs(os.path.dirname(OUTPUT_M3U_FILE), exist_ok=True)
    if crawler_status["success_lines"] == 0:
        # 如果什么都没抓到，留个防空提示，避免播放器报错
        m3u_lines.append("# 当前时间段没有抓取到符合条件的直播源\n")
        txt_dict["系统提示"] = ["当前无比赛或全部解析失败,http://127.0.0.1/error.mp4"]

    with open(OUTPUT_M3U_FILE, 'w', encoding='utf-8') as f:
        f.writelines(m3u_lines)
    with open(OUTPUT_TXT_FILE, 'w', encoding='utf-8') as f:
        for group, channels in txt_dict.items():
            f.write(f"{group},#genre#\n")
            for ch in channels: f.write(f"{ch}\n")
    
    last_update_time = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    crawler_status["current_action"] = "抓取完成，等待下一次运行 (休眠中)"

# ==========================================
# 核心四：Web 管理与 API
# ==========================================
@app.route('/')
def index():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>IPTV 抓取管理后台</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f7f6; padding: 40px; text-align: center; }
            .container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); max-width: 600px; margin: auto; }
            .btn { display: inline-block; margin: 10px; padding: 12px 24px; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }
            .btn-blue { background-color: #007bff; }
            .btn-green { background-color: #28a745; }
            .status-box { background: #e9ecef; padding: 15px; border-radius: 8px; margin: 20px 0; text-align: left; font-size: 14px; }
            .status-box span { font-weight: bold; color: #333; }
            .action-text { color: #d63384; font-weight: bold; }
        </style>
        <meta http-equiv="refresh" content="10">
    </head>
    <body>
        <div class="container">
            <h2>IPTV 自动化抓取系统</h2>
            <div class="status-box">
                <p>🕒 最后更新时间: <span>{{ last_update }}</span></p>
                <p>📡 爬虫当前动作: <span class="action-text">{{ status.current_action }}</span></p>
                <hr>
                <p>🔍 发现总比赛数: <span>{{ status.total_matches }}</span> 场</p>
                <p>⏳ 在时间范围内: <span>{{ status.in_time_matches }}</span> 场 (±12小时)</p>
                <p>✅ 成功解密线路: <span style="color: #198754; font-size: 18px;">{{ status.success_lines }}</span> 条</p>
            </div>
            <div>
                <a href="/m3u" class="btn btn-blue">获取 M3U 订阅</a>
                <a href="/txt" class="btn btn-green">获取 TXT 订阅</a>
            </div>
            <p style="font-size: 12px; color: #888; margin-top: 20px;">遇到了顽固死链？使用排障工具：<br><code>/debug?url=网页链接</code></p>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, last_update=last_update_time, status=crawler_status)

@app.route('/m3u')
def get_m3u():
    try: return send_file(OUTPUT_M3U_FILE, mimetype='application/vnd.apple.mpegurl', as_attachment=False)
    except FileNotFoundError: return "文件尚未生成，请稍后再试", 404

@app.route('/txt')
def get_txt():
    try: return send_file(OUTPUT_TXT_FILE, mimetype='text/plain', as_attachment=False)
    except FileNotFoundError: return "文件尚未生成，请稍后再试", 404

@app.route('/debug')
def debug_url():
    target_url = request.args.get('url')
    if not target_url: return jsonify({"error": "请提供 url"}), 400
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
    threading.Thread(target=generate_playlist, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=80)
