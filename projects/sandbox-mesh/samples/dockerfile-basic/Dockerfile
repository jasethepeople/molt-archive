FROM python:3.12-slim
WORKDIR /srv
COPY app.py /srv/app.py
USER 65532:65532
EXPOSE 8080
CMD ["python3", "/srv/app.py"]
