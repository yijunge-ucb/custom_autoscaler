FROM python:3.9-slim
RUN pip install flask
COPY main.py /app/main.py
WORKDIR /app
CMD ["python", "main.py"]
