# The public Cloud Custodian c7n-org image, pinned to the exact tested tag
# (see ../c7n-custom/Dockerfile for the same pin, resolved to a digest there).
ARG C7N_ORG_IMAGE=docker.io/cloudcustodian/c7n-org:0.9.51.0

FROM ${C7N_ORG_IMAGE} AS plugin

LABEL org.opencontainers.image.title="c7n-org FinOps AMI+EBS extension (baked, sitecustomize)" \
      org.opencontainers.image.version="0.9.51.0-finops.2" \
      org.opencontainers.image.base.name="docker.io/cloudcustodian/c7n-org:0.9.51.0"

USER root
COPY --chown=root:root finops_c7n/ /opt/finops-c7n/finops_c7n/
ENV PYTHONPATH=/opt/finops-c7n

# This is the one meaningful difference from ../c7n-custom/Dockerfile: instead
# of a custom ENTRYPOINT (`python -m finops_c7n.bootstrap`) that every caller
# has to remember to use instead of the bare `c7n-org`/`custodian` binaries,
# sitecustomize.py is baked in at the exact path this image's Python resolves
# first (verified empirically in ../c7n-updated/ - /usr/lib/python3.12/
# sitecustomize.py wins over the venv's own site-packages copy). Python's
# interpreter auto-executes it at startup regardless of what command actually
# runs, so finops_c7n registers itself no matter how this image is invoked -
# the bare `c7n-org` command works directly, no wrapper, no "never call bare
# c7n-org" foot-gun. Best of both: a custom image (so the test stage below is
# a real pre-deploy gate again, unlike ../c7n-updated*/'s ConfigMap delivery)
# without reintroducing the ENTRYPOINT-bypass bug class bootstrap.py had.
COPY --chown=root:root sitecustomize.py /usr/lib/python3.12/sitecustomize.py

FROM plugin AS test

COPY --chown=root:root tests/ /opt/finops-c7n/tests/
COPY --chown=root:root examples/ /opt/finops-c7n/examples/
USER custodian

# Both run via the BARE binaries - proving the baked sitecustomize.py works,
# not just that the Python files are syntactically fine. Mirrors exactly what
# ../c7n-updated-cloudtrail/scripts/test-locally.sh proves against the plain
# upstream image + volume mounts; here it's proven at build time instead.
RUN /src/.venv/bin/python -m unittest discover \
      -s /opt/finops-c7n/tests -v \
    && c7n-org validate \
      -c /opt/finops-c7n/examples/accounts.yml \
      -u /opt/finops-c7n/examples/policies.yml \
    && touch /tmp/c7n-custom-verified

FROM plugin AS runtime

USER root
# This tiny artifact makes the runtime stage depend on the successful test stage.
COPY --from=test --chown=root:root /tmp/c7n-custom-verified /opt/finops-c7n/.verified

# Keep the same non-root runtime identity as the upstream image, and
# deliberately do NOT override ENTRYPOINT/CMD - sitecustomize.py makes that
# unnecessary, and leaving the upstream image's own entrypoint alone means
# `docker run <image> c7n-org run ...` (or a Kubernetes `command:` that also
# replaces ENTRYPOINT, same as it always does) both work correctly.
USER custodian
