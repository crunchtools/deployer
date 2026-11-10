# Two stages. The runtime image carries no package manager, no podman binary,
# no systemctl and no journalctl: the process talks to the podman REST API over
# its unix socket and to systemd over D-Bus, and reads a failing service's own
# output through podman's logs endpoint. That is not a size optimisation, it is
# the capability boundary -- see src/deployer/__init__.py.

FROM quay.io/hummingbird/python:3.14-fips-builder AS lock
USER 0
RUN pip install --no-cache-dir uv==0.13.7
WORKDIR /lock
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project \
      --format requirements-txt -o /requirements.txt

FROM quay.io/hummingbird/python:3.14-fips-builder AS builder
USER 0
WORKDIR /app
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY --from=lock /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

FROM quay.io/hummingbird/python:3.14-fips

LABEL name="deployer" \
      version="0.4.1" \
      summary="Deterministic deploy robot for the crunchtools fleet" \
      maintainer="fatherlinux <scott.mccarty@crunchtools.com>" \
      description="Drains the RT Deploy queue, deploys by commit, verifies, and rolls back once. No model runs in the deploy path." \
      org.opencontainers.image.source="https://github.com/crunchtools/deployer" \
      org.opencontainers.image.description="Deterministic deploy robot: RT Deploy queue in, verified deploy out" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# THE CLOUDFLARE ORIGIN CA ROOT, for an `https` probe with a `ca_file`.
#
# Shipped in the image because it is a public root, not a secret, and because a
# trust anchor a probe depends on should be versioned with the code that
# verifies against it rather than mounted from a host path that can change
# underneath a release.
#
# It is here so a reverse proxy can be probed for the certificate it is
# actually serving. Cloudflare terminates TLS publicly and talks to the origin
# with an Origin CA certificate, which is deliberately not in any public trust
# store, so without this bundle the only probe expressible against the proxy is
# a plain `:80` redirect check that would pass with TLS entirely broken.
#
# Verified rather than trusted on provenance: this file verifies the
# certificate gloval's proxy actually serves. `openssl verify -CAfile` against
# that live leaf returns OK, the leaf's issuer is this subject, and the subject
# matches the "CloudFlare Origin SSL Certificate Authority" Cloudflare
# publishes. sha256 91a8a5567efa6bf941162aa806b3ba476aaddf7867640e53053b35fb225a5dae,
# valid to 2029-08-15. Re-verify the same way before replacing it.
COPY trust/cloudflare-origin-ca.pem /etc/deployer/cloudflare-origin-ca.pem

# A oneshot, not a server: nothing is exposed and nothing listens. The unit
# starts it, it drains the queue, it exits.
ENTRYPOINT ["python", "-m", "deployer"]
CMD ["run"]
