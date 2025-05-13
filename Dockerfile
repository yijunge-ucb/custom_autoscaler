FROM python:3.9-slim
RUN pip install flask requests
COPY main.py /app/main.py
WORKDIR /app
CMD ["python", "main.py"]
