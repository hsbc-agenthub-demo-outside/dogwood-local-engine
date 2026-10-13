"""The framing, tested against a socketpair and nothing else.

No engine, no server, no filesystem: the framing is the piece that has to be right
before anything above it can be trusted, and it is small enough to test exhaustively
by hand. Every test here is a property of the wire format transcribed from
``dogwood-server/src/protocol.rs``.
"""

from __future__ import annotations

import json
import socket
import struct
import unittest

from dogwood_sdk.errors import DogwoodProtocolError, DogwoodTimeoutError, DogwoodTransportError
from dogwood_sdk.framing import MAX_FRAME, read_frame, write_frame


class FramingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.writer, self.reader = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.writer.close)
        self.addCleanup(self.reader.close)

    def send_raw(self, payload: bytes) -> None:
        self.writer.sendall(payload)

    def test_a_frame_round_trips(self) -> None:
        message = {"op": "submit", "event": {"action": "A::Action::Read", "kind": "request"}}
        write_frame(self.writer, message)
        self.assertEqual(read_frame(self.reader), message)

    def test_several_frames_in_a_row_stay_separate(self) -> None:
        # The engine serves many requests per connection, so the boundary between
        # frames must not depend on the payload's contents.
        write_frame(self.writer, {"n": 1})
        write_frame(self.writer, {"n": 2})
        write_frame(self.writer, {"n": 3})
        self.assertEqual([read_frame(self.reader) for _ in range(3)], [{"n": 1}, {"n": 2}, {"n": 3}])

    def test_the_length_header_is_big_endian(self) -> None:
        write_frame(self.writer, {"a": 1})
        header = self.reader.recv(4)
        self.assertEqual(struct.unpack(">I", header)[0], len(json.dumps({"a": 1}, separators=(",", ":"))))

    def test_a_frame_over_the_limit_is_refused_before_it_is_sent(self) -> None:
        with self.assertRaises(DogwoodProtocolError) as caught:
            write_frame(self.writer, {"pad": "x" * 64}, max_frame=32)
        self.assertIn("exceeds", str(caught.exception))

    def test_a_hostile_length_header_is_refused_without_allocating(self) -> None:
        # The declared length is never trusted enough to allocate against: this
        # header claims 4 GiB and must be rejected from the four bytes alone.
        self.send_raw(struct.pack(">I", 0xFFFFFFFF))
        with self.assertRaises(DogwoodProtocolError) as caught:
            read_frame(self.reader)
        self.assertIn(str(MAX_FRAME), str(caught.exception))

    def test_a_frame_that_stops_halfway_is_a_transport_error(self) -> None:
        self.send_raw(struct.pack(">I", 64) + b'{"partial":')
        self.writer.close()
        with self.assertRaises(DogwoodTransportError) as caught:
            read_frame(self.reader)
        self.assertIn("closed mid-frame", str(caught.exception))

    def test_a_body_that_is_not_json_is_a_protocol_error(self) -> None:
        body = b"not json at all"
        self.send_raw(struct.pack(">I", len(body)) + body)
        with self.assertRaises(DogwoodProtocolError):
            read_frame(self.reader)

    def test_json_that_is_not_an_object_is_a_protocol_error(self) -> None:
        # A bare array or a number would decode happily and then blow up as
        # `response["status"]` somewhere far from the cause.
        body = b"[1, 2, 3]"
        self.send_raw(struct.pack(">I", len(body)) + body)
        with self.assertRaises(DogwoodProtocolError) as caught:
            read_frame(self.reader)
        self.assertIn("not an object", str(caught.exception))

    def test_silence_past_the_deadline_is_a_timeout(self) -> None:
        self.reader.settimeout(0.05)
        with self.assertRaises(DogwoodTimeoutError):
            read_frame(self.reader)

    def test_a_timeout_is_also_a_transport_error(self) -> None:
        # So that one `except DogwoodTransportError` handles every "no answer".
        self.assertTrue(issubclass(DogwoodTimeoutError, DogwoodTransportError))
        self.assertTrue(issubclass(DogwoodProtocolError, DogwoodTransportError))


if __name__ == "__main__":
    unittest.main()
