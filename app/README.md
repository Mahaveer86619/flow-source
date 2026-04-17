# Flow Source - Application Logic (`app`)

This directory contains the Python FastAPI application and its core logic.

## 📂 Components

- **[`main.py`](./main.py):** FastAPI initialization, middleware (CORS), and router inclusion.
- **[`routes.py`](./routes.py):** All RESTful endpoints organized by tags (Auth, Home, Search, Library, Stream).
- **[`services.py`](./services.py):** Complex business logic, including `yt-dlp` streaming and account linking.
- **[`models.py`](./models.py):** SQLAlchemy database models (Users, Credentials, Metadata).
- **[`database.py`](./database.py):** Database engine setup and session dependency management.
- **[`utils.py`](./utils.py):** Helper functions for authentication, password hashing, and token generation.
- **[`config.py`](./config.py):** Environment-based configuration (SECRET_KEY, DATABASE_URL).

## 🚀 Key Workflows

- **Streaming:** The `/v1/stream/{id}` endpoint uses a streaming response from `yt-dlp` for on-demand playback.
- **Account Linking:** Users can submit their YouTube Music headers to link their account for private library access.
- **Caching:** Personalized home shelves are cached server-side to minimize metadata fetching latency.
