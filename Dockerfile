FROM python:3.6-alpine

WORKDIR /locust

COPY . .

RUN apk --update add python-dev libzmq zeromq-dev musl-dev gcc \
    && python setup.py install \
    && apk del musl-dev gcc zeromq-dev python-dev
