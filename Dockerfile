ARG TERRAFORM_VERSION=1.14.1
FROM hashicorp/terraform:${TERRAFORM_VERSION} AS terraform

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY skills ./skills
RUN pip install --no-cache-dir ".[azure,github]" \
    && useradd --uid 10001 --create-home worker \
    && mkdir -p /app/reports/audit /workspace \
    && chown -R worker:worker /app /workspace
USER worker
ENV PYTHONUNBUFFERED=1 \
    DRIFT_REPOS_CONFIG=config/repos.yaml \
    DRIFT_RISK_POLICY=config/risk-policy.yaml \
    DRIFT_SKILL_PATH=skills/terraform-drift/SKILL.md \
    DRIFT_AUDIT_DIR=/app/reports/audit \
    DRIFT_DRY_RUN=true
ENTRYPOINT ["drift-agent"]
CMD ["--help"]
