# Flow Source — The Powerhouse Backend ⚙️

Flow Source is the high-performance engine that powers the Flow ecosystem. Built with FastAPI and SQLite, it provides a secure, self-hosted platform for streaming your favorite music with zero tracking and full control.

---

## 🛠️ Key Features

- **🚀 Instant Streaming:** A high-performance audio proxy using `yt-dlp` and `httpx` with support for HTTP Range requests for instant seeking.
- **🔐 Secure Authentication:** JWT-based user management for secure access.
- **🎵 Deep YT Music Integration:** Custom account linking to fetch your personalized library, playlists, and history.
- **🖼️ Image Proxying:** Built-in image proxy to bypass cross-origin restrictions for high-quality album art.
- **📦 Containerized Ready:** Full Docker support with Docker Compose for one-click deployments.
- **🚇 Seamless Access:** Integrated Cloudflare Tunnel for secure, remote access without complex networking.

---

## 🏗️ Architecture Overview

The backend is structured for scalability and clarity:

- `app/main.py`: The entry point for the FastAPI application.
- `app/routes.py`: RESTful endpoints for Auth, Home, Library, Search, and Playback.
- `app/services.py`: Business logic, including `yt-dlp` streaming and account linking.
- `app/models.py`: Database schemas for Users, Account credentials, and Metadata.
- `app/database.py`: SQLAlchemy engine and session management.

---

## 🚀 Deployment Guide

### Option 1: Docker Compose (Recommended)

The easiest way to run Flow Source is using Docker. This ensures all dependencies (SQLite, Python 3.12, ffmpeg) are correctly configured.

1.  **Clone and Configure:**
    ```bash
    cd flow-source
    # Ensure your .env file is set up with a strong SECRET_KEY
    ```

2.  **Launch the Services:**
    ```bash
    docker-compose up -d
    ```

3.  **Find your Remote URL:**
    Flow Source automatically starts a **Cloudflare Tunnel**. To find the public URL:
    ```bash
    docker-compose logs -f tunnel
    ```
    Wait for a line like: `https://[random-subdomain].trycloudflare.com`. Use this URL in your app's `.env` configuration.

### Option 2: Manual Setup

If you prefer to run manually for development:

1.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Initialize the Database:**
    ```bash
    python manage.py create
    python manage.py seed
    ```

3.  **Run the Server:**
    ```bash
    python run.py
    ```

---

## 📋 Common Management Commands

Use `manage.py` for common database and administrative tasks:

- `python manage.py create`: Initialize the PostgreSQL database schema.
- `python manage.py seed`: Populate the database with initial roles and an admin user.
- `python manage.py drop`: (Caution) Drop all database tables.

---

## 🌍 API Documentation

Once the server is running, you can access the interactive Swagger documentation at:
`http://localhost:8000/docs`

For a pre-configured testing environment, import the Postman collection:
`Flow_v1_Postman_Collection.json`

---

## 🛡️ Security Considerations

- **SECRET_KEY:** Always change the `SECRET_KEY` in your `.env` file for production.
- **HTTPS:** When using the Cloudflare Tunnel, your connection is end-to-end encrypted.
- **Data Privacy:** Your YouTube Music credentials are stored in your private database and are never shared.

---

<p align="center">Empower your listening. 🎧</p>
