"""Test bootstrap.

`app.settings` builds a module-level `Settings()` at import time with several required
fields. These are set before any test module is imported so the package can be imported
without depending on a developer's local environment.
"""

import os

_TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "000000:test-bot-token",
    "TELEGRAM_CHAT_ID": "-1001111111111",
    "MINIO_ROOT_USER": "test-minio-user",
    "MINIO_ROOT_PASSWORD": "test-minio-password",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)
