from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path


test_db = Path(tempfile.gettempdir()) / f"amnezia-service-{uuid.uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{test_db.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-with-at-least-thirty-two-characters"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "strong-test-admin-password"
os.environ["TRUSTED_HOSTS"] = '["testserver", "localhost"]'
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["VPN_BACKEND"] = "mock"

