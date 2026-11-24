FROM python:3.12-slim
WORKDIR /app
COPY avatar/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY avatar/avatar_service ./avatar_service
COPY avatar/avatar_contract ./avatar_contract
COPY gateway.py cards.py actions.py bindings.py ./
RUN groupadd --gid 10000 app && useradd --uid 10000 --gid app app && install -d -o app -g app /data
USER app
ENV AVATAR2_DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1
CMD ["python","-m","uvicorn","gateway:create_app","--factory","--host","0.0.0.0","--port","8790"]

