# /home/appuser/students_deployer/wsgi_prefix.py
import os
import importlib
from typing import Callable, Iterable

def _load_app(import_str: str):
    # import_str: "app:app" のような形式
    mod_name, obj_name = import_str.split(":", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, obj_name)

class PrefixMiddleware:
    """
    Nginxから渡された X-Forwarded-Prefix を SCRIPT_NAME に反映し、
    PATH_INFO から prefix を剥がす。
    """
    def __init__(self, app: Callable, header_name: str = "HTTP_X_FORWARDED_PREFIX"):
        self.app = app
        self.header_name = header_name

    def __call__(self, environ, start_response):
        prefix = environ.get(self.header_name, "")
        if prefix:
            # 末尾スラッシュ無しに正規化
            prefix = prefix.rstrip("/")
            path = environ.get("PATH_INFO", "")

            # PATH_INFO が prefix で始まるなら剥がす
            if path.startswith(prefix):
                environ["SCRIPT_NAME"] = prefix
                new_path = path[len(prefix):]
                environ["PATH_INFO"] = new_path if new_path else "/"
            else:
                # 念のため SCRIPT_NAME だけセット（PATH_INFO不一致時は剥がさない）
                environ["SCRIPT_NAME"] = prefix

        return self.app(environ, start_response)

# どの受講者アプリを読み込むか（systemd の Environment で指定）
APP_IMPORT = os.environ.get("APP_IMPORT", "app:app")
_inner = _load_app(APP_IMPORT)

# X-Forwarded-Prefix は WSGI環境では HTTP_X_FORWARDED_PREFIX になる
app = PrefixMiddleware(_inner, header_name="HTTP_X_FORWARDED_PREFIX")
