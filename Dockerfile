FROM python:3.12-slim

# 設定台灣時區與無緩衝輸出
ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# 安裝時區必要套件
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 安裝 Python 相依套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案檔案
COPY . .

# 預設啟動常駐全天候監控 (自動 08:50 晨報、09:00~13:30 盤中掃描、14:30 盤後結算)
CMD ["python", "main.py"]
