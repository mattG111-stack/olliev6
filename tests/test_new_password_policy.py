import pytest
from fastapi import HTTPException
from password_policy import validate_new_password

@pytest.mark.parametrize("value", ["Abcdef1", "ABCDEf9", "1Abcdef", "Abcdef123!", "Abcdefgh1"])
def test_accepts_requested_policy(value):
    validate_new_password(value)

@pytest.mark.parametrize("value", ["abcdef1", "Abcdefg", "Abcde1", "ABC1", "", "1234567", "abcDEF!"])
def test_rejects_missing_requirement(value):
    with pytest.raises(HTTPException) as error:
        validate_new_password(value)
    assert error.value.status_code == 422
