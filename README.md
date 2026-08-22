# AI Revenue Recovery Agent

## Overview
An AI-powered system to help merchants recover revenue at risk from failed payments and checkout abandonment.

## Technology Stack
- **Backend**: Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x (synchronous), Alembic, PostgreSQL, psycopg 3, httpx
- **Testing**: pytest, Ruff
- **AI**: Provider-agnostic abstraction with structured Pydantic outputs
- **Payment**: Razorpay Test Mode, Payment Links
- **Infra**: No Docker, no Redis, no Celery initially

## Local Development (Windows)

### Prerequisites
- Python 3.12+ installed
- Neon PostgreSQL database (cloud, no local installation needed)
- Git

### Setup Steps

1. **Clone / open the project**
   ```
   cd C:\Users\<YourUser>\Desktop\revenue-recovery
   ```

2. **Create a virtual environment**
   ```
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```
   pip install -r pyproject.toml
   ```
   (or `pip install fastapi uvicorn pydantic pydantic-settings "sqlalchemy>=2.0" "psycopg[binary]" httpx pytest ruff`)

4. **Configure environment variables**
   ```
   copy .env.example .env
   ```
   Edit `.env` and set your Neon PostgreSQL `DATABASE_URL`.

   ```
    DATABASE_URL=postgresql+psycopg://username:password@ep-name.region.postgres.neon.tech/database_name?sslmode=require
   APP_ENV=development
   DEBUG=True
   ```

5. **Verify the application starts**
   ```
   python -m app.main
   ```
   Visit `http://127.0.0.1:8000/health` to confirm the health endpoint works.

6. **Run the tests**
   ```
   pytest
   ```

7. **Run the linter**
   ```
   ruff check .
   ruff format .
   ```

### Database
- No local PostgreSQL installation needed — uses Neon (cloud PostgreSQL)
- Connection string comes from `DATABASE_URL` environment variable
- SQLAlchemy 2.x synchronous with psycopg 3
- Alembic migrations managed separately

### Security
- `.env` is git-ignored (see `.gitignore`)
- Never commit real database credentials
- Currency values stored as integer paise (minor units), not floats
- All timestamps stored in UTC

### Project Structure
```
revenue-recovery/
├── app/
│   ├── main.py          # FastAPI application with /health
│   ├── config.py        # pydantic-settings configuration
│   ├── database.py      # SQLAlchemy sync engine/session
│   └── ... (other packages)
├── tests/               # pytest tests
├── migrations/          # Alembic migrations
├── .env.example         # Environment variable templates
├── .env                 # Local overrides (git-ignored)
├── pyproject.toml       # Dependencies and configs
└── README.md            # This file
```

### Default Port
- FastAPI runs on `http://127.0.0.1:8000` by default

## License
MIT