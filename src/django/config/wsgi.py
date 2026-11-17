import os

from django.core.wsgi import get_wsgi_application

from config.client_timeouts import with_client_send_timeout

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = with_client_send_timeout(get_wsgi_application())
