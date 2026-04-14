# common_paths.py
from pathlib import Path

BASE_DIR = Path("/srv/students")

def app_root(term, student_id, app_name):
    return BASE_DIR / term / student_id / app_name

def public_dir(term, student_id, app_name):
    return app_root(term, student_id, app_name) / "public"

def service_name(app_type, term, student_id, app_name):
    return f"{app_type}_{term}_{student_id}_{app_name}"
