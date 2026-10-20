FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements-server.txt README.md LICENSE seed.py seed_server.py seed_client.py seed_postgres.py benchmark.py seed_agent.py seed_workspace.py workflow_benchmark.py ./
RUN pip install --no-cache-dir -r requirements-server.txt && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home seed \
    && mkdir /data && chown seed:seed /data
USER seed
ENV SEED_DATA=/data
EXPOSE 7331
CMD ["seed-server", "serve", "--host", "0.0.0.0", "--workers", "4"]
