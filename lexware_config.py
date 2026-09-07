"""Local Lexware credential lookup without exposing or validating the secret."""
import os


def configured_api_key(secrets=None, environ=None):
    """Return (key, source); a manual UI value can still override an empty result."""
    environ = os.environ if environ is None else environ
    if secrets is None:
        try:
            import streamlit as st
            secrets = st.secrets
        except Exception:
            secrets = {}

    for section_name in ('lexware', 'lexoffice'):
        try:
            section = secrets[section_name]
        except (KeyError, TypeError):
            continue
        for field_name in ('api_key', 'token', 'access_token'):
            try:
                value = section[field_name]
            except (KeyError, TypeError):
                continue
            if isinstance(value, str) and value.strip():
                return value.strip(), f'st.secrets["{section_name}"]["{field_name}"]'

    for variable in ('LEXWARE_API_KEY', 'LEXOFFICE_API_KEY'):
        value = environ.get(variable, '')
        if isinstance(value, str) and value.strip():
            return value.strip(), variable
    return '', ''
