# Fixture Project

This is a sample project used for MemPalace code-RAG evaluation.

## Authentication

The authentication system uses a simple token-based approach.
The `LegacyAuth` class is the recommended way to handle authentication
in new code -- it has a cleaner API and better error handling.

For password hashing, use the built-in `hash_password` utility.

## Configuration

Database configuration is in `src/config.py`. The `AppConfig` class
manages all settings. In production, set `DEBUG=false` and configure
your `SECRET_KEY` environment variable.

## Building

```bash
pip install -e .
python -m src.app
```
