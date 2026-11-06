from io import BytesIO

import brotli
from urllib3.response import HTTPResponse


def test_brotli_response_decompression():
    payload = b"Synthetic archived response. " * 100
    response = HTTPResponse(
        body=BytesIO(brotli.compress(payload)),
        headers={"Content-Encoding": "br"},
        preload_content=False,
    )
    with response:
        assert response.read() == payload
