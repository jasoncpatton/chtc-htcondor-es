FROM python:3.9-buster

ADD requirements.txt /.
RUN pip install -r requirements.txt

COPY htcondor_es/*.py /opt/es_push/lib/es_push/
RUN mv /opt/es_push/lib/es_push/es_push.py /run.py
ENV PYTHONPATH "${PYTHONPATH}:/opt/es_push/lib"

RUN mkdir -p /etc/condor && touch /etc/condor/condor_config

CMD ["/run.py"]
