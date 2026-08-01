FROM eclipse-temurin:21-jre

RUN apt-get update && \
    apt-get install -y \
        wget \
        unzip \
        python3 \
        python3-pip \
	poppler-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN wget https://github.com/Audiveris/audiveris/releases/download/5.10.2/Audiveris-5.10.2-ubuntu24.04-x86_64.deb \
 && dpkg-deb -x Audiveris-5.10.2-ubuntu24.04-x86_64.deb /opt/audiveris

ENV AUDIVERIS_HOME=/opt/Audiveris

COPY requirements.txt .

RUN apt-get update && \
    apt-get install -y python3 python3-pip python3-venv

RUN python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

COPY app /app

WORKDIR /app

EXPOSE 8080

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8080"]
