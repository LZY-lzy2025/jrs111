# 使用微软官方的 Playwright 镜像作为底层
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# 设置工作目录
WORKDIR /app

# 设置时区为亚洲/上海
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复制依赖配置并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制运行代码
COPY main.py .

# 暴露 Web 后台端口（默认 8080，可由 PORT 环境变量覆盖）
EXPOSE 8080

# 运行服务
CMD ["python", "-u", "main.py"]
