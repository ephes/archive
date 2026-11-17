"""Gunicorn settings read automatically from the server's working directory (src/django).

The deployed command passes only the bind address and the worker count. With
gunicorn's default sync worker, one process serves one request at a time: a slow
client reading a public audio file holds the whole site, and any response that
streams longer than ``timeout`` (30 s) is killed mid-body. Threaded workers keep
serving other requests and are supervised by their main loop, so long downloads
complete. Because a busy thread is no longer recycled by the worker timeout,
``config/wsgi.py`` puts a stall timeout on every client socket. Command-line options
still take precedence over this file.
"""

worker_class = "gthread"
threads = 8
