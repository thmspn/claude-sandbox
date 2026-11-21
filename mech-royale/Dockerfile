FROM python:3.13-slim
WORKDIR /bot
COPY bot.py walls.py world.py mechfight.py /bot/
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
