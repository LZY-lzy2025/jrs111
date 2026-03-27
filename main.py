import re
import os
import time
import threading
import datetime
import pytz
import requests
import schedule
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from flask import Flask, send_file, render_template_string

# --- 配置区 ---
SOURCE_URL = "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5"
BASE_URL = "http://play.sportsteam368.com"
OUTPUT_M3U_FILE = "/app/output/playlist.m3u"
OUTPUT_TXT_FILE = "/app/output/playlist.txt"  # 新增 TXT 输出路径
# --------------

app = Flask(__name__)
last_update_time = "尚未更新"

def get_html_from_js(js_url):
    try:
        response = requests.get(js_url, timeout=10)
        response.encoding = 'utf-8'
        return "".join(re.findall(r"document\.write\('(.*?)'\);", response.text))
    except Exception as e:
        print(f"获取 JS 文件失败: {e}")
        return ""

def extract_from_resource_tree(page):
    """从网页加载完毕的资源树/框架路径中剥离真实 ID"""
    for frame in page.frames:
        if 'paps.html?id=' in frame.url:
            return frame.url.split('paps.html?id=')[-1]

    resource_urls = page.evaluate("""
        () => performance.getEntriesByType('resource').map(r => r.name)
    """)
    for url in resource_urls:
        if 'paps.html?id=' in url:
            return url.split('paps.html?id=')[-1]

    return None

def generate_playlist():
    global last_update_time
    print(f"[{datetime.datetime.now()}] 开始执行抓取任务...")
    
    html_content = get_html_from_js(SOURCE_URL)
    if not html_content:
        return

    soup = BeautifulSoup(html_content, 'html.parser')
    matches = soup.find_all('ul', class_='item play d-touch active')
    
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(tz)
    current_year = now.year
    
    m3u_lines = ["#EXTM3U\n"]
    txt_lines = []  # 新增用于存储 TXT 格式的列表

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = browser.new_context()
            page = context.new_page()
            
            # 为了让 txt 列表更好看，我们可以加个分类头
            txt_lines.append("体育直播,#genre#\n")

            for match in matches:
                try:
                    time_tag = match.find('li', class_='lab_time')
                    if not time_tag: continue
                    
                    match_time_str = f"{current_year}-{time_tag.text.strip()}"
                    match_dt = tz.localize(datetime.datetime.strptime(match_time_str, "%Y-%m-%d %H:%M"))
                    
                    time_diff = abs((match_dt - now).total_seconds()) / 3600
                    if time_diff > 3:
                        continue

                    home_team = match.find('li', class_='lab_team_home').find('strong').text.strip()
                    away_team = match.find('li', class_='lab_team_away').find('strong').text.strip()
                    match_name = f"{home_team} VS {away_team}"

                    channel_li = match.find('li', class_='lab_channel')
                    target_link = None
                    if channel_li:
                        for a_tag in channel_li.find_all('a', href=True):
                            if 'play.sportsteam368.com' in a_tag['href']:
                                target_link = a_tag['href']
                                break
                    
                    if not target_link: continue

                    detail_resp = requests.get(target_link, timeout=10)
                    detail_soup = BeautifulSoup(detail_resp.text, 'html.parser')
                    
                    play_path = None
                    for a in detail_soup.find_all('a', class_='item ok me'):
                        a_text = a.text.strip()
                        if '高清直播' in a_text or '蓝光' in a_text:
                            play_path = a.get('data-play')
                            break
                    
                    if not play_path: continue

                    final_url = f"{BASE_URL}{play_path}"
                    print(f"正在分析资源树: {final_url}")

                    page.goto(final_url, wait_until="networkidle", timeout=15000)
                    token = extract_from_resource_tree(page)

                    if token:
                        # ⚠️ 注意替换这里的域名前缀
                        stream_url = f"http://YOUR_PROXY_SERVER_OR_DOMAIN/{token}" 
                        
                        # 写入 M3U 格式
                        m3u_lines.append(f"#EXTINF:-1 tvg-name=\"{match_name}\",{match_name}\n")
                        m3u_lines.append(f"{stream_url}\n")
                        
                        # 写入 TXT 格式 (频道名称,链接)
                        txt_lines.append(f"{match_name},{stream_url}\n")
                        
                        print(f"成功提取: {match_name}")

                except Exception as e:
                    print(f"解析单场比赛时出错: {e}")
                    continue
            
            browser.close()
    except Exception as e:
        print(f"Playwright 运行出错: {e}")

    # 同时写入 M3U 和 TXT 文件
    os.makedirs(os.path.dirname(OUTPUT_M3U_FILE), exist_ok=True)
    
    with open(OUTPUT_M3U_FILE, 'w', encoding='utf-8') as f:
        f.writelines(m3u_lines)
        
    with open(OUTPUT_TXT_FILE, 'w', encoding='utf-8') as f:
        f.writelines(txt_lines)
    
    last_update_time = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    print("播放列表(M3U/TXT)已更新完成。")


# --- Web 管理后台路由 ---
@app.route('/')
def index():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>IPTV 抓取管理后台</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f7f6; padding: 40px; text-align: center; }
            .container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); max-width: 500px; margin: auto; }
            h2 { color: #333; }
            .btn { display: inline-block; margin: 10px; padding: 12px 24px; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }
            .btn-blue { background-color: #007bff; }
            .btn-blue:hover { background-color: #0056b3; }
            .btn-green { background-color: #28a745; }
            .btn-green:hover { background-color: #218838; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>IPTV 自动化抓取系统</h2>
            <p>系统状态: <span style="color: green; font-weight: bold;">运行中 🟢</span></p>
            <p>最后更新: <strong>{{ last_update }}</strong></p>
            <div style="margin-top: 20px;">
                <a href="/m3u" class="btn btn-blue">获取 M3U 订阅</a>
                <a href="/txt" class="btn btn-green">获取 TXT 订阅</a>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, last_update=last_update_time)

@app.route('/m3u')
def get_m3u():
    try:
        return send_file(OUTPUT_M3U_FILE, mimetype='application/vnd.apple.mpegurl', as_attachment=False)
    except FileNotFoundError:
        return "M3U 文件尚未生成，请稍后再试。", 404

# 新增的 TXT 接口路由
@app.route('/txt')
def get_txt():
    try:
        # 使用 text/plain 确保浏览器会直接显示文字，而不是强制下载（除非用户右键另存为）
        return send_file(OUTPUT_TXT_FILE, mimetype='text/plain', as_attachment=False)
    except FileNotFoundError:
        return "TXT 文件尚未生成，请稍后再试。", 404

def run_scheduler():
    schedule.every(1).hours.do(generate_playlist)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=generate_playlist, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=80)
