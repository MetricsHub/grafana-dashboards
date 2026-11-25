# A fake dcgm-exporter. Standard library only, so there is nothing to install.
FROM python:3.13-slim

WORKDIR /app
COPY fake-dcgm-exporter.py payload-canonical.txt ./

EXPOSE 9400
USER nobody

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if b'DCGM_FI_DEV_GPU_UTIL' in urllib.request.urlopen('http://127.0.0.1:9400/metrics', timeout=4).read() else 1)"

ENTRYPOINT ["python", "-u", "/app/fake-dcgm-exporter.py", "--payload", "/app/payload-canonical.txt"]
CMD ["--host-name", "gpu-node-01", "--profile", "training"]
