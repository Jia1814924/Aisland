"""IMAP TLS checks run in a child process.

test_maintenance asserts PySide6 is absent from the main test process, so any
module that imports aisland (and therefore Qt) must be loaded out of process.
"""
from __future__ import annotations

import socket
import ssl
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "imap_self_signed.pem"

from aisland.services import mailcheck


class ImapTlsChildTests(unittest.TestCase):
    def test_verified_context(self):
        fake = mock.MagicMock()
        fake.uid.return_value = ("OK", [b""])
        with mock.patch.dict(mailcheck.imaplib.Commands, {}, clear=False), \
                mock.patch.object(mailcheck.imaplib, "IMAP4_SSL", return_value=fake) as ctor:
            result = mailcheck.MailCheckService._unseen(
                {"host": "imap.example.com", "port": 993, "user": "u", "password": "p"})
        self.assertEqual(result, set())
        context = ctor.call_args.kwargs.get("ssl_context")
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        # The library default is unverified; the fix must not fall back to it.
        self.assertFalse(ssl._create_stdlib_context().check_hostname)

    def test_self_signed_rejected(self):
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(FIXTURE))
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        port = listener.getsockname()[1]

        def serve():
            try:
                conn, _ = listener.accept()
                try:
                    with server_context.wrap_socket(conn, server_side=True) as tls:
                        tls.settimeout(1)
                        tls.recv(1024)
                except (OSError, ssl.SSLError):
                    pass
                finally:
                    conn.close()
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                mailcheck.MailCheckService._unseen(
                    {"host": "127.0.0.1", "port": port, "user": "u", "password": "p"})
        finally:
            thread.join(timeout=10)
            listener.close()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], "-v", *sys.argv[1:]])
