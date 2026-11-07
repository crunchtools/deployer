# Two stages. The runtime image carries no package manager, no podman binary
# and no systemctl: the process talks to the podman REST API over its unix
# socket and to systemd over D-Bus. That is not a size optimisation, it is the
# capability boundary -- see src/deployer/__init__.py.
#
# journalctl is the one exception and has to be a real binary, because there is
# no D-Bus route to the journal. It is staged out of the builder with its
# libraries, the same way mcp-podman does it.

FROM quay.io/hummingbird/python:3.14-fips-builder AS builder
USER 0
RUN dnf install -y --setopt=install_weak_deps=False systemd && dnf clean all
WORKDIR /app
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
RUN mkdir -p /staging/usr/bin /staging/usr/lib64 && \
    cp /usr/bin/journalctl /staging/usr/bin/ && \
    ldd /usr/bin/journalctl | \
    grep "=> /" | awk '{print $3}' | sort -u | \
    xargs -I{} cp -L {} /staging/usr/lib64/ 2>/dev/null || true && \
    find /usr/lib64/systemd -name 'libsystemd-shared*' -exec cp {} /staging/usr/lib64/ \;

FROM quay.io/hummingbird/python:3.14-fips

LABEL name="deployer" \
      version="0.0.0" \
      summary="Deterministic deploy robot for the crunchtools fleet" \
      maintainer="fatherlinux <scott.mccarty@crunchtools.com>" \
      description="Drains the RT Deploy queue, deploys by commit, verifies, and rolls back once. No model runs in the deploy path." \
      org.opencontainers.image.source="https://github.com/crunchtools/deployer" \
      org.opencontainers.image.description="Deterministic deploy robot: RT Deploy queue in, verified deploy out" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

COPY --from=builder /app/venv /app/venv
COPY --from=builder /staging/usr/bin/ /usr/bin/
COPY --from=builder /staging/usr/lib64/ /usr/lib64/
ENV PATH="/app/venv/bin:$PATH"

# A oneshot, not a server: nothing is exposed and nothing listens. The unit
# starts it, it drains the queue, it exits.
ENTRYPOINT ["python", "-m", "deployer"]
CMD ["run"]
