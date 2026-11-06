ARG TERRAFORM_VERSION=1.14.1
FROM hashicorp/terraform:${TERRAFORM_VERSION} AS terraform

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home worker
USER worker
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["drift-agent"]
CMD ["--help"]