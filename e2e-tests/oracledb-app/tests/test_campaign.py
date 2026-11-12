import socket
import threading

import pytest

from campaign import validate_report
from relay import Relay


def report():
    return {"profile": "smoke", "status": "passed", "campaign_id": "new", "harness_revision": "revision",
            "source_sha256": "source", "wheel_sha256": "wheel",
            "cases": [{"id": "E0"+str(i), "status": "passed"} for i in range(1,6)]}


@pytest.mark.parametrize("change", [{"campaign_id":"historical"}, {"harness_revision":"changed"},
                                    {"source_sha256":"other"}, {"wheel_sha256":"other"},
                                    {"coverage":"selected"}, {"cases": []}])
def test_campaign_rejects_historical_changed_partial_or_missing_evidence(change):
    candidate = report()
    candidate.update(change)
    with pytest.raises(AssertionError):
        validate_report(candidate,"smoke","new","revision","source","wheel")


def test_complete_matching_campaign_report_is_accepted():
    validate_report(report(),"smoke","new","revision","source","wheel")


def test_relay_forwards_then_withholds_actual_reply_and_closes_threads():
    server = socket.socket()
    server.bind(("127.0.0.1",0))
    server.listen(1)
    def echo():
        peer,_ = server.accept()
        with peer:
            while True:
                data = peer.recv(1024)
                if not data:
                    break
                peer.sendall(data)
    thread = threading.Thread(target=echo,daemon=True)
    thread.start()
    try:
        with Relay("127.0.0.1",server.getsockname()[1]) as relay:
            with socket.create_connection(("127.0.0.1",relay.port),timeout=1) as client:
                client.sendall(b"first")
                assert client.recv(5)==b"first"
                relay.hold_responses.set()
                client.sendall(b"held")
                client.settimeout(.1)
                with pytest.raises(socket.timeout):
                    client.recv(4)
                relay.cut()
                assert client.recv(4)==b""
        assert relay.bytes["held_server"] >= 4
    finally:
        server.close()
        thread.join(timeout=2)
    assert not thread.is_alive()
