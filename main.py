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
OUTPUT_FILE = "/app/output/playlist.m3u"
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
    # 方法 1：遍历页面的框架树 (Frames)
    for frame in page.frames:
        if 'paps.html?id=' in frame.url:
            return frame.url.split('paps.html?id=')[-1]

    # 方法 2：调用浏览器 Console 提取 Performance 资源树
    resource_urls = page.evaluate("""
        () => performance.getEntriesByType('resource').map(r => r.name)
    """)
    for url in resource_urls:
        if 'paps.html?id=' in url:
            return url.split('paps.html?id=')[-1]

    return None

def generate_m3u():
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

    try:
        with sync_playwright() as p:
            # 禁用沙盒，优化容器内运行表现
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = browser.new_context()
            page = context.new_page()
            
            for match in matches:
                try:
                    time_tag = match.find('li', class_='lab_time')
                    if not time_tag: continue
                    
                    # 解析时间并判断是否在前后 3 小时内
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

                    # 请求中间页，提取带有高清/蓝光的 data-play 路径
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

                    # 访问目标页，等待网络请求静默（确保资源文件已加载完成）
                    page.goto(final_url, wait_until="networkidle", timeout=15000)
                    
                    # 从资源树提取
                    token = extract_from_resource_tree(page)

                    if token:
                        # ⚠️ 注意：这里根据你实际使用的代理/播放器情况修改前面的域名
                        stream_url = f"http://YOUR_PROXY_SERVER_OR_DOMAIN/{token}" 
                        m3u_lines.append(f"#EXTINF:-1 tvg-name=\"{match_name}\",{match_name}\n")
                        m3u_lines.append(f"{stream_url}\n")
                        print(f"成功提取 Token: {token[:20]}...")

                except Exception as e:
                    print(f"解析单场比赛时出错: {e}")
                    continue
            
            browser.close()
    except Exception as e:
        print(f"Playwright 运行出错: {e}")

    # 写入 m3u 文件
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.writelines(m3u_lines)
    
    last_update_time = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    print("M3U 文件已更新完成。")

# --- Web 管理后台 (避免根目录 Not Found) ---
@app.route('/')
def index():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>IPTV M3U 抓取后台</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f7f6; padding: 40px; text-align: center; }
            .container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); max-width: 500px; margin: auto; }
            h2 { color: #333; }
            .btn { display: inline-block; margin-top: 20px; padding: 12px 24px; background-color: #007bff; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }
            .btn:hover { background-color: #0056b3; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>IPTV 自动化抓取系统</h2>
            <p>系统状态: <span style="color: green; font-weight: bold;">运行中 🟢</span></p>
            <p>最后更新时间: <strong>{{ last_update }}</strong></p>
            <a href="/m3u" class="btn">查看 / 下载 M3U</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, last_update=last_update_time)

@app.route('/m3u')
def get_m3u():
    try:
        # 提供 m3u 格式下载/在线读取
        return send_file(OUTPUT_FILE, mimetype='application/vnd.apple.mpegurl', as_attachment=False)
    except FileNotFoundError:
        return "M3U 文件尚未生成，爬虫可能正在运行，请稍后再试。", 404

def run_scheduler():
    # 每隔 1 小时执行一次抓取
    schedule.every(1).hours.do(generate_m3u)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    # 启动时立即执行一次抓取（开子线程，不阻塞 Web 服务）
    threading.Thread(target=generate_m3u, daemon=True).start()
    
    # 启动定时任务（子线程）
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    # 启动 Flask Web 管理端监听 80 端口
    app.run(host="0.0.0.0", port=80)
