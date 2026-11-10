FROM python:3.12-alpine
RUN adduser -D -u 10001 app
COPY app.py /app/app.py
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-u", "/app/app.py"]
