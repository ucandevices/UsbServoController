# MCP server (tools/servo_mcp.py) in a container.
# No USB access here, so it runs against the built-in emulator (--mock).
# To drive a real board, run servo_mcp.py on the host instead, or pass the
# device through: docker run -i --device /dev/ttyACM0 IMAGE --port /dev/ttyACM0
FROM python:3.12-slim

WORKDIR /app
COPY tools/requirements.txt tools/
RUN pip install --no-cache-dir -r tools/requirements.txt
COPY tools/ tools/

ENTRYPOINT ["python", "tools/servo_mcp.py"]
CMD ["--mock"]
