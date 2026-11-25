FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PUBLIC_SHOWCASE=1

WORKDIR /app

COPY requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY config.py models.py data_loader.py report.py smc_bot.py strategy_arena.py download_mt5_data.py live_mt5_bot.py market_feed.py terminal_data.py terminal_server.py arena_4y_results.csv ./
COPY web ./web

EXPOSE 8080

CMD ["python", "terminal_server.py", "--host", "0.0.0.0", "--public-showcase"]
