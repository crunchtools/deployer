# Two stages. The runtime image carries no package manager, no podman binary,
# no systemctl and no journalctl: the process talks to the podman REST API over
# its unix socket and to systemd over D-Bus, and reads a failing service's own
# output through podman's logs endpoint. That is not a size optimisation, it is
# the capability boundary -- see src/deployer/__init__.py.

FROM quay.io/hummingbird/python:3.14-fips-builder AS builder
USER 0
WORKDIR /app
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

FROM quay.io/hummingbird/python:3.14-fips

LABEL name="deployer" \
      version="0.1.3" \
      summary="Deterministic deploy robot for the crunchtools fleet" \
      maintainer="fatherlinux <scott.mccarty@crunchtools.com>" \
      description="Drains the RT Deploy queue, deploys by commit, verifies, and rolls back once. No model runs in the deploy path." \
      org.opencontainers.image.source="https://github.com/crunchtools/deployer" \
      org.opencontainers.image.description="Deterministic deploy robot: RT Deploy queue in, verified deploy out" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# A oneshot, not a server: nothing is exposed and nothing listens. The unit
# starts it, it drains the queue, it exits.
ENTRYPOINT ["python", "-m", "deployer"]
CMD ["run"]
