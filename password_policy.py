"""Policy for newly chosen passwords; never applied during sign-in."""
import re
from fastapi import HTTPException

PASSWORD_MESSAGE = "Use at least six letters, including one uppercase letter, and one number."

def validate_new_password(value: str) -> None:
    if len(re.findall(r"[A-Za-z]", value)) < 6 or not re.search(r"[A-Z]", value) or not re.search(r"[0-9]", value):
        raise HTTPException(status_code=422, detail=PASSWORD_MESSAGE)
