import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
FIXTURES = Path(__file__).parent / "fixtures"


def free_port(kind=socket.SOCK_DGRAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def settings(tmp_path):
    from telerouter.settings import Settings
    return Settings(data_dir=str(tmp_path), https_port=free_port(socket.SOCK_STREAM),
                    internal_port=free_port(socket.SOCK_STREAM), sip_port=free_port(),
                    rtp_port_min=33000, rtp_port_max=33099, advertise_ip="127.0.0.1",
                    whisper_uri="tcp://127.0.0.1:9", piper_uri="tcp://127.0.0.1:9", wake_uri="tcp://127.0.0.1:9",
                    setup_code="ABCD-EFGH")
