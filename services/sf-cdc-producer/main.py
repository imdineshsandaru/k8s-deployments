import asyncio
import json
import logging
import os
import signal

from aiosfstream import SalesforceStreamingClient, ReplayOption
from confluent_kafka import Producer

LOG = logging.getLogger(__name__)

SF_CHANNEL = os.environ.get("SF_CHANNEL", "/data/AccountChangeEvent")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sf.AccountChangeEvent")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "cdc-kafka-kafka-bootstrap.kafka.svc.cluster.local:9092")
RETRY_DELAY = int(os.environ.get("RETRY_DELAY_SECONDS", "10"))

SF_CLIENT_ID = os.environ["SF_CLIENT_ID"]
SF_CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
SF_USERNAME = os.environ["SF_USERNAME"]
SF_PASSWORD = os.environ["SF_PASSWORD"]

_shutdown = asyncio.Event()


def _on_delivery(err, msg):
    if err:
        LOG.error("Delivery failed [%s]: %s", msg.topic(), err)
    else:
        LOG.debug("Delivered to %s [%d] @ %d", msg.topic(), msg.partition(), msg.offset())


async def run(producer: Producer) -> None:
    while not _shutdown.is_set():
        try:
            async with SalesforceStreamingClient(
                consumer_key=SF_CLIENT_ID,
                consumer_secret=SF_CLIENT_SECRET,
                username=SF_USERNAME,
                password=SF_PASSWORD,
                replay=ReplayOption.NEW_EVENTS,
            ) as client:
                await client.subscribe(SF_CHANNEL)
                LOG.info("Subscribed to %s, publishing to Kafka topic %s", SF_CHANNEL, KAFKA_TOPIC)

                async for message in client:
                    value = json.dumps(message["data"]).encode()
                    producer.produce(KAFKA_TOPIC, value=value, on_delivery=_on_delivery)
                    producer.poll(0)

        except Exception as exc:
            if _shutdown.is_set():
                break
            LOG.error("Connection error (%s), retrying in %ds", exc, RETRY_DELAY)
            await asyncio.sleep(RETRY_DELAY)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)

    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP})
    try:
        loop.run_until_complete(run(producer))
    finally:
        producer.flush(timeout=10)
        loop.close()
        LOG.info("Shutdown complete")


if __name__ == "__main__":
    main()
